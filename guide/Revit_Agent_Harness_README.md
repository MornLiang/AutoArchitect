# Revit Agent Harness：防止 Failure Dialog 阻塞的推荐方案

## 目标

在 Agent 调用 Revit API 进行 IFC/RVT 生成、SFT/RL 数据构建、模型验证与导出时，避免 Revit 弹出不可忽略的 modal failure dialog 导致流程卡死。典型错误包括：

```text
Can't keep elements joined.
Resolve First Error: Unjoin Elements
```

该问题通常不是硬件或 GPU 问题，而是 Revit 在几何 join、元素移动、楼板/墙/柱/梁修改、开洞或自动连接时触发了事务失败。推荐将其视为 **可记录、可回滚、可重试的 execution feedback**，而不是让 Agent 直接面对 Revit UI 弹窗。

---

## 核心推荐

采用组合策略：

```text
生成阶段：默认禁止 Auto-Join
执行阶段：每个 Agent action 独立 SafeTransaction
失败阶段：FailurePreprocessor 自动处理 warning/error
修复阶段：Join 类错误直接 rollback，并返回结构化反馈
后处理阶段：可选 Geometry Cleanup / Join Pass
兜底阶段：DialogBoxShowing 自动关闭异常弹窗
```

一句话原则：

> 生成时不要追求 Revit UI 中的自动 Join 完美性；先保证模型可生成、可导出、可验证，再在后处理阶段尝试几何清理。

---

## 推荐执行架构

```text
Agent
  ↓
Action JSON
  ↓
RevitActionExecutor
  ↓
Pre-check
  - 参数合法性检查
  - 修改类操作检测 joined elements
  - 必要时 pre_unjoin
  ↓
SafeTransaction(action)
  - 每个 action 一个独立 Transaction
  - 设置 IFailuresPreprocessor
  - 禁止/减少 modal blocking dialog
  ↓
Commit / Rollback
  ↓
Result JSON
  ↓
Agent retry / skip / repair / learn
```

每一步 Revit 操作都应返回结构化结果，例如：

```json
{
  "action_id": "step_023",
  "status": "rollback",
  "failure_type": "JoinGeometryFailure",
  "message": "Can't keep elements joined",
  "suggested_next_action": "retry_without_join"
}
```

---

## 默认策略表

| 场景 | 推荐策略 |
|---|---|
| 创建墙、楼板、柱、梁 | `auto_join = false` |
| 插入门窗、洞口 | 不主动 join，先保证宿主关系有效 |
| 修改已有元素位置/尺寸/高度 | `pre_unjoin = true` |
| `Can't keep elements joined` | rollback 当前 action，不弹窗等待人工处理 |
| 普通 warning | 尝试 `DeleteWarning` 后继续 |
| 其他 error | rollback 当前 action，并记录 failure |
| 模型主体生成完成后 | 单独运行 Geometry Cleanup / Join Pass |
| 仍出现 UI 弹窗 | `DialogBoxShowing` 兜底 cancel/close |

---

## Agent Action Schema 建议

创建类 action 默认禁止 join：

```json
{
  "action": "create_wall",
  "params": {
    "start": [0, 0, 0],
    "end": [5000, 0, 0],
    "level": "Level 1",
    "height": 3000,
    "auto_join": false,
    "rollback_on_failure": true,
    "allow_repair": true
  }
}
```

修改类 action 默认先解除 join：

```json
{
  "action": "move_element",
  "params": {
    "element_id": 12345,
    "offset": [1000, 0, 0],
    "pre_unjoin": true,
    "rejoin_after_modify": false,
    "rollback_on_failure": true
  }
}
```

---

## C# 实现要点

### 1. FailurePreprocessor

```csharp
public class AgentFailurePreprocessor : IFailuresPreprocessor
{
    public FailureProcessingResult PreprocessFailures(FailuresAccessor accessor)
    {
        IList<FailureMessageAccessor> failures = accessor.GetFailureMessages();

        foreach (FailureMessageAccessor f in failures)
        {
            string desc = f.GetDescriptionText();
            FailureSeverity severity = f.GetSeverity();

            if (severity == FailureSeverity.Warning)
            {
                accessor.DeleteWarning(f);
                continue;
            }

            if (desc.Contains("Can't keep elements joined") ||
                desc.Contains("joined") ||
                desc.Contains("Join"))
            {
                return FailureProcessingResult.ProceedWithRollBack;
            }

            if (severity == FailureSeverity.Error)
            {
                return FailureProcessingResult.ProceedWithRollBack;
            }
        }

        return FailureProcessingResult.Continue;
    }
}
```

### 2. SafeTransaction 包装每个 action

```csharp
using (Transaction tx = new Transaction(doc, actionName))
{
    tx.Start();

    FailureHandlingOptions opts = tx.GetFailureHandlingOptions();
    opts.SetFailuresPreprocessor(new AgentFailurePreprocessor());
    opts.SetForcedModalHandling(false);
    tx.SetFailureHandlingOptions(opts);

    try
    {
        action();

        TransactionStatus status = tx.Commit();
        if (status != TransactionStatus.Committed)
        {
            // return rollback / failed result to Agent
        }
    }
    catch (Exception ex)
    {
        tx.RollBack();
        // return exception message to Agent
    }
}
```

### 3. DialogBoxShowing 兜底

```csharp
uiApp.DialogBoxShowing += OnDialogBoxShowing;

private void OnDialogBoxShowing(object sender, DialogBoxShowingEventArgs e)
{
    LogDialog(e.DialogId);

    if (AgentRuntime.IsRunning)
    {
        e.OverrideResult((int)TaskDialogResult.Cancel);
    }
}
```

注意：`DialogBoxShowing` 只能作为兜底，不应作为主方案。主方案应是 `SafeTransaction + IFailuresPreprocessor`。

---

## 后处理 Geometry Cleanup Pass

主模型生成完成后，再单独尝试：

```text
墙-墙 Join
墙-楼板 Join
柱-楼板 Join
梁-柱 Join
墙端点对齐
重叠元素检测
空间闭合检查
IFC 导出验证
```

后处理阶段的原则：

```text
Join 成功：记录 cleaned
Join 失败：跳过该 pair，不影响主体模型
严重失败：rollback cleanup transaction，不影响原始生成模型
```

---

## 对 SFT/RL 数据构建的价值

该方案可以把 Revit 的失败从“阻塞 UI 的弹窗”变成 Agent 可学习的数据：

```text
Action → Revit execution → Success / Rollback / Repair feedback → Agent retry policy
```

这对 SFT/RL 尤其有用，因为模型不仅能学习“正确的 API 调用”，还能学习：

```text
哪些几何操作容易失败
什么时候应该 pre_unjoin
什么时候应该 retry_without_join
什么时候应该 rollback 当前 action
什么时候应该把 join 放到后处理
```

---

## 最终结论

推荐实现 **Revit Agent Harness v1**：

```text
No Auto-Join First
+ SafeTransaction per Action
+ IFailuresPreprocessor Rollback
+ Pre-Unjoin for Modify Actions
+ Optional Final Geometry Cleanup
+ DialogBoxShowing Fallback
```

对于 `Can't keep elements joined`，默认策略应是：

```text
rollback 当前 transaction
记录 failure
返回 suggested_next_action = retry_without_join 或 pre_unjoin_then_retry
继续 Agent 流程
```

这比人工点击 `Unjoin Elements` 更稳定，也更适合你们的 Revit-based SFT/RL 数据构建和生成模型验证流程。
