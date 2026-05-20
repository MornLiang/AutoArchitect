"""
Three-role Multi-Agent system for IFC QA and editing.

Architecture inspired by Text2BIM (PO / Architect / Programmer separation):

  ProductOwnerAgent  — decomposes user queries into sub-tasks; decides which
                       graph level each sub-task should start from.

  ArchitectAgent     — iterative DFS through the hierarchical graph
                       (L0 building → L1 storey → L2 element).
                       At each level the LLM judges difficulty and decides:
                         ANSWER      — enough context to answer from the graph
                         DRILL_DOWN  — need finer-grained graph detail
                         EXECUTE_API — delegate to ProgrammerAgent for API calls

  ProgrammerAgent    — receives natural-language instructions from the Architect,
                       plans concrete ifcopenshell API calls via TreePlanner,
                       executes them via TreeExecutor, and returns results.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

import ifcopenshell

from ifc_agent.graph_extractor import extract_element_graph

logger = logging.getLogger(__name__)


# ===================================================================
# Data structures
# ===================================================================

@dataclass
class SubTask:
    task_id: str
    description: str
    intent: str                             # "qa" | "edit"
    initial_level: int = 0                  # 0 / 1 / 2
    hints: dict = field(default_factory=dict)  # e.g. {"storey": "地板"}


@dataclass
class ProgrammerResult:
    success: bool
    output: Any = None
    operations: list = field(default_factory=list)
    plan: dict = field(default_factory=dict)


# ===================================================================
# Graph serialization helpers (graph JSON → LLM-readable text)
# ===================================================================

def serialize_building_graph(graph: dict) -> str:
    """Level 0 — building overview for the LLM."""
    stats = graph.get("stats", {})
    lines = [
        f"=== Building Overview (Level 0) — "
        f"{stats.get('node_count', '?')} nodes, "
        f"{stats.get('edge_count', '?')} edges ===",
        "",
        "Spatial hierarchy:",
    ]

    spatial = [n for n in graph["nodes"] if n.get("category") == "spatial"]
    for n in spatial:
        lines.append(
            f"  {n['ifc_class']}: \"{n.get('Name', 'unnamed')}\" "
            f"[{n.get('GlobalId', '')}]"
        )

    lines.append("")
    lines.append("Element statistics:")
    elem_counter = Counter()
    for n in graph["nodes"]:
        if n.get("category") == "element":
            elem_counter[n["ifc_class"]] += 1
    for cls, cnt in elem_counter.most_common():
        lines.append(f"  {cls}: {cnt}")

    lines.append("")
    lines.append("Relationship statistics:")
    edge_counter = Counter(e["relation"] for e in graph["edges"])
    for rel, cnt in edge_counter.most_common():
        lines.append(f"  {rel}: {cnt}")

    return "\n".join(lines)


def serialize_storey_graph(graph: dict) -> str:
    """Level 1 — single storey detail for the LLM."""
    storey_name = graph.get("storey_name", "unknown")
    stats = graph.get("stats", {})
    lines = [
        f"=== Storey \"{storey_name}\" Detail (Level 1) — "
        f"{stats.get('node_count', '?')} nodes, "
        f"{stats.get('edge_count', '?')} edges ===",
        "",
        "Elements:",
    ]

    type_groups: dict[str, list] = {}
    for n in graph["nodes"]:
        if n.get("category") == "element":
            cls = n["ifc_class"]
            type_groups.setdefault(cls, []).append(n)

    for cls in sorted(type_groups.keys()):
        items = type_groups[cls]
        lines.append(f"  [{cls}] × {len(items)}")
        for n in items[:20]:
            lines.append(
                f"    {n['id']} \"{n.get('Name', '')}\" "
                f"GUID={n.get('GlobalId', '')}"
            )
        if len(items) > 20:
            lines.append(f"    ... and {len(items) - 20} more")

    lines.append("")
    lines.append("Key connections:")
    edge_counter = Counter(e["relation"] for e in graph["edges"])
    for rel, cnt in edge_counter.most_common():
        lines.append(f"  {rel}: {cnt}")

    lines.append("")
    lines.append("Connection details (first 30):")
    for e in graph["edges"][:30]:
        extra = ""
        if "attrs" in e:
            extra = f" {e['attrs']}"
        lines.append(f"  {e['source']} —[{e['relation']}]→ {e['target']}{extra}")
    if len(graph["edges"]) > 30:
        lines.append(f"  ... and {len(graph['edges']) - 30} more")

    return "\n".join(lines)


def serialize_element_graph_text(graph: dict) -> str:
    """Level 2 — single element detail for the LLM."""
    lines = [f"=== Element Detail (Level 2): \"{graph.get('graph_id', '')}\" ===", ""]

    el_node = None
    for n in graph["nodes"]:
        if n.get("category") == "element" and not n["id"].startswith("#") is False:
            el_node = n
            break
    if el_node is None:
        el_nodes = [n for n in graph["nodes"] if n.get("category") == "element"]
        if el_nodes:
            el_node = el_nodes[0]

    if el_node:
        lines.append(f"Type: {el_node.get('ifc_class')}")
        lines.append(f"Name: {el_node.get('Name')}")
        lines.append(f"GUID: {el_node.get('GlobalId')}")
        lines.append(f"IFC ID: {el_node.get('ifc_id')}")

    psets: dict[str, list] = {}
    for n in graph["nodes"]:
        if n.get("category") == "property_set":
            psets[n["id"]] = []
    for n in graph["nodes"]:
        if n.get("category") == "property":
            parent_id = n["id"].rsplit("/", 1)[0]
            if parent_id in psets:
                psets[parent_id].append(n)

    if psets:
        lines.append("")
        lines.append("Properties:")
        for pset_id, props in psets.items():
            pset_name = pset_id.rsplit("pset:", 1)[-1] if "pset:" in pset_id else pset_id
            lines.append(f"  [{pset_name}]")
            for p in props:
                lines.append(f"    {p.get('Name')}: {p.get('value')}")

    mat_nodes = [n for n in graph["nodes"]
                 if n.get("category") in ("material", "material_layer")]
    if mat_nodes:
        lines.append("")
        lines.append("Materials:")
        for m in mat_nodes:
            if m.get("category") == "material":
                lines.append(f"  {m.get('ifc_class')}: {m.get('Name', m.get('LayerSetName', ''))}")
            elif m.get("category") == "material_layer":
                lines.append(
                    f"    Layer: {m.get('MaterialName', '?')}, "
                    f"thickness={m.get('LayerThickness', '?')}mm"
                )

    geo_nodes = [n for n in graph["nodes"]
                 if n.get("category", "").startswith("geometry")]
    if geo_nodes:
        lines.append("")
        lines.append("Geometry:")
        for g in geo_nodes:
            cat = g.get("category", "")
            if cat == "geometry_point" and "coordinates" in g:
                label = g.get("ifc_class", "Point")
                lines.append(f"  {label}: {g['coordinates']}")
                if "axis" in g:
                    lines.append(f"    axis={g['axis']}")
                if "ref_direction" in g:
                    lines.append(f"    ref_dir={g['ref_direction']}")
            elif cat == "geometry_solid":
                lines.append(
                    f"  {g.get('ifc_class')}: Depth={g.get('Depth')}, "
                    f"Profile={g.get('profile_type')}"
                )
                if "XDim" in g:
                    lines.append(f"    XDim={g['XDim']}, YDim={g.get('YDim')}")
            elif cat == "geometry_curve":
                lines.append(
                    f"  {g.get('ifc_class')}: {g.get('point_count', '?')} points"
                )
                for pt in (g.get("points") or [])[:5]:
                    lines.append(f"    {pt}")

    type_node = next(
        (n for n in graph["nodes"] if n.get("category") == "type"), None,
    )
    if type_node:
        lines.append("")
        lines.append(f"Type definition: {type_node.get('ifc_class')} "
                      f"\"{type_node.get('Name', '')}\"")

    container = next(
        (n for n in graph["nodes"] if n.get("category") == "spatial"), None,
    )
    if container:
        lines.append(f"Contained in: {container.get('ifc_class')} "
                      f"\"{container.get('Name', '')}\"")

    return "\n".join(lines)


# ===================================================================
# ProductOwnerAgent
# ===================================================================

PO_DECOMPOSE_PROMPT = """\
You are the Product Owner of an IFC/BIM analysis system. Your job is to:
1. Decompose the user's query into concrete sub-tasks.
2. Classify each sub-task as "qa" (question answering) or "edit" (modification).
3. Determine the initial graph level each sub-task should examine.

