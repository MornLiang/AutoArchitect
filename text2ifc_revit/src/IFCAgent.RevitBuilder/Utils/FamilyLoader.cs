using System;
using System.IO;
using System.Linq;
using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Utils;

// Loads default door / window families from the Revit content library so
// we can place real IfcDoor / IfcWindow on export.  If a family can't be
// found, callers fall back to DirectShape.
public static class FamilyLoader
{
    // Default library roots; overridable via env var IFC_AGENT_FAMILY_ROOT.
    private static readonly string[] DefaultLibraryRoots = new[]
    {
        @"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\Doors",
        @"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\Windows",
        @"C:\ProgramData\Autodesk\RVT 2025\Libraries\English-Imperial\Doors",
        @"C:\ProgramData\Autodesk\RVT 2025\Libraries\English-Imperial\Windows",
    };

    public static FamilySymbol? FindOrLoadDoorSymbol(Document doc)
        => FindOrLoadFirstSymbol(doc, BuiltInCategory.OST_Doors,
            new[] { "Single-Flush.rfa", "Single-Panel 1.rfa" });

    public static FamilySymbol? FindOrLoadWindowSymbol(Document doc)
        => FindOrLoadFirstSymbol(doc, BuiltInCategory.OST_Windows,
            new[] { "Fixed.rfa", "Casement 3x3 with Trim.rfa" });

    private static FamilySymbol? FindOrLoadFirstSymbol(
        Document doc, BuiltInCategory cat, string[] candidates)
    {
        // 1. Already-loaded symbol of the right category?
        var existing = new FilteredElementCollector(doc)
            .OfClass(typeof(FamilySymbol))
            .OfCategory(cat)
            .Cast<FamilySymbol>()
            .FirstOrDefault();
        if (existing != null)
        {
            if (!existing.IsActive) existing.Activate();
            return existing;
        }

        // 2. Search the candidate file names under the library roots.
        var extraRoot = Environment.GetEnvironmentVariable("IFC_AGENT_FAMILY_ROOT");
        var roots = string.IsNullOrEmpty(extraRoot)
            ? DefaultLibraryRoots
            : new[] { extraRoot! }.Concat(DefaultLibraryRoots).ToArray();

        foreach (var root in roots)
        {
            if (!Directory.Exists(root)) continue;
            foreach (var name in candidates)
            {
                var hit = Directory.EnumerateFiles(root, name, SearchOption.AllDirectories)
                    .FirstOrDefault();
                if (hit == null) continue;

                if (doc.LoadFamily(hit, out var fam) && fam != null)
                {
                    var symId = fam.GetFamilySymbolIds().FirstOrDefault();
                    if (symId != null && symId != ElementId.InvalidElementId)
                    {
                        var sym = doc.GetElement(symId) as FamilySymbol;
                        if (sym != null && !sym.IsActive) sym.Activate();
                        return sym;
                    }
                }
            }
        }
        return null;
    }
}
