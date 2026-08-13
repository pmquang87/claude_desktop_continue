# Continue Sender

Sends a text message (default `continue`) to **Claude Desktop** or **Google
Antigravity IDE** — once, after a delay, or on a repeating schedule. Useful
when a rate-limit reset unblocks a paused conversation.

## Requirements

- Windows 10 / 11
- Python 3.10+
- `pip install -r requirements.txt` (`pyautogui` + `comtypes`; the latter is
  used to verify via UI Automation that the target input really has keyboard
  focus before anything is typed)

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
- Before every send, a tiny mouse movement wakes the display if it's asleep.
  (A *locked* session cannot be typed into — the run stops with an error.)

## Files

- `sender.py` — shared core (window finding, activation, focus, send loop)
- `claude_continue.py` — CLI wrapper for Claude Desktop
- `antigravity_continue.py` — CLI wrapper for Antigravity IDE
- `gui.py` — Tkinter control panel
- `settings.json` — created by the GUI; last-used values
- `tests/` — regression tests (`python -m unittest discover -s tests`);
  fully mocked, they never move the mouse or type
