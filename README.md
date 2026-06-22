# abtop-py

[![Version](https://img.shields.io/badge/version-1.2.0-blue.svg)](CHANGELOG.md)

`abtop-py` is a single-file, dependency-free terminal monitor for local AI agent sessions. It tracks Claude Code and Codex CLI/Desktop activity by reading local process state, session files, transcripts, and rate-limit caches.

The program does not call provider APIs and does not require third-party Python packages.

## Features

- Live curses dashboard for Claude Code and Codex sessions.
- btop-style top status line with host CPU, memory, one-minute load average, attributed agent memory, context, and session counters.
- One-shot text output for scripts and terminals without TTY support.
- JSON snapshot mode for integrations.
- Token totals, context usage, token-rate history with a relative time axis, and dated session timelines.
- Tool-call and child-process visibility, including memory and listening ports.
- MCP server panel with parent/profile/activity columns when MCP processes are visible.
- Separate waiting labels for ordinary user input, between-step idle moments, and explicit user decisions.
- Project activity panel with one row per project, sorted by last activity, showing state, last update age, and token rate.
- Time-aware session table, chat header, and timeline headers with compact dates.
- Codex approval waits are marked as decisions, overlapping approval waits are collapsed, and queued commands are not shown as long-running work.
- Claude and Codex rate-limit display when local data is available.

## Dashboard Notes

The top header uses compact labels:

- `CPU` / `MEM` are host utilization sampled from the local machine.
- `L` is the one-minute load average.
- `agents Σ...` is memory attributed to visible agent process trees. `Σ-` means memory is unknown, not zero.
- `ctx%...` is the average non-zero context-window usage across visible sessions.

The `context` panel shows token rate as vertical bars. Its bottom axis is relative to `now` at the right edge and marks 30-second intervals, with the left edge labeled even when it is not a round 30-second boundary.

The `projects` panel is an activity view, not a Git summary. It shows one row per project:

```text
Project      State   Last   Tok/m
vaultui-py   Work      5s   13.0k
```

Rows are sorted by `Last`, newest first. `Tok/m` is the per-project active-token rate for the current refresh interval, scaled to tokens per minute.

`CHAT` and `TIMELINE` headers include compact dates such as `Jun 22 00:04`. Timeline rows still show durations, while the title shows the overall time range.

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

This writes `~/.claude/abtop-statusline.sh` and configures Claude Code `statusLine` so local Claude quota data can be exported to `abtop-rate-limits.json`.

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
- Host CPU, memory, and load average from `/proc` on Linux.

## Limitations

- It shows sessions that can be found in local files or running processes.
- Some process details may be unavailable without `/proc`, `ps`, or `lsof`.
- Codex Desktop/recent rollout sessions may not have a reliable owning process tree; their memory is shown as unknown (`-` / `Σ-`) rather than zero.
- Rate-limit data depends on local Claude StatusLine output or recent Codex rollout events.

## License

MIT License. See [LICENSE](LICENSE).

## Author

**Tarasov Dmitry**
- Email: dtarasov7@gmail.com

## Attribution
Parts of this code were generated with assistance
