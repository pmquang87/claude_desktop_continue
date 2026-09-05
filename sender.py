"""Shared core for typing a message into a target Windows application.

Used by claude_continue.py, antigravity_continue.py, codex_continue.py (via
cli.py) and gui.py.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, NamedTuple, Optional

import pyautogui

pyautogui.FAILSAFE = False

# Bound at import so the check keeps working when tests replace the pyautogui
# module attribute with a mock.
_is_valid_key = pyautogui.isValidKey

# comtypes initialises COM for the importing thread at import time. Import it
# here, on the main thread, so the first `import comtypes` never happens on a
# worker thread that some library already put into the multi-threaded
# apartment (that import would raise RPC_E_CHANGED_MODE before _uia()'s own
# tolerance for it applies). Missing comtypes is handled in _uia().
try:
    import comtypes  # noqa: F401
    import comtypes.client  # noqa: F401
except Exception:
    pass

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


def codex_message_problem(message: str) -> Optional[str]:
    """Why `message` must not be typed into the Codex composer, or None.
    A leading '/' opens the slash-command menu and '@' opens the mention
    list, so Enter would pick a menu entry instead of sending the text."""
    if message.lstrip().startswith("/"):
        return ("starts with '/' (opens the Codex slash-command menu; Enter "
                "would run a command instead of sending)")
    if "@" in message:
        return ("contains '@' (opens the Codex mention list; Enter would "
                "pick a mention instead of sending)")
    return None


@dataclass(frozen=True)
class TargetSpec:
    name: str
    window_title_contains: str  # "" disables title matching (exe-only target)
    exe_names: tuple[str, ...]
    focus_method: str  # "click_bottom_center", "agent_input" or "uia_composer"
    blocklist: tuple[str, ...] = ()
    # Pick the largest candidate window instead of the first (Z-order) one:
    # for apps whose pop-ups share the main window's title and process.
    prefer_largest_window: bool = False
    # An exe-name match additionally needs one of these substrings in the
    # lower-cased full image path (e.g. an MSIX package family), so a
    # same-named executable of another app is not accepted.
    exe_path_contains: tuple[str, ...] = ()
    # Target-specific message validation: returns a reason to refuse the
    # message, or None. Checked by the GUI up front and by send_once.
    message_problem: Optional[Callable[[str], Optional[str]]] = None


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
    # The Codex desktop app ships as the MSIX package OpenAI.Codex, but its
    # process image is ChatGPT.exe (a Codex.exe launcher stub sits next to
    # it). The window title is "ChatGPT", which would also match browser tabs
    # and Explorer folders when the app is closed, so matching is exe-only —
    # and the image path must lie in the OpenAI.Codex package, so a legacy
    # stand-alone ChatGPT desktop install (also ChatGPT.exe) is not accepted.
    # The app's quick-chat / hotkey pop-ups and the avatar overlay are
    # windows of the same process with the same title; the primary window is
    # the largest one.
    "codex": TargetSpec(
        name="Codex",
        window_title_contains="",
        exe_names=("chatgpt.exe", "codex.exe"),
        focus_method="uia_composer",
        blocklist=(),
        prefer_largest_window=True,
        exe_path_contains=("openai.codex",),
        message_problem=codex_message_problem,
    ),
}

# UIA control types that accept typed text. Chromium exposes the Antigravity
# chat input as a ComboBox (50003); Edit (50004) and Document (50030) cover
# ordinary inputs and rich-text editors.
UIA_TEXT_ENTRY_TYPES = frozenset({50003, 50004, 50030})

# The Codex desktop app composer, as seen by UI Automation: an Edit element
# whose class is the ProseMirror editor root. It is the bottom-most such
# element in the window (the button row below it holds no editors).
UIA_EDIT_CONTROL_TYPE = 50004
CODEX_COMPOSER_CLASS = "ProseMirror"
# Chromium builds its accessibility tree lazily on first UIA contact; the
# first FindAll after activation may legitimately come back empty.
CODEX_COMPOSER_FIND_ATTEMPTS = 4
CODEX_COMPOSER_FIND_DELAY_S = 1.0
# Chromium applies a UIA focus request asynchronously, so the focus check is
# polled briefly instead of read once.
CODEX_FOCUS_POLL_ATTEMPTS = 5
CODEX_FOCUS_POLL_DELAY_S = 0.15
# Same for the text read-back after typing (the Value pattern lags a little).
CODEX_TEXT_POLL_ATTEMPTS = 3
CODEX_TEXT_POLL_DELAY_S = 0.2

# Focus methods that verify, via UIA, that a text element still holds focus
# after typing and before Enter is pressed.
_VERIFIED_FOCUS_METHODS = frozenset({"agent_input", "uia_composer"})

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

def _get_process_path(hwnd) -> str:
    """Lower-cased full image path of the process owning `hwnd`, '' if it
    cannot be read."""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h_process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not h_process:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(1024)
        kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size))
        return buf.value.lower() if buf.value else ""
    finally:
        kernel32.CloseHandle(h_process)


def _get_process_name(hwnd) -> str:
    """Lower-cased image file name (e.g. 'claude.exe') of the process owning
    `hwnd`, '' if it cannot be read."""
    return _get_process_path(hwnd).rsplit("\\", 1)[-1]


def _matches_spec(spec: TargetSpec, title: str, exe: str, path: str = "") -> bool:
    """Window (title, exe, image path) belongs to `spec`. Title matching is
    opt-in: an empty window_title_contains means exe-only (see the codex
    target). An exe match must also satisfy exe_path_contains when set."""
    needle = spec.window_title_contains.lower()
    if needle and title and needle in title.lower():
        return True
    if exe not in spec.exe_names:
        return False
    if spec.exe_path_contains:
        return any(part in path for part in spec.exe_path_contains)
    return True


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

                path = _get_process_path(hwnd)
                exe = path.rsplit("\\", 1)[-1]

                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value

                if _matches_spec(spec, title, exe, path):
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
    windows: list[tuple[int, str]], prefer_largest: bool = False,
) -> Optional[tuple[int, str]]:
    """First visible window that plausibly is the main app window — or, with
    `prefer_largest`, the largest such window. Minimized windows are only a
    fallback: their rect is meaningless (-32000 coords) until
    force_activate_window restores them, so their true size is unknown and a
    visible full-size window is the safer pick."""
    iconic_fallback = None
    best = None
    best_area = -1
    for hwnd, title in windows:
        if user32.IsIconic(hwnd):
            if iconic_fallback is None:
                iconic_fallback = (hwnd, title)
            continue
        _left, _top, width, height = get_window_rect(hwnd)
        if width >= MIN_MAIN_WINDOW_W and height >= MIN_MAIN_WINDOW_H:
            if not prefer_largest:
                return hwnd, title
            if width * height > best_area:
                best, best_area = (hwnd, title), width * height
    return best if best is not None else iconic_fallback


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


# CoInitialize on a thread that some library already put into the
# multi-threaded apartment: COM is initialised and usable, just not in the
# apartment comtypes asked for.
_RPC_E_CHANGED_MODE = -2147417850


def _uia():
    """(IUIAutomation client, UIAutomationCore module) or None when UI
    Automation cannot be used (comtypes missing, COM failure). None means
    "cannot verify", never "verification failed"."""
    try:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitialize()
        except OSError as e:
            if getattr(e, "winerror", None) != _RPC_E_CHANGED_MODE:
                raise
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",  # CLSID_CUIAutomation
            interface=mod.IUIAutomation,
        )
        return uia, mod
    except Exception:
        return None


def _focused_control_type() -> Optional[int]:
    """UIA control type of the element that has keyboard focus, or None when
    it cannot be read. None means "cannot verify", not "verification
    failed" — callers must not treat it as a mismatch."""
    handles = _uia()
    if handles is None:
        return None
    uia, _mod = handles
    try:
        return int(uia.GetFocusedElement().CurrentControlType)
    except Exception:
        return None


