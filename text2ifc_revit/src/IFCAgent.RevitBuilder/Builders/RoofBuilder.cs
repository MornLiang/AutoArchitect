// using System.Collections.Generic;
// using System.Linq;
// using Autodesk.Revit.DB;
// using IFCAgent.RevitBuilder.Schemas;
// using IFCAgent.RevitBuilder.Utils;

// namespace IFCAgent.RevitBuilder.Builders;

// public static class RoofBuilder
// {
//     public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
//     {
//         // FootPrintRoof requires a sloped-glazing-or-roof RoofType.  Pick
//         // the first available; users can swap in a richer type by editing
//         // the project template.
//         var roofType = new FilteredElementCollector(ctx.Doc)
//             .OfClass(typeof(RoofType))
//             .Cast<RoofType>()
//             .FirstOrDefault();

//         foreach (var r in storey.Roofs)
//         {
//             if (roofType == null)
//             {
//                 // Fallback: extrude a thin prism as a DirectShape — at least
//                 // the geometry is there even if the IFC class won't be
//                 // IfcRoof.
//                 FallbackPrism(ctx, r, level);
//                 continue;
//             }

//             var loop = FloorBuilder.MakeLoop(r.Boundary,
//                 baseZFt: level.Elevation + UnitConvert.MmToFt(r.Elevation));
//             if (loop == null) continue;

//             var footprint = new CurveArray();
//             foreach (var c in loop)
//                 footprint.Append(c);

//             var roof = ctx.Doc.Create.NewFootPrintRoof(
//                 footprint, level, roofType, out var mca);

//             // For pitched roofs, mark each footprint edge as defining slope.
//             if (r.PitchDeg > 0.01)
//             {
//                 foreach (ModelCurve mc in mca)
//                 {
//                     roof.set_DefinesSlope(mc, true);
//                     roof.set_SlopeAngle(mc, System.Math.Tan(r.PitchDeg * System.Math.PI / 180.0));
//                 }
//             }
//         }
//     }

//     private static void FallbackPrism(BuildContext ctx, RoofDto r, Level level)
//     {
//         if (r.Boundary.Count < 3) return;
//         var ptsFt = r.Boundary
//             .Select(p => new XYZ(UnitConvert.MmToFt(p[0]), UnitConvert.MmToFt(p[1]), 0))
//             .ToList();
//         double baseZ = level.Elevation + UnitConvert.MmToFt(r.Elevation);
//         double thick = UnitConvert.MmToFt(r.Thickness);
//         var solid = DirectShapeHelper.CreatePrismSolid(ptsFt, baseZ, thick);
//         var ds = DirectShape.CreateElement(ctx.Doc, new ElementId(BuiltInCategory.OST_Roofs));
//         ds.SetShape(new GeometryObject[] { solid });
//         try { ds.SetName($"Roof-{r.Id}"); } catch { }
//     }
// }







using System;
using System.Linq;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

// 双路屋顶:
//   pitch_deg == 0  -> DirectShape 棱柱，跟原 ifcopenshell 版本几何对齐，IFC 仍为 IfcRoof
//   pitch_deg > 0   -> 尝试 Revit 原生 NewFootPrintRoof 拿到真斜屋顶；任何环节失败都自动
//                      回退 DirectShape，build 不会因此挂掉
public static class RoofBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        foreach (var r in storey.Roofs)
        {
            if (r.Boundary.Count < 3)
            {
                continue;
            }

            bool wantPitched = r.PitchDeg > 0.01;
            bool madeNative = false;

            if (wantPitched)
            {
                try
                {
                    madeNative = TryNativeFootPrintRoof(ctx, r, level);
                }
                catch
                {
                    madeNative = false; // 静默回退
                }
            }

            if (!madeNative)
            {
                BuildDirectShape(ctx, r, level);
            }
        }
    }

    // ---- 原生路径 ---------------------------------------------------------

    // NewFootPrintRoof 的脾气:
    //   1. RoofType 必须是 Basic 类型，InPlace / Curtain / Mass 都不行
    //   2. footprint 应该建在 level 平面上，Revit 自己往上拉
    //   3. 高度通过 ROOF_LEVEL_OFFSET_PARAM 偏移到 roof.elevation
    //   4. 斜度通过 set_DefinesSlope + set_SlopeAngle(tan) 加到每条边上
    private static bool TryNativeFootPrintRoof(BuildContext ctx, RoofDto r, Level level)
    {
        var roofType = new FilteredElementCollector(ctx.Doc)
            .OfClass(typeof(RoofType))
            .Cast<RoofType>()
            .FirstOrDefault(t => t.IsValidObject);

        if (roofType == null)
        {
            return false;
        }

        double zFt = level.Elevation;

        var pts = r.Boundary
            .Select(p => new XYZ(
                UnitConvert.MmToFt(p[0]),
                UnitConvert.MmToFt(p[1]),
                zFt))
            .ToList();

        if (pts.Count >= 2 && pts[0].IsAlmostEqualTo(pts[^1]))
        {
            pts.RemoveAt(pts.Count - 1);
        }

        if (pts.Count < 3)
        {
            return false;
        }

        var footprint = new CurveArray();

        for (int i = 0; i < pts.Count; i++)
        {
            var a = pts[i];
            var b = pts[(i + 1) % pts.Count];

            if (a.IsAlmostEqualTo(b))
            {
                continue;
            }

            footprint.Append(Line.CreateBound(a, b));
        }

        if (footprint.Size < 3)
        {
            return false;
        }

        var roof = ctx.Doc.Create.NewFootPrintRoof(
            footprint,
            level,
            roofType,
            out var modelCurves);

        if (roof == null)
        {
            return false;
        }

        roof.get_Parameter(BuiltInParameter.ROOF_LEVEL_OFFSET_PARAM)
            ?.Set(UnitConvert.MmToFt(r.Elevation));

        double slopeTan = Math.Tan(r.PitchDeg * Math.PI / 180.0);

        foreach (ModelCurve modelCurve in modelCurves)
        {
            roof.set_DefinesSlope(modelCurve, true);
            roof.set_SlopeAngle(modelCurve, slopeTan);
        }

        roof.LookupParameter("IfcExportAs")?.Set("IfcRoof");

        return true;
    }

    // ---- 兜底路径：DirectShape 棱柱 -------------------------------------

    private static void BuildDirectShape(BuildContext ctx, RoofDto r, Level level)
    {
        var ptsFt = r.Boundary
            .Select(p => new XYZ(
                UnitConvert.MmToFt(p[0]),
                UnitConvert.MmToFt(p[1]),
                0))
            .ToList();

        double baseZ = level.Elevation + UnitConvert.MmToFt(r.Elevation);
        double thick = UnitConvert.MmToFt(r.Thickness);

        var solid = DirectShapeHelper.CreatePrismSolid(ptsFt, baseZ, thick);

        var ds = DirectShape.CreateElement(
            ctx.Doc,
            new ElementId(BuiltInCategory.OST_Roofs));

        ds.ApplicationId = "IFCAgent.RevitBuilder";
        ds.ApplicationDataId = $"Roof-{r.Id}";
        ds.SetShape(new GeometryObject[] { solid });

        try
        {
            ds.SetName($"Roof-{r.Id}");
        }
        catch
        {
            // SetName 偶发抛出异常，不影响几何创建
        }

        ds.LookupParameter("IfcExportAs")?.Set("IfcRoof");
    }
}