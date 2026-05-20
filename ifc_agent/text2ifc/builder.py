"""
Deterministic BuildingGraph → IFC builder.

This is the "Programmer" of the Text2IFC pipeline, but unlike Text2BIM the
builder is **not** an LLM — it is a pure Python function that consumes the
graph and emits IFC entities via ifcopenshell.api.  The contract is:

    builder.build(graph) -> ifcopenshell.file

Idempotent and side-effect free.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

import ifcopenshell
import ifcopenshell.api

from ifc_agent.text2ifc.schemas import (
    BuildingGraph,
    StoreyNode,
    WallNode,
    OpeningNode,
    ColumnNode,
    SlabNode,
    RoofNode,
    RailingNode,
    Point2D,
    SpaceNode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class IFCBuilder:
    """Build an IFC file from a BuildingGraph.

    Usage::

        builder = IFCBuilder()
        ifc = builder.build(graph)
        builder.save("out.ifc")
    """

    def __init__(self, schema: str = "IFC4"):
        self.schema = schema
        self.model: Optional[ifcopenshell.file] = None
        self._project = None
        self._site = None
        self._building = None
        self._body_context = None
        self._storey_entities: dict[str, Any] = {}  # storey.id -> IfcBuildingStorey
        self._wall_entities: dict[str, Any] = {}    # wall.id  -> IfcWall

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, graph: BuildingGraph) -> ifcopenshell.file:
        self.schema = graph.metadata.schema
        # ifcopenshell 0.8.x uses the run() facade for all API calls
        self.model = ifcopenshell.api.run("project.create_file", version=self.schema)

        self._create_project(graph)
        self._create_site_and_building(graph)
        for storey in graph.storeys:
            self._create_storey(storey, graph.metadata)
        return self.model

    def save(self, path: str) -> str:
        if self.model is None:
            raise RuntimeError("Nothing to save: call build() first.")
        self.model.write(path)
        return path

    # ------------------------------------------------------------------
    # Project / site / building / storey skeleton
    # ------------------------------------------------------------------

    def _create_project(self, graph: BuildingGraph):
        self._project = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcProject", name=graph.metadata.project_name,
        )
        ifcopenshell.api.run(
            "unit.assign_unit", self.model,
            length={"is_metric": True, "raw": "MILLIMETERS"},
        )
        model_ctx = ifcopenshell.api.run(
            "context.add_context", self.model, context_type="Model"
        )
        self._body_context = ifcopenshell.api.run(
            "context.add_context", self.model,
            context_type="Model",
            context_identifier="Body",
            target_view="MODEL_VIEW",
            parent=model_ctx,
        )
        # Axis context — required for IfcWall.Axis representation.
        self._axis_context = ifcopenshell.api.run(
            "context.add_context", self.model,
            context_type="Model",
            context_identifier="Axis",
            target_view="GRAPH_VIEW",
            parent=model_ctx,
        )

    def _create_site_and_building(self, graph: BuildingGraph):
        self._site = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcSite", name=graph.metadata.site_name,
        )
        ifcopenshell.api.run(
            "aggregate.assign_object", self.model,
            products=[self._site], relating_object=self._project,
        )
        self._building = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcBuilding", name=graph.metadata.name,
        )
        ifcopenshell.api.run(
            "aggregate.assign_object", self.model,
            products=[self._building], relating_object=self._site,
        )

    def _create_storey(self, storey: StoreyNode, meta) -> None:
        st = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcBuildingStorey", name=storey.name,
        )
        ifcopenshell.api.run(
            "aggregate.assign_object", self.model,
            products=[st], relating_object=self._building,
        )
        try:
            st.Elevation = float(storey.elevation)
        except Exception:
            pass

        # Place storey at its elevation
        self._set_object_placement(st, (0.0, 0.0, float(storey.elevation)))
        self._storey_entities[storey.id] = st

        # 1. Walls first (openings/columns/slabs may reference them later)
        for wall in storey.walls:
            self._create_wall(wall, st, storey)

        # 2. Openings (door / window) — host walls are already built
        for opening in storey.openings:
            self._create_opening(opening, st, storey)

        # 3. Columns
        for col in storey.columns:
            self._create_column(col, st, storey)

        # 4. Slabs (floor) and roofs
        for slab in storey.slabs:
            self._create_slab(slab, st, storey, predefined=slab.predefined_type)

        for roof in storey.roofs:
            self._create_roof(roof, st, storey)

        # 5. Railings
        for rail in storey.railings:
            self._create_railing(rail, st, storey)

        # 6. Spaces (IfcSpace) — one per room, aggregated under the storey
        for space in storey.spaces:
            self._create_space(space, st, storey)

        # 7. Stair assemblies (IfcStair aggregating IfcStairFlight + landing)
        self._create_stair_assemblies(storey, st)

        # 8. Storey-level furniture (rare; mostly populated via rooms)
        for furn in getattr(storey, "furniture", []) or []:
            self._create_furniture(furn, st)

    # ------------------------------------------------------------------
    # Walls
    # ------------------------------------------------------------------

    def _create_wall(self, wall: WallNode, storey, storey_node: StoreyNode):
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcWallStandardCase", name=f"Wall-{wall.id}",
        )
        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[ent], relating_structure=storey,
        )

        # Geometry — extrude rectangle along wall direction, then place
        length = wall.length
        if length < 1e-3:
            logger.warning("Wall %s has zero length, skipping geometry.", wall.id)
            self._wall_entities[wall.id] = ent
            return

        # Wall local geometry: rectangle (length × thickness) extruded by height.
        # Local frame: X = wall axis, Y = wall thickness normal, Z = up.
        # We model the wall centred on its axis.
        body_rep = self._make_rect_extrusion(
            x_dim=length,
            y_dim=wall.thickness,
            z_dim=wall.height,
            x_center=length / 2.0,
            y_center=0.0,
        )
        # IfcWall convention: an "Axis" representation (a 2D polyline
        # along the wall's centre line) is required so downstream tools
        # can reconstruct the wall's run direction.
        axis_rep = self._make_axis_polyline_2d(length=length)
        rep_assignment = self.model.create_entity(
            "IfcProductDefinitionShape",
            Representations=[axis_rep, body_rep],
        )
        ent.Representation = rep_assignment

        # Placement: relative to the containing storey, so the wall's
        # local z=0 sits on the storey floor (storey itself already
        # carries the elevation in its own ObjectPlacement).
        angle_rad = math.atan2(wall.end[1] - wall.start[1], wall.end[0] - wall.start[0])
        self._set_object_placement(
            ent,
            location=(wall.start[0], wall.start[1], 0.0),
            rot_z_rad=angle_rad,
            relative_to=storey,
        )

        # Material — single layer for simplicity
        self._assign_single_material(ent, wall.material)
        self._wall_entities[wall.id] = ent

    # ------------------------------------------------------------------
    # Openings (door / window)
    # ------------------------------------------------------------------

    def _create_opening(self, opening: OpeningNode, storey, storey_node: StoreyNode):
        host = self._wall_entities.get(opening.host_wall)
        if host is None:
            logger.warning("Opening %s references unknown wall %s, skipping.",
                           opening.id, opening.host_wall)
            return

        # We approximate door/window by creating an IfcOpeningElement that
        # voids the wall, then create an IfcDoor/IfcWindow that fills it.
        # The opening cube is placed in wall-local frame:
        #   X: along-wall (= offset + width/2)
        #   Y: 0 (centred in wall thickness)
        #   Z: sill_height + height/2

        op_cube_rep = self._make_rect_extrusion(
            x_dim=opening.width,
            y_dim=self._wall_thickness_of(opening.host_wall, default=300.0) + 100.0,
            z_dim=opening.height,
            x_center=opening.width / 2.0,
            y_center=0.0,
        )
        op_rep_assign = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[op_cube_rep]
        )

        opening_ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcOpeningElement", name=f"Opening-{opening.id}",
        )
        opening_ent.Representation = op_rep_assign
        self._place_relative_to_wall(
            opening_ent, host,
            offset_x=opening.offset,
            offset_y=0.0,
            offset_z=opening.sill_height,
        )

        # Void relation: wall has_opening opening_ent
        try:
            ifcopenshell.api.run(
                "feature.add_feature", self.model,
                feature=opening_ent, element=host,
            )
        except Exception:
            # Direct entity-level fallback
            try:
                self.model.create_entity(
                    "IfcRelVoidsElement",
                    GlobalId=ifcopenshell.guid.new(),
                    RelatingBuildingElement=host,
                    RelatedOpeningElement=opening_ent,
                )
            except Exception:
                pass

        # Door / Window
        kind = opening.kind.lower()
        ifc_class = "IfcDoor" if kind == "door" else "IfcWindow"
        elem = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class=ifc_class, name=f"{ifc_class[3:]}-{opening.id}",
        )
        elem.OverallWidth = float(opening.width)
        elem.OverallHeight = float(opening.height)

        # Simple geometry for the door / window itself (same as opening)
        elem_rep = self._make_rect_extrusion(
            x_dim=opening.width,
            y_dim=self._wall_thickness_of(opening.host_wall, default=200.0),
            z_dim=opening.height,
            x_center=opening.width / 2.0,
            y_center=0.0,
        )
        elem.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[elem_rep]
        )
        self._place_relative_to_wall(
            elem, host,
            offset_x=opening.offset,
            offset_y=0.0,
            offset_z=opening.sill_height,
        )

        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[elem], relating_structure=storey,
        )

        # Fill relation: opening filled by door/window
        try:
            ifcopenshell.api.run(
                "feature.add_filling", self.model,
                opening=opening_ent, element=elem,
            )
        except Exception:
            try:
                self.model.create_entity(
                    "IfcRelFillsElement",
                    GlobalId=ifcopenshell.guid.new(),
                    RelatingOpeningElement=opening_ent,
                    RelatedBuildingElement=elem,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    def _create_column(self, col: ColumnNode, storey, storey_node: StoreyNode):
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcColumn", name=f"Col-{col.id}",
        )
        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[ent], relating_structure=storey,
        )
        rep = self._make_rect_extrusion(
            x_dim=col.section[0],
            y_dim=col.section[1],
            z_dim=col.height,
            x_center=0.0,
            y_center=0.0,
        )
        ent.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        )
        self._set_object_placement(
            ent, location=(col.position[0], col.position[1], 0.0),
            relative_to=storey,
        )
        self._assign_single_material(ent, col.material)

    # ------------------------------------------------------------------
    # Slabs / Roofs
    # ------------------------------------------------------------------

    def _create_slab(self, slab: SlabNode, storey, storey_node: StoreyNode,
                     predefined: str = "FLOOR"):
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcSlab", name=f"Slab-{slab.id}",
        )
        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[ent], relating_structure=storey,
        )
        try:
            ent.PredefinedType = predefined
        except Exception:
            pass

        rep = self._make_polygon_extrusion(
            boundary=slab.boundary,
            depth=slab.thickness,
        )
        ent.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        )
        self._set_object_placement(
            ent, location=(0.0, 0.0, float(slab.elevation)),
            relative_to=storey,
        )
        self._assign_single_material(ent, slab.material)

    def _create_roof(self, roof: RoofNode, storey, storey_node: StoreyNode):
        """Create an IfcRoof aggregating an IfcSlab(PredefinedType=ROOF).

        This keeps the floor-slab count distinct from the roof count and
        produces semantically richer IFC than a bare slab.
        """
        roof_ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcRoof", name=f"Roof-{roof.id}",
        )
        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[roof_ent], relating_structure=storey,
        )
        self._set_object_placement(
            roof_ent, location=(0.0, 0.0, float(roof.elevation)),
            relative_to=storey,
        )

        # Roof slab (child of IfcRoof)
        slab_like = SlabNode(
            id=f"{roof.id}-slab",
            boundary=roof.boundary,
            thickness=roof.thickness,
            elevation=roof.elevation,
            material=roof.material,
            predefined_type="ROOF",
        )
        self._create_slab(slab_like, storey, storey_node, predefined="ROOF")
        # The slab is the most recently created IfcSlab on this storey
        slab_ents = [s for s in self.model.by_type("IfcSlab")
                     if s.PredefinedType == "ROOF" or
                     (s.Name or "").endswith(slab_like.id)]
        if slab_ents:
            try:
                ifcopenshell.api.run(
                    "aggregate.assign_object", self.model,
                    products=[slab_ents[-1]], relating_object=roof_ent,
                )
            except Exception as exc:
                logger.debug("Roof→slab aggregation failed: %s", exc)

    # ------------------------------------------------------------------
    # Railings
    # ------------------------------------------------------------------

    def _create_railing(self, rail: RailingNode, storey, storey_node: StoreyNode):
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcRailing", name=f"Rail-{rail.id}",
        )
        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[ent], relating_structure=storey,
        )
        if len(rail.polyline) < 2:
            return

        # Approximate as a thin bar following the polyline
        # For simplicity, model only first segment as an extrusion.
        a, b = rail.polyline[0], rail.polyline[1]
        length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        if length < 1e-3:
            return
        rep = self._make_rect_extrusion(
            x_dim=length, y_dim=50.0, z_dim=rail.height,
            x_center=length / 2.0, y_center=0.0,
        )
        ent.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        )
        angle_rad = math.atan2(b[1] - a[1], b[0] - a[0])
        self._set_object_placement(
            ent, location=(a[0], a[1], float(rail.elevation)),
            rot_z_rad=angle_rad,
            relative_to=storey,
        )
        self._assign_single_material(ent, rail.material)

    # ------------------------------------------------------------------
    # Stair assemblies (IfcStair)
    # ------------------------------------------------------------------

    def _create_stair_assemblies(self, storey_node: StoreyNode, storey_ent) -> None:
        """Group IfcStairFlight furniture items into proper IfcStair aggregates.

        Scans every space in the storey; when a space contains one or more
        IfcStairFlight furniture nodes, an IfcStair is created and the
        flights (plus a landing slab) are aggregated under it.
        """
        for space in storey_node.spaces:
            flights = [f for f in space.furniture if f.ifc_class == "IfcStairFlight"]
            if not flights:
                continue

            stair_ent = ifcopenshell.api.run(
                "root.create_entity", self.model,
                ifc_class="IfcStair", name=f"Stair-{space.id}",
            )
            ifcopenshell.api.run(
                "spatial.assign_container", self.model,
                products=[stair_ent], relating_structure=storey_ent,
            )

            # Create each flight as a child of the IfcStair
            flight_ents: list[Any] = []
            for furn in flights:
                flight_ent = self._create_stair_flight(furn, storey_ent)
                flight_ents.append(flight_ent)

            # Create a landing slab between flights (if 2+ flights)
            if len(flight_ents) >= 2:
                self._create_stair_landing(space, flight_ents, storey_ent, stair_ent)

            # Aggregate flights under the stair
            for fe in flight_ents:
                try:
                    ifcopenshell.api.run(
                        "aggregate.assign_object", self.model,
                        products=[fe], relating_object=stair_ent,
                    )
                except Exception as exc:
                    logger.debug("Stair aggregation failed: %s", exc)

    def _create_stair_flight(self, furn, storey_ent):
        """Create a single IfcStairFlight entity from a FurnitureNode."""
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcStairFlight", name=furn.name or furn.id,
        )
        try:
            ent.PredefinedType = furn.predefined_type or "STRAIGHT"
        except Exception:
            pass

        dx, dy, dz = furn.size
        rep = self._make_rect_extrusion(
            x_dim=dx, y_dim=dy, z_dim=dz,
            x_center=0.0, y_center=0.0,
        )
        ent.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        )

        angle_rad = math.radians(furn.rot_z_deg)
        self._set_object_placement(
            ent,
            location=(float(furn.position[0]),
                      float(furn.position[1]),
                      float(furn.elevation)),
            rot_z_rad=angle_rad,
            relative_to=storey_ent,
        )

        ifcopenshell.api.run(
            "spatial.assign_container", self.model,
            products=[ent], relating_structure=storey_ent,
        )
        if furn.material:
            self._assign_single_material(ent, furn.material)
        return ent

    def _create_stair_landing(self, space, flight_ents, storey_ent, stair_ent):
        """Create a landing slab between stair flights."""
        if len(space.boundary) < 4:
            return
        # Use a reduced boundary (inset by 200mm) for the landing
        landing_boundary = list(space.boundary)
        landing = SlabNode(
            id=f"{space.id}-landing",
            boundary=landing_boundary,
            thickness=200.0,
            elevation=space.elevation,
            material="Concrete",
            predefined_type="LANDING",
        )
        self._create_slab(landing, storey_ent, None, predefined="LANDING")
        # Aggregate landing under stair
        slab_ents = [s for s in self.model.by_type("IfcSlab")
                     if (s.Name or "").endswith(landing.id)]
        if slab_ents:
            try:
                ifcopenshell.api.run(
                    "aggregate.assign_object", self.model,
                    products=[slab_ents[-1]], relating_object=stair_ent,
                )
            except Exception as exc:
                logger.debug("Landing aggregation failed: %s", exc)

    # ------------------------------------------------------------------
    # Spaces (IfcSpace per room)
    # ------------------------------------------------------------------

    def _create_space(self, space: SpaceNode, storey, storey_node: StoreyNode):
        ent = ifcopenshell.api.run(
            "root.create_entity", self.model,
            ifc_class="IfcSpace", name=f"Space-{space.id}",
        )
        if space.function:
            try:
                ent.LongName = space.function
            except Exception:
                pass
        # Provide a tidy display name for BIM viewers.
        if space.name and space.name != space.id:
            try:
                ent.Description = space.name
            except Exception:
                pass

        # Geometry: extrude the room boundary upwards to ceiling height.
        try:
            rep = self._make_polygon_extrusion(
                boundary=space.boundary, depth=float(space.height),
            )
            ent.Representation = self.model.create_entity(
                "IfcProductDefinitionShape", Representations=[rep]
            )
        except Exception:
            logger.warning("Failed to build geometry for space %s", space.id)

        # Place at storey origin (boundary is already in storey-local XY).
        self._set_object_placement(
            ent, location=(0.0, 0.0, float(space.elevation)),
            relative_to=storey,
        )

        # Aggregate IfcSpace under the storey (this is the correct
        # IFC pattern — Spaces are decomposed FROM storeys, not contained
        # in them).
        try:
            ifcopenshell.api.run(
                "aggregate.assign_object", self.model,
                products=[ent], relating_object=storey,
            )
        except Exception:
            self.model.create_entity(
                "IfcRelAggregates",
                GlobalId=ifcopenshell.guid.new(),
                RelatingObject=storey,
                RelatedObjects=[ent],
            )

        # Furniture / fixtures inside the room
        for furn in space.furniture:
            self._create_furniture(furn, storey)

    # ------------------------------------------------------------------
    # Furniture / fixtures / fittings
    # ------------------------------------------------------------------

    def _create_furniture(self, furn, storey):
        """Emit an IFC entity for a FurnitureNode (desk, chair, toilet, …).

        We don't fall over if ``furn.ifc_class`` isn't a valid IFC type —
        instead we fall back to ``IfcBuildingElementProxy`` so the model
        still validates.

        NOTE: IfcStairFlight items are handled by _create_stair_assemblies
        so they are skipped here.
        """
        if furn.ifc_class == "IfcStairFlight":
            return  # handled by _create_stair_assemblies

        ifc_class = furn.ifc_class or "IfcFurniture"
        try:
            ent = ifcopenshell.api.run(
                "root.create_entity", self.model,
                ifc_class=ifc_class, name=furn.name or furn.id,
            )
        except Exception:
            ifc_class = "IfcBuildingElementProxy"
            ent = ifcopenshell.api.run(
                "root.create_entity", self.model,
                ifc_class=ifc_class, name=furn.name or furn.id,
            )

        # PredefinedType (best-effort)
        if furn.predefined_type and furn.predefined_type != "NOTDEFINED":
            try:
                ent.PredefinedType = furn.predefined_type
            except Exception:
                pass

        # Geometry: a rectangular extrusion centred on (0,0) horizontally
        # so we can place it via the centre point.
        dx, dy, dz = furn.size
        rep = self._make_rect_extrusion(
            x_dim=dx, y_dim=dy, z_dim=dz,
            x_center=0.0, y_center=0.0,
        )
        ent.Representation = self.model.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        )

        angle_rad = math.radians(furn.rot_z_deg)
        self._set_object_placement(
            ent,
            location=(float(furn.position[0]),
                      float(furn.position[1]),
                      float(furn.elevation)),
            rot_z_rad=angle_rad,
            relative_to=storey,
        )

        # Place into the spatial structure (contained in storey).
        try:
            ifcopenshell.api.run(
                "spatial.assign_container", self.model,
                products=[ent], relating_structure=storey,
            )
        except Exception:
            pass

        if furn.material:
            self._assign_single_material(ent, furn.material)

    # ------------------------------------------------------------------
    # Helpers — geometry / placement
    # ------------------------------------------------------------------

    def _make_rect_extrusion(self, *, x_dim, y_dim, z_dim,
                             x_center=0.0, y_center=0.0):
        profile = self.model.create_entity(
            "IfcRectangleProfileDef",
            ProfileType="AREA",
            XDim=float(x_dim),
            YDim=float(y_dim),
            Position=self.model.create_entity(
                "IfcAxis2Placement2D",
                Location=self.model.create_entity(
                    "IfcCartesianPoint",
                    Coordinates=(float(x_center), float(y_center)),
                ),
            ),
        )
        ext_dir = self.model.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        )
        solid = self.model.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=self._identity_axis2placement3d(),
            ExtrudedDirection=ext_dir,
            Depth=float(z_dim),
        )
        return self.model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=self._body_context,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid],
        )

    def _make_axis_polyline_2d(self, *, length: float):
        """Build an IfcShapeRepresentation containing a 2D IfcPolyline
        along the wall's local X axis — needed so downstream tools can
        reconstruct the wall direction (gt_describer relies on it)."""
        p0 = self.model.create_entity(
            "IfcCartesianPoint", Coordinates=(0.0, 0.0),
        )
        p1 = self.model.create_entity(
            "IfcCartesianPoint", Coordinates=(float(length), 0.0),
        )
        polyline = self.model.create_entity(
            "IfcPolyline", Points=[p0, p1],
        )
        return self.model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=self._axis_context,
            RepresentationIdentifier="Axis",
            RepresentationType="Curve2D",
            Items=[polyline],
        )

    def _make_polygon_extrusion(self, *, boundary: list[Point2D], depth: float):
        # Close the boundary if user did not
        pts = list(boundary)
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        cartesian_pts = [
            self.model.create_entity("IfcCartesianPoint",
                                     Coordinates=(float(p[0]), float(p[1])))
            for p in pts
        ]
        polyline = self.model.create_entity(
            "IfcPolyline", Points=cartesian_pts
        )
        profile = self.model.create_entity(
            "IfcArbitraryClosedProfileDef",
            ProfileType="AREA",
            OuterCurve=polyline,
        )
        ext_dir = self.model.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        )
        solid = self.model.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=self._identity_axis2placement3d(),
            ExtrudedDirection=ext_dir,
            Depth=float(depth),
        )
        return self.model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=self._body_context,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid],
        )

    def _identity_axis2placement3d(self):
        return self.model.create_entity(
            "IfcAxis2Placement3D",
            Location=self.model.create_entity(
                "IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)
            ),
            Axis=self.model.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
            ),
            RefDirection=self.model.create_entity(
                "IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)
            ),
        )

    def _set_object_placement(self, product, location, rot_z_rad: float = 0.0,
                              relative_to=None):
        """Create an ``IfcLocalPlacement`` for *product*.

        Parameters
        ----------
        location : (x, y, z)
            Coordinates expressed in the frame of *relative_to* (or the
            world frame if it is ``None``).
        relative_to : IfcProduct or IfcLocalPlacement, optional
            When given, the new placement chains onto its
            ``ObjectPlacement`` (for products) or directly onto the
            placement itself.  This is what makes storey-local Z
            coordinates compose with the storey's own elevation.
        """
        x, y, z = (float(c) for c in location)
        cos_a = math.cos(rot_z_rad)
        sin_a = math.sin(rot_z_rad)
        axis = self.model.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        )
        ref_dir = self.model.create_entity(
            "IfcDirection", DirectionRatios=(cos_a, sin_a, 0.0)
        )
        placement = self.model.create_entity(
            "IfcAxis2Placement3D",
            Location=self.model.create_entity(
                "IfcCartesianPoint", Coordinates=(x, y, z)
            ),
            Axis=axis,
            RefDirection=ref_dir,
        )
        parent_placement = None
        if relative_to is not None:
            parent_placement = (getattr(relative_to, "ObjectPlacement", None)
                                or relative_to)
        kwargs = {"RelativePlacement": placement}
        if parent_placement is not None:
            kwargs["PlacementRelTo"] = parent_placement
        local = self.model.create_entity("IfcLocalPlacement", **kwargs)
        product.ObjectPlacement = local

    def _place_relative_to_wall(self, product, wall, offset_x: float,
                                offset_y: float, offset_z: float):
        """Place product at (offset_x, offset_y, offset_z) in the wall's
        local frame, by composing onto the wall's ObjectPlacement."""
        cos_a = 1.0
        sin_a = 0.0
        # Use identity rotation relative to wall; coordinates are wall-local.
        rel = self.model.create_entity(
            "IfcAxis2Placement3D",
            Location=self.model.create_entity(
                "IfcCartesianPoint",
                Coordinates=(float(offset_x), float(offset_y), float(offset_z)),
            ),
            Axis=self.model.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
            ),
            RefDirection=self.model.create_entity(
                "IfcDirection", DirectionRatios=(cos_a, sin_a, 0.0)
            ),
        )
        local = self.model.create_entity(
            "IfcLocalPlacement",
            PlacementRelTo=wall.ObjectPlacement,
            RelativePlacement=rel,
        )
        product.ObjectPlacement = local

    # ------------------------------------------------------------------
    # Helpers — material / lookups
    # ------------------------------------------------------------------

    def _assign_single_material(self, product, material_name: str):
        try:
            mat = ifcopenshell.api.run(
                "material.add_material", self.model, name=material_name,
            )
            ifcopenshell.api.run(
                "material.assign_material", self.model,
                products=[product], type="IfcMaterial", material=mat,
            )
        except Exception as exc:
            logger.debug("Material assignment failed for %s: %s", product, exc)

    def _wall_thickness_of(self, wall_id: str, default: float) -> float:
        # Look back into graph: requires the WallNode definitions to have
        # been retained.  We didn't store them by id, so use default unless
        # we extend the registry.  For now, return default.
        return default


# ---------------------------------------------------------------------------
# Convenience top-level functions
# ---------------------------------------------------------------------------

def build_ifc(graph: BuildingGraph, output_path: str,
              schema: str = "IFC4") -> str:
    """One-shot helper: build graph → IFC and write to *output_path*."""
    if not graph.metadata.schema:
        graph.metadata.schema = schema
    builder = IFCBuilder(schema=graph.metadata.schema)
    builder.build(graph)
    builder.save(output_path)
    return output_path
