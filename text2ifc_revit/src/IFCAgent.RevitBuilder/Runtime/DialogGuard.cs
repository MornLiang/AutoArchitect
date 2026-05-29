using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;

namespace IFCAgent.RevitBuilder.Runtime;

public static class DialogGuard
{
    public static bool IsRunning { get; set; }

    public static void Register(UIControlledApplication app)
    {
        app.DialogBoxShowing += OnDialogBoxShowing;
    }

    private static void OnDialogBoxShowing(object? sender, DialogBoxShowingEventArgs e)
    {
        if (!IsRunning) return;
        e.OverrideResult((int)TaskDialogResult.Cancel);
    }
}
