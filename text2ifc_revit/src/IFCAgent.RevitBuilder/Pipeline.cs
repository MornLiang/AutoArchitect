using System;
using System.IO;
using System.Text.Json;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Builders;
using IFCAgent.RevitBuilder.Export;
using IFCAgent.RevitBuilder.Runtime;
using IFCAgent.RevitBuilder.Schemas;

namespace IFCAgent.RevitBuilder;

// Shared build logic, callable from either an IExternalCommand (manual
// testing through the Add-Ins menu) or from an IExternalApplication that
// auto-fires on startup (the Python launcher's path).
public static class Pipeline
{
    public static void Run(Application app,
                           string jsonPath,
                           string rvtOut,
                           string ifcOut,
                           string? templatePath)
    {
        var graph = LoadGraph(jsonPath);

        Document doc = string.IsNullOrEmpty(templatePath)
            ? app.NewProjectDocument(UnitSystem.Metric)
            : app.NewProjectDocument(templatePath!);

        var ctx = new BuildContext { Doc = doc, Graph = graph };

        RequireCommitted(doc, "project", () => ProjectBuilder.Apply(ctx));
        foreach (var storey in graph.Storeys)
        {
            Level? level = null;
            RequireCommitted(doc, $"level:{storey.Id}",
                () => level = LevelBuilder.GetOrCreate(ctx, storey));
            if (level == null)
                throw new InvalidOperationException($"Failed to create level {storey.Id}.");

            RequireCommitted(doc, $"walls:{storey.Id}",
                () => WallBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"openings:{storey.Id}",
                () => OpeningBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"columns:{storey.Id}",
                () => ColumnBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"floors:{storey.Id}",
                () => FloorBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"roofs:{storey.Id}",
                () => RoofBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"railings:{storey.Id}",
                () => RailingBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"spaces:{storey.Id}",
                () => SpaceBuilder.BuildAll(ctx, storey, level));
            RequireCommitted(doc, $"furniture:{storey.Id}",
                () => FurnitureBuilder.BuildAll(ctx, storey, level));
        }

        Directory.CreateDirectory(Path.GetDirectoryName(rvtOut)!);
        doc.SaveAs(rvtOut, new SaveAsOptions { OverwriteExistingFile = true });

        Directory.CreateDirectory(Path.GetDirectoryName(ifcOut)!);
        IfcExporter.Export(doc, ifcOut, graph.Metadata.Schema);

        doc.Close(false);
    }

    public static BuildingGraphDto LoadGraph(string path)
    {
        var opts = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
            AllowTrailingCommas = true,
            ReadCommentHandling = JsonCommentHandling.Skip,
        };
        var text = File.ReadAllText(path);
        return JsonSerializer.Deserialize<BuildingGraphDto>(text, opts)
            ?? throw new InvalidDataException("Failed to deserialize BuildingGraph.");
    }

    public static void WriteStatus(string? path, string body)
    {
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path!)!);
            File.WriteAllText(path!, body);
        }
        catch { /* best-effort; don't mask the real exit */ }
    }

    private static void RequireCommitted(Document doc, string actionId, Action action)
    {
        var result = SafeTransaction.Run(doc, actionId, action);
        if (result.Status != "committed")
            throw new InvalidOperationException(result.ToJson());
    }
}
