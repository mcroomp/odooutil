import json
from pathlib import Path

import pytest

from odoobase import RpcRecord
from odoobase.rpc import RpcClient
from odooutil import report
from tests.helpers import ScriptedRpc


def spec() -> RpcRecord:
    return {
        "report": {"name": "Example"},
        "columns": [{"name": "Balance", "expression_label": "balance"}],
        "lines": [
            {
                "code": "INCOME",
                "name": "Income",
                "expressions": [{"label": "balance", "engine": "account_codes", "formula": "-7"}],
            }
        ],
    }


@pytest.mark.parametrize("kind", ["monetary", "float"])
@pytest.mark.parametrize("amount", [0, -12.34, 123.45])
def test_report_numeric_settings_preserve_amounts(kind: str, amount: int | float) -> None:
    rpc = ScriptedRpc()
    rpc.queue("account.report", "fields_get", {"x_amount": {"type": kind}})
    values: RpcRecord = {"x_amount": amount}
    assert (
        report.native_values(
            RpcClient(*rpc.connection()),
            "account.report",
            values,
            {},
        )
        == values
    )
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize("amount", [False, True, None, "12.34", float("inf"), float("nan")])
def test_report_monetary_settings_refuse_invalid_amounts(amount: object) -> None:
    rpc = ScriptedRpc()
    rpc.queue("account.report", "fields_get", {"x_amount": {"type": "monetary"}})
    with pytest.raises(ValueError):
        report.native_values(
            RpcClient(*rpc.connection()),
            "account.report",
            {"x_amount": amount},
            {},
        )
    assert not rpc.mutations
    rpc.assert_complete()


def queue_plan(rpc: ScriptedRpc, *, existing: bool = False) -> None:
    for model, fields in {
        "account.report": {"name": {"type": "char"}},
        "account.report.column": {
            "name": {"type": "char"},
            "expression_label": {"type": "char"},
        },
        "account.report.line": {"code": {"type": "char"}, "name": {"type": "char"}},
        "account.report.expression": {
            "label": {"type": "char"},
            "engine": {"type": "char"},
            "formula": {"type": "text"},
        },
    }.items():
        rpc.queue(model, "fields_get", fields)
    if not existing:
        rpc.queue("account.report", "search_read", [])
        return
    rpc.queue(
        "account.report",
        "read",
        [
            {
                "id": 41,
                "name": "Example",
                "line_ids": [51],
                "column_ids": [61],
                "write_date": False,
            }
        ],
    )
    rpc.queue(
        "account.report.line",
        "read",
        [
            {
                "id": 51,
                "code": "INCOME",
                "name": "Income",
                "parent_id": False,
                "expression_ids": [71],
                "write_date": False,
            }
        ],
    )
    rpc.queue(
        "account.report.expression",
        "read",
        [
            {
                "id": 71,
                "label": "balance",
                "engine": "account_codes",
                "formula": "-7",
                "write_date": False,
            }
        ],
    )
    rpc.queue(
        "account.report.column",
        "read",
        [
            {
                "id": 61,
                "name": "Balance",
                "expression_label": "balance",
                "write_date": False,
            }
        ],
    )


def arguments(tmp_path: Path) -> report.ReportArgs:
    args = report.ReportArgs()
    args.target_url, args.target_db = "https://example.test", "example"
    args.report_id = None
    args.journal = tmp_path / "journal.json"
    args.allow_delete = False
    return args


@pytest.mark.parametrize("failure", ["none", "lost_create", "render", "rollback"])
def test_report_publication_preserves_verification_and_uncertain_outcomes(
    tmp_path: Path, failure: str
) -> None:
    rpc = ScriptedRpc()
    queue_plan(rpc)
    queue_plan(rpc)
    rpc.queue(
        "account.report",
        "create",
        RuntimeError("Create response lost") if failure == "lost_create" else 41,
    )
    if failure != "lost_create":
        queue_plan(rpc, existing=True)
        rpc.queue("account.report", "get_options", {"report_id": 41})
        rpc.queue(
            "account.report",
            "get_report_information",
            {"lines": [{"id": "line-51"}]} if failure == "none" else RuntimeError("Render failed"),
        )
        if failure in {"render", "rollback"}:
            rpc.queue(
                "account.report",
                "unlink",
                RuntimeError("Cleanup failed") if failure == "rollback" else True,
            )
    client = RpcClient(*rpc.connection())
    plan = report.build_plan(client, spec())
    assert not rpc.mutations
    if failure == "none":
        journal = report.apply_plan(client, arguments(tmp_path), spec(), plan)
        assert journal["status"] == "verified"
        assert journal.get("rendered_lines") == 1
    else:
        with pytest.raises(RuntimeError):
            report.apply_plan(client, arguments(tmp_path), spec(), plan)
        journal = report.object_record(
            json.loads((tmp_path / "journal.json").read_text(encoding="utf-8")), "Journal"
        )
        assert (
            journal["status"]
            == {
                "lost_create": "requires_review",
                "render": "rolled_back",
                "rollback": "rollback_failed",
            }[failure]
        )
        if failure == "lost_create":
            assert journal["inflight_create"] == "account.report"
            assert len(rpc.mutations) == 1
    rpc.assert_complete()


@pytest.mark.parametrize(
    ("formula", "message"),
    [("MISSING.balance", "undefined"), ("INCOME.balance", "Cyclic")],
)
def test_invalid_aggregation_dependencies_fail(formula: str, message: str) -> None:
    value = report.validate_spec(spec())
    value["lines"][0]["expressions"] = [
        {"label": "balance", "engine": "aggregation", "formula": formula}
    ]
    with pytest.raises(ValueError, match=message):
        report.validate_spec(value)


def test_report_removals_need_explicit_permission(tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_plan(rpc)
    client = RpcClient(*rpc.connection())
    plan = report.build_plan(client, spec())
    plan["removals"] = [{"id": 10, "key": "old"}]
    with pytest.raises(ValueError, match="allow-delete"):
        report.apply_plan(client, arguments(tmp_path), spec(), plan)
    assert not (tmp_path / "journal.json").exists()
    assert not rpc.mutations
    rpc.assert_complete()


def test_report_cli_requests_jsonrpc(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rpc = ScriptedRpc()
    queue_plan(rpc)
    connection_options: dict[str, object] = {}

    def connect(**kwargs: object) -> tuple[ScriptedRpc, str, int, str]:
        connection_options.update(kwargs)
        return rpc, "example", 17, "test-key"

    path = tmp_path / "report.json"
    path.write_text(json.dumps(spec()), encoding="utf-8")
    report.main(
        [
            "--target-url",
            "https://example.test",
            "--target-db",
            "example",
            "--spec",
            str(path),
        ],
        connection_provider=connect,
    )
    assert connection_options == {
        "key_file": None,
        "target_url": "https://example.test",
        "target_db": "example",
        "rpc_protocol": "jsonrpc",
    }
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert not rpc.mutations
    rpc.assert_complete()
