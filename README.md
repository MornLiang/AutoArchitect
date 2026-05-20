# IFC-Agent: LLM-based IFC File Generation, QA & Editing Agent

A multi-agent system that **generates**, **queries** and **edits** IFC
(Industry Foundation Classes) building models from natural-language prompts.

Two complementary pipelines coexist:

| Pipeline | Direction | Entry point |
|----------|-----------|-------------|
| **Text2IFC** (new, Text2BIM-style) | prompt → requirements → hierarchical graph → IFC | `python run_text2ifc.py` |
| **QA / Edit** (existing)            | natural-language question or edit command on an existing IFC | `python main.py` |

The architecture fuses ideas from two research systems:

- **[Text2BIM](https://arxiv.org/abs/2408.08054)** — Multi-role agent architecture (Product Owner / Architect / Programmer / Reviewer) with structured prompt templates, HuggingFace `Tool` class pattern, and Solibri-based checking loop.
- **[GenArtist](https://arxiv.org/abs/2407.05600)** (NeurIPS 2024 Spotlight) — LLM as planner with tool catalog prompts, `command_parse` translation layer, and MLLM-driven verification/self-correction loop.

## Text2IFC pipeline (NEW)

```
prompt.txt ──►  RequirementsAnalyst (LLM)  ──►  requirements.json
                  │                                 │
                  ▼                                 ▼
            Architect (LLM)  ◄── refinement ── Refiner (LLM)
                  │                                 ▲
                  ▼                                 │
            SpatialGraph.json   (no coordinates)    │
                  │                                 │
                  ▼ expander (deterministic)        │
            BuildingGraph.json  (with coordinates)  │
                  │                                 │
                  ▼ IFCBuilder (deterministic)      │
            generated.ifc  ─────► Comparator (vs GT)
```

Key design choices:

1. **The LLM only emits a SpatialGraph**, i.e. the high-level 3D
   structure (storeys, element counts per storey, footprint dimensions,
   qualitative layout hints).  It does **not** emit any coordinates.
   A deterministic ``expander`` then materialises the SpatialGraph into
   a fully coordinate-resolved BuildingGraph.  This keeps the LLM's JSON
   output ~6% the size of a fully-coordinate description, dramatically
   shortening reasoning time (especially under DeepSeek/Claude thinking
   mode — see [SpatialGraph](#spatialgraph-vs-buildinggraph) below).
2. **The Builder is fully deterministic**: ifcopenshell API calls are
   never hallucinated.  The LLM never sees the IFC schema.
3. The Refiner consumes a structured comparison report (count deltas
   per element type, storey-height and footprint deltas) and produces a
   short natural-language note that the Architect applies on the next
   iteration.

### Quick demo (no LLM required)

```bash
# Reproducible run from a prompt file (recommended)
python run_text2ifc.py \
    --prompt-file demo_prompts/1px_two_storey_office.txt \
    --gt ../demo_data/"1px(1).ifc" \
    --no-llm \
    --iterations 1 \
    --run-name demo
```

Output (truncated):

```
Text prompt (source: file=demo_prompts/1px_two_storey_office.txt):
A 2-storey building with a roughly rectangular footprint of about 15m × 24m,
and a floor-to-floor height of 2.68m, containing 45 walls, 16 doors, 26
windows, 7 columns, 1 floor slab(s), 2 railings, a ceiling covering, built
primarily with concrete.

Prompt saved to test_output/text2ifc/demo_prompt.txt

--- Iteration 1 ---
IFC: test_output/text2ifc/demo_iter1.ifc
SpatialGraph: test_output/text2ifc/demo_iter1_spatial.json
BuildingGraph: test_output/text2ifc/demo_iter1_graph.json
Score: 0.999
All metrics within tolerance.
```

The deterministic agents (`DeterministicAnalyst`, `DeterministicArchitect`,
`DeterministicRefiner` in `ifc_agent/text2ifc/deterministic.py`) match
all metrics of the GT on iteration 1 and the generated IFC passes all 8
rule-based verification checks.

### LLM-mode demo (DeepSeek / OpenAI / Claude)

```bash
# Full LLM pipeline + GT-aware iterative refinement
python run_text2ifc.py \
    --prompt-file demo_prompts/1px_two_storey_office.txt \
    --gt ../demo_data/"1px(1).ifc" \
    --iterations 3 \
    --run-name demo_llm
```

On the bundled `1px(1).ifc` GT with DeepSeek V4 in thinking mode:

| Iteration | Score | Notes |
|-----------|-------|-------|
| 1 | 0.877 | LLM proposes 3 storeys + 2 roofs |
| 2 | **0.999** | Refiner says "remove the topmost storey, delete both roofs"; Architect complies; **target reached** |

Total wall-clock ≈ 5.5 min with `DEEPSEEK_THINKING=true,
DEEPSEEK_REASONING_EFFORT=high`.

### Other prompt sources

The CLI accepts three prompt sources, in priority order:

| Flag | What it does | Saved to disk? |
|---|---|---|
| `--prompt-file PATH` | **Recommended.** Reads from a plain-text file (e.g. `demo_prompts/*.txt`).  Reproducible and version-controllable. | yes |
| `--prompt "..."` | Inline string. | yes (echoed to `<run_name>_prompt.txt`) |
| `--gt PATH` *(alone)* | Auto-derive the prompt from a GT IFC's metadata (`describe_ifc`). | yes |

Every run writes the prompt actually used to
`<output_dir>/<run_name>_prompt.txt` so the run is reproducible from the
output directory alone.

### Switching to LLM mode

Drop `--no-llm` (your `.env` provider is used).  If the LLM call fails
(rate limit, network, parse error) the workflow **auto-falls back to the
deterministic agents per stage**, so a partial outage never breaks the
pipeline.

### SpatialGraph vs BuildingGraph

The LLM Architect emits a compact SpatialGraph:

```json
{
  "metadata": {"name": "Office", "schema": "IFC4"},
  "footprint": {"shape": "rectangle", "x_mm": 15000, "y_mm": 24000},
  "storeys": [
    {
      "id": "s1", "name": "Ground Floor",
      "elevation_mm": 0, "height_mm": 2680,
      "layout_hint": "central_corridor",
      "elements": {
        "walls": 45, "doors": 16, "windows": 26,
        "columns": 7, "slabs": 1, "roofs": 0, "railings": 2
      }
    },
    {
      "id": "s2", "name": "Roof Level",
      "elevation_mm": 2680, "height_mm": 0,
      "layout_hint": "empty",
      "elements": {"roofs": 1}
    }
  ]
}
```

A deterministic ``expander`` materialises this into a coordinate-resolved
BuildingGraph (every wall's start/end, every opening's offset, every
column's position), then the Builder writes the IFC.  On the bundled
demo, the SpatialGraph is **~84 lines**, vs ~1240 lines for the
equivalent BuildingGraph the LLM previously had to produce.

### File layout

```
ifc_agent/text2ifc/
├── __init__.py
├── schemas.py             # SpatialGraph + BuildingGraph dataclasses
├── expander.py            # Deterministic SpatialGraph → BuildingGraph
├── builder.py             # Deterministic BuildingGraph → IFC (ifcopenshell.api.run)
├── gt_describer.py        # IFC → English description + stats
├── comparator.py          # Generated vs GT → similarity score + diffs
├── agents.py              # LLM agents: RequirementsAnalyst, Architect, Refiner
├── deterministic.py       # No-LLM stand-ins for the same three roles
├── workflow.py            # Orchestrator with iterative refinement
└── prompts/
    ├── requirements_analyst.txt
    ├── architect.txt      # SpatialGraph-only prompt (no coordinates)
    └── refiner.txt

demo_prompts/
├── README.md
├── 1px_two_storey_office.txt
└── single_storey_shed.txt
```

## Architecture

```
User Query ("How many doors?" / "Delete all doors")
         |
         v
+------------------------------------------+
|  Router Agent (intent classification)    |
|  QA / Edit / Mixed                       |
+--------+-----------------+---------------+
         |                 |
    QA Pipeline       Edit Pipeline
         |                 |
         v                 v
  IFC Expert Agent    Planner Agent
  (domain knowledge)  (tool selection, JSON commands)
         |                 |
         v                 v
  Coder Agent         command_parse()
  (code generation)   (high-level -> low-level tools)
         |                 |
         v                 v
  Execute & Format    Execute & Verify
         |                 |
         v                 v
  Text Answer         Modified IFC + Log
```

### Design Mapping

| Component | Inspired By | Role |
|-----------|------------|------|
| `RouterAgent` | Text2BIM Product Owner | Classifies intent, routes to QA or Edit pipeline |
| `IFCExpertAgent` | Text2BIM Architect | Provides IFC schema knowledge (via function call) |
| `CoderAgent` | Text2BIM Programmer | Generates Python code calling Tool APIs, auto-retry on failure |
| `PlannerAgent` | GenArtist LLM Planner | Outputs structured JSON command sequences from tool catalog |
| `ReviewerAgent` | Text2BIM Reviewer | Verifies edit results against user intent |
| `CorrectionAgent` | GenArtist Correction Loop | Compares pre/post model state, emits fix commands |
| `command_parse()` | GenArtist `command_parse()` | Translates high-level commands to low-level tool call sequences |
| `RuleBasedVerifier` | Text2BIM Solibri Checker | 8 programmatic IFC checks (no external tools needed) |
| `ifc_tools.py` | Text2BIM `vw_tools_extend.py` | 18 Tool classes wrapping ifcopenshell APIs |

## Quick Start

### 1. Install Dependencies

```bash
cd IFC_Agent
pip install -r requirements.txt
```

### 2. Configure LLM

```bash
cp .env.example .env
```

Three first-class providers are supported.  Pick one by setting
`LLM_PROVIDER` and filling in the matching section of `.env`.

#### Supported providers

| Provider | `LLM_PROVIDER` | Default endpoint | Default model | API-key env |
|----------|---------------|------------------|---------------|-------------|
| **DeepSeek V4** | `deepseek` | `https://api.deepseek.com` | `deepseek-chat` (V4) | `DEEPSEEK_API_KEY` |
| **ChatGPT (OpenAI)** | `openai` | `https://api.openai.com/v1` | `gpt-4o` | `OPENAI_API_KEY` |
| **Claude (Anthropic)** | `claude` | *(Anthropic default)* | `claude-sonnet-4-20250514` | `CLAUDE_API_KEY` |

Each provider has its own block in `.env` / `.env.example`:

```ini
# Pick one of: deepseek | openai | claude
LLM_PROVIDER=deepseek

# (A) DeepSeek V4  (thinking mode — see below)
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro       # or deepseek-chat / deepseek-reasoner
DEEPSEEK_THINKING=true               # enable chain-of-thought
DEEPSEEK_REASONING_EFFORT=high       # high | max

# (B) OpenAI ChatGPT
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o                 # or gpt-4o-mini / o3-mini / o4-mini

# (C) Anthropic Claude
CLAUDE_API_KEY=sk-ant-...
CLAUDE_BASE_URL=
CLAUDE_MODEL=claude-sonnet-4-20250514
```

Any OpenAI-compatible vendor (MiniMax, GLM, Qwen, …) can still be used by
setting `LLM_PROVIDER=openai` and pointing `OPENAI_BASE_URL` at the
vendor's endpoint.

> CLI flags `--provider` / `--model` override `.env` settings at runtime.
> Example: `--provider deepseek --model deepseek-reasoner`.

#### DeepSeek thinking mode

For the `deepseek` provider, `LLMBackend` honours the [DeepSeek thinking-mode
spec](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode):

| Env var | Default | Effect |
|---------|---------|--------|
| `DEEPSEEK_THINKING` | `true` | Adds `extra_body={"thinking": {"type": "enabled"}}`; set to `false` to disable |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `low/medium → high`, `xhigh → max` (DeepSeek API auto-promotes) |

Behaviour when thinking is **enabled**:

1. Request kwargs auto-strip the unsupported sampling params
   (`temperature`, `top_p`, `presence_penalty`, `frequency_penalty`) — the
   API accepts them silently but ignores them, so we drop them upfront.
2. `reasoning_content` from the response is logged at DEBUG level (use
   `--verbose`) and never leaks into the final answer string.
3. In tool-call turns, `reasoning_content` is preserved in the
   assistant message that's posted back to the API (required by DeepSeek;
   missing it produces a 400 error).

## Usage

### Non-LLM Commands (no API key needed)

```bash
# View model summary
python main.py --ifc "../demo_data/1px(1).ifc" --summary

# Run 8 model verification checks
python main.py --ifc "../demo_data/1px(1).ifc" --verify

# Direct edit — delete all doors (no LLM)
python main.py --ifc "../demo_data/1px(1).ifc" --direct-edit "delete_all:IfcDoor" -o edited.ifc

# Direct edit — chain multiple operations
python main.py --ifc "../demo_data/1px(1).ifc" --direct-edit "delete_all:IfcDoor;delete_all:IfcWindow" -o edited.ifc
```

### LLM-Powered Commands (API key required)

After configuring `.env`:

```bash
# ---- QA: Retrieval questions ----
python main.py --ifc "../demo_data/1px(1).ifc" --query "这栋建筑有多少层？"
python main.py --ifc "../demo_data/1px(1).ifc" --query "这栋建筑有多少扇门？"
python main.py --ifc "../demo_data/1px(1).ifc" --query "外墙的厚度是多少？"

# ---- QA: Reasoning questions ----
python main.py --ifc "../demo_data/1px(1).ifc" --query "这栋建筑的结构体系是什么？"
python main.py --ifc "../demo_data/1px(1).ifc" --query "这栋建筑使用了什么屋面系统？"
python main.py --ifc "../demo_data/1px(1).ifc" --query "分析这栋建筑的围护结构"

# ---- Edit: Modify IFC file ----
python main.py --ifc "../demo_data/1px(1).ifc" --query "删除所有的门构件" -o edited.ifc

# ---- Interactive mode ----
python main.py --ifc "../demo_data/1px(1).ifc" --interactive

# ---- Override provider/model at runtime ----
python main.py --ifc "../demo_data/1px(1).ifc" --provider openai --model gpt-4o --query "How many windows?"
```

### Interactive Mode Commands

In interactive mode, the following special commands are available:

| Command | Description |
|---------|-------------|
| `verify` | Run model verification checks |
| `summary` | Print model summary |
| `save [path]` | Save current model state |
| `quit` / `exit` | Exit interactive mode |

## Project Structure

```
IFC_Agent/
├── main.py                          # CLI entry point
├── requirements.txt
├── .env.example                     # LLM configuration template
├── ifc_agent/
│   ├── ifc_parser.py                # IFC file parsing, indexing, LLM serialization
│   ├── ifc_tools.py                 # 18 Tool classes (9 query + 9 edit)
│   ├── command_parse.py             # High-level command → tool sequence translation
│   ├── agents.py                    # 6 Agent roles + multi-provider LLM backend
│   ├── workflow.py                  # QA / Edit / Mixed pipeline orchestration
│   ├── verifier.py                  # 8 rule-based IFC checks
│   ├── utils.py                     # Prompt loading, JSON parsing, code extraction
│   └── prompts/
│       ├── router_prompt.txt        # Intent classification prompt
│       ├── qa_tools.txt             # QA tool catalog + few-shot examples
│       ├── edit_tools.txt           # Edit tool catalog + few-shot examples
│       ├── ifc_schema_knowledge.txt # IFC domain knowledge reference
│       ├── coder_prompt.txt         # Code generation prompt template
│       ├── reviewer_prompt.txt      # Result review prompt template
│       └── correction_prompt.txt    # Self-correction loop prompt
├── demo/
│   ├── qa_demo.json                 # QA command sequence examples
│   └── edit_demo.json               # Edit command sequence examples
└── tests/
    ├── test_workflow.py             # Unit tests (28 tests)
    └── test_demo_data.py            # Demo data validation tests (26 tests)
```

## Available Tools (18 total)

### Query Tools (9)

| Tool | Description |
|------|-------------|
| `query_elements_by_type` | Query all elements of a given IFC type |
| `count_elements` | Count elements of a given type |
| `get_element_properties` | Get all PropertySets/QuantitySets for an element |
| `get_element_info` | Get full IFC attribute dictionary |
| `get_spatial_structure` | Get Project → Site → Building → Storey hierarchy |
| `get_element_material` | Get material assignments |
| `get_element_relationships` | Get containment, type, connectivity relations |
| `get_storey_elements` | List all elements in a specific storey |
| `get_model_context` | Serialize model summary for LLM consumption |

### Edit Tools (9)

| Tool | Description |
|------|-------------|
| `delete_element` | Delete a single element by GUID (cascade-removes openings) |
| `delete_elements_by_type` | Delete all elements of a given type |
| `modify_property` | Modify a PropertySet property value |
| `modify_element_attribute` | Modify a direct IFC attribute (Name, Description, etc.) |
| `move_element` | Move an element by relative offset |
| `copy_element` | Deep copy an element with new GUID |
| `modify_material` | Change material assignments |
| `validate_model` | Run 8 programmatic model checks |
| `save_model` | Save modified model to disk |

## Demo Data

The project includes demo data for testing and evaluation:

- `demo_data/1px(1).ifc` — Original building model (IFC2X3, 12131 entities)
  - 45 walls, 16 doors, 26 windows, 7 columns, 2 storeys, 2 railings
- `demo_data/1px_modified(1).ifc` — After deleting all doors (0 doors, 26 openings)
- `demo_data/SFT_QA_pair(1).jsonc` — QA pairs covering:
  - **Retrieval**: floor count, door count, wall thickness, wall materials
  - **Reasoning**: roof system, structural system, enclosure analysis, type misclassification
  - **Editing**: delete all door components

## Testing

```bash
# Run all 54 tests (no LLM needed)
python -m pytest tests/ -v

# Run only demo data tests
python -m pytest tests/test_demo_data.py -v

# Run only unit tests
python -m pytest tests/test_workflow.py -v
```

### Test Coverage

| Test Suite | Tests | Coverage |
|-----------|-------|---------|
| `TestUtils` | 6 | Prompt injection, JSON parsing, code extraction |
| `TestIFCParser` | 6 | Schema summary, spatial structure, element queries |
| `TestIFCTools` | 6 | Tool registry, counting, validation, description |
| `TestCommandParse` | 4 | Delete/move command parsing, LLM output parsing |
| `TestVerifier` | 4 | Rule checks, report formatting |
| `TestEditExecution` | 2 | Delete doors, command-parse-and-execute pipeline |
| `TestRetrievalQA` | 7 | Door/window/wall/column counts, materials, thickness |
| `TestReasoningQA` | 5 | Roof misclassification, structural system, enclosure |
| `TestEditingDeleteDoors` | 2 | Full edit pipeline vs. reference modified IFC |
| `TestReferenceComparison` | 6 | Original vs. modified IFC element counts |
| `TestVerifierOnDemoData` | 3 | Verification on both original and modified models |
| `TestToolsForPromptInjection` | 3 | Tool descriptions for prompt quality |

## Verification Checks

The `RuleBasedVerifier` runs 8 checks without external tools:

1. **Project Structure** — IfcProject, IfcSite, IfcBuilding, IfcBuildingStorey exist
2. **Spatial Containment** — Building products are contained in spatial elements
3. **Storey Elevations** — Storey elevations are monotonically increasing
4. **Wall Integrity** — Walls have geometric representations
5. **Door/Window Hosting** — Doors and windows are in openings or spatially contained
6. **Material Assignments** — Structural elements have materials
7. **Property Completeness** — Common Pset exists on key element types
8. **Duplicate GUIDs** — No duplicate GlobalIds in the model

## Example Workflow

### QA Pipeline

```
User: "What is the structural system of this building?"
  → Router: intent = "qa" (reasoning question)
  → IFC Expert: provides domain knowledge about structural systems
  → Coder: generates code to count walls, columns, check wall thickness
  → Execute: IfcWall=45, IfcColumn=7, wall thickness ~240mm
  → Format: "Load-bearing wall system. Evidence: walls dominate (45 vs 7 columns),
             wall thickness 240mm consistent with load-bearing construction."
```

### Edit Pipeline

```
User: "Delete all door components in this IFC file"
  → Router: intent = "edit"
  → Planner: [{"tool": "delete", "input": {"target": "IfcDoor", "scope": "all"}}]
  → command_parse: → [delete_elements_by_type("IfcDoor"), validate_model()]
  → Execute: "Deleted 16 elements of type IfcDoor"
  → Correction: verify 0 doors remain, walls/windows preserved
  → Save: model_modified.ifc
```
