"""
Deterministic expansion: SpatialGraph → BuildingGraph.

Pipeline:

    SpatialGraph (rooms + adjacency + footprint + shafts + structural_system)
            │
            ▼ BSP partitioning by area_ratio  (rectangular or polygonal)
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

New capabilities (v2):
  * Polygonal footprints (L, U, T, custom) with voids (atrium, courtyard).
  * Vertical shafts (stair / elevator / mechanical) spanning storeys.
  * Structural system abstraction (frame / shear_wall / core_tube / mixed).
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
    OpeningNode,
    Point2D,
    RailingNode,
    RoofNode,
    RoomNode,
    SlabNode,
    SpaceNode,
    SpatialGraph,
    SpatialStorey,
    StoreyElements,
    StoreyNode,
    StructuralSystem,
    VerticalShaft,
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


# --- Polygon utilities (pure Python, no shapely required) ------------------

def _bbox_polygon(pts: list[Point2D]) -> tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) for a point sequence."""
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _polygon_area(pts: list[Point2D]) -> float:
    """Shoelace formula; returns absolute signed area."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _point_in_polygon(px: float, py: float, poly: list[Point2D]) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _is_rectangular_footprint(fp: Footprint) -> bool:
    """Return True if the footprint is a simple axis-aligned rectangle."""
    if fp.shape == "rectangle" and not fp.boundary:
        return True
    if fp.boundary:
        # Check if boundary is exactly a rectangle
        bb = _bbox_polygon(fp.boundary)
        expected = [
            (bb[0], bb[1]), (bb[2], bb[1]),
            (bb[2], bb[3]), (bb[0], bb[3]),
        ]
        # Tolerate closed polygons
        b = list(fp.boundary)
        if b[0] == b[-1]:
            b = b[:-1]
        if len(b) != 4:
            return False
        # Allow rotation of vertices
        pts_set = set((round(p[0], 1), round(p[1], 1)) for p in b)
        exp_set = set((round(p[0], 1), round(p[1], 1)) for p in expected)
        return pts_set == exp_set
    return True


def _footprint_boundary(fp: Footprint) -> list[Point2D]:
    """Return the closed polygon boundary for a footprint.

    For rectangular footprints without an explicit boundary, returns the
    canonical rectangle.  For custom / L / U / T shapes, returns the
    explicitly stored boundary.
    """
    if fp.boundary:
        b = list(fp.boundary)
        if b[0] != b[-1]:
            b.append(b[0])
        return b
    # Canonical rectangle
    return [(0, 0), (fp.x_mm, 0), (fp.x_mm, fp.y_mm), (0, fp.y_mm), (0, 0)]


def _footprint_bbox(fp: Footprint) -> tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) for a footprint."""
    if fp.boundary:
        return _bbox_polygon(fp.boundary)
    return (0.0, 0.0, fp.x_mm, fp.y_mm)


def _clip_rect_to_polygon(rect: _Rect, poly: list[Point2D]) -> Optional[_Rect]:
    """If *rect* is fully inside *poly*, return it unchanged.
    If partially inside, return the intersection (approximated as a
    sub-rect clipped to the polygon bbox).  If fully outside, return None.
    """
    if not poly:
        return rect
    # Quick bbox rejection
    p_bb = _bbox_polygon(poly)
    if rect.x1 < p_bb[0] or rect.x0 > p_bb[2] or rect.y1 < p_bb[1] or rect.y0 > p_bb[3]:
        return None
    # Sample centre + corners
    corners = [
        (rect.x0, rect.y0), (rect.x1, rect.y0),
        (rect.x1, rect.y1), (rect.x0, rect.y1),
        (rect.cx, rect.cy),
    ]
    inside_count = sum(1 for c in corners if _point_in_polygon(c[0], c[1], poly))
    if inside_count == 0:
        return None
    if inside_count == 5:
        return rect
    # Partial overlap — shrink to the intersection of the two bboxes
    x0 = max(rect.x0, p_bb[0])
    y0 = max(rect.y0, p_bb[1])
    x1 = min(rect.x1, p_bb[2])
    y1 = min(rect.y1, p_bb[3])
    if x1 - x0 < 100.0 or y1 - y0 < 100.0:
        return None
    return _Rect(x0, y0, x1, y1, room_id=rect.room_id)


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


