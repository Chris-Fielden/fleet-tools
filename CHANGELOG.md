# Changelog

All notable changes to this repository are recorded here, one section per ship.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are the `ship-YYYY.MM.DD.N` tags that mark known-good points for rollback.

## [Unreleased]

## [ship-2026.10.27.5] - 2026-10-27

### Changed

- OC-12: On-call: fix BD-143: Gauntlet integrity: no inferred side, baselines stay in copy

## [ship-2026.10.27.4] - 2026-10-27

### Changed

- BD-141: Cursor gauntlet: best 2 of 3

## [ship-2026.10.27.3] - 2026-10-27

### Changed

- BD-140: Cursor stress test: go or no-go

## [ship-2026.10.27.2] - 2026-10-27

### Changed

- BD-139: Cursor probe f: keep hook evidence

## [ship-2026.10.27.1] - 2026-10-27

### Changed

- BD-137: Cursor probe fix and fail-closed hook

## [ship-2026.10.26.3] - 2026-10-26

### Changed

- BD-136: Cursor reviewer with counted vote

## [ship-2026.10.26.2] - 2026-10-26

### Changed

- BD-129: Controller: handoff intake, ordering, ship ping

## [ship-2026.10.26.1] - 2026-10-26

### Changed

- BD-128: Auto-open rules and file-area locks for parallel work

## [ship-2026.10.25.8] - 2026-10-25

### Changed

- BD-127: Entry to review for human-built tickets

## [ship-2026.10.25.7] - 2026-10-25

### Changed

- BD-126: Wake the agent to rebase on current main before review

## [ship-2026.10.25.6] - 2026-10-25

### Changed

- BD-125: Revive failed ticket to review when the tip passes; OC uses current tip

## [ship-2026.10.25.5] - 2026-10-25

### Changed

- BD-124: Voters: add cursor kind, company-distinct vote data model

## [ship-2026.10.25.4] - 2026-10-25

### Changed

- BD-120: Cursor probe script (human-run)

## [ship-2026.10.25.3] - 2026-10-25

### Changed

- BD-123: Block agents launching Cursor (EM-35 carry)

## [ship-2026.10.25.2] - 2026-10-25

### Changed

- BD-121: Human-only fleet answer; deny herdr

## [ship-2026.10.25.1] - 2026-10-25

### Changed

- BD-119: Deny AskUserQuestion; agents pick safer option

## [ship-2026.10.16.9] - 2026-10-16

### Changed

- BD-106: Ship: timeouts on detached merge-job steps and age/command check on reservation

## [ship-2026.10.16.8] - 2026-10-16

### Changed

- OC-9: On-call: fix BD-105: One deduped ping per persistent warn-only stall condition

## [ship-2026.10.16.7] - 2026-10-16

### Changed

- EM-31: ship: no y/N prompt below high risk; keep typed id

## [ship-2026.10.16.6] - 2026-10-16

### Changed

- BD-114: Split long findings into several notes, do not cut at 4000

## [ship-2026.10.16.5] - 2026-10-16

### Changed

- BD-115: Fail an idle agent with no commit after 5 min, not 45

## [ship-2026.10.16.4] - 2026-10-16

### Changed

- EM-32: Stop CHANGELOG.md conflicts on every stacked ship

## [ship-2026.10.16.3] - 2026-10-16

### Changed

- BD-117: Rebuild BD-116: hold tickets with unmet needs

## [ship-2026.10.16.2] - 2026-10-16

### Changed

- BD-111: Read-only digest command for informational fleet events (last 24h)

## [ship-2026.10.16.1] - 2026-10-16

### Changed

- BD-110: Notify: delay review_unrecorded ping until still unrecorded after N cycles

## [ship-2026.10.15.5] - 2026-10-15

### Changed

- BD-104: Heartbeat checker verb plus systemd timer (merges ID-34 health-check)

## [ship-2026.10.15.4] - 2026-10-15

### Changed

- BD-112: Backup script: restic snapshot of fleet home with manifest

## [ship-2026.10.15.3] - 2026-10-15

### Changed

- BD-107: Docs: supervisor handoff format spec (no code)

## [ship-2026.10.15.2] - 2026-10-15

### Changed

- BD-108: Docs: rollback runbook (docs/rollback.md)

## [ship-2026.10.15.1] - 2026-10-15

### Changed

- BD-102: Incident template and decision log

