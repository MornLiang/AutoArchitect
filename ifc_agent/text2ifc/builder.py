"""
BuildingGraph → IFC builder (Revit C# API backend).

Replaces the previous ifcopenshell-based implementation.  The graph is
serialised to JSON, then a Revit instance is launched in the background
with the ``IFCAgent.RevitBuilder`` add-in installed.  The add-in reads
the JSON via environment variables, builds the model in Revit, exports
it to IFC, and signals completion through a status file.

Public contract is unchanged::

    build_ifc(graph, output_path, schema="IFC4") -> str   # path to .ifc

so the rest of the project (workflow.py, run_text2ifc.py, ...) is not
affected.  All ifcopenshell imports are gone from this module.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from ifc_agent.text2ifc.schemas import BuildingGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default install path for Revit 2025; override with $REVIT_EXE.
_DEFAULT_REVIT_EXE = r"C:\Program Files\Autodesk\Revit 2025\Revit.exe"

# How long we wait for Revit to finish (writes status file).
_DEFAULT_TIMEOUT_SEC = 600


class RevitBuildError(RuntimeError):
    """Raised when the Revit subprocess fails to produce an IFC file."""


# ---------------------------------------------------------------------------
# Public entrypoint — same signature as the old ifcopenshell builder
# ---------------------------------------------------------------------------

def build_ifc(graph: BuildingGraph,
              output_path: str,
              schema: str = "IFC4") -> str:
    """Build a Revit model from *graph* and export it to ``output_path``.

    The Revit ``.rvt`` is written next to the IFC (``output_path`` with
    ``.rvt`` extension).  Returns the IFC path.
    """
    if not graph.metadata.schema:
        graph.metadata.schema = schema

    output_path = os.path.abspath(output_path)
    rvt_path = os.path.splitext(output_path)[0] + ".rvt"
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    revit_exe = _resolve_revit_exe()
    timeout = float(os.environ.get("IFC_AGENT_REVIT_TIMEOUT", _DEFAULT_TIMEOUT_SEC))

    with tempfile.TemporaryDirectory(prefix="ifc_agent_revit_") as workdir:
        json_path = os.path.join(workdir, "graph.json")
        status_path = os.path.join(workdir, "status.txt")

        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(graph.to_dict(), fh, ensure_ascii=False)

        env = os.environ.copy()
        env["IFC_AGENT_GRAPH_JSON"] = json_path
        env["IFC_AGENT_RVT_OUT"] = rvt_path
        env["IFC_AGENT_IFC_OUT"] = output_path
        env["IFC_AGENT_STATUS_OUT"] = status_path
        # The C# add-in is responsible for calling Environment.Exit once
        # done, but we also pass /language ENU to bypass startup prompts.
        cmd = [revit_exe, "/language", "ENU"]

        logger.info("Launching Revit: %s", cmd[0])
        logger.debug("Graph JSON: %s", json_path)
        logger.debug("RVT out: %s, IFC out: %s", rvt_path, output_path)

        try:
            proc = subprocess.Popen(cmd, env=env)
        except FileNotFoundError as exc:
            raise RevitBuildError(
                f"Could not launch Revit at {revit_exe!r}. "
                f"Set $REVIT_EXE to the absolute path of Revit.exe."
            ) from exc

        try:
            _wait_for_status(status_path, proc, timeout)
        finally:
            if proc.poll() is None:
                # Revit didn't quit by itself within the deadline; kill it
                # so we don't leak a zombie process across runs.
                try:
                    proc.kill()
                except Exception:
                    pass

        status_text = _read_status(status_path)
        if not status_text.startswith("OK"):
            raise RevitBuildError(
                f"Revit add-in reported failure: {status_text or '(no status)'}"
            )

        if not os.path.exists(output_path):
            raise RevitBuildError(
                f"Revit add-in reported OK but {output_path} is missing."
            )

    return output_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_revit_exe() -> str:
    env_exe = os.environ.get("REVIT_EXE")
    if env_exe:
        return env_exe
    # PATH lookup (rare on Windows but free to check).
    on_path = shutil.which("Revit.exe") or shutil.which("Revit")
    if on_path:
        return on_path
    return _DEFAULT_REVIT_EXE


def _wait_for_status(status_path: str,
                     proc: subprocess.Popen,
                     timeout: float) -> None:
    """Block until *status_path* exists, the subprocess dies, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(status_path):
            return
        if proc.poll() is not None:
            # Revit exited (cleanly or not) without writing a status file.
            return
        time.sleep(0.5)
    raise RevitBuildError(
        f"Timed out after {timeout:.0f}s waiting for Revit to finish."
    )


def _read_status(status_path: str) -> str:
    try:
        with open(status_path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------

class IFCBuilder:
    """Thin shim preserving the old class-style API for any caller that
    still uses ``IFCBuilder().build(graph); builder.save(path)``.

    Internally it just delegates to :func:`build_ifc`.  The Revit pipeline
    cannot expose an in-memory model object, so :pyattr:`model` is unused.
    """

    def __init__(self, schema: str = "IFC4"):
        self.schema = schema
        self.model = None  # kept for legacy attribute access; always None
        self._graph: Optional[BuildingGraph] = None

    def build(self, graph: BuildingGraph):
        self._graph = graph
        if not graph.metadata.schema:
            graph.metadata.schema = self.schema
        return None  # the old API returned an ifcopenshell.file; nothing equivalent here

    def save(self, path: str) -> str:
        if self._graph is None:
            raise RuntimeError("Nothing to save: call build() first.")
        return build_ifc(self._graph, path, schema=self.schema)
