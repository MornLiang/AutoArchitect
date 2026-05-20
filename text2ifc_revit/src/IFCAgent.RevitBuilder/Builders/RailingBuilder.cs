using System.Linq;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

// Revit's native railing API is very particular (needs a host stair/floor
// reference, RailingType etc.).  For now we fall back to DirectShape
// strips along each polyline segment — visually faithful and IFC exports
// them as IfcRailing if we set IfcExportAs.
public static class RailingBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        double thicknessMm = 50.0;  // visual thickness; IFC exporter uses geometry

        foreach (var r in storey.Railings)
        {
            if (r.Polyline.Count < 2) continue;

            for (int i = 0; i < r.Polyline.Count - 1; i++)
            {
                var a = r.Polyline[i];
                var b = r.Polyline[i + 1];
                double dx = b[0] - a[0];
                double dy = b[1] - a[1];
                double len = System.Math.Sqrt(dx * dx + dy * dy);
                if (len < 1e-3) continue;

                double cxMm = (a[0] + b[0]) / 2.0;
                double cyMm = (a[1] + b[1]) / 2.0;
                double angle = System.Math.Atan2(dy, dx);

                var origin = new XYZ(
                    UnitConvert.MmToFt(cxMm),
                    UnitConvert.MmToFt(cyMm),
                    level.Elevation + UnitConvert.MmToFt(r.Elevation + r.Height / 2.0));

                var ds = DirectShapeHelper.CreateBoxShape(
                    ctx.Doc,
                    BuiltInCategory.OST_StairsRailing,
                    origin,
                    UnitConvert.MmToFt(len),
                    UnitConvert.MmToFt(thicknessMm),
                    UnitConvert.MmToFt(r.Height),
                    rotZRad: angle,
                    name: $"Railing-{r.Id}-{i}");
                // Hint Revit's IFC exporter what to call this.
                ds.LookupParameter("IfcExportAs")?.Set("IfcRailing");
            }
        }
    }
}
