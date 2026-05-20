"""
Plan tree executor.

Walks a PlanTree in topological (dependency-respecting) order, resolving
``$step_id`` variable references and calling ifcopenshell skills through
the SkillRegistry.

Supports three execution patterns:
  - **Scalar**: call a skill once with literal params.
  - **Batch** (``$ref.each.*``): iterate over a list output from a prior step
    and call the skill once per item.  Batches with ≥4 items use
    ThreadPoolExecutor for parallel execution.
  - **Reference** (``$ref`` or ``$ref.field``): substitute a prior step's
    output (or a field of it) into the params.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ifc_agent.planner import PlanNode, PlanTree

logger = logging.getLogger(__name__)

PARALLEL_BATCH_THRESHOLD = 4
MAX_BATCH_WORKERS = 8

# Regex for variable references: $step_id or $step_id.field or $step_id.each.field
_VAR_RE = re.compile(r'^\$(\w+?)(?:\.each\.(\w+))?(?:\.(\w+))?$')


class ExecutionResult:
    """Accumulated results from executing a plan tree."""

    def __init__(self):
        self.context: dict[str, Any] = {}
        self.log: list[dict] = []
        self.errors: list[dict] = []

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        ok = [r for r in self.log if r["status"] == "success"]
        fail = [r for r in self.log if r["status"] == "error"]
        lines = [f"Executed {len(self.log)} steps: {len(ok)} succeeded, {len(fail)} failed."]
        for r in self.log:
            symbol = "OK" if r["status"] == "success" else "FAIL"
            lines.append(f"  [{symbol}] {r['step_id']}: {r['description']}")
            if r["status"] == "error":
                lines.append(f"         Error: {r['error']}")
        return "\n".join(lines)

    def to_dict(self) -> list[dict]:
        return list(self.log)


class TreeExecutor:
    """Execute a PlanTree using a SkillRegistry."""

    def __init__(self, skill_registry):
        self.registry = skill_registry
        self._entity_params = self._build_entity_param_index()

    def _build_entity_param_index(self) -> dict[str, set[str]]:
        """Pre-compute which parameters of each API skill expect entity_ref."""
        index: dict[str, set[str]] = {}
        for path, skill in self.registry.skills.items():
            entity_params = {p.name for p in skill.params if p.is_entity}
            if entity_params:
                index[path] = entity_params
        return index

    def execute(self, plan: PlanTree) -> ExecutionResult:
        result = ExecutionResult()
        ordered = plan.topological_order()
        logger.info("Executing plan with %d steps", len(ordered))

        for node in ordered:
            self._execute_node(node, result)

        return result

    def _execute_node(self, node: PlanNode, result: ExecutionResult):
        """Execute a single plan node."""
        try:
            is_batch, batch_ref_id, batch_field = self._detect_batch(node)

            if is_batch:
                self._execute_batch(node, result, batch_ref_id, batch_field)
            else:
                resolved = self._resolve_params(node.params, result.context)
                resolved = self._coerce_entity_params(node.skill_path, resolved)
                output = self.registry.call(node.skill_path, **resolved)
                result.context[node.step_id] = output
                if node.output_var:
                    result.context[node.output_var] = output
                result.log.append({
                    "step_id": node.step_id,
                    "skill": node.skill_path,
                    "description": node.description,
                    "status": "success",
                    "result": _safe_str(output),
                })
                logger.info("[OK] %s: %s → %s",
                            node.step_id, node.description, _safe_str(output)[:200])

        except Exception as exc:
            err_msg = str(exc)
            result.context[node.step_id] = None
            result.log.append({
                "step_id": node.step_id,
                "skill": node.skill_path,
                "description": node.description,
                "status": "error",
                "error": err_msg,
            })
            result.errors.append({"step_id": node.step_id, "error": err_msg})
            logger.error("[FAIL] %s: %s — %s", node.step_id, node.description, err_msg)

    # ------------------------------------------------------------------
    # Batch execution  ($ref.each.field)
    # ------------------------------------------------------------------

    def _detect_batch(self, node: PlanNode) -> tuple[bool, str, str]:
        """Check if any param uses $ref.each.field syntax."""
        for val in node.params.values():
            if isinstance(val, str):
                m = re.match(r'^\$(\w+)\.each\.(\w+)$', val)
                if m:
                    return True, m.group(1), m.group(2)
        return False, "", ""

    _WRITE_SKILLS = frozenset({
        "root.remove_product", "root.create_entity",
        "composite.delete_with_openings",
        "attribute.edit_attributes", "spatial.assign_container",
        "spatial.unassign_container",
    })

    def _is_read_only_skill(self, skill_path: str) -> bool:
        return skill_path not in self._WRITE_SKILLS

    def _execute_batch(
        self, node: PlanNode, result: ExecutionResult,
        batch_ref_id: str, batch_field: str,
    ):
        """Iterate over a list from a prior step and call the skill per item.

        Read-only batch operations with ≥PARALLEL_BATCH_THRESHOLD items
        use ThreadPoolExecutor for parallel execution.
        """
        source = result.context.get(batch_ref_id, [])
        if not isinstance(source, list):
            source = [source]

        batch_param_key = None
        for k, v in node.params.items():
            if isinstance(v, str) and "$" in v and ".each." in v:
                batch_param_key = k
                break

        use_parallel = (
            self._is_read_only_skill(node.skill_path)
            and len(source) >= PARALLEL_BATCH_THRESHOLD
        )

        if use_parallel:
            batch_results, ok_count, fail_count = self._execute_batch_parallel(
                node, result, source, batch_field, batch_param_key,
            )
        else:
            batch_results, ok_count, fail_count = self._execute_batch_sequential(
                node, result, source, batch_field, batch_param_key,
            )

        result.context[node.step_id] = batch_results
        if node.output_var:
            result.context[node.output_var] = batch_results

        status = "success" if fail_count == 0 else ("error" if ok_count == 0 else "partial")
        mode = "parallel" if use_parallel else "sequential"
        summary = f"Batch {ok_count}/{ok_count+fail_count} succeeded ({mode})"
        result.log.append({
            "step_id": node.step_id,
            "skill": node.skill_path,
            "description": node.description,
            "status": status,
            "result": summary,
        })
        if fail_count > 0:
            result.errors.append({"step_id": node.step_id, "error": summary})
        logger.info("[BATCH] %s: %s", node.step_id, summary)

    def _execute_batch_sequential(
        self, node, result, source, batch_field, batch_param_key,
    ):
        batch_results = []
        ok_count = fail_count = 0
        for item in source:
            item_val = item
            if isinstance(item, dict) and batch_field:
                item_val = item.get(batch_field, item)
            single_params = dict(node.params)
            if batch_param_key:
                single_params[batch_param_key] = item_val
            try:
                resolved = self._resolve_params(
                    single_params, result.context, skip_batch=True,
                )
                resolved = self._coerce_entity_params(node.skill_path, resolved)
                out = self.registry.call(node.skill_path, **resolved)
                batch_results.append(out)
                ok_count += 1
            except Exception as exc:
                batch_results.append({"error": str(exc)})
                fail_count += 1
                logger.warning("Batch item failed: %s", exc)
        return batch_results, ok_count, fail_count

    def _execute_batch_parallel(
        self, node, result, source, batch_field, batch_param_key,
    ):
        import threading

        batch_results = [None] * len(source)
        ok_count = fail_count = 0
        lock = threading.Lock()

        def _run_one(idx, item):
            item_val = item
            if isinstance(item, dict) and batch_field:
                item_val = item.get(batch_field, item)
            single_params = dict(node.params)
            if batch_param_key:
                single_params[batch_param_key] = item_val
            resolved = self._resolve_params(
                single_params, result.context, skip_batch=True,
            )
            resolved = self._coerce_entity_params(node.skill_path, resolved)
            return self.registry.call(node.skill_path, **resolved)

        workers = min(MAX_BATCH_WORKERS, len(source))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one, i, item): i
                for i, item in enumerate(source)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    batch_results[idx] = fut.result()
                    with lock:
                        ok_count += 1
                except Exception as exc:
                    batch_results[idx] = {"error": str(exc)}
                    with lock:
                        fail_count += 1
                    logger.warning("Batch item %d failed: %s", idx, exc)

        return batch_results, ok_count, fail_count

    # ------------------------------------------------------------------
    # Parameter resolution
    # ------------------------------------------------------------------

    def _coerce_entity_params(self, skill_path: str, params: dict) -> dict:
        """Auto-resolve params that an API skill expects as entity_instance.

        If the resolved value is a dict with 'guid' or 'id', look up the
        actual entity from the model.
        """
        expected = self._entity_params.get(skill_path, set())
        if not expected:
            return params
        result = dict(params)
        for key in expected:
            if key not in result:
                continue
            val = result[key]
            if isinstance(val, (dict, str, int)):
                try:
                    result[key] = self.registry.resolve_entity(val)
                except Exception:
                    pass
        return result

    def _resolve_params(
        self, params: dict, context: dict, *, skip_batch: bool = False,
    ) -> dict:
        """Resolve $variable references in parameter values."""
        resolved = {}
        for key, val in params.items():
            resolved[key] = self._resolve_value(val, context, skip_batch=skip_batch)
        return resolved

    def _resolve_value(
        self, val: Any, context: dict, *, skip_batch: bool = False,
    ) -> Any:
        if isinstance(val, str) and val.startswith("$"):
            if skip_batch and ".each." in val:
                return val
            return self._dereference(val, context)
        if isinstance(val, list):
            return [self._resolve_value(v, context, skip_batch=skip_batch) for v in val]
        if isinstance(val, dict):
            return {
                k: self._resolve_value(v, context, skip_batch=skip_batch)
                for k, v in val.items()
            }
        return val

    def _dereference(self, ref: str, context: dict) -> Any:
        """Resolve $step_id, $step_id.field, or $step_id.0.field (chained)."""
        ref = ref.lstrip("$")

        if ".each." in ref:
            return context.get(ref.split(".each.")[0], [])

        parts = ref.split(".")
        step_id = parts[0]

        if step_id not in context:
            raise ValueError(f"Variable reference ${step_id} not found in context")

        obj = context[step_id]

        for part in parts[1:]:
            if obj is None:
                break
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            elif isinstance(obj, (list, tuple)) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if idx < len(obj) else None
            elif hasattr(obj, part):
                obj = getattr(obj, part)
            else:
                break

        return obj


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(obj: Any, max_len: int = 500) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s
