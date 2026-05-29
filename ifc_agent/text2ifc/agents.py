"""
LLM-driven agents for the Text2IFC pipeline.

The three roles map to Text2BIM's PO / Architect / Programmer but the
last step (graph → IFC) is deterministic, so we replace Text2BIM's
Programmer with a Refiner that proposes corrections after comparing the
generated IFC to the ground-truth.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from ifc_agent.agents import LLMBackend
from ifc_agent.text2ifc.expander import expand_spatial_to_geometric
from ifc_agent.text2ifc.schemas import (
    BuildingGraph,
    SpatialGraph,
)
from ifc_agent.utils import clean_json_from_llm, inject_prompt

logger = logging.getLogger(__name__)

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(name: str) -> str:
    with open(os.path.join(_PROMPT_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _Text2IFCAgent:
    def __init__(self, llm: LLMBackend, prompt_template: str):
        self.llm = llm
        self.prompt_template = prompt_template

    def _build_prompt(self, **kwargs) -> str:
        return inject_prompt(self.prompt_template, **kwargs)


# ---------------------------------------------------------------------------
# Requirements Analyst
# ---------------------------------------------------------------------------

class RequirementsAnalyst(_Text2IFCAgent):
    """Expand a short user prompt into a structured requirements JSON doc."""

    PROMPT_NAME = "requirements_analyst.txt"

    def __init__(self, llm: LLMBackend):
        super().__init__(llm, _load_prompt(self.PROMPT_NAME))

    def expand(self, user_description: str) -> dict:
        prompt = self._build_prompt(task=user_description)
        response = self.llm.generate(prompt, temperature=0)
        return _safe_parse_json_dict(response, fallback_field="raw_response")


# ---------------------------------------------------------------------------
# Architect
# ---------------------------------------------------------------------------

class Architect(_Text2IFCAgent):
    """Turn a requirements document into a SpatialGraph, then expand
    it deterministically into a BuildingGraph for the builder.

    The LLM is asked for the 3D SPATIAL STRUCTURE only — element counts,
    overall footprint, and qualitative layout hints — not coordinates.
    The shared expander then materialises every wall/opening/column
    position.  This keeps the LLM's JSON small and fast, especially in
    DeepSeek/Claude "thinking" mode.
    """

    PROMPT_NAME = "architect.txt"

    def __init__(self, llm: LLMBackend):
        super().__init__(llm, _load_prompt(self.PROMPT_NAME))

    def design(
        self,
        requirements: dict,
        *,
        refinement: str = "",
    ) -> BuildingGraph:
        """Generate (or regenerate) the BuildingGraph.

        Internally this is a two-step process:
          1. LLM emits a SpatialGraph JSON (no coordinates).
          2. ``expand_spatial_to_geometric`` produces the BuildingGraph.

        Parameters
        ----------
        requirements:
            The dict produced by ``RequirementsAnalyst.expand``.
        refinement:
            Optional natural-language correction notes from the Refiner.
        """
        sg = self.design_spatial(requirements, refinement=refinement)
        return expand_spatial_to_geometric(sg)

    def design_spatial(
        self,
        requirements: dict,
        *,
        refinement: str = "",
    ) -> SpatialGraph:
        prompt = self._build_prompt(
            task=json.dumps(requirements, indent=2, ensure_ascii=False),
            refinement=refinement or "",
        )
        response = self.llm.generate(prompt, temperature=0)
        sg_dict = _safe_parse_json_dict(response)
        sg_dict = _autorepair_spatial(sg_dict, requirements)
        try:
            return SpatialGraph.from_dict(sg_dict)
        except Exception as exc:
            logger.warning(
                "Architect produced an unparseable SpatialGraph (%s) — "
                "falling back to a stub.", exc,
            )
            return _stub_spatial_from_requirements(requirements)


# ---------------------------------------------------------------------------
# Refiner
# ---------------------------------------------------------------------------

class Refiner(_Text2IFCAgent):
    """Read a comparison report and emit natural-language correction notes."""

    PROMPT_NAME = "refiner.txt"

    def __init__(self, llm: LLMBackend):
        super().__init__(llm, _load_prompt(self.PROMPT_NAME))

    def refine(self, comparison_summary: str) -> str:
        prompt = self._build_prompt(task=comparison_summary)
        return self.llm.generate(prompt, temperature=0).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_parse_json_dict(text: str, *, fallback_field: str | None = None) -> dict:
    """Best-effort parse of an LLM response into a dict."""
    text = clean_json_from_llm(text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Fallback: extract the largest curly-braced span
    match = _JSON_OBJ_RE.search(text)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse JSON from LLM output (head: %s ...)", text[:200])
    if fallback_field:
        return {fallback_field: text}
    return {}


def _autorepair_spatial(sg: dict, requirements: dict | None = None) -> dict:
    """Forgive common LLM mistakes when emitting a rooms-based SpatialGraph.

    Adds missing keys, coerces types, backfills footprint / element counts
    from the requirements doc, and ensures every storey has either
    rooms (inhabited) or is_inhabited=False (roof level).
    """
    if not isinstance(sg, dict):
        return sg

    req = requirements or {}

    meta = sg.setdefault("metadata", {})
    meta.setdefault("schema", "IFC4")
    meta.setdefault("name", req.get("building_type", "Generated Building"))
    meta.setdefault("project_name", "Text2IFC")
    meta.setdefault("site_name", "Default Site")

    fp = sg.setdefault("footprint", {})
    if not isinstance(fp, dict):
        fp = {}
        sg["footprint"] = fp
    fp.setdefault("shape", "rectangle")
    req_fp = req.get("footprint", {}) or {}
    fp.setdefault("x_mm", req_fp.get("x_mm", 10000))
    fp.setdefault("y_mm", req_fp.get("y_mm", 8000))
    fp.setdefault("boundary", req_fp.get("boundary", []))
    fp.setdefault("voids", req_fp.get("voids", []))
    sg.setdefault("structural_system", req.get("structural_system", {
        "kind": "frame",
        "grid_spacing_x_mm": 6000,
        "grid_spacing_y_mm": 6000,
        "core_position": "center",
    }))
    sg.setdefault("shafts", [])

    storeys = sg.get("storeys") or []
    if not isinstance(storeys, list) or not storeys:
        storeys = _stub_storeys_from_requirements(req)
        sg["storeys"] = storeys

    storey_height = float(req.get("storey_height_mm", 3000) or 3000)

    for idx, s in enumerate(storeys):
        s.setdefault("id", s.get("name") or f"s{idx + 1}")
        s.setdefault("name", f"Storey {idx + 1}")
        s.setdefault("elevation_mm", idx * storey_height)
        s.setdefault("height_mm", storey_height)
        s.setdefault("notes", [])
        s.setdefault("shaft_ids", [])

        # Common LLM key-name mistakes
        if "elevation" in s and "elevation_mm" not in s:
            s["elevation_mm"] = s.pop("elevation")
        if "height" in s and "height_mm" not in s:
            s["height_mm"] = s.pop("height")

        # Rooms / inhabited flag
        rooms = s.get("rooms") or []
        s.setdefault("is_inhabited", bool(rooms))
        s.setdefault("layout_hint", "central_corridor" if rooms else "empty")
        if rooms:
            _autorepair_rooms(rooms)
        s["rooms"] = rooms

        # Storey-level element overrides
        elems = s.setdefault("elements", {})
        if not isinstance(elems, dict):
            elems = {}
            s["elements"] = elems
        for k in ("walls", "doors", "windows", "columns",
                  "slabs", "roofs", "railings"):
            elems.setdefault(k, 0)
        elems.setdefault("wall_thickness_mm", 200)

    return sg


def _autorepair_rooms(rooms: list[dict]) -> None:
    """Patch a rooms list in-place: make adjacency symmetric, ensure
    opening_to ⊆ adjacent_to, fill missing defaults."""
    by_id: dict[str, dict] = {}
    for r in rooms:
        rid = str(r.get("id") or r.get("name") or "room")
        r["id"] = rid
        r.setdefault("function", "office")
        r.setdefault("area_ratio", 0.1)
        r.setdefault("adjacent_to", [])
        r.setdefault("opening_to", [])
        r.setdefault("has_external_facade", False)
        r.setdefault("n_windows", 0)
        r.setdefault("n_external_doors", 0)
        r.setdefault("shaft_id", "")
        r.setdefault("is_core", False)
        by_id[rid] = r

    # Symmetrise adjacency
    for r in rooms:
        for nb in list(r["adjacent_to"]):
            if nb in by_id and r["id"] not in by_id[nb]["adjacent_to"]:
                by_id[nb]["adjacent_to"].append(r["id"])

    # opening_to ⊆ adjacent_to
    for r in rooms:
        r["opening_to"] = [nb for nb in r["opening_to"]
                           if nb in r["adjacent_to"]]


def _stub_spatial_from_requirements(req: dict) -> SpatialGraph:
    """Build a minimal but valid rooms-based SpatialGraph from a
    requirements dict, used when the LLM's output is unparseable."""
    sg_dict = {
        "metadata": {
            "name": req.get("building_type", "Stub Building"),
            "description": "Fallback spatial graph",
            "schema": "IFC4",
        },
        "footprint": {
            "shape": "rectangle",
            "x_mm": (req.get("footprint") or {}).get("x_mm", 10000),
            "y_mm": (req.get("footprint") or {}).get("y_mm", 8000),
        },
        "storeys": _stub_storeys_from_requirements(req),
    }
    return SpatialGraph.from_dict(sg_dict)


def _stub_storeys_from_requirements(req: dict) -> list[dict]:
    """Generate a default rooms-based storey list for fallback.

    The DeterministicArchitect.design_spatial method is the source of
    truth for this layout, so we just delegate to it.
    """
    from ifc_agent.text2ifc.deterministic import DeterministicArchitect

    sg = DeterministicArchitect().design_spatial(req)
    return sg.to_dict().get("storeys", [])
