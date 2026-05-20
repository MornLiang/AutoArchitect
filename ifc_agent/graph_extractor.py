"""
Multi-level hierarchical graph extraction from IFC models.

Produces three levels of graph structures:

  Level 0 — **Building graph** (overall):
      Nodes = spatial elements (Project/Site/Building/Storey) + building
      elements (Wall/Door/Window/Column/Slab/…).
      Edges = aggregation, spatial containment, voids/fills, path
      connections, type assignments.
      NO geometry points, properties, or material detail.

  Level 1 — **Storey graphs** (one per IfcBuildingStorey):
      Same node/edge semantics, filtered to a single storey plus
      cross-storey connections.

  Level 2 — **Element graphs** (one per building element):
      Full detail for a single component: property sets, individual
      property values, material layers, geometry placement, profile
      shape points, extrusion parameters.

All graphs are plain JSON dicts ``{nodes: [...], edges: [...]}``.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any, Optional

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement

logger = logging.getLogger(__name__)

# IFC types treated as concrete building elements in the overview graph.
_BUILDING_ELEMENT_CLASSES = {
    "IfcWall", "IfcWallStandardCase",
    "IfcDoor", "IfcWindow",
    "IfcSlab", "IfcRoof",
    "IfcColumn", "IfcBeam",
    "IfcStair", "IfcStairFlight",
    "IfcRamp", "IfcRampFlight",
    "IfcRailing", "IfcCovering",
    "IfcCurtainWall", "IfcPlate", "IfcMember",
    "IfcFooting", "IfcPile",
    "IfcBuildingElementProxy",
    "IfcFurnishingElement",
    "IfcDistributionElement",
    "IfcOpeningElement",
    "IfcSpace",
}

_SPATIAL_CLASSES = {
    "IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey",
}


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _node_id(entity: ifcopenshell.entity_instance) -> str:
    """Stable node identifier: ``#step_id``."""
    return f"#{entity.id()}"


def _basic_attrs(entity: ifcopenshell.entity_instance) -> dict:
    """Extract lightweight attributes for a node."""
    attrs: dict[str, Any] = {
        "ifc_id": entity.id(),
        "ifc_class": entity.is_a(),
    }
    for attr in ("GlobalId", "Name", "Description", "ObjectType", "Tag",
                 "PredefinedType", "LongName"):
        val = getattr(entity, attr, None)
        if val is not None:
            attrs[attr] = str(val) if not isinstance(val, (str, int, float, bool)) else val
    return attrs


def _is_building_element(entity: ifcopenshell.entity_instance) -> bool:
    return any(entity.is_a(c) for c in _BUILDING_ELEMENT_CLASSES)


def _is_spatial(entity: ifcopenshell.entity_instance) -> bool:
    return any(entity.is_a(c) for c in _SPATIAL_CLASSES)


# -----------------------------------------------------------------------
# Level 0 — Building Overview Graph
# -----------------------------------------------------------------------

