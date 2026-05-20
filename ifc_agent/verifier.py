"""
Verification module combining rule-based checks (Text2BIM's Solibri approach)
and MLLM semantic verification (GenArtist's correction loop).

Rule-based checks use ifcopenshell directly, avoiding external tool dependencies.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Optional

from ifc_agent.ifc_parser import IFCParser

logger = logging.getLogger(__name__)


class RuleBasedVerifier:
    """
    Programmatic verification of IFC model integrity.

    Inspired by Text2BIM's Solibri checking but implemented directly
    with ifcopenshell, making it portable and dependency-free.
    """

    def __init__(self, parser: IFCParser):
        self.parser = parser

    def run_all_checks(self) -> dict:
        """Run all rule-based checks and return aggregated results."""
        results = {
            "checks_run": 0,
            "passed": 0,
            "failed": 0,
            "warnings": 0,
            "issues": [],
        }

        checks = [
            self.check_project_structure,
            self.check_spatial_containment,
            self.check_storey_elevations,
            self.check_wall_integrity,
            self.check_door_window_hosting,
            self.check_material_assignments,
            self.check_property_completeness,
            self.check_duplicate_guids,
        ]

        for check_fn in checks:
            try:
                check_result = check_fn()
                results["checks_run"] += 1
                if check_result["status"] == "pass":
                    results["passed"] += 1
                elif check_result["status"] == "fail":
                    results["failed"] += 1
                    results["issues"].extend(check_result.get("issues", []))
                elif check_result["status"] == "warning":
                    results["warnings"] += 1
                    results["issues"].extend(check_result.get("issues", []))
            except Exception as e:
                logger.warning(f"Check {check_fn.__name__} failed with error: {e}")
                results["issues"].append({
                    "check": check_fn.__name__,
                    "severity": "error",
                    "message": f"Check failed: {e}",
                })

        results["valid"] = results["failed"] == 0
        return results

    def check_project_structure(self) -> dict:
        """Verify basic project hierarchy exists."""
        model = self.parser.model
        issues = []

        if not model.by_type("IfcProject"):
            issues.append({"severity": "error", "message": "No IfcProject found."})
        if not model.by_type("IfcSite"):
            issues.append({"severity": "warning", "message": "No IfcSite found."})
        if not model.by_type("IfcBuilding"):
            issues.append({"severity": "error", "message": "No IfcBuilding found."})
        if not model.by_type("IfcBuildingStorey"):
            issues.append({"severity": "error", "message": "No IfcBuildingStorey found."})

        return {
            "check": "project_structure",
            "status": "fail" if any(i["severity"] == "error" for i in issues) else (
                "warning" if issues else "pass"
            ),
            "issues": issues,
        }

    def check_spatial_containment(self) -> dict:
        """Check that building products are spatially contained."""
        model = self.parser.model
        issues = []

        element_types = [
            "IfcWall", "IfcWallStandardCase", "IfcDoor", "IfcWindow",
            "IfcSlab", "IfcColumn", "IfcBeam", "IfcRoof",
        ]

        orphan_count = 0
        for et in element_types:
            for el in model.by_type(et):
                contained = getattr(el, "ContainedInStructure", [])
                decomposed = getattr(el, "Decomposes", [])
                if not contained and not decomposed:
                    orphan_count += 1

        if orphan_count > 0:
            issues.append({
                "severity": "warning",
                "message": f"{orphan_count} building element(s) lack spatial containment.",
            })

        return {
            "check": "spatial_containment",
            "status": "warning" if issues else "pass",
            "issues": issues,
        }

    def check_storey_elevations(self) -> dict:
        """Verify storey elevations are monotonically increasing."""
        storeys = self.parser.model.by_type("IfcBuildingStorey")
        issues = []

        if len(storeys) <= 1:
            return {"check": "storey_elevations", "status": "pass", "issues": []}

        elevations = []
        for s in storeys:
            elev = getattr(s, "Elevation", None)
            if elev is not None:
                elevations.append((s.Name or "Unnamed", float(elev)))

        elevations.sort(key=lambda x: x[1])

        for i in range(1, len(elevations)):
            if elevations[i][1] <= elevations[i - 1][1]:
                issues.append({
                    "severity": "warning",
                    "message": (
                        f"Storey '{elevations[i][0]}' (elev={elevations[i][1]}) "
                        f"is not above '{elevations[i - 1][0]}' (elev={elevations[i - 1][1]})."
                    ),
                })

        return {
            "check": "storey_elevations",
            "status": "warning" if issues else "pass",
            "issues": issues,
        }

    def check_wall_integrity(self) -> dict:
        """Check walls have valid geometry representations."""
        issues = []
        walls = self.parser.model.by_type("IfcWall")

        for wall in walls:
            rep = getattr(wall, "Representation", None)
            if rep is None:
                issues.append({
                    "severity": "warning",
                    "message": f"Wall '{wall.Name or wall.GlobalId}' has no geometric representation.",
                })

        return {
            "check": "wall_integrity",
            "status": "warning" if issues else "pass",
            "issues": issues,
        }

    def check_door_window_hosting(self) -> dict:
        """Check that doors/windows are hosted in openings or walls."""
        issues = []
        model = self.parser.model

        for ifc_type in ("IfcDoor", "IfcWindow"):
            for el in model.by_type(ifc_type):
                fills = getattr(el, "FillsVoids", [])
                contained = getattr(el, "ContainedInStructure", [])
                if not fills and not contained:
                    issues.append({
                        "severity": "warning",
                        "message": (
                            f"{ifc_type} '{el.Name or el.GlobalId}' is not "
                            f"hosted in any opening or spatially contained."
                        ),
                    })

        return {
            "check": "door_window_hosting",
            "status": "warning" if issues else "pass",
            "issues": issues,
        }

    def check_material_assignments(self) -> dict:
        """Check that key structural elements have material assignments."""
        issues = []
        model = self.parser.model

        for ifc_type in ("IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcColumn"):
            for el in model.by_type(ifc_type):
                has_material = False
                for rel in getattr(el, "HasAssociations", []):
                    if rel.is_a("IfcRelAssociatesMaterial"):
                        has_material = True
                        break
                if not has_material:
                    issues.append({
                        "severity": "info",
                        "message": f"{ifc_type} '{el.Name or el.GlobalId}' has no material assignment.",
                    })

        return {
            "check": "material_assignments",
            "status": "warning" if len(issues) > 5 else "pass",
            "issues": issues[:10],  # cap to avoid flooding
        }

    def check_property_completeness(self) -> dict:
        """Spot-check that common property sets exist on key elements."""
        issues = []
        model = self.parser.model

        pset_expectations = {
            "IfcWall": "Pset_WallCommon",
            "IfcDoor": "Pset_DoorCommon",
            "IfcWindow": "Pset_WindowCommon",
            "IfcSlab": "Pset_SlabCommon",
        }

        for ifc_type, expected_pset in pset_expectations.items():
            elements = model.by_type(ifc_type)
            if not elements:
                continue
            sample = elements[0]
            props = self.parser.get_element_properties(sample.GlobalId)
            if expected_pset not in props:
                issues.append({
                    "severity": "info",
                    "message": f"{ifc_type} elements are missing {expected_pset}.",
                })

        return {
            "check": "property_completeness",
            "status": "pass",
            "issues": issues,
        }

    def check_duplicate_guids(self) -> dict:
        """Check for duplicate GlobalIds (should never happen in valid IFC)."""
        seen: dict[str, int] = defaultdict(int)
        for entity in self.parser.model:
            guid = getattr(entity, "GlobalId", None)
            if guid:
                seen[guid] += 1

        duplicates = {g: c for g, c in seen.items() if c > 1}
        issues = []
        if duplicates:
            issues.append({
                "severity": "error",
                "message": f"{len(duplicates)} duplicate GlobalId(s) found.",
                "guids": list(duplicates.keys())[:5],
            })

        return {
            "check": "duplicate_guids",
            "status": "fail" if issues else "pass",
            "issues": issues,
        }


def format_verification_report(results: dict) -> str:
    """Format verification results into a human-readable report."""
    lines = [
        f"IFC Model Verification Report",
        f"{'=' * 40}",
        f"Checks run: {results['checks_run']}",
        f"Passed: {results['passed']}",
        f"Failed: {results['failed']}",
        f"Warnings: {results['warnings']}",
        f"Overall: {'VALID' if results['valid'] else 'INVALID'}",
    ]

    if results["issues"]:
        lines.append(f"\nIssues ({len(results['issues'])}):")
        for issue in results["issues"]:
            severity = issue.get("severity", "info").upper()
            msg = issue.get("message", "")
            lines.append(f"  [{severity}] {msg}")

    return "\n".join(lines)
