using System;
using System.IO;
using System.Text.Json;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using IFCAgent.RevitBuilder.Builders;
using IFCAgent.RevitBuilder.Export;
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

        using (var tx = new Transaction(doc, "IFCAgent.Build"))
        {
            tx.Start();

            ProjectBuilder.Apply(ctx);
            foreach (var storey in graph.Storeys)
            {
                var level = LevelBuilder.GetOrCreate(ctx, storey);
                WallBuilder.BuildAll(ctx, storey, level);
                OpeningBuilder.BuildAll(ctx, storey, level);
                ColumnBuilder.BuildAll(ctx, storey, level);
                FloorBuilder.BuildAll(ctx, storey, level);
                RoofBuilder.BuildAll(ctx, storey, level);
                RailingBuilder.BuildAll(ctx, storey, level);
                SpaceBuilder.BuildAll(ctx, storey, level);
                FurnitureBuilder.BuildAll(ctx, storey, level);
            }

            tx.Commit();
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
}
