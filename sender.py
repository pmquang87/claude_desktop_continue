"""Shared core for typing a message into a target Windows application.

Used by claude_continue.py, antigravity_continue.py, and gui.py.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import pyautogui

pyautogui.FAILSAFE = False

# Bound at import so the check keeps working when tests replace the pyautogui
# module attribute with a mock.
_is_valid_key = pyautogui.isValidKey

# Windows API constants
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOTOWNER = 3

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, ctypes.wintypes.LPARAM]
user32.EnumWindows.restype = ctypes.wintypes.BOOL
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.wintypes.HWND


@dataclass(frozen=True)
class TargetSpec:
    name: str
    window_title_contains: str
    exe_names: tuple[str, ...]
    focus_method: str  # "click_bottom_center" or "hotkey_ctrl_l"
    blocklist: tuple[str, ...] = ()


TARGETS: dict[str, TargetSpec] = {
    "claude": TargetSpec(
        name="Claude Desktop",
        window_title_contains="Claude",
        exe_names=("claude.exe",),
        focus_method="click_bottom_center",
        blocklist=(" – ", " - Visual Studio", ".py", ".js", ".ts"),
    ),
    "antigravity": TargetSpec(
        name="Antigravity IDE",
        window_title_contains="Antigravity",
        exe_names=("antigravity ide.exe", "antigravity.exe"),
        focus_method="agent_input",
        blocklist=(),
    ),
}

# UIA control types that accept typed text. Chromium exposes the Antigravity
# chat input as a ComboBox (50003); Edit (50004) and Document (50030) cover
# ordinary inputs and rich-text editors.
UIA_TEXT_ENTRY_TYPES = frozenset({50003, 50004, 50030})

# Click-fallback geometry for the Agent Manager chat input, measured from the
# live window: the input box is anchored to the window bottom, its editable
# line ~92 CSS px above the bottom edge (below it sits the model-selector row,
# which must not be clicked — it opens a dropdown). Horizontally the box spans
# the conversation column: right of the ~440 px sidebar and, when an artifact
# panel is open, left of that panel — the fractions probe positions that fall
# inside the box in every observed layout.
AGENT_INPUT_BOTTOM_OFFSET_PX = 92
AGENT_INPUT_X_FRACTIONS = (0.42, 0.55, 0.30, 0.68)

# The IDE editor window titles itself "<file> - <folder> - Antigravity"; the
# Agent Manager window is titled after the active conversation instead.
_IDE_TITLE_SUFFIX = "antigravity"

def _get_process_name(hwnd) -> str:
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h_process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not h_process:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size))
        return buf.value.rsplit("\\", 1)[-1].lower() if buf.value else ""
    finally:
        kernel32.CloseHandle(h_process)


def find_target_windows(spec: TargetSpec) -> list[tuple[int, str]]:
    """Return (hwnd, title) pairs matching `spec`. Exe-name matches first."""
    matches: list[tuple[int, str, str]] = []
    GW_OWNER = 4

    cb = None
    def enum_cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd):
                # Only consider unowned (main) windows to avoid invisible overlays
                if user32.GetWindow(hwnd, GW_OWNER) != 0:
                    return True
                
                exe = _get_process_name(hwnd)

                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                
                if (title and spec.window_title_contains.lower() in title.lower()) or exe in spec.exe_names:
                    matches.append((hwnd, title, exe))
        except Exception as e:
            import traceback
            traceback.print_exc()
        return True

    cb = EnumWindowsProc(enum_cb)
    user32.EnumWindows(cb, 0)

    return _select_matches(matches, spec)


def _select_matches(
    matches: list[tuple[int, str, str]], spec: TargetSpec
) -> list[tuple[int, str]]:
    """Pick target windows from raw (hwnd, title, exe) matches.

    Exe-name matches are authoritative. Title-only matches must survive the
    blocklist; a blocklisted window is never a valid target — better to find
    nothing than to type into an editor."""
    preferred = [(h, t) for h, t, exe in matches if exe in spec.exe_names]
    if preferred:
        return preferred

    # Case-insensitive, like the title match itself.
    blocklist = tuple(b.lower() for b in spec.blocklist)
    return [
        (h, t) for h, t, _ in matches
        if not any(b in t.lower() for b in blocklist)
    ]


def get_window_rect(hwnd) -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


# Anything smaller is treated as a utility/overlay window (quick-entry panel,
# undocked DevTools stub, ...), not the main app window. Real main windows are
# far larger; the click_bottom_center offset (80 px) also needs headroom so the
# focus click cannot land outside the window.
MIN_MAIN_WINDOW_W = 200
MIN_MAIN_WINDOW_H = 200


def _pick_main_window(
    windows: list[tuple[int, str]]
) -> Optional[tuple[int, str]]:
    """First visible window that plausibly is the main app window. Minimized
    windows are only a fallback: their rect is meaningless (-32000 coords)
    until force_activate_window restores them, so their true size is unknown
    and a visible full-size window is the safer pick."""
    iconic_fallback = None
    for hwnd, title in windows:
        if user32.IsIconic(hwnd):
            if iconic_fallback is None:
                iconic_fallback = (hwnd, title)
            continue
        _left, _top, width, height = get_window_rect(hwnd)
        if width >= MIN_MAIN_WINDOW_W and height >= MIN_MAIN_WINDOW_H:
            return hwnd, title
    return iconic_fallback


def _is_foreground(hwnd) -> bool:
    """True if `hwnd` (or a window it owns, e.g. a dialog) has the focus.
    Returns False on the secure desktop (locked workstation): GetForegroundWindow
    is NULL there, and typing would go to the lock screen."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    return fg == hwnd or user32.GetAncestor(fg, GA_ROOTOWNER) == hwnd


