"""Safe, idempotent installation of packaged CodeCortex resources for Codex."""

import fcntl
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.items import Table

RESOURCE_PACKAGE = "codecortex.integrations.codex.resources"
SKILL_RELATIVE = Path(".agents/skills/codecortex/SKILL.md")
AGENT_RELATIVE = Path(".codex/agents/codecortex-analyzer.toml")
CONFIG_RELATIVE = Path(".codex/config.toml")
INSTALL_LOCK_RELATIVE = Path(".codex/.codecortex-install.lock")
ApprovalMode = Literal["prompt", "approve"]


@dataclass(frozen=True)
class InstallResult:
    """Machine-readable result of one planned or completed installation."""

    changed: bool
    dry_run: bool
    changed_paths: tuple[str, ...]
    backups: tuple[str, ...] = ()


def install_codex(
    home: Path,
    executable: Path,
    dry_run: bool,
    force: bool,
    approval_mode: ApprovalMode = "prompt",
) -> InstallResult:
    """Install CodeCortex resources without touching unrelated Codex settings.

    ``home`` is explicit so callers and tests choose the target safely. The
    function never reads environment variables or implicitly writes a real
    user directory. Resource files with unexpected content require ``force``;
    the managed ``mcp_servers.codecortex`` TOML entry is reconciled in place
    while retaining all unrelated document content.
    """
    root = _validated_home(home)
    resolved_executable = _validated_executable(executable)
    _validate_approval_mode(approval_mode)
    if dry_run:
        _validate_target_paths(root, {path: b"" for path in _managed_paths(root)})
        targets = _desired_targets(root, resolved_executable, approval_mode)
        changes = _changed_targets(targets)
        _require_force_for_modified_resources(changes, targets, force)
        changed_paths = _relative_paths(root, changes)
        return InstallResult(bool(changes), True, changed_paths)
    with _exclusive_install_lock(root):
        _validate_target_paths(root, {path: b"" for path in _managed_paths(root)})
        targets = _desired_targets(root, resolved_executable, approval_mode)
        changes = _changed_targets(targets)
        _require_force_for_modified_resources(changes, targets, force)
        changed_paths = _relative_paths(root, changes)
        if not changes:
            return InstallResult(False, False, ())

        backups: list[str] = []
        previous: dict[Path, bytes | None] = {
            path: path.read_bytes() if path.exists() else None for path in changes
        }
        written: list[Path] = []
        try:
            for path, old_bytes in previous.items():
                _ensure_safe_parent(root, path)
                if old_bytes is not None:
                    backup = _write_backup(path, old_bytes)
                    backups.append(backup.relative_to(root).as_posix())
                _write_bytes_atomic(path, changes[path])
                written.append(path)
            _validate_outputs(root, targets, resolved_executable, approval_mode)
        except Exception:
            _restore_previous(root, previous, written)
            raise
    return InstallResult(True, False, changed_paths, tuple(backups))


def _desired_targets(
    root: Path, executable: Path, approval_mode: ApprovalMode
) -> dict[Path, bytes]:
    skill = _resource_bytes("SKILL.md")
    agent = _resource_bytes("codecortex-analyzer.toml")
    config = _merged_config_bytes(root / CONFIG_RELATIVE, executable, approval_mode)
    return {
        root / SKILL_RELATIVE: skill,
        root / AGENT_RELATIVE: agent,
        root / CONFIG_RELATIVE: config,
    }


def _managed_paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / SKILL_RELATIVE,
        root / AGENT_RELATIVE,
        root / CONFIG_RELATIVE,
    )


def _changed_targets(targets: Mapping[Path, bytes]) -> dict[Path, bytes]:
    return {
        path: payload
        for path, payload in targets.items()
        if not path.exists() or path.read_bytes() != payload
    }


def _relative_paths(root: Path, targets: Mapping[Path, bytes]) -> tuple[str, ...]:
    return tuple(path.relative_to(root).as_posix() for path in targets)


def _resource_bytes(name: str) -> bytes:
    return resources.files(RESOURCE_PACKAGE).joinpath(name).read_bytes()


