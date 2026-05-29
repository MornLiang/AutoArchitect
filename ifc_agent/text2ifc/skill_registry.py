"""Persistent JSON skill registry for Text2IFC design-review fixes."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from ifc_agent.text2ifc.design_reviewer import DesignIssue


@dataclass
class SkillTrigger:
    building_type: str = ""
    min_storeys: int = 0
    max_storeys: int = 999
    footprint_shape: str = ""
    structural_system: str = ""


@dataclass
class SkillFix:
    path: str
    value: Any


@dataclass
class Skill:
    id: str
    name: str
    trigger: SkillTrigger = field(default_factory=SkillTrigger)
    fixes: list[SkillFix] = field(default_factory=list)
    source_issue: str = ""
    hit_count: int = 0
    verified: bool = False


class SkillRegistry:
    def __init__(self, path: str | None = None):
        self.path = path
        self.skills: list[Skill] = []
        if path and os.path.isfile(path):
            self.load(path)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.skills = [_skill_from_dict(x) for x in data.get("skills", [])]

    def save(self, path: str | None = None) -> None:
        out_path = path or self.path
        if not out_path:
            return
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"skills": [asdict(s) for s in self.skills]}, f,
                      indent=2, ensure_ascii=False)

    def match(self, requirements: dict) -> list[Skill]:
        building_type = str(requirements.get("building_type", "")).lower()
        storeys = int(requirements.get("storey_count", 1) or 1)
        footprint = (requirements.get("footprint") or {}).get("shape", "")
        ss = requirements.get("structural_system", "")
        structural = ss.get("kind", "") if isinstance(ss, dict) else str(ss)
        matched = []
        for skill in self.skills:
            trig = skill.trigger
            if trig.building_type and trig.building_type.lower() not in building_type:
                continue
            if not (trig.min_storeys <= storeys <= trig.max_storeys):
                continue
            if trig.footprint_shape and trig.footprint_shape != footprint:
                continue
            if trig.structural_system and trig.structural_system != structural:
                continue
            skill.hit_count += 1
            matched.append(skill)
        return matched

    def pre_apply(self, requirements: dict) -> dict:
        matched = self.match(requirements)
        fixes = [fix for skill in matched for fix in skill.fixes]
        return self.apply_fixes(requirements, fixes)

    def mint_from_issue(self, issue: DesignIssue, requirements: dict) -> Skill | None:
        fixes = _fixes_for_issue(issue, requirements)
        if not fixes:
            return None
        sid = f"{issue.rule_id.lower()}_{len(self.skills) + 1}"
        skill = Skill(
            id=sid,
            name=f"Auto fix for {issue.rule_id}",
            trigger=SkillTrigger(
                building_type=str(requirements.get("building_type", "")),
                min_storeys=max(0, int(requirements.get("storey_count", 1) or 1) - 1),
                max_storeys=999,
                footprint_shape=str((requirements.get("footprint") or {}).get("shape", "")),
                structural_system=_structural_kind(requirements),
            ),
            fixes=fixes,
            source_issue=issue.rule_id,
            hit_count=0,
            verified=False,
        )
        self.skills.append(skill)
        return skill

    @staticmethod
    def apply_fixes(requirements: dict, fixes: list[SkillFix]) -> dict:
        out = copy.deepcopy(requirements)
        for fix in fixes:
            _set_path(out, fix.path.split("."), fix.value)
        return out


def _fixes_for_issue(issue: DesignIssue, requirements: dict) -> list[SkillFix]:
    rule = issue.rule_id
    area = (requirements.get("footprint") or {}).get("x_mm", 10000) * (
        requirements.get("footprint") or {}).get("y_mm", 8000) / 1_000_000
    if rule == "COLUMNS_TOO_SPARSE":
        return [SkillFix("element_targets.columns", max(4, int(area / 100 * 3)))]
    if rule in {"STRUCT_SYSTEM_MISMATCH", "COLUMNS_INSUFFICIENT_HIGHRISE"}:
        return [SkillFix("structural_system.kind", "core_tube")]
    if rule == "NO_STAIRS":
        return [SkillFix("vertical_circulation", [{"kind": "stair", "count": 1}])]
    if rule == "INSUFFICIENT_STAIRS":
        return [SkillFix("vertical_circulation", [{"kind": "stair", "count": 2}])]
    if rule == "NO_ELEVATOR":
        return [SkillFix("vertical_circulation", [{"kind": "elevator", "count": 1}])]
    if rule == "INSUFFICIENT_ELEVATORS":
        return [SkillFix("vertical_circulation", [{"kind": "elevator", "count": 2}])]
    if rule == "WALLS_TOO_THIN":
        return [SkillFix("element_targets.wall_thickness_mm", 300)]
    return []


def _set_path(obj: dict, path: list[str], value: Any) -> None:
    cur = obj
    for part in path[:-1]:
        cur = cur.setdefault(part, {})
    cur[path[-1]] = value


def _structural_kind(requirements: dict) -> str:
    raw = requirements.get("structural_system", "")
    return raw.get("kind", "") if isinstance(raw, dict) else str(raw)


def _skill_from_dict(raw: dict) -> Skill:
    trigger = SkillTrigger(**(raw.get("trigger") or {}))
    fixes = [SkillFix(**x) for x in raw.get("fixes", [])]
    return Skill(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        trigger=trigger,
        fixes=fixes,
        source_issue=str(raw.get("source_issue", "")),
        hit_count=int(raw.get("hit_count", 0)),
        verified=bool(raw.get("verified", False)),
    )
