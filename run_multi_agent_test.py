"""
Comprehensive Multi-Agent test using all demo QA and Edit queries.
Tests the ProductOwner → Architect (DFS) → Programmer pipeline
with the architect_knowledge skill.
"""

import os
import sys
import json
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("test")

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

from ifc_agent.workflow import IFCAgentWorkflow

IFC_PATH = os.path.join(os.path.dirname(__file__), "..", "demo_data", "1px(1).ifc")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")
EDIT_IFC = os.path.join(OUTPUT_DIR, "multi_agent_edited.ifc")

QUERIES = [
    # --- Retrieval ---
    {
        "id": "R1",
        "query": "How many floors does this building have?",
        "expected": "1 floor",
        "category": "retrieval",
    },
    {
        "id": "R2",
        "query": "What is the thickness of the exterior walls in this IFC file?",
        "expected": "240mm",
        "category": "retrieval",
    },
    {
        "id": "R3",
        "query": "How many doors are there in this IFC file?",
        "expected": "16 doors (demo says 15)",
        "category": "retrieval",
    },
    {
        "id": "R4",
        "query": "What materials are assigned to the wall components in this IFC file?",
        "expected": "Default Wall (placeholder, no real physical properties)",
        "category": "retrieval",
    },
    # --- Reasoning ---
    {
        "id": "Q1",
        "query": "What type of roof system does this building use?",
        "expected": "Flat roof, misclassified as IfcCovering/CEILING instead of IfcRoof",
        "category": "reasoning",
    },
    {
        "id": "Q2",
        "query": "What is the structural system of this building?",
        "expected": "Load-bearing wall system, columns are decorative",
        "category": "reasoning",
    },
    {
        "id": "Q3",
        "query": "Is this building fully enclosed?",
        "expected": "No, central corridor has only IfcRailing, no walls; open-air walkway",
        "category": "reasoning",
    },
    # --- Edit ---
    {
        "id": "E1",
        "query": "Delete all door components in this IFC file.",
        "expected": "All doors removed, 0 remaining",
        "category": "edit",
    },
]


def run_one(wf, q):
    qid = q["id"]
    logger.info("=" * 60)
    logger.info("Running %s [%s]: %s", qid, q["category"], q["query"])
    logger.info("Expected: %s", q["expected"])

    t0 = time.time()
    try:
        result = wf.run(q["query"])
        elapsed = time.time() - t0
        answer = result.get("answer", "")
        return {
            "id": qid,
            "query": q["query"],
            "category": q["category"],
            "expected": q["expected"],
            "answer": answer,
            "sub_tasks": result.get("sub_tasks", []),
            "traversal_log": result.get("traversal_log", []),
            "operations": len(result.get("operations", [])),
            "output_path": result.get("output_path"),
            "elapsed_s": round(elapsed, 1),
            "success": True,
            "cache_stats": result.get("cache_stats"),
        }
    except Exception as exc:
        elapsed = time.time() - t0
        logger.error("FAILED %s: %s", qid, exc)
        return {
            "id": qid,
            "query": q["query"],
            "category": q["category"],
            "expected": q["expected"],
            "answer": f"ERROR: {exc}",
            "elapsed_s": round(elapsed, 1),
            "success": False,
        }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    provider = os.environ.get("LLM_PROVIDER", "openai")
    model = os.environ.get("OPENAI_MODEL", "") or None
    logger.info("Provider: %s, Model: %s", provider, model)

    results = []

    # --- QA queries (shared workflow) ---
    logger.info("Initializing QA workflow...")
    wf_qa = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model, multi_agent=True)

    for q in QUERIES:
        if q["category"] == "edit":
            continue
        r = run_one(wf_qa, q)
        results.append(r)
        logger.info(
            "[%s] %s — %.1fs — Answer: %s",
            r["id"], "OK" if r["success"] else "FAIL",
            r["elapsed_s"], r["answer"][:200],
        )

    # --- Edit query (separate workflow) ---
    for q in QUERIES:
        if q["category"] != "edit":
            continue
        logger.info("Initializing Edit workflow...")
        wf_edit = IFCAgentWorkflow(
            IFC_PATH, provider=provider, model=model,
            multi_agent=True, output_path=EDIT_IFC,
        )
        r = run_one(wf_edit, q)
        results.append(r)

        if r.get("output_path") and os.path.exists(EDIT_IFC):
            import ifcopenshell
            edited = ifcopenshell.open(EDIT_IFC)
            doors_left = len(edited.by_type("IfcDoor"))
            r["verification"] = f"Doors remaining: {doors_left}"
            logger.info("Verification: %s", r["verification"])

    # --- Summary ---
    print("\n" + "=" * 70)
    print("MULTI-AGENT TEST RESULTS (with optimizations)")
    print("=" * 70)

    for r in results:
        status = "PASS" if r["success"] else "FAIL"
        print(f"\n[{status}] {r['id']} ({r['category']}) — {r['elapsed_s']}s")
        print(f"  Q: {r['query']}")
        print(f"  Expected: {r['expected']}")
        ans = r['answer'].replace('\n', ' ')[:300]
        print(f"  Answer: {ans}")
        if r.get("traversal_log"):
            for t in r["traversal_log"]:
                levels = [f"L{s['level']}({s['chars']})" for s in t.get("traversal", [])]
                print(f"  DFS: {' → '.join(levels)}")
        if r.get("verification"):
            print(f"  Verify: {r['verification']}")
        if r.get("cache_stats"):
            cs = r["cache_stats"]
            print(f"  Cache: LLM hits={cs.get('hits',0)} misses={cs.get('misses',0)}")

    total = len(results)
    ok = sum(1 for r in results if r["success"])
    print(f"\n{'=' * 70}")
    print(f"Total: {total}, Success: {ok}, Failed: {total - ok}")
    total_time = sum(r["elapsed_s"] for r in results)
    print(f"Total time: {total_time:.0f}s")

    # Baseline comparison (from previous run)
    baseline_times = {
        "R1": 31.9, "R2": 114.7, "R3": 12.0, "R4": 79.1,
        "Q1": 88.5, "Q2": 26.5, "Q3": 16.4, "E1": 119.3,
    }
    print(f"\n--- Speed Comparison ---")
    print(f"{'ID':<5} {'Baseline':>10} {'Optimized':>10} {'Speedup':>10}")
    for r in results:
        base = baseline_times.get(r["id"], 0)
        opt = r["elapsed_s"]
        speedup = f"{base/opt:.2f}x" if opt > 0 else "N/A"
        print(f"{r['id']:<5} {base:>9.1f}s {opt:>9.1f}s {speedup:>10}")
    baseline_total = sum(baseline_times.values())
    print(f"{'TOTAL':<5} {baseline_total:>9.1f}s {total_time:>9.1f}s "
          f"{baseline_total/total_time:.2f}x")

    # Save
    out_path = os.path.join(OUTPUT_DIR, "multi_agent_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "provider": provider,
            "model": model,
            "architecture": "multi_agent",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
