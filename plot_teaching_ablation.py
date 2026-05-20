"""Ablation of image input.

All four panels share the SAME text prompt and the SAME deterministic
expander+builder pipeline.  Only the source of the initial SpatialGraph
differs:

  (a) reference floor plan (image, oracle)
  (b) image-seed iter1 — graph hand-crafted while LOOKING at the image
  (c) text-seed iter1  — graph hand-crafted from TEXT only (this ablates
                         the image input)
  (d) LLM-only iter1   — DeepSeek Architect reads the text and produces
                         the graph autonomously
"""
from __future__ import annotations

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from plot_ifc_topview import _draw_ifc

fig, axes = plt.subplots(1, 4, figsize=(34, 8))

ref = mpimg.imread("floor_plan/demo_floor.png")
axes[0].imshow(ref)
axes[0].set_title("(a) Reference floor plan (image)", fontsize=11)
axes[0].axis("off")

panels = [
    (axes[1], "test_output/text2ifc/teaching_seed_iter1.ifc",
     "(b) image-seed iter1\nhand-crafted graph WITH image"),
    (axes[2], "test_output/text2ifc/teaching_textseed_iter1.ifc",
     "(c) text-seed iter1\nhand-crafted graph WITHOUT image"),
    (axes[3], "test_output/text2ifc/teaching_textonly_iter1.ifc",
     "(d) LLM-only iter1\nDeepSeek Architect from text"),
]
for ax, ifc, title in panels:
    _draw_ifc(ax, ifc, title, filter_storey="Ground Floor")
    ax.axhline(0, color="tab:red", linewidth=0.5, alpha=0.4)
    ax.axvline(0, color="tab:red", linewidth=0.5, alpha=0.4)
    ax.plot(0, 0, "o", color="tab:red", markersize=4)

fig.suptitle(
    "Image-input ablation — all four use the same text + same pipeline; "
    "only the SpatialGraph source differs",
    fontsize=13, fontweight="bold",
)
fig.tight_layout()
out = "test_output/text2ifc/teaching_ablation.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print(f"Saved → {out}")
