using System.Collections.Generic;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;

namespace IFCAgent.RevitBuilder;

// Shared per-build state passed between builders.  Mirrors the dict
// caches in ifc_agent/text2ifc/builder.py (_storey_entities, _wall_entities).
public sealed class BuildContext
{
    public required Document Doc { get; init; }
    public required BuildingGraphDto Graph { get; init; }

    // storey.id -> Revit Level
    public Dictionary<string, Level> Levels { get; } = new();

    // wall.id -> Revit Wall (so openings can resolve their host)
    public Dictionary<string, Wall> Walls { get; } = new();

    // For loose lookups: wall.id -> the source WallDto (for thickness etc.)
    public Dictionary<string, WallDto> WallDtos { get; } = new();

    // Lazily resolved family symbols.  Builders ask via the helpers in
    // Utils.FamilyLoader and we cache the result for subsequent calls.
    public FamilySymbol? DoorSymbol { get; set; }
    public FamilySymbol? WindowSymbol { get; set; }
}
