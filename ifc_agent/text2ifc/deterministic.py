"""
Deterministic, no-LLM stand-ins for the three Text2IFC LLM agents.

They exist so the pipeline (and the GT-aware iteration loop) can run
end-to-end on machines without an LLM quota.  They are also useful as a
strong baseline for ablation experiments: the LLM only earns its keep if
it beats these heuristics.

DeterministicArchitect now mirrors the LLM Architect: it produces a
SpatialGraph from the requirements doc, then delegates to the shared
expander to materialise coordinates.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ifc_agent.text2ifc.expander import expand_spatial_to_geometric
from ifc_agent.text2ifc.schemas import (
    BuildingGraph,
    BuildingMetadata,
    Footprint,
    RoomNode,
    SpatialGraph,
    SpatialStorey,
    StoreyElements,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DeterministicAnalyst — regex parsing of the prompt
# ---------------------------------------------------------------------------

class DeterministicAnalyst:
    """Extract structured requirements from a freeform description.

    Used as a fallback when the LLM is unavailable.  Recognises patterns
    like::

        - "2-storey", "3-story"
        - "footprint ... 15m × 24m"
        - "floor-to-floor height of 2.68m"
        - "containing 45 walls, 16 doors, 26 windows, 7 columns, ..."
        - "built primarily with <material>"
    """

    def expand(self, description: str) -> dict:
        text = description.lower()

        storey_count = _first_int(
            text, r"(\d+)\s*[-\s]*(storey|stor[ey]ies|story|stories|floors?|层)"
        ) or 1

        # Footprint: "15m x 24m" or "15m × 24m" or "15 m by 24 m"
        fp = re.search(
            r"(\d+(?:\.\d+)?)\s*m\s*[x×by]+\s*(\d+(?:\.\d+)?)\s*m",
            text,
        )
        if fp:
            fx, fy = float(fp.group(1)) * 1000, float(fp.group(2)) * 1000
        else:
            fx, fy = 10000.0, 8000.0

        # Floor-to-floor height
        sh = re.search(
            r"floor[- ]to[- ]floor\s+(?:height\s+of\s+)?(\d+(?:\.\d+)?)\s*m",
            text,
        )
        if sh:
            storey_height = float(sh.group(1)) * 1000
        else:
            storey_height = 3000.0

        # Element counts
        def _count(token: str) -> int:
            return _first_int(text, rf"(\d+)\s+{token}") or 0

        walls   = _count(r"wall(?:s)?")
        doors   = _count(r"door(?:s)?")
        windows = _count(r"window(?:s)?")
        columns = _count(r"column(?:s)?")
        slabs   = _count(r"(?:floor\s+)?slab(?:s)?(?:\(s\))?")
        roofs   = 1 if "roof" in text else 0
        railings = _count(r"railing(?:s)?")

        # Material
        mat = re.search(r"built\s+primarily\s+with\s+([^\n.,;]+)", text)
        wall_mat = mat.group(1).strip() if mat else "Concrete"

        return {
            "building_type": "Inferred building",
            "storey_count": int(storey_count),
            "storey_height_mm": int(storey_height),
            "footprint": {
                "shape": "rectangle",
                "x_mm": int(fx),
                "y_mm": int(fy),
            },
            "element_targets": {
                "walls": int(walls),
                "doors": int(doors),
                "windows": int(windows),
                "columns": int(columns),
                "slabs": max(1, int(slabs)),
                "roofs": int(roofs),
                "railings": int(railings),
            },
            "materials": {
                "wall": wall_mat,
                "slab": wall_mat,
                "roof": wall_mat,
            },
            "layout_notes": [],
            "assumptions": ["Parsed via deterministic regex; counts taken literally."],
        }


# ---------------------------------------------------------------------------
# DeterministicArchitect — requirements → SpatialGraph (with rooms) →
# BuildingGraph
# ---------------------------------------------------------------------------

class DeterministicArchitect:
    """Build a rooms-based SpatialGraph from a requirements dict.

    Strategy (a "central corridor" architectural template):
      - storey 1 carries all rooms; the topmost storey is a pure
        non-inhabited roof level,
      - rooms = 1 lobby + 1 corridor + N offices + (optionally) 1 meeting
        + 1 bathroom + 1 service, where N is chosen so the total room
        count yields the requested wall count via the wall budget rule
        (every adjacency-pair short-wall + every exterior face = 1 wall),
      - the heavy lifting (coordinates) is delegated to ``expander``.

    NOTE: the legacy ``elements.walls`` count is still attached to the
    SpatialStorey for back-compat with comparator/score, but the expander
    ignores it when ``rooms`` is non-empty.
    """

    def design(self, requirements: dict, *,
               refinement: str = "") -> BuildingGraph:
        sg = self.design_spatial(requirements, refinement=refinement)
        return expand_spatial_to_geometric(sg)

    def design_spatial(self, requirements: dict, *,
                       refinement: str = "") -> SpatialGraph:
        requirements = _apply_refinement_to_targets(requirements, refinement)

        meta = BuildingMetadata(
            name=str(requirements.get("building_type", "Generated Building")),
            description="Deterministically generated rooms-based graph",
            project_name="Text2IFC",
            schema="IFC4",
        )

        n_storeys = max(1, int(requirements.get("storey_count", 1)))
        storey_height = float(requirements.get("storey_height_mm", 3000))
        fp_raw = requirements.get("footprint", {}) or {}
        footprint = Footprint(
            shape=str(fp_raw.get("shape", "rectangle")),
            x_mm=float(fp_raw.get("x_mm", 10000)),
            y_mm=float(fp_raw.get("y_mm", 8000)),
        )

        targets = requirements.get("element_targets", {}) or {}
        total_walls   = max(4, int(targets.get("walls", 4)))
        total_doors   = int(targets.get("doors", 1))
        total_windows = int(targets.get("windows", 4))
        total_columns = int(targets.get("columns", 0))
        total_slabs   = max(1, int(targets.get("slabs", 1)))
        n_roofs       = int(targets.get("roofs", 0))
        n_railings    = int(targets.get("railings", 0))

        materials = requirements.get("materials", {}) or {}
        wall_mat = str(materials.get("wall", "Concrete"))
        slab_mat = str(materials.get("slab", wall_mat))
        roof_mat = str(materials.get("roof", wall_mat))

        # Decide which storey is "inhabited" (rooms) and which is the
        # roof level.  Pattern: all inhabited storeys carry rooms; the
        # last storey is a pure roof level only if the GT-style
        # description suggests so (storey_count == 2 → split as
        # 1 inhabited + 1 roof; otherwise → all inhabited except the
        # very last).
        inhabited = [True] * n_storeys
        if n_storeys >= 2:
            inhabited[-1] = False  # topmost = roof level

        # Per-storey window/door budget: concentrate on inhabited storeys.
        n_inhab = sum(inhabited) or 1
        win_per = _split_among(total_windows, inhabited)
        door_per = _split_among(total_doors,  inhabited)
        col_per  = _split_among(total_columns, inhabited)
        rail_per = _split_among(n_railings,    inhabited)
        slab_per = _split_among(total_slabs,   inhabited, force_first=True)
        roof_per = [0] * n_storeys
        if n_roofs > 0:
            roof_per[-1] = n_roofs

        storeys: list[SpatialStorey] = []
        for i in range(n_storeys):
            elements = StoreyElements(
                walls=total_walls if inhabited[i] else 0,  # legacy
                doors=door_per[i],
                windows=win_per[i],
                columns=col_per[i],
                slabs=slab_per[i],
                roofs=roof_per[i],
                railings=rail_per[i],
                wall_material=wall_mat,
                slab_material=slab_mat,
                roof_material=roof_mat,
                column_material=wall_mat,
            )
            rooms = (
                _make_central_corridor_rooms(
                    win_per[i], door_per[i],
                )
                if inhabited[i] else []
            )
            storeys.append(SpatialStorey(
                id=f"s{i + 1}",
                name=("Ground Floor" if i == 0 and inhabited[i]
                      else "Roof Level" if not inhabited[i]
                      else f"Storey {i + 1}"),
                elevation_mm=i * storey_height,
                height_mm=storey_height if inhabited[i] else 0.0,
                is_inhabited=inhabited[i],
                rooms=rooms,
                elements=elements,
                layout_hint="central_corridor" if inhabited[i] else "empty",
            ))

        return SpatialGraph(metadata=meta, footprint=footprint, storeys=storeys)


# ---------------------------------------------------------------------------
# Room-template generators
# ---------------------------------------------------------------------------

def _make_central_corridor_rooms(
    n_windows: int, n_doors: int,
) -> list[RoomNode]:
    """Build a `central_corridor` room layout.

    Layout sketch (cross-section is irrelevant — only the topology is
    used by the BSP partitioner)::

        +--------+--------+--------+--------+
        | office | office | office | office |   <- north row
        +--------+--------+--------+--------+
        |               corridor            |   <- east/west passage
        +--------+--------+--------+--------+
        | office | office | office | office |   <- south row
        +--------+--------+--------+--------+

    Plus a small lobby + bathroom + service block near one end.

    The number of offices is sized so each office gets ~2 windows; we
    add a small lobby (1 external door) and a corridor (the "core").
    """
    # ~2 windows per office, capped to 8 offices each row
    offices_per_row = max(2, min(8, n_windows // 4 if n_windows >= 8
                                  else max(1, n_windows // 2)))

    rooms: list[RoomNode] = []

    # Corridor — central spine
    rooms.append(RoomNode(
        id="corridor",
        name="Corridor",
        function="corridor",
        area_ratio=0.18,
        adjacent_to=[],   # filled below
        opening_to=[],    # interior doors emitted via opening_to
        has_external_facade=False,
        n_windows=0,
    ))

    # Lobby + service + bathroom near west end (also external)
    rooms.append(RoomNode(
        id="lobby",
        name="Lobby",
        function="lobby",
        area_ratio=0.06,
        adjacent_to=["corridor"],
        opening_to=["corridor"],
        has_external_facade=True,
        n_windows=1,
        n_external_doors=max(1, n_doors // 8),
    ))
    rooms.append(RoomNode(
        id="bathroom",
        name="Bathroom",
        function="bathroom",
        area_ratio=0.05,
        adjacent_to=["corridor"],
        opening_to=["corridor"],
        has_external_facade=False,
        n_windows=0,
    ))
    rooms.append(RoomNode(
        id="service",
        name="Service",
        function="service",
        area_ratio=0.04,
        adjacent_to=["corridor"],
        opening_to=["corridor"],
        has_external_facade=False,
        n_windows=0,
    ))
    rooms.append(RoomNode(
        id="meeting",
        name="Meeting Room",
        function="meeting",
        area_ratio=0.08,
        adjacent_to=["corridor"],
        opening_to=["corridor"],
        has_external_facade=True,
        n_windows=2,
    ))

    # Offices distributed north + south of corridor.  We size their
    # window counts so the SUM of n_windows across rooms equals
    # the target n_windows exactly (lobby + meeting already consume 3).
    n_offices = offices_per_row * 2
    remaining_windows = max(0, n_windows - 3)
    base, extra = divmod(remaining_windows, max(1, n_offices))

    for i in range(offices_per_row):
        rooms.append(RoomNode(
            id=f"office_n{i + 1}",
            name=f"Office N{i + 1}",
            function="office",
            area_ratio=0.04,
            adjacent_to=["corridor"],
            opening_to=["corridor"],
            has_external_facade=True,
            n_windows=base + (1 if (2 * i) < extra else 0),
        ))
        rooms.append(RoomNode(
            id=f"office_s{i + 1}",
            name=f"Office S{i + 1}",
            function="office",
            area_ratio=0.04,
            adjacent_to=["corridor"],
            opening_to=["corridor"],
            has_external_facade=True,
            n_windows=base + (1 if (2 * i + 1) < extra else 0),
        ))

    # Bring corridor.adjacent_to in sync (it should mirror every other
    # room's adjacency — defensive, in case the expander relies on it).
    for r in rooms:
        if r.id != "corridor" and "corridor" in r.adjacent_to:
            rooms[0].adjacent_to.append(r.id)
            rooms[0].opening_to.append(r.id)

    return rooms


def _split_among(total: int, mask: list[bool], *,
                 force_first: bool = False) -> list[int]:
    """Distribute *total* across slots flagged ``True`` in *mask*.

    With ``force_first=True``, the first True slot consumes everything.
    """
    n = len(mask)
    out = [0] * n
    if total <= 0 or not any(mask):
        return out
    idxs = [i for i, m in enumerate(mask) if m]
    if force_first:
        out[idxs[0]] = total
        return out
    base, rem = divmod(total, len(idxs))
    for k, i in enumerate(idxs):
        out[i] = base + (1 if k < rem else 0)
    return out


# ---------------------------------------------------------------------------
# DeterministicRefiner — adjust target counts toward the GT
# ---------------------------------------------------------------------------

class DeterministicRefiner:
    """Generate a refinement note that the deterministic Architect uses to
    bring the next iteration closer to the GT.

    The note is a simple imperative line per discrepancy, e.g.
    ``set walls to 45``.
    """

    def refine(self, comparison_summary: str) -> str:
        bullets: list[str] = []

        # 1) GT-comparison style: ``[severity] field: gt=A, generated=B``
        for m in re.finditer(
            r"\[(\w+)\]\s+(\w+):\s*gt=(-?\d+(?:\.\d+)?),\s*generated=(-?\d+(?:\.\d+)?)",
            comparison_summary,
        ):
            severity, field, gt, _gen = m.groups()
            bullets.append(f"set {field} to {gt}  # severity={severity}")

        # 2) IDS "Target count" warnings: ``Target count: walls: target=45, generated=53``
        for m in re.finditer(
            r"Target count:\s*(\w+):\s*target=(-?\d+(?:\.\d+)?),\s*"
            r"generated=(-?\d+(?:\.\d+)?)",
            comparison_summary,
        ):
            field, tgt, _gen = m.groups()
            # Re-map plural keys to *_count canonical names used by Architect
            field_key = {
                "walls":    "wall_count",
                "doors":    "door_count",
                "windows":  "window_count",
                "columns":  "column_count",
                "slabs":    "slab_count",
                "roofs":    "roof_count",
                "railings": "railing_count",
            }.get(field, field)
            bullets.append(f"set {field_key} to {tgt}  # ids-target")

        # 3) Generic IDS errors (presence / attribute / material) — emit a
        # textual instruction the LLM Architect can act on, but the
        # deterministic Architect already does the right thing.
        for m in re.finditer(
            r"\[ERROR\]\s+At least one (.+?) must exist",
            comparison_summary,
        ):
            cls = m.group(1).strip()
            bullets.append(f"# ensure presence of {cls}")

        if not bullets:
            return "All metrics within tolerance — no refinement needed."
        return "Refinement directives:\n" + "\n".join(bullets)


# ---------------------------------------------------------------------------
# Refinement application: parse "set X to N" and inject into requirements
# ---------------------------------------------------------------------------

_FIELD_TO_REQ_PATH: dict[str, tuple[str, ...]] = {
    "storey_count":      ("storey_count",),
    "wall_count":        ("element_targets", "walls"),
    "door_count":        ("element_targets", "doors"),
    "window_count":      ("element_targets", "windows"),
    "column_count":      ("element_targets", "columns"),
    "slab_count":        ("element_targets", "slabs"),
    "railing_count":     ("element_targets", "railings"),
    "roof_count":        ("element_targets", "roofs"),
    "storey_height_mm":  ("storey_height_mm",),
    "footprint_x_mm":    ("footprint", "x_mm"),
    "footprint_y_mm":    ("footprint", "y_mm"),
}


def _apply_refinement_to_targets(req: dict, refinement: str) -> dict:
    """Parse ``set <field> to <value>`` lines and update *req* in place."""
    if not refinement:
        return req
    out = _deep_copy_dict(req)
    for m in re.finditer(
        r"set\s+(\w+)\s+to\s+(-?\d+(?:\.\d+)?)", refinement, re.IGNORECASE
    ):
        field, value_s = m.groups()
        path = _FIELD_TO_REQ_PATH.get(field)
        if not path:
            continue
        value = float(value_s)
        if value == int(value):
            value = int(value)
        _set_nested(out, path, value)
    return out


def _deep_copy_dict(d: dict) -> dict:
    import copy
    return copy.deepcopy(d)


def _set_nested(d: dict, path: tuple[str, ...], value):
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = value


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _first_int(text: str, pattern: str) -> Optional[int]:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None


def _first_only(total: int, n_buckets: int) -> list[int]:
    """Place ``total`` on the first bucket and 0 on the rest."""
    if n_buckets <= 0:
        return []
    return [int(total)] + [0] * (n_buckets - 1)


def _distribute(total: int, n_buckets: int, *,
                weight_first: bool = False) -> list[int]:
    """Distribute *total* items across *n_buckets*.

    With weight_first=True, the first bucket gets a larger share so the
    ground floor accumulates the bulk of the elements (matching the GT
    demo, where storey 2 only contains the roof).
    """
    if n_buckets <= 0 or total <= 0:
        return [0] * max(n_buckets, 1)

    if weight_first and n_buckets > 1:
        # Concentrate small totals on the first bucket
        if total <= n_buckets:
            return [total] + [0] * (n_buckets - 1)
        # Otherwise: first gets >= half, rest split evenly
        first = total - (n_buckets - 1)
        rest_total = total - first
        rest = [rest_total // (n_buckets - 1)] * (n_buckets - 1)
        for i in range(rest_total % (n_buckets - 1)):
            rest[i] += 1
        return [first] + rest

    base = total // n_buckets
    out = [base] * n_buckets
    for i in range(total % n_buckets):
        out[i] += 1
    return out


