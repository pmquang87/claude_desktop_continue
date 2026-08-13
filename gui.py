"""Tkinter control panel for claude_continue / antigravity_continue.

Drives sender.send_loop in a background thread. Communicates with the Tk main
loop through a queue.Queue polled every 100 ms.
"""

from __future__ import annotations

import json
import math
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


def _valid_setting(key: str, value) -> bool:
    """Type/finiteness check against the DEFAULTS schema. Bad values would
    otherwise crash the Tk variable constructors or, for NaN/Infinity (which
    json.loads accepts), survive validation and kill the worker later."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str)


def load_settings() -> tuple[dict, str | None]:
    """Return (settings, warning_or_None). Missing/corrupt file → defaults."""
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS), None
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # ValueError covers both JSONDecodeError and UnicodeDecodeError
        # (e.g. the file was hand-edited and saved as ANSI/cp1252).
        return dict(DEFAULTS), f"Could not read settings.json ({e}); using defaults."
    if not isinstance(data, dict):
        return dict(DEFAULTS), "settings.json is not a JSON object; using defaults."
    merged = dict(DEFAULTS)
    dropped = []
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        if _valid_setting(key, value):
            merged[key] = value
        else:
            dropped.append(key)
    if dropped:
        return merged, (
            f"Ignored invalid settings.json value(s) for: {', '.join(dropped)}."
        )
    return merged, None


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2), encoding="utf-8"
    )


def validate_settings(settings: dict) -> str | None:
    """Sanity-check parsed settings before saving/starting a run.
    Returns an error message, or None if everything is usable."""
    if settings["target"] not in sender.TARGETS:
        return f"Unknown target: {settings['target']}"
    if not settings["message"]:
        return "Message is empty."
    bad = sender.unsupported_chars(settings["message"])
    if bad:
        return (
            f"Message contains characters that cannot be typed and would be "
            f"silently dropped: {bad!r}. Use plain ASCII text."
        )
    numbers = (
        settings["initial_hours"], settings["initial_minutes"],
        settings["every_hours"], settings["every_minutes"],
    )
    # NaN fails every <= / >= comparison, so test finiteness explicitly.
    if any(not math.isfinite(v) or v < 0 for v in numbers):
        return "Delay and interval values must be finite and not negative."
    if settings["count"] < 0:
        return "Count must be 0 (= forever) or a positive number."
    if settings["repeat_enabled"]:
        every_s = settings["every_hours"] * 3600 + settings["every_minutes"] * 60
        if every_s <= 0:
            return "Repeat is enabled but interval is 0. Set hours/minutes."
    return None


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

        error = validate_settings(settings)
        if error:
            messagebox.showerror("Invalid input", error)
            return

        try:
            save_settings(settings)
        except OSError as e:
            self._append_log(f"Warning: could not save settings.json ({e}).")

        initial_s = (
            settings["initial_hours"] * 3600 + settings["initial_minutes"] * 60
        )
        every_s: float | None
        if settings["repeat_enabled"]:
            every_s = (
                settings["every_hours"] * 3600 + settings["every_minutes"] * 60
            )
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
        finally:
            # Always reschedule — a raising handler must not stop the pump
            # for the rest of the session.
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
