# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-06-19

### Added

- btop-style top status line in `abtop-py` with host CPU, memory, load average, aggregate agent memory, average context usage, time, and session counters.
- Codex between-step waiting label to avoid showing `waiting for input` during short idle gaps after assistant messages.

## [1.0.0] - 2026-06-18

### Added

- Initial public release of `abtop-py`.
- Dependency-free single-file Python monitor for Claude Code and Codex CLI/Desktop sessions.
- Interactive curses dashboard with context, quota, token, project, port, MCP server, session, chat, subagent, and timeline views.
- One-shot text snapshot mode with `--once`.
- JSON snapshot mode with `--json`.
- Claude StatusLine helper installation with `--setup`.
- Local-only collection from process state, session files, transcripts, rollout logs, Git state, and rate-limit cache files.
- Waiting-state distinction between ordinary user input and explicit user decisions or confirmations.
- English and Russian README files.
- PlantUML architecture diagrams in English and Russian.