Graph levels:
  Level 0 (Building) — overall structure: spatial hierarchy, element counts,
      relationship counts. Good for high-level questions ("how many doors?",
      "what structural system?") and broad edits ("delete all doors").
  Level 1 (Storey)   — element detail within a single storey: specific element
      names/GUIDs, inter-element connections. Good for storey-specific queries
      or edits targeting elements on a specific floor.
  Level 2 (Element)  — full detail for a single component: properties, materials,
      geometry points, dimensions. Good for property lookups, dimension queries,
      or property edits on specific elements.

Current building overview:
\"\"\"
{building_summary}
\"\"\"

User query: {query}

Return ONLY a JSON array of sub-task objects:
[
  {{
    "task_id": "t1",
    "description": "concise sub-task description",
    "intent": "qa" or "edit",
    "initial_level": 0 or 1 or 2,
    "hints": {{"storey": "storey name if relevant", "element_type": "IfcXxx if relevant"}}
  }}
]

Rules:
- Simple queries may have just one sub-task.
- If the query is already specific, keep it as one sub-task.
- Do NOT over-decompose: for reasoning questions, keep the ORIGINAL question
  as one sub-task and let the Architect handle the analysis.
- For "edit" tasks, always include a verification sub-task (qa) at the end.
- initial_level should be the LOWEST level sufficient for the task.
  e.g. "how many doors?" → level 0 (counts are in the overview).
  e.g. "properties of wall X" → level 2 (need element detail).

