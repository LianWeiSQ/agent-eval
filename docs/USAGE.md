# 使用与配置

所有命令从仓库根目录运行。建议 Python 3.12+；Terminal-Bench 的 Harbor 依赖要求 Python 3.12+。

## 基础启动

按 README 创建并激活 `.venv`，然后执行：

```bash
python -m pip install -e .
python -m agent_eval serve --host 127.0.0.1 --port 8765 --data-dir .data
```

首次启动只初始化 MMLU 20 题，以及 DSH 执行和 LLM Supervisor 两个配置。Terminal-Bench 通过专门脚本按需导入。运行数据随后写入指定目录，不同实验可指定不同 `--data-dir`。已有数据库的历史记录不自动删除。

代码包名为 `agent_eval`，安装后的命令为 `agent-eval`。更新开发副本后重新执行 `python -m pip install -e .`；旧论文评测入口及执行器已移除。已有 Terminal-Bench 快照冻结了代码路径、模型配置与哈希，升级后应通过 Portal 或 `terminal-bench-setup` 为所选模型创建新的 `2.0.0-<配置摘要>` 快照。旧评测记录保留，不能直接复用旧快照运行已改名的代码。

Windows PowerShell 可执行：

```powershell
.\scripts\evaluation-service.ps1 -DataDirectory .data
```

未指定目录时，脚本优先复用已有 `.data-supervised-loop`，否则使用 `.data`。从新仓库启动不会带入开发者的数据库。

## 模型凭据

| 环境变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | Codex Terminal-Bench API Key 认证（与登录文件二选一） |
| `CODEX_AUTH_JSON_PATH` | Codex 登录文件路径；Harbor 只在当次临时容器中使用 |
| `EVAL_OPENAI_BASE_URL` | Codex 评测的 OpenAI 兼容 Responses API 地址；优先级高于 `OPENAI_BASE_URL` |
| `DEEPSEEK_API_KEY` | 可选 Terminal-Bench DSH 执行模型 |
| `EVAL_SUPERVISOR_API_KEY` | LLM Supervisor |
| `DEEPSEEK_BASE_URL` | 可选的 DeepSeek 接口覆盖 |
| `DEEPSEEK_SEARCH_BASE_URL` | 可选的 DeepSeek 搜索接口覆盖 |
| `DSH_COMMAND` | MMLU / 本机 DSH 的可执行文件，默认 `dsh` |
| `DSH_WORKING_DIRECTORY` | 本机 DSH 的工作目录，默认仓库根目录 |
| `AGENT_EVAL_DATA_DIR` | CLI 默认数据目录 |

凭据不填进 Snapshot JSON、任务请求或代码。Codex 运行器支持把 `OPENAI_API_KEY` 注入当次 Harbor Agent 进程，或由 Harbor 将 `CODEX_AUTH_JSON_PATH` 指向的登录文件临时上传到任务容器；运行结束会清除容器内认证目录。DSH 运行器兼容已有 DSH 凭据库中的 `DEEPSEEK_API_KEY`，不会复制整个用户目录。

Windows 需要监督时可使用隐藏输入：

```powershell
.\scripts\evaluation-service.ps1 -WithSupervisor -DataDirectory .data
```

服务已经运行且没有进行中的评测时，可在持有密钥的同一终端添加 `-Restart`。重启会加载当前代码；不要在运行中修改某个不可变执行快照所绑定的代码。

默认 Supervisor 为 `LLM Supervisor Agent@1.3.2`，初始配置使用 32768 输出 token 预算。实际是否可调用，仍取决于接口地址、权限、网络和模型响应。切换配置应创建新快照。

## 新建评测与审核

1. 在“Agent 配置”检查执行 Agent 的环境与配置。
2. 在“评测任务”新建任务，选择兼容的 Benchmark、执行快照、监督快照以及并发和重复次数。
3. 启动后查看完成数量、每题结果和轨迹。完成数量不等于通过数量。
4. 首次结果保存后，按需对样本运行独立监督。
5. 在“纠错审核”检查证据和建议，接受、修改或拒绝；人工批准后才创建下一次 Attempt。
6. 在任务详情查看报告与对比。

Terminal-Bench 以官方 verifier 为评分依据。MMLU 使用固定题目与答案规则。Fixture Agent 只验证平台流程，不能用作真实模型能力结论。

## 本机 DSH 与 MMLU

先在本机安装并验证 DSH，再通过 `DSH_COMMAND` 指定其可执行文件。Windows npm 安装通常使用 `dsh.cmd`。如果必须从 DSH 源码仓库运行，请参照 `examples/snapshot-dsh.json` 创建自己的命令数组和工作目录，不要沿用其他电脑的绝对路径。

MMLU 默认包含高中计算机科学公开小集的 20 题定义；这不是完整 MMLU。重新生成公开定义的入口为 `scripts/import_mmlu_smoke.py`。Terminal-Bench 的 Codex 或 DSH Agent 运行在任务容器中，详见专门文档。

## 直接运行 Harbor / 可切换模型

安装可选依赖并启动 Docker Linux 引擎后，可以在 Portal 点击“Agent / Harbor 评测”。选择模型和推理强度后，后台会自动下载固定 Terminal-Bench 源码、注册 Benchmark 和独立执行快照，再启动填写的任务。默认使用 Codex `gpt-5.6-sol` 和 `xhigh`；也可切换到 Terra、Luna、GPT-5.5 或 DeepSeek DSH。只有 DSH 配置需要构建固定 DSH 运行包。首次准备可能需要几分钟，但不会调用模型；点击“准备并启动”后才会产生真实模型请求。

命令行也提供同一条后台链路：

```bash
python -m pip install -e '.[terminal-bench]'
python -m agent_eval terminal-bench-status --data-dir .data
python -m agent_eval terminal-bench-setup --model-profile codex-gpt-5.6-sol --reasoning-effort xhigh --data-dir .data
python -m agent_eval terminal-bench-run --model-profile codex-gpt-5.6-sol --reasoning-effort xhigh --task openssl-selfsigned-cert --data-dir .data
```

全量 89 题必须显式添加 `--all-tasks --confirm-full-run`。模型凭据根据配置从 `OPENAI_API_KEY`、`CODEX_AUTH_JSON_PATH`、`DEEPSEEK_API_KEY` 或当前用户的 DSH 凭据库读取，响应中不会返回密钥。首次运行某题会拉取其 Docker 镜像；命令行会等待 Job 完成，Portal/API 会在后台运行并可持续查看进度。

## 命令行

```bash
python -m agent_eval init --data-dir .data
python -m agent_eval benchmark-list --data-dir .data
python -m agent_eval snapshot-list --data-dir .data
python -m agent_eval --help
```

任务 ID、Benchmark ID 和快照 ID 必须从当前数据库获取，不使用其他机器的历史 ID。配置示例位于 `examples/`；其中的占位 ID 需要替换。

## 本地文件

运行目录可能包含模型输出和工具证据，不应加入 Git。仓库已忽略 `.data*`、`runs`、`results`、`reports`、日志、数据库、凭据与缓存，`benchmarks/` 也仅允许提交保留的 MMLU 目录。不要为上传结果或其他 Benchmark 使用 `git add -f`。
