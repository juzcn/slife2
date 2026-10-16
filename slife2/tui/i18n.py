"""The window's two languages: English and Chinese, chosen from the OS.

One translation table and a :func:`t` accessor.  Two languages only, no YAML, no
catalogs, no dependency — detection is the OS's own answer in a dozen lines of
stdlib, and the whole user-facing surface is the strings in this one file.

**What is translated is the window, and nothing else.**  The model never reads
any of this: the system prompt, every tool schema, every job's result strings and
every log line are English by rule, and a conversation that changed language with
the terminal's would be a second variable in the one place a difference is
expensive to find.  External text — a tool's result, a peer's message, a
daemon's own sentence — passes through untouched, because it is somebody else's
words rather than this window's chrome.  Tool *labels* stay English too: `read
file` is a name the model calls, and translating it would put the screen and the
tool list in disagreement about what a tool is called.

**A missing key raises rather than rendering blank.**  `t()` is looked up in a
plain dict and `str.format` is always applied, so a typo'd key and a forgotten
placeholder are both a `KeyError` at the call site — which is where the bug is —
instead of an empty slot or a literal `{err}` on somebody's screen.  Strings
without placeholders format to themselves.

**The language is decided once, before anything is built.**  It is resolved at
import from the OS and `--lang` (`slife2.main`) overrides it at start-up, so
every `t()` in this process answers the same way and a string composed early —
the prompt's placeholder, the status bar's first connection state — cannot
disagree with one composed late.  Nothing switches it while a window is up.
Tests override it through :func:`set_language`; the autouse fixture in
`tests/conftest.py` pins it to English, so the suite's assertions do not depend
on the locale of the machine running them.
"""

from __future__ import annotations

import locale
import os
import sys


def _on_windows() -> bool:
    """Whether this is the one platform whose UI language is not in the C locale.

    A function rather than the comparison inlined below, because a type checker
    folds `sys.platform == "win32"` against the machine it is *analysing* on and
    then calls the other branch unreachable — which is true of the analysis and
    false of the code.  Here the answer is a `bool` that nothing can fold.
    """
    return sys.platform == "win32"


def _os_language() -> str:
    """The OS display language tag — ``zh_CN``, ``en_US``, ...

    **Windows keeps the UI language out of the C locale**, so the Win32 API is
    asked directly: an English Windows in Spain reports ``es_ES`` from
    `locale.getlocale()`, which is the *region* rather than the language the
    interface is in.  `GetUserDefaultUILanguage` is the user's own setting,
    falling back to the system's when none is set — which is the language the
    window should follow.  Elsewhere the POSIX variables are the authority, most
    specific first (`LC_ALL` overrides `LANG`, as POSIX specifies).
    """
    if _on_windows():  # pragma: no cover — Windows-only
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return locale.windows_locale.get(langid, "")
    return (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    )


def _detect_language() -> str:
    """``"zh"`` for any Chinese locale, else ``"en"``.

    A tag like ``zh`` / ``zh_CN`` / ``zh_HK`` counts as Chinese on its ``zh``
    prefix: Simplified, Traditional and the region variants share the strings
    here, and a window that refused one of them would be a window with no words
    in it.  Detection never raises — the OS query can fail on an unknown
    language id or an unset environment — and any failure degrades to English,
    because a window that cannot be drawn is worse than one in the wrong
    language.
    """
    try:
        code = _os_language().lower()
    except Exception:  # noqa: BLE001 - degrade to English on any detection failure
        return "en"
    return "zh" if code.startswith("zh") else "en"


#: Resolved once at import, overridable before anything is built (`set_language`).
_LANGUAGE: str = _detect_language()


def set_language(lang: str) -> None:
    """Override the active language — `--lang`, and the tests' own pin."""
    global _LANGUAGE
    _LANGUAGE = lang


def get_language() -> str:
    """Which language this process is answering in."""
    return _LANGUAGE


