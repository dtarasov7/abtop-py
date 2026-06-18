# abtop-py

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](CHANGELOG.md)

`abtop-py` is a single-file, dependency-free terminal monitor for local AI agent sessions. It tracks Claude Code and Codex CLI/Desktop activity by reading local process state, session files, transcripts, and rate-limit caches.

The program does not call provider APIs and does not require third-party Python packages.

## Features

- Live curses dashboard for Claude Code and Codex sessions.
- One-shot text output for scripts and terminals without TTY support.
- JSON snapshot mode for integrations.
- Token totals, context usage, token-rate history, and session timelines.
- Tool-call and child-process visibility, including memory and listening ports.
- MCP server panel with parent/profile/activity columns when MCP processes are visible.
- Separate waiting labels for ordinary user input and explicit user decisions.
- Git branch and working-tree counters for active projects.
- Claude and Codex rate-limit display when local data is available.

## Requirements

- Python 3.8 or newer.
- Linux gives the richest process and port data through `/proc`.
- Other Unix-like systems can fall back to `ps` and `lsof` when available.

## Usage

Run the interactive dashboard:

```bash
./abtop-py.py
```

Print one text snapshot and exit:

```bash
./abtop-py.py --once
```

Print one JSON snapshot and exit:

```bash
./abtop-py.py --json
```

Install the Claude StatusLine helper used for Claude rate-limit collection:

```bash
./abtop-py.py --setup
```

## Options

```text
--once                         print one text snapshot and exit
--json                         print one JSON snapshot and exit
--interval SECONDS             refresh interval, default 2.0
--hide claude|codex            hide an agent type, can be repeated
--claude-config-dir PATH       add a Claude config root
--setup                        install Claude StatusLine rate-limit helper
--version                      print version and exit
```

## Configuration

Optional configuration is read from:

```text
~/.config/abtop/config.toml
```

Supported simple array keys:

```toml
hidden_agents = ["codex"]
claude_config_dirs = ["~/.claude-work"]
```

`hidden_agents` accepts `claude` and `codex`. `claude_config_dirs` adds extra Claude Code configuration roots to scan.

## Data Sources

`abtop-py` reads only local data:

- Claude Code session metadata from `.claude*/sessions/*.json`.
- Claude Code transcripts from `.claude*/projects/**/*.jsonl`.
- Codex rollout logs from `.codex/sessions/**/rollout-*.jsonl`.
- Process, memory, child process, and port data from the operating system.
- MCP server hints derived from visible child process command lines.
- Git state from the session working directories.

## Limitations

- It shows sessions that can be found in local files or running processes.
- Some process details may be unavailable without `/proc`, `ps`, or `lsof`.
- Rate-limit data depends on local Claude StatusLine output or recent Codex rollout events.

## License

MIT License. See [LICENSE](LICENSE).

## Author

**Tarasov Dmitry**
- Email: dtarasov7@gmail.com

## Attribution
Parts of this code were generated with assistance
