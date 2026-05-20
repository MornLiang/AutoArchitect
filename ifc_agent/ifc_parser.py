"""
IFC file parsing, indexing, and serialization module.

Provides structured access to IFC model data for LLM consumption,
including spatial hierarchy, element properties, and relationship queries.
"""

import os
from collections import defaultdict
from typing import Any, Optional

import ifcopenshell
import ifcopenshell.util.element as element_util


class IFCParser:
    """Loads and indexes an IFC file, exposes query helpers and LLM-oriented serialization."""

    def __init__(self, ifc_path: str):
        if not os.path.isfile(ifc_path):
            raise FileNotFoundError(f"IFC file not found: {ifc_path}")
        self.ifc_path = ifc_path
        self.model: ifcopenshell.file = ifcopenshell.open(ifc_path)
        self._type_index: dict[str, list] = {}
        self._guid_index: dict[str, Any] = {}
        self._build_index()

    def _build_index(self):
        """Build in-memory indexes for fast lookup."""
        for entity in self.model:
            ifc_type = entity.is_a()
            self._type_index.setdefault(ifc_type, []).append(entity)
            if hasattr(entity, "GlobalId"):
                self._guid_index[entity.GlobalId] = entity

    # ------------------------------------------------------------------
    # Schema & statistics
    # ------------------------------------------------------------------

    def get_schema_summary(self) -> dict:
        """Return schema version and entity-type counts."""
        type_counts = {t: len(entities) for t, entities in self._type_index.items()}
        sorted_counts = dict(sorted(type_counts.items(), key=lambda x: -x[1]))
        return {
            "schema": self.model.schema,
            "total_entities": len(list(self.model)),
            "type_counts": sorted_counts,
        }

    # ------------------------------------------------------------------
    # Spatial structure
    # ------------------------------------------------------------------

    def get_spatial_structure(self) -> dict:
        """Extract the IfcProject → Site → Building → Storey → Space hierarchy."""
        projects = self.model.by_type("IfcProject")
        if not projects:
            return {}
        project = projects[0]
        return self._walk_spatial(project)

    def _walk_spatial(self, entity) -> dict:
        node: dict[str, Any] = {
            "type": entity.is_a(),
            "name": getattr(entity, "Name", None) or getattr(entity, "LongName", None) or "",
            "guid": getattr(entity, "GlobalId", None),
        }
        children = []
        for rel in getattr(entity, "IsDecomposedBy", []):
            for child in rel.RelatedObjects:
                children.append(self._walk_spatial(child))
        if not children:
            # Also check IfcRelContainedInSpatialStructure for leaf elements
            pass
        if children:
            node["children"] = children
        return node

    # ------------------------------------------------------------------
    # Element queries
    # ------------------------------------------------------------------

    def get_elements_by_type(self, ifc_type: str) -> list[dict]:
        """Return summarised info for all elements of given IFC type."""
        elements = self.model.by_type(ifc_type)
        results = []
        for el in elements:
            info = {
                "guid": getattr(el, "GlobalId", None),
                "type": el.is_a(),
                "name": getattr(el, "Name", None),
                "description": getattr(el, "Description", None),
            }
            results.append(info)
        return results

    def get_element_by_guid(self, guid: str):
        """Return raw ifcopenshell entity by GlobalId."""
        return self._guid_index.get(guid) or self.model.by_guid(guid)

    def get_element_info(self, guid: str) -> dict:
        """Return full info dict for one element."""
        el = self.get_element_by_guid(guid)
        return el.get_info() if el else {}

    def get_element_properties(self, guid: str) -> dict[str, dict]:
        """Collect all PropertySets and QuantitySets for an element."""
        el = self.get_element_by_guid(guid)
        if el is None:
            return {}
        props: dict[str, dict] = {}

        for definition in getattr(el, "IsDefinedBy", []):
            if definition.is_a("IfcRelDefinesByProperties"):
                pset = definition.RelatingPropertyDefinition
                pset_name = getattr(pset, "Name", "Unknown")
                pset_props = {}
                if pset.is_a("IfcPropertySet"):
                    for prop in pset.HasProperties:
                        val = getattr(prop, "NominalValue", None)
                        pset_props[prop.Name] = val.wrappedValue if val else None
                elif pset.is_a("IfcElementQuantity"):
                    for q in pset.Quantities:
                        for attr_name in ("LengthValue", "AreaValue", "VolumeValue", "CountValue", "WeightValue"):
                            v = getattr(q, attr_name, None)
                            if v is not None:
                                pset_props[q.Name] = v
                                break
                props[pset_name] = pset_props

        return props

    def get_element_material(self, guid: str) -> list[str]:
        """Return material names associated with an element."""
        el = self.get_element_by_guid(guid)
        if el is None:
            return []
        materials = []
        for rel in getattr(el, "HasAssociations", []):
            if rel.is_a("IfcRelAssociatesMaterial"):
                mat = rel.RelatingMaterial
                if mat.is_a("IfcMaterial"):
                    materials.append(mat.Name)
                elif mat.is_a("IfcMaterialLayerSetUsage"):
                    for layer in mat.ForLayerSet.MaterialLayers:
                        if layer.Material:
                            materials.append(layer.Material.Name)
                elif mat.is_a("IfcMaterialLayerSet"):
                    for layer in mat.MaterialLayers:
                        if layer.Material:
                            materials.append(layer.Material.Name)
                elif mat.is_a("IfcMaterialList"):
                    for m in mat.Materials:
                        materials.append(m.Name)
        return materials

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    def get_relationships(self, guid: str) -> dict:
        """Return spatial containment, connectivity, and type relationships."""
        el = self.get_element_by_guid(guid)
        if el is None:
            return {}
        rels: dict[str, Any] = {}

        # Spatial containment
        for rel in getattr(el, "ContainedInStructure", []):
            container = rel.RelatingStructure
            rels["contained_in"] = {
                "guid": container.GlobalId,
                "type": container.is_a(),
                "name": getattr(container, "Name", None),
            }

        # Type
        for rel in getattr(el, "IsTypedBy", []):
            type_obj = rel.RelatingType
            rels["element_type"] = {
                "guid": type_obj.GlobalId,
                "type": type_obj.is_a(),
                "name": getattr(type_obj, "Name", None),
            }

        # Connections
        connections = []
        for rel in getattr(el, "ConnectedTo", []):
            other = rel.RelatedElement
            connections.append({
                "guid": other.GlobalId,
                "type": other.is_a(),
                "name": getattr(other, "Name", None),
            })
        for rel in getattr(el, "ConnectedFrom", []):
            other = rel.RelatingElement
            connections.append({
                "guid": other.GlobalId,
                "type": other.is_a(),
                "name": getattr(other, "Name", None),
            })
        if connections:
            rels["connections"] = connections

        return rels

    def get_storey_elements(self, storey_guid: str) -> list[dict]:
        """Return all elements contained in a given storey."""
        storey = self.get_element_by_guid(storey_guid)
        if storey is None:
            return []
        results = []
        for rel in getattr(storey, "ContainsElements", []):
            for el in rel.RelatedElements:
                results.append({
                    "guid": el.GlobalId,
                    "type": el.is_a(),
                    "name": getattr(el, "Name", None),
                })
        return results

    # ------------------------------------------------------------------
    # Serialization for LLM
    # ------------------------------------------------------------------

    def serialize_context(self, max_chars: int = 8000) -> str:
        """Serialize IFC model summary into a text context for LLM consumption."""
        parts = []

        # Schema summary
        summary = self.get_schema_summary()
        parts.append(f"IFC Schema: {summary['schema']}")
        parts.append(f"Total entities: {summary['total_entities']}")

        # Key element counts
        bim_types = [
            "IfcWall", "IfcWallStandardCase", "IfcDoor", "IfcWindow",
            "IfcSlab", "IfcRoof", "IfcColumn", "IfcBeam", "IfcStair",
            "IfcSpace", "IfcBuildingStorey", "IfcBuilding", "IfcSite",
            "IfcRailing", "IfcCurtainWall", "IfcFurnishingElement", "IfcCovering",
            "IfcOpeningElement", "IfcMember", "IfcPlate",
        ]
        parts.append("\nElement counts:")
        for t in bim_types:
            try:
                count = len(self.model.by_type(t))
            except RuntimeError:
                continue
            if count > 0:
                parts.append(f"  {t}: {count}")

        # Spatial structure
        spatial = self.get_spatial_structure()
        if spatial:
            parts.append("\nSpatial structure:")
            parts.append(self._format_spatial_tree(spatial, indent=2))

        # Storey details with element summaries
        storeys = self.model.by_type("IfcBuildingStorey")
        if storeys:
            parts.append("\nStorey details:")
            for storey in storeys:
                elev = getattr(storey, "Elevation", None)
                parts.append(f"  {storey.Name or 'Unnamed'} (GUID: {storey.GlobalId}, Elevation: {elev})")
                contained = self.get_storey_elements(storey.GlobalId)
                type_counts: dict[str, int] = defaultdict(int)
                for el in contained:
                    type_counts[el["type"]] += 1
                for t, c in sorted(type_counts.items()):
                    parts.append(f"    {t}: {c}")

        result = "\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result

    def _format_spatial_tree(self, node: dict, indent: int = 0) -> str:
        prefix = " " * indent
        line = f"{prefix}{node['type']}: {node.get('name', '')} [{node.get('guid', '')}]"
        lines = [line]
        for child in node.get("children", []):
            lines.append(self._format_spatial_tree(child, indent + 2))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def count_by_type(self, ifc_type: str) -> int:
        return len(self.model.by_type(ifc_type))

    def list_element_types(self) -> list[str]:
        """Return all distinct IFC types present in the model."""
        return sorted(self._type_index.keys())

    def save(self, output_path: Optional[str] = None):
        """Write the (potentially modified) model back to disk."""
        path = output_path or self.ifc_path
        self.model.write(path)

    def reload(self):
        """Reload the model from disk and rebuild indexes."""
        self.model = ifcopenshell.open(self.ifc_path)
        self._type_index.clear()
        self._guid_index.clear()
        self._build_index()
