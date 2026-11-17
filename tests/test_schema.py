import json
import os
from pathlib import Path

import pytest

from odoobase import RpcConnection, RpcRecord
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


def computed_name_spec() -> RpcRecord:
    return {
        "compute": (
            "for record in self:\n"
            "    record['x_name'] = '%s - %s' % "
            "(record['x_date'], record['x_partner_id'].name)"
        ),
        "depends": "x_date,x_partner_id.name",
        "store": True,
        "readonly": True,
        "copied": False,
    }


def computed_name_field() -> RpcRecord:
    return {
        **partner_field(),
        "name": "x_name",
        "model": "x_document",
        "compute": False,
        "depends": False,
        "readonly": False,
        "copied": True,
        "store": True,
    }


def queue_computed_name_preview(rpc: ScriptedRpc, *, records: int = 0) -> None:
    rpc.queue("ir.model", "search_read", [{"id": 1, "model": "x_document", "state": "manual"}])
    rpc.queue("ir.model.fields", "search_read", [computed_name_field()])
    rpc.queue("x_document", "fields_get", {"x_date": {"type": "date"}})
    rpc.queue(
        "x_document",
        "fields_get",
        {"x_partner_id": {"type": "many2one", "relation": "res.partner"}},
    )
    rpc.queue("res.partner", "fields_get", {"name": {"type": "char"}})
    rpc.queue("x_document", "search_count", records)


def test_computed_name_is_previewed_journaled_and_verified(tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_computed_name_preview(rpc)
    queue_computed_name_preview(rpc)
    rpc.queue("ir.model.fields", "write", True)
    rpc.queue("ir.model.fields", "search_read", [{**computed_name_field(), **computed_name_spec()}])
    args = arguments(tmp_path)
    args.command, args.model, args.name = "field-update", "x_document", "x_name"
    client = schema.SchemaClient(*rpc.connection())
    plan = schema.build_plan(client, args, computed_name_spec())
    assert not rpc.mutations
    journal = schema.apply_plan(client, args, plan)
    assert journal["status"] == "verified"
    assert len(rpc.mutations) == 1
    assert rpc.mutations[0].args == [[2], computed_name_spec()]
    assert all(
        call.kwargs == {"context": {"active_test": False}}
        for call in rpc.calls
        if call.method == "search_count"
    )
    rpc.assert_complete()


@pytest.mark.parametrize("stale", [False, True])
def test_computed_name_refuses_existing_records(tmp_path: Path, stale: bool) -> None:
    rpc = ScriptedRpc()
    queue_computed_name_preview(rpc, records=0 if stale else 1)
    args = arguments(tmp_path)
    args.command, args.model, args.name = "field-update", "x_document", "x_name"
    client = schema.SchemaClient(*rpc.connection())
    if stale:
        plan = schema.build_plan(client, args, computed_name_spec())
        queue_computed_name_preview(rpc, records=1)
        with pytest.raises(ValueError, match="populated"):
            schema.apply_plan(client, args, plan)
        assert (
            json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))["status"]
            == "requires_review"
        )
    else:
        with pytest.raises(ValueError, match="populated"):
            schema.build_plan(client, args, computed_name_spec())
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"readonly": False}, "readonly=true"),
        ({"copied": True}, "copied=false"),
        ({"compute": "for:"}, "invalid syntax"),
        ({"compute": "   "}, "nonempty"),
        ({"depends": "x_name"}, "themselves"),
        ({"depends": "x_date;import os"}, "field paths"),
    ],
)
def test_computed_name_rejects_unsafe_settings(
    tmp_path: Path, changes: RpcRecord, message: str
) -> None:
    rpc = ScriptedRpc()
    rpc.queue("ir.model", "search_read", [{"id": 1, "model": "x_document", "state": "manual"}])
    rpc.queue("ir.model.fields", "search_read", [computed_name_field()])
    args = arguments(tmp_path)
    args.command, args.model, args.name = "field-update", "x_document", "x_name"
    with pytest.raises((ValueError, SyntaxError), match=message):
        schema.build_plan(
            schema.SchemaClient(*rpc.connection()), args, {**computed_name_spec(), **changes}
        )
    assert not rpc.mutations
    rpc.assert_complete()


