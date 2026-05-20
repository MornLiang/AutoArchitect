# IFCAgent.RevitBuilder

Revit C# add-in that replaces the `ifcopenshell` backend in
`ifc_agent.text2ifc.builder`.  Python serialises a `BuildingGraph` to
JSON; this add-in reads the JSON, builds a Revit model, exports `.rvt`
and `.ifc`, then exits.

```
[Python: build_ifc(graph, out.ifc)]
   │ writes graph.json + sets env vars
   ▼
[Revit.exe /language ENU]   <-- launched by Python
   │ loads IFCAgent.RevitBuilder.addin
   │ BuildOnStartupApp sees IFC_AGENT_GRAPH_JSON, runs Pipeline.Run
   │ saves out.rvt, exports out.ifc, writes status.txt, Environment.Exit
   ▼
[Python reads status, returns out.ifc path]
```

## Prerequisites (Windows machine)

- Windows 10 / 11 (Revit doesn't run on Linux/macOS)
- Revit **2025** installed (default path: `C:\Program Files\Autodesk\Revit 2025\`)
- .NET 8 SDK — <https://dotnet.microsoft.com/download>
- Visual Studio 2022 (optional, for IDE) or just `dotnet` CLI
- Python 3.10+ with the rest of the `IFC_Agent` requirements

## Build

From this directory:

```powershell
dotnet build IFCAgent.RevitBuilder.sln -c Release
```

If your Revit lives somewhere other than the default path, pass:

```powershell
dotnet build IFCAgent.RevitBuilder.sln -c Release `
    /p:RevitInstallPath="D:\Autodesk\Revit 2025"
```

Output lands in `src/IFCAgent.RevitBuilder/bin/Release/net8.0-windows/`:

- `IFCAgent.RevitBuilder.dll`
- `IFCAgent.RevitBuilder.addin`

## Install the add-in

Revit reads add-ins from one of:

- All users: `C:\ProgramData\Autodesk\Revit\Addins\2025\`
- Current user: `%APPDATA%\Autodesk\Revit\Addins\2025\`

Copy **both** the `.dll` and the `.addin` to one of those folders:

```powershell
$dst = "$env:APPDATA\Autodesk\Revit\Addins\2025"
New-Item -Force -ItemType Directory $dst | Out-Null
Copy-Item -Force `
    src\IFCAgent.RevitBuilder\bin\Release\net8.0-windows\IFCAgent.RevitBuilder.dll, `
    src\IFCAgent.RevitBuilder\bin\Release\net8.0-windows\IFCAgent.RevitBuilder.addin `
    $dst
```

The add-in stays inert in interactive Revit sessions because the
auto-fire app exits early when the env vars aren't set, and the manual
command only fires when the user clicks **Add-Ins → Build IFC from
BuildingGraph JSON**.

## Configure the Python side

Optional environment variables (read by `ifc_agent/text2ifc/builder.py`):

| Variable                  | Default                                                      | Meaning                                |
|---------------------------|--------------------------------------------------------------|----------------------------------------|
| `REVIT_EXE`               | `C:\Program Files\Autodesk\Revit 2025\Revit.exe`             | Path to Revit's executable             |
| `IFC_AGENT_REVIT_TIMEOUT` | `600` (seconds)                                              | How long to wait for Revit to finish   |
| `IFC_AGENT_TEMPLATE`      | _(unset)_                                                    | Optional `.rte` template path          |
| `IFC_AGENT_FAMILY_ROOT`   | _(unset)_                                                    | Extra root for Door/Window `.rfa` files |

The add-in side also receives these env vars (set automatically by the
Python builder; you only need them if invoking the add-in by hand):

| Variable                | Set by Python? | Notes                                       |
|-------------------------|----------------|---------------------------------------------|
| `IFC_AGENT_GRAPH_JSON`  | yes            | absolute path to `BuildingGraph` JSON       |
| `IFC_AGENT_RVT_OUT`     | yes            | where to write `.rvt`                       |
| `IFC_AGENT_IFC_OUT`     | yes            | where to write `.ifc`                       |
| `IFC_AGENT_STATUS_OUT`  | yes            | status file: `OK` or `ERR: <message>`       |
| `IFC_AGENT_TEMPLATE`    | optional       | `.rte` project template                     |

## Usage from Python

Nothing changes at the call site — `workflow.py` already calls
`build_ifc(graph, ifc_path)` and the contract is preserved:

```python
from ifc_agent.text2ifc.builder import build_ifc
build_ifc(graph, "out.ifc")    # generates out.ifc AND out.rvt
```

## Manual smoke test

1. Write a `BuildingGraph` to JSON (the Python builder already does
   this; you can also drop one in by hand).
2. From a PowerShell session:

   ```powershell
   $env:IFC_AGENT_GRAPH_JSON = "C:\tmp\graph.json"
   $env:IFC_AGENT_RVT_OUT    = "C:\tmp\out.rvt"
   $env:IFC_AGENT_IFC_OUT    = "C:\tmp\out.ifc"
   $env:IFC_AGENT_STATUS_OUT = "C:\tmp\status.txt"
   & "C:\Program Files\Autodesk\Revit 2025\Revit.exe" /language ENU
   ```

3. Revit will start, run the build, write the files, and exit. Check
   `status.txt` for `OK` or an `ERR:` line.

## Known gaps and follow-ups

These are deliberate trade-offs from the first pass — file an issue if
they bite you on a real model.

- **Headless-ish, not headless.** Revit briefly shows its splash and
  home screen during the run because Revit has no true non-UI mode.
- **Doors/windows** depend on Revit's bundled `Single-Flush.rfa` and
  `Fixed.rfa` content (English library).  When they're missing the
  add-in falls back to bare rectangular wall openings.
- **Columns, furniture, railings** use `DirectShape` boxes — geometry
  is faithful but the IFC export class is hinted via `IfcExportAs`
  rather than a true Revit family.  Swap in `.rfa` content if you want
  parametric instances.
- **Materials.** The graph carries material names ("Concrete", "Steel",
  ...) but materials aren't applied yet; everything inherits the
  WallType / FloorType / DirectShape default material.  Add a
  `MaterialAssigner` if you need this.
- **Roofs with `pitch_deg > 0`** use `NewFootPrintRoof` with all edges
  marked as defining slope; this produces a hipped roof.  Single-slope
  shed roofs aren't supported yet.
- **Spaces (rooms)** require enclosed wall regions; if `WallBuilder`
  didn't fully close a region, `SpaceBuilder.NewRoom` will silently
  skip it.

## File layout

```
text2ifc_revit/
├── IFCAgent.RevitBuilder.sln
├── README.md                          (this file)
└── src/
    └── IFCAgent.RevitBuilder/
        ├── IFCAgent.RevitBuilder.csproj
        ├── IFCAgent.RevitBuilder.addin
        ├── BuildContext.cs            shared per-build state
        ├── BuildFromJsonCommand.cs    IExternalCommand (manual)
        ├── BuildOnStartupApp.cs       IExternalApplication (auto)
        ├── Pipeline.cs                shared build logic
        ├── Schemas/
        │   └── BuildingGraph.cs       POCOs mirroring schemas.py
        ├── Builders/
        │   ├── ProjectBuilder.cs
        │   ├── LevelBuilder.cs
        │   ├── WallBuilder.cs
        │   ├── OpeningBuilder.cs
        │   ├── ColumnBuilder.cs
        │   ├── FloorBuilder.cs
        │   ├── RoofBuilder.cs
        │   ├── SpaceBuilder.cs
        │   ├── RailingBuilder.cs
        │   └── FurnitureBuilder.cs
        ├── Export/
        │   └── IfcExporter.cs
        └── Utils/
            ├── UnitConvert.cs
            ├── DirectShapeHelper.cs
            └── FamilyLoader.cs
```
