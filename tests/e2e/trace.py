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
_NON_SECRET_TOKEN_COUNT_KEYS = frozenset(
    {"input_tokens", "input_token_count", "output_tokens", "output_token_count"}
)
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_./\\-])(?:"
    r"/(?!/)(?:[^\s\"'`()\[\]{}<>;,/]+/)*[^\s\"'`()\[\]{}<>;,/]+"
    r"|\\\\(?:[^\s\"'`()\[\]{}<>;,\\]+\\)+[^\s\"'`()\[\]{}<>;,\\]+"
    r"|[A-Za-z]:\\(?:[^\s\"'`()\[\]{}<>;,\\]+\\)*[^\s\"'`()\[\]{}<>;,\\]+"
    r")",
)
_BEARER = re.compile(r"\bBearer\s+[^\s\"']+", re.IGNORECASE)
_BASIC = re.compile(r"\bBasic\s+[A-Za-z0-9+/=_-]+", re.IGNORECASE)
_OPENAI_TOKEN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
_PROVIDER_TOKEN = re.compile(
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}\b", re.IGNORECASE
)
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_INLINE_SECRET = re.compile(
    r"\b(?:api[_-]?key|token|secret|password|"
    r"[A-Za-z][A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password)|"
    r"OPENAI_API_KEY)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_SOURCE_REFERENCE = re.compile(
    r"(?P<path>(?:src|tests)/[A-Za-z0-9_.\-/]+\.(?:py|toml|md)):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?"
)
_COGNITIVE_ID = re.compile(r"(?:responsibility|behavior|capability)\.[a-z0-9-]+\Z")


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
    anchors = _cognitive_anchor_ids(values, tool_names)
    route = _route_from_mcp_trace(values, tool_names, anchors)
    input_tokens, output_tokens = _token_counts(values)
    analyzer_count = sum("codecortex-analyzer" in name for name in tool_names)
    materialization_count = len(
        re.findall(r"\b(?:materialize|materialization)\b", text, re.IGNORECASE)
    )
    return ParsedTrace(
        answer=_final_answer(values),
        trace=Trace(
            route=route,
            cognitive_anchor_ids=anchors,
            source_references=references,
            mcp_tool_names=tool_names,
            analyzer_count=analyzer_count,
            materialization_prompt_count=materialization_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
        ),
    )


def _cognitive_anchor_ids(
    values: Iterable[object], tool_names: tuple[str, ...]
) -> tuple[str, ...]:
    if not any("search_cognitive_graph" in name for name in tool_names):
        return ()
    anchors: set[str] = set()
    for record in _matching_tool_records(values, "search_cognitive_graph"):
        for key, item in _tool_output_pairs(record):
            if key.casefold() == "node_id" and isinstance(item, str):
                if _COGNITIVE_ID.fullmatch(item):
                    anchors.add(item)
            elif key.casefold() == "node_ids" and isinstance(item, list):
                anchors.update(
                    candidate
                    for candidate in item
                    if isinstance(candidate, str) and _COGNITIVE_ID.fullmatch(candidate)
                )
    return tuple(sorted(anchors))


def _route_from_mcp_trace(
    values: Iterable[object],
    tool_names: tuple[str, ...],
    anchors: tuple[str, ...],
) -> str | None:
    codecortex_tools = tuple(
        name for name in tool_names if "codecortex" in name.casefold()
    )
    if not codecortex_tools:
        return None
    if not anchors:
        return "native_fallback"
    effective_records = tuple(
        _matching_tool_records(values, "effective_query_freshness")
    )
    pairs = tuple(
        pair for record in effective_records for pair in _tool_output_pairs(record)
    )
    statuses = {
        item.casefold()
        for key, item in pairs
        if key.casefold() == "status" and isinstance(item, str)
    }
    materialization_statuses = {
        item.casefold()
        for record in _matching_tool_records(values, "get_discussion_context")
        for key, item in _tool_output_pairs(record)
        if key.casefold() == "materialization_status" and isinstance(item, str)
    }
    if (
        any("get_discussion_context" in name for name in codecortex_tools)
        and "unmaterialized" in materialization_statuses
    ):
        return "offer_materialization"
    if not effective_records:
        return None
    if statuses & {"affected_source_first", "unknown_source_first"}:
        return "source_first"
    if "unaffected_current" in statuses:
        return "graph_unaffected"
    if "current" in statuses:
        return "graph_current"
    return None


def _redact_value(value: object, key: str | None = None) -> object:
    if key is not None and _sensitive_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            "<redacted-key>" if _sensitive_key(str(item_key)) else str(item_key): _redact_value(
                item, str(item_key)
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _sensitive_key(key: str) -> bool:
    return (
        key.casefold() not in _NON_SECRET_TOKEN_COUNT_KEYS
        and _SENSITIVE_KEY.search(key) is not None
    )


def _redact_text(value: str) -> str:
    value = _BEARER.sub("Bearer <redacted>", value)
    value = _BASIC.sub("Basic <redacted>", value)
    value = _OPENAI_TOKEN.sub("<redacted-token>", value)
    value = _PROVIDER_TOKEN.sub("<redacted-token>", value)
    value = _JWT.sub("<redacted-token>", value)
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


def _matching_tool_records(
    values: Iterable[object], tool_fragment: str
) -> Iterable[Mapping[object, object]]:
    for value in values:
        yield from _matching_tool_records_in(value, tool_fragment)


def _matching_tool_records_in(
    value: object, tool_fragment: str
) -> Iterable[Mapping[object, object]]:
    if isinstance(value, Mapping):
        direct_names = tuple(_direct_tool_names(value))
        if any(tool_fragment in name for name in direct_names):
            yield value
            return
        for nested in value.values():
            yield from _matching_tool_records_in(nested, tool_fragment)
    elif isinstance(value, list):
        for nested in value:
            yield from _matching_tool_records_in(nested, tool_fragment)


def _direct_tool_names(value: Mapping[object, object]) -> Iterable[str]:
    possible = value.get("tool_name") or value.get("name")
    if isinstance(possible, str) and "codecortex" in possible.casefold():
        yield possible
    server = value.get("server") or value.get("server_name")
    tool = value.get("tool") or value.get("tool_name")
    if isinstance(server, str) and "codecortex" in server.casefold() and isinstance(tool, str):
        yield f"{server}.{tool}"


def _tool_output_pairs(record: Mapping[object, object]) -> Iterable[tuple[str, object]]:
    for key, value in record.items():
        if str(key).casefold() not in {"output", "result", "response"}:
            continue
        yield from _walk_pairs(value)
        for decoded in _embedded_json_values(value):
            yield from _walk_pairs(decoded)


def _embedded_json_values(value: object) -> Iterable[object]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        if isinstance(decoded, (Mapping, list)):
            yield decoded
            yield from _embedded_json_values(decoded)
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _embedded_json_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _embedded_json_values(nested)


def _tool_names_in(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        yield from _direct_tool_names(value)
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
