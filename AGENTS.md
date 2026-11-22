# Agent guide

Tool schemas are in the catalogue. Names take these forms: built-ins (`turn_read`), external MCP and REST tools (`{server}__{tool}`), jobs (`job__<name>`), harness tools (`_`-prefixed), skills (`skill:<name>`) and CLI entries (`cli:<name>`).

- Every function tool takes `_timeout`, `_async` and `_approve`. `_async` answers later: end the turn, do not wait. `_approve` asks only when someone can answer.
- `tool_search` finds tools by what they do. `tool_list` lists everything installed. `func_tool_load` adds a tool to your list.
- Missing capability: find an existing MCP server, OpenAPI API, CLI or Skill; add it with `mcp_set`, `rest_api_set`, `cli_set` or `skill_set`.
- Write a tool with `job_set` (follow the `job-coding` skill). Turn a workflow into a skill with `skill_draft`, then `skill_set`.
- Read a `skill:<name>` with `skill_use`. Run a `cli:<name>` through your shell tool.
- Prefer a dedicated tool over a shell command or ad-hoc code.

## Built-in tools

| Category | Tools |
|---|---|
| Tools | `tool_search`, `tool_list`, `func_tool_load` |
| Memory | `turn_list`, `turn_read`, `turn_search`, `turn_summarize` |
| Cabinet | `note_write`, `note_read`, `note_update`, `note_remove`, `note_list`, `diary_write`, `diary_read`, `diary_update`, `diary_remove`, `diary_list`, `file_save`, `file_read`, `file_grep`, `file_remove`, `file_list`, `report_write`, `report_read`, `report_update`, `report_remove`, `report_list`, `cabinet_search`, `cabinet_summarize`, `dump_read`, `dump_grep` |
| Subagents | `subagent_create`, `subagent_remove`, `subagent_list`, `subagent_send_message`, `subagent_task_response`, `subagent_continue_task`, `subagent_broadcast`, `subagent_task_list`, `subagent_cancel_task`, `subagent_group_set`, `subagent_group_list`, `subagent_group_member_list`, `subagent_group_remove`, `subagent_group_leave`, `subagent_group_send_message` |
| Peers | `peer_agent_send_message`, `peer_agent_task_response`, `peer_agent_continue_task`, `peer_agent_cancel_task`, `peer_agent_list`, `peer_agent_task_list`, `peer_agent_broadcast`, `peer_agent_group_set`, `peer_agent_group_list`, `peer_agent_group_member_list`, `peer_agent_group_remove`, `peer_agent_group_leave`, `peer_agent_group_send_message` |
| Agent tasks | `agent_task_start`, `agent_task_status`, `agent_task_list`, `agent_task_cancel` |
| Background | `check_async`, `cancel_async` |
| Plan | `plan_write` |
| Media | `attach_image`, `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio`, `expose_local_image`, `unexpose_local_image` |
| Skills | `skill_use`, `skill_draft`, `skill_list`, `skill_set`, `skill_edit`, `skill_remove`, `skill_set_enabled` |
| Jobs | `job_list`, `job_read`, `job_set`, `job_remove`, `job_run`, `job__<name>` |
| Schedule | `schedule_set`, `schedule_remove`, `schedule_list`, `schedule_runs`, `schedule_run_now`, `schedule_run_skip` |
| WeChat | `wechat_login`, `wechat_check_status`, `wechat_send_message`, `wechat_logout` |
| MCP | `mcp_get`, `mcp_list`, `mcp_set`, `mcp_remove`, `mcp_set_enabled` |
| REST API | `rest_api_get`, `rest_api_list`, `rest_api_set`, `rest_api_remove`, `rest_api_set_enabled` |
| CLI | `cli_get`, `cli_list`, `cli_set`, `cli_remove`, `cli_set_enabled` |
| Models | `model_get`, `model_list`, `model_set`, `model_remove`, `model_switch` |

## External tools

The shipped `slife2.yaml` configures these:

- **MCP** (`{server}__{tool}`): serper, fetch, tavily-mcp, duckduckgo-search,
  amap-maps, playwright, context7, chrome-devtools, citecheck, mcp-pandoc,
  markitdown, office-word-mcp, excel-mcp, pdf-toolkit, arxiv, crossref,
  google-scholar, desktop-commander, deepseek-search, baidu-search, gitee.
- **REST API** (`{server}__{operation}`): github, mcp-registry.
- **CLI** (`cli:<name>`): yt-dlp, browser-harness, rg.
- **Skills** (`skill:<name>`): baidu-search, browser-harness,
  browser-harness-headless, job-coding.

## Harness tools

`_`-prefixed. The system invokes them automatically; you may call them yourself.

- `_check_new_input` — take a message that arrived mid-turn.
- `_func_tool_unload` — trim the loaded tool list.
- `_turn_compact` — drop messages this turn no longer needs. Automatic at `agent.compact_at`.
- `_context_rebuild` — keep some historical turns and recall others from the turn log. Automatic at the first boundary; the running turn is never selected.

A failed `_turn_compact` or `_context_rebuild` leaves the turn on its current context.

## Who you are talking to

The marker on a message says where it came from; reply with the tool named beside it.

| From | Marker | Reply |
|---|---|---|
| Person, WeChat | `[Wechat:{…}]` | `wechat_send_message` |
| Your subagent | `[Subagent:{…}]` | `subagent_send_message` |
| Peer agent | `[PeerAgent:{…}]` | `peer_agent_task_response` / `peer_agent_send_message` / `peer_agent_continue_task` |
| Group message | `[PeerAgentGroup:{…}]` / `[SubagentGroup:{…}]` | `peer_agent_group_send_message` / `subagent_group_send_message` |
| Agent task or background call | `[AgentTaskResult:{…}]` / `[AgentToolResult:{…}]` | already delivered |

## The turn you are in

- Your message opens with `[TURN-PROMPT:{…}]`: `now`, and `plan` when the conversation has one.
- `[TURN:{…}]` records when a past turn ran.
- A turn that could not go on ends with `[TURN-FAILED:{"by":"harness","error":…}]`: the harness stopped it, not you.
- A tool failure is a tool result; the turn continues.

## Working rules

- `subagent_*` reaches only your subagents; `peer_agent_*` reaches only peers. Each has its own conversation and memory.
- Inside an agent task: do not use `_async`; do not start another task.
- Check `schedule_runs` before assuming a scheduled run happened.
- `plan_write` replaces the whole plan, which is replayed every turn. Use it for work longer than a step or two; put a blocked item's reason in its text.
