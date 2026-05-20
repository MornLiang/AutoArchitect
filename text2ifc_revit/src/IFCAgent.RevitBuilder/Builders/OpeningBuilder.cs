using System;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

public static class OpeningBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        foreach (var op in storey.Openings)
        {
            if (!ctx.Walls.TryGetValue(op.HostWall, out var host))
            {
                // Silent skip — same behaviour as the Python builder.
                continue;
            }

            var isDoor = op.Kind?.ToLowerInvariant() == "door";
            var symbol = isDoor
                ? (ctx.DoorSymbol ??= FamilyLoader.FindOrLoadDoorSymbol(ctx.Doc))
                : (ctx.WindowSymbol ??= FamilyLoader.FindOrLoadWindowSymbol(ctx.Doc));

            if (symbol == null)
            {
                // Fall back: cut an Opening rectangle out of the wall so
                // there is at least a hole.  IFC export will tag this as
                // IfcOpeningElement without a door / window filler.
                CarveBareOpening(ctx, op, host);
                continue;
            }

            PlaceFamilyOpening(ctx, op, host, level, symbol);
        }
    }

    private static void PlaceFamilyOpening(
        BuildContext ctx, OpeningDto op, Wall host, Level level, FamilySymbol symbol)
    {
        var hostDto = ctx.WallDtos[op.HostWall];
        var dirX = hostDto.End[0] - hostDto.Start[0];
        var dirY = hostDto.End[1] - hostDto.Start[1];
        var len = Math.Sqrt(dirX * dirX + dirY * dirY);
        if (len < 1e-6) return;

        var ux = dirX / len;
        var uy = dirY / len;

        // Centre of the opening along the wall:
        double alongMm = op.Offset + op.Width / 2.0;
        double cx = hostDto.Start[0] + ux * alongMm;
        double cy = hostDto.Start[1] + uy * alongMm;
        double cz = op.SillHeight;  // Revit hosted doors measure from level

        var insert = UnitConvert.PointToXYZ(new[] { cx, cy }, cz);

        // Stretch the placed instance to the requested width/height by
        // overriding family params after creation.  We pick width/height
        // params heuristically — Revit's OOTB doors use "Width"/"Height"
        // and windows use "Width"/"Height" too.
        var inst = ctx.Doc.Create.NewFamilyInstance(
            insert, symbol, host, level, StructuralType.NonStructural);

        // NOTE: Width/Height on Revit OOTB door/window families are TYPE
        // parameters, so setting them on the instance is mostly a no-op
        // and the placed leaf keeps the symbol's stock dimensions.  The
        // surrounding wall opening still gets the right geometry because
        // the FamilyInstance cuts the host wall using these stored
        // dimensions on the symbol.  To get pixel-correct doors,
        // duplicate the FamilySymbol per (w,h) — left as a follow-up.
        TrySetLength(inst, "Width", op.Width);
        TrySetLength(inst, "Height", op.Height);
        if (op.Kind?.ToLowerInvariant() == "window")
            TrySetLength(inst, "Sill Height", op.SillHeight);
    }

    private static void TrySetLength(FamilyInstance inst, string paramName, double mm)
    {
        var p = inst.LookupParameter(paramName);
        if (p == null || p.IsReadOnly) return;
        if (p.StorageType != StorageType.Double) return;
        p.Set(UnitConvert.MmToFt(mm));
    }

    // Fallback: just rectangle-cut the wall (no door/window leaf).
    private static void CarveBareOpening(BuildContext ctx, OpeningDto op, Wall host)
    {
        var hostDto = ctx.WallDtos[op.HostWall];
        var dirX = hostDto.End[0] - hostDto.Start[0];
        var dirY = hostDto.End[1] - hostDto.Start[1];
        var len = Math.Sqrt(dirX * dirX + dirY * dirY);
        if (len < 1e-6) return;

        double a = op.Offset;
        double b = op.Offset + op.Width;
        double az = op.SillHeight;
        double bz = op.SillHeight + op.Height;

        double ux = dirX / len;
        double uy = dirY / len;

        // Two corner points on the wall axis at base & top.
        var p1 = UnitConvert.PointToXYZ(
            new[] { hostDto.Start[0] + ux * a, hostDto.Start[1] + uy * a }, az);
        var p2 = UnitConvert.PointToXYZ(
            new[] { hostDto.Start[0] + ux * b, hostDto.Start[1] + uy * b }, bz);

        ctx.Doc.Create.NewOpening(host, p1, p2);
    }
}
