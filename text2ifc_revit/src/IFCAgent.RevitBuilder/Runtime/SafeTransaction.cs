using System;
using System.Text.Json;
using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Runtime;

public sealed record ActionResult(
    string ActionId,
    string Status,
    string FailureType,
    string Message,
    string SuggestedNextAction)
{
    public string ToJson() => JsonSerializer.Serialize(this);
}

public static class SafeTransaction
{
    public static ActionResult Run(Document doc, string actionId, Action action)
    {
        using var tx = new Transaction(doc, actionId);
        bool started = false;
        try
        {
            tx.Start();
            started = true;

            FailureHandlingOptions opts = tx.GetFailureHandlingOptions();
            opts.SetFailuresPreprocessor(new AgentFailurePreprocessor());
            opts.SetForcedModalHandling(false);
            opts.SetClearAfterRollback(true);
            tx.SetFailureHandlingOptions(opts);

            action();

            TransactionStatus status = tx.Commit();
            if (status == TransactionStatus.Committed)
            {
                return new ActionResult(actionId, "committed", "", "", "");
            }

            return new ActionResult(
                actionId,
                "rollback",
                "TransactionFailure",
                $"Transaction finished with status {status}",
                "retry_without_join");
        }
        catch (Exception ex)
        {
            if (started)
            {
                try { tx.RollBack(); } catch { /* best effort */ }
            }
            string failureType = IsJoinFailure(ex.Message)
                ? "JoinGeometryFailure"
                : ex.GetType().Name;
            string next = failureType == "JoinGeometryFailure"
                ? "retry_without_join"
                : "skip_or_repair_action";
            return new ActionResult(actionId, "rollback", failureType, ex.Message, next);
        }
    }

    private static bool IsJoinFailure(string message)
    {
        return message.Contains("Can't keep elements joined")
            || message.Contains("joined")
            || message.Contains("Join");
    }
}
