"""Side-by-side: reference floor plan PNG vs generated teaching-building IFC."""
from __future__ import annotations

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from plot_ifc_topview import _draw_ifc

fig, axes = plt.subplots(1, 2, figsize=(20, 8))

ref = mpimg.imread("floor_plan/demo_floor.png")
axes[0].imshow(ref)
axes[0].set_title("Reference layout — 教学楼一层平面图", fontsize=12)
axes[0].axis("off")

_draw_ifc(
    axes[1], "test_output/text2ifc/teaching_v3_iter1.ifc",
    "Generated IFC — teaching_v3_iter1.ifc (top view, furnished)",
    filter_storey="Ground Floor",
)
axes[1].axhline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[1].axvline(0, color="tab:red", linewidth=0.5, alpha=0.4)
axes[1].plot(0, 0, "o", color="tab:red", markersize=4)

fig.suptitle("Text2BIM — reference layout vs. generated IFC",
             fontsize=14, fontweight="bold")
fig.tight_layout()
out = "test_output/text2ifc/teaching_v3_compare.png"
fig.savefig(out, dpi=120, bbox_inches="tight")
print(f"Saved → {out}")
