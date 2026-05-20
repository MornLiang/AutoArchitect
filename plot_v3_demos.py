"""Render a 3-column collage of the three v3 Text2BIM demos
(after the *centered-origin* expander fix).

Each column = one demo (P1 hotel / P2 residential / P3 house),
each row    = one storey (Ground Floor / First Floor).
A small crosshair at (0, 0) makes it visible that every model
is now centred on the world origin.
"""
from __future__ import annotations

import matplotlib.pyplot as plt

from plot_ifc_topview import _draw_ifc, _list_storey_names

DEMOS = [
    ("v3_p1_hotel_iter2",       "P1 — Hotel (16 rooms / 2 floors)"),
    ("v3_p2_residential_iter2", "P2 — Residential"),
    ("v3_p3_house_iter2",       "P3 — Single-family house"),
]


def _add_origin_marker(ax):
    """Draw a crosshair + dot at world origin (0, 0)."""
    ax.axhline(0, color="tab:red", linewidth=0.6, alpha=0.45, zorder=5)
    ax.axvline(0, color="tab:red", linewidth=0.6, alpha=0.45, zorder=5)
    ax.plot(0, 0, marker="o", color="tab:red", markersize=5,
            zorder=6, label="origin (0,0)")


def _pick_storeys(ifc_path: str) -> list[str]:
    """Return only the 'inhabited' storeys (drop pure roof levels)."""
    sns = [s for s in _list_storey_names(ifc_path) if s]
    return [s for s in sns if "roof" not in s.lower()]


def main():
    # Show at most 2 inhabited storeys per demo: ground + one upper.
    n_rows = 2
    storey_lists: list[list[str]] = []
    for run, _ in DEMOS:
        sns = _pick_storeys(f"test_output/text2ifc/{run}.ifc")
        storey_lists.append(sns[:n_rows])

    fig, axes = plt.subplots(
        n_rows, len(DEMOS),
        figsize=(6.2 * len(DEMOS), 5.0 * n_rows),
        squeeze=False,
    )

    for col, ((run, title), storeys) in enumerate(zip(DEMOS, storey_lists)):
        ifc_path = f"test_output/text2ifc/{run}.ifc"
        for row in range(n_rows):
            ax = axes[row, col]
            if row < len(storeys):
                storey = storeys[row]
                _draw_ifc(ax, ifc_path, f"{title}\n{storey}",
                          filter_storey=storey)
                _add_origin_marker(ax)
                if row == 0 and col == 0:
                    ax.legend(loc="lower left", fontsize=8, framealpha=0.8)
            else:
                ax.axis("off")
                ax.set_title(f"{title}\n(single-storey)",
                             fontsize=9, alpha=0.5)

    fig.suptitle(
        "Text2BIM demos — centered-origin expander\n"
        "(top-view of real Body geometry; red cross-hair = world origin)",
        fontsize=13, fontweight="bold",
    )
    fig.tight_layout()
    out = "test_output/text2ifc/v3_three_demos_centered.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"Saved → {out}")


if __name__ == "__main__":
    main()
