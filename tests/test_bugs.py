"""Regression tests for sender.py / gui.py bug fixes.

Run:  python -m unittest discover -s tests
All GUI-automation side effects (pyautogui, user32) are stubbed; these tests
never move the mouse, type, or open windows.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui
import sender
from sender import TargetSpec


def _spec(blocklist=(), exe_names=("target.exe",)):
    return TargetSpec(
        name="Test Target",
        window_title_contains="Target",
        exe_names=exe_names,
        focus_method="click_bottom_center",
        blocklist=blocklist,
    )


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
            with self.assertRaises(RuntimeError):
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


if __name__ == "__main__":
    unittest.main()

