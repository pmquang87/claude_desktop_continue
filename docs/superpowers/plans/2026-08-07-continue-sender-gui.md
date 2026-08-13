# Continue-Sender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `claude_continue.py` into a shared `sender.py` core, add an Antigravity IDE sibling script, add repeat-with-count mode, and add a Tkinter GUI that drives both.

**Architecture:** `sender.py` holds all Windows-API window handling, focus methods, and the send loop. Two thin CLI wrappers (`claude_continue.py`, `antigravity_continue.py`) parse argparse and call `sender.send_loop(...)`. `gui.py` calls the same `send_loop` in a background thread and communicates state through a `queue.Queue` polled by the Tk main loop.

**Tech Stack:** Python 3, pyautogui (already installed), Windows user32/kernel32 via ctypes, Tkinter (stdlib).

## Global Constraints

- **Platform:** Windows only. No cross-platform code paths.
- **New dependencies:** None. Tkinter is stdlib; pyautogui is already in `requirements.txt`.
- **Message default:** `"continue"` — everywhere the message is optional.
- **Focus methods:** Claude → click `(left + width/2, top + height - 80)`; Antigravity → `pyautogui.hotkey("ctrl", "l")`.
- **Re-find on every iteration:** `send_loop` must call `find_target_windows` before every send, not cache the handle.
- **Settings file:** `settings.json` next to `gui.py`. Missing/corrupt → defaults + a warning line in the log.
- **No git operations in this plan** — this working directory is not a git repo. Skip commit steps.
- **Verification is manual:** each task ends with a smoke-test step, not an automated test, because window activation and keyboard input require a real Windows desktop with the target app running.

---

## File Structure

```
claude_desktop_continue/
├── sender.py                    # NEW — shared core
├── claude_continue.py           # REWRITTEN — thin CLI wrapper
├── antigravity_continue.py      # NEW — thin CLI wrapper
├── gui.py                       # NEW — Tkinter control panel
├── settings.json                # written at runtime by gui.py
├── requirements.txt             # unchanged
└── README.md                    # UPDATED
```

Each file has one responsibility. `sender.py` is the only file that touches Windows API or pyautogui. The two CLI wrappers do nothing but parse args and call `sender.send_loop`. `gui.py` owns all Tk state and threading.

---

## Task 1: Build `sender.py` (core module)

