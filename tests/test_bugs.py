"""Regression tests for sender.py / gui.py bug fixes.

Run:  python -m unittest discover -s tests
All GUI-automation side effects (pyautogui, user32) are stubbed; these tests
never move the mouse, type, or open windows.
"""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import antigravity_continue
import claude_continue
import codex_continue
import debug_windows
import gui
import sender
import zcode_continue
from sender import TargetSpec


def _spec(blocklist=(), exe_names=("target.exe",)):
    return TargetSpec(
        name="Test Target",
        window_title_contains="Target",
        exe_names=exe_names,
        focus_method="click_bottom_center",
        blocklist=blocklist,
    )


def _fake_element(rect, cls="ProseMirror", ctype=50004, has_focus=True):
    """A stand-in for an IUIAutomationElement."""
    el = mock.Mock()
    r = mock.Mock()
    r.left, r.top, r.right, r.bottom = rect
    el.CurrentBoundingRectangle = r
    el.CurrentClassName = cls
    el.CurrentControlType = ctype
    el.CurrentHasKeyboardFocus = 1 if has_focus else 0
    return el


def _fake_handle(value="continue", pattern_available=True,
                 cls="ProseMirror"):
    """A ComposerHandle whose element's Value pattern reports `value`."""
    element = _fake_element((41, 1649, 986, 1713), cls=cls)
    pattern = mock.Mock()
    pattern.__bool__ = lambda _self: pattern_available
    pattern.QueryInterface.return_value.CurrentValue = value
    element.GetCurrentPattern.return_value = pattern
    mod = types.SimpleNamespace(UIA_ValuePatternId=10002,
                                IUIAutomationValuePattern=object())
    return sender.ComposerHandle(mock.Mock(), mod, element)


def _fake_uia(find_results):
    """(uia, mod) whose root.FindAll returns the element lists in
    `find_results`, one list per call."""
    uia = mock.Mock()
    mod = types.SimpleNamespace(UIA_ControlTypePropertyId=30003,
                                UIA_ClassNamePropertyId=30012,
                                TreeScope_Descendants=4)
    calls = iter(find_results)

    def find_all(_scope, _cond):
        elems = next(calls)
        arr = mock.Mock()
        arr.Length = len(elems)
        arr.GetElement.side_effect = lambda i: elems[i]
        return arr

    root = mock.Mock()
    root.FindAll.side_effect = find_all
    uia.ElementFromHandle.return_value = root
    return uia, mod


def _fake_filtering_uia(find_results):
    """Like _fake_uia, but FindAll really APPLIES the condition it is handed
    (property conditions on ControlType/ClassName plus AND). So a test can
    pass one mixed element list and assert what the query keeps — the
    difference between "the finder picks the right element" and "the finder
    asks for the right elements"."""
    uia = mock.Mock()
    mod = types.SimpleNamespace(UIA_ControlTypePropertyId=30003,
                                UIA_ClassNamePropertyId=30012,
                                TreeScope_Descendants=4)

    def matches(cond, el):
        if cond[0] == "and":
            return matches(cond[1], el) and matches(cond[2], el)
        _kind, pid, value = cond
        if pid == 30003:
            return int(el.CurrentControlType) == value
        return (el.CurrentClassName or "") == value

    uia.CreatePropertyCondition.side_effect = (
        lambda pid, value: ("prop", pid, value))
    uia.CreateAndCondition.side_effect = lambda a, b: ("and", a, b)

    calls = iter(find_results)

    def find_all(_scope, cond):
        elems = [e for e in next(calls) if matches(cond, e)]
        arr = mock.Mock()
        arr.Length = len(elems)
        arr.GetElement.side_effect = lambda i: elems[i]
        return arr

    root = mock.Mock()
    root.FindAll.side_effect = find_all
    uia.ElementFromHandle.return_value = root
    return uia, mod


# The real ZCode composer class: a run of Tailwind utility classes, which is
# exactly why the zcode target pins no class.
ZCODE_COMPOSER_CLASS = ("min-h-10 max-h-40 overflow-y-auto text-ui-base "
                        "leading-5 text-foreground outline-none")


class SelectMatchesTests(unittest.TestCase):
    """Window selection: exe matches are authoritative, blocklisted
    title-matches must never be returned."""

    def test_exe_match_preferred_over_title_match(self):
        spec = _spec(blocklist=(" – ",))
        matches = [
            (1, "Something else", "target.exe"),
            (2, "Target in title only", "chrome.exe"),
        ]
        self.assertEqual(sender._select_matches(matches, spec), [(1, "Something else")])

    def test_blocklisted_title_matches_are_filtered(self):
        spec = _spec(blocklist=(" – ", ".py"))
        matches = [
            (1, "proj – sender.py – PyCharm", "pycharm64.exe"),
            (2, "Target App", "chrome.exe"),
        ]
        self.assertEqual(sender._select_matches(matches, spec), [(2, "Target App")])

    def test_all_blocklisted_returns_empty_not_blocklisted_windows(self):
        # Bug: when every title match was blocklisted, the old code fell
        # through and returned the blocklisted windows anyway (typing
        # "continue" into e.g. a PyCharm editor).
        spec = _spec(blocklist=(" – ", ".py"))
        matches = [
            (1, "claude_desktop_continue – sender.py", "pycharm64.exe"),
            (2, "Target – notes.py", "code.exe"),
        ]
        self.assertEqual(sender._select_matches(matches, spec), [])

    def test_empty_blocklist_keeps_title_matches(self):
        spec = _spec(blocklist=())
        matches = [(1, "Target App", "chrome.exe")]
        self.assertEqual(sender._select_matches(matches, spec), [(1, "Target App")])

    def test_blocklist_is_case_insensitive(self):
        # Title matching is case-insensitive, so the blocklist must be too:
        # "NOTES.PY" must not slip past a ".py" blocklist entry.
        spec = _spec(blocklist=(".py",))
        matches = [(1, "Target NOTES.PY - editor", "other.exe")]
        self.assertEqual(sender._select_matches(matches, spec), [])


class PickMainWindowTests(unittest.TestCase):
    """Tiny utility/overlay windows must never be picked as the send target:
    for click_bottom_center, a window shorter than the 80 px offset would get
    the focus click placed outside itself, into whatever app is behind it."""

    def test_prefers_normal_sized_over_tiny_topmost(self):
        stub = mock.Mock()
        stub.IsIconic.return_value = 0
        rects = {1: (0, 0, 100, 60), 2: (0, 0, 1200, 800)}
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "get_window_rect",
                               side_effect=lambda h: rects[h]):
            picked = sender._pick_main_window([(1, "tiny"), (2, "main")])
        self.assertEqual(picked, (2, "main"))

    def test_minimized_window_is_acceptable(self):
        # A minimized window has a meaningless rect (-32000, tiny size) but is
        # a valid target: force_activate_window restores it before use.
        stub = mock.Mock()
        stub.IsIconic.return_value = 1
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(-32000, -32000, 160, 28)):
            picked = sender._pick_main_window([(1, "minimized main")])
        self.assertEqual(picked, (1, "minimized main"))

    def test_all_tiny_returns_none(self):
        stub = mock.Mock()
        stub.IsIconic.return_value = 0
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(0, 0, 100, 60)):
            self.assertIsNone(sender._pick_main_window([(1, "tiny")]))

    def test_prefers_visible_qualifying_window_over_minimized(self):
        # A minimized window's size is unknown until restored — it could be a
        # tiny utility window. When a visible full-size window exists, use it.
        stub = mock.Mock()
        stub.IsIconic.side_effect = lambda h: 1 if h == 1 else 0
        rects = {2: (0, 0, 1200, 800)}
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "get_window_rect",
                               side_effect=lambda h: rects[h]):
            picked = sender._pick_main_window([(1, "minimized"), (2, "main")])
        self.assertEqual(picked, (2, "main"))

    def test_prefer_largest_picks_the_biggest_qualifying_window(self):
        # Codex: quick-chat / hotkey pop-ups and the avatar overlay are
        # windows of the same process with the same title "ChatGPT"; the
        # primary window is the largest one, whatever the Z-order.
        stub = mock.Mock()
        stub.IsIconic.return_value = 0
        rects = {1: (0, 0, 560, 400), 2: (0, 0, 1200, 800), 3: (0, 0, 300, 300)}
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "get_window_rect",
                               side_effect=lambda h: rects[h]):
            windows = [(1, "ChatGPT"), (2, "ChatGPT"), (3, "ChatGPT")]
            self.assertEqual(sender._pick_main_window(windows), (1, "ChatGPT"))
            self.assertEqual(
                sender._pick_main_window(windows, prefer_largest=True),
                (2, "ChatGPT"))

    def test_codex_spec_prefers_largest_window(self):
        self.assertTrue(sender.TARGETS["codex"].prefer_largest_window)
        self.assertFalse(sender.TARGETS["antigravity"].prefer_largest_window)


def _stub_user32(foreground_hwnd):
    u = mock.Mock()
    u.IsIconic.return_value = 0
    u.SetForegroundWindow.return_value = 1
    u.GetForegroundWindow.return_value = foreground_hwnd
    # Root owner of a top-level window is the window itself.
    u.GetAncestor.side_effect = lambda hwnd, flag: hwnd
    return u


class ForceActivateTests(unittest.TestCase):
    """force_activate_window must report real success, verified against
    GetForegroundWindow — not unconditionally True."""

    def test_returns_false_when_foreground_is_another_window(self):
        stub = _stub_user32(foreground_hwnd=999)
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "pyautogui", mock.Mock()), \
             mock.patch.object(sender, "time", mock.Mock()):
            self.assertFalse(sender.force_activate_window(42))

    def test_returns_true_when_target_is_foreground(self):
        stub = _stub_user32(foreground_hwnd=42)
        with mock.patch.object(sender, "user32", stub), \
             mock.patch.object(sender, "pyautogui", mock.Mock()), \
             mock.patch.object(sender, "time", mock.Mock()):
            self.assertTrue(sender.force_activate_window(42))


