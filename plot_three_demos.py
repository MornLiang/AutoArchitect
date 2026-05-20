"""Render a single PNG showing the GROUND FLOOR top-view of three demo IFCs."""

from __future__ import annotations

import matplotlib.pyplot as plt
from plot_ifc_topview import _draw_ifc


demos = [
    ("test_output/text2ifc/t2b_p1_hotel_iter1.ifc", "Ground Floor",
     "Prompt 1 — 2-story hotel\n8 rooms/floor, 4m central corridor"),
    ("test_output/text2ifc/t2b_p2_residential_iter1.ifc", "Ground Floor",
     "Prompt 2 — 4-story residential\n5m × 3m footprint"),
    ("test_output/text2ifc/t2b_p3_house_iter1.ifc", "Ground Floor",
     "Prompt 3 — Single-story house\n120 m², 3 BR + 2 BA + kitchen + living"),
]

fig, axes = plt.subplots(1, 3, figsize=(20, 7))
for ax, (path, storey, title) in zip(axes, demos):
    _draw_ifc(ax, path, title, filter_storey=storey)

fig.suptitle("Text2BIM benchmark prompts → Text2IFC output (Ground Floor only)",
             fontsize=14, fontweight="bold")
fig.tight_layout()
out = "test_output/text2ifc/t2b_three_demos.png"
fig.savefig(out, dpi=130, bbox_inches="tight")
print(f"Saved → {out}")
