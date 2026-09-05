# Codex Desktop App Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the OpenAI Codex desktop app (`ChatGPT.exe`, MSIX package `OpenAI.Codex`) as a third target of the Continue Sender, with a UI-Automation-based composer focus that fails closed.

**Architecture:** `sender.py` gains a `codex` `TargetSpec` (exe-only window matching) and a `uia_composer` focus method that locates the bottom-most `ProseMirror` edit element through UI Automation, focuses it with `SetFocus`, verifies, and falls back to a click into the element. The three CLI wrappers share a new `cli.py`; `gui.py` gets a third radio button; `debug_windows.py` learns a target argument and prints the composer element for `codex`.

**Tech Stack:** Python 3.12, pyautogui, comtypes (UI Automation), Tkinter, unittest with `unittest.mock`.

## Global Constraints

- Windows only; no cross-platform code paths.
- No new dependencies (`pyautogui`, `comtypes` already in `requirements.txt`).
- Tests must stay fully mocked: never move the mouse, type, activate, or open windows. Run with `.venv\Scripts\python.exe -m unittest discover -s tests`.
- Every failure path in the Codex focus routine raises `RuntimeError` **before** anything is typed.
- No keyboard shortcut for the Codex composer: `Shift+Escape` = "clear all unreads", plain `Escape` stops a running turn (from the app bundle).
- Codex target identity: `exe_names=("chatgpt.exe", "codex.exe")`, `window_title_contains=""` (exe-only), `focus_method="uia_composer"`.
- Composer identity: UIA `ControlType == 50004 (Edit)` and `ClassName == "ProseMirror"`, bottom-most candidate wins.
- No unverified path: when UI Automation is unavailable the Codex send raises before typing (the geometric fallback originally planned in Task 3 was removed by the amendments at the end of this plan).
- Commits: the user has not asked for commits. Leave the working tree uncommitted; offer one commit at the end.
- Console is cp1252: scripts that print live UIA data start with `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.

---

## File Structure

```
claude_desktop_continue/
├── sender.py                 # MODIFY: codex TargetSpec, _matches_spec, _uia, composer lookup + focus
├── cli.py                    # NEW: shared argparse surface for the CLI wrappers
├── claude_continue.py        # MODIFY: thin wrapper over cli.py
├── antigravity_continue.py   # MODIFY: thin wrapper over cli.py
├── codex_continue.py         # NEW: thin wrapper over cli.py
├── gui.py                    # MODIFY: third radio button
├── debug_windows.py          # MODIFY: target argument + codex composer probe
├── README.md                 # MODIFY: Codex documentation
└── tests/test_bugs.py        # MODIFY: new test classes
```

---

### Task 1: Exe-only window matching and the Codex TargetSpec

**Files:**
- Modify: `sender.py` (TargetSpec comment, `TARGETS`, `find_target_windows` match predicate)
- Test: `tests/test_bugs.py`

**Interfaces:**
- Produces: `sender.TARGETS["codex"]`; `sender._matches_spec(spec: TargetSpec, title: str, exe: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bugs.py` (before `if __name__ == "__main__":`):

```python
class ExeOnlyMatchingTests(unittest.TestCase):
    """An empty window_title_contains disables title matching. The Codex app
    window is titled 'ChatGPT', which would also match browser tabs, Explorer
    folders and editor buffers when the app is NOT running; exe-only matching
    reports 'not running' instead of typing into one of those."""

    def test_empty_title_needle_never_matches_by_title(self):
        spec = _spec(exe_names=("chatgpt.exe",))
        spec = TargetSpec(name=spec.name, window_title_contains="",
                          exe_names=spec.exe_names, focus_method=spec.focus_method)
        self.assertFalse(sender._matches_spec(spec, "ChatGPT - Google Chrome", "chrome.exe"))
        self.assertFalse(sender._matches_spec(spec, "anything", "explorer.exe"))

    def test_empty_title_needle_still_matches_by_exe(self):
        spec = TargetSpec(name="Codex", window_title_contains="",
                          exe_names=("chatgpt.exe",), focus_method="uia_composer")
        self.assertTrue(sender._matches_spec(spec, "ChatGPT", "chatgpt.exe"))
        self.assertTrue(sender._matches_spec(spec, "", "chatgpt.exe"))

    def test_non_empty_needle_keeps_title_matching(self):
        spec = _spec()  # window_title_contains="Target"
        self.assertTrue(sender._matches_spec(spec, "my target app", "other.exe"))
        self.assertFalse(sender._matches_spec(spec, "nothing here", "other.exe"))
        self.assertTrue(sender._matches_spec(spec, "nothing here", "target.exe"))


class CodexTargetSpecTests(unittest.TestCase):
    def test_codex_target_shape(self):
        spec = sender.TARGETS["codex"]
        self.assertEqual(spec.window_title_contains, "")
        self.assertIn("chatgpt.exe", spec.exe_names)
        self.assertEqual(spec.focus_method, "uia_composer")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_bugs.ExeOnlyMatchingTests tests.test_bugs.CodexTargetSpecTests -v`
Expected: FAIL / ERROR with `AttributeError: module 'sender' has no attribute '_matches_spec'` and `KeyError: 'codex'`.

- [ ] **Step 3: Implement**

In `sender.py`, change the `TargetSpec` comment and add the target:

```python
@dataclass(frozen=True)
class TargetSpec:
    name: str
    window_title_contains: str  # "" disables title matching (exe-only target)
    exe_names: tuple[str, ...]
    focus_method: str  # "click_bottom_center", "agent_input" or "uia_composer"
    blocklist: tuple[str, ...] = ()


TARGETS: dict[str, TargetSpec] = {
    "claude": TargetSpec(...unchanged...),
    "antigravity": TargetSpec(...unchanged...),
    # The Codex desktop app ships as the MSIX package OpenAI.Codex, but its
    # process image is ChatGPT.exe (a Codex.exe launcher stub sits next to
    # it). The window title is "ChatGPT", which would also match browser tabs
    # and Explorer folders when the app is closed, so matching is exe-only.
    "codex": TargetSpec(
        name="Codex",
        window_title_contains="",
        exe_names=("chatgpt.exe", "codex.exe"),
        focus_method="uia_composer",
        blocklist=(),
    ),
}
```

Add the predicate above `find_target_windows` and use it inside the enum callback:

```python
def _matches_spec(spec: TargetSpec, title: str, exe: str) -> bool:
    """Window (title, exe) belongs to `spec`. Title matching is opt-in: an
    empty window_title_contains means exe-only (see the codex target)."""
    needle = spec.window_title_contains.lower()
    if needle and title and needle in title.lower():
        return True
    return exe in spec.exe_names
```

Replace in `enum_cb`:

```python
                if _matches_spec(spec, title, exe):
                    matches.append((hwnd, title, exe))
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests`
Expected: all tests pass (40 old + 4 new).

---

### Task 2: UIA client factory and composer lookup

**Files:**
- Modify: `sender.py` (`_focused_control_type` refactor, new helpers)
- Test: `tests/test_bugs.py`

**Interfaces:**
- Produces:
  - `sender._uia() -> tuple[IUIAutomation, module] | None`
  - `sender._element_rect(element) -> tuple[int, int, int, int]` (left, top, right, bottom)
  - `sender._find_codex_composer(uia, mod, hwnd) -> element | None`
  - `sender._is_element_focused(uia, element) -> bool`
  - Constants `UIA_EDIT_CONTROL_TYPE = 50004`, `CODEX_COMPOSER_CLASS = "ProseMirror"`, `CODEX_COMPOSER_FIND_ATTEMPTS = 4`, `CODEX_COMPOSER_FIND_DELAY_S = 1.0`.

- [ ] **Step 1: Write the failing tests**

Add near the top of `tests/test_bugs.py` (after the imports) these helpers:

```python
import types


def _fake_element(rect, cls="ProseMirror", ctype=50004):
    el = mock.Mock()
    r = mock.Mock()
    r.left, r.top, r.right, r.bottom = rect
    el.CurrentBoundingRectangle = r
    el.CurrentClassName = cls
    el.CurrentControlType = ctype
    return el


def _fake_uia(find_results):
    """(uia, mod) whose root.FindAll returns the element lists in
    `find_results`, one list per call."""
    uia = mock.Mock()
    mod = types.SimpleNamespace(UIA_ControlTypePropertyId=30003,
                                UIA_ClassNamePropertyId=30012,
                                TreeScope_Descendants=4)
    calls = iter(find_results)

    def find_all(_scope, _cond):
        elems = next(calls)
        arr = mock.Mock()
        arr.Length = len(elems)
        arr.GetElement.side_effect = lambda i: elems[i]
        return arr

    root = mock.Mock()
    root.FindAll.side_effect = find_all
    uia.ElementFromHandle.return_value = root
    return uia, mod
```

Append the test classes:

```python
class FindCodexComposerTests(unittest.TestCase):
    """The Codex composer is the bottom-most ProseMirror edit element. Chromium
    builds its accessibility tree lazily on first UIA contact, so an empty
    FindAll result is retried before giving up."""

    def test_picks_bottom_most_prosemirror_edit(self):
        top_editor = _fake_element((40, 300, 900, 360))
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[top_editor, composer]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)

    def test_skips_empty_rectangles(self):
        ghost = _fake_element((0, 0, 0, 0))
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[composer, ghost]])
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)

    def test_retries_while_tree_is_empty(self):
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[], [composer]])
        fake_time = mock.Mock()
        with mock.patch.object(sender, "time", fake_time):
            self.assertIs(sender._find_codex_composer(uia, mod, 42), composer)
        fake_time.sleep.assert_called_once_with(sender.CODEX_COMPOSER_FIND_DELAY_S)

    def test_returns_none_after_attempt_budget(self):
        uia, mod = _fake_uia([[]] * sender.CODEX_COMPOSER_FIND_ATTEMPTS)
        with mock.patch.object(sender, "time", mock.Mock()):
            self.assertIsNone(sender._find_codex_composer(uia, mod, 42))
        root = uia.ElementFromHandle.return_value
        self.assertEqual(root.FindAll.call_count,
                         sender.CODEX_COMPOSER_FIND_ATTEMPTS)

    def test_condition_uses_edit_type_and_prosemirror_class(self):
        composer = _fake_element((40, 1649, 986, 1713))
        uia, mod = _fake_uia([[composer]])
        with mock.patch.object(sender, "time", mock.Mock()):
            sender._find_codex_composer(uia, mod, 42)
        uia.CreatePropertyCondition.assert_any_call(30003, 50004)
        uia.CreatePropertyCondition.assert_any_call(30012, "ProseMirror")


