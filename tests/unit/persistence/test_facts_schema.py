"""SQLite fact-cache schema and connection-policy invariants."""

import sqlite3

import pytest

from codecortex.infrastructure.persistence.facts_db import FactsDatabase


def test_schema_has_foreign_keys_and_required_indexes(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    assert database.foreign_keys_enabled()
    assert database.has_index("relations", ("target_uid", "relation_type"))
    assert database.has_index(
        "baseline_entity_snapshots", ("module_name", "fingerprint")
    )


def test_open_read_uses_read_only_connection(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    with database.open_read() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")


def test_open_write_uses_wal_foreign_keys_and_busy_timeout(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    with database.open_write() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000


def test_deleting_a_file_cascades_its_entities_and_relations(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    with database.open_write() as connection:
        connection.execute(
            "INSERT INTO source_files "
            "(relative_path, content_digest, size_bytes, parse_status, is_test) "
            "VALUES (?, ?, ?, ?, ?)",
            ("pkg/a.py", "sha256:" + "a" * 64, 1, "parsed", 0),
        )
        file_id = connection.execute(
            "SELECT file_id FROM source_files WHERE relative_path = ?", ("pkg/a.py",)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO entities "
            "(uid, file_id, address, module_name, qualname, kind, name, "
            "start_line, end_line, fingerprint, resolution_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ent_parent", file_id, "pkg.a:", "pkg.a", "", "module", "pkg.a", 1, 1,
             "sha256:" + "b" * 64, "resolved"),
        )
        connection.execute(
            "INSERT INTO entities "
            "(uid, file_id, address, module_name, qualname, kind, name, parent_uid, "
            "start_line, end_line, fingerprint, resolution_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ent_child", file_id, "pkg.a:child", "pkg.a", "child", "function", "child",
             "ent_parent", 2, 2, "sha256:" + "c" * 64, "resolved"),
        )
        connection.execute(
            "INSERT INTO relations "
            "(relation_type, source_uid, source_file_id, resolution_status, confidence, "
            "resolver_version, relation_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("contains", "ent_parent", file_id, "resolved", "syntactic", "syntax-v1", "rel_a"),
        )
        connection.execute("DELETE FROM source_files WHERE file_id = ?", (file_id,))
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_entity_reference_fallback_requires_unique_kind_signature_match(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    with database.open_write() as connection:
        connection.execute(
            "INSERT INTO source_files "
            "(relative_path, content_digest, size_bytes, parse_status, is_test) "
            "VALUES ('pkg/new.py', ?, 1, 'parsed', 0)",
            ("sha256:" + "a" * 64,),
        )
        file_id = connection.execute("SELECT file_id FROM source_files").fetchone()[0]
        for uid, address in (("ent_new", "pkg.new:work"), ("ent_other", "pkg.other:work")):
            connection.execute(
                "INSERT INTO entities "
                "(uid, file_id, address, module_name, qualname, kind, name, "
                "start_line, end_line, signature, fingerprint, resolution_status) "
                "VALUES (?, ?, ?, 'pkg.new', 'work', 'function', 'work', 1, 2, "
                "'(value: int) -> int', ?, 'resolved')",
                (uid, file_id, address, "sha256:" + "b" * 64),
            )

    ambiguous = database.resolve_entity_reference(
        last_known_address="pkg.old:work",
        kind="function",
        signature="(value: int) -> int",
        fingerprint="sha256:" + "b" * 64,
    )
    assert ambiguous.status == "ambiguous"
    assert ambiguous.entity is None

    with database.open_write() as connection:
        connection.execute("DELETE FROM entities WHERE uid = 'ent_other'")
    resolved = database.resolve_entity_reference(
        last_known_address="pkg.old:work",
        kind="function",
        signature="(value: int) -> int",
        fingerprint="sha256:" + "b" * 64,
    )
    assert resolved.status == "resolved"
    assert resolved.entity is not None
    assert resolved.entity.relative_path == "pkg/new.py"

    missing = database.resolve_entity_reference(
        last_known_address="pkg.old:work",
        kind="function",
        signature="(other: str) -> int",
        fingerprint="sha256:" + "b" * 64,
    )
    assert missing.status == "missing"
