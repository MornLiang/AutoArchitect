"""Deterministic room furnishing.

Given a SpaceNode (room with a rectangular boundary) and its semantic
function, populate ``space.furniture`` with FurnitureNode instances
representing desks, chairs, toilets, sinks, blackboards, etc.

Everything here is purely geometric / heuristic — no LLM call.  The
goal is to produce *plausible* layouts so the resulting IFC contains
the right second-order entities (IfcFurniture, IfcSanitaryTerminal,
IfcBuildingElementProxy) without requiring the LLM to emit pixel-level
coordinates.
"""
from __future__ import annotations

from dataclasses import replace

from ifc_agent.text2ifc.schemas import FurnitureNode, SpaceNode

# Standard component footprints (mm).  (dx along room X, dy along room Y, dz height)
_DESK = (1200.0, 500.0, 750.0)
_STUDENT_DESK = (1200.0, 600.0, 750.0)
_CHAIR = (450.0, 450.0, 450.0)
_TEACHER_DESK = (1600.0, 700.0, 750.0)
_BLACKBOARD = (4000.0, 100.0, 1200.0)        # long, thin, mounted on wall
_TOILET_PAN = (380.0, 700.0, 400.0)
_URINAL = (380.0, 400.0, 900.0)
_WASHBASIN = (550.0, 450.0, 850.0)
_BED_SINGLE = (1000.0, 2000.0, 450.0)
_BED_DOUBLE = (1800.0, 2000.0, 450.0)
_SOFA = (2000.0, 850.0, 800.0)
_DINING_TABLE = (1600.0, 800.0, 750.0)
_KITCHEN_COUNTER = (2400.0, 600.0, 900.0)
_OFFICE_DESK = (1400.0, 700.0, 750.0)
_MEETING_TABLE = (2400.0, 1200.0, 750.0)
_RECEPTION_DESK = (2000.0, 700.0, 1100.0)
_STAIR_FLIGHT = (1200.0, 3600.0, 150.0)


def furnish_room(space: SpaceNode) -> None:
    """Populate ``space.furniture`` based on ``space.function``.

    Existing entries are kept.  Idempotent: if furniture is already
    present this is a no-op.
    """
    if space.furniture:
        return

    fn = (space.function or "").lower()
    if fn in ("classroom",):
        space.furniture.extend(_furnish_classroom(space))
    elif fn in ("toilet", "bathroom", "wc"):
        space.furniture.extend(_furnish_toilet(space))
    elif fn in ("office",):
        space.furniture.extend(_furnish_office(space))
    elif fn in ("meeting", "conference"):
        space.furniture.extend(_furnish_meeting(space))
    elif fn in ("bedroom",):
        space.furniture.extend(_furnish_bedroom(space))
    elif fn in ("livingroom", "living_room"):
        space.furniture.extend(_furnish_livingroom(space))
    elif fn in ("kitchen",):
        space.furniture.extend(_furnish_kitchen(space))
    elif fn in ("diningroom", "dining_room"):
        space.furniture.extend(_furnish_diningroom(space))
    elif fn in ("lobby", "reception"):
        space.furniture.extend(_furnish_reception(space))
    elif fn in ("stairwell", "stair"):
        space.furniture.extend(_furnish_stairwell(space))
    # corridor / mechanical / storage / open_space → no furniture


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _bbox(space: SpaceNode) -> tuple[float, float, float, float]:
    xs = [p[0] for p in space.boundary]
    ys = [p[1] for p in space.boundary]
    return min(xs), min(ys), max(xs), max(ys)


def _grid_positions(x0, y0, x1, y1, item_dx, item_dy,
                    margin_x=600.0, margin_y=1500.0,
                    spacing_x=600.0, spacing_y=300.0):
    """Yield grid (cx, cy) centres for items of size ``(item_dx, item_dy)``
    inside a rectangle, with side margins and inter-item spacing."""
    cx0 = x0 + margin_x + item_dx / 2.0
    cx_step = item_dx + spacing_x
    cy0 = y0 + margin_y + item_dy / 2.0
    cy_step = item_dy + spacing_y
    cx = cx0
    while cx + item_dx / 2.0 <= x1 - margin_x:
        cy = cy0
        while cy + item_dy / 2.0 <= y1 - margin_y:
            yield cx, cy
            cy += cy_step
        cx += cx_step


# ---------------------------------------------------------------------------
# Per-function furnishers
# ---------------------------------------------------------------------------

