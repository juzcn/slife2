# slife2

[English](README.md)

**slife2 是一个终端 AI agent。** 它与语言模型进行对话，通过模型上下文协议（MCP）
向模型提供工具，并通过本地 plugin 进程连接外部世界。终端窗口只是一个客户端：
agent 主循环、工具 hub、模型后端、记忆以及每一项能力，都是 slife2 启动并共享的
MCP server。

本文件是用户手册。设计见 [SDD.md](SDD.md)。这里每一条都是读者可以照着做的事；
不改变读者做法的解释属于设计文档。

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
| **plugin** | slife2 自己的一个 MCP server，由 launcher 启动并共享。外部 MCP server 不是 plugin。 |
| **agent** | 由 `--agent NAME` 命名的身份。一个名字在一台机器上只能有一个活着的持有者。 |
| **client id** | `(agent, subagent)`：一次调用所代表的身份。 |
| **conversation** | 一个 client id 的历史。 |
| **turn** | 一次交换：输入、模型的回答以及它发起的工具调用。 |
| **channel** | 消息到达的位置：`tui`、`wechat`、`peer_agent:<peer>`、`agent:<name>`、`subagent:<name>`、`agent-task:<name>`、`async-tool:<name>` 或 `schedule:<name>`。 |
| **marker** | 发送者在消息前添加的机器可读前缀，例如 `[Wechat:{…}]`。随消息存储，只在显示时移除。 |
| **family** | 拥有一个配置节或文件夹，并向 hub 声明其所含行的 plugin。 |
| **catalogue** | 模型可以发现的所有工具、playbook、job 和命令。 |
| **job** | `jobs/` 里的一个 Python 函数，暴露为工具 `job__<name>`。 |
| **dump** | 因为对对话而言过大而完整保存在文件柜中的工具输出。 |
| **home** | 一个数据目录：配置、`AGENTS.md`、skills、jobs、agent 及其文件。 |
| **runtime 目录** | 共享 server 在整台机器范围内的记录、锁和日志。 |

## 环境要求

