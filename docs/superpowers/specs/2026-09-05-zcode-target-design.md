# Continue-Sender: ZCode desktop app target - Design

Date: 2026-09-05

## Goal

Add a fourth target, the **ZCode desktop app** (Zhipu's GLM-based coding
agent), to the Continue Sender (`sender.py`, `gui.py`, CLI wrappers), so a
scheduled "continue" can be typed into a running ZCode task after a
rate-limit reset - exactly like the Claude Desktop, Antigravity and Codex
targets.

The Codex target is the template: same focus method (`uia_composer`), same
verify-before-Enter chain. The one structural change is that the composer
lookup no longer hard-codes the Codex editor class.

## Evidence (all verified read-only on this machine, today)

Raw dumps: `zcode_uia.txt`, `zcode_uia2.txt` (UIA trees), `asar_scan.txt`,
`asar_scan2.txt` (app bundle) in the session scratchpad.

### The app and its windows

- ZCode 3.11.2, productName `ZCode`, an **Electron app** (not a VS Code
  fork), installed at `C:\Program Files\ZCode\ZCode.exe`; ~19 processes; user
  data in `%APPDATA%\ZCode`. No MSIX package, no same-named legacy exe.
- The process image name is `ZCode.exe`, which `sender._get_process_path`
  lower-cases to `zcode.exe`.
- Top-level windows of `zcode.exe`: the **main window** (class
  `Chrome_WidgetWin_1`, title `ZCode`, 1706x2100, visible, unowned), an
  **invisible 588x102** `ZCode` pop-up/quick window, several 0x0
  `Chrome_WidgetWin_0` windows, a 3840x1550 but invisible
  `Chrome_WidgetWin_0`, plus tray/IME/DDE helper windows.
- The title `ZCode` alone is not evidence of the app: an Explorer folder or a
  browser tab named "ZCode" would carry it while the app is closed.

### The composer

- Exactly **one** UIA `Edit` element (ControlType 50004) in the whole window;
  aria role `textbox`, multiline, framework `Chrome`, localized type `edit`.
  It is a **Lexical** (Meta) contenteditable - not ProseMirror.
- Its `ClassName` is a run of Tailwind utility classes
  (`min-h-10 max-h-40 overflow-y-auto text-ui-base leading-5 text-foreground
  outline-none`): a styling artefact, not an identifier that survives a
  restyling.
- Its `Name` (and `HelpText`) is the **placeholder**, which changes with the
  app's state: `Ask ZCode anything...`, `Ask for follow-up changes`,
  `Keep typing to queue follow-up changes`,
  `@ to add context, / for commands or capabilities`, `Initializing task...`.
- Geometry (dump): composer rect `(2240, 1902, 3336, 1976)` inside window
  rect `(1707, 0, 3415, 2101)` - anchored ~125 px above the window bottom,
  above a button row at `y = 1997..2048` that holds ComboBoxes (`Switch
  mode`, `Max`) and a Button (`Choose model`). The window sits on the second
  monitor (`x >= 1707`).
- The DOM tags the input `data-testid="chat-input"`; UIA does not expose that
  attribute, so it cannot be used as a locator.
- Patterns: **Value** (`CurrentValue` is `"\n"` when EMPTY - *not* the
  placeholder, unlike Codex), Text (DocumentRange `"\n"`), LegacyIAccessible
  (role 42, value `"\n"`), TextEdit, TextChild, ScrollItem. `IsEnabled` and
  `IsKeyboardFocusable` are True. Its only child is a Text element (50020)
  named `"\n"`.
- **The Chromium tree is lazy.** The first `ElementFromHandle` + `FindAll`
  returned **13 descendants and no Edit at all**; a later probe returned
  **266 descendants** including the composer. A warm-tree probe run after the
  implementation: 214 descendants, 1 Edit, found on attempt 1, value `'\n'`,
  draft `''`.

### Keyboard

- Lexical's `KEY_ENTER_COMMAND`: plain **Enter sends**, Shift+Enter inserts a
  newline. Same submit semantics as the other targets.
- The Electron menu's only accelerators are `CmdOrCtrl+N` (new task),
  `CmdOrCtrl+O` (open workspace), `CmdOrCtrl+W` (**close window**) and
  zoom in/out/reset. There is **no shortcut that focuses the composer**, and
  `Ctrl+W` is actively dangerous.
