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
