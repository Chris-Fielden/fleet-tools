# Changelog

All notable changes to this repository are recorded here, one section per ship.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are the `ship-YYYY.MM.DD.N` tags that mark known-good points for rollback.

## [Unreleased]

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

