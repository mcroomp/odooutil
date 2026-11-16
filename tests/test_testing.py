import json
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy

import pytest

from odoobase import ConnectionProvider
from odoobase.testing import FakeOdooTransport, RpcCall
from odooutil import query


def rpc(
    server: FakeOdooTransport,
    model: str,
    method: str,
    args: Sequence[object],
    **kwargs: object,
) -> object:
    models, db, uid, password = server.connection()
    return models.execute_kw(db, uid, password, model, method, args, kwargs)


def test_query_provider_uses_independent_records_and_detached_replies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records: dict[str, list[dict[str, object]]] = {
        "res.partner": [{"id": 7, "name": "Before", "category_id": [2]}]
    }
    original = deepcopy(records)
    server = FakeOdooTransport(records=records)
    provider: ConnectionProvider = server.connection
    server["res.partner"][7]["name"] = "Remote"
    query.main(
        ["search-read", "res.partner", "--fields", "name", "category_id"],
        connection_provider=provider,
    )
    assert json.loads(capsys.readouterr().out) == [{"id": 7, "name": "Remote", "category_id": [2]}]
    assert records == original
    result = rpc(server, "res.partner", "read", [[7]], fields=["category_id"])
    assert isinstance(result, list)
    result.clear()
    assert server["res.partner"][7]["category_id"] == [2]
    assert server.calls[0] == RpcCall(
        "res.partner",
        "search_read",
        [[]],
        {"fields": ["name", "category_id"], "limit": 20, "offset": 0},
    )
    assert server.connection_count == server.request_count == 2
    with pytest.raises(AssertionError, match="target database"):
        server.connection(target_db="other")
    with pytest.raises(AssertionError, match="connection"):
        server.execute_kw("other", 1, "fixture", "res.partner", "read", [[7]])


def test_metadata_drives_queries_on_custom_models_including_empty_tables() -> None:
    server = FakeOdooTransport(
        records={
            "work.item": [
                {"id": 1, "name": "First Task", "active": True, "owner_id": [7, "Seven"]},
                {"id": 2, "name": "Other task", "active": False, "owner_id": False},
                {"id": 3, "name": "Third", "active": True, "owner_id": [8, "Eight"]},
            ],
            "work.empty": [],
        },
        fields={
            "work.item": {
                "name": {"type": "char", "string": "Title", "required": True},
                "owner_id": {"type": "many2one", "relation": "res.partner"},
            },
            "work.empty": {"name": {"type": "char"}},
        },
    )
    assert rpc(server, "work.item", "fields_get", [["name"]], attributes=["type", "required"]) == {
        "name": {"type": "char", "required": True}
    }
    assert rpc(
        server,
        "work.item",
        "search",
        [["|", ["id", "=", 1], ["id", ">=", 2]]],
        order="id desc",
        offset=1,
        limit=1,
        context={"active_test": False},
    ) == [2]
    assert rpc(
        server,
        "work.item",
        "search_read",
        [[["owner_id", "in", [7, 8]], "!", ["name", "ilike", "task"]]],
        fields=["name"],
    ) == [{"id": 3, "name": "Third"}]
    assert rpc(server, "work.item", "search", [[["name", "=like", "First _ask"]]]) == [1]
    assert rpc(server, "work.item", "search_count", [[]], limit=1) == 1
    assert rpc(server, "work.empty", "search_read", [[]], fields=["name"]) == []


INVALID_CALLS: list[tuple[str, str, list[object], dict[str, object], str]] = [
    ("unknown", "search", [[]], {}, "Unconfigured"),
    ("empty", "made_up", [], {}, "Unsupported fake RPC"),
    ("empty", "search", [[["id", "child_of", 1]]], {}, "domain"),
    ("empty", "search", [[["id", "ilike", 1]]], {}, "text domain"),
    ("empty", "search", [["|", ["id", "=", 1]]], {}, "Incomplete"),
    ("empty", "search_read", [[]], {"fields": ["unknown"]}, "Unknown fake fields"),
    ("empty", "search", [[]], {"order": "unknown"}, "sort field"),
    ("empty", "search", [[]], {"order": "id sideways"}, "sort"),
    ("empty", "search", [[]], {"limit": -1}, "limit"),
    ("empty", "search", [[]], {"offset": True}, "options"),
    ("empty", "create", [{"unknown": 1}], {}, "Unknown or immutable"),
]


