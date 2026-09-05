"""Send a message (default 'continue') to the ZCode desktop app, optionally on repeat.

The composer is located via UI Automation (ZCode ships no shortcut that
focuses it, and Ctrl+W would close the window) and focus is verified before
typing.

Usage:
    python zcode_continue.py                                # send now
    python zcode_continue.py --hours 5                      # wait 5h, send
    python zcode_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python zcode_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python zcode_continue.py --message "keep going"         # custom message
"""

import argparse

import cli


def parse_args() -> argparse.Namespace:
    return cli.build_parser(
        "Send a message to the ZCode desktop app."
    ).parse_args()


def main() -> None:
    cli.run("zcode", parse_args())


if __name__ == "__main__":
    main()