IMPORTANT — Common reasoning queries and how to decompose them:

1. "Is the building fully enclosed?" / "建筑是否完全封闭？"
   → Keep as ONE sub-task. The Architect must check boundary elements
     (IfcRailing vs IfcWall), not just openings. Do NOT rephrase this as
     "are all openings filled?" — that is a different question.
   → initial_level=0, the Architect will drill down as needed.

2. "What is the structural system?" / "结构体系是什么？"
   → Keep as ONE sub-task. The Architect must query IfcWall, IfcColumn,
     IfcBeam elements and analyze their counts, LoadBearing properties,
     and spatial distribution.
   → initial_level=0, hints: {{"element_type": "IfcColumn, IfcWall, IfcBeam"}}

3. "What roof system does this building use?" / "屋顶类型？"
   → Keep as ONE sub-task. Check IfcRoof, IfcSlab(ROOF), IfcCovering.
   → initial_level=0

4. "Does every room have a window?" / "每个房间是否有窗户？"
   → Keep as ONE sub-task. Needs spatial analysis of walls and windows.
   → initial_level=1

General rule: for complex reasoning questions, pass the ORIGINAL question
directly as the task description. Do NOT substitute your own interpretation.
"""


class ProductOwnerAgent:
    """Decomposes user queries into sub-tasks with level routing."""

    def __init__(self, llm):
        self.llm = llm

    def decompose(self, query: str, building_summary: str) -> list[SubTask]:
        prompt = PO_DECOMPOSE_PROMPT.format(
            building_summary=building_summary,
            query=query,
        )
        response = self.llm.generate(prompt)
        logger.info("ProductOwner raw: %s", response[:500])
        return self._parse(response, query)

    def _parse(self, response: str, original_query: str) -> list[SubTask]:
        data = _parse_json(response)
        if isinstance(data, dict):
            data = data.get("tasks", data.get("sub_tasks", [data]))
        if not isinstance(data, list):
            data = [data]

        tasks = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            tasks.append(SubTask(
                task_id=item.get("task_id", f"t{i+1}"),
                description=item.get("description", original_query),
                intent=item.get("intent", "qa"),
                initial_level=int(item.get("initial_level", 0)),
                hints=item.get("hints", {}),
            ))

        if not tasks:
            tasks.append(SubTask(
                task_id="t1",
                description=original_query,
                intent="qa",
                initial_level=0,
            ))

        return tasks


# ===================================================================
# ArchitectAgent
# ===================================================================

_ARCHITECT_DECIDE_TEMPLATE = """\
You are the Architect of an IFC/BIM analysis system. You are performing a \
depth-first traversal of the building's hierarchical graph structure.