class SendOnceTests(unittest.TestCase):
    def test_does_not_type_when_activation_fails(self):
        # Bug: send_once ignored force_activate_window's result and typed
        # into whatever window actually had focus (lock screen, editor, ...).
        fake_pyautogui = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "Target App")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "Target App")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=False), \
             mock.patch.object(sender, "_focus_input") as focus, \
             mock.patch.object(sender, "pyautogui", fake_pyautogui), \
             mock.patch.object(sender, "time", mock.Mock()):
            with self.assertRaises(RuntimeError):
                sender.send_once("claude", "continue", log=lambda _t: None)
            focus.assert_not_called()
            fake_pyautogui.typewrite.assert_not_called()

    def test_raises_when_only_degenerate_windows_exist(self):
        # Bug: send_once took windows[0] blindly; a 100x60 overlay window got
        # the bottom-center click computed 80 px above its own bottom edge —
        # i.e. outside the window, into whatever lies behind it.
        fake_pyautogui = mock.Mock()
        stub_user32 = mock.Mock()
        stub_user32.IsIconic.return_value = 0
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(1, "tiny overlay")]), \
             mock.patch.object(sender, "user32", stub_user32), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(0, 0, 100, 60)), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input") as focus, \
             mock.patch.object(sender, "pyautogui", fake_pyautogui), \
             mock.patch.object(sender, "time", mock.Mock()):
            with self.assertRaises(RuntimeError):
                sender.send_once("claude", "continue", log=lambda _t: None)
            focus.assert_not_called()
            fake_pyautogui.typewrite.assert_not_called()


class SendLoopTests(unittest.TestCase):
    def test_unexpected_exception_still_emits_done_event(self):
        # Bug: an exception outside the send_once try/except (e.g. from
        # _wake_screen) escaped send_loop without a terminal "done" event,
        # leaving the GUI disabled forever.
        events = []
        with mock.patch.object(sender, "_wake_screen",
                               side_effect=ValueError("boom")), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                sender.send_loop(
                    target="claude", message="x", initial_delay_s=0,
                    every_s=None, count=0, on_event=events.append,
                )
            except ValueError:
                pass  # escaping is part of the bug; assertion below decides
        self.assertTrue(
            events and events[-1].get("type") == "done"
            and events[-1].get("reason") == "error",
            f"expected terminal done/error event, got: {events}",
        )

    def test_single_send_emits_sent_then_done(self):
        events = []
        with mock.patch.object(sender, "_wake_screen"), \
             mock.patch.object(sender, "send_once"):
            sender.send_loop(
                target="claude", message="x", initial_delay_s=0,
                every_s=None, count=0, on_event=events.append,
            )
        types = [e["type"] for e in events]
        self.assertIn("sent", types)
        self.assertEqual(events[-1]["type"], "done")

    # --- retry policy for unattended repeat runs ---------------------------

    def _run(self, send_effects, every_s, count, events, waits):
        """Runs send_loop with send_once following `send_effects` (None =
        success, an exception = that failure) and the interval wait replaced
        by an immediate return that records (seconds, label) in `waits`.
        Returns the send_once mock."""

        def fake_wait(seconds, label, stop_event, emit):
            waits.append((seconds, label))
            return True

        with mock.patch.object(sender, "_wake_screen"), \
             mock.patch.object(sender, "_countdown_wait",
                               side_effect=fake_wait), \
             mock.patch.object(sender, "send_once",
                               side_effect=send_effects) as once, \
             contextlib.redirect_stdout(io.StringIO()):
            sender.send_loop(
                target="claude", message="x", initial_delay_s=0,
                every_s=every_s, count=count, on_event=events.append,
            )
        return once

    @staticmethod
    def _failed(events):
        return [e for e in events if e["type"] == "send_failed"]

    def test_transient_failure_then_success_continues_a_repeat_run(self):
        # A draft in the composer / focus lost / read-back mismatch is often
        # gone by the next interval: log it, report it, wait, try again.
        events, waits = [], []
        once = self._run(
            [RuntimeError("composer holds a draft"), None, None],
            every_s=60, count=2, events=events, waits=waits,
        )
        self.assertEqual(once.call_count, 3)
        failed = self._failed(events)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["consecutive"], 1)
        self.assertEqual(failed[0]["limit"], sender.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("draft", failed[0]["error"])
        # Failed sends do not count towards `count`.
        self.assertEqual([e["n"] for e in events if e["type"] == "sent"],
                         [1, 2])
        self.assertEqual(events[-1], {"type": "done",
                                      "reason": "count_reached"})
        # The failure waited a full interval before the retry, like a
        # success would have.
        self.assertEqual([w[0] for w in waits], [60, 60])
        self.assertTrue(any(e["type"] == "log" and "[ERROR]" in e["text"]
                            for e in events))

    def test_consecutive_failures_end_a_repeat_run(self):
        events, waits = [], []
        limit = sender.MAX_CONSECUTIVE_FAILURES
        once = self._run(
            [RuntimeError("no window")] * (limit + 2),
            every_s=60, count=0, events=events, waits=waits,
        )
        self.assertEqual(once.call_count, limit)
        self.assertEqual([e["consecutive"] for e in self._failed(events)],
                         list(range(1, limit + 1)))
        self.assertEqual(events[-1], {"type": "done", "reason": "error"})
        # No wait after the last, fatal failure.
        self.assertEqual(len(waits), limit - 1)

    def test_a_success_resets_the_failure_streak(self):
        events, waits = [], []
        limit = sender.MAX_CONSECUTIVE_FAILURES
        streak = [RuntimeError("focus lost")] * (limit - 1)
        once = self._run(
            streak + [None] + streak + [None],
            every_s=60, count=2, events=events, waits=waits,
        )
        self.assertEqual(once.call_count, 2 * limit)
        self.assertEqual(events[-1], {"type": "done",
                                      "reason": "count_reached"})
        self.assertEqual(max(e["consecutive"] for e in self._failed(events)),
                         limit - 1)

    def test_configuration_error_ends_a_repeat_run_at_once(self):
        # Unknown target, untypeable message, message the target refuses:
        # retrying cannot help, so the run ends like before.
        events, waits = [], []
        once = self._run(
            [sender.ConfigurationError("Message starts with '/'")],
            every_s=60, count=0, events=events, waits=waits,
        )
        self.assertEqual(once.call_count, 1)
        self.assertEqual(self._failed(events), [])
        self.assertEqual(waits, [])
        self.assertEqual(events[-1], {"type": "done", "reason": "error"})

    def test_count_one_with_an_interval_retries_too(self):
        # The interval is the retry cadence; count only limits successes.
        events, waits = [], []
        once = self._run(
            [RuntimeError("no window"), None],
            every_s=60, count=1, events=events, waits=waits,
        )
        self.assertEqual(once.call_count, 2)
        self.assertEqual(len(self._failed(events)), 1)
        self.assertEqual([w[0] for w in waits], [60])
        self.assertEqual([e["n"] for e in events if e["type"] == "sent"], [1])
        self.assertEqual(events[-1], {"type": "done",
                                      "reason": "count_reached"})

    def test_single_shot_failure_ends_at_once(self):
        # No interval means nothing to wait for: fail immediately, whatever
        # the count.
        for every_s, count in ((None, 0), (None, 1), (None, 3)):
            events, waits = [], []
            once = self._run(
                [RuntimeError("no window"), None],
                every_s=every_s, count=count, events=events, waits=waits,
            )
            self.assertEqual(once.call_count, 1, (every_s, count))
            self.assertEqual(self._failed(events), [], (every_s, count))
            self.assertEqual(waits, [], (every_s, count))
            self.assertEqual(events[-1], {"type": "done", "reason": "error"},
                             (every_s, count))

    def test_stop_during_the_retry_wait_ends_with_stopped(self):
        events = []
        with mock.patch.object(sender, "_wake_screen"), \
             mock.patch.object(sender, "_countdown_wait",
                               return_value=False), \
             mock.patch.object(sender, "send_once",
                               side_effect=[RuntimeError("x"), None]), \
             contextlib.redirect_stdout(io.StringIO()):
            sender.send_loop(
                target="claude", message="x", initial_delay_s=0,
                every_s=60, count=0, on_event=events.append,
            )
        self.assertEqual(events[-1], {"type": "done", "reason": "stopped"})


class ConfigurationErrorTests(unittest.TestCase):
    """send_once must raise ConfigurationError (a ValueError) for every
    condition that no retry can fix, so send_loop can tell them apart from
    transient refusals."""

    def test_is_a_value_error(self):
        self.assertTrue(issubclass(sender.ConfigurationError, ValueError))

    def test_unknown_target(self):
        with self.assertRaises(sender.ConfigurationError):
            sender.send_once("nope", "continue", log=lambda _t: None)

    def _send(self, target, message):
        fake = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "Target")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "Target")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True) as activate, \
             mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            with self.assertRaises(sender.ConfigurationError):
                sender.send_once(target, message, log=lambda _t: None)
        activate.assert_not_called()
        fake.typewrite.assert_not_called()

    def test_untypeable_message(self):
        self._send("claude", "weiter pr\u00fcfen")

    def test_message_the_target_refuses(self):
        self._send("codex", "/compact")

    def test_transient_refusals_stay_runtime_errors(self):
        # Sanity: the "not running" refusal is NOT a configuration error.
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                sender.send_once("claude", "continue", log=lambda _t: None)
        self.assertNotIsInstance(ctx.exception, sender.ConfigurationError)


class LoadSettingsTests(unittest.TestCase):
    def test_valid_json_that_is_not_an_object_falls_back_to_defaults(self):
        # Bug: "[1, 2, 3]" is valid JSON, so json.loads succeeded and
        # data.items() crashed the GUI at startup. Docstring promises
        # corrupt file -> defaults.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with mock.patch.object(gui, "SETTINGS_PATH", path):
                settings, warning = gui.load_settings()
        self.assertEqual(settings, gui.DEFAULTS)
        self.assertTrue(warning)

    def test_missing_file_returns_defaults_without_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with mock.patch.object(gui, "SETTINGS_PATH", path):
                settings, warning = gui.load_settings()
        self.assertEqual(settings, gui.DEFAULTS)
        self.assertIsNone(warning)

    def test_non_utf8_file_falls_back_to_defaults(self):
        # Bug: Notepad saving the file as ANSI/cp1252 (e.g. an umlaut in the
        # message) made read_text raise UnicodeDecodeError — a ValueError,
        # caught by neither OSError nor JSONDecodeError -> startup crash.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_bytes(b'{"message": "gr\xfc\xdfe"}')  # cp1252
            with mock.patch.object(gui, "SETTINGS_PATH", path):
                settings, warning = gui.load_settings()
        self.assertEqual(settings, gui.DEFAULTS)
        self.assertTrue(warning)

    def test_wrong_typed_values_fall_back_per_key(self):
        # Bug: values went unchecked into Tk variable constructors;
        # e.g. "repeat_enabled": "" raised TclError before the window opened.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                '{"repeat_enabled": "", "initial_hours": "abc", "count": 3}',
                encoding="utf-8",
            )
            with mock.patch.object(gui, "SETTINGS_PATH", path):
                settings, warning = gui.load_settings()
        self.assertEqual(settings["repeat_enabled"],
                         gui.DEFAULTS["repeat_enabled"])
        self.assertEqual(settings["initial_hours"],
                         gui.DEFAULTS["initial_hours"])
        self.assertEqual(settings["count"], 3)  # valid value survives
        self.assertTrue(warning)

    def test_nonfinite_numbers_fall_back_per_key(self):
        # json.loads accepts NaN/Infinity literals; NaN survives every
        # "<= 0" validation and later crashes the worker in timedelta().
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"initial_hours": NaN, "every_hours": Infinity}',
                            encoding="utf-8")
            with mock.patch.object(gui, "SETTINGS_PATH", path):
                settings, warning = gui.load_settings()
        self.assertEqual(settings["initial_hours"],
                         gui.DEFAULTS["initial_hours"])
        self.assertEqual(settings["every_hours"], gui.DEFAULTS["every_hours"])
        self.assertTrue(warning)


