This document is designer's memory

1. Design and Coding Rules

- Fallback 越少越好，它容易隐藏真正的bugs，带来不可预测的后果。影响系统设计行为的失败不要fallback, 要hard fail. 失败可以重试。真正需要fallback时。要让fallback成为一种设计行为。

- 要强化架构和结构设计，通过plugins 和 公共库共享能力和代码。

- 大模型的工具schema要准确、简洁表达工具是干什么的、参数是什么，不要重复大模型知道的知识，尽量少的交叉引用，交叉引用容易造成不易维护的依赖。

- Schema over prompt

- 工具名称和参数名称要统一规范，便于大模型推理。特别是相似的工具集，规范性更为重要。工具输出统一为json格式输出。

2. LLM 的 Harness Tooks

- _check_new_input
- _func_tool_unload

3. Channel与Marker

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

4. To do list

- subagent design
- --a2a-broker host:port , use external broker, do not start/stop mosquitto
