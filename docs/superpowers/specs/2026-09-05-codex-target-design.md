# Continue-Sender: Codex desktop app target - Design

Date: 2026-09-05

## Goal

Add a third target, the **OpenAI Codex desktop app**, to the Continue Sender
(`sender.py`, `gui.py`, CLI wrappers), so a scheduled "continue" can be typed
into a Codex thread after a usage-limit reset - exactly like the existing
Claude Desktop and Antigravity targets.

## Assumptions (made autonomously, verified on this machine)

- "Codex" means the **Codex desktop app**, not the Codex CLI in a terminal
  and not the VS Code extension. Evidence: the app is installed and running
  here (MSIX package `OpenAI.Codex_26.901.4073.0_x64__2p2nqsd0c76g0`), the
  `~/.codex/config.toml` has a `[desktop]` section, no `codex` CLI is on
  `PATH`, and no VS Code extension is installed.
- The app's process image is **`ChatGPT.exe`** (path
  `C:\Program Files\WindowsApps\OpenAI.Codex_...\app\ChatGPT.exe`); the main
  window is class `Chrome_WidgetWin_1` (Electron) and is currently titled
  `ChatGPT`. A `Codex.exe` launcher stub sits next to it.
- The composer (chat input) is exposed to UI Automation as
  `ControlType = Edit (50004)`, `ClassName = "ProseMirror"`, placeholder
  name `Do anything`, anchored just above a button row (Add files /
  Change permissions / model selector / Dictate / Start voice chat) at the
  window bottom. Measured on a 96-DPI 1024x1088 window: edit rect
  `y = bottom-143 ... bottom-79`, button row `y = bottom-74 ... bottom-32`.

## Key finding: no safe keyboard shortcut

The readable app bundle (`resources/app.asar`) shows:

- `Shift+Escape` is the default keybinding of the command `clearAllUnreads`,
  not "focus composer".
- Plain `Escape` is handled by a renderer function that returns one of
  `stop-turn`, `confirm-stop-turn`, `close-template-picker`,
  `stop-realtime-session`, `abort-dictation`, or `focus-composer` depending
  on state. **While a response is in progress, Escape stops the turn.**
  Unusable for an unattended sender.
- `focus-composer` is otherwise only sent by the main process when the
  window is opened through the app's global hotkey.

Therefore the Codex target does **not** use a keyboard shortcut. It locates
the composer through UI Automation and verifies focus before typing.

## Design

### `sender.py`

New target:

```python
"codex": TargetSpec(
    name="Codex",
    window_title_contains="",            # "" = exe-only matching
    exe_names=("chatgpt.exe", "codex.exe"),
    focus_method="uia_composer",
    blocklist=(),
    prefer_largest_window=True,          # pop-ups share title and process
    exe_path_contains=("openai.codex",), # the MSIX package, not a legacy ChatGPT
    message_problem=codex_message_problem,
),
```

- **Exe-only matching, pinned to the package.** `_matches_spec` treats an
  empty `window_title_contains` as "do not title-match". The title
  `ChatGPT` would otherwise also match browser tabs, Explorer folders, or
  editor buffers when the app is not running; better to report "not
  running". An exe match must additionally have `openai.codex` in its
  lower-cased image path: a legacy stand-alone ChatGPT desktop install is
  also `ChatGPT.exe` with the same Chromium/ProseMirror UI and would pass
  every later check. `codex.exe` is included so a future rename of the
  launcher keeps working; the bundled backend `codex.exe` lives outside the
  package and has no visible top-level windows, so it cannot be mis-targeted.
- **Largest window wins.** The app's quick-chat and hotkey pop-ups and the
  avatar overlay are windows of the same process, all titled `ChatGPT`.
  `_pick_main_window(windows, prefer_largest=True)` takes the largest
  visible candidate instead of the first in Z-order. `send_once` logs every
  candidate when there is more than one, and the image path of the pick.
- **Message validation.** `codex_message_problem` refuses a message whose
  first non-space character is `/` (slash-command menu) or that contains
  `@` (mention list): Enter would select a menu entry instead of sending.
  Checked by `gui.validate_settings` up front and by `send_once`.
