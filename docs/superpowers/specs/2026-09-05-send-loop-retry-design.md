# Send loop: survive momentary refusals in repeat runs

Date: 2026-09-05. Applies to all targets (Claude Desktop, Antigravity IDE,
Codex desktop app).

## Context

`sender.send_loop` treated every exception from `send_once` as terminal:
`[ERROR]` log line, `{"type": "done", "reason": "error"}`, return. That was
the original design (`2026-08-07-continue-sender-gui-design.md`). The Codex
target (`2026-09-05-codex-target-design.md`, "Risks") added refusal
conditions that are often momentary: a draft already in the composer, the
window losing the foreground while typing, the composer element losing
focus, the Value-pattern read-back not showing the message, UI Automation
temporarily unavailable. An unattended overnight run stopped at the first
one and the user found it idle in the morning.

## Decision

Unattended **repeat runs** survive such refusals; **single sends** do not.

- A repeat run is any run with an interval (`every_s` set and `> 0`),
  whatever the count: the interval is the retry cadence, the count only
  limits successes. (First draft treated count 1 with an interval as a
  single send; the user asked for it to retry too, same day.)
- Errors are split into two classes:
  - `sender.ConfigurationError` (a `ValueError`): unknown target, unknown
    focus method, a message `pyautogui.typewrite` would corrupt
    (`unsupported_chars`), a message the target's `message_problem`
    refuses (Codex `/` and `@`). Retrying cannot help; every run ends at
    once with `done/error`, as before.
  - Everything else raised by `send_once` (window not found, activation
    failed, focus refused or lost, draft in the composer, read-back
    mismatch, UI Automation unavailable, COM errors) is momentary.
- `MAX_CONSECUTIVE_FAILURES = 3` (module constant). A success resets the
  streak. Failed sends do not count towards `count`.

## Behaviour of a repeat run on a momentary failure

1. `[ERROR] <message>` log line (unchanged).
2. New event `{"type": "send_failed", "error": str, "consecutive": k,
   "limit": MAX_CONSECUTIVE_FAILURES}`.
3. If `k >= limit`: `[ERROR] k sends failed in a row; giving up.` and
   `done/error`.
4. Otherwise `[INFO] Send failed (k/limit in a row); trying again at the
   next interval.`, then the normal interval wait (status/countdown events;
   Stop ends the run with `done/stopped`), then the next attempt.

Single sends (`every_s` None) keep today's behaviour: `[ERROR]` plus
`done/error` at the first failure, no `send_failed` event.

## GUI

- New `Failed: n` label under `Sent: n`; both reset on Start.
- `send_failed` bumps the counter and sets the status line to
  `send failed (k/limit in a row); retrying after the interval`. The
  countdown then overwrites it as usual. The `[ERROR]`/`[INFO]` lines
  reach the log pane as `log` events; the GUI adds no line of its own.
- `done/error` still re-enables the inputs.

## Implementation notes

- The countdown wait moved from a closure to the module-level
  `_countdown_wait(seconds, label, stop_event, emit)` so tests can replace
  it and assert that a failure waits a full interval before the retry.
- CLI wrappers pass no `on_event`; their output gains the `[INFO]` retry
  line and the `giving up` line, nothing else.

## Tests (`tests/test_bugs.py`)

`SendLoopTests`: momentary failure then success continues the run and
does not count the failure; `MAX_CONSECUTIVE_FAILURES` failures in a row
end it with `done/error` and no wait after the last one; a success resets
the streak; a `ConfigurationError` ends a repeat run at once; count 1
with an interval retries too; single sends (`every_s` None, any count) end
at once; Stop during the retry wait ends with `done/stopped`.
`ConfigurationErrorTests`: `send_once` raises `ConfigurationError` for an
unknown target, an untypeable message and a refused message, and a plain
`RuntimeError` for "not running". `SendFailedEventTests`: the GUI handler
updates the failed counter and status line and leaves the sent counter
alone.

## Non-goals

- No back-off or shorter retry interval: the next attempt is at the next
  regular interval.
- No per-target or configurable failure limit.
- No retry for single sends (runs without an interval).
