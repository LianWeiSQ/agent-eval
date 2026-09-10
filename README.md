# Agent Eval

本地 Agent 评测与人工审核纠错平台。当前主要接入 MMLU 知识题与 Terminal-Bench 终端任务，支持执行 Agent、官方或规则评分、独立监督 Agent、人工批准后的纠错重试。

此开发分支只保留 MMLU 的 20 题定义与 Terminal-Bench 的导入、运行代码，以及平台源码、配置示例、测试和说明文档。其他 Benchmark、数据库、模型输出、运行轨迹、真实评测报告、截图、密钥、第三方运行包和虚拟环境不在仓库中。

## 启动

建议使用 Python 3.12+。在克隆后的仓库根目录执行：

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[terminal-bench]"
python -m agent_eval serve --host 127.0.0.1 --port 8765 --data-dir .data
```

打开 <http://127.0.0.1:8765/>。基础 Portal、API、SQLite 使用 Python 标准库；首次仅初始化 MMLU 和 DSH / Supervisor 配置，不会自动调用付费模型。点击“Agent / Harbor 评测”可以选择模型和推理强度，由后台自动准备并注册 Terminal-Bench、Harbor 和执行快照，再直接启动指定任务。默认评测模型为 `gpt-5.6-sol`，推理强度为 `xhigh`。Windows 也可在已激活的终端执行 `./scripts/evaluation-service.ps1`。

详细操作见 [使用说明](docs/USAGE.md)。真实 Terminal-Bench 需要 Docker Linux 容器、Docker Compose v2、`harbor==0.22.0` 和所选模型的凭据；Codex 配置支持只读 `auth.json` 或 `OPENAI_API_KEY`，DSH 配置使用 `DEEPSEEK_API_KEY`。任务集与可选的固定 DSH 运行包可由后台自动准备，细节见 [Terminal-Bench 接入](docs/TERMINAL_BENCH.md)。

## 工作流程

```text
Benchmark + AgentSnapshot
    -> EvalJob -> Trial / Attempt 1
    -> 执行 Agent、工具调用、轨迹采集
    -> 官方 verifier / 规则评分
    -> 独立 Supervisor 给出结构化建议
    -> 人工接受、修改或拒绝建议
    -> 批准后创建 Attempt 2
    -> 对比首次成绩与纠错后成绩
```

页面保留“评测任务、Benchmark、Agent 配置、纠错审核”四个主入口。首次成绩与监督辅助后的成绩分开记录；监督 Agent 不能自行修改评分规则或批准重试。

## 目录

| 目录 | 用途 |
| --- | --- |
| `agent_eval/` | 调度与状态、Adapter、Grader、监督、数据库、API、CLI |
| `agent_eval/static/` | Portal、轨迹展示、大 JSON 读取 Worker |
| `benchmarks/` | 仅保留 MMLU 高中计算机科学 20 题定义 |
| `scripts/` | 启动、数据集导入、Harbor 运行、运行包准备与终止验证 |
| `examples/` | 无真实密钥的 Snapshot、Job、Grader 配置示例 |
| `tests/` | 平台、评分、监督、轨迹及前端回归测试 |
| `docs/` | 使用、架构、接口、Terminal-Bench 和已知限制 |

Terminal-Bench 的完整任务、生成的导入包和运行器缓存在本地按需准备。通用调度、评分和审核测试使用测试函数内构造的最小输入，不依赖其他 Benchmark 目录。保留 Codex、DSH、Terminal-Bench / Harbor、独立 Supervisor、通用 Runtime HTTP 与测试用 Echo。论文检索执行器、论文专用评分、Core Fixture 演示执行器和旧论文评测命令已移除。

## 测试

基础安装不包含 Harbor。执行全量开发测试时使用 Python 3.12+ 并安装可选依赖：

```bash
python -m pip install -e '.[terminal-bench]'
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_json_projection.js
node tests/test_supervisor_selection.js
```

上述回归不调用真实模型。真实 Docker 终止验证另见 Terminal-Bench 文档；真实模型评测需要由使用者明确启动。

## 文档与边界

- [架构与评测设计](docs/ARCHITECTURE.md)
- [使用与配置](docs/USAGE.md)
- [Terminal-Bench 准备与运行](docs/TERMINAL_BENCH.md)
- [REST API](docs/API.md)
- [Benchmark 分层](docs/BENCHMARK_LAYERS.md)
- [已知问题与开发范围](docs/KNOWN_ISSUES.md)

当前是本地开发平台，角色头用于本地项目隔离，不是可直接暴露公网的完整认证方案。代码仍有已知的环境准备和分类边界，详见已知问题。本文不提供任何真实批次的成绩或通过率。
