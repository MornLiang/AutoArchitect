"""
Text2IFC pipeline: natural-language prompt → IFC file.

Pipeline stages
---------------
1. RequirementsAnalyst (LLM) — expand a user prompt into a structured
   requirements document.
2. Architect (LLM)           — derive a high-level SpatialGraph
   (project → footprint → storeys → element counts) from the requirements
   document.  No coordinates are emitted.
3. Expander (deterministic)  — materialise the SpatialGraph into a fully
   coordinate-resolved BuildingGraph.
4. Builder (deterministic)   — traverse the BuildingGraph and call
   ifcopenshell.api to write an .ifc file.
5. Comparator (deterministic)— diff the generated IFC against a
   ground-truth IFC and emit a structured discrepancy report.
6. Refiner (LLM, optional)   — given the diff report, propose corrections;
   the Architect re-runs to produce a refined SpatialGraph.

The decoupling of Architect (LLM → SpatialGraph) and Expander/Builder
(deterministic → coordinates → IFC) is the key design choice that avoids
both API-hallucination and slow coordinate emission failure modes common
to LLM-driven IFC generation.
"""

from ifc_agent.text2ifc.schemas import (
    BuildingGraph,
    StoreyNode,
    WallNode,
    OpeningNode,
    ColumnNode,
    SlabNode,
    RoofNode,
    RailingNode,
    Point2D,
    BuildingMetadata,
    SpatialGraph,
    SpatialStorey,
    RoomNode,
    SpaceNode,
    FurnitureNode,
    StoreyElements,
    Footprint,
)

__all__ = [
    "BuildingGraph",
    "StoreyNode",
    "WallNode",
    "OpeningNode",
    "ColumnNode",
    "SlabNode",
    "RoofNode",
    "RailingNode",
    "Point2D",
    "BuildingMetadata",
    "SpatialGraph",
    "SpatialStorey",
    "RoomNode",
    "SpaceNode",
    "FurnitureNode",
    "StoreyElements",
    "Footprint",
]
