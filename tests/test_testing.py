import json
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy

import pytest

from odoobase import ConnectionProvider
from odoobase.models import RawPartner
from odoobase.models.v19_4 import ODOO_VERSION
from odoobase.models.v19_4 import RawPartner as VersionedPartner
from odoobase.testing import FakeOdooTransport, FakeSchema, RpcCall
from odooutil import query
from odooutil.testing import FakeOdooTransport as UtilityFakeOdooTransport


class ExtendedPartner(VersionedPartner, total=False):
    x_reference: str


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


def test_seed_records_preserves_remote_changes_deletions_hooks_and_id_allocation() -> None:
    server = UtilityFakeOdooTransport(
        records={
            "item": [
                {"id": 7, "name": "Original"},
                {"id": 8, "name": "Deleted"},
            ]
        }
    )
    server["item"][7]["name"] = "Remote"
    del server["item"][8]
    server.fail_next("item", "write", RuntimeError("Still queued"))
    baseline: dict[str, list[dict[str, object]]] = {
        "item": [
            {"id": 7, "name": "Cached", "active": True},
            {"id": 8, "name": "Cached deletion"},
            {"id": 3, "name": "Cache-only"},
        ]
    }
    server.seed_records(baseline)
    server["item"][3]["name"] = "Detached"
    assert baseline["item"][2]["name"] == "Cache-only"
    assert server["item"][7] == {"id": 7, "name": "Remote", "active": True}
    assert 8 not in server["item"]
    assert rpc(server, "item", "create", [{"name": "New"}]) == 9
    with pytest.raises(RuntimeError, match="Still queued"):
        rpc(server, "item", "write", [[7], {"name": "Rejected"}])
    with pytest.raises(ValueError, match="seeded only once"):
        server.seed_records({})


def test_invalid_seed_does_not_replace_state_or_consume_seeding_opportunity() -> None:
    server = FakeOdooTransport(records={"item": [{"id": 1, "name": "Before"}]})
    with pytest.raises(ValueError, match="Duplicate fake record"):
        server.seed_records({"item": [{"id": 2}, {"id": 2}]})
    assert server["item"] == {1: {"id": 1, "name": "Before"}}
    server.seed_records({"item": [{"id": 2, "name": "Added"}]})
    assert set(server["item"]) == {1, 2}


def test_explicit_schema_names_relations_and_label_fields_work_without_field_metadata() -> None:
    server = FakeOdooTransport(
        records={"item": [], "owner": [{"id": 7, "title": "Seven"}]},
        known_fields={"item": ["name", "owner_id", "dynamic_id"]},
        relations={"item": {"owner_id": "owner"}},
        label_fields=("display_name", "title"),
    )
    assert rpc(server, "item", "search_read", [[]], fields=["dynamic_id"]) == []
    assert rpc(server, "item", "create", [{"name": "New", "owner_id": 7}]) == 1
    assert server["item"][1]["owner_id"] == [7, "Seven"]
    rpc(server, "item", "unlink", [[1]])
    assert rpc(server, "item", "create", [{"name": "Next"}]) == 2
    server.known_fields["item"].add("later_id")
    assert rpc(server, "item", "fields_get", [["later_id"]]) == {
        "later_id": {"type": "fixture"},
    }


def test_filtered_child_projections_and_hidden_cascades_are_independent() -> None:
    server = FakeOdooTransport(
        records={
            "parent": [{"id": 1, "name": "Parent"}],
            "child": [
                {"id": 2, "parent_id": [1, "Parent"], "visible": True},
                {"id": 3, "parent_id": [1, "Parent"], "visible": False},
            ],
            "detail": [{"id": 4, "child_id": [3, "Hidden"]}],
        },
        children={
            "parent": {
                "child_ids": ("child", "parent_id"),
                "visible_ids": ("child", "parent_id"),
            }
        },
        child_filters={"parent": {"visible_ids": lambda row: row["visible"] is True}},
        cascades={"child": [("detail", "child_id")]},
    )
    assert rpc(server, "parent", "read", [[1]]) == [
        {"id": 1, "name": "Parent", "child_ids": [2, 3], "visible_ids": [2]},
    ]
    assert rpc(server, "child", "read", [[3]]) == [
        {"id": 3, "parent_id": [1, "Parent"], "visible": False},
    ]
    rpc(server, "parent", "unlink", [[1]])
    assert server["parent"] == server["child"] == server["detail"] == {}
    with pytest.raises(ValueError, match="configured child fields"):
        FakeOdooTransport(child_filters={"parent": {"absent": lambda _: True}})