@pytest.mark.parametrize(("model", "method", "args", "kwargs", "message"), INVALID_CALLS)
def test_unconfigured_behavior_fails_even_for_empty_models(
    model: str,
    method: str,
    args: list[object],
    kwargs: dict[str, object],
    message: str,
) -> None:
    server = FakeOdooTransport(records={"empty": []})
    with pytest.raises(AssertionError, match=message):
        rpc(server, model, method, args, **kwargs)
    assert server["empty"] == {}


@pytest.mark.parametrize("record_id", [0, -1, True, "7"])
def test_invalid_native_ids_are_rejected(record_id: object) -> None:
    with pytest.raises(AssertionError, match="positive fake record ID"):
        FakeOdooTransport(records={"item": [{"id": record_id}]})


def test_duplicate_ids_are_not_silently_overwritten() -> None:
    with pytest.raises(ValueError, match="Duplicate fake record"):
        FakeOdooTransport(records={"item": [{"id": 1}, {"id": 1}]})


def test_crud_defaults_and_relation_labels_use_only_supplied_metadata() -> None:
    server = FakeOdooTransport(
        records={"work.owner": [{"id": 7, "display_name": "Seven"}]},
        fields={
            "work.item": {
                "name": {"type": "char"},
                "owner_id": {"type": "many2one", "relation": "work.owner"},
            }
        },
        defaults={"work.item": {"name": "Default"}},
    )
    assert rpc(server, "work.item", "create", [[{"owner_id": 7}, {"name": "Second"}]]) == [1, 2]
    assert rpc(server, "work.item", "read", [[1]], fields=["name", "owner_id"]) == [
        {"id": 1, "name": "Default", "owner_id": [7, "Seven"]},
    ]
    assert rpc(server, "work.item", "write", [[1], {"owner_id": False, "name": "Updated"}])
    assert server["work.item"][1]["owner_id"] is False
    with pytest.raises(KeyError):
        rpc(server, "work.item", "write", [[1], {"owner_id": [99, "Missing"]}])
    assert server["work.item"][1]["owner_id"] is False
    assert rpc(server, "work.item", "unlink", [[1]]) is True
    assert rpc(server, "work.item", "create", [{"name": "Next"}]) == 3


def test_owned_child_commands_reparent_and_cascade_without_model_name_assumptions() -> None:
    server = FakeOdooTransport(
        records={
            "tree": [{"id": 5, "name": "Tree"}],
            "node": [
                {"id": 1, "name": "Root", "parent_id": False, "tree_id": [5, "Tree"]},
                {"id": 2, "name": "Child", "parent_id": [1, "Root"], "tree_id": [5, "Tree"]},
                {"id": 3, "name": "Other", "parent_id": False, "tree_id": [5, "Tree"]},
            ],
        },
        fields={"node": {"name": {"type": "char"}}},
        children={
            "tree": {"node_ids": ("node", "tree_id")},
            "node": {"children_ids": ("node", "parent_id")},
        },
    )
    rpc(
        server,
        "node",
        "write",
        [
            [1],
            {
                "children_ids": [
                    [1, 2, {"name": "Updated"}],
                    [0, 0, {"name": "Created"}],
                ]
            },
        ],
    )
    assert server["node"][4]["parent_id"] == [1, "Root"]
    rpc(server, "node", "write", [[3], {"children_ids": [[4, 2, 0]]}])
    assert server["node"][2]["parent_id"] == [3, "Other"]
    rpc(server, "node", "write", [[1], {"children_ids": [[2, 4, 0]]}])
    assert rpc(server, "node", "read", [[1]], fields=["children_ids"]) == [
        {"id": 1, "children_ids": []}
    ]
    before = deepcopy(server["node"])
    with pytest.raises(AssertionError, match="child command"):
        rpc(
            server,
            "tree",
            "write",
            [
                [5],
                {
                    "node_ids": [
                        [1, 1, {"name": "Must roll back"}],
                        [99, 0, 0],
                    ]
                },
            ],
        )
    assert server["node"] == before
    rpc(server, "tree", "unlink", [[5]])
    assert server["tree"] == server["node"] == {}


def test_many2many_commands_are_atomic_and_do_not_become_many2one_relations() -> None:
    server = FakeOdooTransport(
        records={"item": [{"id": 1, "tag_ids": [2]}]},
        fields={"item": {"tag_ids": {"type": "many2many", "relation": "tag"}}},
    )
    for commands, expected in (
        ([[4, 3, 0], [4, 3, 0], [3, 2, 0]], [3]),
        ([[6, 0, [4, 5]]], [4, 5]),
        ([[5, 0, 0]], []),
    ):
        rpc(server, "item", "write", [[1], {"tag_ids": commands}])
        assert rpc(server, "item", "read", [[1], ["tag_ids"]]) == [{"id": 1, "tag_ids": expected}]
    with pytest.raises(AssertionError, match="relation command"):
        rpc(server, "item", "write", [[1], {"tag_ids": [[4, 2, 0], []]}])
    assert server["item"][1]["tag_ids"] == []


