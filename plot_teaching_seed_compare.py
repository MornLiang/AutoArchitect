"""3-panel comparison:
1) Reference floor plan (the image).
2) Hand-crafted seed SpatialGraph → expander → builder (iter1).
3) LLM Refiner+Architect output on top of seed (iter2).
"""
from __future__ import annotations

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from plot_ifc_topview import _draw_ifc

fig, axes = plt.subplots(1, 3, figsize=(28, 9))

ref = mpimg.imread("floor_plan/demo_floor.png")
axes[0].imshow(ref)
axes[0].set_title(
    "(a) Reference floor plan — 教学楼一层平面图", fontsize=12,
)
axes[0].axis("off")

_draw_ifc(
    axes[1], "test_output/text2ifc/teaching_seed_iter1.ifc",
    "(b) iter1 — hand-crafted SpatialGraph (no Architect LLM)\n"
    "49 walls · 16 doors · 26 windows · IDS 13/13",
    filter_storey="Ground Floor",
)
axes[1].axhline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[1].axvline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[1].plot(0, 0, "o", color="tab:red", markersize=4)

_draw_ifc(
    axes[2], "test_output/text2ifc/teaching_seed_iter2.ifc",
    "(c) iter2 — DeepSeek Refiner→Architect on top of seed\n"
    "49 walls · 16 doors · 22 windows · IDS 13/13",
    filter_storey="Ground Floor",
)
axes[2].axhline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[2].axvline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[2].plot(0, 0, "o", color="tab:red", markersize=4)

fig.suptitle(
    "Text2BIM teaching-building demo — seed SpatialGraph + LLM refinement",
    fontsize=14, fontweight="bold",
)
fig.tight_layout()
out = "test_output/text2ifc/teaching_seed_compare.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print(f"Saved → {out}")
