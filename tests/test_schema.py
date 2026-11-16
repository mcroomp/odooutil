import json
import os
from pathlib import Path

import pytest
from odoobase import RpcRecord
from odooutil import schema

from tests.helpers import ScriptedRpc, partner_field


def arguments(tmp_path: Path, *, apply: bool = False) -> schema.SchemaArgs:
    args = schema.SchemaArgs()
    args.target_url = "https://example.test"
    args.target_db = "example"
    args.command = "field-create"
    args.model = "res.partner"
    args.name = "x_reference"
    args.apply = apply
    args.journal = tmp_path / "journal.json"
    return args


def queue_field_preview(rpc: ScriptedRpc, *, existing: bool = False) -> None:
    rpc.queue("ir.model", "search_read", [{"id": 1, "model": "res.partner", "state": "base"}])
    rpc.queue("ir.model.fields", "search_read", [partner_field()] if existing else [])


def test_preview_is_read_only() -> None:
    rpc = ScriptedRpc()
    queue_field_preview(rpc)
    args = schema.SchemaArgs()
    args.command, args.model, args.name = "field-create", "res.partner", "x_reference"
    plan = schema.build_plan(
        schema.SchemaClient(*rpc.connection()),
        args,
        {"ttype": "char", "field_description": "Reference"},
    )
    assert plan["action"] == "create"
    assert not rpc.mutations
    rpc.assert_complete()


def test_field_creation_is_rechecked_journaled_and_verified(tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_field_preview(rpc)
    queue_field_preview(rpc)
    rpc.queue("ir.model.fields", "create", 2)
    rpc.queue("ir.model.fields", "search_read", [partner_field()])
    client = schema.SchemaClient(*rpc.connection())
    args = arguments(tmp_path, apply=True)
    plan = schema.build_plan(client, args, {"ttype": "char", "field_description": "Reference"})
    journal = schema.apply_plan(client, args, plan)
    assert journal["status"] == "verified"
    path = tmp_path / "journal.json"
    assert json.loads(path.read_text(encoding="utf-8")) == journal
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert "test-key" not in path.read_text(encoding="utf-8")
    assert len(rpc.mutations) == 1
    rpc.assert_complete()


@pytest.mark.parametrize("failure", ["stale", "lost_response", "readback"])
def test_failed_schema_writes_retain_review_journal(tmp_path: Path, failure: str) -> None:
    rpc = ScriptedRpc()
    queue_field_preview(rpc)
    queue_field_preview(rpc, existing=failure == "stale")
    if failure != "stale":
        rpc.queue(
            "ir.model.fields",
            "create",
            RuntimeError("Unknown mutation outcome") if failure == "lost_response" else 2,
        )
    if failure == "readback":
        rpc.queue("ir.model.fields", "search_read", [{**partner_field(), "ttype": "text"}])
    client = schema.SchemaClient(*rpc.connection())
    args = arguments(tmp_path)
    plan = schema.build_plan(client, args, {"ttype": "char", "field_description": "Reference"})
    with pytest.raises((ValueError, RuntimeError)):
        schema.apply_plan(client, args, plan)
    journal = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    assert journal["status"] == "requires_review"
    assert len(rpc.mutations) == (0 if failure == "stale" else 1)
    rpc.assert_complete()


def test_existing_journal_refuses_mutation(tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_field_preview(rpc)
    client = schema.SchemaClient(*rpc.connection())
    args = arguments(tmp_path)
    plan = schema.build_plan(client, args, {"ttype": "char", "field_description": "Reference"})
    path = tmp_path / "journal.json"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        schema.apply_plan(client, args, plan)
    assert path.read_text(encoding="utf-8") == "original"
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize("noop", [False, True])
def test_record_updates_and_noops_keep_expected_value_guards(tmp_path: Path, noop: bool) -> None:
    rpc = ScriptedRpc()
    metadata = {
        "name": {"type": "char", "store": True},
        "write_date": {"type": "datetime"},
    }
    original = {"id": 9, "name": "Before", "write_date": "2026-01-01 00:00:00"}
    for _ in range(2):
        rpc.queue("res.partner", "fields_get", metadata)
        rpc.queue("res.partner", "search_read", [original])
        rpc.queue("ir.module.module", "search_read", [])
    desired = "Before" if noop else "After"
    if not noop:
        rpc.queue("res.partner", "write", True)
    rpc.queue("res.partner", "search_read", [{**original, "name": desired}])
    args = arguments(tmp_path)
    args.command, args.record_id = "record-set", 9
    client = schema.SchemaClient(*rpc.connection())
    plan = schema.build_plan(
        client, args, {"values": {"name": desired}, "expected": {"name": "Before"}}
    )
    journal = schema.apply_plan(client, args, plan)
    assert journal["status"] == "verified"
    assert bool(journal.get("no_change")) == noop
    assert len(rpc.mutations) == (0 if noop else 1)
    rpc.assert_complete()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ({"type": "many2one"}, True),
        ({"type": "many2one"}, 0),
        ({"type": "integer"}, False),
        ({"type": "float"}, float("nan")),
        ({"type": "char", "required": True}, False),
        ({"type": "date"}, "2026-13-01"),
        ({"type": "json"}, {"invalid": float("inf")}),
    ],
)
def test_invalid_record_values_fail(field: RpcRecord, value: object) -> None:
    with pytest.raises(ValueError):
        schema.record_value(field, value)


@pytest.mark.parametrize(
    "options",
    [
        ["--apply"],
        ["--apply", "--confirm-db", "wrong", "--journal", "journal.json"],
        ["--apply", "--confirm-db", "example", "--journal", "journal.json"],
    ],
)
def test_delete_confirmation_and_data_loss_gates(options: list[str]) -> None:
    with pytest.raises(SystemExit):
        schema.parse_args(
            [
                "--target-url",
                "https://example.test",
                "--target-db",
                "example",
                "field-delete",
                "res.partner",
                "x_reference",
                *options,
            ]
        )


def test_batch_record_ids_are_isolated_between_argument_instances() -> None:
    first, second = schema.SchemaArgs(), schema.SchemaArgs()
    first.record_ids.append(1)
    assert second.record_ids == []