class IsElementFocusedTests(unittest.TestCase):
    def test_true_when_uia_says_same_element(self):
        uia = mock.Mock()
        uia.CompareElements.return_value = 1
        self.assertTrue(sender._is_element_focused(uia, _fake_element((0, 0, 1, 1))))

    def test_true_when_focused_is_a_prosemirror_edit_at_the_same_rect(self):
        # Fallback identity when CompareElements says no: same type, class
        # AND bounding rectangle.
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element((41, 1649, 986, 1713))
        self.assertTrue(sender._is_element_focused(
            uia, _fake_element((41, 1649, 986, 1713))))

    def test_false_for_a_prosemirror_edit_elsewhere(self):
        # Another ProseMirror editor (rename dialog, notes) must not pass as
        # the composer just because it shares type and class.
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element((300, 400, 700, 460))
        self.assertFalse(sender._is_element_focused(
            uia, _fake_element((41, 1649, 986, 1713))))

    def test_false_for_other_elements(self):
        uia = mock.Mock()
        uia.CompareElements.return_value = 0
        uia.GetFocusedElement.return_value = _fake_element((0, 0, 1, 1), cls="btn", ctype=50000)
        self.assertFalse(sender._is_element_focused(uia, _fake_element((0, 0, 1, 1))))

    def test_false_when_uia_raises(self):
        uia = mock.Mock()
        uia.GetFocusedElement.side_effect = OSError("COM failure")
        self.assertFalse(sender._is_element_focused(uia, _fake_element((0, 0, 1, 1))))


