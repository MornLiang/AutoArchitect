using System.Collections.Generic;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

// Furniture is always emitted as a DirectShape box sized by the
// FurnitureDto.  IfcExportAs is set so Revit's IFC exporter classifies
// the export entity correctly (IfcFurniture / IfcSanitaryTerminal / ...).
public static class FurnitureBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        // Furniture lives in two places on the graph: directly on the
        // storey and nested inside spaces.  We emit both.
        foreach (var f in storey.Furniture)
            Place(ctx, f, level);
        foreach (var s in storey.Spaces)
            foreach (var f in s.Furniture)
                Place(ctx, f, level);
    }

    private static void Place(BuildContext ctx, FurnitureDto f, Level level)
    {
        double dx = UnitConvert.MmToFt(f.Size[0]);
        double dy = UnitConvert.MmToFt(f.Size[1]);
        double dz = UnitConvert.MmToFt(f.Size[2]);

        // The DTO's position is the XY centre of the footprint; elevation
        // is the bottom of the object relative to the storey floor.  Our
        // helper takes the SOLID's centre, so shift by dz/2.
        var origin = new XYZ(
            UnitConvert.MmToFt(f.Position[0]),
            UnitConvert.MmToFt(f.Position[1]),
            level.Elevation + UnitConvert.MmToFt(f.Elevation) + dz / 2.0);

        double rot = f.RotZDeg * System.Math.PI / 180.0;

        var cat = ResolveCategory(f.IfcClass);
        var ds = DirectShapeHelper.CreateBoxShape(
            ctx.Doc, cat, origin, dx, dy, dz, rot,
            name: string.IsNullOrEmpty(f.Name) ? $"Furn-{f.Id}" : f.Name);

        // Hint Revit's IFC exporter: "IfcFurniture.CHAIR" et al.
        string ifcAs = string.IsNullOrEmpty(f.PredefinedType) || f.PredefinedType == "NOTDEFINED"
            ? f.IfcClass
            : $"{f.IfcClass}.{f.PredefinedType}";
        ds.LookupParameter("IfcExportAs")?.Set(ifcAs);
    }

    private static BuiltInCategory ResolveCategory(string ifcClass) => ifcClass switch
    {
        "IfcFurniture"             => BuiltInCategory.OST_Furniture,
        "IfcSanitaryTerminal"      => BuiltInCategory.OST_PlumbingFixtures,
        "IfcLightFixture"          => BuiltInCategory.OST_LightingFixtures,
        "IfcStair" or "IfcStairFlight" => BuiltInCategory.OST_Stairs,
        _                          => BuiltInCategory.OST_GenericModel,
    };
}
