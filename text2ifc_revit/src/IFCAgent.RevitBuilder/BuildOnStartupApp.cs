using System;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;

namespace IFCAgent.RevitBuilder;

// Auto-fire path used by the Python launcher.
//
// On Revit startup, if IFC_AGENT_GRAPH_JSON is set the add-in subscribes
// to ApplicationInitialized and, once Revit is ready, runs Pipeline.Run
// using the same env-var contract as BuildFromJsonCommand, writes a
// status file, then forcibly exits the process so the Python side can
// proceed.
//
// We do NOT use Idling here because Idling requires an active document;
// in our headless-ish flow there is no doc yet, so ApplicationInitialized
// is the right hook.
public sealed class BuildOnStartupApp : IExternalApplication
{
    public Result OnStartup(UIControlledApplication app)
    {
        // Bail out cheaply if we weren't asked to do anything — keeps the
        // add-in safe to install for interactive Revit sessions.
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("IFC_AGENT_GRAPH_JSON")))
            return Result.Succeeded;

        app.ControlledApplication.ApplicationInitialized += OnAppInitialized;
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication app) => Result.Succeeded;

    private void OnAppInitialized(object? sender, ApplicationInitializedEventArgs e)
    {
        var status = Environment.GetEnvironmentVariable("IFC_AGENT_STATUS_OUT");
        int exitCode = 0;
        try
        {
            // sender is an Autodesk.Revit.ApplicationServices.Application
            var application = (Application)sender!;
            Pipeline.Run(
                application,
                jsonPath: Require("IFC_AGENT_GRAPH_JSON"),
                rvtOut:   Require("IFC_AGENT_RVT_OUT"),
                ifcOut:   Require("IFC_AGENT_IFC_OUT"),
                templatePath: Environment.GetEnvironmentVariable("IFC_AGENT_TEMPLATE"));
            Pipeline.WriteStatus(status, "OK");
        }
        catch (Exception ex)
        {
            Pipeline.WriteStatus(status, "ERR: " + ex);
            exitCode = 1;
        }
        finally
        {
            // Force Revit down — graceful shutdown via the UI thread isn't
            // reliable from inside an event handler, and the Python
            // launcher just needs the process to exit and the status file
            // to be flushed.
            Environment.Exit(exitCode);
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
