#!/usr/bin/env bash
# Snapshot the fleet home with restic, plus a manifest of what was saved.
#
# Saved: tickets/*.json, settings.json, kaneo.json, notify.json,
# kaneo-map.json, transitions.jsonl (whichever exist) and the fleet.service unit file.
# Not saved: logs, locks, and anything else in the fleet home.
#
# Repository and password come from the environment or a password file that
# lives OUTSIDE the fleet home and is mode 0600:
#   FLEET_BACKUP_REPO / RESTIC_REPOSITORY               the restic repository
#   FLEET_BACKUP_PASSWORD_FILE / RESTIC_PASSWORD_FILE   file holding the password
#   RESTIC_PASSWORD                                     or the password itself
# Never put the password in this repo. The repository is initialised if empty.
# This script is not scheduled and no off-site target is chosen: it only
# makes one snapshot when you run it.
#
# manifest.json (stored in the snapshot, printed on success):
#   {"tickets": N, "ids_sha256": sha256 of the sorted ticket ids, one per line}
set -euo pipefail

die() { echo "fleet-backup: $*" >&2; exit 1; }

fleet_home="${FLEET_HOME:-$HOME/.fleet}"
restic_bin="${FLEET_RESTIC:-restic}"
unit="${FLEET_BACKUP_UNIT:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/fleet.service}"

command -v "$restic_bin" >/dev/null 2>&1 || die "restic is not installed ($restic_bin)"
# Backup runs from a staging folder, so every local path must be absolute.
if [[ "$restic_bin" == */* ]]; then
  restic_bin="$(cd "$(dirname "$restic_bin")" 2>/dev/null && pwd -P)/$(basename "$restic_bin")"
fi
[[ -d "$fleet_home" ]] || die "fleet home not found: $fleet_home"
fleet_home=$(cd "$fleet_home" && pwd -P)

repo="${FLEET_BACKUP_REPO:-${RESTIC_REPOSITORY:-}}"
[[ -n "$repo" ]] || die "set FLEET_BACKUP_REPO (or RESTIC_REPOSITORY) to a restic repository"
# A repository without a scheme/host prefix (sftp:, s3:, rest:, ...) is a path.
if [[ ! "$repo" =~ ^[A-Za-z][A-Za-z0-9+.-]+: && "$repo" != /* ]]; then
  repo="$PWD/$repo"
fi
export RESTIC_REPOSITORY="$repo"

pw_file="${FLEET_BACKUP_PASSWORD_FILE:-${RESTIC_PASSWORD_FILE:-}}"
unset RESTIC_PASSWORD_FILE
if [[ -n "$pw_file" ]]; then
  [[ -f "$pw_file" ]] || die "password file not found: $pw_file"
  pw_file=$(cd "$(dirname "$pw_file")" && pwd -P)/$(basename "$pw_file")
  pw_real="$pw_file"
  case "$pw_real" in
    "$fleet_home"/*) die "password file must be outside the fleet home" ;;
  esac
  [[ "$(stat -c %a "$pw_file")" == "600" ]] || die "password file must be mode 0600: $pw_file"
  export RESTIC_PASSWORD_FILE="$pw_file"
elif [[ -z "${RESTIC_PASSWORD:-}" ]]; then
  die "set FLEET_BACKUP_PASSWORD_FILE (0600, outside the fleet home) or RESTIC_PASSWORD"
fi

if [[ "$repo" == /* ]]; then
  mkdir -p "$repo"
  case "$(cd "$repo" && pwd -P)" in
    "$fleet_home"|"$fleet_home"/*) die "repository must be outside the fleet home" ;;
  esac
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
chmod 700 "$stage"

# Ticket ids are the file names. Skip the temporary files writers leave
# briefly (.tmp-*), which a plain glob does not match anyway.
ids=()
if [[ -d "$fleet_home/tickets" ]]; then
  mkdir "$stage/tickets"
  for f in "$fleet_home"/tickets/*.json; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f" .json)
    cp -p "$f" "$stage/tickets/$base.json"
    ids+=("$base")
  done
fi

for name in settings.json kaneo.json notify.json kaneo-map.json transitions.jsonl; do
  if [[ -f "$fleet_home/$name" ]]; then cp -p "$fleet_home/$name" "$stage/$name"; fi
done
if [[ -f "$unit" ]]; then
  cp -p "$unit" "$stage/fleet.service"
else
  echo "fleet-backup: note: no unit file at $unit" >&2
fi

count=${#ids[@]}
if (( count )); then
  sha=$(printf '%s\n' "${ids[@]}" | LC_ALL=C sort | sha256sum | cut -d' ' -f1)
else
  sha=$(printf '' | sha256sum | cut -d' ' -f1)
fi
printf '{"tickets": %d, "ids_sha256": "%s"}\n' "$count" "$sha" > "$stage/manifest.json"

if ! "$restic_bin" cat config >/dev/null 2>&1; then
  "$restic_bin" init >/dev/null || die "could not initialise the repository"
fi
(cd "$stage" && "$restic_bin" backup --quiet --tag fleet-backup --host fleet .) \
  || die "restic backup failed"

echo "fleet-backup: snapshot saved ($count tickets)"
cat "$stage/manifest.json"