def extract_building_graph(model: ifcopenshell.file) -> dict:
    """Extract the top-level building structure graph.

    Nodes: spatial structure + building elements.
    Edges: aggregation, containment, voids, fills, path-connections,
           type-assignment.
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    # -- Spatial structure nodes --
    for cls in ("IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace"):
        try:
            for el in model.by_type(cls):
                nid = _node_id(el)
                nodes[nid] = {"id": nid, **_basic_attrs(el), "category": "spatial"}
        except RuntimeError:
            pass

    # -- Building element nodes --
    for el in model.by_type("IfcProduct"):
        if _is_building_element(el) and _node_id(el) not in nodes:
            nid = _node_id(el)
            nodes[nid] = {"id": nid, **_basic_attrs(el), "category": "element"}

    # -- Aggregation edges (Project→Site→Building→Storey→Space) --
    for rel in model.by_type("IfcRelAggregates"):
        parent = rel.RelatingObject
        pid = _node_id(parent)
        if pid not in nodes:
            nodes[pid] = {"id": pid, **_basic_attrs(parent), "category": "spatial"}
        for child in rel.RelatedObjects:
            cid = _node_id(child)
            if cid not in nodes:
                nodes[cid] = {"id": cid, **_basic_attrs(child), "category": "spatial"}
            edges.append({
                "source": pid, "target": cid,
                "relation": "aggregates",
            })

    # -- Spatial containment (Storey→Element) --
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        structure = rel.RelatingStructure
        sid = _node_id(structure)
        for el in rel.RelatedElements:
            eid = _node_id(el)
            if eid in nodes:
                edges.append({
                    "source": sid, "target": eid,
                    "relation": "contains",
                })

    # -- Voids (Wall → OpeningElement) --
    for rel in model.by_type("IfcRelVoidsElement"):
        building_el = rel.RelatingBuildingElement
        opening = rel.RelatedOpeningElement
        bid = _node_id(building_el)
        oid = _node_id(opening)
        if bid in nodes and oid in nodes:
            edges.append({
                "source": bid, "target": oid,
                "relation": "has_opening",
            })

    # -- Fills (Door/Window → OpeningElement) --
    for rel in model.by_type("IfcRelFillsElement"):
        opening = rel.RelatingOpeningElement
        filling = rel.RelatedBuildingElement
        oid = _node_id(opening)
        fid = _node_id(filling)
        if oid in nodes and fid in nodes:
            edges.append({
                "source": fid, "target": oid,
                "relation": "fills_opening",
            })

    # -- Path connections (Wall ↔ Wall) --
    for rel in model.by_type("IfcRelConnectsPathElements"):
        e1 = rel.RelatingElement
        e2 = rel.RelatedElement
        id1, id2 = _node_id(e1), _node_id(e2)
        if id1 in nodes and id2 in nodes:
            edges.append({
                "source": id1, "target": id2,
                "relation": "connects_path",
                "attrs": {
                    "relating_connection": str(getattr(rel, "RelatingConnectionType", "")),
                    "related_connection": str(getattr(rel, "RelatedConnectionType", "")),
                },
            })

    # -- Type assignment (Element → ElementType) --
    for rel in model.by_type("IfcRelDefinesByType"):
        el_type = rel.RelatingType
        tid = _node_id(el_type)
        if tid not in nodes:
            nodes[tid] = {"id": tid, **_basic_attrs(el_type), "category": "type"}
        for el in rel.RelatedObjects:
            eid = _node_id(el)
            if eid in nodes:
                edges.append({
                    "source": eid, "target": tid,
                    "relation": "defined_by_type",
                })

    return _make_graph("building_overview", "building", model, nodes, edges)


# -----------------------------------------------------------------------
# Level 1 — Per-Storey Graphs
# -----------------------------------------------------------------------

def extract_storey_graphs(model: ifcopenshell.file) -> list[dict]:
    """Extract one graph per IfcBuildingStorey."""
    storey_elements: dict[str, set[str]] = defaultdict(set)

    # Map each element to its storey
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        structure = rel.RelatingStructure
        if structure.is_a("IfcBuildingStorey"):
            sid = _node_id(structure)
            for el in rel.RelatedElements:
                storey_elements[sid].add(_node_id(el))
                # Include openings and fillings of contained elements
                if el.is_a("IfcElement"):
                    for void_rel in getattr(el, "HasOpenings", []):
                        opening = void_rel.RelatedOpeningElement
                        storey_elements[sid].add(_node_id(opening))
                        for fill_rel in getattr(opening, "HasFillings", []):
                            storey_elements[sid].add(_node_id(fill_rel.RelatedBuildingElement))

    building_graph = extract_building_graph(model)
    all_nodes = {n["id"]: n for n in building_graph["nodes"]}
    all_edges = building_graph["edges"]

    graphs = []
    for storey in model.by_type("IfcBuildingStorey"):
        sid = _node_id(storey)
        member_ids = storey_elements.get(sid, set())
        member_ids.add(sid)

        nodes = {nid: all_nodes[nid] for nid in member_ids if nid in all_nodes}

        # Also include type nodes referenced by members
        for edge in all_edges:
            if edge["relation"] == "defined_by_type":
                if edge["source"] in nodes and edge["target"] in all_nodes:
                    nodes[edge["target"]] = all_nodes[edge["target"]]

        # Filter edges to those whose both endpoints are in this storey
        edges = [
            e for e in all_edges
            if e["source"] in nodes and e["target"] in nodes
        ]

        storey_name = getattr(storey, "Name", None) or storey.GlobalId
        g = _make_graph(
            f"storey_{storey_name}",
            "storey",
            model, nodes, edges,
        )
        g["storey_guid"] = storey.GlobalId
        g["storey_name"] = storey_name
        graphs.append(g)

    return graphs


# -----------------------------------------------------------------------
# Level 2 — Per-Element Detail Graphs
# -----------------------------------------------------------------------

def extract_element_graph(
    model: ifcopenshell.file,
    element: ifcopenshell.entity_instance,
) -> dict:
    """Extract a detailed graph for a single building element.

    Includes property sets, properties, materials, geometry placement,
    profile shape points, and extrusion parameters.
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    eid = _node_id(element)
    nodes[eid] = {"id": eid, **_basic_attrs(element), "category": "element"}

    # -- Properties & Quantities --
    _extract_psets(model, element, eid, nodes, edges)

    # -- Material --
    _extract_materials(element, eid, nodes, edges)

    # -- Type --
    el_type = ifcopenshell.util.element.get_type(element)
    if el_type:
        tid = _node_id(el_type)
        nodes[tid] = {"id": tid, **_basic_attrs(el_type), "category": "type"}
        edges.append({"source": eid, "target": tid, "relation": "defined_by_type"})
        _extract_psets(model, el_type, tid, nodes, edges)

    # -- Spatial container --
    container = ifcopenshell.util.element.get_container(element)
    if container:
        cid = _node_id(container)
        nodes[cid] = {"id": cid, **_basic_attrs(container), "category": "spatial"}
        edges.append({"source": cid, "target": eid, "relation": "contains"})

    # -- Placement --
    _extract_placement(element, eid, nodes, edges)

    # -- Geometry (profile + extrusion, NOT tessellation faces) --
    _extract_geometry(element, eid, nodes, edges)

    # -- Openings / Fillings --
    for void_rel in getattr(element, "HasOpenings", []):
        opening = void_rel.RelatedOpeningElement
        oid = _node_id(opening)
        nodes[oid] = {"id": oid, **_basic_attrs(opening), "category": "element"}
        edges.append({"source": eid, "target": oid, "relation": "has_opening"})
        _extract_placement(opening, oid, nodes, edges)
        for fill_rel in getattr(opening, "HasFillings", []):
            filling = fill_rel.RelatedBuildingElement
            fid = _node_id(filling)
            nodes[fid] = {"id": fid, **_basic_attrs(filling), "category": "element"}
            edges.append({"source": fid, "target": oid, "relation": "fills_opening"})

    el_name = getattr(element, "Name", None) or element.GlobalId
    return _make_graph(
        f"element_{el_name}",
        "element",
        model, nodes, edges,
    )