- The bundle ships a slash-command menu (`chat.slash.emptyResults` etc.), and
  the placeholder itself advertises `@` for context and `/` for commands.
- Typing while a task runs **queues a follow-up** ("Keep typing to queue
  follow-up changes") - which is exactly the intended use.

## Design

### `sender.py`

New target:

```python
"zcode": TargetSpec(
    name="ZCode",
    window_title_contains="",       # "" = exe-only matching
    exe_names=("zcode.exe",),
    focus_method="uia_composer",
    blocklist=(),
    prefer_largest_window=True,     # a pop-up shares process and title
    message_problem=zcode_message_problem,
    composer_class=None,            # any Edit element
),
```

- **Exe-only matching, no path pin.** `window_title_contains=""` disables
  title matching (the mechanism the Codex target introduced), so a folder or
  browser tab titled "ZCode" can never receive keystrokes; a closed app
  reports "not running". Unlike Codex, no `exe_path_contains` is needed:
  there is no MSIX package and no same-named foreign executable to tell
  apart, so the exe name alone identifies the app.
- **Visibility, not size, isolates the main window.** Besides the main
  window the process owns the invisible 588x102 pop-up, 0x0 helpers and a
  hidden, unowned **3840x1550** Chromium helper — *larger* than the
  1706x2100 main window (see Evidence). `find_target_windows` drops hidden
  and owned windows before any size is compared, and the 200x200 minimum in
  `_pick_main_window` drops the pop-up, so exactly one candidate is left.
  `prefer_largest=True` is therefore inert for ZCode today; it is set so a
  second *visible* full-size window cannot win on Z-order alone — and if the
  hidden 3840x1550 helper ever became visible, largest-wins would pick the
  wrong window. `debug_windows.py zcode` lists every candidate, so that is
  what to check after an app update.
- **Message validation.** `zcode_message_problem` refuses a message whose
  first non-space character is `/` (slash-command menu) or that contains `@`
  (context picker): Enter would pick a menu entry instead of sending. Same
  rule as Codex with ZCode wording; both are two-line wrappers around a
  shared `_slash_at_problem(message, app, at_menu)`.

#### Generalized composer lookup

`TargetSpec` gains one field:

```python
composer_class: Optional[str] = None   # uia_composer targets only
```

`_find_codex_composer(uia, mod, hwnd, log)` becomes
`_find_composer(uia, mod, hwnd, composer_class=None, log=None)`:

- `composer_class` set (Codex: `"ProseMirror"`) -> the FindAll condition stays
  `AND(ControlType == Edit, ClassName == <class>)`, unchanged.
- `composer_class` None (ZCode) -> the condition is `ControlType == Edit`
  alone.
- Either way: retry the query while it is empty or raises, keep candidates
  with a non-empty bounding rectangle, and pick the **bottom-most** one.

Why no class or name filter for ZCode: the class is Tailwind output and the
name is a state-dependent placeholder, so both would break silently at the
next release or when the app changes state; the window's *structure* - one
Edit, at the bottom, above a row of ComboBoxes and Buttons - is the stable
fact. If ZCode ever grows a second Edit above the composer, bottom-most still
picks the right one; a second Edit *below* it would be a new finding for
`debug_windows.py`.

`_find_codex_composer` is kept as a thin wrapper pinned to
`CODEX_COMPOSER_CLASS`, so the Codex behaviour has a name of its own and the
existing tests still exercise it. `_focus_codex_composer(hwnd, log)` likewise
becomes a wrapper around `_focus_composer(spec, hwnd, log)`, which takes the
spec so every error message names the right app ("the ZCode composer already
contains text ...") and the finder gets the right class.

`_is_element_focused`'s same-rect fallback compared the focused element's
class against the hard-coded `"ProseMirror"`; it now compares it against the
**element's own** class. Same behaviour for Codex, and the guard also works
for a target that pins no class.

#### Retry budget: grown, deliberately

`COMPOSER_FIND_ATTEMPTS` goes **4 -> 6** (delay unchanged at 1.0 s), i.e. up
to ~5 s instead of ~3 s of waiting for a cold accessibility tree. Reason: the
measured ZCode cold tree had 13 descendants and *no* Edit, and only a later
probe saw all 266 - the app was mid-startup of its renderer. The budget costs
nothing on the happy path (the loop returns from the first successful
attempt; the warm-tree probe finds the composer on attempt 1 in 0.03 s) and
only lengthens the road to a failure that would be reported anyway. It is
shared by every `uia_composer` target rather than made per-target: the cause
is Chromium, not the app, and Codex is a Chromium app too.

The budget constants are renamed `CODEX_* -> COMPOSER_*` (they are no longer
Codex-specific); the old names remain as aliases so external callers and
tests keep working.

#### What is unchanged

`_composer_draft` already strips whitespace, so ZCode's empty value `"\n"`
reads as "no draft"; the Codex placeholder-equals-name rule stays as a second
branch. `_verify_typed_text` compares the read-back against the baseline
taken before typing, so `"\n"` -> `"\ncontinue"` passes and an unchanged
`"\n"` fails. The pre-Enter chain (foreground window, element focus, text
read-back) and the `_VERIFIED_FOCUS_METHODS` control-type re-check apply to
ZCode with no change.

### `gui.py`

A fourth radio button **ZCode** (value `zcode`), and the Target row becomes a
**2x2 grid**. Measured with a withdrawn Tk root
(`root.withdraw()`; build; `update_idletasks()`) in a **DPI-aware** process -
importing `sender` pulls in `pyautogui`, which sets DPI awareness; measuring
without it reports 96-dpi numbers and wrongly says the row fits: four buttons
in one row request **462 px**, while the 460 px window leaves **444 px**
between the outer paddings - the ZCode label would be clipped.

The grid buys that width with height, in a window that is already over-full
(the whole layout requests 832x764 into a 460x580 window): the frame grows
**58 -> 92 px**, and since every frame above the log pane is packed at its
requested height - 520 px together - while the log pane (packed last,
`expand=True`) takes the remainder, those 34 px come straight out of the log:
**52 px of the 236 it asks for** instead of 86, i.e. from about three visible
lines to one, in the pane where the sender's errors appear. So the window
geometry grows with it, `460x580` -> **`460x614`**
(the window is resizable, so this is a better starting size, not a
guarantee - at a different display scaling the numbers differ).
`validate_settings` needs no change: it already applies
`spec.message_problem` generically, which now covers ZCode.

### CLI

New `zcode_continue.py`, the same four-line wrapper around `cli.py` as the
other three, with a ZCode-specific docstring.

### `debug_windows.py`

Usage lists `zcode`. The composer probe now runs for **every target whose
`focus_method` is `uia_composer`** (previously `key == "codex"`), uses
`spec.composer_class`, and additionally prints the Value-pattern text and the
computed draft - the two reads the real send depends on. Still read-only:
nothing is activated, focused, clicked or typed.

Live output on this machine:

```
Windows found for ZCode:
HWND: 2233662, Title: 'ZCode', Rect: w=1706 h=2100, exe='c:\program files\zcode\zcode.exe'
Would use: (2233662, 'ZCode')
Composer: name='Ask for follow-up changes' class='min-h-10 max-h-40 overflow-y-auto text-ui-base leading-5 text-foreground outline-none' rect=(2240, 1902, 3336, 1976) focusable=True has_focus=False
  value='\n' draft=''
```

### README

ZCode added to the intro, the entry points, "How it works" (exe-only match,
Lexical composer found as the only/bottom-most Edit, no focus shortcut and
the `Ctrl+W` hazard, `/` and `@` refusal, placeholder is not the value) and
the Files list.

## Error handling

All new messages come from the generalized `_focus_composer`, so they name
the target:

- `No window matching ZCode found. Is it running?`
- `Message starts with '/' (opens the ZCode slash-command menu; ...). Not
  typing it into ZCode.` / `Message contains '@' (opens the ZCode context
  picker; ...)` - both `ConfigurationError`, so a repeat run ends at once
  instead of retrying something that can never work.
- `UI Automation is unavailable ...; refusing to type into the ZCode composer
  unverified.`
- `Could not find the ZCode composer (no edit element in the window after 6
  attempts). Not typing.`
- `Could not focus the ZCode composer (SetFocus and a click both left focus
  elsewhere). Not typing.`
- `The ZCode composer already contains text ('...'); not typing on top of a
  draft.`
- `The ZCode window lost the foreground while typing; not pressing Enter.` /
  `The ZCode composer lost focus while typing; not pressing Enter.` /
  `Composer text after typing is ...`

Everything except the message rules is a `RuntimeError`, so an unattended
repeat run retries at the next interval and gives up after three failures in
a row (`2026-09-05-send-loop-retry-design.md`).

## Testing

All tests stay fully mocked - no mouse, no keyboard, no windows. New cases in
`tests/test_bugs.py`:

- `TARGETS["zcode"]` shape, including `composer_class is None` and the empty
  `exe_path_contains`, while Codex still pins `ProseMirror`.
- `zcode_message_problem` (plain passes, leading `/` and any `@` refused, the
  reason names ZCode), `gui.validate_settings` applying it, and
  `send_once("zcode", "/clear")` refusing before activation.
- Exe-only matching: a window titled `ZCode` owned by `explorer.exe` /
  `chrome.exe` / PyCharm does not match; `zcode.exe` matches with and without
  an image path; `find_target_windows` end-to-end keeps only the visible
  unowned main window, and - enumeration plus `_pick_main_window` together -
  the hidden 3840x1550 sibling never wins even though it is the largest
  window of the process.
- `_find_composer` with a **condition-honouring** UIA fake (`FindAll` really
  applies the condition it is handed, so the test proves the *query*, not
  only the pick): without a class filter only Edits are asked for and the
  ComboBoxes of the row *below* the composer cannot win; any class is
  accepted; the bottom-most Edit wins; `_find_codex_composer` still AND-s the
  ProseMirror class and ignores a lower Edit of another class; a renamed
  class yields None (fail closed) instead of falling back to "any Edit".
- The retry budget survives **five** empty answers in a row - the composer
  is only found on the sixth attempt, so a silent revert to the pre-ZCode
  budget of 4 turns the test red instead of passing unnoticed - and the
  legacy `CODEX_*` budget names still equal the `COMPOSER_*` ones.
- `_is_element_focused`: the same-rect fallback matches on the element's own
  class and rejects a different class.
- `_focus_composer` for zcode: no `hotkey`/`press` is ever issued, the finder
  is called with `composer_class=None`, click fallback at the rect centre,
  draft/not-found/no-UIA all raise and name ZCode without clicking.
- `send_once("zcode")`: happy path with baseline `"\n"` -> `"\ncontinue"`;
  no Enter when the box still reads `"\n"`, when the control type changed,
  when the window lost the foreground, or when the element lost focus;
  `prefer_largest=True` is passed to the window picker; `_focus_input` gets
  the zcode spec; and the **complete key inventory** of a send is asserted
  against the list documented above (four modifier releases, the activation
  `Alt` tap, the message, Enter - no `hotkey`, no `W`), with the real
  `force_activate_window` in the path, so the docs cannot drift from the
  code again.
- `debug_windows`: the composer is probed for `zcode` (class filter absent)
  and for `codex` (pinned to `ProseMirror`) and its value printed, a missing
  composer is reported not crashed, a non-composer target is not probed, an
  unknown key exits.
- `zcode_continue.py` forwards all flags, and *every* `TARGETS` key has a CLI
  wrapper.
- `gui.py`: every `TARGETS` key has a radio button driving `target_var`
  (unchanged), and the four buttons are placed with `grid`, at most two per
  row - a revert to a single packed row is the clipping regression the grid
  exists to prevent, and it is invisible to a DPI-unaware measurement, so
  placement is asserted instead of width.

Existing tests changed:

- `CodexComposerFocusTests` patched `sender._find_codex_composer`; the focus
  path now calls the generalized `sender._find_composer`, so the patch target
  moved. Its `_run` helper now also returns that patch mock, and a new case
  asserts the finder is called **with** `CODEX_COMPOSER_CLASS` - the mirror
  of the zcode "called without a class filter" case. This is the only test on
  the link the generalization introduced (`_focus_composer` forwarding
  `spec.composer_class`): `_find_codex_composer` hard-codes the class itself,
  so every test that goes through it stays green when the pin is dropped at
  the call site, and a Codex send would then accept any bottom-most Edit in
  the window - with the focus check and the read-back confirming it, since
  both read that same element.
- The retry-budget case, see above.
- `GuiTargetChoicesTests` builds the layout through a shared helper that now
  keeps each fake Radiobutton widget, so the placement assertion can look at
  how it was gridded.

Every guard here was checked by mutation on a scratch copy of the sources.
Six mutations - dropping the class pin at the call site, reverting the budget
to 4, packing the radios into one row, dropping the class filter in
`debug_windows.py`, removing the activation `Alt` tap, and slipping a
`Ctrl+W` into the send path - each turn exactly one test red. The first four
left the suite green before these guards existed, which is why they were
written.

Live verification is limited to **read-only** probes: window enumeration, the
`FindAll` query, and Value/pattern reads. No send was performed - the user's
ZCode is running a real task and a stray keystroke would land in it.

## Non-goals

- The ZCode CLI/terminal client and any editor extension.
- Detecting the rate-limit banner or a running task (typing while a task runs
  queues a follow-up, which is the point).
- Appending to an existing draft (refused instead).
- Choosing which task/workspace receives the message - the app's current view
  is used, as for the other targets.
- macOS/Linux.

## Risks

- **A second Edit element appears.** Today there is exactly one. A future
  search box or rename dialog *below* the composer would win the bottom-most
  rule, and **the draft check and the read-back would not stop it.** Both
  read `handle.element` - the element that was picked - so they answer "did
  the keystrokes land where I aimed", never "did I aim at the composer". A
  wrong element that already holds text does abort the send (`_composer_draft`
  is non-empty, e.g. a pre-filled rename dialog); a wrong element that is
  **empty and focusable** - a fresh search box - passes the draft check,
  receives the message and gets Enter, and `send_once` still logs a
  successful send. `_is_element_focused` cannot help either: it compares the
  focused element against that same picked element.
  The real controls are therefore: `debug_windows.py zcode`, which prints
  the element UIA finds together with its value and computed draft, run
  after a ZCode update; and pinning `composer_class` if a second Edit ever
  shows up - the field exists for exactly that. A window-rectangle or
  `IsOffscreen` filter would *not* close this hole: the hypothetical search
  box sits inside the window and on screen (such a filter would only help
  against the "scrolled out of view" risk below).
- **Elements scrolled out of view keep real rectangles.** The dump shows
  history items at negative `y`. They are Buttons, not Edits, so the type
  filter excludes them - but an Edit scrolled *below* the viewport would beat
  the composer. Not observed.
- **Window title / exe rename.** A renamed executable means "not running"
  until `exe_names` is updated; `debug_windows.py zcode` prints the image
  path of every candidate.
- **Cold accessibility tree.** Mitigated by the 6 x 1 s budget; a slower
  machine could still need more, and the failure is then a clear "could not
  find the ZCode composer", retried at the next interval by a repeat run.
- **`Ctrl+W` is one keystroke away from closing the window.** The sender
  presses no *shortcut* for this target - it focuses via UIA `SetFocus`
  (click as fallback) and types plain ASCII plus Enter - but it is not true
  that it presses no key or no modifier. The complete key inventory of a
  ZCode send is: the stuck-modifier releases (`keyUp` of ctrl/shift/alt/win),
  then the single **`Alt` tap** (`keyDown`+`keyUp`) that
  `force_activate_window` uses on *every* target to let Windows change the
  foreground window, then the message, then Enter. That Alt tap also fires
  when ZCode is already the foreground window, i.e. at every interval of a
  repeat run. It is harmless here - the window has no menu bar for a bare
  Alt to open (`GetMenu(hwnd)` is NULL and the UIA tree of the main window
  contains no `Menu`, `MenuBar` or `MenuItem` element at all; the Electron
  accelerators are an accelerator table, not a rendered menu bar) - and no
  combination the sender ever presses contains `W`.
- **Placeholder-as-name.** Because the name changes with state, nothing in
  the sender may key off it. `_composer_draft`'s
  "value equals the accessible name" branch (which exists for Codex) could in
  principle mistake a draft that reads exactly like the current placeholder
  for an empty box; harmless in practice, and ZCode's empty value is `"\n"`
  anyway.
