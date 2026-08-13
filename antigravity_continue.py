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
