"""Programmatic design review for Text2IFC SpatialGraph/BuildingGraph pairs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ifc_agent.text2ifc.schemas import BuildingGraph, SpatialGraph


@dataclass
class DesignIssue:
    category: str
    severity: str
    rule_id: str
    message: str
    affected_storey: str = ""
    affected_room: str = ""
    metric_value: Any = None
    metric_target: Any = None
    fix_hint: str = ""


@dataclass
class DesignReviewReport:
    passed_checks: int = 0
    failed_checks: int = 0
    warning_checks: int = 0
    issues: list[DesignIssue] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.failed_checks == 0 and self.warning_checks == 0

    def add(self, issue: DesignIssue) -> None:
        self.issues.append(issue)
        if issue.severity == "error":
            self.failed_checks += 1
        else:
            self.warning_checks += 1

    def pass_(self) -> None:
        self.passed_checks += 1

    def to_dict(self) -> dict:
        return {
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "warning_checks": self.warning_checks,
            "issues": [asdict(i) for i in self.issues],
        }

    def to_text(self) -> str:
        if not self.issues:
            return "Design review passed."
        lines = []
        for i in self.issues:
            loc = " ".join(x for x in [i.affected_storey, i.affected_room] if x)
            lines.append(
                f"[{i.severity.upper()}] {i.rule_id} {loc}: "
                f"{i.message}. Hint: {i.fix_hint}"
            )
        return "\n".join(lines)


MIN_ROOM_AREA_M2 = {
    "corridor": 6.0,
    "bathroom": 3.0,
    "toilet": 2.0,
    "office": 8.0,
    "meeting": 12.0,
    "classroom": 35.0,
    "lobby": 10.0,
    "stairwell": 6.0,
}


def review(spatial: SpatialGraph, graph: BuildingGraph) -> DesignReviewReport:
    report = DesignReviewReport()
    _review_geometry(spatial, report)
    _review_structure(spatial, graph, report)
    _review_code(spatial, graph, report)
    _review_function(spatial, report)
    return report


def _review_geometry(spatial: SpatialGraph, report: DesignReviewReport) -> None:
    footprint_area_m2 = spatial.footprint.x_mm * spatial.footprint.y_mm / 1_000_000
    for storey in spatial.storeys:
        if storey.is_inhabited and storey.height_mm < 2400:
            report.add(DesignIssue(
                "geometry", "error", "STOREY_TOO_LOW",
                f"Inhabited storey height is {storey.height_mm:.0f}mm",
                affected_storey=storey.id,
                metric_value=storey.height_mm,
                metric_target=2600,
                fix_hint="Set height_mm >= 2600",
            ))
        else:
            report.pass_()
        for room in storey.rooms:
            area = max(0.0, room.area_ratio) * footprint_area_m2
            target = MIN_ROOM_AREA_M2.get(room.function, 6.0)
            if area < target * 0.5:
                report.add(DesignIssue(
                    "geometry", "error", "ROOM_TOO_SMALL",
                    f"{room.function} area is {area:.1f}m2",
                    affected_storey=storey.id,
                    affected_room=room.id,
                    metric_value=round(area, 2),
                    metric_target=target,
                    fix_hint="Increase area_ratio or merge this room",
                ))
            elif area < target:
                report.add(DesignIssue(
                    "geometry", "warning", "ROOM_SMALL",
                    f"{room.function} area is {area:.1f}m2",
                    affected_storey=storey.id,
                    affected_room=room.id,
                    metric_value=round(area, 2),
                    metric_target=target,
                    fix_hint="Consider increasing area_ratio",
                ))
            else:
                report.pass_()
            if room.function == "corridor" and room.area_ratio < 0.08:
                report.add(DesignIssue(
                    "geometry", "warning", "CORRIDOR_TOO_NARROW",
                    "Corridor area ratio suggests width below 1.5m",
                    affected_storey=storey.id,
                    affected_room=room.id,
                    metric_value=room.area_ratio,
                    metric_target=0.12,
                    fix_hint="Set corridor.area_ratio >= 0.12",
                ))


def _review_structure(
    spatial: SpatialGraph,
    graph: BuildingGraph,
    report: DesignReviewReport,
) -> None:
    storey_count = len(spatial.storeys)
    kind = spatial.structural_system.kind
    total_columns = sum(len(s.columns) for s in graph.storeys)
    area_100 = max(1.0, spatial.footprint.x_mm * spatial.footprint.y_mm / 100_000_000)
    if kind in {"frame", "mixed"} and total_columns / area_100 < 3:
        report.add(DesignIssue(
            "structure", "warning", "COLUMNS_TOO_SPARSE",
            "Frame/mixed structural system has sparse columns",
            metric_value=round(total_columns / area_100, 2),
            metric_target=3,
            fix_hint="Increase element_targets.columns or reduce grid spacing",
        ))
    else:
        report.pass_()
    if storey_count >= 10 and total_columns < 20:
        report.add(DesignIssue(
            "structure", "error", "COLUMNS_INSUFFICIENT_HIGHRISE",
            f"High-rise has only {total_columns} columns",
            metric_value=total_columns,
            metric_target=20,
            fix_hint="Use core_tube/mixed and add columns",
        ))
    if storey_count >= 10 and kind == "frame":
        report.add(DesignIssue(
            "structure", "warning", "STRUCT_SYSTEM_MISMATCH",
            "High-rise uses a plain frame system",
            metric_value=kind,
            metric_target="core_tube",
            fix_hint="Set structural_system.kind to core_tube or mixed",
        ))
    min_wall = min(
        (s.elements.wall_thickness_mm for s in spatial.storeys if s.is_inhabited),
        default=200,
    )
    if storey_count >= 10 and min_wall < 250:
        report.add(DesignIssue(
            "structure", "warning", "WALLS_TOO_THIN",
            "High-rise exterior walls are thinner than 250mm",
            metric_value=min_wall,
            metric_target=300,
            fix_hint="Set element wall_thickness_mm to 300",
        ))


def _review_code(
    spatial: SpatialGraph,
    graph: BuildingGraph,
    report: DesignReviewReport,
) -> None:
    storey_count = len(spatial.storeys)
    stairs = sum(1 for s in spatial.shafts if s.kind == "stair")
    elevators = sum(1 for s in spatial.shafts if s.kind == "elevator")
    if storey_count >= 2 and stairs == 0:
        report.add(DesignIssue("code", "error", "NO_STAIRS",
                               "Multi-storey building has no stair shaft",
                               metric_value=0, metric_target=1,
                               fix_hint="Add a stair shaft"))
    elif storey_count >= 3 and stairs < 2:
        report.add(DesignIssue("code", "warning", "INSUFFICIENT_STAIRS",
                               f"Building with {storey_count} storeys has {stairs} stair shaft",
                               metric_value=stairs, metric_target=2,
                               fix_hint="Add a second stair shaft for fire egress"))
    else:
        report.pass_()
    if storey_count >= 4 and elevators == 0:
        report.add(DesignIssue("code", "warning", "NO_ELEVATOR",
                               "Building has no elevator shaft",
                               metric_value=0, metric_target=1,
                               fix_hint="Add an elevator shaft"))
    elif storey_count >= 10 and elevators < 2:
        report.add(DesignIssue("code", "warning", "INSUFFICIENT_ELEVATORS",
                               "High-rise has fewer than two elevators",
                               metric_value=elevators, metric_target=2,
                               fix_hint="Add zoned elevators"))
    else:
        report.pass_()
    if storey_count >= 15 and not any("refuge" in s.name.lower() for s in spatial.storeys):
        report.add(DesignIssue("code", "warning", "NO_REFUGE_FLOOR",
                               "High-rise has no named refuge floor",
                               metric_value=0, metric_target=1,
                               fix_hint="Insert or name a refuge floor every 15 storeys"))
    for storey in spatial.storeys:
        for room in storey.rooms:
            if room.has_external_facade and room.n_windows <= 0:
                report.add(DesignIssue(
                    "code", "warning", "EXTERIOR_ROOM_NO_WINDOW",
                    "Exterior room has no windows",
                    affected_storey=storey.id,
                    affected_room=room.id,
                    metric_value=0,
                    metric_target=1,
                    fix_hint="Set n_windows >= 1",
                ))


def _review_function(spatial: SpatialGraph, report: DesignReviewReport) -> None:
    for storey in spatial.storeys:
        ids = [r.id for r in storey.rooms]
        if len(ids) != len(set(ids)):
            report.add(DesignIssue("function", "error", "DUPLICATE_ROOM_ID",
                                   "Storey contains duplicate room ids",
                                   affected_storey=storey.id,
                                   fix_hint="Rename duplicate rooms"))
        by_id = {r.id: r for r in storey.rooms}
        for room in storey.rooms:
            bad = [x for x in room.opening_to if x not in room.adjacent_to]
            if bad:
                report.add(DesignIssue(
                    "function", "error", "OPENING_NOT_ADJACENT",
                    f"opening_to contains non-adjacent rooms: {bad}",
                    affected_storey=storey.id,
                    affected_room=room.id,
                    fix_hint="Ensure opening_to is a subset of adjacent_to",
                ))
            if room.function not in {"corridor", "lobby", "open_space"}:
                reachable = any(
                    by_id.get(nb) and by_id[nb].function in {"corridor", "lobby", "open_space"}
                    for nb in room.adjacent_to
                )
                if storey.rooms and not reachable:
                    report.add(DesignIssue(
                        "function", "warning", "ROOM_UNREACHABLE",
                        "Room is not connected to the corridor/lobby network",
                        affected_storey=storey.id,
                        affected_room=room.id,
                        fix_hint="Add adjacency/opening_to to a corridor or lobby",
                    ))
