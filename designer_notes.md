This document is designer's memory

1. Design and Coding Rules

- 觉得每个操作都可能失败就会有无穷多个fallback，而设计的行为正常必须成功的不允许fallback.fallback是不影响正常行为degrade，degrade必须要得到设计批准。我们有时候更愿意hard fail，而不做一些隐藏bug的fallbcak。失败可以重试。真正需要fallback时。要让fallback成为一种/设计行为。

- 要强化架构和结构设计，通过plugins 和 公共库共享能力和代码。plugin要功能单一，数量多没有关系，性能上损失几跳没有关系，因为是本机的通讯。统一plugin协议，做到plugin易维护易替换。

- 代码库的组织要模块化、结构化， 按plugin, library, helper ... 等分层次分功能组织。

- 大模型工具设计
    - Schema over prompt
    - 大模型的工具schema要准确、简洁表达工具是干什么的、参数是什么，不要重复大模型知道的知识，尽量少的交叉引用，交叉引用容易造成不易维护的依赖。
    - 工具名称和参数名称要统一规范，便于大模型推理。特别是相似的工具集，规范性更为重要。工具输出统一为json格式输出。例外：_check_new_input

- 同一台机器可以以隔离方式运行多个agent实例。两个实例是两个独立agent，碰巧它们在一台机器上，我们共享plugins; 碰巧 --data-dir, 共享 slife2.yaml 和 tools.db, 原生多agent设计，复用plugins, 复用外部mcp servers.

- 完整详尽的pytest，保障系统功能正常进化，更完善的端到端测试

1. 文档
readme 和 sdd属于规范文档，design.md是开发者文档，designer_notes.md是我的文档

1. LLM 的 Harness Tooks

loop自动调用的工具，生成tool 消息对，就像大模型自己调用的一样。有三个小差别：1）TUI 2）元参数不起作用 3）fake call id，大模型知道不是自己调用的

- _check_new_input
- _func_tool_unload

4. Channel与Marker

- 注入到loop 的 user message的Channel和Marker 设计

channel, 注入描述，TUI显示，Marker

tui, 从tui输入进入agent loop, You>, 无
wechat, 从微信输入进入agent loop， Wechat>, [Wechat:json]
a2a:<peer>, 从a2a消息进入loop, PeerAgent(jill)>, [PeerAgent:json]

agent-task:<name>, 从Agent Task的结果中注入到当前Agent loop中， AsyncTask(reseach)>，[AgentTaskResult:json]

async-tool:<name>, 从async tool的async执行结果中进当前loop, AsyncTool(pdf_to_md)>, [AgentToolResult:json]

- In memory Harness Marker

[TURN:json]

- 因为Inbox有插对机制，注入的信息插队时，无TUI处理。

5. 工具输出管理设计

正常的工具都会输出合理的长度，不会太离谱，内容多可以翻页。
cli/shell 的输出长度不可控，有些工具注定输出很长，比如给我写一篇1万字的论文。

设计了判别器。提取内容，全文存在文件中， dump， 文本文件 dump read，

二进制数据截断，dump，做为遗留问题，

遗留问题，判别器输入超出context，极小概率事件。先搁置。


1. To do list

- subagent design
- --a2a-broker host:port , use external broker, do not start/stop mosquitto

1. 设计issues

每个headless的cost，能并发到1000个吗

- 判别器总是带当前上下文的，总是在当前上下文的条件下进行判别。现在已经有两个判别器了，一个是rebuild context, 一个是 compact 工具输出。

- Async Agent Task 是一个异步one loop run，它是的设计目的是让agent委派任务，可以用在agent把任务拆分后委派任务。它没有subagent的cost，创建和停止。

- Aync Tool ，可以让工具异步执行，用在tool 执行时间比较长，agent选择它异步。

现在的一个未定的设计选项，就是subagent，多loop的后台执行。沿用 a2a 消息机制可能是最一致性的。

还有 agent swarm 的考虑，做agent swarm，是不是就不用subagent概念了。

要做一个判断，是用headless做subagent，还是专门做subagent。用headless做的好处就是改动比较小，只去掉turn save就可以。如果考虑长程任务，也许完整的agent更合适。

subagent共享agent的一切
agent swarm按照 agent headless? 用headless

现在rebuld context 是针对每个 prompt， 唯一的drawback就是长程任务的loop可能直接就超过了context 预算。是否可以在agent loop中动态rebuild context。最好是一致的方式。

- 内存里Context管理：大模型每调用一次，让agent知道当前的context usage，而这个信息不进memory，不重复。也就是每次提交给大模型的最后一条消息的append [TOKEN-USAGE:json]，json包括token使用数和占比。但这个marker不进记忆，也不改变当前message,也就是说手工append。
难点是，大模型怎么rebuild turn 和选择相关messages. 主要对长程任务。遗留。

Subagent设计， 主agent 和 它的子agent组成另一个 A2A 网

子agent就是完全正常功能的agent，没有TUI（但不是字面意思的headless, 压根就没有tui）, 具有agent的所有功能，创建时可以指定大模型，默认值为主agent的大模型，它没有turn memory，与主agent共享cabinet。它没有context rebuild. session lived。

如果用headless建，是不是所有机制都有了？



  