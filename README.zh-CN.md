# slife2

**终端 AI 智能体 —— slife v2，推倒重来的一次重建。**

[English](README.md)

**plugin 就是 Streamable HTTP 上的一个 MCP server，外加一份很小的契约** —— 它是
这套协议的一个实现，而不是自创的协议。TUI 只是个客户端，别的什么都不是；agent
主循环既是 server 又是 client；每个模型后端都是自己的 server，工具来源的 hub 也是，
连终端自己的收件箱也是一个 plugin，与其余的一视同仁。这个词把 slife2 自己启动的
server 和 `mcp:` 下别人写的 server 区分开，而这个区分是承重的：**plugin 是必需的**
—— 少一个这一轮就失败；而 `mcp:` 下的一项是可选的，它不在只是意味着工具少一些。

由此有两条性质，而它们正是这套安排的意义所在：

- **密钥只存在于需要它的那个进程里。** provider 的 API key 住在调用它的 LLM
  server 里；工具 server 的 token 住在持有那个 server 的 plugin 里 —— `mcp:` 归
  `slife2-mcp-tools`，`rest-api:` 归 `slife2-restapi-tools`。agent 循环哪一个都
  泄露不出去，因为它从来没有过。
- **agent 循环不 import 任何 provider SDK。** 换 provider 就是改一个 URL，加工具
  就是往配置文件里加一项。

**plugin 是一种能力，library 是它的实现。** 持有凭证、SDK 或别人的连接的东西是一个
进程；持有一个*文件*的不是，因为单一写入者本来就是 SQLite 锁。所以
`slife2.libs.store.db`（对话轮次日志）是一个 library，由拥有它的 plugin
`slife2-context` import；`slife2.libs.store.catalogue` 是工具目录，由 `slife2-toolhub`
在进程内打开。`slife2.libs.store.index` 也一样，那是这两个存储被*检索*所用的机制，
同时也是文件柜的 library。判定标准和它的代价见 DESIGN §1.1。

[DESIGN.md](DESIGN.md) 讲的是这些决定，以及它们背后的实测数据。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 和 Python 3.13。

```bash
uv sync
credstore set DEEPSEEK_API_KEY         # 或者 export 出去
uv run slife2
```

就这么多。`slife2` 会拉起配置需要的那些 MCP server，接上已经在跑的那些，然后启动
TUI。

**macOS 上必须用一个能加载 SQLite 扩展的 Python**，而你最可能正在用的那两个都不是。
向量索引是一个 SQLite 扩展（`sqlite-vec`），而没有用
`--enable-loadable-sqlite-extensions` 构建的解释器根本没有办法加载它：uv 自带的托管
CPython 就是（它静态链接 SQLite），Apple 的 `/usr/bin/python3` 也是（系统 SQLite
出于沙箱原因没编这个功能）。这里没有任何绕过的办法 —— 没有索引数据库就打不开，于是
一个 plugin 都起不来，第一条命令会以 `this Python's sqlite3 cannot load extensions`
失败。Windows 和 Linux 上的构建有这个功能，Homebrew 的 Python 也有：

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

```
slife2 [--data-dir DIR] [--agent NAME] [--model PROVIDER/MODEL] [--url URL]
       [--a2a-broker HOST:PORT] [--keep-servers] [--headless] [--lang EN|ZH]
                                         确保各个 server 就绪，然后运行 TUI
slife2 status                            什么在运行，在哪里
slife2 down                              停掉本配置指名的那些 server
```

`--model` 选择这个实例启动时用哪个模型，写成 `provider/model`（也可以只写
provider，表示它的第一个模型）；不写就用配置里的 `default`。`--url` 覆盖 agent
server 的端点，用在它已经在别处跑着、而当前配置并不知道那个地址的时候。

**`--a2a-broker` 把网格指向另一个 broker，而不是配置里的那个** —— 别人的、团队的、
已经在另一台机器上跑着的 —— 这样为了一次会话加入某个网格，不必去改一个每次会话都要
读的文件。它只覆盖 `a2a.broker`，别的都不动 —— **本地那张网的 broker 不在其内**：那
是 `subagent:` 的、是本机自己的，所以一个 agent 的子 agent 仍然留在文件给它们的
broker 上。这是哪张网（`org`/`unit`）仍然是那一节
说了算，而没有 `a2a:` 节的机器仍然不在网上，所以在那里这个参数会被拒绝，而不是顺手把
网格打开。**broker 归属于一个正在运行的整套服务**，所以这个参数只能由启动网格的那条
命令来兑现：如果 server 们已经跑着了，它们就在自己启动时用的那个 broker 上，`slife2`
会这么说然后停下，而不是把你留在一个错误的 broker 上。先 `slife2 down`。

**`--headless` 让那个窗口不带终端运行。** 同样的 server、同样的连接、同样的轮次，
由同一份代码运行 —— 只是什么都不画、什么都不打印。它要解决的问题是面前没人的机器：
窗口持有 **agent 在 A2A 网格上的位置**，所以同伴可以把任务派给它并得到回答；而没有
人开过窗口的机器会让那个 agent 干脆处于离线状态。agent 本身别的什么都不变 —— 它是一
个没有屏幕的窗口，而不是另一种 agent —— 而告诉*模型*没人在那儿的，是它的系统提示词
（提示词模板里的 `headless`），那才是决定它行为方式的杠杆。**Ctrl+C** 是退出的方式。

**`--lang` 是窗口的语言** —— 英文或中文，不给就从系统语言推断。不给时中文系统得到中文
窗口、其它一律英文；给了则可以在任何机器上要其中任意一种，这正是它的用途。它覆盖的是
窗口：标签、harness 自己那几行、各个面板、选择器、批准行、状态栏。**它到不了模型那里。**
无论这个参数怎么说，系统提示词、每一个工具 schema、每一行日志都是英文 —— 因为一个会
随终端改变语言的对话，是在最不该出岔子的那个地方多出了一个变量 —— 而外部文本（工具的
结果、同伴的消息）是别人的话，原样通过。按键保留英文名，因为 `Esc` 和 `Ctrl+S` 是键盘
上印着的；它们后面的动作词才是界面的。

**各个 server 是共享的。** 再起一个 `slife2`（无论 `--agent` 叫什么）会发现它们已经在
跑，于是直接复用；没有任何东西会被启动两次，也没有任何东西是按 agent 一份的。它们是
守护进程，所以关掉一个 TUI 不会打断另一个；而当**最后一个**实例退出时，它会停掉正在
运行的那些。`--keep-servers` 则把它们留着。

`--agent NAME`（默认 `slife2`）给这个实例命名。它是窗口标题、是助手消息的署名、也是
渲染系统提示词用的名字 —— 并且它是**独占的**，两个活着的实例不能共用一个名字。它*确实*
做了划分的地方，在那些仍然共享的 server 内部：每个 agent 的轮次进自己的数据库，每个
agent 是 `slife2-tui` 上自己的一个会话 —— 这就是为什么从手机发来的消息到达的是*你的*
终端，而不是旁边那一个。

**上下文是选出来的，不是攒出来的**：每一轮之前先做一次模型调用，决定手上的对话留下
什么、又从日志里召回什么，这一轮就跑在这两者之上。没有关掉它的开关。一个重启的对话，
或者闲置够久以致循环被回收的对话，会在退出时的那个上下文上回来。下一节讲的完全是这件事。

那份日志是模型可以回看的地方。`turn_list` 浏览它 —— 最新在前，一轮一行，`since`/`until`
接受 ISO 日期或者一个人会写的那种说法（`yesterday`、`last month`、`3 days ago`），用
`limit`/`offset` 按 `total` 翻页 —— 而 `turn_read` 返回完整的一轮。两者都不接受 `agent`：
调用本身带着它代表的是哪段对话，所以模型读到的是自己的历史，别的什么都读不到，它的任何
参数都改变不了这一点。

每一轮在存入时还会**被索引两次** —— 一个建立在「这一轮靠什么被找到」的文本上的关键词
索引，和一个建立在「它讲的是什么」上的向量索引 —— 于是以后一轮可以被相关性找到，而不
只是按时间。两个索引都派生自轮次、也可以从轮次重建，这就是为什么换 embedding 模型
（配置里的 `embeddings:`）的代价是下次启动时把整段历史重新嵌入一遍，而不是一次迁移。
模型可以浏览日志，但还不能*搜索*它：上下文决策所用的召回是存储自己的，把同一个搜索作为
工具提供出来是另一件事（DESIGN §9）。

每个 plugin 也是自己的 console script，所以进程管理器不用传参数就能单独跑一个：

