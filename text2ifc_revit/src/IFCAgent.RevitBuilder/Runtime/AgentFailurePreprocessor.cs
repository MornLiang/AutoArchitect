using System.Collections.Generic;
using Autodesk.Revit.DB;

namespace IFCAgent.RevitBuilder.Runtime;

public sealed class AgentFailurePreprocessor : IFailuresPreprocessor
{
    public FailureProcessingResult PreprocessFailures(FailuresAccessor accessor)
    {
        IList<FailureMessageAccessor> failures = accessor.GetFailureMessages();
        foreach (var failure in failures)
        {
            string desc = failure.GetDescriptionText() ?? string.Empty;
            FailureSeverity severity = failure.GetSeverity();

            if (severity == FailureSeverity.Warning)
            {
                accessor.DeleteWarning(failure);
                continue;
            }

            if (IsJoinFailure(desc) || severity == FailureSeverity.Error)
                return FailureProcessingResult.ProceedWithRollBack;
        }

        return FailureProcessingResult.Continue;
    }

    private static bool IsJoinFailure(string description)
    {
        return description.Contains("Can't keep elements joined")
            || description.Contains("joined")
            || description.Contains("Join");
    }
}
