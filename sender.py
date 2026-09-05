"""Shared core for typing a message into a target Windows application.

Used by claude_continue.py, antigravity_continue.py, codex_continue.py,
zcode_continue.py (via cli.py) and gui.py.
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


class ConfigurationError(ValueError):
    """A send that no retry can rescue: an unknown target, a message that
    typing would corrupt, or one the target's own rules refuse. send_loop
    ends a run at once on these; every other send_once error is treated as
    momentary in a repeat run (see send_loop)."""


def _slash_at_problem(message: str, app: str, at_menu: str) -> Optional[str]:
    """Shared rule for composers that open a menu on '/' and on '@': the
    keystroke opens a pop-up list, so the Enter that should send the message
    picks a list entry instead. Returns the reason to refuse, or None."""
    if message.lstrip().startswith("/"):
        return (f"starts with '/' (opens the {app} slash-command menu; Enter "
                f"would run a command instead of sending)")
    if "@" in message:
        return (f"contains '@' (opens the {app} {at_menu}; Enter would pick "
                f"an entry from it instead of sending)")
    return None


def codex_message_problem(message: str) -> Optional[str]:
    """Why `message` must not be typed into the Codex composer, or None.
    A leading '/' opens the slash-command menu and '@' opens the mention
    list."""
    return _slash_at_problem(message, "Codex", "mention list")


def zcode_message_problem(message: str) -> Optional[str]:
    """Why `message` must not be typed into the ZCode composer, or None.
    The composer's own placeholder advertises both menus ("@ to add context,
    / for commands or capabilities"), and the app bundle ships the
    slash-command list (chat.slash.* strings)."""
    return _slash_at_problem(message, "ZCode", "context picker")


# UIA control types that accept typed text. Chromium exposes the Antigravity
# chat input as a ComboBox (50003); Edit (50004) and Document (50030) cover
# ordinary inputs and rich-text editors.
UIA_TEXT_ENTRY_TYPES = frozenset({50003, 50004, 50030})

# The composer of a "uia_composer" target, as seen by UI Automation: an Edit
# element, optionally pinned to a ClassName (TargetSpec.composer_class). It is
# the bottom-most such element in the window — the button row below it holds
# buttons and combo boxes, never an editor.
UIA_EDIT_CONTROL_TYPE = 50004
# Codex uses the ProseMirror editor and exposes its root class; ZCode uses
# Lexical and exposes a run of Tailwind utility classes instead, so it pins no
# class (composer_class=None) and relies on being the only Edit in the window.
CODEX_COMPOSER_CLASS = "ProseMirror"
# Chromium builds its accessibility tree lazily on first UIA contact; the
# first FindAll after activation may legitimately come back empty. Measured on
# ZCode: the first ElementFromHandle+FindAll saw 13 descendants and no Edit at
# all, a later probe saw 266 and the composer. The budget only costs time on
# the way to a failure — a successful find returns from the first attempt —
# so it is generous.
COMPOSER_FIND_ATTEMPTS = 6
COMPOSER_FIND_DELAY_S = 1.0
# Chromium applies a UIA focus request asynchronously, so the focus check is
# polled briefly instead of read once.
COMPOSER_FOCUS_POLL_ATTEMPTS = 5
COMPOSER_FOCUS_POLL_DELAY_S = 0.15
# Same for the text read-back after typing (the Value pattern lags a little).
COMPOSER_TEXT_POLL_ATTEMPTS = 3
COMPOSER_TEXT_POLL_DELAY_S = 0.2

# Former names of the budgets above, from when Codex was the only
# uia_composer target. Kept so external callers and tests keep working; the
# budgets are shared by every uia_composer target.
CODEX_COMPOSER_FIND_ATTEMPTS = COMPOSER_FIND_ATTEMPTS
CODEX_COMPOSER_FIND_DELAY_S = COMPOSER_FIND_DELAY_S
CODEX_FOCUS_POLL_ATTEMPTS = COMPOSER_FOCUS_POLL_ATTEMPTS
CODEX_FOCUS_POLL_DELAY_S = COMPOSER_FOCUS_POLL_DELAY_S
CODEX_TEXT_POLL_ATTEMPTS = COMPOSER_TEXT_POLL_ATTEMPTS
CODEX_TEXT_POLL_DELAY_S = COMPOSER_TEXT_POLL_DELAY_S


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
    # focus_method "uia_composer" only: the UIA ClassName the composer Edit
    # element must have. None accepts ANY Edit element in the window — the
    # right choice when the app's class attribute is not a stable identifier
    # (ZCode's is a run of Tailwind utility classes) and the window holds no
    # other Edit. See _find_composer.
    composer_class: Optional[str] = None


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
        composer_class=CODEX_COMPOSER_CLASS,
    ),
    # ZCode, Zhipu's GLM coding agent desktop app: an Electron app (not a VS
    # Code fork) installed at C:\Program Files\ZCode\ZCode.exe, process image
    # zcode.exe. Its window title is "ZCode", which would also match an
    # Explorer folder or a browser tab while the app is closed, so matching is
    # exe-only — there is no MSIX package and no same-named legacy exe, so no
    # exe_path_contains is needed. The same process owns an invisible 588x102
    # pop-up window and several 0x0 helper windows besides the main window;
    # the main one is the largest. The composer is a Lexical contenteditable
    # whose UIA ClassName is a run of Tailwind utility classes (not a stable
    # identifier) and whose Name is the placeholder, which changes with the
    # app's state — so it pins no class and is found as the bottom-most (and
    # in fact only) Edit element in the window.
    "zcode": TargetSpec(
        name="ZCode",
        window_title_contains="",
        exe_names=("zcode.exe",),
        focus_method="uia_composer",
        blocklist=(),
        prefer_largest_window=True,
        message_problem=zcode_message_problem,
        composer_class=None,
    ),
}

# Focus methods that verify, via UIA, that a text element still holds focus
# after typing and before Enter is pressed.
_VERIFIED_FOCUS_METHODS = frozenset({"agent_input", "uia_composer"})

# A repeat run (interval set) survives this many failed sends in a row
# before send_loop gives up with done/error.
MAX_CONSECUTIVE_FAILURES = 3

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
    opt-in: an empty window_title_contains means exe-only (see the codex and
    zcode targets). An exe match must also satisfy exe_path_contains when
    set."""
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