```bash
uv run slife2-agent            # the agent loop,             :8000
uv run slife2-toolhub          # the model's tools,          :8020
uv run slife2-skills           # the playbooks,              :8031
uv run slife2-cli              # the cli: registry,          :8032
uv run slife2-mcp-tools        # holds the mcp: servers,   :8033
uv run slife2-restapi-tools    # holds the rest-api: ones,   :8034
uv run slife2-wechat           # WeChat, a login per agent,  :8030
uv run slife2-cabinet          # notes, diary, files,        :8035
uv run slife2-media            # images, video, speech,      :8036
uv run slife2-file-tunnel      # a public URL for a file,    :8038
uv run slife2-schedule         # scheduled tasks, the clock, :8039
uv run slife2-a2a              # the A2A mesh over MQTT,    :8040
uv run slife2-tui              # the terminal's inbox,       :8005
uv run slife2-context          # turns, and the context,  :8010
uv run slife2-llm-openai       # the OpenAI-compatible API,  :8001
uv run slife2-llm-anthropic    # the Anthropic Messages API, :8002
uv run slife2-llm-openai-responses  # the OpenAI Responses API, :8003
uv run slife2-llm-embeddings   # vectors for both indexes,  :8004
```

TUI 里：**Enter** 发送，**Shift+Enter** 换行，**Esc** 取消正在跑的一轮，**Ctrl+C**
也取消，没有在跑的就退出（有选中内容时则改为复制），**Ctrl+S** 打开模型选择器 ——
`↑`/`↓` 移动，`Enter` 选定，`Esc` 离开。这些就是状态栏点名的键，而且只点名这些：
**Ctrl+Q** 也能退出（任何 Textual 应用都这样），之所以不说，是因为带流控的终端会在窗口
看到它之前就把它当成 XON 吃掉。

**PageUp/PageDown** 翻动对话记录，**Home/End** 跳到它的两端，滚轮在指针所在的任何位置
滚动它 —— 这些键移动的是对话记录而不是输入框，所以下一封消息还没写完时也能读一段很长
的回答。向上滚动会把页面停在那儿：流式到来的 token 不会把视图拽回去，发一条消息才会把
它带回末尾。输入框保留 **Ctrl+A** 和 **Ctrl+E** 用于行的两端。

**Ctrl+C 仍然是复制键。** 有选中内容时 —— 在输入框里，或者用鼠标在对话记录里选中 ——
它复制，什么都不取消；别的程序里它就是这个行为，而一个不能复制的终端，比一个「停下当前
一轮」需要第二种写法的终端更糟。**Esc** 就是那第二种写法，而且仅仅是它：它取消在飞的
那一轮，无论按多少次都不会关掉窗口。没有选中内容时，**Ctrl+C** 取消这一轮；连在跑的
都没有时，它退出。

**Ctrl+S 切换模型。** 选择器列出 `providers:` 下的每一个模型并标出正在用的那个，选中
一个会调用模型自己调用的同一个 `model_switch`，所以人切换模型和模型切换模型是同一种
行为。对话保留自己的历史，从下一轮起换到新模型上 —— 已经在飞的那一轮用启动它的模型跑完
—— 状态栏也跟着换成新模型的窗口和各项能力标记。**这条路径上没有任何一步需要那个正在
失败的模型**：它是一次配置读取加一行发往 agent server 的请求，这正是这个键的意义，因为
一个已经不回答的模型不是拿来跟它讲道理的东西。

在一轮还在被回答时再发一条消息，它会**排队**：已经在跑的跑完，你的下一个跑，状态栏说
还有几条在等。Esc 停掉在飞的那一轮，不碰队列 —— 你放弃的那条和还在等的那条不是同一条
消息。

**重启不等于新对话。** 关掉窗口再打开 —— 或者重启守护进程，或者让它闲置到空闲清理把
这段对话放掉 —— 对话记录会回来：同样的问题、同样的回答、同样的工具调用连同它们返回的
结果，每一行打的都是它被发出的那一刻，而不是窗口打开的那一刻。被放回去的是这段对话的
*上下文*，由 agent server 在它构建循环的那一刻恢复；窗口则去要那段上下文是由哪些轮次
构成的，并把它们画出来，连同推理一起 —— 一个先想后答的步骤回来时，它的思考折叠在
活着的那个版本里原本折叠的位置。

**窗口也会说上下文正在做什么。** 一段被恢复的对话会在状态栏里显示它的大小 —— 是上一次
调用把对话留在的那个数，不是累加总数，以及它占模型窗口的百分之多少。之后每一轮前面都会
有一行注记，说明*判别器*为它决定了什么：手上的对话留下了多少，又为多少回到轮次日志里
去取。那是每一轮一次、别的地方都不会展示给你的模型调用，而它正是让长对话不至于无界增长
的机制 —— `slife2-context` 自己的判断，论证见 DESIGN §5.1。

指名一张图片，模型就会把它附上：

```
这个布局哪里不对？ D:\shots\screenshot.png
```

**这就是全部约定，它不是一种语法** —— 路径就是一句话里的一个路径，模型会为它调用
`attach_image`。没有要学的标记：人写的就是人的意思。一次调用支持三种来源 —— 本地路径、
`http(s)` URL 和 `data:` URI —— 也可以同时给几个，读不到的来源会在回答里被点名，而不是
让其余的失败。URL 交给 provider 去取，这里不取。

只在模型的配置在 `input` 下列了 `image` 时才成立：读不了图片的模型会被告知这一点，并且
说出来，而不是默默忽略你指名的东西。

图片只到模型为止。日志留下的是那句话和工具的答复（它点名了每一个文件），再加上一行注记
说明图片原本在那里、有多大 —— 所以重建出来的对话是文本，而一轮如果又需要那张图，就按
那个名字重新附一次。重新附就是重新发 —— 文件在提示词里被指名，所以一个月后回看的一轮
仍然说得清它讲的是哪张图。

**provider 不肯读的一张图，是唯一可能把整段对话一起带走的东西**，因为它拒绝的那些字节会
被之后每一轮重新发送，而每一轮都会以同样的方式失败。所以在那里死掉的一轮会把像素留下：
活着的对话丢掉它们，附上日志也保留的那行注记，并在错误里说明这件事。凡是提示词已经指名
过的东西，一样都没有丢。

## 上下文

一段对话不累积上下文，它**选择**一个。`slife2-context` 保存轮次日志 —— 每个
`(agent, subagent)` 一个 SQLite 文件，和以前完全一样 —— 并在每一轮之前做**一次模型
调用**，读手上的这段对话，回答两件事：已经在手的那几轮留下什么，以及从日志里召回什么。
这一轮就跑在这两者之上，按时间排序。

这次读的是整段对话而不是单独那条新消息，这不是细节：要被召回的那一轮通常是追问，而
「另一个呢」只有通过它上面的那些轮次才说得清自己指的是什么。它的回答是一次选择，绝不是
一次倾倒 —— 上下文是有界的，所以一次匹配到的东西超过容量时，返回的是放得下的那部分，
而 keep 列表就是模型说明自己还在用手中哪几轮的方式。它读到的每一轮都带一条 `[TURN:{…}]`
脚注，写明它的 id、它的 channel 和它的跨度，那些 id 就是从这里来的 —— 而进程自己跑过的
每一轮也有一条，在存入的那一刻写下，所以最新的轮次和恢复回来的一样可寻址。
按约定，大写就是提示：大写里的名字是 harness 自己的注解，从来不是记录的一部分；而读起来
像一个词的标记 —— 比如别人从微信发来的那一轮上的 `[Wechat:{…}]` —— 是某个人说过的，
和人说的话一起存着。

**模型能调用的任何东西都决定不了这件事。** 模型有 `turn_list` 和 `turn_read` —— 读自己
的历史正是日志的意义 —— 而那两个决定是 harness 的：`restore`，重放一段对话上次停下时
（进程重启过，或者空闲清理把它放掉了）所拥有的上下文；以及 `rebuild`，执行上面那个决定。
一次决定「手里这些原样保留」的 rebuild 什么都不重建，这是最常见的情况，也正是让一次这么
贵的调用不至于白花钱的原因。

它在 `slife2.yaml` 的 `context:` 下调参 —— 上下限是模型自己窗口的占比，召回的各项上限，
以及超时之后这一轮干脆用手上的上下文跑：

```yaml
context:
  ceiling: 0.8         # 保留的上下文以什么为衡量基准
  floor: 0.2
  recall_limit: 40
  min_similarity: 0.45 # 只把关*实测*相似度；关键词命中不受它管
  timeout: 20.0        # 判别器调用的超时，超过就按现有上下文跑
```

**这里失败不等于对话失败。** 判别器超时、provider 报错、回答不是要求的形状 —— 全都会
保留上下文并让这一轮跑下去。存储*不见了*是另一回事，它会让这一轮失败，和别的任何缺失的
plugin 一样。

## 工具

模型的工具清单 —— slife2 自带的，和别人在跑的 —— 由 **`slife2-toolhub`** 决定，而且
它是唯一做这个决定的进程。它不是*够到*那些 server 的进程：`mcp:` 归 `slife2-mcp-tools`
持有，`rest-api:` 归 `slife2-restapi-tools`，各自申报自己持有什么 —— 来源、它的工具、
它是否在应答 —— 而 hub 合并这些行、给它们把关、给它们命名，并把调用路由回去。一个
plugin 无论前面是别人的 server 还是根本没有连接，贡献方式都一样：一份 playbook 和一条
命令同样是被申报的行，这就是它们「可被找到但不可被调用」的方式。

