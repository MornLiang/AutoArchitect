"""Render iter1 vs iter2 ground-floor top views for 3 v2 demos."""

import matplotlib.pyplot as plt
from plot_ifc_topview import _draw_ifc

demos = [
    ('v2_p1_hotel',       'P1: hotel'),
    ('v2_p2_residential', 'P2: residential'),
    ('v2_p3_house',       'P3: house'),
]

fig, axes = plt.subplots(3, 2, figsize=(18, 18))
for row, (name, title) in enumerate(demos):
    _draw_ifc(axes[row, 0], f'test_output/text2ifc/{name}_iter1.ifc',
              f'{title} — iter 1', filter_storey='Ground Floor')
    _draw_ifc(axes[row, 1], f'test_output/text2ifc/{name}_iter2.ifc',
              f'{title} — iter 2 (after IDS-aware refinement)',
              filter_storey='Ground Floor')

fig.suptitle('Text2BIM demos with IDS-aware iterative refinement '
             '(iter1 → iter2)', fontsize=14, fontweight='bold')
fig.tight_layout()
out = 'test_output/text2ifc/v2_three_demos_iter_compare.png'
fig.savefig(out, dpi=120, bbox_inches='tight')
print(f'Saved → {out}')
