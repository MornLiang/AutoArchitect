"""
Deterministic expansion: SpatialGraph → BuildingGraph.

Pipeline:

    SpatialGraph (rooms + adjacency + footprint)
            │
            ▼ BSP partitioning by area_ratio
        rectangular room layout (a Rect per RoomNode)
            │
            ▼ extract every shared edge between two rooms (interior wall
            │ candidates) + every edge that lies on the storey perimeter
            │ (exterior wall candidates).
            ▼
        WallNodes (short, with start/end on world XY)
            │
            ▼ openings: doors on `opening_to` interior walls,
            │           windows on rooms that face the outside
            ▼
        BuildingGraph
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from ifc_agent.text2ifc.schemas import (
    BuildingGraph,
    BuildingMetadata,
    ColumnNode,
    Footprint,
    FurnitureNode,
    OpeningNode,
    Point2D,
    RailingNode,
    RoofNode,
    RoomNode,
    SlabNode,
    SpaceNode,
    SpatialGraph,
    SpatialStorey,
    StructuralSystem,
    StoreyElements,
    StoreyNode,
    WallNode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _Rect:
    """A storey-relative rectangle: [x0..x1] × [y0..y1] in mm."""
    x0: float
    y0: float
    x1: float
    y1: float
    room_id: str = ""

    @property
    def w(self) -> float: return self.x1 - self.x0
    @property
    def h(self) -> float: return self.y1 - self.y0
    @property
    def area(self) -> float: return self.w * self.h
    @property
    def cx(self) -> float: return (self.x0 + self.x1) * 0.5
    @property
    def cy(self) -> float: return (self.y0 + self.y1) * 0.5


def _shared_segment(a: _Rect, b: _Rect, *, tol: float = 1.0
                    ) -> Optional[tuple[Point2D, Point2D, str]]:
    """If *a* and *b* share an edge (within ``tol``), return
    (p_start, p_end, orientation).  Orientation is "H" (horizontal,
    parallel to X) or "V" (vertical, parallel to Y).  Returns None
    otherwise."""
    # Vertical shared edge: a is on the left of b (or vice versa)
    if abs(a.x1 - b.x0) < tol:
        y0 = max(a.y0, b.y0)
        y1 = min(a.y1, b.y1)
        if y1 - y0 > tol:
            return ((a.x1, y0), (a.x1, y1), "V")
    if abs(b.x1 - a.x0) < tol:
        y0 = max(a.y0, b.y0)
        y1 = min(a.y1, b.y1)
        if y1 - y0 > tol:
            return ((a.x0, y0), (a.x0, y1), "V")
    # Horizontal shared edge: a below b (or vice versa)
    if abs(a.y1 - b.y0) < tol:
        x0 = max(a.x0, b.x0)
        x1 = min(a.x1, b.x1)
        if x1 - x0 > tol:
            return ((x0, a.y1), (x1, a.y1), "H")
    if abs(b.y1 - a.y0) < tol:
        x0 = max(a.x0, b.x0)
        x1 = min(a.x1, b.x1)
        if x1 - x0 > tol:
            return ((x0, a.y0), (x1, a.y0), "H")
    return None


def _side_of_edge(rect: _Rect, p0: Point2D, p1: Point2D,
                  tol: float = 1.0) -> str:
    """Classify which side of ``rect`` the segment (p0, p1) lies on.

    Returns one of ``"-x" | "+x" | "-y" | "+y"`` or ``""``.
    """
    x0, x1 = sorted((p0[0], p1[0]))
    y0, y1 = sorted((p0[1], p1[1]))
    if abs(x0 - rect.x0) < tol and abs(x1 - rect.x0) < tol:
        return "-x"
    if abs(x0 - rect.x1) < tol and abs(x1 - rect.x1) < tol:
        return "+x"
    if abs(y0 - rect.y0) < tol and abs(y1 - rect.y0) < tol:
        return "-y"
    if abs(y0 - rect.y1) < tol and abs(y1 - rect.y1) < tol:
        return "+y"
    return ""


def _detect_door_side(room: "RoomNode", rect: _Rect, all_rooms,
                      rects: dict, corridor_funcs: set) -> str:
    """Find which side of ``rect`` the door into the main circulation
    (corridor/lobby) is on.  Falls back to the side that shares the
    longest edge with a corridor-like neighbour."""
    by_id = {r.id: r for r in all_rooms}
    candidates: list[tuple[float, str]] = []
    for nb_id in room.opening_to:
        nb = by_id.get(nb_id)
        nb_rect = rects.get(nb_id)
        if nb is None or nb_rect is None:
            continue
        seg = _shared_segment(rect, nb_rect)
        if seg is None:
            continue
        side = _side_of_edge(rect, seg[0], seg[1])
        if not side:
            continue
        length = ((seg[1][0] - seg[0][0]) ** 2
                  + (seg[1][1] - seg[0][1]) ** 2) ** 0.5
        priority = 2.0 if nb.function in corridor_funcs else 1.0
        candidates.append((length * priority, side))
    if not candidates:
        # Fall back: longest shared edge with any corridor-like room
        for nb_id, nb_rect in rects.items():
            if nb_id == room.id:
                continue
            nb = by_id.get(nb_id)
            if nb is None or nb.function not in corridor_funcs:
                continue
            seg = _shared_segment(rect, nb_rect)
            if seg is None:
                continue
            side = _side_of_edge(rect, seg[0], seg[1])
            if not side:
                continue
            length = ((seg[1][0] - seg[0][0]) ** 2
                      + (seg[1][1] - seg[0][1]) ** 2) ** 0.5
            candidates.append((length, side))
    if not candidates:
        return ""
    candidates.sort(key=lambda t: -t[0])
    return candidates[0][1]


def _detect_window_side(rect: _Rect, *, fx: float, fy: float,
                        avoid: str = "", tol: float = 1.0) -> str:
    """Find an external-facing side of ``rect``, preferring one that is
    NOT ``avoid`` (typically the door side)."""
    sides: list[tuple[float, str]] = []
    if abs(rect.x0) < tol:
        sides.append((rect.h, "-x"))
    if abs(rect.x1 - fx) < tol:
        sides.append((rect.h, "+x"))
    if abs(rect.y0) < tol:
        sides.append((rect.w, "-y"))
    if abs(rect.y1 - fy) < tol:
        sides.append((rect.w, "+y"))
    if not sides:
        return ""
    sides.sort(key=lambda t: -t[0])  # longest first
    for length, side in sides:
        if side != avoid:
            return side
    return sides[0][1]


def _exterior_edges_of_rect(r: _Rect, *, fx: float, fy: float,
                            tol: float = 1.0) -> list[tuple[Point2D, Point2D, str]]:
    """Return the sub-edges of *r* that lie on the storey perimeter."""
    out: list[tuple[Point2D, Point2D, str]] = []
    # bottom (y = 0)
    if abs(r.y0 - 0.0) < tol:
        out.append(((r.x0, 0.0), (r.x1, 0.0), "H"))
    # top (y = fy)
    if abs(r.y1 - fy) < tol:
        out.append(((r.x0, fy), (r.x1, fy), "H"))
    # left (x = 0)
    if abs(r.x0 - 0.0) < tol:
        out.append(((0.0, r.y0), (0.0, r.y1), "V"))
    # right (x = fx)
    if abs(r.x1 - fx) < tol:
        out.append(((fx, r.y0), (fx, r.y1), "V"))
    return out


def _polygon_area(poly: list[Point2D]) -> float:
    if len(poly) < 3:
        return 0.0
    acc = 0.0
    for i, p in enumerate(poly):
        q = poly[(i + 1) % len(poly)]
        acc += p[0] * q[1] - q[0] * p[1]
    return abs(acc) * 0.5


def _point_in_poly(p: Point2D, poly: list[Point2D]) -> bool:
    """Ray-casting point-in-polygon test."""
    if len(poly) < 3:
        return True
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)):
            x_cross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _default_boundary(fp: Footprint) -> list[Point2D]:
    fx, fy = float(fp.x_mm), float(fp.y_mm)
    shape = fp.shape.lower()
    if fp.boundary:
        return list(fp.boundary)
    if shape == "l":
        return [(0, 0), (fx, 0), (fx, fy * 0.45), (fx * 0.55, fy * 0.45),
                (fx * 0.55, fy), (0, fy)]
    if shape == "u":
        return [(0, 0), (fx, 0), (fx, fy), (fx * 0.65, fy),
                (fx * 0.65, fy * 0.40), (fx * 0.35, fy * 0.40),
                (fx * 0.35, fy), (0, fy)]
    if shape == "t":
        return [(0, 0), (fx, 0), (fx, fy * 0.35), (fx * 0.62, fy * 0.35),
                (fx * 0.62, fy), (fx * 0.38, fy), (fx * 0.38, fy * 0.35),
                (0, fy * 0.35)]
    if shape in {"hex", "octagon"}:
        n = 6 if shape == "hex" else 8
        cx, cy = fx * 0.5, fy * 0.5
        rx, ry = fx * 0.5, fy * 0.5
        return [
            (cx + rx * math.cos(2 * math.pi * i / n + math.pi / n),
             cy + ry * math.sin(2 * math.pi * i / n + math.pi / n))
            for i in range(n)
        ]
    return [(0, 0), (fx, 0), (fx, fy), (0, fy)]


def _effective_footprint(sg: SpatialGraph, sp: SpatialStorey) -> Footprint:
    return sp.footprint_override or sg.footprint


def _polygon_bsp_partition(
    rooms: list[RoomNode],
    fp: Footprint,
    *,
    min_room_dim: float = 1500.0,
) -> dict[str, _Rect]:
    rects = _bsp_partition(rooms, float(fp.x_mm), float(fp.y_mm),
                           min_room_dim=min_room_dim)
    boundary = _default_boundary(fp)
    voids = fp.voids
    if fp.shape.lower() == "rectangle" and not fp.boundary and not voids:
        return rects
    filtered: dict[str, _Rect] = {}
    for rid, rect in rects.items():
        center = (rect.cx, rect.cy)
        if not _point_in_poly(center, boundary):
            continue
        if any(_point_in_poly(center, void) for void in voids):
            continue
        filtered[rid] = rect
    if filtered:
        return filtered
    # Degenerate custom footprints should not make the whole building blank.
    return rects


def _footprint_area(fp: Footprint) -> float:
    area = _polygon_area(_default_boundary(fp)) or float(fp.x_mm) * float(fp.y_mm)
    for void in fp.voids:
        area -= _polygon_area(void)
    return max(0.0, area)


# ---------------------------------------------------------------------------
# BSP room partitioning
# ---------------------------------------------------------------------------

def _bsp_partition(rooms: list[RoomNode], fx: float, fy: float,
                   *, min_room_dim: float = 1500.0) -> dict[str, _Rect]:
    """Cut a (fx × fy) footprint into rectangles, one per room.

    Strategy — **slice-and-dice** (a.k.a. row-major treemap), which is
    better-balanced than naive recursive BSP because it processes
    rooms in groups rather than one-at-a-time.  This produces a
    plausible "central corridor" plan automatically when one of the
    rooms is much wider than the others.

    Algorithm:
      1. Pull out the room with function == "corridor".  It always
         becomes a horizontal band of height ≈ corridor.area_ratio·fy.
      2. Split remaining rooms into "north" and "south" relative to
         the corridor, balancing total area_ratio between the two sides.
      3. Within each side, lay rooms out as a horizontal row whose
         widths sum to fx and are proportional to area_ratio.

    Rectangles are returned with their ``room_id`` filled in.  If a
    cell would be smaller than ``min_room_dim`` in either dimension,
    we grow it at the expense of its neighbour to maintain a minimum.
    """
    if not rooms:
        return {}

    # Normalise area ratios so they sum to 1
    total_ratio = sum(max(r.area_ratio, 1e-6) for r in rooms)
    norm = {r.id: max(r.area_ratio, 1e-6) / total_ratio for r in rooms}

    # --- Step 1: corridor band -------------------------------------------
    corridor_room = next((r for r in rooms if r.function == "corridor"), None)

    placed: dict[str, _Rect] = {}

    if corridor_room is not None:
        # Place the corridor as a horizontal band roughly in the middle.
        corr_h = max(min_room_dim, fy * norm[corridor_room.id])
        corr_h = min(corr_h, fy * 0.35)  # cap so it doesn't dominate
        # Center vertically
        cy0 = (fy - corr_h) * 0.5
        cy1 = cy0 + corr_h
        placed[corridor_room.id] = _Rect(0, cy0, fx, cy1,
                                         room_id=corridor_room.id)
        remaining = [r for r in rooms if r.id != corridor_room.id]
    else:
        # No corridor — treat whole footprint as a single band
        cy0, cy1 = fy * 0.5, fy * 0.5
        remaining = list(rooms)

    # --- Step 2: split remaining into north/south rows -------------------
    # We *preserve the original input order* so adjacent rooms in the
    # LLM's rooms[] list end up adjacent in the final layout.  The split
    # point is chosen so the two halves have roughly equal total area
    # ratio.
    total_remaining = sum(norm[r.id] for r in remaining)
    half = total_remaining / 2.0
    north_band, south_band = [], []
    running = 0.0
    cut_idx = 0
    for i, r in enumerate(remaining):
        running += norm[r.id]
        if running >= half:
            cut_idx = i + 1
            break
    if cut_idx == 0 or cut_idx >= len(remaining):
        cut_idx = len(remaining) // 2
    north_band = remaining[:cut_idx]
    south_band = remaining[cut_idx:]

    # --- Step 3: lay out each row (north → top of footprint, south → bottom)
    def _row_layout(band: list[RoomNode], y0: float, y1: float):
        if not band:
            return
        widths_total = sum(norm[r.id] for r in band)
        if widths_total <= 0:
            return
        x_cur = 0.0
        for i, r in enumerate(band):
            is_last = (i == len(band) - 1)
            if is_last:
                x_next = fx
            else:
                share = norm[r.id] / widths_total
                w = max(min_room_dim, fx * share)
                x_next = min(fx, x_cur + w)
            placed[r.id] = _Rect(x_cur, y0, x_next, y1, room_id=r.id)
            x_cur = x_next

    # North row sits above the corridor (closer to y = fy)
    _row_layout(north_band, cy1, fy)
    # South row sits below the corridor (closer to y = 0)
    _row_layout(south_band, 0.0, cy0)

    return placed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def expand_spatial_to_geometric(sg: SpatialGraph,
                                *, centered: bool = True) -> BuildingGraph:
    """Turn a high-level SpatialGraph into a coordinate-resolved BuildingGraph.

    The building is *centered on the world origin* by default (``centered=True``):
    its footprint occupies ``[-fx/2, fx/2] × [-fy/2, fy/2]`` in storey-local
    coordinates.  This matches the BIM convention where the IfcSite/IfcBuilding
    placement is at the building's centroid, not its bottom-left corner.
    Pass ``centered=False`` to keep the legacy "first-quadrant" placement.

    Two code paths:
      * ``storey.rooms`` populated (preferred) → BSP partition + short
        walls along every adjacency edge.
      * No rooms → fall back to the legacy "perimeter + interior grid"
        layout from element counts.  This keeps backward compatibility
        with older SpatialGraph JSONs.
    """
    storeys: list[StoreyNode] = []
    for sp in sg.storeys:
        fp = _effective_footprint(sg, sp)
        fx = float(fp.x_mm)
        fy = float(fp.y_mm)
        sn = StoreyNode(
            id=str(sp.id),
            name=str(sp.name),
            elevation=float(sp.elevation_mm),
            height=float(sp.height_mm),
        )
        if sp.is_inhabited and sp.rooms:
            _layout_storey_from_rooms(sn, sp, fp=fp, structural=sg.structural_system)
        else:
            _layout_storey_from_counts(sn, sp, fp=fp, structural=sg.structural_system)
        _emit_shafts(sn, sp, sg.shafts)
        if centered:
            _recenter_storey(sn, dx=-fx / 2.0, dy=-fy / 2.0)
        storeys.append(sn)

    meta = BuildingMetadata(
        name=sg.metadata.name,
        description=sg.metadata.description,
        project_name=sg.metadata.project_name,
        site_name=sg.metadata.site_name,
        schema=sg.metadata.schema,
        length_unit=sg.metadata.length_unit,
    )
    return BuildingGraph(metadata=meta, storeys=storeys)


def _recenter_storey(storey: StoreyNode, *, dx: float, dy: float) -> None:
    """Translate every 2-D coordinate in ``storey`` by ``(dx, dy)`` in place.

    Used to move the building so its footprint is centred on the world
    origin instead of sitting in the first quadrant.  ``elevation`` /
    ``height`` (Z) are untouched – only the XY plane is shifted.
    """
    def _shift(p: Point2D) -> Point2D:
        return (float(p[0]) + dx, float(p[1]) + dy)

    for w in storey.walls:
        w.start = _shift(w.start)
        w.end = _shift(w.end)
    # Openings store along-wall offsets, not world coords → nothing to shift.
    for c in storey.columns:
        c.position = _shift(c.position)
    for s in storey.slabs:
        s.boundary = [_shift(p) for p in s.boundary]
    for r in storey.roofs:
        r.boundary = [_shift(p) for p in r.boundary]
    for rl in storey.railings:
        rl.polyline = [_shift(p) for p in rl.polyline]
    for sp in storey.spaces:
        sp.boundary = [_shift(p) for p in sp.boundary]
        for f in sp.furniture:
            f.position = _shift(f.position)
    for f in storey.furniture:
        f.position = _shift(f.position)


# ---------------------------------------------------------------------------
# Rooms-driven layout (preferred path)
# ---------------------------------------------------------------------------

def _layout_storey_from_rooms(
    storey: StoreyNode,
    sp: SpatialStorey,
    *,
    fp: Footprint,
    structural: StructuralSystem,
) -> None:
    el = sp.elements
    fx, fy = float(fp.x_mm), float(fp.y_mm)
    rects = _polygon_bsp_partition(sp.rooms, fp)
    room_by_id = {r.id: r for r in sp.rooms}

    # --- Collect wall segments ---
    # Interior walls: every pair of adjacent rooms contributes one short
    # wall along their shared edge.  We deduplicate by frozenset(ids).
    interior_segs: dict[frozenset, tuple[Point2D, Point2D, str, str, str]] = {}
    # Map: pair-id-set → (p0, p1, orient, room_a, room_b)

    # Build adjacency: explicit + geometric (any pair whose rectangles share
    # an edge is considered adjacent, in case the Architect under-specifies).
    adjacency_pairs: set[frozenset] = set()
    for r in sp.rooms:
        for nb in r.adjacent_to:
            if nb in room_by_id:
                adjacency_pairs.add(frozenset((r.id, nb)))

    room_ids = list(room_by_id.keys())
    for i in range(len(room_ids)):
        for j in range(i + 1, len(room_ids)):
            a_id, b_id = room_ids[i], room_ids[j]
            if a_id not in rects or b_id not in rects:
                continue
            seg = _shared_segment(rects[a_id], rects[b_id])
            if seg is None:
                continue
            adjacency_pairs.add(frozenset((a_id, b_id)))
            interior_segs[frozenset((a_id, b_id))] = (
                seg[0], seg[1], seg[2], a_id, b_id,
            )

    # Exterior walls: every rect edge on the storey perimeter becomes an
    # exterior wall.  We split by room so each segment maps to a known
    # room (useful for window placement).
    exterior_segs: list[tuple[Point2D, Point2D, str, str]] = []
    # (p0, p1, orient, room_id)
    for r in sp.rooms:
        if r.id not in rects:
            continue
        for p0, p1, o in _exterior_edges_of_rect(rects[r.id], fx=fx, fy=fy):
            exterior_segs.append((p0, p1, o, r.id))

    # --- Materialise as WallNodes ---
    walls: list[WallNode] = []
    wall_lookup: dict[str, WallNode] = {}

    def _add_wall(prefix: str, p0, p1, *, external: bool):
        idx = len(walls)
        wid = f"{storey.id}-{prefix}{idx}"
        w = WallNode(
            id=wid, start=p0, end=p1,
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=external,
        )
        walls.append(w)
        wall_lookup[wid] = w
        return w

    # Interior walls
    interior_wall_ids_by_pair: dict[frozenset, str] = {}
    for pair, (p0, p1, o, a_id, b_id) in interior_segs.items():
        w = _add_wall("iw", p0, p1, external=False)
        interior_wall_ids_by_pair[pair] = w.id

    # Exterior walls
    exterior_wall_ids_by_room: dict[str, list[str]] = {}
    for p0, p1, o, rid in exterior_segs:
        w = _add_wall("ew", p0, p1, external=True)
        exterior_wall_ids_by_room.setdefault(rid, []).append(w.id)

    storey.walls.extend(walls)

    # --- Doors (interior + exterior) ---
    doors_emitted = 0

    def _add_door(host_wall: WallNode, position_along: float = 0.5):
        nonlocal doors_emitted
        # Shrink the door if the wall is too short.  Always emit *something*
        # so the topology (room ↔ corridor connections) is preserved.
        if host_wall.length < 400.0:
            return  # truly degenerate wall — can't fit any door
        width = min(el.door_width_mm, max(400.0, host_wall.length - 100.0))
        offset = max(50.0, host_wall.length * position_along - width * 0.5)
        offset = min(offset, host_wall.length - width - 50.0)
        offset = max(50.0, offset)
        storey.openings.append(OpeningNode(
            id=f"{storey.id}-d{doors_emitted + 1}",
            host_wall=host_wall.id, kind="door",
            offset=offset,
            width=width,
            height=el.door_height_mm,
            sill_height=0.0,
        ))
        doors_emitted += 1

    # 1) Interior doors on every (a, b) listed in opening_to.  We dedup
    # by pair so a symmetric A.opening_to=[B] + B.opening_to=[A]
    # declaration produces a SINGLE door.
    seen_pairs: set[frozenset] = set()
    for r in sp.rooms:
        for nb in r.opening_to:
            pair = frozenset((r.id, nb))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if pair in interior_wall_ids_by_pair:
                _add_door(wall_lookup[interior_wall_ids_by_pair[pair]])

    # If the LLM forgot opening_to entirely, fall back to: one door
    # between every pair where at least one side is corridor / lobby.
    if doors_emitted == 0:
        corridor_funcs = {"corridor", "lobby", "circulation", "open_space"}
        for pair, wid in interior_wall_ids_by_pair.items():
            a_id, b_id = tuple(pair)
            a, b = room_by_id.get(a_id), room_by_id.get(b_id)
            if a and b and (a.function in corridor_funcs
                            or b.function in corridor_funcs):
                _add_door(wall_lookup[wid])

    # 2) External doors per room.n_external_doors
    for r in sp.rooms:
        if r.n_external_doors <= 0:
            continue
        ext_ids = exterior_wall_ids_by_room.get(r.id, [])
        for k in range(r.n_external_doors):
            if not ext_ids:
                break
            host = wall_lookup[ext_ids[k % len(ext_ids)]]
            _add_door(host, position_along=0.5)

    # --- Windows on each room's exterior facade ---
    windows_emitted = 0

    def _add_window(host_wall: WallNode, position_along: float):
        nonlocal windows_emitted
        if host_wall.length < 500.0:
            return
        width = min(el.window_width_mm, max(500.0, host_wall.length - 100.0))
        offset = max(50.0, host_wall.length * position_along - width * 0.5)
        offset = min(offset, host_wall.length - width - 50.0)
        offset = max(50.0, offset)
        storey.openings.append(OpeningNode(
            id=f"{storey.id}-w{windows_emitted + 1}",
            host_wall=host_wall.id, kind="window",
            offset=offset,
            width=width,
            height=el.window_height_mm,
            sill_height=el.window_sill_mm,
        ))
        windows_emitted += 1

    for r in sp.rooms:
        if r.n_windows <= 0:
            continue
        ext_ids = exterior_wall_ids_by_room.get(r.id, [])
        if not ext_ids and r.has_external_facade:
            # Room is flagged exterior but no exterior wall: skip safely.
            continue
        # Distribute windows evenly across the room's exterior walls.
        per_wall = max(1, r.n_windows // max(1, len(ext_ids)))
        remaining = r.n_windows
        for wid in ext_ids:
            if remaining <= 0:
                break
            host = wall_lookup[wid]
            for k in range(per_wall):
                if remaining <= 0:
                    break
                # Evenly spaced along the wall
                pos = (k + 1) / (per_wall + 1)
                _add_window(host, position_along=pos)
                remaining -= 1
        # Anything left over: dump on the room's first exterior wall
        while remaining > 0 and ext_ids:
            host = wall_lookup[ext_ids[0]]
            _add_window(host, position_along=0.5 + 0.05 * remaining)
            remaining -= 1

    # --- Columns: at room-corner intersections (most architecturally
    # meaningful), then fall back to a regular grid if there aren't enough
    # candidates.
    _emit_columns(storey, sp, fx=fx, fy=fy, rects=rects, structural=structural)

    # --- Slabs / Roofs / Railings ---
    _emit_slabs_and_roofs(storey, sp, fp=fp)
    _emit_railings(storey, sp, perim=[wall_lookup[w] for ids in
                                       exterior_wall_ids_by_room.values()
                                       for w in ids])

    # --- Spaces (IfcSpace) + auto-furnishing ---
    from ifc_agent.text2ifc.furnishing import furnish_room
    _corridor_funcs = {"corridor", "lobby", "circulation", "open_space"}
    for r in sp.rooms:
        rect = rects.get(r.id)
        if rect is None:
            continue
        door_side = _detect_door_side(r, rect, sp.rooms, rects, _corridor_funcs)
        window_side = _detect_window_side(rect, fx=fx, fy=fy,
                                          avoid=door_side)
        space = SpaceNode(
            id=r.id,
            name=r.display_name(),
            function=r.function,
            boundary=[
                (rect.x0, rect.y0),
                (rect.x1, rect.y0),
                (rect.x1, rect.y1),
                (rect.x0, rect.y1),
            ],
            elevation=0.0,
            height=storey.height,
            door_side=door_side,
            window_side=window_side,
        )
        furnish_room(space)
        storey.spaces.append(space)


# ---------------------------------------------------------------------------
# Legacy counts-driven layout (fallback when no rooms are given)
# ---------------------------------------------------------------------------

def _layout_storey_from_counts(
    storey: StoreyNode,
    sp: SpatialStorey,
    *,
    fp: Footprint,
    structural: StructuralSystem,
) -> None:
    el = sp.elements
    hint = sp.layout_hint.lower().strip()
    fx, fy = float(fp.x_mm), float(fp.y_mm)

    if hint == "empty" or not sp.is_inhabited or el.walls <= 0:
        _emit_slabs_and_roofs(storey, sp, fp=fp)
        return

    perim = _emit_perimeter_walls(storey, sp, fx=fx, fy=fy)
    _emit_interior_walls(storey, sp, fx=fx, fy=fy, perim_count=len(perim))
    _trim_or_pad_walls(storey, sp, fx=fx, fy=fy)
    _emit_openings(storey, sp)
    _emit_columns(storey, sp, fx=fx, fy=fy, structural=structural)
    _emit_slabs_and_roofs(storey, sp, fp=fp)
    _emit_railings(storey, sp, perim=perim)


# ---------------------------------------------------------------------------
# Wall placement (legacy)
# ---------------------------------------------------------------------------

def _emit_perimeter_walls(
    storey: StoreyNode, sp: SpatialStorey, *, fx: float, fy: float,
) -> list[WallNode]:
    el = sp.elements
    perim_specs = [
        ("pw_b", (0.0, 0.0), (fx,  0.0)),
        ("pw_r", (fx,  0.0), (fx,  fy )),
        ("pw_t", (fx,  fy ), (0.0, fy )),
        ("pw_l", (0.0, fy ), (0.0, 0.0)),
    ]
    n_perim = min(4, el.walls)
    out: list[WallNode] = []
    for suffix, s, e in perim_specs[:n_perim]:
        out.append(WallNode(
            id=f"{storey.id}-{suffix}",
            start=s, end=e,
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=True,
        ))
    storey.walls.extend(out)
    return out


def _emit_interior_walls(
    storey: StoreyNode, sp: SpatialStorey, *,
    fx: float, fy: float, perim_count: int,
) -> None:
    el = sp.elements
    interior_budget = max(0, el.walls - perim_count)
    if interior_budget == 0:
        return

    hint = sp.layout_hint.lower().strip()
    if hint == "central_corridor":
        nx, ny = _grid_central_corridor(interior_budget)
    elif hint == "grid":
        nx, ny = _grid_balanced(interior_budget, prefer_square=True)
    else:
        nx, ny = _grid_balanced(interior_budget)

    while nx + ny > interior_budget:
        if nx >= ny:
            nx -= 1
        else:
            ny -= 1

    for k in range(1, nx + 1):
        x = fx * k / (nx + 1)
        storey.walls.append(WallNode(
            id=f"{storey.id}-iw_v{k}",
            start=(x, 0.0), end=(x, fy),
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=False,
        ))
    for k in range(1, ny + 1):
        y = fy * k / (ny + 1)
        storey.walls.append(WallNode(
            id=f"{storey.id}-iw_h{k}",
            start=(0.0, y), end=(fx, y),
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=False,
        ))


def _trim_or_pad_walls(
    storey: StoreyNode, sp: SpatialStorey, *, fx: float, fy: float,
) -> None:
    el = sp.elements
    target = el.walls
    if len(storey.walls) > target:
        del storey.walls[target:]
    while len(storey.walls) < target:
        k = len(storey.walls)
        offset = 500.0 + 700.0 * k
        storey.walls.append(WallNode(
            id=f"{storey.id}-stub{k}",
            start=(offset, fy * 0.5),
            end=(offset + 1500.0, fy * 0.5),
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=False,
        ))


def _emit_openings(storey: StoreyNode, sp: SpatialStorey) -> None:
    el = sp.elements
    walls = storey.walls
    if not walls:
        return

    wc = len(walls)
    for d in range(el.doors):
        host = walls[d % wc]
        offset = host.length * 0.3 + 100.0 * d
        offset = min(offset, max(50.0, host.length - 1000.0))
        storey.openings.append(OpeningNode(
            id=f"{storey.id}-d{d + 1}",
            host_wall=host.id, kind="door",
            offset=offset,
            width=el.door_width_mm,
            height=el.door_height_mm,
            sill_height=0.0,
        ))
    for w in range(el.windows):
        host = walls[(w + el.doors) % wc]
        offset = host.length * 0.5 + 100.0 * w
        offset = min(offset, max(50.0, host.length - 1500.0))
        storey.openings.append(OpeningNode(
            id=f"{storey.id}-win{w + 1}",
            host_wall=host.id, kind="window",
            offset=offset,
            width=el.window_width_mm,
            height=el.window_height_mm,
            sill_height=el.window_sill_mm,
        ))


# ---------------------------------------------------------------------------
# Columns / Slabs / Roofs / Railings (shared)
# ---------------------------------------------------------------------------

def _emit_columns(
    storey: StoreyNode, sp: SpatialStorey, *,
    fx: float, fy: float, rects: Optional[dict[str, _Rect]] = None,
    structural: Optional[StructuralSystem] = None,
) -> None:
    el = sp.elements
    system = structural or StructuralSystem()
    kind = (sp.structural_system_override or system.kind or "frame").lower()
    n = el.columns
    if kind in {"frame", "mixed"} and n <= 0:
        gx = max(1500.0, float(system.grid_spacing_x_mm))
        gy = max(1500.0, float(system.grid_spacing_y_mm))
        n = max(0, (max(1, int(fx // gx)) + 1) * (max(1, int(fy // gy)) + 1))
    elif kind == "core_tube" and n <= 0:
        n = 8
    elif kind == "shear_wall" and n <= 0:
        n = 0
    if n <= 0:
        return

    if kind in {"core_tube", "mixed"}:
        cx0, cx1 = fx * 0.42, fx * 0.58
        cy0, cy1 = fy * 0.38, fy * 0.62
        candidates = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1)]
    else:
        candidates = []

    # If we have rooms, try to drop columns at the interior corners of
    # the partition (i.e. where 3+ rectangles meet) for architectural
    # plausibility.  Fall back to a regular grid otherwise.
    if rects:
        xs: set[float] = set()
        ys: set[float] = set()
        for r in rects.values():
            xs.update([r.x0, r.x1])
            ys.update([r.y0, r.y1])
        xs.discard(0.0); xs.discard(fx)
        ys.discard(0.0); ys.discard(fy)
        for x in sorted(xs):
            for y in sorted(ys):
                p = (x, y)
                if p not in candidates:
                    candidates.append(p)

    if len(candidates) < n:
        side = int(math.sqrt(n)) or 1
        cx = side
        cy = max(1, math.ceil(n / cx))
        while cx * cy < n:
            cx += 1
        candidates = []
        for i in range(cx):
            for j in range(cy):
                candidates.append(
                    (fx * (i + 1) / (cx + 1), fy * (j + 1) / (cy + 1))
                )

    for k, (x, y) in enumerate(candidates[:n]):
        storey.columns.append(ColumnNode(
            id=f"{storey.id}-c{k + 1}",
            position=(x, y),
            section=el.column_section_mm,
            height=storey.height,
            material=el.column_material,
        ))


def _emit_slabs_and_roofs(
    storey: StoreyNode, sp: SpatialStorey, *, fp: Footprint,
) -> None:
    el = sp.elements
    boundary: list[Point2D] = _default_boundary(fp)
    for k in range(el.slabs):
        storey.slabs.append(SlabNode(
            id=f"{storey.id}-floor{k + 1}",
            boundary=list(boundary),
            thickness=el.slab_thickness_mm,
            elevation=0,
            material=el.slab_material,
            predefined_type="FLOOR",
        ))
    for k in range(el.roofs):
        storey.roofs.append(RoofNode(
            id=f"{storey.id}-roof{k + 1}",
            boundary=list(boundary),
            thickness=el.roof_thickness_mm,
            elevation=storey.height,
            material=el.roof_material,
            pitch_deg=0,
        ))


def _emit_shafts(
    storey: StoreyNode,
    sp: SpatialStorey,
    shafts,
) -> None:
    """Materialise each declared shaft as simple enclosure walls plus a
    stair/elevator proxy.  This is intentionally conservative: the IFC
    builder already supports proxy furniture objects, while enclosure walls
    make the shaft visible in downstream geometry and validation.
    """
    selected = []
    explicit = set(sp.shaft_ids)
    for sh in shafts:
        if explicit and sh.id not in explicit:
            continue
        if sh.storey_ids and sp.id not in sh.storey_ids:
            continue
        selected.append(sh)
    for idx, sh in enumerate(selected):
        poly = sh.footprint
        if not poly:
            sx, sy = sh.shaft_mm
            offset = idx * (sx + 800.0)
            poly = [(500.0 + offset, 500.0),
                    (500.0 + offset + sx, 500.0),
                    (500.0 + offset + sx, 500.0 + sy),
                    (500.0 + offset, 500.0 + sy)]
        for k, p0 in enumerate(poly):
            p1 = poly[(k + 1) % len(poly)]
            storey.walls.append(WallNode(
                id=f"{storey.id}-{sh.id}-wall{k + 1}",
                start=p0,
                end=p1,
                height=storey.height,
                thickness=sh.wall_thickness_mm,
                material=sh.material,
                is_external=False,
            ))
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        if sh.kind == "stair":
            storey.furniture.append(FurnitureNode(
                id=f"{storey.id}-{sh.id}-stair",
                ifc_class="IfcStair",
                predefined_type="STRAIGHT_RUN_STAIR",
                name=f"{sh.id} Stair",
                position=(cx, cy),
                size=(sh.shaft_mm[0] * 0.8, sh.shaft_mm[1] * 0.8, storey.height),
                material=sh.material,
            ))
        elif sh.kind == "elevator":
            storey.furniture.append(FurnitureNode(
                id=f"{storey.id}-{sh.id}-elevator",
                ifc_class="IfcBuildingElementProxy",
                predefined_type="ELEVATOR",
                name=f"{sh.id} Elevator",
                position=(cx, cy),
                size=(sh.shaft_mm[0] * 0.75, sh.shaft_mm[1] * 0.75, storey.height),
                material="Steel",
            ))


def _emit_railings(
    storey: StoreyNode, sp: SpatialStorey, *, perim: list[WallNode],
) -> None:
    el = sp.elements
    if el.railings <= 0 or not perim:
        return
    for k in range(el.railings):
        edge = perim[k % len(perim)]
        storey.railings.append(RailingNode(
            id=f"{storey.id}-rail{k + 1}",
            polyline=[edge.start, edge.end],
            height=el.railing_height_mm,
            elevation=0,
            material="Steel",
        ))


# ---------------------------------------------------------------------------
# Interior-wall grid heuristics (legacy)
# ---------------------------------------------------------------------------

def _grid_balanced(n: int, *, prefer_square: bool = False) -> tuple[int, int]:
    if n <= 0:
        return (0, 0)
    if prefer_square:
        side = int(round(math.sqrt(n)))
        return (side, max(1, (n + side - 1) // side))
    nx = n // 2
    ny = n - nx
    return (nx, ny)


def _grid_central_corridor(n: int) -> tuple[int, int]:
    if n <= 0:
        return (0, 0)
    ny = 1
    nx = max(0, n - ny)
    return (nx, ny)
