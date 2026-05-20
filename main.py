"""
IFC-Agent CLI entry point.

Usage:
    # Query (provider is read from LLM_PROVIDER in .env, defaults to openai)
    python main.py --ifc path/to/file.ifc --query "How many doors are in this building?"

    # Override provider / model via CLI
    python main.py --ifc path/to/file.ifc --provider claude --model MiniMax-M2.7 --query "..."

    # Edit via LLM
    python main.py --ifc path/to/file.ifc --query "Delete all door components" -o edited.ifc

    # Direct edit (no LLM required)
    python main.py --ifc path/to/file.ifc --direct-edit "delete_all:IfcDoor" -o edited.ifc

    # Verify / Summary (no LLM required)
    python main.py --ifc path/to/file.ifc --verify
    python main.py --ifc path/to/file.ifc --summary

Configuration:
    Set LLM_PROVIDER, API keys, base URL, and model in .env (see .env.example).
    CLI flags --provider / --model override .env settings.
"""

import argparse
import logging
import os

import dotenv

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
dotenv.load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

from ifc_agent.agents import SUPPORTED_PROVIDERS  # noqa: E402
from ifc_agent.ifc_parser import IFCParser  # noqa: E402
from ifc_agent.verifier import RuleBasedVerifier, format_verification_report  # noqa: E402
from ifc_agent.workflow import IFCAgentWorkflow  # noqa: E402


# ---------------------------------------------------------------------------
# Direct-edit DSL parser
# ---------------------------------------------------------------------------

_DIRECT_EDIT_HELP = """\
Direct edit operations (no LLM required). Supported formats:

  delete_all:<IFC_TYPE>            Delete all elements of a type
                                   e.g. delete_all:IfcDoor

  delete:<GUID>                    Delete a single element by GUID

  modify_attr:<IFC_TYPE>:<ATTR>=<VALUE>
                                   Modify an attribute on all elements of a type
                                   e.g. modify_attr:IfcWall:Description=Updated

  modify_prop:<IFC_TYPE>:<PSET>.<PROP>=<VALUE>
                                   Modify a property on all elements of a type
                                   e.g. modify_prop:IfcWall:Pset_WallCommon.IsExternal=True

Multiple operations can be chained with ';':
  delete_all:IfcDoor;delete_all:IfcWindow
"""


def _parse_direct_edits(spec: str) -> list[dict]:
    """Parse a direct-edit spec string into command dicts."""
    commands = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue

        if part.startswith("delete_all:"):
            ifc_type = part[len("delete_all:"):]
            commands.append({"tool": "delete", "input": {"target": ifc_type, "scope": "all"}})

        elif part.startswith("delete:"):
            guid = part[len("delete:"):]
            commands.append({"tool": "delete_single", "input": {"guid": guid}})

        elif part.startswith("modify_attr:"):
            rest = part[len("modify_attr:"):]
            ifc_type, kv = rest.split(":", 1)
            attr, val = kv.split("=", 1)
            commands.append({
                "tool": "modify_attribute",
                "input": {"target": ifc_type, "scope": "all",
                          "attribute_name": attr, "new_value": val},
            })

        elif part.startswith("modify_prop:"):
            rest = part[len("modify_prop:"):]
            ifc_type, kv = rest.split(":", 1)
            pset_prop, val = kv.split("=", 1)
            pset, prop = pset_prop.split(".", 1)
            commands.append({
                "tool": "modify_property",
                "input": {"target": ifc_type, "scope": "all",
                          "pset_name": pset, "property_name": prop, "new_value": val},
            })

        else:
            print(f"[WARN] Unknown direct-edit operation: {part}")

    return commands


def main():
    parser = argparse.ArgumentParser(
        description="IFC-Agent: MLLM-based IFC file QA and editing agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_DIRECT_EDIT_HELP,
    )
    parser.add_argument("--ifc", required=True, help="Path to the input IFC file")
    parser.add_argument("--output", "-o", default=None,
                        help="Path for the edited output IFC file (default: <input>_modified.ifc)")
    default_provider = os.environ.get("LLM_PROVIDER", "").strip().lower() or "openai"
    if default_provider not in SUPPORTED_PROVIDERS:
        default_provider = "openai"
    parser.add_argument("--provider", default=default_provider, choices=SUPPORTED_PROVIDERS,
                        help=f"LLM provider (default from LLM_PROVIDER env: {default_provider})")
    parser.add_argument("--model", default=None, help="Specific model name (optional)")
    parser.add_argument("--query", default=None, help="Single query to execute (requires LLM)")
    parser.add_argument("--direct-edit", default=None, dest="direct_edit",
                        help="Direct edit operations without LLM (see syntax below)")
    parser.add_argument("--verify", action="store_true", help="Run model verification checks")
    parser.add_argument("--summary", action="store_true", help="Print model summary")
    parser.add_argument("--interactive", action="store_true", help="Interactive chat mode")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Summary mode (no LLM needed)
    if args.summary:
        _print_summary(args.ifc)
        return

    # Verify mode (no LLM needed)
    if args.verify:
        _run_verification(args.ifc)
        return

    # Direct edit mode (no LLM needed)
    if args.direct_edit:
        _run_direct_edit(args.ifc, args.direct_edit, args.output)
        return

    # Query or interactive mode (needs LLM)
    if args.query:
        _run_single_query(args.ifc, args.query, args.provider, args.model, args.output)
    elif args.interactive:
        _run_interactive(args.ifc, args.provider, args.model, args.output)
    else:
        _print_summary(args.ifc)
        print()
        _run_verification(args.ifc)