def _element_rect(element) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a UIA element in physical pixels —
    pyautogui makes this process DPI-aware at import, so these coordinates
    can be clicked directly."""
    r = element.CurrentBoundingRectangle
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def _find_codex_composer(uia, mod, hwnd, log: Callable[[str], None] = None):
    """Bottom-most ProseMirror edit element inside `hwnd`, or None.

    A COM error during an attempt (provider not ready yet, element gone
    between two calls) counts like an empty result: it is logged and the
    next attempt runs — the retry budget exists exactly for that."""
    condition = uia.CreateAndCondition(
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId,
                                    UIA_EDIT_CONTROL_TYPE),
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId,
                                    CODEX_COMPOSER_CLASS),
    )
    for attempt in range(CODEX_COMPOSER_FIND_ATTEMPTS):
        if attempt:
            time.sleep(CODEX_COMPOSER_FIND_DELAY_S)
        try:
            root = uia.ElementFromHandle(hwnd)
            found = root.FindAll(mod.TreeScope_Descendants, condition)
            best = None
            best_bottom = None
            for i in range(found.Length):
                try:
                    element = found.GetElement(i)
                    left, top, right, bottom = _element_rect(element)
                except Exception:
                    continue  # element vanished mid-scan; look at the rest
                if right <= left or bottom <= top:
                    continue  # collapsed/offscreen editor, cannot be clicked
                if best is None or bottom > best_bottom:
                    best, best_bottom = element, bottom
        except Exception as e:
            if log is not None:
                log(f"[INFO] UIA query failed on attempt {attempt + 1}: {e}")
            continue
        if best is not None:
            return best
    return None


def _is_element_focused(uia, element) -> bool:
    """True if `element` holds keyboard focus, judged by two independent
    signals that must both agree: the system-wide focused element is this
    one (UIA CompareElements, verified stable across queries in the Codex
    app; as a guard against a version where it is not, a focused ProseMirror
    edit at exactly the same bounding rectangle counts too — a different
    editor never sits at the composer's bottom-anchored rectangle), and the
    element itself reports HasKeyboardFocus. A window that could not become
    foreground can mark its element focused internally while keystrokes
    would land elsewhere; the system-wide check catches that."""
    try:
        focused = uia.GetFocusedElement()
        same = bool(uia.CompareElements(focused, element)) or (
            int(focused.CurrentControlType) == UIA_EDIT_CONTROL_TYPE
            and (focused.CurrentClassName or "") == CODEX_COMPOSER_CLASS
            and _element_rect(focused) == _element_rect(element)
        )
        return same and bool(element.CurrentHasKeyboardFocus)
    except Exception:
        return False


def _wait_for_focus(uia, element) -> bool:
    """Poll _is_element_focused: Chromium honours SetFocus/clicks a little
    after the call returns."""
    for attempt in range(CODEX_FOCUS_POLL_ATTEMPTS):
        if attempt:
            time.sleep(CODEX_FOCUS_POLL_DELAY_S)
        if _is_element_focused(uia, element):
            return True
    return False


class ComposerHandle(NamedTuple):
    """A focused text element plus the UIA client it came from, so the text
    typed into it can be read back before Enter is pressed."""
    uia: object
    mod: object
    element: object


def _composer_text(handle: ComposerHandle) -> Optional[str]:
    """Text the composer currently holds (UIA Value pattern; the empty
    composer reports its placeholder). None when it cannot be read — that
    means "cannot verify", not "empty"."""
    try:
        pattern = handle.element.GetCurrentPattern(handle.mod.UIA_ValuePatternId)
        if not pattern:
            return None
        value = pattern.QueryInterface(handle.mod.IUIAutomationValuePattern)
        raw = value.CurrentValue
        # A NULL BSTR arrives as None: unreadable, not the word "None".
        return None if raw is None else str(raw)
    except Exception:
        return None


def _composer_draft(handle: ComposerHandle) -> Optional[str]:
    """Text already sitting in the composer: '' when it is empty (the Value
    pattern then reports the placeholder, which equals the element's
    accessible name), the draft otherwise, None when it cannot be read."""
    text = _composer_text(handle)
    if text is None:
        return None
    stripped = text.strip()
    try:
        placeholder = (handle.element.CurrentName or "").strip()
    except Exception:
        placeholder = ""
    if not stripped or (placeholder and stripped == placeholder):
        return ""
    return stripped


def _verify_typed_text(
    handle: ComposerHandle, message: str, log: Callable[[str], None],
    baseline: Optional[str] = None,
) -> None:
    """Assert the effect of typing, not the action: the composer's own text
    must contain `message` AND differ from `baseline` (the text read before
    typing — the empty composer reports its placeholder, and a short message
    such as 'hi' is a substring of 'Do anything'). Only a run in which no
    poll could be read at all is "cannot verify" (a warning); any readable
    mismatch aborts the send, whichever poll produced it."""
    before = (baseline or "").strip()
    last_readable = None
    for attempt in range(CODEX_TEXT_POLL_ATTEMPTS):
        if attempt:
            time.sleep(CODEX_TEXT_POLL_DELAY_S)
        text = _composer_text(handle)
        if text is None:
            continue
        last_readable = text
        if message in text and (baseline is None or text.strip() != before):
            return
    if last_readable is None:
        log("[WARN] Could not read the composer text back; trusting the "
            "keystrokes.")
        return
    raise RuntimeError(
        f"Composer text after typing is {last_readable!r}, which does not "
        f"show {message!r} as newly typed text; not pressing Enter."
    )


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


def _focus_codex_composer(
    hwnd, log: Callable[[str], None],
) -> Optional[ComposerHandle]:
    """Focus the composer of the Codex desktop app (ChatGPT.exe).

    No keyboard shortcut is safe here: the app ships no default binding that
    focuses the composer, Shift+Escape is 'clear all unreads', and plain
    Escape STOPS a running turn before it would ever focus the composer. So
    the composer is located through UI Automation (bottom-most ProseMirror
    edit element), focused via UIA SetFocus and verified; a click into the
    element is the fallback. Every failure — UIA unavailable, composer not
    found, focus not taken, a draft already in the box — raises before
    anything is typed. Returns the focused element so send_once can re-check
    focus and read the typed text back before Enter."""
    handles = _uia()
    if handles is None:
        raise RuntimeError(
            "UI Automation is unavailable (comtypes missing or COM failure); "
            "refusing to type into the Codex composer unverified."
        )
    uia, mod = handles
    composer = _find_codex_composer(uia, mod, hwnd, log=log)
    if composer is None:
        raise RuntimeError(
            f"Could not find the Codex composer (no {CODEX_COMPOSER_CLASS} "
            f"edit element in the window after "
            f"{CODEX_COMPOSER_FIND_ATTEMPTS} attempts). Not typing."
        )
    try:
        composer.SetFocus()
    except Exception as e:
        log(f"[INFO] UIA SetFocus failed ({e}); falling back to a click.")
    if not _wait_for_focus(uia, composer):
        left, top, right, bottom = _element_rect(composer)
        click_x, click_y = (left + right) // 2, (top + bottom) // 2
        log(f"[INFO] Composer not focused after SetFocus; clicking "
            f"({click_x}, {click_y}).")
        pyautogui.click(click_x, click_y)
        if not _wait_for_focus(uia, composer):
            raise RuntimeError(
                "Could not focus the Codex composer (SetFocus and a click "
                "both left focus elsewhere). Not typing."
            )

    handle = ComposerHandle(uia, mod, composer)
    draft = _composer_draft(handle)
    if draft is None:
        log("[WARN] Could not read the composer text; cannot tell whether a "
            "draft is already there.")
    elif draft:
        raise RuntimeError(
            f"The Codex composer already contains text ({draft!r}); not "
            f"typing on top of a draft."
        )
    return handle


def _focus_input(
    spec: TargetSpec, hwnd, title: str,
    log: Callable[[str], None] = print,
) -> Optional[ComposerHandle]:
    """Focus the target's input. Returns a ComposerHandle when the focused
    element is known (uia_composer), else None."""
    if spec.focus_method == "click_bottom_center":
        left, top, width, height = get_window_rect(hwnd)
        click_x = left + width // 2
        click_y = top + height - 80
        pyautogui.click(click_x, click_y)
        time.sleep(0.5)
        return None
    if spec.focus_method == "agent_input":
        if title.strip().lower().endswith(_IDE_TITLE_SUFFIX):
            _focus_ide_agent_panel(log)
        else:
            _focus_agent_manager_input(hwnd, log)
        return None
    if spec.focus_method == "uia_composer":
        return _focus_codex_composer(hwnd, log)
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

    picked = _pick_main_window(
        windows, prefer_largest=spec.prefer_largest_window
    )
    if picked is None:
        raise RuntimeError(
            f"Found {len(windows)} {spec.name} window(s), but none looks "
            f"like a main window (all tiny/overlay windows)."
        )
    hwnd, title = picked
    if len(windows) > 1:
        log(f"[INFO] {len(windows)} candidate windows: "
            + ", ".join(f"{h} '{t}'" for h, t in windows))
    log(f"[INFO] Using window: '{title}' (handle: {hwnd}, "
        f"exe: {_get_process_path(hwnd) or '?'})")

    bad = unsupported_chars(message)
    if bad:
        raise RuntimeError(
            f"Message contains characters that typing would silently drop "
            f"or misinterpret: {bad!r}. Use plain single-line ASCII text."
        )
    if spec.message_problem is not None:
        problem = spec.message_problem(message)
        if problem:
            raise RuntimeError(
                f"Message {problem}. Not typing it into {spec.name}."
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

    handle = _focus_input(spec, hwnd, title, log=log)
    baseline = None
    if isinstance(handle, ComposerHandle):
        baseline = _composer_text(handle)  # what the box shows before typing

    log(f"[INFO] Typing '{message}'...")
    pyautogui.typewrite(message, interval=0.05)
    time.sleep(0.3)
    if spec.focus_method in _VERIFIED_FOCUS_METHODS:
        ct = _focused_control_type()
        if ct is not None and ct not in UIA_TEXT_ENTRY_TYPES:
            raise RuntimeError(
                f"Focus left the input while typing (control type "
                f"{ct}); not pressing Enter."
            )
    if isinstance(handle, ComposerHandle):
        # Re-check against the very element that was focused, not just any
        # text control on the desktop: the window must still be foreground,
        # the composer must still hold focus, and the typed text must be in
        # it. Each check is independent of a fresh COM bootstrap.
        if not _is_foreground(hwnd):
            raise RuntimeError(
                f"The {spec.name} window lost the foreground while typing; "
                f"not pressing Enter."
            )
        if not _is_element_focused(handle.uia, handle.element):
            raise RuntimeError(
                f"The {spec.name} composer lost focus while typing; not "
                f"pressing Enter."
            )
        _verify_typed_text(handle, message, log, baseline=baseline)
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