def force_activate_window(hwnd) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)

    # Press and release ALT key to trick Windows into allowing foreground change
    # This avoids the risk of deadlocks associated with AttachThreadInput
    pyautogui.keyDown('alt')
    pyautogui.keyUp('alt')

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.BringWindowToTop(hwnd)
    ok = user32.SetForegroundWindow(hwnd)

    if ok:
        time.sleep(0.5)
        if _is_foreground(hwnd):
            return True
        # SetForegroundWindow reported success but focus went elsewhere;
        # fall through to the topmost dance.

    user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    time.sleep(0.2)
    user32.SetWindowPos(
        hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return _is_foreground(hwnd)


def _focused_control_type() -> Optional[int]:
    """UIA control type of the element that has keyboard focus, or None when
    it cannot be read (comtypes missing, COM failure). None means "cannot
    verify", not "verification failed" — callers must not treat it as a
    mismatch."""
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",  # CLSID_CUIAutomation
            interface=mod.IUIAutomation,
        )
        return int(uia.GetFocusedElement().CurrentControlType)
    except Exception:
        return None


def _window_dpi_scale(hwnd) -> float:
    try:
        dpi = user32.GetDpiForWindow(hwnd)
    except Exception:
        return 1.0
    return dpi / 96.0 if dpi else 1.0


def unsupported_chars(message: str) -> str:
    """Characters that pyautogui.typewrite would silently drop (anything not
    in its key map: umlauts, Vietnamese diacritics, dashes, emoji) or that
    would corrupt the send (newline submits mid-message, tab can move focus
    out of the input). Returns them deduplicated, '' if the message is safe."""
    bad = [ch for ch in message
           if ch in "\n\t\r" or not _is_valid_key(ch)]
    return "".join(dict.fromkeys(bad))


