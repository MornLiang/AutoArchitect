"""Isometric 3D view of the furnished teaching building."""
from __future__ import annotations

import math

import ifcopenshell
import ifcopenshell.geom as gm
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

IFC_PATH = "test_output/text2ifc/teaching_v3_iter1.ifc"
OUT = "test_output/text2ifc/teaching_v3_axonometric.png"

_THETA = math.radians(40)
_PHI = math.radians(22)
_COS_T, _SIN_T = math.cos(_THETA), math.sin(_THETA)
_COS_P, _SIN_P = math.cos(_PHI), math.sin(_PHI)


def _project(x, y, z):
    xs = x * _COS_T - y * _SIN_T
    ys = (x * _SIN_T + y * _COS_T) * _SIN_P + z * _COS_P
    return xs, ys


def _settings():
    s = gm.settings()
    s.set(s.USE_WORLD_COORDS, True)
    return s


def _faces(shape):
    verts = shape.geometry.verts
    faces = shape.geometry.faces
    for i in range(0, len(faces), 3):
        tri = []
        for k in (faces[i], faces[i + 1], faces[i + 2]):
            tri.append((verts[3 * k], verts[3 * k + 1], verts[3 * k + 2]))
        yield tri


def main():
    model = ifcopenshell.open(IFC_PATH)
    s = _settings()

    styles = [
        ("IfcSlab",                 "#cdb78a", 0.35),
        ("IfcRoof",                 "#c08a5c", 0.40),
        ("IfcWall",                 "#aaaaaa", 0.85),
        ("IfcDoor",                 "#d04444", 1.0),
        ("IfcWindow",               "#4a82c4", 0.95),
        ("IfcRailing",              "#e0a040", 0.9),
        ("IfcStairFlight",          "#a05a2c", 1.0),
        ("IfcFurniture",            "#5b8c41", 1.0),
        ("IfcSanitaryTerminal",     "#3a83bd", 1.0),
        ("IfcBuildingElementProxy", "#222222", 0.95),
    ]

    fig, ax = plt.subplots(figsize=(16, 9))
    all_xs, all_ys = [], []
    for cls, color, alpha in styles:
        polys = []
        for el in model.by_type(cls):
            try:
                shape = gm.create_shape(s, el)
            except Exception:
                continue
            for tri in _faces(shape):
                proj = [_project(*p) for p in tri]
                polys.append(proj)
                for px, py in proj:
                    all_xs.append(px); all_ys.append(py)
        if polys:
            ax.add_collection(PolyCollection(
                polys, facecolor=color, edgecolor="#222",
                linewidth=0.03, alpha=alpha,
            ))

    ax.set_aspect("equal")
    ax.axis("off")
    if all_xs:
        pad = (max(all_xs) - min(all_xs)) * 0.03
        ax.set_xlim(min(all_xs) - pad, max(all_xs) + pad)
        ax.set_ylim(min(all_ys) - pad, max(all_ys) + pad)
    fig.suptitle(
        "Teaching building — furnished IFC (3D isometric)\n"
        "desks / chairs / blackboards / toilets / washbasins / stair flights",
        fontsize=12, fontweight="bold",
    )
    fig.tight_layout()
    fig.savefig(OUT, dpi=140, bbox_inches="tight")
    print(f"Saved → {OUT}")


if __name__ == "__main__":
    main()