def _furnish_classroom(space: SpaceNode) -> list[FurnitureNode]:
    """Rows of student desks + chairs facing a blackboard.

    Chinese teaching-building convention:
      * Door on the corridor side (``space.door_side``).
      * Windows on the OPPOSITE long wall.
      * Blackboard on one of the SHORT side walls (perpendicular to door),
        i.e. the wall away from any door.  Teacher desk sits in front of
        the blackboard.  Student desks face the blackboard.
    """
    out: list[FurnitureNode] = []
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id

    door = space.door_side or "-y"          # which wall hosts the door
    # Blackboard is on a side wall perpendicular to the door.  Pick the
    # one whose normal direction gives the LONGEST possible desk depth.
    if door in ("-y", "+y"):
        # Door on horizontal wall → blackboard on -x or +x (vertical wall)
        bb_side = "-x"            # arbitrary; the room is symmetric in X
        bb_normal = (1.0, 0.0)    # board normal points into the room (+x)
    else:
        # Door on vertical wall → blackboard on -y or +y
        bb_side = "-y"
        bb_normal = (0.0, 1.0)

    width = x1 - x0
    depth = y1 - y0

    # --- Blackboard geometry along the chosen side wall ---
    if bb_side == "-x":
        bb_size = (100.0, min(_BLACKBOARD[0], depth - 1200.0), _BLACKBOARD[2])
        bb_pos = (x0 + 60.0, (y0 + y1) / 2.0)
        teacher_pos = (x0 + 1200.0, (y0 + y1) / 2.0)
        teacher_rot = 90.0
        desk_rot = 90.0
        # Student desks: rows going from "+x of teacher" toward +x wall.
        student_x_start = x0 + 2400.0
        student_x_end = x1 - 600.0
        student_y_start = y0 + 600.0
        student_y_end = y1 - 600.0
        # "Along-row" axis = Y (desks line up along Y), "row-step" axis = X.
        # Each desk occupies (dy, dx) when rotated.
        along_size = _STUDENT_DESK[0]    # along Y after rotation
        cross_size = _STUDENT_DESK[1]    # along X after rotation
        rows_axis = "x"
    else:   # "-y" (default) — blackboard on south wall
        bb_size = (min(_BLACKBOARD[0], width - 1200.0), 100.0, _BLACKBOARD[2])
        bb_pos = ((x0 + x1) / 2.0, y0 + 60.0)
        teacher_pos = ((x0 + x1) / 2.0, y0 + 1200.0)
        teacher_rot = 0.0
        desk_rot = 0.0
        student_x_start = x0 + 600.0
        student_x_end = x1 - 600.0
        student_y_start = y0 + 2400.0
        student_y_end = y1 - 600.0
        along_size = _STUDENT_DESK[0]
        cross_size = _STUDENT_DESK[1]
        rows_axis = "y"

    out.append(FurnitureNode(
        id=f"{rid}_blackboard",
        ifc_class="IfcBuildingElementProxy", predefined_type="NOTDEFINED",
        name="Blackboard", position=bb_pos, size=bb_size,
        material="Slate", elevation=900.0,
    ))
    out.append(FurnitureNode(
        id=f"{rid}_teacher_desk",
        ifc_class="IfcFurniture", predefined_type="DESK",
        name="Teacher Desk", position=teacher_pos,
        size=_TEACHER_DESK, rot_z_deg=teacher_rot, material="Wood",
    ))

    # Student desks grid.
    if rows_axis == "y":
        i = 0
        cy = student_y_start
        while cy + cross_size / 2.0 <= student_y_end:
            cx = student_x_start + along_size / 2.0
            while cx + along_size / 2.0 <= student_x_end:
                i += 1
                out.append(FurnitureNode(
                    id=f"{rid}_desk_{i}",
                    ifc_class="IfcFurniture", predefined_type="DESK",
                    name=f"Student Desk {i}", position=(cx, cy),
                    size=_STUDENT_DESK, rot_z_deg=desk_rot, material="Wood",
                ))
                out.append(FurnitureNode(
                    id=f"{rid}_chair_{i}",
                    ifc_class="IfcFurniture", predefined_type="CHAIR",
                    name=f"Student Chair {i}",
                    position=(cx, cy + cross_size / 2 + 200),
                    size=_CHAIR, material="Wood",
                ))
                cx += along_size + 600.0
                if i >= 30: break
            cy += cross_size + 500.0
            if i >= 30: break
    else:   # rows_axis == "x" (rows step along +X away from blackboard)
        i = 0
        cx = student_x_start
        while cx + cross_size / 2.0 <= student_x_end:
            cy = student_y_start + along_size / 2.0
            while cy + along_size / 2.0 <= student_y_end:
                i += 1
                out.append(FurnitureNode(
                    id=f"{rid}_desk_{i}",
                    ifc_class="IfcFurniture", predefined_type="DESK",
                    name=f"Student Desk {i}", position=(cx, cy),
                    size=_STUDENT_DESK, rot_z_deg=desk_rot, material="Wood",
                ))
                out.append(FurnitureNode(
                    id=f"{rid}_chair_{i}",
                    ifc_class="IfcFurniture", predefined_type="CHAIR",
                    name=f"Student Chair {i}",
                    position=(cx + cross_size / 2 + 200, cy),
                    size=_CHAIR, material="Wood",
                ))
                cy += along_size + 600.0
                if i >= 30: break
            cx += cross_size + 500.0
            if i >= 30: break
    return out


