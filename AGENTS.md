# Using your tools

The tool descriptions are the authority on each tool. This page is what no single description can say.

- Tools catalogue: built-in tools (bare names like `turn_read`), external MCP and REST tools (`{server}__{tool}`), jobs (`job__<name>`), harness tools (`_`-prefixed), skills (`skill:<name>`) and CLI entries (`cli:<name>`).
- Every function tool also takes `_timeout`, `_async` and `_approve`; the harness strips them before dispatch. With `_async` the result arrives later — never wait for it; end the turn. `_approve` asks only when someone can answer.
- Prefer a dedicated tool over a shell command or ad-hoc code. Read a refusal and correct the call. 

## Finding and loading

- The catalogue is larger than the list sent with each request: `tool_search` finds names, `func_tool_load` makes one callable. Search before concluding a capability is missing.
- A tool can answer "not in the catalogue yet" while the catalogue is still starting. 
- **A missing capability can be installed, not just reported.** If `tool_search` really holds nothing, search the web for an existing MCP server, OpenAPI API, CLI or playbook, then add it yourself: `mcp_set` (stdio or URL), `rest_api_set` (OpenAPI spec), `skill_set` (a playbook you write), `cli_set` (a command you installed first with the shell). `mcp_list_tools` / `rest_api_list_tools` (and `skill_list` / `cli_list`) show the new surface, and the next `tool_search` finds it.

## The turn you are in

- Current turn's message opens with `[TURN-PROMPT:{…}]`, carrying `now` and `token-usage`.
- A `[TURN:{…}]` footnote records when a past turn ran.

## Built-in tools

| Category | Tools |
|---|---|
| Tools | `tool_search`, `func_tool_load` |
| Memory | `turn_list`, `turn_read`, `turn_search`, `turn_summarize` |
| Cabinet | `note_edit`, `note_remove`, `note_read`, `note_list`, `diary_write`, `diary_read`, `diary_list`, `file_save`, `file_remove`, `file_read`, `file_list`, `report_save`, `report_read`, `report_list`, `cabinet_search`, `cabinet_summarize`, `dump_read` |
| Subagents | `subagent_set`, `subagent_remove`, `subagent_list`, `subagent_send_message`, `subagent_broadcast`, `subagent_task_list`, `subagent_cancel_task`, `subagent_group_set`, `subagent_group_list`, `subagent_group_member_list`, `subagent_group_remove`, `subagent_group_leave`, `subagent_group_send_message` |
| Peers | `peer_agent_send_message`, `peer_agent_cancel_task`, `peer_agent_list`, `peer_agent_task_list`, `peer_agent_broadcast`, `peer_agent_group_set`, `peer_agent_group_list`, `peer_agent_group_member_list`, `peer_agent_group_remove`, `peer_agent_group_leave`, `peer_agent_group_send_message` |
| Agent tasks | `agent_task_start`, `agent_task_status`, `agent_task_list`, `agent_task_cancel` |
| Background | `check_async`, `cancel_async` |
| Media | `attach_image`, `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio`, `expose_local_image`, `unexpose_local_image` |
| Skills | `skill_use`, `skill_draft`, `skill_list`, `skill_set`, `skill_edit`, `skill_remove`, `skill_set_enabled` |
| Jobs | `job_list`, `job_read`, `job_set`, `job_remove`, `job_run`, `job__<name>` |
| Schedule | `schedule_set`, `schedule_remove`, `schedule_list`, `schedule_runs`, `schedule_run_now`, `schedule_run_skip` |
| WeChat | `wechat_login`, `wechat_check_status`, `wechat_send_message`, `wechat_logout` |
| MCP | `mcp_list`, `mcp_set`, `mcp_remove`, `mcp_set_enabled`, `mcp_list_tools` |
| REST API | `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled`, `rest_api_list_tools` |
| CLI | `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| Models | `model_list`, `model_set`, `model_remove`, `model_switch` |

## External tools

The names the shipped `slife2.yaml` configures. 

- **MCP** (`{server}__{tool}`): serper, fetch, tavily-mcp, duckduckgo-search,
  amap-maps, playwright, context7, chrome-devtools, citecheck, mcp-pandoc,
  markitdown, office-word-mcp, excel-mcp, pdf-toolkit, arxiv, crossref,
  google-scholar, desktop-commander, deepseek-search, baidu-search.
- **REST API** (`{server}__{operation}`): github, mcp-registry.
- **CLI** (`cli:<name>`): yt-dlp, browser-harness, rg.
- **Skills** (`skill:<name>`): baidu-search, browser-harness,
  browser-harness-headless, job-coding.

## Harness tools

The harness's tools are the `_`-prefixed ones; the system invokes them automatically.

- `_check_new_input` hands over a message that arrived while the turn was running.
- `_func_tool_unload` trims the loaded list.

## Who you are talking to

The marker at the start of a message says where it came from.

| From | Marker | Reply |
|---|---|---|
| Person, WeChat | `[Wechat:{…}]` | `wechat_send_message` |
| Your subagent | `[Subagent:{…}]` | `subagent_send_message` |
| Peer agent | `[PeerAgent:{…}]` | `peer_agent_send_message` |
| Group message | `[PeerAgentGroup:{…}]` / `[SubagentGroup:{…}]` | `peer_agent_group_send_message` / `subagent_group_send_message` |
| Agent task or background call | `[AgentTaskResult:{…}]` / `[AgentToolResult:{…}]` | already delivered |

- **Subagents** are yours, with their own conversation and memory; **peers** are other agents, with their own identity and memory. `subagent_*` does not reach a peer, and `peer_agent_*` does not reach your subagents.
- **Agent tasks** run as you in the background, from this conversation's stored turns. Nobody can answer their questions, so `description` is their whole context.

## Memory and the cabinet

- Your conversation is stored automatically. A `[TURN:{…}]` footnote names a rebuilt turn's id and channel.
- The cabinet is your durable, per-agent store; another agent cannot read it.
- A shortened tool result is kept whole: read the rest with `dump_read`, by its `[DUMP:{…}]` marker. Do not re-run the tool.

## Doing work

- **Skills** are playbooks you follow; **jobs** are code that becomes a tool (`job__<name>`). Read `job-coding` before writing a job.
- **A job can use everything you can.** Its code reaches the hub's catalogue, any plugin server, a full agent turn, and a raw model call — so a new tool that calls a model, or a workflow you keep repeating, can become a `job__<name>` you own. Save a recurring procedure as one of the two: a job when the steps are known, a skill when the judgement is the model's.
