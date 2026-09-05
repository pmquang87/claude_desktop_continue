"""Send a message (default 'continue') to Google Antigravity IDE, optionally on repeat.

Focuses the agent input with Ctrl+I (Agent Manager window) or Ctrl+1 then
Ctrl+L (IDE editor window), verified via UI Automation.

Usage:
    python antigravity_continue.py                                # send now
    python antigravity_continue.py --hours 5                      # wait 5h, send
    python antigravity_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python antigravity_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python antigravity_continue.py --message "keep going"         # custom message
"""

import argparse

import cli


def parse_args() -> argparse.Namespace:
    return cli.build_parser(
        "Send a message to Google Antigravity IDE."
    ).parse_args()


def main() -> None:
    cli.run("antigravity", parse_args())


if __name__ == "__main__":
    main()