<<DOMAIN_KNOWLEDGE>>

Current depth: Level <<LEVEL>> (0=Building, 1=Storey, 2=Element)
Maximum depth: Level 2
Task: <<TASK>>
Intent: <<INTENT>>

Accumulated graph context from traversal so far:
\"\"\"
<<CONTEXT>>
\"\"\"

Based on the graph context AND your domain knowledge above, decide your \
next action:

ANSWER      — You have ENOUGH information from the graph to directly answer
              the question. Provide your answer in "content".
              You MUST apply the domain knowledge rules (e.g. storey counting,
              material assessment) when formulating the answer.
              (Use this for QA tasks where the graph data suffices.)

DRILL_DOWN  — You need MORE detailed information. Specify what to examine
              at the next level in "targets".
              Level 0 → 1: specify {"storey": "storey name"}
              Level 1 → 2: specify {"element_id": "#ifc_step_id"} or
                           {"element_type": "IfcXxx"} for representative elements.
              (Cannot drill below Level 2.)

EXECUTE_API — You need to call ifcopenshell APIs to get the answer or
              perform the edit. In "content", describe what needs to be done
              AND specify which skills/APIs the Programmer should use.
              This delegates execution to the Programmer agent.
              (Use this when: the task requires computation/modification, or
               graph data alone cannot answer the question, or the task is
               simple enough that traversal is unnecessary.)

Return ONLY a JSON object:
{
  "action": "ANSWER" or "DRILL_DOWN" or "EXECUTE_API",
  "reasoning": "brief explanation of your decision",
  "content": "answer text (ANSWER) or execution instruction (EXECUTE_API)",
  "targets": {"storey": "..."} or {"element_id": "#..."} (only for DRILL_DOWN)
}

Decision guidelines:
- Apply domain knowledge FIRST: e.g. for floor counting, do NOT simply count
  IfcBuildingStorey; inspect ObjectType, Elevation, and element counts.
- If element counts/types are visible in L0 and that answers the question → ANSWER
- If you need specific element properties/geometry → DRILL_DOWN to L2
- For EDIT tasks → usually EXECUTE_API (possibly after gathering context)
- At Level 2, you CANNOT DRILL_DOWN further; choose ANSWER or EXECUTE_API
- For questions requiring cross-referencing multiple elements or precise
  property lookup → EXECUTE_API

CRITICAL — When choosing EXECUTE_API, your "content" MUST be specific:
- For structural system analysis: "Use query.by_type to query IfcWallStandardCase,
  IfcColumn, IfcBeam separately. Then use query.get_pset for each to check
  LoadBearing property. Do NOT use get_spatial_structure — it may return empty."
- For floor counting: "Use query.by_type for IfcBuildingStorey, then
  query.get_pset for each to get ObjectType and Elevation."
- For material queries: "Use query.by_type for the target element type, then
  query.get_material or query.get_materials for each element."
- For deletion: "Use query.by_type to find elements, then
  composite.delete_with_openings for each."
