"""
Test the new GenArtist-style architecture with demo data.

Runs all QA (retrieval + reasoning) and Edit queries, saves structured
results to test_output/new_arch_results.json.
"""

import json
import os
import sys
import time
import traceback
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("new_arch_test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)

from ifc_agent.workflow import IFCAgentWorkflow

IFC_PATH = os.path.join(os.path.dirname(__file__), "..", "demo_data", "1px(1).ifc")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")
EDIT_IFC = os.path.join(OUTPUT_DIR, "edited_new_arch.ifc")
RESULTS_FILE = os.path.join(OUTPUT_DIR, "new_arch_results.json")

RETRIEVAL_QUERIES = [
    ("retrieval_floor_count", "这栋建筑有多少层？"),
    ("retrieval_door_count", "这个IFC文件中有多少扇门？"),
    ("retrieval_wall_thickness", "这个IFC文件中外墙的厚度是多少？"),
    ("retrieval_wall_material", "这个IFC文件中墙体构件分配了哪些材料？"),
]

REASONING_QUERIES = [
    ("reasoning_structural_system", "这栋建筑的结构体系是什么？"),
    ("reasoning_roof_system", "这栋建筑使用了什么类型的屋面系统？"),
    ("reasoning_enclosed", "这栋建筑是否完全封闭？"),
]

EDIT_QUERIES = [
    ("edit_delete_doors", "删除所有的门构件"),
]

EXPECTED = {
    "retrieval_floor_count": "1层",
    "retrieval_door_count": "15-16扇门",
    "retrieval_wall_thickness": "240mm",
    "retrieval_wall_material": "Default Wall",
    "reasoning_structural_system": "承重墙结构",
    "reasoning_roof_system": "平屋面",
    "reasoning_enclosed": "未完全封闭",
    "edit_delete_doors": "所有门已删除",
}


def run_query(wf, qid, qtxt, output_path=None):
    logger.info("=" * 60)
    logger.info("Running: [%s] %s", qid, qtxt)
    logger.info("=" * 60)
    t0 = time.time()
    try:
        if output_path:
            wf._user_output_path = output_path
        result = wf.run(qtxt)
        elapsed = time.time() - t0
        entry = {
            "query_id": qid,
            "query": qtxt,
            "intent": result.get("intent", "unknown"),
            "answer": result.get("answer", ""),
            "operations": result.get("operations", []),
            "plan": result.get("plan"),
            "output_path": result.get("output_path"),
            "elapsed_seconds": round(elapsed, 2),
            "success": True,
            "error": None,
        }
        logger.info("Answer: %s", entry["answer"][:200])
        if entry.get("plan"):
            logger.info("Plan modules: %s", entry["plan"].get("selected_modules", []))
            logger.info("Plan steps: %d", len(entry["plan"].get("steps", [])))
        return entry
    except Exception as exc:
        elapsed = time.time() - t0
        logger.error("FAILED: %s", exc)
        traceback.print_exc()
        return {
            "query_id": qid,
            "query": qtxt,
            "intent": "unknown",
            "answer": "",
            "operations": [],
            "plan": None,
            "output_path": None,
            "elapsed_seconds": round(elapsed, 2),
            "success": False,
            "error": str(exc),
        }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    provider = os.environ.get("LLM_PROVIDER", "openai")
    model = os.environ.get("OPENAI_MODEL", "") or None

    logger.info("Provider: %s, Model: %s", provider, model)
    logger.info("IFC: %s", IFC_PATH)

    all_results = []

    # --- Retrieval QA ---
    logger.info("\n>>> RETRIEVAL QA <<<")
    wf = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model)
    for qid, qtxt in RETRIEVAL_QUERIES:
        r = run_query(wf, qid, qtxt)
        all_results.append(r)

    # --- Reasoning QA ---
    logger.info("\n>>> REASONING QA <<<")
    for qid, qtxt in REASONING_QUERIES:
        r = run_query(wf, qid, qtxt)
        all_results.append(r)

    # --- Edit (fresh workflow to avoid state pollution) ---
    logger.info("\n>>> EDIT <<<")
    wf_edit = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model,
                                output_path=EDIT_IFC)
    for qid, qtxt in EDIT_QUERIES:
        r = run_query(wf_edit, qid, qtxt, output_path=EDIT_IFC)
        all_results.append(r)

    # --- Summary ---
    meta = {
        "provider": provider,
        "model": model,
        "ifc_file": os.path.basename(IFC_PATH),
        "architecture": "GenArtist-style (TreePlanner + Executor + SkillRegistry)",
        "total_skills": len(wf.skill_registry.skills),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    output = {
        "meta": meta,
        "expected": EXPECTED,
        "results": all_results,
    }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info("Results saved to %s", RESULTS_FILE)

    # Print summary table
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    ok = sum(1 for r in all_results if r["success"])
    fail = sum(1 for r in all_results if not r["success"])
    total_time = sum(r["elapsed_seconds"] for r in all_results)
    print(f"Total: {len(all_results)} queries | Success: {ok} | Failed: {fail} | Time: {total_time:.1f}s")
    print()
    for r in all_results:
        status = "OK" if r["success"] else "FAIL"
        answer_preview = r["answer"][:80].replace("\n", " ") if r["answer"] else r.get("error", "")[:80]
        plan_info = ""
        if r.get("plan"):
            steps = len(r["plan"].get("steps", []))
            mods = r["plan"].get("selected_modules", [])
            plan_info = f" [plan: {steps} steps, modules: {mods}]"
        print(f"  [{status}] {r['query_id']} ({r['elapsed_seconds']:.1f}s): {answer_preview}{plan_info}")


if __name__ == "__main__":
    main()
