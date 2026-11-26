import json

import pytest

from odooutil import query
from tests.helpers import ScriptedRpc


@pytest.mark.parametrize(
    ("argv", "method", "args", "result"),
    [
        (
            ["search-read", "res.partner", "--fields", "id", "name", "--offset", "2"],
            "search_read",
            [list[object]()],
            [{"id": 3, "name": "Example"}],
        ),
        (["search-count", "res.partner"], "search_count", [list[object]()], 42),
        (
            ["fields-get", "res.partner"],
            "fields_get",
            list[object](),
            {"name": {"type": "char"}},
        ),
    ],
)
def test_query_cli_only_performs_requested_read(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    method: str,
    args: list[object],
    result: object,
) -> None:
    rpc = ScriptedRpc()
    rpc.queue("res.partner", method, result)
    query.main(argv, connection_provider=rpc.connection)
    assert json.loads(capsys.readouterr().out) == result
    assert rpc.calls[0].args == args
    if method == "search_read":
        assert rpc.calls[0].kwargs == {"fields": ["id", "name"], "limit": 20, "offset": 2}
    assert not rpc.mutations
    rpc.assert_complete()


def test_monetary_queries_preserve_amounts_and_discover_currency_binding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rpc = ScriptedRpc()
    metadata = {"x_amount": {"type": "monetary", "currency_field": "x_currency_id"}}
    rpc.queue("x_example_item", "fields_get", metadata)
    query.main(["fields-get", "x_example_item"], connection_provider=rpc.connection)
    assert json.loads(capsys.readouterr().out) == metadata
    assert rpc.calls[0].kwargs == {
        "attributes": ["string", "type", "relation", "required", "readonly", "currency_field"]
    }
    amounts = [{"id": 1, "x_amount": -12.34, "x_currency_id": [7, "Example currency"]}]
    rpc.queue("x_example_item", "search_read", amounts)
    query.main(
        ["search-read", "x_example_item", "--fields", "id", "x_amount", "x_currency_id"],
        connection_provider=rpc.connection,
    )
    assert json.loads(capsys.readouterr().out) == amounts
    assert not rpc.mutations
    rpc.assert_complete()


@pytest.mark.parametrize(
    "argv",
    [
        ["search-count", "res.partner", "--domain", "{}"],
        ["search-count", "res.partner", "--domain", "bad-json"],
        ["search-count", "not a model"],
        ["search-read", "res.partner"],
        ["search-read", "res.partner", "--fields", "id", "--limit", "1001"],
        ["search-read", "res.partner", "--fields", "id", "--limit", "0"],
        ["search-read", "res.partner", "--fields", "id", "--offset", "-1"],
        ["--target-db", "example", "search-count", "res.partner"],
    ],
)
def test_invalid_queries_fail_without_connecting(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        query.main(argv)