def test_owned_child_metadata_supports_creating_into_an_empty_child_table() -> None:
    server = FakeOdooTransport(
        records={"parent": [{"id": 1, "name": "Parent"}], "child": []},
        fields={"child": {"name": {"type": "char"}}},
        children={"parent": {"child_ids": ("child", "parent_id")}},
    )
    rpc(server, "parent", "write", [[1], {"child_ids": [[0, 0, {"name": "New"}]]}])
    assert server["child"] == {1: {"id": 1, "name": "New", "parent_id": [1, "Parent"]}}


@pytest.mark.parametrize("after", [False, True])
def test_failure_injection_distinguishes_rejection_from_lost_reply(after: bool) -> None:
    server = FakeOdooTransport(records={"item": [{"id": 1, "name": "Before"}]})
    server.fail_next("item", "write", RuntimeError("Unavailable"), ids=[1], after=after)
    with pytest.raises(RuntimeError, match="Unavailable"):
        rpc(server, "item", "write", [[1], {"name": "After"}])
    assert server["item"][1]["name"] == ("After" if after else "Before")
    assert rpc(server, "item", "write", [[1], {"name": "Retry"}]) is True
    assert server["item"][1]["name"] == "Retry"


def test_hooks_keep_mutations_real_and_restore_metadata_and_ids_on_failure() -> None:
    server = FakeOdooTransport(
        records={"item": [{"id": 7, "name": "Before"}]},
        fields={"item": {"name": {"type": "char"}}},
    )
    server.reply_next("item", "write", False, ids=[7])
    assert rpc(server, "item", "write", [[7], {"name": "Committed"}]) is False
    assert server["item"][7]["name"] == "Committed"
    server.transform_reply("item", "read", lambda _, __, result: {"rows": result})
    assert rpc(server, "item", "read", [[7]], fields=["name"]) == {
        "rows": [{"id": 7, "name": "Committed"}]
    }

    def failing_effect(remote: FakeOdooTransport, _: RpcCall) -> None:
        remote.fields["item"]["incomplete"] = {"type": "char"}
        raise RuntimeError("Scenario failed")

    server.after("item", "create", failing_effect)
    with pytest.raises(RuntimeError, match="Scenario failed"):
        rpc(server, "item", "create", [{"name": "Rolled back"}])
    assert server["item"] == {7: {"id": 7, "name": "Committed"}}
    assert server.fields["item"] == {"name": {"type": "char"}}
    server.after("item", "create", lambda _, __: None)
    assert rpc(server, "item", "create", [{"name": "Next"}]) == 8
    server.on("item", "custom_action", lambda remote, _: len(remote["item"]))
    assert rpc(server, "item", "custom_action", []) == 2
    with pytest.raises(ValueError, match="do not replace"):
        server.on("item", "write", lambda _, __: False)


def test_trace_logs_metadata_without_credentials_queries_or_record_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = FakeOdooTransport(
        records={"item": [{"id": 1, "name": "private-content"}]},
        password="private-key",
    )
    with caplog.at_level(logging.DEBUG, logger="odoobase.testing"):
        rpc(
            server,
            "item",
            "search_read",
            [[["name", "=", "private-content"]]],
            fields=["name"],
            context={"token": "private-context"},
        )
        server.fail_next("item", "write", RuntimeError("private-error"))
        with pytest.raises(RuntimeError, match="private-error"):
            rpc(server, "item", "write", [[1], {"name": "private-mutation"}])
    assert "model=item method=search_read result_count=1" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    for private in (
        "private-content",
        "private-key",
        "private-context",
        "private-error",
        "private-mutation",
    ):
        assert private not in caplog.text


def test_before_hook_observes_original_state() -> None:
    server = FakeOdooTransport(records={"item": [{"id": 1, "name": "Before"}]})
    seen: list[Mapping[str, object]] = []
    server.before("item", "write", lambda remote, _: seen.append(dict(remote["item"][1])))
    rpc(server, "item", "write", [[1], {"name": "After"}])
    assert seen == [{"id": 1, "name": "Before"}]