- **Focus method `uia_composer`** (`_focus_codex_composer(hwnd, log)`):
  1. Obtain the UIA client (`_uia()` -> `(IUIAutomation, module)` or `None`).
     `None` -> `RuntimeError`, nothing is typed: this target's whole safety
     story is UIA-based, so there is no unverified path worth taking.
     `_uia()` tolerates `RPC_E_CHANGED_MODE` (a thread some library already
     put into the multi-threaded apartment is still usable).
  2. `_find_codex_composer(uia, mod, hwnd, log)`: `ElementFromHandle(hwnd)`,
     then `FindAll(TreeScope_Descendants, AND(ControlType == Edit,
     ClassName == "ProseMirror"))`. Chromium builds its accessibility tree
     lazily on first UIA contact, so retry up to 4 times with 1 s pauses
     while the result is empty **or the query raises** (a provider that is
     not ready raises rather than returning nothing). Among candidates keep
     those with a non-empty bounding rectangle and pick the **bottom-most**
     (the composer sits at the window bottom; any other ProseMirror editor,
     e.g. in a dialog, would be higher).
  3. `element.SetFocus()`, then `_wait_for_focus` polls
     `_is_element_focused(uia, element)` up to 5 times, 0.15 s apart
     (Chromium applies the focus request asynchronously). Focused means
     **both**: the system-wide focused element is this one
     (`CompareElements`, verified stable across queries here; as a guard for
     a future version, a focused `Edit` + `ProseMirror` at exactly the same
     bounding rectangle counts too) **and** the element's own
     `HasKeyboardFocus` flag is set.
  4. If not focused: click the centre of the element's bounding rectangle
     (`pyautogui.click`) and poll again.
  5. Still not focused -> `RuntimeError`, nothing is typed.
  6. Composer not found -> `RuntimeError`, nothing is typed.
  7. **Draft check.** `_composer_draft` reads the Value pattern; the empty
     composer reports its placeholder, which equals the element's accessible
     name. Any other text -> `RuntimeError` (it would be submitted together
     with the message). Unreadable -> `[WARN]`, continue.
  8. Returns a `ComposerHandle(uia, mod, element)`.
- **Pre-Enter guards** in `send_once`, for the returned handle: the window
  must still be foreground (`_is_foreground`), the composer element must
  still hold focus (`_is_element_focused` on the same element, no second COM
  bootstrap), and `_verify_typed_text` must read the message back from the
  Value pattern (polled 3 times, 0.2 s apart): the text must contain the
  message **and differ from the baseline read just before typing** (the
  empty box shows its placeholder, and a short message such as `hi` is a
  substring of `Do anything`). Any readable mismatch aborts, whichever poll
  produced it; only a run in which no poll could be read is a `[WARN]`. A
  NULL Value (comtypes returns `None`) counts as unreadable, never as text.
  The generic control-type re-check for
  `_VERIFIED_FOCUS_METHODS = {"agent_input", "uia_composer"}` runs as well.
- `sender.py` imports comtypes on the main thread at import time, because
  comtypes initialises COM for the importing thread; a first import on a
  worker thread already in the multi-threaded apartment would raise before
  `_uia()`'s tolerance applies.
- `_focused_control_type()` is rebuilt on top of `_uia()` so both paths share
  one COM bootstrap; behaviour (`None` = cannot verify) is unchanged.
- Coordinates: `pyautogui` calls `SetProcessDPIAware()` at import (verified:
  `IsProcessDPIAware()` flips to 1 on `import sender`), so UIA bounding
  rectangles (physical pixels) and `pyautogui.click` agree.

### Send semantics in the Codex app

Enter submits the composer; the message is typed as plain ASCII exactly as
for the other targets. If a turn is running, the app applies its
"follow-up queue mode" (this machine: `steer`); if the usage limit is
exhausted, the app shows its banner and keeps the composer enabled - the
sender does not try to detect either state (same as the other targets).

### `gui.py`

A third radio button **Codex** (value `codex`) in the Target row, plus the
`spec.message_problem` check in `validate_settings` (see *Message
validation* above). Nothing else: `validate_settings` already checks the
target against `sender.TARGETS`, and `_set_inputs_enabled` handles radio
buttons generically.

### CLI

- New `codex_continue.py`, same flags as the two existing wrappers.
- Those wrappers each duplicate ~22 lines of argparse plus the `send_loop`
  call. All three are collapsed onto a shared `cli.py`
  (`build_parser(description)`, `run(target, args)`); each wrapper keeps its
  docstring, `parse_args()` and `main()` so the documented commands are
  unchanged.

### `debug_windows.py`

Takes an optional target key argument (default `antigravity`, so the bare
command keeps working). The listing gains the target name in the header,
the image path per window and a `Would use:` line naming the window
`_pick_main_window` would pick - all read-only. For `codex` it additionally
prints the composer element found by `_find_codex_composer`, so a layout
change can be diagnosed without sending anything.

### README

Codex added to the intro, entry points, "How it works" (UIA composer lookup,
why Escape is not used, `ChatGPT.exe` identity) and the Files list.

## Error handling

Every failure path raises `RuntimeError` before anything is typed, and
`send_loop` already turns that into an `[ERROR]` log line plus a
`done/error` event. New messages:

- `No window matching Codex found. Is it running?` (existing path)
- `Message starts with '/' (opens the Codex slash-command menu; ...). Not
  typing it into Codex.` / `Message contains '@' (...)`
