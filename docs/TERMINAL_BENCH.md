# Terminal-Bench 接入与准备

## 固定组件

| 项目 | 配置 |
| --- | --- |
| 任务集 | Terminal-Bench 2.1，上游 89 题 |
| 上游 | https://github.com/harbor-framework/terminal-bench-2-1 |
| 固定提交 | `7131e4375048a0e408a8fb404b5f499d726b695b` |
| Harbor | `0.22.0`，要求 Python 3.12+ |
| 默认 Agent | Harbor 内置 Codex `0.154.0` |
| 默认模型 | `gpt-5.6-sol`，推理强度 `xhigh` |
| 可切换模型 | `gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、DSH `deepseek-v4-flash` |
| 可选 DSH 运行时 | DSH `0.1.1-rc.2`，Node `22.19.0` Linux x64 |
| 后台快照版本 | `2.0.0-<配置摘要>`（模型、执行集成或清理策略变化时自动新建） |

仓库保存接入代码和准备配方。上游完整任务、镜像、可选 Node/DSH 运行包以及生成的本机注册信息均按需准备，不随代码提交。后台 API 和 Portal 可以自动完成以下准备步骤；手工命令保留用于排障。具体环境问题见 [已知问题](KNOWN_ISSUES.md)。

## 后台直接接入

安装 `.[terminal-bench]`、启动 Docker Linux 引擎与 Docker Compose v2，并准备 Codex `auth.json` 或 `OPENAI_API_KEY` 后，在 Portal 点击“Agent / Harbor 评测”；也可以执行：

```powershell
python -m agent_eval terminal-bench-run --model-profile codex-gpt-5.6-sol --reasoning-effort xhigh --task openssl-selfsigned-cert --data-dir .data
```

后台会幂等执行任务集固定、Benchmark/模型快照注册、Job 创建和启动。只有选择 DSH 模型时才准备 DSH 运行包。默认只运行一题；全量 89 题要求 `--all-tasks --confirm-full-run`。首次运行某题还需拉取对应任务镜像，后续运行复用 Docker 镜像缓存。任务结束只删除该 Trial 的容器、网络和卷，不删除预构建任务镜像。集成给环境准备保留三倍上游时限，避免慢速首次下载被误判成 Agent 失败。

## 1. Python 和任务集

以下示例使用 Windows PowerShell，在仓库根目录执行；Docker Desktop 应切换为 Linux 容器。

```powershell
python -m venv .terminal-bench-venv
& .terminal-bench-venv/Scripts/python.exe -m pip install 'harbor==0.22.0'
New-Item -ItemType Directory -Force .terminal-bench | Out-Null
git -c core.autocrlf=false clone https://github.com/harbor-framework/terminal-bench-2-1.git .terminal-bench/terminal-bench-2-1
git -C .terminal-bench/terminal-bench-2-1 checkout 7131e4375048a0e408a8fb404b5f499d726b695b
docker info --format '{{.OSType}}'
```

预期 Docker 输出 `linux`。Linux 宿主使用 `.terminal-bench-venv/bin/python` 替换示例中的 Windows 路径。当前集成主要在 Windows + Docker Desktop 上开发，其他宿主仍需环境验证。

## 2. 配置模型凭据

默认 Codex 配置优先读取 `OPENAI_API_KEY`；也可以显式设置 `CODEX_AUTH_JSON_PATH`，让 Harbor 0.22.0 将该登录文件只读上传到当次临时任务容器。API Key 模式默认使用 OpenAI 官方 Responses API，`EVAL_OPENAI_BASE_URL` 可显式配置兼容地址；登录文件模式不覆盖 Codex 自身的官方认证端点。两种凭据都不写入 Snapshot、请求记录或结果文件，Harbor 在执行结束时删除任务容器内的临时认证目录。

在 Docker Desktop 配置了无认证信息的引擎代理时，运行器会将该代理自动传入临时任务容器，供 Harbor 安装官方 Agent 依赖及执行任务使用；带用户名或密码的代理不会写入评测配置。

若选择 `dsh-deepseek-v4-flash`，需配置 `DEEPSEEK_API_KEY`，并准备下面的固定 DSH 运行包。

## 3. 准备可选 DSH 运行包

`scripts/prepare_dsh_runtime.sh` 在不含 API Key 的临时 Linux 容器里安装固定 DSH 包及必需依赖，执行无模型调用的帮助命令，并导出运行包。

```powershell
New-Item -ItemType Directory -Force .terminal-bench/runtime-cache | Out-Null
Invoke-WebRequest 'https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz' -OutFile '.terminal-bench/runtime-cache/node.tar.xz'
$evalRepo = (Get-Location).Path
docker run --rm --mount "type=bind,source=$evalRepo,target=/workspace" python:3.12-slim bash -lc 'cp /workspace/.terminal-bench/runtime-cache/node.tar.xz /tmp/node.tar.xz && bash /workspace/scripts/prepare_dsh_runtime.sh && cp /tmp/dsh-runtime.tgz /workspace/.terminal-bench/runtime-cache/dsh-runtime.tgz'
```

本步骤需要访问官方 Node 下载站和 npm。它生成 `.terminal-bench/runtime-cache/dsh-runtime.tgz`；生成后导入器保存该包的 SHA-256。重建导致摘要变化时应使用新执行快照，不能替换已有快照对应的文件。准备时不要向容器传入模型密钥。

## 4. 启动 Portal 并导入完整集

先在一个配置了模型环境变量的终端启动 Portal，明确数据目录：

```powershell
python -m agent_eval serve --host 127.0.0.1 --port 8765 --data-dir .data
```

另一个终端执行：

```powershell
& .terminal-bench-venv/Scripts/python.exe scripts/import_terminal_bench_full.py --api-base http://127.0.0.1:8765 --data-dir .data
```

导入器按固定提交读取并校验任务文件，生成本地包，注册 Benchmark 和执行快照。手工导入器默认创建 DSH 快照；日常使用 Portal 或 `terminal-bench-setup` 创建选定模型的快照。**导入不会启动模型评测**。生成包和注册信息受 `.gitignore` 排除。

`--data-dir` 要与 Portal 使用的目录一致。`--prepare-only` 只准备本地包及快照配置，不向 Portal 注册。相同内容重复导入复用已有记录；同名同版本且配置不同则要求新版本。

## 5. 运行

通过 Portal 的“Agent / Harbor 评测”选择模型、推理强度、任务和并发数后启动。每种模型和推理强度保存为独立不可变快照；历史 Job 继续指向原快照。模型密钥需要对启动执行任务的服务进程可见。

快速开发集的脚本入口：

```powershell
# 只注册五题开发集
& .terminal-bench-venv/Scripts/python.exe scripts/run_terminal_bench.py --register-only --data-dir .data
# 执行单题；此命令会调用模型
& .terminal-bench-venv/Scripts/python.exe scripts/run_terminal_bench.py --task openssl-selfsigned-cert --data-dir .data
```

开发集固定包含证书、日志汇总、Git 修复、数据库 WAL 恢复和异步取消五题，不能用其成绩代替完整集。

每题沿用官方任务中的执行和评分阶段预算；外层等待还覆盖镜像准备、Agent 安装和证据采集。执行前不预装或运行测试依赖，官方 `tests/test.sh` 在 Agent 结束后由 Harbor 原样执行。测试依赖下载失败、环境启动失败、Agent 安装失败和评分超时均记为基础设施异常，不记为模型答错。

正常、失败和取消路径都会在 Harbor 清理后按 Compose project 标签再次确认运行资源已经消失。服务重启时还会根据当前数据目录中的 Trial 请求回收上次进程遗留的容器、网络和卷；该回收范围不包含镜像。

## 6. 终止边界验证

运行包和 Docker 已准备好时可执行：

```powershell
& .terminal-bench-venv/Scripts/python.exe scripts/verify_harbor_termination.py
```

该检查使用合成写入程序，在超时与异常退出场景中验证普通及脱离会话的后台子进程停止，不调用模型。检查输出仍属于本地运行结果，不提交到 Git。

## 7. 结果与监督

每题原始结果、官方测试输出、CTRF、终止证明和 Agent 会话写入指定数据目录。Codex 的原生 rollout JSONL 与用量会归一化为平台轨迹；DSH 会话继续使用原有解析器。官方二元 reward 通过才算该题成功；异常评分单独展示。监督在首次结果保存后按需启动，使用独立 LLM Supervisor 配置，人工批准之后才创建纠错尝试。

本开发分支没有附带任何实验成绩、既有 Job ID、人工审核记录或监督输出。
