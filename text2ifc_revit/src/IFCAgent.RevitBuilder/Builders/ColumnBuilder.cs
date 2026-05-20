using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

public static class ColumnBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        foreach (var c in storey.Columns)
        {
            // Columns aren't part of any default WallType/FamilySymbol, so
            // we always lay them down as a DirectShape box.  That keeps us
            // independent of which content library the user has, and IFC
            // export will emit IfcBuildingElementProxy with the right
            // geometry (better than nothing).
            var origin = UnitConvert.PointToXYZ(c.Position, c.Height / 2.0 + 0.0);
            // origin.Z is mid-height of the column; level provides the floor
            // datum so we keep mm-space simple by translating.
            var baseZFt = level.Elevation;
            origin = new XYZ(origin.X, origin.Y, baseZFt + UnitConvert.MmToFt(c.Height) / 2.0);

            double dxFt = UnitConvert.MmToFt(c.Section[0]);
            double dyFt = UnitConvert.MmToFt(c.Section[1]);
            double dzFt = UnitConvert.MmToFt(c.Height);

            DirectShapeHelper.CreateBoxShape(
                ctx.Doc,
                BuiltInCategory.OST_StructuralColumns,
                origin,
                dxFt, dyFt, dzFt,
                rotZRad: 0.0,
                name: $"Column-{c.Id}");
        }
    }
}