- `UI Automation is unavailable (comtypes missing or COM failure); refusing
  to type into the Codex composer unverified.`
- `Could not find the Codex composer (no ProseMirror edit element in the
  window after N attempts). Not typing.`
- `Could not focus the Codex composer (SetFocus and a click both left focus
  elsewhere). Not typing.`
- `The Codex composer already contains text ('...'); not typing on top of a
  draft.`
- `Focus left the input while typing (control type N); not pressing Enter.`
  (existing guard, reworded from "the agent input", now active for Codex)
- `The Codex window lost the foreground while typing; not pressing Enter.`
- `The Codex composer lost focus while typing; not pressing Enter.`
- `Composer text after typing is '...', which does not contain '...'; not
  pressing Enter.`

## Testing

All tests stay fully mocked (no mouse, no keyboard, no windows). New cases in
`tests/test_bugs.py`:

- `TARGETS["codex"]` shape; `_matches_spec` (the predicate used by the
  `find_target_windows` enum filter) ignores titles when
  `window_title_contains` is empty and requires the package path for exe
  matches when `exe_path_contains` is set.
- `_pick_main_window(..., prefer_largest=True)` picks the largest window;
  `send_once` passes the flag for Codex.
- `codex_message_problem` and its use by `gui.validate_settings` and
  `send_once` (refused before activation).
- `_uia()`: tolerates `RPC_E_CHANGED_MODE`, returns `None` for other COM
  failures and for a missing comtypes.
- `_find_codex_composer`: picks the bottom-most ProseMirror edit (also when
  it is not last in tree order); skips empty rectangles and elements that
  vanish mid-scan; retries while the array is empty or the query raises;
  returns `None` after the attempt budget; the AND condition is what
  reaches `FindAll`.
- `_is_element_focused` / `_wait_for_focus`: both signals required; same-rect
  fallback; polling.
- `_composer_text`, `_composer_draft`, `_verify_typed_text`.
- `_focus_codex_composer`: SetFocus alone when verified (no click); click
  fallback at the rect centre when SetFocus does not take; raises when both
  fail; raises when no composer; raises without clicking when UIA is
  unavailable; raises on a draft; warns when the draft cannot be read.
- `send_once("codex", ...)`: no Enter when the control type, the foreground
  window, the composer element's focus, or the read-back text is wrong.
- `send_once("antigravity", ...)`: Enter still pressed when UIA cannot
  verify (`None` contract).
- `cli.py`: each wrapper's `main()` calls `send_loop` with its target and the
  computed delays.
- `gui.validate_settings` accepts `target = "codex"`; every radio button
  drives the target variable.

Live verification on this machine is limited to **read-only** probes
(window found by exe name; composer element found with the exact FindAll
query). A real send is left to the user, because it would post into their
current Codex thread.

## Non-goals

- Codex CLI (terminal) and the VS Code extension.
- Detecting or dismissing the usage-limit banner (the app shows it and
  fails the submission itself; the composer stays focusable).
- Appending to an existing draft (refused instead, see the draft check).
- Choosing which thread/project receives the message - the app's current
  view is used, as for the other targets.
- macOS/Linux.

## Risks

- **App update changes the composer class.** `ProseMirror` is the editor
  library's root class, stable across the ChatGPT/Codex UI for years; if it
  changes, the sender fails closed with a clear error and
  `debug_windows.py codex` shows what UIA sees.
- **Several top-level windows of ChatGPT.exe.** `_pick_main_window` is
  called with `prefer_largest=True` for this target and takes the largest
  plausibly sized visible window; the composer lookup then fails closed if
  that window has no composer.
- **A refused send ends the loop.** `send_loop` treats every `send_once`
  error as terminal (`done/error`), for all targets — a pre-existing design
  choice. The Codex path has more refusal conditions (draft in the box,
  focus lost, read-back mismatch), so an unattended repeat run stops at the
  first one instead of trying again at the next interval. Changing that is
  a separate decision for all targets. *Decided the same day, see
  `2026-09-05-send-loop-retry-design.md`: repeat runs now retry at the next
  interval and give up after three failed sends in a row; configuration
  errors and single sends still stop at once.*
- **Exe rename or a non-MSIX install.** Covered by `codex.exe` in
  `exe_names`; a rename to something else, or an install outside a path
  containing `openai.codex`, means "not running" until the spec is updated
  (`debug_windows.py codex` prints the image path of every candidate).
- **Upstream focus bug.** The app is reported to sometimes leave the
  composer unfocusable after a turn completes (openai/codex issue 27549,
  macOS report). The sender then fails closed with "could not focus"; a
  retry on the next interval is the only recovery.
- **Enter while a turn runs.** The app either steers the running turn or
  queues the message, per its Follow-up behaviour setting (this machine:
  steer). The sender does not detect a running turn.
