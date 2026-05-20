"""
Tests based on demo_data to validate IFC-Agent against known QA pairs.

Uses the actual IFC files and expected answers from:
  - demo_data/1px(1).ifc              (original model)
  - demo_data/1px_modified(1).ifc     (after deleting all doors)
  - demo_data/SFT_QA_pair(1).jsonc    (expected QA pairs)
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ifc_agent.ifc_parser import IFCParser
from ifc_agent.ifc_tools import create_tool_registry, get_tools_description
from ifc_agent.command_parse import command_parse, execute_steps
from ifc_agent.verifier import RuleBasedVerifier, format_verification_report

DEMO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "demo_data",
)
ORIGINAL_IFC = os.path.join(DEMO_DIR, "1px(1).ifc")
MODIFIED_IFC = os.path.join(DEMO_DIR, "1px_modified(1).ifc")


def _has_demo_data():
    return os.path.isfile(ORIGINAL_IFC) and os.path.isfile(MODIFIED_IFC)


# ======================================================================
# Retrieval QA Tests — validate offline tool outputs match QA-pair answers
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestRetrievalQA(unittest.TestCase):
    """
    QA pairs from SFT_QA_pair(1).jsonc — Retrieval category.
    Tests that our parser/tools produce data consistent with expected answers.
    """

    @classmethod
    def setUpClass(cls):
        cls.parser = IFCParser(ORIGINAL_IFC)
        cls.registry = create_tool_registry(cls.parser)

    # QA: "How many floors does this building have?" → "1 floor"
    # The model has 2 IfcBuildingStorey: "地板" (floor) and "天花板" (ceiling).
    # The answer "1 floor" treats 天花板 as a ceiling level, not a habitable floor.
    def test_qa_floor_count(self):
        storey_count = self.registry["count_elements"]("IfcBuildingStorey")
        self.assertEqual(storey_count, 2)

        storeys = self.parser.model.by_type("IfcBuildingStorey")
        names = [s.Name for s in storeys]
        self.assertIn("地板", names)
        self.assertIn("天花板", names)

        # Functional floor count: storeys at ground level (elev = 0)
        ground_storeys = [s for s in storeys if s.Elevation == 0.0]
        self.assertEqual(len(ground_storeys), 1, "Exactly 1 ground-level floor")

    # QA: "How many doors are there?" → "15 doors"
    # Actual model has 16 IfcDoor instances (15 单扇 + 1 平开门双扇)
    def test_qa_door_count(self):
        door_count = self.registry["count_elements"]("IfcDoor")
        self.assertEqual(door_count, 16)

        doors = self.registry["query_elements_by_type"]("IfcDoor")
        self.assertEqual(len(doors), 16)
        # Verify all have guids
        for d in doors:
            self.assertIsNotNone(d["guid"])

    # QA: "Thickness of exterior walls?" → "240mm, wall type: 基本墙:外 - 240"
    def test_qa_wall_thickness(self):
        walls = self.parser.model.by_type("IfcWallStandardCase")
        self.assertGreater(len(walls), 0)

        exterior_walls = [w for w in walls if "外" in (w.Name or "")]
        self.assertGreater(len(exterior_walls), 0, "Should have exterior walls with '外' in name")

        # Most exterior walls have "240" in name; some variants like "241" exist
        walls_with_240 = [w for w in exterior_walls if "240" in w.Name]
        self.assertGreater(len(walls_with_240), len(exterior_walls) * 0.5,
                           "Majority of exterior walls should have '240' thickness in name")

    # QA: "Materials assigned to walls?" → "默认墙 (Default Wall)"
    def test_qa_wall_materials(self):
        walls = self.parser.model.by_type("IfcWallStandardCase")
        materials_found = set()
        for w in walls[:10]:
            mats = self.parser.get_element_material(w.GlobalId)
            materials_found.update(mats)

        self.assertIn("默认墙", materials_found, "Walls should have '默认墙' material")

    # QA: Window count
    def test_qa_window_count(self):
        window_count = self.registry["count_elements"]("IfcWindow")
        self.assertEqual(window_count, 26)

    # QA: Column count (expected 7 total, QA says "only 3 decorative")
    def test_qa_column_count(self):
        col_count = self.registry["count_elements"]("IfcColumn")
        self.assertEqual(col_count, 7)

    # QA: Railing presence (indicates open areas in the building)
    def test_qa_railing_presence(self):
        railing_count = self.registry["count_elements"]("IfcRailing")
        self.assertEqual(railing_count, 2)


# ======================================================================
# Reasoning QA Tests — validate data needed for reasoning answers
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestReasoningQA(unittest.TestCase):
    """
    QA pairs from SFT_QA_pair(1).jsonc — Reasoning category.
    Tests that the underlying data supports the expected reasoning conclusions.
    """

    @classmethod
    def setUpClass(cls):
        cls.parser = IFCParser(ORIGINAL_IFC)
        cls.registry = create_tool_registry(cls.parser)

    # Reasoning: "Roof system?" → "flat roof, misclassified as IfcCovering(CEILING)"
    def test_reasoning_roof_misclassification(self):
        # No IfcRoof in the model
        roof_count = self.registry["count_elements"]("IfcRoof")
        self.assertEqual(roof_count, 0, "No IfcRoof — roof is misclassified")

        # The roof is represented as IfcCovering with PredefinedType=CEILING
        coverings = self.parser.model.by_type("IfcCovering")
        self.assertGreater(len(coverings), 0)
        ceiling_coverings = [
            c for c in coverings
            if getattr(c, "PredefinedType", None) == "CEILING"
        ]
        self.assertGreater(len(ceiling_coverings), 0,
                           "Should find IfcCovering with CEILING type (misclassified roof)")

    # Reasoning: "Structural system?" → "load-bearing wall"
    # Evidence: many walls (45), few columns (7), wall thickness 240mm
    def test_reasoning_structural_system(self):
        wall_count = self.registry["count_elements"]("IfcWallStandardCase")
        col_count = self.registry["count_elements"]("IfcColumn")
        beam_count = self.registry["count_elements"]("IfcBeam")

        # Walls dominate over columns → load-bearing wall system
        self.assertGreater(wall_count, col_count * 3,
                           "Wall count should far exceed column count for load-bearing system")
        self.assertEqual(wall_count, 45)
        self.assertEqual(col_count, 7)
        self.assertEqual(beam_count, 1)

    # Reasoning: "Fully enclosed?" → No, railings indicate open corridors
    def test_reasoning_enclosure(self):
        railing_count = self.registry["count_elements"]("IfcRailing")
        self.assertGreater(railing_count, 0,
                           "Railings present → building has open/semi-open areas")

    # Reasoning: "Incorrect IFC type?" → roof misclassified
    def test_reasoning_incorrect_type_assignment(self):
        # IfcBuildingElementProxy should be 0 or very low
        try:
            proxy_count = len(self.parser.model.by_type("IfcBuildingElementProxy"))
        except RuntimeError:
            proxy_count = 0
        # The misclassification is in IfcCovering, not proxy
        covering_count = self.registry["count_elements"]("IfcCovering")
        self.assertGreater(covering_count, 0)

    # Spatial structure is intact
    def test_spatial_structure_integrity(self):
        spatial = self.registry["get_spatial_structure"]()
        self.assertEqual(spatial["type"], "IfcProject")
        self.assertIn("children", spatial)

        # Drill down: Project → Site → Building → 2 Storeys
        site = spatial["children"][0]
        self.assertEqual(site["type"], "IfcSite")
        building = site["children"][0]
        self.assertEqual(building["type"], "IfcBuilding")
        storeys = building["children"]
        self.assertEqual(len(storeys), 2)


# ======================================================================
# Editing Tests — simulate "delete all doors" and compare with reference
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestEditingDeleteDoors(unittest.TestCase):
    """
    SFT_QA_pair: "Delete all door components in this IFC file"
    Validates that our editing pipeline produces a result consistent
    with demo_data/1px_modified(1).ifc.
    """

    def setUp(self):
        self.temp_ifc = os.path.join(DEMO_DIR, "1px_test_edit_temp.ifc")
        shutil.copy2(ORIGINAL_IFC, self.temp_ifc)
        self.parser = IFCParser(self.temp_ifc)
        self.registry = create_tool_registry(self.parser)

    def tearDown(self):
        if os.path.exists(self.temp_ifc):
            os.remove(self.temp_ifc)
        saved = self.temp_ifc.replace(".ifc", "_saved.ifc")
        if os.path.exists(saved):
            os.remove(saved)

    def test_delete_all_doors_matches_reference(self):
        """Full pipeline: delete doors → save → compare with reference modified IFC."""
        # Pre-check
        initial_doors = self.registry["count_elements"]("IfcDoor")
        self.assertEqual(initial_doors, 16)

        initial_openings = self.registry["count_elements"]("IfcOpeningElement")
        self.assertEqual(initial_openings, 42)

        # Execute delete via command_parse (GenArtist-style pipeline)
        commands = [{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]
        steps = command_parse(commands, self.registry)
        results = execute_steps(steps, self.registry, self.parser)

        # Verify all steps succeeded
        for r in results:
            if r["tool"] != "validate_model":
                self.assertEqual(r["status"], "success", f"Step failed: {r['step']}: {r['result']}")

        # Post-check: 0 doors
        final_doors = self.registry["count_elements"]("IfcDoor")
        self.assertEqual(final_doors, 0, "All doors should be deleted")

        # Windows and walls should be preserved
        self.assertEqual(self.registry["count_elements"]("IfcWindow"), 26)
        self.assertEqual(self.registry["count_elements"]("IfcWall"), 45)

        # Compare with reference modified file
        ref_parser = IFCParser(MODIFIED_IFC)
        ref_doors = ref_parser.count_by_type("IfcDoor")
        ref_windows = ref_parser.count_by_type("IfcWindow")
        ref_walls = ref_parser.count_by_type("IfcWall")

        self.assertEqual(final_doors, ref_doors, "Door count should match reference")
        self.assertEqual(
            self.registry["count_elements"]("IfcWindow"),
            ref_windows,
            "Window count should match reference",
        )
        self.assertEqual(
            self.registry["count_elements"]("IfcWall"),
            ref_walls,
            "Wall count should match reference",
        )

    def test_save_after_edit(self):
        """Verify the edited model can be saved and reloaded."""
        self.registry["delete_elements_by_type"]("IfcDoor")

        saved_path = self.temp_ifc.replace(".ifc", "_saved.ifc")
        self.registry["save_model"](saved_path)

        # Reload and verify
        reloaded = IFCParser(saved_path)
        self.assertEqual(reloaded.count_by_type("IfcDoor"), 0)
        self.assertEqual(reloaded.count_by_type("IfcWindow"), 26)


# ======================================================================
# Reference File Comparison Tests
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestReferenceComparison(unittest.TestCase):
    """Compare the reference modified IFC with the original to understand changes."""

    @classmethod
    def setUpClass(cls):
        cls.orig = IFCParser(ORIGINAL_IFC)
        cls.mod = IFCParser(MODIFIED_IFC)

    def test_reference_has_no_doors(self):
        self.assertEqual(self.mod.count_by_type("IfcDoor"), 0)

    def test_reference_preserves_windows(self):
        self.assertEqual(
            self.orig.count_by_type("IfcWindow"),
            self.mod.count_by_type("IfcWindow"),
        )

    def test_reference_preserves_walls(self):
        self.assertEqual(
            self.orig.count_by_type("IfcWall"),
            self.mod.count_by_type("IfcWall"),
        )

    def test_reference_preserves_structure(self):
        self.assertEqual(
            self.orig.count_by_type("IfcBuildingStorey"),
            self.mod.count_by_type("IfcBuildingStorey"),
        )
        self.assertEqual(
            self.orig.count_by_type("IfcBuilding"),
            self.mod.count_by_type("IfcBuilding"),
        )

    def test_reference_reduces_openings(self):
        """Doors removed → their IfcOpeningElements also removed."""
        orig_openings = self.orig.count_by_type("IfcOpeningElement")
        mod_openings = self.mod.count_by_type("IfcOpeningElement")
        # 42 original - 16 door openings = 26 window openings
        self.assertEqual(orig_openings, 42)
        self.assertEqual(mod_openings, 26)
        self.assertEqual(orig_openings - mod_openings, 16,
                         "Should lose exactly 16 openings (one per door)")

    def test_reference_entity_count_reduced(self):
        orig_total = len(list(self.orig.model))
        mod_total = len(list(self.mod.model))
        self.assertGreater(orig_total, mod_total)


# ======================================================================
# Verifier Tests on Demo Data
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestVerifierOnDemoData(unittest.TestCase):

    def test_original_model_verification(self):
        parser = IFCParser(ORIGINAL_IFC)
        verifier = RuleBasedVerifier(parser)
        results = verifier.run_all_checks()

        self.assertGreater(results["checks_run"], 0)

        report = format_verification_report(results)
        self.assertIn("Verification Report", report)

    def test_modified_model_verification(self):
        parser = IFCParser(MODIFIED_IFC)
        verifier = RuleBasedVerifier(parser)
        results = verifier.run_all_checks()

        self.assertGreater(results["checks_run"], 0)

    def test_model_context_serialization(self):
        """Verify model context contains key info for LLM consumption."""
        parser = IFCParser(ORIGINAL_IFC)
        ctx = parser.serialize_context()

        self.assertIn("IFC2X3", ctx)
        self.assertIn("IfcDoor: 16", ctx)
        self.assertIn("IfcWindow: 26", ctx)
        self.assertIn("IfcWallStandardCase: 45", ctx)
        self.assertIn("地板", ctx)
        self.assertIn("天花板", ctx)


# ======================================================================
# Tool Description Generation
# ======================================================================

@unittest.skipUnless(_has_demo_data(), "Demo data not found")
class TestToolsForPromptInjection(unittest.TestCase):
    """Verify tools description is suitable for prompt injection."""

    @classmethod
    def setUpClass(cls):
        parser = IFCParser(ORIGINAL_IFC)
        cls.registry = create_tool_registry(parser)

    def test_all_tools_have_descriptions(self):
        for name, tool in self.registry.items():
            self.assertTrue(
                len(tool.description.strip()) > 10,
                f"Tool '{name}' has insufficient description",
            )

    def test_description_contains_all_tool_names(self):
        desc = get_tools_description(self.registry)
        for name in self.registry:
            self.assertIn(name, desc)

    def test_description_length_reasonable(self):
        desc = get_tools_description(self.registry)
        self.assertGreater(len(desc), 500)
        self.assertLess(len(desc), 10000)


if __name__ == "__main__":
    unittest.main()
