"""
Schemas for Text2IFC.

Two intermediate representations are used.  The LLM only emits the first;
the second is derived deterministically from it.

  SpatialGraph     — high-level 3D structure: storeys, element COUNTS,
                     overall footprint, layout hints.  NO coordinates.
                     Produced by the Architect LLM (small JSON, easy to
                     emit even in thinking mode).
        │
        ▼ deterministic expansion (expander.py)
  BuildingGraph    — fully coordinate-resolved description: every wall's
                     start/end, every opening's offset, every column's
                     position.  Consumed by the IFC builder.
                     NEVER emitted by an LLM.

All linear dimensions are in MILLIMETRES, angles in DEGREES.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

Point2D = tuple[float, float]


# ---------------------------------------------------------------------------
# Building elements
# ---------------------------------------------------------------------------

@dataclass
class WallNode:
    """A straight wall segment.

    The wall is built by extruding a rectangular cross-section of width
    ``thickness`` along the line from ``start`` to ``end``, with vertical
    extrusion ``height`` (relative to the storey).
    """
    id: str
    start: Point2D
    end: Point2D
    thickness: float = 200.0
    height: float = 3000.0
    material: str = "Concrete"
    is_external: bool = True
    # NEW: walls that belong to a vertical shaft get a special marker
    shaft_id: str = ""

    @property
    def length(self) -> float:
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx * dx + dy * dy) ** 0.5


@dataclass
class OpeningNode:
    """A door or window cut into ``host_wall`` of the same storey.

    ``offset`` is the distance along the wall (from its start point) to the
    opening's left edge.  ``sill_height`` is the bottom of the opening
    relative to the storey floor.
    """
    id: str
    host_wall: str
    kind: str                 # "door" | "window"
    offset: float             # along-wall offset to left edge (mm)
    width: float
    height: float
    sill_height: float = 0.0  # bottom elevation relative to storey floor


@dataclass
class ColumnNode:
    id: str
    position: Point2D
    section: tuple[float, float] = (400.0, 400.0)  # (XDim, YDim)
    height: float = 3000.0
    material: str = "Concrete"


@dataclass
class SlabNode:
    """A horizontal slab.  ``boundary`` is a closed polygon in storey-local
    coordinates (last vertex automatically connected to first)."""
    id: str
    boundary: list[Point2D]
    thickness: float = 200.0
    elevation: float = 0.0
    material: str = "Concrete"
    predefined_type: str = "FLOOR"   # FLOOR | ROOF | BASESLAB | LANDING


@dataclass
class RoofNode:
    """A flat / pitched roof realised as a slab with PredefinedType=ROOF
    plus optional pitch."""
    id: str
    boundary: list[Point2D]
    thickness: float = 200.0
    elevation: float = 3000.0
    material: str = "Concrete"
    pitch_deg: float = 0.0


@dataclass
class RailingNode:
    id: str
    polyline: list[Point2D]
    height: float = 1100.0
    elevation: float = 0.0
    material: str = "Steel"


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------

@dataclass
class StoreyNode:
    id: str
    name: str
    elevation: float
    height: float = 3000.0
    walls: list[WallNode] = field(default_factory=list)
    openings: list[OpeningNode] = field(default_factory=list)
    columns: list[ColumnNode] = field(default_factory=list)
    slabs: list[SlabNode] = field(default_factory=list)
    roofs: list[RoofNode] = field(default_factory=list)
    railings: list[RailingNode] = field(default_factory=list)
    # Spaces (rooms) are populated by the expander when the SpatialGraph
    # provides rooms; consumed by the builder to emit IfcSpace.
    spaces: list = field(default_factory=list)   # list[SpaceNode]
    # Furniture / fixtures placed directly on the storey (e.g. stairwell
    # railings or lobby benches that are not bound to a specific room).
    furniture: list = field(default_factory=list)   # list[FurnitureNode]


@dataclass
class BuildingMetadata:
    name: str = "Generated Building"
    description: str = ""
    project_name: str = "Text2IFC Project"
    site_name: str = "Default Site"
    schema: str = "IFC4"  # IFC2X3 | IFC4 | IFC4X3
    length_unit: str = "MILLIMETRE"


# ---------------------------------------------------------------------------
# SpatialGraph — high-level 3D structure (no coordinates)
# ---------------------------------------------------------------------------

@dataclass
class Footprint:
    """Overall building footprint."""
    shape: str = "rectangle"      # rectangle | L | U | T | hex | octagon | custom
    x_mm: float = 10000.0
    y_mm: float = 8000.0
    # NEW: explicit polygon boundary for non-rectangular footprints.
    # When populated, ``shape`` is treated as "custom" and x_mm/y_mm are
    # only used for fallback / bounding-box heuristics.
    boundary: list[Point2D] = field(default_factory=list)
    # NEW: interior voids (atrium, courtyard, light-well).  Each void is a
    # closed polygon in the same coordinate frame as ``boundary``.
    voids: list[list[Point2D]] = field(default_factory=list)


@dataclass
class StoreyElements:
    """Element counts + default per-type properties for a single storey.

    ``walls`` is the TOTAL wall count for the storey; the expander decides
    how many are perimeter vs. interior based on the layout hint.
    """
    walls: int = 0
    doors: int = 0
    windows: int = 0
    columns: int = 0
    slabs: int = 0
    roofs: int = 0
    railings: int = 0

    wall_thickness_mm: float = 200.0
    wall_material: str = "Concrete"
    door_width_mm: float = 900.0
    door_height_mm: float = 2100.0
    window_width_mm: float = 1200.0
    window_height_mm: float = 1200.0
    window_sill_mm: float = 900.0
    column_section_mm: tuple[float, float] = (400.0, 400.0)
    column_material: str = "Concrete"
    slab_thickness_mm: float = 200.0
    slab_material: str = "Concrete"
    roof_thickness_mm: float = 200.0
    roof_material: str = "Concrete"
    railing_height_mm: float = 1100.0


@dataclass
class RoomNode:
    """A room (IfcSpace) at the spatial / semantic level — no coordinates.

    Rooms are the primary information the Architect LLM provides; the
    deterministic expander turns them into real walls/openings/columns.
    """
    id: str
    function: str = "office"
    # office | corridor | lobby | meeting | classroom | bathroom | toilet |
    # bedroom | livingroom | kitchen | diningroom | stairwell | reception |
    # service | mechanical | storage | open_space | retail | lab |
    # conference | parking | unknown
    area_ratio: float = 0.1            # of the storey footprint, 0..1
    adjacent_to: list[str] = field(default_factory=list)
    # Subset of adjacent_to that should have an interior door between
    opening_to: list[str] = field(default_factory=list)
    has_external_facade: bool = False
    n_windows: int = 0                 # windows on this room's exterior wall(s)
    n_external_doors: int = 0          # doors to the OUTSIDE (e.g. main entry)
    name: str = ""                     # optional display name
    # NEW: if this room belongs to a vertical shaft (e.g. stairwell shaft),
    # the expander will carve out the shaft footprint instead of BSP-partitioning.
    shaft_id: str = ""
    # NEW: if this room is the structural core on this storey (e.g. elevator
    # core in a core-tube system).
    is_core: bool = False

    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class SpatialStorey:
    """A storey at the spatial / semantic level (no coordinates).

    The storey is described by its ROOMS (a list of RoomNode) and a
    coarse layout_hint.  Element counts are derived per room; bulk
    counts (slabs, roofs, railings, columns) are kept at the storey
    level because they don't belong to a specific room.
    """
    id: str
    name: str
    elevation_mm: float
    height_mm: float = 3000.0
    is_inhabited: bool = True
    # True  → has rooms; expander generates short walls per adjacency.
    # False → roof / mezzanine / attic level: only slabs/roofs/coverings.

    rooms: list[RoomNode] = field(default_factory=list)
    layout_hint: str = "central_corridor"
    # central_corridor | grid | atrium | linear | open_plan

    # Storey-level bulk counts (the rooms-driven path overrides walls /
    # doors / windows derived from rooms).
    elements: StoreyElements = field(default_factory=StoreyElements)
    notes: list[str] = field(default_factory=list)

    # NEW: IDs of vertical shafts that pass through this storey.
    shaft_ids: list[str] = field(default_factory=list)
    # NEW: structural system override for this storey (falls back to global).
    structural_system_override: Optional[str] = None
    # NEW: if this storey has a different footprint from the global one
    # (e.g. setback upper floors, podium + tower).
    footprint_override: Optional[Footprint] = None


# NEW: Vertical shaft (stair, elevator, mechanical) spanning multiple storeys.
@dataclass
class VerticalShaft:
    """A vertical circulation or service shaft spanning multiple storeys.

    The shaft's ``footprint`` is a closed polygon in building-local XY
    coordinates.  It is reproduced on every storey listed in
    ``storey_ids``.  The expander carves this footprint out of the
    storey plan instead of assigning it via BSP partitioning.
    """
    id: str
    kind: str = "stair"          # stair | elevator | mechanical | service
    storey_ids: list[str] = field(default_factory=list)
    # Closed polygon in building-local XY (mm).  If empty, a default
    # rectangle of size ``shaft_mm`` is used.
    footprint: list[Point2D] = field(default_factory=list)
    # Fallback rectangle size when ``footprint`` is empty.
    shaft_mm: tuple[float, float] = (2000.0, 3000.0)  # (dx, dy)
    wall_thickness_mm: float = 200.0
    material: str = "Concrete"


# NEW: Global structural system description.
@dataclass
class StructuralSystem:
    """Building-wide structural system parameters.

    The expander uses these parameters to decide how columns, shear walls,
    and cores are distributed on each storey.
    """
    kind: str = "frame"          # frame | shear_wall | core_tube | mixed | none
    grid_spacing_x_mm: float = 6000.0
    grid_spacing_y_mm: float = 6000.0
    core_position: str = "center"  # center | corner | edge


@dataclass
class SpatialGraph:
    """High-level 3D structure of a building.

    Produced by the Architect LLM.  Contains no coordinates — only counts,
    overall dimensions, and qualitative layout hints.  An expander turns
    this into a BuildingGraph that the IFC builder can materialise.
    """
    metadata: BuildingMetadata = field(default_factory=BuildingMetadata)
    footprint: Footprint = field(default_factory=Footprint)
    storeys: list[SpatialStorey] = field(default_factory=list)

    # NEW: vertical shafts (stair / elevator / mechanical).
    shafts: list[VerticalShaft] = field(default_factory=list)
    # NEW: global structural system.
    structural_system: StructuralSystem = field(default_factory=StructuralSystem)

    # -- Serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "SpatialGraph":
        meta = BuildingMetadata(**(data.get("metadata") or {}))
        fp_raw = data.get("footprint") or {}
        fp = _coerce_footprint(fp_raw)
        storeys: list[SpatialStorey] = []
        for s in data.get("storeys", []) or []:
            elems = _coerce_storey_elements(s.get("elements", {}) or {})
            rooms = [_coerce_room(r) for r in (s.get("rooms") or [])]
            storeys.append(SpatialStorey(
                id=str(s.get("id") or s.get("name") or "storey"),
                name=str(s.get("name", "Storey")),
                elevation_mm=float(s.get("elevation_mm", 0.0)),
                height_mm=float(s.get("height_mm", 3000.0)),
                is_inhabited=bool(s.get("is_inhabited", True)),
                rooms=rooms,
                elements=elems,
                layout_hint=str(s.get("layout_hint", "central_corridor")),
                notes=list(s.get("notes", []) or []),
                shaft_ids=[str(x) for x in (s.get("shaft_ids") or [])],
                structural_system_override=s.get("structural_system_override") or None,
                footprint_override=_coerce_footprint(s.get("footprint_override")) if s.get("footprint_override") else None,
            ))
        shafts = [_coerce_shaft(sh) for sh in (data.get("shafts") or [])]
        ss_raw = data.get("structural_system") or {}
        structural_system = StructuralSystem(
            kind=str(ss_raw.get("kind", "frame")),
            grid_spacing_x_mm=float(ss_raw.get("grid_spacing_x_mm", 6000.0)),
            grid_spacing_y_mm=float(ss_raw.get("grid_spacing_y_mm", 6000.0)),
            core_position=str(ss_raw.get("core_position", "center")),
        )
        return cls(
            metadata=meta,
            footprint=fp,
            storeys=storeys,
            shafts=shafts,
            structural_system=structural_system,
        )

    @classmethod
    def from_json(cls, text: str) -> "SpatialGraph":
        return cls.from_dict(json.loads(text))

    def stats(self) -> dict:
        """Aggregate element counts (for quick comparison)."""
        counts = {
            "storeys": len(self.storeys),
            "walls": 0, "doors": 0, "windows": 0,
            "columns": 0, "slabs": 0, "roofs": 0, "railings": 0,
            "shafts": len(self.shafts),
        }
        for s in self.storeys:
            counts["walls"]    += s.elements.walls
            counts["doors"]    += s.elements.doors
            counts["windows"]  += s.elements.windows
            counts["columns"] += s.elements.columns
            counts["slabs"]    += s.elements.slabs
            counts["roofs"]    += s.elements.roofs
            counts["railings"] += s.elements.railings
        return counts


def _coerce_footprint(raw: dict) -> Footprint:
    """Forgiving constructor for a Footprint from JSON."""
    if not isinstance(raw, dict):
        raw = {}
    boundary = _tuplist(raw.get("boundary") or [])
    voids_raw = raw.get("voids") or []
    voids = [_tuplist(v) for v in voids_raw if v]
    return Footprint(
        shape=str(raw.get("shape", "rectangle")),
        x_mm=float(raw.get("x_mm", 10000.0)),
        y_mm=float(raw.get("y_mm", 8000.0)),
        boundary=boundary,
        voids=voids,
    )


def _coerce_shaft(raw: dict) -> VerticalShaft:
    """Forgiving constructor for a VerticalShaft from JSON."""
    if not isinstance(raw, dict):
        raw = {}
    footprint = _tuplist(raw.get("footprint") or [])
    shaft_mm = raw.get("shaft_mm", (2000.0, 3000.0))
    if isinstance(shaft_mm, (list, tuple)) and len(shaft_mm) >= 2:
        shaft_tuple = (float(shaft_mm[0]), float(shaft_mm[1]))
    else:
        shaft_tuple = (2000.0, 3000.0)
    return VerticalShaft(
        id=str(raw.get("id") or raw.get("name") or "shaft"),
        kind=str(raw.get("kind", "stair")),
        storey_ids=[str(x) for x in (raw.get("storey_ids") or [])],
        footprint=footprint,
        shaft_mm=shaft_tuple,
        wall_thickness_mm=float(raw.get("wall_thickness_mm", 200.0)),
        material=str(raw.get("material", "Concrete")),
    )


def _coerce_room(raw: dict) -> RoomNode:
    """Forgiving constructor for a RoomNode from JSON."""
    return RoomNode(
        id=str(raw.get("id") or raw.get("name") or "room"),
        name=str(raw.get("name") or raw.get("id") or ""),
        function=str(raw.get("function", "office")),
        area_ratio=float(raw.get("area_ratio", 0.1)),
        adjacent_to=[str(x) for x in (raw.get("adjacent_to") or [])],
        opening_to=[str(x) for x in (raw.get("opening_to") or [])],
        has_external_facade=bool(raw.get("has_external_facade", False)),
        n_windows=int(raw.get("n_windows", 0)),
        n_external_doors=int(raw.get("n_external_doors", 0)),
        shaft_id=str(raw.get("shaft_id", "")),
        is_core=bool(raw.get("is_core", False)),
    )


def _coerce_storey_elements(raw: dict) -> StoreyElements:
    """Forgiving constructor for StoreyElements from JSON."""
    sec = raw.get("column_section_mm", (400.0, 400.0))
    if isinstance(sec, (list, tuple)) and len(sec) >= 2:
        sec_tuple = (float(sec[0]), float(sec[1]))
    else:
        sec_tuple = (400.0, 400.0)
    return StoreyElements(
        walls=int(raw.get("walls", 0)),
        doors=int(raw.get("doors", 0)),
        windows=int(raw.get("windows", 0)),
        columns=int(raw.get("columns", 0)),
        slabs=int(raw.get("slabs", 0)),
        roofs=int(raw.get("roofs", 0)),
        railings=int(raw.get("railings", 0)),
        wall_thickness_mm=float(raw.get("wall_thickness_mm", 200.0)),
        wall_material=str(raw.get("wall_material", "Concrete")),
        door_width_mm=float(raw.get("door_width_mm", 900.0)),
        door_height_mm=float(raw.get("door_height_mm", 2100.0)),
        window_width_mm=float(raw.get("window_width_mm", 1200.0)),
        window_height_mm=float(raw.get("window_height_mm", 1200.0)),
        window_sill_mm=float(raw.get("window_sill_mm", 900.0)),
        column_section_mm=sec_tuple,
        column_material=str(raw.get("column_material", "Concrete")),
        slab_thickness_mm=float(raw.get("slab_thickness_mm", 200.0)),
        slab_material=str(raw.get("slab_material", "Concrete")),
        roof_thickness_mm=float(raw.get("roof_thickness_mm", 200.0)),
        roof_material=str(raw.get("roof_material", "Concrete")),
        railing_height_mm=float(raw.get("railing_height_mm", 1100.0)),
    )


# ---------------------------------------------------------------------------
# BuildingGraph — low-level coordinate-resolved description
# (Never produced by an LLM; only by the expander.)
# ---------------------------------------------------------------------------

@dataclass
class FurnitureNode:
    """A piece of furniture, fixture, sanitary terminal or equipment
    placed inside a room.

    ``ifc_class`` chooses the IFC entity to emit:
      * ``IfcFurniture``         → desks, chairs, tables, beds, sofas
      * ``IfcSanitaryTerminal``  → toilet pan, washbasin, urinal, sink
      * ``IfcBuildingElementProxy`` → blackboard / whiteboard / generic
      * ``IfcLightFixture``      → lights
      * ``IfcStair`` / ``IfcStairFlight`` → stairs

    ``predefined_type`` maps to the corresponding IFC PredefinedType
    enum (e.g. CHAIR / DESK / TOILETPAN / WASHHANDBASIN).
    """
    id: str
    ifc_class: str = "IfcFurniture"
    predefined_type: str = "NOTDEFINED"
    name: str = ""
    position: Point2D = (0.0, 0.0)         # storey-local XY of footprint centre
    size: tuple[float, float, float] = (600.0, 600.0, 750.0)  # (dx, dy, dz)
    rot_z_deg: float = 0.0                 # rotation around Z (degrees)
    elevation: float = 0.0                 # bottom of object relative to storey
    material: str = "Wood"


@dataclass
class SpaceNode:
    """A room as a geometric rectangle on a storey.

    Produced by the expander (one per RoomNode) and consumed by the
    builder to emit an IfcSpace.

    ``door_side`` indicates which wall of the room hosts the (primary)
    door into the corridor — one of ``"-x" | "+x" | "-y" | "+y"`` — so
    that the furnishing pass can put the blackboard / teacher zone on
    the OPPOSITE or PERPENDICULAR wall instead of overlapping the door.
    ``window_side`` is the wall carrying the room's external windows.
    """
    id: str
    name: str = ""
    function: str = ""
    boundary: list[Point2D] = field(default_factory=list)
    elevation: float = 0.0
    height: float = 3000.0
    furniture: list = field(default_factory=list)   # list[FurnitureNode]
    door_side: str = ""        # "" | "-x" | "+x" | "-y" | "+y"
    window_side: str = ""      # "" | "-x" | "+x" | "-y" | "+y"


@dataclass
class BuildingGraph:
    """Top-level hierarchical building description."""
    metadata: BuildingMetadata = field(default_factory=BuildingMetadata)
    storeys: list[StoreyNode] = field(default_factory=list)

    # -- Serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "BuildingGraph":
        meta_raw = data.get("metadata", {})
        meta = BuildingMetadata(**meta_raw)

        storeys: list[StoreyNode] = []
        for s_raw in data.get("storeys", []):
            storey = StoreyNode(
                id=s_raw["id"],
                name=s_raw.get("name", s_raw["id"]),
                elevation=float(s_raw.get("elevation", 0.0)),
                height=float(s_raw.get("height", 3000.0)),
                walls=[WallNode(**_coerce_wall(w)) for w in s_raw.get("walls", [])],
                openings=[OpeningNode(**_coerce_opening(o)) for o in s_raw.get("openings", [])],
                columns=[ColumnNode(**_coerce_column(c)) for c in s_raw.get("columns", [])],
                slabs=[SlabNode(**_coerce_slab(sl)) for sl in s_raw.get("slabs", [])],
                roofs=[RoofNode(**_coerce_roof(r)) for r in s_raw.get("roofs", [])],
                railings=[RailingNode(**_coerce_railing(rg)) for rg in s_raw.get("railings", [])],
            )
            storeys.append(storey)
        return cls(metadata=meta, storeys=storeys)

    @classmethod
    def from_json(cls, text: str) -> "BuildingGraph":
        return cls.from_dict(json.loads(text))

    # -- Convenience --------------------------------------------------------

    def stats(self) -> dict:
        """Aggregate element counts for quick comparison."""
        counts = {
            "storeys": len(self.storeys),
            "walls": 0,
            "doors": 0,
            "windows": 0,
            "columns": 0,
            "slabs": 0,
            "roofs": 0,
            "railings": 0,
        }
        for s in self.storeys:
            counts["walls"] += len(s.walls)
            counts["doors"] += sum(1 for o in s.openings if o.kind == "door")
            counts["windows"] += sum(1 for o in s.openings if o.kind == "window")
            counts["columns"] += len(s.columns)
            counts["slabs"] += len(s.slabs)
            counts["roofs"] += len(s.roofs)
            counts["railings"] += len(s.railings)
        return counts


# ---------------------------------------------------------------------------
# Coercion helpers — accept tuples-as-lists from JSON
# ---------------------------------------------------------------------------

def _tup2(v) -> Point2D:
    return (float(v[0]), float(v[1]))


def _tuplist(seq) -> list[Point2D]:
    return [_tup2(p) for p in seq]


def _coerce_wall(w: dict) -> dict:
    return {
        "id": str(w["id"]),
        "start": _tup2(w["start"]),
        "end": _tup2(w["end"]),
        "thickness": float(w.get("thickness", 200.0)),
        "height": float(w.get("height", 3000.0)),
        "material": str(w.get("material", "Concrete")),
        "is_external": bool(w.get("is_external", True)),
        "shaft_id": str(w.get("shaft_id", "")),
    }


def _coerce_opening(o: dict) -> dict:
    return {
        "id": str(o["id"]),
        "host_wall": str(o["host_wall"]),
        "kind": str(o.get("kind", "door")).lower(),
        "offset": float(o.get("offset", 0.0)),
        "width": float(o.get("width", 900.0)),
        "height": float(o.get("height", 2100.0)),
        "sill_height": float(o.get("sill_height", 0.0)),
    }


def _coerce_column(c: dict) -> dict:
    sec = c.get("section", (400.0, 400.0))
    return {
        "id": str(c["id"]),
        "position": _tup2(c["position"]),
        "section": (float(sec[0]), float(sec[1])),
        "height": float(c.get("height", 3000.0)),
        "material": str(c.get("material", "Concrete")),
    }


def _coerce_slab(s: dict) -> dict:
    return {
        "id": str(s["id"]),
        "boundary": _tuplist(s["boundary"]),
        "thickness": float(s.get("thickness", 200.0)),
        "elevation": float(s.get("elevation", 0.0)),
        "material": str(s.get("material", "Concrete")),
        "predefined_type": str(s.get("predefined_type", "FLOOR")).upper(),
    }


def _coerce_roof(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "boundary": _tuplist(r["boundary"]),
        "thickness": float(r.get("thickness", 200.0)),
        "elevation": float(r.get("elevation", 3000.0)),
        "material": str(r.get("material", "Concrete")),
        "pitch_deg": float(r.get("pitch_deg", 0.0)),
    }


def _coerce_railing(rg: dict) -> dict:
    return {
        "id": str(rg["id"]),
        "polyline": _tuplist(rg["polyline"]),
        "height": float(rg.get("height", 1100.0)),
        "elevation": float(rg.get("elevation", 0.0)),
        "material": str(rg.get("material", "Steel")),
    }
