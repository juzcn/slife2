# slife2

[English](README.md)

**slife2 是一个终端 AI agent。** 它与语言模型进行对话，通过模型上下文协议（MCP）向模型提供工具，并通过本地插件进程连接外部世界。终端窗口只是一个客户端：agent 主循环、工具 hub、模型后端、记忆以及每一项能力，都是 slife2 启动并共享的 MCP server。

本文件是用户手册。设计见 [SDD.md](SDD.md)。这里每一条都是读者可以照着做的事；不改变读者做法的解释属于设计文档。

**目录**

- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令行接口](#命令行接口)
- [使用终端界面](#使用终端界面)
- [配置](#配置)
- [能力](#能力)
- [数据位置](#数据位置)
- [开发](#开发)
- [已知限制](#已知限制)
- [许可证](#许可证)

### 术语

| 术语 | 含义 |
|---|---|
| **插件** | slife2 自己的一个 MCP server，由启动器启动并共享。外部 MCP server 不是插件。 |
| **agent** | 由 `--agent NAME` 命名的身份。一个名字在一台机器上只能有一个活着的持有者。 |
| **对话 id** | `(agent, subagent)`：一次调用所面向的对话身份。 |
| **对话** | 一个 agent 自己的历史，或它的某个 subagent 的历史。 |
| **轮次** | 一次交换：输入、模型的回答，以及它发起的工具调用。 |
| **计划** | 模型为一个对话准备的步骤列表，存放在该对话旁，并在每一轮重放。 |
| **channel** | 消息到达的位置：`tui`、`wechat`、`peer_agent:<peer>`、`agent:<name>`、`subagent:<name>`、`agent-task:<name>` 或 `async-tool:<name>`。 |
| **marker** | 发送者在消息前添加的机器可读前缀，例如 `[Wechat:{…}]`。随消息一起存储，只在显示时移除。 |
| **家族** | 拥有一个配置节或文件夹，并向 hub 声明其所含条目的插件。 |
| **catalogue** | 模型可以发现的所有工具、playbook、job 和命令。 |
| **job** | `jobs/` 里的一个 Python 函数，暴露为工具 `job__<name>`。 |
| **dump** | 因为对对话而言过大而完整保存在文件柜中的工具输出。 |
| **home** | 一个数据目录：配置、`AGENTS.md`、skills、jobs、agents。 |
| **运行时目录** | 共享 server 在整台机器范围内的记录、锁和日志。 |

## 环境要求

- **Python 3.13 或更高版本**
- **[uv](https://docs.astral.sh/uv/)**，用于依赖管理和运行

slife2 在 Windows、macOS 和 Linux 上测试。

### SQLite 扩展支持

向量索引是一个 SQLite 扩展（`sqlite-vec`），解释器必须能够加载扩展。Windows 和 Linux 的构建通常可以。macOS 上，uv 自带的 CPython 和 Apple 的 `/usr/bin/python3` 都不能；请使用 Homebrew 的 Python：

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

如果无法加载扩展，数据库就无法打开，任何插件都不会启动，第一条命令会以 `this Python's sqlite3 cannot load extensions` 失败。

### 外部程序（配置后即必需）

配置中的 server、隧道或 skill 点名的每个程序都是必需的。安装器在用户级别提供每一个程序；当某个程序不可用时，它会指出对应的配置条目并停止。

`cli:` 条目是例外：它的 `install:` 行是提示，不是指令。当这样的程序缺失且安装器无法提供时，运行会在最后打印该行，并且仍然成功。没有 `install:` 行的 `cli:` 条目仍然是必需的。

| 程序 | 由谁点名 | 安装器提供方式 |
|---|---|---|
| `mosquitto` | `peer_agent:` / `subagent:` broker | 如果 `127.0.0.1:1883` 上已有 broker，则直接使用；否则 Debian/Ubuntu 的包解包到 `~/.local`，macOS 使用 Homebrew，Windows 使用正在运行的 broker，或打印手动安装命令。 |
| `cloudflared` | `file-tunnel.providers.cloudflare` | 官方二进制放在 `~/.local/bin`（或 `%LOCALAPPDATA%\slife2\tools`）；macOS 使用 Homebrew |
| `ssh` | `file-tunnel.providers.localhost.run` | 系统 OpenSSH 客户端 |
| `npx` | 带 `command: npx` 的 `mcp:` 条目 | 官方 Node.js LTS 压缩包放在 `~/.local/lib/nodejs`（或 `%LOCALAPPDATA%\slife2\tools\node`）；其包在首次使用时下载 |
| `uvx` | 带 `command: uvx` 的 `mcp:` 条目，以及 `rest-api:` 条目 | uv |
| `yt-dlp` | `cli:yt-dlp` | `uv tool install yt-dlp` |
| `browser-harness` | `cli:browser-harness` | `uv tool install --python 3.12 browser-harness` |

skill 的头部也可以声明程序（`requires.bins`）；这些程序同样是必需的。例如随附的 `baidu-search` skill 声明了 `python3`。Windows 上，`python3` 来自 uv 在 `%USERPROFILE%\.local\bin` 中的 `--default` 启动器。

## 安装

### 一键安装

macOS 或 Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
```

Windows 10/11（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1 | iex"
```

该命令下载最新的 `main`，把 slife2 安装到隔离环境中，并安装**随附**配置所需的外部程序。数据和配置位于 `~/.slife2`；二进制和库位于 `~/.local`（Windows 上为 `%LOCALAPPDATA%\slife2`）。再次运行即升级。

已经打开的 shell 会保留它启动时的 `PATH`；安装器最后几行会打印需要执行的 `source` 行，或者请打开一个新终端。

只有当机器上没有 Python 3.13 时才会安装它；`uv python upgrade 3.13` 可将其升级到更新的补丁版本。安装器会把随附的 `skills/` 和 `jobs/` 放入数据目录，**覆盖同名文件**；你用其他名字添加的 skill 或 job 不受影响。`AGENTS.md` 同样会被覆盖——它是系统给模型的工具指南，不是供你编辑的文件。`slife2.yaml` **永不覆盖**：随附版本有变化时，会在它旁边写入 `slife2-default.yaml`，供你比较和合并。安装器要安装什么，读取的是**随附**的 `slife2.yaml`，绝不是你 home 里的文件：升级不会等待那次合并。

从源码检出目录运行时，就在那里运行安装器：

```bash
./scripts/install.sh
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`scripts\install.cmd` 也可以启动安装器。源码目录使用它自己的 `slife2.yaml` 和 `.venv`。

| 选项 | 含义 |
|---|---|
| `--dry-run` | 报告计划执行的动作。 |
| `--help` | 显示用法和选项。 |

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash -s -- uninstall
```

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1))) uninstall"
```

`uninstall` 删除安装器添加的组件；安装前就已存在的组件保持不变。无法删除的组件会保留记录，之后的 `uninstall` 会重试。

### 手动安装

```bash
git clone https://github.com/juzcn/slife2.git
cd slife2
uv sync
```

把你打算使用的 provider 的 API key 存入 OS keyring（通过 `credstore`），或存为环境变量：

```bash
uv run credstore set DEEPSEEK_API_KEY
# or
export DEEPSEEK_API_KEY=...
```

`slife2.yaml` 是必需的——没有内置配置。安装器把随附文件放入新的 home；源码检出使用代码旁的 `slife2.yaml`。`AGENTS.md` 同样必需；它由安装器写入，重装会替换它。

### Docker

容器发行版从源码检出构建。它运行项目自己的安装器，并原样使用随附的 `slife2.yaml`。

```bash
docker build -f docker/Dockerfile -t slife2:local .
```

镜像以非 root 用户 `slife2` 运行；它的源码检出和数据目录是 `/opt/slife2`，运行时目录是 `/home/slife2/.slife2/runtime`。镜像约 3 GB，因为它包含完整配置点名的程序。

镜像没有 OS keyring，因此它的 key 是环境变量。构建运行与宿主机安装相同的 `scripts/install.sh`，因此其日志会列出随附配置点名的每个凭据；把这些凭据写进一个 env 文件，并用 `--env-file` 传入（不要用 `-e KEY=...`，那会把值放进进程表）。运行一个 prompt 后退出：

```bash
docker run --rm -it --env-file slife2.env \
  slife2:local 'Use the shell tool to run pwd and report the output.'
```

或者以 headless 方式启动 agent，让它持续运行：

```bash
docker run --rm -it --env-file slife2.env slife2:local
```

key 是环境变量，不是命令行参数。挂载一份值写成字面量的 `slife2.yaml`，是另一种受支持的来源。如果对话和日志要在容器之外保留，请挂载 `/opt/slife2/agents` 和 `/home/slife2/.slife2`。凭据配置、一次性 prompt 和容器相关说明见 [docker/README.md](docker/README.md)。

## 快速开始

一键安装之后，依赖已经同步，`credstore` 也已安装。设置 key 并启动窗口：

```bash
credstore set DEEPSEEK_API_KEY
slife2
```

从源码检出运行时，用 `uv run slife2` 启动。手动安装要从更早一步开始：

```bash
uv sync
uv run credstore set DEEPSEEK_API_KEY
uv run slife2
```

`slife2` 会启动配置所需的 MCP server，复用已经在运行的，打开终端界面，并在最后一个实例退出时停止这些 server。

```bash
slife2 status    # what this configuration's servers are doing
slife2 down      # stop them
```

## 命令行接口

```text
slife2 [COMMAND] [OPTIONS]

Commands:
  run       Ensure the servers are available, then run the TUI (default).
  status    Report what each server named by the configuration is doing.
  down      Stop the servers named by the configuration.
  logs      Read a server's log, or list the logs that exist.
  probe     Ask a running agent one task over the peer network.
```

`down` 停止共享 server 和 broker，但不关闭已打开的窗口。保持打开的窗口在 server 再次启动后的下一次输入时重连，对话会被恢复。它的 agent 名字在窗口退出前一直保持占用，因此在此之前，同名 `--agent` 的新实例会被拒绝。另一个活着的客户端正在使用 server 时，`down` 也会拒绝；`--force` 仍然停止它们，并打断该客户端正在进行的轮次。

选项：

| 选项 | 含义 |
|---|---|
| `--data-dir DIR` | 使用 `DIR` 作为数据目录。覆盖 `SLIFE2_DATA_DIR`。 |
| `--agent NAME` | 以 `NAME` 运行，或用 `probe` 询问 `NAME`。默认 `slife2`。每个名字每台机器只能有一个活实例。 |
| `--model PROVIDER/MODEL` | 以该模型启动。只写 provider 时选它的第一个模型。默认使用配置的 `default`。仅 `run`。 |
| `--url URL` | 把窗口连接到 `URL` 处的 agent server。 |
| `--a2a-broker HOST:PORT` | 本次运行中，peer-agent 网络使用这个 broker。它只覆盖 `peer_agent.broker`，绝不覆盖 subagent 网络的 broker。它不能启用一个没有 `peer_agent:` 节的网络；当 A2A server 已在另一个 broker 上运行时，它会被拒绝。 |
| `--keep-servers` | 本实例退出后保留共享 server。 |
| `--headless` | 在没有显示的情况下运行同一个窗口、server、连接和轮次。agent 仍可在 peer 网络上访问。按 Ctrl+C 退出。 |
| `--lang {en,zh}` | 设置窗口语言。不带该参数时从操作系统检测。 |
| `--json` | 以 JSON 打印 `status`（`ok`、`servers`、`brokers`；每一行都带自己的日志路径）。 |
| `--force` | 即使另一个客户端正在使用，也停止共享 server。仅 `down`。 |
| `--prompt TEXT` | 要询问运行中 agent 的任务。仅 `probe`。 |
| `--timeout SECONDS` | `probe` 等待完整回答的时长。默认 3600。 |
| `--channel {a2a,tui}` | `probe` 走哪条路询问：A2A peer 网络（默认），或 agent task 投递的回答。 |
| `--lines N`、`-n N` | `logs` 显示多少行日志；`0` 读取整个日志。默认 50。 |
| `--all` | 配合 `logs` 使用，在每个已配置 server 自己的标题下打印其日志。 |
| `--version` | 打印版本并退出。 |

只有整套栈完全可用时，`status` 和 `down` 才以 0 退出：任一配置的 server 或 broker 未运行时，`status` 返回 2；server 或 broker 未停止，或命令因另一个客户端活着而被拒绝时，`down` 返回 2。server 无法启动时，`run` 返回 2。

`--headless` 是同一个窗口去掉显示，不是另一种 agent。system prompt 会被告知没有人在看。

`slife2 probe --agent NAME --prompt '…'` 向运行中的 agent——headless 或打开的窗口——询问一个任务。`--channel a2a`（默认）是一个模拟 peer：它在 peer 网络上发送 `task_request`，目标运行该轮，模型的 `task_response` 就是回答；它需要 `peer_agent:` 节。`--channel tui` 通过 agent task 询问，回答是 task 投递的 `agent-task:<name>` 消息——而不是窗口的 assistant 文本，后者回答的是并入这一轮的所有内容。两种模式在得到完整回答时退出 0，用法错误时退出 2，没有完整回答返回时退出 3。容器的一次性模式走默认的 A2A 路。

`--lang` 只影响窗口：标签、harness 行、面板、模型选择器、审批行和状态栏。system prompt、工具 schema 和日志行保持英文。外部文本按原语言透传。

### 插件可执行文件

每个插件也可以直接启动，用于进程管理器，或单独调试某个 server。

| 可执行文件 | server | 默认端口 |
|---|---|---|
| `slife2-agent` | agent 主循环与对话 | 24000 |
| `slife2-llm-openai` | OpenAI 兼容 chat completions | 24001 |
| `slife2-llm-anthropic` | Anthropic Messages | 24002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 24003 |
| `slife2-llm-embeddings` | Embeddings | 24004 |
| `slife2-tui` | 终端收件箱，每个 agent 一个会话 | 24005 |
| `slife2-context` | 轮次日志与上下文决策 | 24010 |
| `slife2-toolhub` | 工具目录与模型工具清单 | 24020 |
| `slife2-wechat` | 微信登录与收发 | 24030 |
| `slife2-skills` | playbook 与 `skill_use` | 24031 |
| `slife2-cli` | `cli:` 节 | 24032 |
| `slife2-jobs` | 代码定义的工具（`job__<name>`） | 24042 |
| `slife2-mcp-tools` | `mcp:` 节 | 24033 |
| `slife2-restapi-tools` | `rest-api:` 节 | 24034 |
| `slife2-cabinet` | 笔记、日记、文件、报告；一轮的 dump 和图片 | 24035 |
| `slife2-media` | 图片、视频、语音、转写 | 24036 |
| `slife2-agent-task` | 后台 agent task | 24037 |
| `slife2-file-tunnel` | 本地文件的公网 HTTPS URL | 24038 |
| `slife2-schedule` | 定时任务及其运行账本 | 24039 |
| `slife2-a2a` | 基于 MQTT 的 peer-agent 网络 | 24040 |

subagent 网络通过自己的入口模块 `python -m slife2.plugins.a2a.local_server` 运行同一实现，端口 24041；它没有单独的控制台脚本。

**WSL2 使用 `networkingMode=mirrored` 时，WSL 实例会把这些端口整体偏移 100**（agent 24100、toolhub 24120）。本地 MQTT broker 不偏移：两套栈共享它。镜像模式让 Windows 和 WSL 的 loopback 合为一体，因此默认端口上的 WSL 栈会暴露给 Windows 实例，并被后者静默复用。`wsl_port_offset` 改变偏移量：默认 100，`0` 关闭，在镜像模式之外被忽略。

## 使用终端界面

### 按键

| 按键 | 动作 |
|---|---|
| `Enter` | 发送消息。 |
| `Shift+Enter` | 插入换行。 |
| `Esc` | 打断正在运行的轮次。 |
| `Ctrl+C` | 有选中内容时复制；否则打断；再否则退出。 |
| `Ctrl+S` | 打开模型选择器。`↑`/`↓` 移动，`Enter` 选择，`Esc` 关闭。 |
| `Ctrl+O` | 打开 subagent 和正在运行的 agent task 的选择器；选中一个会打开只读视图。`Esc` 关闭。 |
| `Ctrl+Q` | 退出（Textual 的绑定；不显示，因为带流控的终端可能截获它）。 |
| `PageUp` / `PageDown` | 翻页浏览对话记录。 |
| `Home` / `End` | 跳到对话记录的开头或结尾。 |
| `Ctrl+A` / `Ctrl+E` | 移到草稿的开头或结尾。 |

鼠标滚轮滚动对话记录。向上滚动时，新 token 到达不会把视图拉走；发送消息后回到结尾。

### 发送消息

轮次运行期间发送的消息会在队列中等待。运行中的轮次会在下一个迭代边界取走它，并并入该轮；本轮没有触及的消息，轮到它的位置时作为自己的一轮运行。状态栏显示队列深度。`Esc` 打断正在运行的轮次，但保留等待中的消息。

### 计划

模型可以为手头的工作维护一个计划。每一项是一行文本加一个状态——`pending`、`active`、`done` 或 `blocked`——模型用 `plan_write` 写入整份列表。列表随每一轮开头的 `[TURN-PROMPT:{…}]` marker 一同传递，因此上下文重建不会丢掉它；输入框上方的条带会显示它。这些状态是模型自己的声明，不是 harness 的观测。计划以 `plan.yaml` 的形式存放在对话自己的目录中；写入空列表即删除它。

### 模型选择

`Ctrl+S` 列出 `providers:` 下的每一个模型，并标出正在使用的那个。选中一个会调用模型自己也能调用的同一个 `model_switch` 工具。对话保留历史，并在下一轮切换到新模型；进行中的轮次会在它启动时的模型上跑完。

### 观看 subagent 或 agent task

`Ctrl+O` 列出本 agent 创建的 subagent 和它正在运行的 agent task，并打开你所选条目的只读视图。视图先显示已存储的对话，再显示正在进行的轮次——包括你打开它时已经在运行的那一轮。task 没有已存储的对话：它的视图以收到的 description 开头，然后是它唯一的一轮。`Esc` 返回。视图是只读的：观看不会让你成为该 subagent 的窗口，prompt 不会显示，审批请求也不会交给你。在被观看的对话中，唤醒它的消息显示为 `Agent(<name>)>`；来自兄弟 subagent 的消息显示为 `Subagent(<name>)>`；task 说的话显示为 `AsyncTask(<name>)>`。

只列出正在运行的 task。如果 task 在选择器打开之后、你做出选择之前停止，它的视图会以最终状态和结束时携带的那句话打开。观看永远不会让 task 变成 finished：它的回答仍然通过 hand-over 到达对话。

### 恢复对话

关闭再打开窗口、重启 server，或经过空闲清理，都不会开始新对话。对话记录会按原始时间戳、工具调用和结果恢复；推理折叠在它实时运行时折叠的位置。

状态栏报告上一次模型调用留下的上下文大小，以及它占 **prompt 预算**（模型窗口减去 `max_tokens`，provider 把两者算在同一个窗口里）的比例。每一轮开始时，`_context_rebuild` 的回答会点名上下文决策保留了什么、召回了什么。它还报告 `tools 26/358`：先是 hub 已加载的集合，然后是目录的大小。skill、job 和 CLI 条目都计入总数，已下线 server 的工具也计入；前半部分是下一次请求实际携带的内容。两个数字来自同一次询问。

### 图片

在句子里写出一个图片文件：

```text
What is wrong with this layout? D:\shots\screenshot.png
```

模型会为它调用 `attach_image`。没有特殊语法。一次调用可以接受本地路径、`http(s)` URL 或 `data:` URI，也可以一次接受多个。无法读取的来源会在回答中被点名，但不会让其余部分失败。URL 会交给 provider，由它去抓取。

只有当模型条目在 `input` 下列出 `image` 时，图片才可用。实时对话携带像素；轮次日志保留句子、工具调用与回答，以及指向文件柜副本的 `[Image:…]` 引用。之后的轮次会用引用的 key 或图片的 URL 调用 `attach_image`。provider 拒绝某张图片时会丢弃它并说明；那一轮的行是 note-only。

## 配置

### 位置

配置文件是数据目录里的 `slife2.yaml`。当工作目录是本源码检出目录——其 `pyproject.toml` 点名了本项目——时，数据目录就是该工作目录；否则是 `~/.slife2`。用 `--data-dir DIR` 或 `SLIFE2_DATA_DIR` 覆盖它。

文件是必需的，并且必须在 `providers:` 下点名至少一个 provider，在 `embeddings:` 下点名至少一个 embedding provider。文件缺失时，启动会停止，并用一行错误指出搜索过的目录；文件无法加载时，启动会停止，并指出文件与错误。

安装器可能在已有配置旁写入 `slife2-default.yaml` 作为参考。slife2 只读 `slife2.yaml`。

`AGENTS.md` 与配置并列，同样必需。它是模型对其工具的指南，包含在每个对话开头的 system prompt 中。它是系统的交付物，不是供人编辑的文件：重装会替换它，也不存在 `AGENTS-default.md`。你自己要追加的 prompt 内容，应放在 `agent.system_prompt` 点名的模板中。

`slife2.yaml` 可以安全地提交到版本库。它不应包含秘密；每个凭据都是运行时解析的 `${VAR}` 引用。

### 配置节

| 节 | 用途 |
|---|---|
| `providers:` | chat provider 及其模型；`providers.default:` 点名新对话启动时使用的模型，写作 `provider/model`。 |
| `embeddings:` | 轮次、工具和文件柜索引使用的 embedding provider；`embeddings.default:` 选择正在使用的那个，每个可以指定一个可选的 `rerank_model`，供召回的精排阶段使用。 |
| `media:` | 图片、视频、语音和转写 provider。 |
| `file-tunnel:` | 本地文件的公网 URL provider；`file-tunnel.default:` 选择它。 |
| `mcp:` | 外部 MCP server（stdio 或 HTTP）。 |
| `rest-api:` | 暴露为工具的 OpenAPI 文档。 |
| `cli:` | 机器上已经安装的命令。 |
| `skills:` | `skills/` 下 playbook 的环境。 |
| `agent:` | 迭代上限和 system-prompt 模板。 |
| `context:` | 上下文决策的限额。 |
| `agent-task:` | `max_running`，每个 agent 的后台任务数。 |
| `subagent:` | `max_running`，以及本地 subagent 网络的 broker 和 `org`。 |
| `peer_agent:` | 远端 peer-agent 网络。 |
| `tool_load:` | 已加载工具的上限，以及启动即加载且永不被逐出的工具。 |
| `tool_result:` | 单个工具结果的大小预算，单位为 UTF-8 字节。 |
| `servers:` | 每个插件 server 的地址和端口。缺省时使用默认值。 |

没有 `wechat:`、`cabinet:`、`schedule:` 或 `tui:` 节。微信登录通过扫描二维码创建，并保存在 agent 的目录下。文件柜、调度器和终端收件箱由各自的 server 及其持有的数据启用。

### 秘密

值可以是字面量字符串，按原样使用；也可以是 `${VAR}`，依次通过 shell 环境和 [credstore](https://pypi.org/project/credstore/) 的 OS keyring 解析。

**只有 credstore 管理的秘密会被输出保护。** slife2 从 credstore 解析出的凭据在工具结果、工具清单和日志行中以掩码显示——当两端至少各有四个字符被隐藏时，显示前四后四，否则显示 `***`。短于八个字符的值不登记。环境变量或字面量是操作者自己的。把每个秘密都放进 credstore：它是 slife2 唯一会在所有地方遮罩其值的存储。

解析是惰性且宽松的。provider 或 media 调用在凭据未能解析时，会在使用前拒绝。工具 server 的 `env:` 或 `headers:` 条目如果仍保留着引用，会被省略并给出警告，由子进程自己的缺失 key 策略决定：有的 server 无 key 运行，有的用它自己的话拒绝。

### 示例

```yaml
agent:
  max_iterations: 0          # 0 or absent means no cap
  compact_at: 0.9            # of the prompt budget (window - max_tokens); 0 disables

context:
  ceiling: 0.8               # of the same budget
  recall_page_limit: 80
  min_similarity: 0.52
  keyword_weight: 1.0
  rerank_min_score: 0.3

mcp:
  serper:
    command: npx
    args: [-y, serper-search-scrape-mcp-server]
    env:
      SERPER_API_KEY: ${SERPER_API_KEY}

rest-api:
  github:
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.yaml
    base_url: https://api.github.com
    api_key: ${GITHUB_TOKEN}

cli:
  yt-dlp:
    command: yt-dlp
    description: Download video from 1000+ sites, subtitles and playlists.
    install: uv pip install yt-dlp
```

随附的 `slife2.yaml` 就地带有注解，是每个 key 的参考。

### 编辑配置

配置可以手工编辑，也可以由模型编辑。每个配置节或文件夹有各自的工具：

| 配置节或文件夹 | 工具 |
|---|---|
| `mcp:` | `mcp_get`、`mcp_set`、`mcp_list`、`mcp_remove`、`mcp_set_enabled` |
| `rest-api:` | `rest_api_get`、`rest_api_set`、`rest_api_list`、`rest_api_remove`、`rest_api_set_enabled` |
| `cli:` | `cli_get`、`cli_set`、`cli_list`、`cli_remove`、`cli_set_enabled` |
| `skills:` | `skill_draft`、`skill_set`、`skill_edit`、`skill_list`、`skill_remove`、`skill_set_enabled` |
| `jobs/`（文件夹） | `job_set`、`job_list`、`job_read`、`job_run`、`job_remove` |
| `providers:`（含其 `default:`） | `model_get`、`model_set`、`model_list`、`model_remove`、`model_switch` |

所有写入都经过同一个模块（`slife2.libs.host.configfile`），并在跨进程锁下进行。写入会保留注释，结果由启动时使用的同一个 loader 校验。loader 拒绝的条目会被还原，并告诉调用方原因。

`set` 写入整个条目：省略的字段会被删除。对于 `model`、`mcp`、`rest-api` 和 `cli`，请先读取对应的 `*_get`：它返回整个对象，`*_set` 会按所有字段必填的方式收回它，省略字段是 schema 错误，而不是删除。`set_enabled` 只改 `enabled`。写入前没有审批关卡。

## 能力

### 对话与记忆

对话不会无界地累积上下文。每一轮的第一个 boundary，harness 会询问决策模型：哪些历史轮次保留、哪些从日志召回（`keep`/`recall`），然后运行这一轮，上下文是这些选择加上本轮本身。模型也可以自己调用 `_context_rebuild`。预算按 prompt 预算（窗口减去 `max_tokens`）计算；超过 `context.ceiling` 的选择会从最旧的一端裁掉，`fitted` 说明裁掉了多少轮。

一轮超过该预算的 `agent.compact_at`（默认 0.9）时，会用 `_turn_compact` 把自己不再需要的内容从视图中隐藏；日志保留一切，下一轮从完整对话开始。

`turn_list`、`turn_search`、`turn_read` 和 `turn_summarize` 用于浏览模型自己的历史。`turn_search`、`cabinet_search` 和 `tool_search` 共用一种回答形式：输入 `keywords` 和 `sentences`，输出 `results`、`best`、`confidence`、`browsed` 和 `limit`。

一轮以 `[TURN-PROMPT:{…}]` 开头（包含 `now`，存在计划时还有 `plan`）；被重建的轮次带有一个 `[TURN:{…}]` 脚注。大写开头的 marker 是 harness 的注解；像 `[Wechat:{…}]` 这样词形的 marker 是发送者写的，会随消息一起存储。

### 工具

`slife2-toolhub` 拥有模型的工具清单。`slife2-mcp-tools` 持有 `mcp:` 连接，`slife2-restapi-tools` 持有 `rest-api:` 连接；hub 合并它们声明的内容、为工具命名、进行把关，并把调用路由回去。

hub 提供四个集合级工具：

- `tool_search` 按关键词和语义搜索整个目录；`unloaded_only` 排除已经在清单中的工具。
- `tool_list` 用 `category`/`source` 过滤条件分页列出目录。
- `func_tool_load` 把工具放入展示给模型的清单。
- `_func_tool_unload` 移除它们；`tool_load.autoload` 中的名字会被拒绝。

模型可以不加载就直接调用目录中的任何名字：harness 会加载它，并在同一轮中运行这次调用，所以被预算逐出、只留在历史里的名字也照样可用。目录只负责确认工具存在，参数由工具自己的服务判断，它的拒绝就是模型读到的回答。加载后，该工具及其当前声明会保留在每次请求随附的清单中。`tool_load.autoload` 点名启动时即加载的工具；其余工具按需发现。

已加载的集合在重启后保留。它受 `tool_load.threshold` 限制（默认 100）；超过上限时，最久未被使用或加载的工具会被卸载，`tool_load.autoload` 中的任何工具都不会被逐出，模型也不能卸载它们。

工具命名：`mcp:` 工具是 `{server}__{tool}`；插件自己的工具使用裸名字。两个会呈现同一个名字的工具会被拒绝。

工具结果是一个 JSON 对象，`_check_new_input`（人的消息原文）和 `skill_use`（playbook 文本）除外。列表类回答 `{"<noun>": [], "total": N, "limit": L, "offset": O}`；拒绝会抛出 `ToolError`。超大的输出会存成 dump（见[文件柜](#文件柜)）。

#### 工具调用选项

每个函数工具都接受三个元参数，它们会被加入每个对外公布的 schema，并在分发前剥离。

| 参数 | 效果 |
|---|---|
| `_timeout` | 本次调用的秒数，覆盖工具的默认值。任何不是正有限数的值——`0`、负数、文本——都会使调用失败；它绝不会被读作“不限时”或“默认值”。自带 `timeout` 参数的工具会自己执行该超时。 |
| `_async` | 在后台运行调用。模型会收到一个 `task_id`；结果带着 `[AgentToolResult:{…}]` marker 从 `async-tool:<tool>` channel 到达。`check_async` 报告状态；`cancel_async` 放弃它。agent task 只有一轮：它的 prompt 告诉模型不要使用 `_async` 或启动另一个任务。 |
| `_approve` | 运行前询问用户。终端会画出一行内联提示（`y` 批准，`n` 或 `Esc` 拒绝），并用 `answer_approval` 回答。如果 channel 背后没有人，调用会直接运行而不询问。能够回答的调用方用 `watched=true` 声明这一点。 |

#### skill 与命令

playbook 位于 `<data>/skills/<name>/SKILL.md`。`skill_use` 返回 playbook 的文本，并在前面加上它所在的目录。skill 是指令，不是代码；slife2 中没有任何东西会执行它。它的头部可以声明环境要求，从 `skills:` 节解析；`skill_use` 会在模型依赖它之前报告缺失的 key。`skill_draft` 把工作流转成 `SKILL.md` 而不写入任何内容；`skill_set` 安装 skill，`skill_edit` 只写入它点名的文件。`skill_remove` 删除文件夹及其 `skills:` 条目。

`cli:` 条目描述机器上已经安装的程序。每个条目都是目录中的一行（`cli:yt-dlp`），可以被 `tool_search` 找到，但它不是可调用工具，也没有加载状态：模型通过自己工具集里的 shell 工具运行该命令（随附配置提供 `desktop-commander`）。程序缺失时会显示条目的 `install:` 行。`enabled: false` 保留条目的配置，但从不连接它；该值必须是真正的 YAML 布尔值（在 YAML 1.2 下 `no`、`off` 和 `'false'` 都是字符串，会被拒绝）。

#### job

`job_set` 写入一个 **job**——`<data>/jobs/` 里的一个 Python 文件，一个与文件同名的模块级函数——该函数成为 `job__<name>`。docstring 和带类型注解的签名就是工具定义；没有 manifest。同一次调用会替换 job，如果新文件无法加载，则恢复上一版本。`job_remove` 删除它；`job_list`、`job_read` 和 `job_run` 分别列出、读取和测试它。

job 通过 `slife2.job` 访问系统：`call_tool(name, arguments)` 按模型调用的名字访问 hub 的目录，`call(server, tool, arguments)` 按 `servers:` 中的键访问任何 server。代码在 jobs 插件中以调用方的身份运行，不在沙箱中，顶层工具调用就是审批点。`job-coding` skill 是编写指南。

### 媒体

`media:` 节提供四项能力：

- `generate_image`——按提示词绘制图片。
- `generate_video`——按提示词或图片渲染视频。
- `text_to_speech`——把 `text`，或 `input` 点名的文本文件，读成音频文件。
- `transcribe_audio`——转写 `input` 点名的音频文件；`output` 把 transcript 写到你指定的路径，不给 `output` 时 transcript 就是回答，过长时会存成 dump。

每个生成工具默认写入 agent 的 media 目录；`output` 改为指定文件（相对路径相对于本对话的工作目录）。每个工具都以文本回答文件路径，或——对不带 `output` 的 `transcribe_audio`——回答 transcript。

每个模型声明自己的用途：

```yaml
media:
  defaults:
    image: bailian_personal/wan2.7-image
    video: bailian_personal/happyhorse-1.1-t2v
  providers:
    bailian_personal:
      api: dashscope-aigc
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1
      api_key: ${QWEN_TOKEN_PLAN_PERSONAL}
      models:
        - {model: wan2.7-image, kind: image}
        - {model: happyhorse-1.1-t2v, kind: video, params: {resolution: 720P, duration: 5}}
        - {model: qwen-audio-3.0-tts-plus, kind: tts, voice: longanhuan_v3.6}
        - {model: qwen-audio-3.0-asr-flash, kind: asr}
```

支持的 `api:` 值是 `dashscope-aigc`（图片、视频、语音、转写）和 `openai-images`（仅图片生成）。选择不支持所请求 kind 的模型会被拒绝。

生成的文件默认写在 `<data>/agents/<agent>/media/` 下。每个生成工具还接受一个 `output` 路径：相对路径相对于对话的工作目录，绝对路径按原样使用。长时间运行的视频调用如果超过超时时间，会带着 provider 的 job id 被拒绝，而不是返回结果。

### 文件隧道

`slife2-file-tunnel` 把本地文件发布到公网 HTTPS URL：

```text
expose_local_image(path)      -> https://<tunnel>/share/<token>
unexpose_local_image(token)   -> the URL stops working
```

文件从原位置提供。文件变化、调用 `unexpose_local_image`，或进程退出时，链接就会失效。一个共享比创建它的轮次活得更久。私钥、`.env` 文件和保存的密码会按名字被拒绝。`path` 是绝对路径，或相对于数据目录的路径——agent 自己的工具就在那里运行。

隧道指向一个只提供 `/share/<token>` 的 loopback 监听器；MCP 端点不会被发布，因此知道主机名并不能让调用方运行工具。

支持三个 provider。恰好有一个处于活动状态；其余可以保留配置但不生效。

| provider | 要求 | URL 行为 |
|---|---|---|
| `ngrok` | `NGROK_AUTHTOKEN` | 每个账号一个稳定的开发域名；免费版对浏览器显示插页。 |
| `localhost.run` | `ssh` 客户端 | 随机的 `*.lhr.life` 主机名，每隔几小时轮换；TLS 在 provider 处终止。 |
| `cloudflare` | `cloudflared` 二进制 | 进程生命周期内稳定的 `*.trycloudflare.com` 主机名。 |

内置默认值是 `ngrok`。随附的 `slife2.yaml` 选择 `cloudflare`，它不需要账号；如果未安装 `cloudflared`，把 `active:` 设为 `localhost.run`。未启动、或 URL 无法访问的隧道会被拒绝，而不是被交出去。`tunnel_status` 工具报告状态、URL、可达性、边缘地址和所用的二进制；它是内部诊断工具，不暴露给模型。

### 文件柜

文件柜是每个 agent 的存储，保存笔记、日记条目、已保存文件、报告、dump 和图片。前四类构成它的语料库；语义检索按文件的全文覆盖笔记、日记的某一天和报告，而已保存的文件只按词找到，dump 和图片则通过轮次中的 marker 寻址。

笔记、日记的某一天和报告共用四个动词：`{kind}_write` 创建，key 已存在时拒绝（并指出 `_read` 和 `_update`）；`{kind}_read` 读取；`{kind}_update` 替换整篇文档；`{kind}_remove` 删除它。

| 种类 | key | 写入的含义 |
|---|---|---|
| `note` | 用户选定的主题 | 在该主题下创建笔记。 |
| `diary` | 日期 | 创建当天的条目；一天就是一篇文档。 |
| `file` | 它在文件柜内的路径 | `file_save` 复制本地文件或抓取 `http(s)` URL 进来；`file_read`、`file_grep` 和 `file_remove` 读取、搜索和删除它。 |
| `report` | 标题 | 在该标题下创建报告。 |
| `dump` | 产生该输出的工具调用 | 由 harness 写入；同一调用的第二次写入会替换它。 |
| `image` | 它的内容摘要 | 由 harness 写入；字节存放在 `pictures/` 中；一个摘要就是一行。 |

替换是整篇文档；省略的 tag 或 period 会被清空。

dump 是对对话而言过大的工具输出。它完整存储在它的文件中，以工具调用 id 为键；对话携带一个标有 marker 的开头，大小在 `tool_result.bytes` 之内。`dump_read` 按行或按字符读取任意部分；`dump_grep` 在单行内查找字面量，并同时回答两个地址。

已保存的文件用同一套读取器（`file_read`、`file_grep`）在其解码后的文本上读取和搜索。两者都流式处理，因此内存中存放的是窗口，而不是整个文件；文件永远不会被截断，`next` 请求下一个窗口。PDF、图片或压缩包会以其字节的有界转义渲染显示。

图片完整存储在 `pictures/` 下，以内容摘要为键，轮次携带 `[Image:…]` 引用；`attach_image` 是模型再次看到它的方式。

笔记、日记条目和报告只作为文件存在一份，位于 `agents/<agent>/cabinet/` 下；行是地址，文件的文本就是语义检索所嵌入的内容。每个回答都带 `path`，即相对于数据目录的文件路径，因此模型可以把它交给 `pandoc` 或 `rg`。

`file_save` 复制本地文件（从不移动它），或抓取 `http(s)` URL。每个来源各自独立：回答会列出保存了什么、哪些失败了。抓取的 URL 只能访问公网——带凭据、`localhost` 和非全局可路由的字面地址会被拒绝——并且上限为 50 MiB。

### 微信

`slife2-wechat` 通过 iLink 的托管桥接为每个 agent 连接一个微信账号。登录靠扫描而不是配置。`wechat_login` 返回二维码；扫描后会把 token 保存到 `agents/<agent>/wechat.yaml`，下次启动时读取。登录约持续一天；`wechat_check_status` 报告剩余时间。

一个微信账号只有一个活登录。为第二个 agent 或在第二台机器上扫描同一账号，会静默让第一个登录退役，后者随后会从桥接收到 `session timeout`。第一个 agent 的窗口显示 `[✗ Wechat offline]`，`wechat_check_status` 给出原因。再扫一次即可拿回账号。

| 工具 | 用途 |
|---|---|
| `wechat_login` | 获取二维码并开始等待扫码。 |
| `wechat_check_status` | 报告连接状态、剩余时间，以及要回复的对端。 |
| `wechat_send_message` | 向对端发送文本消息。 |
| `wechat_logout` | 忘记本 agent 的登录。 |

连接的到达与离开会画成 harness 行（`[⚡ Wechat online]`、`[✗ Wechat offline]`），而不是轮次。

来自微信的消息会成为该 agent 对话中的一轮，并画在显示该对话的窗口里。它以 channel `wechat` 和点名发送者的 `[Wechat:{…}]` 前缀记录。回复是模型的工作：没有任何机制会自动派发回答。模型用对端和 marker 中的 `context_token` 调用 `wechat_send_message`。轮次运行期间，对方会看到“正在输入”的提示。每个 agent 单独登录；subagent 不登录，而插件是一个共享进程。

### Agent 任务

agent task 是发起对话——agent 自己的，或某个 subagent 的——的一个独立轮次，用名字和描述启动，稍后由一条消息回答。它只有一轮：它的 prompt 告诉它不要使用 `_async` 或启动另一个任务，因为稍后到达的结果无处可去。

| 工具 | 用途 |
|---|---|
| `agent_task_start` | 启动任务并立即返回其 id。 |
| `agent_task_status` | 报告任务正在做什么。从不返回其结果。 |
| `agent_task_list` | 列出本 agent 的任务，正在运行的在前。 |
| `agent_task_cancel` | 停止任务，并报告调用之后的状态。 |

任务以发起它的对话身份运行：agent 的任务使用 agent 的 system prompt、模型、工具，以及自己的上下文决策；subagent 的任务使用 subagent 的 prompt、角色和模型。**上下文是克隆**：任务读取对话的上下文，并选择它这一轮运行在什么之上；任务结束时，对话存储的上下文保持不变。它没有启动它的那一轮，因此描述必须复述相关上下文。它的回答带着 `[AgentTaskResult:{…}]` marker 从 `agent-task:<name>` channel 到达，显示为 `AsyncTask(<name>)>`。subagent 没有窗口，因此 subagent 的任务回答会投递到其 agent 的对话。任务停止时如果没有窗口正在显示该 agent，任务以 `failed` 结束——回答就是产物，不会被保留——日志会说明原因。

```yaml
agent-task:
  max_running: 4
```

`max_running` 限制的是并发，不是一段时间内的总工作量。

### 定时任务

定时任务是模型写下的名字、描述和 cron 表达式。`slife2-schedule` 在 slife2 运行期间触发它。任务到期时，调度器会询问是否有窗口正在显示该 agent：没有窗口时，这次运行以 `missed` 结束——回答将无处送达——有窗口时，则直接以 agent task 启动工作。每次运行的工作都是一个名为 `<task>@<due time>` 的 agent task，因此同一任务两次重叠的运行是两个任务，而不是一个名字同时为两者作答。不会打开轮次，也不消耗模型调用。终端无法询问，或派发失败时，这次运行以 `failed` 结束，并记录原因。

| 工具 | 用途 |
|---|---|
| `schedule_set` | 写入或替换任务：名字、描述、cron、时区。 |
| `schedule_remove` | 删除任务及其全部运行。 |
| `schedule_list` | 列出任务及其下次到期时间。 |
| `schedule_runs` | 列出账本：每次触发一行。 |
| `schedule_run_now` | 立刻启动这次运行的工作；带 `due_at` 时，补跑一次错过或失败的运行。 |
| `schedule_run_skip` | 带原因关闭一次不会发生的运行。 |

一次运行就是一次触发，以它到期的那一刻命名。它的状态从 `pending` 变为 `ran`、`failed`、`missed` 或 `skipped`。`ran` 表示任务已完成，且回答到达了对话；无法送达的任务是 `failed`，可以重跑。行会在询问窗口之前写入，因此同一次触发不会在下一次轮询时重复。这个时钟是轮询：一次触发可能在到期时刻之后最多 30 秒才启动，已完成的运行在下一轮轮询时关闭；`ran_at` 是任务自己的结束时间。

任务只在 slife2 运行时触发。启动时，扫描会关闭被停止的进程遗留下来的运行——从未启动的运行记为 `missed`，已经点名了任务的运行记为 `failed`——轮询会为每个任务记录最新一次错过的触发。`schedule_runs(status="missed")` 和 `status="failed"` 列出它们；两者都可以用 `schedule_run_now(name, due_at)` 重跑，或用 `schedule_run_skip` 关闭。

账本是 `<data>/agents/<agent>/schedule.db`。没有配置节。任务的 `timezone` 是 IANA 名称；省略时使用机器当前的时钟，它不跟随夏令时的变化。

### Peer agent

agent 的 peer 是其他 agent，通过已发布的 A2A-over-MQTT profile（0.1，基于 A2A v1.0.0）访问。人在 `slife2.yaml` 中写入 `peer_agent:`；模型看到 `peer_agent_*` 工具，而协议名不会出现在它读到的任何内容中。

| 工具 | 用途 |
|---|---|
| `peer_agent_send_message` | 发送任务（`task_request`）或普通消息；结果稍后到达。 |
| `peer_agent_task_response` | 结算 peer 发给你的任务：completed、failed、rejected，或让任务保持打开的中断状态。 |
| `peer_agent_continue_task` | 继续一个你发出、被 peer 暂停等待输入的任务。 |
| `peer_agent_cancel_task` | 撤回本 agent 发出的任务。 |
| `peer_agent_list` | 先列出本 agent，再按名字列出每个 peer。 |
| `peer_agent_task_list` | 列出未结束的任务：等待回答的，以及仍欠回答的。 |
| `peer_agent_broadcast` | 向每个 peer 发送即发即忘的事件。 |
| `peer_agent_group_set` | 创建群组并拉入你点名的成员；你成为 owner。 |
| `peer_agent_group_list` | 列出本对话知道的群组，包含 owner 和你的角色。 |
| `peer_agent_group_member_list` | 列出群组的 owner 和成员。 |
| `peer_agent_group_remove` | 解散你拥有的群组。 |
| `peer_agent_group_leave` | 退出群组；如果你是 owner，所有权移交给剩余成员中的第一位。 |
| `peer_agent_group_send_message` | 向其他每个成员发送即发即忘的消息。`@name` 和 `@all`/`@所有人` 像微信群一样点名成员。 |

`task_request` 立即返回 id；结果稍后带着 `[PeerAgent:…]` marker 到达。id 和 `status: "unverified"` 描述的是接受，不是送达；无人回答的发送会以 unreachable 通知结束。接受了任务但无法把它交给其对话的 peer 会回答 `rejected`。peer 可以暂停任务并索取输入：用 `peer_agent_continue_task` 继续它，或用 `peer_agent_cancel_task` 撤回它；不要发送新任务。peer 发给你的任务用 `peer_agent_task_response` 结算（`completed`、`failed` 或 `rejected`；中断状态会暂停它）。

每个 agent 一个 mesh；同一名字下的第二个活实例会被拒绝。subagent 不在这个网络上。在线状态是 harness 行，不是轮次；名册是 `peer_agent_list`；窗口（或 `--headless`）持有其 `join` 调用期间，agent 在线。任务状态保存在内存中，因此重启后从零开始。

群组是带 owner 的具名名册；没有邀请流程。群消息是向其他每个成员的即发即忘消息，走 `peer_agent_group:<group>:<sender>` channel。**群聊是随意的**：没有成员欠一个回答，`sent` 只确认已发送，不表示有人读过；需要回答时使用直接发送工具。发送消息时离线的成员会错过它。

配置的 broker 是本地的且不存在时，它会被启动；只有 slife2 自己启动的 broker 才会随栈停止。`provider: mosquitto` 是唯一支持的值。

```yaml
peer_agent:
  provider: mosquitto
  broker:
    host: localhost
    port: 1883
  org: default
  unit: default
```

省略 `broker:` 时，peer 网络使用 subagent 网络的 broker 地址；省略 `peer_agent:` 则让这台机器完全不在该网络上。重启会同样丢弃进行中的轮次和已暂停的任务；仍在等待的 peer 会在自己的 `peer_agent_task_list` 中看到该任务，可以取消或重发。

### Subagent

subagent 是属于另一个 agent 的 agent。它有创建者的工具集、自己的角色化 system prompt 和自己的对话，但没有进程、没有窗口、不占用 agent 名字，也没有外部身份。当有消息发给它时，它在本地 subagent 网络上运行。subagent 是一个常驻 agent，拥有自己的记忆；若只是在对话旁做一件工作，请使用 agent task。

subagent 用 `subagent_create` 创建，用 `subagent_remove` 删除。创建后，它通过本地网络以 `subagent_*` 名字寻址：`subagent_send_message`、`subagent_task_response`、`subagent_continue_task`、`subagent_cancel_task`、`subagent_list`、`subagent_task_list`、`subagent_broadcast`，以及六个群组工具。

| 键 | 含义 |
|---|---|
| `name` | subagent 的名字和目录。 |
| `model` | `provider/model`；为空则使用创建者的模型。 |
| `role` | 一个包含在 subagent prompt 中的词。 |

subagent 位于 `agents/<maker>/subagents/<name>/`，拥有自己的 `turns.db`、`subagent.yaml`、文件柜和媒体目录。调度留在 agent 一侧。subagent 不能创建或删除 subagent。

删除 subagent 时，先让本地网络释放它的位置，然后停止它的循环，最后删除它的目录。如果无法完成网络通知，subagent 保持不变。

本地网络配置在 `subagent:` 下。它需要一个 `broker`；没有 broker 的 `peer_agent:` 节会使用同一地址。`unit` 永远是 agent 的名字，写入一个会被拒绝。`org` 可以设置为群组 mesh。

```yaml
subagent:
  max_running: 32
  provider: mosquitto
  broker: {host: localhost, port: 1883}
  # org: subagent
```

## 数据位置

一个数据目录（**home**）包含：

```text
<data>/
  slife2.yaml                  configuration; required
  AGENTS.md                    the model's guide to its tools; required
  skills/                      <name>/SKILL.md playbooks
  jobs/                        <name>.py code-defined tools
  agents/
    tools.db                   the shared tool catalogue for this home
    <agent>/
      turns.db                 the conversation and its live context
      schedule.db              scheduled tasks and their run ledger
      cabinet/                 notes/, diary/, reports/, saved files, .index.db
      media/                   generated images, video, and audio
      wechat.yaml              this agent's WeChat login
      subagents/
        <name>/                a subagent's turns.db, subagent.yaml,
                               cabinet/ and media/
```

工具目录位于 agent 目录旁边，因为它属于整个 home，而不是某一个 agent。

### 运行时目录

这台机器的共享 server 记账保存在 `~/.slife2/runtime`：

```text
<runtime>/
  servers/        one record per running server
  logs/           each server's output
  locks/          per-endpoint start locks
  agents/         agent-name claims
  clients/        which clients are using the shared servers
  homes/          data directories this machine has used
```

`SLIFE2_RUNTIME_DIR` 覆盖该位置。运行时目录是可重建的：删除它后，下次启动会重建所需内容。

每个 server 默认以 INFO 级别记录日志。`SLIFE2_LOG_LEVEL=DEBUG` 会提高每个用它启动的 server 的级别；每次调用的耗时、工具调用和连接细节都在那里；不是级别名称的值会退回 INFO，并写一行说明。超过上限的日志会在下次启动时轮转为 `<log>.1`。外部 `mcp:` 条目的 stdio 子进程会在 `logs/` 下写自己的文件。

### 多个数据目录

一个数据目录可以容纳多个 agent。它们共享该目录的 `slife2.yaml`、`skills/` 和 `agents/tools.db`，因此运行在同一套工具上。一个 agent 加载的工具，对另一个来说也已加载。

agent 身份独立于目录。`--agent jack --data-dir D` 命名 agent，并指明它的文件去哪里。名字在整台机器上只能被占用一次，因此另一个数据目录中的第二个活 `jack` 会被拒绝。系统不会记住一个名字以前用过哪个目录。

server 跨数据目录共享。为一个 home 启动的 server 服务于机器上的每个 home，每次调用都按它来自的 home 解析。外部 `mcp:` 条目按名字只有一个 server，无论多少个 home 配置了它；如果两个 home 给同名条目不同的命令，日志会报告这一分歧，并使用正在运行的 server。

## 开发

从源码检出运行：

```bash
uv run pytest                  # fast lane: unit and integration, parallel
uv run pytest -m unit          # unit tests only
uv run pytest -m integration   # tests that bind a socket or spawn a process
uv run pytest -m e2e           # full stack as processes
uv run ruff check . && ruff format --check .
uv run pyright
```

Windows 上，`uv run pytest` 不能在活栈旁运行：`uv` 每次运行都会重写 `.venv\Scripts\` 中的入口点，而正在运行的实例会占用 `slife2.exe`（`os error 32`）。请改用环境自己的解释器：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试 lane、CI 矩阵、项目结构和插件注册表见 [SDD.md](SDD.md) §13 与附录 B。

## 已知限制

- skill 是指令，`cli:` 条目是命令：slife2 不为两者提供运行器。模型读取 playbook，并通过其工具集提供的 shell 运行命令。
- job 是代码：它们在 `slife2-jobs` 中以调用方的身份运行，且不在沙箱中。顶层工具调用是审批点。
- 配置编辑之前没有审批关卡，包括带 `command:` 的 `mcp_set`——它记录的是 slife2 将要启动的程序。
- `agent-task.max_running` 限制并发任务数，而不是一段时间内的总工作量。

## 许可证

MIT。见 [LICENSE](LICENSE)。
