# Evaluation Service REST API

服务默认监听 `http://127.0.0.1:8765`，API 前缀为 `/api/v1`。所有 JSON 响应使用统一格式：

```json
{
  "data": {},
  "error": null,
  "request_id": "req-..."
}
```

## 请求范围与角色

本地 MVP 从请求头读取范围和角色：

```http
X-Tenant-ID: dev-tenant
X-Project-ID: dev-project
X-Actor-ID: local-user
X-Role: project_admin
```

缺省值就是上面的本地开发空间。支持角色为 `platform_admin`、`project_admin`、`project_operator` 和 `reviewer`。服务端查询强制追加 tenant/project 范围；客户端不能通过请求体切换资源所属项目。

部署在受信任网关之后时，应由网关校验 Token，并把可信身份映射到这些请求头，不能直接信任公网客户端提交的 `X-Role`。

## 路由

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/health` | 服务健康检查及当前监督规则版本 |
| GET | `/api/v1/dashboard` | 概览统计 |
| POST | `/api/v1/bootstrap` | 幂等初始化 MMLU 和 DSH / Supervisor 配置 |
| GET | `/api/v1/terminal-bench/status` | 检查 Harbor、Docker、可选模型、凭据和注册状态 |
| POST | `/api/v1/terminal-bench/setup` | 自动准备并注册固定 Terminal-Bench 与所选模型快照，不调用模型 |
| POST | `/api/v1/terminal-bench/evaluations` | 自动准备、创建并启动所选模型的评测任务 |
| GET/POST | `/api/v1/benchmarks` | 列表/创建 Benchmark |
| POST | `/api/v1/benchmarks/validate` | 校验本地 Benchmark 路径 |
| POST | `/api/v1/benchmarks/import` | 导入目录包、JSON、JSONL 或 YAML |
| GET | `/api/v1/benchmarks/{id}` | Benchmark 和 Task 详情 |
| POST | `/api/v1/benchmarks/{id}/publish` | 发布不可变版本 |
| POST | `/api/v1/benchmarks/{id}/archive` | 归档，不删除历史 |
| POST | `/api/v1/benchmarks/{id}/copy` | 复制为新名称/版本并记录父版本 |
| GET | `/api/v1/benchmarks/{id}/export/{json/jsonl}` | 导出平台包或 Task JSONL |
| GET/POST | `/api/v1/agent-snapshots` | 列表/创建 AgentSnapshot |
| GET | `/api/v1/agent-snapshots/{id}/health` | Adapter 健康检查 |
| POST | `/api/v1/agent-snapshots/{id}/try` | 使用快照执行一次可观测试跑 |
| GET/POST | `/api/v1/graders` | 列表/注册 GraderSpec |
| POST | `/api/v1/jobs/estimate` | 估算 Task、Trial 和最坏耗时 |
| GET/POST | `/api/v1/jobs` | 列表/创建 EvalJob |
| GET | `/api/v1/jobs/{id}` | Job、Trial 和最新报告 |
| POST | `/api/v1/jobs/{id}/start` | 异步启动 |
| POST | `/api/v1/jobs/{id}/pause` | 暂停提交新 Trial，不杀死已运行 Trial |
| POST | `/api/v1/jobs/{id}/resume` | 继续调度 |
| POST | `/api/v1/jobs/{id}/cancel` | 取消并保留已产生证据 |
| GET | `/api/v1/jobs/{id}/report` | 最新聚合报告 |
| GET | `/api/v1/jobs/{id}/export/{format}` | `json/jsonl/csv/markdown` 导出 |
| GET | `/api/v1/trials?job_id=...` | Trial 列表和筛选 |
| GET | `/api/v1/trials/{id}` | 输出、事件、Grade、Artifact 和 Review |
| GET | `/api/v1/trials/{id}/trace` | 轨迹投影，可用 `view` 参数选择视图 |
| POST | `/api/v1/trials/{id}/supervise` | 显式调用监督，可指定监督快照 |
| POST | `/api/v1/trials/{id}/retry` | 仅重试 `infra_failed/grader_failed`，创建新 attempt |
| POST | `/api/v1/trials/{id}/regrade` | 保留 AgentRun，只追加新版 GradeResult |
| GET | `/api/v1/reviews` | 人工评审队列 |
| POST | `/api/v1/reviews/{id}/submit` | 提交分数、结论、理由和证据 |
| GET | `/api/v1/supervisions` | 监督记录列表，可用 `status` 筛选 |
| POST | `/api/v1/supervisions/{id}/decide` | 提交人工纠错决定与获批反馈 |
| GET | `/api/v1/artifacts/{id}` | 按项目范围读取 Artifact |
| GET | `/api/v1/audit` | 管理员读取审计日志 |

## 服务加载版本

`GET /health` 的 `data` 包含：

```json
{
  "status": "ok",
  "service": "evaluation-service",
  "schema_version": "1.0.0",
  "supervision_contract_version": "task-completion-v2"
}
```

该版本来自当前运行进程，用于确认重启生效。此接口不调用模型；监督快照的健康检查也只验证本地配置和凭据是否齐备，不证明外部请求一定成功。

## 创建 Job 示例

```json
{
  "name": "mmlu-agent-comparison",
  "benchmark_id": "bench-...",
  "agent_snapshot_ids": ["asnap-baseline", "asnap-candidate"],
  "compatibility_mode": "strict",
  "task_filter": {},
  "execution": {
    "repetitions": 3,
    "max_concurrency": 20,
    "timeout_seconds": 180,
    "infra_retry_limit": 1
  },
  "baseline_agent_snapshot_id": "asnap-baseline",
  "report_policy": {
    "human_sample_rate": 0.1,
    "invalid_rate_limit": 0.2
  },
  "gate": {
    "min_pass_rate": 0.8,
    "max_hard_failures": 0
  },
  "idempotency_key": "release-agent-v42"
}
```

创建返回 `draft`；再调用 `POST /jobs/{id}/start`。Job 后台展开为 `AgentSnapshot × Task × repetition`，客户端轮询详情或概览即可。

## 直接运行可切换模型

下面的请求会自动准备缺失的 Terminal-Bench 源码，注册 `gpt-5.6-sol + xhigh` 的 Harbor Codex 执行快照，并只启动指定的一题：

```json
POST /api/v1/terminal-bench/evaluations
{
  "model_profile": "codex-gpt-5.6-sol",
  "reasoning_effort": "xhigh",
  "task_names": ["openssl-selfsigned-cert"],
  "repetitions": 1,
  "max_concurrency": 1
}
```

省略 `model_profile` 时使用 `codex-gpt-5.6-sol`，省略 `reasoning_effort` 时使用该配置的默认值 `xhigh`。省略 `task_names` 时也只运行默认单题。运行全部 89 题必须同时传入 `"all_tasks": true` 和 `"confirm_full_run": true`，防止意外产生大量真实模型调用。可选配置和对应凭据状态由 `GET /terminal-bench/status` 的 `model_profiles` 返回。

`POST /jobs/estimate` 会同时返回 Benchmark 要求、每个 Snapshot 的能力声明和缺失项。默认 `strict` 会拒绝不兼容组合；只有合规负测才显式设置 `compatibility_mode=allow`。Benchmark 类型、能力声明和带作用域的报告结论见 [BENCHMARK_LAYERS.md](BENCHMARK_LAYERS.md)。

## 错误语义

- `400`：Schema、路径、Task、Job 或状态参数错误；
- `403`：角色权限不足或路径越界；
- `404`：资源不存在，或资源不属于当前 tenant/project；
- `409`：不可变版本冲突、重复幂等键、状态迁移冲突或 `incompatible_agent`；
- `413`：请求超过 5 MB；
- `500`：未分类服务错误。
- `502`：Supervisor 调用失败或输出无效；`supervisor_provider_unavailable` 表示上游 HTTP、网络或 TLS 等连接错误，`supervisor_input_too_large` 表示规范化输入超过限制，`supervisor_output_truncated` 表示输出达到 token 上限，`supervisor_invalid_output` 表示空正文、无效 JSON 或缺少字段。

失败响应的 `error` 包含稳定 `code` 和中文 `message`。

## Supervisor 检查

创建 Job 可传 `supervisor_snapshot_id`。`POST /api/v1/trials/{id}/supervise` 可用同名字段指定检查快照；省略时使用 Job 的配置。

真实监督需要指定 `llm-supervisor` 快照；请求和 Job 都没有监督快照时会走确定性的 fixture 逻辑，不能计为真实模型监督。已有有效监督记录时返回原记录，不会因请求中更换快照而重新调用模型。实验中应核对返回记录的快照与版本。

批量实验应在 Job 全部执行完成后再送审。`fail` 或 `uncertain` 会将 Job 标记为 `review_pending`；当前不提供与同批执行并行的独立监督调度器。

无效监督响应会记录 `supervision.failed` 审计，保留失败类型、阶段和不含正文的响应诊断，不创建 `SupervisionRun` 或新的 Attempt。修复配置后可对同一 Trial 再次请求监督。有效监督记录仍保持每 Trial 一条；历史空记录不会被自动覆盖。

有效监督结果的 `raw_output.response_metadata` 保存 `finish_reason`、`completion_tokens`、可用的 `reasoning_tokens` 和 `has_reasoning_content`，帮助区分生成截断与证据不足。只有人工批准纠错才创建下一次 Attempt。

`result.validation` 会记录监督原始 `pass` 与 Trial 未通过的冲突及原始判断。服务内部的 `revalidate_supervision` 可以显式、带审计地重新校验尚无人工决定的旧记录；当前没有对应 REST 路由。

## 人工纠错决定

`POST /api/v1/supervisions/{id}/decide` 接收：

```json
{
  "decision": "approve_retry",
  "reason": "审核人确认此建议有证据支持并批准重试。",
  "approved_feedback": "请核对工具返回结果与任务要求，再执行必要的验证。"
}
```

`decision` 可为 `approve_retry`、`reject_feedback`、`accept_final`、`terminate`，`reason` 必填。批准重试保存最终反馈并创建带 `parent_attempt_id` 的子 Trial；其他决定只记录处理结果。当前人工纠错最多到 Attempt 2，重复决定返回状态冲突。示例仅说明结构，不能代替真实人工审核。
