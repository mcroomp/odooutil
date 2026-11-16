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