def _merged_config_bytes(
    path: Path, executable: Path, approval_mode: ApprovalMode
) -> bytes:
    if path.exists():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError) as error:
            raise ValueError("Existing Codex config.toml is not valid UTF-8 TOML") from error
    else:
        document = tomlkit.document()

    servers = _table(document, "mcp_servers")
    server = tomlkit.table()
    server["command"] = str(executable)
    server["args"] = ["mcp", "--profile", "main"]
    server["required"] = False
    server["startup_timeout_sec"] = 10
    server["tool_timeout_sec"] = 120
    tools = tomlkit.table()
    apply = tomlkit.table()
    apply["approval_mode"] = approval_mode
    tools["apply_cognitive_proposal"] = apply
    server["tools"] = tools
    servers["codecortex"] = server
    return tomlkit.dumps(document).encode("utf-8")


def _table(container: Mapping[str, Any], key: str) -> Table:
    existing = container.get(key)
    if existing is None:
        table = tomlkit.table()
        container[key] = table  # type: ignore[index]
        return table
    if not isinstance(existing, Table):
        raise TypeError(f"Codex config key {key!r} must be a TOML table")
    return existing


def _validated_home(home: Path) -> Path:
    root = home.expanduser()
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise ValueError("Installation home must be an existing non-symlink directory")
    return root.resolve()


def _validated_executable(executable: Path) -> Path:
    candidate = executable.expanduser()
    if not candidate.exists() or not candidate.is_file():
        raise ValueError("CodeCortex executable must be an existing regular file")
    return candidate.resolve()


def _validate_approval_mode(value: str) -> None:
    if value not in {"prompt", "approve"}:
        raise ValueError("Approval mode must be 'prompt' or 'approve'")


def _validate_target_paths(root: Path, targets: Mapping[Path, bytes]) -> None:
    for path in targets:
        _ensure_safe_parent(root, path, create=False)
        if path.is_symlink():
            raise ValueError(f"Refusing symlinked managed target: {path.relative_to(root)}")
        if path.exists() and not path.is_file():
            raise ValueError(f"Managed target must be a regular file: {path.relative_to(root)}")


def _require_force_for_modified_resources(
    changes: Mapping[Path, bytes], targets: Mapping[Path, bytes], force: bool
) -> None:
    if force:
        return
    for relative in (SKILL_RELATIVE, AGENT_RELATIVE):
        target = next(path for path in targets if path.name == relative.name)
        if target in changes and target.exists():
            raise ValueError(
                f"Managed resource was modified: {target.name}; rerun with --force to replace it"
            )


def _ensure_safe_parent(root: Path, target: Path, *, create: bool = True) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError("Managed target escapes installation home") from error
    cursor = root
    for part in relative.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"Refusing symlinked directory: {cursor.relative_to(root)}")
        if cursor.exists():
            if not cursor.is_dir():
                raise ValueError(f"Managed parent is not a directory: {cursor.relative_to(root)}")
        elif create:
            cursor.mkdir()


@contextmanager
def _exclusive_install_lock(root: Path) -> Iterator[None]:
    lock_path = root / INSTALL_LOCK_RELATIVE
    _ensure_safe_parent(root, lock_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_backup(path: Path, payload: bytes) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.codecortex-backup-{stamp}-{secrets.token_hex(4)}")
    _write_bytes_atomic(backup, payload)
    return backup


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.codecortex-tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_previous(
    root: Path, previous: Mapping[Path, bytes | None], written: list[Path]
) -> None:
    for path in reversed(written):
        prior = previous[path]
        if prior is None:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        else:
            _ensure_safe_parent(root, path)
            _write_bytes_atomic(path, prior)


def _validate_outputs(
    root: Path,
    targets: Mapping[Path, bytes],
    executable: Path,
    approval_mode: ApprovalMode,
) -> None:
    for path, expected in targets.items():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
            raise ValueError(f"Installed target failed validation: {path.relative_to(root)}")
    config = tomlkit.parse((root / CONFIG_RELATIVE).read_text(encoding="utf-8"))
    server = config["mcp_servers"]["codecortex"]
    if server["command"] != str(executable) or server["args"] != [
        "mcp",
        "--profile",
        "main",
    ]:
        raise ValueError("Installed CodeCortex MCP configuration failed validation")
    tools = server.get("tools")
    apply = tools.get("apply_cognitive_proposal") if isinstance(tools, Table) else None
    if not isinstance(apply, Table) or apply.get("approval_mode") != approval_mode:
        raise ValueError("Installed CodeCortex proposal approval mode failed validation")
    tomlkit.parse((root / AGENT_RELATIVE).read_text(encoding="utf-8"))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
