#!/usr/bin/env python3
"""
abtop-py.py - a single-file, dependency-free AI agent monitor.

This is a compact Python 3.8 implementation inspired by abtop. It reads only
local process and filesystem state:

- Claude Code sessions from ~/.claude*/sessions/*.json and transcript JSONL.
- Codex CLI/Desktop sessions from ~/.codex/sessions/**/rollout-*.jsonl.
- Process memory, child processes, listening ports, git status, and rate limits.

It intentionally avoids API calls and external Python packages. On Linux it uses
/proc directly; on other Unix-like systems it falls back to ps/lsof where
available.
"""

from __future__ import annotations

import argparse
import curses
import datetime as _dt
import glob
import json
import locale
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


APP_NAME = "abtop-py"
__VERSION__ = "1.2.0"
__AUTHOR__ = "Tarasov Dmitry"

DEFAULT_INTERVAL = 2.0
MAX_CHAT_MESSAGES = 12
MAX_HISTORY = 10000
MAX_LINE_BYTES = 10 * 1024 * 1024
RECENT_CODEX_SECONDS = 10 * 60
CODEX_BETWEEN_STEPS_GRACE_MS = 30 * 1000
WAIT_REASON_USER_INPUT = "user_input"
WAIT_REASON_USER_DECISION = "user_decision"
WAIT_REASON_BETWEEN_STEPS = "between_steps"
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class ProcessInfo:
    """Process metadata (PID, parent PID, RSS, CPU%, command).

    Рус: Метаданные процесса (PID, родительский PID, RSS, CPU%, команда).
    """
    pid: int
    ppid: int
    rss_kb: int
    cpu_pct: float
    command: str


@dataclass
class ChildProcess:
    """Child process info with optional listening port.

    Рус: Информация о дочернем процессе с опциональным портом прослушивания.
    """
    pid: int
    command: str
    mem_kb: int
    port: Optional[int] = None


@dataclass
class ToolCall:
    """AI tool invocation record (name, arguments, timing).

    Рус: Запись вызова инструмента ИИ (имя, аргументы, время выполнения).
    """
    name: str
    arg: str = ""
    duration_ms: int = 0
    call_id: str = ""
    started_ms: int = 0
    completed_ms: int = 0
    needs_approval: bool = False


@dataclass
class ChatMessage:
    """Chat message with role (user/assistant) and text content.

    Рус: Сообщение чата с ролью (пользователь/ассистент) и текстом.
    """
    role: str
    text: str
    timestamp_ms: int = 0


@dataclass
class RateLimitInfo:
    """API rate limit tracking (source, usage percentages, reset times).

    Рус: Отслеживание ограничений API (источник, проценты использования, время сброса).
    """
    source: str
    five_hour_pct: Optional[float] = None
    five_hour_resets_at: Optional[int] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets_at: Optional[int] = None
    updated_at: Optional[int] = None


@dataclass
class HostMetrics:
    """Lightweight host vitals for the top status line.

    Рус: Легкие показатели хоста для верхней строки статуса.
    """
    cpu_pct: float
    mem_pct: float
    load1: float


@dataclass
class ProjectActivity:
    """Aggregated activity row for one project.

    Рус: Агрегированная строка активности для одного проекта.
    """
    name: str
    status: str
    wait_reason: str
    last_activity_ms: int
    token_rate: float
    session_count: int


@dataclass
class AgentSession:
    """Full session for an AI agent run (tokens, tasks, context, children).

    Рус: Полный сеанс запуска ИИ-агента (токены, задачи, контекст, дочерние процессы).
    """
    agent_cli: str
    pid: int
    session_id: str
    cwd: str
    project_name: str
    started_at: int
    status: str
    wait_reason: str = ""
    model: str = "-"
    effort: str = ""
    context_percent: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_create: int = 0
    turn_count: int = 0
    current_tasks: List[str] = field(default_factory=list)
    mem_mb: int = 0
    version: str = ""
    git_branch: str = ""
    git_added: int = 0
    git_modified: int = 0
    token_history: List[int] = field(default_factory=list)
    context_history: List[int] = field(default_factory=list)
    compaction_count: int = 0
    context_window: int = 0
    children: List[ChildProcess] = field(default_factory=list)
    initial_prompt: str = ""
    first_assistant_text: str = ""
    chat_messages: List[ChatMessage] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    pending_since_ms: int = 0
    thinking_since_ms: int = 0
    last_activity_ms: int = 0
    config_root: str = ""

    def total_tokens(self) -> int:
        """Sum of all token categories (input, output, cache read, cache create).

        Рус: Сумма всех категорий токенов (вход, выход, чтение кэша, создание кэша).

        Returns:
            Total token count across all categories. / Общее количество токенов.
        """
        return (
            self.total_input_tokens
            + self.total_output_tokens
            + self.total_cache_read
            + self.total_cache_create
        )

    def active_tokens(self) -> int:
        """Input + output + cache-create tokens (excludes cache-read).

        Рус: Вход + выход + создание кэша (исключает чтение кэша).

        Returns:
            Count of active (non-reused) tokens. / Количество активных (непереиспользованных) токенов.
        """
        return self.total_input_tokens + self.total_output_tokens + self.total_cache_create

    def elapsed_seconds(self) -> int:
        """Seconds since session started (from ms timestamp).

        Рус: Секунды с начала сеанса (из временной метки в мс).

        Returns:
            Elapsed seconds, or 0 if started_at is invalid. / Прошедшие секунды, или 0 если started_at некорректен.
        """
        if self.started_at <= 0:
            return 0
        return max(0, int(time.time() - self.started_at / 1000.0))


@dataclass
class TranscriptState:
    """Incremental transcript parsing state (offsets, token counts, tool calls).

    Рус: Состояние инкрементного парсинга транскрипта (смещения, количество токенов, вызовы инструментов).
    """
    file_identity: Tuple[int, int] = (0, 0)
    offset: int = 0
    model: str = "-"
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_create: int = 0
    last_context_tokens: int = 0
    max_context_tokens: int = 0
    prev_cache_read: int = 0
    context_history: List[int] = field(default_factory=list)
    token_history: List[int] = field(default_factory=list)
    compaction_count: int = 0
    turn_count: int = 0
    current_task: str = ""
    version: str = ""
    git_branch: str = ""
    last_activity: float = 0.0
    initial_prompt: str = ""
    first_assistant_text: str = ""
    chat_messages: List[ChatMessage] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    last_user_ts_ms: int = 0
    last_assistant_ts_ms: int = 0
    pending_since_ms: int = 0
    thinking_since_ms: int = 0
    saw_turn: bool = False


@dataclass
class CodexResult:
    """Parsed Codex session result (session info, tokens, tool calls, rate limits).

    Рус: Результат разбора сеанса Codex (информация о сеансе, токены, вызовы инструментов, ограничения).
    """
    session_id: str = ""
    cwd: str = ""
    originator: str = ""
    started_at: int = 0
    model: str = "-"
    effort: str = ""
    version: str = ""
    git_branch: str = ""
    context_window: int = 0
    turn_count: int = 0
    current_task: str = ""
    task_complete: bool = False
    model_generating: bool = False
    user_decision_pending: bool = False
    last_activity: float = 0.0
    last_assistant_ts_ms: int = 0
    initial_prompt: str = ""
    chat_messages: List[ChatMessage] = field(default_factory=list)
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    last_context_tokens: int = 0
    token_history: List[int] = field(default_factory=list)
    rate_limit: Optional[RateLimitInfo] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    pending_since_ms: int = 0
    thinking_since_ms: int = 0


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def now_ms() -> int:
    """Return current time in milliseconds since epoch.

    Рус: Возвращает текущее время в миллисекундах с начала эпохи.
    """
    return int(time.time() * 1000)


def home_dir() -> Path:
    """Expand $HOME to a Path object.

    Рус: Раскрывает $HOME в объект Path.
    """
    return Path.home()


def cache_dir() -> Path:
    """Return the XDG cache directory path.

    Рус: Возвращает путь к каталогу кэша XDG.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base)
    return home_dir() / ".cache"


def config_dir() -> Path:
    """Return the XDG configuration directory path.

    Рус: Возвращает путь к каталогу конфигурации XDG.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base)
    return home_dir() / ".config"


def abbrev_path(path: Path) -> str:
    """Tilde-abbreviate path relative to home directory.

    Рус: Сокращает путь, заменяя домашнюю директорию на тильду.

    Args:
        path: Absolute or relative path. / Абсолютный или относительный путь.

    Returns:
        Path starting with ~/ if under home, otherwise the original string. / Путь с ~/ если под домашней, иначе оригинальная строка.
    """
    try:
        rel = path.expanduser().resolve().relative_to(home_dir().resolve())
        return "~/" + str(rel)
    except Exception:
        return str(path)