**Files:**
- Create: `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\sender.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `TARGETS: dict[str, TargetSpec]` with keys `"claude"` and `"antigravity"`.
  - `send_once(target: str, message: str, log: Callable[[str], None] = print) -> None`
  - `send_loop(target: str, message: str, initial_delay_s: float, every_s: Optional[float], count: int, stop_event: Optional[threading.Event] = None, on_event: Optional[Callable[[dict], None]] = None) -> None`
  - Event dicts emitted by `send_loop` via `on_event`:
    - `{"type": "status", "text": str}`
    - `{"type": "countdown", "remaining_s": int}`
    - `{"type": "sent", "n": int}`
    - `{"type": "log", "text": str}`
    - `{"type": "done", "reason": "count_reached" | "stopped" | "error"}`

- [ ] **Step 1: Create the file with imports, Win32 constants, and `TargetSpec` + `TARGETS`**

Write to `sender.py`:

```python
"""Shared core for typing a message into a target Windows application.

Used by claude_continue.py, antigravity_continue.py, and gui.py.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import pyautogui

pyautogui.FAILSAFE = False

# Windows API constants
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


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
        focus_method="hotkey_ctrl_l",
        blocklist=(),
    ),
}
```

- [ ] **Step 2: Add process/window helpers**

Append to `sender.py`:

```python
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

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if spec.window_title_contains.lower() in title.lower():
                    exe = _get_process_name(hwnd)
                    matches.append((hwnd, title, exe))
        return True

    user32.EnumWindows(enum_cb, 0)

    preferred = [(h, t) for h, t, exe in matches if exe in spec.exe_names]
    if preferred:
        return preferred

    if spec.blocklist:
        filtered = [
            (h, t) for h, t, _ in matches
            if not any(b in t for b in spec.blocklist)
        ]
        if filtered:
            return filtered

    return [(h, t) for h, t, _ in matches]


def get_window_rect(hwnd) -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
```

- [ ] **Step 3: Add `force_activate_window`**

Append to `sender.py` (this is a direct port of the multi-strategy activation from the old `claude_continue.py`):

```python
def force_activate_window(hwnd) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    user32.AllowSetForegroundWindow(pid.value)

    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)

    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, True)

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.BringWindowToTop(hwnd)
    ok = user32.SetForegroundWindow(hwnd)

    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, False)

    if ok:
        time.sleep(0.5)
        return True

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
    return True
```

- [ ] **Step 4: Add `_focus_input` and `send_once`**

Append to `sender.py`:

```python
def _focus_input(spec: TargetSpec, hwnd) -> None:
    if spec.focus_method == "click_bottom_center":
        left, top, width, height = get_window_rect(hwnd)
        click_x = left + width // 2
        click_y = top + height - 80
        pyautogui.click(click_x, click_y)
    elif spec.focus_method == "hotkey_ctrl_l":
        pyautogui.hotkey("ctrl", "l")
    else:
        raise ValueError(f"Unknown focus_method: {spec.focus_method}")
    time.sleep(0.5)


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
            f"No window containing '{spec.window_title_contains}' found. "
            f"Is {spec.name} running?"
        )

    hwnd, title = windows[0]
    log(f"[INFO] Using window: '{title}' (handle: {hwnd})")

    force_activate_window(hwnd)
    time.sleep(1.0)

    _focus_input(spec, hwnd)

    log(f"[INFO] Typing '{message}'...")
    pyautogui.typewrite(message, interval=0.05)
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.3)

    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Sent '{message}' to {spec.name}.")
```

- [ ] **Step 5: Add `send_loop`**

Append to `sender.py`:

```python
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
        log("[INFO] Cancelled by user.")
        emit({"type": "done", "reason": "stopped"})
```

- [ ] **Step 6: Smoke-test the module import**

Run in a terminal from the project directory:

```
python -c "import sender; print(list(sender.TARGETS)); print(sender.send_once.__doc__ or 'ok')"
```

Expected output: `['claude', 'antigravity']` followed by `ok` (or the send_once docstring).
If it errors (SyntaxError, ImportError, missing pyautogui), fix before continuing.

---

## Task 2: Rewrite `claude_continue.py` as a thin CLI wrapper

**Files:**
- Modify (full rewrite): `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\claude_continue.py`

**Interfaces:**
- Consumes: `sender.send_loop` (from Task 1).
- Produces: `python claude_continue.py [flags]` CLI. Flags: `--hours FLOAT`, `--minutes FLOAT`, `--every-hours FLOAT`, `--every-minutes FLOAT`, `--count INT`, `--message TEXT`.

- [ ] **Step 1: Replace the file with the wrapper**

Overwrite `claude_continue.py` with:

```python
"""Send a message (default 'continue') to Claude Desktop, optionally on repeat.

Usage:
    python claude_continue.py                                # send now
    python claude_continue.py --hours 5                      # wait 5h, send
    python claude_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python claude_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python claude_continue.py --message "keep going"         # custom message
"""

import argparse

import sender


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send a message to Claude Desktop.")
    p.add_argument("--hours", type=float, default=0,
                   help="Initial delay hours (default 0)")
    p.add_argument("--minutes", type=float, default=0,
                   help="Initial delay minutes (default 0)")
    p.add_argument("--every-hours", type=float, default=0,
                   help="Repeat every N hours (0 = no repeat)")
    p.add_argument("--every-minutes", type=float, default=0,
                   help="Repeat every N minutes (0 = no repeat)")
    p.add_argument("--count", type=int, default=0,
                   help="Stop after N sends (0 = forever)")
    p.add_argument("--message", type=str, default="continue",
                   help="Text to send (default 'continue')")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    every_s = args.every_hours * 3600 + args.every_minutes * 60
    sender.send_loop(
        target="claude",
        message=args.message,
        initial_delay_s=args.hours * 3600 + args.minutes * 60,
        every_s=every_s if every_s > 0 else None,
        count=args.count,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test `--help`**

Run:

```
python claude_continue.py --help
```

Expected: usage block listing `--hours`, `--minutes`, `--every-hours`, `--every-minutes`, `--count`, `--message`. Exit code 0.

- [ ] **Step 3: Manual functional smoke test (with Claude Desktop open)**

Open Claude Desktop, then run:

```
python claude_continue.py --message "test"
```

Expected: the word "test" (no trailing space) appears in Claude Desktop's input and is submitted, and a `Sent 'test' to Claude Desktop.` line is printed. If it fails, do NOT proceed to the next task — the wrapper is a pass-through, so any regression is in Task 1's `sender.py`.

---

## Task 3: Create `antigravity_continue.py`

**Files:**
- Create: `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\antigravity_continue.py`

**Interfaces:**
- Consumes: `sender.send_loop` (from Task 1).
- Produces: `python antigravity_continue.py [flags]` CLI with the same flag surface as `claude_continue.py`.

- [ ] **Step 1: Create the file**

Write to `antigravity_continue.py`:

```python
"""Send a message (default 'continue') to Google Antigravity IDE, optionally on repeat.