def test_computed_name_readback_mismatch_retains_review_journal(tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_computed_name_preview(rpc)
    queue_computed_name_preview(rpc)
    rpc.queue("ir.model.fields", "write", True)
    rpc.queue(
        "ir.model.fields",
        "search_read",
        [{**computed_name_field(), **computed_name_spec(), "compute": "different"}],
    )
    args = arguments(tmp_path)
    args.command, args.model, args.name = "field-update", "x_document", "x_name"
    client = schema.SchemaClient(*rpc.connection())
    plan = schema.build_plan(client, args, computed_name_spec())
    with pytest.raises(ValueError, match="compute"):
        schema.apply_plan(client, args, plan)
    assert (
        json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))["status"]
        == "requires_review"
    )
    assert len(rpc.mutations) == 1
    rpc.assert_complete()


@pytest.mark.parametrize(("model", "state"), [("res.partner", "base"), ("x_document", "base")])
def test_computed_name_refuses_nonmanual_models(tmp_path: Path, model: str, state: str) -> None:
    rpc = ScriptedRpc()
    rpc.queue("ir.model", "search_read", [{"id": 1, "model": model, "state": state}])
    rpc.queue("ir.model.fields", "search_read", [computed_name_field()])
    args = arguments(tmp_path)
    args.command, args.model, args.name = "field-update", model, "x_name"
    with pytest.raises(ValueError, match="manual x_ model"):
        schema.build_plan(schema.SchemaClient(*rpc.connection()), args, computed_name_spec())
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize("apply", [False, True])
def test_schema_cli_accepts_a_provider_without_bypassing_apply_guards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    rpc = ScriptedRpc()
    queue_field_preview(rpc)
    if apply:
        queue_field_preview(rpc)
        rpc.queue("ir.model.fields", "create", 2)
        rpc.queue("ir.model.fields", "search_read", [partner_field()])
    options: dict[str, object] = {}

    def connect(**kwargs: object) -> RpcConnection:
        options.update(kwargs)
        return rpc.connection()

    path = tmp_path / "field.json"
    path.write_text(
        json.dumps({"ttype": "char", "field_description": "Reference"}), encoding="utf-8"
    )
    argv = [
        "--key-file",
        "explicit.key",
        "--target-url",
        "https://example.test",
        "--target-db",
        "example",
        "field-create",
        "res.partner",
        "x_reference",
        "--spec",
        str(path),
    ]
    if apply:
        with pytest.raises(SystemExit):
            schema.main([*argv, "--apply"], connection_provider=connect)
        assert not options
        argv += ["--apply", "--confirm-db", "example", "--journal", str(tmp_path / "journal.json")]
    schema.main(argv, connection_provider=connect)
    output = json.loads(capsys.readouterr().out)
    assert (output["status"] == "verified") if apply else (output["applied"] is False)
    assert options == {
        "key_file": Path("explicit.key"),
        "target_url": "https://example.test",
        "target_db": "example",
    }
    assert len(rpc.mutations) == (1 if apply else 0)
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


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("chatter", ["quiet", "normal"])
def test_record_create_cli_previews_or_creates_with_context_and_readback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], apply: bool, chatter: str
) -> None:
    rpc = ScriptedRpc()
    metadata = {
        "name": {"type": "char", "required": True, "store": True},
        "parent_id": {"type": "many2one", "relation": "res.partner"},
        "comment": {"type": "text"},
        "properties": {"type": "json"},
        "write_date": {"type": "datetime"},
        "message_ids": {"type": "one2many"},
    }
    values = {"name": "New", "parent_id": 7, "comment": False, "properties": {"key": "value"}}
    for _ in range(2 if apply else 1):
        rpc.queue("res.lang", "search_read", [{"id": 3}])
        rpc.queue("res.partner", "fields_get", metadata)
        rpc.queue("res.partner", "search_read", [{"id": 7}])
        if chatter == "quiet":
            rpc.queue("ir.module.module", "search_read", [])
    if apply:
        rpc.queue("res.partner", "create", 42)
        rpc.queue(
            "res.partner",
            "search_read",
            [{**values, "id": 42, "parent_id": [7, "Parent"], "write_date": "2026-01-01 00:00:00"}],
        )
        if chatter == "quiet":
            rpc.queue("mail.message", "search_read", [])
            rpc.queue("mail.mail", "search_read", [])
    spec_path, journal_path = tmp_path / "spec.json", tmp_path / "journal.json"
    spec_path.write_text(json.dumps({"values": values}), encoding="utf-8")
    argv = [
        "--target-url",
        "https://example.test",
        "--target-db",
        "example",
        "record-create",
        "res.partner",
        "--spec",
        str(spec_path),
        "--chatter",
        chatter,
        "--lang",
        "fr_FR",
    ]
    if apply:
        for options in (
            ["--apply"],
            ["--apply", "--confirm-db", "wrong", "--journal", str(journal_path)],
        ):
            with pytest.raises(SystemExit):
                schema.main([*argv, *options], connection_provider=rpc.connection)
            assert not rpc.calls
        argv += ["--apply", "--confirm-db", "example", "--journal", str(journal_path)]
    schema.main(argv, connection_provider=rpc.connection)
    output = json.loads(capsys.readouterr().out)
    if apply:
        assert output["status"] == "verified"
        assert output["result"] == output["after"]["id"] == 42
        assert output["plan"]["before"] is None
        assert json.loads(journal_path.read_text(encoding="utf-8")) == output
        if os.name == "posix":
            assert journal_path.stat().st_mode & 0o777 == 0o600
        assert len(rpc.mutations) == 1
        mutation = rpc.mutations[0]
        assert mutation.args == [values]
        assert mutation.kwargs["context"] == (
            {**schema.QUIET_CONTEXT, "lang": "fr_FR"}
            if chatter == "quiet"
            else {"active_test": False, "lang": "fr_FR"}
        )
        readback = next(
            call
            for call in rpc.calls
            if call.model == "res.partner"
            and call.method == "search_read"
            and call.args == [[["id", "in", [42]]]]
        )
        assert readback.kwargs["fields"] == [
            "id",
            "comment",
            "name",
            "parent_id",
            "properties",
            "write_date",
        ]
        if chatter == "quiet":
            assert (
                output["chatter_before"]
                == output["chatter_after"]
                == {"mail.message": [], "mail.mail": []}
            )
            audits = [call for call in rpc.calls if call.model in {"mail.message", "mail.mail"}]
            assert all(
                call.args == [[["model", "=", "res.partner"], ["res_id", "in", [42]]]]
                for call in audits
            )
    else:
        assert output["applied"] is False
        assert output["action"] == "create"
        assert output["before"] is None
        assert output["values"] == values
        assert not rpc.mutations
        assert not journal_path.exists()
    rpc.assert_complete()