class ValidateSettingsTests(unittest.TestCase):
    """Pure validation used by _on_start before saving/launching a run."""

    def _settings(self, **overrides):
        s = dict(gui.DEFAULTS)
        s.update(overrides)
        return s

    def test_valid_settings_pass(self):
        self.assertIsNone(gui.validate_settings(
            self._settings(repeat_enabled=True, every_hours=2.0)))

    def test_negative_count_rejected(self):
        # Bug: count -3 passed validation; sender's "count > 0" test then
        # never triggered, turning a bounded run into an infinite one.
        self.assertTrue(gui.validate_settings(self._settings(count=-3)))

    def test_negative_delay_rejected(self):
        self.assertTrue(gui.validate_settings(
            self._settings(initial_minutes=-120.0)))

    def test_nan_interval_rejected(self):
        # Bug: nan <= 0 is False, so NaN passed the interval check and the
        # worker later died in timedelta(seconds=nan).
        self.assertTrue(gui.validate_settings(
            self._settings(repeat_enabled=True, every_hours=float("nan"))))

    def test_infinite_delay_rejected(self):
        self.assertTrue(gui.validate_settings(
            self._settings(initial_hours=float("inf"))))

    def test_repeat_enabled_with_zero_interval_rejected(self):
        self.assertTrue(gui.validate_settings(
            self._settings(repeat_enabled=True)))

    def test_codex_target_accepted(self):
        self.assertIsNone(gui.validate_settings(self._settings(target="codex")))


def _antigravity_spec():
    return sender.TARGETS["antigravity"]


class AgentInputFocusTests(unittest.TestCase):
    """Antigravity focus logic.

    Bug: the Agent Manager window (standalone 'Antigravity 2.0' surface) does
    NOT react to Ctrl+L at all — both of its Ctrl+L bindings are disabled on
    that surface (disableInWeb). The first send only ever worked because the
    chat input happened to still hold the window's internal focus; as soon as
    the user clicked elsewhere (artifact viewer, conversation), every later
    send typed into a focus-dead element and vanished. Fix: Ctrl+I (the only
    live 'focusInput' binding there), verified via UIA, with a click ladder
    into the input box as fallback. The IDE editor window keeps Ctrl+L but is
    normalized with Ctrl+1 first, because antigravity.toggleChatFocus CLOSES
    the panel when it is already open and focused."""

    MANAGER_TITLE = "Prioritize Backlog Item"
    IDE_TITLE = "main.py - FEM_solver - Antigravity"

    def _run(self, title, focused_types, rect=(0, 0, 1706, 2100), scale=1.25):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "get_window_rect", return_value=rect), \
             mock.patch.object(sender, "_window_dpi_scale", return_value=scale), \
             mock.patch.object(sender, "_focused_control_type",
                               side_effect=focused_types):
            sender._focus_input(_antigravity_spec(), 42, title,
                                log=lambda _t: None)
        return fake

    def test_manager_ctrl_i_alone_when_verified(self):
        fake = self._run(self.MANAGER_TITLE, focused_types=[50003])
        fake.hotkey.assert_called_once_with("ctrl", "i")
        fake.click.assert_not_called()

    def test_manager_falls_back_to_click_ladder(self):
        # Ctrl+I leaves focus on a non-input (50026 = Group), first click too,
        # second click lands. y = bottom - int(92 * 1.25) = 2100 - 115.
        fake = self._run(self.MANAGER_TITLE,
                         focused_types=[50026, 50026, 50003])
        self.assertEqual(fake.click.call_count, 2)
        (x1, y1), _ = fake.click.call_args_list[0]
        (x2, y2), _ = fake.click.call_args_list[1]
        self.assertEqual((x1, y1), (int(1706 * 0.42), 2100 - 115))
        self.assertEqual((x2, y2), (int(1706 * 0.55), 2100 - 115))

    def test_manager_raises_when_no_candidate_focuses_input(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(0, 0, 1706, 2100)), \
             mock.patch.object(sender, "_window_dpi_scale", return_value=1.25), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50026):
            with self.assertRaises(RuntimeError):
                sender._focus_input(_antigravity_spec(), 42,
                                    self.MANAGER_TITLE, log=lambda _t: None)
        self.assertEqual(fake.click.call_count,
                         len(sender.AGENT_INPUT_X_FRACTIONS))
        fake.typewrite.assert_not_called()

    def test_manager_without_uia_trusts_ctrl_i(self):
        # UIA unavailable (comtypes missing) -> cannot verify; the documented
        # Ctrl+I focusInput binding is trusted, and no blind clicks are fired
        # (a blind click could land in the artifact panel and UNDO the focus).
        fake = self._run(self.MANAGER_TITLE, focused_types=[None])
        fake.hotkey.assert_called_once_with("ctrl", "i")
        fake.click.assert_not_called()

    def test_ide_window_normalizes_with_ctrl1_before_ctrl_l(self):
        fake = self._run(self.IDE_TITLE, focused_types=[50004])
        self.assertEqual(fake.hotkey.call_args_list,
                         [mock.call("ctrl", "1"), mock.call("ctrl", "l")])
        fake.click.assert_not_called()

    def test_ide_window_raises_when_input_not_focused(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50026):
            with self.assertRaises(RuntimeError):
                sender._focus_input(_antigravity_spec(), 42, self.IDE_TITLE,
                                    log=lambda _t: None)
        fake.typewrite.assert_not_called()


class UnsupportedCharsTests(unittest.TestCase):
    """pyautogui.typewrite silently drops characters it cannot map (umlauts,
    Vietnamese diacritics, emoji) and then Enter was pressed anyway — a
    message-dependent silent failure. Such messages must be rejected up
    front."""

    def test_plain_ascii_passes(self):
        self.assertEqual(sender.unsupported_chars("yes, continue as planned"),
                         "")

    def test_non_ascii_flagged(self):
        bad = sender.unsupported_chars("tiếp tục — weiter prüfen")
        for ch in ("ế", "ụ", "—", "ü"):
            self.assertIn(ch, bad)

    def test_newline_and_tab_flagged(self):
        # \n would submit mid-message, \t can move focus out of the input.
        self.assertIn("\n", sender.unsupported_chars("a\nb"))
        self.assertIn("\t", sender.unsupported_chars("a\tb"))

    def test_validate_settings_rejects_untypeable_message(self):
        s = dict(gui.DEFAULTS)
        s["message"] = "weiter prüfen"
        self.assertTrue(gui.validate_settings(s))


class SendOnceFocusSafetyTests(unittest.TestCase):
    def _base_patches(self, fake_pyautogui, focused_types):
        return [
            mock.patch.object(sender, "find_target_windows",
                              return_value=[(42, "Prioritize Backlog Item")]),
            mock.patch.object(sender, "_pick_main_window",
                              return_value=(42, "Prioritize Backlog Item")),
            mock.patch.object(sender, "force_activate_window",
                              return_value=True),
            mock.patch.object(sender, "_focus_input"),
            mock.patch.object(sender, "_focused_control_type",
                              side_effect=focused_types),
            mock.patch.object(sender, "pyautogui", fake_pyautogui),
            mock.patch.object(sender, "time", mock.Mock()),
        ]

    def test_modifier_cleanup_happens_before_activation(self):
        # A stray Alt key-up delivered to the target window after activation
        # can focus its menu bar; the stuck-modifier cleanup must fire while
        # the PREVIOUS window still has focus.
        manager = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "Prioritize Backlog Item")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "Prioritize Backlog Item")), \
             mock.patch.object(sender, "force_activate_window",
                               manager.activate), \
             mock.patch.object(sender, "_focus_input", manager.focus), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50003), \
             mock.patch.object(sender, "pyautogui", manager.pyautogui), \
             mock.patch.object(sender, "time", mock.Mock()):
            manager.activate.return_value = True
            sender.send_once("antigravity", "continue", log=lambda _t: None)
        calls = [c for c in manager.mock_calls
                 if c[0] in ("pyautogui.keyUp", "activate")]
        keyup_idx = [i for i, c in enumerate(calls)
                     if c[0] == "pyautogui.keyUp"]
        activate_idx = [i for i, c in enumerate(calls) if c[0] == "activate"]
        self.assertTrue(keyup_idx and activate_idx)
        self.assertLess(max(keyup_idx), min(activate_idx))

    def test_untypeable_message_raises_before_typing(self):
        fake = mock.Mock()
        patches = self._base_patches(fake, focused_types=[50003])
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6]:
            with self.assertRaises(sender.ConfigurationError):
                sender.send_once("antigravity", "weiter prüfen",
                                 log=lambda _t: None)
            fake.typewrite.assert_not_called()
            fake.press.assert_not_called()

    def test_no_enter_when_focus_lost_after_typing(self):
        # Focus verified fine by _focus_input, but stolen while typing:
        # Enter must NOT be pressed into whatever grabbed it.
        fake = mock.Mock()
        patches = self._base_patches(fake, focused_types=[50026])
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6]:
            with self.assertRaises(RuntimeError):
                sender.send_once("antigravity", "continue",
                                 log=lambda _t: None)
            fake.typewrite.assert_called_once()
            fake.press.assert_not_called()

    def test_enter_still_pressed_when_uia_cannot_verify(self):
        # Contract of _focused_control_type: None means "cannot verify", not
        # "verification failed" — the documented shortcut is trusted.
        fake = mock.Mock()
        patches = self._base_patches(fake, focused_types=[None])
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6]:
            sender.send_once("antigravity", "continue", log=lambda _t: None)
        fake.press.assert_called_once_with("enter")