def _exterior_edges_of_rect_polygon(
    r: _Rect, poly: list[Point2D], *, tol: float = 1.0
) -> list[tuple[Point2D, Point2D, str]]:
    """Return the sub-edges of *r* that lie on the polygon perimeter.

    For non-rectangular footprints we approximate the exterior test by
    checking whether a rect edge lies on the polygon boundary (within tol).
    """
    out: list[tuple[Point2D, Point2D, str]] = []
    edges = [
        ((r.x0, r.y0), (r.x1, r.y0), "H"),  # bottom
        ((r.x1, r.y0), (r.x1, r.y1), "V"),  # right
        ((r.x1, r.y1), (r.x0, r.y1), "H"),  # top
        ((r.x0, r.y1), (r.x0, r.y0), "V"),  # left
    ]
    for p0, p1, orient in edges:
        # Sample the midpoint — if it lies exactly on the polygon boundary
        # (and outside any void), treat as exterior.
        mx = (p0[0] + p1[0]) * 0.5
        my = (p0[1] + p1[1]) * 0.5
        if _point_on_polygon_boundary(mx, my, poly, tol):
            out.append((p0, p1, orient))
    return out


def _point_on_polygon_boundary(px: float, py: float, poly: list[Point2D],
                               tol: float = 1.0) -> bool:
    """Check if point lies on any edge of the polygon (within tol)."""
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        # Bounding-box quick reject
        if (px < min(x1, x2) - tol or px > max(x1, x2) + tol or
                py < min(y1, y2) - tol or py > max(y1, y2) + tol):
            continue
        # Distance from point to line segment
        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-12:
            dist_sq = (px - x1) ** 2 + (py - y1) ** 2
        else:
            t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
            proj_x = x1 + t * dx
            proj_y = y1 + t * dy
            dist_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
        if dist_sq <= tol * tol:
            return True
    return False


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


def _polygon_bsp_partition(
    rooms: list[RoomNode],
    poly: list[Point2D],
    voids: list[list[Point2D]],
    *,
    min_room_dim: float = 1500.0,
) -> dict[str, _Rect]:
    """Partition rooms inside a polygonal footprint.

    Strategy:
      1. Compute the polygon's axis-aligned bounding box.
      2. Run the standard rectangular BSP on the bbox.
      3. Clip each resulting rect to the polygon; discard fully-outside ones.
      4. Remove any rect that falls inside a void.

    This keeps the algorithm simple and deterministic while supporting
    L, U, T, and custom footprints.
    """
    if not rooms or not poly:
        return {}

    bb = _bbox_polygon(poly)
    fx = bb[2] - bb[0]
    fy = bb[3] - bb[1]
    if fx <= 0 or fy <= 0:
        return {}

    # Run standard BSP on the bbox
    raw_rects = _bsp_partition(rooms, fx, fy, min_room_dim=min_room_dim)

    # Translate rects to the polygon's local origin and clip
    placed: dict[str, _Rect] = {}
    for rid, r in raw_rects.items():
        shifted = _Rect(r.x0 + bb[0], r.y0 + bb[1],
                        r.x1 + bb[0], r.y1 + bb[1], room_id=r.room_id)
        clipped = _clip_rect_to_polygon(shifted, poly)
        if clipped is None:
            continue
        # Reject if the rect's centre falls inside a void
        in_void = False
        for void in voids:
            if _point_in_polygon(clipped.cx, clipped.cy, void):
                in_void = True
                break
        if in_void:
            continue
        placed[rid] = clipped

    return placed


# ---------------------------------------------------------------------------
# Structural system helpers
# ---------------------------------------------------------------------------

