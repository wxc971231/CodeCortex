import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.jsonio import canonical_json_bytes, write_json_atomic
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.repository import Repository
from codecortex.infrastructure.views import render_views


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def app(repo_root: Path) -> ApplicationServices:
    repository = Repository(repo_root)
    return ApplicationServices(
        repository=repository,
        formal_store=FormalStore(repository),
        repository_lock=RepositoryLock(repo_root),
        view_renderer=render_views,
    )


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(("d:" if path.is_dir() else "f:").encode())
        digest.update(relative.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_initialize_creates_exact_valid_empty_formal_state(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Omitting any formal artifact would leave a revision-zero repository unreadable."""
    overview = app.initialize_repository()

    assert overview.graph_revision == 0
    assert overview.cognition_initialized is False
    assert overview.cognition_baseline_source_digest is None
    assert app.validate_graph().valid is True
    assert json.loads(
        (repo_root / ".codecortex/source_baseline.json").read_text()
    )["files"] == []
    assert json.loads(
        (repo_root / ".codecortex/entity_refs.json").read_text()
    ) == {"entities": [], "graph_revision": 0, "schema_version": 1}
    assert {
        path.relative_to(repo_root / ".codecortex").as_posix()
        for path in (repo_root / ".codecortex").rglob("*")
    } == {
        ".cache",
        ".cache/repository.lock",
        "PROJECT.md",
        "config.toml",
        "entity_refs.json",
        "graph.json",
        "history",
        "history/events",
        "manifest.json",
        "source_baseline.json",
        "views",
        "views/TREE.md",
        "views/behaviors",
        "views/capabilities",
        "views/responsibilities",
    }
    assert (repo_root / ".codecortex/views/TREE.md").read_bytes() == (
        b"# CodeCortex Cognitive Tree\n\nNo cognition has been initialized.\n"
    )


def test_formal_load_treats_absent_empty_structural_directories_as_empty(
    app: ApplicationServices, repo_root: Path
) -> None:
    app.initialize_repository()
    codecortex_root = repo_root / ".codecortex"
    absent = (
        "history/events",
        "history",
        "views/responsibilities",
        "views/behaviors",
        "views/capabilities",
    )
    for relative in absent:
        (codecortex_root / relative).rmdir()

    state = app.formal_store.load()

    assert state.history_events == ()
    assert all(not (codecortex_root / relative).exists() for relative in absent)


def test_initialize_is_byte_idempotent(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Rewriting initialized state could change reviewed bytes or audit timestamps."""
    app.initialize_repository()
    before = digest_tree(repo_root / ".codecortex")

    app.initialize_repository()

    assert digest_tree(repo_root / ".codecortex") == before


def test_load_rejects_source_baseline_with_forged_aggregate_digest(
    app: ApplicationServices, repo_root: Path
) -> None:
    app.initialize_repository()
    baseline_path = repo_root / ".codecortex" / "source_baseline.json"
    manifest_path = repo_root / ".codecortex" / "manifest.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged = "sha256:" + "a" * 64
    baseline["repository_source_digest"] = forged
    baseline["files"] = [
        {"relative_path": "app.py", "content_digest": "sha256:" + "b" * 64}
    ]
    manifest["cognition_initialized"] = True
    manifest["cognition_baseline"] = forged
    baseline_path.write_bytes(canonical_json_bytes(baseline))
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(CodeCortexError) as raised:
        app.formal_store.load()

    assert raised.value.code is ErrorCode.FORMAL_STATE_CORRUPT


@pytest.mark.parametrize(
    ("relative_path", "payload"),
    [
        ("manifest.json", b"{}\n"),
        ("graph.json", b"{not-json\n"),
    ],
)
def test_initialize_refuses_partial_or_malformed_state_without_overwriting(
    app: ApplicationServices,
    repo_root: Path,
    relative_path: str,
    payload: bytes,
) -> None:
    """A recovery-by-overwrite branch would silently destroy evidence of corruption."""
    codecortex = repo_root / ".codecortex"
    codecortex.mkdir()
    target = codecortex / relative_path
    target.write_bytes(payload)
    before = digest_tree(codecortex)

    with pytest.raises(CodeCortexError) as exc:
        app.initialize_repository()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    assert target.read_bytes() == payload
    assert digest_tree(codecortex) != before  # only the persistent lock may be added
    assert not (codecortex / "source_baseline.json").exists()


def test_initialize_refuses_unsupported_schema_without_overwriting(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Treating a future schema as empty state could erase data this Core cannot read."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 2
    write_json_atomic(manifest_path, manifest)
    before = digest_tree(repo_root / ".codecortex")

    with pytest.raises(CodeCortexError) as exc:
        app.initialize_repository()

    assert exc.value.code is ErrorCode.UNSUPPORTED_SCHEMA
    assert digest_tree(repo_root / ".codecortex") == before


def test_validate_rejects_dangling_approval_event(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Ignoring a missing applied event would detach formal cognition from approval history."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    graph_path = repo_root / ".codecortex/graph.json"
    refs_path = repo_root / ".codecortex/entity_refs.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["graph_revision"] = 1
    graph = json.loads(graph_path.read_text())
    graph["graph_revision"] = 1
    graph["nodes"] = [
        {
            "id": "capability.storage",
            "kind": "capability",
            "node_revision": 1,
            "approval": {
                "approval_event_id": "evt_01J00000000000000000000000"
            },
        }
    ]
    refs = json.loads(refs_path.read_text())
    refs["graph_revision"] = 1
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(graph_path, graph)
    write_json_atomic(refs_path, refs)

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_initialize_refuses_invalid_config_value_types_without_overwriting(
    app: ApplicationServices, repo_root: Path
) -> None:
    """A key-only TOML check would accept unusable managed-source rules."""
    app.initialize_repository()
    config_path = repo_root / ".codecortex/config.toml"
    invalid = config_path.read_text().replace(
        'include = ["**/*.py"]', 'include = "**/*.py"'
    )
    config_path.write_text(invalid)
    before = digest_tree(repo_root / ".codecortex")

    with pytest.raises(CodeCortexError) as exc:
        app.initialize_repository()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
    assert digest_tree(repo_root / ".codecortex") == before


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda payload: b"\xef\xbb\xbf" + payload,
        lambda payload: payload.decode().encode("utf-16"),
        lambda payload: payload.decode().encode("utf-32"),
    ],
)
def test_validate_rejects_non_utf8_formal_json(
    app: ApplicationServices,
    repo_root: Path,
    payload_factory,
) -> None:
    """Letting JSON auto-detect BOM encodings would violate the UTF-8 formal format."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    manifest_path.write_bytes(payload_factory(manifest_path.read_bytes()))

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


@pytest.mark.parametrize(
    "payload_factory",
    [
        lambda value: json.dumps(value, sort_keys=True).encode(),
        lambda value: canonical_json_bytes(value) + b"\n",
        lambda value: json.dumps(value, indent=4, sort_keys=True).encode() + b"\n",
    ],
)
def test_validate_rejects_noncanonical_formal_json_bytes(
    app: ApplicationServices,
    repo_root: Path,
    payload_factory,
) -> None:
    """Accepting alternate whitespace or terminators would defeat byte-level determinism."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_bytes(payload_factory(manifest))

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_validate_rejects_duplicate_json_object_keys(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Last-key-wins parsing must not conceal ambiguous formal state."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    payload = manifest_path.read_bytes().replace(
        b'  "graph_revision": 0,\n',
        b'  "graph_revision": 0,\n  "graph_revision": 0,\n',
    )
    manifest_path.write_bytes(payload)

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_validate_rejects_unknown_top_level_json_fields(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Ignoring fields under schema v1 would silently accept an undefined format extension."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    write_json_atomic(manifest_path, manifest)

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


@pytest.mark.parametrize(("field", "value"), [("epistemic_status", []), ("created_by", {})])
def test_validate_wraps_malformed_nested_node_values_as_stable_errors(
    app: ApplicationServices,
    repo_root: Path,
    field: str,
    value: object,
) -> None:
    """Malformed nested values must not leak adapter-unstable Python exceptions."""
    app.initialize_repository()
    manifest_path = repo_root / ".codecortex/manifest.json"
    graph_path = repo_root / ".codecortex/graph.json"
    refs_path = repo_root / ".codecortex/entity_refs.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["graph_revision"] = 1
    graph = json.loads(graph_path.read_text())
    graph["graph_revision"] = 1
    graph["nodes"] = [
        {
            "id": "capability.storage",
            "kind": "capability",
            "node_revision": 1,
            field: value,
            "approval": {"approval_event_id": "evt_01J00000000000000000000000"},
        }
    ]
    refs = json.loads(refs_path.read_text())
    refs["graph_revision"] = 1
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(graph_path, graph)
    write_json_atomic(refs_path, refs)

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT


def test_validate_rejects_complete_snapshot_with_malformed_graph_shape(
    app: ApplicationServices, repo_root: Path
) -> None:
    """A complete file tree must still pass structural parsing before it is accepted."""
    app.initialize_repository()
    graph_path = repo_root / ".codecortex/graph.json"
    graph = json.loads(graph_path.read_text())
    graph["nodes"] = ["not-an-object"]
    write_json_atomic(graph_path, graph)

    with pytest.raises(CodeCortexError) as exc:
        app.validate_graph()

    assert exc.value.code is ErrorCode.FORMAL_STATE_CORRUPT