class EventPumpTests(unittest.TestCase):
    def test_pump_reschedules_even_if_a_handler_raises(self):
        # Bug (latent): _drain_events only rescheduled itself after a clean
        # drain; one raising handler stopped all event processing forever
        # while the window stayed open.
        g = object.__new__(gui.ContinueSenderGUI)
        g.events = __import__("queue").Queue()
        g.root = mock.Mock()
        g.events.put(None)  # _handle_event(None) raises AttributeError
        try:
            g._drain_events()
        except Exception:
            pass  # the raise may propagate; rescheduling must happen anyway
        g.root.after.assert_called_once()


class SendFailedEventTests(unittest.TestCase):
    def _gui(self):
        g = object.__new__(gui.ContinueSenderGUI)
        g.status_label = mock.Mock()
        g.sent_label = mock.Mock()
        g.failed_label = mock.Mock()
        g.log_text = mock.Mock()
        g.failed_total = 0
        return g

    @staticmethod
    def _label_text(label):
        return label.configure.call_args.kwargs["text"]

    def test_send_failed_updates_status_and_failed_count(self):
        g = self._gui()
        g._handle_event({"type": "send_failed", "error": "draft in box",
                         "consecutive": 1, "limit": 3})
        g._handle_event({"type": "send_failed", "error": "draft in box",
                         "consecutive": 2, "limit": 3})
        self.assertEqual(g.failed_total, 2)
        self.assertEqual(self._label_text(g.failed_label), "Failed: 2")
        status = self._label_text(g.status_label)
        self.assertIn("2/3", status)
        self.assertIn("retry", status.lower())
        # The run is still going: inputs stay disabled (no re-enable call).
        self.assertFalse(hasattr(g, "start_btn"))

    def test_send_failed_leaves_sent_count_alone(self):
        g = self._gui()
        g._handle_event({"type": "send_failed", "error": "x",
                         "consecutive": 1, "limit": 3})
        g.sent_label.configure.assert_not_called()


class ExeOnlyMatchingTests(unittest.TestCase):
    """An empty window_title_contains disables title matching. The Codex app
    window is titled 'ChatGPT', which would also match browser tabs, Explorer
    folders and editor buffers when the app is NOT running; exe-only matching
    reports 'not running' instead of typing into one of those."""

    def _exe_only(self, exe_names=("chatgpt.exe",)):
        return TargetSpec(name="Codex", window_title_contains="",
                          exe_names=exe_names, focus_method="uia_composer")

    def test_empty_title_needle_never_matches_by_title(self):
        spec = self._exe_only()
        self.assertFalse(sender._matches_spec(
            spec, "ChatGPT - Google Chrome", "chrome.exe"))
        self.assertFalse(sender._matches_spec(spec, "anything", "explorer.exe"))

    def test_empty_title_needle_still_matches_by_exe(self):
        spec = self._exe_only()
        self.assertTrue(sender._matches_spec(spec, "ChatGPT", "chatgpt.exe"))
        self.assertTrue(sender._matches_spec(spec, "", "chatgpt.exe"))

    def test_non_empty_needle_keeps_title_matching(self):
        spec = _spec()  # window_title_contains="Target"
        self.assertTrue(sender._matches_spec(spec, "my target app", "other.exe"))
        self.assertFalse(sender._matches_spec(spec, "nothing here", "other.exe"))
        self.assertTrue(sender._matches_spec(spec, "nothing here", "target.exe"))

    def test_exe_match_requires_package_path_when_configured(self):
        # A legacy stand-alone ChatGPT desktop install is also ChatGPT.exe
        # (and the same Chromium/ProseMirror UI); only the OpenAI.Codex
        # package may be targeted.
        spec = sender.TARGETS["codex"]
        codex_path = (r"c:\program files\windowsapps"
                      r"\openai.codex_26.901.4073.0_x64__2p2nqsd0c76g0"
                      r"\app\chatgpt.exe")
        legacy_path = (r"c:\program files\windowsapps"
                       r"\openai.chatgpt-desktop_1.2.3_x64__abc\app\chatgpt.exe")
        self.assertTrue(sender._matches_spec(spec, "ChatGPT", "chatgpt.exe",
                                             codex_path))
        self.assertFalse(sender._matches_spec(spec, "ChatGPT", "chatgpt.exe",
                                              legacy_path))
        self.assertFalse(sender._matches_spec(spec, "ChatGPT", "chatgpt.exe"))

    def test_specs_without_path_requirement_ignore_the_path(self):
        spec = _spec()
        self.assertTrue(sender._matches_spec(spec, "", "target.exe",
                                             r"c:\anywhere\target.exe"))


def _stub_enum_user32(windows):
    """user32 stub for find_target_windows. `windows` maps hwnd -> dict with
    title, visible, owner (0 = unowned)."""
    u = mock.Mock()

    def enum(cb, lparam):
        for h in windows:
            cb(h, lparam)
        return 1

    def get_text(h, buf, _n):
        buf.value = windows[h]["title"]
        return len(buf.value)

    u.EnumWindows.side_effect = enum
    u.IsWindowVisible.side_effect = lambda h: windows[h]["visible"]
    u.GetWindow.side_effect = lambda h, _cmd: windows[h]["owner"]
    u.GetWindowTextLengthW.side_effect = lambda h: len(windows[h]["title"])
    u.GetWindowTextW.side_effect = get_text
    return u


class FindTargetWindowsTests(unittest.TestCase):
    """The enumeration that decides which window receives the keystrokes,
    driven end to end with a stubbed EnumWindows: the image path produced
    here is what _matches_spec judges."""

    CODEX = (r"c:\program files\windowsapps"
             r"\openai.codex_26.901.4073.0_x64__2p2nqsd0c76g0\app\chatgpt.exe")
    LEGACY = (r"c:\program files\windowsapps"
              r"\openai.chatgpt-desktop_1.0_x64__abc\app\chatgpt.exe")

    def _find(self, windows, paths, spec=None):
        spec = spec or sender.TARGETS["codex"]
        with mock.patch.object(sender, "user32", _stub_enum_user32(windows)), \
             mock.patch.object(sender, "_get_process_path",
                               side_effect=lambda h: paths[h]):
            return sender.find_target_windows(spec)

    def test_codex_package_window_is_found(self):
        windows = {1: dict(title="ChatGPT", visible=1, owner=0)}
        self.assertEqual(self._find(windows, {1: self.CODEX}),
                         [(1, "ChatGPT")])

    def test_legacy_chatgpt_install_is_rejected(self):
        windows = {1: dict(title="ChatGPT", visible=1, owner=0)}
        self.assertEqual(self._find(windows, {1: self.LEGACY}), [])

    def test_hidden_and_owned_windows_are_skipped(self):
        windows = {
            1: dict(title="ChatGPT", visible=0, owner=0),   # hidden
            2: dict(title="ChatGPT", visible=1, owner=99),  # owned pop-up
            3: dict(title="ChatGPT", visible=1, owner=0),
        }
        paths = {1: self.CODEX, 2: self.CODEX, 3: self.CODEX}
        self.assertEqual(self._find(windows, paths), [(3, "ChatGPT")])

    def test_title_match_still_works_for_title_targets(self):
        windows = {1: dict(title="Claude", visible=1, owner=0)}
        found = self._find(windows, {1: r"c:\apps\claude\claude.exe"},
                           spec=sender.TARGETS["claude"])
        self.assertEqual(found, [(1, "Claude")])


class CodexTargetSpecTests(unittest.TestCase):
    def test_codex_target_shape(self):
        spec = sender.TARGETS["codex"]
        self.assertEqual(spec.window_title_contains, "")
        self.assertIn("chatgpt.exe", spec.exe_names)
        self.assertEqual(spec.focus_method, "uia_composer")
        self.assertEqual(spec.exe_path_contains, ("openai.codex",))
        self.assertIs(spec.message_problem, sender.codex_message_problem)


class CodexMessageProblemTests(unittest.TestCase):
    """A leading '/' opens the Codex slash-command menu and '@' the mention
    list; Enter would then pick a menu entry instead of sending."""

    def test_plain_message_passes(self):
        self.assertIsNone(sender.codex_message_problem("continue"))
        self.assertIsNone(sender.codex_message_problem("go on, use a/b tests"))

    def test_leading_slash_rejected(self):
        self.assertTrue(sender.codex_message_problem("/compact"))
        self.assertTrue(sender.codex_message_problem("  /model"))

    def test_at_sign_rejected(self):
        self.assertTrue(sender.codex_message_problem("look at @README.md"))

    def test_gui_validation_applies_it_to_codex_only(self):
        s = dict(gui.DEFAULTS)
        s["target"] = "codex"
        s["message"] = "/compact"
        self.assertTrue(gui.validate_settings(s))
        s["message"] = "mail me @home"
        self.assertTrue(gui.validate_settings(s))
        s["message"] = "continue"
        self.assertIsNone(gui.validate_settings(s))
        s["target"] = "claude"
        s["message"] = "/compact"
        self.assertIsNone(gui.validate_settings(s))


class ZCodeTargetSpecTests(unittest.TestCase):
    """ZCode (Zhipu's GLM coding agent, an Electron app at
    C:\\Program Files\\ZCode\\ZCode.exe, process image zcode.exe)."""

    def test_zcode_target_shape(self):
        spec = sender.TARGETS["zcode"]
        self.assertEqual(spec.name, "ZCode")
        self.assertEqual(spec.window_title_contains, "")  # exe-only
        self.assertEqual(spec.exe_names, ("zcode.exe",))
        self.assertEqual(spec.focus_method, "uia_composer")
        self.assertTrue(spec.prefer_largest_window)
        self.assertIs(spec.message_problem, sender.zcode_message_problem)

    def test_zcode_pins_no_composer_class(self):
        # The composer's UIA ClassName is a run of Tailwind utility classes;
        # pinning it would break at the next restyling. It is the only Edit
        # in the window, so "any Edit, bottom-most" is the stable locator.
        self.assertIsNone(sender.TARGETS["zcode"].composer_class)
        self.assertEqual(sender.TARGETS["codex"].composer_class,
                         sender.CODEX_COMPOSER_CLASS)

    def test_zcode_needs_no_image_path_check(self):
        # Unlike Codex there is no MSIX package and no same-named legacy
        # install to tell apart, so the exe name alone identifies the app.
        self.assertEqual(sender.TARGETS["zcode"].exe_path_contains, ())


