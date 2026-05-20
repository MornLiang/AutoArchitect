using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Utils;

// DirectShape is Revit's "trust me, just put this solid here" element.
// We use it as a fallback for furniture, railings, and roof slabs of
// arbitrary shape where the built-in Revit families are too restrictive.
public static class DirectShapeHelper
{
    // Axis-aligned box centred on (cx,cy) with bottom at z=baseZ.
    // All inputs in Revit internal units (feet).
    public static Solid CreateBoxSolid(
        XYZ origin, double dx, double dy, double dz, double rotZRad = 0.0)
    {
        // Build a closed rectangular CurveLoop in the XY plane, centred
        // on origin and (optionally) rotated about its centre.
        var hx = dx / 2.0;
        var hy = dy / 2.0;

        XYZ[] local = new[]
        {
            new XYZ(-hx, -hy, 0),
            new XYZ( hx, -hy, 0),
            new XYZ( hx,  hy, 0),
            new XYZ(-hx,  hy, 0),
        };

        var cos = Math.Cos(rotZRad);
        var sin = Math.Sin(rotZRad);
        XYZ World(XYZ p) => new XYZ(
            origin.X + p.X * cos - p.Y * sin,
            origin.Y + p.X * sin + p.Y * cos,
            origin.Z + p.Z);

        var profile = new CurveLoop();
        for (int i = 0; i < 4; i++)
        {
            var a = World(local[i]);
            var b = World(local[(i + 1) % 4]);
            profile.Append(Line.CreateBound(a, b));
        }

        return GeometryCreationUtilities.CreateExtrusionGeometry(
            new List<CurveLoop> { profile },
            XYZ.BasisZ,
            dz);
    }

    public static DirectShape CreateBoxShape(
        Document doc,
        BuiltInCategory category,
        XYZ origin,
        double dx, double dy, double dz,
        double rotZRad,
        string name)
    {
        var solid = CreateBoxSolid(origin, dx, dy, dz, rotZRad);
        var ds = DirectShape.CreateElement(doc, new ElementId(category));
        ds.ApplicationId = "IFCAgent.RevitBuilder";
        ds.ApplicationDataId = name;
        ds.SetShape(new GeometryObject[] { solid });
        try { ds.SetName(name); } catch { /* SetName may throw on older API; non-fatal */ }
        return ds;
    }

    // Generic polygonal prism (e.g. for floors / roofs with arbitrary
    // boundary polygons).  Polygon is closed automatically.
    public static Solid CreatePrismSolid(
        IList<XYZ> polygonXY, double baseZ, double thickness)
    {
        var loop = new CurveLoop();
        int n = polygonXY.Count;
        for (int i = 0; i < n; i++)
        {
            var a = new XYZ(polygonXY[i].X, polygonXY[i].Y, baseZ);
            var b = new XYZ(polygonXY[(i + 1) % n].X, polygonXY[(i + 1) % n].Y, baseZ);
            if (a.IsAlmostEqualTo(b)) continue;
            loop.Append(Line.CreateBound(a, b));
        }

        return GeometryCreationUtilities.CreateExtrusionGeometry(
            new List<CurveLoop> { loop },
            XYZ.BasisZ,
            thickness);
    }
}