def safe_int(value: Any, default: int = 0) -> int:
    """Safely cast a value to int, returning default on failure.

    Рус: Безопасно преобразует значение в int, возвращая default при ошибке.

    Args:
        value: Value to cast. / Значение для преобразования.
        default: Fallback value. / Значение по умолчанию.

    Returns:
        Integer value or default. / Целое значение или default.
    """
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely cast a value to float, returning default on failure.

    Рус: Безопасно преобразует значение в float, возвращая default при ошибке.

    Args:
        value: Value to cast. / Значение для преобразования.
        default: Fallback value. / Значение по умолчанию.

    Returns:
        Float value or default. / Числовое значение с плавающей точкой или default.
    """
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def parse_timestamp_ms(value: Any) -> int:
    """Parse an ISO timestamp string to milliseconds since epoch.

    Рус: Парсит ISO-временную метку в миллисекунды с начала эпохи.

    Args:
        value: ISO-format timestamp string (e.g. "2025-01-15T10:30:00Z"). / ISO-строка временной метки.

    Returns:
        Millisecond epoch, or 0 on parse failure. / Миллисекунды с начала эпохи, или 0 при ошибке.
    """
    if not isinstance(value, str) or not value:
        return 0
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(_dt.datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return 0


def file_identity(path: Path) -> Tuple[int, int]:
    """Return (device, inode) tuple for file change detection.

    Рус: Возвращает пару (устройство, inode) для обнаружения изменений файла.

    Args:
        path: Path to the file. / Путь к файлу.

    Returns:
        (dev, ino) tuple, or (0, 0) on failure. / Пара (dev, ino), или (0, 0) при ошибке.
    """
    try:
        st = path.stat()
        return (int(st.st_dev), int(st.st_ino))
    except Exception:
        return (0, 0)


def is_symlink(path: Path) -> bool:
    """Check whether the given path is a symbolic link.

    Рус: Проверяет, является ли путь символической ссылкой.

    Args:
        path: Path to check. / Проверяемый путь.

    Returns:
        True if path is a symlink. / True если путь — симлинк.
    """
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except Exception:
        return False


def last_path_segment(path: str) -> str:
    """Return the basename (last component) of a path.

    Рус: Возвращает имя последнего компонента пути.

    Args:
        path: File or directory path. / Путь к файлу или каталогу.

    Returns:
        Last path segment, or "?" for empty input. / Последний компонент пути, или "?" для пустого ввода.
    """
    if not path:
        return "?"
    p = path.rstrip("/\\")
    name = os.path.basename(p)
    return name or p or "?"


def human_tokens(value: int) -> str:
    """Format token count as human-readable string (K/M/B).

    Рус: Форматирует количество токенов в читаемый формат (К/М/Б).

    Args:
        value: Raw token count. / Число токенов.

    Returns:
        Formatted string like "1.2M". / Отформатированная строка, например "1.2M".
    """
    value = int(value or 0)
    if value >= 1_000_000_000:
        return "%.1fB" % (value / 1_000_000_000.0)
    if value >= 1_000_000:
        return "%.1fM" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%.1fk" % (value / 1_000.0)
    return str(value)


def human_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable string (1h 2m 3s).

    Рус: Форматирует длительность в секундах в читаемый формат (1ч 2м 3с).

    Args:
        seconds: Duration in seconds. / Длительность в секундах.

    Returns:
        Formatted duration string. / Отформатированная строка длительности.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


def human_duration_ms(ms: int) -> str:
    """Format duration from milliseconds to human-readable string.

    Рус: Форматирует длительность из миллисекунд в читаемый формат.

    Args:
        ms: Duration in milliseconds. / Длительность в миллисекундах.

    Returns:
        Formatted duration string. / Отформатированная строка длительности.
    """
    ms = max(0, int(ms))
    if ms >= 60_000:
        minutes = ms // 60_000
        seconds = (ms % 60_000) / 1000.0
        return "%dm%.0fs" % (minutes, seconds)
    if ms >= 1000:
        return "%.1fs" % (ms / 1000.0)
    return "%dms" % ms


def compact_age_label(timestamp_ms: int) -> str:
    """Return compact age from a millisecond timestamp, e.g. 6s, 2m, 10h.

    Рус: Вернуть короткий возраст по timestamp в мс, например 6s, 2m, 10h.
    """
    if timestamp_ms <= 0:
        return "-"
    delta = max(0, int(time.time()) - int(timestamp_ms // 1000))
    if delta < 60:
        return "%ds" % delta
    if delta < 3600:
        return "%dm" % (delta // 60)
    if delta < 86400:
        return "%dh" % (delta // 3600)
    return "%dd" % (delta // 86400)


def date_clock_label_ms(timestamp_ms: int) -> str:
    """Format a millisecond timestamp as local Mon DD HH:MM.

    Рус: Форматировать timestamp в мс как локальное Mon DD HH:MM.
    """
    if timestamp_ms <= 0:
        return ""
    try:
        dt = _dt.datetime.fromtimestamp(timestamp_ms / 1000.0)
        month = MONTH_ABBR[max(0, min(11, dt.month - 1))]
        return "%s %02d %s" % (month, dt.day, dt.strftime("%H:%M"))
    except Exception:
        return ""


def time_range_label_ms(start_ms: int, end_ms: int, end_is_now: bool = False) -> str:
    """Return a compact local time range.

    Рус: Вернуть компактный локальный диапазон времени.
    """
    start = date_clock_label_ms(start_ms)
    if end_is_now:
        end = "now"
    else:
        start_dt = _dt.datetime.fromtimestamp(start_ms / 1000.0) if start_ms > 0 else None
        end_dt = _dt.datetime.fromtimestamp(end_ms / 1000.0) if end_ms > 0 else None
        if start_dt and end_dt and start_dt.date() == end_dt.date():
            end = end_dt.strftime("%H:%M")
        else:
            end = date_clock_label_ms(end_ms)
    if not start or not end:
        return ""
    return "%s-%s" % (start, end)


def minute_second_label(seconds: int) -> str:
    """Return M:SS for a small relative time axis.

    Рус: Вернуть M:SS для небольшой относительной временной оси.
    """
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def relative_time_axis(width: int, seconds_per_col: float, tick_seconds: int = 30) -> str:
    """Render a right-anchored relative time axis for a rolling chart.

    Рус: Отрисовать правостороннюю относительную ось времени для скользящего графика.
    """
    if width <= 0:
        return ""
    seconds_per_col = max(0.1, float(seconds_per_col))
    tick_seconds = max(1, int(tick_seconds))
    chars = [" "] * width
    max_age = int(round(max(0, width - 1) * seconds_per_col))
    placements: List[Tuple[int, str]] = [(0, "! " + minute_second_label(max_age))]
    age = (max_age // tick_seconds) * tick_seconds
    if age == max_age:
        age -= tick_seconds
    while age >= tick_seconds:
        pos = width - 1 - int(round(age / seconds_per_col))
        if pos > 0:
            placements.append((pos, "! " + minute_second_label(age)))
        age -= tick_seconds
    placements.append((width - 1, "!"))
    placed_end = -1
    for pos, label in sorted(placements, key=lambda item: item[0]):
        if placed_end >= 0 and pos <= placed_end + 1:
            continue
        end = min(width, pos + len(label))
        for idx, ch in enumerate(label[: max(0, end - pos)]):
            chars[pos + idx] = ch
        placed_end = end - 1
    return "".join(chars)


def clean_text(text: Any, limit: int = 500) -> str:
    """Strip control characters, redact secrets, and truncate text.

    Рус: Удаляет управляющие символы, маскирует секреты и обрезает текст.

    Args:
        text: Input text. / Входной текст.
        limit: Maximum output length. / Максимальная длина вывода.

    Returns:
        Cleaned, redacted, truncated string. / Очищенная, маскированная, обрезанная строка.
    """
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch for ch in text if (not ord(ch) < 32) or ch in "\t ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = redact_secrets(cleaned)
    if len(cleaned) > limit:
        return cleaned[: max(0, limit - 1)] + "…"
    return cleaned


def redact_secrets(text: str) -> str:
    """Mask known API keys, tokens, and password patterns in text.

    Рус: Маскирует известные API-ключи, токены и паттерны паролей в тексте.

    Args:
        text: Input text possibly containing secrets. / Входной текст, возможно содержащий секреты.

    Returns:
        Text with secret values replaced by [REDACTED]. / Текст с заменёнными секретами на [REDACTED].
    """
    patterns = [
        "sk-ant-",
        "sk-proj-",
        "sk-or-",
        "sk_live_",
        "sk_test_",
        "rk_live_",
        "rk_test_",
        "ghp_",
        "gho_",
        "ghs_",
        "ghr_",
        "ghu_",
        "github_pat_",
        "glpat-",
        "xoxb-",
        "xoxp-",
        "xoxa-",
        "xoxs-",
        "AKIA",
        "ASIA",
        "Bearer ",
    ]
    result = text
    for pat in patterns:
        start = 0
        while True:
            pos = result.find(pat, start)
            if pos < 0:
                break
            end = pos
            while end < len(result) and not result[end].isspace():
                end += 1
            result = result[:pos] + "[REDACTED]" + result[end:]
            start = pos + len("[REDACTED]")
    return result


def push_chat(messages: List[ChatMessage], role: str, text: str, timestamp_ms: int = 0) -> None:
    """Append a cleaned chat message to the history, enforcing max size.

    Рус: Добавляет очищенное сообщение в историю чата с ограничением размера.

    Args:
        messages: Chat message list to append to. / Список сообщений чата.
        role: Message role (user/assistant). / Роль сообщения (пользователь/ассистент).
        text: Raw message text. / Исходный текст сообщения.
        timestamp_ms: Message timestamp in milliseconds. / Timestamp сообщения в миллисекундах.
    """
    text = clean_text(text, 500)
    if not text:
        return
    messages.append(ChatMessage(role=role, text=text, timestamp_ms=timestamp_ms))
    if len(messages) > MAX_CHAT_MESSAGES:
        del messages[: len(messages) - MAX_CHAT_MESSAGES]


def tail(values: Sequence[Any], size: int) -> List[Any]:
    """Return the last n elements of a sequence.

    Рус: Возвращает последние n элементов последовательности.

    Args:
        values: Input sequence. / Входная последовательность.
        size: Number of trailing elements to return. / Количество последних элементов.

    Returns:
        List of the last size elements (or all if fewer). / Список последних size элементов (или всех, если меньше).
    """
    if len(values) <= size:
        return list(values)
    return list(values[-size:])


def run_command(args: Sequence[str], timeout: float = 1.5, cwd: Optional[str] = None) -> str:
    """Execute an external command without a shell and return its stdout.

    Рус: Выполняет внешнюю команду без оболочки и возвращает её stdout.

    Args:
        args: Command and arguments. / Команда и аргументы.
        timeout: Maximum execution time in seconds. / Максимальное время выполнения в секундах.
        cwd: Working directory for the command. / Рабочий каталог для команды.

    Returns:
        Command stdout as string, or empty string on failure. / stdout команды как строка, или пустая строка при ошибке.
    """
    try:
        proc = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            timeout=timeout,
            check=False,
            universal_newlines=True,
        )
        return proc.stdout or ""
    except Exception:
        return ""


def binary_name(token: str) -> str:
    """Extract the basename of a binary, stripping .exe suffix.

    Рус: Извлекает имя бинарного файла, убирая суффикс .exe.

    Args:
        token: Path or command token. / Путь или токен команды.

    Returns:
        Basename without .exe. / Имя файла без .exe.
    """
    base = os.path.basename(token.strip().strip('"').strip("'"))
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def command_tokens(command: str) -> List[str]:
    """Split a process command line into whitespace-separated tokens.

    Рус: Разбивает строку командной строки процесса на токены по пробелам.

    Args:
        command: Process command line. / Командная строка процесса.

    Returns:
        List of non-empty tokens. / Список непустых токенов.
    """
    return [part for part in command.replace("\x00", " ").split() if part]


def cmd_has_binary(command: str, name: str) -> bool:
    """Check whether a command string references an external binary by name.

    Рус: Проверяет, ссылается ли команда на внешний бинарный файл по имени.

    Args:
        command: Full command string. / Полная строка команды.
        name: Binary basename to look for. / Имя бинарного файла для поиска.

    Returns:
        True if the binary is referenced. / True если бинарный файл найден.
    """
    for token in command_tokens(command):
        base = binary_name(token)
        if base == name or base == ("%s.js" % name):
            return True
    return False


def first_token_has_binary(command: str, name: str) -> bool:
    """Check whether the first token in a command is a specific binary.

    Рус: Проверяет, является ли первый токен команды указанным бинарным файлом.

    Args:
        command: Full command string. / Полная строка команды.
        name: Expected binary basename. / Ожидаемое имя бинарного файла.

    Returns:
        True if the first token matches the binary name. / True если первый токен совпадает.
    """
    parts = command_tokens(command)
    return bool(parts) and binary_name(parts[0]) == name


def parse_simple_toml_list(text: str, key: str) -> List[str]:
    """Parse a simple TOML array value like key = ["a", "b", "c"].

    Рус: Парсит простой список TOML вида key = ["a", "b", "c"].

    Args:
        text: Full TOML text. / Полный текст TOML.
        key: Key name to look for. / Искомое имя ключа.

    Returns:
        List of quoted string values. / Список строковых значений в кавычках.
    """
    pattern = re.compile(r"^\s*%s\s*=\s*\[(.*?)\]\s*$" % re.escape(key), re.M | re.S)
    match = pattern.search(text)
    if not match:
        return []
    body = match.group(1)
    return [m.group(1) for m in re.finditer(r'"([^"]*)"', body)]


def load_abtop_config() -> Tuple[Set[str], List[Path]]:
    """Load ~/.config/abtop/config.toml: hidden agents and extra Claude config dirs.

    Рус: Загружает ~/.config/abtop/config.toml: скрытые агенты и дополнительные каталоги Claude.

    Returns:
        (hidden_agents_set, claude_config_dirs_list). / (множество скрытых агентов, список каталогов Claude).
    """
    path = config_dir() / "abtop" / "config.toml"
    if not path.exists():
        return set(), []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set(), []
    hidden = {x.lower() for x in parse_simple_toml_list(text, "hidden_agents")}
    claude_dirs = []
    for item in parse_simple_toml_list(text, "claude_config_dirs"):
        claude_dirs.append(Path(os.path.expanduser(item)))
    return hidden, claude_dirs


# ---------------------------------------------------------------------------
# Process, ports, and git
# ---------------------------------------------------------------------------


def get_process_info() -> Dict[int, ProcessInfo]:
    """Collect process metadata for all running processes.

    Рус: Собирает метаданные процессов для всех запущенных процессов.

    Returns:
        Dict mapping PID to ProcessInfo. / Словарь: PID -> ProcessInfo.
    """
    if sys.platform.startswith("linux") and Path("/proc").exists():
        return get_process_info_linux()
    return get_process_info_ps()


def get_process_info_linux() -> Dict[int, ProcessInfo]:
    """Collect process metadata from /proc on Linux.

    Рус: Собирает информацию о процессах из /proc на Linux.

    Returns:
        Dict mapping PID to ProcessInfo. / Словарь: PID -> ProcessInfo.
    """
    result: Dict[int, ProcessInfo] = {}
    try:
        clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
    except Exception:
        clk_tck = 100
    try:
        page_size = os.sysconf(os.sysconf_names.get("SC_PAGESIZE", "SC_PAGESIZE"))
    except Exception:
        page_size = 4096
    try:
        uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        uptime_secs = 0.0

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            close = stat_text.rfind(")")
            if close < 0:
                continue
            fields = stat_text[close + 2 :].split()
            if len(fields) < 22:
                continue
            ppid = safe_int(fields[1])
            utime = safe_int(fields[11])
            stime = safe_int(fields[12])
            starttime = safe_int(fields[19])
            rss_pages = safe_int(fields[21])
            rss_kb = int(rss_pages * page_size / 1024)
            uptime_ticks = int(uptime_secs * clk_tck)
            elapsed_ticks = max(0, uptime_ticks - starttime)
            cpu_pct = ((utime + stime) / float(elapsed_ticks) * 100.0) if elapsed_ticks else 0.0
            raw_cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").strip()
            if not raw_cmd:
                continue
            command = raw_cmd.decode("utf-8", errors="replace")
            result[pid] = ProcessInfo(pid=pid, ppid=ppid, rss_kb=rss_kb, cpu_pct=cpu_pct, command=command)
        except Exception:
            continue
    return result


def get_process_info_ps() -> Dict[int, ProcessInfo]:
    """Collect process metadata via ps command (fallback for non-Linux).

    Рус: Собирает информацию о процессах через команду ps (резервный вариант).

    Returns:
        Dict mapping PID to ProcessInfo. / Словарь: PID -> ProcessInfo.
    """
    output = run_command(["ps", "-ww", "-eo", "pid,ppid,rss,%cpu,command"], timeout=2.0)
    result: Dict[int, ProcessInfo] = {}
    for line in output.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid = safe_int(parts[0], -1)
        if pid < 0:
            continue
        result[pid] = ProcessInfo(
            pid=pid,
            ppid=safe_int(parts[1]),
            rss_kb=safe_int(parts[2]),
            cpu_pct=safe_float(parts[3]),
            command=parts[4],
        )
    return result


def children_map(processes: Dict[int, ProcessInfo]) -> Dict[int, List[int]]:
    """Map each parent PID to its list of child PIDs.

    Рус: Сопоставляет родительский PID со списком дочерних PID.

    Args:
        processes: All process info dict. / Словарь со всей информацией о процессах.

    Returns:
        Dict mapping parent PID to list of child PIDs. / Словарь: родительский PID -> список дочерних PID.
    """
    result: Dict[int, List[int]] = {}
    for proc in processes.values():
        result.setdefault(proc.ppid, []).append(proc.pid)
    return result


def is_descendant_of(pid: int, ancestor: int, processes: Dict[int, ProcessInfo]) -> bool:
    """Check whether pid is a descendant of the given ancestor.

    Рус: Проверяет, является ли pid потомком указанного ancestor.

    Args:
        pid: Child process PID to trace. / PID потомка для проверки.
        ancestor: Ancestor PID to look for. / Искомый PID предка.
        processes: All process info dict. / Словарь со всей информацией о процессах.

    Returns:
        True if pid descends from ancestor. / True если pid является потомком ancestor.
    """
    if pid <= 0 or ancestor <= 0 or pid == ancestor:
        return False
    seen: Set[int] = set()
    current = pid
    while current not in seen:
        seen.add(current)
        proc = processes.get(current)
        if proc is None:
            return False
        if proc.ppid == ancestor:
            return True
        if proc.ppid in (0, 1):
            return False
        current = proc.ppid
    return False


def has_active_descendant(
    pid: int,
    child_map: Dict[int, List[int]],
    processes: Dict[int, ProcessInfo],
    cpu_threshold: float = 5.0,
) -> bool:
    """Check whether a process has any descendant with CPU above threshold.

    Рус: Проверяет, имеет ли процесс потомка с CPU выше порога.

    Args:
        pid: Parent process PID. / PID родительского процесса.
        child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
        processes: All process info dict. / Словарь со всей информацией о процессах.
        cpu_threshold: CPU% threshold for "active". / Порог CPU% для определения "активности".

    Returns:
        True if any descendant exceeds the CPU threshold. / True если потомок превышает порог CPU.
    """
    stack = list(child_map.get(pid, []))
    seen: Set[int] = set()
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        proc = processes.get(child)
        if proc and proc.cpu_pct > cpu_threshold:
            return True
        stack.extend(child_map.get(child, []))
    return False


def collect_children(
    pid: int,
    child_map: Dict[int, List[int]],
    processes: Dict[int, ProcessInfo],
    ports: Dict[int, List[int]],
) -> List[ChildProcess]:
    """Recursively collect all child processes with their info and ports.

    Рус: Рекурсивно собирает всех дочерних процессов с их информацией и портами.

    Args:
        pid: Parent process PID. / PID родительского процесса.
        child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
        processes: All process info dict. / Словарь со всей информацией о процессах.
        ports: PID-to-listening-ports mapping. / Сопоставление PID -> порты прослушивания.

    Returns:
        List of ChildProcess objects. / Список объектов ChildProcess.
    """
    result: List[ChildProcess] = []
    stack = list(child_map.get(pid, []))
    seen: Set[int] = set()
    while stack:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        proc = processes.get(child)
        if proc:
            port = ports.get(child, [None])[0]
            result.append(ChildProcess(pid=child, command=proc.command, mem_kb=proc.rss_kb, port=port))
        stack.extend(child_map.get(child, []))
    return result


def scan_proc_fds(pid: int) -> List[Path]:
    """Scan /proc/pid/fd for file descriptors, resolving symlinks.

    Рус: Сканирует /proc/pid/fd для поиска дескрипторов файлов, разрешая символические ссылки.

    Args:
        pid: Process ID to scan. / PID сканируемого процесса.

    Returns:
        List of resolved file paths, or empty list on failure. / Список разрешённых путей к файлам, или пустой список при ошибке.
    """
    fd_dir = Path("/proc") / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except Exception:
        return []
    result: List[Path] = []
    for entry in entries:
        try:
            result.append(Path(os.readlink(str(entry))))
        except Exception:
            continue
    return result


def get_process_cwd(pid: int) -> Optional[Path]:
    """Get the current working directory of a process.

    Рус: Получает текущий рабочий каталог процесса.

    Args:
        pid: Process ID. / PID процесса.

    Returns:
        Path to the working directory, or None on failure. / Путь к рабочему каталогу, или None при ошибке.
    """
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink("/proc/%d/cwd" % pid))
        except Exception:
            return None
    output = run_command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=2.0)
    for line in output.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def read_proc_env(pid: int, key: str) -> Optional[str]:
    """Read an environment variable from a process /proc/pid/environ.

    Рус: Считывает переменную окружения из /proc/pid/environ.

    Args:
        pid: Process ID. / PID процесса.
        key: Environment variable name. / Имя переменной окружения.

    Returns:
        Variable value, or None on failure. / Значение переменной, или None при ошибке.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        data = (Path("/proc") / str(pid) / "environ").read_bytes()
    except Exception:
        return None
    prefix = (key + "=").encode("utf-8")
    for item in data.split(b"\x00"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8", errors="replace")
    return None


def parse_proc_net_file(path: Path) -> Dict[str, int]:
    """Parse /proc/net/tcp or tcp6 file, mapping socket inodes to listening ports.

    Рус: Парсит файл /proc/net/tcp или tcp6, сопоставляя inode сокетов с портами прослушивания.

    Args:
        path: Path to the /proc/net/tcp (or tcp6) file. / Путь к файлу /proc/net/tcp (или tcp6).

    Returns:
        Dict mapping socket inode to port number. / Словарь: inode сокета -> номер порта.
    """
    result: Dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
    except Exception:
        return result
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        local = parts[1]
        state = parts[3]
        inode = parts[9]
        if state != "0A":
            continue
        try:
            port_hex = local.rsplit(":", 1)[1]
            port = int(port_hex, 16)
        except Exception:
            continue
        result[inode] = port
    return result


def get_listening_ports() -> Dict[int, List[int]]:
    """Get all listening ports, dispatching to Linux or lsof implementation.

    Рус: Получает все порты прослушивания, вызывая реализацию Linux или lsof.

    Returns:
        Dict mapping PID to list of listening ports. / Словарь: PID -> список портов прослушивания.
    """
    if sys.platform.startswith("linux") and Path("/proc/net/tcp").exists():
        return get_listening_ports_linux()
    return get_listening_ports_lsof()


def get_listening_ports_linux() -> Dict[int, List[int]]:
    """Linux-specific listening port scanner using /proc/net/tcp and /proc/pid/fd.

    Рус: Специфичный для Linux сканер портов прослушивания, использующий /proc/net/tcp и /proc/pid/fd.

    Returns:
        Dict mapping PID to list of listening ports. / Словарь: PID -> список портов прослушивания.
    """
    inode_to_port: Dict[str, int] = {}
    inode_to_port.update(parse_proc_net_file(Path("/proc/net/tcp")))
    inode_to_port.update(parse_proc_net_file(Path("/proc/net/tcp6")))
    if not inode_to_port:
        return {}
    result: Dict[int, List[int]] = {}
    socket_re = re.compile(r"socket:\[(\d+)\]")
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except Exception:
        return result
    for proc_dir in entries:
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        fd_dir = proc_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except Exception:
            continue
        for fd in fds:
            try:
                target = os.readlink(str(fd))
            except Exception:
                continue
            match = socket_re.match(target)
            if not match:
                continue
            port = inode_to_port.get(match.group(1))
            if port is None:
                continue
            result.setdefault(pid, [])
            if port not in result[pid]:
                result[pid].append(port)
    for values in result.values():
        values.sort()
    return result


def get_listening_ports_lsof() -> Dict[int, List[int]]:
    """lsof-based listening port scanner for non-Linux systems.

    Рус: Сканер портов прослушивания на основе lsof для систем, отличных от Linux.

    Returns:
        Dict mapping PID to list of listening ports. / Словарь: PID -> список портов прослушивания.
    """
    output = run_command(["lsof", "-i", "-P", "-n", "-sTCP:LISTEN"], timeout=3.0)
    result: Dict[int, List[int]] = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        pid = safe_int(parts[1], -1)
        if pid < 0:
            continue
        name = parts[-1]
        match = re.search(r":(\d+)(?:\s|\(|$)", name)
        if not match:
            continue
        port = safe_int(match.group(1), -1)
        if port < 0:
            continue
        result.setdefault(pid, [])
        if port not in result[pid]:
            result[pid].append(port)
    for values in result.values():
        values.sort()
    return result


def collect_git_stats(cwd: str) -> Tuple[str, int, int]:
    """Collect git branch, added file count, and modified file count for a directory.

    Рус: Собирает ветку git, количество добавленных и изменённых файлов в каталоге.

    Args:
        cwd: Working directory to inspect. / Проверяемый рабочий каталог.

    Returns:
        (branch_name, added_count, modified_count). / (имя_ветки, количество_добавленных, количество_изменённых).
    """
    if not cwd or not Path(cwd).is_dir():
        return "", 0, 0
    branch = run_command(["git", "-C", cwd, "branch", "--show-current"], timeout=0.8).strip()
    status = run_command(["git", "-C", cwd, "status", "--porcelain"], timeout=1.2)
    added = 0
    modified = 0
    for line in status.splitlines():
        if len(line) < 2:
            continue
        x = line[0]
        y = line[1]
        if x == "A" or y == "A" or line.startswith("??"):
            added += 1
        elif x.strip() or y.strip():
            modified += 1
    return branch, added, modified


# ---------------------------------------------------------------------------
# Claude collector
# ---------------------------------------------------------------------------


def encode_claude_cwd(cwd: str) -> str:
    """Encode a cwd path for file matching by replacing path separators with dashes.

    Рус: Кодирует путь cwd для сопоставления файлов, заменяя разделители путей на тире.

    Args:
        cwd: Working directory path. / Путь к рабочему каталогу.

    Returns:
        Encoded path string, or "-" for empty input. / Закодированная строка пути, или "-" для пустого ввода.
    """
    if not cwd:
        return "-"
    encoded = cwd.replace("\\", "-").replace("/", "-")
    return encoded or "-"


def is_claude_config_root(path: Path) -> bool:
    """Check if a path is a Claude Code configuration root (has sessions and projects dirs).

    Рус: Проверяет, является ли путь корнем конфигурации Claude Code (имеет каталоги sessions и projects).

    Args:
        path: Path to check. / Проверяемый путь.

    Returns:
        True if the path has both 'sessions' and 'projects' subdirectories. / True если путь содержит подкаталоги 'sessions' и 'projects'.
    """
    return (path / "sessions").is_dir() and (path / "projects").is_dir()


def discover_home_claude_dirs() -> List[Path]:
    """Discover .claude directories in the home directory that are config roots.

    Рус: Находит каталоги .claude в домашнем каталоге, которые являются корнями конфигурации.

    Returns:
        List of .claude config root paths. / Список путей к корням конфигурации .claude.
    """
    result: List[Path] = []
    home = home_dir()
    try:
        entries = list(home.iterdir())
    except Exception:
        return result
    for entry in entries:
        if entry.name == ".claude" or entry.name.startswith(".claude-"):
            if is_claude_config_root(entry):
                result.append(entry)
    return result


def find_transcript_path(config_root: Path, cwd: str, session_id: str) -> Optional[Path]:
    """Find the transcript JSONL file for a Claude session.

    Рус: Находит файл транскрипта JSONL для сеанса Claude.

    Args:
        config_root: Claude config root directory. / Корневой каталог конфигурации Claude.
        cwd: Project working directory. / Рабочий каталог проекта.
        session_id: Session identifier. / Идентификатор сеанса.

    Returns:
        Path to the transcript JSONL file, or None if not found. / Путь к файлу транскрипта JSONL, или None если не найден.
    """
    direct = config_root / "projects" / encode_claude_cwd(cwd) / ("%s.jsonl" % session_id)
    if direct.exists() and not is_symlink(direct):
        return direct
    pattern = str(config_root / "projects" / "*" / ("%s.jsonl" % session_id))
    for match in glob.glob(pattern):
        p = Path(match)
        if p.exists() and not is_symlink(p):
            return p
    return None


def extract_user_text(content: Any) -> Tuple[str, bool]:
    """Extract text from user message content and detect synthetic tool output.

    Рус: Извлекает текст из содержимого сообщения пользователя и обнаруживает синтетический вывод инструментов.

    Args:
        content: Message content (string or list of blocks). / Содержимое сообщения (строка или список блоков).

    Returns:
        (text, is_synthetic_tool_output). / (текст, является_ли_синтетическим_выводом_инструмента).
    """
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("<command-") or stripped.startswith("<local-command-"):
            return "", True
        return stripped, False
    if isinstance(content, list):
        texts: List[str] = []
        synthetic = False
        for item in content:
            if not isinstance(item, dict):
                continue
            typ = item.get("type")
            if typ == "tool_result":
                synthetic = True
                continue
            if typ == "text" and isinstance(item.get("text"), str):
                texts.append(item.get("text", ""))
        return "\n".join(texts).strip(), synthetic and not texts
    return "", False


def tool_arg(name: str, payload: Any) -> str:
    """Extract a human-readable argument from a Claude tool call payload.

    Рус: Извлекает читаемый аргумент из полезной нагрузки вызова инструмента Claude.

    Args:
        name: Tool name (e.g. Bash, Grep, Glob, Agent). / Имя инструмента (например Bash, Grep, Glob, Agent).
        payload: Tool input dictionary. / Словарь входных данных инструмента.

    Returns:
        Human-readable argument string, or empty string. / Читаемая строка аргумента, или пустая строка.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("file_path", "path", "notebook_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if name in ("Bash", "Shell"):
        return clean_text(payload.get("command", ""), 80)
    if name in ("Grep", "Search"):
        return clean_text(payload.get("pattern", ""), 80)
    if name == "Glob":
        return clean_text(payload.get("pattern", ""), 80)
    if name == "Agent":
        return clean_text(payload.get("description", "") or payload.get("prompt", ""), 80)
    return ""


def explicit_duration_ms(value: Any, depth: int = 0) -> int:
    """Recursively search for an explicit duration-in-milliseconds field in nested data.

    Рус: Рекурсивно ищет поле длительности в миллисекундах во вложенных данных.

    Args:
        value: Data structure to search. / Искомая структура данных.
        depth: Recursion depth limit. / Лимит глубины рекурсии.

    Returns:
        Duration in milliseconds, or 0 if not found. / Длительность в миллисекундах, или 0 если не найдено.
    """
    if depth > 4:
        return 0
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in (
                "durationms",
                "duration_ms",
                "elapsedms",
                "elapsed_ms",
                "executiontimems",
                "execution_time_ms",
                "walltimems",
                "wall_time_ms",
            ):
                ms = safe_int(item)
                if ms > 0:
                    return ms
            if key_l in ("duration", "elapsed", "walltime", "wall_time"):
                if isinstance(item, str):
                    match = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s)\b", item, re.I)
                    if match:
                        value_f = safe_float(match.group(1))
                        return int(value_f if match.group(2).lower() == "ms" else value_f * 1000)
        for item in value.values():
            ms = explicit_duration_ms(item, depth + 1)
            if ms > 0:
                return ms
    elif isinstance(value, list):
        for item in value:
            ms = explicit_duration_ms(item, depth + 1)
            if ms > 0:
                return ms
    return 0


def codex_output_duration_ms(output: Any) -> int:
    """Extract the actual command wall time from Codex tool output.

    Рус: Извлекает фактическое wall time команды из вывода инструмента Codex.
    """
    hinted = explicit_duration_ms(output)
    if hinted > 0:
        return hinted
    if not isinstance(output, str):
        return 0
    match = re.search(
        r"\bWall time:\s*(\d+(?:\.\d+)?)\s*(milliseconds?|msec|ms|seconds?|secs?|s)\b",
        output,
        re.I,
    )
    if not match:
        return 0
    value = safe_float(match.group(1))
    unit = match.group(2).lower()
    if unit in ("ms", "msec", "millisecond", "milliseconds"):
        return max(1, int(round(value)))
    return max(1, int(round(value * 1000)))


def claude_tool_results(content: Any, parent: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Get tool result call IDs and durations from user message content.

    Рус: Получает ID вызовов инструментов и длительности из содержимого сообщения пользователя.

    Args:
        content: User message content (list of tool_result blocks). / Содержимое сообщения пользователя (список блоков tool_result).
        parent: Parent message dict for duration hints. / Словарь родительского сообщения для подсказок длительности.

    Returns:
        List of (call_id, duration_ms) tuples. / Список кортежей (call_id, duration_ms).
    """
    results: List[Tuple[str, int]] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            call_id = str(
                item.get("tool_use_id")
                or item.get("toolUseId")
                or item.get("toolUseID")
                or ""
            )
            results.append((call_id, explicit_duration_ms(item)))
    if len(results) == 1 and results[0][1] <= 0:
        hinted = explicit_duration_ms(parent)
        if hinted > 0:
            results[0] = (results[0][0], hinted)
    return results


def open_claude_tool_calls(state: TranscriptState) -> Dict[str, ToolCall]:
    """Get currently open (uncompleted) tool calls from transcript state.

    Рус: Получает текущие открытые (незавершённые) вызовы инструментов из состояния транскрипта.

    Args:
        state: Transcript parsing state. / Состояние парсинга транскрипта.

    Returns:
        Dict mapping call_id to open ToolCall objects. / Словарь: call_id -> открытые объекты ToolCall.
    """
    result: Dict[str, ToolCall] = {}
    for call in state.tool_calls:
        if call.call_id and call.started_ms > 0 and call.duration_ms <= 0:
            result[call.call_id] = call
    return result


def oldest_open_tool_call(open_calls: Dict[str, ToolCall]) -> Tuple[str, Optional[ToolCall]]:
    """Find the oldest open tool call by earliest start time.

    Рус: Находит самый старый открытый вызов инструмента по самому раннему времени начала.

    Args:
        open_calls: Dict of open tool calls. / Словарь открытых вызовов инструментов.

    Returns:
        (call_id, ToolCall) of the oldest, or ("", None) if none. / (call_id, ToolCall) самого старого, или ("", None) если нет.
    """
    best_key = ""
    best_call: Optional[ToolCall] = None
    for key, call in open_calls.items():
        if call.duration_ms > 0:
            continue
        if best_call is None or call.started_ms < best_call.started_ms:
            best_key = key
            best_call = call
    return best_key, best_call


def complete_tool_call(call: ToolCall, completed_ms: int, duration_hint_ms: int = 0) -> None:
    """Complete a tool call with timing information.

    Рус: Завершает вызов инструмента с информацией о времени.

    Args:
        call: Tool call to complete. / Завершаемый вызов инструмента.
        completed_ms: Completion timestamp in milliseconds. / Временная метка завершения в миллисекундах.
        duration_hint_ms: Optional duration hint in milliseconds. / Опциональная подсказка длительности в миллисекундах.
    """
    if duration_hint_ms > 0:
        call.duration_ms = duration_hint_ms
        if completed_ms > 0:
            call.completed_ms = completed_ms
        elif call.started_ms > 0:
            call.completed_ms = call.started_ms + duration_hint_ms
        return
    if call.started_ms > 0 and completed_ms > 0:
        call.completed_ms = completed_ms
        call.duration_ms = max(0, completed_ms - call.started_ms)


def pending_since_from_open_tools(open_calls: Dict[str, ToolCall]) -> int:
    """Calculate pending duration from the earliest open tool call start time.

    Рус: Вычисляет длительность ожидания по самому раннему времени начала открытого вызова.

    Args:
        open_calls: Dict of open tool calls. / Словарь открытых вызовов инструментов.

    Returns:
        Start timestamp of the oldest open call, or 0 if none. / Временная метка начала самого старого открытого вызова, или 0 если нет.
    """
    starts = [call.started_ms for call in open_calls.values() if call.started_ms > 0 and call.duration_ms <= 0]
    return min(starts) if starts else 0


def collapse_approval_timeline_calls(calls: Sequence[ToolCall], now: int) -> List[ToolCall]:
    """Collapse overlapping Codex approval waits into one timeline row.

    Рус: Схлопывает перекрывающиеся ожидания Codex approval в одну строку timeline.
    """
    result: List[ToolCall] = []
    group: List[ToolCall] = []
    group_end = 0
    active_approval_end = 0

    def interval(call: ToolCall) -> Tuple[int, int, bool]:
        start = call.started_ms or 0
        pending = call.duration_ms <= 0 and call.completed_ms <= 0 and start > 0
        if pending:
            return start, now, True
        if call.completed_ms > 0:
            return start, call.completed_ms, False
        if call.duration_ms > 0 and start > 0:
            return start, start + call.duration_ms, False
        return start, start, False

    def flush_group() -> None:
        nonlocal group, group_end, active_approval_end
        if not group:
            return
        if len(group) == 1:
            result.append(group[0])
            start, end, _pending = interval(group[0])
            if start > 0 and end > 0:
                active_approval_end = max(active_approval_end, end)
        else:
            starts: List[int] = []
            ends: List[int] = []
            pending = False
            for item in group:
                start, end, is_pending = interval(item)
                if start > 0:
                    starts.append(start)
                if end > 0:
                    ends.append(end)
                pending = pending or is_pending
            start = min(starts) if starts else 0
            end = max(ends) if ends else 0
            result.append(
                ToolCall(
                    name="approval",
                    arg="%d approvals" % len(group),
                    duration_ms=0 if pending else max(0, end - start),
                    call_id="approval:%s" % ",".join(call.call_id for call in group if call.call_id),
                    started_ms=start,
                    completed_ms=0 if pending else end,
                    needs_approval=True,
                )
            )
            if start > 0 and end > 0:
                active_approval_end = max(active_approval_end, end)
        group = []
        group_end = 0

    for call in calls:
        if not call.needs_approval:
            flush_group()
            if (
                call.duration_ms <= 0
                and call.completed_ms <= 0
                and call.started_ms > 0
                and active_approval_end > 0
                and call.started_ms <= active_approval_end
            ):
                result.append(
                    ToolCall(
                        name="queued",
                        arg=call.arg,
                        duration_ms=0,
                        call_id=call.call_id,
                        started_ms=0,
                        completed_ms=0,
                    )
                )
                continue
            result.append(call)
            continue
        start, end, _pending = interval(call)
        if not group:
            group = [call]
            group_end = end
            continue
        if start > 0 and group_end > 0 and start <= group_end:
            group.append(call)
            group_end = max(group_end, end)
            continue
        flush_group()
        group = [call]
        group_end = end
    flush_group()
    return result


def is_user_decision_tool_name(name: str) -> bool:
    """Detect tool names that usually pause for user approval or choice.

    Рус: Определяет имена инструментов, которые обычно ждут одобрения или выбора пользователя.
    """
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name or "").casefold()).strip("_")
    if not normalized:
        return False
    markers = (
        "request_user_input",
        "ask_user",
        "user_choice",
        "approval_request",
        "request_approval",
        "confirm_action",
        "confirmation",
        "permission_request",
    )
    return any(marker in normalized for marker in markers)


