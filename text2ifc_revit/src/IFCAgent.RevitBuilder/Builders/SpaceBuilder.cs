using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Architecture;
using IFCAgent.RevitBuilder.Schemas;
using IFCAgent.RevitBuilder.Utils;

namespace IFCAgent.RevitBuilder.Builders;

// Spaces in the BuildingGraph map to Revit Rooms.  Revit Room placement
// expects a bounded space (i.e. walls already drawn that enclose the
// point); we rely on WallBuilder having already run and place a Room at
// the centroid of the SpaceDto boundary.
public static class SpaceBuilder
{
    public static void BuildAll(BuildContext ctx, StoreyDto storey, Level level)
    {
        if (storey.Spaces.Count == 0) return;

        // Phase placement requires a Phase — use the document's first phase.
        var phase = ctx.Doc.Phases.Cast<Phase>().FirstOrDefault();

        foreach (var s in storey.Spaces)
        {
            if (s.Boundary.Count < 3) continue;
            var centroid = Centroid(s.Boundary);
            var location = UnitConvert.PointToXYZ(centroid);

            Room? room = null;
            try
            {
                room = phase != null
                    ? ctx.Doc.Create.NewRoom(level, new UV(location.X, location.Y))
                    : ctx.Doc.Create.NewRoom(level, new UV(location.X, location.Y));
            }
            catch
            {
                // Room placement fails if the point isn't inside an enclosed
                // region.  In that case we skip — IFC export will still get
                // the structural shell, just without IfcSpace records.
            }
            if (room == null) continue;

            room.Name = string.IsNullOrEmpty(s.Name) ? s.Id : s.Name;
            var occ = room.LookupParameter("Occupancy");
            occ?.Set(s.Function ?? "");
        }
    }

    private static double[] Centroid(List<double[]> polygonMm)
    {
        double sx = 0, sy = 0;
        foreach (var p in polygonMm) { sx += p[0]; sy += p[1]; }
        return new[] { sx / polygonMm.Count, sy / polygonMm.Count };
    }
}
