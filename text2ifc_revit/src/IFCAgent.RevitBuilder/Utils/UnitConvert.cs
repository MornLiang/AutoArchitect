using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Utils;

// BuildingGraph dims are in millimetres, but the Revit API uses
// "internal units" = decimal feet for length.  All builders call MmToFt /
// PointToXYZ before passing values to the Revit API.
public static class UnitConvert
{
    private const double MmPerFoot = 304.8;

    public static double MmToFt(double mm) => mm / MmPerFoot;
    public static double FtToMm(double ft) => ft * MmPerFoot;

    public static XYZ PointToXYZ(double xMm, double yMm, double zMm = 0.0)
        => new XYZ(MmToFt(xMm), MmToFt(yMm), MmToFt(zMm));

    public static XYZ PointToXYZ(double[] pMm, double zMm = 0.0)
        => new XYZ(MmToFt(pMm[0]), MmToFt(pMm[1]), MmToFt(zMm));
}
