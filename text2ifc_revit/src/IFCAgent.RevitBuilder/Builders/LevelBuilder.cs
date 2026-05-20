using System.Linq;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

public static class LevelBuilder
{
    public static Level GetOrCreate(BuildContext ctx, StoreyDto storey)
    {
        if (ctx.Levels.TryGetValue(storey.Id, out var cached))
            return cached;

        // Reuse a level at this elevation if one already exists (Revit
        // templates ship with "Level 1" / "Level 2").
        double elFt = UnitConvert.MmToFt(storey.Elevation);
        var existing = new FilteredElementCollector(ctx.Doc)
            .OfClass(typeof(Level))
            .Cast<Level>()
            .FirstOrDefault(l => System.Math.Abs(l.Elevation - elFt) < 1e-4);

        if (existing != null)
        {
            try { existing.Name = string.IsNullOrWhiteSpace(storey.Name) ? storey.Id : storey.Name; }
            catch { /* duplicate-name errors are non-fatal */ }
            ctx.Levels[storey.Id] = existing;
            return existing;
        }

        var lvl = Level.Create(ctx.Doc, elFt);
        try { lvl.Name = string.IsNullOrWhiteSpace(storey.Name) ? storey.Id : storey.Name; }
        catch { }
        ctx.Levels[storey.Id] = lvl;
        return lvl;
    }
}
