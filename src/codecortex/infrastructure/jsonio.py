"""Deterministic UTF-8 JSON serialization for formal state."""

import json
import os
import tempfile
from pathlib import Path


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON deterministically with a single LF terminator."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return f"{text}\n".encode()


def write_json_atomic(path: Path, value: object) -> None:
    """Durably replace ``path`` with canonical JSON from a sibling temporary file."""
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(canonical_json_bytes(value))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