def looks_like_user_decision_prompt(text: str) -> bool:
    """Detect prompts that ask for confirmation, approval, or a concrete choice.

    Рус: Определяет сообщения с запросом подтверждения, одобрения или конкретного выбора.
    """
    value = clean_text(text, 1000).casefold()
    if not value:
        return False

    strong_phrases = (
        "approval required",
        "requires approval",
        "needs approval",
        "please approve",
        "please confirm",
        "confirmation required",
        "confirm before",
        "permission to",
        "allow me to",
        "do you approve",
        "yes/no",
        "yes or no",
        "y/n",
        "требуется подтверждение",
        "нужно подтверждение",
        "подтвердите",
        "требуется разрешение",
        "нужно разрешение",
        "разрешите",
        "одобрите",
        "да/нет",
    )
    if any(phrase in value for phrase in strong_phrases):
        return True

    has_question = "?" in value or "？" in value
    if not has_question:
        return False

    question_markers = (
        "do you want",
        "would you like me",
        "would you like to proceed",
        "would you like to continue",
        "should i",
        "shall i",
        "can i",
        "may i",
        "proceed",
        "continue",
        "choose",
        "select",
        "pick",
        "which option",
        "which variant",
        "хотите ли",
        "хотите, чтобы",
        "хотите чтобы",
        "хотите продолжить",
        "нужно ли",
        "можно ли",
        "продолжить",
        "выберите",
        "выбрать",
        "какой вариант",
        "какую опцию",
    )
    return any(marker in value for marker in question_markers)


def is_user_decision_tool_call(call: ToolCall) -> bool:
    """Check whether an open tool call represents waiting for a user decision.

    Рус: Проверяет, означает ли открытый вызов инструмента ожидание решения пользователя.
    """
    return is_user_decision_tool_name(call.name)


def is_user_decision_task(task: str) -> bool:
    """Check whether a task string represents a user decision request.

    Рус: Проверяет, означает ли строка задачи запрос решения пользователя.
    """
    parts = command_tokens(str(task or ""))
    return bool(parts) and is_user_decision_tool_name(parts[0])


def decode_codex_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Return Codex tool arguments as a dict when possible.

    Рус: Вернуть аргументы инструмента Codex как dict, если возможно.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def is_codex_approval_tool_call(name: str, arguments: Any) -> bool:
    """Detect Codex tool calls that are paused for external approval.

    Рус: Определить вызовы Codex, остановленные в ожидании внешнего разрешения.
    """
    args = decode_codex_tool_arguments(arguments)
    if not args:
        return False
    sandbox = str(args.get("sandbox_permissions") or args.get("sandbox_permission") or "").casefold()
    if sandbox in ("require_escalated", "requires_escalated", "escalated"):
        return True
    if args.get("with_escalated_permissions") is True:
        return True
    return bool(args.get("justification")) and "escalat" in sandbox


def infer_wait_reason(messages: Sequence[ChatMessage]) -> str:
    """Infer whether a waiting session needs input or a decision from the user.

    Рус: Определяет, ждёт ли сессия обычный ввод или решение пользователя.
    """
    for message in reversed(messages):
        if not message.text:
            continue
        if message.role == "assistant" and looks_like_user_decision_prompt(message.text):
            return WAIT_REASON_USER_DECISION
        break
    return WAIT_REASON_USER_INPUT


def wait_task_text(wait_reason: str) -> str:
    """Return user-facing task text for a waiting session.

    Рус: Возвращает текст задачи для сессии в состоянии ожидания.
    """
    if wait_reason == WAIT_REASON_USER_DECISION:
        return "waiting for user decision"
    if wait_reason == WAIT_REASON_BETWEEN_STEPS:
        return "between steps"
    return "waiting for input"


def within_codex_between_steps_grace(result: CodexResult) -> bool:
    """Return True shortly after Codex emits an assistant message.

    Рус: Возвращает True вскоре после сообщения ассистента Codex.
    """
    if result.last_assistant_ts_ms <= 0:
        return False
    age_ms = now_ms() - result.last_assistant_ts_ms
    return 0 <= age_ms <= CODEX_BETWEEN_STEPS_GRACE_MS


def parse_claude_transcript_delta(
    path: Path,
    offset: int,
    initial_context_tokens: int,
    initial_cache_read: int,
    active_tool_calls: Optional[Dict[str, ToolCall]] = None,
) -> TranscriptState:
    """Incrementally parse a Claude transcript JSONL file from a given offset.

    Reads new lines since the last offset, updates token counts, tool calls,
    chat messages, and detects context compaction events.

    Рус: Инкрементно парсит файл транскрипта Claude JSONL с заданного смещения.
    Считывает новые строки с последнего смещения, обновляет количество токенов,
    вызовы инструментов, сообщения чата и обнаруживает события сжатия контекста.

    Args:
        path: Path to the transcript JSONL file. / Путь к файлу транскрипта JSONL.
        offset: Byte offset to start reading from. / Смещение байта для начала чтения.
        initial_context_tokens: Previous context token count. / Предыдущее количество токенов контекста.
        initial_cache_read: Previous cache-read token count. / Предыдущее количество токенов чтения кэша.
        active_tool_calls: Currently open tool calls. / Текущие открытые вызовы инструментов.

    Returns:
        TranscriptState with updated counts and messages. / TranscriptState с обновлёнными счётчиками и сообщениями.
    """
    identity = file_identity(path)
    result = TranscriptState(file_identity=identity, offset=offset)
    open_tools: Dict[str, ToolCall] = dict(active_tool_calls or {})
    anonymous_index = 0
    try:
        file_len = path.stat().st_size
        result.last_activity = path.stat().st_mtime
    except Exception:
        return result
    if file_len == offset:
        result.offset = file_len
        return result
    if file_len < offset:
        offset = 0
        result.offset = 0

    prev_context = initial_context_tokens if offset > 0 else 0
    prev_cache_read = initial_cache_read if offset > 0 else 0

    try:
        fh = path.open("rb")
    except Exception:
        return result
    with fh:
        try:
            fh.seek(offset)
        except Exception:
            pass
        while True:
            start = fh.tell()
            raw = fh.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                result.offset = file_len
                break
            has_newline = raw.endswith(b"\n")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                if has_newline:
                    result.offset = fh.tell()
                continue
            try:
                value = json.loads(line)
            except Exception:
                if has_newline:
                    result.offset = fh.tell()
                else:
                    fh.seek(start)
                    break
                continue
            result.offset = fh.tell()
            typ = value.get("type")
            ts_ms = parse_timestamp_ms(value.get("timestamp"))
            if ts_ms:
                result.last_activity = max(result.last_activity, ts_ms / 1000.0)

            if typ == "assistant":
                result.saw_turn = True
                result.turn_count += 1
                result.current_task = ""
                result.last_assistant_ts_ms = ts_ms
                result.last_user_ts_ms = 0
                result.thinking_since_ms = 0
                msg = value.get("message") or {}
                if isinstance(msg, dict):
                    model = msg.get("model")
                    if isinstance(model, str) and model:
                        result.model = model
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        inp = safe_int(usage.get("input_tokens"))
                        out = safe_int(usage.get("output_tokens"))
                        cr = safe_int(usage.get("cache_read_input_tokens"))
                        cc = safe_int(usage.get("cache_creation_input_tokens"))
                        result.total_input += inp
                        result.total_output += out
                        result.total_cache_read += cr
                        result.total_cache_create += cc
                        current_context = inp + cc if cr == 0 and cc > 0 else inp + cr
                        result.last_context_tokens = current_context
                        result.max_context_tokens = max(result.max_context_tokens, current_context)
                        if (
                            prev_context > 0
                            and current_context < prev_context * 7 // 10
                            and prev_cache_read > 1000
                            and cr < prev_cache_read // 5
                        ):
                            result.compaction_count += 1
                        prev_context = current_context
                        prev_cache_read = cr
                        result.prev_cache_read = cr
                        if len(result.context_history) < MAX_HISTORY:
                            result.context_history.append(current_context)
                        if len(result.token_history) < MAX_HISTORY:
                            result.token_history.append(inp + out + cr + cc)

                    content = msg.get("content")
                    if isinstance(content, list):
                        text_blocks: List[str] = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type == "text" and isinstance(block.get("text"), str):
                                text_blocks.append(block.get("text", ""))
                            elif block_type == "tool_use":
                                name = str(block.get("name") or "tool")
                                arg = tool_arg(name, block.get("input"))
                                raw_id = block.get("id")
                                call_id = raw_id if isinstance(raw_id, str) and raw_id else ""
                                if not call_id:
                                    anonymous_index += 1
                                    call_id = "anonymous:%d:%d" % (ts_ms or now_ms(), anonymous_index)
                                started_ms = ts_ms or now_ms()
                                result.current_task = ("%s %s" % (name, arg)).strip()
                                call = ToolCall(
                                    name=name,
                                    arg=arg,
                                    duration_ms=0,
                                    call_id=call_id,
                                    started_ms=started_ms,
                                )
                                result.tool_calls.append(call)
                                open_tools[call_id] = call
                                result.pending_since_ms = pending_since_from_open_tools(open_tools)
                        if text_blocks:
                            text = "\n".join(text_blocks)
                            if not result.first_assistant_text:
                                result.first_assistant_text = clean_text(text, 200)
                            push_chat(result.chat_messages, "assistant", text, ts_ms)

            elif typ == "user":
                result.saw_turn = True
                result.version = str(value.get("version") or result.version or "")
                result.git_branch = str(value.get("gitBranch") or result.git_branch or "")
                msg = value.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                text, synthetic = extract_user_text(content)
                if not synthetic and text:
                    result.last_user_ts_ms = ts_ms
                    result.thinking_since_ms = ts_ms
                    result.pending_since_ms = 0
                    if not result.initial_prompt:
                        result.initial_prompt = clean_text(text, 120)
                    push_chat(result.chat_messages, "user", text, ts_ms)
                elif synthetic:
                    completed_ms = ts_ms or now_ms()
                    tool_results = claude_tool_results(content, value)
                    if tool_results:
                        for call_id, duration_hint in tool_results:
                            call = open_tools.pop(call_id, None) if call_id else None
                            if call is None:
                                fallback_key, call = oldest_open_tool_call(open_tools)
                                if call is not None:
                                    open_tools.pop(fallback_key, None)
                            if call is not None:
                                complete_tool_call(call, completed_ms, duration_hint)
                    else:
                        fallback_key, call = oldest_open_tool_call(open_tools)
                        if call is not None:
                            complete_tool_call(call, completed_ms)
                            open_tools.pop(fallback_key, None)
                    result.pending_since_ms = pending_since_from_open_tools(open_tools)

            elif typ == "last-prompt":
                prompt = value.get("lastPrompt")
                if isinstance(prompt, str) and prompt and not result.initial_prompt:
                    result.initial_prompt = clean_text(prompt, 120)

    return result


def merge_transcript_state(prev: TranscriptState, delta: TranscriptState) -> TranscriptState:
    """Merge a delta TranscriptState into the previous one.

    Accumulates token counts, appends histories (capped), merges chat messages,
    tool calls, and timestamps. Modifies prev in-place and returns it.

    Рус: Объединяет дельту TranscriptState с предыдущей.
    Накапливает счётчики токенов, добавляет истории (с ограничением), объединяет
    сообщения чата, вызовы инструментов и временные метки. Изменяет prev на месте и возвращает его.

    Args:
        prev: Base transcript state to merge into. / Базовое состояние транскрипта для объединения.
        delta: New transcript state to merge from. / Новое состояние транскрипта для добавления.

    Returns:
        The merged prev state (modified in-place). / Объединённое состояние prev (изменено на месте).
    """
    if delta.model != "-":
        prev.model = delta.model
    prev.total_input += delta.total_input
    prev.total_output += delta.total_output
    prev.total_cache_read += delta.total_cache_read
    prev.total_cache_create += delta.total_cache_create
    if delta.last_context_tokens:
        prev.last_context_tokens = delta.last_context_tokens
        prev.prev_cache_read = delta.prev_cache_read
    prev.max_context_tokens = max(prev.max_context_tokens, delta.max_context_tokens)
    prev.context_history.extend(delta.context_history)
    if len(prev.context_history) > MAX_HISTORY:
        del prev.context_history[: len(prev.context_history) - MAX_HISTORY]
    prev.token_history.extend(delta.token_history)
    if len(prev.token_history) > MAX_HISTORY:
        del prev.token_history[: len(prev.token_history) - MAX_HISTORY]
    prev.compaction_count += delta.compaction_count
    prev.turn_count += delta.turn_count
    if delta.turn_count > 0:
        prev.current_task = delta.current_task
    if delta.version:
        prev.version = delta.version
    if delta.git_branch:
        prev.git_branch = delta.git_branch
    prev.last_activity = max(prev.last_activity, delta.last_activity)
    if not prev.initial_prompt and delta.initial_prompt:
        prev.initial_prompt = delta.initial_prompt
    if not prev.first_assistant_text and delta.first_assistant_text:
        prev.first_assistant_text = delta.first_assistant_text
    prev.chat_messages.extend(delta.chat_messages)
    if len(prev.chat_messages) > MAX_CHAT_MESSAGES:
        del prev.chat_messages[: len(prev.chat_messages) - MAX_CHAT_MESSAGES]
    prev.tool_calls.extend(delta.tool_calls)
    if len(prev.tool_calls) > 500:
        del prev.tool_calls[: len(prev.tool_calls) - 500]
    if delta.saw_turn:
        prev.last_user_ts_ms = delta.last_user_ts_ms
        prev.last_assistant_ts_ms = delta.last_assistant_ts_ms
        prev.pending_since_ms = delta.pending_since_ms
        prev.thinking_since_ms = delta.thinking_since_ms
    prev.offset = delta.offset
    prev.file_identity = delta.file_identity
    return prev