- Always prefer query.by_type over spatial containment queries.
"""


def _load_architect_knowledge() -> str:
    """Load domain knowledge from the prompt file."""
    import os
    path = os.path.join(
        os.path.dirname(__file__), "prompts", "architect_knowledge.txt",
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("architect_knowledge.txt not found")
        return ""


_ARCH_KNOWLEDGE = _load_architect_knowledge()


def _build_architect_prompt(level: int, task: str, intent: str, context: str) -> str:
    return (_ARCHITECT_DECIDE_TEMPLATE
            .replace("<<DOMAIN_KNOWLEDGE>>", _ARCH_KNOWLEDGE)
            .replace("<<LEVEL>>", str(level))
            .replace("<<TASK>>", task)
            .replace("<<INTENT>>", intent)
            .replace("<<CONTEXT>>", context))


class ArchitectAgent:
    """DFS traversal through graph levels; delegates API execution to Programmer."""

    def __init__(self, llm, graph_context: dict, model: ifcopenshell.file):
        self.llm = llm
        self.graph_context = graph_context
        self.model = model
        self._text_cache: dict[str, str] = {}

    def invalidate_cache(self):
        """Clear serialization cache (call after graph refresh)."""
        self._text_cache.clear()

    def process(self, sub_task: SubTask) -> dict:
        """Iterative DFS through the graph hierarchy.

        Returns a dict with:
          type:       "answer" | "execute"
          content:    answer text or instruction for Programmer
          traversal:  list of {level, text} showing the DFS path
        """
        level = min(sub_task.initial_level, 2)
        targets = dict(sub_task.hints) if sub_task.hints else {}
        context_stack: list[dict] = []

        # Always include L0 overview for global context
        if level > 0:
            l0_text = self._get_level_text(0, {})
            context_stack.append({"level": 0, "text": l0_text})

        while level <= 2:
            graph_text = self._get_level_text(level, targets)
            context_stack.append({"level": level, "text": graph_text})

            full_context = "\n\n".join(c["text"] for c in context_stack)

            decision = self._decide(sub_task, level, full_context)
            action = decision.get("action", "EXECUTE_API").upper()

            logger.info(
                "Architect L%d decision: %s — %s",
                level, action, decision.get("reasoning", "")[:120],
            )

            if action == "ANSWER":
                return {
                    "type": "answer",
                    "content": decision.get("content", ""),
                    "traversal": context_stack,
                }

            elif action == "EXECUTE_API":
                return {
                    "type": "execute",
                    "content": decision.get("content", sub_task.description),
                    "context": full_context,
                    "traversal": context_stack,
                }

            elif action == "DRILL_DOWN" and level < 2:
                new_targets = decision.get("targets", {})
                if isinstance(new_targets, list):
                    new_targets = new_targets[0] if new_targets else {}
                targets = new_targets
                level += 1

            else:
                return {
                    "type": "execute",
                    "content": decision.get("content", sub_task.description),
                    "context": full_context,
                    "traversal": context_stack,
                }

        return {
            "type": "execute",
            "content": sub_task.description,
            "context": "\n\n".join(c["text"] for c in context_stack),
            "traversal": context_stack,
        }

    # --- Level text retrieval -----------------------------------------------

    def _get_level_text(self, level: int, targets: dict) -> str:
        cache_key = f"L{level}:{json.dumps(targets, sort_keys=True, default=str)}"
        if cache_key in self._text_cache:
            logger.debug("Graph text cache HIT: %s", cache_key[:60])
            return self._text_cache[cache_key]

        text = self._get_level_text_uncached(level, targets)
        self._text_cache[cache_key] = text
        return text

    def _get_level_text_uncached(self, level: int, targets: dict) -> str:
        if level == 0:
            return serialize_building_graph(self.graph_context["building"])

        elif level == 1:
            storey_name = targets.get("storey", "")
            for sg in self.graph_context["storeys"]:
                if sg.get("storey_name") == storey_name:
                    return serialize_storey_graph(sg)
            if self.graph_context["storeys"]:
                return serialize_storey_graph(self.graph_context["storeys"][0])
            return "No storey graph data available."

        elif level == 2:
            element_id = targets.get("element_id", "")
            element_type = targets.get("element_type", "")

            if element_id:
                try:
                    ifc_id = int(str(element_id).lstrip("#"))
                    element = self.model.by_id(ifc_id)
                    eg = extract_element_graph(self.model, element)
                    return serialize_element_graph_text(eg)
                except Exception as exc:
                    logger.warning("Failed to extract element %s: %s", element_id, exc)

            if element_type:
                types = [t.strip() for t in element_type.split(",") if t.strip()]
                parts = []
                for et in types:
                    sample = self._serialize_type_sample(et, max_count=2)
                    if sample:
                        parts.append(sample)
                return "\n\n".join(parts) if parts else f"No elements found for types: {element_type}"

            return "No element target specified for Level 2."

        return ""

    def _serialize_type_sample(self, ifc_class: str, max_count: int = 3) -> str:
        """Serialize a few representative elements of the given type."""
        try:
            elements = self.model.by_type(ifc_class)
        except RuntimeError:
            return f"No elements of type {ifc_class} found."

        if not elements:
            return f"No elements of type {ifc_class} found."

        parts = []
        for el in elements[:max_count]:
            eg = extract_element_graph(self.model, el)
            parts.append(serialize_element_graph_text(eg))

        if len(elements) > max_count:
            parts.append(f"\n... and {len(elements) - max_count} more {ifc_class} elements")

        return "\n\n".join(parts)

    # --- LLM decision -------------------------------------------------------

    def _decide(self, sub_task: SubTask, level: int, context: str) -> dict:
        prompt = _build_architect_prompt(
            level=level,
            task=sub_task.description,
            intent=sub_task.intent,
            context=context,
        )
        response = self.llm.generate(prompt)
        return _parse_json(response, fallback={"action": "EXECUTE_API",
                                                "content": sub_task.description})


# ===================================================================
# ProgrammerAgent
# ===================================================================

PROGRAMMER_RESULT_PROMPT = """\
You are the Programmer agent of an IFC/BIM system. The Architect has gathered \
context and asks you to execute the following task via ifcopenshell API calls.

