#!/usr/bin/env bash
# Tag a shipped change and record it in CHANGELOG.md. Run by finish-task.sh on the
# base branch, only after the post-merge checks have passed and before the push.
#
# Usage:  scripts/release-tag.sh <ticket-id-or-task-name> [title]
#
# Tag scheme: ship-YYYY.MM.DD.N  (UTC date; N counts that day's ships from 1).
# Not SemVer: a ship is a deploy point, not an API release, so there is no
# honest major/minor/patch to pick. The date says when, N keeps ships on the
# same day apart. List in ship order with `git tag -l --sort=version:refname 'ship-*'`
# (plain name order puts .10 before .2).
# Each tag is annotated with the ticket and the commit, and marks a known-good
# point: roll back by deploying that tag or `git revert -m 1` the merge after it.
#
# CHANGELOG.md follows Keep a Changelog: newest section first, below Unreleased.
set -euo pipefail

ref="${1:-}"
title="${2:-}"
[[ -n "$ref" ]] || { echo "Usage: scripts/release-tag.sh <ticket-id-or-task-name> [title]"; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "release-tag: the working tree is not clean."; exit 1; }
branch="$(git branch --show-current)"
[[ -n "$branch" ]] || { echo "release-tag: HEAD is detached."; exit 1; }

today="${RELEASE_DATE:-$(date -u +%Y.%m.%d)}"
n=1
while git rev-parse -q --verify "refs/tags/ship-$today.$n" >/dev/null; do n=$((n + 1)); done
tag="ship-$today.$n"
iso="${today//./-}"

python3 -I - "$ref" "$title" "$tag" "$iso" <<'PY'
import pathlib, re, sys
ref, title, tag, iso = sys.argv[1:5]
title = " ".join(title.split())
entry = f"- {ref}" + (f": {title}" if title else "") + "\n"
head = ("# Changelog\n\nAll notable changes to this repository are recorded here, one section per ship.\n"
        "The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n"
        "Versions are the `ship-YYYY.MM.DD.N` tags that mark known-good points for rollback.\n\n"
        "## [Unreleased]\n\n")
path = pathlib.Path("CHANGELOG.md")
text = path.read_text() if path.exists() else head
if not re.search(r"^## \[Unreleased\]", text, re.M):
    text = text.rstrip("\n") + "\n\n## [Unreleased]\n\n"
section = f"## [{tag}] - {iso}\n\n### Changed\n\n{entry}\n"
m = re.search(r"^## \[(?!Unreleased\])", text, re.M)
if m:
    at = m.start()
else:
    text = text.rstrip("\n") + "\n\n"
    at = len(text)
path.write_text(text[:at] + section + text[at:])
PY

who_name="$(git config user.name || echo fleet)"
who_mail="$(git config user.email || echo fleet@localhost)"
git add CHANGELOG.md
git -c user.name="$who_name" -c user.email="$who_mail" commit -q -m "Release $tag: $ref"
git -c user.name="$who_name" -c user.email="$who_mail" tag -a "$tag" -m "$tag: $ref${title:+ - $title}

ticket: $ref
shipped: $(git rev-parse HEAD~1)
branch: $branch"
echo "Tagged $tag ($ref) and added it to CHANGELOG.md."