def _furnish_toilet(space: SpaceNode) -> list[FurnitureNode]:
    """Row of toilet pans + washbasin counter on opposite wall.

    The toilet rectangle from the BSP partition can be either landscape
    (dx > dy) or portrait (dy > dx).  We pick the longest interior
    dimension as the "row" direction so the fixtures actually fit even
    when the room is narrow.
    """
    out: list[FurnitureNode] = []
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    width = x1 - x0
    depth = y1 - y0

    is_mens = "men" in (space.id + space.name).lower()

    # Decide row axis.  Each fixture group lines a side wall, so the
    # row direction should be the LONGER axis.
    if width >= depth:
        # Lay fixtures along X.  Toilet/urinal row hugs +Y wall;
        # washbasin row hugs -Y wall.
        max_count = max(1, int((width - 800.0) // 800.0))
        n_fixtures = min(max_count, 5)
        n_fixtures = max(2, n_fixtures)
        span = (n_fixtures - 1) * min(800.0, (width - 600.0) / max(1, n_fixtures - 1))
        xc0 = (x0 + x1) / 2.0 - span / 2.0
        for i in range(n_fixtures):
            if is_mens and i < max(1, n_fixtures // 2):
                kind, sz, ptype, elev = "urinal", _URINAL, "URINAL", 600.0
            else:
                kind, sz, ptype, elev = "pan", _TOILET_PAN, "TOILETPAN", 0.0
            cx = xc0 + i * (span / max(1, n_fixtures - 1) if n_fixtures > 1 else 0)
            cy = y1 - sz[1] / 2.0 - 80.0
            out.append(FurnitureNode(
                id=f"{rid}_{kind}_{i+1}",
                ifc_class="IfcSanitaryTerminal", predefined_type=ptype,
                name=f"{kind.title()} {i+1}",
                position=(cx, cy), size=sz, material="Ceramic",
                elevation=elev,
            ))
        # Washbasins on -Y wall
        n_basins = max(1, n_fixtures - 1)
        span_b = (n_basins - 1) * 750.0
        xb0 = (x0 + x1) / 2.0 - span_b / 2.0
        for i in range(n_basins):
            out.append(FurnitureNode(
                id=f"{rid}_basin_{i+1}",
                ifc_class="IfcSanitaryTerminal", predefined_type="WASHHANDBASIN",
                name=f"Washbasin {i+1}",
                position=(xb0 + i * 750.0, y0 + _WASHBASIN[1] / 2 + 80.0),
                size=_WASHBASIN, material="Ceramic", elevation=700.0,
            ))
    else:
        # Lay fixtures along Y (the long axis).  Toilet row on +X side,
        # washbasins on -X side.
        max_count = max(1, int((depth - 800.0) // 800.0))
        n_fixtures = min(max_count, 5)
        n_fixtures = max(2, n_fixtures)
        step = (depth - 600.0) / max(1, n_fixtures - 1)
        yc0 = y0 + 300.0
        for i in range(n_fixtures):
            if is_mens and i < max(1, n_fixtures // 2):
                kind = "urinal"
                sz = (_URINAL[1], _URINAL[0], _URINAL[2])  # rotate 90°
                ptype, elev = "URINAL", 600.0
            else:
                kind = "pan"
                sz = (_TOILET_PAN[1], _TOILET_PAN[0], _TOILET_PAN[2])
                ptype, elev = "TOILETPAN", 0.0
            cx = x1 - sz[0] / 2.0 - 80.0
            cy = yc0 + i * step
            out.append(FurnitureNode(
                id=f"{rid}_{kind}_{i+1}",
                ifc_class="IfcSanitaryTerminal", predefined_type=ptype,
                name=f"{kind.title()} {i+1}",
                position=(cx, cy), size=sz, material="Ceramic",
                rot_z_deg=90.0, elevation=elev,
            ))
        n_basins = max(1, n_fixtures - 1)
        step_b = (depth - 600.0) / max(1, n_basins) if n_basins > 0 else 0
        yb0 = y0 + 300.0 + step_b / 2.0
        for i in range(n_basins):
            sz_b = (_WASHBASIN[1], _WASHBASIN[0], _WASHBASIN[2])
            out.append(FurnitureNode(
                id=f"{rid}_basin_{i+1}",
                ifc_class="IfcSanitaryTerminal", predefined_type="WASHHANDBASIN",
                name=f"Washbasin {i+1}",
                position=(x0 + sz_b[0] / 2 + 80.0, yb0 + i * step_b),
                size=sz_b, material="Ceramic",
                rot_z_deg=90.0, elevation=700.0,
            ))
    return out


def _furnish_office(space: SpaceNode) -> list[FurnitureNode]:
    out: list[FurnitureNode] = []
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    i = 0
    for cx, cy in _grid_positions(
        x0, y0, x1, y1,
        item_dx=_OFFICE_DESK[0], item_dy=_OFFICE_DESK[1],
        margin_x=900.0, margin_y=900.0,
        spacing_x=900.0, spacing_y=600.0,
    ):
        i += 1
        out.append(FurnitureNode(
            id=f"{rid}_odesk_{i}",
            ifc_class="IfcFurniture", predefined_type="DESK",
            name=f"Office Desk {i}", position=(cx, cy),
            size=_OFFICE_DESK, material="Wood",
        ))
        out.append(FurnitureNode(
            id=f"{rid}_ochair_{i}",
            ifc_class="IfcFurniture", predefined_type="CHAIR",
            name=f"Office Chair {i}",
            position=(cx, cy + _OFFICE_DESK[1] / 2 + 250),
            size=_CHAIR, material="Wood",
        ))
        if i >= 6:
            break
    return out


def _furnish_meeting(space: SpaceNode) -> list[FurnitureNode]:
    out: list[FurnitureNode] = []
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    out.append(FurnitureNode(
        id=f"{rid}_table",
        ifc_class="IfcFurniture", predefined_type="TABLE",
        name="Meeting Table", position=(cx, cy),
        size=_MEETING_TABLE, material="Wood",
    ))
    # Chairs around the long sides
    n_per_side = 4
    span = _MEETING_TABLE[0]
    for i in range(n_per_side):
        x = cx - span / 2.0 + (i + 0.5) * span / n_per_side
        out.append(FurnitureNode(
            id=f"{rid}_mchair_n_{i+1}",
            ifc_class="IfcFurniture", predefined_type="CHAIR",
            name=f"Chair N{i+1}",
            position=(x, cy - _MEETING_TABLE[1] / 2 - 350),
            size=_CHAIR, material="Wood",
        ))
        out.append(FurnitureNode(
            id=f"{rid}_mchair_s_{i+1}",
            ifc_class="IfcFurniture", predefined_type="CHAIR",
            name=f"Chair S{i+1}",
            position=(x, cy + _MEETING_TABLE[1] / 2 + 350),
            size=_CHAIR, material="Wood",
        ))
    return out


def _furnish_bedroom(space: SpaceNode) -> list[FurnitureNode]:
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    cx = (x0 + x1) / 2.0
    bed_size = _BED_DOUBLE if (x1 - x0) > 3500 else _BED_SINGLE
    return [
        FurnitureNode(
            id=f"{rid}_bed", ifc_class="IfcFurniture",
            predefined_type="BED", name="Bed",
            position=(cx, y0 + bed_size[1] / 2 + 400),
            size=bed_size, material="Wood",
        ),
        FurnitureNode(
            id=f"{rid}_wardrobe", ifc_class="IfcFurniture",
            predefined_type="SHELF", name="Wardrobe",
            position=(x1 - 350, (y0 + y1) / 2),
            size=(600.0, 1800.0, 2200.0), material="Wood",
        ),
    ]


def _furnish_livingroom(space: SpaceNode) -> list[FurnitureNode]:
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [
        FurnitureNode(
            id=f"{rid}_sofa", ifc_class="IfcFurniture",
            predefined_type="SOFA", name="Sofa",
            position=(cx, y0 + _SOFA[1] / 2 + 600),
            size=_SOFA, material="Fabric",
        ),
        FurnitureNode(
            id=f"{rid}_coffee", ifc_class="IfcFurniture",
            predefined_type="TABLE", name="Coffee Table",
            position=(cx, cy),
            size=(1200.0, 600.0, 450.0), material="Wood",
        ),
    ]


def _furnish_kitchen(space: SpaceNode) -> list[FurnitureNode]:
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    return [
        FurnitureNode(
            id=f"{rid}_counter", ifc_class="IfcFurniture",
            predefined_type="TABLE", name="Kitchen Counter",
            position=((x0 + x1) / 2.0, y0 + _KITCHEN_COUNTER[1] / 2 + 100),
            size=_KITCHEN_COUNTER, material="Stone",
        ),
        FurnitureNode(
            id=f"{rid}_sink", ifc_class="IfcSanitaryTerminal",
            predefined_type="SINK", name="Kitchen Sink",
            position=((x0 + x1) / 2.0 - 600, y0 + _KITCHEN_COUNTER[1] / 2 + 100),
            size=(500.0, 450.0, 200.0), material="Steel", elevation=900.0,
        ),
    ]


def _furnish_diningroom(space: SpaceNode) -> list[FurnitureNode]:
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    items = [FurnitureNode(
        id=f"{rid}_dining_table", ifc_class="IfcFurniture",
        predefined_type="TABLE", name="Dining Table",
        position=(cx, cy), size=_DINING_TABLE, material="Wood",
    )]
    for i, (dx, dy) in enumerate([
        (-_DINING_TABLE[0] / 3, -_DINING_TABLE[1] / 2 - 350),
        (+_DINING_TABLE[0] / 3, -_DINING_TABLE[1] / 2 - 350),
        (-_DINING_TABLE[0] / 3, +_DINING_TABLE[1] / 2 + 350),
        (+_DINING_TABLE[0] / 3, +_DINING_TABLE[1] / 2 + 350),
    ]):
        items.append(FurnitureNode(
            id=f"{rid}_dchair_{i+1}", ifc_class="IfcFurniture",
            predefined_type="CHAIR", name=f"Dining Chair {i+1}",
            position=(cx + dx, cy + dy), size=_CHAIR, material="Wood",
        ))
    return items


def _furnish_reception(space: SpaceNode) -> list[FurnitureNode]:
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    return [
        FurnitureNode(
            id=f"{rid}_reception_desk", ifc_class="IfcFurniture",
            predefined_type="DESK", name="Reception Desk",
            position=((x0 + x1) / 2.0, y0 + _RECEPTION_DESK[1] / 2 + 600),
            size=_RECEPTION_DESK, material="Wood",
        ),
        FurnitureNode(
            id=f"{rid}_lobby_sofa", ifc_class="IfcFurniture",
            predefined_type="SOFA", name="Lobby Sofa",
            position=((x0 + x1) / 2.0, y1 - _SOFA[1] / 2 - 600),
            size=_SOFA, material="Fabric",
        ),
    ]


def _furnish_stairwell(space: SpaceNode) -> list[FurnitureNode]:
    """A pair of stair flights occupying the room.

    Modelled as two thin oblique slabs (IfcStairFlight); a proper
    IfcStair would aggregate them but for visualisation purposes the
    flights are enough to show "this is a stair".
    """
    x0, y0, x1, y1 = _bbox(space)
    rid = space.id
    dx = x1 - x0
    dy = y1 - y0
    if dy > dx:
        flight_dx = max(900.0, dx / 2 - 100.0)
        flight_dy = min(_STAIR_FLIGHT[1], dy - 600.0)
        return [
            FurnitureNode(
                id=f"{rid}_flight_up",
                ifc_class="IfcStairFlight", predefined_type="STRAIGHT",
                name="Stair Flight Up",
                position=(x0 + flight_dx / 2 + 200, (y0 + y1) / 2),
                size=(flight_dx, flight_dy, 1500.0), material="Concrete",
            ),
            FurnitureNode(
                id=f"{rid}_flight_dn",
                ifc_class="IfcStairFlight", predefined_type="STRAIGHT",
                name="Stair Flight Down",
                position=(x1 - flight_dx / 2 - 200, (y0 + y1) / 2),
                size=(flight_dx, flight_dy, 1500.0), material="Concrete",
            ),
        ]
    else:
        flight_dx = min(_STAIR_FLIGHT[1], dx - 600.0)
        flight_dy = max(900.0, dy / 2 - 100.0)
        return [
            FurnitureNode(
                id=f"{rid}_flight_up",
                ifc_class="IfcStairFlight", predefined_type="STRAIGHT",
                name="Stair Flight Up",
                position=((x0 + x1) / 2, y0 + flight_dy / 2 + 200),
                size=(flight_dx, flight_dy, 1500.0), material="Concrete",
            ),
            FurnitureNode(
                id=f"{rid}_flight_dn",
                ifc_class="IfcStairFlight", predefined_type="STRAIGHT",
                name="Stair Flight Down",
                position=((x0 + x1) / 2, y1 - flight_dy / 2 - 200),
                size=(flight_dx, flight_dy, 1500.0), material="Concrete",
            ),
        ]