def test_reusable_schema_supports_per_test_overrides_without_sharing_state() -> None:
    schema = FakeSchema(
        fields={"item": {"name": {"type": "char"}, "tag_ids": {"type": "many2many"}}},
        defaults={"item": {"name": "Default", "tag_ids": []}},
        known_fields={"item": {"extra"}},
    )
    first = FakeOdooTransport(schema=schema, defaults={"item": {"name": "First"}})
    second = FakeOdooTransport(schema=schema)
    assert rpc(first, "item", "create", [{}]) == 1
    rpc(first, "item", "write", [[1], {"tag_ids": [[4, 7, 0]]}])
    first.fields["item"]["name"]["required"] = True
    first.known_fields["item"].add("only_first")
    assert rpc(second, "item", "create", [{}]) == 1
    assert second["item"][1] == {"id": 1, "name": "Default", "tag_ids": []}
    assert "required" not in second.fields["item"]["name"]
    assert "only_first" not in second.known_fields["item"]
    assert schema.defaults["item"]["tag_ids"] == []
    assert schema.known_fields["item"] == {"extra"}


def test_schema_composition_preserves_relations_children_and_hidden_cascades() -> None:
    base = FakeSchema(
        known_fields={"parent": {"name"}, "child": {"name"}},
        children={"parent": {"child_ids": ("child", "parent_id")}},
        cascades={"parent": [("child", "parent_id")]},
        label_fields=("title", "name"),
    )
    extended = base.extend(
        FakeSchema(
            known_fields={"parent": {"title"}},
            fields={"child": {"other_id": {"type": "many2one", "relation": "other"}}},
            cascades={"parent": [("child", "parent_id"), ("other", "parent_id")]},
        )
    )
    server = FakeOdooTransport(
        schema=extended,
        records={
            "parent": [{"id": 1, "title": "Parent"}],
            "child": [],
            "other": [
                {"id": 3, "name": "Other", "parent_id": [1, "Parent"]},
            ],
        },
    )
    rpc(
        server,
        "parent",
        "write",
        [
            [1],
            {
                "child_ids": [
                    [
                        0,
                        0,
                        {
                            "name": "Child",
                            "other_id": 3,
                        },
                    ]
                ]
            },
        ],
    )
    assert server["child"][1]["parent_id"] == [1, "Parent"]
    assert server["child"][1]["other_id"] == [3, "Other"]
    rpc(server, "parent", "unlink", [[1]])
    assert server["parent"] == server["child"] == server["other"] == {}
    assert base.known_fields["parent"] == {"name"}


def test_extended_native_models_define_normal_empty_table_writes_and_queries() -> None:
    schema = FakeSchema.from_models({"res.partner": ExtendedPartner}, odoo_version=ODOO_VERSION)
    server = FakeOdooTransport(schema=schema)
    assert RawPartner is VersionedPartner
    assert server.odoo_version == "19.4"
    assert (
        rpc(
            server,
            "res.partner",
            "create",
            [
                {
                    "name": "Example",
                    "x_reference": "reference",
                }
            ],
        )
        == 1
    )
    assert rpc(server, "res.partner", "search_read", [[]], fields=["name", "x_reference"]) == [
        {"id": 1, "name": "Example", "x_reference": "reference"},
    ]
    with pytest.raises(AssertionError, match="Unknown or immutable"):
        rpc(server, "res.partner", "write", [[1], {"typo": "Rejected"}])
    rpc(server, "res.partner", "unlink", [[1]])
    assert rpc(server, "res.partner", "create", [{"x_reference": "next"}]) == 2
    with pytest.raises(TypeError, match="TypedDict"):
        FakeSchema.from_models({"res.partner": dict})


def test_schema_composition_rejects_mixed_versions_and_inherits_unversioned_overrides() -> None:
    schema = FakeSchema.from_models({"res.partner": ExtendedPartner}, odoo_version="19.4")
    combined = schema.extend(FakeSchema(defaults={"res.partner": {"name": "Default"}}))
    server = FakeOdooTransport(schema=combined)
    assert server.odoo_version == "19.4"
    assert rpc(server, "res.partner", "create", [{}]) == 1
    assert server["res.partner"][1]["name"] == "Default"
    with pytest.raises(ValueError, match=r"19\.4 and 18\.0"):
        schema.extend(FakeSchema(odoo_version="18.0"))
