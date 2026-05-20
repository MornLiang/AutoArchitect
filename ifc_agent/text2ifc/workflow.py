"""
Top-level Text2IFC orchestrator.

Pipeline
--------

    prompt
      │
      ▼  RequirementsAnalyst (LLM)
    requirements_doc (dict)
      │
      ▼  Architect (LLM)               ← refinement note from previous iter
    SpatialGraph (json, no coords)
      │
      ▼  expand_spatial_to_geometric (deterministic)
    BuildingGraph (json, coords)
      │
      ▼  IFCBuilder (deterministic)
    generated.ifc
      │
      ▼  Comparator (vs GT, deterministic)
    diff_report (dict)
      │
      ▼  Refiner (LLM)                 → feeds back into Architect next round
    refinement note

The loop runs ``max_iterations`` times or until the similarity score
reaches ``target_score``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from ifc_agent.agents import LLMBackend
from ifc_agent.text2ifc.agents import (
    Architect,
    Refiner,
    RequirementsAnalyst,
)
from ifc_agent.text2ifc.builder import build_ifc
from ifc_agent.text2ifc.comparator import ComparisonReport, compare, format_report
from ifc_agent.text2ifc.deterministic import (
    DeterministicAnalyst,
    DeterministicArchitect,
    DeterministicRefiner,
)
from ifc_agent.text2ifc.expander import expand_spatial_to_geometric
from ifc_agent.text2ifc.ids_validator import IDSResult, validate as ids_validate
from ifc_agent.text2ifc.schemas import BuildingGraph, SpatialGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class IterationResult:
    iteration: int
    requirements: dict
    spatial_graph_path: str
    graph_path: str
    ifc_path: str
    score: float
    diff_summary: str
    refinement: str = ""
    # Pipeline-2 additions: IDS validation outcome
    ids_pass_rate: float = 1.0          # 0..1, fraction of IDS specs that passed
    ids_total_specs: int = 0
    ids_failed_specs: int = 0
    ids_summary: str = ""                # human-readable findings text
    ids_html_path: str = ""              # optional artefact path
    ids_xml_path: str = ""


@dataclass
class WorkflowResult:
    prompt: str
    gt_path: Optional[str]
    iterations: list[IterationResult] = field(default_factory=list)
    best_iteration: int = -1
    best_score: float = 0.0
    best_ifc_path: str = ""

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "gt_path": self.gt_path,
            "iterations": [asdict(it) for it in self.iterations],
            "best_iteration": self.best_iteration,
            "best_score": self.best_score,
            "best_ifc_path": self.best_ifc_path,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Text2IFCWorkflow:
    """Drive the full prompt → IFC pipeline with optional GT-aware iteration.

    The workflow has three roles — Analyst, Architect, Refiner — each of
    which can run in **LLM** mode or **deterministic** mode.  Set
    ``use_llm=False`` to skip LLM calls entirely (useful when your provider
    quota is exhausted, in CI, or as an ablation baseline).
    """

    def __init__(
        self,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        output_dir: str = "test_output/text2ifc",
        max_iterations: int = 3,
        target_score: float = 0.95,
        use_llm: bool = True,
        auto_fallback: bool = True,
        enable_ids: bool = True,
        ids_min_pass_rate: float = 1.0,
    ):
        self.output_dir = output_dir
        self.max_iterations = max_iterations
        self.target_score = target_score
        self.use_llm = use_llm
        self.auto_fallback = auto_fallback
        self.enable_ids = enable_ids
        # Even if score >= target_score, only stop if IDS pass-rate >= this
        self.ids_min_pass_rate = ids_min_pass_rate
        os.makedirs(output_dir, exist_ok=True)

        # Deterministic agents are always available
        self._d_analyst = DeterministicAnalyst()
        self._d_architect = DeterministicArchitect()
        self._d_refiner = DeterministicRefiner()

        self.llm = None
        self.analyst = None
        self.architect = None
        self.refiner = None
        if use_llm:
            try:
                self.llm = LLMBackend(provider=provider, model=model)
                self.analyst = RequirementsAnalyst(self.llm)
                self.architect = Architect(self.llm)
                self.refiner = Refiner(self.llm)
            except Exception as exc:
                if not auto_fallback:
                    raise
                logger.warning(
                    "LLM init failed (%s) — falling back to deterministic mode.",
                    exc,
                )
                self.use_llm = False

    # ------------------------------------------------------------------
    # One-shot run
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        gt_ifc_path: Optional[str] = None,
        run_name: str = "run",
        seed_spatial: Optional[SpatialGraph] = None,
    ) -> WorkflowResult:
        """Execute the pipeline, optionally iterating against *gt_ifc_path*.

        Parameters
        ----------
        seed_spatial:
            Optional pre-made SpatialGraph to use for ITERATION 1, replacing
            the Architect LLM call.  Useful when the user (or an
            image-aware model) wants to inject a high-fidelity layout
            instead of asking a non-multimodal LLM to invent it.  All
            subsequent iterations still go through the LLM Architect with
            the Refiner's feedback.
        """
        result = WorkflowResult(prompt=prompt, gt_path=gt_ifc_path)

        # --- Stage 1: requirements (run once) ---
        logger.info("[Analyst] expanding prompt … (mode=%s)",
                    "LLM" if self.use_llm and self.analyst else "deterministic")
        requirements = self._call_analyst(prompt)
        req_path = self._save_json(requirements, f"{run_name}_requirements.json")
        logger.info("Requirements doc saved to %s", req_path)

        refinement = ""
        for it in range(1, self.max_iterations + 1):
            logger.info("=== Iteration %d/%d ===", it, self.max_iterations)

            # --- Stage 2a: architect → SpatialGraph (no coordinates) ---
            if it == 1 and seed_spatial is not None:
                logger.info(
                    "[Architect] using seed SpatialGraph (Architect LLM skipped)"
                )
                spatial = seed_spatial
            else:
                spatial = self._call_architect_spatial(
                    requirements, refinement=refinement,
                )
            spatial_path = self._save_json(
                spatial.to_dict(), f"{run_name}_iter{it}_spatial.json",
            )
            logger.info(
                "SpatialGraph (counts=%s) saved to %s",
                spatial.stats(), spatial_path,
            )

            # --- Stage 2b: deterministic expansion → BuildingGraph ---
            graph = expand_spatial_to_geometric(spatial)
            graph_path = self._save_json(
                graph.to_dict(), f"{run_name}_iter{it}_graph.json",
            )
            logger.info(
                "BuildingGraph (counts=%s) saved to %s",
                graph.stats(), graph_path,
            )

            # --- Stage 3: build IFC ---
            ifc_path = os.path.join(self.output_dir, f"{run_name}_iter{it}.ifc")
            try:
                build_ifc(graph, ifc_path)
                logger.info("IFC written to %s", ifc_path)
            except Exception as exc:
                logger.exception("Builder failed: %s", exc)
                result.iterations.append(IterationResult(
                    iteration=it,
                    requirements=requirements,
                    spatial_graph_path=spatial_path,
                    graph_path=graph_path,
                    ifc_path="",
                    score=0.0,
                    diff_summary=f"BUILDER FAILED: {exc}",
                    refinement=refinement,
                ))
                refinement = (
                    f"The previous build failed with: {exc}. "
                    f"Simplify the spatial graph: keep one storey with a "
                    f"rectangular footprint, layout_hint='rectangular', and "
                    f"at least one floor slab."
                )
                continue

            # --- Stage 4a: IDS validation (always-on, when ifctester is
            # installed and enable_ids=True) ---
            ids_result = self._run_ids(
                ifc_path, requirements=requirements, spatial=spatial,
                run_name=run_name, iteration=it,
            )

            # --- Stage 4b: GT comparison (if provided) ---
            if gt_ifc_path:
                report = compare(gt_ifc_path, ifc_path)
                diff_summary = format_report(report)
                logger.info(
                    "Comparison score=%.3f | IDS pass-rate=%.2f (%d/%d)",
                    report.score, ids_result.pass_rate(),
                    ids_result.passed_specs, ids_result.total_specs,
                )

                iter_res = IterationResult(
                    iteration=it,
                    requirements=requirements,
                    spatial_graph_path=spatial_path,
                    graph_path=graph_path,
                    ifc_path=ifc_path,
                    score=report.score,
                    diff_summary=diff_summary,
                    refinement=refinement,
                    **self._ids_to_kwargs(ids_result),
                )
                result.iterations.append(iter_res)

                if report.score > result.best_score:
                    result.best_score = report.score
                    result.best_iteration = it
                    result.best_ifc_path = ifc_path

                target_hit = report.score >= self.target_score
                ids_clean = ids_result.pass_rate() >= self.ids_min_pass_rate
                if target_hit and ids_clean:
                    logger.info(
                        "Target score reached (%.3f) and IDS clean "
                        "(%.2f). Stopping.",
                        report.score, ids_result.pass_rate(),
                    )
                    break

                # --- Stage 5: refiner produces feedback for next iter ---
                if it < self.max_iterations:
                    combined = self._combine_diff_and_ids(
                        diff_summary, ids_result,
                    )
                    refinement = self._call_refiner(combined)
                    logger.info("Refinement: %s", refinement[:200])
            else:
                # No GT — use IDS as the sole signal
                logger.info(
                    "IDS pass-rate=%.2f (%d/%d) — no GT comparison.",
                    ids_result.pass_rate(),
                    ids_result.passed_specs, ids_result.total_specs,
                )
                iter_res = IterationResult(
                    iteration=it,
                    requirements=requirements,
                    spatial_graph_path=spatial_path,
                    graph_path=graph_path,
                    ifc_path=ifc_path,
                    score=ids_result.pass_rate(),
                    diff_summary="(no GT provided)",
                    refinement=refinement,
                    **self._ids_to_kwargs(ids_result),
                )
                result.iterations.append(iter_res)

                if ids_result.pass_rate() > result.best_score:
                    result.best_score = ids_result.pass_rate()
                    result.best_iteration = it
                    result.best_ifc_path = ifc_path

                # Stop if IDS clean; otherwise refine and retry
                if ids_result.pass_rate() >= self.ids_min_pass_rate \
                        and not ids_result.findings:
                    logger.info(
                        "IDS clean — stopping without GT.",
                    )
                    break
                if it < self.max_iterations:
                    combined = self._combine_diff_and_ids("", ids_result)
                    refinement = self._call_refiner(combined)
                    logger.info("Refinement: %s", refinement[:200])
                else:
                    result.best_iteration = it
                    result.best_ifc_path = ifc_path

        return result

    # ------------------------------------------------------------------
    # Internal: route to LLM agents with deterministic fallback
    # ------------------------------------------------------------------

    def _call_analyst(self, prompt: str) -> dict:
        if self.use_llm and self.analyst is not None:
            try:
                return self.analyst.expand(prompt)
            except Exception as exc:
                logger.warning("Analyst LLM failed (%s) — fallback.", exc)
                if not self.auto_fallback:
                    raise
        return self._d_analyst.expand(prompt)

    def _call_architect_spatial(self, requirements: dict, *,
                                refinement: str = "") -> SpatialGraph:
        if self.use_llm and self.architect is not None:
            try:
                return self.architect.design_spatial(
                    requirements, refinement=refinement,
                )
            except Exception as exc:
                logger.warning("Architect LLM failed (%s) — fallback.", exc)
                if not self.auto_fallback:
                    raise
        return self._d_architect.design_spatial(
            requirements, refinement=refinement,
        )

    def _run_ids(self, ifc_path: str, *,
                 requirements: dict, spatial: SpatialGraph,
                 run_name: str, iteration: int) -> IDSResult:
        """Validate *ifc_path* against an auto-generated IDS spec set."""
        if not self.enable_ids:
            return IDSResult(available=False)
        html_path = os.path.join(
            self.output_dir, f"{run_name}_iter{iteration}_ids.html",
        )
        xml_path = os.path.join(
            self.output_dir, f"{run_name}_iter{iteration}_ids.xml",
        )
        try:
            return ids_validate(
                ifc_path,
                requirements=requirements,
                spatial=spatial,
                html_report_path=html_path,
                ids_xml_path=xml_path,
            )
        except Exception as exc:
            logger.warning("IDS validation failed (%s) — skipping.", exc)
            return IDSResult(available=False)

    @staticmethod
    def _ids_to_kwargs(ids_result: IDSResult) -> dict:
        if not ids_result.available:
            return {
                "ids_pass_rate": 1.0,
                "ids_total_specs": 0,
                "ids_failed_specs": 0,
                "ids_summary": "(IDS skipped)",
                "ids_html_path": "",
                "ids_xml_path": "",
            }
        # We can't recover the artefact paths from IDSResult alone, so leave
        # them blank here; _run_ids already wrote them to disk under
        # output_dir/<run_name>_iter<N>_ids.{html,xml}.
        return {
            "ids_pass_rate": ids_result.pass_rate(),
            "ids_total_specs": ids_result.total_specs,
            "ids_failed_specs": ids_result.failed_specs,
            "ids_summary": ids_result.to_text(),
            "ids_html_path": "",
            "ids_xml_path": "",
        }

    @staticmethod
    def _combine_diff_and_ids(diff_summary: str,
                              ids_result: IDSResult) -> str:
        """Merge GT comparison output and IDS findings into a single
        natural-language brief for the Refiner agent."""
        parts: list[str] = []
        if diff_summary.strip():
            parts.append("=== GT comparison ===")
            parts.append(diff_summary.strip())
        if ids_result.available and ids_result.findings:
            parts.append("")
            parts.append("=== IDS validation findings ===")
            parts.append(ids_result.to_text())
        if not parts:
            parts.append("All metrics within tolerance.")
        return "\n".join(parts)

    def _call_refiner(self, diff_summary: str) -> str:
        if self.use_llm and self.refiner is not None:
            try:
                return self.refiner.refine(diff_summary)
            except Exception as exc:
                logger.warning("Refiner LLM failed (%s) — fallback.", exc)
                if not self.auto_fallback:
                    raise
        return self._d_refiner.refine(diff_summary)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_json(self, obj, name: str) -> str:
        path = os.path.join(self.output_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
        return path