class FocusedControlTypeTests(unittest.TestCase):
    def test_none_when_uia_unavailable(self):
        with mock.patch.object(sender, "_uia", return_value=None):
            self.assertIsNone(sender._focused_control_type())

    def test_reads_type_from_focused_element(self):
        uia = mock.Mock()
        uia.GetFocusedElement.return_value.CurrentControlType = 50003
        with mock.patch.object(sender, "_uia", return_value=(uia, object())):
            self.assertEqual(sender._focused_control_type(), 50003)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_bugs.FindCodexComposerTests tests.test_bugs.IsElementFocusedTests tests.test_bugs.FocusedControlTypeTests -v`
Expected: ERROR with `AttributeError: module 'sender' has no attribute '_find_codex_composer'` (and `_uia`, `_is_element_focused`).

- [ ] **Step 3: Implement**

In `sender.py`, after `UIA_TEXT_ENTRY_TYPES`, add:

```python
# The Codex desktop app composer, as seen by UI Automation: an Edit element
# whose class is the ProseMirror editor root. It is the bottom-most such
# element in the window (the button row below it holds no editors).
UIA_EDIT_CONTROL_TYPE = 50004
CODEX_COMPOSER_CLASS = "ProseMirror"
# Chromium builds its accessibility tree lazily on first UIA contact; the
# first FindAll after activation may legitimately come back empty.
CODEX_COMPOSER_FIND_ATTEMPTS = 4
CODEX_COMPOSER_FIND_DELAY_S = 1.0
```

Replace `_focused_control_type` with:

```python
def _uia():
    """(IUIAutomation client, UIAutomationCore module) or None when UI
    Automation cannot be used (comtypes missing, COM failure). None means
    "cannot verify", never "verification failed"."""
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
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


