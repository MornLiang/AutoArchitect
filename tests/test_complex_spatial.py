from ifc_agent.text2ifc.design_reviewer import review
from ifc_agent.text2ifc.expander import expand_spatial_to_geometric
from ifc_agent.text2ifc.schemas import SpatialGraph


def test_spatial_v3_schema_round_trip_defaults():
    sg = SpatialGraph.from_dict({
        "footprint": {"shape": "L", "x_mm": 20000, "y_mm": 16000},
        "storeys": [{"id": "s1", "name": "Ground", "elevation_mm": 0}],
    })

    assert sg.footprint.boundary == []
    assert sg.structural_system.kind == "frame"
    assert sg.shafts == []
    assert sg.storeys[0].shaft_ids == []


def test_shaft_expands_to_walls_and_stair_proxy():
    sg = SpatialGraph.from_dict({
        "footprint": {"shape": "rectangle", "x_mm": 12000, "y_mm": 9000},
        "shafts": [{
            "id": "stair_1",
            "kind": "stair",
            "storey_ids": ["s1", "s2"],
            "footprint": [[500, 500], [2500, 500], [2500, 3500], [500, 3500]],
        }],
        "storeys": [
            {"id": "s1", "name": "Ground", "elevation_mm": 0, "shaft_ids": ["stair_1"]},
            {"id": "s2", "name": "Upper", "elevation_mm": 3000, "shaft_ids": ["stair_1"]},
        ],
    })

    graph = expand_spatial_to_geometric(sg, centered=False)

    assert all(any("stair_1-wall" in w.id for w in s.walls) for s in graph.storeys)
    assert all(any(f.ifc_class == "IfcStair" for f in s.furniture) for s in graph.storeys)


def test_structural_system_generates_frame_columns_when_target_missing():
    sg = SpatialGraph.from_dict({
        "footprint": {"shape": "rectangle", "x_mm": 12000, "y_mm": 12000},
        "structural_system": {
            "kind": "frame",
            "grid_spacing_x_mm": 6000,
            "grid_spacing_y_mm": 6000,
        },
        "storeys": [{
            "id": "s1",
            "name": "Ground",
            "elevation_mm": 0,
            "elements": {"walls": 4, "slabs": 1},
        }],
    })

    graph = expand_spatial_to_geometric(sg, centered=False)

    assert len(graph.storeys[0].columns) >= 4


def test_design_reviewer_flags_missing_stairs_for_multistorey():
    sg = SpatialGraph.from_dict({
        "footprint": {"shape": "rectangle", "x_mm": 10000, "y_mm": 8000},
        "storeys": [
            {"id": "s1", "name": "Ground", "elevation_mm": 0},
            {"id": "s2", "name": "Upper", "elevation_mm": 3000},
        ],
    })
    graph = expand_spatial_to_geometric(sg)

    report = review(sg, graph)

    assert any(i.rule_id == "NO_STAIRS" for i in report.issues)