def _effective_structural_system(
    sp: SpatialStorey, global_ss: StructuralSystem,
) -> StructuralSystem:
    """Return the structural system active for a given storey."""
    kind = sp.structural_system_override or global_ss.kind
    return StructuralSystem(
        kind=kind,
        grid_spacing_x_mm=global_ss.grid_spacing_x_mm,
        grid_spacing_y_mm=global_ss.grid_spacing_y_mm,
        core_position=global_ss.core_position,
    )


def _emit_columns_structural(
    storey: StoreyNode,
    sp: SpatialStorey,
    ss: StructuralSystem,
    *,
    fx: float,
    fy: float,
    rects: Optional[dict[str, _Rect]] = None,
) -> None:
    """Emit columns according to the structural system."""
    el = sp.elements
    n = el.columns
    if n <= 0:
        return

    kind = ss.kind.lower().strip()

    if kind == "none":
        return

    candidates: list[tuple[float, float]] = []

    if kind in ("frame", "mixed"):
        # Regular grid
        gx = ss.grid_spacing_x_mm
        gy = ss.grid_spacing_y_mm
        nx = max(2, int(fx / gx) + 1)
        ny = max(2, int(fy / gy) + 1)
        for i in range(1, nx):
            for j in range(1, ny):
                candidates.append((fx * i / nx, fy * j / ny))

    if kind in ("shear_wall", "mixed"):
        # Add columns near corners for shear-wall system
        margin = min(fx, fy) * 0.1
        corner_offsets = [
            (margin, margin), (fx - margin, margin),
            (fx - margin, fy - margin), (margin, fy - margin),
        ]
        for cx, cy in corner_offsets:
            if (cx, cy) not in candidates:
                candidates.append((cx, cy))

    if kind in ("core_tube", "mixed"):
        # Central core columns
        cx, cy = fx * 0.5, fy * 0.5
        core_w, core_h = fx * 0.15, fy * 0.15
        core_cols = [
            (cx - core_w * 0.5, cy - core_h * 0.5),
            (cx + core_w * 0.5, cy - core_h * 0.5),
            (cx + core_w * 0.5, cy + core_h * 0.5),
            (cx - core_w * 0.5, cy + core_h * 0.5),
        ]
        for cc in core_cols:
            if cc not in candidates:
                candidates.append(cc)

    # If we still don't have enough, fall back to room-corner candidates
    if len(candidates) < n and rects:
        xs: set[float] = set()
        ys: set[float] = set()
        for r in rects.values():
            xs.update([r.x0, r.x1])
            ys.update([r.y0, r.y1])
        xs.discard(0.0)
        xs.discard(fx)
        ys.discard(0.0)
        ys.discard(fy)
        for x in sorted(xs):
            for y in sorted(ys):
                if (x, y) not in candidates:
                    candidates.append((x, y))

    # Ultimate fallback: uniform grid
    if len(candidates) < n:
        side = int(math.sqrt(n)) or 1
        cx = side
        cy = max(1, math.ceil(n / cx))
        while cx * cy < n:
            cx += 1
        for i in range(1, cx + 1):
            for j in range(1, cy + 1):
                pt = (fx * i / (cx + 1), fy * j / (cy + 1))
                if pt not in candidates:
                    candidates.append(pt)

    # Deduplicate (within 10mm)
    deduped: list[tuple[float, float]] = []
    for c in candidates:
        is_dup = False
        for d in deduped:
            if abs(c[0] - d[0]) < 10.0 and abs(c[1] - d[1]) < 10.0:
                is_dup = True
                break
        if not is_dup:
            deduped.append(c)

    for k, (x, y) in enumerate(deduped[:n]):
        storey.columns.append(ColumnNode(
            id=f"{storey.id}-c{k + 1}",
            position=(x, y),
            section=el.column_section_mm,
            height=storey.height,
            material=el.column_material,
        ))