#: key → {lang: text}.  Every user-facing string of the window lives here.
#:
#: **The glyphs are not in these strings.**  `slife2.tui.theme.GLYPHS`' marks
#: (▸ ▾ ⟐ ↑ ✗ ✅ …) are attached as their own spans at the call site, so a
#: translation cannot drop one, and no entry carries a symbol that is not a word.
#: The one thing that *is* inside the text is a trailing or leading space where
#: the string is a fragment of a line rather than a line — `" requests
#: approval"`, `"Switched to "` — because a translator moving the fragment has
#: to be able to move its spacing with it.
#:
#: **A counted noun is two keys rather than one.**  `str.format` has no plural
#: machinery and neither does this table, so `{n} turn` and `{n} turns` are
#: separate entries that Chinese gives the same value: the *caller* picks, which
#: is the only place that knows the count, and a language that inflects
#: differently from English is a value change rather than a code change.
_STRINGS: dict[str, dict[str, str]] = {
    # ── the harness's own lines (`slife2.tui.notes`) ──────────────────────
    # The count of turns, in the two spellings the notes below are written in.
    "turn_one": {
        "en": "{n} turn",
        "zh": "{n} 轮",
    },
    "turn_many": {
        "en": "{n} turns",
        "zh": "{n} 轮",
    },
    # History came back when the window opened.
    "restored": {
        "en": "restored {turns}",
        "zh": "已恢复 {turns}",
    },
    # What one turn's discriminator decided to run on.  Two counts that are not
    # the same number: what it kept of the conversation in hand, and how many
    # earlier turns it went to the log for.
    "kept": {
        "en": "kept {turns}, recalled {recalled}",
        "zh": "保留 {turns}，召回 {recalled}",
    },
    # A turn the person stopped.
    "cancelled": {
        "en": "cancelled",
        "zh": "已取消",
    },
    # The window could not put its agent on the mesh, and why.  `reason` is the
    # transport's own sentence, quoted rather than restated.
    "not_on_the_mesh": {
        "en": "not on the mesh: {reason}",
        "zh": "不在 A2A 网上：{reason}",
    },
    # A peer, or a channel's own link, coming up and going away.  One sentence
    # for both, because to the person reading a transcript they are the same
    # news; what the two producers supply is the name.
    "online": {
        "en": "{name} online",
        "zh": "{name} 在线",
    },
    "offline": {
        "en": "{name} offline",
        "zh": "{name} 离线",
    },
    # ── a restored conversation (`slife2.tui.restore`) ────────────────────
    # One stored row the screen could not read.  The row itself goes to the log;
    # this is what the person is told, because the note under it counts what did
    # come back.
    "turn_unreadable": {
        "en": "a stored turn could not be drawn",
        "zh": "有一条存档轮次无法绘制",
    },
    # ── the window (`slife2.tui.app`) ─────────────────────────────────────
    # The prompt, addressed to whoever this instance is.
    "input_placeholder": {
        "en": "Message {agent}…",
        "zh": "给 {agent} 发消息…",
    },
    # The status bar's connection word.  Two states on screen and no more; the
    # reasons are `system_health`'s (`sdd.md` §9.2).
    "connection_connecting": {
        "en": "connecting",
        "zh": "连接中",
    },
    "connection_disconnected": {
        "en": "disconnected: {err}",
        "zh": "连接已断开：{err}",
    },
    # What a turn, a switch or an approval raises when there is no transport.
    "not_connected": {
        "en": "not connected to the agent server ({state})",
        "zh": "未连接到 agent server（{state}）",
    },
    # `Ctrl+S` with nothing to offer: said rather than shown as an empty picker,
    # because a list with no rows is a key that looks broken.  The tool that
    # would fix it is the thing worth naming.
    "no_models": {
        "en": "no models are configured; `model_set` adds one",
        "zh": "未配置任何模型；可用 `model_set` 添加",
    },
    "model_switch_failed": {
        "en": "the model was not switched: {err}",
        "zh": "模型未切换：{err}",
    },
    # The approval answer that arrived after the turn it belonged to had ended.
    "approval_undelivered": {
        "en": "the answer was not delivered: {err}",
        "zh": "答复未能送达：{err}",
    },
    # ── a tool call's panel (`slife2.tui.widgets`) ────────────────────────
    # The three states of a call, as the collapsed header row's trailing word.
    "tool_running": {
        "en": "running",
        "zh": "运行中",
    },
    "tool_done": {
        "en": "done",
        "zh": "完成",
    },
    "tool_error": {
        "en": "error",
        "zh": "错误",
    },
    "tool_arguments": {
        "en": "Arguments",
        "zh": "参数",
    },
    "tool_no_arguments": {
        "en": "(no arguments)",
        "zh": "（无参数）",
    },
    "tool_error_heading": {
        "en": "Error",
        "zh": "错误",
    },
    "tool_result_heading": {
        "en": "Result",
        "zh": "结果",
    },
    # What the tool returned, when that is more than the preview shows.  The
    # number is the whole result's, which is the one fact the preview cannot
    # carry.
    "tool_result_chars": {
        "en": "{n:,} characters in all",
        "zh": "共 {n:,} 字符",
    },
    # ── reasoning (`slife2.tui.widgets.AssistantMessage`) ─────────────────
    # The label, and the count the collapsed form carries beside it.  Chinese
    # counts reasoning in characters too, so `字` rather than `字符`: the block
    # is prose, and that is the counter a reader of it expects.
    "thinking": {
        "en": "Thinking",
        "zh": "思考",
    },
    "thinking_chars": {
        "en": "({n:,} chars)",
        "zh": "（{n:,} 字）",
    },
    # ── token counts ──────────────────────────────────────────────────────
    # Under an answer, and in the status bar when the model has no window to be
    # a percentage of.  One key for both, because it is one word — and it is a
    # key at all rather than a literal because it is written twice.
    #
    # **The two values are the same on purpose.**  `token` is the word Chinese
    # technical writing uses; the entry exists so that a translator looking for
    # it finds it, not because this file has a better one.
    "tokens": {
        "en": "{n:,} tokens",
        "zh": "{n:,} tokens",
    },
    # ── the model picker (`slife2.tui.widgets.ModelPicker`) ───────────────
    "picker_title": {
        "en": "Switch model",
        "zh": "切换模型",
    },
    # A conversation on a model this list does not offer.  Said, so the missing
    # cursor is not read as a broken picker and `Enter` doing nothing is
    # explained rather than felt.
    "picker_missing": {
        "en": "{name} is not one of these — Enter picks nothing",
        "zh": "{name} 不在其中 — Enter 不会选择任何模型",
    },
    # …and what to call that model when the record has no name at all.
    "picker_model_in_use": {
        "en": "the model in use",
        "zh": "当前使用的模型",
    },
    # The hint row.  The *keys* are the keys and stay as they are; only the
    # action words after them are words.
    "picker_select": {
        "en": "select",
        "zh": "选择",
    },
    "picker_pick": {
        "en": "pick",
        "zh": "确认",
    },
    "picker_cancel": {
        "en": "cancel",
        "zh": "取消",
    },
    "picker_cancelled": {
        "en": "Cancelled",
        "zh": "已取消",
    },
    # Trailing space: the reference the conversation moved onto follows it.
    "picker_switched": {
        "en": "Switched to ",
        "zh": "已切换到 ",
    },
    # ── the approval row (`slife2.tui.widgets.ApprovalPrompt`) ────────────
    # Leading spaces, because the tool's name is a span of its own before it:
    # the fragment carries its own separator, so a translation that reorders the
    # sentence does not have to reach back into the code for one.
    "approval_requests": {
        "en": "  requests approval",
        "zh": "  请求批准",
    },
    "approval_more": {
        "en": "{n} more",
        "zh": "还有 {n} 项",
    },
    "approval_approve": {
        "en": "approve",
        "zh": "批准",
    },
    "approval_deny": {
        "en": "deny",
        "zh": "拒绝",
    },
    "approval_approved": {
        "en": "approved",
        "zh": "已批准",
    },
    "approval_denied": {
        "en": "denied",
        "zh": "已拒绝",
    },
    # ── what a model *is* (the picker's note, and the status bar) ─────────
    "model_context": {
        "en": "ctx {n:,}",
        "zh": "上下文 {n:,}",
    },
    "model_thinking": {
        "en": "thinking",
        "zh": "推理",
    },
    "model_vision": {
        "en": "vision",
        "zh": "视觉",
    },
    # ── the status bar (`slife2.tui.widgets.StatusBar`) ───────────────────
    "status_working": {
        "en": "working",
        "zh": "处理中",
    },
    # Messages sent that have not had their turn yet.
    "status_queued": {
        "en": "{n} queued",
        "zh": "{n} 个排队中",
    },
    # How many steps into its cap the turn is.  English inflects the noun, so it
    # is the two keys above in miniature; the caller knows the count.
    "status_step": {
        "en": "{n} step",
        "zh": "{n} 步",
    },
    "status_steps": {
        "en": "{n} steps",
        "zh": "{n} 步",
    },
    # The one dim row's key hint.  **The key caps stay English** — `Esc`, `Ctrl+S`
    # — because they are what is printed on the keyboard and a translated one
    # breaks the key-to-action scan; the words after them are the words.
    "status_keybinds": {
        "en": "Esc/Ctrl+C cancel  Ctrl+S model  Ctrl+C quit",
        "zh": "Esc/Ctrl+C 取消  Ctrl+S 模型  Ctrl+C 退出",
    },
}


def t(key: str, **fmt: object) -> str:
    """The localized string for *key*, formatted with *fmt*.

    Looked up strictly and formatted unconditionally, so both a key that does
    not exist and a placeholder nobody supplied raise `KeyError` here — at the
    call site, which is where the typo is — rather than reaching a screen as a
    blank slot or a literal `{err}`.  A key with no placeholders formats to
    itself, so there is one code path rather than two.
    """
    entry = _STRINGS[key]
    text = entry.get(_LANGUAGE, entry["en"])
    return text.format(**fmt)
