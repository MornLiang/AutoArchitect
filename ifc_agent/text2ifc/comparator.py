"""
Compare a generated IFC against a ground-truth IFC and emit a structured
discrepancy report consumable by the Refiner agent.

We compute:
  - element-count deltas (walls, doors, windows, columns, slabs, …)
  - storey deltas (count, elevation, height)
  - footprint extent deltas (bbox in X/Y from wall placements)
  - schema / unit consistency

The output is a JSON-serialisable dict so the orchestrator can both log it
and feed it back into a prompt.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from ifc_agent.text2ifc.gt_describer import describe_ifc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

@dataclass
class DiffEntry:
    field: str
    gt: float
    generated: float
    delta: float
    severity: str  # "ok" | "minor" | "major"


@dataclass
class ComparisonReport:
    gt_stats: dict = field(default_factory=dict)
    generated_stats: dict = field(default_factory=dict)
    diffs: list[DiffEntry] = field(default_factory=list)
    score: float = 0.0       # 0..1, higher is better
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "gt_stats": self.gt_stats,
            "generated_stats": self.generated_stats,
            "diffs": [asdict(d) for d in self.diffs],
            "score": self.score,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

# Element-count fields scored with a tolerance-aware similarity.
_COUNT_FIELDS = [
    "storey_count",
    "wall_count",
    "door_count",
    "window_count",
    "column_count",
    "slab_count",
    "railing_count",
    "roof_count",
]

_DIM_FIELDS = [
    ("storey_height_mm", 200.0),  # tolerance in mm
    ("footprint_x_mm",   500.0),
    ("footprint_y_mm",   500.0),
    # ---- Wall topology metrics (added in Phase 2) -----------------------
    ("wall_length_median_mm",  1500.0),   # tolerance in mm
    ("wall_length_std_mm",     2000.0),
    # ratio / entropy: tolerance is on the metric itself (small numbers)
    ("wall_short_ratio",       0.25),     # 0..1
    ("wall_orient_entropy",    0.4),      # max ≈ 1.585
]


def _severity(delta_ratio: float, ratio_minor: float = 0.10,
              ratio_major: float = 0.30) -> str:
    if delta_ratio < ratio_minor:
        return "ok"
    if delta_ratio < ratio_major:
        return "minor"
    return "major"


def compare(gt_path: str, generated_path: str) -> ComparisonReport:
    """Compare the IFC at *generated_path* with the GT at *gt_path*."""
    gt = describe_ifc(gt_path)
    gen = describe_ifc(generated_path)

    report = ComparisonReport(gt_stats=gt, generated_stats=gen)

    similarities: list[float] = []

    # --- Count fields ---
    for fname in _COUNT_FIELDS:
        gt_v = float(gt.get(fname, 0))
        gen_v = float(gen.get(fname, 0))
        denom = max(gt_v, gen_v, 1.0)
        delta = gen_v - gt_v
        delta_ratio = abs(delta) / denom
        sim = max(0.0, 1.0 - delta_ratio)
        similarities.append(sim)
        report.diffs.append(DiffEntry(
            field=fname,
            gt=gt_v, generated=gen_v, delta=delta,
            severity=_severity(delta_ratio),
        ))

    # --- Dimension fields ---
    for fname, tol in _DIM_FIELDS:
        gt_v = float(gt.get(fname, 0))
        gen_v = float(gen.get(fname, 0))
        delta = gen_v - gt_v
        if gt_v == 0 and gen_v == 0:
            sim = 1.0
            sev = "ok"
        else:
            denom = max(abs(gt_v), abs(gen_v), tol)
            delta_ratio = abs(delta) / denom
            sim = max(0.0, 1.0 - delta_ratio)
            sev = _severity(delta_ratio)
        similarities.append(sim)
        report.diffs.append(DiffEntry(
            field=fname,
            gt=gt_v, generated=gen_v, delta=delta,
            severity=sev,
        ))

    report.score = sum(similarities) / len(similarities) if similarities else 0.0
    report.summary = _make_summary(report)
    return report


def _make_summary(report: ComparisonReport) -> str:
    issues = [d for d in report.diffs if d.severity != "ok"]
    lines = [f"Overall similarity score: {report.score:.3f}",
             f"GT: {report.gt_stats.get('description', '')}",
             f"Generated: {report.generated_stats.get('description', '')}",
             ""]
    if not issues:
        lines.append("All metrics within tolerance.")
        return "\n".join(lines)
    lines.append(f"{len(issues)} metric(s) outside tolerance:")
    for d in issues:
        sign = "+" if d.delta >= 0 else "-"
        lines.append(
            f"  [{d.severity:>5}] {d.field}: gt={d.gt:g}, generated={d.generated:g} "
            f"(delta={sign}{abs(d.delta):g})"
        )
    return "\n".join(lines)


def format_report(report: ComparisonReport) -> str:
    """Pretty-print a ComparisonReport."""
    return report.summary
