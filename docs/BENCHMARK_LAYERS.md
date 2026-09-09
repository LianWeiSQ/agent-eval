# Benchmark 分层与兼容性

评测平台保持通用，具体测试集按目的分层。平台不把所有结果压成一个跨领域总分。

## Benchmark 类型

| `benchmark_type` | 目的 | 失败结论 |
|---|---|---|
| `platform` | 验证调度、评分、存储和报告链路 | `platform_check_failed` |
| `conformance` | 验证 Runtime、工具、事件和可观测协议 | `conformance_failed` |
| `general` | 验证可复用的基础能力 | `general_below_gate` |
| `domain` | 验证代码等具体业务能力 | `domain_below_gate` |
| `regression` | 验证已知生产行为没有回归 | `regression_blocked` |

`domain` 类型必须同时声明 `domain`。门禁通过仍统一返回 `passed`；报告同时保留底层 `gate_conclusion`，便于区分统计门禁和带语义的结论。

```json
{
  "benchmark_id": "custom-domain-example",
  "version": "1.0.0",
  "benchmark_type": "domain",
  "domain": "software-engineering"
}
```

## Agent 能力声明

AgentSnapshot 的 `config.capabilities` 使用四类字符串数组：

```json
{
  "capabilities": {
    "features": ["final_output", "structured_output", "events", "usage"],
    "tools": ["bash", "read_file"],
    "events": ["tool_call", "tool_result", "assistant_final"],
    "domains": ["software-engineering"]
  }
}
```

- `features`：运行契约能力，例如 `events`、`usage`、`artifacts`、`trace`；
- `tools`：评测环境向 Agent 提供的工具名称；
- `events`：Adapter 能采集并归一化的事件类型；
- `domains`：Agent 面向的业务领域。

能力声明描述可用接口，不代表 Agent 一定会正确使用。是否真的调用工具、产生事件或满足预算，仍由 Trial Grader 判断。

内置 Adapter 提供保守的默认声明，Snapshot 可以通过 `config.capabilities` 增补。`*` 仅在 Adapter 明确支持对应类别全部接口时声明；具体工具应按实际环境列出。

## Task 要求

Task 可以显式声明 `agent_requirements`：

```json
{
  "agent_requirements": {
    "features": ["events"],
    "tools": ["bash"],
    "events": ["tool_call", "tool_result"],
    "domains": ["software-engineering"]
  }
}
```

平台还会从 Grader 自动推导要求，包括必需工具、工具顺序、必需事件、Schema 输出、Usage 预算、Artifact 和 Trace。

## 运行前检查

`POST /api/v1/jobs/estimate` 返回 `compatibility` 分析。创建 Job 默认使用 `compatibility_mode=strict`，缺少接口时返回 `409 incompatible_agent`，不会把协议不匹配误报成模型能力不足。

只有专门进行合规负测时才使用：

```json
{
  "compatibility_mode": "allow"
}
```

`allow` 模式的报告会保留缺失项，并明确说明结果不能直接解释为模型语义或领域能力。

## 本分支保留的 Benchmark

| Benchmark | 类型 | 解释边界 |
|---|---|---|
| `mmlu-high-school-computer-science-smoke@1.0.1` | `general` | 20 道公开无工具知识题，不是完整 MMLU |
| Terminal-Bench 2.1 | `general` | 固定提交的真实终端任务；仅提交导入与运行代码，任务按需下载 |

两者分别报告，不生成跨 Benchmark 的混合总分。其他类型的兼容和评分接口仍可接收使用者自行导入的任务包，但其示例数据集不随本分支分发。