def _print_summary(ifc_path: str):
    print(f"Loading IFC file: {ifc_path}")
    ifc_parser = IFCParser(ifc_path)
    print(ifc_parser.serialize_context())


def _run_verification(ifc_path: str):
    print(f"Verifying IFC model: {ifc_path}")
    ifc_parser = IFCParser(ifc_path)
    verifier = RuleBasedVerifier(ifc_parser)
    results = verifier.run_all_checks()
    print(format_verification_report(results))


def _run_direct_edit(ifc_path: str, edit_spec: str, output_path: str = None):
    """Execute edits without LLM and save the result."""
    commands = _parse_direct_edits(edit_spec)
    if not commands:
        print("No valid edit operations parsed.")
        return

    print(f"Loading IFC file: {ifc_path}")
    print(f"Operations to execute: {len(commands)}")
    for i, cmd in enumerate(commands, 1):
        print(f"  {i}. {cmd['tool']} → {cmd['input']}")
    print("-" * 60)

    wf = IFCAgentWorkflow(ifc_path, output_path=output_path)
    result = wf.direct_edit(commands)

    print(result["answer"])

    if result.get("output_path"):
        size_kb = os.path.getsize(result["output_path"]) / 1024
        print(f"\nOutput IFC saved to: {result['output_path']} ({size_kb:.0f} KB)")

    # Quick validation of the output
    print("\nPost-edit validation:")
    out_parser = IFCParser(result["output_path"])
    verifier = RuleBasedVerifier(out_parser)
    vr = verifier.run_all_checks()
    print(f"  Checks: {vr['passed']} passed, {vr['failed']} failed, {vr['warnings']} warnings")
    if vr["issues"]:
        for issue in vr["issues"][:5]:
            sev = issue.get("severity", "info")
            msg = issue.get("message", str(issue))
            print(f"  [{sev.upper():>7}] {msg}")


def _run_single_query(ifc_path: str, query: str, provider: str,
                      model: str = None, output_path: str = None):
    print(f"Loading IFC file: {ifc_path}")
    print(f"Provider: {provider} | Model: {model or 'default'}")
    print(f"Query: {query}")
    print("-" * 60)

    wf = IFCAgentWorkflow(ifc_path, provider=provider, model=model,
                          output_path=output_path)
    result = wf.run(query)

    print(f"\nIntent: {result['intent']}")
    print(f"\nAnswer:\n{result['answer']}")

    if result.get("operations"):
        print(f"\nOperations ({len(result['operations'])}):")
        for op in result["operations"]:
            status = "OK" if op["status"] == "success" else "FAIL"
            print(f"  [{status}] {op['step']}: {op['result']}")

    if result.get("output_path"):
        print(f"\nModified IFC saved to: {result['output_path']}")


def _run_interactive(ifc_path: str, provider: str, model: str = None,
                     output_path: str = None):
    print("IFC-Agent Interactive Mode")
    print(f"IFC file: {ifc_path}")
    print(f"Provider: {provider} | Model: {model or 'default'}")
    print("Commands: 'quit', 'verify', 'summary', 'save [path]'")
    print("=" * 60)

    wf = IFCAgentWorkflow(ifc_path, provider=provider, model=model,
                          output_path=output_path)

    while True:
        try:
            query = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        if query.lower() == "verify":
            verifier = RuleBasedVerifier(wf.parser)
            results = verifier.run_all_checks()
            print(format_verification_report(results))
            continue
        if query.lower() == "summary":
            print(wf.parser.serialize_context())
            continue
        if query.lower().startswith("save"):
            parts = query.split(maxsplit=1)
            save_path = parts[1] if len(parts) > 1 else wf._get_output_path()
            wf.parser.save(save_path)
            print(f"Model saved to: {save_path}")
            continue

        result = wf.run(query)
        print(f"\n[{result['intent'].upper()}]")
        print(result["answer"])

        if result.get("output_path"):
            print(f"\nModified IFC saved to: {result['output_path']}")


if __name__ == "__main__":
    main()