def _find_composer(uia, mod, hwnd, composer_class: Optional[str] = None,
                   log: Callable[[str], None] = None):
    """Bottom-most edit element inside `hwnd`, or None.

    `composer_class` pins the UIA ClassName the element must have (Codex:
    "ProseMirror"); None accepts any Edit element, which is what ZCode needs —
    its composer class is a run of Tailwind utility classes that changes with
    every restyling, and it is the only Edit in the window. Either way the
    bottom-most candidate with a non-empty rectangle wins: the composer sits
    at the window bottom, above a button row of buttons and combo boxes.

    A COM error during an attempt (provider not ready yet, element gone
    between two calls) counts like an empty result: it is logged and the
    next attempt runs — the retry budget exists exactly for that."""
    condition = uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId,
                                            UIA_EDIT_CONTROL_TYPE)
    if composer_class:
        condition = uia.CreateAndCondition(
            condition,
            uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId,
                                        composer_class),
        )
    for attempt in range(COMPOSER_FIND_ATTEMPTS):
        if attempt:
            time.sleep(COMPOSER_FIND_DELAY_S)
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


def _find_codex_composer(uia, mod, hwnd, log: Callable[[str], None] = None):
    """The Codex composer: _find_composer pinned to the ProseMirror class."""
    return _find_composer(uia, mod, hwnd, CODEX_COMPOSER_CLASS, log=log)


