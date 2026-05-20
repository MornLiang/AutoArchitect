using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

public static class FloorBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        var floorType = new FilteredElementCollector(ctx.Doc)
            .OfClass(typeof(FloorType))
            .Cast<FloorType>()
            .FirstOrDefault();
        if (floorType == null) return;

        foreach (var s in storey.Slabs)
        {
            var loop = MakeLoop(s.Boundary, baseZFt: level.Elevation + UnitConvert.MmToFt(s.Elevation));
            if (loop == null) continue;

            // Revit 2022+ API: Floor.Create(doc, IList<CurveLoop>, typeId, levelId)
            var floor = Floor.Create(
                ctx.Doc,
                new List<CurveLoop> { loop },
                floorType.Id,
                level.Id);

            // Thickness is driven by the FloorType; we don't try to switch
            // type per slab (would explode the family count).  The IFC
            // exporter still reports the geometric thickness so downstream
            // tools see the right shape.

            // Mark as roof if requested.  Revit doesn't really model this
            // on a Floor — we tag via the IFC export PredefinedType
            // comments parameter, which Revit's IFC exporter respects.
            if (s.PredefinedType?.ToUpperInvariant() == "ROOF")
            {
                var p = floor.LookupParameter("IfcExportAs");
                p?.Set("IfcSlab.ROOF");
            }
            else
            {
                var p = floor.LookupParameter("IfcExportAs");
                p?.Set("IfcSlab." + (s.PredefinedType ?? "FLOOR"));
            }
        }
    }

    internal static CurveLoop? MakeLoop(List<double[]> boundaryMm, double baseZFt)
    {
        if (boundaryMm.Count < 3) return null;

        var pts = boundaryMm
            .Select(p => new XYZ(UnitConvert.MmToFt(p[0]), UnitConvert.MmToFt(p[1]), baseZFt))
            .ToList();

        // Drop duplicate last==first if the Python side closed the loop.
        if (pts.Count >= 2 && pts[0].IsAlmostEqualTo(pts[^1]))
            pts.RemoveAt(pts.Count - 1);

        if (pts.Count < 3) return null;

        var loop = new CurveLoop();
        for (int i = 0; i < pts.Count; i++)
        {
            var a = pts[i];
            var b = pts[(i + 1) % pts.Count];
            if (a.IsAlmostEqualTo(b)) continue;
            loop.Append(Line.CreateBound(a, b));
        }
        return loop;
    }
}