**凭证和连接放在一起**，所以某个工具 server 的 key 由持有它那条配置项的 plugin 保存
—— `SERPER_API_KEY` 在 `slife2-mcp-tools`，provider 的 key 在需要它的模型 server，
`BAIDU_API_KEY` 在 `slife2-skills`。模型被*交到*手上的是它已经加载的那部分清单，也就是
配置之后的这一节：

### 配置

```yaml
# 下面每一节都既是手写的、也是模型写的：每个 family 提供编辑自己那一节的工具
# （`mcp_set`、`rest_api_set`、`cli_set`、`skill_set`，旁边还有 `remove` / `list` /
# `set_enabled`，用的都是 v1 的名字）。底部的 `providers:` 和 `default:` 由
# `model_set` / `model_remove` / `model_switch` 以同样的方式编辑，它们住在 agent
# server 上而不是自己的 family plugin 里（DESIGN.md §8）。两种方式下，写入都经过
# `slife2.libs.host.configfile` —— 单一写入者、文件的注释保留，而每次启动都读这个
# 文件的加载器就是裁判。

mcp:                                  # 别人写的 MCP server
  arxiv:
    url: https://arxiv.mcp.brunosan.de/mcp
  serper:
    command: npx                        # stdio：由 slife2 启动的进程
    args: [-y, serper-search-scrape-mcp-server]
    env:
      SERPER_API_KEY: ${SERPER_API_KEY}

rest-api:                               # OpenAPI 文档，一个端点一个工具
  github:
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.yaml
    base_url: https://api.github.com
    api_key: ${GITHUB_TOKEN}

cli:                                    # 这台机器上已经装好的程序
  yt-dlp:
    command: yt-dlp
    description: Download video from 1000+ sites, subtitles and playlists.
    install: uv pip install yt-dlp

skills:                                 # skills/ 里某份 playbook 被给到的东西
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}
    # enabled: false                    # 装了，但先放一边

tool_load:                              # 模型最多能持有多少个工具
  threshold: 100

tool_result:                            # 一次工具结果交给它多少
  tokens: 4000                          # 花在超长结果上，同时用它
                                        # 给 `dump_read` 封顶 —— 就这一个数，
                                        # 且至少 128：窗口比它自己开头那行
                                        # 地址还小的话，会被拒绝

context:                                # 一段对话的上下文如何决定
  ceiling: 0.8
  floor: 0.2
```

**没有 `wechat:` 节**：微信登录是扫出来的而不是配出来的，它住在
`agents/<agent>/wechat.yaml` —— 见上文「WeChat」。

`command` 启动一个进程并经由它的标准输入交谈；`url` 经网络连上别人的。一条 `rest-api`
配置项是同一件事的简短写法 —— 它会被展开成服务它的那条 `uvx mcp-openapi-proxy` 命令，
所以持有它的 plugin 只有一套机制而不是两套。`${VAR}` 与 provider 的 key 走同一条解析链。

`cli` 一项是个例外：没有进程要启，也没有 URL 要连，因为程序本来就装好了。写下来是为了
能让模型知道它存在 —— 而写在*这里*、写在运维者自己的文件里，是因为一条配置项本身就是
opt-in，跟 `mcp:` 下的一条配置项完全一样。命令最终不在 `PATH` 上时，告诉人的就是
`install`；`cli_set` 会在有人看着的时候检查这件事。**每一条配置项都是工具目录里的一行**
（`cli:yt-dlp`），所以 `tool_search` 能按它做什么而不是按它的名字找到一个命令 —— 可被
找到是已经落地的那一半。**还没有任何东西真的去运行它**：做这件事的工具是下一个改动
（DESIGN.md §9），在那之前 `func_tool_load` 说的正是这个。**`slife2-cli`** 申报这些行，
并提供写这一节的 `cli_*` 工具。

`skills/` 里的 playbook 以同样的方式进目录，而且*确实*可读 —— `skill_use`，由
**`slife2-skills`** 提供，与它一起的还有 `skill_list` 以及安装、移除、启停一份 playbook
的那些 `skill_*` 工具。它成为一个 plugin 是同一个理由，并且它持有 `skills:` 这一节解析
出来的东西，因为一份声明了 `requires.env` 的 skill 需要一个知道答案的进程。

**模型可以编辑这些节，读这一段就是为了这件事。** 四个 family 各自提供自己的管理工具 ——
`mcp_set` / `rest_api_set` / `cli_set` / `skill_set`，旁边是 `remove`、`list` 和
`set_enabled`，用的都是 v1 的名字 —— 所以「写在*这里*、写在运维者的文件里」不再等于
「运维者写的」。**第五个编辑者是模型配置本身**：`model_list` / `model_set` /
`model_remove` / `model_switch` 作用于 `providers:` 和顶层的 `default:`，由
`slife2-agent` 而不是某个 family plugin 提供 —— 把 provider 和模型变成一段对话所用模型
的正是 agent server，所以这次编辑和兑现它的那次读取是同一个进程（DESIGN.md §8）。`set`
写的是*整条*配置项而不是打补丁：漏写的字段就是被丢掉，而 `set_enabled` 才是那个只改一个
字段、其余照旧的操作。不变的是编辑所走的那条路：文件由一个模块
（`slife2.libs.host.configfile`）在跨进程锁下写入，作为一次保留全部注释的文档编辑，结果
交给和*启动*时同一批解析器和同一个加载器 —— 加载器拒绝的配置项会被放回去，并告诉调用方
为什么。所以能写进去的，就是运维者写得出来的，而且落在他们会写的地方。这里*还*没有的是
写入之前的一道闸：带 `command:` 的 `mcp_set` 指名了 slife2 将要启动的一个程序，而没有
任何东西先问一句（DESIGN.md §9，「Tool approval」）。

### 交到模型手上的是什么

工具的名字在有来源可带的时候会带上来它的 server：`mcp:` 下的一条配置项到模型那里是
`{name}__{tool}` —— `arxiv__arxiv_search_papers`。**我们自己的不带。** `turn_read` 和
`tool_search` 是 slife2 自己的工具，而 `context__turn_read` 等于把这套系统自己的安排
泄进了模型每次请求都要读的那唯一一样东西里：哪个 plugin 提供某个工具是关于我们的事实，
而 `servers()` 会把它报告给任何在调试的人。所以我们自己的工具是裸名，而且它们共用一个
命名空间 —— 两个 plugin 不能合计提供一个名字 —— 目录会大声拒绝，而不是替它做裁决。

**交到模型手上的是它已经加载的工具，不是存在的工具。** 这份清单在*每次模型调用*之前都
从 hub 重新读一遍，一开始就在里面的是 slife2 自己的：`tool_search`、`func_tool_load`、
`_func_tool_unload`、`skill_use`、两个历史工具、`attach_image`、harness 的
`_check_new_input`，以及编辑那五个配置节的二十二个工具 —— 最后这一组正是清单明显变长的
原因，也是唯一一组「常驻加载」值得再看一眼的。其余一切都按需加载 —— 一个有九十个工具的
server，在其中一个被需要之前不花任何代价。`tool_search` 同时按关键词*和*按语义搜索整个
目录，一次混合检索；`func_tool_load` 把一个或几个名字放进清单，它们从同一轮的下一步起
就在那里。目录是 `<data>/agents/tools.db`，整个数据目录一个文件而不是每个 agent 一个：
一段对话里加载的工具，下一段对话也是加载的，重启之后仍然是。

有两件事决定什么一开始就是加载的。**我们自己的永远加载** —— 一个悄悄丢掉了 `turn_read`
的模型，是没有人看得见的故障 —— 别人的则在它的配置项写了 `autoload: true` 时加载，这也
是一个「每一轮都要用到它的工具」的 server 被写下来的方式。其余一切都按需到来。

### 每个工具都接受的三个参数

**每个工具都接受三个模型可以加到任何一次调用上的元参数。** 它们不在任何工具自己的
schema 里 —— 它们是在每个 schema 出去的路上被加上去的（`slife2.libs.turn.tools.advertised`），
循环会把它们读走并在分发之前剥掉，所以没有任何工具 server 见过它们。

```yaml
# 来自模型的任意一次工具调用
{ "e": "6*7", "_timeout": 30 }              # 这一次调用可以花 30 s
{ "prompt": "a cat", "_async": true }        # 放到后台去跑
{ "path": "notes.md", "_approve": true }     # 先问一下用户
```

- **`_timeout`** —— 秒。它覆盖工具自己的默认值；`0` 或更小表示「用默认」，
  **绝不**表示「不限时」。一个自己声明了 `timeout` 参数的工具由它自己执行（一次调用
  恰好一个计时器）；两者都没有时，调用一直跑到它结束。这里**没有默认预算**：一个套在
  所有工具上的数字，对其中大多数都是错的。
