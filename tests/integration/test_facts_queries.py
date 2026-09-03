"""Bounded SQLite fact-cache query behavior."""

import sqlite3

import pytest

from codecortex.infrastructure.persistence.facts_db import FactScope, FactsDatabase


def test_entity_query_is_bounded(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    database.insert_test_entities(module_name="pkg", count=200)
    page = database.query_entities(FactScope.module("pkg"), cursor=None, limit=25)

    assert len(page.items) == 25
    assert page.next_cursor is not None
    assert page.truncated is True


def test_entity_query_rejects_unbounded_or_invalid_limits(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    with pytest.raises(ValueError, match="positive"):
        database.query_entities(FactScope.module("pkg"), cursor=None, limit=0)
    with pytest.raises(ValueError, match="maximum"):
        database.query_entities(
            FactScope.module("pkg"), cursor=None, limit=database.max_page_size + 1
        )
    with pytest.raises(ValueError, match="cursor"):
        database.query_entities(FactScope.module("pkg"), cursor="not-a-cursor", limit=1)


def test_scope_value_is_parameter_bound_not_sql(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    database.insert_test_entities(module_name="pkg", count=1)

    page = database.query_entities(
        FactScope.module("pkg' OR 1=1 --"), cursor=None, limit=10
    )

    assert page.items == ()


def test_relation_query_requires_allowlisted_types_and_is_bounded(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    database.insert_test_entities(module_name="pkg", count=2)
    database.insert_test_relations(source_uid="test_ent_00000", count=200)

    page = database.query_relations(
        entity_uids=("test_ent_00000",), relation_types=("calls",), limit=25
    )

    assert len(page.items) == 25
    assert page.next_cursor is not None
    assert page.truncated is True
    with pytest.raises(ValueError, match="allowlisted"):
        database.query_relations(
            entity_uids=("test_ent_00000",), relation_types=("calls; DROP TABLE entities",), limit=1
        )
    with pytest.raises(ValueError, match="at least one"):
        database.query_relations(entity_uids=(), relation_types=("calls",), limit=1)


def test_relation_query_uses_one_select_not_n_plus_one(tmp_path, monkeypatch):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")
    database.insert_test_entities(module_name="pkg", count=2)
    database.insert_test_relations(source_uid="test_ent_00000", count=200)
    statements: list[str] = []
    with database.open_read() as connection:
        monkeypatch.setattr(database, "open_read", lambda: connection)
        connection.set_trace_callback(statements.append)
        database.query_relations(
            entity_uids=("test_ent_00000",), relation_types=("calls",), limit=100
        )
        connection.set_trace_callback(None)

    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1


def test_read_connection_cannot_write_even_with_sql_parameters(tmp_path):
    database = FactsDatabase.create_new(tmp_path / "facts.sqlite3")

    with database.open_read() as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO source_files(relative_path) VALUES (?)", ("x.py",))