Architect's instruction:
\"\"\"
{instruction}
\"\"\"

Additional IFC context (from graph traversal):
\"\"\"
{ifc_context}
\"\"\"

{function_catalog}

Create a step-by-step API execution plan.

IMPORTANT RULES:
1. Each step must reference a real skill path from the catalog above.
2. Use "$step_id" syntax in params to reference a previous step's output.
3. For batch operations, use "$ref.each.field" to iterate over list results.
4. For deleting doors/windows, prefer "composite.delete_with_openings".
5. For QA queries, a query step plus returning the result is sufficient.

Return ONLY a JSON array of step objects:
[
  {{
    "step_id": "s1",
    "skill": "module.function_name",
    "params": {{"param_name": "value"}},
    "depends_on": [],
    "output_var": "descriptive_name",
    "description": "What this step does"
  }}
]
"""


class ProgrammerAgent:
    """Executes ifcopenshell API calls via SkillRegistry + TreeExecutor."""

    def __init__(self, llm, skill_registry, tree_executor):
        self.llm = llm
        self.registry = skill_registry
        self.executor = tree_executor

    def execute(self, instruction: str, ifc_context: str) -> ProgrammerResult:
        """Plan and execute API calls based on the Architect's instruction."""
        from ifc_agent.planner import TreePlanner

        planner = TreePlanner(self.llm, self.registry)
        plan_tree = planner.plan(instruction, ifc_context)

        logger.info(
            "Programmer planned %d steps, modules: %s",
            len(plan_tree.nodes), plan_tree.selected_modules,
        )

        exec_result = self.executor.execute(plan_tree)

        return ProgrammerResult(
            success=exec_result.success,
            output=exec_result.context,
            operations=exec_result.to_dict(),
            plan=plan_tree.to_dict(),
        )

    def execute_and_summarize(self, instruction: str, ifc_context: str) -> tuple[ProgrammerResult, str]:
        """Execute and produce a human-readable summary."""
        result = self.execute(instruction, ifc_context)

        ok = [r for r in result.operations if r.get("status") == "success"]
        fail = [r for r in result.operations if r.get("status") in ("error", "partial")]

        lines = [f"Programmer executed {len(result.operations)} steps: "
                 f"{len(ok)} OK, {len(fail)} failed."]
        for r in result.operations:
            sym = "OK" if r["status"] == "success" else "FAIL"
            lines.append(f"  [{sym}] {r['step_id']}: {r.get('description', '')}")
            if r.get("error"):
                lines.append(f"         Error: {r['error']}")
            elif r.get("result"):
                lines.append(f"         → {str(r['result'])[:200]}")

        return result, "\n".join(lines)


# ===================================================================
# MultiAgentOrchestrator — wires the three roles together
# ===================================================================

