#!/usr/bin/env python3
"""
Batch script to generate comprehensive documentation and code for IFC files.

For each IFC file under BIMNet_release/BIMNet_release/ifc, this script:
  1. Extracts structural statistics and spatial hierarchy using IFC_Agent tools
  2. Calls an LLM (ChatGPT / OpenAI / DeepSeek / Claude / Gemini) to generate:
     - Revit C# code that recreates the building
     - Spatial structure diagram (Mermaid + text hierarchy)
     - Requirements document
     - Detailed natural-language description
     - Short text prompt (suitable for Text2IFC pipelines)
  3. Saves all artefacts to an output directory tree

Usage:
    cd /Users/hubin/Desktop/我/个人/论文/2026/Nips/BIM_IFC/IFC_Agent
    export OPENAI_API_KEY=sk-...
    python scripts/generate_ifc_documentation.py \
        --ifc-root ../BIMNet_release/BIMNet_release/ifc \
        --output ./ifc_documentation_output \
        --provider openai \
        --model gpt-4o

Requirements:
    pip install ifcopenshell openai anthropic google-genai python-dotenv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Ensure IFC_Agent is on the path so we can reuse its modules.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import ifcopenshell
from ifc_agent.text2ifc.gt_describer import describe_ifc
from ifc_agent.graph_extractor import extract_building_graph, extract_storey_graphs
from ifc_agent.agents import LLMBackend

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert BIM architect, structural engineer, and Revit API developer with deep knowledge of IFC (Industry Foundation Classes) and Autodesk Revit.

Your task is to analyze the provided IFC model data and generate five distinct outputs. Follow the exact section markers so the parser can split your response.

--- Output format instructions ---
Use these exact delimiters (each on its own line):
---REVIT_CODE_START---
... C# code ...
---REVIT_CODE_END---

---SPATIAL_STRUCTURE_START---
... diagram & hierarchy ...
---SPATIAL_STRUCTURE_END---

---REQUIREMENTS_START---
... requirements doc ...
---REQUIREMENTS_END---

---DETAILED_DESCRIPTION_START---
... description ...
---DETAILED_DESCRIPTION_END---

---TEXT_PROMPT_START---
... short prompt ...
---TEXT_PROMPT_END---

--- Detailed instructions per output ---

1. REVIT C# CODE
   • Write a complete, compilable C# class using the Revit 2027 API.
   • The class must inherit from IExternalCommand and implement Execute().
   • Recreate ALL building elements found in the IFC data (walls, doors, windows, columns, slabs, beams, railings, roofs, coverings, etc.) with approximately correct dimensions, positions, and levels.
   • Use Document, UIDocument, Level, Wall, Floor, FamilyInstance (for doors/windows/columns), and other relevant Revit API classes.
   • Include comments explaining key parameters (storey heights, footprint sizes, element counts).
   • If exact coordinates are not available, use reasonable approximations based on footprint and storey height.

2. SPATIAL STRUCTURE DIAGRAM
   • Provide a Mermaid flowchart (```mermaid ... ```) showing the spatial hierarchy: IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey(s) → major element groups.
   • Also provide a plain-text indented hierarchy list showing each storey and the types/quantities of elements it contains.
   • Include key metrics (footprint dimensions, storey height, total element counts) in the diagram.

3. REQUIREMENTS DOCUMENT
   • Write a structured software/BIM requirements document in Markdown.
   • Sections: 1. Overview, 2. Functional Requirements (spatial structure, geometry, materials), 3. Non-Functional Requirements (performance, accuracy, standards compliance), 4. Data Requirements (IFC schema, entity types, properties), 5. Constraints & Assumptions.
   • Be specific: mention exact counts, dimensions, materials, and IFC entity types.

4. DETAILED TEXT DESCRIPTION
   • Write a rich, natural-language description of the building (2-4 paragraphs).
   • Describe the architectural layout, structural system, envelope (walls/windows/doors), spatial organisation per storey, materials, and any notable features.
   • Include quantitative details (dimensions, counts, heights) woven naturally into the prose.
   • This description should be detailed enough for a human architect to visualise the building without seeing the model.

5. SHORT TEXT PROMPT
   • Write a single concise sentence or short paragraph (1-3 sentences, under 200 words) that could serve as a text prompt for an AI Text-to-IFC generation system.
   • The prompt must capture the essential identity of the building: type, size, storeys, key elements, and materials.
   • It should be generative (i.e., telling an AI what to build) rather than descriptive.
"""


