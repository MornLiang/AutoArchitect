# Verified Builder-Evolving Harness for Multimodal Revit/BIM Agents

## Core Research Question

Given **prompt, image, floor plan, room program, partial IFC/Revit files, or constraint documents**, how can an agent **decide which Revit C# APIs to call**, **dynamically modify or create reusable builders**, and **safely evolve its engineering capability layer** through Harness Engineering?

The work should not be framed as a simple *Revit API RAG Agent*. For an ICLR/CVPR oral-level contribution, the stronger framing is:

> **A verified builder-evolving agent harness that grounds API decisions programmatically, evolves reusable C# Revit builders, and commits only validated capability improvements.**

---

## Recommended Method Combination

```text
Verified Builder-Evolving Harness
= AHE-style Harness Evolution
+ Programmatic API Grounding
+ Builder-Centric Evolution
+ S1-style Plan-and-RevitAct
+ EXG-style BIM Experience Graph
+ Autogenesis-style Commit/Rollback
```

### 1. Main Paradigm: AHE-style Harness Evolution

Use **Agentic Harness Engineering** as the system-level framework. The editable objects are not only prompts, but also:

```text
Revit C# builders / API wrappers / code templates / validators /
repair policies / skill metadata / builder dispatchers / experience memory
```

Key principles:

- **Component observability:** every builder, wrapper, validator, and policy is file-level, versioned, diffable, and reversible.
- **Experience observability:** each run records input, selected APIs, generated/modified builders, compile logs, Revit execution logs, validation results, and repair traces.
- **Decision observability:** each harness patch must declare its expected improvement and be tested against benchmark cases.

---

## Core Technical Modules

### 2. Programmatic API Grounding

Do **not** rely on traditional chunk-based RAG over Revit API documents. Instead, build structured registries:

```text
API Registry + Builder Registry + Validator Registry + Experience Graph
```

The agent programmatically queries these registries to produce an executable API-builder plan:

```text
input intent
 → query API / builder / validator / failure-memory registries
 → rank candidate APIs and builders
 → return executable builder plan
```

This module answers:

```text
Which Revit C# APIs are needed?
Which existing builders can be reused?
Which builder capability is missing?
Which validators must be attached?
```

### 3. Builder-Centric Evolution

The agent should not generate one-off C# code. It should evolve reusable builders.

Each builder is a versioned capability unit:

```json
{
  "name": "CurtainWallBuilder",
  "capabilities": ["curtain_wall", "grid", "mullion"],
  "used_apis": ["Wall.Create", "CurtainGrid", "Mullion"],
  "inputs": ["boundary_curve", "level", "height", "panel_pattern"],
  "outputs": ["wall_ids", "grid_ids", "mullion_ids"],
  "preconditions": ["level_exists", "curtain_wall_type_exists"],
  "validators": ["geometry_valid", "ifc_export_valid", "spatial_consistency_valid"],
  "failure_modes": ["missing_wall_type", "grid_creation_failure"],
  "version": "v3"
}
```

Allowed evolution actions:

```text
select existing builder
compose multiple builders
patch builder code
patch builder metadata
add validator binding
add repair policy
generate new builder
commit or rollback after validation
```

### 4. S1-style Plan-and-RevitAct Execution

Use S1-NexusAgent only as an execution inspiration, not as the main novelty.

```text
Prompt / Image / File
  ↓
Multimodal Requirement Parser
  ↓
Global BIM Planner
  ↓
Subtask Decomposer
  ↓
API / Builder Selector
  ↓
RevitAct / IFCAct Executor
  ↓
Compile + Execute + Validate
  ↓
Critic → Builder Patch / Skill Distillation
```

This gives the system long-horizon planning, dynamic tool/API selection, and trajectory-level skill extraction.

### 5. EXG-style BIM Experience Graph

Store cross-task experience as a structured graph, not unstructured memory.

```text
InputPattern → APIIntent → RevitAPI → Builder → ValidationError
→ FailureMode → RootCause → RepairAction → Validator → VerifiedSkill
```

Example:

```text
floor-plan image contains stair core
 → no StairCoreBuilder
 → retrieve stair/landing/level APIs
 → create StairCoreBuilder v1
 → validation finds disconnected landing
 → add vertical-connectivity validator
 → pass on benchmark cases
 → distill two-flight stair generation skill
```

### 6. Autogenesis-style Resource Governance

Treat every builder/tool/prompt/validator as a versioned resource:

```text
propose patch
 → assess by compile + Revit execution + IFC/geometry/spatial validation
 → compare with previous version
 → commit if improved
 → rollback if regression occurs
```

---

## Full System Flow

```text
Prompt / Image / File
  ↓
Multimodal Requirement Parser
  ↓
Global BIM Planner
  ↓
Programmatic API / Builder Grounding
  ├─ API Registry
  ├─ Builder Registry
  ├─ Validator Registry
  └─ BIM Experience Graph
  ↓
Plan-and-RevitAct Executor
  ├─ select existing builder
  ├─ compose builders
  ├─ call C# Revit API
  ├─ patch old builder
  └─ generate new builder
  ↓
Compile + Revit Execution
  ↓
Validation
  ├─ C# compile pass
  ├─ Revit transaction pass
  ├─ geometry pass
  ├─ spatial graph consistency
  ├─ IFC export pass
  └─ IDS / ifctester pass
  ↓
Harness Evolution
  ├─ localize failure to API / builder / validator / planner
  ├─ propose patch
  ├─ predict expected improvement
  ├─ evaluate on benchmark cases
  └─ commit / rollback
  ↓
Experience Graph Update
  ↓
Verified Builder / Skill Library
```

---

## Oral-level Contribution Framing

### Contribution 1: Verified Builder-Evolving Harness

A self-evolving Revit/BIM harness where reusable builders, validators, wrappers, and repair policies are editable, observable, versioned, and validated.

### Contribution 2: Programmatic API Grounding

A structured API/builder/validator registry that lets agents decide Revit C# API usage through executable capability search rather than traditional document RAG.

### Contribution 3: Builder-Centric Evolution

The system evolves reusable builders instead of producing one-off C# code, enabling cross-task capability accumulation.

### Contribution 4: BIM Experience Graph

A structured graph that links input patterns, API choices, builder patches, failure causes, repair actions, validators, and verified skills for cross-task reuse.

---

## Recommended Positioning

Avoid framing the work as:

```text
Revit API RAG Agent
S1-NexusAgent + EXG + Harness combination
generic multi-agent BIM system
```

Use this framing instead:

> **EvoRevit-Harness: a verified builder-evolving harness that enables multimodal agents to ground Revit API decisions, dynamically evolve reusable C# builders, and commit only validation-backed capability improvements.**
