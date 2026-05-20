"""Shared utility functions."""

from __future__ import annotations

import json
import os
import re


def load_prompt(prompt_name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")
    path = os.path.join(prompts_dir, prompt_name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def inject_prompt(template: str, **replacements) -> str:
    """Replace <<key>> placeholders in a prompt template."""
    result = template
    for key, value in replacements.items():
        placeholder = f"<<{key}>>"
        result = result.replace(placeholder, str(value) if value else "")
    return result


def clean_json_from_llm(text: str) -> str:
    """Strip markdown fences and whitespace from LLM JSON output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_json_response(text: str) -> dict | list:
    """Parse LLM response as JSON, with fallback to eval."""
    text = clean_json_from_llm(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return eval(text, {"__builtins__": {}}, {})
    except Exception:
        pass
    raise ValueError(f"Failed to parse JSON from LLM response: {text[:300]}")


def extract_code_blocks(text: str) -> list[str]:
    """Extract Python code blocks from LLM output."""
    pattern = r"```(?:python|py)?\s*\n(.*?)```"
    blocks = re.findall(pattern, text, re.DOTALL)
    if blocks:
        return [b.strip() for b in blocks]

    # Fallback: if no code fences, treat the whole thing as code
    # if it looks like Python
    lines = text.strip().split("\n")
    code_lines = [l for l in lines if not l.startswith("#") or "import" in l]
    if any(kw in text for kw in ("result =", "print(", "for ", "if ", "import ")):
        return [text.strip()]
    return []
