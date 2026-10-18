# slife2

[English](README.md)

**slife2 是一个终端 AI agent。** 它与语言模型进行对话，通过模型上下文协议（MCP）
向模型提供工具，并通过本地 plugin 进程连接外部世界。终端窗口只是一个客户端：
agent 主循环、工具 hub、模型后端、记忆以及每一项能力，都是 slife2 启动并共享的
MCP server。

本文件是用户手册。系统设计见 [sdd.md](sdd.md)，设计决策与实测记录见
[DESIGN.md](DESIGN.md)。

**目录**

- [概述](#概述)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令行接口](#命令行接口)
- [使用终端界面](#使用终端界面)
- [配置](#配置)
- [能力](#能力)
- [数据位置](#数据位置)
- [架构](#架构)
- [开发](#开发)
- [已知限制](#已知限制)
- [许可证](#许可证)

## 概述

slife2 围绕四条规则构建。

1. **plugin 是 slife2 自己的一个 MCP server。** 它通过 Streamable HTTP 使用
   MCP，并实现一份很小的附加契约：状态按 client id 而不是按 session 保存；
   每个面向模型的工具都声明自己的受众；客户端可以校验 server 的身份；
   每个 plugin 在中央注册表中占一行。plugin 是必需的：如果某个 plugin
   无法连接，这一轮就会失败。`mcp:` 下的外部 MCP server 是可选的；
   无法连接时，模型只是少一些工具。

2. **凭证只留在使用它的进程里。** provider 的 key 住在调用该 provider 的模型
   server 中。工具 server 的 key 住在拥有该连接的 plugin 中（`mcp:` 归
   `slife2-mcp-tools`，`rest-api:` 归 `slife2-restapi-tools`）。agent 主循环
   不 import 任何 provider SDK，也不持有任何 key。

3. **server 在整台机器上共享。** launcher 启动配置所需的 server，并复用已经
   在运行的那些。server 是守护进程，所以关闭一个窗口不会打断另一个窗口。
   最后一个客户端退出时，会停掉正在运行的 server；使用 `--keep-servers`
   可以保留它们。

4. **`slife2.yaml` 是“存在什么”的唯一描述。** provider、server、工具来源、
   embedding、媒体后端、隧道和网络设置都从该文件读取，没有第二份配置存储。

### 术语

| 术语 | 含义 |
|---|---|
| **plugin** | slife2 自己的一个 MCP server，由 launcher 启动并共享，实现 plugin 契约。外部 MCP server 不是 plugin。 |
| **agent** | 由 `--agent NAME` 命名的身份。一个名字在一台机器上只能有一个活着的持有者。 |
| **client id** | `(agent, subagent)`：一次调用所代表的身份。每个 server 都按它保存状态。 |
| **conversation** | 一个 client id 的历史。 |
| **turn** | 一次交换：输入、模型的回答以及它发起的工具调用，作为一行存储。 |
| **channel** | 消息到达的位置：`tui`、`wechat`、`peer_agent:<peer>`、`subagent:<name>`、`agent-task:<name>`、`async-tool:<name>` 或 `schedule:<name>`。 |
| **marker** | channel 在投递消息前添加的机器可读前缀，例如 `[Wechat:{…}]`。marker 随消息存储，只在显示时移除。 |
| **family** | 拥有一个配置节并声明该节所描述行的 plugin。 |
| **source** | plugin 持有的一个条目，例如 `mcp:` server、playbook 或命令。 |
| **catalogue** | 模型可以发现的所有工具、playbook 和命令的机器可读清单。 |
| **dump** | 因为对对话而言过大而完整保存在文件柜中的工具输出。 |
| **home** | 一个数据目录：配置、skills、agent 及其文件。 |
| **runtime 目录** | 共享 server 在整台机器范围内的记录、锁和日志。 |

## 环境要求

- **Python 3.13 或更高版本**
- **[uv](https://docs.astral.sh/uv/)**，用于依赖管理和运行

slife2 在 Windows、macOS 和 Linux 上测试。

安装脚本还会安装当前 `slife2.yaml` 所需的所有外部程序，升级已有版本，并验证
结果。手动安装使用 Python 3.13 和 uv。

### SQLite 扩展支持

向量索引是一个 SQLite 扩展（`sqlite-vec`），因此 Python 解释器必须能够加载
SQLite 扩展。

Windows 和 Linux 的构建通常支持该功能。在 macOS 上，uv 自带的 CPython 和
Apple 的 `/usr/bin/python3` 都不能加载扩展。请使用 Homebrew 的 Python：

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

如果无法加载扩展，数据库就无法打开，任何 plugin 都无法启动，第一条命令会以
如下错误失败：

```text
this Python's sqlite3 cannot load extensions
```

### 外部程序（配置中出现的程序均为必需）

当前配置中点名的每个程序都是必需的。安装器以普通用户身份提供这些程序；当某个
程序不可用时，安装会停止，并指出要求它的配置条目。

| 程序 | 由什么配置点名 | 安装器如何提供 |
|---|---|---|
| `mosquitto` | `peer_agent:` / `subagent:` broker | `127.0.0.1:1883` 上已有 broker 时直接使用。Debian/Ubuntu 的包解到 `~/.local`；macOS 用 Homebrew；Windows 使用已有 broker，或打印手动安装命令。 |
| `cloudflared` | `file-tunnel.providers.cloudflare` | 官方二进制放到 `~/.local/bin`（Windows 为 `%LOCALAPPDATA%\slife2\tools`）；macOS 用 Homebrew |
| `ssh` | `file-tunnel.providers.localhost.run` | 使用系统自带的 OpenSSH 客户端（Windows 10+、macOS 和 Linux 均自带） |
| `npx` | `command: npx` 的 `mcp:` 条目 | 官方 Node.js LTS 压缩包解到 `~/.local/lib/nodejs`（Windows 为 `%LOCALAPPDATA%\slife2\tools\node`）；它要拉的包在首次使用时下载 |
| `uvx` | `command: uvx` 的 `mcp:` 条目和 `rest-api:` 条目 | uv |
| `yt-dlp` | `cli:yt-dlp` | `uv tool install yt-dlp` |
| `browser-harness` | `cli:browser-harness` | `uv tool install --python 3.12 browser-harness` |

skill 也可以在头部通过 `requires.bins` 声明所需的程序，这些程序同样属于必需项。
例如随附的 `baidu-search` 声明了 `python3`。安装器只负责让 `npx` 和 `uvx`
启动器可用；它们首次运行时才下载各自的第三方包。

## 安装

### 一键安装

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
```

Windows 10/11（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1 | iex"
```

这条命令会下载最新的 `main`，把 slife2 安装到独立环境，并安装当前配置所需的
全部外部程序。数据和配置保存在 `~/.slife2`，二进制和库位于 `~/.local`（Windows
为 `%LOCALAPPDATA%\slife2`）。再次运行同一条命令即可升级。

从源码开发时，在该目录运行：

```bash
./scripts/install.sh
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`scripts\install.cmd` 也可以启动安装。源码目录使用自己的 `slife2.yaml` 和
`.venv`。

| 选项 | 含义 |
|---|---|
| `--dry-run` | 报告计划执行的操作。 |
| `--help` | 显示用法和选项。 |

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash -s -- uninstall
```

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1))) uninstall"
```

`uninstall` 会移除安装器添加的组件，已有组件保持原样。

### 手动安装

从源码手动安装：

```bash
git clone https://github.com/juzcn/slife2.git
cd slife2
uv sync
```

保存你将使用的 provider 的 API key。可以通过 `credstore` 存入操作系统
keyring，也可以导出为环境变量：

```bash
uv run credstore set DEEPSEEK_API_KEY
# 或者
export DEEPSEEK_API_KEY=...
```

`slife2.yaml` 是可选的：配置文件尚未创建时，slife2 使用内置默认值，其中包括
一个 OpenAI 兼容 provider。

## 快速开始

使用一键安装后，依赖已经同步，`credstore` 也已安装。设置 key 并启动窗口：

```bash
credstore set DEEPSEEK_API_KEY
slife2
```

从源码目录运行时，用 `uv run slife2` 启动。

手动安装则从同步开始：

```bash
uv sync
uv run credstore set DEEPSEEK_API_KEY
uv run slife2
```

`slife2` 会启动配置所需的 MCP server，复用已经运行的那些，打开终端界面，
并在最后一个实例退出时停掉这些 server。

```bash
slife2 status    # 当前配置中的 server 在做什么
slife2 down      # 停掉它们
```

## 命令行接口

```text
slife2 [COMMAND] [OPTIONS]

命令：
  run       确保 server 可用，然后运行 TUI（默认）。
  status    报告配置所命名的每个 server 的状态。
  down      停掉配置所命名的 server。
```

`down` 只停共享 server 和 broker，不会关闭已经打开的窗口。遗留窗口在下一次输入
时自动重连，并从 turns 日志恢复对话。它持有的 agent 名字要到窗口退出才释放，
所以在它退出前，用同一个 `--agent` 名字启动新实例会被拒绝。

选项：

| 选项 | 含义 |
|---|---|
| `--data-dir DIR` | 使用 `DIR` 作为数据目录。覆盖 `SLIFE2_DATA_DIR`。 |
| `--agent NAME` | 以 `NAME` 身份运行。默认值：`slife2`。同一台机器上一个名字只能有一个活着的实例。 |
| `--model PROVIDER/MODEL` | 以此模型启动。只写 provider 时选择它的第一个模型。默认值：配置中的 `default`。 |
| `--url URL` | 将窗口连接到 `URL` 上的 agent server，而不是配置中的地址。 |
| `--a2a-broker HOST:PORT` | 本次运行让 peer agent 网络使用该 broker。它只覆盖 `peer_agent.broker`，从不覆盖 subagent 网络的 broker。它不能启用一个没有 `peer_agent:` 节的网络；如果 A2A server 已在另一个 broker 上运行，它会被拒绝，此时先停止整套服务。 |
| `--keep-servers` | 本实例退出后保留共享 server。 |
| `--headless` | 运行同一个窗口、同一批 server、连接和轮次，但不使用终端：不绘制、不打印。agent 仍然可以在 peer 网络上被访问。使用 Ctrl+C 退出。 |
| `--lang {en,zh}` | 设置窗口语言。不指定时从操作系统检测。 |

`--headless` 不是另一种 agent。它运行的是没有屏幕的同一个窗口；系统提示词会
被告知没有人在看着，以便模型调整行为。

`--lang` 只影响窗口：标签、harness 自己的行、面板、模型选择器、批准行和状态栏。
系统提示词、工具 schema 和日志行始终是英文。外部文本（例如工具结果和 peer
消息）保持其原始语言。

### plugin 可执行文件

每个 plugin 也可以直接启动，便于进程管理器或单独调试。

| 可执行文件 | server | 默认端口 |
|---|---|---|
| `slife2-agent` | agent 主循环与对话 | 24000 |
| `slife2-llm-openai` | OpenAI 兼容 chat completions | 24001 |
| `slife2-llm-anthropic` | Anthropic Messages | 24002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 24003 |
| `slife2-llm-embeddings` | embeddings | 24004 |
| `slife2-tui` | 终端收件箱，每个 agent 一个会话 | 24005 |
| `slife2-context` | 轮次日志与上下文决策 | 24010 |
| `slife2-toolhub` | 工具目录与模型工具清单 | 24020 |
| `slife2-wechat` | 微信登录与消息 | 24030 |
| `slife2-skills` | playbook 与 `skill_use` | 24031 |
| `slife2-cli` | `cli:` 节 | 24032 |
| `slife2-mcp-tools` | `mcp:` 节 | 24033 |
| `slife2-restapi-tools` | `rest-api:` 节 | 24034 |
| `slife2-cabinet` | 笔记、日记、文件、报告、dump | 24035 |
| `slife2-media` | 图片、视频、语音、转写 | 24036 |
| `slife2-agent-task` | 后台 agent 任务 | 24037 |
| `slife2-file-tunnel` | 本地文件的公开 HTTPS URL | 24038 |
| `slife2-schedule` | 定时任务及其账本 | 24039 |
| `slife2-a2a` | 基于 MQTT 的 peer agent 网络 | 24040 |

subagent 网络由同一个模块在 24041 端口提供，通过
`python -m slife2.plugins.a2a.local_server` 启动；它没有单独的命令行脚本。

**在 WSL2 的 `networkingMode=mirrored` 模式下，WSL 实例会把这些端口整体偏移
100**（agent 24100、toolhub 24120）。本机 MQTT broker 不偏移：两套 stack 共用它，
这正是两个 agent 能互相看见的原因。mirrored 模式让
Windows 与 WSL 的 loopback 合二为一，WSL 如果继续用默认端口，会被 Windows
实例发现并静默复用。Windows 侧保持文档中的默认端口。偏移量由 WSL 的
`slife2.yaml` 里的 `wsl_port_offset` 控制：默认 100，设为 `0` 可关闭，
在非 WSL mirrored 环境中该键会被忽略。

## 使用终端界面

### 按键

| 按键 | 操作 |
|---|---|
| `Enter` | 发送消息。 |
| `Shift+Enter` | 插入换行。 |
| `Esc` | 取消正在运行的轮次。 |
| `Ctrl+C` | 有选中内容时复制；否则取消正在运行的轮次；否则退出。 |
| `Ctrl+S` | 打开模型选择器。`↑`/`↓` 移动，`Enter` 选择，`Esc` 关闭。 |
| `Ctrl+Q` | 退出（Textual 的标准绑定）。状态栏不显示它，因为带流控的终端可能在应用看到它之前就将其消费。 |
| `PageUp` / `PageDown` | 翻动对话记录。 |
| `Home` / `End` | 跳到对话记录的开头或结尾。 |
| `Ctrl+A` / `Ctrl+E` | 移到输入行的开头或结尾。 |

鼠标滚轮在指针所在位置滚动对话记录。向上滚动会让视图停在原处，新的 token
不会把它拽回底部；发送消息后才会回到末尾。

### 发送消息

如果在一轮运行时发送消息，它会排队。当前轮次结束后，排队的消息开始运行。
状态栏显示队列深度。`Esc` 取消正在运行的轮次，但不会清空队列。

轮次运行期间到达的消息会在下一个迭代边界交付给该轮次，而不是留到下一轮。
这适用于窗口中的输入、其他 channel 转交的消息，以及 headless 窗口的输入。

### 模型选择

`Ctrl+S` 列出 `providers:` 下的所有模型，并标记当前使用的那个。选择一个模型会
调用模型自己也能调用的同一个 `model_switch` 工具。对话保留历史，从下一轮起使用
新模型；已经在运行的轮次会用启动它时的模型完成。状态栏会更新上下文窗口和
能力标记。

### 恢复对话

关闭再打开窗口、重启 server，或者等待空闲清理放掉对话，都不会开始新对话。
对话记录会带着原始时间戳、工具调用和结果恢复。推理内容会折叠在实时对话中
原本折叠的位置。

状态栏还会显示上一次模型调用留下的上下文大小，以及它占模型窗口的比例。每一轮
之前，会有一行注记说明上下文决策保留了哪些轮次、从日志中召回了哪些轮次。

### 图片

在句子中写出图片文件：

```text
这个布局哪里不对？ D:\shots\screenshot.png
```

模型会为它调用 `attach_image`。没有特殊语法。一次调用可以接受本地路径、
`http(s)` URL 或 `data:` URI，也可以同时接受多个。无法读取的来源会在回答中
被点名，而不会让其余来源失败。URL 会交给 provider 去获取。

只有模型的配置在 `input` 下列出 `image` 时，图片才可用。图片只发送到模型，
不会存进日志。轮次日志保留句子、点名文件的工具回答，以及一条记录图片曾被
附加及其大小的注记。因此恢复后的对话是文本；之后需要图片的轮次会按点名路径
重新附加。

## 配置

### 位置与默认值

配置文件是数据目录中的 `slife2.yaml`。从代码检出运行时，数据目录就是检出目录；
否则是 `~/.slife2`。使用 `--data-dir DIR` 或 `SLIFE2_DATA_DIR` 覆盖它。

缺少配置文件不是错误：会使用内置默认值。文件存在但无法加载时，启动会以一行
错误停止，并点名文件和错误原因。

安装器可能会在已有配置旁边写入 `slife2-default.yaml` 作为参考。slife2 只读取
`slife2.yaml`；参考文件供人比较和合并。

配置文件可以安全地纳入版本控制。它不应包含秘密；每个凭证都是运行时解析的
`${VAR}` 或 `keyring:` 引用。

### 配置节

| 配置节 | 作用 |
|---|---|
| `servers:` | 每个 plugin server 的地址与端口。未指定时使用默认值。 |
| `providers:` | chat provider 及其模型。 |
| `default:` | 新对话使用的模型，格式为 `provider/model`。 |
| `agent:` | 迭代上限与系统提示词模板。 |
| `agent-task:` | `max_running`，每个 agent 的后台任务数。 |
| `subagent:` | `max_running`，以及本地 subagent 网络的 broker 和 `org`。 |
| `embeddings:` | 记忆索引使用的 embedding provider。 |
| `context:` | 上下文决策的限制。 |
| `tool_load:` | 模型可加载工具的上限。 |
| `tool_result:` | 单个工具结果的 token 预算。 |
| `mcp:` | 外部 MCP server（stdio 或 HTTP）。 |
| `rest-api:` | 作为工具暴露的 OpenAPI 文档。 |
| `cli:` | 本机已安装的命令。 |
| `skills:` | `skills/` 下 playbook 的环境变量。 |
| `media:` | 图片、视频、语音和转写 provider。 |
| `file-tunnel:` | 本地文件的公开 URL provider。 |
| `peer_agent:` | 远程 peer agent 网络。 |

没有 `wechat:`、`cabinet:`、`schedule:` 或 `tui:` 配置节。微信登录通过扫码
创建，保存在 agent 目录下。文件柜、定时器和终端收件箱由其 server 以及所保存的
数据决定是否启用。

### 秘密

一个值可以是：

- 字面字符串；
- `${VAR}`，依次通过 shell 环境、[credstore](https://pypi.org/project/credstore/)
  和可选的字面默认值（`${VAR:-default}`）解析；
- `keyring:<service>/<key>` 引用，直接从操作系统 keyring 解析。

解析是惰性且宽松的。无法解析的引用会原样保留，因此缺失的 key 会在 API 调用处
失败，并能在错误信息中被点名，而不是在启动时失败。

### 示例

```yaml
agent:
  max_iterations: 0          # 0 或省略表示不设上限
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
  timeout: 20.0              # 判别器调用的秒数

tool_load:
  threshold: 100             # 加载工具超过该数量后卸载最久未使用的

tool_result:
  tokens: 8000               # 单个结果的预算；最小 128

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

随附的 `slife2.yaml` 在原处带有注释，是每个配置键的参考。

### 编辑配置

配置既可以手写，也可以由模型编辑。每个 family 为自己的配置节提供工具：

| family | 工具 |
|---|---|
| `mcp:` | `mcp_set`、`mcp_list`、`mcp_list_tools`、`mcp_remove`、`mcp_set_enabled` |
| `rest-api:` | `rest_api_set`、`rest_api_list`、`rest_api_list_tools`、`rest_api_remove`、`rest_api_set_enabled` |
| `cli:` | `cli_set`、`cli_list`、`cli_remove`、`cli_set_enabled` |
| `skills:` | `skill_set`、`skill_list`、`skill_remove`、`skill_set_enabled` |
| `providers:` 与 `default:` | `model_set`、`model_list`、`model_remove`、`model_switch` |

所有写入都经过同一个模块（`slife2.libs.host.configfile`），并在跨进程锁下执行。
写入是一次保留注释的文档编辑，结果由启动时使用的同一个加载器校验。加载器拒绝的
条目会被恢复，并告知调用方原因。

`set` 写入整个条目：省略的字段会被删除。`set_enabled` 只修改 `enabled` 字段。
写入之前没有批准闸；特别是，带 `command:` 的 `mcp_set` 会记录一个 slife2 将要
启动的程序。

## 能力

### 对话与记忆

对话不会无界地累积上下文。`slife2-context` 保存轮次日志，并在每一轮之前进行
一次模型调用，决定：

- 手头的对话中保留哪些轮次；
- 从日志中召回哪些轮次。

这一轮在两者合并后的结果上运行，按时间排序。这次调用读取整个手头对话，而不是
只看新消息，因为追问通常通过上面的轮次来指代对象。回答是一次选择，绝不是倾倒；
如果召回匹配到的内容超过容量，只返回放得下的部分。

被决策读取的每一轮都带有 `[TURN:{…}]` 脚注，写明它的 id、channel 和跨度。
最新的轮次也有脚注，因此它们和被恢复的轮次一样可寻址。大写 marker 是 harness
添加的注解，不属于存储记录。读起来像词的 marker（例如 `[Wechat:{…}]`）是发送者
写的，会随消息一起存储。

模型可以用 `turn_list` 和 `turn_read` 浏览历史。`turn_list` 按最新优先，每轮一行；
`since`/`until` 接受 ISO 日期或 `yesterday`、`last month`、`3 days ago` 之类的
说法，`limit`/`offset` 按 `total` 翻页。两个工具都不接受 agent 参数：调用本身
携带对话身份，因此模型只能读取自己的历史。

每一轮存储时都会建立两个索引：一个按“这一轮靠什么被找到”的文本建立的关键词
索引，一个按“它讲的是什么”建立的向量索引。两个索引都派生自轮次，也可以从轮次
重建。修改 `embeddings:` 下的 embedding 模型后，下次启动会重新嵌入全部历史。

有两个 harness 决策不是工具：

- `restore` 在对话开始时重放已保存的上下文。
- `rebuild` 在每一轮之前执行上下文决策。

如果 rebuild 决定保留手头的全部内容，就不会重建任何东西。判别器超时、provider
报错或回答无法解析时，会保留现有上下文并继续运行该轮。轮次存储缺失则会使该轮
失败，和其他缺失的 plugin 一样。

模型可以浏览轮次日志，但不能语义搜索它；上下文决策使用的召回属于存储自身。

### 工具

`slife2-toolhub` 是唯一决定模型可以看到哪些工具的进程。它自己不去连接外部
server。`slife2-mcp-tools` 持有 `mcp:` 连接，`slife2-restapi-tools` 持有
`rest-api:` 连接；它们各自声明来源及其提供的工具，hub 合并声明、命名工具、
把关并把调用路由回来源。

hub 提供三个集合级工具：

- `tool_search` 按关键词和语义搜索整个目录。
- `func_tool_load` 将一个或多个工具放入模型看到的清单。
- `_func_tool_unload` 从清单中移除工具。harness 也会在轮次边界调用它来执行预算。

模型可以调用 `tool_search` 返回的名字而不加载它，但加载会让它保留在每次请求
随附的清单中。模型的清单在每次模型调用前都从 hub 重新读取，因此轮次中加载的
工具从下一次迭代起可用。

slife2 自己的 plugin 提供的工具一开始都是加载的。外部来源只有在其条目设置
`autoload: true` 时才一开始加载。其他一切都按需发现和加载。

已加载清单由 `tool_load.threshold` 限制（默认 100）。超过限制时，最近最少被
调用的工具会被卸载；slife2 自己的工具和 `autoload` 来源永远不会被卸载。从未
被调用过的工具按加载时间排序。裁剪发生在轮次边界；被移除的名字会作为一次工具
调用及其结果写入对话，因此模型会知道清单缩小了。被逐出的代价是一次搜索和一次
加载，而不是失去一种能力。

工具命名：

- `mcp:` 条目提供的工具名为 `{server}__{tool}`，例如
  `arxiv__arxiv_search_papers`。
- plugin 自己的工具使用裸名，例如 `turn_read`。哪个 plugin 提供工具是系统内部
  的事实，不需要出现在模型读取的名字里。

每个工具结果都是一个 JSON 对象。列表回答：

```json
{"<noun>": [], "total": 0, "limit": 20, "offset": 0}
```

`total` 说明是否还有下一页。读取回答该行自己的字段。拒绝或未找到会抛出
`ToolError`，harness 将其作为失败调用连同原因交给模型。`_check_new_input`
是唯一例外：它原样返回一个人的消息。

任何回答都不应超过 `tool_result.tokens`。可能增长的列表使用 `limit`/`offset`；
无法设界的输出会保存为 dump（见[文件柜](#文件柜)）。

#### 工具调用参数

每个工具都接受三个元参数。它们会被添加到每个对外公布的 schema 中，并在分发前
剥离，因此工具 server 永远看不到它们。

| 参数 | 作用 |
|---|---|
| `_timeout` | 本次调用的秒数，覆盖工具的默认值。`0` 或负数表示“使用默认值”，绝不是“不设限”。自身声明了 `timeout` 参数的工具会自行执行该参数。两者都不适用时，调用会一直运行到结束。没有默认超时。 |
| `_async` | 在后台运行。模型会收到一个 `task_id`；结果会带着 `[AgentToolResult:{…}]` marker 交回对话，并绘制在 `async-tool:<tool>` channel 上。`check_async` 报告调用是在运行、已完成、失败还是已取消；`cancel_async` 放弃它。后台化只是去掉等待，不是去掉结果预算。agent task 的循环没有 `_async`。 |
| `_approve` | 运行前询问用户。终端会画一行内联提示（`y` 批准，`n` 或 `Esc` 拒绝），并通过 `answer_approval` 回答。如果 channel 背后没有人（微信、定时运行、另一个 agent，或无法绘制提示的客户端），调用会直接运行。能够回答的调用方会用 `send_message(..., watched=true)` 表明这一点。 |

#### Skills 与命令

Playbook 位于 `<data>/skills/<name>/SKILL.md`。`slife2-skills` 提供 `skill_use`，
它返回 playbook 文本，并在前面附上其所在目录。安装 skill 就是把一个目录放进
`skills/`；该工具每次调用都会读取磁盘，因此不需要重启。

Skill 可以在头部声明环境变量要求。值来自 `skills:` 配置节，通过和其他秘密相同的
链解析。如果缺少 key，`skill_use` 会在模型执行依赖它的指令之前报告。

`cli:` 条目描述本机已经安装的程序。每个条目都是目录中的一行（`cli:yt-dlp`），
因此 `tool_search` 可以按功能找到它。该行不是可调用工具，也没有加载状态。目前
没有工具执行 `cli:` 条目；`func_tool_load` 会报告这一点。

`enabled: false` 让条目保持配置但不连接。该值必须是真正的 YAML 布尔值：在
YAML 1.2 中，`no`、`off` 和 `'false'` 是字符串，会被拒绝而不是猜测。禁用条目的
工具仍留在目录中，标记为 `disabled`，搜索仍能找到它们。

### 媒体

`media:` 配置节提供四种能力：

- `generate_image`：根据提示词绘制图片。
- `generate_video`：根据提示词或图片渲染视频。
- `text_to_speech`：将文本写成音频文件。
- `transcribe_audio`：转写音频文件，使用 `offset` 和 `limit` 分页。

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

支持的 `api:` 值为 `dashscope-aigc`（图片、视频、语音、转写）和
`openai-images`（仅图片生成）。选择不支持所请求类型的模型会被拒绝。

生成的文件写在 `<data>/agents/<agent>/media/` 下。`folder` 参数会在该根目录
内部创建子目录；指向根目录之外的路径会被拒绝。超过超时的长时间视频调用会带着
provider 的作业 id 被拒绝，而不是返回结果。

### 文件隧道

`slife2-file-tunnel` 将本地文件发布到公开 HTTPS URL，使无法读取本地磁盘的服务
也能获取它：

```text
expose_local_image(path)      -> https://<tunnel>/share/<token>
unexpose_local_image(token)   -> 该 URL 失效
```

文件从原位置提供，不会被复制。以下情况会使链接失效：

- 文件发生变化（token 与文件的身份和元数据绑定）；
- 使用 token 调用 `unexpose_local_image`；
- 进程退出。

分享会比创建它的轮次活得更久；由模型或操作者撤回。私钥、`.env` 文件和保存的
密码会被按名字拒绝。

支持三个 provider，恰好一个生效，其他可以保持配置但不参与：

| provider | 要求 | URL 行为 |
|---|---|---|
| `ngrok` | `NGROK_AUTHTOKEN` | 每个账号一个稳定的开发域名。免费档会向浏览器显示插页提示页。 |
| `localhost.run` | `ssh` 客户端 | 随机 `*.lhr.life` 主机名，每几小时轮换一次。TLS 在 provider 处终止，字节由第三方中转。 |
| `cloudflare` | `cloudflared` 二进制 | 在进程生命周期内稳定的 `*.trycloudflare.com` 主机名。 |

内置默认值是 `ngrok`。随附的 `slife2.yaml` 选择 `cloudflare`，它不需要账号，
也不把字节中转给任何人；如果未安装 `cloudflared`，将 `active:` 改为
`localhost.run`。

没有启动的隧道，或者发布了无法访问的 URL 的隧道，会被拒绝而不是交出去。
`tunnel_status` 工具报告状态、URL、可达性、边缘地址和使用的二进制；它是内部
诊断工具，不暴露给模型。

### 文件柜

文件柜按 agent 保存笔记、日记、文件、报告和 dump。

| 类型 | 键 | 第二次写入 |
|---|---|---|
| `note` | 用户选择的主题 | 替换笔记。 |
| `diary` | 日期 | 追加一节。 |
| `file` | 文件柜内的路径 | 复制文件进来。 |
| `report` | 标题 | 追加一节。 |
| `dump` | 产生输出的工具调用 | 由 harness 写入。 |

笔记和文件是文档：第二次写入就是它们的内容，因此只有这两种类型有删除工具。
日记和报告是日志：同一日期或标题下的两条记录是两个事实，后一段文字不会说明
它替换了前一段的哪一部分。

dump 是对对话而言过大的工具输出。输出会完整保存，以工具调用 id 为键；对话携带
输出自身若干行的选集，并在 `tool_result.tokens` 之内。不做任何改写。选集由一次
判别器调用完成：它接收输出、手头对话和当前输入，并回答行范围。模型可以用
`dump_read` 读回 dump 的任意部分，这是一个会搜索的读取器：`pattern` 查找行，
`context` 加上相邻行，`offset` 从某一行开始读（负数从末尾算），`limit` 少读一些。
每个回答都保持在同一个 token 预算内。除非 `kind` 为 `dump`，搜索不会看到 dump。

每个文档都会写两遍：文件柜索引中的一行，以及 `agents/<agent>/cabinet/` 下的
Markdown 文件（`notes/`、`diary/`、`reports/`），无需 slife2 即可打开、编辑或
删除。主题名如果本身不是全小写，文件名中会带一个短摘要
（`notes/Python~6e3604888c.md`），因为 Windows 和 macOS 将 `Python.md` 和
`python.md` 视为同一个文件。保存文件的字节也放在文件柜下，但不进入索引；
`file_read` 返回文件文本，遇到 PDF、图片或压缩包会说出它是什么而拒绝。其他编码
的文本会被解码，并在回答中说明编码。

`cabinet_search` 接受与 `tool_search` 相同的两种输入：`keywords` 是必须全部出现
的词，`sentences` 是按语义匹配的问题。多个句子就是多个问题，每个问题只会增加
结果。`cabinet_summarize` 为文档写描述；为文件写描述会重新嵌入它，因为描述是
构建其向量所用材料的一部分。

`file_save` 复制本机已有的文件，不获取 URL。文件柜没有 `grep` 搜索腿。

### 微信

`slife2-wechat` 通过 iLink 托管网桥为每个 agent 连接一个微信账号。登录是扫描
出来的，而不是配置出来的。`wechat_login` 返回二维码；扫码后 token 保存到
`agents/<agent>/wechat.yaml`，下次启动时读取。登录大约持续一天；
`wechat_check_status` 报告剩余时间。

一个微信账号只能有一个活着的登录。用同一账号为第二个 agent 或第二台机器扫码，
会静默作废第一个登录，之后它会收到网桥的 `session timeout`。第一个 agent 的
窗口显示 `[✗ Wechat offline]`，`wechat_check_status` 给出原因。再次扫码可以
拿回账号。

| 工具 | 作用 |
|---|---|
| `wechat_login` | 获取二维码并开始监听扫码。 |
| `wechat_check_status` | 报告连接状态、剩余时间，以及应回复的 peer。 |
| `wechat_send_message` | 向 peer 发送文本消息。 |
| `wechat_logout` | 忘记此 agent 的登录。 |

连接的到来和离开会作为 harness 行绘制，而不是轮次：`[⚡ Wechat online]` 和
`[✗ Wechat offline]`。原因仍可通过 `wechat_check_status` 获取。

来自微信的消息会成为该 agent 对话中的一个轮次，并绘制在显示该对话的窗口中。
它以 `wechat` channel 和 `[Wechat:{…}]` 前缀记录，前缀点名发送者。回复是模型的
职责：没有任何东西会自动派发回答。模型使用 peer 和 marker 中的 `context_token`
调用 `wechat_send_message`。轮次运行期间，对方会看到“正在输入”提示。

每个 agent 单独登录，有自己的文件和轮询循环。subagent 没有微信登录：微信是
对外身份，而 subagent 是其创建者的内部成员。plugin 是一个共享进程。

### Agent 任务

agent 任务是由同一个 agent 运行的独立循环，以名称和描述启动，稍后在同一段对话
中通过消息回答。

| 工具 | 作用 |
|---|---|
| `agent_task_start` | 启动任务并立即返回 id。 |
| `agent_task_status` | 报告任务正在做什么，从不返回结果。 |
| `agent_task_list` | 列出此 agent 的任务，运行中的在前。 |
| `agent_task_cancel` | 停止任务并报告调用后的状态。 |

任务以 agent 身份运行，使用相同的系统提示词、模型、工具和上下文决策。它没有
启动它的那个轮次：`restore` 读取已存储的轮次列表，而轮次只在结束时写入，因此
描述必须重述相关上下文。任务的轮次不会写入 agent 的对话。它的回答作为一条带有
`[AgentTaskResult:{…}]` marker 的消息，在 `agent-task:<name>` channel 上投递，
并绘制为 `AsyncTask(<name>)>`。如果没有窗口显示该对话，回答会被报告为
`undelivered`，日志会说明这一点。

```yaml
agent-task:
  max_running: 4
```

`max_running` 是该节唯一的键。其他一切来自 `agent:`。一个任务可以启动另一个
任务，因此 `max_running` 限制并发，但不限制随时间发生的总工作量。

### 定时任务

定时任务是由模型写下的名称、描述和 cron 表达式。`slife2-schedule` 在 slife2
运行期间触发它。任务到期时，agent 在显示该对话的窗口中收到它，作为一个轮次。
该轮次调用 `schedule_run_now`，并带上它携带的到期时间。如果触发时没有窗口，
这次运行会以 `failed` 结束并记录原因。

| 工具 | 作用 |
|---|---|
| `schedule_set` | 写入或替换任务：名称、描述、cron、时区。 |
| `schedule_remove` | 移除任务及其所有运行记录。 |
| `schedule_list` | 列出任务及其下次到期时间。 |
| `schedule_runs` | 列出账本：每次触发一行。 |
| `schedule_run_now` | 立即启动本次运行的工作；带 `due_at` 时补做某次运行。 |
| `schedule_run_skip` | 关闭一次不会发生的运行，并说明原因。 |

一次运行就是一次触发，以到期时刻命名。状态从 `pending` 迁移到 `ran`、`failed`、
`missed` 或 `skipped`。记录在工作交接之前写入，因此下一次轮询不会重复触发。
交接被拒绝会将运行标记为 `failed`。工作无法启动的运行会以 `failed` 关闭并记录
原因，而不是保持打开。

任务只在 slife2 运行期间触发。启动时，清扫会关闭被停止进程留下的运行，轮询会
记录每个任务最近一次错过的触发。`schedule_runs(status="missed")` 列出它们。
错过的运行可以用 `schedule_run_now(name, due_at)` 补做，或用 `schedule_run_skip`
关闭。

账本位于 `<data>/agents/<agent>/schedule.db`。没有配置节。任务的 `timezone` 是
IANA 名称；省略时使用机器当前时钟，不会跟随夏令时变化。

### Peer agent

agent 的 peer 是其他 agent，通过公开发布的 A2A-over-MQTT profile 通信。用户在
`slife2.yaml` 中写 `peer_agent:`；模型看到 `peer_agent_*` 工具。底层协议名称不会
出现在模型读取的任何内容中。

| 工具 | 作用 |
|---|---|
| `peer_agent_send_message` | 发送任务、完成收到的任务，或发送消息。由 `message_type` 选择操作。 |
| `peer_agent_cancel_task` | 撤回此 agent 发出的任务。 |
| `peer_agent_list` | 列出此 agent（排第一），然后按名字列出每个 peer。 |
| `peer_agent_task_list` | 列出仍然打开的任务：等待回答的和仍欠回答的。 |
| `peer_agent_broadcast` | 向每个 peer 发送即发即忘的事件。 |

`task_request` 立即返回 id；结果稍后作为一条带有
`[PeerAgent:{"from": …, "type": "task_response", "task_id": …}]` marker 的消息
到达。没有需要轮询的东西。`task_response` 使用 marker 中的 `task_id` 完成此
agent 收到的任务。方向由 `from` 表示；`agent_name` 是 agent 自己的身份。

每个 agent 有一张 mesh，因为 agent 名称就是 MQTT client id 和请求主题的最后
一段。同一台机器上的两个 agent 是两个 peer。一个 agent 从两个地方被访问时，
会在加入之前被名称冲突检查拒绝。subagent 不在这张网上：它没有对外身份，只能
通过其创建者的本地 subagent 网络访问。

在线状态作为 harness 行绘制，而不是轮次：`[⚡ jill online]`。花名册可通过
`peer_agent_list` 获取。窗口持有 `join` 调用期间，agent 就在 mesh 上；
`--headless` 在没有窗口的情况下持有同一个调用。入站任务不持久化：进行中的
任务保存在内存中，重启后从零开始。

当配置的 broker 在本机且不存在时，会自动启动；只有 slife2 启动的 broker 才会随
整套服务停止。系统安装的 broker 或手动启动的 broker 不会被碰。无法服务的 broker
会使启动失败，并点名它及其日志。`provider: mosquitto` 是唯一支持的值。

```yaml
peer_agent:
  provider: mosquitto
  broker:
    host: localhost
    port: 1883
  org: default
  unit: default
```

省略 `broker:` 时，peer 网络使用 subagent 网络的 broker 地址。完全省略
`peer_agent:` 节会让机器不接入该网络。

重启会丢掉所有进行中的任务。仍在等待的 peer 会在自己的 `peer_agent_task_list`
中看到该任务并重新发送，重发会作为新请求到达。

### Subagent

subagent 是属于另一个 agent 的 agent。它拥有创建者的工具集、自己的角色系统
提示词和自己的对话，但没有进程、没有窗口，也不占用 agent 名称。它也没有独立的
对外身份：没有微信登录，也不在 peer agent 网络上。它只通过本地 subagent 网络
被访问，在有消息发送给它时运行。它的两个成员管理工具 `subagent_set` 和
`subagent_remove` 会拒绝 subagent 调用者。

subagent 通过 `subagent_set` 创建，通过 `subagent_remove` 移除。创建后，它通过
本地网络使用与 peer agent 相同的五个操作访问，名称为 `subagent_*`：
`subagent_send_message`、`subagent_cancel_task`、`subagent_list`、
`subagent_task_list` 和 `subagent_broadcast`。

| 键 | 含义 |
|---|---|
| `name` | subagent 的名称和目录。 |
| `model` | `provider/model`；为空时使用创建者的模型。 |
| `role` | 包含在 subagent 提示词中的一个词。 |
| `fork` | 将创建者当前对话复制为 subagent 的起点。fork 的轮次不会存储，且只存活于当前会话。 |

非 fork 的 subagent 位于 `agents/<maker>/subagents/<name>/`，有自己的
`turns.db`、`subagent.yaml`、文件柜和媒体目录。fork 在该目录下只有
`subagent.yaml` 记录：它不存储轮次，对话作为创建者对话的副本保存在内存中，
只读取创建者的 turn log 作为上下文，不向其中写入；文件柜和媒体也共享创建者的。
fork 是会话级（session-lived）的，就像创建者在当前会话中的一个临时分身：
agent server 下次启动时会删除上一会话留下的所有 fork 记录，名称重新可用。
微信和 peer agent 网络属于 agent，不属于 subagent；定时任务也留在 agent 上。
树只到一层：subagent 不能创建或移除 subagent，因为本地网络只为其创建者自己的
subagent 保留位置，再下一层没有位置。

移除 subagent 时，先通知本地网络释放其位置，再停止其循环，最后删除其目录。
如果网络通知无法完成，subagent 保持不变。

本地网络在 `subagent:` 下配置。它需要 `broker`；没有 broker 的 `peer_agent:`
会使用同一地址。`unit` 始终是 agent 的名称，写入它会被拒绝。可以设置 `org`
来对 mesh 分组。

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
  slife2.yaml                  配置；不存在时使用内置默认值
  skills/                      <name>/SKILL.md playbook
  agents/
    tools.db                   此 home 共享的工具目录
    <agent>/
      turns.db                 对话及其实时上下文
      schedule.db              定时任务及其运行账本
      cabinet/                 notes/、diary/、reports/、保存的文件、.index.db
      media/                   生成的图片、视频和音频
      wechat.yaml              此 agent 的微信登录
      subagents/
        <name>/                非 fork 的 subagent：turns.db、subagent.yaml、
                               cabinet/、media/；fork 只有记录文件
```

工具目录位于 agent 目录旁边，因为它属于整个 home，而不属于某一个 agent。

### 运行时目录

机器级共享 server 的记录保存在 `~/.slife2/runtime`：

```text
<runtime>/
  servers/        每个运行中 server 的一条记录
  logs/           每个 server 的输出
  locks/          按端点的启动锁
  agents/         agent 名称占用
  clients/        哪些客户端正在使用共享 server
  homes/          此机器使用过的数据目录
```

`SLIFE2_RUNTIME_DIR` 覆盖该位置。运行时目录是可重建的：删除它后，下次启动会
重建所需内容。

### 多个数据目录

一个数据目录可以包含多个 agent。它们共享该目录的 `slife2.yaml`、`skills/` 和
`agents/tools.db`，因此运行在同一套工具上。一个 agent 加载的工具对另一个也是
加载的。

agent 身份与目录无关。`--agent jack --data-dir D` 命名 agent 并说明其文件位置。
名称在整台机器上只占用一次，因此另一个数据目录中的第二个活着的 `jack` 会被拒绝。
一个名称上次在哪个目录使用不会被记住。

server 跨数据目录共享。为一个 home 启动的 server 为该机器上的每个 home 服务，
每次调用按它来自的 home 解析。外部 `mcp:` 条目按名称只对应一个 server，无论多少
个 home 配置它；如果两个 home 为同一个名称给出不同命令，日志会报告分歧，并使用
正在运行的那个。

## 架构

### 分层

包按模块“是什么”划分，而不是按大小划分：

| 目录 | 内容 |
|---|---|
| `slife2/plugins/` | 每个 plugin 一个进程。每个 plugin 有一个带 `main` 和 `SERVER_NAME` 的模块。 |
| `slife2/clients/` | 一次 MCP 跳转的客户端部分。 |
| `slife2/libs/` | 被多个组件 import 的库。 |
| `slife2/helpers/` | 小型无状态辅助函数。 |
| `slife2/tui/` | 终端窗口。它是客户端，不提供任何东西。 |

plugin 目录可以保留自己的私有库，例如 `plugins/cabinet/store.py` 或
`plugins/schedule/expressions.py`；`libs/` 用于被多个组件 import 的代码。

依赖方向是单向的：

```text
tui/ -> MCP -> plugins/agent/ -> libs/turn/loop.py
     -> {libs/model/base.py, libs/turn/tools.py, libs/turn/events.py,
         libs/turn/messages.py} -> libs/host/config.py
```

主循环既不 import agent server，也不 import 终端。它接收一个 backend 和一个
返回工具注册表的协程；注册表来自哪里是 agent server 的职责。

### Plugin 注册表

`libs/host/config.py` 的 `PLUGINS` 中每个 plugin 一行。一行记录：

- `servers:` 中使用的键；
- 提供它的模块；
- 它应答的 MCP 名称；
- 用于识别它的探针工具；
- 默认端口；
- 它是否为模型 API 后端。

启动顺序、默认地址、server 名称常量和 launcher 规格都从该表派生。添加 plugin
就是写模块并添加一行；删除就是删除两者。

`libs/plugin/protocol.py` 提供共享的 server 脚手架（`serve_plugin`）。server 提供
三个钩子：构建什么、关于启动说什么，以及它需要什么。所有 server 都走这条路径，
包括模型后端，它们通过一层包装到达同一个函数。

## 开发

### 测试

```bash
uv run pytest                  # 快车道：单元与集成测试，并行
uv run pytest -m unit          # 仅单元测试
uv run pytest -m integration   # 需要绑定 socket 或启动进程的测试
uv run pytest -m e2e           # 整套服务作为进程（约 70 秒）
uv run ruff check . && ruff format --check .
uv run pyright
```

默认运行会跨所有核心执行（`addopts` 中的 `-n auto`），并排除 `e2e` 标记。

快车道不需要 API key，也不需要运行中的 server。模型是假的，server 通过 FastMCP
的内存传输驱动：真实的 MCP 消息和进度通知，不绑定端口。集成测试绑定临时端口，
因此测试套件可以在正在运行的服务旁边运行。

端到端车道将真实整套服务作为进程启动：launcher、十七个守护进程、磁盘上的配置，
以及一个桩 OpenAI 兼容 provider。它验证启动顺序、跨进程轮次、工具路由、进度
通知、大输出处理和对话恢复。

共享测试替身位于 `tests/fakes.py`：一个桩模型、一个桩 embedder、假的窗口和 mesh
客户端，以及被多个测试模块使用的工厂。

### CI

CI 在 Ubuntu、macOS 和 Windows 上使用 Python 3.13 运行：

1. 在每个操作系统上执行只读的 `install --dry-run`；
2. 构建 wheel；
3. 断言解释器可以加载 SQLite 扩展；
4. `ruff check` 与 `ruff format --check`；
5. `pyright`；
6. 默认测试车道；
7. 端到端车道。

## 已知限制

- `cli:` 条目会进入目录并可被发现，但目前没有工具执行它们。
- slife2 不执行 playbook 脚本；`skill_use` 返回 playbook 文本，由模型自己运行
  其中的命令。
- 文件柜的 `file_save` 不获取 URL，文件柜也没有 `grep` 搜索腿。
- 模型可以用 `turn_list` 和 `turn_read` 浏览轮次日志，但不能语义搜索它。
- 编辑配置前没有批准闸，包括带 `command:` 的 `mcp_set`；该调用会记录一个
  slife2 将要启动的程序。
- 一个 agent 任务可以启动另一个 agent 任务；`agent-task.max_running` 限制并发，
  不限制随时间发生的总工作量。

## 许可证

MIT。见 [LICENSE](LICENSE)。