def _find_codex_composer(uia, mod, hwnd):
    """Bottom-most ProseMirror edit element inside `hwnd`, or None."""
    condition = uia.CreateAndCondition(
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId,
                                    UIA_EDIT_CONTROL_TYPE),
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId,
                                    CODEX_COMPOSER_CLASS),
    )
    for attempt in range(CODEX_COMPOSER_FIND_ATTEMPTS):
        if attempt:
            time.sleep(CODEX_COMPOSER_FIND_DELAY_S)
        root = uia.ElementFromHandle(hwnd)
        found = root.FindAll(mod.TreeScope_Descendants, condition)
        best = None
        best_bottom = None
        for i in range(found.Length):
            element = found.GetElement(i)
            left, top, right, bottom = _element_rect(element)
            if right <= left or bottom <= top:
                continue  # collapsed/offscreen editor, cannot be clicked
            if best is None or bottom > best_bottom:
                best, best_bottom = element, bottom
        if best is not None:
            return best
    return None


def _is_element_focused(uia, element) -> bool:
    """True if `element` holds keyboard focus. Identity via UIA
    CompareElements first (verified stable across queries in the Codex app);
    as a guard against a version where it is not, a focused ProseMirror edit
    at exactly the same bounding rectangle counts too — a different editor
    (dialog, notes field) never sits at the composer's bottom-anchored
    rectangle."""
    try:
        focused = uia.GetFocusedElement()
        if uia.CompareElements(focused, element):
            return True
        return (int(focused.CurrentControlType) == UIA_EDIT_CONTROL_TYPE
                and (focused.CurrentClassName or "") == CODEX_COMPOSER_CLASS
                and _element_rect(focused) == _element_rect(element))
    except Exception:
        return False
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests`
Expected: all pass.

---

### Task 3: The `uia_composer` focus method and the post-typing guard

> Historical: executed as written, then superseded in part by the
> "Amendments" section at the end (no geometric fallback, focus polling,
> draft check, `ComposerHandle` return, pre-Enter re-checks).

**Files:**
- Modify: `sender.py` (`_focus_codex_composer`, `_focus_input`, `send_once`)
- Test: `tests/test_bugs.py`

**Interfaces:**
- Consumes: `_uia`, `_find_codex_composer`, `_is_element_focused`, `_element_rect` from Task 2.
- Produces: `sender._focus_codex_composer(hwnd, log)`; `sender._VERIFIED_FOCUS_METHODS`; constant `CODEX_COMPOSER_BOTTOM_OFFSET_PX = 110`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bugs.py`:

