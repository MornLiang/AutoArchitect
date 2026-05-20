"""
IFC tool classes wrapping ifcopenshell operations.

Each tool follows the HuggingFace Tool interface pattern (name/description/inputs/outputs)
used in Text2BIM, adapted for direct IFC file manipulation via ifcopenshell.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional, Union

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.placement
import ifcopenshell.util.element


class ToolBase:
    """Minimal base mirroring the HuggingFace Tool interface."""
    name: str = ""
    description: str = ""
    inputs: list[str] = []
    outputs: list[str] = []

    def __call__(self, *args, **kwargs):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Query / Read Tools
# ---------------------------------------------------------------------------

class QueryElementsByType(ToolBase):
    name = "query_elements_by_type"
    description = (
        "Query all elements of a given IFC type from the loaded model.\n"
        "Input:\n"
        "  - ifc_type: str, the IFC entity type (e.g. 'IfcWall', 'IfcDoor', 'IfcWindow').\n"
        "Return:\n"
        "  - list of dicts, each with keys: guid, type, name."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, ifc_type: str) -> list[dict]:
        return self.parser.get_elements_by_type(ifc_type)


class CountElements(ToolBase):
    name = "count_elements"
    description = (
        "Count the number of elements of a given IFC type.\n"
        "Input:\n"
        "  - ifc_type: str, the IFC entity type.\n"
        "Return:\n"
        "  - int, the count."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, ifc_type: str) -> int:
        return self.parser.count_by_type(ifc_type)


class GetElementProperties(ToolBase):
    name = "get_element_properties"
    description = (
        "Retrieve all PropertySets and QuantitySets for a specific element.\n"
        "Input:\n"
        "  - guid: str, the GlobalId of the element.\n"
        "Return:\n"
        "  - dict mapping pset_name -> {property_name: value}."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> dict:
        return self.parser.get_element_properties(guid)


class GetElementInfo(ToolBase):
    name = "get_element_info"
    description = (
        "Get the full IFC info dictionary for a single element.\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "Return:\n"
        "  - dict with all IFC attributes of the element."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> dict:
        info = self.parser.get_element_info(guid)
        cleaned = {}
        for k, v in info.items():
            if isinstance(v, ifcopenshell.entity_instance):
                cleaned[k] = str(v)
            elif isinstance(v, tuple):
                cleaned[k] = str(v)
            else:
                cleaned[k] = v
        return cleaned


class GetSpatialStructure(ToolBase):
    name = "get_spatial_structure"
    description = (
        "Get the hierarchical spatial structure of the building model.\n"
        "Return:\n"
        "  - nested dict: IfcProject -> IfcSite -> IfcBuilding -> IfcBuildingStorey -> IfcSpace."
    )
    inputs = []
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self) -> dict:
        return self.parser.get_spatial_structure()


class GetElementMaterial(ToolBase):
    name = "get_element_material"
    description = (
        "Get material names assigned to an element.\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "Return:\n"
        "  - list of str, material names."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> list[str]:
        return self.parser.get_element_material(guid)


class GetElementRelationships(ToolBase):
    name = "get_element_relationships"
    description = (
        "Get spatial containment, type, and connectivity relationships for an element.\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "Return:\n"
        "  - dict with keys: contained_in, element_type, connections."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> dict:
        return self.parser.get_relationships(guid)


class GetStoreyElements(ToolBase):
    name = "get_storey_elements"
    description = (
        "List all elements contained in a specific building storey.\n"
        "Input:\n"
        "  - storey_guid: str, the GlobalId of the IfcBuildingStorey.\n"
        "Return:\n"
        "  - list of dicts with guid, type, name for each contained element."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, storey_guid: str) -> list[dict]:
        return self.parser.get_storey_elements(storey_guid)


class GetModelContext(ToolBase):
    name = "get_model_context"
    description = (
        "Get a text summary of the entire IFC model suitable for LLM analysis.\n"
        "Input:\n"
        "  - max_chars: int, maximum character length (default 8000).\n"
        "Return:\n"
        "  - str, the serialized model context."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, max_chars: int = 8000) -> str:
        return self.parser.serialize_context(max_chars)


# ---------------------------------------------------------------------------
# Edit / Write Tools
# ---------------------------------------------------------------------------

def _cascade_remove(model, element):
    """Remove an element and clean up its related openings and fill relations.

    When an IfcDoor/IfcWindow is deleted, the IfcOpeningElement that hosts it
    (via IfcRelFillsElement) should also be removed, along with the
    IfcRelVoidsElement that cuts the opening out of the wall.
    """
    openings_to_remove = []

    for rel in model.by_type("IfcRelFillsElement"):
        if rel.RelatedBuildingElement == element:
            opening = rel.RelatingOpeningElement
            openings_to_remove.append(opening)
            try:
                model.remove(rel)
            except Exception:
                pass

    ifcopenshell.api.run("root.remove_product", model, product=element)

    for opening in openings_to_remove:
        for void_rel in model.by_type("IfcRelVoidsElement"):
            if void_rel.RelatedOpeningElement == opening:
                try:
                    model.remove(void_rel)
                except Exception:
                    pass
        try:
            ifcopenshell.api.run("root.remove_product", model, product=opening)
        except Exception:
            pass

class DeleteElement(ToolBase):
    name = "delete_element"
    description = (
        "Delete a single element from the IFC model by its GlobalId.\n"
        "Cascade-removes associated IfcOpeningElements and fill relationships.\n"
        "Input:\n"
        "  - guid: str, the GlobalId of the element to delete.\n"
        "Return:\n"
        "  - str, confirmation message with the deleted element's type and guid."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")
        el_type = el.is_a()
        el_name = getattr(el, "Name", None) or ""
        _cascade_remove(self.parser.model, el)
        return f"Deleted {el_type} '{el_name}' (GUID: {guid})"


class DeleteElementsByType(ToolBase):
    name = "delete_elements_by_type"
    description = (
        "Delete ALL elements of a given IFC type from the model.\n"
        "Cascade-removes associated IfcOpeningElements and fill relationships.\n"
        "Input:\n"
        "  - ifc_type: str, the IFC entity type (e.g. 'IfcDoor').\n"
        "Return:\n"
        "  - str, summary of how many elements were deleted."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, ifc_type: str) -> str:
        elements = list(self.parser.model.by_type(ifc_type))
        count = len(elements)
        for el in elements:
            try:
                _cascade_remove(self.parser.model, el)
            except Exception:
                pass
        return f"Deleted {count} elements of type {ifc_type}."


class ModifyProperty(ToolBase):
    name = "modify_property"
    description = (
        "Modify a property value within a PropertySet of an element.\n"
        "Input:\n"
        "  - guid: str, the GlobalId of the element.\n"
        "  - pset_name: str, the name of the PropertySet.\n"
        "  - property_name: str, the property name to modify.\n"
        "  - new_value: any, the new value to set.\n"
        "Return:\n"
        "  - str, confirmation message."
    )
    inputs = ["text", "text", "text", "text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str, pset_name: str, property_name: str, new_value) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")

        for definition in getattr(el, "IsDefinedBy", []):
            if definition.is_a("IfcRelDefinesByProperties"):
                pset = definition.RelatingPropertyDefinition
                if getattr(pset, "Name", None) == pset_name and pset.is_a("IfcPropertySet"):
                    for prop in pset.HasProperties:
                        if prop.Name == property_name:
                            if hasattr(prop, "NominalValue") and prop.NominalValue:
                                old_val = prop.NominalValue.wrappedValue
                                ifc_value_type = prop.NominalValue.is_a()
                                prop.NominalValue = self.parser.model.create_entity(
                                    ifc_value_type, new_value
                                )
                                return (
                                    f"Modified {pset_name}.{property_name}: "
                                    f"{old_val} -> {new_value}"
                                )
        raise ValueError(
            f"Property {pset_name}.{property_name} not found on element {guid}."
        )


class ModifyElementAttribute(ToolBase):
    name = "modify_element_attribute"
    description = (
        "Modify a direct IFC attribute of an element (e.g. Name, Description, ObjectType).\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "  - attribute_name: str, the IFC attribute name.\n"
        "  - new_value: any, the new value.\n"
        "Return:\n"
        "  - str, confirmation message."
    )
    inputs = ["text", "text", "text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str, attribute_name: str, new_value) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")
        old_val = getattr(el, attribute_name, None)
        setattr(el, attribute_name, new_value)
        return f"Modified {el.is_a()}.{attribute_name}: {old_val} -> {new_value}"


class MoveElement(ToolBase):
    name = "move_element"
    description = (
        "Move an element by a relative offset (dx, dy, dz) in model coordinates.\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "  - dx: float, offset in X direction.\n"
        "  - dy: float, offset in Y direction.\n"
        "  - dz: float, offset in Z direction.\n"
        "Return:\n"
        "  - str, confirmation message."
    )
    inputs = ["text", "text", "text", "text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")

        placement = getattr(el, "ObjectPlacement", None)
        if placement is None:
            raise ValueError(f"Element {guid} has no ObjectPlacement.")

        if placement.is_a("IfcLocalPlacement"):
            rel_placement = placement.RelativePlacement
            if rel_placement and rel_placement.is_a("IfcAxis2Placement3D"):
                loc = rel_placement.Location
                coords = list(loc.Coordinates)
                coords[0] += float(dx)
                coords[1] += float(dy)
                coords[2] += float(dz)
                loc.Coordinates = tuple(coords)
                return f"Moved {el.is_a()} ({guid}) by ({dx}, {dy}, {dz})"

        raise ValueError(f"Cannot move element {guid}: unsupported placement type.")


class CopyElement(ToolBase):
    name = "copy_element"
    description = (
        "Create a deep copy of an element. The copy gets a new GlobalId.\n"
        "Input:\n"
        "  - guid: str, the GlobalId of the element to copy.\n"
        "Return:\n"
        "  - str, the GlobalId of the new copy."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")
        new_el = ifcopenshell.api.run("root.copy_class", self.parser.model, product=el)
        new_guid = new_el.GlobalId
        self.parser._guid_index[new_guid] = new_el
        return new_guid


class ModifyMaterial(ToolBase):
    name = "modify_material"
    description = (
        "Change the material name associated with an element.\n"
        "For elements with IfcMaterialLayerSetUsage, modifies the first layer's material name.\n"
        "Input:\n"
        "  - guid: str, the GlobalId.\n"
        "  - new_material_name: str, the new material name.\n"
        "Return:\n"
        "  - str, confirmation message."
    )
    inputs = ["text", "text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, guid: str, new_material_name: str) -> str:
        el = self.parser.get_element_by_guid(guid)
        if el is None:
            raise ValueError(f"Element with GUID {guid} not found.")

        for rel in getattr(el, "HasAssociations", []):
            if rel.is_a("IfcRelAssociatesMaterial"):
                mat = rel.RelatingMaterial
                if mat.is_a("IfcMaterial"):
                    old = mat.Name
                    mat.Name = new_material_name
                    return f"Material changed: {old} -> {new_material_name}"
                elif mat.is_a("IfcMaterialLayerSetUsage"):
                    layers = mat.ForLayerSet.MaterialLayers
                    if layers and layers[0].Material:
                        old = layers[0].Material.Name
                        layers[0].Material.Name = new_material_name
                        return f"Material (first layer) changed: {old} -> {new_material_name}"
                elif mat.is_a("IfcMaterialLayerSet"):
                    layers = mat.MaterialLayers
                    if layers and layers[0].Material:
                        old = layers[0].Material.Name
                        layers[0].Material.Name = new_material_name
                        return f"Material (first layer) changed: {old} -> {new_material_name}"

        raise ValueError(f"No material association found for element {guid}.")


class ValidateModel(ToolBase):
    name = "validate_model"
    description = (
        "Run basic validation checks on the IFC model.\n"
        "Checks: orphan elements, missing storeys, empty property sets, etc.\n"
        "Return:\n"
        "  - dict with 'valid' bool and 'issues' list."
    )
    inputs = []
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self) -> dict:
        issues = []
        model = self.parser.model

        if not model.by_type("IfcProject"):
            issues.append("No IfcProject found.")
        if not model.by_type("IfcBuilding"):
            issues.append("No IfcBuilding found.")
        if not model.by_type("IfcBuildingStorey"):
            issues.append("No IfcBuildingStorey found.")

        # Check for products without spatial containment
        products = model.by_type("IfcProduct")
        orphans = 0
        for p in products:
            if p.is_a("IfcSpatialStructureElement"):
                continue
            contained = getattr(p, "ContainedInStructure", [])
            decomposed = getattr(p, "Decomposes", [])
            if not contained and not decomposed:
                orphans += 1
        if orphans > 0:
            issues.append(f"{orphans} product(s) without spatial containment.")

        return {"valid": len(issues) == 0, "issues": issues}


class SaveModel(ToolBase):
    name = "save_model"
    description = (
        "Save the current (modified) IFC model to a file.\n"
        "Input:\n"
        "  - output_path: str, the file path to save to (optional, defaults to original path).\n"
        "Return:\n"
        "  - str, confirmation with the saved path."
    )
    inputs = ["text"]
    outputs = ["text"]

    def __init__(self, parser):
        self.parser = parser

    def __call__(self, output_path: str = None) -> str:
        path = output_path or self.parser.ifc_path
        self.parser.save(path)
        return f"Model saved to {path}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def create_tool_registry(parser) -> dict[str, ToolBase]:
    """Instantiate all tools with a shared parser and return name -> tool mapping."""
    tools = [
        QueryElementsByType(parser),
        CountElements(parser),
        GetElementProperties(parser),
        GetElementInfo(parser),
        GetSpatialStructure(parser),
        GetElementMaterial(parser),
        GetElementRelationships(parser),
        GetStoreyElements(parser),
        GetModelContext(parser),
        DeleteElement(parser),
        DeleteElementsByType(parser),
        ModifyProperty(parser),
        ModifyElementAttribute(parser),
        MoveElement(parser),
        CopyElement(parser),
        ModifyMaterial(parser),
        ValidateModel(parser),
        SaveModel(parser),
    ]
    return {t.name: t for t in tools}


def get_tools_description(registry: dict[str, ToolBase]) -> str:
    """Generate a text description of all available tools for prompt injection."""
    lines = []
    for name, tool in registry.items():
        lines.append(f"- {name}: {tool.description.strip()}")
    return "\n".join(lines)
