"""Render a 3-column axonometric (3D isometric) collage of the v3 demos.

The point is to make the *multi-storey* nature visible: a top-view alone
cannot show two stacked floors, but an axonometric projection can.
"""
from __future__ import annotations

import math

import ifcopenshell
import ifcopenshell.geom as gm
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

DEMOS = [
    ("v3_p1_hotel_iter2",       "P1 — Hotel (2 floors, 16 rooms)"),
    ("v3_p2_residential_iter2", "P2 — Residential (4 floors)"),
    ("v3_p3_house_iter2",       "P3 — Single-family house (1 floor)"),
]

# Isometric projection: rotate world about Z by 45°, then tilt around X.
_THETA = math.radians(35)     # azimuth (around Z)
_PHI = math.radians(25)       # elevation tilt

_COS_T, _SIN_T = math.cos(_THETA), math.sin(_THETA)
_COS_P, _SIN_P = math.cos(_PHI), math.sin(_PHI)


def _project(x: float, y: float, z: float) -> tuple[float, float]:
    """World → screen (2D) via standard isometric projection."""
    xs = x * _COS_T - y * _SIN_T
    ys = (x * _SIN_T + y * _COS_T) * _SIN_P + z * _COS_P
    return xs, ys


def _settings():
    s = gm.settings()
    s.set(s.USE_WORLD_COORDS, True)
    return s


def _faces_of_shape(shape):
    """Yield (face_vertices_world) tuples per triangle."""
    verts = shape.geometry.verts
    faces = shape.geometry.faces
    for i in range(0, len(faces), 3):
        i0, i1, i2 = faces[i], faces[i + 1], faces[i + 2]
        v = []
        for k in (i0, i1, i2):
            v.append((verts[3 * k], verts[3 * k + 1], verts[3 * k + 2]))
        yield v


def _draw_axonometric(ax, ifc_path: str, title: str):
    model = ifcopenshell.open(ifc_path)
    s = _settings()

    # Draw order matters: slabs first (semi-transparent so upper-floor
    # elements remain visible through them), then walls/openings on top.
    class_styles = [
        ("IfcSlab",    "#cdb78a", 0.30),
        ("IfcRoof",    "#c08a5c", 0.35),
        ("IfcWall",    "#9a9a9a", 0.85),
        ("IfcRailing", "#e0a040", 0.9),
        ("IfcDoor",    "#d04444", 1.0),
        ("IfcWindow",  "#4a82c4", 0.95),
    ]

    all_xs, all_ys = [], []
    for cls, color, alpha in class_styles:
        polys2d = []
        for el in model.by_type(cls):
            try:
                shape = gm.create_shape(s, el)
            except Exception:
                continue
            for tri in _faces_of_shape(shape):
                proj = [_project(*p) for p in tri]
                polys2d.append(proj)
                for px, py in proj:
                    all_xs.append(px); all_ys.append(py)
        if polys2d:
            ax.add_collection(PolyCollection(
                polys2d, facecolor=color, edgecolor="#333",
                linewidth=0.05, alpha=alpha,
            ))

    ax.set_aspect("equal")
    ax.set_title(title, fontsize=10)
    ax.axis("off")
    if all_xs:
        pad_x = (max(all_xs) - min(all_xs)) * 0.05
        pad_y = (max(all_ys) - min(all_ys)) * 0.05
        ax.set_xlim(min(all_xs) - pad_x, max(all_xs) + pad_x)
        ax.set_ylim(min(all_ys) - pad_y, max(all_ys) + pad_y)


def main():
    fig, axes = plt.subplots(1, len(DEMOS), figsize=(6.5 * len(DEMOS), 6.0))
    for ax, (run, title) in zip(axes, DEMOS):
        _draw_axonometric(ax, f"test_output/text2ifc/{run}.ifc", title)
    fig.suptitle(
        "Text2BIM demos — isometric 3D view (multi-storey now stacks correctly)",
        fontsize=13, fontweight="bold",
    )
    fig.tight_layout()
    out = "test_output/text2ifc/v3_three_demos_axonometric.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    print(f"Saved → {out}")


if __name__ == "__main__":
    main()
