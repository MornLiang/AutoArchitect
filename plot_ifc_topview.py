"""
Render a TRUE TOP-VIEW PNG of an IFC file based on each element's
real Body (extruded mesh) geometry — not Axis.

Usage::

    python plot_ifc_topview.py --gt ../demo_data/"1px(1).ifc" \\
        --gen test_output/text2ifc/final_rooms_llm_iter2.ifc \\
        --out /tmp/topview.png
"""

from __future__ import annotations

import argparse
import math

import ifcopenshell
import ifcopenshell.geom as gm
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
from matplotlib.collections import PatchCollection


def _settings():
    s = gm.settings()
    s.set(s.USE_WORLD_COORDS, True)
    return s


def _shape_top_polygon(verts, faces) -> list[tuple[float, float]]:
    """Return the convex-ish outline of *verts* projected to XY plane.

    Good-enough for top-view rendering: we take the convex hull of all
    XY-projected vertices.
    """
    pts = list({(round(verts[i], 1), round(verts[i + 1], 1))
                for i in range(0, len(verts), 3)})
    if len(pts) < 3:
        return pts

    # Andrew's monotone chain convex hull
    pts.sort()
    def _cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _draw_ifc(ax, ifc_path: str, title: str, *,
              filter_storey: str | None = None):
    model = ifcopenshell.open(ifc_path)
    s = _settings()

    classes = [
        ("IfcSpace",                "#ffe8b3",    0.25, None),
        ("IfcSlab",                 "#888",       0.15, None),
        ("IfcWall",                 "#444",       0.9,  "wall"),
        ("IfcDoor",                 "tab:red",    0.7,  "door"),
        ("IfcWindow",               "tab:blue",   0.7,  "window"),
        ("IfcColumn",               "tab:green",  0.8,  "column"),
        ("IfcRailing",              "tab:orange", 0.7,  None),
        ("IfcFurniture",            "#5b8c41",    0.85, "furniture"),
        ("IfcSanitaryTerminal",     "#3a83bd",    0.9,  "sanitary"),
        ("IfcBuildingElementProxy", "#222",       0.85, "blackboard"),
        ("IfcStairFlight",          "#a05a2c",    0.85, "stair"),
    ]

    storey_names: set[str] = set()
    all_xs, all_ys = [], []
    plotted_kinds: set[str] = set()

    for cls, color, alpha, legend in classes:
        patches = []
        for el in model.by_type(cls):
            sn = None
            rels = list(getattr(el, "ContainedInStructure", []) or [])
            rels += list(getattr(el, "Decomposes", []) or [])
            for rel in rels:
                rel_struct = (getattr(rel, "RelatingStructure", None)
                              or getattr(rel, "RelatingObject", None))
                if rel_struct is not None and rel_struct.is_a("IfcBuildingStorey"):
                    sn = rel_struct.Name or ""
                    break
            storey_names.add(sn or "")
            if filter_storey is not None and sn != filter_storey:
                continue
            try:
                shape = gm.create_shape(s, el)
            except Exception:
                continue
            verts = shape.geometry.verts
            if not verts:
                continue
            poly = _shape_top_polygon(verts, None)
            if len(poly) >= 3:
                patches.append(Polygon(poly, closed=True))
                for x, y in poly:
                    all_xs.append(x); all_ys.append(y)
        if patches:
            ax.add_collection(PatchCollection(
                patches, facecolor=color, edgecolor=color,
                linewidth=0.4, alpha=alpha,
            ))
            if legend:
                plotted_kinds.add(legend)

    ax.set_aspect("equal")
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.grid(True, alpha=0.2)
    if all_xs:
        pad = 1.0
        ax.set_xlim(min(all_xs) - pad, max(all_xs) + pad)
        ax.set_ylim(min(all_ys) - pad, max(all_ys) + pad)
    return storey_names


def _list_storey_names(ifc_path: str) -> list[str]:
    m = ifcopenshell.open(ifc_path)
    storeys = m.by_type("IfcBuildingStorey")
    storeys.sort(key=lambda s: float(getattr(s, "Elevation", 0.0) or 0.0))
    return [s.Name or "" for s in storeys]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gt", required=True)
    p.add_argument("--gen", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--gt-storey", default=None)
    p.add_argument("--gen-storey", default=None)
    p.add_argument("--per-storey", action="store_true",
                   help="One row per storey, both files side by side")
    args = p.parse_args()

    if not args.per_storey:
        fig, axes = plt.subplots(1, 2, figsize=(16, 9))
        sn_gt = _draw_ifc(axes[0], args.gt, f"GT — {args.gt}",
                          filter_storey=args.gt_storey)
        sn_gen = _draw_ifc(axes[1], args.gen, f"GENERATED — {args.gen}",
                           filter_storey=args.gen_storey)
        print(f"GT storey names: {sorted(sn_gt)}")
        print(f"GEN storey names: {sorted(sn_gen)}")
    else:
        gt_storeys = [s for s in _list_storey_names(args.gt) if s]
        gen_storeys = [s for s in _list_storey_names(args.gen) if s]
        n = max(len(gt_storeys), len(gen_storeys), 1)
        fig, axes = plt.subplots(n, 2, figsize=(16, 4.5 * n),
                                 squeeze=False)
        for i in range(n):
            gt_s = gt_storeys[i] if i < len(gt_storeys) else None
            gen_s = gen_storeys[i] if i < len(gen_storeys) else None
            _draw_ifc(axes[i, 0], args.gt,
                      f"GT — {gt_s or '(missing)'}",
                      filter_storey=gt_s)
            _draw_ifc(axes[i, 1], args.gen,
                      f"GEN — {gen_s or '(missing)'}",
                      filter_storey=gen_s)
        print(f"GT storeys (sorted by elev): {gt_storeys}")
        print(f"GEN storeys (sorted by elev): {gen_storeys}")

    fig.suptitle("Top-view of Body geometry (what the BIM viewer renders)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(args.out, dpi=120, bbox_inches="tight")
    print(f"Saved → {args.out}")


if __name__ == "__main__":
    main()
