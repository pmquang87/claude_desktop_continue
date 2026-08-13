import sender

spec = sender.TARGETS["antigravity"]
windows = sender.find_target_windows(spec)
print("Windows found:")
for hwnd, title in windows:
    left, top, w, h = sender.get_window_rect(hwnd)
    print(f"HWND: {hwnd}, Title: {title!r}, Rect: w={w} h={h}")

if not windows:
    print("NO WINDOWS FOUND.")
    
    # Try finding ALL windows for the executable without the size filter
    print("\nAll raw matches for antigravity.exe:")
    raw_matches = []
    def enum_cb(h, _):
        if sender.user32.IsWindowVisible(h):
            exe = sender._get_process_name(h)
            if exe in spec.exe_names:
                l = sender.user32.GetWindowTextLengthW(h)
                t = ""
                if l > 0:
                    buf = sender.ctypes.create_unicode_buffer(l+1)
                    sender.user32.GetWindowTextW(h, buf, l+1)
                    t = buf.value
                left, top, w, h_size = sender.get_window_rect(h)
                raw_matches.append(f"HWND: {h}, Title: {t!r}, Size: {w}x{h_size}")
        return True
    cb = sender.EnumWindowsProc(enum_cb)
    sender.user32.EnumWindows(cb, 0)
    for rm in raw_matches:
        print(rm)