Uses Ctrl+L to focus the agent chat input (documented shortcut).

Usage:
    python antigravity_continue.py                                # send now
    python antigravity_continue.py --hours 5                      # wait 5h, send
    python antigravity_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python antigravity_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python antigravity_continue.py --message "keep going"         # custom message
"""

import argparse

import sender


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send a message to Google Antigravity IDE.")
    p.add_argument("--hours", type=float, default=0,
                   help="Initial delay hours (default 0)")
    p.add_argument("--minutes", type=float, default=0,
                   help="Initial delay minutes (default 0)")
    p.add_argument("--every-hours", type=float, default=0,
                   help="Repeat every N hours (0 = no repeat)")
    p.add_argument("--every-minutes", type=float, default=0,
                   help="Repeat every N minutes (0 = no repeat)")
    p.add_argument("--count", type=int, default=0,
                   help="Stop after N sends (0 = forever)")
    p.add_argument("--message", type=str, default="continue",
                   help="Text to send (default 'continue')")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    every_s = args.every_hours * 3600 + args.every_minutes * 60
    sender.send_loop(
        target="antigravity",
        message=args.message,
        initial_delay_s=args.hours * 3600 + args.minutes * 60,
        every_s=every_s if every_s > 0 else None,
        count=args.count,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test `--help`**

Run:

```
python antigravity_continue.py --help
```

Expected: usage block with the same flags as the Claude script. Exit code 0.

- [ ] **Step 3: Manual functional smoke test (with Antigravity IDE open)**

Open Antigravity IDE, ensure the agent panel is either closed or not focused (Ctrl+L will focus it), then run:

```
python antigravity_continue.py --message "test"
```

Expected: the agent chat input receives focus, receives the text "test", and the message is submitted. If Ctrl+L behaves unexpectedly (e.g., closes an already-open panel), note this in the log and proceed — this is a known risk in the spec and can be revisited later.

---

## Task 4: Create `gui.py` — Tkinter control panel

**Files:**
- Create: `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\gui.py`

**Interfaces:**
- Consumes: `sender.send_loop`, `sender.TARGETS` (from Task 1).
- Produces: `python gui.py` opens a window that drives `send_loop` in a background thread. Reads/writes `settings.json` next to itself.

- [ ] **Step 1: Create the file with imports, defaults, and settings load/save**

Write to `gui.py`:

```python
"""Tkinter control panel for claude_continue / antigravity_continue.

Drives sender.send_loop in a background thread. Communicates with the Tk main
loop through a queue.Queue polled every 100 ms.
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import sender

SETTINGS_PATH = Path(__file__).with_name("settings.json")

DEFAULTS: dict = {
    "target": "claude",
    "message": "continue",
    "initial_hours": 0.0,
    "initial_minutes": 0.0,
    "repeat_enabled": False,
    "every_hours": 0.0,
    "every_minutes": 0.0,
    "count": 0,
}


def load_settings() -> tuple[dict, str | None]:
    """Return (settings, warning_or_None). Missing/corrupt file → defaults."""
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS), None
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return dict(DEFAULTS), f"Could not read settings.json ({e}); using defaults."
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged, None


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2), encoding="utf-8"
    )