def _build_user_prompt(ifc_path: str, stats: dict, building_graph: dict, storey_graphs: list[dict]) -> str:
    """Assemble the user-facing prompt from extracted IFC data."""

    # Spatial hierarchy text
    hierarchy_lines = []
    spatial_nodes = [n for n in building_graph.get("nodes", []) if n.get("category") == "spatial"]
    spatial_nodes.sort(key=lambda n: (n.get("ifc_class", ""), n.get("Name", "")))

    for n in spatial_nodes:
        cls = n.get("ifc_class", "")
        name = n.get("Name", "")
        gid = n.get("GlobalId", "")
        hierarchy_lines.append(f"  [{cls}] Name='{name}' GlobalId={gid}")

    # Storey summaries
    storey_summaries = []
    for sg in storey_graphs:
        sname = sg.get("storey_name", "unknown")
        elem_nodes = [n for n in sg.get("nodes", []) if n.get("category") == "element"]
        type_counts: dict[str, int] = {}
        for n in elem_nodes:
            cls = n.get("ifc_class", "Unknown")
            type_counts[cls] = type_counts.get(cls, 0) + 1
        summary = f"  Storey '{sname}': " + ", ".join(
            f"{cnt} {cls}" for cls, cnt in sorted(type_counts.items())
        )
        storey_summaries.append(summary)

    # Element type counts across whole building
    all_elem_nodes = [n for n in building_graph.get("nodes", []) if n.get("category") == "element"]
    global_type_counts: dict[str, int] = {}
    for n in all_elem_nodes:
        cls = n.get("ifc_class", "Unknown")
        global_type_counts[cls] = global_type_counts.get(cls, 0) + 1

    user_prompt = f"""IFC file path: {ifc_path}
IFC schema: {stats.get('schema', 'unknown')}

=== BUILDING STATISTICS ===
Storey count: {stats.get('storey_count', 0)}
Storey names: {stats.get('storey_names', [])}
Storey elevations (mm): {stats.get('storey_elevations_mm', [])}
Floor-to-floor height (mm): {stats.get('storey_height_mm', 0)}
Footprint X × Y (mm): {stats.get('footprint_x_mm', 0):.1f} × {stats.get('footprint_y_mm', 0):.1f}

Element counts:
  Walls:        {stats.get('wall_count', 0)}
  Doors:        {stats.get('door_count', 0)}
  Windows:      {stats.get('window_count', 0)}
  Columns:      {stats.get('column_count', 0)}
  Floor slabs:  {stats.get('slab_count', 0)}
  Beams:        {stats.get('beam_count', 0)}
  Railings:     {stats.get('railing_count', 0)}
  Coverings:    {stats.get('covering_count', 0)}
  Roofs:        {stats.get('roof_count', 0)}
Dominant material: {stats.get('dominant_material', 'unknown')} (raw: {stats.get('dominant_material_raw', '')})

Wall topology metrics:
  Median wall length (mm): {stats.get('wall_length_median_mm', 0):.1f}
  Wall length std (mm):    {stats.get('wall_length_std_mm', 0):.1f}
  Short-wall ratio:        {stats.get('wall_short_ratio', 0):.2f}
  Orientation H/V/D:       {stats.get('wall_orient_h', 0)}/{stats.get('wall_orient_v', 0)}/{stats.get('wall_orient_d', 0)}
  Orientation entropy:     {stats.get('wall_orient_entropy', 0):.3f}

=== SPATIAL HIERARCHY ===
{chr(10).join(hierarchy_lines) if hierarchy_lines else '  (no spatial hierarchy extracted)'}

=== PER-STOREY ELEMENT SUMMARY ===
{chr(10).join(storey_summaries) if storey_summaries else '  (no storey data)'}

=== GLOBAL ELEMENT TYPE COUNTS ===
{chr(10).join(f'  {cls}: {cnt}' for cls, cnt in sorted(global_type_counts.items())) if global_type_counts else '  (no elements)'}

Now generate the five required outputs using the exact delimiters specified in the system instructions.
"""
    return user_prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_RE_DELIMITERS = {
    "revit_code": ("---REVIT_CODE_START---", "---REVIT_CODE_END---"),
    "spatial_structure": ("---SPATIAL_STRUCTURE_START---", "---SPATIAL_STRUCTURE_END---"),
    "requirements": ("---REQUIREMENTS_START---", "---REQUIREMENTS_END---"),
    "detailed_description": ("---DETAILED_DESCRIPTION_START---", "---DETAILED_DESCRIPTION_END---"),
    "text_prompt": ("---TEXT_PROMPT_START---", "---TEXT_PROMPT_END---"),
}


def _extract_sections(text: str) -> dict[str, str]:
    """Pull out each delimited section from the LLM response."""
    sections: dict[str, str] = {}
    for key, (start_tag, end_tag) in _RE_DELIMITERS.items():
        start_idx = text.find(start_tag)
        end_idx = text.find(end_tag)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            content = text[start_idx + len(start_tag) : end_idx].strip()
            # Strip markdown code fences if present for code
            if key == "revit_code" and content.startswith("```"):
                lines = content.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines)
            sections[key] = content
        else:
            sections[key] = f"<!-- Section '{key}' not found in model response -->"
    return sections


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