def _focus_agent_manager_input(hwnd, log: Callable[[str], None]) -> None:
    """Focus the chat input of the standalone Agent Manager window.

    Ctrl+L is DEAD on this surface (both bindings ship disableInWeb), so the
    input only ever received text when it happened to still hold the window's
    internal focus. Ctrl+I is the one live 'focusInput' binding; the result is
    verified via UIA, with clicks into the input box as fallback."""
    pyautogui.hotkey("ctrl", "i")
    time.sleep(0.5)
    ct = _focused_control_type()
    if ct is None:
        # Cannot verify. Trust the documented shortcut rather than clicking
        # blind — a miss-click could land in the artifact panel and steal the
        # focus Ctrl+I just set.
        log("[WARN] UIA focus check unavailable; trusting Ctrl+I unverified.")
        return
    if ct in UIA_TEXT_ENTRY_TYPES:
        return
    left, top, width, height = get_window_rect(hwnd)
    click_y = top + height - int(
        AGENT_INPUT_BOTTOM_OFFSET_PX * _window_dpi_scale(hwnd)
    )
    for frac in AGENT_INPUT_X_FRACTIONS:
        click_x = left + int(width * frac)
        log(f"[INFO] Input not focused (control type {ct}); "
            f"clicking ({click_x}, {click_y}).")
        pyautogui.click(click_x, click_y)
        time.sleep(0.4)
        ct = _focused_control_type()
        if ct in UIA_TEXT_ENTRY_TYPES:
            return
    raise RuntimeError(
        "Could not focus the Antigravity agent input (Ctrl+I and every click "
        "candidate left focus on a non-text element). Not typing."
    )


def _focus_ide_agent_panel(log: Callable[[str], None]) -> None:
    """Focus the agent input of the IDE editor window. Ctrl+L there
    (antigravity.toggleChatFocus) CLOSES the panel when it is already open and
    focused, so focus is first forced into the editor (Ctrl+1) — after that
    Ctrl+L can only open-and-focus or focus, never close."""
    pyautogui.hotkey("ctrl", "1")
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.5)
    ct = _focused_control_type()
    if ct is None:
        log("[WARN] UIA focus check unavailable; trusting Ctrl+L unverified.")
        return
    if ct not in UIA_TEXT_ENTRY_TYPES:
        raise RuntimeError(
            f"Ctrl+L left focus on a non-text element (control type {ct}); "
            f"not typing into the editor."
        )


def _focus_input(
    spec: TargetSpec, hwnd, title: str,
    log: Callable[[str], None] = print,
) -> None:
    if spec.focus_method == "click_bottom_center":
        left, top, width, height = get_window_rect(hwnd)
        click_x = left + width // 2
        click_y = top + height - 80
        pyautogui.click(click_x, click_y)
        time.sleep(0.5)
    elif spec.focus_method == "agent_input":
        if title.strip().lower().endswith(_IDE_TITLE_SUFFIX):
            _focus_ide_agent_panel(log)
        else:
            _focus_agent_manager_input(hwnd, log)
    else:
        raise ValueError(f"Unknown focus_method: {spec.focus_method}")


def send_once(
    target: str,
    message: str,
    log: Callable[[str], None] = print,
) -> None:
    if target not in TARGETS:
        raise ValueError(f"Unknown target: {target}. Choices: {list(TARGETS)}")
    spec = TARGETS[target]

    windows = find_target_windows(spec)
    if not windows:
        raise RuntimeError(
            f"No window matching {spec.name} found. "
            f"Is it running?"
        )

    picked = _pick_main_window(windows)
    if picked is None:
        raise RuntimeError(
            f"Found {len(windows)} {spec.name} window(s), but none looks "
            f"like a main window (all tiny/overlay windows)."
        )
    hwnd, title = picked
    log(f"[INFO] Using window: '{title}' (handle: {hwnd})")

    bad = unsupported_chars(message)
    if bad:
        raise RuntimeError(
            f"Message contains characters that typing would silently drop "
            f"or misinterpret: {bad!r}. Use plain single-line ASCII text."
        )

    # Release possibly-stuck modifiers BEFORE activating the target: a stray
    # Alt key-up delivered to the target window can focus its menu bar, and
    # the keystrokes that follow would land there instead of the input.
    for key in ("ctrl", "shift", "alt", "win"):
        pyautogui.keyUp(key)

    if not force_activate_window(hwnd):
        raise RuntimeError(
            f"Could not bring the {spec.name} window to the foreground "
            f"(is the workstation locked?). Not typing, to avoid sending "
            f"the message to the wrong window."
        )
    time.sleep(1.0)

    _focus_input(spec, hwnd, title, log=log)

    log(f"[INFO] Typing '{message}'...")
    pyautogui.typewrite(message, interval=0.05)
    time.sleep(0.3)
    if spec.focus_method == "agent_input":
        ct = _focused_control_type()
        if ct is not None and ct not in UIA_TEXT_ENTRY_TYPES:
            raise RuntimeError(
                f"Focus left the agent input while typing (control type "
                f"{ct}); not pressing Enter."
            )
    pyautogui.press("enter")
    time.sleep(0.3)

    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Sent '{message}' to {spec.name}.")