class ZCodeMessageProblemTests(unittest.TestCase):
    """The ZCode composer's own placeholder advertises both menus ('@ to add
    context, / for commands or capabilities'), so Enter after such a message
    would pick a menu entry instead of sending."""

    def test_plain_message_passes(self):
        self.assertIsNone(sender.zcode_message_problem("continue"))
        self.assertIsNone(sender.zcode_message_problem("go on, use a/b tests"))

    def test_leading_slash_rejected(self):
        self.assertTrue(sender.zcode_message_problem("/clear"))
        self.assertTrue(sender.zcode_message_problem("  /compact"))

    def test_at_sign_rejected(self):
        self.assertTrue(sender.zcode_message_problem("look at @README.md"))

    def test_reason_names_zcode(self):
        # The message reaches the user through the GUI dialog and the log.
        self.assertIn("ZCode", sender.zcode_message_problem("/clear"))
        self.assertIn("ZCode", sender.zcode_message_problem("@file"))

    def test_gui_validation_applies_it_to_zcode(self):
        s = dict(gui.DEFAULTS)
        s["target"] = "zcode"
        s["message"] = "/clear"
        self.assertTrue(gui.validate_settings(s))
        s["message"] = "mail me @home"
        self.assertTrue(gui.validate_settings(s))
        s["message"] = "continue"
        self.assertIsNone(gui.validate_settings(s))

    def test_gui_accepts_the_zcode_target(self):
        s = dict(gui.DEFAULTS)
        s["target"] = "zcode"
        self.assertIsNone(gui.validate_settings(s))


class ZCodeMatchingTests(unittest.TestCase):
    """Exe-only matching. The window title is plain 'ZCode', which an
    Explorer folder or a browser tab would also carry while the app is
    closed; matching such a window would type into it."""

    ZCODE_PATH = r"c:\program files\zcode\zcode.exe"

    def test_title_alone_never_matches(self):
        spec = sender.TARGETS["zcode"]
        self.assertFalse(sender._matches_spec(
            spec, "ZCode", "explorer.exe", r"c:\windows\explorer.exe"))
        self.assertFalse(sender._matches_spec(
            spec, "ZCode - Google Chrome", "chrome.exe",
            r"c:\program files\google\chrome\chrome.exe"))
        self.assertFalse(sender._matches_spec(
            spec, "zcode.py - PyCharm", "pycharm64.exe"))

    def test_exe_matches_with_and_without_a_path(self):
        spec = sender.TARGETS["zcode"]
        self.assertTrue(sender._matches_spec(spec, "ZCode", "zcode.exe",
                                             self.ZCODE_PATH))
        self.assertTrue(sender._matches_spec(spec, "", "zcode.exe"))

    def test_enumeration_finds_the_main_window_only(self):
        # The same process also owns an invisible pop-up and 0x0 helper
        # windows; find_target_windows drops hidden and owned ones.
        windows = {
            1: dict(title="ZCode", visible=0, owner=0),   # 588x102 pop-up
            2: dict(title="ZCode", visible=1, owner=0),   # main window
            3: dict(title="Default IME", visible=1, owner=7),
        }
        paths = {h: self.ZCODE_PATH for h in windows}
        with mock.patch.object(sender, "user32", _stub_enum_user32(windows)), \
             mock.patch.object(sender, "_get_process_path",
                               side_effect=lambda h: paths[h]):
            found = sender.find_target_windows(sender.TARGETS["zcode"])
        self.assertEqual(found, [(2, "ZCode")])

    def test_explorer_window_titled_zcode_is_not_found(self):
        windows = {1: dict(title="ZCode", visible=1, owner=0)}
        with mock.patch.object(sender, "user32", _stub_enum_user32(windows)), \
             mock.patch.object(sender, "_get_process_path",
                               return_value=r"c:\windows\explorer.exe"):
            self.assertEqual(
                sender.find_target_windows(sender.TARGETS["zcode"]), [])


class UiaBootstrapTests(unittest.TestCase):
    """_uia() must survive a thread that some library already initialised
    for the multi-threaded apartment (RPC_E_CHANGED_MODE): COM is usable
    there, and treating it as 'unavailable' would switch every Codex guard
    off for the rest of the GUI session."""

    def _with_fake_comtypes(self, coinit_error):
        fake = types.ModuleType("comtypes")
        client = types.ModuleType("comtypes.client")

        def coinit():
            if coinit_error is not None:
                raise coinit_error
        fake.CoInitialize = coinit
        fake.client = client
        mod = types.SimpleNamespace(IUIAutomation=object())
        client.GetModule = lambda _name: mod
        client.CreateObject = lambda _clsid, interface=None: "uia-client"
        with mock.patch.dict(sys.modules,
                             {"comtypes": fake, "comtypes.client": client}):
            return sender._uia(), mod

    def test_changed_mode_is_tolerated(self):
        err = OSError()
        err.winerror = sender._RPC_E_CHANGED_MODE
        result, mod = self._with_fake_comtypes(err)
        self.assertEqual(result, ("uia-client", mod))

    def test_other_com_failures_yield_none(self):
        err = OSError()
        err.winerror = -2147467259  # E_FAIL
        result, _ = self._with_fake_comtypes(err)
        self.assertIsNone(result)

    def test_missing_comtypes_yields_none(self):
        with mock.patch.dict(sys.modules, {"comtypes": None,
                                           "comtypes.client": None}):
            self.assertIsNone(sender._uia())


class FindCodexComposerTests(unittest.TestCase):
    """The Codex composer is the bottom-most ProseMirror edit element. Chromium
    builds its accessibility tree lazily on first UIA contact, so an empty
    FindAll result is retried before giving up."""

    def test_picks_bottom_most_prosemirror_edit(self):
        top_editor = _fake_element((40, 300, 900, 360))
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[top_editor, composer]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)

    def test_skips_empty_rectangles(self):
        ghost = _fake_element((0, 0, 0, 0))
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[composer, ghost]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)

    def test_retries_while_tree_is_empty(self):
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[], [composer]])
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)
        fake_time.sleep.assert_called_once_with(
            sender.CODEX_COMPOSER_FIND_DELAY_S)

    def test_returns_none_after_attempt_budget(self):
        uia, mod = _fake_uia([[]] * sender.CODEX_COMPOSER_FIND_ATTEMPTS)
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIsNone(sender._find_codex_composer(uia, mod, 42))
        root = uia.ElementFromHandle.return_value
        self.assertEqual(root.FindAll.call_count,
                         sender.CODEX_COMPOSER_FIND_ATTEMPTS)

    def test_condition_uses_edit_type_and_prosemirror_class(self):
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[composer]])
        conditions = {}
        uia.CreatePropertyCondition.side_effect = (
            lambda pid, value: conditions.setdefault((pid, value), object()))
        with mock.patch.object(sender, "time", mock.Mock()):
            sender._find_codex_composer(uia, mod, 42)
        # The two property conditions must be AND-ed, and that AND condition
        # is what reaches FindAll — not just one of its ingredients.
        uia.CreateAndCondition.assert_called_once_with(
            conditions[(30003, 50004)], conditions[(30012, "ProseMirror")])
        root = uia.ElementFromHandle.return_value
        self.assertEqual(root.FindAll.call_args.args,
                         (4, uia.CreateAndCondition.return_value))

    def test_bottom_most_wins_even_when_not_last_in_tree_order(self):
        # A ProseMirror editor in a dialog can come AFTER the composer in
        # tree order; visual position decides, not enumeration order.
        composer = _fake_element((40, 1649, 986, 1713))
        dialog = _fake_element((300, 400, 700, 460))
        uia, mod = _fake_uia([[composer, dialog]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)

    def test_com_error_on_an_attempt_uses_the_retry_budget(self):
        # A provider that is not ready yet raises rather than returning an
        # empty array; that must count as "try again", not abort.
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[composer]])
        root = uia.ElementFromHandle.return_value
        good = root.FindAll.side_effect
        root.FindAll.side_effect = [OSError("UIA_E_ELEMENTNOTAVAILABLE"),
                                    good(None, None)]
        logs = []
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42,
                                                      log=logs.append),
                          composer)
        self.assertTrue(any("attempt 1" in line for line in logs))

    def test_element_that_vanishes_mid_scan_is_skipped(self):
        ghost = _fake_element((0, 0, 10, 10))
        type(ghost).CurrentBoundingRectangle = mock.PropertyMock(
            side_effect=OSError("gone"))
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[ghost, composer]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)