- **`_async`** —— 放到后台跑。模型拿到一个 `task_id`，结果到达时会被**交回这段对话**，
  戴着 `[AgentToolResult:{"call_id":…,"tool":…,"task_id":…,"status":…}]`；窗口把它
  画成 `async-tool:<tool>` channel 上的一条消息。结果和别的结果一样要过
  `tool_result: tokens:` —— 放到后台是让这次调用不必*等*，不是让它不受这个上限 ——
  所以超大的输出到达时是一个窗口，其余部分用 `dump_read` 读。`check_async` 回答这次调用*现在*在哪
  （在跑、已结束、失败、已取消），`cancel_async` 放弃它。**agent task 自己的循环没有
  `_async`** —— 它只跑一轮，没有地方可以把结果交回去 —— 它会这么说，而不是假装可以
  （DESIGN.md §9）。
- **`_approve`** —— 调用跑之前先问用户。终端画一行内联的行（`y` 批准，`n`/`Esc` 拒绝），
  并用 `answer_approval` 作答；循环等着那个回答，被拒绝就拒绝这次调用，不挂任何工具面板。
  **背后没有人的 channel 不会问，调用直接跑** —— 微信、定时运行、另一个 agent，以及任何
  没有被教会画这个提示的客户端（一个*能*回答的调用方会用
  `send_message(..., watched=true)` 说明这一点，终端就是这么做的）。

### 预算，以及 harness 写下的那几对

清单是有界的，因为它随每一次请求出去：超过 `tool_load: threshold:`（`slife2.yaml` 里的
100）时，最近最少被*调用*的工具会被卸载，我们自己的和 `autoload` 的除外。算数的是调用
而不是加载 —— 模型整轮都在用的工具，比刚被拿进来的活得久 —— 而一个加载之后从未被调用过
的工具，按它被加载的时间排序。这次裁剪发生在轮的边界上 —— harness 在存下这一轮之前调用
`_func_tool_unload`，那些名字会回来，所以模型刚刚失去的东西是日志说得出来的，而不是没有
任何东西注意到的事。**而且模型也读得到它**：这次裁剪被写成一次工具调用和它的结果进入
对话，所以一个清单缩水了的模型是从对话记录里知道的，而不是伸手去拿一个已经不存在的工具
才知道。什么都没拿走时，什么都不写。被逐出的代价是一次搜索和一次加载，不是一种能力。

**一轮正在跑时到达的消息会被交给那一轮。** 在下一个步骤边界，harness 调用
`_check_new_input`，把这次调用和这条消息自己的话写进正在跑的那一轮 —— v1 的 *cut-in*，
始终开启 —— 所以 agent 干活时你说的话，在它到达的那一轮之内就传到了，而不是晚一轮。
和裁剪一样，它是一对工具调用：它在记录里，也在之后每一次请求里，模型把它读成「它干活时
到达的东西」。**还没有任何屏幕展示它。** 那是一个推迟的显示决定，而不是一个做了一半的
机制 —— 对话记录从事件里画，而这次调用不产生任何事件，重建出来的那份则按名字跳过 `_`
开头的调用（DESIGN.md §9）—— 而无论哪种情况，这段对话都拥有它。

### 一个 plugin 如何贡献

一个 plugin 的哪些工具模型可以调用，是写在工具上的 —— `@mcp.tool(meta=FOR_THE_MODEL)`，
`skill_use` 带着它，而上下文存储的 `remember` 没有。一个 plugin 的工具在其中一个开口说
之前，都是它自己代码的，所以你忘了标记的工具是不可见的，而不是危险的。

**hub 够得到我们的 plugin，对其它一切则是被*告知*的。** 有两类东西到达它。一个 plugin
的**自有工具**来自 `tools/list`，就是它回答任何人的方式。一个 plugin 的**来源**则是被
申报的：`mcp:` 的一条、`rest-api:` 的一条、一份 playbook、一条命令 —— hub 一个都够不到
—— 由拥有那一节的 plugin 在一次 `list_sources` 调用里替它们作答，hub 合并这些行，并把
每次调用路由回持有它们的 plugin。所以这个进程不持有任何通往别人运行的 server 的连接，
也不持有属于它的 key：`slife2-mcp-tools` 和 `slife2-restapi-tools` 持有那些，一条配置项
一个连接，而 hub 拥有工具*集合*，别的什么都不拥有。

**一次申报就是整份清单，而且每次搜索之前都会再问一遍。** 整份清单是「被删掉的一条不再
是命中」的原因 —— 合并把缺席的名字读成来源已经没有的那一行 —— 再问一遍则是「把目录丢进
`skills/`、或者改配置，在下次搜索就生效而不是下次重启才生效」的原因。无论哪种方式，hub
都是目录唯一的写入者：来源申报，hub 合并。

hub 自己的工具就是剩下的那些，而它们恰好是集合层面的活。
**`tool_search`、`func_tool_load` 和 `_func_tool_unload`** 由 hub 提供，因为它们每一个
都是关于*整个*目录的问题 —— 存在什么、模型正拿着什么、预算收回了什么 —— 所以拥有这个
集合的进程来回答，别的 plugin 回答不了。它们的名字不带 server，因为没有 server 可带，
我们自己的每一个工具都是这么命名的。

**`skill_use`** 已经不再是它们中的一员：**`slife2-skills`** 提供它，而 `slife2-cli`
拥有 `cli:` 那一节。两者都是和其它 plugin 一样的 plugin，理由从另一端看更容易明白 ——
每个 family 都还有一个面向模型的工具没做（DESIGN.md §9），而工具需要一个 server 才能被
提供。一份 skill 是 `<data>/skills/` 里的一份文档，工具去读它：
`skill_use(name="browser-harness")` 返回那份 skill 的 `SKILL.md`，前面带上它所在的目录，
这样正文里的路径才有意义。安装一份 skill 就是把一个目录放进那个文件夹 —— 工具每次调用
都读磁盘，所以没有东西需要重启。

**一份 skill 也是一行**（`skill:browser-harness`），每一条 `cli:` 配置项也是 —— 这正是
让 `tool_search` 能回答「关于浏览器我能做什么」的原因，而不必模型已经知道要按名字
问 `browser-harness`：被搜索索引的是 playbook 的整份文档，而「drive a browser」能命中
它就是全部意义。这一行不是一个工具：它没有加载状态，从不进入模型的清单，调用它得到的
回答是真正够得到那东西的那一步（`skill_use`），而不是一次拒绝。名字带命名空间是因为
`browser-harness` 既是一条命令*又是*记录它的那份 skill，而一个名字就是一行。

**模型可以调用的一切都是工具目录里的一行**，hub 自己那三个也算。hub 决定工具是*什么*
—— 哪些来源、上面的命名规则、谁可以调用 —— 而记录是 hub 自己打开的一个文件，
`<data>/agents/tools.db`，经由 `slife2.libs.store.catalogue.ToolStore`：行加上建在它们
上面的两个索引，用的就是轮次用的那套机制，因为一个存储只有一个写入者，而单一写入者本来
就是 SQLite 文件。它回答关于这些行的问题 —— 哪些是加载的、某一个在远端叫什么、以及那两
条搜索腿 —— 里面没有任何东西知道什么是代理名。

一份 skill 是一份文档，它仍然可能需要一个 key：`baidu-search` 在自己的头部声明了
`BAIDU_API_KEY`，而它的第一条指令就要跑一个没有它就会死的脚本。值来自 `skills:` ——
和别的每一个密钥走同一条链，这正是把它写下来的意义，因为一个几天前启动的守护进程从来
没见过你今天早上 export 的 key。

```yaml
skills:
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}   # shell env → credstore
```

`skill_use` 会把这个差别报出来：读一份 key 没有配置的 skill，回答会开门见山地说出来，
而不是让模型等到它被告知要跑的那条命令失败时才发现。什么都没声明的 skill 什么都不说。
目前还没有任何东西真的去跑那些脚本（DESIGN.md §9）—— 而这正是这个检查属于读取者的原因。

`enabled: false` 让一条配置项保持配置好但永不连接，这是值得知道的那个杠杆：凡是启用的，
启动时都是一个进程、每次请求里都是它的工具。`servers()` 把它报告成 `off`，所以「我的
工具为什么不见了」连一条在跑起来之前就被关掉的配置项都有答案 —— 而它开着时提供的那些
工具留在目录里，标成 `disabled`，所以搜索仍然找得到它们。`slife2 down` 把这些再推进一步
—— 它停掉的一个 plugin，会带走那个 plugin 启动的子进程。

plugin 是必需的，而 `mcp:` 里的一切都是可选的：hub 向每个 plugin 要一份工具清单，答不上
来的那个会让这一轮失败，而不是悄悄少几个工具继续跑；而别人的 server 会被报告出来并排除
在外。一个悄悄丢掉了 `turn_read` 的模型是没有人看得见的故障，而这就是这条规则值得存在的
原因。

### 媒体

一个配置节背后是四种能力：**画一张图、渲染一段视频、开口说一句话、把一段音频里说的
写下来**。

