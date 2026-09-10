# 架构与评测设计

## 目标

将执行 Agent 的任务表现、评测过程是否可信、监督与人工纠错的效果分开衡量。平台负责调度、版本和证据；Benchmark 定义任务与评分规则；执行 Agent 负责完成任务；监督 Agent 分析证据并提出建议。

## 组件

| 组件 | 实现 | 职责 |
| --- | --- | --- |
| Portal / API / CLI | `static/`、`api.py`、`cli.py` | 创建任务、查进度、读轨迹、审核建议 |
| EvaluationService | `service.py` | Snapshot、Job、Trial、Attempt 状态与调度 |
| 存储 | `database.py` | SQLite 元数据、项目范围、审计 |
| Adapter | `adapters.py` | DSH、Harbor、HTTP Runtime、示例及模型接口 |
| 轨迹 | `dsh_trajectory.py` | JSONL 会话归一化、工具事件与用量 |
| 评分 | `grading_pipeline.py`、`graders.py` | 规则、Schema、可执行评分、LLM、官方结果 |
| Terminal-Bench | `terminal_bench.py`、`harbor_dsh.py`、`run_harbor_trial.py` | 模型执行配置、轨迹归一化、官方评分采信、进程边界 |
| 独立监督 | `supervision.py`、监督 Adapter | 结构化建议与人工批准后的纠错 |

## 数据模型

- **Benchmark**：版本化任务包，含 instruction、分类、预算、环境要求和 graders。
- **AgentSnapshot**：不可变执行配置；模型与预算、适配代码及运行包摘要用于追踪执行条件。
- **EvalJob**：一次配置好的批次，展开为 `执行快照 × Task × repetitions`。
- **Trial / Attempt**：每次独立尝试，保留输入、输出、事件、用量、评分和父尝试关系。
- **Supervision / Review**：监督建议与人工决定。建议不直接改写首次输出。
- **Report**：分别聚合首次表现、无效样本、失败原因及纠错效果。

默认使用 SQLite 和本地文件，因此从代码仓库拉取不会携带运行记录。正式服务化需要另行完善共享存储、调度和身份认证。

## Terminal-Bench 执行与可信评分

Harbor 在每道题的隔离 Docker Linux 容器中运行所选 Agent。Codex 配置通过 Responses API 调用 OpenAI 兼容服务；DSH 配置保留为可选模型。官方测试在执行阶段结束后运行，执行 Agent 自称成功不能替代测试结果。

超时、取消或异常退出时，进程守卫根据本任务启动前的进程基线终止新进程，并确认无残留后交回控制。无法确认停止时阻止评分。采集时同时核对终止时间、评分时间和工具调用时间。

原始 reward 与可采信 reward 分开保存。完整 CTRF 测试报告的计数、逐项状态和 reward 需一致；缺报告、边界不明确或测试未正常开始时，不能仅凭 reward 判断能力。当前尚有安装阶段与 CLI 启动错误的分类缺口，见已知问题。

## 监督与人工纠错

监督读取任务、执行证据、工具结果与评分理由，不接收标准答案。监督输出失败类别、证据引用、置信度与修改建议。人工决定是否批准及最终反馈，平台随后创建新 Attempt；原尝试和原始评分保留。

监督能够看到官方评分，因此监督统计不能解释为独立盲评准确率。比较纠错效果时，应控制任务、模型、工具和预算，并区分自然重试与监督辅助重试。

## 结果解释

`pass`、`unresolved`、`agent_failed`、`infra_failed`、`grader_failed` 和 `hard_fail` 分开记录。执行问题和评分问题可以同时保留。总题数、有效评分数与通过数分别展示；有效样本通过率的分母不包含无效评分。

开发子集、完整集、单次尝试和纠错后尝试不得混为同一指标。仓库不附带实际评测成绩。