def _emit_shear_walls(
    storey: StoreyNode,
    sp: SpatialStorey,
    poly: list[Point2D],
    *,
    fx: float,
    fy: float,
) -> None:
    """For shear-wall systems, thicken exterior walls and add interior
    shear walls along the primary axes."""
    el = sp.elements
    # Thicken all existing exterior walls
    for w in storey.walls:
        if w.is_external:
            w.thickness = max(w.thickness, el.wall_thickness_mm * 2.0)
            w.material = "ReinforcedConcrete"

    # Add cross shear walls (one horizontal, one vertical through centre)
    mid_x, mid_y = fx * 0.5, fy * 0.5
    # Only add if they don't collide too badly with existing walls
    storey.walls.append(WallNode(
        id=f"{storey.id}-sw_h", start=(0, mid_y), end=(fx, mid_y),
        height=storey.height, thickness=el.wall_thickness_mm * 1.5,
        material="ReinforcedConcrete", is_external=False,
    ))
    storey.walls.append(WallNode(
        id=f"{storey.id}-sw_v", start=(mid_x, 0), end=(mid_x, fy),
        height=storey.height, thickness=el.wall_thickness_mm * 1.5,
        material="ReinforcedConcrete", is_external=False,
    ))


# ---------------------------------------------------------------------------
# Vertical shaft helpers
# ---------------------------------------------------------------------------

def _shaft_rect(shaft: VerticalShaft) -> _Rect:
    """Return a _Rect for a shaft's footprint."""
    if shaft.footprint:
        bb = _bbox_polygon(shaft.footprint)
        return _Rect(bb[0], bb[1], bb[2], bb[3], room_id=shaft.id)
    dx, dy = shaft.shaft_mm
    # Default: centred on origin (will be repositioned by caller)
    return _Rect(-dx * 0.5, -dy * 0.5, dx * 0.5, dy * 0.5, room_id=shaft.id)


def _emit_shaft_walls(
    storey: StoreyNode,
    shaft: VerticalShaft,
    sp: SpatialStorey,
) -> list[WallNode]:
    """Generate perimeter walls for a vertical shaft on this storey.

    Returns the list of created walls.
    """
    el = sp.elements
    if shaft.footprint:
        pts = list(shaft.footprint)
        if pts[0] != pts[-1]:
            pts.append(pts[0])
    else:
        dx, dy = shaft.shaft_mm
        pts = [(-dx * 0.5, -dy * 0.5), (dx * 0.5, -dy * 0.5),
               (dx * 0.5, dy * 0.5), (-dx * 0.5, dy * 0.5),
               (-dx * 0.5, -dy * 0.5)]

    out: list[WallNode] = []
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        w = WallNode(
            id=f"{storey.id}-{shaft.id}-w{i}",
            start=p0, end=p1,
            height=storey.height,
            thickness=shaft.wall_thickness_mm,
            material=shaft.material,
            is_external=False,
            shaft_id=shaft.id,
        )
        storey.walls.append(w)
        out.append(w)
    return out


