"""
Run the Text2IFC pipeline end-to-end.

The pipeline always reads its natural-language input from a FILE so the
exact prompt used for a run is reproducible and version-controllable.
Three ways to provide the prompt, in priority order:

1. ``--prompt-file PATH``  — read the natural-language description from
   PATH (e.g. ``demo_prompts/1px_two_storey_office.txt``).
2. ``--prompt "..."``       — inline string (will also be saved to disk
   in the output directory for reproducibility).
3. ``--gt PATH``            — no prompt given: auto-derive a description
   from the ground-truth IFC at PATH (saved to disk as well).

Examples
--------

    # 1) Reproducible run from a prompt file (recommended)
    python run_text2ifc.py \
        --prompt-file demo_prompts/1px_two_storey_office.txt \
        --gt ../demo_data/"1px(1).ifc" \
        --iterations 3 --run-name office

    # 2) Inline prompt (still saved to <output_dir>/<run_name>_prompt.txt)
    python run_text2ifc.py --prompt "A 1-storey rectangular shed, 6m x 4m."

    # 3) Closed-loop demo on a GT IFC (prompt auto-derived from the GT)
    python run_text2ifc.py --gt ../demo_data/"1px(1).ifc" --iterations 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import dotenv

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
dotenv.load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

from ifc_agent.text2ifc.gt_describer import describe_ifc  # noqa: E402
from ifc_agent.text2ifc.schemas import SpatialGraph  # noqa: E402
from ifc_agent.text2ifc.workflow import Text2IFCWorkflow  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Text2IFC end-to-end runner")
    parser.add_argument(
        "--prompt-file", default=None,
        help="Path to a plain-text file containing the user prompt. "
             "Takes precedence over --prompt. Recommended for "
             "reproducibility.",
    )
    parser.add_argument(
        "--prompt", default=None,
        help="Inline user prompt (ignored if --prompt-file is set).",
    )
    parser.add_argument(
        "--gt", default=None,
        help="Optional ground-truth IFC.  If --prompt-file and --prompt "
             "are both omitted, a prompt is auto-derived from the GT IFC.",
    )
    parser.add_argument("--iterations", type=int, default=3,
                        help="Maximum iteration rounds (default: 3).")
    parser.add_argument("--target-score", type=float, default=0.95,
                        help="Stop iterating once similarity ≥ this (default: 0.95).")
    parser.add_argument("--output-dir", default="test_output/text2ifc",
                        help="Directory for generated artefacts.")
    parser.add_argument("--run-name", default="demo")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-ids", action="store_true",
                        help="Disable IDS validation step (ifctester)")
    parser.add_argument("--ids-min-pass-rate", type=float, default=1.0,
                        help="Stop early only if IDS pass-rate ≥ this value "
                             "(0..1, default 1.0)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Use deterministic (no-LLM) fallback agents.")
    parser.add_argument("--seed-spatial", default=None,
                        help="Path to a pre-made SpatialGraph JSON. When set, "
                             "iteration 1 uses this graph directly (the LLM "
                             "Architect is bypassed for iter 1). Iterations "
                             "2+ still run the LLM Refiner→Architect loop. "
                             "Useful for multimodal seeding when the LLM "
                             "cannot see images.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.prompt_file and not args.prompt and not args.gt:
        parser.error("Specify --prompt-file, --prompt, or --gt.")

    # ----- Resolve the prompt -----
    prompt, prompt_origin = _resolve_prompt(args)
    print("=" * 70)
    print(f"Text prompt (source: {prompt_origin}):")
    print(prompt)
    print("=" * 70)

    # ----- Always persist the prompt next to the artefacts so a run is
    #       reproducible from just the output directory.
    os.makedirs(args.output_dir, exist_ok=True)
    saved_prompt_path = os.path.join(args.output_dir, f"{args.run_name}_prompt.txt")
    with open(saved_prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt.rstrip() + "\n")
    print(f"Prompt saved to {saved_prompt_path}")

    # ----- Run the workflow -----
    wf = Text2IFCWorkflow(
        provider=args.provider,
        model=args.model,
        output_dir=args.output_dir,
        max_iterations=args.iterations,
        target_score=args.target_score,
        use_llm=not args.no_llm,
        enable_ids=not args.no_ids,
        ids_min_pass_rate=args.ids_min_pass_rate,
    )
    seed_spatial = None
    if args.seed_spatial:
        if not os.path.isfile(args.seed_spatial):
            raise SystemExit(f"--seed-spatial not found: {args.seed_spatial}")
        with open(args.seed_spatial, "r", encoding="utf-8") as f:
            seed_spatial = SpatialGraph.from_dict(json.load(f))
        print(f"Seed SpatialGraph loaded from {args.seed_spatial} "
              f"(stats={seed_spatial.stats()})")

    result = wf.run(
        prompt, gt_ifc_path=args.gt, run_name=args.run_name,
        seed_spatial=seed_spatial,
    )

    # ----- Print iteration log -----
    print()
    print("#" * 70)
    print("Iteration log")
    print("#" * 70)
    for it in result.iterations:
        print()
        print(f"--- Iteration {it.iteration} ---")
        print(f"IFC: {it.ifc_path}")
        print(f"SpatialGraph: {it.spatial_graph_path}")
        print(f"BuildingGraph: {it.graph_path}")
        print(f"Score: {it.score:.3f}")
        if it.ids_total_specs:
            print(f"IDS: {it.ids_total_specs - it.ids_failed_specs}"
                  f"/{it.ids_total_specs} specs passed "
                  f"(rate={it.ids_pass_rate:.2f})")
        print(it.diff_summary)
        if it.ids_summary and it.ids_failed_specs:
            print()
            print("IDS findings:")
            print(it.ids_summary)
        if it.refinement:
            print()
            print("Refinement note (input to NEXT iteration):")
            print(it.refinement)
    print()
    print(f"BEST: iter={result.best_iteration}, score={result.best_score:.3f}, "
          f"ifc={result.best_ifc_path}")

    # ----- Persist the full transcript -----
    log_path = os.path.join(args.output_dir, f"{args.run_name}_transcript.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"Transcript saved to {log_path}")


def _resolve_prompt(args) -> tuple[str, str]:
    """Pick the prompt according to CLI flag priority.

    Returns
    -------
    (prompt_text, origin_label)
        ``origin_label`` is a short string for logs: ``"file=…"`` /
        ``"inline"`` / ``"auto-from-GT"``.
    """
    if args.prompt_file:
        if not os.path.isfile(args.prompt_file):
            raise SystemExit(f"--prompt-file not found: {args.prompt_file}")
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            raise SystemExit(f"--prompt-file is empty: {args.prompt_file}")
        return text, f"file={args.prompt_file}"

    if args.prompt:
        return args.prompt.strip(), "inline"

    # Fallback: derive from the GT IFC.
    stats = describe_ifc(args.gt)
    return stats["description"], f"auto-from-GT={args.gt}"


if __name__ == "__main__":
    main()
