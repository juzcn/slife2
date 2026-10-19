# Tools

slife2's own tools — the `plugin` category of the tool catalogue. External
`mcp:`, `rest-api:`, `cli:` and `skills:` entries are not listed.

Arguments are the wire schemas the hub advertises. A tool's answer is not part
of that schema, so every entry says what it returns. Generated from
`agents/tools.db` (91 tools) by `scripts/gen_tools_doc.py`.

## Contents

- [Agent](#agent) (8)
- [Agent tasks](#agent-tasks) (4)
- [Cabinet](#cabinet) (17)
- [CLI catalogue](#cli-catalogue) (4)
- [History](#history) (2)
- [File tunnel](#file-tunnel) (2)
- [MCP configuration](#mcp-configuration) (5)
- [Media](#media) (4)
- [Peer agent network](#peer-agent-network) (11)
- [REST configuration](#rest-configuration) (5)
- [Schedule](#schedule) (6)
- [Skills](#skills) (5)
- [Subagent network](#subagent-network) (11)
- [Tool catalogue](#tool-catalogue) (3)
- [WeChat](#wechat) (4)

## Agent

The conversation loop, model configuration, images and subagents.

Source: [`slife2/plugins/agent/server.py`](slife2/plugins/agent/server.py)

### `_check_new_input`

[`slife2/plugins/agent/server.py:2367`](slife2/plugins/agent/server.py#L2367)

The message that arrived while the turn was running, verbatim.

The harness calls this at a turn's next iteration boundary and writes the
call and its result into the conversation, so a message that arrives
mid-turn reaches the turn it arrived in. It hands over the first message
waiting for this conversation.

**Parameters**

No parameters.

**Returns**

A string: the first message waiting for this conversation, verbatim, or `"Nothing waiting: no message has arrived since the last boundary."`

### `attach_image`

[`slife2/plugins/agent/server.py:2303`](slife2/plugins/agent/server.py#L2303)

Add the image parts a vision model reads to the request.

Call this when a message names a picture — a path on this machine, a
URL, a `data:` URI.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `sources` | `array<string>` | yes | — | Absolute paths on this machine, `http(s)` URLs that serve image bytes, or `data:` URIs — as many as you need in one call. |

**Returns**

`attached` — the sources the model can now see, named exactly as
this call named them — and `refused`, one entry per source that
could not be read, each naming the source and the reason.

### `model_list`

[`slife2/plugins/agent/server.py:2524`](slife2/plugins/agent/server.py#L2524)

List the models this config defines.

Reads the config file, so it is what the next conversation would use.

**Parameters**

No parameters.

**Returns**

`models`, one row per model: `provider` and `model`, which name it —
`provider/model` is the reference the rest of these tools take —
`name`, whether it is the `default`, and whether it reasons or reads
images, under `marks`; and `context_window` and `max_tokens`, both
`0` when the config does not say.  Plus `total`, how many models
there are, and `default`, the reference the system runs on — `null`
when the config names none.

### `model_remove`

[`slife2/plugins/agent/server.py:2711`](slife2/plugins/agent/server.py#L2711)

Remove one model, named `provider/model`.

The model stays at its provider, and the provider goes too when that was
its last model. Refused for the default model — switch to another first
— and for one a running conversation is on, whose model is fixed when it
starts.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ref` | `string` | yes | — | The model, as `provider/model`. |

**Returns**

`ref`, and `provider_removed`: whether the provider's whole entry
went with it, which happens when this was its last model.

### `model_set`

[`slife2/plugins/agent/server.py:2579`](slife2/plugins/agent/server.py#L2579)

Add or update one model, creating its provider.

This is the model's whole entry: a field left out is dropped, so restate
everything it should have. The next conversation can name it.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ref` | `string` | yes | — | The model, as `provider/model` — the same reference its siblings take. The provider is created when it is new. |
| `name` | `string` | yes | — | The display name. |
| `api` | `string` | no | `""` | The wire protocol — `openai-completions`, `anthropic-messages` or `openai-responses`. Empty uses the provider's own, or openai-completions for a new one. |
| `base_url` | `string` | no | `""` | The endpoint. A new provider needs it. |
| `api_key` | `string` | no | `""` | A `${VAR}` reference, so the key stays out of the file. A new provider needs it. |
| `reasoning` | `boolean` | no | `false` | The model thinks natively, and its reasoning is kept on the turn. |
| `input` | `array<string> or null` | no | `null` | What it reads — `["text"]`, or `["text", "image"]`. |
| `context_window` | `integer` | no | `0` | Its context window, in tokens. |
| `max_tokens` | `integer` | no | `0` | Its output limit, in tokens. |
| `temperature` | `number or null` | no | `null` | Sampling temperature. |
| `top_p` | `number or null` | no | `null` | Nucleus sampling. |
| `compat` | `object or null` | no | `null` | Wire quirks the endpoint needs, e.g. `{"thinking": "omit"}`. |

**Returns**

`ref`, and what the write did: `created` when this call added a model
the file did not have rather than replacing one it did, and
`provider_created` when it made the provider too. `backend` is the
protocol's backend when this call had to start one — `{"api", "up",
"detail"}`, where `up: false` carries the reason — and `null` when a
provider for that protocol was already configured.

### `model_switch`

[`slife2/plugins/agent/server.py:2799`](slife2/plugins/agent/server.py#L2799)

Make `provider/model` the model this system runs on, at once.

Writes the top-level `default:` in the config file, and moves every
conversation this server is holding onto it: each keeps its history and
runs on the new model from its next turn. That is what makes it the way
out of a model that has stopped answering. The model must be one the
config defines.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ref` | `string` | yes | — | The model, as `provider/model`. |

**Returns**

`default` — the reference the system now runs on — `previous`, what
`default:` said before, and `moved`, how many conversations that were
already running this server moved onto it from their next turn.

### `subagent_remove`

[`slife2/plugins/agent/server.py:3462`](slife2/plugins/agent/server.py#L3462)

Unmake a subagent, and take everything it wrote with it.

The whole of one subagent: its conversation is forgotten (the loop, and
the list it would restore from), and its directory — turns, cabinet,
ledger and the record of what it was made with — is deleted.  A subagent
is a directory, so this is one operation rather than a list somebody has
to keep complete.

**Nothing outside `subagents/` can be removed by it, and that is the
layout rather than a check.**  The name is a name inside the container
(`subagent_path`), so the agent's own `cabinet/`, `media/` and `turns.db`
are not reachable from here at all — and inside it, what makes a directory
a subagent's is the record the directory carries (`subagent.yaml`).  A
directory there without the record is refused rather than emptied; a name
that is not there at all is not an error, because the caller wanted it
gone and it is gone.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | Which of your subagents. The same name you made it with. |

**Returns**

`name`, and `removed`: whether there was one here to remove.

### `subagent_set`

[`slife2/plugins/agent/server.py:3338`](slife2/plugins/agent/server.py#L3338)

Make a subagent: an agent of your own, named, with its own memory.

A subagent is an agent that belongs to you — the same tools, the same way
of working, its own conversation, and its own directory underneath
yours (`agents/<you>/subagents/<name>/`).  It has no window and no
process: it runs when something sends it a message, and from then on you
address it by name.  It
is *made* here and *unmade* by `subagent_remove`; everything else it does,
it does with the tools you have.

**A subagent you already have is the same subagent** — `set` rather than
`add`, so naming one twice is safe.  What a second call changes is its
model and its role; when it was made, and whether it was made as a fork,
are what they were — a fork's conversation is a copy already taken, and
what it is made of is not something a later call can re-decide.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | What to call it. A name is one subagent and one directory inside yours, so it is also the address you send to. |
| `model` | `string` | no | `""` | Which model it runs on, as `provider/model`. Empty — the default — is *your* model, read when the subagent first runs rather than now, so a `model_switch` you make in between reaches it the way it reaches your own next turn. |
| `role` | `string` | no | `"subagent"` | What it is for, as a word: it goes into the prompt the subagent starts with, which opens `You are <name>, one of <agent>'s subagents — your role is <role>`. The default is the plain one — `subagent` — and anything else is worth saying only when it changes what the agent would do, the way "code reviewer" does. |
| `fork` | `boolean` | no | `false` | Whether it starts from a **copy of your own conversation** instead of from nothing — a temporary stand-in for you. A fork is handed everything you have said so far, as it stood at the moment you made it, and nothing it does is written down: it remembers its own turns for as long as this session runs, and a restart forgets it — the name is free for a new one. Ask one a question *about* what you are in the middle of when you want the answer to that question to be the only thing that enters your history. |

**Returns**

`name` (made safe to be a directory), `model`, `role`, `fork`,
`created_at`, and `created`: whether this call was the one that made
it.

## Agent tasks

Background loops started by the model.

Source: [`slife2/plugins/agent_task/server.py`](slife2/plugins/agent_task/server.py)

### `agent_task_cancel`

[`slife2/plugins/agent_task/server.py:1090`](slife2/plugins/agent_task/server.py#L1090)

Stop an agent task, by name.

Answers with the task's state *after* the call: `cancelled` when this is
what stopped it, `finished` or `failed` when it was already over — a
finished task still delivers, and a cancelled one never does. Nothing is
rolled back: a tool call that was running is wherever it got to.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The task's handle. |

**Returns**

`task_id`, `name`, `status` after the call (`cancelled`, `finished`, `failed` or `unknown`), `started_at` and `finished_at`.

### `agent_task_list`

[`slife2/plugins/agent_task/server.py:1054`](slife2/plugins/agent_task/server.py#L1054)

Every agent task this agent has, the running ones first.

Each row is a task's id, name, state and times with a glance at its
description, so that a name chosen a while ago still means something. It
is bounded — the most recent finished tasks are kept and the tail is
forgotten — so a row can go `unknown` like any other.

**Parameters**

No parameters.

**Returns**

`tasks` — one row per task, running first, each `task_id`, `name`, `status`, `started_at`, `finished_at` and a shortened `description`.

### `agent_task_start`

[`slife2/plugins/agent_task/server.py:965`](slife2/plugins/agent_task/server.py#L965)

Start an agent task: one background loop that runs as you.

For work that would hold you — long, or waiting on the home. The call
returns at once and the answer reaches this conversation on its own: do
not wait for it, and do not poll.

`description` is the task itself. It starts from this conversation's
stored turns, so **the turn you are answering is not among them** — put
in what the task needs from the last exchange, and let it read everything
older for itself. It runs with your prompt, your model and your tools,
and nobody can answer a question it raises: what it sends when it stops
is the whole of what comes back.

`name` is the handle — how the task is asked about and stopped. This is
not a way to run a program in the background.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The handle the task is asked about and stopped by. |
| `description` | `string` | yes | — | What the task should do — the whole of what it is given. |

**Returns**

`task_id`, `name`, `status` (`running`), `started_at` and `finished_at`.

### `agent_task_status`

[`slife2/plugins/agent_task/server.py:1039`](slife2/plugins/agent_task/server.py#L1039)

What one agent task is doing, by name.

Its state — `running`, `finished`, `failed` or `cancelled` — and never
the result, which arrives by itself and cannot be fetched. Do not poll:
if it is `running`, end your turn. `unknown` is a name this process no
longer holds.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The task's handle. |

**Returns**

`task_id`, `name`, `status` (`running`, `finished`, `failed`, `cancelled` or `unknown`), `started_at` and `finished_at`. Never the task's result.

## Cabinet

Notes, diary, files, reports and tool dumps.

Source: [`slife2/plugins/cabinet/server.py`](slife2/plugins/cabinet/server.py)

### `cabinet_search`

[`slife2/plugins/cabinet/server.py:939`](slife2/plugins/cabinet/server.py#L939)

Search the cabinet — every note, day, file and report at once.

The two legs add up rather than competing: give both for the strongest
answer, either alone for one leg, and neither to browse what is there,
newest first.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `keywords` | `array<string> or null` | no | `null` | Words that must appear. A path like `files/docs/spec.pdf` works here. |
| `sentences` | `array<string> or null` | no | `null` | What you are looking for, asked normally. One per question you have. |
| `kind` | `string` | no | `""` | `note`, `diary`, `file`, `report` or `dump` — a tool's own output, which the harness saves — to look in one of them. Empty searches all of them but the dumps, which are reached by the marker naming them rather than by a search. |
| `since` | `string` | no | `""` | Only documents from this time on. |
| `until` | `string` | no | `""` | Only documents up to this time. |
| `limit` | `integer` | no | `20` | How many hits this page may hold. |

**Returns**

`results` — `kind`, `key`, `title`, `time`, `tags`, `summary` and
`excerpt` per hit, with `similarity` where the meaning leg ranked it
and `matched_in` (`line`, or `rest` for elsewhere in the document)
where the words leg found it — and `browsed`, true when neither leg
was given and the page is simply newest first.

### `cabinet_summarize`

[`slife2/plugins/cabinet/server.py:990`](slife2/plugins/cabinet/server.py#L990)

Describe a document already in the cabinet so it can be found.

Tags work for any kind; a summary is what a file's meaning is built
from, so writing one for a file re-embeds it — a file has no text of its
own, and a search reaches it by its name and paths until somebody
describes it.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `kind` | `string` | yes | — | `note`, `diary`, `file`, `report` or `dump`. |
| `key` | `string` | yes | — | The document's key — a subject, a date, a cabinet path, a title or the tool call a dump is the output of. |
| `summary` | `string` | no | `""` | What it is, in words somebody would search with. |
| `tags` | `string` | no | `""` | Comma-separated words to file it under. |

**Returns**

The document's own fields, and `reembedded` — true when the file's
vector was built again, so a search by meaning finds the new words.

### `diary_list`

[`slife2/plugins/cabinet/server.py:526`](slife2/plugins/cabinet/server.py#L526)

List diary days, newest first.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `since` | `string` | no | `""` | Only days at or after this. |
| `until` | `string` | no | `""` | Only days at or before this. |
| `limit` | `integer` | no | `50` | How many days this page may hold. |
| `offset` | `integer` | no | `0` | How many days to skip — that is how you page back. |

**Returns**

`entries` — `kind`, `key` (the day), `title`, `time`, `tags`,
`summary`, `mirror` and `excerpt` per day — and `total`, how many
days the window holds, so `offset + len(entries) < total` says
whether more remain.

### `diary_read`

[`slife2/plugins/cabinet/server.py:510`](slife2/plugins/cabinet/server.py#L510)

Read one day's diary entry.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `date` | `string` | yes | — | The day, `YYYY-MM-DD`. |

**Returns**

The day's `kind`, `key`, `title`, `time`, `tags`, `summary` and
`mirror`, and the whole entry under `text`.

### `diary_write`

[`slife2/plugins/cabinet/server.py:474`](slife2/plugins/cabinet/server.py#L474)

Write today's diary entry, or add to the entry for a given day.

A later write of the same day appends, so this is safe to call again:
two entries under one date are two facts about that day.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `content` | `string` | yes | — | The entry. |
| `date` | `string` | no | `""` | The day it is about, `YYYY-MM-DD`. Empty means today. |
| `tags` | `string` | no | `""` | Comma-separated words to file it under. |

**Returns**

The day as it is stored — `kind`, `key`, `title`, `time` (the day
itself), `tags`, `summary` and `mirror`.

### `dump_read`

[`slife2/plugins/cabinet/server.py:820`](slife2/plugins/cabinet/server.py#L820)

Read a tool output the harness saved, by finding — not by scrolling.

A result too large for the conversation is shortened for you and kept
whole here; the marker standing in for it names the call. This is the way
back to the part the shortening did not carry, and it is a *searching*
reader: name what you are looking for and how much of what surrounds it,
or name the line you already know and read from there. The lines are
numbered, a matched line is marked `:` and the lines around it `-`, and
the numbers are the ones the marker beside the result shows.

**Every answer is within the configured budget** (`tool_result.tokens`),
the same number the harness spends shortening a result — so reading an
output back can never hand over more than the shortening took away, and
this tool can never feed the thing that shortens results. Read as much
as you like, in as many windows as it takes.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `call` | `string` | yes | — | The tool call id, from the marker in the conversation. |
| `offset` | `integer` | no | `1` | The first line to read; `1` is the first and `-1` the last. |
| `limit` | `integer` | no | `0` | The most lines to show. `0` shows as many as the budget holds. |
| `pattern` | `string` | no | `""` | Show the lines containing this text, matched literally and ignoring case. |
| `context` | `integer` | no | `0` | With a pattern, how many lines either side of a match to show with it. |

**Returns**

`total`, how many lines the output has; `text`, the window, each line
prefixed with its own number; `where`, the file the whole output is
in; `next`, the `offset` of the window after this one — absent when
there is nothing further to read of what you asked for; and
`matched`, how many lines at or after `offset` hold `pattern`, which
is there only when you gave one, and is `0` with an empty window when
nothing matched.

### `file_list`

[`slife2/plugins/cabinet/server.py:701`](slife2/plugins/cabinet/server.py#L701)

List saved files, newest first.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `category` | `string` | no | `""` | Only files in this directory of the cabinet. |
| `since` | `string` | no | `""` | Only files saved at or after this. |
| `until` | `string` | no | `""` | Only files saved at or before this. |
| `limit` | `integer` | no | `50` | How many files this page may hold. |
| `offset` | `integer` | no | `0` | How many files to skip — that is how you page back. |

**Returns**

`entries` — `kind`, `key` (the path inside the cabinet), `title`,
`time`, `tags`, `summary`, `size`, `mime`, `source` and `excerpt`
per file — and `total`, how many files the window holds, so
`offset + len(entries) < total` says whether more remain.

### `file_read`

[`slife2/plugins/cabinet/server.py:659`](slife2/plugins/cabinet/server.py#L659)

Read a saved file's text.

A PDF, an image or an archive has no text: the call is refused with what
the file is, how big, and where its bytes are. Text in another encoding
reaches you decoded, with the encoding named beside it.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | `string` | yes | — | The saved file's path inside the cabinet. |

**Returns**

The file's `kind`, `key`, `title`, `time`, `tags`, `summary`,
`size`, `mime` and `source`, its text under `text`, and `encoding`
when that is not UTF-8.

### `file_remove`

[`slife2/plugins/cabinet/server.py:642`](slife2/plugins/cabinet/server.py#L642)

Delete a saved file — the cabinet's copy, its row and its vectors.

The file it was copied from stays where it is.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | `string` | yes | — | The saved file's path inside the cabinet. |

**Returns**

`kind`, `key` and `removed` — always `true`, because a path the
cabinet has no file at is refused rather than answered.

### `file_save`

[`slife2/plugins/cabinet/server.py:554`](slife2/plugins/cabinet/server.py#L554)

Copy files from this machine into the cabinet.

It copies and never moves, so what you point at stays where it is. The
bytes are not indexed: a search matches the file's name, where it came
from and any `summary` you write.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `paths` | `array<string>` | yes | — | Local paths to copy. Name the files in a directory rather than the directory itself. |
| `title` | `string` | no | `""` | A name for it. Empty uses the filename. |
| `tags` | `string` | no | `""` | Comma-separated words to file it under. |
| `summary` | `string` | no | `""` | What the file is, in your words — the text its semantic search is built from. |
| `category` | `string` | no | `""` | The directory inside the cabinet — `images`, `documents` and so on. Empty picks one from the extension. |

**Returns**

`saved` — one row per file, as `file_read` describes it, so the
`key` is the path each file was stored under. A file already there
under that name is stored beside it rather than over it, which is
why the key is worth reading.

### `note_edit`

[`slife2/plugins/cabinet/server.py:383`](slife2/plugins/cabinet/server.py#L383)

Write a private note about a subject, replacing what it held.

A note is a document: this is the whole of what the cabinet keeps under
that subject, and an older body goes. It lands as markdown in the
agent's cabinet directory too.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `subject` | `string` | yes | — | What the note is about — its name, and reusing one replaces the note rather than making a second. |
| `content` | `string` | yes | — | The note itself. |
| `tags` | `string` | no | `""` | Comma-separated words to file it under. |

**Returns**

The note as it is stored — `kind`, `key`, `title`, `time`, `tags`,
`summary` and `mirror`.

### `note_list`

[`slife2/plugins/cabinet/server.py:445`](slife2/plugins/cabinet/server.py#L445)

List notes, most recently written first.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `since` | `string` | no | `""` | Only notes written at or after this. Accepts `2026-10-07`, `last week`, `3 days ago`. |
| `until` | `string` | no | `""` | Only notes written at or before this. |
| `limit` | `integer` | no | `50` | How many notes this page may hold. |
| `offset` | `integer` | no | `0` | How many notes to skip — that is how you page back. |

**Returns**

`entries` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
`mirror` and `excerpt` per note — and `total`, how many notes the
window holds, so `offset + len(entries) < total` says whether more
remain.

### `note_read`

[`slife2/plugins/cabinet/server.py:429`](slife2/plugins/cabinet/server.py#L429)

Read a note by its subject.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `subject` | `string` | yes | — | The note's subject. |

**Returns**

The note's `kind`, `key`, `title`, `time`, `tags`, `summary` and
`mirror`, and the whole of it under `text`.

### `note_remove`

[`slife2/plugins/cabinet/server.py:414`](slife2/plugins/cabinet/server.py#L414)

Delete a note — its markdown, its index entry and its vectors.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `subject` | `string` | yes | — | The note's subject. |

**Returns**

`kind`, `key` and `removed` — always `true`, because a subject with
no note is refused rather than answered.

### `report_list`

[`slife2/plugins/cabinet/server.py:792`](slife2/plugins/cabinet/server.py#L792)

List reports, newest first, by when they were written.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `since` | `string` | no | `""` | Only reports written at or after this. |
| `until` | `string` | no | `""` | Only reports written at or before this. |
| `limit` | `integer` | no | `50` | How many reports this page may hold. |
| `offset` | `integer` | no | `0` | How many reports to skip — that is how you page back. |

**Returns**

`entries` — `kind`, `key`, `title`, `time`, `tags`, `summary`,
`mirror` and `excerpt` per report — and `total`, how many reports
the window holds, so `offset + len(entries) < total` says whether
more remain.

### `report_read`

[`slife2/plugins/cabinet/server.py:775`](slife2/plugins/cabinet/server.py#L775)

Read a report by its title.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `title` | `string` | yes | — | The report's title. |

**Returns**

The report's `kind`, `key`, `title`, `time`, `tags`, `summary`,
`mirror`, `period_start` and `period_end`, and every section under
`text`.

### `report_save`

[`slife2/plugins/cabinet/server.py:737`](slife2/plugins/cabinet/server.py#L737)

Save a report under a title, or add to the one already there.

A report is a log like a diary: a title that exists gets a new dated
section, and the earlier one stays.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `title` | `string` | yes | — | What it is a report of. This is its key. |
| `content` | `string` | yes | — | The report. |
| `tags` | `string` | no | `""` | Comma-separated words to file it under. |
| `period_start` | `string` | no | `""` | The first day the report covers, `YYYY-MM-DD`. |
| `period_end` | `string` | no | `""` | The last day it covers. |

**Returns**

The title as it is stored — `kind`, `key`, `title`, `time`, `tags`,
`summary`, `mirror`, `period_start` and `period_end`.

## CLI catalogue

Commands installed on this machine.

Source: [`slife2/plugins/cli/server.py`](slife2/plugins/cli/server.py)

### `cli_list`

[`slife2/plugins/cli/server.py:306`](slife2/plugins/cli/server.py#L306)

List the commands recorded under `cli:`, with their switch.

Each is a command recorded as installed on this machine, and a row in
the tool catalogue, so a search finds one by what it does.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | no | `0` | How many entries this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many entries to skip — that is how you page. |

**Returns**

`commands` — one row per entry, each with `name`, `state` (`on` or
`off`), `command`, and `install`, `source` and `description` when the
entry has them — and `total`, `limit` and `offset`, so
`offset + len(commands) < total` says whether more remain.

### `cli_remove`

[`slife2/plugins/cli/server.py:378`](slife2/plugins/cli/server.py#L378)

Forget a command recorded under `cli:`.

The program itself stays on the machine. A name that is not recorded is
refused, so a success means the entry is gone.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The key it is recorded under. |

**Returns**

`name` and `removed`, which a success makes true.

### `cli_set`

[`slife2/plugins/cli/server.py:327`](slife2/plugins/cli/server.py#L327)

Record a command that is already installed on this machine.

It writes the entry down for a search to find, installing and running
nothing. This is the whole entry: a field left out is dropped, so
restate everything.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The key it is recorded under. |
| `command` | `string` | yes | — | The invocation, resolved on `PATH` — one string, which may be more than one word (`python -m mytool`). |
| `description` | `string` | yes | — | What it does, in the words a search should find it by. |
| `install` | `string` | no | `""` | How to get it, for somebody who does not have it. |
| `source` | `object or null` | no | `null` | Where the entry came from — `url`, `type`, `version`. |
| `enabled` | `boolean` | no | `true` | False records it and keeps it out of the catalogue. |

**Returns**

`name`, `enabled` and `state` (`on`, or `off` for a switch written
off) — and `missing_program`, the program this entry names that is
not on `PATH`, when there is one. That is a note about the machine
and not about the entry: it is written down either way.

### `cli_set_enabled`

[`slife2/plugins/cli/server.py:402`](slife2/plugins/cli/server.py#L402)

Switch a recorded command on or off, keeping the entry.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The key it is recorded under. |
| `enabled` | `boolean` | yes | — | True records it as usable; False keeps it out of the catalogue, the same switch as on a server. |

**Returns**

`name`, `enabled`, and `state` — `on` or `off` — which is the same
word the listing's rows carry.

## History

The agent's own stored turns.

Source: [`slife2/plugins/context/server.py`](slife2/plugins/context/server.py)

### `turn_list`

[`slife2/plugins/context/server.py:400`](slife2/plugins/context/server.py#L400)

Browse what was said before, newest first, one line per turn.

Each line carries `turn_id`, when the turn was, and what was asked and
answered, both cut short. Read one whole with `turn_read`. Your own
history: no argument names whose memory to read.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `since` | `string or null` | no | `null` | Lower bound on when the turn was written — an ISO date or datetime, or one of: today, yesterday, tomorrow, now, last\|this week\|month\|quarter\|year, or '<N> day(s)\|week(s)\|month(s)\|year(s) ago'. Omit for no lower bound. |
| `until` | `string or null` | no | `null` | Upper bound on the same grammar. A date means the whole day. Omit for no upper bound. |
| `limit` | `integer` | no | `20` | How many turns this page may hold. |
| `offset` | `integer` | no | `0` | How many turns to skip — that is how you page back. |

**Returns**

`entries` — `turn_id`, `created_at`, `user_message`,
`assistant_message` and `token_count` (the turn's own estimated size)
per turn — and `total`, how many turns the window holds, so
`offset + len(entries) < total` says whether more remain.

### `turn_read`

[`slife2/plugins/context/server.py:448`](slife2/plugins/context/server.py#L448)

One turn in full, by the id `turn_list` gave you.

Everything it holds, in the order it happened: what was said, every
assistant message, the tool calls and their answers. Your own history,
like `turn_list` — an id resolves inside it and nowhere else.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `turn_id` | `integer` | yes | — | The turn to read, as `turn_list` reported it. |

**Returns**

The whole turn: `turn_id`, `messages`, `summary`, `tags`, `created_at`, `completed_at`, `channel`, `what_model` and `token_count` (the turn's own size, estimated from its messages).

## File tunnel

Public URLs for local files.

Source: [`slife2/plugins/file_tunnel/server.py`](slife2/plugins/file_tunnel/server.py)

### `expose_local_image`

[`slife2/plugins/file_tunnel/server.py:432`](slife2/plugins/file_tunnel/server.py#L432)

Give a local file a public HTTPS URL, so something that cannot read
this machine's disk can fetch it — a vision model asked about a
picture, a chat API that only accepts links.

The file is not moved or copied: it is served from where it is, for as
long as this process is running. It stops being served the moment the
file changes, and `unexpose_local_image` stops it sooner — call that
when the file is no longer wanted out there, and expect the URL to
outlive the conversation otherwise.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | `string` | yes | — | The file on this machine, as an absolute path or one relative to where slife2 was started. |

**Returns**

`url` — the public link to hand to whoever needs the file — its
`token`, which is what `unexpose_local_image` takes — and `file`,
the name being served.

### `unexpose_local_image`

[`slife2/plugins/file_tunnel/server.py:500`](slife2/plugins/file_tunnel/server.py#L500)

Stop serving a file that `expose_local_image` published. Its URL
answers 403 from then on, and the file itself is untouched.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `token` | `string` | yes | — | The last part of the share URL — the `xxxx` in `https://…/share/xxxx`. |

**Returns**

`token` — the link that was withdrawn, whether it arrived as a bare
token or as a whole share URL. From here that URL answers 403.

## MCP configuration

Other people's MCP servers, as configured entries.

Source: [`slife2/plugins/mcp_tools/server.py`](slife2/plugins/mcp_tools/server.py)

### `mcp_list`

[`slife2/plugins/mcp_tools/server.py:170`](slife2/plugins/mcp_tools/server.py#L170)

List the MCP servers configured under `mcp:`, with their state.

Each entry as the file writes it — how it is reached, what it is for,
and whether its link is answering.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | no | `0` | How many entries this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many entries to skip — that is how you page. |

**Returns**

`servers` — one row per entry, each with `name`, `state`
(`ready`, `connecting`, `failed`, `idle` or `off`) and whatever the
file has of it — and `total`, `limit` and `offset`, so
`offset + len(servers) < total` says whether more remain.

### `mcp_list_tools`

[`slife2/plugins/mcp_tools/server.py:284`](slife2/plugins/mcp_tools/server.py#L284)

List the tools one MCP server offers.

These are the names to call them by, each `{name}__{tool}`.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The server — the key it is written under. |
| `limit` | `integer` | no | `0` | How many tools this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many tools to skip — that is how you page. |

**Returns**

`name`, `state` (`ready`, `connecting`, `failed`, `idle` or `off`),
`tools` — `name` and `description` per tool — `total`, `limit` and
`offset`, and `error` when the link has one. An empty `tools` with a
`state` other than `ready` is why it is empty.

### `mcp_remove`

[`slife2/plugins/mcp_tools/server.py:251`](slife2/plugins/mcp_tools/server.py#L251)

Stop an MCP server and delete its entry from `mcp:`.

Its tools leave the catalogue at the next search. A package the command
names stays on the machine. A name that is not in `mcp:` is refused, so
a success means the entry is gone.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The server to remove — the key it is written under. |

**Returns**

`removed`, whether the entry was in the file, and `stopped`, whether
this process was holding a link to it.

### `mcp_set`

[`slife2/plugins/mcp_tools/server.py:191`](slife2/plugins/mcp_tools/server.py#L191)

Add or update an MCP server under `mcp:` (upsert).

One entry is one transport: `command` for a process slife2 starts, `url`
for an endpoint somebody else runs. This is the whole entry — a field
left out is dropped, so restate everything, which is also how a server
switches between the two. It is connected here and now, and the answer
says whether it answered.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The server's name — the key it is written under, and the prefix every tool of it reaches you as (`{name}__{tool}`). |
| `command` | `string` | no | `""` | stdio — the program to start (npx, uvx, python). |
| `args` | `array<string> or null` | no | `null` | stdio — its arguments. |
| `env` | `object or null` | no | `null` | stdio — environment overrides; `${VAR}` resolves when needed. |
| `url` | `string` | no | `""` | http — the endpoint (SSE or Streamable HTTP). |
| `headers` | `object or null` | no | `null` | http — request headers, `${VAR}` for anything secret. |
| `cwd` | `string` | no | `""` | stdio — the process's working directory; the data directory when left out. |
| `description` | `string` | no | `""` | What it is for, for whoever reads the config. |
| `enabled` | `boolean` | no | `true` | Connect it now. False writes the entry switched off. |
| `autoload` | `boolean` | no | `false` | Every tool it offers starts in your list and is never evicted. |

**Returns**

`name` and `enabled`; `state`, one of `ready`, `connecting`,
`failed`, `idle` or `off`; `tools`, how many it offers, when it
answered; `large`, true when that count is past a page's worth and
the next search has a row per tool to write and embed; and `error`,
what it said, when it has not answered.

### `mcp_set_enabled`

[`slife2/plugins/mcp_tools/server.py:268`](slife2/plugins/mcp_tools/server.py#L268)

Switch an MCP server on or off, keeping its entry in `mcp:`.

Off keeps the entry and stops the server being connected or started.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The server — the key it is written under. |
| `enabled` | `boolean` | yes | — | True connects it; False stops it and keeps the entry. |

**Returns**

The same payload `mcp_set` answers with: `name`, `enabled`, `state`,
and `tools` or `error` when the link has one of them.

## Media

Image, video, speech and transcription models.

Source: [`slife2/plugins/media/server.py`](slife2/plugins/media/server.py)

### `generate_image`

[`slife2/plugins/media/server.py:199`](slife2/plugins/media/server.py#L199)

Draw an image from a description, and answer where it was written.

The picture is a file on this machine: copy it, or save it into the
cabinet, to keep it.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `prompt` | `string` | yes | — | What the image should be. |
| `model` | `string` | no | `""` | `provider/model`, or a bare name. Empty uses the configured default for images. |
| `size` | `string` | no | `""` | `WIDTH*HEIGHT`, e.g. `1024*1024`. Empty uses the model's own default. |
| `image` | `string` | no | `""` | A picture to work from — a local path, an `http(s)` URL or a `data:` URI. For models that take a reference image. |
| `folder` | `string` | no | `""` | A folder inside the media directory to write into. Empty writes at the top. |

**Returns**

`path` — the file the picture was written to.

### `generate_video`

[`slife2/plugins/media/server.py:243`](slife2/plugins/media/server.py#L243)

Render a video from a description, and answer where it was written.

A render takes minutes: one that outruns `timeout` fails, with the
provider's job id in the message so the same render can be asked after
there instead of paid for twice. Nothing here fetches it back — a call
that outran its own patience is told what became of it and nothing more.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `prompt` | `string` | yes | — | What happens in the video. |
| `model` | `string` | no | `""` | `provider/model`, or a bare name. Empty uses the configured default for video. |
| `image` | `string` | no | `""` | A first frame to animate, as a local path. For image-to-video models. |
| `resolution` | `string` | no | `""` | `720P`, `1080P`. Empty uses the model's own default. |
| `ratio` | `string` | no | `""` | `16:9`, `9:16`, `1:1`. Empty uses the model's own default. |
| `duration` | `integer` | no | `0` | Seconds. Zero uses the model's own default. |
| `folder` | `string` | no | `""` | A folder inside the media directory to write into. |
| `timeout` | `number` | no | `0` | How long to wait, in seconds. Zero or less uses the default. |

**Returns**

`path` — the file the video was written to.

### `text_to_speech`

[`slife2/plugins/media/server.py:303`](slife2/plugins/media/server.py#L303)

Say something out loud, into an audio file, and answer where it is.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `text` | `string` | yes | — | What to say. |
| `model` | `string` | no | `""` | `provider/model`, or a bare name. Empty uses the configured default for speech. |
| `voice` | `string` | no | `""` | Which voice, by the provider's own name. Empty uses the model's default. |
| `folder` | `string` | no | `""` | A folder inside the media directory to write into. |

**Returns**

`path` — the file the audio was written to.

### `transcribe_audio`

[`slife2/plugins/media/server.py:333`](slife2/plugins/media/server.py#L333)

Write down what an audio file says, and answer the text.

A recording can be longer than one answer may carry, so this is read in
pages: `offset` says where to start and `limit` how much to ask for.
Read the rest from `offset + len(text)` — an hour of speech is several
turns of reading, and every page says where the next one begins.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | `string` | yes | — | The audio file, as a local path. |
| `model` | `string` | no | `""` | `provider/model`, or a bare name. Empty uses the configured default for transcription. |
| `offset` | `integer` | no | `0` | The character to start at; `0` is the beginning. |
| `limit` | `integer` | no | `0` | The most characters to answer with. `0` answers as much as fits the tool-result budget, which is also the ceiling on any `limit` — a page is never longer than the budget whatever is asked for. |

**Returns**

`text` — the transcript from `offset` — `total`, how many characters
the whole transcript is, and `offset`/`limit` as asked. So
`offset + len(text) < total` says whether more remains.

## Peer agent network

The remote A2A mesh.

Source: [`slife2/plugins/a2a/server.py`](slife2/plugins/a2a/server.py)

### `peer_agent_broadcast`

[`slife2/plugins/a2a/server.py:2027`](slife2/plugins/a2a/server.py#L2027)

Publish a fire-and-forget event to every peer agent.

Nobody answers one and it is delivered at most once, so it is for
information somebody may act on and not for anything you need a reply
to — use `peer_agent_send_message` for that. It arrives as a `[PeerAgent:…]` of type
'broadcast', with no task and no completion.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `event` | `string` | yes | — | The event to publish. |

**Returns**

`broadcast` (the event sent) and `note`.

### `peer_agent_cancel_task`

[`slife2/plugins/a2a/server.py:1832`](slife2/plugins/a2a/server.py#L1832)

Withdraw a task you sent to another peer agent.

`status` is the answer: 'cancelled' when the withdrawal went out,
'completed'/'failed'/'cancelled' when the task had already ended (a
finished result is never lost by asking), or 'not_found' for an id this
process never sent.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent` | `string` | yes | — | The member the task was sent to. |
| `task_id` | `string` | yes | — | The task to withdraw. |

**Returns**

`peer`, `task_id` and `status` — `cancelled`, `completed`, `failed` or `not_found`.

### `peer_agent_group_leave`

[`slife2/plugins/a2a/server.py:2317`](slife2/plugins/a2a/server.py#L2317)

Leave a group you are in — your own decision, no one's approval.

If you are the owner, ownership passes to the first remaining member;
if nobody remains, the group is disbanded. The other members are
notified, and the group disappears from your list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to leave. |

**Returns**

`group`, `left`, `owner` and `members` after the leave, and `sent`; a last-member leave also says `disbanded`.

### `peer_agent_group_list`

[`slife2/plugins/a2a/server.py:2190`](slife2/plugins/a2a/server.py#L2190)

List the group chats this conversation knows — ones you made and ones
you were pulled into.

Every row is `{"group": …, "owner": …, "role": "owner"|"member"}`.
`role` says whether the group is yours to disband. The members are a
separate read: `peer_agent_group_member_list`. This is what this conversation
has persisted locally, not a live check of who is connected.

`error` says when the local record could not be read.

**Parameters**

No parameters.

**Returns**

`agent_name`, `groups` — rows of `group`, `owner` and `role` (`owner` or `member`) — and `error`.

### `peer_agent_group_member_list`

[`slife2/plugins/a2a/server.py:2234`](slife2/plugins/a2a/server.py#L2234)

List the members of a group this conversation knows.

Returns `group`, `owner`, `members` (including the owner), your `role`
(`owner` or `member`) and `error`. This is the separate member read,
as in a chat app: the message marker does not repeat the roster, so a
large group is not copied into every message. `peer_agent_group_list` shows
the groups you know. Membership is not activity: a member that never
answers is still a member. This is what this conversation has
persisted locally, not a live check of who is connected.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group whose members to list. |

**Returns**

`group`, `owner`, `members` (including the owner), `role` (`owner` or `member`) and `error`.

### `peer_agent_group_remove`

[`slife2/plugins/a2a/server.py:2271`](slife2/plugins/a2a/server.py#L2271)

Disband a group you own.

Only the owner can do this; a member's way out is `peer_agent_group_leave`,
which hands the group on rather than ending it. The members are
notified and the group disappears from your list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to disband. |

**Returns**

`group`, `disbanded` and `sent`.

### `peer_agent_group_send_message`

[`slife2/plugins/a2a/server.py:2386`](slife2/plugins/a2a/server.py#L2386)

Send a message to every other member of a group you belong to.

**A reply is this call**: ending a turn sends nothing. You are never
one of the recipients. The message arrives with a `[PeerAgentGroup:…]`
naming `from` (the sender), the group and its owner. The members are a
separate read — `peer_agent_group_member_list` — so a large group is not
repeated in every message. Address people with `@name` as in a chat
group:
`@researcher` puts that member's name in the message, and `@all`
addresses everyone. A mention is attention, not delivery — every other
member still receives the message. Delivery is fire-and-forget — no
task, no completion — so a member that is not on the network now may
miss it. **A group chat is casual**: nobody has to answer, and silence
is not 'pending' or 'undelivered'; `sent` is only a publish count. Use
`peer_agent_send_message` when you need an answer.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to speak in. |
| `message` | `string` | yes | — | The words; `@name` and `@all` address members, as in a chat group. |

**Returns**

`group`, `sent` (how many other members were published to) and `note`.

### `peer_agent_group_set`

[`slife2/plugins/a2a/server.py:2099`](slife2/plugins/a2a/server.py#L2099)

Create a group chat and pull in the peer agents you name. **You become
its owner**, and the roster is the whole membership — nobody has to
accept. Calling this again on a group you own replaces the roster (the
way to add or remove someone); a member leaves with `peer_agent_group_leave`.

Every member's group list learns the roster from a group-set event.
That notice is fire-and-forget: a member not on the network now may
miss it, but the next group message updates their group list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group's name; the handle every group tool uses. |
| `members` | `array<string>` | yes | — | The member names to pull in; you are added as owner. |

**Returns**

`group`, `owner`, `members` (the roster, owner first), `sent` (how many members were notified) and `note`.

### `peer_agent_list`

[`slife2/plugins/a2a/server.py:1856`](slife2/plugins/a2a/server.py#L1856)

List the peer agents — you first, then each by name.

Every card is `{"agent_name": …, "status": "online"|"offline"}`, and
`status` on the first one is your own state on this network.

Offline entries are the ones that have said they are leaving, so
they are listed rather than hidden.

`error` says why the list is short when it is.

**Parameters**

No parameters.

**Returns**

`agent_name`, `status` (your own), `broker`, `org`, `unit`, `agents` — cards of `{agent_name, status}` — and `error`, empty when nothing is wrong.

### `peer_agent_send_message`

[`slife2/plugins/a2a/server.py:1750`](slife2/plugins/a2a/server.py#L1750)

Send a message to another peer agent. **A reply is this call**: ending a turn
sends nothing to the other side.

`message_type` decides what it is. 'task_request' (the default) asks for
work and returns an id straight away — the answer arrives later, on its
own, as a `[PeerAgent:…]` of type 'task_response'. Never wait for it and
never poll: there is nothing to poll. 'task_response' completes a task
somebody sent you, and needs the `task_id` from that task's `[PeerAgent:…]`
marker. 'message' is a plain conversation — no task, no completion, and
the reply arrives as a message.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent` | `string` | yes | — | The member's name, as the network's list tool gives it. |
| `message` | `string` | yes | — | The message, or the result for a `task_response`. |
| `message_type` | `string` | no | `"task_request"` | `task_request` (default), `task_response` or `message`. |
| `task_id` | `string` | no | `""` | The task being answered, for `task_response`. |

**Returns**

`peer` (the member addressed), `task_id`, `message_type` and `note`; a `task_response` answers `peer`, `task_id` and `completed`.

### `peer_agent_task_list`

[`slife2/plugins/a2a/server.py:1985`](slife2/plugins/a2a/server.py#L1985)

List the tasks you have open on the peer network.

A task is open while it is waiting for an answer ('sent') or while an
answer is owed ('received'). Every row is `{"task_id": …, "member": …,
"direction": "sent"|"received", "status": …, "since": …}`, and `member`
is the name the task is with — what `peer_agent_send_message` and `peer_agent_cancel_task` address.

`status` is 'pending' for a task still running, or 'unreachable' for one
you sent that was never acknowledged — a late answer can still arrive, so
check before sending it again.

An answer that already arrived is not listed; it is in this conversation.

**Parameters**

No parameters.

**Returns**

`agent_name`, `tasks` — rows of `task_id`, `member`, `direction` (`sent` or `received`), `status` (`pending` or `unreachable`) and `since` — and `error`.

## REST configuration

OpenAPI documents exposed as tools.

Source: [`slife2/plugins/restapi/server.py`](slife2/plugins/restapi/server.py)

### `rest_api_list`

[`slife2/plugins/restapi/server.py:217`](slife2/plugins/restapi/server.py#L217)

List the REST APIs configured under `rest-api:`, with their state.

Each entry as the file writes it — the spec, the base URL, the
credential — and whether it is answering.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | no | `0` | How many entries this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many entries to skip — that is how you page. |

**Returns**

`apis` — one row per entry, each with `name`, `state` (`ready`,
`connecting`, `failed`, `idle` or `off`) and whatever the file has
of it — and `total`, `limit` and `offset`, so
`offset + len(apis) < total` says whether more remain.

### `rest_api_list_tools`

[`slife2/plugins/restapi/server.py:324`](slife2/plugins/restapi/server.py#L324)

List the operations one REST API exposes.

These are the names to call them by, each `{name}__{operation}`, one per
endpoint in the spec.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The API — the key it is written under. |
| `limit` | `integer` | no | `0` | How many operations this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many operations to skip — that is how you page. |

**Returns**

`name`, `state` (`ready`, `connecting`, `failed`, `idle` or `off`),
`operations` — `name` and `description` per operation — `total`,
`limit` and `offset`, and `error` when the link has one. An empty
`operations` with a `state` other than `ready` is why it is empty.

### `rest_api_remove`

[`slife2/plugins/restapi/server.py:290`](slife2/plugins/restapi/server.py#L290)

Stop a REST API and delete its entry from `rest-api:`.

Its operations leave the catalogue at the next search. A name that is
not in `rest-api:` is refused, so a success means the entry is gone.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The API to remove — the key it is written under. |

**Returns**

`removed`, whether the entry was in the file, and `stopped`, whether
this process was holding a link to it.

### `rest_api_set`

[`slife2/plugins/restapi/server.py:238`](slife2/plugins/restapi/server.py#L238)

Add or update a REST API under `rest-api:` (upsert).

Every endpoint in the OpenAPI document becomes an operation named
`{name}__{operation}`, so one spec can be hundreds of them. This is the
whole entry: a field left out is dropped, so restate everything.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The API's name — the key it is written under, and the prefix every operation of it reaches you as (`{name}__{operation}`). |
| `spec` | `string` | yes | — | The OpenAPI document's URL (JSON or YAML). It is fetched here, so it has to be http(s) and reachable. |
| `base_url` | `string` | yes | — | Where the API actually is, overriding whatever server the document names — e.g. https://api.github.com. |
| `api_key` | `string` | no | `""` | The credential's *name*, not the secret: `GITHUB_TOKEN` stores `${GITHUB_TOKEN}`. Omit it for an API that needs no key. |
| `description` | `string` | no | `""` | What the API is for. |
| `enabled` | `boolean` | no | `true` | Serve it now. False writes the entry switched off. |
| `autoload` | `boolean` | no | `false` | Every operation starts in your tool list and is never evicted. Check how many there are first. |

**Returns**

The payload `mcp_set` answers with over this family's words:
`name`, `enabled`, `state` (`ready`, `connecting`, `failed`, `idle`
or `off`), `operations` when it answered, `large` when that count is
past a page's worth, `error` when it has not — and `unresolved`, the
credential reference that resolves to nothing yet, when there is one.

### `rest_api_set_enabled`

[`slife2/plugins/restapi/server.py:306`](slife2/plugins/restapi/server.py#L306)

Switch a REST API on or off, keeping its entry in `rest-api:`.

A spec generates an operation per endpoint, so one served API can
outweigh every other tool in the catalogue; off keeps it written down.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The API — the key it is written under. |
| `enabled` | `boolean` | yes | — | True serves it; False stops it and keeps the entry. |

**Returns**

The same payload `rest_api_set` answers with, and no `unresolved`:
writing the entry is what put the credential there, and this call
changed only the switch.

## Schedule

Cron tasks and their ledger.

Source: [`slife2/plugins/schedule/server.py`](slife2/plugins/schedule/server.py)

### `schedule_list`

[`slife2/plugins/schedule/server.py:926`](slife2/plugins/schedule/server.py#L926)

This agent's scheduled tasks, by name, with when each next comes due.

Each row carries the whole description, shortened, and `next_run` — which
is what the expression means right now rather than something stored, so
it is right the moment you read it. A task that is switched off is here
too, and says so.

`limit` bounds this page (at most 200); left out it is 50. `offset` skips
that many, which is how you page back.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | no | `0` | How many tasks this page may hold (at most 200; default 50). |
| `offset` | `integer` | no | `0` | How many tasks to skip. |

**Returns**

`tasks` — the rows of this page — and `total`, so
`offset + len(tasks) < total` says whether more remain.

### `schedule_remove`

[`slife2/plugins/schedule/server.py:911`](slife2/plugins/schedule/server.py#L911)

Forget a scheduled task, and everything it ever did.

Deleting rather than switching off, and the difference is the history:
`schedule_set(enabled=False)` keeps the ledger, and this takes it. Do it
when the task is not a thing any more; switch it off when it is.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The scheduled task's name. |

**Returns**

`name` and `removed` (always `true`).

### `schedule_run_now`

[`slife2/plugins/schedule/server.py:991`](slife2/plugins/schedule/server.py#L991)

Start a scheduled run's work now — which is what a trigger asks for.

**This is what to call when a task has come due**, with the `due_at` the
trigger carried: it starts the work as a background task of yours and
returns at once. Do not wait for it and do not follow it up — the answer
arrives on its own, and **the run closes itself** when that task ends, so
nothing here has to be remembered later.

`due_at` names the run — the moment it was due, as the trigger carried
it. Any spelling of that moment will do; a value that is not a moment is
refused rather than answered with "no such run".

With no `due_at` it is a new run due at this moment, for work somebody
wants done early. With the `due_at` of a run that was **missed** or that
failed to start, it re-opens *that* run — the same run, the same due time
— which is how a gap is answered.

The answer is the run's row, with the background task's id on it. A
`pending` row means the work is in flight; `failed` means it could not be
started, and the error says why.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The scheduled task's name. |
| `due_at` | `string` | no | `""` | The moment the run was due; empty means now. |

**Returns**

The run's row: `name`, `due_at`, `status`, `ran_at`, `note`, `error` and `agent_task_id`.

### `schedule_run_skip`

[`slife2/plugins/schedule/server.py:1048`](slife2/plugins/schedule/server.py#L1048)

Close a run that is not going to happen, and say why.

The run by its due time — any spelling of that moment will do — or the
newest one still open; and the opposite ending. A skipped run is an
answer rather than a gap: the ledger says the task came due, that nobody
did it, and what the reason was.

**A task that has come due is not yours to decline** — the trigger tells
you to run it. This is for the runs that *did not happen*: one that could
not be started, one whose moment passed while slife2 was not running,
where doing it late is the other answer, and a trigger nobody acted on,
which sits `pending` accounting for nothing until somebody says so.

**And a run that is *being* done is not one of those.** `pending` with an
`agent_task_id` means the work is happening now, and its answer is on its
way to you however this call goes — so skipping it is refused, and the
row you get back is the one that is still in flight. A run closes itself
when the task doing it ends; `schedule_runs` says which run is which.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The scheduled task's name. |
| `due_at` | `string` | no | `""` | The run's due moment; empty means the newest open run. |
| `note` | `string` | no | `""` | Why it is being skipped, recorded on the run. |

**Returns**

The run's row: `name`, `due_at`, `status` (`skipped`), `ran_at`, `note`, `error` and `agent_task_id`.

### `schedule_runs`

[`slife2/plugins/schedule/server.py:955`](slife2/plugins/schedule/server.py#L955)

What a scheduled task has actually done, newest first.

`name` narrows it to one task, `status` to one state — `pending`, `ran`,
`failed`, `missed` or `skipped` — and `limit` bounds this page (at most
200); left out it is 20. `offset` skips that many, which is how you page
back through a ledger that only ever grows. Each row is one fire, named
by the moment it was *due* rather than by when it ran, with the note
whoever closed it left.

This is the answer to "did it run", and it is the only place that answer
exists: a run nobody closed stays here until somebody does.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | no | `""` | Narrow to one scheduled task. |
| `status` | `string` | no | `""` | One run state: `pending`, `ran`, `failed`, `missed` or `skipped`. |
| `limit` | `integer` | no | `0` | How many runs this page may hold (at most 200; default 20). |
| `offset` | `integer` | no | `0` | How many runs to skip. |

**Returns**

`runs` — the rows of this page — and `total`, so
`offset + len(runs) < total` says whether more remain.

### `schedule_set`

[`slife2/plugins/schedule/server.py:855`](slife2/plugins/schedule/server.py#L855)

Write a scheduled task, or replace one that exists.

The clock runs it — not you, and not the person. When it comes due you
are handed the description as a turn, with the order to run it as a
background task of your own; say in `description` what to do and what
"done" looks like, because it is the whole of what that task is given and
nobody can answer a question it raises.

`cron` is the usual five fields — `minute hour day-of-month month
day-of-week` — and `0 9 * * *` is every morning at nine. `timezone` is an
IANA name like `Asia/Shanghai`; left out, the task follows this machine's
clock as it is now and therefore does not follow a daylight-saving
change.

This is the whole task, so anything you leave out is cleared. `enabled`
false keeps it written down and never fires it.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The task's name, used by every other schedule tool. |
| `description` | `string` | yes | — | What to do when it comes due, and what done looks like. |
| `cron` | `string` | yes | — | Five fields: `minute hour day-of-month month day-of-week`. |
| `timezone` | `string` | no | `""` | IANA name, e.g. `Asia/Shanghai`; empty follows this machine's clock. |
| `enabled` | `boolean` | no | `true` | `false` keeps the task written down and never fires it. |

**Returns**

The task row: `name`, `description`, `cron`, `timezone`, `enabled`, `created_at`, `last_due` and `next_run`.

## Skills

Playbooks under `skills/`.

Source: [`slife2/plugins/skills/server.py`](slife2/plugins/skills/server.py)

### `skill_list`

[`slife2/plugins/skills/server.py:383`](slife2/plugins/skills/server.py#L383)

List the playbooks installed on this machine, with their state.

This is where the name `skill_use` takes comes from: each row says what
the playbook is for and whether what it requires is at hand — a key, a
program.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | no | `0` | How many skills this page may hold. Omit for the cap. |
| `offset` | `integer` | no | `0` | How many skills to skip — that is how you page. |

**Returns**

`skills` — one row per skill, each with `name`, `state` (`on` or
`off`) and, when it has them, `description` and `requirements` —
and `total`, `limit` and `offset`, so
`offset + len(skills) < total` says whether more remain.

### `skill_remove`

[`slife2/plugins/skills/server.py:462`](slife2/plugins/skills/server.py#L462)

Uninstall a skill: delete its directory and everything in it.

The `skills:` entry naming what it was given stays, and `skill_set`
installs it again.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The skill to remove. |

**Returns**

`name` and `removed`, which a success makes true. A name with no
folder is refused: nothing was deleted, so the call failed.

### `skill_set`

[`slife2/plugins/skills/server.py:425`](slife2/plugins/skills/server.py#L425)

Install or replace a skill: write a playbook onto this machine.

`SKILL.md` is the playbook and has to be one of the paths; anything else
you write lands beside it. Write the procedure for one kind of job — the
commands, the order, the steps that are easy to get wrong — with paths
relative to the skill's directory. A skill of the same name is replaced
in one step.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The directory it lives in — what `skill_use` takes. |
| `files` | `array<object>` | yes | — | The files to write, each `{"path": "SKILL.md", "content": "..."}`. Paths are relative to the skill's own directory, and one that leaves it is refused. |

**Returns**

`name`, `state` (`on`), and `files`, how many were written. A
refused install raises instead, so a success means the folder is
there.

### `skill_set_enabled`

[`slife2/plugins/skills/server.py:485`](slife2/plugins/skills/server.py#L485)

Switch a skill on or off, keeping it installed.

Off keeps the files, stops it being findable, and `skill_use` refuses
it. The switch lives in the config.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The skill. |
| `enabled` | `boolean` | yes | — | True offers it again; False keeps it out of the way. |

**Returns**

`name`, `enabled`, and `state` — `on` or `off` — the same word a
row of `skill_list` carries.

### `skill_use`

[`slife2/plugins/skills/server.py:236`](slife2/plugins/skills/server.py#L236)

Read a skill: a playbook to follow rather than a tool to call.

Read the one for a job before starting it. The answer is the skill's
whole `SKILL.md`, preceded by the root its paths are relative to. It is
the document itself rather than JSON, and it is not shortened here: a
playbook cut at the result budget would stop in the middle of a step.
One too large for a conversation is the harness's oversized result,
whose selection is chosen from the document's own lines.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | `string` | yes | — | The skill's name, e.g. `browser-harness`. |

**Returns**

The skill's whole `SKILL.md` as text, preceded by the root the paths
in it are relative to and a note saying whether what it requires is
at hand.

## Subagent network

The local network of an agent's subagents.

Source: [`slife2/plugins/a2a/server.py`](slife2/plugins/a2a/server.py)

### `subagent_broadcast`

[`slife2/plugins/a2a/server.py:2027`](slife2/plugins/a2a/server.py#L2027)

Publish a fire-and-forget event to every member of your subagent network.

Nobody answers one and it is delivered at most once, so it is for
information somebody may act on and not for anything you need a reply
to — use `subagent_send_message` for that. It arrives as a `[Agent:…]` or `[Subagent:…]` of type
'broadcast', with no task and no completion.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `event` | `string` | yes | — | The event to publish. |

**Returns**

`broadcast` (the event sent) and `note`.

### `subagent_cancel_task`

[`slife2/plugins/a2a/server.py:1832`](slife2/plugins/a2a/server.py#L1832)

Withdraw a task you sent to another member of your subagent network —
one of the subagents in it, or your agent if you are one of them.

`status` is the answer: 'cancelled' when the withdrawal went out,
'completed'/'failed'/'cancelled' when the task had already ended (a
finished result is never lost by asking), or 'not_found' for an id this
process never sent.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent` | `string` | yes | — | The member the task was sent to. |
| `task_id` | `string` | yes | — | The task to withdraw. |

**Returns**

`peer`, `task_id` and `status` — `cancelled`, `completed`, `failed` or `not_found`.

### `subagent_group_leave`

[`slife2/plugins/a2a/server.py:2317`](slife2/plugins/a2a/server.py#L2317)

Leave a group you are in — your own decision, no one's approval.

If you are the owner, ownership passes to the first remaining member;
if nobody remains, the group is disbanded. The other members are
notified, and the group disappears from your list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to leave. |

**Returns**

`group`, `left`, `owner` and `members` after the leave, and `sent`; a last-member leave also says `disbanded`.

### `subagent_group_list`

[`slife2/plugins/a2a/server.py:2190`](slife2/plugins/a2a/server.py#L2190)

List the group chats this conversation knows — ones you made and ones
you were pulled into.

Every row is `{"group": …, "owner": …, "role": "owner"|"member"}`.
`role` says whether the group is yours to disband. The members are a
separate read: `subagent_group_member_list`. This is what this conversation
has persisted locally, not a live check of who is connected.

`error` says when the local record could not be read.

**Parameters**

No parameters.

**Returns**

`agent_name`, `groups` — rows of `group`, `owner` and `role` (`owner` or `member`) — and `error`.

### `subagent_group_member_list`

[`slife2/plugins/a2a/server.py:2234`](slife2/plugins/a2a/server.py#L2234)

List the members of a group this conversation knows.

Returns `group`, `owner`, `members` (including the owner), your `role`
(`owner` or `member`) and `error`. This is the separate member read,
as in a chat app: the message marker does not repeat the roster, so a
large group is not copied into every message. `subagent_group_list` shows
the groups you know. Membership is not activity: a member that never
answers is still a member. This is what this conversation has
persisted locally, not a live check of who is connected.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group whose members to list. |

**Returns**

`group`, `owner`, `members` (including the owner), `role` (`owner` or `member`) and `error`.

### `subagent_group_remove`

[`slife2/plugins/a2a/server.py:2271`](slife2/plugins/a2a/server.py#L2271)

Disband a group you own.

Only the owner can do this; a member's way out is `subagent_group_leave`,
which hands the group on rather than ending it. The members are
notified and the group disappears from your list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to disband. |

**Returns**

`group`, `disbanded` and `sent`.

### `subagent_group_send_message`

[`slife2/plugins/a2a/server.py:2386`](slife2/plugins/a2a/server.py#L2386)

Send a message to every other member of a group you belong to.

**A reply is this call**: ending a turn sends nothing. You are never
one of the recipients. The message arrives with a `[SubagentGroup:…]`
naming `from` (the sender), the group and its owner. The members are a
separate read — `subagent_group_member_list` — so a large group is not
repeated in every message. Address people with `@name` as in a chat
group:
`@researcher` puts that member's name in the message, and `@all`
addresses everyone. A mention is attention, not delivery — every other
member still receives the message. Delivery is fire-and-forget — no
task, no completion — so a member that is not on the network now may
miss it. **A group chat is casual**: nobody has to answer, and silence
is not 'pending' or 'undelivered'; `sent` is only a publish count. Use
`subagent_send_message` when you need an answer.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group to speak in. |
| `message` | `string` | yes | — | The words; `@name` and `@all` address members, as in a chat group. |

**Returns**

`group`, `sent` (how many other members were published to) and `note`.

### `subagent_group_set`

[`slife2/plugins/a2a/server.py:2099`](slife2/plugins/a2a/server.py#L2099)

Create a group chat and pull in the members of your subagent network you name. **You become
its owner**, and the roster is the whole membership — nobody has to
accept. Calling this again on a group you own replaces the roster (the
way to add or remove someone); a member leaves with `subagent_group_leave`.

Every member's group list learns the roster from a group-set event.
That notice is fire-and-forget: a member not on the network now may
miss it, but the next group message updates their group list.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `group` | `string` | yes | — | The group's name; the handle every group tool uses. |
| `members` | `array<string>` | yes | — | The member names to pull in; you are added as owner. |

**Returns**

`group`, `owner`, `members` (the roster, owner first), `sent` (how many members were notified) and `note`.

### `subagent_list`

[`slife2/plugins/a2a/server.py:1856`](slife2/plugins/a2a/server.py#L1856)

List the members of your subagent network —
you first, then each by name.

Every card is `{"agent_name": …, "status": "online"|"offline"}`, and
`status` on the first one is your own state on this network.

A member is listed whether or not it is awake: `offline` means
nothing is listening for it at this moment, and a message to it
wakes it.

`error` says why the list is short when it is.

**Parameters**

No parameters.

**Returns**

`agent_name`, `status` (your own), `broker`, `org`, `unit`, `agents` — cards of `{agent_name, status}` — and `error`, empty when nothing is wrong.

### `subagent_send_message`

[`slife2/plugins/a2a/server.py:1750`](slife2/plugins/a2a/server.py#L1750)

Send a message to another member of your subagent network —
one of the subagents in it, or your agent if you are one of them. **A reply is this call**: ending a turn
sends nothing to the other side.

`message_type` decides what it is. 'task_request' (the default) asks for
work and returns an id straight away — the answer arrives later, on its
own, as a `[Agent:…]` or `[Subagent:…]` of type 'task_response'. Never wait for it and
never poll: there is nothing to poll. 'task_response' completes a task
somebody sent you, and needs the `task_id` from that task's `[Agent:…]` or `[Subagent:…]`
marker. 'message' is a plain conversation — no task, no completion, and
the reply arrives as a message.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent` | `string` | yes | — | The member's name, as the network's list tool gives it. |
| `message` | `string` | yes | — | The message, or the result for a `task_response`. |
| `message_type` | `string` | no | `"task_request"` | `task_request` (default), `task_response` or `message`. |
| `task_id` | `string` | no | `""` | The task being answered, for `task_response`. |

**Returns**

`peer` (the member addressed), `task_id`, `message_type` and `note`; a `task_response` answers `peer`, `task_id` and `completed`.

### `subagent_task_list`

[`slife2/plugins/a2a/server.py:1985`](slife2/plugins/a2a/server.py#L1985)

List the tasks you have open on the subagent network.

A task is open while it is waiting for an answer ('sent') or while an
answer is owed ('received'). Every row is `{"task_id": …, "member": …,
"direction": "sent"|"received", "status": …, "since": …}`, and `member`
is the name the task is with — what `subagent_send_message` and `subagent_cancel_task` address.

`status` is 'pending' for a task still running, or 'unreachable' for one
you sent that was never acknowledged — a late answer can still arrive, so
check before sending it again.

An answer that already arrived is not listed; it is in this conversation.

**Parameters**

No parameters.

**Returns**

`agent_name`, `tasks` — rows of `task_id`, `member`, `direction` (`sent` or `received`), `status` (`pending` or `unreachable`) and `since` — and `error`.

## Tool catalogue

Searching, loading and unloading tools.

Source: [`slife2/plugins/toolhub/server.py`](slife2/plugins/toolhub/server.py)

### `_func_tool_unload`

[`slife2/plugins/toolhub/server.py:3124`](slife2/plugins/toolhub/server.py#L3124)

Take tools out of your list. Name the ones you are done with, or leave it empty and the harness's trim runs at a turn boundary. Either way the answer names what went.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `array<string>` | no | — | The tools to take out of your list, as `tool_search` reports them. Empty hands the trim to the harness, which calls it that way at a turn boundary. |

**Returns**

`unloaded` — the names that moved, which is the answer this exists
for — plus `refused` (named but not unloadable), `unknown` (no row
has that name) and `not_loaded` (named but already out of the
list), and `text`, the same thing said in a sentence.

### `func_tool_load`

[`slife2/plugins/toolhub/server.py:1875`](slife2/plugins/toolhub/server.py#L1875)

Put tools into the list you are shown before each request. A name from `tool_search` can be called without loading; loading is what keeps it in that list. The least recently used go when the list passes its cap.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `array<string>` | yes | — | The tool names, as `tool_search` reports them: bare for slife2's own tools, '{server}__{tool}' for a server's. One or several. |

**Returns**

`loaded` — the names now in the model's list — and `failed` — rows of `name` and `reason` for each name that could not be loaded.

### `tool_search`

[`slife2/plugins/toolhub/server.py:1865`](slife2/plugins/toolhub/server.py#L1865)

Find a tool by what it does, across the whole catalogue — including MCP, REST API, CLI and SKILL tools.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `keywords` | `array<string>` | yes | — | Words a tool's own text must contain, every one of them — 'screenshot', 'pdf'. Empty when you have none. |
| `sentences` | `array<string>` | yes | — | What you want to do, in your own words, one phrase per idea — 'take a screenshot of a web page'. Matched by meaning. Empty when you have none. |

**Returns**

`results` — rows of `name`, `category`, `source`, `status`, `loaded`, `matched`, `meaning` and `description` — plus `best` and `confidence`.

- `meaning` — the row's cosine similarity to the closest thing asked; higher is closer. Every row on the page has one, including rows the keyword leg found; `null` only when the row has no vector.
- `matched` — what found the row: `words` when every keyword you gave is in the fields the result prints (name, description, category, source), `parameters` when the match is only in a tool's parameter schema, `body` for the same in a playbook's text, and empty when only the meaning leg returned it.
- `best` — the largest `meaning` on the page (`0.0` when no row has one). `confidence` is derived from it and from the word matches: `sure`, `weak` or `none`.

## WeChat

Login and messaging through WeChat.

Source: [`slife2/plugins/wechat/server.py`](slife2/plugins/wechat/server.py)

### `wechat_check_status`

[`slife2/plugins/wechat/server.py:1649`](slife2/plugins/wechat/server.py#L1649)

How the WeChat link is, and who a reply goes to.

The one place that says where a reply goes: the peer and the
`context_token` their conversation needs.

**Parameters**

No parameters.

**Returns**

`connected` — whether a message can go out — `polling`, whether
messages are being watched for, and `hours_left` of the login's day.
`qr_state` is a login in progress: `waiting` for the code to be
scanned, `scanned` for one to be confirmed on the phone, `confirmed`
or `expired` or `error`, and then call `wechat_login` again. `error`
says what is wrong whenever anything is — empty means nothing is —
and `peer_wechat_id` and `context_token` are who a reply goes to, and
`heard_from` is whether they wrote to you — false means nobody has,
and that peer is the account this login belongs to.

### `wechat_login`

[`slife2/plugins/wechat/server.py:1627`](slife2/plugins/wechat/server.py#L1627)

Get a QR code that logs this agent into WeChat.

Put `qr` in your reply for somebody to scan with the WeChat app: the
account that scans is the account this conversation speaks as, and other
people reach this conversation by messaging it. The code expires in
about ten minutes, and the login lasts about a day.

**Parameters**

No parameters.

**Returns**

`state` — `waiting` for a code to be scanned, or `connected` when
there was already a login, in which case call `wechat_logout` first
to switch accounts — `qr`, the code to put in your reply, `qr_url`,
what the code encodes (send it as a link instead when `qr` is empty,
which is a machine that cannot draw one), and `seconds_left`, how
long the code lasts.

### `wechat_logout`

[`slife2/plugins/wechat/server.py:1703`](slife2/plugins/wechat/server.py#L1703)

Disconnect WeChat and forget the saved login.

The account stays logged in on the phone; what goes is this
conversation's token and its place in the message stream. Messages
arrive again after a new scan, which is how accounts are switched.

**Parameters**

No parameters.

**Returns**

`logged_out` — true — `login_removed`, whether the saved login file
is gone (the next start would adopt that account again if it is
not), and `login_file`, the path it was at.

### `wechat_send_message`

[`slife2/plugins/wechat/server.py:1672`](slife2/plugins/wechat/server.py#L1672)

Send a WeChat message to somebody who has written to you.

An incoming message carries its peer and context token in a
`[Wechat:{...}]` prefix: pass those back here and the reply lands in the
conversation it belongs to. A turn that sends nothing reaches nobody.

**Parameters**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `peer_wechat_id` | `string` | yes | — | The id to send to, from the message's marker or `wechat_check_status`. |
| `text` | `string` | yes | — | The message body. |
| `context_token` | `string` | no | `""` | The conversation's token, from the same place. Empty starts a new thread, which a first message wants. |

**Returns**

`sent` — true, because a send that did not land raises — with the
`peer_wechat_id` and `context_token` it went to.