```python
class CodexComposerFocusTests(unittest.TestCase):
    """Codex desktop app: no safe focus shortcut exists (Shift+Esc = clear
    unreads, Esc STOPS a running turn), so the composer is located via UIA,
    focused with SetFocus, verified, and clicked as a fallback. Every failure
    raises before anything is typed."""

    RECT = (41, 1649, 986, 1713)

    def _run(self, focused, composer="default", uia_available=True):
        composer = _fake_element(self.RECT) if composer == "default" else composer
        fake = mock.Mock()
        logs = []
        handles = (mock.Mock(), object()) if uia_available else None
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia", return_value=handles), \
             mock.patch.object(sender, "_find_codex_composer",
                               return_value=composer), \
             mock.patch.object(sender, "_is_element_focused",
                               side_effect=focused), \
             mock.patch.object(sender, "get_window_rect",
                               return_value=(0, 704, 1024, 1088)), \
             mock.patch.object(sender, "_window_dpi_scale", return_value=1.25):
            sender._focus_input(sender.TARGETS["codex"], 42, "ChatGPT",
                                log=logs.append)
        return fake, composer, logs

    def test_setfocus_alone_when_verified(self):
        fake, composer, _ = self._run(focused=[True])
        composer.SetFocus.assert_called_once_with()
        fake.click.assert_not_called()

    def test_click_into_element_when_setfocus_does_not_take(self):
        fake, composer, _ = self._run(focused=[False, True])
        composer.SetFocus.assert_called_once_with()
        fake.click.assert_called_once_with((41 + 986) // 2, (1649 + 1713) // 2)

    def test_setfocus_exception_falls_back_to_click(self):
        composer = _fake_element(self.RECT)
        composer.SetFocus.side_effect = OSError("E_FAIL")
        fake, _, _ = self._run(focused=[False, True], composer=composer)
        fake.click.assert_called_once()

    def test_raises_when_neither_setfocus_nor_click_focuses(self):
        with self.assertRaises(RuntimeError):
            self._run(focused=[False, False])

    def test_raises_when_composer_not_found(self):
        fake = mock.Mock()
        with mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()), \
             mock.patch.object(sender, "_uia", return_value=(mock.Mock(), object())), \
             mock.patch.object(sender, "_find_codex_composer", return_value=None):
            with self.assertRaises(RuntimeError):
                sender._focus_codex_composer(42, log=lambda _t: None)
        fake.click.assert_not_called()

    def test_without_uia_clicks_geometric_fallback_and_warns(self):
        # rect (0, 704, 1024, 1088): x = 0 + 512, y = 704 + 1088 - int(110 * 1.25)
        fake, _, logs = self._run(focused=[], uia_available=False)
        fake.click.assert_called_once_with(512, 704 + 1088 - 137)
        self.assertTrue(any("WARN" in line for line in logs))


class SendOnceCodexTests(unittest.TestCase):
    def _send(self, focused_after_typing):
        fake = mock.Mock()
        with mock.patch.object(sender, "find_target_windows",
                               return_value=[(42, "ChatGPT")]), \
             mock.patch.object(sender, "_pick_main_window",
                               return_value=(42, "ChatGPT")), \
             mock.patch.object(sender, "force_activate_window",
                               return_value=True), \
             mock.patch.object(sender, "_focus_input"), \
             mock.patch.object(sender, "_focused_control_type",
                               return_value=focused_after_typing), \
             mock.patch.object(sender, "pyautogui", fake), \
             mock.patch.object(sender, "time", mock.Mock()):
            sender.send_once("codex", "continue", log=lambda _t: None)
        return fake

    def test_enter_pressed_when_composer_keeps_focus(self):
        fake = self._send(focused_after_typing=50004)
        fake.typewrite.assert_called_once()
        fake.press.assert_called_once_with("enter")

    def test_no_enter_when_focus_lost_after_typing(self):
        with self.assertRaises(RuntimeError):
            self._send(focused_after_typing=50000)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_bugs.CodexComposerFocusTests tests.test_bugs.SendOnceCodexTests -v`
Expected: `ValueError: Unknown focus_method: uia_composer` and, for `test_no_enter_when_focus_lost_after_typing`, no exception raised (guard not active yet).

- [ ] **Step 3: Implement**

In `sender.py`, next to the other composer constants:

```python
# Geometric fallback (UIA unavailable only): the composer's editable line sits
# ~110 CSS px above the window bottom (measured: edit box bottom-143..-79,
# the button row below it bottom-74..-32).
CODEX_COMPOSER_BOTTOM_OFFSET_PX = 110

# Focus methods that verify, via UIA, that a text element holds focus after
# typing and before Enter is pressed.
_VERIFIED_FOCUS_METHODS = frozenset({"agent_input", "uia_composer"})
```

After `_focus_ide_agent_panel`, add:

```python
def _focus_codex_composer(hwnd, log: Callable[[str], None]) -> None:
    """Focus the composer of the Codex desktop app (ChatGPT.exe).

    No keyboard shortcut is safe here: the app binds Shift+Escape to 'clear
    all unreads', and plain Escape STOPS a running turn before it would ever
    focus the composer. So the composer is located through UI Automation
    (bottom-most ProseMirror edit element), focused via UIA SetFocus and
    verified; a click into the element is the fallback. Every failure raises
    before anything is typed."""
    handles = _uia()
    if handles is None:
        left, top, width, height = get_window_rect(hwnd)
        click_x = left + width // 2
        click_y = top + height - int(
            CODEX_COMPOSER_BOTTOM_OFFSET_PX * _window_dpi_scale(hwnd)
        )
        log(f"[WARN] UIA unavailable; clicking the composer area blind at "
            f"({click_x}, {click_y}).")
        pyautogui.click(click_x, click_y)
        time.sleep(0.5)
        return

    uia, mod = handles
    composer = _find_codex_composer(uia, mod, hwnd)
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
    time.sleep(0.3)
    if _is_element_focused(uia, composer):
        return

    left, top, right, bottom = _element_rect(composer)
    click_x, click_y = (left + right) // 2, (top + bottom) // 2
    log(f"[INFO] Composer not focused after SetFocus; clicking "
        f"({click_x}, {click_y}).")
    pyautogui.click(click_x, click_y)
    time.sleep(0.4)
    if _is_element_focused(uia, composer):
        return
    raise RuntimeError(
        "Could not focus the Codex composer (SetFocus and a click both left "
        "focus elsewhere). Not typing."
    )
```

In `_focus_input`, add before the `else`:

```python
    elif spec.focus_method == "uia_composer":
        _focus_codex_composer(hwnd, log)
```

In `send_once`, replace the post-typing guard:

```python
    if spec.focus_method in _VERIFIED_FOCUS_METHODS:
        ct = _focused_control_type()
        if ct is not None and ct not in UIA_TEXT_ENTRY_TYPES:
            raise RuntimeError(
                f"Focus left the input while typing (control type "
                f"{ct}); not pressing Enter."
            )
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests`
Expected: all pass.

---

### Task 4: Shared CLI module and `codex_continue.py`

**Files:**
- Create: `cli.py`, `codex_continue.py`
- Modify: `claude_continue.py`, `antigravity_continue.py`
- Test: `tests/test_bugs.py`

**Interfaces:**
- Produces: `cli.build_parser(description: str) -> argparse.ArgumentParser`; `cli.run(target: str, args: argparse.Namespace) -> None`; wrappers keep `parse_args()` and `main()`.

- [ ] **Step 1: Write the failing tests**

Add imports at the top of `tests/test_bugs.py` (after `import gui`):

```python
import antigravity_continue
import claude_continue
import codex_continue
```

Append:

```python
class CliWrapperTests(unittest.TestCase):
    """The three *_continue.py wrappers share cli.py; each must keep its
    target and turn the flags into send_loop arguments unchanged."""

    def _run(self, module, argv):
        with mock.patch.object(sys, "argv", ["prog"] + argv), \
             mock.patch.object(sender, "send_loop") as loop:
            module.main()
        return loop

    def test_codex_wrapper_forwards_all_flags(self):
        loop = self._run(codex_continue, [
            "--minutes", "30", "--every-hours", "2", "--count", "3",
            "--message", "go on"])
        loop.assert_called_once_with(target="codex", message="go on",
                                     initial_delay_s=1800.0, every_s=7200.0,
                                     count=3)

    def test_no_repeat_passes_none_interval(self):
        loop = self._run(codex_continue, ["--hours", "1"])
        loop.assert_called_once_with(target="codex", message="continue",
                                     initial_delay_s=3600.0, every_s=None,
                                     count=0)

    def test_other_wrappers_keep_their_targets(self):
        self.assertEqual(
            self._run(claude_continue, []).call_args.kwargs["target"], "claude")
        self.assertEqual(
            self._run(antigravity_continue, []).call_args.kwargs["target"],
            "antigravity")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_bugs.CliWrapperTests -v`
Expected: `ModuleNotFoundError: No module named 'codex_continue'` at import.

- [ ] **Step 3: Implement**

Create `cli.py`:

```python
"""Shared argparse surface of the *_continue.py CLI wrappers.

Each wrapper is `cli.run("<target>", cli.build_parser("...").parse_args())`;
the flags and their semantics are identical for every target.
"""

from __future__ import annotations

import argparse

import sender


def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
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
    return p


def run(target: str, args: argparse.Namespace) -> None:
    every_s = args.every_hours * 3600 + args.every_minutes * 60
    sender.send_loop(
        target=target,
        message=args.message,
        initial_delay_s=args.hours * 3600 + args.minutes * 60,
        every_s=every_s if every_s > 0 else None,
        count=args.count,
    )
```

Create `codex_continue.py`:

```python
"""Send a message (default 'continue') to the Codex desktop app, optionally on repeat.

The composer is located via UI Automation (no keyboard shortcut is safe:
Escape stops a running turn) and focus is verified before typing.

Usage:
    python codex_continue.py                                # send now
    python codex_continue.py --hours 5                      # wait 5h, send
    python codex_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python codex_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python codex_continue.py --message "keep going"         # custom message
"""

import argparse

import cli


def parse_args() -> argparse.Namespace:
    return cli.build_parser(
        "Send a message to the Codex desktop app."
    ).parse_args()


def main() -> None:
    cli.run("codex", parse_args())


if __name__ == "__main__":
    main()
```

Rewrite `claude_continue.py` (keep its docstring) so the body is:

```python
import argparse

import cli


def parse_args() -> argparse.Namespace:
    return cli.build_parser("Send a message to Claude Desktop.").parse_args()


def main() -> None:
    cli.run("claude", parse_args())


if __name__ == "__main__":
    main()
```