class FindComposerTests(unittest.TestCase):
    """The generalized lookup. ZCode's composer class is a run of Tailwind
    utility classes — not an identifier that survives a restyling — but it is
    the only Edit element in the window, so that target pins no class and the
    query is ControlType == Edit alone. Codex keeps the ProseMirror class."""

    # The real ZCode geometry: composer at the window bottom, the "Switch
    # mode" / "Max" combo boxes of the button row BELOW it.
    COMPOSER = (2240, 1902, 3336, 1976)
    COMBO_LEFT = (2298, 1997, 2512, 2048)
    COMBO_RIGHT = (3137, 1997, 3278, 2048)

    def _zcode_window(self):
        """One window's worth of elements: the composer, the widgets below
        it, and a Document root that spans everything."""
        composer = _fake_element(self.COMPOSER, cls=ZCODE_COMPOSER_CLASS)
        return composer, [
            _fake_element((1707, 0, 3415, 2101), cls="", ctype=50030),
            composer,
            _fake_element(self.COMBO_LEFT, cls="flex items-center",
                          ctype=50003),
            _fake_element((2908, 1997, 3131, 2048), cls="group/button",
                          ctype=50000),
            _fake_element(self.COMBO_RIGHT, cls="flex w-fit", ctype=50003),
        ]

    def test_without_a_class_filter_only_edits_are_asked_for(self):
        composer, elements = self._zcode_window()
        uia, mod = _fake_filtering_uia([elements])
        with mock.patch.object(sender, "time", mock.Mock()):
            found = sender._find_composer(uia, mod, 42)
        # The ComboBoxes sit LOWER than the composer; if the query did not
        # restrict the control type, "bottom-most" would pick one of them.
        self.assertIs(found, composer)
        uia.CreateAndCondition.assert_not_called()
        root = uia.ElementFromHandle.return_value
        self.assertEqual(root.FindAll.call_args.args,
                         (4, ("prop", 30003, 50004)))

    def test_without_a_class_filter_any_edit_class_is_accepted(self):
        # The point of composer_class=None: whatever Tailwind emits today.
        for cls in (ZCODE_COMPOSER_CLASS, "", "something-else-entirely"):
            composer = _fake_element(self.COMPOSER, cls=cls)
            uia, mod = _fake_filtering_uia([[composer]])
            with mock.patch.object(sender, "time", mock.Mock()):
                self.assertIs(sender._find_composer(uia, mod, 42), composer)

    def test_without_a_class_filter_bottom_most_edit_wins(self):
        top_edit = _fake_element((2240, 300, 3336, 360), cls="search-box")
        composer, elements = self._zcode_window()
        uia, mod = _fake_filtering_uia([[top_edit] + elements])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_composer(uia, mod, 42), composer)

    def test_codex_finder_still_requires_the_prosemirror_class(self):
        # A lower, differently-classed Edit must NOT be taken for the Codex
        # composer: the class is what tells the two apart.
        prosemirror = _fake_element((41, 1649, 986, 1713))
        lower_other = _fake_element((41, 1800, 986, 1860),
                                    cls=ZCODE_COMPOSER_CLASS)
        uia, mod = _fake_filtering_uia([[prosemirror, lower_other]])
        with mock.patch.object(sender, "time", mock.Mock()):
            found = sender._find_codex_composer(uia, mod, 42)
        self.assertIs(found, prosemirror)
        uia.CreateAndCondition.assert_called_once_with(
            ("prop", 30003, 50004), ("prop", 30012, "ProseMirror"))

    def test_class_filter_finds_nothing_when_the_class_is_gone(self):
        # Fail closed: an app update that renames the class must not silently
        # fall back to "any edit element".
        uia, mod = _fake_filtering_uia(
            [[_fake_element(self.COMPOSER, cls=ZCODE_COMPOSER_CLASS)]]
            * sender.COMPOSER_FIND_ATTEMPTS)
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIsNone(sender._find_codex_composer(uia, mod, 42))

    def test_retry_budget_covers_a_cold_chromium_tree(self):
        # Measured on ZCode: the first FindAll after ElementFromHandle saw 13
        # descendants and no Edit at all; a later probe saw 266 and the
        # composer. The budget must survive more than one empty answer.
        composer = _fake_element(self.COMPOSER, cls=ZCODE_COMPOSER_CLASS)
        uia, mod = _fake_filtering_uia([[], [], [], [composer]])
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time):
            self.assertIs(sender._find_composer(uia, mod, 42), composer)
        self.assertEqual(fake_time.sleep.call_count, 3)
        self.assertGreaterEqual(sender.COMPOSER_FIND_ATTEMPTS, 4)

    def test_legacy_budget_names_still_point_at_the_shared_budgets(self):
        self.assertEqual(sender.CODEX_COMPOSER_FIND_ATTEMPTS,
                         sender.COMPOSER_FIND_ATTEMPTS)
        self.assertEqual(sender.CODEX_COMPOSER_FIND_DELAY_S,
                         sender.COMPOSER_FIND_DELAY_S)
        self.assertEqual(sender.CODEX_FOCUS_POLL_ATTEMPTS,
                         sender.COMPOSER_FOCUS_POLL_ATTEMPTS)
        self.assertEqual(sender.CODEX_TEXT_POLL_ATTEMPTS,
                         sender.COMPOSER_TEXT_POLL_ATTEMPTS)


class IsElementFocusedTests(unittest.TestCase):
    def test_true_when_uia_says_same_element(self):
        uia = mock.Mock()
        uia.CompareElements.return_value = 1
        self.assertTrue(sender._is_element_focused(
            uia, _fake_element((0, 0, 1, 1))))

    def test_true_when_focused_is_a_prosemirror_edit_at_the_same_rect(self):
        # Fallback identity when CompareElements says no: same type, class
        # AND bounding rectangle.
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element((41, 1649, 986, 1713))
        self.assertTrue(sender._is_element_focused(
            uia, _fake_element((41, 1649, 986, 1713))))

    def test_same_class_fallback_is_not_pinned_to_prosemirror(self):
        # The fallback identity compares the focused element's class with the
        # element's OWN class, so it also works for a target that pins no
        # class (ZCode's Tailwind class run).
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element(
            (2240, 1902, 3336, 1976), cls=ZCODE_COMPOSER_CLASS)
        self.assertTrue(sender._is_element_focused(
            uia, _fake_element((2240, 1902, 3336, 1976),
                               cls=ZCODE_COMPOSER_CLASS)))

    def test_false_when_the_focused_edit_has_another_class(self):
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element(
            (2240, 1902, 3336, 1976), cls="some-other-editor")
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((2240, 1902, 3336, 1976),
                               cls=ZCODE_COMPOSER_CLASS)))

    def test_false_for_a_prosemirror_edit_elsewhere(self):
        # Another ProseMirror editor (rename dialog, notes) must not pass as
        # the composer just because it shares type and class.
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element((300, 400, 700, 460))
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((41, 1649, 986, 1713))))

    def test_false_for_other_elements(self):
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element(
            (0, 0, 1, 1), cls="btn", ctype=50000)
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((0, 0, 1, 1))))

    def test_false_when_element_itself_denies_keyboard_focus(self):
        # Both signals must agree: the system-wide focused element AND the
        # element's own HasKeyboardFocus flag (Chromium applies focus
        # asynchronously; a window that failed to come to the foreground can
        # disagree with itself).
        uia = mock.Mock()
        uia.CompareElements.return_value = 1
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((0, 0, 1, 1), has_focus=False)))

    def test_false_when_uia_raises(self):
        uia = mock.Mock()
        uia.GetFocusedElement.side_effect = OSError("COM failure")
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((0, 0, 1, 1))))


class WaitForFocusTests(unittest.TestCase):
    def test_returns_immediately_on_first_success(self):
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time), \
             mock.patch.object(sender, "_is_element_focused",
                               return_value=True):
            self.assertTrue(sender._wait_for_focus(mock.Mock(), mock.Mock()))
        fake_time.sleep.assert_not_called()

    def test_polls_then_gives_up(self):
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time), \
             mock.patch.object(sender, "_is_element_focused",
                               return_value=False) as check:
            self.assertFalse(sender._wait_for_focus(mock.Mock(), mock.Mock()))
        self.assertEqual(check.call_count, sender.CODEX_FOCUS_POLL_ATTEMPTS)
        self.assertEqual(fake_time.sleep.call_count,
                         sender.CODEX_FOCUS_POLL_ATTEMPTS - 1)

    def test_late_focus_is_accepted(self):
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_is_element_focused",
                               side_effect=[False, False, True]):
            self.assertTrue(sender._wait_for_focus(mock.Mock(), mock.Mock()))


class ComposerTextTests(unittest.TestCase):
    def test_reads_value_pattern(self):
        self.assertEqual(sender._composer_text(_fake_handle("hello")), "hello")

    def test_null_value_is_unreadable_not_the_word_none(self):
        # comtypes maps a NULL BSTR to None; str(None) would invent a draft
        # called 'None'.
        self.assertIsNone(sender._composer_text(_fake_handle(None)))
        self.assertIsNone(sender._composer_draft(_fake_handle(None)))

    def test_none_when_pattern_unavailable(self):
        # comtypes never returns None from GetCurrentPattern; a falsy
        # pointer is the "unsupported" signal.
        self.assertIsNone(
            sender._composer_text(_fake_handle(pattern_available=False)))

    def test_none_when_uia_raises(self):
        handle = _fake_handle()
        handle.element.GetCurrentPattern.side_effect = OSError("COM failure")
        self.assertIsNone(sender._composer_text(handle))


class ComposerDraftTests(unittest.TestCase):
    """The empty composer reports its placeholder through the Value pattern;
    the placeholder equals the element's accessible name."""

    def _handle(self, value, name="Do anything"):
        handle = _fake_handle(value)
        handle.element.CurrentName = name
        return handle

    def test_placeholder_means_empty(self):
        self.assertEqual(sender._composer_draft(self._handle("\nDo anything")),
                         "")
        self.assertEqual(sender._composer_draft(self._handle("   ")), "")

    def test_real_text_is_reported(self):
        self.assertEqual(sender._composer_draft(self._handle("\nhalf a thought")),
                         "half a thought")

    def test_unreadable_is_none(self):
        self.assertIsNone(sender._composer_draft(
            _fake_handle(pattern_available=False)))


class VerifyTypedTextTests(unittest.TestCase):
    """Assert the effect of typing: the composer must contain the message
    before Enter is pressed."""

    def test_passes_when_text_contains_message(self):
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time):
            sender._verify_typed_text(_fake_handle("draft continue"),
                                      "continue", log=lambda _t: None)
        fake_time.sleep.assert_not_called()

    def test_accepts_late_value(self):
        # The Value pattern can lag the keystrokes a little.
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text",
                               side_effect=["\nDo anything", "continue"]):
            sender._verify_typed_text(_fake_handle(), "continue",
                                      log=lambda _t: None)

    def test_raises_when_text_never_contains_message(self):
        logs = []
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text",
                               return_value="\nDo anything") as read:
            with self.assertRaises(RuntimeError):
                sender._verify_typed_text(_fake_handle(), "continue",
                                          log=logs.append)
        self.assertEqual(read.call_count, sender.CODEX_TEXT_POLL_ATTEMPTS)

    def test_unreadable_text_warns_and_passes(self):
        logs = []
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text", return_value=None):
            sender._verify_typed_text(_fake_handle(), "continue",
                                      log=logs.append)
        self.assertTrue(any("WARN" in line for line in logs))

    def test_readable_mismatch_stays_sticky_when_later_polls_are_unreadable(self):
        # A readable mismatch is evidence the keystrokes never landed; a
        # later unreadable poll must not launder it into "cannot verify".
        logs = []
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text",
                               side_effect=["\nDo anything", None, None]):
            with self.assertRaises(RuntimeError):
                sender._verify_typed_text(_fake_handle(), "continue",
                                          log=logs.append)
        self.assertFalse(any("WARN" in line for line in logs))

    def test_placeholder_substring_does_not_satisfy_a_short_message(self):
        # 'hi' is a substring of the placeholder 'Do anything'; the text
        # must have CHANGED from what the box showed before typing.
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text",
                               return_value="\nDo anything"):
            with self.assertRaises(RuntimeError):
                sender._verify_typed_text(_fake_handle(), "hi",
                                          log=lambda _t: None,
                                          baseline="\nDo anything")

    def test_changed_text_containing_message_passes_with_baseline(self):
        with mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_composer_text",
                               return_value="\nhi"):
            sender._verify_typed_text(_fake_handle(), "hi",
                                      log=lambda _t: None,
                                      baseline="\nDo anything")


