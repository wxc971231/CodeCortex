import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codecortex.application.services import ApplicationServices
from codecortex.domain.errors import CodeCortexError, ErrorCode
from codecortex.infrastructure.formal import FormalStore
from codecortex.infrastructure.locking import RepositoryLock
from codecortex.infrastructure.repository import Repository


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


def test_initialize_is_byte_idempotent(
    app: ApplicationServices, repo_root: Path
) -> None:
    """Rewriting initialized state could change reviewed bytes or audit timestamps."""
    app.initialize_repository()
    before = digest_tree(repo_root / ".codecortex")

    app.initialize_repository()

    assert digest_tree(repo_root / ".codecortex") == before


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
    manifest_path.write_text(json.dumps(manifest) + "\n")
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
    manifest_path.write_text(json.dumps(manifest) + "\n")
    graph_path.write_text(json.dumps(graph) + "\n")
    refs_path.write_text(json.dumps(refs) + "\n")

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
