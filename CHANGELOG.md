# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] - 2026-06-22

### Added

- Time-aware UI labels: `Last` in the sessions table, dated `CHAT` and `TIMELINE` headers, and month-name date formatting.
- Static relative time axis under the token-rate bar graph, with a labeled left edge and 30-second ticks toward `now`.
- Project activity panel with one row per project, sorted by last activity, showing aggregate state, last update age, session count markers, and per-project tokens/minute.
- Explicit Codex approval handling: approval-required tool calls are detected from escalated sandbox requests, shown as `Decide` / `Approve`, and grouped when multiple approval waits overlap.
- `Queued` timeline rows for commands that were recorded while an approval wait was still blocking execution.

### Changed

- The top header now displays unknown attributed agent memory as `Σ-` instead of misleading `Σ0M`.
- Quota data age is labeled as `upd Ns` instead of a bare `Ns ago`.
- Claude quota setup hint now points to `./abtop-py.py --setup`.
- Token-rate calculations use rolling values for numeric rates and per-tick values for graph bars so stale bursts do not keep drawing as live consumption.
- Completed Codex tool calls use explicit `Wall time` from tool output when available, avoiding user-approval wait time being counted as command runtime.
- Middle dashboard panels are one row taller to prevent quota reset labels from colliding with the token-rate footer.

### Documentation

- README files now describe top-header metrics, the token-rate time axis, project activity rows, dated chat/timeline headers, Codex approval handling, and memory attribution limits.
- PlantUML diagrams now include host metrics, time metadata, project activity aggregation, approval/queued timeline behavior, and the updated UI panel model.

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