class FocusedControlTypeTests(unittest.TestCase):
    def test_none_when_uia_unavailable(self):
        with mock.patch.object(sender, "_uia", return_value=None):
            self.assertIsNone(sender._focused_control_type())

    def test_reads_type_from_focused_element(self):
        uia = mock.Mock()
        uia.GetFocusedElement.return_value.CurrentControlType = 50003
        with mock.patch.object(sender, "_uia", return_value=(uia, object())):
            self.assertEqual(sender._focused_control_type(), 50003)


class CodexComposerFocusTests(unittest.TestCase):
    """Codex desktop app: no safe focus shortcut exists (Shift+Esc = clear
    unreads, Esc STOPS a running turn), so the composer is located via UIA,
    focused with SetFocus, verified, and clicked as a fallback. Every failure
    raises before anything is typed.

    The patched finder is `_find_composer`: the lookup was generalized when
    the ZCode target arrived, and the focus path now calls it with
    `spec.composer_class`. `_find_codex_composer` (the ProseMirror-pinned
    wrapper) still exists and is covered by FindCodexComposerTests."""

    RECT = (41, 1649, 986, 1713)

    def _run(self, focused, composer="default", uia_available=True,
             draft=""):
        """`focused` feeds _wait_for_focus: one entry per focus attempt
        (after SetFocus, after the click)."""
        if composer == "default":
            composer = _fake_element(self.RECT)
        fake = mock.Mock()
        logs = []
        self.uia = mock.Mock(name="uia")
        self.mod = types.SimpleNamespace(tag="mod")
        handles = (self.uia, self.mod) if uia_available else None
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia", return_value=handles), \
             mock.patch.object(sender, "_find_composer",
                               return_value=composer), \
             mock.patch.object(sender, "_wait_for_focus",
                               side_effect=focused), \
             mock.patch.object(sender, "_composer_draft",
                               return_value=draft), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(0, 704, 1024, 1088)), \
             mock.patch.object(sender, "_window_dpi_scale", return_value=1.25):
            result = sender._focus_input(sender.TARGETS["codex"], 42,
                                         "ChatGPT", log=logs.append)
        return fake, composer, logs, result

    def test_existing_draft_aborts_before_typing(self):
        # Whatever is in the composer would be submitted together with the
        # message; refuse rather than append to an unknown draft.
        with self.assertRaises(RuntimeError):
            self._run(focused=[True], draft="half a thought")

    def test_unreadable_draft_warns_and_continues(self):
        _, _, logs, result = self._run(focused=[True], draft=None)
        self.assertTrue(any("WARN" in line for line in logs))
        self.assertIsInstance(result, sender.ComposerHandle)

    def test_setfocus_alone_when_verified(self):
        fake, composer, _, result = self._run(focused=[True])
        composer.SetFocus.assert_called_once_with()
        fake.click.assert_not_called()
        self.assertIsInstance(result, sender.ComposerHandle)
        self.assertIs(result.element, composer)
        # The handle must carry the very client/module it was built from:
        # send_once re-checks focus and reads the text back through them.
        self.assertIs(result.uia, self.uia)
        self.assertIs(result.mod, self.mod)

    def test_click_into_element_when_setfocus_does_not_take(self):
        fake, composer, _, result = self._run(focused=[False, True])
        composer.SetFocus.assert_called_once_with()
        fake.click.assert_called_once_with((41 + 986) // 2, (1649 + 1713) // 2)
        self.assertIs(result.element, composer)

    def test_setfocus_exception_falls_back_to_click(self):
        composer = _fake_element(self.RECT)
        composer.SetFocus.side_effect = OSError("E_FAIL")
        fake, _, _, _ = self._run(focused=[False, True], composer=composer)
        fake.click.assert_called_once()

    def test_raises_when_neither_setfocus_nor_click_focuses(self):
        with self.assertRaises(RuntimeError):
            self._run(focused=[False, False])

    def test_raises_when_composer_not_found(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia",
                               return_value=(mock.Mock(), object())), \
             mock.patch.object(sender, "_find_composer",
                               return_value=None):
            with self.assertRaises(RuntimeError):
                sender._focus_codex_composer(42, log=lambda _t: None)
        fake.click.assert_not_called()

    def test_without_uia_raises_and_never_clicks(self):
        # This target's whole safety story is UIA-based: with UIA gone there
        # is nothing to verify against, so no blind click and no typing.
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia", return_value=None):
            with self.assertRaises(RuntimeError):
                sender._focus_codex_composer(42, log=lambda _t: None)
        fake.click.assert_not_called()


class SendOnceCodexTests(unittest.TestCase):
    def _send(self, focused_after_typing=50004, handle=None,
              composer_texts=("\nDo anything", "continue"), foreground=True,
              element_focused=True, message="continue"):
        """Runs send_once("codex"); the fake pyautogui is kept on self.fake
        so assertions still work when send_once raises. `composer_texts`
        feeds _composer_text: the read before typing, then the read-backs."""
        self.fake = mock.Mock()
        texts = list(composer_texts)
        reads = iter(texts)

        def read(_handle):
            try:
                return next(reads)
            except StopIteration:
                return texts[-1]

        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ChatGPT")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ChatGPT")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input",
                               return_value=handle), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=focused_after_typing), \
             mock.patch.object(sender, "_is_foreground",
                               return_value=foreground), \
             mock.patch.object(sender, "_is_element_focused",
                               return_value=element_focused), \
             mock.patch.object(sender, "_composer_text", side_effect=read), \
             mock.patch.object(sender, "pyautogui", self.fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("codex", message, log=lambda _t: None)
        return self.fake

    def test_enter_pressed_when_composer_keeps_focus(self):
        fake = self._send(focused_after_typing=50004)
        fake.typewrite.assert_called_once()
        fake.press.assert_called_once_with("enter")

    def test_no_enter_when_focus_lost_after_typing(self):
        with self.assertRaises(RuntimeError):
            self._send(focused_after_typing=50000)
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_enter_pressed_when_read_back_shows_the_message(self):
        fake = self._send(handle=_fake_handle(),
                          composer_texts=("\nDo anything", "\ncontinue"))
        fake.press.assert_called_once_with("enter")

    def test_no_enter_when_read_back_lacks_message(self):
        # The keystrokes went somewhere else (or were swallowed): the
        # composer still shows only its placeholder. Fail closed.
        with self.assertRaises(RuntimeError):
            self._send(handle=_fake_handle(),
                       composer_texts=("\nDo anything", "\nDo anything"))
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_no_enter_when_read_back_is_unchanged_for_short_message(self):
        # 'hi' is inside the placeholder 'Do anything': an unchanged box is
        # not proof of typing.
        with self.assertRaises(RuntimeError):
            self._send(handle=_fake_handle(), message="hi",
                       composer_texts=("\nDo anything", "\nDo anything"))
        self.fake.press.assert_not_called()

    def test_no_enter_when_window_lost_foreground(self):
        with self.assertRaises(RuntimeError):
            self._send(handle=_fake_handle(), foreground=False)
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_no_enter_when_composer_element_lost_focus(self):
        # Checked against the very element that was focused, not just "some
        # text control somewhere on the desktop".
        with self.assertRaises(RuntimeError):
            self._send(handle=_fake_handle(), element_focused=False)
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_slash_or_at_message_refused_before_typing(self):
        for message in ("/compact", "ask @codex"):
            fake = mock.Mock()
            with mock.patch.object(sender, "find_target_windows",
                                   return_value=[(42, "ChatGPT")]), \
                 mock.patch.object(sender, "_pick_main_window",
                                   return_value=(42, "ChatGPT")), \
                 mock.patch.object(sender, "force_activate_window",
                                   return_value=True) as activate, \
                 mock.patch.object(sender, "pyautogui", fake), \
                 mock.patch.object(sender, "time", mock.Mock()):
                with self.assertRaises(sender.ConfigurationError):
                    sender.send_once("codex", message, log=lambda _t: None)
            activate.assert_not_called()
            fake.typewrite.assert_not_called()
            fake.press.assert_not_called()

    def test_prefer_largest_is_passed_to_window_picker(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ChatGPT")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ChatGPT")) as pick, \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input", return_value=None), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50004), \
             mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("codex", "continue", log=lambda _t: None)
        pick.assert_called_once_with([(42, "ChatGPT")], prefer_largest=True)


