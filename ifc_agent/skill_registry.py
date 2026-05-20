"""
Skill Registry: auto-discover and catalog all ifcopenshell APIs.

Provides a two-level hierarchy for LLM consumption:
  Level 1 — Module summaries  (35 modules, shown to LLM in module-selection stage)
  Level 2 — Function details  (per module, shown in detailed-planning stage)

Also wraps ifcopenshell.util query utilities so the planner has a unified
catalog for both reading and writing operations.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Optional

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.element
import ifcopenshell.util.selector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hand-written module-level descriptions (concise, LLM-friendly)
# ---------------------------------------------------------------------------

MODULE_DESCRIPTIONS: dict[str, str] = {
    "root": "Core entity lifecycle: create, copy, reclassify, and remove any IFC product.",
    "attribute": "Edit direct IFC attributes (Name, Description, ObjectType, etc.) of any entity.",
    "spatial": "Manage spatial containment — assign/unassign elements to storeys, spaces, buildings.",
    "aggregate": "Manage whole-part decomposition (building→storeys, storey→spaces, etc.).",
    "geometry": "Create/edit geometric representations: walls, doors, windows, slabs, railings, booleans, placements.",
    "material": "Full material management: layers, constituents, profiles, assignments, editing.",
    "pset": "Create and edit property sets (Pset_*) and quantity sets (Qto_*).",
    "feature": "Manage openings & fillings (e.g. a door filling a wall opening).",
    "type": "Assign type/occurrence relationships (e.g. IfcWallType → IfcWall).",
    "classification": "Attach classification references (UniClass, OmniClass, etc.) to elements.",
    "group": "Create and manage element groups.",
    "style": "Manage visual surface styles and material appearances.",
    "system": "Manage MEP distribution systems, ports, and connections.",
    "unit": "Define and assign project measurement units (SI, imperial, monetary).",
    "owner": "Track authorship: persons, organisations, applications, roles.",
    "project": "Project-level: create IFC files, append library assets, declarations.",
    "context": "Manage geometric representation contexts (Body, Axis, FootPrint, etc.).",
    "profile": "Define cross-section profiles (I-beam, rectangle, arbitrary, etc.).",
    "layer": "Manage CAD presentation layers.",
    "document": "Link external documents and references to elements.",
    "constraint": "Define and assign design constraints and objectives.",
    "cost": "Cost schedules, cost items, and values — 5D BIM.",
    "sequence": "Work schedules, tasks, calendars, and timelines — 4D BIM.",
    "structural": "Structural analysis models, loads, boundary conditions, and members.",
    "georeference": "Georeferencing, map conversion, and true north settings.",
    "grid": "Create and manage structural grid axes.",
    "boundary": "Space boundary geometry connections.",
    "nest": "Nesting relationships between elements.",
    "control": "Control relationships (controls → controlled objects).",
    "library": "BIM library and asset reference management.",
    "pset_template": "Define reusable property set templates.",
    "resource": "Construction resource management (labour, material, equipment).",
    "drawing": "Drawing-level annotations and text literals.",
    "alignment": "Road/rail alignment geometry (IFC4x3).",
    "cogo": "Coordinate geometry — survey points.",
}

# Modules that are most relevant for typical BIM QA/editing tasks.
CORE_MODULES = [
    "root", "attribute", "spatial", "aggregate", "geometry",
    "material", "pset", "feature", "type", "classification",
    "group", "style", "system", "project", "context", "unit",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillParam:
    name: str
    annotation: str = "Any"
    default: str | None = None
    is_entity: bool = False

    def to_signature_str(self) -> str:
        s = self.name
        if self.annotation and self.annotation != "Any":
            s += f": {self.annotation}"
        if self.default is not None:
            s += f" = {self.default}"
        return s


@dataclass
class Skill:
    """A single callable ifcopenshell API function or utility."""
    path: str
    module: str
    name: str
    category: str
    doc_short: str
    doc_full: str
    params: list[SkillParam] = field(default_factory=list)
    _callable: Any = field(default=None, repr=False)

    def one_liner(self) -> str:
        sig = ", ".join(p.to_signature_str() for p in self.params)
        return f"{self.path}({sig}): {self.doc_short}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Auto-discovers all ifcopenshell API & util functions and provides
    module/function-level catalogs for the LLM planner.
    """

    def __init__(self, model: ifcopenshell.file):
        self.model = model
        self.skills: dict[str, Skill] = {}
        self._discover_api_skills()
        self._register_query_skills()
        self._register_composite_skills()
        logger.info(
            "SkillRegistry initialised: %d skills across %d modules",
            len(self.skills),
            len({s.module for s in self.skills.values()}),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_api_skills(self):
        """Walk ifcopenshell.api.* and register every public function."""
        api_path = os.path.dirname(ifcopenshell.api.__file__)
        for _, pkg_name, is_pkg in pkgutil.iter_modules([api_path]):
            if not is_pkg or pkg_name.startswith("_"):
                continue
            pkg_dir = os.path.join(api_path, pkg_name)
            for _, mod_name, _ in pkgutil.iter_modules([pkg_dir]):
                if mod_name.startswith("_"):
                    continue
                skill_path = f"{pkg_name}.{mod_name}"
                try:
                    mod = importlib.import_module(
                        f"ifcopenshell.api.{pkg_name}.{mod_name}"
                    )
                    func = getattr(mod, mod_name, None)
                    if func is None or not callable(func):
                        continue
                    self.skills[skill_path] = self._make_skill(
                        func, skill_path, pkg_name, mod_name, category="api",
                    )
                except Exception as exc:
                    logger.debug("Skip %s: %s", skill_path, exc)

    def _make_skill(
        self, func, path: str, module: str, name: str, category: str,
    ) -> Skill:
        doc = inspect.getdoc(func) or ""
        doc_lines = doc.strip().split("\n")
        doc_short = doc_lines[0] if doc_lines else ""
        params = self._extract_params(func)
        return Skill(
            path=path,
            module=module,
            name=name,
            category=category,
            doc_short=doc_short,
            doc_full=doc,
            params=params,
            _callable=func,
        )

    @staticmethod
    def _extract_params(func) -> list[SkillParam]:
        params: list[SkillParam] = []
        try:
            sig = inspect.signature(func)
        except (ValueError, TypeError):
            return params
        skip = {"file", "ifc_file", "self"}
        for pname, param in sig.parameters.items():
            if pname in skip:
                continue
            annot = "Any"
            is_entity = False
            if param.annotation != inspect.Parameter.empty:
                raw = param.annotation
                annot_str = (
                    getattr(raw, "__name__", None) or str(raw)
                )
                if "entity_instance" in annot_str:
                    annot = "entity_ref"
                    is_entity = True
                else:
                    annot = _simplify_annotation(annot_str)
            default = None
            if param.default != inspect.Parameter.empty:
                default = repr(param.default)
            params.append(SkillParam(pname, annot, default, is_entity))
        return params

    # ------------------------------------------------------------------
    # Query skill wrappers (ifcopenshell.util.element, selector, etc.)
    # ------------------------------------------------------------------

    def _register_query_skills(self):
        """Register utility functions as query skills."""
        query_funcs: list[tuple[str, str, str, Any]] = [
            ("query.by_type", "query", "by_type", self._q_by_type),
            ("query.by_guid", "query", "by_guid", self._q_by_guid),
            ("query.by_id", "query", "by_id", self._q_by_id),
            ("query.get_psets", "query", "get_psets", self._q_get_psets),
            ("query.get_pset", "query", "get_pset", self._q_get_pset),
            ("query.get_material", "query", "get_material", self._q_get_material),
            ("query.get_materials", "query", "get_materials", self._q_get_materials),
            ("query.get_container", "query", "get_container", self._q_get_container),
            ("query.get_type", "query", "get_type", self._q_get_type),
            ("query.get_aggregate", "query", "get_aggregate", self._q_get_aggregate),
            ("query.get_contained", "query", "get_contained", self._q_get_contained),
            ("query.get_decomposition", "query", "get_decomposition", self._q_get_decomposition),
            ("query.get_openings", "query", "get_openings", self._q_get_openings),
            ("query.filter_elements", "query", "filter_elements", self._q_filter_elements),
            ("query.count_by_type", "query", "count_by_type", self._q_count_by_type),
            ("query.get_spatial_structure", "query", "get_spatial_structure", self._q_get_spatial_structure),
            ("query.get_element_info", "query", "get_element_info", self._q_get_element_info),
        ]
        for path, module, name, fn in query_funcs:
            self.skills[path] = Skill(
                path=path,
                module=module,
                name=name,
                category="query",
                doc_short=inspect.getdoc(fn) or "",
                doc_full=inspect.getdoc(fn) or "",
                params=self._extract_params(fn),
                _callable=fn,
            )

    def _q_by_type(self, ifc_class: str) -> list[dict]:
        """Get all elements of a given IFC type. Returns list of {id, guid, type, name}."""
        results = []
        try:
            for el in self.model.by_type(ifc_class):
                results.append({
                    "id": el.id(),
                    "guid": getattr(el, "GlobalId", None),
                    "type": el.is_a(),
                    "name": getattr(el, "Name", None),
                })
        except RuntimeError:
            pass
        return results

    def _q_by_guid(self, guid: str) -> dict | None:
        """Get a single element by GlobalId. Returns {id, guid, type, name} or None."""
        try:
            el = self.model.by_guid(guid)
            return {
                "id": el.id(),
                "guid": el.GlobalId,
                "type": el.is_a(),
                "name": getattr(el, "Name", None),
            }
        except Exception:
            return None

    def _q_by_id(self, element_id: int) -> dict | None:
        """Get a single element by numeric step-file ID."""
        try:
            el = self.model.by_id(element_id)
            return {
                "id": el.id(),
                "guid": getattr(el, "GlobalId", None),
                "type": el.is_a(),
                "name": getattr(el, "Name", None),
            }
        except Exception:
            return None

    def _q_get_psets(self, guid: str) -> dict:
        """Get all property sets and quantities for an element (by GUID)."""
        el = self.model.by_guid(guid)
        return ifcopenshell.util.element.get_psets(el)

    def _q_get_pset(self, guid: str, pset_name: str) -> dict | None:
        """Get a specific property set by name for an element."""
        el = self.model.by_guid(guid)
        return ifcopenshell.util.element.get_pset(el, pset_name)

    def _q_get_material(self, guid: str) -> str | None:
        """Get the material entity associated with an element."""
        el = self.model.by_guid(guid)
        mat = ifcopenshell.util.element.get_material(el)
        if mat is None:
            return None
        return str(mat.get_info()) if hasattr(mat, "get_info") else str(mat)

    def _q_get_materials(self, guid: str) -> list[str]:
        """Get individual material names for an element."""
        el = self.model.by_guid(guid)
        mats = ifcopenshell.util.element.get_materials(el)
        return [getattr(m, "Name", str(m)) for m in mats]

    def _q_get_container(self, guid: str) -> dict | None:
        """Get the spatial container (storey, space, etc.) of an element."""
        el = self.model.by_guid(guid)
        c = ifcopenshell.util.element.get_container(el)
        if c is None:
            return None
        return {"id": c.id(), "guid": c.GlobalId, "type": c.is_a(), "name": getattr(c, "Name", None)}

    def _q_get_type(self, guid: str) -> dict | None:
        """Get the type element (e.g. IfcWallType) of an occurrence."""
        el = self.model.by_guid(guid)
        t = ifcopenshell.util.element.get_type(el)
        if t is None:
            return None
        return {"id": t.id(), "guid": t.GlobalId, "type": t.is_a(), "name": getattr(t, "Name", None)}

    def _q_get_aggregate(self, guid: str) -> dict | None:
        """Get the aggregate parent of an element."""
        el = self.model.by_guid(guid)
        agg = ifcopenshell.util.element.get_aggregate(el)
        if agg is None:
            return None
        return {"id": agg.id(), "guid": agg.GlobalId, "type": agg.is_a(), "name": getattr(agg, "Name", None)}

    def _q_get_contained(self, guid: str) -> list[dict]:
        """Get all elements contained in a spatial element."""
        el = self.model.by_guid(guid)
        contained = ifcopenshell.util.element.get_contained(el)
        return [{"id": c.id(), "guid": getattr(c, "GlobalId", None),
                 "type": c.is_a(), "name": getattr(c, "Name", None)} for c in contained]

    def _q_get_decomposition(self, guid: str) -> list[dict]:
        """Get all sub-elements via spatial decomposition (recursive)."""
        el = self.model.by_guid(guid)
        decomp = ifcopenshell.util.element.get_decomposition(el)
        return [{"id": d.id(), "guid": getattr(d, "GlobalId", None),
                 "type": d.is_a(), "name": getattr(d, "Name", None)} for d in decomp]

    def _q_get_openings(self, guid: str) -> list[dict]:
        """Get IfcOpeningElement entities voiding a building element."""
        el = self.model.by_guid(guid)
        openings = list(ifcopenshell.util.element.get_openings(el))
        results = []
        for rel in openings:
            op = rel.RelatedOpeningElement
            results.append({"id": op.id(), "guid": op.GlobalId, "type": op.is_a()})
        return results

    def _q_filter_elements(self, query: str) -> list[dict]:
        """Filter elements using IfcOpenShell selector syntax (CSS-like queries)."""
        results = ifcopenshell.util.selector.filter_elements(self.model, query)
        return [{"id": el.id(), "guid": getattr(el, "GlobalId", None),
                 "type": el.is_a(), "name": getattr(el, "Name", None)} for el in results]

    def _q_count_by_type(self, ifc_class: str) -> int:
        """Count elements of a given IFC type."""
        try:
            return len(self.model.by_type(ifc_class))
        except RuntimeError:
            return 0

    def _q_get_spatial_structure(self) -> dict:
        """Get the hierarchical spatial structure tree of the model."""
        def _build(parent):
            node = {
                "type": parent.is_a(),
                "name": getattr(parent, "Name", None),
                "guid": getattr(parent, "GlobalId", None),
                "children": [],
            }
            for rel in getattr(parent, "IsDecomposedBy", []):
                for child in rel.RelatedObjects:
                    node["children"].append(_build(child))
            return node
        projects = self.model.by_type("IfcProject")
        if not projects:
            return {}
        return _build(projects[0])

    def _q_get_element_info(self, guid: str) -> dict:
        """Get full IFC attribute dictionary for an element."""
        el = self.model.by_guid(guid)
        info = el.get_info()
        cleaned = {}
        for k, v in info.items():
            if isinstance(v, ifcopenshell.entity_instance):
                cleaned[k] = f"#{v.id()} ({v.is_a()})"
            elif isinstance(v, tuple):
                cleaned[k] = str(v)
            else:
                cleaned[k] = v
        return cleaned

    # ------------------------------------------------------------------
    # Composite skills (higher-level patterns built on atomic APIs)
    # ------------------------------------------------------------------

    def _register_composite_skills(self):
        composites = [
            ("composite.delete_with_openings", "composite", "delete_with_openings",
             self._c_delete_with_openings),
            ("composite.save_model", "composite", "save_model", self._c_save_model),
        ]
        for path, module, name, fn in composites:
            self.skills[path] = Skill(
                path=path, module=module, name=name, category="composite",
                doc_short=inspect.getdoc(fn) or "",
                doc_full=inspect.getdoc(fn) or "",
                params=self._extract_params(fn),
                _callable=fn,
            )

    def _c_delete_with_openings(self, guid: str) -> str:
        """Delete an element and cascade-remove its associated openings and fill relationships."""
        el = self.model.by_guid(guid)
        el_type = el.is_a()
        el_name = getattr(el, "Name", "") or ""

        openings_to_remove = []
        for rel in self.model.by_type("IfcRelFillsElement"):
            if rel.RelatedBuildingElement == el:
                openings_to_remove.append(rel.RelatingOpeningElement)
                try:
                    self.model.remove(rel)
                except Exception:
                    pass

        ifcopenshell.api.root.remove_product(self.model, product=el)

        for opening in openings_to_remove:
            for void_rel in self.model.by_type("IfcRelVoidsElement"):
                if void_rel.RelatedOpeningElement == opening:
                    try:
                        self.model.remove(void_rel)
                    except Exception:
                        pass
            try:
                ifcopenshell.api.root.remove_product(self.model, product=opening)
            except Exception:
                pass

        return f"Deleted {el_type} '{el_name}' (GUID: {guid}) with openings"

    def _c_save_model(self, output_path: str) -> str:
        """Save the current model to an IFC file."""
        self.model.write(output_path)
        return f"Model saved to {output_path}"

    # ------------------------------------------------------------------
    # Catalog generation (for LLM prompts)
    # ------------------------------------------------------------------

    def get_module_catalog(self) -> str:
        """Generate module-level catalog text for the LLM module-selection stage."""
        api_modules = sorted(
            {s.module for s in self.skills.values() if s.category == "api"}
        )
        lines = ["# Available ifcopenshell API Modules\n"]
        for mod in api_modules:
            desc = MODULE_DESCRIPTIONS.get(mod, "")
            count = sum(1 for s in self.skills.values()
                        if s.module == mod and s.category == "api")
            lines.append(f"- **{mod}** ({count} functions): {desc}")

        lines.append("\n# Query Utilities\n")
        query_skills = [s for s in self.skills.values() if s.category == "query"]
        for s in query_skills:
            lines.append(f"- {s.path}: {s.doc_short}")

        lines.append("\n# Composite Operations\n")
        comp_skills = [s for s in self.skills.values() if s.category == "composite"]
        for s in comp_skills:
            lines.append(f"- {s.path}: {s.doc_short}")

        return "\n".join(lines)

    def get_function_catalog(self, modules: list[str]) -> str:
        """Generate function-level detail for selected modules."""
        lines = []
        for mod in modules:
            skills = [s for s in self.skills.values() if s.module == mod]
            if not skills:
                continue
            desc = MODULE_DESCRIPTIONS.get(mod, "")
            lines.append(f"\n## Module: {mod}")
            if desc:
                lines.append(f"{desc}\n")
            lines.append("Functions:")
            for s in sorted(skills, key=lambda x: x.name):
                lines.append(f"  - {s.one_liner()}")
                if s.doc_full and len(s.doc_full) > len(s.doc_short) + 10:
                    detail = s.doc_full[len(s.doc_short):].strip()
                    detail_lines = detail.split("\n")[:4]
                    for dl in detail_lines:
                        dl = dl.strip()
                        if dl:
                            lines.append(f"      {dl}")
        return "\n".join(lines)

    def get_full_catalog(self) -> str:
        """Combined module + function catalog (all modules). May be large."""
        all_modules = sorted({s.module for s in self.skills.values()})
        return self.get_module_catalog() + "\n\n" + self.get_function_catalog(all_modules)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def call(self, skill_path: str, **kwargs) -> Any:
        """Execute a skill by its path.

        For API skills (ifcopenshell.api.*), the model is passed automatically
        as the first argument.  For query/composite skills, the function
        handles the model internally.
        """
        skill = self.skills.get(skill_path)
        if skill is None:
            raise ValueError(f"Unknown skill: {skill_path}")

        if skill.category == "api":
            return skill._callable(self.model, **kwargs)
        else:
            return skill._callable(**kwargs)

    def resolve_entity(self, ref: str | int | dict) -> ifcopenshell.entity_instance:
        """Resolve a flexible entity reference to an actual entity instance.

        Accepts:
          - str: treated as GlobalId
          - int: treated as step-file entity id (#id)
          - dict with 'guid' or 'id' key
        """
        if isinstance(ref, ifcopenshell.entity_instance):
            return ref
        if isinstance(ref, int):
            return self.model.by_id(ref)
        if isinstance(ref, dict):
            if "guid" in ref:
                return self.model.by_guid(ref["guid"])
            if "id" in ref:
                return self.model.by_id(ref["id"])
        if isinstance(ref, str):
            if ref.startswith("#") and ref[1:].isdigit():
                return self.model.by_id(int(ref[1:]))
            return self.model.by_guid(ref)
        raise ValueError(f"Cannot resolve entity reference: {ref}")

    def list_modules(self) -> list[str]:
        return sorted({s.module for s in self.skills.values()})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simplify_annotation(s: str) -> str:
    """Simplify verbose type annotations for display."""
    s = s.replace("typing.", "").replace("ifcopenshell.entity_instance.entity_instance", "entity_ref")
    s = s.replace("ifcopenshell.entity_instance", "entity_ref")
    s = s.replace("ifcopenshell.file.file", "ifc_file")
    if len(s) > 80:
        s = s[:77] + "..."
    return s
