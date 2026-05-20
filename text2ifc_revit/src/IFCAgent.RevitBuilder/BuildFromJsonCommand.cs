using System;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace IFCAgent.RevitBuilder;

// Manual entry point: appears in the Add-Ins menu after the .addin is
// installed.  Useful for poking the pipeline interactively; the Python
// launcher uses BuildOnStartupApp (auto-fire) instead.
//
// Inputs (environment variables, identical to the auto-fire path):
//   IFC_AGENT_GRAPH_JSON  -> absolute path to BuildingGraph JSON
//   IFC_AGENT_RVT_OUT     -> absolute path to write the .rvt
//   IFC_AGENT_IFC_OUT     -> absolute path to write the .ifc
//   IFC_AGENT_TEMPLATE    -> (optional) absolute path to an .rte template
//   IFC_AGENT_STATUS_OUT  -> (optional) status file written "OK"/"ERR: ..."
[Transaction(TransactionMode.Manual)]
[Regeneration(RegenerationOption.Manual)]
public sealed class BuildFromJsonCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData,
                          ref string message,
                          ElementSet elements)
    {
        var status = Environment.GetEnvironmentVariable("IFC_AGENT_STATUS_OUT");
        try
        {
            Pipeline.Run(
                commandData.Application.Application,
                jsonPath: Require("IFC_AGENT_GRAPH_JSON"),
                rvtOut:   Require("IFC_AGENT_RVT_OUT"),
                ifcOut:   Require("IFC_AGENT_IFC_OUT"),
                templatePath: Environment.GetEnvironmentVariable("IFC_AGENT_TEMPLATE"));
            Pipeline.WriteStatus(status, "OK");
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            message = ex.ToString();
            Pipeline.WriteStatus(status, "ERR: " + ex.Message);
            return Result.Failed;
        }
    }

    private static string Require(string name)
    {
        var v = Environment.GetEnvironmentVariable(name);
        if (string.IsNullOrWhiteSpace(v))
            throw new InvalidOperationException(
                $"Environment variable {name} is not set; cannot proceed.");
        return v!;
    }
}
