"""
GenArtist-style tree planner for IFC editing.

Two-stage planning:
  Stage 1 — Module Selection:  LLM reads module-level catalog → picks relevant modules
  Stage 2 — Detailed Planning: LLM reads function-level catalog for those modules
                                → outputs an execution plan tree (JSON)

The plan tree is a list of nodes.  Each node may reference outputs of
earlier nodes via ``$step_id`` variable syntax, enabling dependent execution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan tree data structures
# ---------------------------------------------------------------------------

@dataclass
class PlanNode:
    """A single step in an execution plan."""
    step_id: str
    skill_path: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    output_var: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "skill": self.skill_path,
            "params": self.params,
            "depends_on": self.depends_on,
            "output_var": self.output_var,
            "description": self.description,
        }


@dataclass
class PlanTree:
    """Ordered list of plan nodes with dependency tracking."""
    task: str
    selected_modules: list[str] = field(default_factory=list)
    nodes: list[PlanNode] = field(default_factory=list)

    def topological_order(self) -> list[PlanNode]:
        """Return nodes in a valid execution order respecting dependencies."""
        id_to_node = {n.step_id: n for n in self.nodes}
        visited: set[str] = set()
        order: list[PlanNode] = []

        def _visit(node_id: str):
            if node_id in visited:
                return
            visited.add(node_id)
            node = id_to_node.get(node_id)
            if node is None:
                return
            for dep_id in node.depends_on:
                _visit(dep_id)
            order.append(node)

        for n in self.nodes:
            _visit(n.step_id)
        return order

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "selected_modules": self.selected_modules,
            "steps": [n.to_dict() for n in self.nodes],
        }


# ---------------------------------------------------------------------------
# Planner agent (two-stage LLM calls)
# ---------------------------------------------------------------------------

class TreePlanner:
    """GenArtist-style planner that produces a PlanTree from a user task.

    Uses the SkillRegistry's catalog at two levels:
      1. Module selection  (which ifcopenshell.api modules are needed?)
      2. Detailed planning (which specific functions, in what order?)

    When ``fast_mode=True``, combines both stages into a single LLM call
    using keyword-based module pre-selection + the merged prompt.
    """

    def __init__(self, llm, skill_registry, *, qa_mode: bool = False,
                 fast_mode: bool = True):
        self.llm = llm
        self.registry = skill_registry
        self.qa_mode = qa_mode
        self.fast_mode = fast_mode

    # ------------------------------------------------------------------
    # Keyword-based fast module pre-selection (no LLM call needed)
    # ------------------------------------------------------------------

    _KEYWORD_MAP = {
        "delete": ["root", "feature"],
        "remove": ["root", "feature"],
        "wall":   ["query"],
        "door":   ["query"],
        "window": ["query"],
        "column": ["query"],
        "beam":   ["query"],
        "slab":   ["query"],
        "material": ["material", "query"],
        "property": ["pset", "query"],
        "pset":   ["pset", "query"],
        "loadbearing": ["pset", "query"],
        "thickness": ["pset", "query"],
        "spatial": ["spatial", "query"],
        "geometry": ["geometry", "query"],
        "placement": ["geometry", "query"],
        "rename": ["attribute", "query"],
        "modify": ["attribute", "query"],
        "count":  ["query"],
        "layer":  ["material", "query"],
    }

    def _fast_select_modules(self, task: str) -> list[str]:
        """Select modules using keyword matching — zero LLM calls."""
        task_lower = task.lower()
        selected = set()
        for keyword, modules in self._KEYWORD_MAP.items():
            if keyword in task_lower:
                selected.update(modules)
        if not selected:
            from ifc_agent.skill_registry import CORE_MODULES
            selected = set(CORE_MODULES)
        selected.add("query")
        selected.add("composite")
        result = sorted(selected)
        logger.info("Fast module selection: %s", result)
        return result

    # ------------------------------------------------------------------
    # Stage 1: Module selection (LLM-based, used in non-fast mode)
    # ------------------------------------------------------------------

    def _select_modules(self, task: str, ifc_context: str) -> list[str]:
        module_catalog = self.registry.get_module_catalog()
        prompt = MODULE_SELECTOR_PROMPT.format(
            module_catalog=module_catalog,
            ifc_context=ifc_context,
            task=task,
        )
        response = self.llm.generate(prompt)
        logger.info("Module selector raw response: %s", response[:300])

        modules = _parse_json_list(response)
        valid = set(self.registry.list_modules())
        selected = [m for m in modules if m in valid]

        if not selected:
            from ifc_agent.skill_registry import CORE_MODULES
            selected = list(CORE_MODULES)
            logger.warning("Module selection returned empty — falling back to CORE_MODULES")

        if "query" not in selected:
            selected.append("query")
        if "composite" not in selected:
            selected.append("composite")

        logger.info("Selected modules: %s", selected)
        return selected

    # ------------------------------------------------------------------
    # Stage 2: Detailed planning
    # ------------------------------------------------------------------

    def plan(self, task: str, ifc_context: str) -> PlanTree:
        """Run planning and return a PlanTree.

        In fast_mode, skips the LLM module-selection call and uses
        keyword-based pre-selection instead (saves ~5-8s per plan).
        """
        if self.fast_mode:
            selected_modules = self._fast_select_modules(task)
        else:
            selected_modules = self._select_modules(task, ifc_context)

        function_catalog = self.registry.get_function_catalog(selected_modules)
        prompt = TREE_PLANNER_PROMPT.format(
            function_catalog=function_catalog,
            ifc_context=ifc_context,
            task=task,
        )
        response = self.llm.generate(prompt)
        logger.info("Tree planner raw response (first 500): %s", response[:500])

        tree = self._parse_plan_tree(task, selected_modules, response)
        return tree

    def _parse_plan_tree(
        self, task: str, modules: list[str], raw: str,
    ) -> PlanTree:
        steps_data = _parse_json_list_or_dict(raw)

        if isinstance(steps_data, dict):
            steps_data = steps_data.get("steps", [steps_data])

        nodes = []
        for i, s in enumerate(steps_data):
            if not isinstance(s, dict):
                continue
            node = PlanNode(
                step_id=s.get("step_id", f"s{i+1}"),
                skill_path=s.get("skill", s.get("api", s.get("skill_path", ""))),
                params=s.get("params", s.get("parameters", {})),
                depends_on=s.get("depends_on", []),
                output_var=s.get("output_var", s.get("output", f"result_{i+1}")),
                description=s.get("description", s.get("desc", "")),
            )
            nodes.append(node)

        if not nodes:
            logger.warning("Plan tree is empty — creating fallback node")
            nodes.append(PlanNode(
                step_id="s1",
                skill_path="query.get_spatial_structure",
                params={},
                description="Fallback: get spatial structure for inspection",
            ))

        return PlanTree(task=task, selected_modules=modules, nodes=nodes)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

MODULE_SELECTOR_PROMPT = """\
You are an IFC/BIM expert. Given a user task about an IFC model, select which \
ifcopenshell API modules are needed to accomplish it.

