"""
Inspect & compare the SPATIAL STRUCTURE of two IFC files.

Existing ``comparator.py`` only checks element counts and overall bbox.
This script goes one level deeper: it extracts the actual layout — every
storey's walls (start/end), openings (on which wall, where along it),
columns (xy position), slabs (bbox), and renders an ASCII top-view of
each storey so we can eyeball where the generated model differs from the
ground truth.

Usage::

    python inspect_ifc_spatial.py \
        --gt   ../demo_data/"1px(1).ifc" \
        --gen  test_output/text2ifc/final_llm_iter2.ifc
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

import ifcopenshell
import ifcopenshell.util.element as eu
import ifcopenshell.util.placement as up


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class WallInfo:
    guid: str
    storey_name: str
    p0: tuple[float, float]   # start in world XY
    p1: tuple[float, float]   # end in world XY
    length_mm: float
    orientation: str          # H | V | D
    is_external: Optional[bool] = None

    @property
    def midpoint(self) -> tuple[float, float]:
        return ((self.p0[0] + self.p1[0]) * 0.5,
                (self.p0[1] + self.p1[1]) * 0.5)


@dataclass
class PointInfo:
    guid: str
    storey_name: str
    position: tuple[float, float]
    kind: str   # door | window | column


@dataclass
class StoreyView:
    name: str
    elevation_mm: float
    height_mm: float
    walls: list[WallInfo] = field(default_factory=list)
    doors: list[PointInfo] = field(default_factory=list)
    windows: list[PointInfo] = field(default_factory=list)
    columns: list[PointInfo] = field(default_factory=list)
    n_slabs: int = 0
    n_roofs: int = 0
    n_railings: int = 0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for w in self.walls:
            xs.extend([w.p0[0], w.p1[0]])
            ys.extend([w.p0[1], w.p1[1]])
        if not xs:
            return (0, 0, 0, 0)
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def perimeter_walls(self) -> list[WallInfo]:
        """Walls that sit on the storey bbox edge (within 100 mm tolerance)."""
        if not self.walls:
            return []
        x0, y0, x1, y1 = self.bbox
        tol = 100.0
        out: list[WallInfo] = []
        for w in self.walls:
            on_edge = (
                (abs(w.p0[0] - x0) < tol and abs(w.p1[0] - x0) < tol) or
                (abs(w.p0[0] - x1) < tol and abs(w.p1[0] - x1) < tol) or
                (abs(w.p0[1] - y0) < tol and abs(w.p1[1] - y0) < tol) or
                (abs(w.p0[1] - y1) < tol and abs(w.p1[1] - y1) < tol)
            )
            if on_edge:
                out.append(w)
        return out


# ---------------------------------------------------------------------------
# IFC extraction
# ---------------------------------------------------------------------------

def _wall_endpoints_world(wall) -> Optional[tuple[tuple[float, float],
                                                  tuple[float, float],
                                                  float]]:
    """Return (p0_world, p1_world, length_mm) for a straight wall.

    Endpoints are derived from the wall's Axis IfcShapeRepresentation
    (a 2D polyline / line) transformed by its ObjectPlacement.
    """
    placement = up.get_local_placement(wall.ObjectPlacement)
    rep = None
    if wall.Representation is not None:
        for r in wall.Representation.Representations:
            if r.RepresentationIdentifier == "Axis":
                rep = r
                break
    p0_local = (0.0, 0.0)
    p1_local: Optional[tuple[float, float]] = None
    if rep is not None and rep.Items:
        item = rep.Items[0]
        if item.is_a("IfcPolyline") and item.Points:
            pts = [
                (p.Coordinates[0], p.Coordinates[1])
                for p in item.Points
            ]
            if len(pts) >= 2:
                p0_local = pts[0]
                p1_local = pts[-1]
        elif item.is_a("IfcTrimmedCurve") and item.BasisCurve.is_a("IfcLine"):
            # Trimmed line: read trim params
            line = item.BasisCurve
            d = line.Dir.DirectionRatios
            origin = line.Pnt.Coordinates
            t0 = item.Trim1[0].wrappedValue if item.Trim1 else 0.0
            t1 = item.Trim2[0].wrappedValue if item.Trim2 else 0.0
            p0_local = (origin[0] + d[0] * t0, origin[1] + d[1] * t0)
            p1_local = (origin[0] + d[0] * t1, origin[1] + d[1] * t1)

    if p1_local is None:
        # Fall back to a tiny stub at the placement origin.
        p1_local = (1.0, 0.0)

    def _apply(p):
        x = (placement[0, 0] * p[0] + placement[0, 1] * p[1]
             + placement[0, 3])
        y = (placement[1, 0] * p[0] + placement[1, 1] * p[1]
             + placement[1, 3])
        return (x, y)

    p0w = _apply(p0_local)
    p1w = _apply(p1_local)
    length = math.hypot(p1w[0] - p0w[0], p1w[1] - p0w[1])
    return p0w, p1w, length


def _world_xy(entity) -> tuple[float, float]:
    placement = up.get_local_placement(entity.ObjectPlacement)
    return (placement[0, 3], placement[1, 3])


def _classify_orientation(p0, p1) -> str:
    dx = abs(p1[0] - p0[0])
    dy = abs(p1[1] - p0[1])
    if dy < 1.0:
        return "H"   # horizontal (parallel to X axis)
    if dx < 1.0:
        return "V"   # vertical (parallel to Y axis)
    return "D"       # diagonal


def _storey_for(entity) -> Optional[str]:
    """Return the IfcBuildingStorey name that contains *entity*."""
    try:
        for rel in getattr(entity, "ContainedInStructure", []) or []:
            st = rel.RelatingStructure
            if st.is_a("IfcBuildingStorey"):
                return st.Name or "(unnamed storey)"
    except Exception:
        pass
    return None


def _external_flag(wall) -> Optional[bool]:
    try:
        psets = eu.get_psets(wall)
        common = psets.get("Pset_WallCommon") or {}
        return common.get("IsExternal")
    except Exception:
        return None


def extract_storeys(ifc_path: str) -> list[StoreyView]:
    model = ifcopenshell.open(ifc_path)
    storeys = model.by_type("IfcBuildingStorey")

    # storey-name → StoreyView lookup
    views: dict[str, StoreyView] = {}
    for s in storeys:
        nm = s.Name or "(unnamed)"
        views[nm] = StoreyView(
            name=nm,
            elevation_mm=float(getattr(s, "Elevation", 0.0) or 0.0),
            height_mm=0.0,   # filled below from successive elevations
        )

    # Derive heights from successive elevations (stable order by elevation)
    ordered = sorted(views.values(), key=lambda v: v.elevation_mm)
    for i, v in enumerate(ordered):
        if i + 1 < len(ordered):
            v.height_mm = ordered[i + 1].elevation_mm - v.elevation_mm

    # Walls
    for wall in model.by_type("IfcWall"):
        sn = _storey_for(wall)
        if sn is None or sn not in views:
            continue
        try:
            p0, p1, length = _wall_endpoints_world(wall)
        except Exception:
            continue
        views[sn].walls.append(WallInfo(
            guid=getattr(wall, "GlobalId", ""),
            storey_name=sn,
            p0=p0, p1=p1, length_mm=length,
            orientation=_classify_orientation(p0, p1),
            is_external=_external_flag(wall),
        ))

    # Doors / Windows / Columns
    def _ingest(ifc_type: str, kind: str, sink_attr: str):
        for el in model.by_type(ifc_type):
            sn = _storey_for(el)
            if sn is None or sn not in views:
                # Openings often aren't directly contained — link via host
                # wall.  Best-effort: search FillsVoids → VoidsElement.
                if ifc_type in ("IfcDoor", "IfcWindow"):
                    try:
                        for fv in getattr(el, "FillsVoids", []) or []:
                            opening = fv.RelatingOpeningElement
                            host = (opening.VoidsElements[0].RelatingBuildingElement
                                    if opening.VoidsElements else None)
                            if host is not None:
                                sn = _storey_for(host)
                                if sn in views:
                                    break
                    except Exception:
                        pass
            if sn is None or sn not in views:
                continue
            try:
                pos = _world_xy(el)
            except Exception:
                continue
            getattr(views[sn], sink_attr).append(PointInfo(
                guid=getattr(el, "GlobalId", ""),
                storey_name=sn,
                position=pos,
                kind=kind,
            ))

    _ingest("IfcDoor",   "door",   "doors")
    _ingest("IfcWindow", "window", "windows")
    _ingest("IfcColumn", "column", "columns")

    # Slab / Roof / Railing counts per storey
    def _count_per_storey(ifc_type: str, attr: str):
        for el in model.by_type(ifc_type):
            sn = _storey_for(el)
            if sn in views:
                setattr(views[sn], attr,
                        getattr(views[sn], attr) + 1)

    _count_per_storey("IfcSlab", "n_slabs")
    _count_per_storey("IfcRoof", "n_roofs")
    _count_per_storey("IfcRailing", "n_railings")

    return ordered


# ---------------------------------------------------------------------------
# ASCII top-view renderer
# ---------------------------------------------------------------------------

def _render_top_view(view: StoreyView, *,
                     width: int = 80, height: int = 30) -> str:
    if not view.walls:
        return "(empty storey)"

    xs, ys = [], []
    for w in view.walls:
        xs.extend([w.p0[0], w.p1[0]])
        ys.extend([w.p0[1], w.p1[1]])
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max == x_min:
        x_max = x_min + 1
    if y_max == y_min:
        y_max = y_min + 1

    grid = [[" " for _ in range(width)] for _ in range(height)]

    def _to_grid(x: float, y: float) -> tuple[int, int]:
        u = int((x - x_min) / (x_max - x_min) * (width - 1))
        v = int((y - y_min) / (y_max - y_min) * (height - 1))
        v = (height - 1) - v  # flip so +Y goes up
        return (max(0, min(width - 1, u)), max(0, min(height - 1, v)))

    def _draw_line(p0, p1, ch):
        u0, v0 = _to_grid(*p0)
        u1, v1 = _to_grid(*p1)
        steps = max(abs(u1 - u0), abs(v1 - v0)) + 1
        for s in range(steps + 1):
            t = s / steps if steps else 0
            u = int(round(u0 + (u1 - u0) * t))
            v = int(round(v0 + (v1 - v0) * t))
            if grid[v][u] in (" ", ch):
                grid[v][u] = ch

    for w in view.walls:
        ch = "#" if w.is_external else "-" if w.orientation == "H" \
             else "|" if w.orientation == "V" else "/"
        _draw_line(w.p0, w.p1, ch)

    def _mark(points: Iterable[PointInfo], ch: str):
        for p in points:
            u, v = _to_grid(*p.position)
            grid[v][u] = ch

    _mark(view.doors,   "D")
    _mark(view.windows, "W")
    _mark(view.columns, "C")

    header = (
        f"  bbox x:[{x_min:>8.0f}, {x_max:>8.0f}]  "
        f"y:[{y_min:>8.0f}, {y_max:>8.0f}]   "
        f"size = {x_max - x_min:.0f} × {y_max - y_min:.0f} mm"
    )
    rows = ["".join(row) for row in grid]
    return header + "\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# Comparison / report
# ---------------------------------------------------------------------------

def _storey_summary(view: StoreyView) -> dict:
    orient = Counter(w.orientation for w in view.walls)
    return {
        "name": view.name,
        "elevation_mm": view.elevation_mm,
        "height_mm": view.height_mm,
        "wall_total": len(view.walls),
        "wall_perimeter": len(view.perimeter_walls),
        "wall_interior": len(view.walls) - len(view.perimeter_walls),
        "wall_orient_H": orient["H"],
        "wall_orient_V": orient["V"],
        "wall_orient_D": orient["D"],
        "wall_external": sum(1 for w in view.walls if w.is_external is True),
        "doors": len(view.doors),
        "windows": len(view.windows),
        "columns": len(view.columns),
        "slabs": view.n_slabs,
        "roofs": view.n_roofs,
        "railings": view.n_railings,
        "bbox": tuple(round(v, 1) for v in view.bbox),
    }


def _print_summary_table(gt_views, gen_views):
    cols = [
        ("storey", "name"),
        ("elev", "elevation_mm"),
        ("h", "height_mm"),
        ("walls", "wall_total"),
        ("perim", "wall_perimeter"),
        ("inter", "wall_interior"),
        ("H/V/D", None),
        ("ext", "wall_external"),
        ("D", "doors"),
        ("W", "windows"),
        ("C", "columns"),
        ("S", "slabs"),
        ("R", "roofs"),
        ("Rl", "railings"),
    ]

    def _row(label, summary):
        hvd = f"{summary['wall_orient_H']}/{summary['wall_orient_V']}/{summary['wall_orient_D']}"
        cells = []
        for header, key in cols:
            if header == "storey":
                cells.append(f"{label:<10}")
            elif header == "H/V/D":
                cells.append(f"{hvd:>7}")
            elif key == "name":
                cells.append(f"{summary['name'][:14]:<14}")
            else:
                v = summary[key]
                if isinstance(v, float):
                    cells.append(f"{v:>7.0f}")
                else:
                    cells.append(f"{str(v):>5}")
        return " ".join(cells)

    def _header():
        cells = []
        for header, _ in cols:
            if header == "storey":
                cells.append(f"{'':<10}")
            elif header == "H/V/D":
                cells.append(f"{header:>7}")
            else:
                cells.append(f"{header:>5}" if header != "storey" else header)
        return " ".join(cells)

    print(_header())
    print("-" * 110)
    for v in gt_views:
        print(_row("GT/" + v.name[:6], _storey_summary(v)))
    print("-" * 110)
    for v in gen_views:
        print(_row("GEN/" + v.name[:5], _storey_summary(v)))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Inspect & diff the SPATIAL structure of two IFC files")
    parser.add_argument("--gt", required=True, help="Ground-truth IFC")
    parser.add_argument("--gen", required=True, help="Generated IFC")
    parser.add_argument("--width", type=int, default=80,
                        help="ASCII top-view width in characters")
    parser.add_argument("--height", type=int, default=30,
                        help="ASCII top-view height in characters")
    args = parser.parse_args()

    print("\n" + "=" * 110)
    print(f"GROUND TRUTH : {args.gt}")
    print(f"GENERATED    : {args.gen}")
    print("=" * 110)

    gt_views  = extract_storeys(args.gt)
    gen_views = extract_storeys(args.gen)

    print("\nPER-STOREY SUMMARY")
    print("Columns: walls=total, perim/inter=perimeter/interior, "
          "H/V/D=orientation, ext=external, D/W/C=doors/windows/columns, "
          "S=slabs, R=roofs, Rl=railings")
    print()
    _print_summary_table(gt_views, gen_views)

    print("\nASCII TOP-VIEWS")
    print("Legend: # external wall, - horizontal interior, | vertical interior, "
          "/ diagonal, D door, W window, C column")

    def _render_pair(gt_view, gen_view):
        print(f"\n--- GT storey '{gt_view.name}' (elev={gt_view.elevation_mm:.0f}mm) ---")
        print(_render_top_view(gt_view, width=args.width, height=args.height))
        print(f"\n--- GEN storey '{gen_view.name}' (elev={gen_view.elevation_mm:.0f}mm) ---")
        print(_render_top_view(gen_view, width=args.width, height=args.height))

    # Match by index in elevation order
    for i in range(max(len(gt_views), len(gen_views))):
        gt_v  = gt_views[i]  if i < len(gt_views)  else None
        gen_v = gen_views[i] if i < len(gen_views) else None
        if gt_v is None:
            print(f"\n--- (no GT storey #{i+1}) ---")
            print(_render_top_view(gen_v, width=args.width, height=args.height))
        elif gen_v is None:
            print(f"\n--- GT storey '{gt_v.name}' has no generated counterpart ---")
            print(_render_top_view(gt_v, width=args.width, height=args.height))
        else:
            _render_pair(gt_v, gen_v)


if __name__ == "__main__":
    main()
