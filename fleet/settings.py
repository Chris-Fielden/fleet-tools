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
from pathlib import Path

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
    # Non-secret Kaneo values. null means "use what kaneo.json says". The API
    # key and the url (which decides where the key is sent) stay in kaneo.json.
    "kaneo": {"workspace_id": None, "project_icon": "Layout"},
}
MIN_WATCH_INTERVAL = 30
TRACKERS = ("local",)  # keep in step with tracker.BACKENDS (a test checks)
PREFIX_RE = re.compile(r"[A-Z]{2}")


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
