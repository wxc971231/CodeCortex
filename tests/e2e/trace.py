"""Parse and sanitize observable traces from isolated Child Codex processes.

Raw Child Codex JSONL can contain local paths and credentials.  This module
never writes the raw form: callers sanitize before storing an artifact and use
the sanitized form for all score inputs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from tests.benchmark.scoring import SourceReference, Trace

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|token|secret|password|cookie|credential|auth)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(
    r"(?:^|(?<=[\s\"'=]))(?:/(?:home|tmp|var|private|Users)/[^\s\"']+|[A-Za-z]:\\[^\s\"']+)",
)
_BEARER = re.compile(r"\bBearer\s+[^\s\"']+", re.IGNORECASE)
_INLINE_SECRET = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password)|"
    r"OPENAI_API_KEY)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_SOURCE_REFERENCE = re.compile(
    r"(?P<path>(?:src|tests)/[A-Za-z0-9_.\-/]+\.(?:py|toml|md)):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?"
)
_ROUTE = re.compile(
    r"CODECORTEX_BENCHMARK_ROUTE\s*:\s*"
    r"(?P<route>graph_current|graph_unaffected|source_first|native_fallback|offer_materialization)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTrace:
    """Scorable information extracted only from a sanitized JSONL trace."""

    answer: str
    trace: Trace


def sanitize_trace(raw: str) -> str:
    """Return newline-delimited JSON with credentials and machine paths removed."""
    if not isinstance(raw, str):
        raise TypeError("Child Codex trace must be text")
    lines: list[str] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            lines.append(_redact_text(line))
        else:
            lines.append(json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def parse_sanitized_trace(raw: str, *, elapsed_ms: int | None = None) -> ParsedTrace:
    """Extract a conservative score trace without depending on one CLI event schema."""
    sanitized = sanitize_trace(raw)
    values: list[object] = []
    for line in sanitized.splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            values.append(line)
    text = "\n".join(_all_text(value) for value in values)
    references = _source_references(text)
    tool_names = tuple(sorted(set(_tool_names(values))))
    route_match = _ROUTE.search(text)
    route = route_match.group("route").lower() if route_match else None
    input_tokens, output_tokens = _token_counts(values)
    analyzer_count = sum("codecortex-analyzer" in name for name in tool_names)
    materialization_count = len(
        re.findall(r"\b(?:materialize|materialization)\b", text, re.IGNORECASE)
    )
    return ParsedTrace(
        answer=_final_answer(values),
        trace=Trace(
            route=route,
            source_references=references,
            mcp_tool_names=tool_names,
            analyzer_count=analyzer_count,
            materialization_prompt_count=materialization_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
        ),
    )


def _redact_value(value: object, key: str | None = None) -> object:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            "<redacted-key>" if _SENSITIVE_KEY.search(str(item_key)) else str(item_key): _redact_value(
                item, str(item_key)
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    value = _BEARER.sub("Bearer <redacted>", value)
    value = _INLINE_SECRET.sub("<redacted-secret>", value)
    return _ABSOLUTE_PATH.sub("<machine-path>", value)


def _all_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_all_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_all_text(item) for item in value)
    return ""


def _source_references(text: str) -> tuple[SourceReference, ...]:
    found: set[SourceReference] = set()
    for match in _SOURCE_REFERENCE.finditer(text):
        path = PurePosixPath(match.group("path")).as_posix()
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        found.add(SourceReference(path, start, end))
    return tuple(sorted(found, key=lambda item: (item.relative_path, item.start_line, item.end_line)))


def _tool_names(values: Iterable[object]) -> Iterable[str]:
    for value in values:
        yield from _tool_names_in(value)


def _tool_names_in(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        possible = value.get("tool_name") or value.get("name")
        if isinstance(possible, str) and "codecortex" in possible.casefold():
            yield possible
        server = value.get("server") or value.get("server_name")
        tool = value.get("tool") or value.get("tool_name")
        if isinstance(server, str) and "codecortex" in server.casefold() and isinstance(tool, str):
            yield f"{server}.{tool}"
        for nested in value.values():
            yield from _tool_names_in(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _tool_names_in(nested)


def _token_counts(values: Iterable[object]) -> tuple[int | None, int | None]:
    input_values: list[int] = []
    output_values: list[int] = []
    for value in values:
        for key, item in _walk_pairs(value):
            if not isinstance(item, int) or item < 0:
                continue
            lowered = key.casefold()
            if lowered in {"input_tokens", "input_token_count"}:
                input_values.append(item)
            elif lowered in {"output_tokens", "output_token_count"}:
                output_values.append(item)
    return (input_values[-1] if input_values else None, output_values[-1] if output_values else None)


def _walk_pairs(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_pairs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_pairs(item)


def _final_answer(values: Iterable[object]) -> str:
    candidates: list[str] = []
    for value in values:
        for key, item in _walk_pairs(value):
            if key.casefold() in {"text", "content", "message", "output"} and isinstance(item, str):
                candidates.append(item)
    return candidates[-1] if candidates else ""