def _extract_psets(model, element, eid, nodes, edges):
    """Add PropertySet → Property nodes and edges."""
    try:
        psets = ifcopenshell.util.element.get_psets(element)
    except Exception:
        return
    for pset_name, props in psets.items():
        pset_id = f"{eid}/pset:{pset_name}"
        nodes[pset_id] = {
            "id": pset_id,
            "ifc_class": "IfcPropertySet",
            "Name": pset_name,
            "category": "property_set",
        }
        edges.append({"source": eid, "target": pset_id, "relation": "has_property_set"})

        for prop_name, prop_val in props.items():
            if prop_name == "id":
                continue
            prop_id = f"{pset_id}/{prop_name}"
            nodes[prop_id] = {
                "id": prop_id,
                "ifc_class": "IfcProperty",
                "Name": prop_name,
                "value": _safe_value(prop_val),
                "category": "property",
            }
            edges.append({"source": pset_id, "target": prop_id, "relation": "has_property"})


def _extract_materials(element, eid, nodes, edges):
    """Add Material → MaterialLayer nodes and edges."""
    mat = ifcopenshell.util.element.get_material(element)
    if mat is None:
        return

    mat_id = _node_id(mat)
    mat_attrs = {"id": mat_id, "ifc_class": mat.is_a(), "category": "material"}

    if mat.is_a("IfcMaterial"):
        mat_attrs["Name"] = mat.Name
        nodes[mat_id] = mat_attrs
        edges.append({"source": eid, "target": mat_id, "relation": "has_material"})

    elif mat.is_a("IfcMaterialLayerSetUsage"):
        layer_set = mat.ForLayerSet
        ls_id = _node_id(layer_set)
        nodes[ls_id] = {
            "id": ls_id, "ifc_class": "IfcMaterialLayerSet",
            "Name": getattr(layer_set, "LayerSetName", None),
            "category": "material",
        }
        edges.append({"source": eid, "target": ls_id, "relation": "has_material_layer_set"})

        for i, layer in enumerate(layer_set.MaterialLayers):
            lid = f"{ls_id}/layer_{i}"
            layer_mat = layer.Material
            nodes[lid] = {
                "id": lid, "ifc_class": "IfcMaterialLayer",
                "LayerThickness": layer.LayerThickness,
                "MaterialName": layer_mat.Name if layer_mat else None,
                "category": "material_layer",
            }
            edges.append({"source": ls_id, "target": lid, "relation": "has_layer"})

    elif mat.is_a("IfcMaterialLayerSet"):
        nodes[mat_id] = {**mat_attrs, "Name": getattr(mat, "LayerSetName", None)}
        edges.append({"source": eid, "target": mat_id, "relation": "has_material_layer_set"})
        for i, layer in enumerate(mat.MaterialLayers):
            lid = f"{mat_id}/layer_{i}"
            layer_mat = layer.Material
            nodes[lid] = {
                "id": lid, "ifc_class": "IfcMaterialLayer",
                "LayerThickness": layer.LayerThickness,
                "MaterialName": layer_mat.Name if layer_mat else None,
                "category": "material_layer",
            }
            edges.append({"source": mat_id, "target": lid, "relation": "has_layer"})

    elif mat.is_a("IfcMaterialList"):
        for i, m in enumerate(mat.Materials):
            mid = f"{mat_id}/mat_{i}"
            nodes[mid] = {
                "id": mid, "ifc_class": "IfcMaterial",
                "Name": m.Name, "category": "material",
            }
            edges.append({"source": eid, "target": mid, "relation": "has_material"})