@pytest.mark.parametrize(
    ("spec", "field", "message"),
    [
        ({"values": {"name": "New"}, "expected": {}}, None, "supports only values"),
        ({"values": {}}, None, "nonempty object"),
        ({"values": {"name": "New"}}, {}, "Unknown or inaccessible"),
        ({"values": {"name": "New"}}, {"readonly": True}, "readonly"),
        ({"values": {"name": "New"}}, {"store": False}, "non-stored"),
        ({"values": {"name": "New"}}, {"related": "parent_id.name"}, "related"),
        ({"values": {"name": [7]}}, {"type": "many2many"}, "Unsupported record field type"),
        ({"values": {"name": False}}, {"required": True}, "required field"),
    ],
)
def test_record_create_reuses_field_safety_and_rejects_expected_guards(
    tmp_path: Path, spec: RpcRecord, field: RpcRecord | None, message: str
) -> None:
    rpc = ScriptedRpc()
    if field is not None:
        rpc.queue("res.partner", "fields_get", {"name": {"type": "char", **field}} if field else {})
    args = arguments(tmp_path)
    args.command = "record-create"
    with pytest.raises(ValueError, match=message):
        schema.build_plan(schema.SchemaClient(*rpc.connection()), args, spec)
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize("acknowledged", [False, True])
def test_record_create_requires_automation_acknowledgement(
    tmp_path: Path, acknowledged: bool
) -> None:
    rpc = ScriptedRpc()
    rpc.queue("res.partner", "fields_get", {"name": {"type": "char"}})
    rpc.queue("ir.module.module", "search_read", [{"id": 1}])
    args = arguments(tmp_path)
    args.command, args.allow_automation = "record-create", acknowledged
    client = schema.SchemaClient(*rpc.connection())
    if acknowledged:
        assert schema.build_plan(client, args, {"values": {"name": "New"}})["action"] == "create"
    else:
        with pytest.raises(ValueError, match="Automation is installed"):
            schema.build_plan(client, args, {"values": {"name": "New"}})
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize(
    "failure",
    ["existing_journal", "stale", "lost_response", "invalid_response", "readback", "chatter"],
)
def test_record_create_failures_never_retry_or_delete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    rpc = ScriptedRpc()
    metadata = {"name": {"type": "char"}, "message_ids": {"type": "one2many"}}
    rpc.queue("res.partner", "fields_get", metadata)
    rpc.queue("ir.module.module", "search_read", [])
    args = arguments(tmp_path)
    args.command = "record-create"
    client = schema.SchemaClient(*rpc.connection())
    plan = schema.build_plan(client, args, {"values": {"name": "New"}})
    path = tmp_path / "journal.json"
    if failure == "existing_journal":
        path.write_text("original", encoding="utf-8")
        with pytest.raises(FileExistsError):
            schema.apply_plan(client, args, plan)
        assert path.read_text(encoding="utf-8") == "original"
        assert not rpc.mutations
        rpc.assert_complete()
        return
    rpc.queue(
        "res.partner",
        "fields_get",
        {**metadata, "name": {"type": "char", "required": True}}
        if failure == "stale"
        else metadata,
    )
    rpc.queue("ir.module.module", "search_read", [])
    if failure != "stale":
        rpc.queue(
            "res.partner",
            "create",
            RuntimeError("Unknown mutation outcome: synthetic-private-value")
            if failure == "lost_response"
            else True
            if failure == "invalid_response"
            else 42,
        )
    if failure in {"readback", "chatter"}:
        rpc.queue(
            "res.partner",
            "search_read",
            [{"id": 42, "name": "Wrong" if failure == "readback" else "New"}],
        )
    if failure == "chatter":
        rpc.queue("mail.message", "search_read", [{"id": 8}])
        rpc.queue("mail.mail", "search_read", [])
    with pytest.raises((ValueError, RuntimeError)) as caught:
        schema.apply_plan(client, args, plan)
    journal = json.loads(path.read_text(encoding="utf-8"))
    assert journal["status"] == "requires_review"
    if failure in {"readback", "chatter"}:
        assert journal["result"] == 42
    if failure == "lost_response":
        assert "synthetic-private-value" in str(caught.value)
        assert "synthetic-private-value" not in caplog.text
        assert any("error_type=RuntimeError" in record.message for record in caplog.records)
        assert all(record.exc_info is None for record in caplog.records)
    assert [call.method for call in rpc.mutations] == ([] if failure == "stale" else ["create"])
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


