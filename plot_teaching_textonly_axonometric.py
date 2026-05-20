"""Side-by-side 3D isometric of text-only iter1 vs iter2 (no seed)."""
from __future__ import annotations

import math
import ifcopenshell
import ifcopenshell.geom as gm
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection

PAIRS = [
    ("test_output/text2ifc/teaching_textonly_iter1.ifc",
     "(a) text-only iter1 — DeepSeek Architect from text"),
    ("test_output/text2ifc/teaching_textonly_iter2.ifc",
     "(b) text-only iter2 — after LLM Refiner→Architect"),
]
OUT = "test_output/text2ifc/teaching_textonly_axonometric.png"

_THETA = math.radians(40); _PHI = math.radians(22)
_COS_T, _SIN_T = math.cos(_THETA), math.sin(_THETA)
_COS_P, _SIN_P = math.cos(_PHI), math.sin(_PHI)


def _project(x, y, z):
    return (x * _COS_T - y * _SIN_T,
            (x * _SIN_T + y * _COS_T) * _SIN_P + z * _COS_P)


def _settings():
    s = gm.settings(); s.set(s.USE_WORLD_COORDS, True); return s


def _faces(shape):
    v = shape.geometry.verts; f = shape.geometry.faces
    for i in range(0, len(f), 3):
        yield [(v[3*f[i+k]], v[3*f[i+k]+1], v[3*f[i+k]+2]) for k in range(3)]


STYLES = [
    ("IfcSlab", "#cdb78a", 0.35),
    ("IfcRoof", "#c08a5c", 0.40),
    ("IfcWall", "#aaaaaa", 0.85),
    ("IfcDoor", "#d04444", 1.0),
    ("IfcWindow", "#4a82c4", 0.95),
    ("IfcRailing", "#e0a040", 0.9),
    ("IfcStairFlight", "#a05a2c", 1.0),
    ("IfcFurniture", "#5b8c41", 1.0),
    ("IfcSanitaryTerminal", "#3a83bd", 1.0),
    ("IfcBuildingElementProxy", "#222222", 0.95),
]


def _draw(ax, ifc_path, title):
    model = ifcopenshell.open(ifc_path); s = _settings()
    xs, ys = [], []
    for cls, color, alpha in STYLES:
        polys = []
        for el in model.by_type(cls):
            try:
                sh = gm.create_shape(s, el)
            except Exception:
                continue
            for tri in _faces(sh):
                p = [_project(*pt) for pt in tri]
                polys.append(p)
                for px, py in p:
                    xs.append(px); ys.append(py)
        if polys:
            ax.add_collection(PolyCollection(
                polys, facecolor=color, edgecolor="#222",
                linewidth=0.03, alpha=alpha))
    ax.set_aspect("equal"); ax.axis("off")
    if xs:
        pad = (max(xs) - min(xs)) * 0.03
        ax.set_xlim(min(xs) - pad, max(xs) + pad)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_title(title, fontsize=11)


fig, axes = plt.subplots(1, 2, figsize=(22, 9))
for ax, (path, title) in zip(axes, PAIRS):
    _draw(ax, path, title)
fig.suptitle(
    "Teaching building — TEXT-ONLY pipeline (3D isometric)",
    fontsize=13, fontweight="bold")
fig.tight_layout()
fig.savefig(OUT, dpi=140, bbox_inches="tight")
print(f"Saved → {OUT}")