def _extract_placement(element, eid, nodes, edges):
    """Add placement coordinate nodes."""
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None or not placement.is_a("IfcLocalPlacement"):
        return

    rel_pl = placement.RelativePlacement
    if rel_pl is None:
        return

    loc = rel_pl.Location
    if loc is None:
        return

    pid = f"{eid}/placement"
    coords = list(loc.Coordinates)
    node = {
        "id": pid, "ifc_class": "Placement",
        "coordinates": coords,
        "category": "geometry_point",
    }

    axis = rel_pl.Axis
    if axis:
        node["axis"] = list(axis.DirectionRatios)
    ref_dir = rel_pl.RefDirection
    if ref_dir:
        node["ref_direction"] = list(ref_dir.DirectionRatios)

    nodes[pid] = node
    edges.append({"source": eid, "target": pid, "relation": "has_placement"})


def _extract_geometry(element, eid, nodes, edges):
    """Extract key geometry info: profiles, extrusions, polylines — NOT
    individual tessellation faces to keep the graph manageable."""
    rep = getattr(element, "Representation", None)
    if rep is None:
        return

    for shape_rep in rep.Representations:
        rep_id_str = f"{eid}/rep:{shape_rep.RepresentationIdentifier or 'unnamed'}"
        nodes[rep_id_str] = {
            "id": rep_id_str,
            "ifc_class": "IfcShapeRepresentation",
            "RepresentationIdentifier": shape_rep.RepresentationIdentifier,
            "RepresentationType": shape_rep.RepresentationType,
            "category": "representation",
        }
        edges.append({"source": eid, "target": rep_id_str, "relation": "has_representation"})

        for item_idx, item in enumerate(shape_rep.Items):
            item_id = f"{rep_id_str}/item_{item_idx}"

            if item.is_a("IfcExtrudedAreaSolid"):
                profile = item.SweptArea
                profile_info = {
                    "profile_type": profile.is_a(),
                }
                if hasattr(profile, "XDim"):
                    profile_info["XDim"] = profile.XDim
                if hasattr(profile, "YDim"):
                    profile_info["YDim"] = profile.YDim

                nodes[item_id] = {
                    "id": item_id,
                    "ifc_class": "IfcExtrudedAreaSolid",
                    "Depth": item.Depth,
                    "category": "geometry_solid",
                    **profile_info,
                }
                edges.append({"source": rep_id_str, "target": item_id, "relation": "has_item"})

                # Extrusion direction
                ext_dir = item.ExtrudedDirection
                if ext_dir:
                    dir_id = f"{item_id}/ext_dir"
                    nodes[dir_id] = {
                        "id": dir_id,
                        "ifc_class": "IfcDirection",
                        "direction": list(ext_dir.DirectionRatios),
                        "category": "geometry_point",
                    }
                    edges.append({"source": item_id, "target": dir_id, "relation": "extrusion_direction"})

                # Profile points (for arbitrary profiles)
                _extract_profile_points(profile, item_id, nodes, edges)

                # Extrusion position
                pos = item.Position
                if pos:
                    pos_id = f"{item_id}/position"
                    loc = pos.Location
                    pos_node = {
                        "id": pos_id,
                        "ifc_class": "IfcAxis2Placement3D",
                        "coordinates": list(loc.Coordinates) if loc else None,
                        "category": "geometry_point",
                    }
                    if pos.Axis:
                        pos_node["axis"] = list(pos.Axis.DirectionRatios)
                    if pos.RefDirection:
                        pos_node["ref_direction"] = list(pos.RefDirection.DirectionRatios)
                    nodes[pos_id] = pos_node
                    edges.append({"source": item_id, "target": pos_id, "relation": "has_position"})

            elif item.is_a("IfcPolyline"):
                points = []
                for pt in item.Points:
                    points.append(list(pt.Coordinates))
                nodes[item_id] = {
                    "id": item_id,
                    "ifc_class": "IfcPolyline",
                    "points": points,
                    "point_count": len(points),
                    "category": "geometry_curve",
                }
                edges.append({"source": rep_id_str, "target": item_id, "relation": "has_item"})

            elif item.is_a("IfcMappedItem"):
                nodes[item_id] = {
                    "id": item_id,
                    "ifc_class": "IfcMappedItem",
                    "category": "geometry_mapped",
                }
                edges.append({"source": rep_id_str, "target": item_id, "relation": "has_item"})

            elif item.is_a("IfcFaceBasedSurfaceModel"):
                face_count = 0
                for fbs in item.FbsmFaces:
                    face_count += len(fbs.CfsFaces)
                nodes[item_id] = {
                    "id": item_id,
                    "ifc_class": item.is_a(),
                    "face_count": face_count,
                    "category": "geometry_mesh",
                }
                edges.append({"source": rep_id_str, "target": item_id, "relation": "has_item"})

            else:
                nodes[item_id] = {
                    "id": item_id,
                    "ifc_class": item.is_a(),
                    "category": "geometry_other",
                }
                edges.append({"source": rep_id_str, "target": item_id, "relation": "has_item"})


