# Continue Sender

Sends a text message (default `continue`) to **Claude Desktop**, the **Google
Antigravity IDE**, the **OpenAI Codex desktop app** or the **ZCode desktop
app** — once, after a delay, or on a repeating schedule. Useful when a
rate-limit reset unblocks a paused conversation.

## Requirements

- Windows 10 / 11
- Python 3.10+
- `pip install -r requirements.txt` (`pyautogui` + `comtypes`; the latter is
  used to verify via UI Automation that the target input really has keyboard
  focus before anything is typed)

Tkinter is used by the GUI and ships with Python on Windows.

## The entry points

### GUI (recommended)

```
python gui.py
```

Pick target, edit message, set initial delay, optionally enable repeat with
an interval and a count. Start / Stop. The status pane counts the sent and
the failed sends of the current run. Settings persist to `settings.json`
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

### Codex desktop app CLI

```
python codex_continue.py [same flags as above]
```

### ZCode desktop app CLI

```
python zcode_continue.py [same flags as above]
```

Typing into ZCode while a task is running queues a follow-up ("Keep typing to
queue follow-up changes") — which is exactly the point of a scheduled
`continue`.

## Flags (all CLIs)

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
  it, focuses the input, types the message, presses Enter.
- Focus methods: Claude Desktop gets a click near the bottom of the window.
  Antigravity's standalone **Agent Manager** window gets `Ctrl+I` (its
  `focusInput` shortcut — `Ctrl+L` is dead on that surface), verified via UI
  Automation, with clicks into the input box as fallback; the **IDE editor**
  window gets `Ctrl+1` then `Ctrl+L` (`Ctrl+L` alone *closes* the agent panel
  when it is already open and focused, so focus is parked in the editor
  first). If the input still doesn't hold focus, the send is aborted with an
  error instead of typing into the wrong place — and after typing, focus is
  re-checked before Enter is pressed.
- The **Codex desktop app** (MSIX package `OpenAI.Codex`; its process is
  `ChatGPT.exe`, matched by exe name plus package path because its window
  title `ChatGPT` also fits browser tabs and Explorer folders, and a legacy
  stand-alone ChatGPT install is the same exe) has no safe focus shortcut:
  the app ships none for the composer, `Shift+Esc` clears unreads and plain
  `Esc` *stops a running turn*. The composer is therefore located through UI
  Automation (the bottom-most `ProseMirror` edit element), focused with UIA
  `SetFocus` (polled, since Chromium applies it asynchronously), and clicked
  as a fallback. The send is aborted, without typing, if UI Automation is
  unavailable, the composer cannot be found or focused, or it already holds
  a draft. After typing, the window must still be foreground, the composer
  element must still hold focus, and the text read back from the composer
  must contain the message before Enter is pressed. Messages starting with
  `/` or containing `@` are refused for this target: they open the
  slash-command menu or the mention list, and Enter would pick a menu entry.
  `python debug_windows.py codex` shows what UIA sees without sending
  anything.
- The **ZCode desktop app** (Zhipu's GLM coding agent; an Electron app at
  `C:\Program Files\ZCode\ZCode.exe`, process `zcode.exe`) is matched by exe
  name only — its window title is plain `ZCode`, which an Explorer folder or
  a browser tab would carry just as well while the app is closed. Only
  visible, unowned windows of at least 200x200 are considered, which today
  leaves exactly the main window: the same process also owns an invisible
  small pop-up, several 0x0 helpers and a hidden helper window that is
  *larger* than the main window, and it is the visibility filter, not
  "largest wins", that keeps the sender off them. It has no focus shortcut
  either: the whole Electron menu is `Ctrl+N`, `Ctrl+O`, `Ctrl+W` and the
  zoom accelerators, and **`Ctrl+W` would close the window**, so the sender
  presses no shortcut for this target — the only key events before the
  message are the modifier releases and the single `Alt` tap that
  `force_activate_window` uses to let Windows change the foreground window.
  The composer is a Lexical editor found as the bottom-most `Edit` element in
  the window (in practice the only one) — deliberately *not* by class or
  name: the class is a run of Tailwind utility classes, and the name is the
  placeholder, which changes with the app's state (`Ask ZCode anything...`,
  `Ask for follow-up changes`, `Initializing task...`). Unlike Codex, an
  empty ZCode composer reports `"\n"` through the Value pattern rather than
  its placeholder; both count as "empty" for the draft check, and the
  read-back after typing must show the message *and* differ from what the
  box held before. Messages starting with `/` or containing `@` are refused
  here too — the placeholder itself advertises both menus ("@ to add context,
  / for commands or capabilities"). `python debug_windows.py zcode` prints
  the window, the composer element and its current value, read-only.
- Messages must be plain single-line ASCII: `pyautogui.typewrite` silently
  drops characters it cannot map (umlauts, Vietnamese diacritics, emoji), so
  such messages are rejected up front instead of "sending" incomplete text.
- Windows of the target exe are preferred; title-only matches are filtered
  through a blocklist (never targeted, even if nothing else matches), and
  tiny utility/overlay windows are skipped so the focus click cannot land
  outside the window.
- After activation the foreground window is read back and verified. If the
  target could not actually be focused (e.g. the workstation is locked), the
  send is aborted with an error instead of typing into whatever has focus.
- Between repeats, the window is re-found from scratch — so closing and
  reopening the target app during a long run doesn't break the loop.
- A **repeat run** (any run with an interval, whatever the count) is usually
  unattended,
  and most refusals are momentary: a draft sitting in the composer, focus
  stolen while typing, UI Automation not answering, the app not running
  yet. Such a run therefore logs the error, reports it to the GUI (`Failed`
  counter, status line), waits the interval and tries again; failed sends
  do not count towards the count. It gives up with an error only after 3
  failed sends in a row (`MAX_CONSECUTIVE_FAILURES` in `sender.py`) — or at
  once when retrying cannot help: an unknown target, a message that cannot
  be typed, or one the target refuses (the Codex and ZCode `/` and `@`
  rules). A **single send** (no interval) still stops at the first error.
- Before every send, a tiny mouse movement wakes the display if it's asleep.
  (A *locked* session cannot be typed into — a single send stops with an
  error; a repeat run retries at the next interval, see above.)

## Files

- `sender.py` — shared core (window finding, activation, focus, send loop)
- `cli.py` — shared argparse surface of the CLI wrappers
- `claude_continue.py` — CLI wrapper for Claude Desktop
- `antigravity_continue.py` — CLI wrapper for Antigravity IDE
- `codex_continue.py` — CLI wrapper for the Codex desktop app
- `zcode_continue.py` — CLI wrapper for the ZCode desktop app
- `debug_windows.py [target]` — read-only listing of the candidate windows
  (and, for the UIA-composer targets `codex` and `zcode`, the composer
  element UI Automation finds plus its current value)
- `gui.py` — Tkinter control panel
- `settings.json` — created by the GUI; last-used values
- `tests/` — regression tests (`python -m unittest discover -s tests`);
  fully mocked, they never move the mouse or type