def _wake_screen() -> None:
    pyautogui.moveRel(1, 0)
    time.sleep(0.5)
    pyautogui.moveRel(-1, 0)
    time.sleep(1.0)


def send_loop(
    target: str,
    message: str,
    initial_delay_s: float,
    every_s: Optional[float],
    count: int,
    stop_event: Optional[threading.Event] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> None:
    """Send `message` to `target` after `initial_delay_s`, then optionally
    repeat every `every_s`. Stops after `count` sends (0 = infinite) or when
    `stop_event` is set. Fires `on_event(dict)` for GUI hooks."""
    stop_event = stop_event or threading.Event()

    def emit(event: dict) -> None:
        if on_event is not None:
            on_event(event)

    def log(text: str) -> None:
        print(text)
        emit({"type": "log", "text": text})

    def wait(seconds: float, label: str) -> bool:
        if seconds <= 0:
            return True
        target_time = datetime.now() + timedelta(seconds=seconds)
        emit({"type": "status", "text": f"{label} for {int(seconds)}s"})
        while True:
            if stop_event.is_set():
                return False
            remaining = (target_time - datetime.now()).total_seconds()
            if remaining <= 0:
                return True
            emit({"type": "countdown", "remaining_s": int(remaining)})
            hours, rem = divmod(int(remaining), 3600)
            minutes, secs = divmod(rem, 60)
            print(
                f"\r  {label}: {hours:02d}:{minutes:02d}:{secs:02d}  ",
                end="",
                flush=True,
            )
            time.sleep(1)

    sent = 0
    try:
        if initial_delay_s > 0:
            log(f"[INFO] Initial delay: {initial_delay_s / 60:.1f} minutes")
            if not wait(initial_delay_s, "Waiting"):
                emit({"type": "done", "reason": "stopped"})
                return
            print()

        while True:
            if stop_event.is_set():
                emit({"type": "done", "reason": "stopped"})
                return
            _wake_screen()
            try:
                send_once(target, message, log=log)
            except Exception as e:
                log(f"[ERROR] {e}")
                emit({"type": "done", "reason": "error"})
                return
            sent += 1
            emit({"type": "sent", "n": sent})

            if count > 0 and sent >= count:
                emit({"type": "done", "reason": "count_reached"})
                return
            if every_s is None or every_s <= 0:
                emit({"type": "done", "reason": "count_reached"})
                return
            if not wait(every_s, "Next send"):
                emit({"type": "done", "reason": "stopped"})
                return
            print()
    except KeyboardInterrupt:
        print()  # terminate the \r countdown line
        log("[INFO] Cancelled by user.")
        emit({"type": "done", "reason": "stopped"})
    except Exception:
        # send_once errors are handled above; this catches everything else
        # (_wake_screen, wait, ...) so the GUI always gets a terminal event
        # instead of staying disabled forever.
        print()
        log(f"[ERROR] Unexpected failure:\n{traceback.format_exc()}")
        emit({"type": "done", "reason": "error"})
