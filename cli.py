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
