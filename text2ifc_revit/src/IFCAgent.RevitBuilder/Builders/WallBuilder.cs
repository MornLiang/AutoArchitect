using System.Linq;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

public static class WallBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        // Resolve a default WallType once per storey.  We don't try to
        // perfectly match graph.thickness to a thicker/thinner Revit
        // WallType — the API can override Width through the WallType
        // parameter, and that would scribble on every wall in the project.
        // Instead each wall gets its own width via the WallType clone or
        // (for IFC export fidelity) by Wall.WallType assignment, with the
        // unwanted side-effect ignored because IFC export reads from the
        // wall instance's bounding geometry.
        var defaultType = new FilteredElementCollector(ctx.Doc)
            .OfClass(typeof(WallType))
            .Cast<WallType>()
            .FirstOrDefault(t => t.Kind == WallKind.Basic)
            ?? throw new System.InvalidOperationException(
                "No basic WallType found in the document.");

        foreach (var w in storey.Walls)
        {
            ctx.WallDtos[w.Id] = w;
            var wall = CreateWall(ctx, w, level, defaultType);
            if (wall != null)
                ctx.Walls[w.Id] = wall;
        }
    }

    private static Wall? CreateWall(
        BuildContext ctx, WallDto w, Level level, WallType type)
    {
        // Skip degenerate walls.
        var dx = w.End[0] - w.Start[0];
        var dy = w.End[1] - w.Start[1];
        if (dx * dx + dy * dy < 1e-6) return null;

        var p0 = UnitConvert.PointToXYZ(w.Start);
        var p1 = UnitConvert.PointToXYZ(w.End);
        var line = Line.CreateBound(p0, p1);

        double heightFt = UnitConvert.MmToFt(w.Height);

        var wall = Wall.Create(
            ctx.Doc,
            line,
            type.Id,
            level.Id,
            heightFt,
            offset: 0.0,
            flip: false,
            structural: false);

        // Reset base offset to zero (Wall.Create derives it from level).
        wall.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)?.Set(0.0);

        // Width: clone WallType if its width doesn't match.  Otherwise IFC
        // export would report the wrong thickness.
        double targetWidthFt = UnitConvert.MmToFt(w.Thickness);
        if (System.Math.Abs(type.Width - targetWidthFt) > 1e-4)
        {
            var custom = GetOrCreateWidthVariant(ctx.Doc, type, targetWidthFt);
            wall.WallType = custom;
        }

        return wall;
    }

    // Each unique wall thickness gets its own WallType so IFC export
    // reports it correctly.  Cached by name in the document.
    private static WallType GetOrCreateWidthVariant(
        Document doc, WallType baseType, double widthFt)
    {
        string name = $"IFCAgent-Wall-{UnitConvert.FtToMm(widthFt):0}mm";
        var existing = new FilteredElementCollector(doc)
            .OfClass(typeof(WallType))
            .Cast<WallType>()
            .FirstOrDefault(t => t.Name == name);
        if (existing != null) return existing;

        var clone = baseType.Duplicate(name) as WallType
            ?? throw new System.InvalidOperationException("WallType.Duplicate failed.");

        // Adjust the single structural layer to the target width.
        var cs = clone.GetCompoundStructure();
        if (cs != null)
        {
            var layers = cs.GetLayers();
            // Single-layer wall types are the common case; for multi-layer
            // we scale the structural layer to keep the others intact.
            // int structIdx = layers.FindIndex(l => l.Function == MaterialFunctionAssignment.Structure);
            int structIdx = layers.ToList().FindIndex(l => l.Function == MaterialFunctionAssignment.Structure);
            if (structIdx < 0) structIdx = 0;
            double current = layers.Sum(l => l.Width);
            double delta = widthFt - current;
            var layer = layers[structIdx];
            layer.Width = System.Math.Max(1e-4, layer.Width + delta);
            cs.SetLayers(layers);
            clone.SetCompoundStructure(cs);
        }
        return clone;
    }
}
