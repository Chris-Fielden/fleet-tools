#!/usr/bin/env bash
# Prove the newest fleet backup can be restored. It never reads or writes the
# live fleet home. Run it by hand or from a timer.
#
# Steps: restic check --read-data-subset, restore the latest snapshot into a
# mktemp folder, run `ticket list` and `ticket show` there (FLEET_HOME = that
# folder), compare the restored manifest.json with the restored tickets, and
# require mode 600 on the restored config files (settings.json, kaneo.json,
# notify.json, kaneo-map.json, whichever exist). Then one line is appended to
# the log:   <UTC time> PASS|FAIL <short reason>
#
# Repository and password use the same variables as fleet-backup.sh
# (FLEET_BACKUP_REPO / RESTIC_REPOSITORY, FLEET_BACKUP_PASSWORD_FILE /
# RESTIC_PASSWORD_FILE, RESTIC_PASSWORD, FLEET_RESTIC). Restic output is never
# printed, because it can contain the repository address.
#
# Log: fleet-backup.sh names none, so the safer choice is a file outside the
# fleet home: FLEET_RESTORE_LOG, default
# ${XDG_STATE_HOME:-~/.local/state}/fleet/restore-test.log.
# ntfy: fleet-backup.sh names no topic, and this script does not read the live
# notify.json. Set FLEET_RESTORE_NTFY_URL to the full topic URL to be told;
# unset means no message. A message is sent only on FAIL, or when the log has
# no PASS in the last 8 days.
#
#   fleet-restore-test.sh              run the test
#   fleet-restore-test.sh --check-log  only look for a PASS in 8 days (for a
#                                      timer, so a test that stopped running
#                                      is noticed)
# Exit 0 on PASS, 1 on FAIL.
set -euo pipefail

restic_bin="${FLEET_RESTIC:-restic}"
subset="${FLEET_RESTORE_SUBSET:-10%}"
stale_days=8
log="${FLEET_RESTORE_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/fleet/restore-test.log}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ticket_bin="${FLEET_TICKET:-$here/../bin/ticket}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

notify() {
  [[ -n "${FLEET_RESTORE_NTFY_URL:-}" ]] || return 0
  curl -fsS -m 10 -H "Title: Fleet restore test" -d "$1" "$FLEET_RESTORE_NTFY_URL" >/dev/null 2>&1 \
    || echo "fleet-restore-test: warning: could not send the ntfy message" >&2
}

# Succeeds when the log has a PASS newer than $stale_days days.
recent_pass() {
  [[ -f "$log" ]] || return 1
  local cutoff t verdict
  cutoff=$(date -u -d "$stale_days days ago" +%Y-%m-%dT%H:%M:%SZ)
  while read -r t verdict _; do
    [[ "$verdict" == PASS && "$t" > "$cutoff" ]] && return 0
  done < "$log"
  return 1
}

if [[ "${1:-}" == "--check-log" ]]; then
  if recent_pass; then echo "fleet-restore-test: a PASS exists in the last $stale_days days"; exit 0; fi
  echo "fleet-restore-test: no PASS in the last $stale_days days" >&2
  notify "No restore-test PASS in the last $stale_days days"
  exit 1
fi
[[ $# -eq 0 ]] || { echo "usage: fleet-restore-test.sh [--check-log]" >&2; exit 2; }

record() {  # record PASS|FAIL reason
  mkdir -p "$(dirname "$log")"
  printf '%s %s %s\n' "$(now)" "$1" "$2" >> "$log"
}
fail() {
  record FAIL "$1"
  echo "fleet-restore-test: FAIL: $1" >&2
  notify "Restore test FAILED: $1"
  exit 1
}

# The live fleet home is never used; ticket commands get the restored copy.
unset FLEET_HOME

command -v "$restic_bin" >/dev/null 2>&1 || fail "restic is not installed"
repo="${FLEET_BACKUP_REPO:-${RESTIC_REPOSITORY:-}}"
[[ -n "$repo" ]] || fail "no repository set (FLEET_BACKUP_REPO)"
export RESTIC_REPOSITORY="$repo"
pw_file="${FLEET_BACKUP_PASSWORD_FILE:-${RESTIC_PASSWORD_FILE:-}}"
unset RESTIC_PASSWORD_FILE
if [[ -n "$pw_file" ]]; then
  [[ -f "$pw_file" ]] || fail "password file not found"
  [[ "$(stat -c %a "$pw_file")" == "600" ]] || fail "password file must be mode 0600"
  export RESTIC_PASSWORD_FILE="$pw_file"
elif [[ -z "${RESTIC_PASSWORD:-}" ]]; then
  fail "no password set (FLEET_BACKUP_PASSWORD_FILE or RESTIC_PASSWORD)"
fi

work=$(mktemp -d) || fail "cannot create a temporary folder"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work" || fail "cannot secure the temporary folder"
out="$work/out"
dest="$work/restore"
mkdir "$dest" || fail "cannot create the restore folder"

"$restic_bin" check --read-data-subset="$subset" >"$out" 2>&1 || fail "restic check failed"
"$restic_bin" restore latest --tag fleet-backup --host fleet --target "$dest" >"$out" 2>&1 \
  || fail "restic restore failed"

# restic may recreate the staging path under the target: locate the manifest.
mapfile -t found < <(find "$dest" -name manifest.json -type f)
(( ${#found[@]} == 1 )) || fail "snapshot has no single manifest.json"
root=$(dirname "${found[0]}")

python3 -I - "$root/manifest.json" "$root/tickets" <<'PY' || fail "manifest does not match the restored tickets"
import hashlib, json, os, sys
m = json.load(open(sys.argv[1]))
d = sys.argv[2]
ids = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and not f.startswith(".")) if os.path.isdir(d) else []
sha = hashlib.sha256("".join(i + "\n" for i in ids).encode()).hexdigest()
sys.exit(0 if m == {"tickets": len(ids), "ids_sha256": sha} else 1)
PY

for name in settings.json kaneo.json notify.json kaneo-map.json; do
  if [[ -f "$root/$name" && "$(stat -c %a "$root/$name")" != "600" ]]; then
    fail "restored $name is not mode 600"
  fi
done

list=$(FLEET_HOME="$root" "$ticket_bin" list --json 2>"$out") || fail "ticket list failed on the restored copy"
ids=$(printf '%s' "$list" | python3 -I -c 'import json,sys; print("\n".join(sorted(t["id"] for t in json.load(sys.stdin))))') \
  || fail "ticket list output is not valid"
want=""
for f in "$root"/tickets/*.json; do
  [[ -f "$f" ]] && want+="$(basename "$f" .json)"$'\n'
done
want=$(printf '%s' "$want" | LC_ALL=C sort)
[[ "$(printf '%s' "$ids" | LC_ALL=C sort)" == "$want" ]] || fail "ticket list differs from the manifest"
count=0
while IFS= read -r id; do
  [[ -n "$id" ]] || continue
  FLEET_HOME="$root" "$ticket_bin" show "$id" >/dev/null 2>"$out" || fail "ticket show failed for $id"
  count=$((count + 1))
done <<< "$ids"

record PASS "restored $count tickets"
echo "fleet-restore-test: PASS ($count tickets)"
