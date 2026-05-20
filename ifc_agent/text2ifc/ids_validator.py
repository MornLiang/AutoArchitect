"""
IDS-based validation of generated IFC files.

This is the in-pipeline counterpart of ``validate_with_ifctester.py`` —
it auto-builds an IDS Specification set from the current requirements
doc + SpatialGraph and runs IfcTester against the produced IFC.  Failed
specifications are returned as a structured list so the Refiner agent
can include them in its corrections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import ifcopenshell

try:
    from ifctester import ids as _ids
    from ifctester import reporter as _reporter
    _IFCTESTER_AVAILABLE = True
except Exception:  # pragma: no cover
    _IFCTESTER_AVAILABLE = False

from ifc_agent.text2ifc.schemas import SpatialGraph

logger = logging.getLogger(__name__)


@dataclass
class IDSFinding:
    """One failed (or notably degraded) IDS specification."""
    spec_name: str
    severity: str   # "error" | "warning"
    detail: str

    def as_bullet(self) -> str:
        return f"- [{self.severity.upper()}] {self.spec_name}: {self.detail}"


@dataclass
class IDSResult:
    available: bool = _IFCTESTER_AVAILABLE
    total_specs: int = 0
    passed_specs: int = 0
    failed_specs: int = 0
    findings: list[IDSFinding] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    deltas: dict = field(default_factory=dict)
    # raw spec_set object (in case caller wants to dump HTML / XML)
    spec_set: object = None

    def pass_rate(self) -> float:
        return (self.passed_specs / self.total_specs) if self.total_specs else 1.0

    def to_text(self) -> str:
        """Render findings as a plain-text bullet list for the Refiner prompt."""
        if not self.available:
            return "(IDS validation skipped: ifctester not installed)"
        if not self.findings:
            return f"All {self.total_specs} IDS specifications passed."
        lines = [f"{self.failed_specs}/{self.total_specs} IDS specifications failed:"]
        for f in self.findings:
            lines.append(f.as_bullet())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------------

def _entity_facet(classes: list[str]):
    if len(classes) == 1:
        return _ids.Entity(name=classes[0].upper())
    return _ids.Entity(
        name=_ids.Restriction(
            options={"enumeration": [c.upper() for c in classes]}
        ),
    )


def _add_spec(spec_set, *, name: str, applies_to: list[str],
              facets: list, min_occurs: int = 1, max_occurs="unbounded"):
    spec = _ids.Specification(
        name=name,
        minOccurs=min_occurs,
        maxOccurs=max_occurs,
        ifcVersion=["IFC4"],
    )
    spec.applicability.append(_entity_facet(applies_to))
    for f in facets:
        spec.requirements.append(f)
    spec_set.specifications.append(spec)
    return spec


def build_specs(*, requirements: dict | None,
                spatial: SpatialGraph | dict | None) -> object:
    """Build an ``ifctester.ids.Ids`` instance for a Text2IFC building."""
    if not _IFCTESTER_AVAILABLE:
        return None

    spec_set = _ids.Ids(
        title="Text2IFC validation",
        description="Auto-generated IDS for the Text2IFC pipeline.",
    )

    targets = (requirements or {}).get("element_targets", {}) or {}

    # 1. Structural classes that must exist
    needed: list[tuple[str, list[str]]] = [
        ("IfcProject",         ["IfcProject"]),
        ("IfcSite",            ["IfcSite"]),
        ("IfcBuilding",        ["IfcBuilding"]),
        ("IfcBuildingStorey",  ["IfcBuildingStorey"]),
    ]
    if targets.get("walls", 1) > 0:
        needed.append(("IfcWall (any variant)",
                       ["IfcWall", "IfcWallStandardCase"]))
    if targets.get("doors", 1) > 0:
        needed.append(("IfcDoor", ["IfcDoor"]))
    if targets.get("windows", 1) > 0:
        needed.append(("IfcWindow", ["IfcWindow"]))
    if targets.get("slabs", 1) > 0:
        needed.append(("IfcSlab", ["IfcSlab"]))
    if targets.get("roofs", 0) > 0:
        needed.append(("IfcRoof", ["IfcRoof"]))
    if targets.get("columns", 0) > 0:
        needed.append(("IfcColumn", ["IfcColumn"]))
    if targets.get("railings", 0) > 0:
        needed.append(("IfcRailing", ["IfcRailing"]))

    for label, classes in needed:
        _add_spec(
            spec_set,
            name=f"At least one {label} must exist",
            applies_to=classes,
            facets=[],
        )

    # 2. Every wall must have material + Name
    _add_spec(
        spec_set,
        name="Every wall must have a material",
        applies_to=["IfcWall", "IfcWallStandardCase"],
        facets=[_ids.Material(value=None)],
    )
    _add_spec(
        spec_set,
        name="Every wall must have a Name",
        applies_to=["IfcWall", "IfcWallStandardCase"],
        facets=[_ids.Attribute(name="Name", value=None)],
    )

    # 3. If pipeline produced rooms, every IfcSpace must carry a LongName
    has_rooms = False
    if isinstance(spatial, SpatialGraph):
        has_rooms = any(s.rooms for s in spatial.storeys)
    elif isinstance(spatial, dict):
        has_rooms = any(
            (s.get("rooms") or []) for s in (spatial.get("storeys") or [])
        )
    if has_rooms:
        _add_spec(
            spec_set,
            name="Every IfcSpace must have a LongName (room function)",
            applies_to=["IfcSpace"],
            facets=[_ids.Attribute(name="LongName", value=None)],
        )

    return spec_set


# ---------------------------------------------------------------------------
# Soft checks (counts vs targets) — IfcTester can't express these
# ---------------------------------------------------------------------------

def _soft_checks(model, requirements: dict | None) -> tuple[dict, dict, list[IDSFinding]]:
    """Compute counts/deltas + emit "warning" findings for sizable
    mismatches against the requirements doc."""
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
    deltas: dict = {}
    findings: list[IDSFinding] = []

    if requirements is None:
        return counts, deltas, findings

    targets = (requirements or {}).get("element_targets", {}) or {}
    storey_target = requirements.get("storey_count")
    if storey_target is not None:
        deltas["storey_count"] = (storey_target, counts["IfcBuildingStorey"])

    pairs = [
        ("walls",    "IfcWall"),
        ("doors",    "IfcDoor"),
        ("windows",  "IfcWindow"),
        ("slabs",    "IfcSlab"),
        ("roofs",    "IfcRoof"),
        ("columns",  "IfcColumn"),
        ("railings", "IfcRailing"),
    ]
    for tk, ck in pairs:
        if tk in targets:
            deltas[tk] = (int(targets[tk]), counts[ck])

    # Emit a warning per significant delta (>20% or >2 abs)
    for k, (tgt, actual) in deltas.items():
        diff = actual - tgt
        if diff == 0:
            continue
        if abs(diff) <= max(2, int(tgt * 0.2)):
            sev = "warning"
        else:
            sev = "error"
        findings.append(IDSFinding(
            spec_name=f"Target count: {k}",
            severity=sev,
            detail=(
                f"target={tgt}, generated={actual} "
                f"(delta={diff:+d})"
            ),
        ))

    return counts, deltas, findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate(ifc_path: str, *,
             requirements: dict | None = None,
             spatial: SpatialGraph | dict | None = None,
             html_report_path: Optional[str] = None,
             ids_xml_path: Optional[str] = None) -> IDSResult:
    """Run IDS validation on *ifc_path*.

    Returns an ``IDSResult`` with the list of failed specs (as findings)
    and soft count deltas vs the requirements doc.
    """
    if not _IFCTESTER_AVAILABLE:
        logger.warning("ifctester is not installed; IDS validation skipped.")
        return IDSResult(available=False)

    model = ifcopenshell.open(ifc_path)
    spec_set = build_specs(requirements=requirements, spatial=spatial)
    spec_set.validate(model)

    findings: list[IDSFinding] = []
    passed = 0
    for spec in spec_set.specifications:
        status = getattr(spec, "status", None)
        if status is True:
            passed += 1
        elif status is False:
            findings.append(IDSFinding(
                spec_name=spec.name,
                severity="error",
                detail=(
                    "no matching entities satisfy the requirements"
                    if not spec.requirements else
                    "at least one entity failed the requirement(s)"
                ),
            ))

    counts, deltas, soft_findings = _soft_checks(model, requirements)
    findings.extend(soft_findings)

    # Optional artefacts
    if html_report_path:
        try:
            html = _reporter.Html(spec_set)
            html.report()
            html.to_file(html_report_path)
        except Exception as e:
            logger.warning("HTML report failed: %s", e)
    if ids_xml_path:
        try:
            spec_set.to_xml(filepath=ids_xml_path)
        except Exception as e:
            logger.warning("IDS XML save failed: %s", e)

    return IDSResult(
        available=True,
        total_specs=len(spec_set.specifications),
        passed_specs=passed,
        failed_specs=len(spec_set.specifications) - passed,
        findings=findings,
        counts=counts,
        deltas=deltas,
        spec_set=spec_set,
    )
