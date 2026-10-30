"""The single settings file: $FLEET_HOME/settings.json.

Every personal value the fleet used to hard-code lives here, with a documented
default (DEFAULTS below; docs/settings-spec.md explains each key). Secrets do
not: the Kaneo API key stays in $FLEET_HOME/kaneo.json (mode 600).

For backward compatibility $FLEET_HOME/dispatch.json is still read, as the
older name of the same file; keys in settings.json win over it. Both files are
optional, and both are checked the same way: unknown keys or bad values are an
error with a plain message.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ticket as tk

DEFAULTS = {
    # Dispatcher
    "builder": "claude",
    "models": {"claude": "sonnet", "codex": None},
    "max_running": 1,
    "auto_investigate": True,
    "max_investigations_per_day": 3,
    "watch_interval_seconds": 120,
    "max_launches_per_day": 10,
    # Auto-open (dispatcher): a controller-filed proposed ticket opens by rule. Repos never
    # auto-opened, and the daily cap (the hourly cap is the agent ticket limit).
    "auto_open_deny_repos": [],
    "max_auto_open_per_day": 5,
    # When true, the edit guard refuses edits in a task worktree of a repo that has tickets unless
    # the worktree's branch belongs to a claimed or in-review ticket (see .claude/hooks/guard_edits.py).
    "require_ticket_for_edits": False,
    # A file-area lock lasts this many hours, renewed while its ticket's agent runs.
    "area_lease_hours": 6,
    # The daily caps (launches, on-call) reset at midnight in this IANA timezone.
    "timezone": "America/Los_Angeles",
    # Overnight queue: at this time (HH:MM, America/Los_Angeles) the prep pass
    # runs over the tickets in review, if it has not already run.
    "prep_time": "06:00",
    # From prep_time on, at most one prep pass starts per this many hours (0 turns it off).
    "prep_pass_gap_hours": 4,
    # A ship that sees no output from the merge script for this many seconds (it is waiting at a
    # prompt) is cancelled and pinged (0 turns it off).
    "ship_prompt_timeout_s": 900,
    # Wall-clock limit for each other step of a ship (review.sh, the gate) and for the merge
    # script as a whole (0 turns it off). A step past it is stopped and pinged.
    "ship_step_timeout_s": 3600,
    # A ship's reservation older than this many seconds no longer blocks the ticket (0 = no limit).
    "ship_reservation_max_age_s": 14400,
    # Where things live
    "projects_dir": "~/projects",
    "research_dir": "~/fleet-research",
    "herdr_workspace": "fleet",
    # Which backend holds the tickets. "local" is the JSON store in
    # $FLEET_HOME/tickets plus the optional one-way Kaneo mirror.
    "tracker": "local",
    # Ticket id prefixes: two capital letters each. Urgent tickets use
    # emergency_prefix whatever their type.
    "id_prefixes": dict(tk.TYPE_PREFIX),
    "emergency_prefix": tk.EMERGENCY_PREFIX,
    # On-call: its own reserved slot (one agent at a time) and its own daily
    # cap; these launches do not count against max_launches_per_day.
    "oncall_prefix": tk.ONCALL_PREFIX,
    "max_oncall_per_day": 3,
    # FAIL verdicts on different commits before a claimed ticket is "going in
    # circles" and truly fails.
    "fail_rounds_threshold": 8,
    # A saved review that is not on the ticket pings only when it is still unrecorded on this
    # many dispatcher cycles (1 pings on first sight).
    "unrecorded_ping_cycles": 3,
    # A claimed ticket's agent session whose context has passed this many tokens
    # at the end of a review round is replaced by a fresh one (0 turns it off).
    "fresh_session_tokens": 100000,
    # Usage governor (weekly plan usage, from the status line): at pause_weekly_pct no
    # agent of any kind is launched; at hard_stop_weekly_pct running agents are stopped
    # too. Both resume after the weekly reset. 0 turns a limit off.
    "pause_weekly_pct": 90,
    "hard_stop_weekly_pct": 95,
    # Non-secret Kaneo values. null means "use what kaneo.json says". The API
    # key and the url (which decides where the key is sent) stay in kaneo.json.
    "kaneo": {"workspace_id": None, "project_icon": "Layout"},
}
MIN_WATCH_INTERVAL = 30
TRACKERS = ("local",)  # keep in step with tracker.BACKENDS (a test checks)
PREFIX_RE = re.compile(r"[A-Z]{2}")
PREP_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class SettingsError(Exception):
    pass


def settings_path() -> Path:
    return tk.fleet_home() / "settings.json"


def legacy_settings_path() -> Path:
    return tk.fleet_home() / "dispatch.json"


_ABSENT = object()


def _read(path: Path):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _ABSENT
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SettingsError(f"{path} is not valid JSON: {exc}") from None


def load_settings() -> dict:
    """Defaults, overridden by dispatch.json, then by settings.json."""
    settings = copy.deepcopy(DEFAULTS)
    for path in (legacy_settings_path(), settings_path()):
        given = _read(path)
        if given is not _ABSENT:
            _merge(settings, given, path)
    for key in ("projects_dir", "research_dir"):
        try:
            settings[key] = Path(settings[key]).expanduser().absolute()
        except RuntimeError:
            raise SettingsError(f"{key} '{settings[key]}' starts with ~ "
                                "but that home folder can't be found") from None
    return settings


def _whole(path, given, key, minimum, what):
    n = given[key]
    if isinstance(n, bool) or not isinstance(n, int) or n < minimum:
        raise SettingsError(f"{path}: {key} must be a whole number, {what}")
    return n


def _merge(settings: dict, given, path: Path) -> None:
    if not isinstance(given, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    if "cursor_review" in given:   # Cursor was removed from the fleet: accept the old key, ignore it
        given = {k: v for k, v in given.items() if k != "cursor_review"}
        print(f"warning: {path}: cursor_review is no longer used; you can delete it", file=sys.stderr)
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise SettingsError(f"{path}: unknown setting(s): {', '.join(unknown)}")
    if "builder" in given:
        if given["builder"] not in tk.AGENTS:
            raise SettingsError(f"{path}: builder must be one of: {', '.join(tk.AGENTS)}")
        settings["builder"] = given["builder"]
    if "models" in given:
        models = given["models"]
        if not isinstance(models, dict):
            raise SettingsError(f"{path}: models must be an object like {{\"claude\": \"sonnet\"}}")
        for agent, model in models.items():
            if agent not in tk.AGENTS:
                raise SettingsError(f"{path}: models has unknown agent '{agent}'")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise SettingsError(f"{path}: the model for {agent} must be a name or null")
            settings["models"][agent] = model
    if "max_running" in given:
        settings["max_running"] = _whole(path, given, "max_running", 1, "1 or more")
    if "auto_investigate" in given:
        if not isinstance(given["auto_investigate"], bool):
            raise SettingsError(f"{path}: auto_investigate must be true or false")
        settings["auto_investigate"] = given["auto_investigate"]
    if "max_investigations_per_day" in given:
        settings["max_investigations_per_day"] = _whole(
            path, given, "max_investigations_per_day", 0, "0 or more")
    if "watch_interval_seconds" in given:
        settings["watch_interval_seconds"] = _whole(
            path, given, "watch_interval_seconds", MIN_WATCH_INTERVAL, f"{MIN_WATCH_INTERVAL} or more")
    if "max_launches_per_day" in given:
        settings["max_launches_per_day"] = _whole(path, given, "max_launches_per_day", 0, "0 or more")
    if "auto_open_deny_repos" in given:
        repos = given["auto_open_deny_repos"]
        if not isinstance(repos, list) or not all(isinstance(r, str) and tk.REPO_RE.fullmatch(r) for r in repos):
            raise SettingsError(f"{path}: auto_open_deny_repos must be a list of repo names")
        settings["auto_open_deny_repos"] = list(repos)
    if "max_auto_open_per_day" in given:
        settings["max_auto_open_per_day"] = _whole(path, given, "max_auto_open_per_day", 0,
                                                   "0 or more (0 turns auto-open off)")
    if "require_ticket_for_edits" in given:
        if not isinstance(given["require_ticket_for_edits"], bool):
            raise SettingsError(f"{path}: require_ticket_for_edits must be true or false")
        settings["require_ticket_for_edits"] = given["require_ticket_for_edits"]
    if "area_lease_hours" in given:
        settings["area_lease_hours"] = _whole(path, given, "area_lease_hours", 1, "1 or more")
    if "timezone" in given:
        value = given["timezone"]
        try:
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError, OSError):
            raise SettingsError(f"{path}: timezone must be an IANA timezone name like "
                                "\"America/Los_Angeles\"") from None
        settings["timezone"] = value
    if "prep_time" in given:
        value = given["prep_time"]
        if not isinstance(value, str) or not PREP_TIME_RE.fullmatch(value):
            raise SettingsError(f"{path}: prep_time must be a time like \"06:00\" (24 hours, HH:MM)")
        settings["prep_time"] = value
    if "prep_pass_gap_hours" in given:
        settings["prep_pass_gap_hours"] = _whole(path, given, "prep_pass_gap_hours", 0, "0 or more (0 turns it off)")
    if "ship_prompt_timeout_s" in given:
        settings["ship_prompt_timeout_s"] = _whole(path, given, "ship_prompt_timeout_s", 0,
                                                   "0 or more (0 turns it off)")
    for key in ("ship_step_timeout_s", "ship_reservation_max_age_s"):
        if key in given:
            settings[key] = _whole(path, given, key, 0, "0 or more (0 turns it off)")
    for key in ("projects_dir", "research_dir", "herdr_workspace"):
        if key in given:
            if not isinstance(given[key], str) or not given[key].strip():
                raise SettingsError(f"{path}: {key} must be a non-empty string")
            settings[key] = given[key]
    if "tracker" in given:
        if given["tracker"] not in TRACKERS:
            raise SettingsError(f"{path}: tracker must be one of: {', '.join(TRACKERS)}")
        settings["tracker"] = given["tracker"]
    if "id_prefixes" in given:
        prefixes = given["id_prefixes"]
        if not isinstance(prefixes, dict):
            raise SettingsError(f"{path}: id_prefixes must be an object like {{\"build\": \"BD\"}}")
        for kind, prefix in prefixes.items():
            if kind not in tk.TYPES:
                raise SettingsError(f"{path}: id_prefixes has unknown ticket type '{kind}'")
            if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
                raise SettingsError(f"{path}: the id prefix for {kind} must be two capital letters")
            settings["id_prefixes"][kind] = prefix
    if "emergency_prefix" in given:
        if not isinstance(given["emergency_prefix"], str) or not PREFIX_RE.fullmatch(given["emergency_prefix"]):
            raise SettingsError(f"{path}: emergency_prefix must be two capital letters")
        settings["emergency_prefix"] = given["emergency_prefix"]
    if "oncall_prefix" in given:
        if not isinstance(given["oncall_prefix"], str) or not PREFIX_RE.fullmatch(given["oncall_prefix"]):
            raise SettingsError(f"{path}: oncall_prefix must be two capital letters")
        settings["oncall_prefix"] = given["oncall_prefix"]
    if "max_oncall_per_day" in given:
        settings["max_oncall_per_day"] = _whole(path, given, "max_oncall_per_day", 0, "0 or more")
    if "fail_rounds_threshold" in given:
        settings["fail_rounds_threshold"] = _whole(path, given, "fail_rounds_threshold", 1, "1 or more")
        if settings["fail_rounds_threshold"] > tk.FAIL_COMMITS_MAX:   # only that many FAIL commits are kept
            raise SettingsError(f"{path}: fail_rounds_threshold must be {tk.FAIL_COMMITS_MAX} or less")
    if "unrecorded_ping_cycles" in given:
        settings["unrecorded_ping_cycles"] = _whole(path, given, "unrecorded_ping_cycles", 1, "1 or more")
    if "fresh_session_tokens" in given:
        settings["fresh_session_tokens"] = _whole(path, given, "fresh_session_tokens", 0,
                                                  "0 or more (0 turns it off)")
    for key in ("pause_weekly_pct", "hard_stop_weekly_pct"):
        if key in given:
            settings[key] = _whole(path, given, key, 0, "0 to 100 (0 turns it off)")
            if settings[key] > 100:
                raise SettingsError(f"{path}: {key} must be a whole number, 0 to 100 (0 turns it off)")
    used = list(settings["id_prefixes"].values()) + [settings["emergency_prefix"], settings["oncall_prefix"]]
    if len(set(used)) != len(used):
        raise SettingsError(f"{path}: id prefixes must differ from each other and from emergency_prefix and oncall_prefix")
    if "kaneo" in given:
        k = given["kaneo"]
        if not isinstance(k, dict):
            raise SettingsError(f"{path}: kaneo must be an object")
        extra = sorted(set(k) - set(DEFAULTS["kaneo"]))
        if extra:
            raise SettingsError(f"{path}: unknown kaneo setting(s): {', '.join(extra)} "
                                "(the API key and url belong in kaneo.json, not here)")
        for name, value in k.items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise SettingsError(f"{path}: kaneo.{name} must be a non-empty string or null")
            settings["kaneo"][name] = value