def _extract_profile_points(profile, parent_id, nodes, edges):
    """Extract points from profile curves (arbitrary profiles)."""
    outer_curve = None
    if hasattr(profile, "OuterCurve"):
        outer_curve = profile.OuterCurve
    elif hasattr(profile, "Curve"):
        outer_curve = profile.Curve

    if outer_curve is None:
        return

    if hasattr(outer_curve, "Points"):
        for i, pt in enumerate(outer_curve.Points):
            pt_id = f"{parent_id}/profile_pt_{i}"
            nodes[pt_id] = {
                "id": pt_id,
                "ifc_class": "IfcCartesianPoint",
                "coordinates": list(pt.Coordinates),
                "category": "geometry_point",
            }
            edges.append({"source": parent_id, "target": pt_id, "relation": "has_profile_point"})


# -----------------------------------------------------------------------
# Batch extraction
# -----------------------------------------------------------------------

def extract_all_element_graphs(
    model: ifcopenshell.file,
    *,
    max_elements: int | None = None,
) -> list[dict]:
    """Extract element-level graphs for every building element."""
    graphs = []
    count = 0
    for el in model.by_type("IfcProduct"):
        if not _is_building_element(el):
            continue
        if el.is_a("IfcOpeningElement"):
            continue
        graphs.append(extract_element_graph(model, el))
        count += 1
        if max_elements and count >= max_elements:
            break
    return graphs


