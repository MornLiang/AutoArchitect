"""Run all LLM test queries against demo data and save results."""
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dotenv
dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)

from ifc_agent.workflow import IFCAgentWorkflow

IFC_PATH = os.path.join(os.path.dirname(__file__), "..", "demo_data", "1px(1).ifc")
OUTPUT_IFC = os.path.join(os.path.dirname(__file__), "test_output", "edited_by_llm.ifc")
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "test_output", "llm_test_results.json")

RETRIEVAL_QUERIES = [
    ("retrieval_floor_count", "这栋建筑有多少层？"),
    ("retrieval_door_count", "这栋建筑有多少扇门？"),
    ("retrieval_wall_thickness", "外墙的厚度是多少？"),
    ("retrieval_wall_material", "外墙使用了什么材料？"),
]

REASONING_QUERIES = [
    ("reasoning_structural_system", "这栋建筑的结构体系是什么？"),
    ("reasoning_roof_system", "这栋建筑使用了什么屋面系统？"),
    ("reasoning_enclosure", "分析这栋建筑的围护结构"),
]

EDIT_QUERIES = [
    ("edit_delete_doors", "删除所有的门构件"),
]

EXPECTED_ANSWERS = {
    "retrieval_floor_count": "2层",
    "retrieval_door_count": "16扇门",
    "retrieval_wall_thickness": "240mm",
    "retrieval_wall_material": "混凝土",
    "reasoning_structural_system": "承重墙结构",
    "reasoning_roof_system": "平屋面",
    "reasoning_enclosure": "砖墙/混凝土墙体",
    "edit_delete_doors": "删除16扇门",
}


def run_query(wf, query_id, query_text, output_path=None):
    """Run a single query and return structured result."""
    print(f"\n{'='*70}")
    print(f"[{query_id}] {query_text}")
    print(f"{'='*70}")

    start = time.time()
    try:
        if query_id.startswith("edit_") and output_path:
            result = wf.run(query_text)
        else:
            result = wf.run(query_text)
        elapsed = time.time() - start

        print(f"Intent: {result.get('intent', 'N/A')}")
        print(f"Answer:\n{result.get('answer', 'N/A')}")
        if result.get("operations"):
            print(f"Operations: {len(result['operations'])}")
            for op in result["operations"]:
                status = "OK" if op["status"] == "success" else "FAIL"
                print(f"  [{status}] {op['step']}: {op['result']}")
        if result.get("output_path"):
            print(f"Output: {result['output_path']}")
        print(f"Time: {elapsed:.1f}s")

        return {
            "query_id": query_id,
            "query": query_text,
            "intent": result.get("intent", ""),
            "answer": result.get("answer", ""),
            "operations": result.get("operations", []),
            "output_path": result.get("output_path", ""),
            "elapsed_seconds": round(elapsed, 2),
            "success": True,
            "error": None,
        }
    except Exception as e:
        elapsed = time.time() - start
        tb = traceback.format_exc()
        print(f"ERROR: {e}")
        print(tb)
        return {
            "query_id": query_id,
            "query": query_text,
            "intent": "",
            "answer": "",
            "operations": [],
            "output_path": "",
            "elapsed_seconds": round(elapsed, 2),
            "success": False,
            "error": str(e),
        }


def main():
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)

    provider = os.environ.get("LLM_PROVIDER", "openai")
    model = os.environ.get("OPENAI_MODEL", "MiniMax-M2.7-highspeed")
    base_url = os.environ.get("OPENAI_BASE_URL", "")

    print(f"Provider: {provider}")
    print(f"Model: {model}")
    print(f"Base URL: {base_url}")
    print(f"IFC: {IFC_PATH}")

    all_results = []
    meta = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "ifc_file": os.path.basename(IFC_PATH),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # --- Retrieval QA ---
    print("\n" + "#"*70)
    print("# RETRIEVAL QA")
    print("#"*70)
    wf = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model)
    for qid, qtxt in RETRIEVAL_QUERIES:
        r = run_query(wf, qid, qtxt)
        all_results.append(r)

    # --- Reasoning QA ---
    print("\n" + "#"*70)
    print("# REASONING QA")
    print("#"*70)
    wf2 = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model)
    for qid, qtxt in REASONING_QUERIES:
        r = run_query(wf2, qid, qtxt)
        all_results.append(r)

    # --- Edit ---
    print("\n" + "#"*70)
    print("# EDIT")
    print("#"*70)
    wf3 = IFCAgentWorkflow(IFC_PATH, provider=provider, model=model,
                           output_path=OUTPUT_IFC)
    for qid, qtxt in EDIT_QUERIES:
        r = run_query(wf3, qid, qtxt, output_path=OUTPUT_IFC)
        all_results.append(r)

    # Save
    output = {"meta": meta, "expected": EXPECTED_ANSWERS, "results": all_results}
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n\nResults saved to: {RESULTS_FILE}")

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    ok = sum(1 for r in all_results if r["success"])
    fail = sum(1 for r in all_results if not r["success"])
    total_time = sum(r["elapsed_seconds"] for r in all_results)
    print(f"Total queries: {len(all_results)}")
    print(f"Succeeded: {ok}, Failed: {fail}")
    print(f"Total time: {total_time:.1f}s")

    for r in all_results:
        status = "OK" if r["success"] else "FAIL"
        ans_preview = (r["answer"] or "")[:80].replace("\n", " ")
        print(f"  [{status}] {r['query_id']:30s} ({r['elapsed_seconds']:5.1f}s) {ans_preview}")


if __name__ == "__main__":
    main()