def chatter_model(*, enabled: bool = False) -> RpcRecord:
    return {
        "id": 8,
        "name": "Example",
        "model": "x_example",
        "state": "manual",
        "access_ids": [11],
        "is_mail_thread": enabled,
        "is_mail_activity": False,
    }


@pytest.mark.parametrize("apply", [False, True])
def test_enable_chatter_cli_previews_or_rechecks_journals_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], apply: bool
) -> None:
    rpc = ScriptedRpc()
    rpc.queue("ir.model", "search_read", [chatter_model()])
    if apply:
        rpc.queue("ir.model", "search_read", [chatter_model()])
        rpc.queue("ir.model", "write", True)
        rpc.queue("ir.model", "search_read", [chatter_model(enabled=True)])
        rpc.queue(
            "x_example",
            "fields_get",
            {
                "message_ids": {"type": "one2many", "relation": "mail.message"},
                "message_follower_ids": {"type": "one2many", "relation": "mail.followers"},
                "message_partner_ids": {"type": "many2many", "relation": "res.partner"},
            },
        )
    argv = [
        "--target-url",
        "https://example.test",
        "--target-db",
        "example",
        "model-enable-chatter",
        "x_example",
    ]
    path = tmp_path / "chatter-journal.json"
    if apply:
        with pytest.raises(SystemExit):
            schema.main([*argv, "--apply"], connection_provider=rpc.connection)
        with pytest.raises(SystemExit):
            schema.main(
                [*argv, "--apply", "--confirm-db", "wrong", "--journal", str(path)],
                connection_provider=rpc.connection,
            )
        argv += ["--apply", "--confirm-db", "example", "--journal", str(path)]
    schema.main(argv, connection_provider=rpc.connection)
    output = json.loads(capsys.readouterr().out)
    if apply:
        assert output["status"] == "verified"
        assert json.loads(path.read_text(encoding="utf-8")) == output
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600
        assert len(rpc.mutations) == 1
        assert rpc.mutations[0].args == [[8], {"is_mail_thread": True}]
    else:
        assert output["applied"] is False
        assert output["values"] == {"is_mail_thread": True}
        assert "cannot be disabled" in output["side_effect_warning"]
        assert not rpc.mutations
        assert not path.exists()
    rpc.assert_complete()


