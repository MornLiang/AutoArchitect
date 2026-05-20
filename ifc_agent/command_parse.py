"""
Command translation layer (GenArtist-style).

Translates high-level abstract commands (from LLM planner output) into
sequences of low-level tool calls that can be executed against the IFC model.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ifc_agent.ifc_tools import ToolBase

logger = logging.getLogger(__name__)


def command_parse(
    commands: list[dict],
    tool_registry: dict[str, ToolBase],
) -> list[dict]:
    """
    Expand high-level edit commands into executable tool-call sequences.

    Each high-level command is a dict like:
        {"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}

    Returns a list of dicts, each with:
        {"tool_name": str, "tool_fn": callable, "kwargs": dict, "description": str}
    """
    steps: list[dict] = []

    for cmd in commands:
        tool_type = cmd.get("tool", "")
        inp = cmd.get("input", {})

        if tool_type == "delete":
            steps.extend(_expand_delete(inp, tool_registry))
        elif tool_type == "delete_single":
            steps.extend(_expand_delete_single(inp, tool_registry))
        elif tool_type == "modify_property":
            steps.extend(_expand_modify_property(inp, tool_registry))
        elif tool_type == "modify_attribute":
            steps.extend(_expand_modify_attribute(inp, tool_registry))
        elif tool_type == "move":
            steps.extend(_expand_move(inp, tool_registry))
        elif tool_type == "copy":
            steps.extend(_expand_copy(inp, tool_registry))
        elif tool_type == "modify_material":
            steps.extend(_expand_modify_material(inp, tool_registry))
        else:
            logger.warning(f"Unknown command type: {tool_type}")

    # Always append a validation step
    if "validate_model" in tool_registry:
        steps.append({
            "tool_name": "validate_model",
            "tool_fn": tool_registry["validate_model"],
            "kwargs": {},
            "description": "Validate model after edits",
        })

    return steps


# ---------------------------------------------------------------------------
# Expansion helpers
# ---------------------------------------------------------------------------

def _expand_delete(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    target = inp.get("target", "")
    scope = inp.get("scope", "all")

    if scope == "all":
        return [{
            "tool_name": "delete_elements_by_type",
            "tool_fn": registry["delete_elements_by_type"],
            "kwargs": {"ifc_type": target},
            "description": f"Delete all {target} elements",
        }]
    elif scope == "single":
        guid = inp.get("guid", "")
        return [{
            "tool_name": "delete_element",
            "tool_fn": registry["delete_element"],
            "kwargs": {"guid": guid},
            "description": f"Delete single element {guid}",
        }]
    return []


def _expand_delete_single(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    guid = inp.get("guid", "")
    return [{
        "tool_name": "delete_element",
        "tool_fn": registry["delete_element"],
        "kwargs": {"guid": guid},
        "description": f"Delete element {guid}",
    }]


def _expand_modify_property(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    target = inp.get("target", "")
    scope = inp.get("scope", "all")
    pset = inp.get("pset_name", "")
    prop = inp.get("property_name", "")
    val = inp.get("new_value")

    if scope == "single":
        guid = inp.get("guid", "")
        return [{
            "tool_name": "modify_property",
            "tool_fn": registry["modify_property"],
            "kwargs": {"guid": guid, "pset_name": pset, "property_name": prop, "new_value": val},
            "description": f"Modify {pset}.{prop} on {guid}",
        }]

    # scope == "all": need query-then-modify pattern
    # Return a special "batch" step that the executor handles
    return [{
        "tool_name": "_batch_modify_property",
        "tool_fn": None,  # handled by executor
        "kwargs": {
            "ifc_type": target,
            "pset_name": pset,
            "property_name": prop,
            "new_value": val,
        },
        "description": f"Modify {pset}.{prop} on all {target}",
    }]


def _expand_modify_attribute(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    target = inp.get("target", "")
    scope = inp.get("scope", "all")
    attr = inp.get("attribute_name", "")
    val = inp.get("new_value")

    if scope == "single":
        guid = inp.get("guid", "")
        return [{
            "tool_name": "modify_element_attribute",
            "tool_fn": registry["modify_element_attribute"],
            "kwargs": {"guid": guid, "attribute_name": attr, "new_value": val},
            "description": f"Modify {attr} on {guid}",
        }]

    return [{
        "tool_name": "_batch_modify_attribute",
        "tool_fn": None,
        "kwargs": {
            "ifc_type": target,
            "attribute_name": attr,
            "new_value": val,
        },
        "description": f"Modify {attr} on all {target}",
    }]


def _expand_move(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    return [{
        "tool_name": "move_element",
        "tool_fn": registry["move_element"],
        "kwargs": {
            "guid": inp.get("guid", ""),
            "dx": inp.get("dx", 0.0),
            "dy": inp.get("dy", 0.0),
            "dz": inp.get("dz", 0.0),
        },
        "description": f"Move element {inp.get('guid', '')} by ({inp.get('dx', 0)}, {inp.get('dy', 0)}, {inp.get('dz', 0)})",
    }]


def _expand_copy(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    return [{
        "tool_name": "copy_element",
        "tool_fn": registry["copy_element"],
        "kwargs": {"guid": inp.get("guid", "")},
        "description": f"Copy element {inp.get('guid', '')}",
    }]


def _expand_modify_material(inp: dict, registry: dict[str, ToolBase]) -> list[dict]:
    target = inp.get("target", "")
    scope = inp.get("scope", "all")
    new_mat = inp.get("new_material_name", "")

    if scope == "single":
        guid = inp.get("guid", "")
        return [{
            "tool_name": "modify_material",
            "tool_fn": registry["modify_material"],
            "kwargs": {"guid": guid, "new_material_name": new_mat},
            "description": f"Change material of {guid} to {new_mat}",
        }]

    return [{
        "tool_name": "_batch_modify_material",
        "tool_fn": None,
        "kwargs": {
            "ifc_type": target,
            "new_material_name": new_mat,
        },
        "description": f"Change material of all {target} to {new_mat}",
    }]


def execute_steps(
    steps: list[dict],
    tool_registry: dict[str, ToolBase],
    parser,
) -> list[dict]:
    """
    Execute a sequence of tool-call steps and return results.

    Handles special _batch_* steps by querying elements first, then applying
    the operation to each.
    """
    results = []

    for step in steps:
        tool_name = step["tool_name"]
        kwargs = step["kwargs"]
        desc = step["description"]

        try:
            if tool_name == "_batch_modify_property":
                result = _exec_batch_modify_property(kwargs, tool_registry, parser)
            elif tool_name == "_batch_modify_attribute":
                result = _exec_batch_modify_attribute(kwargs, tool_registry, parser)
            elif tool_name == "_batch_modify_material":
                result = _exec_batch_modify_material(kwargs, tool_registry, parser)
            elif step["tool_fn"] is not None:
                result = step["tool_fn"](**kwargs)
            else:
                result = f"No handler for {tool_name}"

            results.append({
                "step": desc,
                "tool": tool_name,
                "status": "success",
                "result": str(result),
            })
            logger.info(f"[OK] {desc}: {result}")

        except Exception as e:
            results.append({
                "step": desc,
                "tool": tool_name,
                "status": "error",
                "result": str(e),
            })
            logger.error(f"[FAIL] {desc}: {e}")

    return results


def _exec_batch_modify_property(kwargs, registry, parser):
    ifc_type = kwargs["ifc_type"]
    pset_name = kwargs["pset_name"]
    prop_name = kwargs["property_name"]
    new_val = kwargs["new_value"]

    elements = parser.get_elements_by_type(ifc_type)
    modified = 0
    for el in elements:
        guid = el["guid"]
        try:
            registry["modify_property"](
                guid=guid, pset_name=pset_name,
                property_name=prop_name, new_value=new_val,
            )
            modified += 1
        except (ValueError, KeyError):
            pass
    return f"Modified {prop_name} on {modified}/{len(elements)} {ifc_type} elements."


def _exec_batch_modify_attribute(kwargs, registry, parser):
    ifc_type = kwargs["ifc_type"]
    attr = kwargs["attribute_name"]
    val = kwargs["new_value"]

    elements = parser.get_elements_by_type(ifc_type)
    modified = 0
    for el in elements:
        guid = el["guid"]
        try:
            registry["modify_element_attribute"](
                guid=guid, attribute_name=attr, new_value=val,
            )
            modified += 1
        except (ValueError, KeyError):
            pass
    return f"Modified {attr} on {modified}/{len(elements)} {ifc_type} elements."


def _exec_batch_modify_material(kwargs, registry, parser):
    ifc_type = kwargs["ifc_type"]
    new_mat = kwargs["new_material_name"]

    elements = parser.get_elements_by_type(ifc_type)
    modified = 0
    for el in elements:
        guid = el["guid"]
        try:
            registry["modify_material"](guid=guid, new_material_name=new_mat)
            modified += 1
        except (ValueError, KeyError):
            pass
    return f"Changed material to '{new_mat}' on {modified}/{len(elements)} {ifc_type} elements."


def parse_llm_commands(text: str) -> list[dict]:
    """Parse LLM output text into a list of command dicts."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: try Python literal eval
    try:
        parsed = eval(text, {"__builtins__": {}}, {})
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass

    raise ValueError(f"Could not parse LLM command output: {text[:200]}")