def read_configured_claude_model(cwd: str) -> str:
    """Read the Claude model from project or home settings files.

    Checks .claude/settings.local.json and .claude/settings.json in the project
    directory, then falls back to home directory settings.

    Рус: Читает модель Claude из файлов настроек проекта или домашней директории.
    Проверяет .claude/settings.local.json и .claude/settings.json в каталоге проекта,
    затем переходит к настройкам домашней директории.

    Args:
        cwd: Current working directory of the session. / Текущий рабочий каталог сеанса.

    Returns:
        Model string (e.g. "claude-sonnet-4-5-20250514"), or empty string if not found. / Строка модели, или пустая строка если не найдена.
    """
    candidates = [
        Path(cwd) / ".claude" / "settings.local.json",
        Path(cwd) / ".claude" / "settings.json",
        home_dir() / ".claude" / "settings.local.json",
        home_dir() / ".claude" / "settings.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        model = data.get("model") if isinstance(data, dict) else None
        if isinstance(model, str):
            return model
    return ""


def claude_context_window(model: str, configured_model: str, max_context_tokens: int) -> int:
    """Determine the Claude context window size for a model.

    Returns 1M for 1M-context models (detected by [1m] suffix or high max_context_tokens),
    otherwise defaults to 200K.

    Рус: Определяет размер окна контекста Claude для модели.
    Возвращает 1M для моделей с 1M-контекстом (определяется по суффиксу [1m] или высокому max_context_tokens),
    иначе по умолчанию 200K.

    Args:
        model: Detected model string from transcript. / Обнаруженная строка модели из транскрипта.
        configured_model: Model from settings files. / Модель из файлов настроек.
        max_context_tokens: Max context tokens from session data. / Максимальные токены контекста из данных сеанса.

    Returns:
        Context window size in tokens (200000 or 1000000). / Размер окна контекста в токенах.
    """
    if "[1m]" in model.lower() or "[1m]" in configured_model.lower() or max_context_tokens > 200000:
        return 1_000_000
    return 200_000


class ClaudeCollector:
    """Collects Claude Code sessions from transcript files.

    Discovers .claude config directories, finds session JSON files,
    and incrementally parses transcript JSONL files for each session.

    Рус: Собирает сеансы Claude Code из файлов транскрипта.
    Обнаруживает каталоги конфигурации .claude, находит файлы сеансов JSON
    и инкрементно парсит файлы транскрипта JSONL для каждого сеанса.
    """

    def __init__(self, configured_dirs: Sequence[Path]) -> None:
        """Initialize with pre-configured .claude directories.

        Рус: Инициализирует с предустановленными каталогами .claude.

        Args:
            configured_dirs: List of .claude config root paths. / Список путей к корням конфигурации .claude.
        """
        self.configured_dirs = list(configured_dirs)
        self.config_dirs: List[Path] = []
        self.cache: Dict[str, TranscriptState] = {}

    def refresh_config_dirs(self, processes: Dict[int, ProcessInfo]) -> None:
        """Discover all valid .claude config directories.

        Checks home directory .claude, configured directories, and
        CWD-based .claude directories from running Claude processes.

        Рус: Обнаруживает все допустимые каталоги конфигурации .claude.
        Проверяет домашний каталог .claude, настроенные каталоги и
        основанные на CWD каталоги .claude из запущенных процессов Claude.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.
        """
        seen: Set[str] = set()
        dirs: List[Path] = []

        def add(path: Path) -> None:
            """Add a config directory if valid and not already seen.

            Рус: Добавляет каталог конфигурации если он действителен и ещё не был добавлен.

            Args:
                path: Path to check. / Путь для проверки.
            """
            try:
                p = path.expanduser()
            except Exception:
                p = path
            key = str(p)
            if key in seen or not is_claude_config_root(p):
                return
            seen.add(key)
            dirs.append(p)

        add(home_dir() / ".claude")
        for p in discover_home_claude_dirs():
            add(p)
        for p in self.configured_dirs:
            add(p)
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            add(Path(os.environ["CLAUDE_CONFIG_DIR"]))
        for pid, proc in processes.items():
            if not cmd_has_binary(proc.command, "claude"):
                continue
            env_dir = read_proc_env(pid, "CLAUDE_CONFIG_DIR")
            if env_dir:
                add(Path(env_dir))
        self.config_dirs = dirs

    def find_claude_pids(self, processes: Dict[int, ProcessInfo]) -> Set[int]:
        """Find PIDs of all running Claude Code processes.

        Excludes the collector's own process and any descendants of it.

        Рус: Находит PID всех запущенных процессов Claude Code.
        Исключает собственный процесс коллектора и его потомков.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.

        Returns:
            Set of Claude process PIDs. / Набор PID процессов Claude.
        """
        self_pid = os.getpid()
        pids: Set[int] = set()
        for pid, proc in processes.items():
            if not cmd_has_binary(proc.command, "claude"):
                continue
            if is_descendant_of(pid, self_pid, processes):
                continue
            pids.add(pid)
        return pids

    def collect(
        self,
        processes: Dict[int, ProcessInfo],
        child_map: Dict[int, List[int]],
        ports: Dict[int, List[int]],
        slow_tick: bool,
    ) -> List[AgentSession]:
        """Collect all Claude sessions from discovered config directories.

        Refreshes config dirs on slow tick or first run, finds live Claude PIDs,
        iterates session JSON files, and incrementally updates transcript state.

        Рус: Собирает все сеансы Claude из обнаруженных каталогов конфигурации.
        Обновляет каталоги конфигурации при медленном тике или первом запуске, находит
        живые PID Claude, перебирает файлы сеансов JSON и инкрементно обновляет состояние транскрипта.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.
            child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
            ports: PID-to-listening-ports mapping. / Сопоставление PID -> порты прослушивания.
            slow_tick: Whether this is a slow/full collection tick. / Является ли это медленным/полным тиком сбора.

        Returns:
            List of AgentSession objects, sorted by start time descending. / Список объектов AgentSession, отсортированных по времени начала (по убыванию).
        """
        if slow_tick or not self.config_dirs:
            self.refresh_config_dirs(processes)
        live_pids = self.find_claude_pids(processes)
        sessions: List[AgentSession] = []
        seen_ids: Set[str] = set()

        for root in self.config_dirs:
            sessions_dir = root / "sessions"
            try:
                files = list(sessions_dir.glob("*.json"))
            except Exception:
                files = []
            for path in files:
                session = self.load_session(root, path, live_pids, processes, child_map, ports)
                if session and session.session_id not in seen_ids:
                    seen_ids.add(session.session_id)
                    sessions.append(session)

        active = {s.session_id for s in sessions}
        self.cache = {sid: state for sid, state in self.cache.items() if sid in active}
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return sessions

    def load_session(
        self,
        root: Path,
        session_path: Path,
        live_pids: Set[int],
        processes: Dict[int, ProcessInfo],
        child_map: Dict[int, List[int]],
        ports: Dict[int, List[int]],
    ) -> Optional[AgentSession]:
        """Load and enrich a single Claude session from its JSON + transcript files.

        Reads the session JSON, finds the transcript JSONL, parses it incrementally,
        and fills in process info, children, ports, git status, and token stats.

        Рус: Загружает и обогащает один сеанс Claude из файлов JSON и транскрипта.
        Читает сеанс JSON, находит транскрипт JSONL, инкрементно парсит его,
        и заполняет информацию о процессе, потомках, портах, статусе git и статистике токенов.

        Args:
            root: Claude config root directory. / Корневой каталог конфигурации Claude.
            session_path: Path to the session JSON file. / Путь к файлу сеанса JSON.
            live_pids: Set of live Claude PIDs. / Набор живых PID Claude.
            processes: All process info dict. / Словарь со всей информацией о процессах.
            child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
            ports: PID-to-listening-ports mapping. / Сопоставление PID -> порты прослушивания.

        Returns:
            AgentSession if valid, or None if invalid/incomplete. / AgentSession если действителен, или None если недействителен/неполон.
        """
        try:
            data = json.loads(session_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None
        pid = safe_int(data.get("pid"))
        session_id = str(data.get("sessionId") or data.get("session_id") or "")
        cwd = str(data.get("cwd") or "")
        started_at = safe_int(data.get("startedAt") or data.get("started_at"))
        if not session_id or not cwd or pid <= 0:
            return None
        if pid not in live_pids:
            return None
        proc = processes.get(pid)
        if not proc:
            return None

        transcript_path = find_transcript_path(root, cwd, session_id)
        state = self.cache.get(session_id, TranscriptState())
        if transcript_path:
            identity = file_identity(transcript_path)
            offset = state.offset if state.file_identity == identity else 0
            delta = parse_claude_transcript_delta(
                transcript_path,
                offset,
                state.last_context_tokens if offset else 0,
                state.prev_cache_read if offset else 0,
                open_claude_tool_calls(state) if offset else None,
            )
            if offset == 0 or state.file_identity != delta.file_identity or delta.offset < offset:
                state = delta
            else:
                state = merge_transcript_state(state, delta)
            self.cache[session_id] = state

        has_active_child = has_active_descendant(pid, child_map, processes, 5.0)
        open_tools = open_claude_tool_calls(state)
        user_decision_pending = any(is_user_decision_tool_call(call) for call in open_tools.values())
        if not user_decision_pending:
            user_decision_pending = is_user_decision_task(state.current_task)
        pending_tool = bool(state.current_task)
        model_generating = state.last_user_ts_ms > 0
        wait_reason = ""
        if user_decision_pending and not has_active_child:
            status = "Waiting"
            wait_reason = WAIT_REASON_USER_DECISION
        elif has_active_child or pending_tool:
            status = "Executing"
        elif model_generating:
            status = "Thinking"
        else:
            status = "Waiting"
            wait_reason = infer_wait_reason(state.chat_messages)

        configured_model = read_configured_claude_model(cwd)
        context_window = claude_context_window(state.model, configured_model, state.max_context_tokens)
        context_percent = (
            (state.last_context_tokens / float(context_window) * 100.0)
            if context_window and state.last_context_tokens
            else 0.0
        )
        if state.current_task:
            task = state.current_task
        elif status == "Waiting":
            task = state.initial_prompt or wait_task_text(wait_reason)
        else:
            task = state.initial_prompt or "thinking..."
        branch, added, modified = collect_git_stats(cwd)
        if not branch:
            branch = state.git_branch
        return AgentSession(
            agent_cli="claude",
            pid=pid,
            session_id=session_id,
            cwd=cwd,
            project_name=last_path_segment(cwd),
            started_at=started_at,
            status=status,
            wait_reason=wait_reason,
            model=state.model,
            effort="",
            context_percent=context_percent,
            total_input_tokens=state.total_input,
            total_output_tokens=state.total_output,
            total_cache_read=state.total_cache_read,
            total_cache_create=state.total_cache_create,
            turn_count=state.turn_count,
            current_tasks=[task],
            mem_mb=int(proc.rss_kb / 1024),
            version=state.version,
            git_branch=branch,
            git_added=added,
            git_modified=modified,
            token_history=tail(state.token_history, MAX_HISTORY),
            context_history=tail(state.context_history, MAX_HISTORY),
            compaction_count=state.compaction_count,
            context_window=context_window,
            children=collect_children(pid, child_map, processes, ports),
            initial_prompt=state.initial_prompt,
            first_assistant_text=state.first_assistant_text,
            chat_messages=list(state.chat_messages),
            tool_calls=tail(state.tool_calls, 500),
            pending_since_ms=state.pending_since_ms,
            thinking_since_ms=state.thinking_since_ms,
            last_activity_ms=int(state.last_activity * 1000) if state.last_activity else started_at,
            config_root=abbrev_path(root),
        )

    def discovered_config_dirs(self) -> List[Path]:
        """Return the list of discovered config directories.

        Рус: Вернуть список обнаруженных директорий конфигурации.
        """
        return list(self.config_dirs)


# ---------------------------------------------------------------------------
# Codex collector
# ---------------------------------------------------------------------------


def is_rollout_path(path: Path) -> bool:
    """Check if a path looks like a Codex rollout JSONL file.

    Matches filenames starting with "rollout-" and ending with ".jsonl".

    Рус: Проверяет, похож ли путь на файл rollout JSONL Codex.
    Совпадает с именами файлов, начинающимися с "rollout-" и заканчивающимися на ".jsonl".

    Args:
        path: Path to check. / Путь для проверки.

    Returns:
        True if the path matches Codex rollout naming convention. / True если путь соответствует соглашению об именовании rollout Codex.
    """
    name = path.name
    return name.startswith("rollout-") and name.endswith(".jsonl")


def event_ms(value: Dict[str, Any]) -> int:
    """Extract millisecond timestamp from a Codex event dict.

    Рус: Извлекает временную метку в миллисекундах из словаря события Codex.

    Args:
        value: Event dict with optional timestamp field. / Словарь события с опциональным полем timestamp.

    Returns:
        Timestamp in ms, or 0 if not found/invalid. / Временная метка в мс, или 0 если не найдена/недействительна.
    """
    return parse_timestamp_ms(value.get("timestamp"))


def parse_codex_tool_arg(arguments: Any) -> str:
    """Extract a human-readable description from a Codex tool argument.

    Handles both JSON-string and dict arguments, extracting command, path, or pattern.

    Рус: Извлекает человекочитаемое описание из аргумента инструмента Codex.
    Обрабатывает как JSON-строки, так и аргументы-словари, извлекая команду, путь или шаблон.

    Args:
        arguments: Tool arguments (string or dict). / Аргументы инструмента (строка или словарь).

    Returns:
        Cleaned description string, or empty string. / Очищенная строка описания, или пустая строка.
    """
    if isinstance(arguments, str):
        decoded = decode_codex_tool_arguments(arguments)
        if not decoded:
            return clean_text(arguments, 80)
        arguments = decoded
    if not isinstance(arguments, dict):
        return ""
    for key in ("cmd", "command", "file_path", "path", "pattern"):
        value = arguments.get(key)
        if isinstance(value, list):
            return clean_text(" ".join(str(x) for x in value), 80)
        if isinstance(value, str) and value:
            return clean_text(value, 80)
    return ""


def parse_codex_jsonl(path: Path) -> Optional[CodexResult]:
    """Parse a Codex rollout JSONL file into a CodexResult.

    Reads session metadata, user messages, tool calls, token counts, and rate limits
    from the JSONL stream. Skips incomplete lines and malformed JSON.

    Рус: Парсит файл rollout JSONL Codex в CodexResult.
    Читает метаданные сеанса, сообщения пользователя, вызовы инструментов,
    количество токенов и ограничения из потока JSONL. Пропускает неполные строки
    и недействительный JSON.

    Args:
        path: Path to the rollout JSONL file. / Путь к файлу rollout JSONL.

    Returns:
        CodexResult with parsed data, or None if file can't be read. / CodexResult с распаршенными данными, или None если файл не может быть прочитан.
    """
    result = CodexResult()
    call_starts: Dict[str, int] = {}
    call_names: Dict[str, str] = {}
    call_indices: Dict[str, int] = {}
    approval_calls: Set[str] = set()
    pending_tasks: List[Tuple[str, str]] = []
    try:
        result.last_activity = path.stat().st_mtime
        fh = path.open("rb")
    except Exception:
        return None
    with fh:
        while True:
            raw = fh.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except Exception:
                continue
            ts_ms = event_ms(value)
            if ts_ms:
                result.last_activity = max(result.last_activity, ts_ms / 1000.0)
            typ = value.get("type")
            if typ == "session_meta":
                payload = value.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                result.session_id = str(payload.get("id") or result.session_id)
                result.cwd = str(payload.get("cwd") or result.cwd)
                result.originator = str(payload.get("originator") or result.originator)
                result.version = str(payload.get("cli_version") or result.version)
                if payload.get("timestamp"):
                    result.started_at = parse_timestamp_ms(payload.get("timestamp"))
                git = payload.get("git") or {}
                if isinstance(git, dict) and isinstance(git.get("branch"), str):
                    result.git_branch = git["branch"]

            elif typ == "event_msg":
                payload = value.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type")
                if ptype == "task_started":
                    cw = safe_int(payload.get("model_context_window"))
                    if cw:
                        result.context_window = cw
                elif ptype == "user_message":
                    result.model_generating = True
                    result.thinking_since_ms = ts_ms
                    msg = payload.get("message")
                    if isinstance(msg, str):
                        if not result.initial_prompt:
                            result.initial_prompt = clean_text(msg, 120)
                        push_chat(result.chat_messages, "user", msg, ts_ms)
                elif ptype == "token_count":
                    info = payload.get("info") or {}
                    if isinstance(info, dict):
                        total = info.get("total_token_usage") or {}
                        if isinstance(total, dict):
                            inp = safe_int(total.get("input_tokens"))
                            out = safe_int(total.get("output_tokens"))
                            cache = safe_int(
                                total.get("cached_input_tokens", total.get("cache_read_input_tokens"))
                            )
                            result.total_input = max(0, inp - cache)
                            result.total_output = out
                            result.total_cache_read = cache
                        last = info.get("last_token_usage") or {}
                        if isinstance(last, dict):
                            inp = safe_int(last.get("input_tokens"))
                            out = safe_int(last.get("output_tokens"))
                            result.last_context_tokens = inp
                            if len(result.token_history) < MAX_HISTORY:
                                result.token_history.append(inp + out)
                        cw = safe_int(info.get("model_context_window"))
                        if cw:
                            result.context_window = cw
                    rl = payload.get("rate_limits")
                    if isinstance(rl, dict) and rl.get("limit_id", "codex") == "codex":
                        info = RateLimitInfo(source="codex", updated_at=int((ts_ms or now_ms()) / 1000))
                        for slot in ("primary", "secondary"):
                            window = rl.get(slot)
                            if not isinstance(window, dict):
                                continue
                            mins = safe_int(window.get("window_minutes"))
                            pct = window.get("used_percent")
                            resets = window.get("resets_at")
                            if mins <= 300:
                                info.five_hour_pct = safe_float(pct)
                                info.five_hour_resets_at = safe_int(resets)
                            else:
                                info.seven_day_pct = safe_float(pct)
                                info.seven_day_resets_at = safe_int(resets)
                        result.rate_limit = info
                elif ptype == "agent_message":
                    result.turn_count += 1
                    result.model_generating = False
                    result.thinking_since_ms = 0
                    if ts_ms:
                        result.last_assistant_ts_ms = ts_ms
                    msg = payload.get("message")
                    if isinstance(msg, str):
                        push_chat(result.chat_messages, "assistant", msg, ts_ms)
                elif ptype == "task_complete":
                    result.task_complete = True
                    result.model_generating = False
                    result.thinking_since_ms = 0

            elif typ == "response_item":
                item = value.get("payload") or value.get("item") or {}
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or len(call_starts))
                    name = str(item.get("name") or "tool")
                    needs_approval = is_codex_approval_tool_call(name, item.get("arguments"))
                    arg = parse_codex_tool_arg(item.get("arguments"))
                    if needs_approval:
                        task = ("approval required: %s" % arg).strip()
                    else:
                        task = ("%s %s" % (name, arg)).strip()
                    pending_tasks.append((call_id, task))
                    call_starts[call_id] = ts_ms or now_ms()
                    call_names[call_id] = name
                    if needs_approval:
                        approval_calls.add(call_id)
                    call_indices[call_id] = len(result.tool_calls)
                    result.tool_calls.append(
                        ToolCall(
                            name=name,
                            arg=arg,
                            duration_ms=0,
                            call_id=call_id,
                            started_ms=call_starts[call_id],
                            needs_approval=needs_approval,
                        )
                    )
                elif item_type == "function_call_output":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    start = call_starts.pop(call_id, 0)
                    call_names.pop(call_id, None)
                    approval_calls.discard(call_id)
                    pending_tasks = [(cid, task) for cid, task in pending_tasks if cid != call_id]
                    idx = call_indices.get(call_id)
                    if start and idx is not None and idx < len(result.tool_calls):
                        completed = ts_ms or now_ms()
                        duration_hint = codex_output_duration_ms(item.get("output"))
                        call = result.tool_calls[idx]
                        if duration_hint > 0 and not call.needs_approval:
                            complete_tool_call(call, completed, duration_hint)
                        else:
                            complete_tool_call(call, completed)
                elif item_type in ("message", "assistant_message"):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        if ts_ms:
                            result.last_assistant_ts_ms = ts_ms
                        push_chat(result.chat_messages, "assistant", text, ts_ms)

            elif typ == "turn_context":
                payload = value.get("payload") or {}
                if isinstance(payload, dict):
                    if isinstance(payload.get("model"), str):
                        result.model = payload["model"]
                    if isinstance(payload.get("effort"), str):
                        result.effort = payload["effort"]
                    cw = safe_int(payload.get("model_context_window"))
                    if cw:
                        result.context_window = cw

    result.user_decision_pending = any(
        is_user_decision_tool_name(call_names.get(call_id, "")) or is_user_decision_task(task)
        or call_id in approval_calls
        for call_id, task in pending_tasks
    )
    if not result.session_id:
        return None
    decision_tasks = [
        task
        for call_id, task in pending_tasks
        if call_id in approval_calls
        or is_user_decision_tool_name(call_names.get(call_id, ""))
        or is_user_decision_task(task)
    ]
    result.current_task = decision_tasks[-1] if decision_tasks else (pending_tasks[-1][1] if pending_tasks else "")
    result.pending_since_ms = min(call_starts.values()) if call_starts else 0
    if not result.model_generating:
        result.thinking_since_ms = 0
    if not result.started_at:
        result.started_at = int(result.last_activity * 1000) if result.last_activity else now_ms()
    return result


class CodexCollector:
    """Collects Codex CLI/Desktop sessions from rollout JSONL files.

    Discovers Codex sessions by scanning ~/.codex/sessions/ and correlating
    with running codex processes.

    Рус: Собирает сеансы Codex CLI/Desktop из файлов rollout JSONL.
    Обнаруживает сеансы Codex путём сканирования ~/.codex/sessions/ и сопоставления
    с запущенными процессами codex.
    """

    def __init__(self) -> None:
        """Initialize with default Codex sessions directory.

        Рус: Инициализирует с каталогом сеансов Codex по умолчанию.
        """
        self.sessions_dir = home_dir() / ".codex" / "sessions"
        self.last_rate_limit: Optional[RateLimitInfo] = None

    def find_codex_pids(self, processes: Dict[int, ProcessInfo]) -> List[Tuple[int, bool]]:
        """Find PIDs of running Codex CLI processes.

        Filters out MCP servers, grep processes, and app servers. Deduplicates
        by keeping only top-level codex processes.

        Рус: Находит PID запущенных процессов Codex CLI.
        Фильтрует MCP-серверы, процессы grep и app-серверы. Устраняет дублирование,
        оставляя только верхнеуровневые процессы codex.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.

        Returns:
            List of (pid, is_exec) tuples. / Список кортежей (pid, is_exec).
        """
        pids: List[Tuple[int, bool]] = []
        for pid, proc in processes.items():
            cmd = proc.command
            if not cmd_has_binary(cmd, "codex"):
                continue
            if " mcp-server" in cmd or "grep" in cmd:
                continue
            if " app-server" in cmd:
                continue
            pids.append((pid, " exec" in cmd))
        candidates = list(pids)
        filtered: List[Tuple[int, bool]] = []
        for pid, is_exec in pids:
            if first_token_has_binary(processes.get(pid, ProcessInfo(pid, 0, 0, 0.0, "")).command, "codex"):
                filtered.append((pid, is_exec))
                continue
            has_codex_child = any(other != pid and is_descendant_of(other, pid, processes) for other, _ in candidates)
            if not has_codex_child:
                filtered.append((pid, is_exec))
        return filtered

    def find_desktop_pids(self, processes: Dict[int, ProcessInfo]) -> List[int]:
        """Find PIDs of running Codex Desktop app-server processes.

        Рус: Находит PID запущенных процессов Codex Desktop app-server.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.

        Returns:
            List of Codex Desktop process PIDs. / Список PID процессов Codex Desktop.
        """
        result: List[int] = []
        for pid, proc in processes.items():
            if cmd_has_binary(proc.command, "codex") and " app-server" in proc.command:
                result.append(pid)
        return sorted(result)

    def map_pid_to_jsonl(self, pids: Sequence[int]) -> Dict[int, Path]:
        """Map Codex PIDs to their open rollout JSONL files.

        On Linux, scans /proc/PID/fd; on other platforms, uses lsof.

        Рус: Сопоставляет PID Codex с их открытыми файлами rollout JSONL.
        На Linux сканирует /proc/PID/fd; на других платформах использует lsof.

        Args:
            pids: List of Codex process PIDs. / Список PID процессов Codex.

        Returns:
            Dict mapping PID to rollout JSONL path. / Словарь: PID -> путь к rollout JSONL.
        """
        result: Dict[int, Path] = {}
        if not pids:
            return result
        if sys.platform.startswith("linux"):
            for pid in pids:
                for target in scan_proc_fds(pid):
                    if is_rollout_path(target):
                        result[pid] = target
                        break
            return result
        output = run_command(["lsof", "-Fn", "-p", ",".join(str(p) for p in pids)], timeout=5.0)
        current_pid: Optional[int] = None
        for line in output.splitlines():
            if line.startswith("p"):
                current_pid = safe_int(line[1:], 0)
            elif line.startswith("n") and current_pid:
                p = Path(line[1:])
                if is_rollout_path(p):
                    result[current_pid] = p
        return result

    def recent_rollouts(self, seen: Set[Path]) -> List[Path]:
        """Find recent Codex rollout files modified within the last 10 minutes.

        Checks today's directory first, falls back to the full sessions directory.
        Returns up to 20 most recent files, excluding already-seen paths.

        Рус: Находит недавние файлы rollout Codex, изменённые за последние 10 минут.
        Сначала проверяет каталог сегодняшнего дня, переходит к полному каталогу сеансов.
        Возвращает до 20 самых недавних файлов, исключая уже просмотренные пути.

        Args:
            seen: Set of already-seen paths to skip. / Набор уже просмотренных путей для пропуска.

        Returns:
            List of recent rollout file paths, sorted newest first. / Список недавних путей файлов rollout, отсортированных от новых к старым.
        """
        if not self.sessions_dir.exists():
            return []
        cutoff = time.time() - RECENT_CODEX_SECONDS
        matches: List[Tuple[float, Path]] = []
        today = _dt.datetime.now()
        today_dir = self.sessions_dir / ("%04d" % today.year) / ("%02d" % today.month) / ("%02d" % today.day)
        roots = [today_dir] if today_dir.exists() else [self.sessions_dir]
        for root in roots:
            pattern = str(root / "**" / "rollout-*.jsonl")
            for path_text in glob.glob(pattern, recursive=True):
                p = Path(path_text)
                if p in seen:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    continue
                if mtime >= cutoff:
                    matches.append((mtime, p))
        matches.sort(reverse=True)
        return [p for _, p in matches[:20]]

    def collect(
        self,
        processes: Dict[int, ProcessInfo],
        child_map: Dict[int, List[int]],
        ports: Dict[int, List[int]],
        slow_tick: bool,
    ) -> List[AgentSession]:
        """Collect all Codex sessions from process files and recent rollouts.

        Finds CLI processes, Desktop app-server processes, and recent rollout files.
        Loads each into an AgentSession, filters out "Done" sessions.

        Рус: Собирает все сеансы Codex из файлов процессов и недавних rollouts.
        Находит CLI-процессы, процессы Desktop app-server и недавние файлы rollout.
        Загружает каждый в AgentSession, фильтрует завершённые сеансы.

        Args:
            processes: All process info dict. / Словарь со всей информацией о процессах.
            child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
            ports: PID-to-listening-ports mapping. / Сопоставление PID -> порты прослушивания.
            slow_tick: Whether this is a slow/full collection tick. / Является ли это медленным/полным тиком сбора.

        Returns:
            List of AgentSession objects, sorted by start time descending. / Список объектов AgentSession, отсортированных по времени начала (по убыванию).
        """
        self.last_rate_limit = None
        if not self.sessions_dir.exists():
            return []
        sessions: List[AgentSession] = []
        seen_paths: Set[Path] = set()
        pid_flags = dict(self.find_codex_pids(processes))
        pid_to_jsonl = self.map_pid_to_jsonl(list(pid_flags.keys()))
        for pid, path in pid_to_jsonl.items():
            session = self.load_session(path, pid, bool(pid_flags.get(pid)), True, processes, child_map, ports)
            if session:
                seen_paths.add(path)
                sessions.append(session)
        desktop_pids = self.find_desktop_pids(processes)
        desktop_paths: Dict[int, Path] = self.map_pid_to_jsonl(desktop_pids)
        for pid, path in desktop_paths.items():
            session = self.load_session(path, pid, False, False, processes, child_map, ports)
            if session:
                seen_paths.add(path)
                sessions.append(session)
        for path in self.recent_rollouts(seen_paths):
            session = self.load_session(path, None, False, False, processes, child_map, ports)
            if session:
                sessions.append(session)
        sessions = [s for s in sessions if s.status != "Done"]
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return sessions

    def load_session(
        self,
        path: Path,
        pid: Optional[int],
        is_exec: bool,
        owns_process_tree: bool,
        processes: Dict[int, ProcessInfo],
        child_map: Dict[int, List[int]],
        ports: Dict[int, List[int]],
    ) -> Optional[AgentSession]:
        """Load and enrich a single Codex session from its rollout JSONL file.

        Parses the JSONL, determines session status (Executing/Thinking/Waiting/Done),
        and fills in process info, children, ports, and token stats.

        Рус: Загружает и обогащает один сеанс Codex из его файла rollout JSONL.
        Парсит JSONL, определяет статус сеанса (Executing/Thinking/Waiting/Done),
        и заполняет информацию о процессе, потомках, портах и статистике токенов.

        Args:
            path: Path to the rollout JSONL file. / Путь к файлу rollout JSONL.
            pid: Optional PID of the owning process. / Опциональный PID владеющего процесса.
            is_exec: Whether the process is an exec-type Codex. / Является ли процесс exec-типа Codex.
            owns_process_tree: Whether PID owns the process tree. / Владеет ли PID деревом процессов.
            processes: All process info dict. / Словарь со всей информацией о процессах.
            child_map: Parent-to-children PID mapping. / Сопоставление родительский PID -> дочерние.
            ports: PID-to-listening-ports mapping. / Сопоставление PID -> порты прослушивания.

        Returns:
            AgentSession if valid, or None if parsing fails. / AgentSession если действителен, или None если парсинг не удался.
        """
        result = parse_codex_jsonl(path)
        if not result:
            return None
        proc = processes.get(pid or 0)
        pid_alive = pid is not None and proc is not None
        wait_reason = ""
        if pid is None:
            status = "Unknown"
        elif (not pid_alive) or (is_exec and result.task_complete):
            status = "Done"
        else:
            active = owns_process_tree and has_active_descendant(pid, child_map, processes, 5.0)
            if result.user_decision_pending and not active:
                status = "Waiting"
                wait_reason = WAIT_REASON_USER_DECISION
            elif active or result.pending_since_ms > 0:
                status = "Executing"
            elif result.model_generating:
                status = "Thinking"
            else:
                status = "Waiting"
                wait_reason = infer_wait_reason(result.chat_messages)
                if wait_reason == WAIT_REASON_USER_INPUT and within_codex_between_steps_grace(result):
                    wait_reason = WAIT_REASON_BETWEEN_STEPS

        context_percent = (
            result.last_context_tokens / float(result.context_window) * 100.0
            if result.context_window and result.last_context_tokens
            else 0.0
        )
        if result.current_task:
            task = result.current_task
        elif status == "Unknown":
            task = "unknown"
        elif status == "Waiting":
            task = result.initial_prompt or wait_task_text(wait_reason)
        elif status == "Thinking":
            task = result.initial_prompt or "thinking..."
        else:
            task = "finished"

        if result.rate_limit:
            if self.last_rate_limit is None or safe_int(result.rate_limit.updated_at) >= safe_int(
                self.last_rate_limit.updated_at
            ):
                self.last_rate_limit = result.rate_limit
                write_codex_rate_cache(result.rate_limit)

        branch, added, modified = collect_git_stats(result.cwd)
        if not branch:
            branch = result.git_branch
        return AgentSession(
            agent_cli="codex",
            pid=pid or 0,
            session_id=result.session_id,
            cwd=result.cwd,
            project_name=last_path_segment(result.cwd),
            started_at=result.started_at,
            status=status,
            wait_reason=wait_reason,
            model=result.model,
            effort=result.effort,
            context_percent=context_percent,
            total_input_tokens=result.total_input,
            total_output_tokens=result.total_output,
            total_cache_read=result.total_cache_read,
            total_cache_create=0,
            turn_count=result.turn_count,
            current_tasks=[task],
            mem_mb=int(proc.rss_kb / 1024) if proc and owns_process_tree else 0,
            version=result.version,
            git_branch=branch,
            git_added=added,
            git_modified=modified,
            token_history=tail(result.token_history, MAX_HISTORY),
            context_history=[],
            compaction_count=0,
            context_window=result.context_window,
            children=collect_children(pid or 0, child_map, processes, ports) if owns_process_tree and pid else [],
            initial_prompt=result.initial_prompt,
            chat_messages=list(result.chat_messages),
            tool_calls=tail(result.tool_calls, 500),
            pending_since_ms=result.pending_since_ms,
            thinking_since_ms=result.thinking_since_ms,
            last_activity_ms=int(result.last_activity * 1000) if result.last_activity else result.started_at,
            config_root=abbrev_path(self.sessions_dir.parent),
        )

    def live_rate_limit(self) -> Optional[RateLimitInfo]:
        """Return the live rate limit info, falling back to cached data.

        Рус: Вернуть информацию о лимите в реальном времени, с падением на кэшированные данные.
        """
        return self.last_rate_limit or read_codex_rate_cache()


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def read_rate_file(path: Path, source: str) -> Optional[RateLimitInfo]:
    """Read rate limit info from a JSON file.

    Parses abtop-rate-limits.json format with five_hour and seven_day windows.

    Рус: Читает информацию об ограничениях из JSON-файла.
    Парсит формат abtop-rate-limits.json с окнами five_hour и seven_day.

    Args:
        path: Path to the rate limits JSON file. / Путь к файлу ограничений JSON.
        source: Source identifier for the rate limit. / Идентификатор источника для ограничения.

    Returns:
        RateLimitInfo if file is valid, or None. / RateLimitInfo если файл действителен, или None.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    five = data.get("five_hour")
    seven = data.get("seven_day")
    if not isinstance(five, dict) and not isinstance(seven, dict):
        return None
    info = RateLimitInfo(source=str(data.get("source") or source), updated_at=safe_int(data.get("updated_at")) or None)
    if isinstance(five, dict):
        info.five_hour_pct = safe_float(five.get("used_percentage"))
        info.five_hour_resets_at = safe_int(five.get("resets_at")) or None
    if isinstance(seven, dict):
        info.seven_day_pct = safe_float(seven.get("used_percentage"))
        info.seven_day_resets_at = safe_int(seven.get("resets_at")) or None
    return info


def read_claude_rate_limits(extra_dirs: Sequence[Path]) -> List[RateLimitInfo]:
    """Read Claude rate limits from all known config directories.

    Checks ~/.claude, CLAUDE_CONFIG_DIR env var, and extra directories.
    Deduplicates by directory path.

    Рус: Читает ограничения Claude из всех известных каталогов конфигурации.
    Проверяет ~/.claude, переменную окружения CLAUDE_CONFIG_DIR и дополнительные каталоги.
    Устраняет дублирование по пути каталога.

    Args:
        extra_dirs: Additional directories to check. / Дополнительные каталоги для проверки.

    Returns:
        List of RateLimitInfo objects. / Список объектов RateLimitInfo.
    """
    candidates: List[Path] = [home_dir() / ".claude"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    candidates.extend(extra_dirs)
    results: List[RateLimitInfo] = []
    seen: Set[str] = set()
    for root in candidates:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        info = read_rate_file(root / "abtop-rate-limits.json", "claude")
        if info:
            results.append(info)
    return results


def codex_rate_cache_path() -> Path:
    """Return the path to the Codex rate limits cache file.

    Рус: Возвращает путь к файлу кэша ограничений Codex.

    Returns:
        Path to codex-rate-limits.json in XDG cache dir. / Путь к codex-rate-limits.json в каталоге кэша XDG.
    """
    return cache_dir() / "abtop" / "codex-rate-limits.json"


def read_codex_rate_cache() -> Optional[RateLimitInfo]:
    """Read the cached Codex rate limits from disk.

    Рус: Читает кэшированные ограничения Codex с диска.

    Returns:
        RateLimitInfo if cache exists and is valid, or None. / RateLimitInfo если кэш существует и действителен, или None.
    """
    return read_rate_file(codex_rate_cache_path(), "codex")


def write_codex_rate_cache(info: RateLimitInfo) -> None:
    """Write Codex rate limits to the cache file atomically.

    Uses a temp file + rename for atomicity. Creates parent directories if needed.

    Рус: Записывает ограничения Codex в файл кэша атомарно.
    Использует временный файл + переименование для атомарности. Создаёт родительские каталоги при необходимости.

    Args:
        info: Rate limit info to write. / Информация об ограничениях для записи.
    """
    path = codex_rate_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": "codex",
            "five_hour": None,
            "seven_day": None,
            "updated_at": info.updated_at,
        }
        if info.five_hour_pct is not None or info.five_hour_resets_at is not None:
            payload["five_hour"] = {
                "used_percentage": info.five_hour_pct,
                "resets_at": info.five_hour_resets_at or 0,
            }
        if info.seven_day_pct is not None or info.seven_day_resets_at is not None:
            payload["seven_day"] = {
                "used_percentage": info.seven_day_pct,
                "resets_at": info.seven_day_resets_at or 0,
            }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def setup_claude_statusline() -> None:
    """Install a Claude StatusLine helper that exports rate limits for abtop-py.

    Рус: Устанавливает helper Claude StatusLine, экспортирующий лимиты для abtop-py.
    """
    root = home_dir() / ".claude"
    root.mkdir(parents=True, exist_ok=True)
    script = root / "abtop-statusline.sh"
    script.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
out="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/abtop-rate-limits.json"
tmp="$out.tmp"
now="$(date +%s)"
input="$(cat || true)"
five_pct="$(printf '%s' "$input" | sed -n 's/.*"five_hour".*"used_percentage"[[:space:]]*:[[:space:]]*\\([0-9.]*\\).*/\\1/p' | head -n1)"
seven_pct="$(printf '%s' "$input" | sed -n 's/.*"seven_day".*"used_percentage"[[:space:]]*:[[:space:]]*\\([0-9.]*\\).*/\\1/p' | head -n1)"
five_reset="$(printf '%s' "$input" | sed -n 's/.*"five_hour".*"resets_at"[[:space:]]*:[[:space:]]*\\([0-9]*\\).*/\\1/p' | head -n1)"
seven_reset="$(printf '%s' "$input" | sed -n 's/.*"seven_day".*"resets_at"[[:space:]]*:[[:space:]]*\\([0-9]*\\).*/\\1/p' | head -n1)"
[ -z "${five_pct:-}" ] && five_json=null || five_json="{\\"used_percentage\\":$five_pct,\\"resets_at\\":${five_reset:-0}}"
[ -z "${seven_pct:-}" ] && seven_json=null || seven_json="{\\"used_percentage\\":$seven_pct,\\"resets_at\\":${seven_reset:-0}}"
printf '{"source":"claude","five_hour":%s,"seven_day":%s,"updated_at":%s}\\n' "$five_json" "$seven_json" "$now" > "$tmp"
mv "$tmp" "$out"
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    settings = root / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8", errors="replace")) if settings.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["statusLine"] = {"type": "command", "command": str(script)}
    settings.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Installed Claude StatusLine helper at %s" % script)


# ---------------------------------------------------------------------------
# Collector orchestration and snapshots
# ---------------------------------------------------------------------------


class MultiCollector:
    """Orchestrate Claude and Codex collectors, compute token rates and rate limits.

    Рус: Координирует сборщиков Claude и Codex, вычисляет скорость токенов и лимиты.
    """

    def __init__(self, hidden_agents: Set[str], claude_dirs: Sequence[Path]) -> None:
        """Initialize collectors for enabled agents.

        Рус: Инициализировать сборщиков для включенных агентов.

        Args:
            hidden_agents: Agent names to skip (e.g. {"claude"}). / Имена агентов для пропуска.
            claude_dirs: Pre-discovered .claude config directories. / Предварительно обнаруженные директории .claude.
        """
        self.hidden_agents = {x.lower() for x in hidden_agents}
        self.claude: Optional[ClaudeCollector] = None
        self.codex: Optional[CodexCollector] = None
        if "claude" not in self.hidden_agents:
            self.claude = ClaudeCollector(claude_dirs)
        if "codex" not in self.hidden_agents:
            self.codex = CodexCollector()
        self.tick_count = 5
        self.rate_limits: List[RateLimitInfo] = []
        self.prev_tokens: Dict[Tuple[str, str], int] = {}
        self.prev_project_tokens: Dict[str, int] = {}
        self.project_token_rates: Dict[str, float] = {}
        self.token_rates: List[float] = []

    def collect(self) -> List[AgentSession]:
        """Collect sessions from all enabled collectors, compute token rates and rate limits.

        Рус: Собрать сессии из всех включенных сборщиков, вычислить скорость токенов и лимиты.

        Returns:
            Filtered and sorted session list. / Отфильтрованный и отсортированный список сессий.
        """
        slow_tick = self.tick_count >= 5
        self.tick_count = 1 if slow_tick else self.tick_count + 1
        processes = get_process_info()
        cmap = children_map(processes)
        ports = get_listening_ports()
        sessions: List[AgentSession] = []
        if self.claude:
            sessions.extend(self.claude.collect(processes, cmap, ports, slow_tick))
        if self.codex:
            sessions.extend(self.codex.collect(processes, cmap, ports, slow_tick))
        sessions = [s for s in sessions if s.status != "Done"]
        sessions.sort(key=lambda s: s.started_at, reverse=True)

        rate = 0.0
        project_totals: Dict[str, int] = {}
        for s in sessions:
            key = (s.agent_cli, s.session_id)
            total = s.active_tokens()
            prev = self.prev_tokens.get(key, total)
            rate += max(0, total - prev)
            self.prev_tokens[key] = total
            project_key = s.cwd or s.project_name
            project_totals[project_key] = project_totals.get(project_key, 0) + total
        project_rates: Dict[str, float] = {}
        for key, total in project_totals.items():
            prev = self.prev_project_tokens.get(key, total)
            project_rates[key] = max(0.0, float(total - prev))
            self.prev_project_tokens[key] = total
        self.prev_project_tokens = {key: value for key, value in self.prev_project_tokens.items() if key in project_totals}
        self.project_token_rates = project_rates
        self.token_rates.append(rate)
        if len(self.token_rates) > 200:
            del self.token_rates[: len(self.token_rates) - 200]

        extra = self.claude.discovered_config_dirs() if self.claude else []
        self.rate_limits = read_claude_rate_limits(extra)
        if self.codex:
            rl = self.codex.live_rate_limit()
            if rl:
                self.rate_limits.append(rl)
        promote_rate_limited(sessions, self.rate_limits)
        return sessions


def promote_rate_limited(sessions: List[AgentSession], rate_limits: List[RateLimitInfo]) -> None:
    """Upgrade session status to RateLimited when API quota is exhausted.

    Рус: Повысить статус сессии до RateLimited, когда квота API исчерпана.
    """
    now = int(time.time())
    by_source = {rl.source.lower(): rl for rl in rate_limits}
    for session in sessions:
        if session.status != "Waiting":
            continue
        if session.wait_reason == WAIT_REASON_USER_DECISION:
            continue
        rl = by_source.get(session.agent_cli.lower())
        if not rl:
            continue
        over = False
        if rl.five_hour_pct is not None and rl.five_hour_pct >= 100:
            over = True
        if rl.seven_day_pct is not None and rl.seven_day_pct >= 100:
            over = True
        future_reset = False
        for reset in (rl.five_hour_resets_at, rl.seven_day_resets_at):
            if reset and reset > now:
                future_reset = True
        if over and future_reset:
            session.status = "RateLimited"


def session_to_json(session: AgentSession) -> Dict[str, Any]:
    """Serialize a session to a JSON-compatible dict with computed fields.

    Рус: Сериализовать сессию в JSON-совместимый словарь с вычисляемыми полями.
    """
    data = asdict(session)
    data["total_tokens"] = session.total_tokens()
    data["elapsed_secs"] = session.elapsed_seconds()
    return data


def snapshot(sessions: List[AgentSession], collector: MultiCollector, interval_ms: int) -> Dict[str, Any]:
    """Build a JSON snapshot of the current state for the API endpoint.

    Рус: Создать JSON-снимок текущего состояния для API-эндпоинта.
    """
    return {
        "generated_at_ms": now_ms(),
        "interval_ms": interval_ms,
        "token_rate": collector.token_rates[-1] if collector.token_rates else 0.0,
        "sessions": [session_to_json(s) for s in sessions],
        "rate_limits": [asdict(r) for r in collector.rate_limits],
        "aggregate": aggregate_sessions(sessions),
    }


def aggregate_sessions(sessions: List[AgentSession]) -> Dict[str, Any]:
    """Compute aggregate statistics across all sessions.

    Рус: Вычислить сводную статистику по всем сессиям.
    """
    return {
        "session_count": len(sessions),
        "active_count": len([s for s in sessions if s.status in ("Thinking", "Executing")]),
        "total_tokens": sum(s.total_tokens() for s in sessions),
        "input_tokens": sum(s.total_input_tokens for s in sessions),
        "output_tokens": sum(s.total_output_tokens for s in sessions),
        "cache_tokens": sum(s.total_cache_read + s.total_cache_create for s in sessions),
    }


def read_cpu_times() -> Optional[Tuple[int, int]]:
    """Return aggregate CPU busy/idle jiffies from /proc/stat.

    Рус: Вернуть суммарные busy/idle jiffies CPU из /proc/stat.
    """
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except Exception:
        return None
    fields = line.split()
    if not fields or fields[0] != "cpu":
        return None
    nums: List[int] = []
    for part in fields[1:]:
        try:
            nums.append(int(part))
        except Exception:
            nums.append(0)
    if len(nums) < 4:
        return None
    user, nice, system, idle = nums[:4]
    iowait = nums[4] if len(nums) > 4 else 0
    irq = nums[5] if len(nums) > 5 else 0
    softirq = nums[6] if len(nums) > 6 else 0
    steal = nums[7] if len(nums) > 7 else 0
    busy = user + nice + system + irq + softirq + steal
    idle_all = idle + iowait
    return busy, idle_all


def sample_mem_pct() -> Optional[float]:
    """Return used memory percentage from /proc/meminfo.

    Рус: Вернуть процент использованной памяти из /proc/meminfo.
    """
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    total = 0
    available = 0
    for line in lines:
        if line.startswith("MemTotal:"):
            total = safe_int(line.split()[1] if len(line.split()) > 1 else "0")
        elif line.startswith("MemAvailable:"):
            available = safe_int(line.split()[1] if len(line.split()) > 1 else "0")
        if total and available:
            break
    if total <= 0:
        return None
    return max(0.0, min(100.0, (total - available) / float(total) * 100.0))


def sample_load1() -> Optional[float]:
    """Return 1-minute load average from /proc/loadavg.

    Рус: Вернуть среднюю нагрузку за 1 минуту из /proc/loadavg.
    """
    try:
        first = Path("/proc/loadavg").read_text(encoding="utf-8", errors="replace").split()[0]
        return float(first)
    except Exception:
        return None


def sample_host_metrics(prev_cpu: Optional[Tuple[int, int]]) -> Tuple[Optional[HostMetrics], Optional[Tuple[int, int]]]:
    """Sample host CPU, memory, and load metrics.

    Рус: Снять показатели CPU, памяти и средней нагрузки хоста.
    """
    current_cpu = read_cpu_times()
    if current_cpu is None:
        return None, prev_cpu
    if prev_cpu is None:
        cpu_pct = 0.0
    else:
        busy_delta = max(0, current_cpu[0] - prev_cpu[0])
        idle_delta = max(0, current_cpu[1] - prev_cpu[1])
        total_delta = busy_delta + idle_delta
        cpu_pct = (busy_delta / float(total_delta) * 100.0) if total_delta > 0 else 0.0
    mem_pct = sample_mem_pct()
    load1 = sample_load1()
    if mem_pct is None or load1 is None:
        return None, current_cpu
    return HostMetrics(cpu_pct=cpu_pct, mem_pct=mem_pct, load1=load1), current_cpu


def header_agent_summary(sessions: Sequence[AgentSession]) -> str:
    """Return the aggregate agent summary used in the top header.

    Рус: Вернуть сводку агентов для верхней строки.
    """
    mem_values = [s.mem_mb for s in sessions if s.mem_mb > 0]
    mem_mb = sum(mem_values)
    if not mem_values:
        mem = "-"
    elif mem_mb >= 1024:
        mem = "%.1fG" % (mem_mb / 1024.0)
    else:
        mem = "%dM" % mem_mb
    ctx_values = [s.context_percent for s in sessions if s.context_percent > 0.0]
    avg_ctx = sum(ctx_values) / float(len(ctx_values)) if ctx_values else 0.0
    return "agents Σ%s ctx%%%.0f%%" % (mem, avg_ctx)


def header_host_summary(host: HostMetrics) -> str:
    """Return the host summary used in the top header.

    Рус: Вернуть сводку хоста для верхней строки.
    """
    return "CPU %2.0f%%  MEM %2.0f%%  L %.1f" % (host.cpu_pct, host.mem_pct, host.load1)


def pick_header_metrics(
    host: Optional[str],
    agent: str,
    width: int,
    base: int,
) -> Tuple[Optional[str], Optional[str]]:
    """Pick which metric blocks fit in the top header.

    Рус: Выбрать, какие блоки метрик помещаются в верхнюю строку.
    """
    if width <= 80:
        return None, None
    agent_w = len(agent) + 2
    host_w = len(host) + 3 if host else 0
    if width >= base + host_w + agent_w:
        return host, agent
    if width >= base + agent_w:
        return None, agent
    return None, None


# ---------------------------------------------------------------------------
# Text and curses UI
# ---------------------------------------------------------------------------


def status_marker(status: str) -> str:
    """Return a single-character icon for a session status.

    Рус: Вернуть односимвольную иконку для статуса сессии.
    """
    return {
        "Executing": "●",
        "Thinking": "◌",
        "Waiting": "⊙",
        "RateLimited": "⚡",
        "Unknown": "?",
    }.get(status, " ")


def status_display(status: str, wait_reason: str = "") -> str:
    """Return a short human-readable label for a session status.

    Рус: Вернуть краткую человеко-читаемую метку для статуса сессии.
    """
    if status == "Executing":
        return "Work"
    if status == "Thinking":
        return "Think"
    if status == "Waiting":
        if wait_reason == WAIT_REASON_BETWEEN_STEPS:
            return "Idle"
        return "Decide" if wait_reason == WAIT_REASON_USER_DECISION else "Wait"
    if status == "RateLimited":
        return "Limit"
    if status == "Unknown":
        return "Unk"
    return status[:5] or "-"


def agent_badge(session: AgentSession) -> str:
    """Return a 2-letter badge for the agent type (CC for Claude Code, CD for Codex).

    Рус: Вернуть 2-символьную метку типа агента (CC для Claude Code, CD для Codex).
    """
    if session.agent_cli == "claude":
        return "CC"
    if session.agent_cli == "codex":
        return "CD"
    return session.agent_cli[:2].upper()


def session_short_id(session: AgentSession) -> str:
    """Return the first 8 characters of a session ID.

    Рус: Вернуть первые 8 символов ID сессии.
    """
    sid = session.session_id or "-"
    return sid[:8]


def session_task(session: AgentSession) -> str:
    """Return the current or last task text for a session.

    Рус: Вернуть текущий или последний текст задачи для сессии.
    """
    if session.current_tasks:
        return session.current_tasks[-1]
    if session.status == "Waiting":
        return wait_task_text(session.wait_reason)
    return session.status.lower()


def session_wait_text(session: AgentSession) -> str:
    """Return the secondary waiting line for a session table row.

    Рус: Возвращает вторичную строку ожидания для строки таблицы сессии.
    """
    if session.status == "Waiting":
        return wait_task_text(session.wait_reason)
    if session.status == "RateLimited":
        return "rate limited"
    return ""


def session_summary(session: AgentSession) -> str:
    """Return a human-readable summary: initial prompt, first assistant text, or latest chat message.

    Рус: Вернуть человеко-читаемое описание: начальный промпт, первый текст ассистента или последнее сообщение чата.
    """
    if session.wait_reason == WAIT_REASON_USER_DECISION:
        for msg in reversed(session.chat_messages):
            if msg.role == "assistant" and msg.text:
                return clean_text(msg.text, 80)
    for text in (session.initial_prompt, session.first_assistant_text):
        cleaned = clean_text(text, 80)
        if cleaned:
            return cleaned
    for msg in reversed(session.chat_messages):
        if msg.role in ("user", "assistant") and msg.text:
            return clean_text(msg.text, 80)
    return session_task(session)


def format_percent(value: float) -> str:
    """Format a percentage value for display, returning '--' for non-positive values.

    Рус: Отформатировать процентное значение для отображения, вернуть '--' для неположительных значений.
    """
    if value <= 0:
        return "--"
    return "%3.0f%%" % min(999, value)


def print_once(sessions: List[AgentSession], collector: MultiCollector) -> None:
    """Print a one-shot text snapshot of all sessions to stdout (non-curses mode).

    Рус: Вывести одноразовый текстовый снимок всех сессий в stdout (режим без curses).
    """
    print("%s - %d session(s)" % (APP_NAME, len(sessions)))
    if collector.rate_limits:
        print("Rate limits:")
        for rl in collector.rate_limits:
            print("  %-6s 5h=%s 7d=%s" % (rl.source, rate_cell(rl.five_hour_pct), rate_cell(rl.seven_day_pct)))
    print()
    header = "%-7s %-6s %-12s %-10s %-10s %-7s %-8s %-5s %s"
    print(header % ("AGENT", "PID", "PROJECT", "STATUS", "MODEL", "CTX", "TOKENS", "TURN", "TASK"))
    print("-" * 100)
    for s in sessions:
        task = s.current_tasks[-1] if s.current_tasks else ""
        model = (s.model or "-").split("/")[-1]
        print(
            header
            % (
                s.agent_cli,
                s.pid or "-",
                s.project_name[:12],
                status_display(s.status, s.wait_reason),
                model[:10],
                format_percent(s.context_percent),
                human_tokens(s.total_tokens()),
                s.turn_count,
                task[:60],
            )
        )


def rate_cell(value: Optional[float]) -> str:
    """Format a rate limit percentage for display in the rate limits section.

    Рус: Отформатировать процент лимита скорости для отображения в разделе лимитов.
    """
    return "--" if value is None else "%3.0f%%" % value


def age_label(updated_at: Optional[int]) -> str:
    """Return a human-readable age string (e.g. '5m ago', '2h ago') for a timestamp.

    Рус: Вернуть человеко-читаемую строку возраста (например '5m ago', '2h ago') для временной метки.
    """
    if not updated_at:
        return "no data"
    delta = max(0, int(time.time()) - int(updated_at))
    if delta < 60:
        return "%ds ago" % delta
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    return "%dh ago" % (delta // 3600)


def reset_label(resets_at: Optional[int]) -> str:
    """Return a human-readable countdown to a rate limit reset time.

    Рус: Вернуть человеко-читаемый обратный отсчет до сброса лимита скорости.
    """
    if not resets_at:
        return ""
    delta = int(resets_at) - int(time.time())
    if delta <= 0:
        return ""
    if delta < 3600:
        return "%dm" % (delta // 60)
    hours = delta // 3600
    minutes = (delta % 3600) // 60
    if hours < 24:
        return "%dh %dm" % (hours, minutes)
    days = hours // 24
    return "%dd %dh" % (days, hours % 24)


def context_window_label(session: AgentSession) -> str:
    """Return a display label for context window usage, including compaction count.

    Рус: Вернуть метку отображения для использования контекстного окна, включая счетчик сжатия.
    """
    if not session.context_window:
        return "-"
    label = human_tokens(session.context_window).replace(".0k", "k").replace(".0M", "M")
    if session.compaction_count:
        label += " C%d" % session.compaction_count
    return label


def pct_bucket(value: Optional[float]) -> str:
    """Return a color bucket name for a percentage value (used for rate limit coloring).

    Рус: Вернуть имя цветового диапазона для процентного значения (используется для окраски лимитов).
    """
    if value is None:
        return "dim"
    if value >= 90:
        return "red"
    if value >= 70:
        return "yellow"
    if value >= 40:
        return "cyan"
    return "green"


def project_status_priority(status: str) -> int:
    """Return priority for aggregating several session statuses.

    Рус: Вернуть приоритет для агрегации нескольких статусов сессий.
    """
    return {
        "Executing": 50,
        "Thinking": 40,
        "Waiting": 30,
        "RateLimited": 20,
        "Unknown": 10,
    }.get(status, 0)


def project_activity_rows(
    sessions: Sequence[AgentSession],
    project_rates: Dict[str, float],
    ticks_per_min: int,
) -> List[ProjectActivity]:
    """Aggregate project activity rows and sort by last activity.

    Рус: Агрегировать строки активности проектов и отсортировать по последней активности.
    """
    seen: Dict[str, ProjectActivity] = {}
    for session in sessions:
        key = session.cwd or session.project_name
        last_ms = session.last_activity_ms or session.started_at
        row = seen.get(key)
        if row is None:
            seen[key] = ProjectActivity(
                session.project_name,
                session.status,
                session.wait_reason,
                last_ms,
                project_rates.get(key, 0.0) * max(1, ticks_per_min),
                1,
            )
        else:
            row.last_activity_ms = max(row.last_activity_ms, last_ms)
            row.session_count += 1
            if project_status_priority(session.status) > project_status_priority(row.status):
                row.status = session.status
                row.wait_reason = session.wait_reason
    return sorted(seen.values(), key=lambda row: row.last_activity_ms, reverse=True)


def live_port_rows(sessions: Sequence[AgentSession]) -> List[Tuple[int, str, str]]:
    """Collect listening port info from all session children.

    Рус: Собрать информацию о прослушиваемых портах из всех дочерних процессов сессий.
    """
    rows: List[Tuple[int, str, str]] = []
    for session in sessions:
        sid = session_short_id(session)
        for child in session.children:
            if child.port:
                rows.append((child.port, session.project_name, sid))
    rows.sort(key=lambda row: row[0])
    return rows


def mcp_server_rows(sessions: Sequence[AgentSession]) -> List[Tuple[str, str, str, str]]:
    """Collect MCP server rows from session child processes.

    Рус: Собирает строки MCP-серверов из дочерних процессов сессий.
    """
    rows: List[Tuple[str, str, str, str]] = []
    for session in sessions:
        for child in session.children:
            command = child.command
            if "mcp" not in command.casefold():
                continue
            tokens = command_tokens(command)
            profile = binary_name(tokens[0]) if tokens else "mcp"
            if profile in ("node", "python", "python3", "uvx", "npx") and len(tokens) > 1:
                profile = last_path_segment(tokens[1])
            last = ":%d" % child.port if child.port else human_tokens(child.mem_kb * 1024)
            rows.append((session.project_name, profile[:18], "1/1", last))
    return rows


def subagent_rows(session: AgentSession) -> List[Tuple[str, str, str]]:
    """Build display rows for subagent-like tool calls.

    Рус: Формирует строки отображения для вызовов инструментов, похожих на subagent.
    """
    rows: List[Tuple[str, str, str]] = []
    for call in session.tool_calls:
        label = call.name.casefold()
        if label not in ("agent", "task"):
            continue
        state = "√" if call.duration_ms else "•"
        detail = call.arg or call.name
        metric = human_duration_ms(call.duration_ms) if call.duration_ms else "running"
        rows.append((state, detail, metric))
    return tail(rows, 8)


def footer_rate_alert(rate_limits: Sequence[RateLimitInfo]) -> str:
    """Return a compact footer warning for exhausted or nearly exhausted quota.

    Рус: Возвращает короткое предупреждение в футере для исчерпанной или почти исчерпанной квоты.
    """
    now = int(time.time())
    for rl in rate_limits:
        pct = max(
            [value for value in (rl.five_hour_pct, rl.seven_day_pct) if value is not None] or [0.0]
        )
        if pct < 90:
            continue
        resets = [value for value in (rl.five_hour_resets_at, rl.seven_day_resets_at) if value and value > now]
        reset = min(resets) if resets else None
        label = "%s Peak Hours" % rl.source.title()
        if reset:
            label += " (resets in %s)" % reset_label(reset)
        return "⚡" + label
    return ""


def clamp(text: str, width: int) -> str:
    """Truncate text to a maximum width, appending ellipsis if needed.

    Рус: Обрезать текст до максимальной ширины, добавляя многоточие при необходимости.
    """
    if width <= 0:
        return ""
    text = str(text)
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def sparkline(values: Sequence[float], width: int) -> str:
    """Render a sparkline chart from a sequence of float values using Unicode block characters.

    Рус: Отрисовать спарклайн-диаграмму из последовательности float-значений с использованием символов Unicode блоков.
    """
    ticks = "▁▂▃▄▅▆▇█"
    if width <= 0:
        return ""
    if not values:
        return " " * width
    values = list(values)[-width:]
    mx = max(values)
    if mx <= 0:
        return ticks[0] * len(values)
    chars = []
    for value in values:
        idx = int(round((len(ticks) - 1) * value / mx))
        chars.append(ticks[max(0, min(len(ticks) - 1, idx))])
    return "".join(chars).rjust(width)


def graph_rows(values: Sequence[float], width: int, height: int) -> List[str]:
    """Render a mini bar chart from float values using Unicode block characters.

    Рус: Отрисовать мини-диаграмму столбцов из float-значений с использованием символов Unicode блоков.
    """
    if width <= 0 or height <= 0:
        return []
    if not values:
        return [" " * width for _ in range(height)]
    values = list(values)[-width:]
    if len(values) < width:
        values = [0.0] * (width - len(values)) + values
    mx = max(values) or 1.0
    levels = [int(round((height - 1) * (value / mx))) if mx > 0 else 0 for value in values]
    rows: List[str] = []
    for row in range(height):
        threshold = height - 1 - row
        chars = ["⣿" if level >= threshold and values[i] > 0 else " " for i, level in enumerate(levels)]
        rows.append("".join(chars))
    return rows


def rolling_token_rates_per_minute(values: Sequence[float], ticks_per_min: int) -> List[float]:
    """Convert per-tick token deltas into rolling tokens-per-minute values.

    Рус: Преобразовать дельты токенов за тик в скользящие значения токенов в минуту.
    """
    window = max(1, ticks_per_min)
    samples = [max(0.0, float(value)) for value in values]
    rates: List[float] = []
    running = 0.0
    for idx, value in enumerate(samples):
        running += value
        if idx >= window:
            running -= samples[idx - window]
        rates.append(running)
    return rates


def per_tick_token_rates_per_minute(values: Sequence[float], ticks_per_min: int) -> List[float]:
    """Convert each per-tick token delta into an equivalent tokens-per-minute rate.

    Рус: Преобразовать каждую дельту токенов за тик в эквивалентную скорость токенов в минуту.
    """
    multiplier = max(1, ticks_per_min)
    return [max(0.0, float(value)) * multiplier for value in values]


RU_PHYSICAL_LAYOUT = {
    "й": "q",
    "ц": "w",
    "у": "e",
    "к": "r",
    "е": "t",
    "н": "y",
    "г": "u",
    "ш": "i",
    "щ": "o",
    "з": "p",
    "х": "[",
    "ъ": "]",
    "ф": "a",
    "ы": "s",
    "в": "d",
    "а": "f",
    "п": "g",
    "р": "h",
    "о": "j",
    "л": "k",
    "д": "l",
    "ж": ";",
    "э": "'",
    "я": "z",
    "ч": "x",
    "с": "c",
    "м": "v",
    "и": "b",
    "т": "n",
    "ь": "m",
    "б": ",",
    "ю": ".",
}


def normalize_hotkey(ch: Any) -> Any:
    """Return a layout/caps-insensitive key token for command hotkeys.

    Рус: Возвращает токен горячей клавиши без учёта регистра и русской раскладки.
    """
    if isinstance(ch, str):
        if not ch:
            return ""
        folded = ch.casefold()
        return RU_PHYSICAL_LAYOUT.get(folded, folded)
    return ch


class CursesUI:
    """Curses-based terminal UI for the AI agent monitor.

    Рус: Curses-ориентированный терминальный UI для монитора AI-агентов.
    """

    def __init__(self, collector: MultiCollector, interval: float) -> None:
        """Initialize the curses UI with a collector and refresh interval.

        Рус: Инициализировать curses UI со сборщиком и интервалом обновления.

        Args:
            collector: MultiCollector instance for data collection. / Экземпляр MultiCollector для сбора данных.
            interval: Seconds between data refreshes. / Секунды между обновлениями данных.
        """
        self.collector = collector
        self.interval = interval
        self.sessions: List[AgentSession] = []
        self.selected = 0
        self.last_collect = 0.0
        self.status = ""
        self.colors: Dict[str, int] = {}
        self.show_timeline = False
        self.host_metrics: Optional[HostMetrics] = None
        self.prev_cpu_times: Optional[Tuple[int, int]] = None

    def run(self) -> None:
        """Start the curses UI loop.

        Рус: Запустить цикл curses UI.
        """
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        curses.wrapper(self._main)

    def _main(self, stdscr: Any) -> None:
        """Main curses event loop: collect data, draw, read keys.

        Рус: Главный цикл событий curses: сбор данных, отрисовка, чтение клавиш.
        """
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.init_colors()
        stdscr.keypad(True)
        stdscr.nodelay(True)
        stdscr.timeout(200)
        self.collect()
        while True:
            now = time.time()
            if now - self.last_collect >= self.interval:
                self.collect()
            self.draw(stdscr)
            ch = self.read_key(stdscr)
            if ch is None:
                continue
            key = normalize_hotkey(ch)
            if key == "q":
                break
            if key in (curses.KEY_DOWN, "j"):
                self.selected = min(max(0, len(self.sessions) - 1), self.selected + 1)
            elif key in (curses.KEY_UP, "k"):
                self.selected = max(0, self.selected - 1)
            elif key == curses.KEY_HOME:
                self.selected = 0
            elif key == curses.KEY_END:
                self.selected = max(0, len(self.sessions) - 1)
            elif key == "r":
                self.collect()
            elif key in ("v", "l"):
                self.show_timeline = not self.show_timeline

    def read_key(self, stdscr: Any) -> Optional[Any]:
        """Read a single key press from the terminal, handling both WCH and CH modes.

        Рус: Прочитать одно нажатие клавиши из терминала, обрабатывая оба режима WCH и CH.
        """
        try:
            return stdscr.get_wch()
        except curses.error:
            return None
        except AttributeError:
            ch = stdscr.getch()
            return None if ch == -1 else ch

    def init_colors(self) -> None:
        """Initialize curses color pairs for the UI.

        Рус: Инициализировать цветовые пары curses для UI.
        """
        try:
            curses.start_color()
            curses.use_default_colors()
        except Exception:
            return

        def pair(name: str, idx: int, fg: int, bg: int = -1) -> None:
            """Initialize a curses color pair by name.

            Рус: Инициализировать цветовую пару curses по имени.
            """
            try:
                curses.init_pair(idx, fg, bg)
                self.colors[name] = curses.color_pair(idx)
            except Exception:
                self.colors[name] = 0

        if getattr(curses, "COLORS", 0) >= 256:
            pair("fg", 1, 252)
            pair("dim", 2, 244)
            pair("green", 3, 108)
            pair("cyan", 4, 109)
            pair("yellow", 5, 180)
            pair("red", 6, 167)
            pair("blue", 7, 67)
            pair("magenta", 8, 139)
            pair("selected", 9, 255, 52)
            pair("header", 10, 252)
            pair("orange", 11, 173)
        else:
            pair("fg", 1, curses.COLOR_WHITE)
            pair("dim", 2, curses.COLOR_WHITE)
            pair("green", 3, curses.COLOR_GREEN)
            pair("cyan", 4, curses.COLOR_CYAN)
            pair("yellow", 5, curses.COLOR_YELLOW)
            pair("red", 6, curses.COLOR_RED)
            pair("blue", 7, curses.COLOR_BLUE)
            pair("magenta", 8, curses.COLOR_MAGENTA)
            pair("selected", 9, curses.COLOR_WHITE, curses.COLOR_BLUE)
            pair("header", 10, curses.COLOR_WHITE)
            pair("orange", 11, curses.COLOR_YELLOW)

    def attr(self, name: str, bold: bool = False, dim: bool = False) -> int:
        """Build a curses attribute mask from a color name with optional bold/dim modifiers.

        Рус: Создать маску атрибутов curses из имени цвета с опциональными модификаторами bold/dim.
        """
        attr = self.colors.get(name, 0)
        if name == "dim":
            attr |= curses.A_DIM
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        return attr

    def collect(self) -> None:
        """Refresh data from the collector and update the session list.

        Рус: Обновить данные из сборщика и обновить список сессий.
        """
        try:
            self.host_metrics, self.prev_cpu_times = sample_host_metrics(self.prev_cpu_times)
            self.sessions = self.collector.collect()
            self.last_collect = time.time()
            if self.selected >= len(self.sessions):
                self.selected = max(0, len(self.sessions) - 1)
            self.status = "updated %s" % time.strftime("%H:%M:%S")
        except Exception as exc:
            self.status = "collect error: %s" % exc

    def draw(self, stdscr: Any) -> None:
        """Erase the screen and redraw all panels.

        Рус: Очистить экран и перерисовать все панели.
        """
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        try:
            stdscr.bkgd(" ", self.attr("fg"))
        except Exception:
            pass
        if height < 18 or width < 80:
            self.add(stdscr, 0, 0, "Terminal too small for %s" % APP_NAME, width)
            stdscr.refresh()
            return

        y = 0
        footer_h = 1
        self.draw_header(stdscr, y, width)
        y += 1
        available = max(0, height - y - footer_h)
        context_h = min(10, max(5, len(self.sessions) + 4)) if available >= 24 and width >= 100 else 0
        mid_h = 9 if available - context_h >= 17 and width >= 100 else 0
        sessions_h = max(5, height - y - footer_h - context_h - mid_h)

        if context_h:
            self.draw_context_panel(stdscr, y, 0, context_h, width)
            y += context_h
        if mid_h:
            gap = 1
            col_w = (width - 4 * gap) // 5
            widths = [col_w, col_w, col_w, col_w, width - 4 * col_w - 4 * gap]
            x = 0
            self.draw_quota_panel(stdscr, y, x, mid_h, widths[0])
            x += widths[0] + gap
            self.draw_tokens_panel(stdscr, y, x, mid_h, widths[1])
            x += widths[1] + gap
            self.draw_projects_panel(stdscr, y, x, mid_h, widths[2])
            x += widths[2] + gap
            self.draw_ports_panel(stdscr, y, x, mid_h, widths[3])
            x += widths[3] + gap
            self.draw_mcp_panel(stdscr, y, x, mid_h, widths[4])
            y += mid_h

        self.draw_sessions_panel(stdscr, y, 0, max(5, height - y - footer_h), width)
        self.draw_footer(stdscr, height - 1, width)
        stdscr.refresh()

    def draw_header(self, stdscr: Any, y: int, width: int) -> None:
        """Draw the btop-style top status line.

        Рус: Отрисовать верхнюю btop-style строку статуса.
        """
        session_count = len(self.sessions)
        active = len([s for s in self.sessions if s.status in ("Thinking", "Executing")])
        now = time.strftime("%H:%M")
        title = " %s v%s " % (APP_NAME, __VERSION__)
        right = " %s  %d↑ %d● " % (now, active, session_count)
        host_str = header_host_summary(self.host_metrics) if self.host_metrics else None
        agent_str = header_agent_summary(self.sessions)
        base = len(title) + len(right) + 4
        host_render, agent_render = pick_header_metrics(host_str, agent_str, width, base)

        self.add(stdscr, y, 0, " " * width, width, self.attr("header", bold=True))
        x = 0
        self.add(stdscr, y, x, title, width - x, self.attr("fg", bold=True))
        x += len(title)
        if host_render:
            text = " %s " % host_render
            self.add(stdscr, y, x, text, width - x, self.attr("dim", bold=True))
            x += len(text)
        if host_render and agent_render:
            self.add(stdscr, y, x, "─", width - x, self.attr("dim"))
            x += 1
        if agent_render:
            text = " %s " % agent_render
            self.add(stdscr, y, x, text, width - x, self.attr("dim", bold=True))
            x += len(text)

        pad = max(0, width - x - len(right))
        x += pad
        self.add(stdscr, y, x, " %s  " % now, width - x, self.attr("dim", bold=True))
        x += len(" %s  " % now)
        active_text = "%d↑" % active
        self.add(stdscr, y, x, active_text, width - x, self.attr("green", bold=True))
        x += len(active_text)
        sessions_text = " %d●  " % session_count
        self.add(stdscr, y, x, sessions_text, width - x, self.attr("fg", bold=True))

    def draw_footer(self, stdscr: Any, y: int, width: int) -> None:
        """Draw the footer with hotkey hints and session count.

        Рус: Отрисовать нижнюю панель с подсказками горячих клавиш и счетчиком сессий.
        """
        left = "↑↓ select  v view  r refresh  q quit  %ss auto" % int(self.interval)
        alert = footer_rate_alert(self.collector.rate_limits)
        right = "%d sessions" % len(self.sessions)
        self.add(stdscr, y, 0, " " * width, width, self.attr("dim"))
        self.add(stdscr, y, 1, clamp(left, width - len(right) - 3), width, self.attr("fg", bold=True))
        if alert:
            alert_x = min(max(1, len(left) + 4), max(1, width - len(alert) - len(right) - 3))
            self.add(stdscr, y, alert_x, clamp(alert, max(0, width - alert_x - len(right) - 3)), width, self.attr("orange", bold=True))
        self.add(stdscr, y, max(0, width - len(right) - 1), right, width, self.attr("dim", bold=True))

    def draw_context_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the context window usage panel with token rate graph.

        Рус: Отрисовать панель использования контекстного окна с графиком скорости токенов.
        """
        self.box(stdscr, y, x, h, w, "context", "¹", "blue")
        inner_y = y + 1
        inner_h = max(0, h - 2)
        agg = aggregate_sessions(self.sessions)
        ticks_per_min = max(1, int(60.0 / max(0.5, self.interval)))
        rate_history = rolling_token_rates_per_minute(self.collector.token_rates, ticks_per_min)
        graph_history = per_tick_token_rates_per_minute(self.collector.token_rates, ticks_per_min)
        tokens_per_min = int(rate_history[-1]) if rate_history else 0
        left_w = min(36, max(24, w // 4))
        right_w = min(74, max(42, w // 3))
        graph_w = max(8, w - left_w - right_w - 6)
        graph_x = x + left_w + 2
        right_x = x + w - right_w - 1

        self.add(stdscr, inner_y, x + 2, "Token Rate ", w, self.attr("dim", bold=True))
        rate_text = "%s/min" % human_tokens(tokens_per_min)
        self.add(stdscr, inner_y, x + 14, rate_text, w, self.attr("green", bold=True))
        self.add(stdscr, y + h - 2, x + 2, "%s Total" % human_tokens(agg["total_tokens"]), w, self.attr("fg", bold=True))

        rows = graph_rows(graph_history, graph_w, max(1, inner_h - 2))
        for idx, row in enumerate(rows):
            self.add(stdscr, inner_y + 1 + idx, graph_x, row, w, self.attr("orange"))
        axis = relative_time_axis(graph_w, self.interval, 30) if graph_w > 1 else ""
        if axis:
            self.add(stdscr, y + h - 2, graph_x, axis, w, self.attr("dim", bold=True))

        self.add(stdscr, inner_y, right_x, "Project", w, self.attr("fg", bold=True))
        self.add(stdscr, inner_y, right_x + 18, "Context", w, self.attr("fg", bold=True))
        self.add(stdscr, inner_y, right_x + 52, "Window", w, self.attr("fg", bold=True))
        max_rows = max(0, inner_h - 1)
        for idx, session in enumerate(self.sessions[:max_rows]):
            row_y = inner_y + 1 + idx
            self.add(stdscr, row_y, right_x, clamp(session.project_name, 16), w, self.attr("fg", bold=True))
            bar_x = right_x + 18
            self.draw_meter(stdscr, row_y, bar_x, 24, session.context_percent, pct_bucket(session.context_percent))
            pct = format_percent(session.context_percent)
            self.add(stdscr, row_y, bar_x + 26, pct, w, self.attr(pct_bucket(session.context_percent), bold=True))
            self.add(stdscr, row_y, right_x + 52, context_window_label(session), w, self.attr("dim", bold=True))

    def draw_quota_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the rate limit quota panel with usage bars.

        Рус: Отрисовать панель квот лимитов скорости с полосами использования.
        """
        self.box(stdscr, y, x, h, w, "quota", "²", "green")
        sources = {rl.source.lower(): rl for rl in self.collector.rate_limits}
        col_w = max(14, (w - 4) // 2)
        for idx, source in enumerate(("claude", "codex")):
            col_x = x + 2 + idx * col_w
            rl = sources.get(source)
            self.add(stdscr, y + 1, col_x, source.upper(), w, self.attr("fg", bold=True))
            if rl:
                updated = "upd %s" % compact_age_label((rl.updated_at or 0) * 1000)
                self.add(stdscr, y + 2, col_x, updated, w, self.attr("dim"))
                self.draw_quota_window(stdscr, y + 3, col_x, col_w, "5h", rl.five_hour_pct, rl.five_hour_resets_at)
                self.draw_quota_window(stdscr, y + 5, col_x, col_w, "7d", rl.seven_day_pct, rl.seven_day_resets_at)
            else:
                self.add(stdscr, y + 2, col_x, "- no data", w, self.attr("dim"))
                self.add(stdscr, y + 3, col_x, "./abtop-py.py --setup" if source == "claude" else "run codex once", w, self.attr("dim"))
        ticks_per_min = max(1, int(60.0 / max(0.5, self.interval)))
        rate_history = rolling_token_rates_per_minute(self.collector.token_rates, ticks_per_min)
        tokens_per_min = int(rate_history[-1]) if rate_history else 0
        total = sum(s.total_tokens() for s in self.sessions)
        self.add(stdscr, y + h - 2, x + 2, "total %s %s/min" % (human_tokens(total), human_tokens(tokens_per_min)), w, self.attr("fg", bold=True))

    def draw_quota_window(
        self,
        stdscr: Any,
        y: int,
        x: int,
        w: int,
        label: str,
        pct: Optional[float],
        reset: Optional[int],
    ) -> None:
        """Draw a single quota bar with percentage and reset countdown.

        Рус: Отрисовать одну полосу квоты с процентом и обратным отсчетом до сброса.
        """
        self.add(stdscr, y, x, label, w, self.attr("dim", bold=True))
        if pct is None:
            self.add(stdscr, y, x + 3, "no data", w, self.attr("dim"))
            return
        bar_w = min(10, max(4, w - 12))
        self.draw_meter(stdscr, y, x + 3, bar_w, pct, pct_bucket(pct))
        self.add(stdscr, y, x + 4 + bar_w, "%3.0f%%" % pct, w, self.attr(pct_bucket(pct), bold=True))
        self.add(stdscr, y + 1, x, reset_label(reset), w, self.attr("dim", bold=True))

    def draw_tokens_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the token breakdown panel showing input/output/cache usage.

        Рус: Отрисовать панель разбивки токенов, показывающую использование вход/выход/кэш.
        """
        selected = self.current_session()
        title = "tokens"
        if selected:
            title = "tokens (%s/%s)" % (clamp(selected.project_name, 10), session_short_id(selected))
        self.box(stdscr, y, x, h, w, title, "³", "magenta")
        if not selected:
            self.add(stdscr, y + 1, x + 2, "No session selected", w, self.attr("dim"))
            return
        self.add(stdscr, y + 1, x + 2, "Total: %s" % human_tokens(selected.total_tokens()), w, self.attr("fg", bold=True))
        values = [
            ("Input", selected.total_input_tokens, "green"),
            ("Output", selected.total_output_tokens, "red"),
            ("CacheR", selected.total_cache_read, "cyan"),
            ("CacheW", selected.total_cache_create, "blue"),
        ]
        max_value = max(1, max(v for _, v, _ in values))
        bar_w = max(4, w - 22)
        for idx, (label, value, color) in enumerate(values[: max(0, h - 4)]):
            row_y = y + 2 + idx
            self.add(stdscr, row_y, x + 2, "%-6s:" % label, w, self.attr("dim", bold=True))
            self.draw_meter(stdscr, row_y, x + 10, min(bar_w, 18), value / float(max_value) * 100.0, color)
            self.add(stdscr, row_y, x + 12 + min(bar_w, 18), human_tokens(value), w, self.attr(color, bold=True))
        if h >= 7:
            graph = sparkline(selected.token_history, max(6, w - 18))
            self.add(stdscr, y + h - 2, x + 2, clamp(graph + " tokens/turn", w - 4), w, self.attr("orange"))

    def draw_projects_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the projects panel showing one activity row per project.

        Рус: Отрисовать панель проектов с одной строкой активности на проект.
        """
        self.box(stdscr, y, x, h, w, "projects", "⁴", "magenta")
        ticks_per_min = max(1, int(60.0 / max(0.5, self.interval)))
        rows = project_activity_rows(self.sessions, self.collector.project_token_rates, ticks_per_min)
        if not rows:
            self.add(stdscr, y + 1, x + 2, "No projects", w, self.attr("dim"))
            return
        state_w = 6
        last_w = 5
        rate_w = 7
        name_w = max(8, w - state_w - last_w - rate_w - 8)
        header = "%-*s %-*s %*s %*s" % (name_w, "Project", state_w, "State", last_w, "Last", rate_w, "Tok/m")
        self.add(stdscr, y + 1, x + 2, clamp(header, w - 4), w, self.attr("fg", bold=True))
        row_y = y + 2
        for row in rows:
            if row_y >= y + h - 1:
                break
            state = status_display(row.status, row.wait_reason)
            if row.session_count > 1:
                state = "%s×%d" % (state[: max(1, state_w - 2)], row.session_count)
            last = compact_age_label(row.last_activity_ms)
            rate = human_tokens(int(row.token_rate))
            line = "%-*s %-*s %*s %*s" % (
                name_w,
                row.name[:name_w],
                state_w,
                state[:state_w],
                last_w,
                last[:last_w],
                rate_w,
                rate[:rate_w],
            )
            color = "green"
            if row.status == "Executing":
                color = "green"
            elif row.status == "Thinking":
                color = "cyan"
            elif row.status == "Waiting":
                color = "dim" if row.wait_reason == WAIT_REASON_BETWEEN_STEPS else "yellow"
            elif row.status == "RateLimited":
                color = "orange"
            elif row.status == "Unknown":
                color = "dim"
            self.add(stdscr, row_y, x + 2, clamp(line, w - 4), w, self.attr(color, bold=color != "dim"))
            row_y += 1

    def draw_ports_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the open ports panel listing child process listening ports.

        Рус: Отрисовать панель открытых портов, перечисляющую порты дочерних процессов.
        """
        self.box(stdscr, y, x, h, w, "ports", "⁵", "yellow")
        self.add(stdscr, y + 1, x + 2, "PORT   SESSION", w, self.attr("fg", bold=True))
        rows = live_port_rows(self.sessions)
        if not rows:
            self.add(stdscr, y + 2, x + 2, "no open ports", w, self.attr("dim"))
            return
        for idx, (port, project, sid) in enumerate(rows[: max(0, h - 3)]):
            self.add(stdscr, y + 2 + idx, x + 2, ":%-5d" % port, w, self.attr("green", bold=True))
            self.add(stdscr, y + 2 + idx, x + 10, clamp("%s %s" % (project, sid), w - 12), w, self.attr("fg", bold=True))

    def draw_mcp_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the MCP servers panel.

        Рус: Отрисовать панель MCP-серверов.
        """
        self.box(stdscr, y, x, h, w, "mcp servers", "⁷", "blue")
        self.add(stdscr, y + 1, x + 2, "PARENT  PROFILE      ACT/TOT LAST", w, self.attr("fg", bold=True))
        rows = mcp_server_rows(self.sessions)
        if not rows:
            self.add(stdscr, y + 2, x + 2, "no mcp servers", w, self.attr("dim"))
            return
        for idx, (parent, profile, active, last) in enumerate(rows[: max(0, h - 3)]):
            row_y = y + 2 + idx
            line = "%-7s %-12s %-7s %s" % (parent[:7], profile[:12], active, last)
            self.add(stdscr, row_y, x + 2, clamp(line, w - 4), w, self.attr("fg", bold=True))

    def draw_sessions_panel(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the sessions panel with the session table and detail view.

        Рус: Отрисовать панель сессий с таблицей сессий и детальной информацией.
        """
        self.box(stdscr, y, x, h, w, "sessions", "⁶", "red")
        if not self.sessions:
            self.add(stdscr, y + 2, x + 2, "No Claude Code or Codex sessions found.", w, self.attr("dim"))
            return
        table_needed = len(self.sessions) * 2 + 1
        if h >= 18:
            table_h = min(max(2, table_needed), max(2, h - 12))
            detail_h = max(0, h - 2 - table_h - 1)
        else:
            detail_h = min(8, max(5, h // 3)) if h >= 12 else 0
            table_h = max(2, h - 2 - detail_h - (1 if detail_h else 0))
        self.draw_session_table(stdscr, y + 1, x + 1, table_h, w - 2)
        if detail_h:
            div_y = y + 1 + table_h
            self.hline(stdscr, div_y, x + 1, w - 2, "red")
            self.draw_session_detail(stdscr, div_y + 1, x + 2, detail_h - 1, w - 4)

    def draw_session_table(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the session list table with header and rows.

        Рус: Отрисовать таблицу списка сессий с заголовком и строками.
        """
        header = " AI  Pid     Project        Session   Config        Summary                      Status   Model          Context Tokens  Memory Last  Turn"
        self.add(stdscr, y, x, clamp(header, w), w, self.attr("fg", bold=True))
        rows_avail = max(0, h - 1)
        row_span = 2 if rows_avail >= 2 else 1
        visible_sessions = max(1, rows_avail // row_span)
        start = 0
        if self.selected >= visible_sessions and visible_sessions > 0:
            start = self.selected - visible_sessions + 1
        for idx in range(visible_sessions):
            session_idx = start + idx
            row_y = y + 1 + idx * row_span
            if session_idx >= len(self.sessions):
                for clear_idx in range(row_span):
                    self.add(stdscr, row_y + clear_idx, x, " " * w, w)
                continue
            session = self.sessions[session_idx]
            selected = session_idx == self.selected
            attr = self.attr("selected") if selected else self.attr("fg", bold=True)
            marker = ">" if selected else " "
            pid = str(session.pid or "-")
            model = (session.model or "-").split("/")[-1]
            status_text = "%s %s" % (status_marker(session.status), status_display(session.status, session.wait_reason))
            config = session.config_root or "-"
            last_text = compact_age_label(session.last_activity_ms or session.started_at)
            line = "%s*%-2s %-7s %-14s %-9s %-13s %-28s %-8s %-14s %-7s %-7s %-6s %-5s %4d" % (
                marker,
                agent_badge(session),
                pid[:7],
                session.project_name[:14],
                session_short_id(session),
                config[:13],
                session_summary(session)[:28],
                status_text[:8],
                model[:14],
                format_percent(session.context_percent),
                human_tokens(session.total_tokens()),
                "%dM" % session.mem_mb if session.mem_mb else "-",
                last_text[:5],
                session.turn_count,
            )
            self.add(stdscr, row_y, x, clamp(line, w), w, attr)
            if row_span > 1 and row_y + 1 < y + h:
                wait_text = session_wait_text(session)
                if wait_text:
                    subline = "%-52s└ %s" % ("", wait_text)
                    self.add(stdscr, row_y + 1, x, clamp(subline, w), w, self.attr("dim", bold=True))
                else:
                    self.add(stdscr, row_y + 1, x, " " * w, w)

    def draw_session_detail(self, stdscr: Any, y: int, x: int, h: int, w: int) -> None:
        """Draw the selected session's detail view: task, tool calls, timeline.

        Рус: Отрисовать детальное представление выбранной сессии: задача, вызовы инструментов, временная шкала.
        """
        session = self.current_session()
        if not session or h <= 0:
            return
        task = session_task(session)
        self.add(
            stdscr,
            y,
            x,
            "SESSION (>%s · %s)" % (session.session_id, session.cwd),
            w,
            self.attr("fg", bold=True),
        )
        if h > 1:
            self.add(stdscr, y + 1, x + 2, "task %s" % task, w, self.attr("dim", bold=True))

        body_y = y + 3
        body_h = max(0, h - 3)
        if self.show_timeline and (session.tool_calls or session.thinking_since_ms > 0):
            self.draw_timeline(stdscr, body_y, x, body_h, w, session)
            return

        footer_lines = 2 if body_h >= 7 else 0
        content_h = max(0, body_h - footer_lines)
        left_w = max(32, min(w // 2, 96))
        right_x = x + left_w + 4
        right_w = max(0, w - left_w - 4)
        if h > 3:
            self.draw_subagents(stdscr, body_y, x, content_h, left_w, session)
        if right_w > 20 and h > 3:
            self.draw_chat(stdscr, body_y, right_x, content_h, right_w, session)
        if footer_lines:
            footer_y = y + h - footer_lines
            self.add(
                stdscr,
                footer_y,
                x,
                "MEM %s · %d/%d chat · %d calls" % (
                    "%dM" % session.mem_mb if session.mem_mb else "0",
                    len(session.chat_messages),
                    MAX_CHAT_MESSAGES,
                    len(session.tool_calls),
                ),
                w,
                self.attr("dim", bold=True),
            )
            ctx_graph = sparkline(session.context_history, min(36, max(4, w - 24)))
            self.add(stdscr, footer_y + 1, x, "CTX %s" % ctx_graph, w, self.attr("orange"))
            self.add(stdscr, footer_y + 1, x + 4 + len(ctx_graph) + 2, context_window_label(session), w, self.attr("orange", bold=True))

    def draw_subagents(self, stdscr: Any, y: int, x: int, h: int, w: int, session: AgentSession) -> None:
        """Draw the subagents area for the selected session.

        Рус: Отрисовать область subagents для выбранной сессии.
        """
        if h <= 0:
            return
        self.add(stdscr, y, x, "SUBAGENTS", w, self.attr("fg", bold=True))
        rows = subagent_rows(session)
        if not rows:
            self.add(stdscr, y + 1, x + 2, "none", w, self.attr("dim"))
            return
        for idx, (state, detail, metric) in enumerate(rows[: max(0, h - 1)]):
            row_y = y + 1 + idx
            metric_w = min(10, max(0, w // 5))
            text_w = max(8, w - metric_w - 4)
            self.add(stdscr, row_y, x, state, w, self.attr("green" if state == "√" else "yellow", bold=True))
            self.add(stdscr, row_y, x + 2, clamp(detail, text_w), w, self.attr("dim", bold=True))
            self.add(stdscr, row_y, x + w - metric_w, clamp(metric, metric_w), w, self.attr("dim", bold=True))

    def draw_chat(self, stdscr: Any, y: int, x: int, h: int, w: int, session: AgentSession) -> None:
        """Draw recent chat messages for the selected session.

        Рус: Отрисовать последние сообщения чата выбранной сессии.
        """
        if h <= 0:
            return
        last_chat_ms = 0
        for msg in reversed(session.chat_messages):
            if msg.timestamp_ms > 0:
                last_chat_ms = msg.timestamp_ms
                break
        last_chat = date_clock_label_ms(last_chat_ms)
        title = "CHAT (%d, last %s)" % (len(session.chat_messages), last_chat) if last_chat else "CHAT (%d)" % len(session.chat_messages)
        self.add(stdscr, y, x, clamp(title, w), w, self.attr("fg", bold=True))
        rows = tail(session.chat_messages, max(0, h - 1))
        if not rows:
            self.add(stdscr, y + 1, x + 2, "no chat messages", w, self.attr("dim"))
            return
        for idx, msg in enumerate(rows):
            row_y = y + 1 + idx
            role = "U" if msg.role == "user" else "A"
            color = "red" if role == "U" else "green"
            self.add(stdscr, row_y, x, role, w, self.attr(color, bold=True))
            self.add(stdscr, row_y, x + 2, clamp(msg.text, max(0, w - 2)), w, self.attr("fg", bold=True))

    def draw_timeline(self, stdscr: Any, y: int, x: int, h: int, w: int, session: AgentSession) -> None:
        """Draw a horizontal timeline showing tool calls and thinking periods.

        Рус: Отрисовать горизонтальную временную шкалу, показывающую вызовы инструментов и периоды размышления.
        """
        if h <= 0 or w <= 20:
            return
        now = now_ms()
        raw_calls = list(session.tool_calls)
        calls = collapse_approval_timeline_calls(raw_calls, now)
        thinking = session.thinking_since_ms > 0 and session.status in (
            "Thinking",
            "Executing",
            "Waiting",
            "Unknown",
        )

        def duration_for(index: int, call: ToolCall) -> int:
            """Return the duration in milliseconds for a tool call.

            Рус: Вернуть длительность в миллисекундах для вызова инструмента.
            """
            if call.name == "queued":
                return 0
            if call.duration_ms > 0:
                return call.duration_ms
            if call.started_ms > 0 and call.completed_ms <= 0:
                return max(0, now - call.started_ms)
            if session.pending_since_ms > 0 and index == len(calls) - 1:
                return max(0, now - session.pending_since_ms)
            return 0

        durations = [duration_for(idx, call) for idx, call in enumerate(calls)]
        total_duration = sum(durations)
        pending_count = len(
            [
                call
                for idx, call in enumerate(calls)
                if call.name != "queued"
                and call.duration_ms == 0
                and (
                    (call.started_ms > 0 and call.completed_ms <= 0)
                    or (session.pending_since_ms > 0 and idx == len(calls) - 1)
                )
            ]
        )
        pending_decision_count = len(
            [
                call
                for idx, call in enumerate(calls)
                if call.needs_approval
                and call.duration_ms == 0
                and (
                    (call.started_ms > 0 and call.completed_ms <= 0)
                    or (session.pending_since_ms > 0 and idx == len(calls) - 1)
                )
            ]
        )
        thinking_duration = max(0, now - session.thinking_since_ms) if thinking else 0
        max_duration = max([1] + durations + ([thinking_duration] if thinking else []))

        notes: List[str] = []
        if pending_count:
            if pending_decision_count or session.wait_reason == WAIT_REASON_USER_DECISION:
                notes.append("waiting for decision")
            else:
                notes.append("%d running" % pending_count)
        if thinking:
            notes.append("thinking %s" % human_duration_ms(thinking_duration))
        range_starts: List[int] = []
        range_ends: List[int] = []
        range_ends_now = False
        for call in raw_calls:
            if call.started_ms > 0:
                range_starts.append(call.started_ms)
                if call.duration_ms <= 0 and call.completed_ms <= 0:
                    range_ends.append(now)
                    range_ends_now = True
                elif call.completed_ms > 0:
                    range_ends.append(call.completed_ms)
                elif call.duration_ms > 0:
                    range_ends.append(call.started_ms + call.duration_ms)
        if thinking and session.thinking_since_ms > 0:
            range_starts.append(session.thinking_since_ms)
            range_ends.append(now)
            range_ends_now = True
        meta = ["%d calls" % len(raw_calls), human_duration_ms(total_duration)]
        if range_starts and range_ends:
            range_label = time_range_label_ms(min(range_starts), max(range_ends), range_ends_now)
            if range_label:
                meta.append(range_label)
        meta.extend(notes)
        title = "TIMELINE (%s)" % ", ".join(meta)
        self.add(stdscr, y, x, clamp(title, w), w, self.attr("fg", bold=True))
        if h == 1:
            return

        name_w = 8
        arg_w = min(32, max(16, w // 5))
        duration_w = 10
        bar_x = x + name_w + arg_w + 3
        bar_w = max(5, w - name_w - arg_w - duration_w - 5)
        visible_rows = max(0, h - 1 - (1 if thinking else 0))
        start = max(0, len(calls) - visible_rows)

        longest = max_duration if calls else 0
        row_y = y + 1
        for idx in range(start, len(calls)):
            if row_y >= y + h:
                break
            call = calls[idx]
            duration = durations[idx]
            pending = call.name != "queued" and call.duration_ms == 0 and (
                (call.started_ms > 0 and call.completed_ms <= 0)
                or (session.pending_since_ms > 0 and idx == len(calls) - 1)
            )
            approval_call = call.needs_approval
            pending_decision = pending and approval_call
            color = "red" if approval_call else self.tool_color(call.name)
            label = "Approve" if approval_call else self.tool_label(call.name)
            star = " *" if duration == longest and duration > 0 and not pending and not approval_call else ""
            soft_tone = color in ("orange", "yellow", "red")
            self.add(stdscr, row_y, x, clamp(label, name_w - 1), w, self.attr(color, bold=not soft_tone))
            self.add(stdscr, row_y, x + name_w, clamp(call.arg, arg_w), w, self.attr("dim", bold=True))
            fill = int(math.ceil((duration / float(max_duration)) * bar_w)) if max_duration else 0
            fill = max(1 if duration > 0 else 0, min(bar_w, fill))
            self.add(stdscr, row_y, bar_x, "█" * fill, w, self.attr(color, bold=(not pending and not soft_tone), dim=pending))
            if fill < bar_w:
                self.add(stdscr, row_y, bar_x + fill, "░" * (bar_w - fill), w, self.attr("dim", dim=True))
            duration_text = "queued" if call.name == "queued" else "%s%s" % (human_duration_ms(duration), "…" if pending else star)
            self.add(stdscr, row_y, x + w - duration_w, "%9s" % duration_text, w, self.attr("dim" if not star else "yellow", bold=not star))
            row_y += 1

        if thinking and row_y < y + h:
            fill = int(math.ceil((thinking_duration / float(max_duration)) * bar_w)) if max_duration else bar_w
            fill = max(1, min(bar_w, fill))
            self.add(stdscr, row_y, x, "●Think", w, self.attr("fg", bold=True))
            self.add(stdscr, row_y, x + name_w, "generating reply", w, self.attr("dim", bold=True))
            self.add(stdscr, row_y, bar_x, "█" * fill, w, self.attr("fg", dim=True))
            if fill < bar_w:
                self.add(stdscr, row_y, bar_x + fill, "░" * (bar_w - fill), w, self.attr("dim", dim=True))
            self.add(stdscr, row_y, x + w - duration_w, "%9s" % (human_duration_ms(thinking_duration) + "…"), w, self.attr("fg", bold=True))

    def tool_label(self, name: str) -> str:
        """Return a short human-readable label for a tool call name.

        Рус: Вернуть краткую человеко-читаемую метку для имени вызова инструмента.
        """
        labels = {
            "exec_command": "Bash",
            "shell": "Bash",
            "bash": "Bash",
            "read": "Read",
            "Read": "Read",
            "write": "Write",
            "Write": "Write",
            "edit": "Edit",
            "Edit": "Edit",
            "apply_patch": "Patch",
            "update_plan": "Plan",
            "write_stdin": "Input",
            "view_image": "Image",
            "queued": "Queued",
        }
        return labels.get(name, name[:6] or "Tool")

    def tool_color(self, name: str) -> str:
        """Return a color name for displaying a tool call in the timeline.

        Рус: Вернуть имя цвета для отображения вызова инструмента на временной шкале.
        """
        label = self.tool_label(name)
        if label in ("Bash", "Input"):
            return "orange"
        if label in ("Read", "Plan"):
            return "yellow"
        if label in ("Edit", "Patch", "Write"):
            return "cyan"
        if label == "Image":
            return "magenta"
        if label == "Queued":
            return "dim"
        return "blue"

    def current_session(self) -> Optional[AgentSession]:
        """Return the currently selected session by index.

        Рус: Вернуть выбранную сессию по индексу.
        """
        if not self.sessions:
            return None
        return self.sessions[min(self.selected, len(self.sessions) - 1)]

    def draw_meter(self, stdscr: Any, y: int, x: int, width: int, pct: float, color: str) -> None:
        """Draw a horizontal percentage meter using block characters.

        Рус: Отрисовать горизонтальную полосу процента с использованием символов блоков.
        """
        width = max(0, width)
        filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * width))
        for idx in range(width):
            attr = self.attr(color, bold=True) if idx < filled else self.attr("dim", dim=True)
            self.add(stdscr, y, x + idx, "■", 1, attr)

    def box(self, stdscr: Any, y: int, x: int, h: int, w: int, title: str, number: str, color: str) -> None:
        """Draw a bordered box with a title and number badge.

        Рус: Отрисовать рамку с заголовком и номером.
        """
        if h <= 1 or w <= 2:
            return
        attr = self.attr(color, dim=True)
        self.add(stdscr, y, x, "┌" + "─" * (w - 2) + "┐", w, attr)
        for row in range(1, h - 1):
            self.add(stdscr, y + row, x, "│" + " " * (w - 2) + "│", w, attr)
        self.add(stdscr, y + h - 1, x, "└" + "─" * (w - 2) + "┘", w, attr)
        label = "%s%s" % (number, title)
        self.add(stdscr, y, x + 2, label, w, self.attr("fg", bold=True))

    def hline(self, stdscr: Any, y: int, x: int, w: int, color: str) -> None:
        """Draw a horizontal dividing line.

        Рус: Отрисовать горизонтальную разделительную линию.
        """
        self.add(stdscr, y, x, "─" * max(0, w), w, self.attr(color, dim=True))

    def add(self, stdscr: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        """Safely add text to the curses screen with bounds checking.

        Рус: Безопасно добавить текст на экран curses с проверкой границ.
        """
        try:
            if y < 0 or x < 0 or width <= 0:
                return
            screen_h, screen_w = stdscr.getmaxyx()
            if y >= screen_h or x >= screen_w:
                return
            limit = min(width, screen_w - x)
            if limit <= 0:
                return
            stdscr.addstr(y, x, str(text)[:limit], attr)
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argparse CLI parser.

    Рус: Создать и вернуть парсер CLI argparse.
    """
    parser = argparse.ArgumentParser(description="Single-file Python monitor for Claude Code and Codex CLI sessions.")
    parser.add_argument("--once", action="store_true", help="print one text snapshot and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="refresh interval in seconds")
    parser.add_argument("--hide", action="append", default=[], help="hide an agent by name: claude or codex")
    parser.add_argument("--claude-config-dir", action="append", default=[], help="additional Claude config root")
    parser.add_argument("--setup", action="store_true", help="install Claude StatusLine rate-limit helper")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point: parse args, collect data, display via curses or stdout.

    Рус: Главная точка входа: парсить аргументы, собирать данные, отображать через curses или stdout.
    """
    args = build_parser().parse_args(argv)
    if args.version:
        print("%s %s" % (APP_NAME, __VERSION__))
        return 0
    if args.setup:
        setup_claude_statusline()
        return 0

    hidden, config_claude_dirs = load_abtop_config()
    hidden.update(x.lower() for x in args.hide)
    claude_dirs = config_claude_dirs + [Path(os.path.expanduser(x)) for x in args.claude_config_dir]
    collector = MultiCollector(hidden, claude_dirs)
    sessions = collector.collect()

    if args.json:
        print(json.dumps(snapshot(sessions, collector, int(args.interval * 1000)), indent=2, sort_keys=True))
        return 0
    if args.once:
        print_once(sessions, collector)
        return 0

    if not sys.stdout.isatty():
        print_once(sessions, collector)
        return 0

    def handle_sigint(signum: int, frame: Any) -> None:
        """Signal handler: raise KeyboardInterrupt on SIGINT.

        Рус: Обработчик сигналов: поднять KeyboardInterrupt при SIGINT.
        """
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        CursesUI(collector, max(0.5, args.interval)).run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