@pytest.mark.parametrize("case", ["builtin", "missing", "base", "enabled", "spec"])
def test_enable_chatter_refuses_unsupported_changes(tmp_path: Path, case: str) -> None:
    rpc = ScriptedRpc()
    args = arguments(tmp_path)
    args.command, args.model = "model-enable-chatter", "x_example"
    if case == "builtin":
        args.model = "res.partner"
    elif case != "spec":
        row = chatter_model(enabled=case == "enabled")
        if case == "base":
            row["state"] = "base"
        rpc.queue("ir.model", "search_read", [] if case == "missing" else [row])
    with pytest.raises(ValueError):
        schema.build_plan(
            schema.SchemaClient(*rpc.connection()),
            args,
            {"is_mail_thread": False} if case == "spec" else None,
        )
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize(
    "failure", ["stale", "lost_response", "readback", "missing_fields", "wrong_relation", "acl"]
)
def test_enable_chatter_failures_require_review_without_retry(tmp_path: Path, failure: str) -> None:
    rpc = ScriptedRpc()
    rpc.queue("ir.model", "search_read", [chatter_model()])
    rpc.queue("ir.model", "search_read", [chatter_model(enabled=failure == "stale")])
    if failure != "stale":
        rpc.queue(
            "ir.model",
            "write",
            RuntimeError("Unknown mutation outcome") if failure == "lost_response" else True,
        )
    if failure not in {"stale", "lost_response"}:
        after = chatter_model(enabled=failure != "readback")
        if failure == "acl":
            after["access_ids"] = [12]
        rpc.queue("ir.model", "search_read", [after])
    if failure in {"missing_fields", "wrong_relation"}:
        rpc.queue(
            "x_example",
            "fields_get",
            {}
            if failure == "missing_fields"
            else {"message_ids": {"type": "many2one", "relation": "mail.message"}},
        )
    args = arguments(tmp_path)
    args.command, args.model = "model-enable-chatter", "x_example"
    client = schema.SchemaClient(*rpc.connection())
    plan = schema.build_plan(client, args, None)
    with pytest.raises((ValueError, RuntimeError)):
        schema.apply_plan(client, args, plan)
    journal = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    assert journal["status"] == "requires_review"
    assert len(rpc.mutations) == (0 if failure == "stale" else 1)
    rpc.assert_complete()