_OUTPUT_FILES = {
    "revit_code": "revit_code.cs",
    "spatial_structure": "spatial_structure.md",
    "requirements": "requirements.md",
    "detailed_description": "detailed_description.md",
    "text_prompt": "text_prompt.txt",
}


def _save_sections(output_dir: Path, sections: dict[str, str], basename: str) -> dict[str, Path]:
    """Write each section to its designated file; return mapping."""
    saved: dict[str, Path] = {}
    for key, fname in _OUTPUT_FILES.items():
        fpath = output_dir / f"{basename}_{fname}"
        fpath.write_text(sections.get(key, ""), encoding="utf-8")
        saved[key] = fpath
    return saved


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_ifc_file(ifc_path: Path, llm: LLMBackend, output_dir: Path) -> dict[str, Any]:
    """Run the full extraction + generation pipeline for a single IFC file."""
    basename = ifc_path.stem
    logger.info("Processing %s ...", ifc_path)

    # 1. Descriptive statistics
    try:
        stats = describe_ifc(str(ifc_path), max_chars=800)
    except Exception as exc:
        logger.error("describe_ifc failed for %s: %s", ifc_path, exc)
        raise

    # 2. Graph extraction (keep it lightweight — we only need hierarchy & counts)
    try:
        model = ifcopenshell.open(str(ifc_path))
        building_graph = extract_building_graph(model)
        storey_graphs = extract_storey_graphs(model)
    except Exception as exc:
        logger.error("Graph extraction failed for %s: %s", ifc_path, exc)
        building_graph = {"nodes": [], "edges": []}
        storey_graphs = []

    # 3. Build prompt
    user_prompt = _build_user_prompt(str(ifc_path), stats, building_graph, storey_graphs)
    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    # 4. LLM generation
    logger.info("  → LLM call for %s", basename)
    try:
        response = llm.generate(full_prompt, temperature=0.2)
    except Exception as exc:
        logger.error("LLM generation failed for %s: %s", ifc_path, exc)
        raise

    # 5. Parse & save
    sections = _extract_sections(response)
    saved = _save_sections(output_dir, sections, basename)

    # Also save raw stats & response for traceability
    meta_path = output_dir / f"{basename}_meta.json"
    meta = {
        "ifc_path": str(ifc_path),
        "basename": basename,
        "stats": {k: v for k, v in stats.items() if k != "description"},
        "description": stats.get("description", ""),
        "graph_stats": building_graph.get("stats", {}),
        "saved_files": {k: str(v) for k, v in saved.items()},
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("  ✓ Saved %d artefacts for %s to %s", len(saved), basename, output_dir)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-generate documentation & Revit code for IFC files")
    parser.add_argument(
        "--ifc-root",
        type=str,
        default="../BIMNet_release/BIMNet_release/ifc",
        help="Root directory to recursively search for .ifc files",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./ifc_documentation_output",
        help="Output directory for generated artefacts",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="LLM provider (openai, deepseek, claude, gemini). Defaults to env var LLM_PROVIDER.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name override (e.g. gpt-4o, gpt-4o-mini).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N files (0 = unlimited).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip files that already have output artefacts.",
    )
    args = parser.parse_args()

    ifc_root = Path(args.ifc_root).expanduser().resolve()
    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    # Collect IFC files
    ifc_files = sorted(ifc_root.rglob("*.ifc"))
    if not ifc_files:
        logger.error("No .ifc files found under %s", ifc_root)
        return 1

    if args.limit > 0:
        ifc_files = ifc_files[: args.limit]

    logger.info("Found %d IFC file(s). Output will go to %s", len(ifc_files), output_root)

    # Initialise LLM backend (reads API keys from .env automatically)
    llm = LLMBackend(provider=args.provider, model=args.model)
    logger.info("LLM backend: provider=%s model=%s", llm.provider, llm.model)

    results: list[dict] = []
    skipped = 0
    processed = 0

    for ifc_path in ifc_files:
        basename = ifc_path.stem
        # Simple resume check: skip if meta.json already exists
        if args.resume and (output_root / f"{basename}_meta.json").exists():
            logger.info("Skipping %s (already processed)", basename)
            skipped += 1
            continue

        try:
            meta = process_ifc_file(ifc_path, llm, output_root)
            results.append(meta)
            processed += 1
        except Exception as exc:
            logger.error("Failed to process %s: %s", ifc_path, exc)
            results.append({
                "ifc_path": str(ifc_path),
                "basename": basename,
                "error": str(exc),
            })

    # Save batch summary
    summary_path = output_root / "_batch_summary.json"
    summary = {
        "total_files": len(ifc_files),
        "processed": processed,
        "skipped": skipped,
        "failed": len(ifc_files) - processed - skipped,
        "provider": llm.provider,
        "model": llm.model,
        "results": results,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Batch complete. Processed: %d, Skipped: %d, Failed: %d. Summary: %s",
                processed, skipped, summary["failed"], summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