Rewrite `antigravity_continue.py` the same way with
`cli.build_parser("Send a message to Google Antigravity IDE.")` and
`cli.run("antigravity", parse_args())`; fix its docstring line "Uses Ctrl+L to
focus the agent chat input" to "Focuses the agent input with Ctrl+I (Agent
Manager) or Ctrl+1, Ctrl+L (IDE window), verified via UI Automation."

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests`
Expected: all pass. Also `.venv\Scripts\python.exe codex_continue.py --help` prints the six flags.

---

### Task 5: GUI radio button

**Files:**
- Modify: `gui.py` (`_build_layout`, Target frame)
- Test: `tests/test_bugs.py`

- [ ] **Step 1: Write the failing test**

Append to `ValidateSettingsTests`:

```python
    def test_codex_target_accepted(self):
        s = self._settings(target="codex")
        self.assertIsNone(gui.validate_settings(s))
```

(`_settings` is the existing helper in that class; if it is named differently, use the class's existing settings builder.)

And a layout test that does not need a display:

```python
class GuiTargetChoicesTests(unittest.TestCase):
    def test_every_sender_target_has_a_radio_button(self):
        # The radio values are the TARGETS keys; a target without a button
        # can only be selected by editing settings.json.
        values = []
        fake_ttk = mock.Mock()
        fake_ttk.Radiobutton.side_effect = (
            lambda *_a, **kw: values.append(kw.get("value")) or mock.Mock())
        with mock.patch.object(gui, "ttk", fake_ttk), \
             mock.patch.object(gui, "tk", mock.Mock()):
            g = object.__new__(gui.ContinueSenderGUI)
            g.root = mock.Mock()
            for name in ("target_var", "message_var", "initial_hours_var",
                         "initial_minutes_var", "repeat_var", "every_hours_var",
                         "every_minutes_var", "count_var"):
                setattr(g, name, mock.Mock())
            g._build_layout()
        self.assertEqual(sorted(values), sorted(sender.TARGETS))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m unittest tests.test_bugs.GuiTargetChoicesTests -v`
Expected: FAIL, `['antigravity', 'claude'] != ['antigravity', 'claude', 'codex']`.

- [ ] **Step 3: Implement**

In `gui.py`, in the Target frame after the Antigravity radio button:

```python
        ttk.Radiobutton(frame_target, text="Codex",
                        variable=self.target_var, value="codex"
                        ).pack(side="left", padx=8, pady=4)
```

Update the module docstring's first line to "Tkinter control panel for claude_continue / antigravity_continue / codex_continue."

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests`
Expected: all pass.

---

### Task 6: `debug_windows.py` target argument and composer probe

> Historical: executed as written, then superseded by the "Amendments"
> section at the end (`prefer_largest`, full image path in both listings).

**Files:**
- Modify: `debug_windows.py`

- [ ] **Step 1: Rewrite the script**

