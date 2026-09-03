"""Portable decoding and SHA-256 digests for managed Python source files."""

import hashlib
import io
import tokenize
from collections.abc import Sequence

from codecortex.domain.facts import DigestProfile, SourceFileDigest, SourceFileInput


class SourceDigestError(ValueError):
    """A managed source file cannot be read or decoded as Python text."""


def digest_source_file(source: SourceFileInput) -> SourceFileDigest:
    """Decode a Python file, normalize line endings only, and hash UTF-8 text."""
    try:
        payload = source.absolute_path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(payload).readline)
        decoded = payload.decode(encoding)
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise SourceDigestError(
            f"Cannot decode managed Python source: {source.relative_path}"
        ) from error
    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return SourceFileDigest(
        source=source,
        normalized_text=normalized,
        content_digest=f"sha256:{digest}",
        size_bytes=len(payload),
        encoding=encoding,
    )


def repository_digest(
    files: Sequence[SourceFileDigest], profile: DigestProfile
) -> str:
    """Hash a profile-tagged, UTF-8-byte-sorted list of file content digests."""
    ordered = sorted(files, key=lambda item: item.source.relative_path.encode("utf-8"))
    paths = [item.source.relative_path for item in ordered]
    if len(set(paths)) != len(paths):
        raise ValueError("Repository digest input contains duplicate source paths")
    payload = bytearray(str(profile.version).encode("ascii"))
    payload.extend(b"\0")
    for item in ordered:
        payload.extend(item.source.relative_path.encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(item.content_digest.encode("ascii"))
        payload.extend(b"\n")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
