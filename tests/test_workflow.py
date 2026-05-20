"""
End-to-end tests for the IFC-Agent workflow.

Tests the parser, tools, command_parse, and verifier modules without requiring
LLM API keys (offline tests). LLM-dependent tests are skipped when no keys
are configured.
"""

import json
import os
import shutil
import sys
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ifc_agent.ifc_parser import IFCParser
from ifc_agent.ifc_tools import create_tool_registry, get_tools_description
from ifc_agent.command_parse import command_parse, execute_steps, parse_llm_commands
from ifc_agent.verifier import RuleBasedVerifier, format_verification_report
from ifc_agent.utils import load_prompt, inject_prompt, parse_json_response, extract_code_blocks

# Path to test IFC file
TEST_IFC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "demo_data", "1px(1).ifc",
)


def _has_test_ifc():
    return os.path.isfile(TEST_IFC)


class TestUtils(unittest.TestCase):

    def test_inject_prompt(self):
        template = "Hello <<name>>, your task is <<task>>."
        result = inject_prompt(template, name="Agent", task="count doors")
        self.assertEqual(result, "Hello Agent, your task is count doors.")

    def test_parse_json_response_dict(self):
        self.assertEqual(parse_json_response('{"a": 1}'), {"a": 1})

    def test_parse_json_response_list(self):
        self.assertEqual(parse_json_response('[1, 2]'), [1, 2])

    def test_parse_json_response_with_markdown(self):
        text = '```json\n{"tool": "delete"}\n```'
        self.assertEqual(parse_json_response(text), {"tool": "delete"})

    def test_extract_code_blocks(self):
        text = "Some text\n```python\nresult = 42\n```\nMore text"
        blocks = extract_code_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("result = 42", blocks[0])

    def test_load_prompt(self):
        prompt = load_prompt("router_prompt.txt")
        self.assertIn("<<task>>", prompt)
        self.assertIn("<<ifc_context>>", prompt)


