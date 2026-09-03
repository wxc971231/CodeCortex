"""Portable source-text and repository digest behavior."""

from pathlib import Path

import pytest

from codecortex.domain.facts import DigestProfile, SourceFileInput
from codecortex.infrastructure.python.digest import (
    SourceDigestError,
    digest_source_file,
    repository_digest,
)


def source(tmp_path: Path, relative: str, payload: bytes) -> SourceFileInput:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return SourceFileInput(relative, path)


def test_crlf_and_lf_have_the_same_digest(tmp_path: Path) -> None:
    crlf = digest_source_file(source(tmp_path / "crlf", "a.py", b"x = 1\r\n"))
    lf = digest_source_file(source(tmp_path / "lf", "a.py", b"x = 1\n"))

    assert crlf.content_digest == lf.content_digest
    assert crlf.normalized_text == "x = 1\n"


def test_digest_uses_detected_python_encoding_without_unicode_normalization(
    tmp_path: Path,
) -> None:
    payload = "# coding: latin-1\nname = 'café'\n".encode("latin-1")

    digest = digest_source_file(source(tmp_path, "pkg/module.py", payload))

    assert digest.normalized_text == "# coding: latin-1\nname = 'café'\n"
    assert digest.content_digest.startswith("sha256:")


def test_digest_preserves_trailing_whitespace_but_normalizes_lone_cr(tmp_path: Path) -> None:
    normalized = digest_source_file(source(tmp_path / "one", "a.py", b"x = 1\r"))
    different = digest_source_file(source(tmp_path / "two", "a.py", b"x = 1 \n"))

    assert normalized.normalized_text == "x = 1\n"
    assert normalized.content_digest != different.content_digest


def test_digest_rejects_declared_encoding_that_cannot_decode_payload(tmp_path: Path) -> None:
    invalid = source(tmp_path, "broken.py", b"# coding: ascii\nname = '\xff'\n")

    with pytest.raises(SourceDigestError, match="broken.py"):
        digest_source_file(invalid)


def test_repository_digest_is_order_independent_and_profile_versioned(
    tmp_path: Path,
) -> None:
    first = digest_source_file(source(tmp_path, "a.py", b"a = 1\n"))
    second = digest_source_file(source(tmp_path, "pkg/b.py", b"b = 2\n"))

    profile_one = DigestProfile(version=1)
    assert repository_digest((second, first), profile_one) == repository_digest(
        (first, second), profile_one
    )
    assert repository_digest((first, second), profile_one) != repository_digest(
        (first, second), DigestProfile(version=2)
    )


def test_repository_digest_rejects_duplicate_relative_paths(tmp_path: Path) -> None:
    first = digest_source_file(source(tmp_path / "one", "a.py", b"a = 1\n"))
    second = digest_source_file(source(tmp_path / "two", "a.py", b"a = 2\n"))

    with pytest.raises(ValueError, match="duplicate"):
        repository_digest((first, second), DigestProfile())