class SendOnceZCodeTests(unittest.TestCase):
    """send_once("zcode"). Same guards as Codex, but the baseline read of an
    EMPTY composer is "\\n" (ZCode's Lexical editor returns a bare newline
    through the Value pattern, not the placeholder)."""

    def _send(self, focused_after_typing=50004, handle=None,
              composer_texts=("\n", "\ncontinue"), foreground=True,
              element_focused=True, message="continue"):
        self.fake = mock.Mock()
        texts = list(composer_texts)
        reads = iter(texts)

        def read(_handle):
            try:
                return next(reads)
            except StopIteration:
                return texts[-1]

        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ZCode")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ZCode")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input",
                               return_value=handle), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=focused_after_typing), \
             mock.patch.object(sender, "_is_foreground",
                               return_value=foreground), \
             mock.patch.object(sender, "_is_element_focused",
                               return_value=element_focused), \
             mock.patch.object(sender, "_composer_text", side_effect=read), \
             mock.patch.object(sender, "pyautogui", self.fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("zcode", message, log=lambda _t: None)
        return self.fake

    def _handle(self):
        return _fake_handle(cls=ZCODE_COMPOSER_CLASS)

    def test_enter_pressed_when_read_back_shows_the_message(self):
        fake = self._send(handle=self._handle())
        fake.typewrite.assert_called_once_with("continue", interval=0.05)
        fake.press.assert_called_once_with("enter")

    def test_no_enter_when_the_box_still_reads_empty(self):
        # The keystrokes went somewhere else: the composer still reports the
        # empty-value newline. Fail closed.
        with self.assertRaises(RuntimeError):
            self._send(handle=self._handle(), composer_texts=("\n", "\n"))
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_no_enter_when_focus_lost_after_typing(self):
        with self.assertRaises(RuntimeError):
            self._send(focused_after_typing=50000)
        self.fake.typewrite.assert_called_once()
        self.fake.press.assert_not_called()

    def test_no_enter_when_window_lost_foreground(self):
        with self.assertRaises(RuntimeError):
            self._send(handle=self._handle(), foreground=False)
        self.fake.press.assert_not_called()

    def test_no_enter_when_composer_element_lost_focus(self):
        with self.assertRaises(RuntimeError):
            self._send(handle=self._handle(), element_focused=False)
        self.fake.press.assert_not_called()

    def test_slash_or_at_message_refused_before_typing(self):
        for message in ("/clear", "ask @zcode"):
            fake = mock.Mock()
            with mock.patch.object(sender, "find_target_windows",
                                   return_value=[(42, "ZCode")]), \
                 mock.patch.object(sender, "_pick_main_window",
                                   return_value=(42, "ZCode")), \
                 mock.patch.object(sender, "force_activate_window",
                                   return_value=True) as activate, \
                 mock.patch.object(sender, "pyautogui", fake), \
                 mock.patch.object(sender, "time", mock.Mock()):
                with self.assertRaises(sender.ConfigurationError):
                    sender.send_once("zcode", message, log=lambda _t: None)
            activate.assert_not_called()
            fake.typewrite.assert_not_called()
            fake.press.assert_not_called()

    def test_prefer_largest_is_passed_to_window_picker(self):
        # The invisible 588x102 pop-up shares process and title with the main
        # window; only size tells them apart.
        fake = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ZCode")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ZCode")) as pick, \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input", return_value=None), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50004), \
             mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("zcode", "continue", log=lambda _t: None)
        pick.assert_called_once_with([(42, "ZCode")], prefer_largest=True)

    def test_focus_input_gets_the_zcode_spec(self):
        # _focus_composer needs the spec: it decides the composer class and
        # the app name in every error message.
        fake = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ZCode")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ZCode")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input",
                               return_value=None) as focus, \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=50004), \
             mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("zcode", "continue", log=lambda _t: None)
        self.assertIs(focus.call_args.args[0], sender.TARGETS["zcode"])


class ZCodeComposerFocusTests(unittest.TestCase):
    """_focus_composer for the zcode spec: no shortcut is pressed (ZCode's
    only accelerators are Ctrl+N / Ctrl+O / Ctrl+W and zoom — Ctrl+W would
    CLOSE the window), the composer is found by control type alone, and
    every failure raises before anything is typed."""

    RECT = (2240, 1902, 3336, 1976)

    def _run(self, focused, composer="default", draft=""):
        if composer == "default":
            composer = _fake_element(self.RECT, cls=ZCODE_COMPOSER_CLASS)
        fake = mock.Mock()
        logs = []
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia",
                               return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(sender, "_find_composer",
                               return_value=composer) as find, \
             mock.patch.object(sender, "_wait_for_focus",
                               side_effect=focused), \
             mock.patch.object(sender, "_composer_draft",
                               return_value=draft):
            result = sender._focus_input(sender.TARGETS["zcode"], 42, "ZCode",
                                         log=logs.append)
        return fake, composer, logs, result, find

    def test_no_keyboard_shortcut_is_pressed(self):
        fake, composer, _logs, result, _find = self._run(focused=[True])
        composer.SetFocus.assert_called_once_with()
        fake.hotkey.assert_not_called()
        fake.press.assert_not_called()
        fake.click.assert_not_called()
        self.assertIsInstance(result, sender.ComposerHandle)

    def test_finder_is_called_without_a_class_filter(self):
        _fake, _composer, _logs, _result, find = self._run(focused=[True])
        self.assertIsNone(find.call_args.args[3])

    def test_click_into_element_when_setfocus_does_not_take(self):
        fake, _composer, _logs, _result, _find = self._run(
            focused=[False, True])
        fake.click.assert_called_once_with((2240 + 3336) // 2,
                                           (1902 + 1976) // 2)

    def test_existing_draft_aborts_before_typing(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(focused=[True], draft="half a thought")
        self.assertIn("ZCode", str(ctx.exception))

    def test_not_found_names_zcode_and_never_clicks(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia",
                               return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(sender, "_find_composer", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                sender._focus_input(sender.TARGETS["zcode"], 42, "ZCode",
                                    log=lambda _t: None)
        self.assertIn("ZCode", str(ctx.exception))
        fake.click.assert_not_called()

    def test_without_uia_raises_and_never_clicks(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia", return_value=None):
            with self.assertRaises(RuntimeError):
                sender._focus_input(sender.TARGETS["zcode"], 42, "ZCode",
                                    log=lambda _t: None)
        fake.click.assert_not_called()
        fake.typewrite.assert_not_called()


class DebugWindowsTests(unittest.TestCase):
    """debug_windows.py is the read-only diagnostic; it must probe the
    composer for EVERY uia_composer target, not just codex."""

    def _main(self, key, title, composer=None):
        found = [] if composer is None else [composer]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["debug_windows.py", key]), \
             mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, title)]), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(1707, 0, 1706, 2100)), \
             mock.patch.object(sender, "_get_process_path",
                               return_value=r"c:\program files\zcode\zcode.exe"), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, title)), \
             mock.patch.object(sender, "_uia",
                               return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(sender, "_find_composer",
                               return_value=composer) as find, \
             mock.patch.object(sender, "_composer_text",
                               return_value="\n"), \
             mock.patch.object(sender, "_composer_draft", return_value=""), \
             contextlib.redirect_stdout(out):
            debug_windows.main()
        return out.getvalue(), find

    def test_zcode_composer_is_probed(self):
        composer = _fake_element((2240, 1902, 3336, 1976),
                                 cls=ZCODE_COMPOSER_CLASS)
        composer.CurrentName = "Ask ZCode anything..."
        composer.CurrentIsKeyboardFocusable = 1
        text, find = self._main("zcode", "ZCode", composer)
        self.assertIn("Composer:", text)
        self.assertIn("Ask ZCode anything...", text)
        self.assertIn("(2240, 1902, 3336, 1976)", text)
        self.assertIn("value='\\n'", text)
        # ...with the target's own (absent) class filter.
        self.assertIsNone(find.call_args.args[3])

    def test_missing_composer_is_reported_not_crashed(self):
        text, _find = self._main("zcode", "ZCode", composer=None)
        self.assertIn("No edit element", text)

    def test_non_composer_target_is_not_probed(self):
        _text, find = self._main("claude", "Claude")
        find.assert_not_called()

    def test_unknown_target_exits(self):
        with mock.patch.object(sys, "argv", ["debug_windows.py", "nope"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                debug_windows.main()


class GuiTargetChoicesTests(unittest.TestCase):
    def test_every_sender_target_has_a_radio_button(self):
        # The radio values are the TARGETS keys; a target without a button
        # can only be selected by editing settings.json.
        pairs = []
        fake_ttk = mock.Mock()
        fake_ttk.Radiobutton.side_effect = (
            lambda *_a, **kw: pairs.append((kw.get("value"),
                                            kw.get("variable")))
            or mock.Mock())
        with mock.patch.object(gui, "ttk", fake_ttk), \
             mock.patch.object(gui, "tk", mock.Mock()):
            g = object.__new__(gui.ContinueSenderGUI)
            g.root = mock.Mock()
            for name in ("target_var", "message_var", "initial_hours_var",
                         "initial_minutes_var", "repeat_var", "every_hours_var",
                         "every_minutes_var", "count_var"):
                setattr(g, name, mock.Mock())
            g._build_layout()
        self.assertEqual(sorted(v for v, _ in pairs), sorted(sender.TARGETS))
        # ...and every button drives the target variable (the classic slip
        # when a third radio is cloned from the second).
        self.assertTrue(all(var is g.target_var for _, var in pairs))


class CliWrapperTests(unittest.TestCase):
    """The four *_continue.py wrappers share cli.py; each must keep its
    target and turn the flags into send_loop arguments unchanged."""

    def _run(self, module, argv):
        with mock.patch.object(sys, "argv", ["prog"] + argv), \
             mock.patch.object(sender, "send_loop") as loop:
            module.main()
        return loop

    def test_codex_wrapper_forwards_all_flags(self):
        loop = self._run(codex_continue, [
            "--minutes", "30", "--every-hours", "2", "--count", "3",
            "--message", "go on"])
        loop.assert_called_once_with(target="codex", message="go on",
                                     initial_delay_s=1800.0, every_s=7200.0,
                                     count=3)

    def test_no_repeat_passes_none_interval(self):
        loop = self._run(codex_continue, ["--hours", "1"])
        loop.assert_called_once_with(target="codex", message="continue",
                                     initial_delay_s=3600.0, every_s=None,
                                     count=0)

    def test_zcode_wrapper_forwards_all_flags(self):
        loop = self._run(zcode_continue, [
            "--minutes", "30", "--every-hours", "2", "--count", "3",
            "--message", "go on"])
        loop.assert_called_once_with(target="zcode", message="go on",
                                     initial_delay_s=1800.0, every_s=7200.0,
                                     count=3)

    def test_zcode_wrapper_defaults(self):
        loop = self._run(zcode_continue, [])
        loop.assert_called_once_with(target="zcode", message="continue",
                                     initial_delay_s=0.0, every_s=None,
                                     count=0)

    def test_other_wrappers_keep_their_targets(self):
        self.assertEqual(
            self._run(claude_continue, []).call_args.kwargs["target"],
            "claude")
        self.assertEqual(
            self._run(antigravity_continue, []).call_args.kwargs["target"],
            "antigravity")

    def test_every_target_has_a_cli_wrapper(self):
        # A target reachable only from the GUI is half-delivered.
        wrappers = {"claude": claude_continue,
                    "antigravity": antigravity_continue,
                    "codex": codex_continue,
                    "zcode": zcode_continue}
        self.assertEqual(sorted(wrappers), sorted(sender.TARGETS))
        for target, module in wrappers.items():
            self.assertEqual(
                self._run(module, []).call_args.kwargs["target"], target)


if __name__ == "__main__":
    unittest.main()

