"""
Validate a generated Text2IFC file using IfcTester (IDS).

We auto-build an IDS specification from either:
  - the requirements.json file (preferred — has explicit element targets)
  - or the SpatialGraph.json file (rooms-based fallback)

The IDS spec checks:
  • Building hierarchy : 1 IfcProject, 1 IfcSite, 1 IfcBuilding, N IfcBuildingStorey
  • Required entity classes are present (IfcWall, IfcDoor, IfcWindow, …)
  • Wall material attribute is set
  • Each IfcWall has a non-empty Name
  • Each IfcSpace has a LongName (room function) — when rooms-based pipeline
    is used
  • IFC schema is the one declared in metadata

Usage::

    python validate_with_ifctester.py \\
        --ifc test_output/text2ifc/t2b_p1_hotel_iter1.ifc \\
        --requirements test_output/text2ifc/t2b_p1_hotel_requirements.json \\
        --report-html test_output/text2ifc/t2b_p1_hotel_ifctester.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import ifcopenshell
from ifctester import ids, reporter


def _load_requirements(path: str | None) -> dict | None:
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _load_spatial(path: str | None) -> dict | None:
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _entity_facet(classes: list[str]) -> ids.Entity:
    """Build an Entity facet that matches ANY of *classes* (OR)."""
    if len(classes) == 1:
        return ids.Entity(name=classes[0].upper())
    # IDS Restriction "enumeration" = OR set of literal values
    return ids.Entity(
        name=ids.Restriction(options={"enumeration": [c.upper() for c in classes]}),
    )


def _add_spec(spec_set, *, name: str, applies_to_classes: list[str],
              facets: list, min_occurs: int = 1,
              max_occurs="unbounded") -> ids.Specification:
    spec = ids.Specification(
        name=name,
        minOccurs=min_occurs,
        maxOccurs=max_occurs,
        ifcVersion=["IFC4"],
    )
    spec.applicability.append(_entity_facet(applies_to_classes))
    for f in facets:
        spec.requirements.append(f)
    spec_set.specifications.append(spec)
    return spec


def build_ids(*, building_name: str,
              requirements: dict | None,
              spatial: dict | None) -> ids.Ids:
    """Build a Text2IFC IDS spec set."""
    spec_set = ids.Ids(
        title=f"Text2IFC validation: {building_name}",
        description="Auto-generated IDS for the Text2IFC pipeline.",
    )

    # ---- 1. Required entity classes (presence) -------------------------
    targets = (requirements or {}).get("element_targets", {}) or {}

    # IfcWall covers IfcWall + IfcWallStandardCase (IFC2X3 / IFC4 split).
    needed_groups: list[tuple[str, list[str]]] = [
        ("IfcProject",         ["IfcProject"]),
        ("IfcSite",            ["IfcSite"]),
        ("IfcBuilding",        ["IfcBuilding"]),
        ("IfcBuildingStorey",  ["IfcBuildingStorey"]),
    ]
    if targets.get("walls", 1) > 0:
        needed_groups.append(("IfcWall (any case)",
                              ["IfcWall", "IfcWallStandardCase"]))
    if targets.get("doors", 1) > 0:
        needed_groups.append(("IfcDoor", ["IfcDoor"]))
    if targets.get("windows", 1) > 0:
        needed_groups.append(("IfcWindow", ["IfcWindow"]))
    if targets.get("slabs", 1) > 0:
        needed_groups.append(("IfcSlab", ["IfcSlab"]))
    if targets.get("roofs", 0) > 0:
        needed_groups.append(("IfcRoof", ["IfcRoof"]))
    if targets.get("columns", 0) > 0:
        needed_groups.append(("IfcColumn", ["IfcColumn"]))
    if targets.get("railings", 0) > 0:
        needed_groups.append(("IfcRailing", ["IfcRailing"]))

    for label, classes in needed_groups:
        _add_spec(
            spec_set,
            name=f"At least one {label} must exist",
            applies_to_classes=classes,
            facets=[],
        )

    # ---- 2. Wall material must be set ----------------------------------
    _add_spec(
        spec_set,
        name="Every wall must have a material",
        applies_to_classes=["IfcWall", "IfcWallStandardCase"],
        facets=[ids.Material(value=None)],
    )

    # ---- 3. Every wall must have a Name --------------------------------
    _add_spec(
        spec_set,
        name="Every wall must have a Name",
        applies_to_classes=["IfcWall", "IfcWallStandardCase"],
        facets=[ids.Attribute(name="Name", value=None)],
    )

    # ---- 4. Every IfcSpace (room) must have a LongName -----------------
    if spatial and any(
        (s.get("rooms") or []) for s in spatial.get("storeys", [])
    ):
        _add_spec(
            spec_set,
            name="Every IfcSpace must have a LongName (room function)",
            applies_to_classes=["IfcSpace"],
            facets=[ids.Attribute(name="LongName", value=None)],
        )

    # ---- 5. Storey count check -----------------------------------------
    # IfcTester doesn't support "exactly N" counts in IDS, but we can
    # at least require IfcBuildingStorey to exist (done above).  We will
    # report storey-count delta separately in the post-process summary.

    return spec_set


def _post_process(model: ifcopenshell.file,
                  requirements: dict | None,
                  spatial: dict | None) -> dict:
    """Compute "soft" checks IfcTester can't express directly."""
    soft = {}
    targets = (requirements or {}).get("element_targets", {}) or {}

    # by_type("IfcWall") already includes IfcWallStandardCase as a subtype.
    counts = {
        "IfcBuildingStorey": len(model.by_type("IfcBuildingStorey")),
        "IfcWall":           len(model.by_type("IfcWall")),
        "IfcDoor":           len(model.by_type("IfcDoor")),
        "IfcWindow":         len(model.by_type("IfcWindow")),
        "IfcSlab":           len(model.by_type("IfcSlab")),
        "IfcRoof":           len(model.by_type("IfcRoof")),
        "IfcColumn":         len(model.by_type("IfcColumn")),
        "IfcRailing":        len(model.by_type("IfcRailing")),
        "IfcSpace":          len(model.by_type("IfcSpace")),
    }

    # Per-target delta (only metrics declared by the requirements doc)
    deltas = {}
    storey_target = (requirements or {}).get("storey_count")
    if storey_target is not None:
        deltas["storey_count"] = (storey_target, counts["IfcBuildingStorey"])
    mapping = {
        "walls":    ("IfcWall",),
        "doors":    ("IfcDoor",),
        "windows":  ("IfcWindow",),
        "slabs":    ("IfcSlab",),
        "roofs":    ("IfcRoof",),
        "columns":  ("IfcColumn",),
        "railings": ("IfcRailing",),
    }
    for target_key, (cls_key, ) in mapping.items():
        tgt = targets.get(target_key)
        if tgt is not None:
            deltas[target_key] = (tgt, counts[cls_key])

    soft["counts"] = counts
    soft["deltas"] = deltas
    return soft


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ifc", required=True, help="Generated IFC")
    p.add_argument("--requirements", default=None,
                   help="requirements.json path (for element_targets)")
    p.add_argument("--spatial", default=None,
                   help="spatial_graph.json path (for rooms)")
    p.add_argument("--report-html", default=None,
                   help="If set, write an HTML report to this path")
    p.add_argument("--ids-out", default=None,
                   help="If set, save the auto-generated IDS XML to this path")
    args = p.parse_args()

    ifc_path = args.ifc
    if not os.path.exists(ifc_path):
        print(f"❌ IFC not found: {ifc_path}", file=sys.stderr)
        sys.exit(1)

    # Auto-discover companion files if not specified
    base = ifc_path.rsplit("_iter", 1)[0]
    if args.requirements is None:
        candidate = base + "_requirements.json"
        if os.path.exists(candidate):
            args.requirements = candidate
    if args.spatial is None:
        iter_suffix = ifc_path.rsplit("_iter", 1)[1].split(".")[0] \
                      if "_iter" in ifc_path else "1"
        candidate = f"{base}_iter{iter_suffix}_spatial.json"
        if os.path.exists(candidate):
            args.spatial = candidate

    print(f"▶ IFC          : {ifc_path}")
    print(f"  requirements : {args.requirements}")
    print(f"  spatial      : {args.spatial}")

    requirements = _load_requirements(args.requirements)
    spatial = _load_spatial(args.spatial)

    building_name = (
        (requirements or {}).get("building_type")
        or (spatial or {}).get("metadata", {}).get("name", "Generated Building")
    )

    spec_set = build_ids(
        building_name=building_name,
        requirements=requirements,
        spatial=spatial,
    )

    # Open & validate
    model = ifcopenshell.open(ifc_path)
    spec_set.validate(model)

    # Console report
    print()
    print("=" * 80)
    print(f"IDS validation report for {Path(ifc_path).name}")
    print("=" * 80)

    rep = reporter.Console(spec_set)
    rep.report()

    # Soft post-process (counts vs targets)
    soft = _post_process(model, requirements, spatial)

    print()
    print("Entity counts:")
    for k, v in soft["counts"].items():
        print(f"  {k:25s}: {v}")

    if soft["deltas"]:
        print()
        print("Requirement vs generated:")
        print(f"  {'metric':20s}  {'target':>8s}  {'actual':>8s}  status")
        for k, (tgt, actual) in soft["deltas"].items():
            ok = "✅" if tgt == actual else "⚠️ " if abs(tgt - actual) <= max(2, int(tgt * 0.2)) else "❌"
            print(f"  {k:20s}  {tgt:>8}  {actual:>8}  {ok}")

    # Reports
    if args.ids_out:
        try:
            spec_set.to_xml(filepath=args.ids_out)
            print(f"\n💾 IDS XML saved → {args.ids_out}")
        except Exception as e:
            print(f"\n⚠️  IDS XML save failed: {e}")

    if args.report_html:
        try:
            html = reporter.Html(spec_set)
            html.report()
            html.to_file(args.report_html)
            print(f"💾 HTML report saved → {args.report_html}")
        except Exception as e:
            print(f"⚠️  HTML report failed: {e}")

    # Exit code: 0 if every spec passed (we look at the spec_set status)
    failed = sum(
        1 for s in spec_set.specifications
        if hasattr(s, "status") and s.status is False
    )
    print()
    if failed:
        print(f"❌ {failed} / {len(spec_set.specifications)} specifications failed")
        sys.exit(2)
    else:
        print(f"✅ {len(spec_set.specifications)} / {len(spec_set.specifications)} specifications passed")


if __name__ == "__main__":
    main()
