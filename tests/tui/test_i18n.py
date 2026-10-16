"""The window's translation layer — the table, the accessor, and the OS query.

Three things are worth pinning, and they fail in three different ways.  The
accessor's contract: `t()` answers in the active language, formats its
placeholders, and **raises** on a key or a placeholder it does not have, because
the alternative is a blank slot or a literal `{err}` on somebody's screen.  The
table's completeness: every entry ships both languages, so no key can be half
translated and render as nothing in one of them.  And the detection underneath:
it is hand-rolled stdlib, so what it does with `zh_TW.UTF-8`, with `C`, and with
an OS query that throws is this code's behaviour rather than a library's promise.

The autouse `english_window` fixture in `tests/conftest.py` pins the language to
English for the suite; the fixture here resets it around each test on top of
that, so a test that switches cannot leak either way.
"""

from __future__ import annotations

import pytest

from slife2.tui import i18n
from slife2.tui.i18n import _STRINGS, set_language, t

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _pin_language():
    """Each test starts in English, and leaves the next one there."""
    set_language("en")
    yield
    set_language("en")


class TestTranslation:
    def test_english(self):
        assert t("cancelled") == "cancelled"

    def test_chinese_when_set(self):
        set_language("zh")
        assert t("cancelled") == "已取消"

    def test_format_placeholders(self):
        assert t("restored", turns="3 turns") == "restored 3 turns"

    def test_format_placeholders_chinese(self):
        set_language("zh")
        out = t("restored", turns="3 轮")
        assert out == "已恢复 3 轮"

    def test_a_switch_takes_effect_both_ways(self):
        set_language("zh")
        assert t("tool_done") == "完成"
        set_language("en")
        assert t("tool_done") == "done"

    def test_placeholders_format_numbers(self):
        """`{n:,}` is `str.format`'s, and the table leans on it."""
        assert t("tokens", n=25000) == "25,000 tokens"

    def test_an_unknown_key_raises(self):
        """A typo'd key is a bug at the call site, not a blank slot."""
        with pytest.raises(KeyError):
            t("this_key_does_not_exist")

    def test_a_missing_placeholder_raises(self):
        """Strict formatting, for the same reason: a call-site bug."""
        with pytest.raises(KeyError):
            t("restored")  # no turns=

    def test_every_key_has_both_languages(self):
        """A half-translated entry is a key that renders as nothing in one language."""
        for key, entry in _STRINGS.items():
            assert "en" in entry, f"{key} has no English"
            assert "zh" in entry, f"{key} has no Chinese"


class TestOsLocaleDetection:
    """The OS query behind the language choice — not the strings."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        """Always take the *nix branch.

        The Windows branch is a Win32 call only a Windows box can answer, and
        this suite runs on both.  The two differ in where the tag comes from and
        not in what is done with it, so the mapping below is what both feed into.
        """
        monkeypatch.setattr(i18n, "_on_windows", lambda: False)

    @staticmethod
    def _env(monkeypatch, **values):
        """Set exactly *values* — this machine's real locale must not leak in."""
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(var, raising=False)
        for var, value in values.items():
            monkeypatch.setenv(var, value)

    def test_every_chinese_variant_is_chinese(self, monkeypatch):
        for value in ("zh", "zh_CN.UTF-8", "zh_TW.UTF-8", "zh-Hans"):
            self._env(monkeypatch, LANG=value)
            assert i18n._detect_language() == "zh", value

    def test_anything_else_is_english(self, monkeypatch):
        for value in ("en_US.UTF-8", "C", "POSIX", "ja_JP.UTF-8"):
            self._env(monkeypatch, LANG=value)
            assert i18n._detect_language() == "en", value

    def test_the_more_specific_variable_wins(self, monkeypatch):
        """`LC_ALL` overrides `LANG`, as POSIX specifies."""
        self._env(monkeypatch, LC_ALL="zh_CN.UTF-8", LANG="en_US.UTF-8")
        assert i18n._detect_language() == "zh"

    def test_an_unset_environment_is_english(self, monkeypatch):
        self._env(monkeypatch)
        assert i18n._detect_language() == "en"

    def test_a_failing_query_degrades_to_english(self, monkeypatch):
        """Detection never raises — a broken OS query still yields a window."""

        def _boom():
            raise OSError("no such API")

        monkeypatch.setattr(i18n, "_os_language", _boom)
        assert i18n._detect_language() == "en"
