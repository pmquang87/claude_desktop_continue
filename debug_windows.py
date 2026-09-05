"""Print the windows sender.py would consider for a target and, for the
UIA-composer targets, the composer element UI Automation finds. Read-only:
nothing is activated, clicked, or typed.

Usage: python debug_windows.py [claude|antigravity|codex|zcode]
       (default antigravity)
"""

import sys

import sender

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _raw_matches(spec):
    """Every visible window of the target exe, without the size filter."""
    raw = []

    def enum_cb(h, _):
        if sender.user32.IsWindowVisible(h):
            path = sender._get_process_path(h)
            exe = path.rsplit("\\", 1)[-1]
            if exe in spec.exe_names:
                n = sender.user32.GetWindowTextLengthW(h)
                title = ""
                if n > 0:
                    buf = sender.ctypes.create_unicode_buffer(n + 1)
                    sender.user32.GetWindowTextW(h, buf, n + 1)
                    title = buf.value
                _l, _t, w, hh = sender.get_window_rect(h)
                raw.append(f"HWND: {h}, Title: {title!r}, Size: {w}x{hh}, "
                           f"exe={path!r}")
        return True

    sender.user32.EnumWindows(sender.EnumWindowsProc(enum_cb), 0)
    return raw


def _probe_composer(spec, hwnd):
    """What sender's composer lookup finds in `hwnd` — the same FindAll the
    real send would run, but nothing is focused, clicked or typed."""
    handles = sender._uia()
    if handles is None:
        print("UIA unavailable (comtypes missing or COM failure).")
        return
    uia, mod = handles
    element = sender._find_composer(uia, mod, hwnd, spec.composer_class,
                                    log=print)
    if element is None:
        pinned = (f"{spec.composer_class} edit element"
                  if spec.composer_class else "edit element")
        print(f"No {pinned} found in the window after "
              f"{sender.COMPOSER_FIND_ATTEMPTS} attempts.")
        return
    handle = sender.ComposerHandle(uia, mod, element)
    print(f"Composer: name={element.CurrentName!r} "
          f"class={element.CurrentClassName!r} "
          f"rect={sender._element_rect(element)} "
          f"focusable={bool(element.CurrentIsKeyboardFocusable)} "
          f"has_focus={bool(element.CurrentHasKeyboardFocus)}")
    # The Value pattern is what the draft check and the post-typing read-back
    # use; an empty composer reads as the placeholder (Codex) or "\n" (ZCode).
    print(f"  value={sender._composer_text(handle)!r} "
          f"draft={sender._composer_draft(handle)!r}")


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
              f"exe={sender._get_process_path(hwnd)!r}")
    if not windows:
        print("NO WINDOWS FOUND.")
        print(f"\nAll raw matches for {spec.exe_names}:")
        for line in _raw_matches(spec):
            print(line)
        return
    picked = sender._pick_main_window(
        windows, prefer_largest=spec.prefer_largest_window
    )
    print(f"Would use: {picked}")
    if spec.focus_method == "uia_composer" and picked is not None:
        _probe_composer(spec, picked[0])


if __name__ == "__main__":
    main()
