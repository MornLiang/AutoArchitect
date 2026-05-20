using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Builders;

public static class ProjectBuilder
{
    public static void Apply(BuildContext ctx)
    {
        var meta = ctx.Graph.Metadata;
        var info = ctx.Doc.ProjectInformation;
        info.Name = meta.ProjectName;
        info.BuildingName = meta.Name;

        // Site name lives on a separate element in Revit; we just stuff it
        // into the project address comments since BasePoint isn't user-named.
        if (!string.IsNullOrEmpty(meta.SiteName))
            info.Address = meta.SiteName;
    }
}