- **Python 3.13 或更高版本**
- **[uv](https://docs.astral.sh/uv/)**，用于依赖管理和运行

slife2 在 Windows、macOS 和 Linux 上测试。

### SQLite 扩展支持

向量索引是一个 SQLite 扩展（`sqlite-vec`），Python 解释器必须能够加载 SQLite
扩展。Windows 和 Linux 的构建通常支持；macOS 上 uv 自带的 CPython 和 Apple 的
`/usr/bin/python3` 都不能，请使用 Homebrew 的 Python：

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

如果无法加载扩展，数据库就无法打开，任何 plugin 都无法启动，第一条命令会以
`this Python's sqlite3 cannot load extensions` 失败。

### 外部程序（按配置提供或提示）

配置里点名的 server、隧道或 skill 所用到的程序都是必需的。安装器以普通用户身份
提供这些程序；当某个程序不可用时，它会指出对应的配置条目并停止。

`cli:` 条目是例外：它的 `install:` 行是提示，不是指令。当程序缺失且安装器无法
提供时，运行会在最后打印该行（与 `credstore set` 提示并列），并且仍然成功。
没有 `install:` 行的 `cli:` 条目仍然是必需程序。

| 程序 | 由谁点名 | 安装器如何提供 |
|---|---|---|
| `mosquitto` | `peer_agent:` / `subagent:` broker | 已经在 `127.0.0.1:1883` 运行的 broker 直接使用；否则 Debian/Ubuntu 包解包到 `~/.local`，macOS 用 Homebrew，Windows 使用运行中的 broker 或打印手动安装命令。 |
| `cloudflared` | `file-tunnel.providers.cloudflare` | 官方二进制放在 `~/.local/bin`（或 `%LOCALAPPDATA%\slife2\tools`）；macOS 用 Homebrew |
| `ssh` | `file-tunnel.providers.localhost.run` | 系统 OpenSSH 客户端 |
| `npx` | `command: npx` 的 `mcp:` 条目 | 官方 Node.js LTS 压缩包解包到 `~/.local/lib/nodejs`（或 `%LOCALAPPDATA%\slife2\tools\node`）；其包在首次使用时下载 |
| `uvx` | `command: uvx` 的 `mcp:` 条目，以及 `rest-api:` 条目 | uv |
| `yt-dlp` | `cli:yt-dlp` | `uv tool install yt-dlp` |
| `browser-harness` | `cli:browser-harness` | `uv tool install --python 3.12 browser-harness` |

skill 的头部也可以声明所需程序（`requires.bins`），它们同样是必需的。例如随附的
`baidu-search` skill 声明了 `python3`。Windows 上 `python3` 来自 uv 在
`%USERPROFILE%\.local\bin` 的 `--default` 启动器。

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

命令下载最新的 `main`，把 slife2 安装到独立环境，并安装当前配置所需的外部程序。
数据和配置在 `~/.slife2`；二进制和库在 `~/.local`（Windows 为
`%LOCALAPPDATA%\slife2`）。再次运行即升级。

只有机器上没有 Python 3.13 时才会安装它；`uv python upgrade 3.13` 可升级到更新的
补丁版本。安装器会把随附的 `skills/` 和 `jobs/` 放入数据目录，**同名文件会被覆盖**；
用其他名字添加的 skill 或 job 不受影响。`AGENTS.md` 和 `slife2.yaml` **永不覆盖**：
随附版本有变化时，会写回 `AGENTS-default.md` / `slife2-default.yaml` 供你比较合并。

从源码检出目录运行安装器：

```bash
./scripts/install.sh
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`scripts\install.cmd` 也可以启动安装器。源码目录使用它自己的 `slife2.yaml` 和
`.venv`。

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

`uninstall` 只删除安装器添加的组件；安装前就存在的组件保持不动。无法删除的组件
会保留记录，之后再次 `uninstall` 会重试。

### 手动安装

```bash
git clone https://github.com/juzcn/slife2.git
cd slife2
uv sync
```

把要用的 provider 的 API key 存进 OS keyring（通过 `credstore`）或导出为环境变量：

```bash
uv run credstore set DEEPSEEK_API_KEY
# 或者
export DEEPSEEK_API_KEY=...
```

`slife2.yaml` 是必需的——没有内置配置。安装器会把随附文件放入新的 home；源码检出
直接使用代码旁的 `slife2.yaml`。`AGENTS.md` 同样必需，规则相同。

### Docker

容器发行版从源码检出构建。它运行项目自己的安装器，并使用随附的 `slife2.yaml`。

```bash
docker build -f docker/Dockerfile -t slife2:local .
```

镜像以非 root 用户 `slife2` 运行；源码和数据目录是 `/opt/slife2`，runtime 目录是
`/home/slife2/.slife2/runtime`。镜像约 3 GB，因为它包含完整配置点名的程序。

镜像没有 OS keyring，所以把 key 放在卷里的 credstore 加密文件中。创建一次，并把
配置点名的每个 key 存进去：

```bash
docker volume create slife2-cred
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  --entrypoint bash slife2:local
# 在该 shell 中：
credstore set-password            # 设置主密码
credstore set DEEPSEEK_API_KEY    # 然后逐条执行构建日志列出的行
```

构建运行与宿主机相同的 `scripts/install.sh`，因此其日志会列出随附配置所需的每一条
`credstore set <KEY>`。跑一次 prompt 然后退出——镜像会询问主密码，配置里的每个
`${VAR}` 都从文件解析：

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  slife2:local 'Use the shell tool to run pwd and report the output.'
```

或者以 headless 方式启动并保持运行（同样会询问密码）：

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore slife2:local
```

key 配置在容器的 credstore 中，而不是命令行上。挂载的、值为字面量的 `slife2.yaml`
是另一种受支持的来源。如果对话和日志要在容器之外保留，挂载 `/opt/slife2/agents`
和 `/home/slife2/.slife2`。凭据配置、一次性 prompt 和容器相关说明见
[docker/README.md](docker/README.md)。

## 快速开始

一键安装之后，依赖已经同步，`credstore` 也已安装。设置 key 并启动窗口：

```bash
credstore set DEEPSEEK_API_KEY
slife2
```

从源码检出运行时改用 `uv run slife2`。手动安装要多一步：

```bash
uv sync
uv run credstore set DEEPSEEK_API_KEY
uv run slife2
```

`slife2` 会启动配置所需的 MCP server，复用已经在运行的，打开终端界面，并在最后一个
实例退出时停止这些 server。

```bash
slife2 status    # 本配置的 server 各自在做什么
slife2 down      # 停止它们
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

`down` 停止共享 server 和 broker，但不关闭已经打开的窗口。保持打开的窗口在 server
重新启动后的下一次输入时重连，对话会被恢复。它的 agent 名字在窗口退出前一直保持
占用，因此同名的新实例会被拒绝。另一个活着的客户端正在使用 server 时 `down` 也会
拒绝；`--force` 强制停止并打断该客户端正在进行的轮次。

选项：

| 选项 | 含义 |
|---|---|
| `--data-dir DIR` | 使用 `DIR` 作为数据目录。覆盖 `SLIFE2_DATA_DIR`。 |
| `--agent NAME` | 以 `NAME` 运行，或用 `probe` 询问 `NAME`。默认 `slife2`。每个名字每台机器只能有一个活实例。 |
| `--model PROVIDER/MODEL` | 以该模型启动。只写 provider 时选它的第一个模型。默认使用配置的 `default`。仅 `run`。 |
| `--url URL` | 把窗口连接到 `URL` 处的 agent server。 |
| `--a2a-broker HOST:PORT` | 本次运行中 peer-agent 网络使用该 broker。只覆盖 `peer_agent.broker`，不覆盖 subagent 网络的 broker。不能启用没有 `peer_agent:` 节的网络；A2A server 已在别的 broker 上运行时会被拒绝。 |
| `--keep-servers` | 本实例退出后保留共享 server。 |
| `--headless` | 运行同一个窗口、server、连接和轮次，但不显示任何内容。agent 仍在 peer 网络上可达。Ctrl+C 退出。 |
| `--lang {en,zh}` | 设置窗口语言。不带该参数时从操作系统检测。 |
| `--json` | `status` 以 JSON 输出（`ok`、`servers`、`broker`；每个 server 行带日志路径）。 |
| `--force` | 即使另一个客户端正在使用，也停止共享 server。仅 `down`。 |
| `--prompt TEXT` | 要问运行中 agent 的任务。仅 `probe`。 |
| `--timeout SECONDS` | `probe` 等待完整回答的秒数。默认 3600。 |
| `--channel {a2a,tui}` | `probe` 走哪条路：A2A peer 网络（默认），或 agent task 的投递消息。 |
| `--lines N`、`-n N` | `logs` 显示多少行；`0` 读整个日志。默认 50。 |
| `--all` | 配合 `logs`，在每个 server 自己的标题下打印它的日志。 |
| `--version` | 打印版本并退出。 |

只有整套栈完全可用时，`status` 和 `down` 才返回 0：任一配置的 server 或 broker
未运行时 `status` 返回 2；server 或 broker 未停止、或因为另一个客户端活着而被拒绝时
`down` 返回 2。server 无法启动时 `run` 返回 2。

`--headless` 是同一个窗口去掉显示，不是另一种 agent；system prompt 会被告知没有人在
看。

`slife2 probe --agent NAME --prompt '…'` 向运行中的 agent（headless 或打开的窗口）
问一个任务。`--channel a2a`（默认）模拟一个 peer：在 peer 网络上发一个
`task_request`，目标运行该轮，模型的 `task_response` 就是回答；它需要
`peer_agent:` 节。`--channel tui` 通过 agent task 询问，答案是 task 投递的
`agent-task:<name>` 消息——不是窗口的 assistant 文本，那是这一轮对所有并入输入的
回答。两种方式在得到完整回答时返回 0，用法错误返回 2，没有完整回答返回 3。容器的
一次性模式走默认的 A2A 路。

`--lang` 只影响窗口：标签、harness 行、面板、模型选择器、审批行和状态栏。system
prompt、工具 schema 和日志行保持英文。外部文本（工具结果、peer 消息）按原文透传。

### plugin 可执行文件

每个 plugin 也可以直接启动，用于进程管理器或单独调试某个 server。

| 可执行文件 | server | 默认端口 |
|---|---|---|
| `slife2-agent` | agent 主循环与对话 | 24000 |
| `slife2-llm-openai` | OpenAI 兼容 chat completions | 24001 |
| `slife2-llm-anthropic` | Anthropic Messages | 24002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 24003 |
| `slife2-llm-embeddings` | Embeddings | 24004 |
| `slife2-tui` | 终端收件箱，每个 agent 一个 session | 24005 |
| `slife2-context` | 轮次日志与上下文决策 | 24010 |
| `slife2-toolhub` | 工具目录与模型工具清单 | 24020 |
| `slife2-wechat` | 微信登录与收发 | 24030 |
| `slife2-skills` | playbook 与 `skill_use` | 24031 |
| `slife2-cli` | `cli:` 节 | 24032 |
| `slife2-jobs` | 代码定义的工具（`job__<name>`） | 24042 |
| `slife2-mcp-tools` | `mcp:` 节 | 24033 |
| `slife2-restapi-tools` | `rest-api:` 节 | 24034 |
| `slife2-cabinet` | 笔记、日记、文件、报告、dump | 24035 |
| `slife2-media` | 图片、视频、语音、转写 | 24036 |
| `slife2-agent-task` | 后台 agent task | 24037 |
| `slife2-file-tunnel` | 本地文件的公网 HTTPS URL | 24038 |
| `slife2-schedule` | 定时任务及其账本 | 24039 |
| `slife2-a2a` | 基于 MQTT 的 peer-agent 网络 | 24040 |

subagent 网络由同一模块在 24041 端口提供，以
`python -m slife2.plugins.a2a.local_server` 启动，没有单独的控制台脚本。

**WSL2 使用 `networkingMode=mirrored` 时，WSL 实例把端口整体偏移 100**（agent
24100、toolhub 24120）。本地 MQTT broker 不偏移：两套栈共享它。镜像模式让 Windows
和 WSL 的 loopback 合一，默认端口上的 WSL 栈会被 Windows 实例看到并被静默复用。
`wsl_port_offset` 改变偏移量：默认 100，`0` 关闭，在镜像模式之外被忽略。
## 使用终端界面

### 按键

| 按键 | 动作 |
|---|---|
| `Enter` | 发送消息。 |
| `Shift+Enter` | 插入换行。 |
| `Esc` | 打断正在运行的轮次。 |
| `Ctrl+C` | 有选中内容时复制；否则打断；再否则退出。 |
| `Ctrl+S` | 打开模型选择器。`↑`/`↓` 移动，`Enter` 选择，`Esc` 关闭。 |
| `Ctrl+O` | 打开 subagent 选择器；选中后打开只读视图。`Esc` 关闭。 |
| `Ctrl+Q` | 退出（Textual 的标准绑定；不在状态栏显示，因为带流控的终端可能先截获它）。 |
| `PageUp` / `PageDown` | 翻页。 |
| `Home` / `End` | 跳到对话开头或结尾。 |
| `Ctrl+A` / `Ctrl+E` | 跳到草稿开头或结尾。 |

鼠标滚轮滚动对话区。向上滚动时新 token 到达不会把视图拉回底部；发送消息后回到结尾。

### 发送消息

轮次运行期间发送的消息会排队；当前轮结束后再运行排队消息。状态栏显示队列深度。
`Esc` 打断当前轮，但保留队列。轮次运行期间到达的消息——窗口里输入的、其他 channel
移交的、headless 窗口的输入——会在下一个迭代边界并入该轮。

### 模型选择

`Ctrl+S` 列出 `providers:` 下的所有模型并标出当前使用的。选择会调用模型自己也能
调用的 `model_switch`。对话保留历史，在下一轮切到新模型；进行中的轮次在它启动时的
模型上跑完。

### 观看 subagent

`Ctrl+O` 列出本 agent 创建的 subagent，并打开其中一个的只读视图。视图先显示已存储
的对话，再显示正在进行的轮次——包括打开时已经在运行的那一轮。`Esc` 返回。视图是
只读的：观看不会让你成为该 subagent 的窗口，prompt 不显示，审批请求也不会交给你。
在被观看的对话中，唤醒它的消息显示为 `Agent(<name>)>`；来自兄弟 subagent 的消息
显示为 `Subagent(<name>)>`。

### 恢复对话

关闭再打开窗口、重启 server、或经过空闲清理，都不会开始新对话。对话按原始时间戳、
工具调用和结果恢复；推理折叠在它原来折叠的位置。

状态栏报告上一次模型调用留下的上下文大小及其占模型窗口的比例。每一轮之前，一条
注释说明上下文决策保留了什么、召回了什么。它还显示 `tools 26/358`：hub 已加载的
清单，以及目录的总量。skill、job、CLI 条目都计入总量，已下线 server 的工具也计入；
前半部分是下一次请求实际携带的。两个数字来自同一次询问。

### 图片

在句子里直接写图片路径：

```text
What is wrong with this layout? D:\shots\screenshot.png
```

模型会调用 `attach_image`。没有特殊语法。一次调用可以带本地路径、`http(s)` URL 或
`data:` URI，也可以同时带多个。无法读取的来源会在回答里点名，不影响其余部分。
URL 交给 provider 去抓取。

只有模型条目在 `input` 下声明了 `image` 时才能附图。实时对话携带像素；轮次日志保留
点名文件的句子、工具调用与回答，以及指向文件柜副本的 `[Image:…]` 引用。之后的轮次
用引用的 key 或图片 URL 调用 `attach_image`。provider 拒绝图片时会丢弃它并说明；
那一轮的行是 note-only。

## 配置

### 位置

配置文件是数据目录里的 `slife2.yaml`。从源码检出运行时数据目录就是检出目录，否则是
`~/.slife2`。用 `--data-dir DIR` 或 `SLIFE2_DATA_DIR` 覆盖。

文件是必需的，且 `providers:` 下至少要有一个 provider，`embeddings:` 下至少要有一个
embedding provider。文件缺失时启动停止，并用一行错误指出搜索过的目录；文件存在但无法
加载时，指出文件和错误。

安装器可能在已有配置旁写入 `slife2-default.yaml` 作为参考。slife2 只读
`slife2.yaml`。

`AGENTS.md` 与配置并列，同样必需。它是模型对自己工具的指南，包含在每个对话开头的
system prompt 中；修改后下一个对话即可读到。安装器按配置的规则放置它：你的副本有
差异时写 `AGENTS-default.md`。

`slife2.yaml` 可以安全地提交到版本库，它不应包含秘密；每个凭据都是运行时解析的
`${VAR}` 或 `keyring:` 引用。

### 配置节

| 节 | 用途 |
|---|---|
| `servers:` | 每个 plugin server 的地址和端口。缺省时使用默认值。 |
| `providers:` | chat provider 及其模型。 |
| `default:` | 新对话使用的模型，`provider/model`。 |
| `agent:` | 迭代上限和 system-prompt 模板。 |
| `agent-task:` | `max_running`，每个 agent 的后台任务数。 |
| `subagent:` | `max_running`，以及本地 subagent 网络的 broker 和 `org`。 |
| `embeddings:` | 轮次、工具和文件柜索引使用的 embedding provider。 |
| `context:` | 上下文决策的限额。 |
| `tool_load:` | 已加载工具上限，以及启动即加载、永不被逐出的工具。 |
| `tool_result:` | 单个工具结果的 token 预算。 |
| `mcp:` | 外部 MCP server（stdio 或 HTTP）。 |
| `rest-api:` | 暴露为工具的 OpenAPI 文档。 |
| `cli:` | 机器上已经安装的命令。 |
| `skills:` | `skills/` 下 playbook 的环境变量。 |
| `media:` | 图片、视频、语音和转写 provider。 |
| `file-tunnel:` | 本地文件的公网 URL provider。 |
| `peer_agent:` | 远端 peer-agent 网络。 |

没有 `wechat:`、`cabinet:`、`schedule:` 或 `tui:` 节。微信登录通过扫码创建并保存在
agent 目录下。文件柜、调度器和终端收件箱由各自的 server 及其持有的数据启用。

### 秘密

值可以是字面量字符串；`${VAR}`，依次从 shell 环境、
[credstore](https://pypi.org/project/credstore/)、可选的默认值（`${VAR:-default}`）
解析；或 `keyring:<service>/<key>`，直接对 OS keyring 解析。

**只有 credstore 管理的秘密会被输出保护。** slife2 从 credstore 解析出的凭据——一个
`keyring:` 引用，或 store 回答了的一个 `${VAR}`——在工具结果、工具清单和日志行中以
掩码形式显示（至少各留四个字符时显示前四后四，否则显示 `***`）。短于八个字符的值
不登记。环境变量或字面量是操作者自己的。把每个秘密都放进 credstore：它是 slife2
唯一会在各处遮罩的存储。

解析是惰性且宽松的。未解析的引用原样保留，因此缺失的 key 会在 API 调用处失败并点名
它。

### 示例

```yaml
agent:
  max_iterations: 0          # 0 或缺省表示不设上限
  # system_prompt: prompts/jack.j2

agent-task:
  max_running: 4

subagent:
  max_running: 32
  provider: mosquitto
  broker: {host: localhost, port: 1883}

context:
  ceiling: 0.8               # 占模型窗口的比例
  floor: 0.2
  recall_limit: 40
  min_similarity: 0.45
  timeout: 20.0              # 判别调用的秒数

tool_load:
  threshold: 100             # 超出后逐出最久未使用的已加载工具
  autoload: [tool_search, tool_list, func_tool_load, _func_tool_unload,
             _check_new_input, skill_use, check_async, cancel_async,
             turn_read]                                # 且永不被逐出

tool_result:
  tokens: 8000               # 每个结果的预算；最小 128

mcp:
  arxiv:
    url: https://arxiv.mcp.brunosan.de/mcp
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

skills:
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}
```

随附的 `slife2.yaml` 就地注解，是每个 key 的参考。

### 编辑配置

配置可以手工编辑，也可以由模型编辑。每个配置节或文件夹有各自的工具：

| 配置节或文件夹 | 工具 |
|---|---|
| `mcp:` | `mcp_set`、`mcp_list`、`mcp_remove`、`mcp_set_enabled` |
| `rest-api:` | `rest_api_set`、`rest_api_list`、`rest_api_remove`、`rest_api_set_enabled` |
| `cli:` | `cli_set`、`cli_list`、`cli_remove`、`cli_set_enabled` |
| `skills:` | `skill_draft`、`skill_set`、`skill_edit`、`skill_list`、`skill_remove`、`skill_set_enabled` |
| `jobs/`（文件夹） | `job_set`、`job_list`、`job_read`、`job_run`、`job_remove` |
| `providers:` 与 `default:` | `model_set`、`model_list`、`model_remove`、`model_switch` |

所有写入都经过同一个模块（`slife2.libs.host.configfile`），在跨进程锁下进行。写入
保留注释，结果由启动时同一个 loader 校验。loader 拒绝的条目会被还原，并告诉调用方
原因。

`set` 写整个条目：没写的字段就是被删掉。`set_enabled` 只改 `enabled`。写入前没有
审批关卡；特别是 `mcp_set` 带 `command:` 时记录的是 slife2 将要启动的程序。

## 能力

### 对话与记忆

对话不会无界地累积上下文。每一轮之前，`slife2-context` 进行一次模型调用，决定保留
哪些轮次、从轮次日志中召回哪些轮次；这一轮在两者合并后的结果上按时间顺序运行。如果
召回匹配到的内容超过容量，只返回放得下的部分。决策失败时保留手头的上下文；轮次存储
缺失则使该轮失败。

模型用 `turn_list` 浏览历史、用 `turn_search` 按一轮讲的内容搜索、用 `turn_read`
读完整的某一轮、用 `turn_summarize` 写摘要和标签。`turn_list` 按最新优先，每轮一行；
`since`/`until` 接受 ISO 日期或 `yesterday` 之类的说法，`limit`/`offset` 按
`total` 翻页。`turn_search` 使用同一个时间窗口，返回同样的一行外加 `similarity`
分数；它的 `query` 词把包含这些词的轮次排在前面，但不是过滤器。这些工具都不接受
agent 参数：模型只能读取自己的历史。

每一轮以 `[TURN-PROMPT:{…}]` 开头，携带 `now` 和 `token-usage`；它是提示文本，不会
存储。被重建进上下文的每一轮另外带有 `[TURN:{…}]` 脚注，写明它的 id、channel 和
跨度。大写 marker 是 harness 添加的注解；读起来像词的 marker（例如 `[Wechat:{…}]`）
是发送者写的，会随消息一起存储。

### 工具

`slife2-toolhub` 拥有模型的工具清单。`slife2-mcp-tools` 持有 `mcp:` 连接，
`slife2-restapi-tools` 持有 `rest-api:` 连接；hub 合并它们声明的来源、命名工具、
把关并把调用路由回来源。

hub 提供四个集合级工具：

- `tool_search` 按关键词和语义搜索整个目录。传 `unloaded_only` 可排除已经在清单中的
  工具；skill 和 CLI 条目没有加载状态，未加载的 job 也还没进清单，所以它们仍然包含
  在内。
- `tool_list` 分页列出整个目录——所有工具、skill、CLI 条目和 job——用于没有词可以搜索
  的时候。可按 `category` 和 `source` 过滤，回答里带 `total`、`limit` 和 `offset`。
- `func_tool_load` 将一个或多个工具放入模型看到的清单。
- `_func_tool_unload` 从清单中移除工具。harness 也会在轮次边界调用它来执行预算。

模型可以调用 `tool_search` 或 `tool_list` 返回的名字而不加载它，但加载会让它保留在
每次请求随附的清单中。`tool_load.autoload` 列出启动即加载的工具，按模型调用它们的名字——plugin 的
裸名字（`turn_read`）或外部 server 的 `{server}__{tool}`。其余工具按需发现和加载。

调用一个目录仍在启动中的工具会回答 "not in the catalogue yet"；几秒后重试，而不是
去找替代品。

已加载清单由 `tool_load.threshold` 限制（默认 100）。超出后，最久未被调用的工具被
卸载；`tool_load.autoload` 里的名字永不被逐出。裁剪发生在轮次边界，并以一次工具调用
及其结果写入对话。

工具命名：`mcp:` 条目的工具是 `{server}__{tool}`，例如
`arxiv__arxiv_search_papers`；plugin 自己的工具用裸名字。

工具结果是 JSON 对象，只有两类回答的文本本身就是内容：`_check_new_input` 原样交出
人的消息，`skill_use` 交出 playbook 文本。列表回答
`{"<noun>": [], "total": N, "limit": L, "offset": O}`；`total` 说明是否还有下一页。
拒绝或未命中会抛出 `ToolError`，harness 将其报告为带原因的工具失败。任何回答都不应
超过 `tool_result.tokens`；无法限界的输出会存成 dump（见[文件柜](#文件柜)）。

#### 工具调用选项

每个函数工具都接受三个元参数。它们被加入每个广播的 schema，并在分发前剥离。

| 参数 | 效果 |
|---|---|
| `_timeout` | 本次调用的秒数，覆盖工具默认值。`0` 或负数表示"用默认值"，绝不是"不限时"。工具自己声明了 `timeout` 参数时由它自己执行。两者都没有时，调用一直运行到结束。 |
| `_async` | 后台运行。模型收到 `task_id`；结果带 `[AgentToolResult:{…}]` marker 从 `async-tool:<tool>` channel 回来。`check_async` 报告状态；`cancel_async` 放弃它。结果预算仍然适用。agent task 的循环没有 `_async`。 |
| `_approve` | 运行前询问用户。终端画一行（`y` 批准，`n` 或 `Esc` 拒绝）并通过 `answer_approval` 回答。channel 背后没有人时直接运行。能回答的调用方用 `watched=true` 声明。 |

#### skill 与命令

playbook 位于 `<data>/skills/<name>/SKILL.md`。`slife2-skills` 提供 `skill_use`，
返回 playbook 文本并在前面加上它的目录。安装 skill 就是把目录放进 `skills/`；工具
每次调用都读磁盘。

skill 可以在头部声明环境需求，值来自 `skills:` 节，走与其他秘密相同的解析链。
`skill_use` 会在模型执行依赖它的指令之前报告缺失的 key。`skill_set` 在传入 `env:`
时写入该条目——重装时省略它则保留原值——`skill_remove` 连同文件夹一起删除条目。

`skill_draft` 把工作流转成 `SKILL.md` 而不写任何东西：它的回答就是 `skill_set` 或
`skill_edit` 接受的 `files` 参数。新 skill 用 `skill_set`；修改已安装的用
`skill_edit`，它只写点名的文件，保留其余部分。

`cli:` 条目描述机器上已经安装的程序。每个条目都是目录里的一行（`cli:yt-dlp`），
`tool_search` 可以按功能找到它。这一行不是可调用工具，也没有加载状态；目前没有工具
执行 `cli:` 条目，`func_tool_load` 会说明这一点。程序缺失时显示条目的 `install:` 行。

`enabled: false` 保留配置但从不连接。值必须是真正的 YAML 布尔值：YAML 1.2 下 `no`、
`off` 和 `'false'` 是字符串，会被拒绝。被禁用条目的工具仍在目录里，标记为
`disabled`。

#### job

`job_set` 写入一个 **job**——`<data>/jobs/` 里的一个 Python 文件，一个与文件同名的
模块级函数——该函数成为工具 `job__<name>`。docstring 是描述，带类型注解的签名是
schema；没有 manifest。同一次调用会替换 job：新文件先加载，加载失败则恢复上一版。
`job_remove` 删除文件与工具；`job_list` / `job_read` / `job_run` 列出、读取和测试。
坏掉的文件仍以 `error` 行可见。

job 通过 `slife2.job` 的两个通用句柄组合系统的其余部分：

- `call_tool(name, arguments)`——hub 的目录，按模型调用的名字：plugin 的工具、外部
  `{server}__{tool}`，或另一个 job。
- `call(server, tool, arguments)`——按 `servers:` 键调用 slife2 的某个 server：一次
  agent 轮次（`send_message`）、一次原始模型调用（`stream_chat`），或任何 plugin
  自己的工具。

`servers()` 读取配置的那一张表，因此后续新增的 plugin 或工具无需改动 job 运行时即可
使用。代码在 jobs plugin 中以调用方身份运行；它不在沙箱中，顶层工具调用是人可以要求
审批的地方。`job-coding` skill 是编写指南：它告诉模型对文件运行 `ruff` 和 `pyright`
并修好发现的问题。

### 媒体

`media:` 节提供四项能力：

- `generate_image`——按提示词画图。
- `generate_video`——按提示词或图片生成视频。
- `text_to_speech`——把文本写成语音文件。
- `transcribe_audio`——转写音频文件，按 `offset` 和 `limit` 分页。

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

支持的 `api:` 值是 `dashscope-aigc`（图片、视频、语音、转写）和 `openai-images`
（仅图片生成）。选择不支持所请求 kind 的模型会被拒绝。

生成的文件写在 `<data>/agents/<agent>/media/` 下。`folder` 参数在该根目录内创建
子目录；越界的路径会被拒绝。超时仍未完成的长时间视频调用会带着 provider 的 job id
被拒绝，而不是返回结果。

### 文件隧道

`slife2-file-tunnel` 把本地文件发布到公网 HTTPS URL：

```text
expose_local_image(path)      -> https://<tunnel>/share/<token>
unexpose_local_image(token)   -> 该 URL 失效
```

文件从原位置提供。文件变化、调用 `unexpose_local_image`、或进程退出时链接失效。共享
比创建它的轮次活得更久。私钥、`.env` 文件和保存的密码会按名字被拒绝。

支持三个 provider。同时只有一个生效；其余可以保留配置但不参与。

| provider | 要求 | URL 行为 |
|---|---|---|
| `ngrok` | `NGROK_AUTHTOKEN` | 每个账号一个稳定的开发域名；免费版对浏览器显示插页。 |
| `localhost.run` | `ssh` 客户端 | 随机的 `*.lhr.life` 主机名，每隔几小时轮换；TLS 在 provider 处终止。 |
| `cloudflare` | `cloudflared` 二进制 | 进程生命周期内稳定的 `*.trycloudflare.com` 主机名。 |

内置默认是 `ngrok`。随附的 `slife2.yaml` 选择 `cloudflare`，它不需要账号；没有安装
`cloudflared` 时把 `active:` 改成 `localhost.run`。未启动、或发布了无法访问的 URL 的
隧道会被拒绝，而不是交出去。`tunnel_status` 报告状态、URL、可达性、边缘地址和所用
二进制；它是内部诊断工具，不暴露给模型。

### 文件柜

文件柜是每个 agent 的存储：笔记、日记、保存的文件、报告、dump 和图片。

| 种类 | key | 第二次写入 |
|---|---|---|
| `note` | 用户选定的主题 | 替换该笔记。 |
| `diary` | 日期 | 追加一节。 |
| `file` | 柜内路径 | 把文件复制进来。 |
| `report` | 标题 | 追加一节。 |
| `dump` | 产生输出的工具调用 | 由 harness 写入。 |
| `image` | 内容摘要 | 由 harness 写入；字节在 `pictures/`。 |

笔记、文件和图片是文档：第二次写入就是它们的内容，所以只有笔记和文件有删除工具。
日记和报告是日志：同一日期或标题下的两条是两件事实。

dump 是对对话而言过大的工具输出。输出完整存储，以工具调用 id 为键，对话携带的是在
`tool_result.tokens` 内从输出自己的行中选出的片段；不做任何改写。模型用 `dump_read`
读回任意部分：`pattern` 按字面匹配行，`context` 带上邻近行，`offset` 从某行读起
（负数从末尾数），`limit` 少读几行。除非 `kind` 是 `dump`，搜索看不到 dump。

图片完整存放在 `pictures/`，以内容摘要为键，轮次行携带 `[Image:…]` 引用而不是像素。
`image_source` 解析引用，`attach_image` 是模型再次看到图片的方式；`http(s)` 图片由
provider 抓取，不在这里。不指定 kind 的搜索能看到图片。

每个文本文档——笔记、日记、报告或 dump——写两份：文件柜索引中的一行，以及
`agents/<agent>/cabinet/` 下的 Markdown 文件（`notes/`、`diary/`、`reports/`、
`dumps/`），可以脱离 slife2 打开、编辑或删除。保存文件的字节在文件柜下，不被索引，
也没有 Markdown 镜像。`file_read` 返回文件文本，遇到 PDF、图片或压缩包时说明它是什么
并拒绝。

`cabinet_search` 像 `tool_search` 一样接受 `keywords` 和 `sentences`。
`cabinet_summarize` 为文档写描述；为文件或图片写描述会重新嵌入它。`file_save` 只复制
机器上已有的文件，不抓取 URL。

### 微信

`slife2-wechat` 通过 iLink 的托管桥接为每个 agent 连接一个微信账号。登录靠扫码而不是
配置。`wechat_login` 返回二维码；扫码后 token 保存到 `agents/<agent>/wechat.yaml`，
下次启动读取。登录约持续一天；`wechat_check_status` 报告剩余时间。

一个微信号只能有一个活登录。同一账号在第二个 agent 或第二台机器上扫码，会静默让第一个
登录失效，后者随后收到桥接的 `session timeout`。第一个 agent 的窗口显示
`[✗ Wechat offline]`，`wechat_check_status` 给出原因。再扫一次即可拿回账号。

| 工具 | 用途 |
|---|---|
| `wechat_login` | 获取二维码并开始等待扫码。 |
| `wechat_check_status` | 报告连接状态、剩余时间，以及要回复的对端。 |
| `wechat_send_message` | 向对端发送文本消息。 |
| `wechat_logout` | 忘记本 agent 的登录。 |

连接的到达与离开画成 harness 行（`[⚡ Wechat online]`、`[✗ Wechat offline]`），
不是轮次。

来自微信的消息成为该 agent 对话中的一轮，画在显示该对话的窗口里。它以 channel
`wechat` 和点名发送者的 `[Wechat:{…}]` 前缀记录。回复是模型的职责：没有任何自动派发。
模型用对端和 marker 里的 `context_token` 调用 `wechat_send_message`。轮次运行期间，
对方看到"正在输入"。

每个 agent 单独登录，有自己的文件和轮询循环。subagent 没有，插件是一个共享进程。

### Agent 任务

agent task 是同一个 agent 的另一条循环，用名字和描述启动，稍后以消息形式在同一对话中
回答。

| 工具 | 用途 |
|---|---|
| `agent_task_start` | 启动任务并立刻返回 id。 |
| `agent_task_status` | 报告任务状态。从不返回结果。 |
| `agent_task_list` | 列出本 agent 的任务，运行中的在前。 |
| `agent_task_cancel` | 停止任务，并报告调用后的状态。 |

任务以 agent 身份运行，使用相同的 system prompt、模型、工具和上下文决策。它没有启动
它的那一轮，因此描述必须复述相关上下文。回答带 `[AgentTaskResult:{…}]` marker 从
`agent-task:<name>` channel 到达，显示为 `AsyncTask(<name>)>`。没有窗口显示该对话时，
回答被报告为 `undelivered`，日志记录这一点。

```yaml
agent-task:
  max_running: 4
```

`max_running` 是该节唯一的键；其余来自 `agent:`。任务可以再启动任务，因此
`max_running` 限制的是并发，不是一段时间内的总工作量。

### 定时任务

定时任务是模型写下的名字、描述和 cron 表达式。`slife2-schedule` 在 slife2 运行期间
触发它。任务到期时，agent 在显示该对话的窗口里收到一轮；这一轮调用
`schedule_run_now` 并带上它携带的到期时间。没有到达任何窗口的触发以 `failed` 结束
该次运行并记录原因。

| 工具 | 用途 |
|---|---|
| `schedule_set` | 写入或替换任务：名字、描述、cron、时区。 |
| `schedule_remove` | 删除任务及其全部运行记录。 |
| `schedule_list` | 列出任务及下次到期时间。 |
| `schedule_runs` | 列出账本：每次触发一行。 |
| `schedule_run_now` | 立刻执行本次运行；带 `due_at` 则补跑。 |
| `schedule_run_skip` | 带原因关闭一次不会发生的运行。 |

一次运行就是一次触发，以它到期的那一刻命名。状态从 `pending` 变为 `ran`、`failed`、
`missed` 或 `skipped`。行在移交工作之前写入，因此同一次触发不会在下一轮询重复。

任务只在 slife2 运行时触发。启动时，扫描会关闭被停止的进程遗留的运行，轮询为每个任务
记录最新的错过触发。`schedule_runs(status="missed")` 列出它们；错过的运行可以用
`schedule_run_now(name, due_at)` 补跑，或用 `schedule_run_skip` 关闭。

账本是 `<data>/agents/<agent>/schedule.db`。没有配置节。任务的 `timezone` 是 IANA
名字；省略时使用机器当前时钟，不跟随夏令时变化。

### Peer agent

agent 的 peer 是其他 agent，通过发布的 A2A-over-MQTT profile（0.1，基于 A2A
v1.0.0）通信。人在 `slife2.yaml` 里写 `peer_agent:`；模型看到 `peer_agent_*` 工具，
协议名不出现在它读到的任何内容里。

peer 网络和 subagent 网络对该 profile **符合 Core**。wire 原语来自
`a2a-over-mqtt` SDK；补上 profile 的「中断任务续传、终态任务拒收新消息、`GetTask`」
的 responder 生命周期是本项目的。可选的 Extended profile（共享订阅分发、信任元数据、
broker 管理状态、原生二进制 artifact、端到端载荷保护、推送通知）未实现。

| 工具 | 用途 |
|---|---|
| `peer_agent_send_message` | 发送任务（`task_request`）或普通消息；结果稍后到达。 |
| `peer_agent_task_response` | 结算 peer 发来的任务：completed、failed、rejected，或保持打开的中断状态。 |
| `peer_agent_continue_task` | 继续一个你发出、被 peer 暂停等待输入的任务。 |
| `peer_agent_cancel_task` | 撤回本 agent 发出的任务。 |
| `peer_agent_list` | 先列本 agent，再按名字列每个 peer。 |
| `peer_agent_task_list` | 列出未结束的任务：等待回答的，以及仍欠回答的。 |
| `peer_agent_broadcast` | 向每个 peer 发送即发即忘的事件。 |
| `peer_agent_group_set` | 创建群组并拉入你点名的成员；你成为 owner。 |
| `peer_agent_group_list` | 列出本对话知道的群组，含 owner 和你的角色。 |
| `peer_agent_group_member_list` | 列出群组的 owner 和成员。 |
| `peer_agent_group_remove` | 解散你拥有的群组。 |
| `peer_agent_group_leave` | 自行退群；作为 owner 时，所有权交给剩余成员中的第一位。 |
| `peer_agent_group_send_message` | 向其他所有成员发送即发即忘的消息。`@name` 和 `@all` 像聊天群一样点名。 |

`task_request` 立即返回 id；结果稍后带
`[PeerAgent:{"from": …, "type": "task_response", "task_id": …}]` marker 到达。id 和
`status: "unverified"` 描述的是接受与否，不是送达；无人回答的发送会在重试阶梯耗尽时
以 unreachable 通知回来。没有可轮询的东西。

peer 可能暂停任务并索取输入；那一行类型是 `task_input`，并写明问题。任务仍然打开：
用 `peer_agent_continue_task`（同一个任务、同一个对话）回答，或用
`peer_agent_cancel_task` 撤回。不要另发一个新任务——那是第二件事。peer 发来的任务用
`peer_agent_task_response` 结算；`completed`、`failed`、`rejected` 结束它，
`input_required`/`auth_required` 则暂停它，等 peer 续传。

每个 agent 一个 mesh：同一名字的第二个活实例在加入前被拒绝。subagent 不在这个网络上。

在线状态画成 harness 行（`[⚡ jill online]`），不是轮次。名册是 `peer_agent_list`。
窗口持有 `join` 调用期间 agent 在线；`--headless` 在没有窗口的情况下持有同一个调用。
任务状态保存在内存中，因此重启从零开始；被暂停任务的问题和 context 也是进程本地的，
仍在等待的 peer 会在自己的 `peer_agent_task_list` 里看到该任务。

群组是有 owner 的具名名册；没有邀请或接受流程。群消息是向其他每个成员的即发即忘事件
——绝不回到发送者——带 `[PeerAgentGroup:{"from": …, "group": …, "owner": …}]` marker
从 `peer_agent_group:<group>:<sender>` channel 到达。查看成员是单独的一次读取。**群聊
是随意的**：没有成员必须回答，沉默不是 `pending` 或 `undelivered`，`sent` 只确认已
发送，不表示有人读过；需要回答时用直接发送工具。每个成员把自己的认知保存在
`agents/<agent>/groups.yaml`，因此群组能跨重启存活。发送时离线的成员会错过消息。
只有 owner 能改名单或解散群组；成员的退出方式是 `peer_agent_group_leave`。

配置的 broker 是本地的且未运行时，会自动启动；只有 slife2 启动的 broker 才会随栈
停止。无法服务的 broker 会让启动失败，并给出名字和日志。`provider: mosquitto` 是
唯一支持的值。

```yaml
peer_agent:
  provider: mosquitto
  broker:
    host: localhost
    port: 1883
  org: default
  unit: default
```

省略 `broker:` 时，peer 网络使用 subagent 网络的 broker 地址。省略 `peer_agent:` 则
机器完全不在该网络上。重启会丢弃进行中的轮次和被暂停的任务；仍在等待的 peer 会在
自己的 `peer_agent_task_list` 里看到该任务，可以取消或重发，重发作为新请求到达。

### Subagent

subagent 是属于另一个 agent 的 agent。它有创建者的工具集、自己的角色化 system
prompt 和自己的对话，但没有进程、没有窗口、不占用 agent 名字，也没有外部身份。它只在
本地 subagent 网络上可达，在有消息发给它时运行。`subagent_set` 和 `subagent_remove`
会拒绝 subagent 调用者。

subagent 用 `subagent_set` 创建，用 `subagent_remove` 删除。创建后，它在本地网络上以
与 peer agent 相同的操作、以 `subagent_*` 名字寻址：`subagent_send_message`、
`subagent_task_response`、`subagent_continue_task`、`subagent_cancel_task`、
`subagent_list`、`subagent_task_list`、`subagent_broadcast`，以及六个群组工具。成员身份就是目录集合，点名的名字不是成员时，
发送在发布前被拒绝。这个网络上的群消息带 `[SubagentGroup:…]` 和
`subagent_group:<group>:<sender>` channel，无论发送者是创建者还是兄弟，每个成员
维护自己的 `groups.yaml`。

| 键 | 含义 |
|---|---|
| `name` | subagent 的名字和目录。 |
| `model` | `provider/model`；为空则用创建者的模型。 |
| `role` | 包含在 subagent prompt 里的一个词。 |
| `fork` | 复制创建者当前的对话作为起点。fork 的轮次不存储，只活当前会话。 |

非 fork 的 subagent 位于 `agents/<maker>/subagents/<name>/`，有自己的 `turns.db`、
`subagent.yaml`、文件柜和媒体目录。fork 只有 `subagent.yaml` 记录：对话以创建者对话
的副本保存在内存中，读取创建者的轮次日志作为上下文，共享创建者的文件柜和媒体。
fork 是会话级的；下一次 server 启动时会删除所有 fork 记录。**fork 不能拉进群组**：
`subagent_group_set` 会拒绝它，它的群消息会被丢弃。调度留在 agent 一侧。subagent
不能再创建或删除 subagent。

删除 subagent 时，先让本地网络释放它的位置，再停止它的循环，最后删除目录。网络通知
无法完成时，subagent 保持不变。正在观看它的窗口会被告知对话已不存在。

窗口通过 agent server 的两个只读管理工具访问 subagent：`subagents` 列出创建者的目录，
`watch` 流式读取一个对话的轮次，直到调用被取消。

本地网络配置在 `subagent:` 下。它需要一个 `broker`；没有 broker 的 `peer_agent:` 节
会使用同一地址。`unit` 永远是 agent 的名字，写入它会被拒绝。`org` 可以设为群组
mesh。

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
  slife2.yaml                  配置；必需
  AGENTS.md                    模型对自己工具的指南；必需
  skills/                      <name>/SKILL.md playbook
  jobs/                        <name>.py 代码定义的工具
  agents/
    tools.db                   本 home 共享的工具目录
    <agent>/
      turns.db                 对话及其实时上下文
      schedule.db              定时任务及其运行账本
      cabinet/                 notes/、diary/、reports/、保存的文件、.index.db
      media/                   生成的图片、视频和音频
      wechat.yaml              本 agent 的微信登录
      subagents/
        <name>/                非 fork subagent 的 turns.db、subagent.yaml、
                               cabinet/ 和 media/；fork 只有记录
```

工具目录放在 agent 目录旁边，因为它属于整个 home，而不是某个 agent。

### 运行时目录

机器的共享 server 记账保存在 `~/.slife2/runtime`：

```text
<runtime>/
  servers/        每个运行中 server 的一条记录
  logs/           每个 server 的输出
  locks/          按端点的启动锁
  agents/         agent 名字占用
  clients/        哪些客户端在使用共享 server
  homes/          这台机器用过的数据目录
```

`SLIFE2_RUNTIME_DIR` 覆盖位置。runtime 目录是可重建的：删除它之后，下次启动会重建
所需内容。

每个 server 默认以 INFO 记录。`SLIFE2_LOG_LEVEL=DEBUG` 提高每个用它启动的 server 的
级别；每次调用的耗时、工具调用和连接细节都在那里，不是级别的值退回 INFO 并写一行
说明。超过上限的日志在下次启动时轮转为 `<log>.1`。外部 `mcp:` 条目的 stdio 子进程
在 `logs/` 下写自己的文件（`mcp-<name>-<digest>.log`）。

### 多个数据目录

一个数据目录可以包含多个 agent。它们共享它的 `slife2.yaml`、`skills/` 和
`agents/tools.db`，因此运行在同一套工具上。一个 agent 加载的工具对另一个也已加载。

agent 身份独立于目录。`--agent jack --data-dir D` 命名 agent 并指出文件位置。名字在
整台机器上只能占用一次，因此另一个数据目录里的第二个活 `jack` 会被拒绝。名字以前用过
哪个目录不会被记住。

server 跨数据目录共享。为一个 home 启动的 server 服务机器上的每个 home，每次调用按
它来时的 home 解析。外部 `mcp:` 条目按名字只有一个 server，无论多少 home 配置了它；
两个 home 给同名条目不同命令时，日志报告分歧并使用正在运行的 server。

## 开发

从源码检出：

```bash
uv run pytest                  # fast lane：单元与集成，并行
uv run pytest -m unit          # 只跑单元测试
uv run pytest -m integration   # 绑定 socket 或启动进程的测试
uv run pytest -m e2e           # 整套栈以进程运行
uv run ruff check . && ruff format --check .
uv run pyright
```

Windows 上 `uv run pytest` 不能在活栈旁边运行：`uv` 每次运行都会重写
`.venv\Scripts\` 的入口，而运行中的实例占用 `slife2.exe`（`os error 32`）。改用环境
自己的解释器：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试 lane、CI 矩阵、项目结构和 plugin 注册表见 [SDD.md](SDD.md) §13 与附录 B。

## 已知限制

- `cli:` 条目会被编目和发现，但目前没有工具执行它们。
- playbook 脚本不由 slife2 执行；`skill_use` 返回 playbook 文本，命令由模型自己运行。
- job 是代码：它在 `slife2-jobs` 中以调用方身份运行，不在沙箱中。顶层工具调用是审批
  点。
- 文件柜的 `file_save` 不抓取 URL，文件柜也没有 `grep` 搜索腿。
- 配置编辑之前没有审批关卡，包括带 `command:` 的 `mcp_set`——它记录的是 slife2 将
  启动的程序。
- agent task 可以再启动 agent task；`agent-task.max_running` 限制并发，不限制一段
  时间内的总工作量。

## 许可证

MIT。见 [LICENSE](LICENSE)。