@unittest.skipUnless(_has_test_ifc(), f"Test IFC not found: {TEST_IFC}")
class TestIFCParser(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser = IFCParser(TEST_IFC)

    def test_schema_summary(self):
        summary = self.parser.get_schema_summary()
        self.assertIn("schema", summary)
        self.assertIn("total_entities", summary)
        self.assertGreater(summary["total_entities"], 0)

    def test_spatial_structure(self):
        spatial = self.parser.get_spatial_structure()
        self.assertIn("type", spatial)
        self.assertEqual(spatial["type"], "IfcProject")

    def test_elements_by_type(self):
        walls = self.parser.get_elements_by_type("IfcWall")
        self.assertIsInstance(walls, list)

    def test_count_by_type(self):
        count = self.parser.count_by_type("IfcDoor")
        self.assertIsInstance(count, int)

    def test_serialize_context(self):
        ctx = self.parser.serialize_context()
        self.assertIsInstance(ctx, str)
        self.assertIn("IFC Schema", ctx)

    def test_list_element_types(self):
        types = self.parser.list_element_types()
        self.assertIsInstance(types, list)
        self.assertGreater(len(types), 0)


@unittest.skipUnless(_has_test_ifc(), f"Test IFC not found: {TEST_IFC}")
class TestIFCTools(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser = IFCParser(TEST_IFC)
        cls.registry = create_tool_registry(cls.parser)

    def test_registry_has_all_tools(self):
        expected = [
            "query_elements_by_type", "count_elements", "get_element_properties",
            "get_element_info", "get_spatial_structure", "get_element_material",
            "get_element_relationships", "get_storey_elements", "get_model_context",
            "delete_element", "delete_elements_by_type", "modify_property",
            "modify_element_attribute", "move_element", "copy_element",
            "modify_material", "validate_model", "save_model",
        ]
        for name in expected:
            self.assertIn(name, self.registry, f"Missing tool: {name}")

    def test_count_elements(self):
        result = self.registry["count_elements"]("IfcDoor")
        self.assertIsInstance(result, int)

    def test_get_spatial_structure(self):
        result = self.registry["get_spatial_structure"]()
        self.assertIsInstance(result, dict)

    def test_validate_model(self):
        result = self.registry["validate_model"]()
        self.assertIn("valid", result)
        self.assertIn("issues", result)

    def test_get_model_context(self):
        result = self.registry["get_model_context"](max_chars=2000)
        self.assertIsInstance(result, str)
        self.assertLessEqual(len(result), 2100)

    def test_tools_description(self):
        desc = get_tools_description(self.registry)
        self.assertIn("query_elements_by_type", desc)
        self.assertIn("delete_element", desc)


class TestCommandParse(unittest.TestCase):

    def test_parse_delete_all(self):
        commands = [{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]
        # Use a mock registry
        mock_registry = {
            "delete_elements_by_type": lambda **kwargs: "deleted",
            "validate_model": lambda: {"valid": True, "issues": []},
        }
        steps = command_parse(commands, mock_registry)
        tool_names = [s["tool_name"] for s in steps]
        self.assertIn("delete_elements_by_type", tool_names)
        self.assertIn("validate_model", tool_names)

    def test_parse_move(self):
        commands = [{"tool": "move", "input": {"guid": "abc123", "dx": 100, "dy": 0, "dz": 0}}]
        mock_registry = {
            "move_element": lambda **kwargs: "moved",
            "validate_model": lambda: {"valid": True, "issues": []},
        }
        steps = command_parse(commands, mock_registry)
        self.assertEqual(steps[0]["tool_name"], "move_element")

    def test_parse_llm_commands_json(self):
        text = '[{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]'
        result = parse_llm_commands(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "delete")

    def test_parse_llm_commands_markdown(self):
        text = '```json\n[{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]\n```'
        result = parse_llm_commands(text)
        self.assertEqual(result[0]["tool"], "delete")


@unittest.skipUnless(_has_test_ifc(), f"Test IFC not found: {TEST_IFC}")
class TestVerifier(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser = IFCParser(TEST_IFC)
        cls.verifier = RuleBasedVerifier(cls.parser)

    def test_run_all_checks(self):
        results = self.verifier.run_all_checks()
        self.assertIn("checks_run", results)
        self.assertIn("valid", results)
        self.assertGreater(results["checks_run"], 0)

    def test_format_report(self):
        results = self.verifier.run_all_checks()
        report = format_verification_report(results)
        self.assertIn("Verification Report", report)

    def test_check_project_structure(self):
        result = self.verifier.check_project_structure()
        self.assertIn("status", result)

    def test_check_duplicate_guids(self):
        result = self.verifier.check_duplicate_guids()
        self.assertIn("status", result)


@unittest.skipUnless(_has_test_ifc(), f"Test IFC not found: {TEST_IFC}")
class TestEditExecution(unittest.TestCase):
    """Test actual edit operations on a copy of the IFC file."""

    def setUp(self):
        self.temp_ifc = TEST_IFC.replace(".ifc", "_test_copy.ifc")
        shutil.copy2(TEST_IFC, self.temp_ifc)
        self.parser = IFCParser(self.temp_ifc)
        self.registry = create_tool_registry(self.parser)

    def tearDown(self):
        if os.path.exists(self.temp_ifc):
            os.remove(self.temp_ifc)

    def test_delete_all_doors(self):
        initial_count = self.registry["count_elements"]("IfcDoor")
        if initial_count == 0:
            self.skipTest("No doors in test file")

        result = self.registry["delete_elements_by_type"]("IfcDoor")
        self.assertIn("Deleted", result)

        final_count = self.registry["count_elements"]("IfcDoor")
        self.assertEqual(final_count, 0)

    def test_command_parse_and_execute(self):
        initial_doors = self.registry["count_elements"]("IfcDoor")
        if initial_doors == 0:
            self.skipTest("No doors in test file")

        commands = [{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]
        steps = command_parse(commands, self.registry)
        results = execute_steps(steps, self.registry, self.parser)

        successes = [r for r in results if r["status"] == "success"]
        self.assertGreater(len(successes), 0)


if __name__ == "__main__":
    unittest.main()
