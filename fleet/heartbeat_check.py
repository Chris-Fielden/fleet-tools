"""`fleet heartbeat-check`: is the watching dispatcher alive and getting work done?

Run every 2 minutes by a systemd --user timer (installed together with the
boot service). It reads $FLEET_HOME/heartbeat.json (written by `dispatch
--watch`) and pings the existing high-priority `dispatcher_down` event when:

- the file is missing or unreadable;
- its pid is dead, or is not a `dispatch --watch`;
- the last finished cycle is older than 3x the watch interval the dispatcher
  recorded in the file (at least 360s);
- 3 or more cycles in a row ended in an error;
- tickets are waiting to launch (status `open`) but nothing has changed for 30 min.

Dedupe: the first alert goes at once, a repeat 30 minutes later, then hourly,
and one "recovered" ping when everything is fine again. State is kept in
$FLEET_HOME/heartbeat-check.json. If the checker itself fails it pings that and
exits 1, so the timer unit shows as failed. Pings carry reasons only, no ticket text.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from . import notify
from . import service as sv
from . import ticket as tk

STALE_CYCLE_SECONDS = 360          # 3 x the 120 s watch interval
ERROR_STREAK = 3
STALL_SECONDS = 30 * 60
FIRST_REPEAT_SECONDS = 30 * 60
REPEAT_SECONDS = 60 * 60


def _parse(value) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _work_owed() -> bool:
    return any(t.get("status") == "open" for t in tk.list_tickets())


def find_problems(now: float) -> list[tuple[str, str]]:
    """(stable id, message) for each reason the dispatcher looks unhealthy; empty
    when it is fine. Only the ids go in the dedupe key: messages carry ages and
    counts that change on every run."""
    path = tk.fleet_home() / "heartbeat.json"
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [("missing", "heartbeat.json is missing (the dispatcher never ran, or it was removed)")]
    except (OSError, ValueError):
        return [("unreadable", "heartbeat.json is unreadable")]
    if not isinstance(beat, dict):
        return [("unreadable", "heartbeat.json is unreadable")]
    problems: list[tuple[str, str]] = []
    pid = beat.get("pid")
    live = sv.dispatcher_pid()
    if not isinstance(pid, int) or live is None or live != pid:
        problems.append(("pid", "dispatch --watch is not running as the pid in heartbeat.json"))
    finished = _parse(beat.get("cycle_finished"))
    if finished is None:
        finished = _parse(beat.get("started_at"))   # no cycle has finished yet
    interval = beat.get("interval_seconds")
    limit = STALE_CYCLE_SECONDS
    if isinstance(interval, int) and not isinstance(interval, bool) and interval > 0:
        limit = max(limit, 3 * interval)
    if finished is None or now - finished > limit:
        age = "unknown" if finished is None else f"{int(now - finished)}s"
        problems.append(("stale", f"no cycle has finished for {age} (limit {limit}s)"))
    errors = beat.get("consecutive_errors")
    if isinstance(errors, int) and errors >= ERROR_STREAK:
        problems.append(("errors", f"{errors} cycles in a row failed (last error: {str(beat.get('last_error'))[:60]})"))
    progress = _parse(beat.get("progress_at"))
    if progress is not None and now - progress > STALL_SECONDS and _work_owed():
        problems.append(("stalled", f"work is waiting but nothing has changed for {int((now - progress) // 60)} min"))
    return problems


def _load_state() -> dict:
    try:
        data = json.loads((tk.fleet_home() / "heartbeat-check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    path = tk.fleet_home() / "heartbeat-check.json"
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tk._write_atomic(path, state)


def _due(state: dict, key: str, now: float) -> bool:
    if state.get("key") != key:
        return True
    last = state.get("last_alert")
    if not isinstance(last, (int, float)):
        return True
    wait = FIRST_REPEAT_SECONDS if state.get("alerts", 1) <= 1 else REPEAT_SECONDS
    return now - last >= wait


def run(now: float | None = None) -> int:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    problems = find_problems(now)
    state = _load_state()
    if problems:
        key = "|".join(sorted(i for i, _ in problems))
        text = "; ".join(m for _, m in problems)
        if _due(state, key, now):
            notify.send_alert("dispatcher_down", "Fleet dispatcher is unhealthy", text)
            same = state.get("key") == key
            _save_state({"key": key, "last_alert": now, "alerts": state.get("alerts", 0) + 1 if same else 1})
        print("heartbeat-check: " + text)
        return 1
    if state.get("key"):
        notify.send_alert("dispatcher_down", "Fleet dispatcher recovered", "The dispatcher heartbeat is healthy again.")
        _save_state({})
    print("heartbeat-check: ok")
    return 0


def main(argv=None) -> int:
    if tk.current_creator() != "human":
        print("fleet: refusing to run inside an agent; only a human runs heartbeat-check", file=sys.stderr)
        return 1
    try:
        return run()
    except Exception as exc:   # includes NotConfigured/NotifyError: say so, never fail silently
        name = type(exc).__name__
        print(f"heartbeat-check: could not run its checks ({name}: {exc})", file=sys.stderr)
        try:
            notify.send_alert("dispatcher_down", "Fleet heartbeat check failed",
                              f"heartbeat-check could not run its checks ({name}); the dispatcher is unmonitored.")
        except Exception:   # nothing more can be done from here; the exit code is the signal
            pass
        return 1