def extract_all(
    model: ifcopenshell.file,
    output_dir: str,
    *,
    max_element_graphs: int | None = None,
) -> dict[str, str]:
    """Extract all three levels and save to *output_dir*.

    Returns a dict mapping level name → file path.
    """
    os.makedirs(output_dir, exist_ok=True)
    saved: dict[str, str] = {}

    # Level 0
    bg = extract_building_graph(model)
    path = os.path.join(output_dir, "level0_building.json")
    _save_json(bg, path)
    saved["building"] = path
    logger.info("Level 0 building graph: %d nodes, %d edges → %s",
                bg["stats"]["node_count"], bg["stats"]["edge_count"], path)

    # Level 1
    storey_graphs = extract_storey_graphs(model)
    for sg in storey_graphs:
        fname = f"level1_storey_{sg['storey_name']}.json"
        path = os.path.join(output_dir, _safe_filename(fname))
        _save_json(sg, path)
        saved[f"storey_{sg['storey_name']}"] = path
        logger.info("Level 1 storey '%s': %d nodes, %d edges → %s",
                     sg["storey_name"],
                     sg["stats"]["node_count"], sg["stats"]["edge_count"], path)

    # Level 2
    elem_graphs = extract_all_element_graphs(model, max_elements=max_element_graphs)
    elem_dir = os.path.join(output_dir, "level2_elements")
    os.makedirs(elem_dir, exist_ok=True)
    for eg in elem_graphs:
        gid = eg["graph_id"]
        fname = _safe_filename(f"{gid}.json")
        path = os.path.join(elem_dir, fname)
        _save_json(eg, path)
    saved["elements_dir"] = elem_dir
    logger.info("Level 2 element graphs: %d files → %s", len(elem_graphs), elem_dir)

    # Summary
    summary = {
        "ifc_file": bg.get("ifc_file"),
        "levels": {
            "level0_building": {
                "file": saved["building"],
                "nodes": bg["stats"]["node_count"],
                "edges": bg["stats"]["edge_count"],
            },
            "level1_storeys": [
                {
                    "storey": sg["storey_name"],
                    "file": saved.get(f"storey_{sg['storey_name']}"),
                    "nodes": sg["stats"]["node_count"],
                    "edges": sg["stats"]["edge_count"],
                }
                for sg in storey_graphs
            ],
            "level2_elements": {
                "dir": elem_dir,
                "count": len(elem_graphs),
            },
        },
    }
    summary_path = os.path.join(output_dir, "graph_summary.json")
    _save_json(summary, summary_path)
    saved["summary"] = summary_path

    return saved


# -----------------------------------------------------------------------
# Internal utilities
# -----------------------------------------------------------------------

def _make_graph(graph_id, level, model, nodes, edges) -> dict:
    node_list = list(nodes.values()) if isinstance(nodes, dict) else nodes
    return {
        "graph_id": graph_id,
        "level": level,
        "ifc_file": _get_ifc_filename(model),
        "stats": {
            "node_count": len(node_list),
            "edge_count": len(edges),
            "node_types": dict(_count_by(node_list, "category")),
        },
        "nodes": node_list,
        "edges": edges,
    }


def _get_ifc_filename(model: ifcopenshell.file) -> str:
    try:
        return os.path.basename(model.wrapped_data.header.file_name.name)
    except Exception:
        pass
    try:
        h = model.header
        if hasattr(h, "file_name") and hasattr(h.file_name, "name"):
            return os.path.basename(h.file_name.name)
    except Exception:
        pass
    return "unknown.ifc"


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    c: dict[str, int] = {}
    for item in items:
        v = item.get(key, "unknown")
        c[v] = c.get(v, 0) + 1
    return c


def _safe_value(val: Any) -> Any:
    if isinstance(val, (str, int, float, bool, type(None))):
        return val
    if isinstance(val, (list, tuple)):
        return [_safe_value(v) for v in val]
    return str(val)


def _safe_filename(name: str) -> str:
    """Replace characters unsafe for filenames."""
    for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', ' '):
        name = name.replace(ch, '_')
    return name


def _save_json(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
