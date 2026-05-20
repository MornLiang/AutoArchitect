"""
Main workflow orchestration for QA and Edit pipelines.

Architecture (Multi-Agent + GenArtist-inspired):

  **Primary pipeline (Multi-Agent):**
    ProductOwnerAgent → ArchitectAgent (DFS graph traversal) → ProgrammerAgent
    → CorrectionAgent (verify & re-plan loop)

  **Legacy pipeline (single-agent):**
    Router → (IFC Expert) → Coder → Execute → Format answer   (QA)
    Router → TreePlanner → TreeExecutor → Correction            (Edit)

Set ``multi_agent=True`` (default) to use the new pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from ifc_agent.agents import (
    LLMBackend,
    RouterAgent,
    IFCExpertAgent,
    CoderAgent,
    ReviewerAgent,
    CorrectionAgent,
)
from ifc_agent.executor import TreeExecutor
from ifc_agent.graph_extractor import (
    extract_building_graph,
    extract_storey_graphs,
    extract_all_element_graphs,
)
from ifc_agent.ifc_parser import IFCParser
from ifc_agent.ifc_tools import create_tool_registry, get_tools_description
from ifc_agent.multi_agent import MultiAgentOrchestrator
from ifc_agent.planner import TreePlanner
from ifc_agent.skill_registry import SkillRegistry
from ifc_agent.utils import load_prompt, inject_prompt

logger = logging.getLogger(__name__)


class IFCAgentWorkflow:
    """
    Top-level orchestrator wiring agent roles, skill registry, planner, and
    executor together.

    Usage::

        wf = IFCAgentWorkflow("path/to/file.ifc", provider="openai")
        result = wf.run("How many doors are in this building?")
        result = wf.run("Delete all door components")
    """

    def __init__(
        self,
        ifc_path: str,
        provider: str = "openai",
        model: str = None,
        api_key: str = None,
        max_correction_rounds: int = 2,
        output_path: str = None,
        multi_agent: bool = True,
    ):
        self.ifc_path = ifc_path
        self.max_correction_rounds = max_correction_rounds
        self._user_output_path = output_path
        self._provider = provider
        self._model = model
        self._api_key = api_key
        self._multi_agent = multi_agent

        self.parser = IFCParser(ifc_path)

        # Legacy tool registry (kept for QA CoderAgent)
        self.tool_registry = create_tool_registry(self.parser)
        self.tools_description = get_tools_description(self.tool_registry)

        # Skill registry (auto-discovers all ifcopenshell APIs)
        self.skill_registry = SkillRegistry(self.parser.model)

        # Pre-extract graph structures (before LLM sees anything)
        self._graph_context = self._extract_graphs()

        # LLM and agents are lazy-initialised
        self._llm = None
        self._agents_ready = False
        self._orchestrator: Optional[MultiAgentOrchestrator] = None

        self.operation_log: list[dict] = []

    @property
    def llm(self):
        if self._llm is None:
            self._llm = LLMBackend(
                provider=self._provider, model=self._model, api_key=self._api_key,
            )
        return self._llm

    def _ensure_agents(self):
        if self._agents_ready:
            return

        if self._multi_agent:
            self.executor = TreeExecutor(self.skill_registry)
            self._orchestrator = MultiAgentOrchestrator(
                llm=self.llm,
                graph_context=self._graph_context,
                model=self.parser.model,
                skill_registry=self.skill_registry,
                tree_executor=self.executor,
                parser=self.parser,
                output_path=self._user_output_path,
                max_correction_rounds=self.max_correction_rounds,
            )
        else:
            # Legacy agents
            self.router = RouterAgent(
                self.llm, load_prompt("router_prompt.txt"),
            )
            self.expert = IFCExpertAgent(
                self.llm, load_prompt("ifc_schema_knowledge.txt"),
            )
            self.coder = CoderAgent(
                self.llm, load_prompt("coder_prompt.txt"),
            )
            self.tree_planner = TreePlanner(self.llm, self.skill_registry)
            self.executor = TreeExecutor(self.skill_registry)
            self.corrector = CorrectionAgent(
                self.llm, load_prompt("correction_prompt.txt"),
            )

        self._agents_ready = True

    # ------------------------------------------------------------------
    # Graph-based context
    # ------------------------------------------------------------------

    def _extract_graphs(self) -> dict:
        """Pre-extract hierarchical graph structures from the IFC model."""
        logger.info("Extracting hierarchical graph structures from IFC model...")
        building_graph = extract_building_graph(self.parser.model)
        storey_graphs = extract_storey_graphs(self.parser.model)
        logger.info(
            "Graph extraction complete: building=%d nodes/%d edges, %d storeys",
            building_graph["stats"]["node_count"],
            building_graph["stats"]["edge_count"],
            len(storey_graphs),
        )
        return {
            "building": building_graph,
            "storeys": storey_graphs,
        }

    def get_graph_context(self, max_chars: int = 6000) -> str:
        """Serialize the building graph into a text summary for LLM context."""
        bg = self._graph_context["building"]
        lines = [
            f"=== IFC Building Graph ({bg['stats']['node_count']} nodes, "
            f"{bg['stats']['edge_count']} edges) ===",
            f"Node types: {json.dumps(bg['stats']['node_types'], ensure_ascii=False)}",
            "",
            "--- Spatial Hierarchy ---",
        ]

        spatial = [n for n in bg["nodes"] if n.get("category") == "spatial"]
        for n in spatial:
            lines.append(f"  {n['ifc_class']}: {n.get('Name', 'unnamed')} ({n.get('GlobalId', '')})")

        lines.append("")
        lines.append("--- Building Elements ---")
        from collections import Counter
        elem_counter = Counter()
        for n in bg["nodes"]:
            if n.get("category") == "element":
                elem_counter[n["ifc_class"]] += 1
        for cls, cnt in elem_counter.most_common():
            lines.append(f"  {cls}: {cnt}")

        lines.append("")
        lines.append("--- Element Connections ---")
        edge_counter = Counter(e["relation"] for e in bg["edges"])
        for rel, cnt in edge_counter.most_common():
            lines.append(f"  {rel}: {cnt}")

        lines.append("")
        lines.append("--- Per-Storey Summary ---")
        for sg in self._graph_context["storeys"]:
            lines.append(
                f"  {sg['storey_name']}: {sg['stats']['node_count']} nodes, "
                f"{sg['stats']['edge_count']} edges"
            )

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def run(self, query: str, chat_history: str = "") -> dict:
        """Process a user query through the Multi-Agent or legacy pipeline."""
        self._ensure_agents()

        if self._multi_agent:
            return self._run_multi_agent(query)
        else:
            return self._run_legacy(query)

    # ------------------------------------------------------------------
    # Multi-Agent pipeline
    # ------------------------------------------------------------------

    def _run_multi_agent(self, query: str) -> dict:
        """ProductOwner → Architect (DFS) → Programmer pipeline."""
        result = self._orchestrator.run(query)
        self.operation_log.extend(result.get("operations", []))
        return result

    # ------------------------------------------------------------------
    # Legacy single-agent pipeline
    # ------------------------------------------------------------------

    def _run_legacy(self, query: str) -> dict:
        """Old-style Router → QA/Edit pipeline."""
        ifc_context = self.parser.serialize_context()
        ifc_context += "\n\n" + self.get_graph_context()

        # Step 1: Route
        logger.info("Routing query: %s", query)
        routing = self.router.classify(query, ifc_context)
        intent = routing.get("intent", "qa")
        logger.info("Intent: %s | Reasoning: %s", intent, routing.get("reasoning", ""))

        # Step 2: Dispatch
        if intent == "qa":
            answer = self._run_qa(query, ifc_context)
            return {"intent": "qa", "answer": answer, "operations": []}

        elif intent == "edit":
            return self._run_edit(query, ifc_context)

        elif intent == "mixed":
            sub_tasks = routing.get("sub_tasks", [query])
            return self._run_mixed(query, sub_tasks, ifc_context)

        else:
            answer = self._run_qa(query, ifc_context)
            return {"intent": "qa", "answer": answer, "operations": []}

    # ------------------------------------------------------------------
    # QA Pipeline (Text2BIM style — kept from previous architecture)
    # ------------------------------------------------------------------

    def _run_qa(self, query: str, ifc_context: str) -> str:
        logger.info("Running QA pipeline")

        expert_hint = ""
        if any(kw in query.lower() for kw in (
            "structural", "system", "comply", "regulation", "ventilation",
            "enclosed", "type assignment", "misclassif", "roof system",
            "结构", "体系", "围护", "屋面",
        )):
            expert_hint = self.expert.analyze(query, ifc_context)
            logger.info("Expert hint obtained (%d chars)", len(expert_hint))

        task = query
        if expert_hint:
            task = f"{query}\n\nExpert analysis for reference:\n{expert_hint}"

        result, code = self.coder.execute_with_retry(
            task, ifc_context, self.tools_description, self.tool_registry,
        )

        answer = self._format_qa_answer(query, result, expert_hint)
        return answer

    def _format_qa_answer(self, query: str, result: Any, expert_hint: str = "") -> str:
        prompt = (
            f"Based on the following query results, provide a clear and informative answer "
            f"to the user's question.\n\n"
            f"User question: {query}\n\n"
            f"Query result: {result}\n"
        )
        if expert_hint:
            prompt += f"\nExpert analysis: {expert_hint}\n"
        prompt += "\nProvide a concise, informative answer:"
        return self.llm.generate(prompt)

    # ------------------------------------------------------------------
    # Edit Pipeline (GenArtist-style: TreePlanner → Executor → Verify)
    # ------------------------------------------------------------------

    def _run_edit(self, query: str, ifc_context: str) -> dict:
        logger.info("Running Edit pipeline (GenArtist-style)")

        pre_context = ifc_context

        plan_tree = self.tree_planner.plan(query, ifc_context)
        logger.info(
            "Plan tree: %d steps, modules: %s",
            len(plan_tree.nodes), plan_tree.selected_modules,
        )

        exec_result = self.executor.execute(plan_tree)
        self.operation_log.extend(exec_result.to_dict())

        post_context = self.parser.serialize_context()
        operations_log = json.dumps(exec_result.to_dict(), indent=2, default=str)

        for correction_round in range(self.max_correction_rounds):
            correction = self.corrector.verify(
                task=query,
                pre_context=pre_context,
                post_context=post_context,
                operations_log=operations_log,
            )

            if correction.get("correct", True):
                logger.info("Correction round %d: verified OK", correction_round)
                break

            logger.info(
                "Correction round %d: needs fixes — %s",
                correction_round, correction.get("explanation", "")[:100],
            )

            correction_cmds = correction.get("corrections", [])
            if not correction_cmds:
                break

            correction_task = (
                f"Apply these corrections to the IFC model:\n"
                f"{json.dumps(correction_cmds, ensure_ascii=False)}\n"
                f"Original task: {query}"
            )
            corr_plan = self.tree_planner.plan(correction_task, post_context)
            corr_result = self.executor.execute(corr_plan)
            self.operation_log.extend(corr_result.to_dict())

            post_context = self.parser.serialize_context()
            all_ops = exec_result.to_dict() + corr_result.to_dict()
            operations_log = json.dumps(all_ops, indent=2, default=str)

        output_path = self._get_output_path()
        self.parser.save(output_path)
        logger.info("Modified IFC saved to %s", output_path)

        answer = self._format_edit_answer(query, exec_result, post_context)

        return {
            "intent": "edit",
            "answer": answer,
            "operations": exec_result.to_dict(),
            "plan": plan_tree.to_dict(),
            "output_path": output_path,
        }

    def _format_edit_answer(self, query: str, exec_result, post_context: str) -> str:
        lines = [f"Editing task completed: {query}", ""]
        lines.append(exec_result.summary())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Mixed Pipeline
    # ------------------------------------------------------------------

    def _run_mixed(self, query: str, sub_tasks: list[str], ifc_context: str) -> dict:
        all_answers = []
        all_operations = []
        output_path = None

        for sub in sub_tasks:
            sub_routing = self.router.classify(sub, ifc_context)
            sub_intent = sub_routing.get("intent", "qa")

            if sub_intent == "qa":
                ans = self._run_qa(sub, ifc_context)
                all_answers.append(ans)
            elif sub_intent == "edit":
                res = self._run_edit(sub, ifc_context)
                all_answers.append(res["answer"])
                all_operations.extend(res.get("operations", []))
                output_path = res.get("output_path")
                ifc_context = self.parser.serialize_context()
            else:
                ans = self._run_qa(sub, ifc_context)
                all_answers.append(ans)

        return {
            "intent": "mixed",
            "answer": "\n\n---\n\n".join(all_answers),
            "operations": all_operations,
            "output_path": output_path,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_output_path(self) -> str:
        if self._user_output_path:
            return self._user_output_path
        base, ext = os.path.splitext(self.ifc_path)
        return f"{base}_modified{ext}"

    # ------------------------------------------------------------------
    # Direct edit (no LLM required — legacy compatibility)
    # ------------------------------------------------------------------

    def direct_edit(self, operations: list[dict]) -> dict:
        """Execute edit operations directly without LLM planning."""
        from ifc_agent.command_parse import command_parse, execute_steps

        pre_context = self.parser.serialize_context()
        steps = command_parse(operations, self.tool_registry)
        results = execute_steps(steps, self.tool_registry, self.parser)
        self.operation_log.extend(results)

        output_path = self._get_output_path()
        self.parser.save(output_path)
        logger.info("Modified IFC saved to %s", output_path)

        successful = [r for r in results if r["status"] == "success"]
        failed = [r for r in results if r["status"] == "error"]
        lines = ["Direct edit completed."]
        if successful:
            lines.append(f"Successfully executed {len(successful)} operation(s):")
            for r in successful:
                lines.append(f"  - {r['step']}: {r['result']}")
        if failed:
            lines.append(f"{len(failed)} operation(s) failed:")
            for r in failed:
                lines.append(f"  - {r['step']}: {r['result']}")

        return {
            "intent": "edit",
            "answer": "\n".join(lines),
            "operations": results,
            "output_path": output_path,
        }