def _emit_shaft_openings(
    storey: StoreyNode,
    shaft: VerticalShaft,
    sp: SpatialStorey,
    shaft_walls: list[WallNode],
) -> None:
    """Add a doorway into the shaft on each accessible wall."""
    el = sp.elements
    for w in shaft_walls:
        if w.length < 600.0:
            continue
        width = min(el.door_width_mm, w.length - 200.0)
        offset = max(100.0, w.length * 0.5 - width * 0.5)
        storey.openings.append(OpeningNode(
            id=f"{w.id}_door",
            host_wall=w.id,
            kind="door",
            offset=offset,
            width=width,
            height=el.door_height_mm,
            sill_height=0.0,
        ))


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
    """
    global_fp = sg.footprint
    storeys: list[StoreyNode] = []

    for sp in sg.storeys:
        sn = StoreyNode(
            id=str(sp.id),
            name=str(sp.name),
            elevation=float(sp.elevation_mm),
            height=float(sp.height_mm),
        )

        # Determine effective footprint for this storey
        fp = sp.footprint_override or global_fp
        is_rect = _is_rectangular_footprint(fp)
        bb = _footprint_bbox(fp)
        fx = bb[2] - bb[0]
        fy = bb[3] - bb[1]

        # Determine effective structural system
        ss = _effective_structural_system(sp, sg.structural_system)

        if sp.is_inhabited and sp.rooms:
            _layout_storey_from_rooms(
                sn, sp, fx=fx, fy=fy, fp=fp, is_rect=is_rect, ss=ss,
                shafts=sg.shafts,
            )
        else:
            _layout_storey_from_counts(sn, sp, fx=fx, fy=fy, fp=fp, is_rect=is_rect)

        # Emit vertical shafts that pass through this storey
        for shaft in sg.shafts:
            if sp.id in shaft.storey_ids:
                shaft_walls = _emit_shaft_walls(sn, shaft, sp)
                _emit_shaft_openings(sn, shaft, sp, shaft_walls)

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
    storey: StoreyNode, sp: SpatialStorey,
    *, fx: float, fy: float,
    fp: Footprint,
    is_rect: bool,
    ss: StructuralSystem,
    shafts: list[VerticalShaft],
) -> None:
    el = sp.elements
    # Defensive: if elements came in as a raw dict, coerce to StoreyElements
    if isinstance(el, dict):
        from ifc_agent.text2ifc.schemas import _coerce_storey_elements
        el = _coerce_storey_elements(el)
        sp.elements = el

    # Separate shaft rooms from regular rooms
    shaft_rooms: list[RoomNode] = []
    regular_rooms: list[RoomNode] = []
    for r in sp.rooms:
        if r.shaft_id and any(s.id == r.shaft_id for s in shafts if sp.id in s.storey_ids):
            shaft_rooms.append(r)
        else:
            regular_rooms.append(r)

    # Partition regular rooms
    if is_rect:
        rects = _bsp_partition(regular_rooms, fx, fy)
    else:
        poly = _footprint_boundary(fp)
        rects = _polygon_bsp_partition(regular_rooms, poly, fp.voids)

    # Add shaft rooms as fixed rectangles (from shaft footprint)
    shaft_by_id = {s.id: s for s in shafts}
    for sr in shaft_rooms:
        shaft = shaft_by_id.get(sr.shaft_id)
        if shaft:
            srect = _shaft_rect(shaft)
            # If shaft footprint is relative, centre it in the storey
            if not shaft.footprint:
                cx, cy = fx * 0.5, fy * 0.5
                srect = _Rect(
                    cx - shaft.shaft_mm[0] * 0.5,
                    cy - shaft.shaft_mm[1] * 0.5,
                    cx + shaft.shaft_mm[0] * 0.5,
                    cy + shaft.shaft_mm[1] * 0.5,
                    room_id=sr.id,
                )
            rects[sr.id] = srect

    room_by_id = {r.id: r for r in sp.rooms}

    # --- Collect wall segments ---
    interior_segs: dict[frozenset, tuple[Point2D, Point2D, str, str, str]] = {}
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

    # Exterior walls
    exterior_segs: list[tuple[Point2D, Point2D, str, str]] = []
    poly = _footprint_boundary(fp) if not is_rect else []
    for r in sp.rooms:
        if r.id not in rects:
            continue
        if is_rect:
            for p0, p1, o in _exterior_edges_of_rect(rects[r.id], fx=fx, fy=fy):
                exterior_segs.append((p0, p1, o, r.id))
        else:
            for p0, p1, o in _exterior_edges_of_rect_polygon(rects[r.id], poly):
                exterior_segs.append((p0, p1, o, r.id))

    # --- Materialise as WallNodes ---
    walls: list[WallNode] = []
    wall_lookup: dict[str, WallNode] = {}

    def _add_wall(prefix: str, p0, p1, *, external: bool, shaft_id: str = ""):
        idx = len(walls)
        wid = f"{storey.id}-{prefix}{idx}"
        w = WallNode(
            id=wid, start=p0, end=p1,
            height=storey.height,
            thickness=el.wall_thickness_mm,
            material=el.wall_material,
            is_external=external,
            shaft_id=shaft_id,
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
        if host_wall.length < 400.0:
            return
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

    seen_pairs: set[frozenset] = set()
    for r in sp.rooms:
        for nb in r.opening_to:
            pair = frozenset((r.id, nb))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if pair in interior_wall_ids_by_pair:
                _add_door(wall_lookup[interior_wall_ids_by_pair[pair]])

    if doors_emitted == 0:
        corridor_funcs = {"corridor", "lobby", "circulation", "open_space"}
        for pair, wid in interior_wall_ids_by_pair.items():
            a_id, b_id = tuple(pair)
            a, b = room_by_id.get(a_id), room_by_id.get(b_id)
            if a and b and (a.function in corridor_funcs
                            or b.function in corridor_funcs):
                _add_door(wall_lookup[wid])

    for r in sp.rooms:
        if r.n_external_doors <= 0:
            continue
        ext_ids = exterior_wall_ids_by_room.get(r.id, [])
        for k in range(r.n_external_doors):
            if not ext_ids:
                break
            host = wall_lookup[ext_ids[k % len(ext_ids)]]
            _add_door(host, position_along=0.5)

    # --- Windows ---
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
            continue
        per_wall = max(1, r.n_windows // max(1, len(ext_ids)))
        remaining = r.n_windows
        for wid in ext_ids:
            if remaining <= 0:
                break
            host = wall_lookup[wid]
            for k in range(per_wall):
                if remaining <= 0:
                    break
                pos = (k + 1) / (per_wall + 1)
                _add_window(host, position_along=pos)
                remaining -= 1
        while remaining > 0 and ext_ids:
            host = wall_lookup[ext_ids[0]]
            _add_window(host, position_along=0.5 + 0.05 * remaining)
            remaining -= 1

    # --- Columns: structural-system aware ---
    _emit_columns_structural(storey, sp, ss, fx=fx, fy=fy, rects=rects)

    # --- Shear walls (if applicable) ---
    if ss.kind.lower().strip() == "shear_wall":
        _emit_shear_walls(storey, sp, poly=_footprint_boundary(fp) if not is_rect else [], fx=fx, fy=fy)

    # --- Slabs / Roofs / Railings ---
    _emit_slabs_and_roofs(storey, sp, fp=fp, fx=fx, fy=fy)
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
        window_side = ""
        if is_rect:
            window_side = _detect_window_side(rect, fx=fx, fy=fy, avoid=door_side)
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
    storey: StoreyNode, sp: SpatialStorey,
    *, fx: float, fy: float,
    fp: Optional[Footprint] = None,
    is_rect: bool = True,
) -> None:
    el = sp.elements
    hint = sp.layout_hint.lower().strip()

    if hint == "empty" or not sp.is_inhabited or el.walls <= 0:
        _emit_slabs_and_roofs(storey, sp, fp=fp, fx=fx, fy=fy)
        return

    perim = _emit_perimeter_walls(storey, sp, fx=fx, fy=fy)
    _emit_interior_walls(storey, sp, fx=fx, fy=fy, perim_count=len(perim))
    _trim_or_pad_walls(storey, sp, fx=fx, fy=fy)
    _emit_openings(storey, sp)
    _emit_columns(storey, sp, fx=fx, fy=fy)
    _emit_slabs_and_roofs(storey, sp, fp=fp, fx=fx, fy=fy)
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
) -> None:
    """Legacy column emitter (structural-system unaware).  Kept for
    backward compatibility with the counts-driven layout path."""
    el = sp.elements
    n = el.columns
    if n <= 0:
        return

    candidates: list[tuple[float, float]] = []
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
                candidates.append((x, y))

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
    storey: StoreyNode, sp: SpatialStorey,
    *,
    fp: Optional[Footprint] = None,
    fx: float, fy: float,
) -> None:
    el = sp.elements
    if fp and fp.boundary:
        boundary = list(fp.boundary)
        if boundary[0] != boundary[-1]:
            boundary.append(boundary[0])
    else:
        boundary = [(0, 0), (fx, 0), (fx, fy), (0, fy)]
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