```yaml
media:
  defaults:                              # 不指名时一次调用拿到哪个模型
    image: bailian_personal/wan2.7-image
    video: bailian_personal/happyhorse-1.1-t2v
  providers:
    bailian_personal:
      api: dashscope-aigc                # 说的是协议，不是品牌
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1
      api_key: ${QWEN_TOKEN_PLAN_PERSONAL}
      models:
        - { model: wan2.7-image, kind: image }
        - { model: happyhorse-1.1-t2v, kind: video, params: { resolution: 720P, duration: 5 } }
        - { model: qwen-audio-3.0-tts-plus, kind: tts, voice: longanhuan_v3.6 }
        - { model: qwen-audio-3.0-asr-flash, kind: asr }
```

**一个模型是*干什么用的*，是关于这个模型的事实**，而这是它与 `providers:` 和
`embeddings:` 唯一不同之处 —— 一个厂商可能四种都提供，它的配置项说明哪个是哪个，所以
「换到便宜的那个画图模型」是一次配置编辑，而不是换一种调用。`defaults:` 是一次不指名
任何模型的调用拿到哪个，而一次调用永远可以指名自己的。

**`api:` 选定协议**，目前有两种：`dashscope-aigc`（百炼：图片、视频、语音和转写）和
`openai-images`（OpenAI 兼容的 `/images/generations`，它只会画图，另外三种会按名字拒绝
—— 比厂商回一个 404 更好）。

生成的文件写在 **`<data>/agents/<agent>/media/`** 下，别处不写。按 agent 分是因为里面
装的是那个 agent 生成的东西；放在 `agents/` 下而不是 `runtime/` 下，是因为守护进程的
工作目录在文档里被定义为可重建的 —— 一张生成好的图留在那里，就会是那个注定会丢的文件。
它们是工作产物而不是文件柜文档：没有任何东西登记它们，回答是一个路径。`folder=` 把它们
归进那个根目录*内部*的子目录，而指向它外部的路径会被拒绝，所以模型不能决定一个文件被
写到你磁盘上的哪里。一段视频可能要几分钟，而等待是有界的：超过 `timeout` 之后，调用会
带着 provider 的作业 id 被拒绝，而不是带着它的结果 —— 因为渲染在那边继续进行，再问一次
就是为它付两次钱。

### 文件隧道

**给一个本地文件一个公开的 HTTPS URL**，好让读不到你磁盘的东西也能取到它 —— 一个被问
到某张图的视觉模型，一个只接受 URL、别的什么都不接受的聊天 API。

```
expose_local_image(path)      → https://<tunnel>/share/<token>
unexpose_local_image(token)   → 那个 URL 失效
```

文件不会被移动也不会被复制：它就放在原地被提供，只要隧道还在。链接在**文件改变**的那一刻
失效（所以一条链接底下被换掉的路径，绝不可能把新字节送给拿着旧链接的人），进程退出时也
失效。私钥、`.env` 或保存下来的密码会被按名字拒绝 —— 隧道还在时分享 URL 就是公开的，
那等于把它发布出去。

**隧道是别人的进程，配置说明是谁的。** 三个 provider，恰好一个生效，其余保持配置好而
不参与：

```yaml
file-tunnel:
  active: cloudflare
  providers:
    ngrok:        { token: ${NGROK_AUTHTOKEN} }
    localhost.run: { binary: ${SSH_BINARY:-ssh}, user: nokey, remote_port: 80 }
    cloudflare:   { binary: ${CLOUDFLARED_BINARY:-cloudflared}, protocol: http2 }
```

| provider | 需要什么 | URL | *浏览器*看到什么 |
|---|---|---|---|
| `ngrok` | 一份 `NGROK_AUTHTOKEN` 凭证 | 每个账号一个固定的 dev domain | 免费档有一个插页式提示页（ngrok 对任何 `Mozilla` User-Agent 都回它，并且禁止在服务端关掉） |
| `localhost.run` | 一个 `ssh` 客户端 —— Windows 10+ 自带 | 一个每几小时轮换一次的随机 `*.lhr.life` 主机名 | 文件本身；TLS 在它们那一端终止。字节由第三方中转 |
| `cloudflare` | `cloudflared` 二进制，没有任何东西会替你装它 | 一个在进程生命周期内稳定的 `*.trycloudflare.com` 主机名 | 文件本身 |

程序化的抓取 —— 也就是它存在的理由，多模态 LLM 那条路 —— 不受 ngrok 提示页影响，这就是
什么都没配置时 ngrok 是默认值的原因。上面这份配置出厂时 **`cloudflare` 生效**，因为它
不需要账号，而且不把字节中转给任何人；如果你没装 `cloudflared`，把 `active:` 改成
`localhost.run`，Windows 自带的 OpenSSH 客户端就是全部要求。**没有起来的隧道，或者发布了
一个没人够得到的 URL 的隧道，会被拒绝而不是被发出去**：一条回 HTTP 530 的链接比一个错误
更糟，因为取不到它的模型说不出为什么。细节在 `tunnel_status` 里 —— 状态、URL、是否可达、
它正在拨的边缘地址，以及正在跑它的那个程序的路径。

### 文件柜

你自己的笔记、日记、存下来的文件和报告，放在一个地方，搜索一次全都能覆盖，人也可以在
编辑器里直接读。四种，它们的区别在于一份*文档*是什么：

| 种类 | 以什么为键 | 第二次写入 |
|---|---|---|
| **note** | 你选的一个主题 | 替换它 |
| **diary** | 日期 | 追加一节 |
| **file** | 它在文件柜里的路径 | 把文件复制进来 |
| **report** | 一个标题 | 追加一节 |

笔记和存下来的文件是文档 —— 第二次写入*就是*它的内容 —— 而一天和一个标题是日志，因为
同一个日期下的两条记录是关于它的两个事实，后一段文字里没有任何东西说明它替换了前一段的
哪一部分。这就是为什么只有那两种文档有删除工具。

**第五种不是你的。** 一份大到放不进对话的工具输出会整份留在这里 —— 一个 **dump**，以
产生它的那次调用为键 —— 而对话携带的是那份输出自己若干行的一个选集，在
`tool_result: tokens:` 之内。什么都不切掉，也什么都不摘要：给模型看的行是工具自己的话，
是在这段对话正在做什么的光照下选出来的，别的一切离一次 `dump_read` 之遥。**`dump_read`
是一个会搜索的读取器**，因为一份大输出需要的正是这个：给它一个词，它找出那些行以及它们
周围的行；给它一个行号，它从那里往下读（`-1` 是最后一行）；给它一个数量，它少读一点。
每个回答都带行号，并且始终在同一个窗口内，所以把一份输出读回来，代价永远不会超过这段
对话本来就要付的 —— 而一次没有指名 `dump` 的搜索看不到 dump，因为机器输出不是你的笔记
应该被拿来比较排序的东西。

**一切都是写两遍的**，其中一遍是给你的：行是 `cabinet_search` 读的，同一份文档还会作为
markdown 落到 `agents/<agent>/cabinet/` 下 —— `notes/Python.md`、`diary/2026-10-11.md`、
`reports/weekly.md` —— 那就是你不必问任何人就能打开、编辑或删除的东西。存下来的文件的
字节也在那里（`<category>/`），而且从不进索引：`file_read` 返回一个文件的*文本*，遇到
PDF 或图片会说出它是什么而拒绝，而不是把它解码成一整页替换字符。别的编码的文本会被读出
来，并把编码写在第一行，因为*静默*的错误解码是唯一值得拒绝的结果。

**`cabinet_search` 接受词或者句子**，和 `tool_search` 接受的同样两种输入：`keywords` 是
必须全部出现的词（一个名字、一个路径、一个符号），`sentences` 是按语义匹配的问题 ——
几个就是几个问题，而每一个都只可能增加结果。一个文件没有自己的文本，所以让它可以按
它*是*什么被找到，靠的是 `cabinet_summarize` —— 给一个文件写摘要会把它重新嵌入一遍，
因为它的描述是构建它那个向量时所用材料的一部分。

有两件事它不做，都是按名字说的：**`file_save` 不抓取 URL**（它复制这台机器上已经有的
文件 —— v1 的 URL 那一半带着一个 SSRF 防护，那是它自己的一个改动），而**没有 `grep`
腿**（DESIGN.md §9 把 v1 的第三种搜索模式推迟到整个系统层面，文件柜继承这一点，而不是
长出自己的第二个正则引擎）。

### 微信

`slife2-wechat` 每个 agent 连一个微信账号，经由 iLink 托管的网桥。它没有任何东西需要
配置：登录是*扫*出来的，所以 `wechat_login` 把一张二维码放进模型的回复里，有人用微信
App 去扫它，回来的 token 存进 `agents/<agent>/wechat.yaml`，下次启动就从那里取。一次
登录大约持续一天；`wechat_check_status` 说还剩多少。

**一个微信号只有一个活着的登录。** 用同一部手机给第二个 agent（或者在第二台机器上）扫码，
原先那个登录会被网桥悄悄作废：从那以后它得到的回答是 `session timeout`，所以它发出去的
东西都到不了。slife2 把这个回答读成它本来的意思，这就是为什么第一个 agent 的窗口会说
`[✗ Wechat offline]`，而它的 `wechat_check_status` 说得清为什么；修法是再扫一次码，把
账号拿回来。

