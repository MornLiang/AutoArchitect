"""
Generate a short, natural-language description for any IFC file.

Used to produce the user-facing prompt that drives the Text2IFC pipeline
when evaluating against ground-truth models.

The generated description is always in plain English: many real-world IFC
files (e.g. those exported from Chinese BIM tools) carry localised material
names like "默认墙"; we map those to an English fallback so downstream
LLM prompts stay monolingual.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import ifcopenshell
import ifcopenshell.util.element as eu
import ifcopenshell.util.placement as up

logger = logging.getLogger(__name__)


# Common Chinese / non-English BIM defaults → English fallback.
# Keys are normalised (lower-case, whitespace-stripped) before lookup.
_MATERIAL_TRANSLATIONS: dict[str, str] = {
    "默认墙":          "concrete",
    "默认":            "concrete",
    "墙":              "concrete",
    "混凝土":          "concrete",
    "钢筋混凝土":      "reinforced concrete",
    "砖":              "brick",
    "砖砌体":          "brick",
    "砖墙":            "brick",
    "石材":            "stone",
    "玻璃":            "glass",
    "木":              "wood",
    "木材":            "wood",
    "钢":              "steel",
    "钢材":            "steel",
}

_NON_ASCII = re.compile(r"[^\x00-\x7F]")


def _wall_topology_metrics(walls) -> dict:
    """Extract length & orientation statistics from a list of IfcWall.

    Returns keys (all numbers):
      * wall_length_median_mm     — median axis length
      * wall_length_std_mm        — sample stdev of axis lengths
      * wall_short_ratio          — fraction of walls shorter than 5 m
                                    (short walls indicate real room
                                     partitioning rather than long
                                     "fence" walls)
      * wall_orient_h / wall_orient_v / wall_orient_d
                                  — counts of horizontal / vertical /
                                    diagonal walls in WORLD frame
      * wall_orient_entropy       — Shannon entropy of the H/V/D
                                    distribution (max ≈ log2(3) ≈ 1.585)
    """
    import math
    import statistics

    lengths: list[float] = []
    orient = {"H": 0, "V": 0, "D": 0}

    for w in walls:
        rep = None
        if w.Representation is not None:
            for r in w.Representation.Representations:
                if r.RepresentationIdentifier == "Axis":
                    rep = r
                    break
        if rep is None or not rep.Items:
            continue
        item = rep.Items[0]
        p0_local, p1_local = None, None
        try:
            if item.is_a("IfcPolyline"):
                pts = item.Points
                if len(pts) >= 2:
                    p0_local = (pts[0].Coordinates[0], pts[0].Coordinates[1])
                    p1_local = (pts[-1].Coordinates[0], pts[-1].Coordinates[1])
            elif item.is_a("IfcTrimmedCurve"):
                line = item.BasisCurve
                d = line.Dir.DirectionRatios
                origin = line.Pnt.Coordinates
                t0 = item.Trim1[0].wrappedValue if item.Trim1 else 0.0
                t1 = item.Trim2[0].wrappedValue if item.Trim2 else 0.0
                p0_local = (origin[0] + d[0] * t0, origin[1] + d[1] * t0)
                p1_local = (origin[0] + d[0] * t1, origin[1] + d[1] * t1)
        except Exception:
            continue
        if p0_local is None or p1_local is None:
            continue

        # Transform local → world via ObjectPlacement
        try:
            placement = up.get_local_placement(w.ObjectPlacement)
            def _apply(p):
                return (
                    placement[0, 0] * p[0] + placement[0, 1] * p[1] + placement[0, 3],
                    placement[1, 0] * p[0] + placement[1, 1] * p[1] + placement[1, 3],
                )
            p0 = _apply(p0_local)
            p1 = _apply(p1_local)
        except Exception:
            p0, p1 = p0_local, p1_local

        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 1e-3:
            continue
        lengths.append(L)

        if abs(dy) < 1.0:
            orient["H"] += 1
        elif abs(dx) < 1.0:
            orient["V"] += 1
        else:
            orient["D"] += 1

    if not lengths:
        return {
            "wall_length_median_mm": 0.0,
            "wall_length_std_mm": 0.0,
            "wall_short_ratio": 0.0,
            "wall_orient_h": 0,
            "wall_orient_v": 0,
            "wall_orient_d": 0,
            "wall_orient_entropy": 0.0,
        }

    median = statistics.median(lengths)
    std = statistics.stdev(lengths) if len(lengths) > 1 else 0.0
    short_ratio = sum(1 for L in lengths if L < 5000.0) / len(lengths)

    total = sum(orient.values()) or 1
    probs = [c / total for c in orient.values() if c > 0]
    entropy = -sum(p * math.log2(p) for p in probs) if probs else 0.0

    return {
        "wall_length_median_mm": median,
        "wall_length_std_mm": std,
        "wall_short_ratio": short_ratio,
        "wall_orient_h": orient["H"],
        "wall_orient_v": orient["V"],
        "wall_orient_d": orient["D"],
        "wall_orient_entropy": entropy,
    }


def _english_material_name(name: Optional[str]) -> str:
    """Return an English-only material label.

    If *name* is ASCII it is returned (lower-cased, trimmed) verbatim.
    Otherwise we look up an explicit translation; if there is none, we
    fall back to a safe generic English label so the description stays
    monolingual.
    """
    if not name:
        return "concrete"
    cleaned = name.strip()
    if not _NON_ASCII.search(cleaned):
        return cleaned
    return _MATERIAL_TRANSLATIONS.get(cleaned, "concrete")


def describe_ifc(ifc_path: str, *, max_chars: int = 600) -> dict:
    """Return a dict with summary stats and a short natural-language sentence.

    The description is intentionally compact (one paragraph), suitable as a
    user prompt for the Text2IFC pipeline.
    """
    model = ifcopenshell.open(ifc_path)

    storeys = model.by_type("IfcBuildingStorey")
    # Avoid double-counting: IfcWall + IfcWallStandardCase, the latter is a
    # subtype of the former, so by_type("IfcWall") already covers both.
    walls = model.by_type("IfcWall")
    doors = model.by_type("IfcDoor")
    windows = model.by_type("IfcWindow")
    columns = model.by_type("IfcColumn")
    all_slabs = model.by_type("IfcSlab")
    beams = model.by_type("IfcBeam")
    railings = model.by_type("IfcRailing")
    coverings = model.by_type("IfcCovering")
    roof_entities = model.by_type("IfcRoof")

    # Separate floor slabs from roof slabs by PredefinedType
    floor_slabs = [s for s in all_slabs
                   if (getattr(s, "PredefinedType", None) or "FLOOR").upper() != "ROOF"]
    roof_slabs = [s for s in all_slabs
                  if (getattr(s, "PredefinedType", None) or "").upper() == "ROOF"]
    # Roof coverings: only count those explicitly typed as ROOFING/ROOF.
    # Anything CEILING / FLOORING / WALL etc. is NOT a roof.
    roof_coverings = [
        c for c in coverings
        if (getattr(c, "PredefinedType", None) or "").upper()
        in ("ROOFING", "ROOF")
    ]
    # Aggregate "roof-like" entities for the roof_count metric
    total_roof_like = len(roof_entities) + len(roof_slabs) + len(roof_coverings)

    # Footprint estimation from wall ObjectPlacements
    xs, ys = [], []
    for w in walls:
        try:
            mat = up.get_local_placement(w.ObjectPlacement)
            xs.append(mat[0, 3]); ys.append(mat[1, 3])
        except Exception:
            continue

    footprint_x = (max(xs) - min(xs)) if xs else 0.0
    footprint_y = (max(ys) - min(ys)) if ys else 0.0

    # Floor-to-floor height from storey elevations
    elevations = sorted(getattr(s, "Elevation", 0.0) or 0.0 for s in storeys)
    storey_height = 3000.0
    if len(elevations) >= 2:
        storey_height = elevations[1] - elevations[0]

    # Dominant wall material (look at first 10 walls)
    mat_counts: dict[str, int] = {}
    for w in walls[:10]:
        try:
            m = eu.get_material(w)
        except Exception:
            m = None
        if m is None:
            continue
        if m.is_a("IfcMaterial"):
            name = m.Name or "Unnamed"
            mat_counts[name] = mat_counts.get(name, 0) + 1
        elif m.is_a("IfcMaterialLayerSet") or m.is_a("IfcMaterialLayerSetUsage"):
            ls = m.ForLayerSet if m.is_a("IfcMaterialLayerSetUsage") else m
            for L in ls.MaterialLayers:
                if L.Material:
                    name = L.Material.Name or "Unnamed"
                    mat_counts[name] = mat_counts.get(name, 0) + 1
    dominant_material_raw = (
        max(mat_counts.items(), key=lambda x: x[1])[0] if mat_counts else "concrete"
    )
    dominant_material = _english_material_name(dominant_material_raw)

    # ---- Wall topology metrics (length & orientation distribution) ----
    wall_topology = _wall_topology_metrics(walls)

    stats = {
        "schema": model.schema,
        "storey_count": len(storeys),
        "storey_names": [getattr(s, "Name", None) or "" for s in storeys],
        "storey_elevations_mm": elevations,
        "storey_height_mm": storey_height,
        "footprint_x_mm": footprint_x,
        "footprint_y_mm": footprint_y,
        "wall_count": len(walls),
        "door_count": len(doors),
        "window_count": len(windows),
        "column_count": len(columns),
        "slab_count": len(floor_slabs),
        "beam_count": len(beams),
        "railing_count": len(railings),
        "covering_count": len(coverings),
        "roof_count": total_roof_like,
        "dominant_material": dominant_material,
        "dominant_material_raw": dominant_material_raw,
        **wall_topology,
    }

    description = _compose_sentence(stats, max_chars=max_chars)
    stats["description"] = description
    return stats


def _compose_sentence(stats: dict, *, max_chars: int) -> str:
    storey_count = max(1, stats["storey_count"])
    storey_height = stats["storey_height_mm"] or 3000.0
    fx, fy = stats["footprint_x_mm"], stats["footprint_y_mm"]
    fx_m = fx / 1000.0 if fx else 0.0
    fy_m = fy / 1000.0 if fy else 0.0

    parts = []
    parts.append(
        f"A {storey_count}-storey building with a roughly rectangular "
        f"footprint of about {fx_m:.0f}m × {fy_m:.0f}m"
    )
    parts.append(
        f"and a floor-to-floor height of {storey_height / 1000.0:.2f}m"
    )

    elem_bits = []
    if stats["wall_count"]:
        elem_bits.append(f"{stats['wall_count']} walls")
    if stats["door_count"]:
        elem_bits.append(f"{stats['door_count']} doors")
    if stats["window_count"]:
        elem_bits.append(f"{stats['window_count']} windows")
    if stats["column_count"]:
        elem_bits.append(f"{stats['column_count']} columns")
    if stats["slab_count"]:
        elem_bits.append(f"{stats['slab_count']} floor slab(s)")
    if stats["railing_count"]:
        elem_bits.append(f"{stats['railing_count']} railings")
    if stats["roof_count"]:
        elem_bits.append("a roof")
    elif stats["covering_count"]:
        elem_bits.append("a ceiling covering")

    if elem_bits:
        parts.append("containing " + ", ".join(elem_bits))

    if stats["dominant_material"]:
        parts.append(f"built primarily with {stats['dominant_material']}")

    text = ", ".join(parts) + "."
    return text[:max_chars]