class MultiAgentOrchestrator:
    """Top-level controller that runs the ProductOwner → Architect → Programmer
    pipeline with DFS graph traversal.
    """

    def __init__(
        self,
        llm,
        graph_context: dict,
        model: ifcopenshell.file,
        skill_registry,
        tree_executor,
        parser,                # IFCParser — for saving and post-edit context
        output_path: str = None,
        max_correction_rounds: int = 2,
    ):
        self.po = ProductOwnerAgent(llm)
        self.architect = ArchitectAgent(llm, graph_context, model)
        self.programmer = ProgrammerAgent(llm, skill_registry, tree_executor)
        self.llm = llm
        self.parser = parser
        self._output_path = output_path
        self.max_corrections = max_correction_rounds

    def run(self, query: str) -> dict:
        """Full Multi-Agent pipeline."""
        # --- Step 1: ProductOwner decomposes ---
        building_summary = serialize_building_graph(
            self.architect.graph_context["building"],
        )
        sub_tasks = self.po.decompose(query, building_summary)
        logger.info("ProductOwner → %d sub-tasks", len(sub_tasks))
        for st in sub_tasks:
            logger.info("  %s [%s] L%d: %s",
                        st.task_id, st.intent, st.initial_level, st.description)

        # --- Step 2–4: process each sub-task ---
        all_answers: list[str] = []
        all_operations: list[dict] = []
        all_plans: list[dict] = []
        traversal_log: list[dict] = []
        has_edit = False

        for st in sub_tasks:
            answer, ops, plan, trav = self._process_subtask(st)
            all_answers.append(answer)
            all_operations.extend(ops)
            if plan:
                all_plans.append(plan)
            traversal_log.append({
                "task_id": st.task_id,
                "description": st.description,
                "intent": st.intent,
                "traversal": [
                    {"level": t["level"], "chars": len(t["text"])}
                    for t in trav
                ],
            })
            if st.intent == "edit":
                has_edit = True
                self._refresh_graphs()

        # --- Step 5: save if edits were made ---
        output_path = None
        if has_edit:
            output_path = self._save()

        combined_answer = "\n\n---\n\n".join(all_answers)

        llm_cache = {
            "hits": self.llm._cache_hits,
            "misses": self.llm._cache_misses,
        }
        graph_cache_size = len(self.architect._text_cache)
        logger.info(
            "Cache stats: LLM hits=%d misses=%d, graph_text_cache=%d entries",
            llm_cache["hits"], llm_cache["misses"], graph_cache_size,
        )

        return {
            "answer": combined_answer,
            "sub_tasks": [
                {"task_id": st.task_id, "description": st.description,
                 "intent": st.intent}
                for st in sub_tasks
            ],
            "operations": all_operations,
            "plans": all_plans,
            "traversal_log": traversal_log,
            "output_path": output_path,
            "cache_stats": llm_cache,
        }

    # --- Internal per-subtask processing ------------------------------------

    def _process_subtask(
        self, st: SubTask,
    ) -> tuple[str, list[dict], Optional[dict], list[dict]]:
        """Process one sub-task through Architect → (Programmer).

        Returns (answer_text, operations_list, plan_dict, traversal_list).
        """
        # --- Architect DFS ---
        arch_result = self.architect.process(st)
        traversal = arch_result.get("traversal", [])

        if arch_result["type"] == "answer":
            logger.info("Architect answered directly (no API call needed)")
            return arch_result["content"], [], None, traversal

        # --- Programmer execution ---
        instruction = arch_result["content"]
        ifc_context = arch_result.get("context", "")

        if not ifc_context:
            ifc_context = self.parser.serialize_context()

        prog_result, summary = self.programmer.execute_and_summarize(
            instruction, ifc_context,
        )
        logger.info("Programmer: %s", summary[:300])

        # --- For edit tasks: correction loop ---
        if st.intent == "edit":
            prog_result = self._correction_loop(
                st, prog_result, ifc_context,
            )

        # --- Format final answer ---
        answer = self._format_answer(st, arch_result, prog_result)

        return answer, prog_result.operations, prog_result.plan, traversal

    def _correction_loop(
        self, st: SubTask, prog_result: ProgrammerResult, pre_context: str,
    ) -> ProgrammerResult:
        """Verify edit results; re-plan and re-execute if needed."""
        for rnd in range(self.max_corrections):
            post_context = self.parser.serialize_context()
            ops_log = json.dumps(prog_result.operations, indent=2, default=str)

            verdict = self._verify(st.description, pre_context, post_context, ops_log)

            if verdict.get("correct", True):
                logger.info("Correction round %d: verified OK", rnd)
                break

            logger.info("Correction round %d: needs fixes", rnd)
            corrections = verdict.get("corrections", [])
            if not corrections:
                break

            fix_instruction = (
                f"Apply these corrections to the IFC model:\n"
                f"{json.dumps(corrections, ensure_ascii=False)}\n"
                f"Original task: {st.description}"
            )
            fix_result, _ = self.programmer.execute_and_summarize(
                fix_instruction, post_context,
            )
            prog_result.operations.extend(fix_result.operations)

        return prog_result

    def _verify(self, task, pre, post, ops_log) -> dict:
        prompt = (
            f"Verify whether the following edit was completed correctly.\n\n"
            f"Task: {task}\n\n"
            f"Pre-edit state summary:\n{pre[:2000]}\n\n"
            f"Post-edit state summary:\n{post[:2000]}\n\n"
            f"Operations performed:\n{ops_log[:2000]}\n\n"
            f"Return JSON: {{\"correct\": true/false, "
            f"\"explanation\": \"...\", \"corrections\": [...]}}"
        )
        response = self.llm.generate(prompt)
        return _parse_json(response, fallback={"correct": True})

    def _format_answer(
        self, st: SubTask, arch_result: dict, prog_result: ProgrammerResult,
    ) -> str:
        """Use LLM to produce a clean, user-facing answer."""
        ops_summary = json.dumps(prog_result.operations, indent=2,
                                 default=str, ensure_ascii=False)
        context_vars = {
            k: _truncate(str(v), 500)
            for k, v in (prog_result.output or {}).items()
            if not k.startswith("_")
        }

        prompt = (
            f"You are an IFC/BIM expert. Apply the following domain knowledge "
            f"when formulating your answer:\n\n"
            f"{_ARCH_KNOWLEDGE[:3000]}\n\n"
            f"Based on the Architect's analysis and the Programmer's execution "
            f"results, provide a clear and concise answer to the user.\n\n"
            f"User task: {st.description}\n"
            f"Intent: {st.intent}\n\n"
            f"Architect reasoning: {arch_result.get('content', '')[:1000]}\n\n"
            f"Programmer operations:\n{ops_summary[:2000]}\n\n"
            f"Key output variables:\n{json.dumps(context_vars, ensure_ascii=False)[:2000]}\n\n"
            f"Provide a concise, informative answer. Apply domain knowledge "
            f"(e.g. floor counting rules, material quality checks, type "
            f"classification caveats) to ensure accuracy:"
        )
        return self.llm.generate(prompt)

    def _refresh_graphs(self):
        """Re-extract graph structures after model edits."""
        from ifc_agent.graph_extractor import extract_building_graph, extract_storey_graphs
        new_bg = extract_building_graph(self.architect.model)
        new_sgs = extract_storey_graphs(self.architect.model)
        self.architect.graph_context = {
            "building": new_bg,
            "storeys": new_sgs,
        }
        self.architect.invalidate_cache()
        logger.info(
            "Graphs refreshed: building=%d nodes/%d edges",
            new_bg["stats"]["node_count"], new_bg["stats"]["edge_count"],
        )

    def _save(self) -> str:
        if self._output_path:
            path = self._output_path
        else:
            import os
            base, ext = os.path.splitext(self.parser.ifc_path)
            path = f"{base}_modified{ext}"
        self.parser.save(path)
        logger.info("Saved modified IFC to %s", path)
        return path


# ===================================================================
# JSON parsing helpers
# ===================================================================

def _parse_json(text: str, fallback: Any = None) -> Any:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'[\[\{][\s\S]*[\]\}]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    if fallback is not None:
        return fallback
    return {}


def _truncate(s: str, max_len: int = 500) -> str:
    return s if len(s) <= max_len else s[:max_len] + "..."