四个工具，模型四个都会用：

| 工具 | 做什么 |
|---|---|
| `wechat_login` | 取一张二维码，并开始盯着这次扫码 |
| `wechat_check_status` | 连上了吗？还剩多久？以及**该回给谁** |
| `wechat_send_message` | 给某个联系人发一条文本 |
| `wechat_logout` | 忘掉这个 agent 的登录 |

**这条链路自己的来去是窗口里的一行**，不是一轮：扫码落地时是 `[⚡ Wechat online]`，
登录掉线或被忘掉时是 `[✗ Wechat offline]` —— 和 A2A 网格过来的 `[⚡ jill online]`
同一个形状，因为两者说的都是关于一段对话的同一件事：它现在够得到什么。*启动时接上*的
登录什么都不说，因为窗口展示的是你看着的时候发生了什么，而不是进程启动过。*原因*留在
`wechat_check_status` 里，那是模型去问的地方。

别人发来的一条消息，作为**那个 agent 自己对话的一轮到达 —— 就在窗口里**。plugin 把它
交给正在展示那段对话的终端，终端像它是被敲进去的一样运行它：同一个队列、同一轮、同一份
对话记录。所以从手机发出的消息出现在屏幕上，它的回答在那里流式写出来，之后两者都在记录
里。

为了做到这一点，消息发给 **`slife2-tui`**，终端自己的收件箱：一个和其它任何一个一样的
plugin —— `servers:` 下的一个地址，由 `slife2` 启动并共享，由 `slife2 status` 列出
—— 它保持**每个 agent 一个会话**。窗口打开时附加上去，并在展示一段对话期间一直持有那次
调用，这才让一次调用说得出它指的是*哪一个*终端；两个窗口同时开着就是两个会话，谁也不比
谁特殊（DESIGN.md §4）。

这一轮以 `channel=wechat` 记录，并带一个说明是谁发来的
`[Wechat:{"peer_wechat_id": …}]` 前缀。这个名字故意不用大写 —— 这是约定而不是规则：
**大写标记的是不属于这段对话的注解**，`[TURN:{…}]` 就是这种，也就是一轮在模型把上下文
读回来时携带的那条脚注（构建清单时加上，从不存储）—— 而这个标记是消息的一部分，和消息
一起进这一轮的日志。没有任何东西检查大小写；它是写给人读的。窗口展示的是 channel 而不是
标记：这一行戴的是 `Wechat> ` 而不是 `You> `，标记里的两个 id 被拿掉，因为它们是回复
需要的，不是读者需要的。回复是模型的事，而且只是模型的事：这里没有任何东西自动派发一条
回复，所以什么都没说的一轮就什么都不发 —— 用联系人加上标记里带的 `context_token` 调用
`wechat_send_message`。这一轮跑的时候，对面那个人看到的是*正在输入…*，回复发出时它停下。

每个 agent 各自登录，有自己的账号、自己的文件、自己的轮询循环；plugin 是一个共享进程
（DESIGN.md §1.2）。没有 `wechat:` 节要写：登录是扫出来的而不是配出来的，而
`agents/<agent>/wechat.yaml` 这些文件就是 plugin 恢复的全部内容。一台不该跟微信说话的
机器，就是一台没有人去扫码的机器。

### Agent task

模型不再调用工具时，一轮就结束了，所以任何耗时的事情都会把那一轮一直挂着：人看着一个
什么都敲不进去的提示符。**agent task** 就是那件活跑到别处去了 —— 一个属于这个 agent
自己的循环，带着一个名字和一段描述启动，稍后由一条消息在同一段对话里被回答。

模型有四个操作它们的工具，而**其中没有一个会等**：

| 工具 | 做什么 |
|---|---|
| `agent_task_start` | 启动一个，立刻带着它的 id 回答 |
| `agent_task_status` | 某一个在干什么 —— 从来不是它的结果，结果自己会来 |
| `agent_task_list` | 这个 agent 有哪些，在跑的排在前面 |
| `agent_task_cancel` | 停掉一个，回答它*在调用之后*的状态 |

```
agent_task_start(name="research", description="Find the retry policy, and say what it is")
→ {"task_id": "agent-task:research", "name": "research", "status": "running", ...}
```

**它以这个 agent 的身份、在这个 agent 自己的上下文上运行。** 同一份系统提示词、同一个
模型、同一批工具 —— 而 `restore` 和 `rebuild` 用的是这个 agent 自己的键，所以一个 task
是从这段对话一直在谈什么开始的，而不是从空开始。它*没有*的是启动它的那一轮：`restore`
读的是*已存储*的清单，而一轮是在它结束时才被写下的，所以最后那次交流必须在那段描述里
重述。没有任何东西代表它被记录 —— 回答作为一条消息抵达这段对话，这就是记录得到的东西
—— 而它自己的回复是自行交回的，所以没有什么要轮询，也没有办法去取一份。

回答在 `agent-task:<name>` channel 上到达，戴着 `[AgentTaskResult:{…}]` —— 那个 task 的
id、名字、状态和时间 —— 所以窗口显示 `AsyncTask(research)> `，而模型知道了它下次调用
需要的那个 id。它去的是正在展示那段对话的终端，别处不去：没有窗口就没有对话在跑，于是
回答是 `undelivered`，日志也这么说，而不是为没人跑一轮。

```yaml
agent-task:
  max_running: 4   # 每个 agent 同时运行的 task 数
```

**那是 task 拥有的唯一一个键。** 构成它的其它一切来自上面的 `agent:`，因为一个 task
*就是*这个 agent 在干活 —— 这个文件里唯一一节有两个读者。`max_running` 是这个特性唯一
的一条界：工具集合是那唯一一份目录，没有任何东西对它做过滤，所以一个 task 可以启动一个
task，由此无界的是随时间发生了多少活，而不是同时发生多少（DESIGN.md §9）。

### 定时任务

**这里是唯一会在它启动的那一刻开始干没人要求它干的活的东西。** 一个定时任务是一个名字、
一段描述和一条 cron 表达式，由模型写下；从那以后 `slife2-schedule` 触发它，到期时 agent
会拿到这个任务，作为它自己的一轮，在正在展示那段对话的终端里。**那一轮只有一条指令**：
用它携带的到期时间调用 `schedule_run_now`。一次没有人接的触发 —— 没有窗口在展示那个
agent —— 会以 `failed` 结束这次运行并记下原因，而不是让它留在 `pending`：账本是回答
「它跑了吗」的地方。一个已经到期的任务是**不由这个 agent 决定推掉的**；
`schedule_run_skip` 是给那些压根没有发生的运行准备的 —— 而一次**正在跑**的运行会被
拒绝而不是被关掉：关掉它等于活儿还在干、却没有任何地方记着这件事。那一种要用
`agent_task_cancel` 收尾，随后 `reconcile` 会带着原因把这次运行记为 `failed`。

| 工具 | 做什么 |
|---|---|
| `schedule_set` | 写下一个任务（或替换一个）—— 名字、描述、cron、时区 |
| `schedule_remove` | 忘掉它，以及它做过的所有事 |
| `schedule_list` | 有什么被排上了，每个下次什么时候到期 |
| `schedule_runs` | 它实际做过什么 —— 一次触发一行 |
| `schedule_run_now` | 现在就启动这次运行的工作；带上 `due_at` 则是补做那一次 |
| `schedule_run_skip` | 关掉一次不会发生的运行，并说明原因 |

```
schedule_set(name="daily", description="Write the day's entry from the turns.",
             cron="0 22 * * *", timezone="Asia/Shanghai")
→ {"name": "daily", "cron": "0 22 * * *", "next_run": "2026-10-12T22:00:00+08:00", ...}
```

**`schedule_run_now` 是 plugin 在启动这件活，不是 agent 在做它。** 这次调用把运行交给
`agent-task`，作为一个属于这个 agent 自己的 task —— 同样的提示词、模型和工具，同样的
上下文 —— 并立刻返回。因为调用方是 *plugin*，它拿到了这个 task 的 id，所以时钟自己的
轮询可以问它后来怎么样了：`finished` 会以 task 自己的 `finished_at` 把这次运行关成
`ran`，一次失败或一次取消会把它关成 `failed`，不需要在之后某一轮里记住任何东西。回答
本身由 `agent-task` 交回这段对话，像任何别的后台 task 一样戴着 `[AgentTaskResult:…]`。

**账本就是这个特性。** 一次*运行*是一次触发，用它**到期**的那一刻命名而不是用它跑的
时刻，它的状态在 `pending → ran | failed | missed | skipped` 之间迁移。这一行在触发被
交出去之前就写下了，所以一次触发不可能在下次轮询时重复；一次被拒绝的交接把这次运行标成
`failed`；一次没有回来的则让它留在 `pending`，因为一次命运未知的运行不能被报告成两者中
的任何一种。一次工作**没能启动**的运行，会带着原因被关成 `failed`，而不是敞着口、后面
什么都没有。