```

- [ ] **Step 2: Add the `ContinueSenderGUI` class skeleton and layout**

Append to `gui.py`:

```python
class ContinueSenderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Continue Sender")
        self.root.geometry("460x560")

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        settings, warning = load_settings()

        # --- Tk variables ---
        self.target_var = tk.StringVar(value=settings["target"])
        self.message_var = tk.StringVar(value=settings["message"])
        self.initial_hours_var = tk.StringVar(value=str(settings["initial_hours"]))
        self.initial_minutes_var = tk.StringVar(value=str(settings["initial_minutes"]))
        self.repeat_var = tk.BooleanVar(value=settings["repeat_enabled"])
        self.every_hours_var = tk.StringVar(value=str(settings["every_hours"]))
        self.every_minutes_var = tk.StringVar(value=str(settings["every_minutes"]))
        self.count_var = tk.StringVar(value=str(settings["count"]))

        self._build_layout()

        if warning:
            self._append_log(warning)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)

    def _build_layout(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Target
        frame_target = ttk.LabelFrame(self.root, text="Target")
        frame_target.pack(fill="x", **pad)
        ttk.Radiobutton(frame_target, text="Claude Desktop",
                        variable=self.target_var, value="claude"
                        ).pack(side="left", padx=8, pady=4)
        ttk.Radiobutton(frame_target, text="Antigravity IDE",
                        variable=self.target_var, value="antigravity"
                        ).pack(side="left", padx=8, pady=4)

        # Message
        frame_msg = ttk.LabelFrame(self.root, text="Message")
        frame_msg.pack(fill="x", **pad)
        ttk.Entry(frame_msg, textvariable=self.message_var).pack(
            fill="x", padx=8, pady=4
        )

        # Initial delay
        frame_delay = ttk.LabelFrame(self.root, text="Initial delay")
        frame_delay.pack(fill="x", **pad)
        ttk.Label(frame_delay, text="Hours:").grid(row=0, column=0, padx=4, pady=4)
        ttk.Entry(frame_delay, textvariable=self.initial_hours_var, width=8).grid(
            row=0, column=1, padx=4, pady=4
        )
        ttk.Label(frame_delay, text="Minutes:").grid(row=0, column=2, padx=4, pady=4)
        ttk.Entry(frame_delay, textvariable=self.initial_minutes_var, width=8).grid(
            row=0, column=3, padx=4, pady=4
        )

        # Repeat
        frame_rep = ttk.LabelFrame(self.root, text="Repeat")
        frame_rep.pack(fill="x", **pad)
        ttk.Checkbutton(frame_rep, text="Enable repeat",
                        variable=self.repeat_var).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=4, pady=4
        )
        ttk.Label(frame_rep, text="Every hours:").grid(row=1, column=0, padx=4, pady=4)
        ttk.Entry(frame_rep, textvariable=self.every_hours_var, width=8).grid(
            row=1, column=1, padx=4, pady=4
        )
        ttk.Label(frame_rep, text="minutes:").grid(row=1, column=2, padx=4, pady=4)
        ttk.Entry(frame_rep, textvariable=self.every_minutes_var, width=8).grid(
            row=1, column=3, padx=4, pady=4
        )
        ttk.Label(frame_rep, text="Count (0 = forever):").grid(
            row=2, column=0, columnspan=2, padx=4, pady=4, sticky="w"
        )
        ttk.Entry(frame_rep, textvariable=self.count_var, width=8).grid(
            row=2, column=2, padx=4, pady=4, sticky="w"
        )

        # Status
        frame_status = ttk.LabelFrame(self.root, text="Status")
        frame_status.pack(fill="x", **pad)
        self.status_label = ttk.Label(frame_status, text="idle")
        self.status_label.pack(anchor="w", padx=8, pady=2)
        self.sent_label = ttk.Label(frame_status, text="Sent: 0")
        self.sent_label.pack(anchor="w", padx=8, pady=2)

        # Buttons
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill="x", **pad)
        self.start_btn = ttk.Button(frame_btn, text="Start", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(frame_btn, text="Stop",
                                   command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        # Log
        frame_log = ttk.LabelFrame(self.root, text="Log")
        frame_log.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(frame_log, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
```

- [ ] **Step 3: Add input parsing, Start/Stop handlers, worker thread**

Append to `gui.py` (inside `ContinueSenderGUI`):

```python
    def _read_settings_from_gui(self) -> dict:
        def to_float(s: str) -> float:
            return float(s) if s.strip() else 0.0

        def to_int(s: str) -> int:
            return int(s) if s.strip() else 0

        return {
            "target": self.target_var.get(),
            "message": self.message_var.get(),
            "initial_hours": to_float(self.initial_hours_var.get()),
            "initial_minutes": to_float(self.initial_minutes_var.get()),
            "repeat_enabled": bool(self.repeat_var.get()),
            "every_hours": to_float(self.every_hours_var.get()),
            "every_minutes": to_float(self.every_minutes_var.get()),
            "count": to_int(self.count_var.get()),
        }

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for child in self.root.winfo_children():
            for sub in child.winfo_children():
                if isinstance(sub, (ttk.Entry, ttk.Radiobutton, ttk.Checkbutton)):
                    sub.configure(state=state)
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if not enabled else "disabled")

    def _on_start(self) -> None:
        try:
            settings = self._read_settings_from_gui()
        except ValueError as e:
            messagebox.showerror("Invalid input",
                                 f"Could not parse a number: {e}")
            return

        if settings["target"] not in sender.TARGETS:
            messagebox.showerror("Invalid target",
                                 f"Unknown target: {settings['target']}")
            return
        if not settings["message"]:
            messagebox.showerror("Invalid input", "Message is empty.")
            return

        save_settings(settings)

        initial_s = (
            settings["initial_hours"] * 3600 + settings["initial_minutes"] * 60
        )
        every_s: float | None
        if settings["repeat_enabled"]:
            every_s = (
                settings["every_hours"] * 3600 + settings["every_minutes"] * 60
            )
            if every_s <= 0:
                messagebox.showerror(
                    "Invalid input",
                    "Repeat is enabled but interval is 0. Set hours/minutes.",
                )
                return
        else:
            every_s = None

        self.stop_event.clear()
        self.status_label.configure(text="starting…")
        self.sent_label.configure(text="Sent: 0")
        self._set_inputs_enabled(False)

        def run() -> None:
            sender.send_loop(
                target=settings["target"],
                message=settings["message"],
                initial_delay_s=initial_s,
                every_s=every_s,
                count=settings["count"],
                stop_event=self.stop_event,
                on_event=self._push_event,
            )

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        self.stop_event.set()
        self._append_log("Stop requested…")
        self.stop_btn.configure(state="disabled")

    def _push_event(self, event: dict) -> None:
        """Called from the worker thread; hand off via queue."""
        self.events.put(event)

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle_event(self, event: dict) -> None:
        t = event.get("type")
        if t == "status":
            self.status_label.configure(text=event.get("text", ""))
        elif t == "countdown":
            secs = event.get("remaining_s", 0)
            h, rem = divmod(int(secs), 3600)
            m, s = divmod(rem, 60)
            self.status_label.configure(
                text=f"next send in {h:02d}:{m:02d}:{s:02d}"
            )
        elif t == "sent":
            n = event.get("n", 0)
            self.sent_label.configure(text=f"Sent: {n}")
            self._append_log(f"Sent #{n}")
        elif t == "log":
            self._append_log(event.get("text", ""))
        elif t == "done":
            reason = event.get("reason", "?")
            self._append_log(f"Done: {reason}")
            self.status_label.configure(text=f"idle ({reason})")
            self._set_inputs_enabled(True)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                "Quit?",
                "A send loop is running. Stop and quit?",
            ):
                return
            self.stop_event.set()
            self.worker.join(timeout=2.0)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ContinueSenderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Smoke-test import + launch**

Run:

```
python gui.py
```

Expected: a window opens with target radios, message field, initial-delay entries, repeat frame with checkbox and entries, status labels, Start/Stop buttons, log pane. Closing the window should exit cleanly. Do NOT press Start yet.

- [ ] **Step 5: Smoke-test settings persistence**

With the window open:
1. Change target to "Antigravity IDE", change message to "hello", enter `1` in initial minutes, enable repeat, enter `2` in every-minutes, enter `3` in count.
2. Click Start (with Antigravity closed is fine — it will emit an error event; that's what we want to test persistence). Immediately click Stop.
3. Close the window. Reopen: `python gui.py`.
4. Verify all fields still show the values set in step 1. Verify `settings.json` exists next to `gui.py` and contains those values.

Expected: values are preserved. If they aren't, check the Start handler saves settings before starting the worker.

- [ ] **Step 6: Manual end-to-end smoke test**

Open Claude Desktop. In the GUI:
1. Target: Claude Desktop. Message: "gui test". Initial delay: 0. Repeat: on. Every: 0h 1m. Count: 2.
2. Click Start. Wait ~90 s.

Expected:
- Log shows "Sent #1" within a second or two of Start.
- Status label counts down `next send in 00:01:00` to 0.
- Log shows "Sent #2" ~1 minute after #1.
- Log shows "Done: count_reached".
- Inputs re-enable.
- Claude Desktop received "gui test" twice.

If the second send never fires, check `_wake_screen` runs and `find_target_windows` re-runs each iteration.

---

## Task 5: Update `README.md`

**Files:**
- Modify: `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\README.md`

**Interfaces:**
- Consumes: everything above (documenting it).
- Produces: an up-to-date README.

- [ ] **Step 1: Read the current README to preserve its style**

Read `C:\Users\pmqua\PycharmProjects\claude_desktop_continue\README.md`. Note its heading style, tone, and section order.

- [ ] **Step 2: Rewrite the README**

Replace the file's content with (adapt to preserve any existing sections like "Requirements" verbatim if their content still applies):

```markdown
# Continue Sender

Sends a text message (default `continue`) to **Claude Desktop** or **Google
Antigravity IDE** — once, after a delay, or on a repeating schedule. Useful
when a rate-limit reset unblocks a paused conversation.

## Requirements

- Windows 10 / 11
- Python 3.10+
- `pip install -r requirements.txt` (currently just `pyautogui`)

Tkinter is used by the GUI and ships with Python on Windows.

## The three entry points

### GUI (recommended)

```
python gui.py
```

Pick target, edit message, set initial delay, optionally enable repeat with
an interval and a count. Start / Stop. Settings persist to `settings.json`
next to `gui.py`.

### Claude Desktop CLI

```
python claude_continue.py                                # send now
python claude_continue.py --hours 5                      # wait 5h, send
python claude_continue.py --hours 5 --every-hours 5      # then repeat every 5h
python claude_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
python claude_continue.py --message "keep going"
```

### Antigravity IDE CLI

```
python antigravity_continue.py [same flags as above]
```

## Flags (both CLIs)

| Flag | Default | Meaning |
|---|---|---|
| `--hours` | 0 | Initial delay hours |
| `--minutes` | 0 | Initial delay minutes |
| `--every-hours` | 0 | Repeat interval hours (0 = no repeat) |
| `--every-minutes` | 0 | Repeat interval minutes (0 = no repeat) |
| `--count` | 0 | Stop after N sends (0 = forever) |
| `--message` | `continue` | Text to send |

Ctrl+C cancels a running CLI cleanly.

## How it works

- `sender.py` finds the target window (Win32 `EnumWindows`), force-activates
  it, focuses the input (click for Claude, `Ctrl+L` for Antigravity per its
  documented shortcut), types the message, presses Enter.
- Between repeats, the window is re-found from scratch — so closing and
  reopening the target app during a long run doesn't break the loop.
- Before every send, a tiny mouse movement wakes the display if it's locked.

## Files

- `sender.py` — shared core (window finding, activation, focus, send loop)
- `claude_continue.py` — CLI wrapper for Claude Desktop
- `antigravity_continue.py` — CLI wrapper for Antigravity IDE
- `gui.py` — Tkinter control panel
- `settings.json` — created by the GUI; last-used values
```

- [ ] **Step 3: Verify the README renders sensibly**

Open `README.md` in a Markdown previewer or read it top-to-bottom. Verify the flag table renders and the code blocks show. No literal `TODO`/`TBD` should remain.

---

## Self-Review Notes

- **Spec coverage:** Antigravity script (Task 3), `--count` (Tasks 2/3 flag `--count`), GUI (Task 4), settings.json (Task 4 steps 1 and 5), Ctrl+L (Task 1 step 4, `_focus_input`), re-find every iteration (Task 1 step 5, `send_loop` calls `send_once` which calls `find_target_windows` inside), mouse jiggle every iteration (Task 1 step 5, `_wake_screen` inside the loop), README (Task 5).
- **Type consistency:** `send_loop` signature identical everywhere it's called; event dict keys match between emitter (Task 1 step 5) and handler (Task 4 step 3).
- **No placeholders:** every code block is complete; no "similar to Task N"; no TBDs.