{module_catalog}

Current IFC model summary:
\"\"\"
{ifc_context}
\"\"\"

User task: {task}

Return ONLY a JSON array of module names. Example: ["root", "geometry", "spatial"]
Include "query" if you need to read/inspect the model first.
Do not include modules that are not needed.
"""

TREE_PLANNER_PROMPT = """\
You are an IFC/BIM editing planner. Given a user task, create a step-by-step \
execution plan using the available ifcopenshell API functions below.

{function_catalog}

Current IFC model summary:
\"\"\"
{ifc_context}
\"\"\"

IMPORTANT RULES:
1. Each step must reference a real skill path from the catalog above.
2. Use "$step_id" syntax in params to reference a previous step's output.
   Example: {{"product": "$s1"}} means "use the output of step s1".
3. For batch operations (e.g. "delete ALL doors"), first query elements, \
then loop in a single step using the appropriate batch skill or composite skill.
4. For deleting elements that may have openings (doors, windows), \
prefer "composite.delete_with_openings" which handles cleanup.
5. Always end with a validation or verification step if editing.
6. For query-only tasks, a single query step may suffice.

User task: {task}

Return ONLY a JSON array of step objects. Each step:
{{
  "step_id": "s1",
  "skill": "module.function_name",
  "params": {{"param_name": "value"}},
  "depends_on": [],
  "output_var": "descriptive_name",
  "description": "What this step does"
}}

Example for "delete all doors":
[
  {{"step_id": "s1", "skill": "query.by_type", "params": {{"ifc_class": "IfcDoor"}}, "depends_on": [], "output_var": "all_doors", "description": "Query all door elements"}},
  {{"step_id": "s2", "skill": "composite.delete_with_openings", "params": {{"guid": "$s1.each.guid"}}, "depends_on": ["s1"], "output_var": "delete_result", "description": "Delete each door with cascade cleanup"}},
  {{"step_id": "s3", "skill": "query.count_by_type", "params": {{"ifc_class": "IfcDoor"}}, "depends_on": ["s2"], "output_var": "remaining_count", "description": "Verify no doors remain"}}
]
"""


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _parse_json_list(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "modules" in parsed:
            return parsed["modules"]
    except json.JSONDecodeError:
        pass

    import re
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []


def _parse_json_list_or_dict(text: str) -> list | dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        return parsed
    except json.JSONDecodeError:
        pass

    import re
    match = re.search(r'[\[\{].*[\]\}]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return []