**任务只在 slife2 运行期间触发**，而错过的东西不会被藏起来：启动时，清扫会把一个停掉的
进程开着口留下的一切判为失败，轮询则记录每个任务错过的最新一次触发 ——
`schedule_runs(status="missed")` 就是你找它们的地方 —— 于是这次运行可以用
`schedule_run_now(name, due_at)` 补做，或者用 `schedule_run_skip` 关掉。没有任何东西
宣告它们：**触发是时钟对一段对话说过的唯一一样东西**，而账本是回答「它跑了吗」的地方
（DESIGN.md §1.7、§9）。触发也不戴任何标记，因为标记这套词汇是用来把*结果*和产生它的
东西对应起来的，而触发不是结果 —— `schedule:<task>` channel 携带的是哪个任务，话里说的
则是**这次运行记在谁的账上**，而这是唯一能把「送错对话的触发」和「读者自己的触发」区分
开的东西。

账本在 `<data>/agents/<agent>/schedule.db`，而且**没有配置节** —— 一个地址就是这个
plugin 被写下的全部。一个任务的 `timezone` 是 IANA 名字，不写就意味着这台机器的时钟
*在现在这一刻*的样子，它不会跟着夏令时的切换走。

```yaml
# 没有什么要配置的：一台不该跑定时任务的机器，就是一台没有人去写任务的机器
```

### A2A 网格

**基于 MQTT 的 agent 间通信**，跑在公开发布的 A2A-over-MQTT profile 上 —— 所以任何
别的会说它的东西都能跟你说话。协议、主题、agent card 和任务生命周期都属于那个 SDK；
这里是驱动和包在它外面的 plugin。v1 的网格，移植过来的。

| 工具 | 做什么 |
|---|---|
| `a2a_send_message` | 发一个任务、完成别人发给你的一个，或者只是聊两句 —— 由 `message_type` 决定 |
| `a2a_cancel_task` | 撤回一个你发出的任务 |
| `a2a_list_agents` | 花名册：你排第一，然后是每一个同伴，按名字 |
| `a2a_broadcast` | 给每一个同伴发一个发了就不管的事件 |

```
a2a_send_message(agent="jill", message="Summarise the release notes.")
→ {"peer": "jill", "task_id": "61ab0c8a…", "message_type": "task_request",
   "note": "The result will arrive in this conversation."}
```

**什么都不等。** 一次 `task_request` 立刻返回一个 id，结果稍后自己作为一条戴着
`[PeerAgent:{"from": "jill", "type": "task_response", "task_id": "…"}]` 的消息到达。
没有什么要轮询。`task_response` 完成一个*你*收到的任务，用的是它自己那个标记里的
`task_id`；方向在 `from` 里，从来不在 `agent_name` 里，因为那个词是这个 agent 自己的
身份。收到的消息在窗口里读作 `PeerAgent(jill)> `，标记被拿掉了。

**一个同伴可能在这台机器上，也可能在另一台上**，而没有任何东西区分它们 —— 本地 agent
只是「它的网格由这个进程持有」的那一个，以自己的名字发布到 broker 上。**每个 agent 一张
网格**，因为 agent 的名字*就是*网格身份：MQTT 的 client id 和请求主题的最后一段。于是
一台机器上的两个 agent 就是两个同伴，而一个 agent 从两个地方被够到，正是那个名字冲突
检查在加入之前就拒绝掉的情况，而不是事后去争。

**来和去是窗口里的一行，不是一轮。** 一个同伴到达时显示成 `[⚡ jill online]`，就在
`[restored 3 turns]` 和 `[kept 12 turns, recalled 3]` 旁边 —— 同一个形状，因为它们是同
一类东西：harness 做的事，而不是这段对话里谁说的话。什么都不跑，模型也不被告知：花名册
是 `a2a_list_agents` 的回答，而一个想被回答的同伴会发一个任务过来。

**有人在当这个 agent 的时候，它才在网格上。** 窗口在它的 agent 活着期间持有一个调用，
所以上线和下线跟随的是真的有人在那里 —— 看着这个 agent 到达的同伴，也会看着它离开，
而这中间没有什么要发。`slife2 --headless` 在完全没有窗口的情况下持有同一个调用，这正是
一台无人值守的机器需要的。一次 `a2a_*` 调用也会让它的 agent 加入并保持加入：一次发送
后面跟着的是这个 agent 正在等的回复。`<data>/agents/<agent>/a2a.yaml` 那本账*不是*入网
登记 —— 它记录的是一个进程死掉时攥在手里的入站任务，而这是重启时区分「一个孤儿」和
「一个它从没见过的 id」所需要的东西。

**broker 不在的话会替你启动，你结束的时候会被停掉**，这是它和 v1 分道扬镳的地方。把
`a2a.broker` 指向一个地址；如果那里没有东西在听、而它又是*这*台机器，`mosquitto` 会被
在那上面启动 —— 最后一个退出的 `slife2` 会再把它停掉（或者 `slife2 down`，或者
`slife2 status` 去看它）。只有 *slife2 启动的* broker 才会被停掉：它启动时写下的那条
记录就是许可的全部，所以一个系统装的 mosquitto 或者你自己跑起来的那个不会被碰。一个
不肯服务的 broker —— 或者一个指名在另一台机器上、而那台机器挂了 —— 会让启动失败，点名
它并展示它的日志，而不是把你留在四个接不到任何东西的工具上。`provider: mosquitto` 是
唯一的值；别的任何东西都是配置错误，而不是一个被悄悄关掉的特性。

```yaml
a2a:
  provider: mosquitto
  broker:
    host: localhost      # 另一台机器也是这样够到的，而且从不在本地启动
    port: 1883           # —— 那台主机上的 broker 是他们的
  org: default           # 主题命名空间：一个 broker 上的两组由
  unit: default          # 这两个来区分
```

**一次重启会把所有在飞的任务变成孤儿**，因为完成用的网桥和同伴的回复主题都住在那个死掉
的进程里。账本在这些任务和从没见过的 id 之间划出界线，而 agent 会**被告知一次**，在发现
它们的那次启动里，好让它能用一条普通消息回答每一个同伴 —— 那是这些任务还能得到的唯一
回复。**整个省掉 `a2a:` 节**就不在网格上：一台不该在网上的机器，就是一台没有人去写它的
机器。

## 东西都放在哪里

**两个地方，因为有两类东西。** 一个 **home** 就是一个数据目录 —— 配置、playbooks，以及
数据住在里面的那些 agents —— 而 home 就是 `--data-dir` 指名的东西：

```
<data>/                            --data-dir DIR，或者 $SLIFE2_DATA_DIR
  slife2.yaml                      配置；不在就是默认值
  skills/                          <name>/SKILL.md —— playbooks，你写的
  agents/                          属于某一个 agent 的一切
    tools.db                       工具目录 —— 那个共享的文件
    <agent>/                       一个目录就是某个 agent 的全部
      turns.db                     它的对话，以及它跑在什么之上
      schedule.db                  它排了什么，以及每一次运行
      cabinet/                     作为 markdown 的 notes/、diary/、reports/，
                                   以及按类别存的文件，里面还有 `.index.db`
                                   （搜索索引）
      media/                       它生成的东西
      wechat.yaml                  它的微信登录（约 24h，扫码时写下）
      a2a.yaml                     它的 A2A 账本：那些完成用的网桥由本进程
                                   持有的入站任务
      a2a-subagent.yaml            本地那张网的同一本账 —— 那是另一个进程，
                                   所以是另一个文件
```

**agent 目录里的任何东西都不重复它的名字** —— 目录本身已经说了它是谁的 —— 而目录是
那条规则的例外，也正因如此印证了它：它坐在各个 agent 目录旁边，因为它属于所有人，因此
不属于任何人。

**一个目录装有数据在它里面的那些 agents。** 一个目录可以装好几个，它们于是共享它的
`slife2.yaml` 和它的目录 —— 工具集合**就是**配置，所以跑在同一份配置上的 agents 跑在
同一批工具上，一个加载的工具对下一个也是加载的。`--agent jack --data-dir D` 既命名了
这个 agent，*也*说明了它的文件去哪里，而这是两个独立的事实：名字是这个 agent 是谁 ——
这台机器上一个名字一个活着的实例，无论在哪个目录 —— 而 `D` 是那次运行所读写的 home，
它的配置、目录和对话都在那里。一个名字上次在哪个目录启动过是不被记住的：它下一次运行用
的是这次给它的那个。

**一个 home 是你把一个 agent 启动在它里面时形成的。** `slife2 --data-dir D` 把 `D`
记录下来，在 `agents/` 和 `skills/` 不在的时候把它们建出来，并构建它的配置指名的工具
目录 —— 所以 `D/agents/tools.db` 从第一次启动起就在那里，而不是从你发出的第一条消息起。
**两个数据目录就有两份目录**，各自由自己的 `slife2.yaml` 构建：在一个里加载的工具不会在
另一个里加载，两者也不会被提供对方的工具。

