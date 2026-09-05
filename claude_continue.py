"""Send a message (default 'continue') to Claude Desktop, optionally on repeat.

Usage:
    python claude_continue.py                                # send now
    python claude_continue.py --hours 5                      # wait 5h, send
    python claude_continue.py --hours 5 --every-hours 5      # then repeat every 5h
    python claude_continue.py --every-minutes 30 --count 4   # send 4 times, 30 min apart
    python claude_continue.py --message "keep going"         # custom message
"""

import argparse

import cli


def parse_args() -> argparse.Namespace:
    return cli.build_parser("Send a message to Claude Desktop.").parse_args()


def main() -> None:
    cli.run("claude", parse_args())


if __name__ == "__main__":
    main()
