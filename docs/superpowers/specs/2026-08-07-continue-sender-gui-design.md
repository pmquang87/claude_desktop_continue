# Continue-Sender: Antigravity support, repeat mode, GUI — Design

Date: 2026-08-07

## Goal

Extend the existing `claude_continue.py` (which types "continue" into Claude
Desktop after an optional delay) with:

1. A sibling script targeting the **Google Antigravity IDE**.
2. **Repeat-every-period** mode with a **`--count N`** cap.
3. A small **Tkinter GUI** that drives both scripts from one window.

Both CLI scripts remain usable standalone; the GUI is an optional front-end.

## File layout

```
claude_desktop_continue/
├── sender.py                    # shared core (new)
├── claude_continue.py           # thin CLI wrapper (rewritten)
├── antigravity_continue.py      # thin CLI wrapper (new)
├── gui.py                       # Tkinter control panel (new)
├── settings.json                # GUI state, written at runtime (new)
├── requirements.txt             # unchanged (pyautogui only; Tkinter is stdlib)
└── README.md                    # updated
```

## Module: `sender.py`

Owns everything that isn't argparse or Tk. Public surface:

```python
TARGETS = {
    "claude": TargetSpec(
        window_title_contains="Claude",
        exe_name="claude.exe",
        focus_method="click_bottom_center",   # click y = bottom - 80
        blocklist=[" – ", " - Visual Studio", ".py", ".js", ".ts"],
    ),
    "antigravity": TargetSpec(
        window_title_contains="Antigravity",
        exe_names=("antigravity ide.exe", "antigravity.exe"),
        focus_method="hotkey_ctrl_l",         # press Ctrl+L to focus agent input
        blocklist=[],
    ),
}

def send_once(target: str, message: str) -> None: ...

def send_loop(
    target: str,
    message: str,
    initial_delay_s: float,
    every_s: float | None,
    count: int,                   # 0 = infinite
    stop_event: threading.Event | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> None: ...
```

### Behavior

- `send_loop` re-finds the target window **every iteration** (per user
  preference — survives close/reopen of the target app during a long run).
- Countdown loop sleeps in 1 s ticks and checks `stop_event` each tick, so the
  GUI Stop button reacts within ≤ 1 s.
- `on_event(dict)` is the GUI hook. Emitted events:
  - `{"type": "status", "text": "..."}` — high-level state
  - `{"type": "countdown", "remaining_s": int}` — every second
  - `{"type": "sent", "n": int}` — after each successful send
  - `{"type": "log", "text": "..."}` — for the log pane
  - `{"type": "done", "reason": "count_reached" | "stopped" | "error"}`
- CLI wrappers pass `on_event=None`; `sender.py` also `print()`s the same
  messages so CLI output is unchanged from today.

### Focus methods

- **`click_bottom_center`** — unchanged from today's `claude_continue.py`
  (click at `(left + width/2, top + height - 80)`).
- **`hotkey_ctrl_l`** — after activating the window, press `Ctrl+L`.
  Antigravity's changelog documents this as the agent-input focus shortcut.
  If the agent panel happens to be already open, Ctrl+L is a toggle risk —
  accepted trade-off; a click-based fallback can be added later if it bites.

## CLI wrappers (both scripts)

Identical arg surface:

```
--hours FLOAT           initial delay hours (default 0)
--minutes FLOAT         initial delay minutes (default 0)
--every-hours FLOAT     repeat interval hours (default 0 = no repeat)
--every-minutes FLOAT   repeat interval minutes (default 0 = no repeat)
--count INT             stop after N sends; 0 = infinite (default 0)
--message TEXT          override default "continue"
```

Semantics:

- No delay, no repeat: single send now (matches today's behavior).
- Only initial delay: single send after delay (matches today's behavior).
- `--every-*` set: after first send, wait `every_s` and send again, up to
  `--count` total sends or forever if count is 0. Ctrl+C exits cleanly.

Each wrapper is just:

```python
def main():
    args = parse_args()
    sender.send_loop(
        target="claude",       # or "antigravity"
        message=args.message,
        initial_delay_s=args.hours*3600 + args.minutes*60,
        every_s=(args.every_hours*3600 + args.every_minutes*60) or None,
        count=args.count,
    )
```

## Module: `gui.py`

Tkinter, single window, ≈ 400 × 500 px. Layout:

```
Target:        ( • ) Claude Desktop     ( ) Antigravity IDE
Message:       [ continue                              ]
Initial delay: Hours [ 0 ]  Minutes [ 0 ]
[x] Repeat
    Every      Hours [ 0 ]  Minutes [ 0 ]
    Count      [ 0 = forever ]
────────────────────────────────────────
Status:  waiting — next in 04:59:12
Sent:    3 / ∞
[ Start ]  [ Stop ]
────────────────────────────────────────
Log:
┌─────────────────────────────────────┐
│ 14:03:12  Sent to Claude Desktop    │
│ ...                                 │
└─────────────────────────────────────┘
```

### Threading

- **Main thread**: Tk event loop only. Never blocks.
- **Worker thread**: runs `sender.send_loop(..., stop_event, on_event)`.
- **Cross-thread comms**: `on_event` pushes dicts into a `queue.Queue`.
  Main loop polls the queue every 100 ms via `root.after(100, drain_queue)`.
  This is the standard Tk-with-threads pattern; no widget is touched off the
  main thread.

### Buttons

- **Start** — validate inputs, disable all input widgets, spawn worker thread.
  Grays out itself; enables Stop.
- **Stop** — sets `stop_event`. Worker exits at the next tick (≤ 1 s).
  Re-enables input widgets and Start.
- **Window close while running** — messagebox: *"A send loop is running. Stop
  and quit?"* → Yes stops the worker and quits, No cancels the close.

### Persistence — `settings.json`

Written next to `gui.py`. Loaded on launch, saved on every successful Start
(so a crash still preserves the last good config).

```json
{
  "target": "claude",
  "message": "continue",
  "initial_hours": 0,
  "initial_minutes": 0,
  "repeat_enabled": true,
  "every_hours": 5,
  "every_minutes": 0,
  "count": 0
}
```

Missing keys / missing file → defaults, no error. Corrupt JSON → log a
warning to the log pane and fall back to defaults.

## Non-goals

- No packaging (no PyInstaller, no shortcuts). Run via `python gui.py`.
- No system tray / minimize-to-tray. Plain window.
- No message templates or per-target message history. Single `message` field.
- No scheduling beyond "delay + interval + count". No cron-like windows.
- No macOS/Linux support — Windows-only, matches the existing script.

## Risks

- **Ctrl+L is a toggle in Antigravity** — if the agent panel is already open
  and focused, the shortcut may close it. Accepted; if it becomes a real
  problem, add a click-in-panel fallback.
- **Long unattended runs** — Windows may lock the screen; the existing
  mouse-jiggle wake-up in `wait_and_send` is preserved and runs before every
  send in the repeat loop, not just the first.
- **Foreground-window steal restrictions** — the existing multi-strategy
  `force_activate_window` is preserved verbatim; no known regressions.