**一个外部 server 是一个名字一个 server，无论多少个 home 指名了它。** 一条 `mcp:` 配置
项是*这台机器上*的一个进程，所以两个都配了 `serper` 的数据目录 —— 也就是复制一份配置会
产生的结果 —— 在它们之间得到的是一个 `serper` 而不是各一个，它的工具出现在两份目录里。
两个 home 用不同的命令指名同一条配置项，是日志会报告的一种分歧：名字才是让它成为同一个
server 的东西，所以正在跑的那个会被用上，给它另一个名字才是同时跑两个的办法。它们全都
会在最后一个实例把整套服务带下来时停下来，和 plugin 持有的其它一切一样。

**那个文件夹在哪里取决于你在跑什么。** 在源码检出里它就是检出本身 —— 这正是让你面前这份
`slife2.yaml` 就是正在用的那一份的原因 —— 否则它是 `~/.slife2`，按用户分，而且与命令
恰好在哪里启动无关。`--data-dir DIR`（或 `$SLIFE2_DATA_DIR`）把两者都覆盖掉。

**各个 server 是这台机器的，不是一个 home 的。** 一个 `slife2` 启动它的配置需要的那些
plugin，并接上已经在跑的任何东西，所以第二个实例就是一条命令、不多一个重复进程 ——
跨 home 也是。一台机器正在跑的一个 plugin 为这台机器上的每一个 home 服务，而每一次调用
都被提供它来的那个 home 的配置和文件。这就是为什么它们的簿记 —— 记着 pid 的记录、启动
锁、日志，以及对一个 agent 名字的占用 —— 也是这台机器的，住在
`~/.slife2/runtime`（`$SLIFE2_RUNTIME_DIR` 覆盖它），而不是某个数据目录里：只有一套
server，所以就只该有一份关于它们的记录。它是*可重建的* —— 删掉它，下次启动会把需要的
东西重新建出来。

所以在一个源码检出里，`agents/` 留在工作树中，这就是 `.gitignore` 覆盖它的原因。它
*不*覆盖 `slife2.yaml` 或 `skills/`：那些是人写的，而不是某次运行产生出来的，它们正是
这套安排的意义所在。

删掉一个 agent 的目录，代价就是那个 agent —— 它的对话、它的文件柜和它的各个登录，都在
一个地方，这正是让备份它、或者把它移到另一台机器上成为一次操作的原因。目录是故意用另
一种方式保存的，坐在它们外面：工具不是任何人的，所以一个 hub 把它们提供给*那个 home 里*
的每一段对话。

`slife2.yaml` 是入库的，也是在原处有文档的，因为**它不装任何秘密**：里面每一个 key 都是
一个运行时才解析的 `${VAR}` 引用。一个里面没有配置的数据目录用内置默认值，所以一次全新
安装没有它也能跑。

秘密从不写在文件里。`${VAR}` 经由 shell env →
[credstore](https://pypi.org/project/credstore/) → 字面默认值解析，而
`keyring:<service>/<key>` 这样的值直接对系统 keyring 解析。解析是宽松的 —— 解析不了的
引用原样留着，所以缺失的 key 会在那次能点名它的 API 调用上失败，而不是在启动时。

## 代码布局

四层，一个模块在哪一层，是关于它*是*什么的事实：

```
slife2/
├─ libs/              # library：被 import，从不经由 socket 够到
│  ├─ host/           # 这台机器 —— 配置文件及它唯一的写入者、一次调用由哪个
│  │                  #   home 服务、守护进程的记录和锁，以及把 server
│  │                  #   拉起来的 launcher
│  ├─ plugin/         # 每个 plugin 都坐在上面的契约：`protocol`（要成为我们的
│  │                  #   server 需要什么）、`audience`（谁可以调用一个工具）、
│  │                  #   `family`（一个 family 共享的那一半）
│  ├─ store/          # 三份语料和它们共享的机制：轮次（`db`）、工具目录、
│  │                  #   FTS5 + `sqlite-vec` 索引，以及那条文本规则
│  ├─ turn/           # 一轮由什么构成：循环、上下文决策、中立的消息、
│  │                  #   事件词汇、工具注册表、提示词和它的 `templates/`
│  ├─ harness/        # harness *放进*一段对话的东西：标记、交接、后台任务
│  └─ model/          # 作为协议的模型：`Chunk`、`Stream`、`LLMBackend`
├─ clients/           # 一跳的客户端那一半，每个对端一个模块
│  ├─ model.py        # MCPBackend —— agent 循环唯一的 backend
│  ├─ toolclient.py   # hub：它的清单、它的调用、那次裁剪
│  ├─ embedder.py     # embeddings 端点，两个索引共用
│  └─ gateway.py      # 别人在跑的一个 server
├─ helpers/           # 小而无可状态：时钟、时间语法、超时
├─ plugins/           # 每个一个进程：一个目录，里面有 `server.py`
│  ├─ agent/          # slife2-agent —— 循环，以及它拥有的那些对话
│  ├─ context/        # slife2-context —— 轮次日志和关于它的那个决定
│  ├─ toolhub/        # slife2-toolhub —— 模型的工具和它们构成的那个集合
│  ├─ llm/            # 四个模型 server，每种协议一个
│  ├─ skills/ cli/ mcp_tools/ restapi/     # 拥有某一节的四个
│  ├─ cabinet/ media/ file_tunnel/         # 文档、生成、一个公开 URL
│  ├─ wechat/ a2a/ agent_task/ schedule/   # 活找上门的几种方式
│  └─ tui/            # slife2-tui —— 终端的收件箱，每个 agent 一个会话
└─ tui/               # 终端：Textual，以及上面两者的客户端
   ├─ app.py client.py widgets.py      # 窗口、它的各个客户端、它的绘制
   ├─ notes.py restore.py theme.py     # harness 的那几行、一段重新打开的
   └─ app.tcss                         #   对话、调色板
```

一个 plugin 目录保留自己私有的 library（`cabinet/store.py`、
`schedule/expressions.py`、`skills/playbooks.py`、`wechat/ilink.py`、
`media/adapters.py`、`a2a/mesh.py`）：一种能力放在拥有它的那个 plugin 里，而 `libs/`
是给*不止一个*东西 import 的东西的。测试树和这一棵一一对应，一个文件对一个文件。

依赖方向是单向的，而正是它让每一层都能被单独测试：`tui/` → MCP →
`plugins/agent/` → `libs/turn/loop.py` → {`libs/model/base.py`、
`libs/turn/tools.py`、`libs/turn/events.py`、`libs/turn/messages.py`} →
`libs/host/config.py`。循环既不 import agent server 也不 import 终端，它也不 import
hub 或 hub 的客户端：交给它的是一个返回注册表的协程，而那个注册表是从哪来的，是 agent
server 自己的事。

`libs/plugin/protocol.py` 是每一个 server 坐在上面的叶子：成为我们的一个 server 意味着
什么 —— 那些标志、HTTP 传输、说明这里有守护进程的记录、那些否则会被复制进每个 server
的约定（`house_server`），以及一个 plugin `main` 的全部（`serve_plugin`），那是十一个
行、否则每一个都要自己拼一遍。它不是 LLM 专属的，这就是它不在 `plugins/llm/` 下的原因：
上下文存储、hub 和 agent server 都不是 LLM plugin，而它们所坐的脚手架不该从 LLM 包里
出来。embeddings server 是 `llm/` 下唯一一个不是聊天后端的 plugin —— 它只说
`/embeddings`，别的什么都不说 —— 它在那里，是因为它是这套系统里另一个 import provider
SDK 的东西。

## 测试

**测试套件是这个项目据以改动的东西**，所以有两条道，默认那条很快：

```bash
uv run pytest                  # unit + integration，每个模块，跨核并行
uv run pytest -m unit          # 单独跑快的那条道
uv run pytest -m integration   # 会绑 socket 或起进程的那些测试
uv run pytest -m e2e           # 整套服务作为进程（约 75s）
uv run ruff check . && ruff format --check .
uv run pyright                 # 一个 dev 依赖，接进了 CI
```

`uv run pytest` 在所有核上跑（`-n auto`，来自 `addopts`），并排除 `e2e`，那是唯一一条
会启动真实整套服务的道：十七个守护进程、磁盘上的一份配置和一个桩 provider，端到端驱动
（`tests/e2e/`）。两条道都在 CI 里跑。

**快的那条道里没有任何东西需要 API key 或一个正在跑的 server。** 模型是假的，server
们经由 FastMCP 的**内存传输**驱动 —— 真的
MCP 消息和真的进度通知，没有绑定任何端口。`integration` 的那些测试才是需要 socket 的，
而每一个绑的都是临时端口而不是固定端口，所以在一个活着的整套服务旁边跑测试，仍然是能
跑过的测试。

那些替身住在 `tests/fakes.py` 里并且是共享的：一个桩模型、一个桩 embedder、假的窗口和
网格客户端，以及不止一个测试模块会用到的那几个小工厂（`upstream_server`、
`plugin_transports`、`config_file`）。一个测试模块跑到另一个模块里去拿 fixture，就是
两条道最后对「一个替身长什么样」产生分歧的方式。