def _is_element_focused(uia, element) -> bool:
    """True if `element` holds keyboard focus, judged by two independent
    signals that must both agree: the system-wide focused element is this
    one (UIA CompareElements, verified stable across queries in the Codex
    app; as a guard against a version where it is not, a focused edit of the
    SAME class at exactly the same bounding rectangle counts too — a
    different editor never sits at the composer's bottom-anchored
    rectangle), and the element itself reports HasKeyboardFocus. A window
    that could not become foreground can mark its element focused internally
    while keystrokes would land elsewhere; the system-wide check catches
    that."""
    try:
        focused = uia.GetFocusedElement()
        same = bool(uia.CompareElements(focused, element)) or (
            int(focused.CurrentControlType) == UIA_EDIT_CONTROL_TYPE
            and (focused.CurrentClassName or "")
            == (element.CurrentClassName or "")
            and _element_rect(focused) == _element_rect(element)
        )
        return same and bool(element.CurrentHasKeyboardFocus)
    except Exception:
        return False


def _wait_for_focus(uia, element) -> bool:
    """Poll _is_element_focused: Chromium honours SetFocus/clicks a little
    after the call returns."""
    for attempt in range(COMPOSER_FOCUS_POLL_ATTEMPTS):
        if attempt:
            time.sleep(COMPOSER_FOCUS_POLL_DELAY_S)
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
    """Text the composer currently holds (UIA Value pattern). What an EMPTY
    composer reports differs per app: Codex echoes its placeholder, ZCode a
    bare newline — _composer_draft maps both to "". None when the value
    cannot be read — that means "cannot verify", not "empty"."""
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
    """Text already sitting in the composer: '' when it is empty, the draft
    otherwise, None when it cannot be read. Empty covers both observed
    shapes — whitespace only (ZCode reports "\\n") and the placeholder echoed
    back (Codex), recognised because it equals the element's accessible
    name."""
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
    typing — an empty Codex composer reports its placeholder, and a short
    message such as 'hi' is a substring of 'Do anything'; an empty ZCode
    composer reports '\\n', so any typed text changes it). Only a run in
    which no poll could be read at all is "cannot verify" (a warning); any
    readable mismatch aborts the send, whichever poll produced it."""
    before = (baseline or "").strip()
    last_readable = None
    for attempt in range(COMPOSER_TEXT_POLL_ATTEMPTS):
        if attempt:
            time.sleep(COMPOSER_TEXT_POLL_DELAY_S)
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


def _focus_composer(
    spec: TargetSpec, hwnd, log: Callable[[str], None],
) -> Optional[ComposerHandle]:
    """Focus the composer of a "uia_composer" target (Codex, ZCode).

    Neither app has a safe focus shortcut. Codex ships no default binding for
    the composer, Shift+Escape is 'clear all unreads' and plain Escape STOPS
    a running turn. ZCode's Electron menu offers only Ctrl+N / Ctrl+O /
    Ctrl+W and the zoom accelerators — no composer binding, and Ctrl+W would
    close the window. So the composer is located through UI Automation
    (bottom-most Edit element, optionally pinned to spec.composer_class),
    focused via UIA SetFocus and verified; a click into the element is the
    fallback. Every failure — UIA unavailable, composer not found, focus not
    taken, a draft already in the box — raises before anything is typed.
    Returns the focused element so send_once can re-check focus and read the
    typed text back before Enter."""
    handles = _uia()
    if handles is None:
        raise RuntimeError(
            f"UI Automation is unavailable (comtypes missing or COM "
            f"failure); refusing to type into the {spec.name} composer "
            f"unverified."
        )
    uia, mod = handles
    composer = _find_composer(uia, mod, hwnd, spec.composer_class, log=log)
    if composer is None:
        what = (f"no {spec.composer_class} edit element"
                if spec.composer_class else "no edit element")
        raise RuntimeError(
            f"Could not find the {spec.name} composer ({what} in the window "
            f"after {COMPOSER_FIND_ATTEMPTS} attempts). Not typing."
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
                f"Could not focus the {spec.name} composer (SetFocus and a "
                f"click both left focus elsewhere). Not typing."
            )

    handle = ComposerHandle(uia, mod, composer)
    draft = _composer_draft(handle)
    if draft is None:
        log("[WARN] Could not read the composer text; cannot tell whether a "
            "draft is already there.")
    elif draft:
        raise RuntimeError(
            f"The {spec.name} composer already contains text ({draft!r}); "
            f"not typing on top of a draft."
        )
    return handle


def _focus_codex_composer(
    hwnd, log: Callable[[str], None],
) -> Optional[ComposerHandle]:
    """The Codex composer: _focus_composer for the codex target."""
    return _focus_composer(TARGETS["codex"], hwnd, log)


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
        return _focus_composer(spec, hwnd, log)
    raise ConfigurationError(f"Unknown focus_method: {spec.focus_method}")


def send_once(
    target: str,
    message: str,
    log: Callable[[str], None] = print,
) -> None:
    if target not in TARGETS:
        raise ConfigurationError(
            f"Unknown target: {target}. Choices: {list(TARGETS)}"
        )
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
        raise ConfigurationError(
            f"Message contains characters that typing would silently drop "
            f"or misinterpret: {bad!r}. Use plain single-line ASCII text."
        )
    if spec.message_problem is not None:
        problem = spec.message_problem(message)
        if problem:
            raise ConfigurationError(
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


def _countdown_wait(
    seconds: float, label: str, stop_event: threading.Event,
    emit: Callable[[dict], None],
) -> bool:
    """Wait `seconds` in 1 s ticks, printing a countdown and emitting
    status/countdown events. Returns False as soon as `stop_event` is set."""
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
    repeat every `every_s`. Stops after `count` successful sends (0 =
    infinite) or when `stop_event` is set. Fires `on_event(dict)` for GUI
    hooks.

    Failure policy. A single-shot run (no interval) ends at the first
    failed send with done/error, as it always did. A repeat run (interval
    set, whatever the count) is usually unattended, and most refusals
    are momentary (a draft in the composer, focus stolen while typing, UI
    Automation not answering, the app not running yet): the run logs the
    error, emits a `send_failed` event, waits the interval and tries again,
    and gives up with done/error only after MAX_CONSECUTIVE_FAILURES failed
    sends in a row. Failed sends do not count towards `count`. A
    ConfigurationError (unknown target, untypeable or refused message) can
    never succeed on retry and ends any run at once."""
    stop_event = stop_event or threading.Event()

    def emit(event: dict) -> None:
        if on_event is not None:
            on_event(event)

    def log(text: str) -> None:
        print(text)
        emit({"type": "log", "text": text})

    def wait(seconds: float, label: str) -> bool:
        return _countdown_wait(seconds, label, stop_event, emit)

    repeat_run = every_s is not None and every_s > 0
    sent = 0
    failures = 0  # failed sends in a row; a success resets it
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
            except ConfigurationError as e:
                log(f"[ERROR] {e}")
                emit({"type": "done", "reason": "error"})
                return
            except Exception as e:
                log(f"[ERROR] {e}")
                if not repeat_run:
                    emit({"type": "done", "reason": "error"})
                    return
                failures += 1
                emit({"type": "send_failed", "error": str(e),
                      "consecutive": failures,
                      "limit": MAX_CONSECUTIVE_FAILURES})
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"[ERROR] {failures} sends failed in a row; "
                        f"giving up.")
                    emit({"type": "done", "reason": "error"})
                    return
                log(f"[INFO] Send failed ({failures}/"
                    f"{MAX_CONSECUTIVE_FAILURES} in a row); trying again "
                    f"at the next interval.")
                if not wait(every_s, "Next send"):
                    emit({"type": "done", "reason": "stopped"})
                    return
                print()
                continue
            failures = 0
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