```python
"""Print the windows sender.py would consider for a target and, for codex,
the composer element UI Automation finds. Read-only: nothing is activated,
clicked, or typed.

Usage: python debug_windows.py [claude|antigravity|codex]   (default antigravity)
"""

import sys

import sender

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _raw_matches(spec):
    """Every visible window of the target exe, without the size filter."""
    raw = []

    def enum_cb(h, _):
        if sender.user32.IsWindowVisible(h):
            exe = sender._get_process_name(h)
            if exe in spec.exe_names:
                n = sender.user32.GetWindowTextLengthW(h)
                title = ""
                if n > 0:
                    buf = sender.ctypes.create_unicode_buffer(n + 1)
                    sender.user32.GetWindowTextW(h, buf, n + 1)
                    title = buf.value
                _l, _t, w, hh = sender.get_window_rect(h)
                raw.append(f"HWND: {h}, Title: {title!r}, Size: {w}x{hh}")
        return True

    sender.user32.EnumWindows(sender.EnumWindowsProc(enum_cb), 0)
    return raw


def _probe_codex_composer(hwnd):
    handles = sender._uia()
    if handles is None:
        print("UIA unavailable (comtypes missing or COM failure).")
        return
    uia, mod = handles
    element = sender._find_codex_composer(uia, mod, hwnd)
    if element is None:
        print(f"No {sender.CODEX_COMPOSER_CLASS} edit element found in the window.")
        return
    print(f"Composer: name={element.CurrentName!r} class={element.CurrentClassName!r} "
          f"rect={sender._element_rect(element)} "
          f"focusable={bool(element.CurrentIsKeyboardFocusable)} "
          f"has_focus={bool(element.CurrentHasKeyboardFocus)}")


def main() -> None:
    key = sys.argv[1] if len(sys.argv) > 1 else "antigravity"
    if key not in sender.TARGETS:
        sys.exit(f"Unknown target {key!r}. Choices: {list(sender.TARGETS)}")
    spec = sender.TARGETS[key]
    windows = sender.find_target_windows(spec)
    print(f"Windows found for {spec.name}:")
    for hwnd, title in windows:
        _l, _t, w, h = sender.get_window_rect(hwnd)
        print(f"HWND: {hwnd}, Title: {title!r}, Rect: w={w} h={h}, "
              f"exe={sender._get_process_name(hwnd)!r}")
    if not windows:
        print("NO WINDOWS FOUND.")
        print(f"\nAll raw matches for {spec.exe_names}:")
        for line in _raw_matches(spec):
            print(line)
        return
    picked = sender._pick_main_window(windows)
    print(f"Would use: {picked}")
    if key == "codex" and picked is not None:
        _probe_codex_composer(picked[0])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it read-only against the live app**

Run: `.venv\Scripts\python.exe debug_windows.py codex`
Expected: one `ChatGPT` window from `chatgpt.exe`, "Would use: (hwnd, 'ChatGPT')", and a composer line with `class='ProseMirror'` and a non-empty rect. Run `.venv\Scripts\python.exe debug_windows.py` too: the Antigravity output is unchanged.

---

### Task 7: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the text**

- Intro: "Sends a text message (default `continue`) to **Claude Desktop**, **Google Antigravity IDE** or the **OpenAI Codex desktop app** ...".
- Entry points: add a "### Codex desktop app CLI" section with `python codex_continue.py [same flags as above]`.
- "Flags (both CLIs)" -> "Flags (all CLIs)".
- How it works, focus methods: add
  "The **Codex desktop app** (MSIX package `OpenAI.Codex`; its process is `ChatGPT.exe`, matched by exe name only because its window title `ChatGPT` also fits browser tabs) has no safe focus shortcut — `Shift+Esc` clears unreads and plain `Esc` stops a running turn. The composer is therefore located through UI Automation (the bottom-most `ProseMirror` edit element), focused with UIA `SetFocus`, verified, and clicked as a fallback. If it cannot be found or focused, the send is aborted."
- Files: add `codex_continue.py — CLI wrapper for the Codex desktop app`, `cli.py — shared argparse surface of the three CLI wrappers`, and describe `debug_windows.py [target] — read-only listing of candidate windows (and the Codex composer element)`.

---

### Amendments after the research and review passes (applied)

The tasks above were executed as written; two workflow passes (a
three-lens research sweep and a 31-finding adversarial review) then led to
these changes, all covered by tests in `tests/test_bugs.py`:

- `TargetSpec` gained `prefer_largest_window`, `exe_path_contains` and
  `message_problem`; the Codex spec sets all three (`("openai.codex",)`,
  `codex_message_problem`). `_get_process_path` keeps the full image path.
- `_uia()` tolerates `RPC_E_CHANGED_MODE`; `_focus_codex_composer` raises
  instead of clicking blind when `_uia()` is `None` (the geometric fallback
  and `CODEX_COMPOSER_BOTTOM_OFFSET_PX` are gone).
- `_find_codex_composer` treats a raising attempt like an empty one and
  skips elements that vanish mid-scan; it takes an optional `log`.
- `_is_element_focused` requires `CompareElements` (or same type, class and
  rectangle) **and** `CurrentHasKeyboardFocus`; `_wait_for_focus` polls it.
- `_focus_codex_composer` returns a `ComposerHandle(uia, mod, element)`
  after refusing a non-empty draft (`_composer_draft`); `_focus_input`
  returns it; `send_once` re-checks `_is_foreground`, `_is_element_focused`
  and `_verify_typed_text` (Value pattern read-back) before Enter.
- `gui.validate_settings` and `send_once` apply `spec.message_problem`.
- `debug_windows.py` prints the full image path (`_get_process_path`) per
  window in both listings and passes `prefer_largest=spec.prefer_largest_window`
  to `_pick_main_window`, so `Would use:` names the window `send_once` picks.
- Second review pass: `_verify_typed_text` keeps the last *readable* value
  (a readable mismatch stays sticky) and takes a `baseline` read before
  typing that the text must differ from; `_composer_text` maps a NULL Value
  to `None`; comtypes is imported on the main thread at `sender` import;
  new `FindTargetWindowsTests` drive `find_target_windows` end to end.

### Task 8: Verification

- [ ] **Step 1: Full test suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -v`
Expected: every test passes; count = 40 + new tests.

- [ ] **Step 2: Read-only live checks**

Run: `.venv\Scripts\python.exe debug_windows.py codex` — composer element reported.
Run: `.venv\Scripts\python.exe codex_continue.py --help` and `.venv\Scripts\python.exe claude_continue.py --help` — six flags each.

- [ ] **Step 3: Hand the live send to the user**

Do not run a real send (it would post into the user's current Codex thread). Report the command: `python codex_continue.py --message "continue"`.
