"""Preview, create, and update native Odoo financial reports from JSON."""

import argparse
import ast
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import NotRequired, TypedDict, TypeGuard
from urllib.parse import urlsplit

from odoobase import ConnectionProvider, RpcRecord, get_odoo_models, record_list
from odoobase.relations import relation_id_optional
from odoobase.rpc import RpcClient, load_spec, require

logger = logging.getLogger(__name__)
LINE_CODE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REFERENCE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
LIMIT = 1000


class ReportArgs(argparse.Namespace):
    target_url: str
    target_db: str
    key_file: Path | None
    spec: Path
    report_id: int | None
    apply: bool
    confirm_db: str | None
    journal: Path | None
    allow_delete: bool


class ReportSpec(TypedDict):
    report: RpcRecord
    columns: list[RpcRecord]
    lines: list[RpcRecord]
    menu: NotRequired[RpcRecord]


class ReportBefore(TypedDict):
    report: RpcRecord | None
    lines: list[RpcRecord]
    expressions: list[RpcRecord]
    columns: list[RpcRecord]
    action: RpcRecord | None
    menu: RpcRecord | None


class PublicationPlan(TypedDict):
    action: RpcRecord
    menu: RpcRecord


class ReportPlan(TypedDict):
    report_id: int | None
    values: RpcRecord
    removals: list[RpcRecord]
    before: ReportBefore
    parent_menu: RpcRecord | None
    publication: PublicationPlan | None


class CreatedRecord(TypedDict):
    model: str
    id: int


class ReportJournal(TypedDict):
    target_url: str
    target_db: str
    spec: ReportSpec
    plan: ReportPlan
    status: str
    created: list[CreatedRecord]
    inflight_create: NotRequired[str]
    report_id: NotRequired[int]
    action_id: NotRequired[int]
    menu_id: NotRequired[int]
    rendered_lines: NotRequired[int]
    no_report_change: NotRequired[bool]


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def object_record(value: object, message: str) -> RpcRecord:
    if not is_object_mapping(value):
        raise ValueError(message)
    result: RpcRecord = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(message)
        result[key] = item
    return result


def object_records(value: object, message: str) -> list[RpcRecord]:
    if not is_object_list(value):
        raise ValueError(message)
    return [object_record(item, message) for item in value]


def identifier(value: object) -> str:
    if not isinstance(value, str) or not LINE_CODE.fullmatch(value):
        raise ValueError("Expected an identifier")
    return value


def integer_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Expected a positive record ID")
    return value


def record_ids(value: object) -> list[int]:
    if not is_object_list(value):
        raise ValueError("Expected record IDs")
    return [integer_id(item) for item in value]


def text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def action_context(action: RpcRecord | None) -> RpcRecord:
    if not action:
        return {}
    return object_record(ast.literal_eval(text(action["context"])), "Invalid action context")


def parse_args(argv: Sequence[str] | None = None) -> ReportArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report-id", type=int, help="Explicit identity, including when renaming")
    parser.add_argument("--apply", action="store_true", help="Otherwise preview only")
    parser.add_argument("--confirm-db")
    parser.add_argument("--journal", type=Path, help="New private recovery journal")
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="Allow removal of lines, columns, or expressions omitted from JSON",
    )
    args = parser.parse_args(argv, namespace=ReportArgs())
    target = urlsplit(args.target_url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
        or target.path not in ("", "/")
    ):
        parser.error("--target-url must be an HTTPS origin without credentials")
    if not args.target_db.strip():
        parser.error("--target-db must not be empty")
    if args.report_id is not None and args.report_id <= 0:
        parser.error("--report-id must be positive")
    if args.apply and (args.confirm_db != args.target_db or not args.journal):
        parser.error("--apply requires matching --confirm-db and a new --journal")
    return args


def flatten(lines: object) -> list[RpcRecord]:
    if not is_object_list(lines):
        raise ValueError("lines and children must be arrays")
    result: list[RpcRecord] = []
    for item in lines:
        line = object_record(item, "Each report line must be an object")
        result.append(line)
        result.extend(flatten(line.get("children", [])))
    require(len(result) <= LIMIT, "Report exceeds 1000 lines")
    return result


def validate_spec(value: object) -> ReportSpec:
    spec = object_record(value, "Spec must be an object")
    require(set(spec) <= {"report", "columns", "lines", "menu"}, "Unknown top-level spec key")
    report = object_record(spec.get("report"), "report must be an object")
    name = report.get("name")
    require(isinstance(name, str) and bool(name.strip()), "report.name is required")
    require(
        not {"id", "line_ids", "column_ids", "custom_handler_model_id"} & report.keys(),
        "Use lines/columns; Python handlers are not supported",
    )
    columns = object_records(spec.get("columns"), "columns must be a nonempty array of objects")
    require(bool(columns), "columns must be a nonempty array")
    require(
        all(not {"id", "report_id"} & column.keys() for column in columns),
        "Column IDs and ownership are managed",
    )
    raw_labels = [column.get("expression_label") for column in columns]
    require(
        all(isinstance(label, str) and LINE_CODE.fullmatch(label) for label in raw_labels),
        "Each column needs an expression_label identifier",
    )
    labels = [identifier(label) for label in raw_labels]
    require(len(labels) == len(set(labels)), "Column expression labels must be unique")
    nodes = flatten(spec.get("lines"))
    require(bool(nodes), "lines must not be empty")
    raw_codes = [node.get("code") for node in nodes]
    require(
        all(isinstance(code, str) and LINE_CODE.fullmatch(code) for code in raw_codes),
        "Each line needs a code identifier",
    )
    codes = [identifier(code) for code in raw_codes]
    require(len(codes) == len(set(codes)), "Line codes must be unique")
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for node in nodes:
        require(
            not {"id", "report_id", "parent_id", "children_ids", "expression_ids"} & node.keys(),
            "Use children/expressions instead of raw relation fields",
        )
        name = node.get("name")
        require(isinstance(name, str) and bool(name.strip()), "Each line needs a name")
        expressions = object_records(
            node.get("expressions", []), "expressions must be an array of objects"
        )
        names = [expr.get("label") for expr in expressions]
        require(
            len(names) == len(expressions)
            and all(isinstance(name, str) and LINE_CODE.fullmatch(name) for name in names),
            "Each expression needs a label identifier",
        )
        require(
            len(names) == len({identifier(name) for name in names}),
            "Expression labels must be unique within a line",
        )
        for expr in expressions:
            require(not {"id", "report_line_id"} & expr.keys(), "Expression IDs are managed")
            require(
                expr.get("engine") in {"account_codes", "domain", "aggregation"},
                "Supported engines: account_codes, domain, aggregation",
            )
            formula = expr.get("formula")
            require(
                isinstance(formula, str) and bool(formula.strip()),
                "Each expression needs a formula",
            )
            formula = text(formula)
            dependencies: set[tuple[str, str]] = set()
            if expr["engine"] == "aggregation":
                dependencies = set(REFERENCE.findall(formula))
            elif expr["engine"] == "domain":
                require(
                    isinstance(ast.literal_eval(formula), list),
                    "Domain formula must be a literal list",
                )
            graph[(identifier(node["code"]), identifier(expr["label"]))] = dependencies
    require(
        all(dependency in graph for refs in graph.values() for dependency in refs),
        "Aggregation references an undefined line/expression",
    )
    require(
        set(labels) <= {label for _, label in graph},
        "Column references an undefined expression label",
    )
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("Cyclic report expression dependency") from exc
    if "menu" in spec:
        menu = object_record(spec["menu"], "Invalid menu settings")
        require(
            set(menu) <= {"name", "parent_id", "sequence", "group_ids"}, "Invalid menu settings"
        )
        parent_id = menu.get("parent_id")
        require(type(parent_id) is int and parent_id > 0, "menu.parent_id must be positive")
        menu_name = menu.get("name")
        require(
            "name" not in menu or (isinstance(menu_name, str) and bool(menu_name.strip())),
            "menu.name must be nonempty",
        )
        require(
            "sequence" not in menu or type(menu["sequence"]) is int,
            "menu.sequence must be an integer",
        )
        if "group_ids" in menu:
            groups = menu["group_ids"]
            require(
                is_object_list(groups)
                and all(type(value) is int and value > 0 for value in groups),
                "menu.group_ids must contain unique positive IDs",
            )
            ids = record_ids(groups)
            require(len(ids) == len(set(ids)), "menu.group_ids must contain unique positive IDs")
    result: ReportSpec = {
        "report": report,
        "columns": columns,
        "lines": object_records(spec["lines"], "lines must be an array"),
    }
    if "menu" in spec:
        result["menu"] = object_record(spec["menu"], "Invalid menu settings")
    return result


def native_values(
    client: RpcClient,
    model: str,
    values: RpcRecord,
    fields_cache: dict[str, dict[str, RpcRecord]],
) -> RpcRecord:
    require("id" not in values, f"{model} IDs are managed")
    if model not in fields_cache:
        metadata = object_record(
            client.call(
                model,
                "fields_get",
                [],
                attributes=["type", "readonly", "selection", "relation"],
            ),
            f"Invalid {model} field metadata",
        )
        fields_cache[model] = {
            name: object_record(field, f"Invalid {model}.{name} metadata")
            for name, field in metadata.items()
        }
    fields = fields_cache[model]
    for name, value in values.items():
        require(
            name in fields and not fields[name].get("readonly"),
            f"Unknown or readonly setting: {model}.{name}",
        )
        field = fields[name]
        kind = field["type"]
        require(value is not None, f"Use false rather than null: {model}.{name}")
        if kind == "boolean":
            require(type(value) is bool, f"{model}.{name} requires a boolean")
        elif kind in {"float", "monetary"}:
            require(
                isinstance(value, (int, float))
                and type(value) in (int, float)
                and abs(value) <= sys.float_info.max
                and math.isfinite(value),
                f"{model}.{name} requires a finite number",
            )
        elif kind in {"integer", "many2one"}:
            require(
                type(value) is int or (kind == "many2one" and value is False),
                f"{model}.{name} requires an integer ID/value",
            )
            if kind == "many2one" and value:
                require(
                    integer_id(value) > 0
                    and client.call(
                        text(field["relation"]),
                        "search_count",
                        [[["id", "=", value]]],
                    )
                    == 1,
                    f"Related record missing: {model}.{name}={value}",
                )
        elif kind in {"char", "text", "selection"}:
            require(
                isinstance(value, str) or value is False,
                f"{model}.{name} requires a string or false",
            )
            if kind == "selection" and value is not False:
                selection = field["selection"]
                if not is_object_list(selection):
                    raise ValueError(f"Invalid selection metadata: {model}.{name}")
                choices: list[object] = []
                for pair in selection:
                    if not is_object_list(pair) or len(pair) != 2:
                        raise ValueError(f"Invalid selection metadata: {model}.{name}")
                    choices.append(pair[0])
                require(value in choices, f"Invalid selection: {model}.{name}")
        else:
            raise ValueError(f"Unsupported setting type: {model}.{name} ({kind})")
    return values


def read_records(
    client: RpcClient,
    model: str,
    ids: Sequence[int],
    fields: Iterable[str],
) -> list[RpcRecord]:
    if not ids:
        return []
    records = record_list(client.call(model, "read", [ids, sorted(set(fields) | {"id"})]))
    require(
        {integer_id(record["id"]) for record in records} == set(ids), f"Incomplete {model} read"
    )
    return records


def scalar_changes(before: RpcRecord, requested: RpcRecord) -> RpcRecord:
    result: RpcRecord = {}
    for key, value in requested.items():
        old = before.get(key)
        if is_object_list(old) and type(value) is int:
            old = relation_id_optional(old)
        if old != value:
            result[key] = value
    return result


def keyed(records: Sequence[RpcRecord], key: str) -> dict[str, RpcRecord]:
    require(all(record.get(key) for record in records), f"Existing records need {key}")
    result = {text(record[key]): record for record in records}
    require(len(result) == len(records), f"Ambiguous existing {key}; refusing update")
    return result


def commands(
    requested: Sequence[RpcRecord],
    existing: Sequence[RpcRecord],
    key: str,
    removals: list[RpcRecord],
) -> list[list[object]]:
    old = keyed(existing, key)
    result: list[list[object]] = []
    for values in requested:
        before = old.pop(text(values[key]), None)
        if before is None:
            result.append([0, 0, values])
        else:
            changes = scalar_changes(before, values)
            if changes:
                result.append([1, before["id"], changes])
    for before in old.values():
        removals.append({"id": before["id"], "key": before[key]})
        result.append([2, before["id"], 0])
    return result


def build_plan(
    client: RpcClient,
    value: object,
    report_id: int | None = None,
) -> ReportPlan:
    spec = validate_spec(value)
    nodes = flatten(spec["lines"])
    fields_cache: dict[str, dict[str, RpcRecord]] = {}
    report_settings = native_values(client, "account.report", spec["report"], fields_cache)
    column_settings = [
        native_values(client, "account.report.column", column, fields_cache)
        for column in spec["columns"]
    ]
    for node in nodes:
        native_values(
            client,
            "account.report.line",
            {k: v for k, v in node.items() if k not in ("children", "expressions")},
            fields_cache,
        )
        for expression in object_records(node.get("expressions", []), "Invalid expressions"):
            native_values(client, "account.report.expression", expression, fields_cache)
    report_fields = set(report_settings) | {"line_ids", "column_ids", "write_date"}
    if report_id is None:
        before = client.one(
            "account.report",
            [["name", "=", report_settings["name"]]],
            sorted(report_fields | {"id"}),
        )
    else:
        before = read_records(client, "account.report", [report_id], report_fields)[0]
    report_id = integer_id(before["id"]) if before else None
    line_fields = {
        key for node in nodes for key in node if key not in ("children", "expressions")
    } | {"code", "parent_id", "expression_ids", "write_date"}
    lines = read_records(
        client, "account.report.line", record_ids(before["line_ids"]) if before else [], line_fields
    )
    expressions = read_records(
        client,
        "account.report.expression",
        [expression_id for line in lines for expression_id in record_ids(line["expression_ids"])],
        {
            key
            for node in nodes
            for expr in object_records(node.get("expressions", []), "Invalid expressions")
            for key in expr
        }
        | {"label", "write_date"},
    )
    expressions_by_id = {integer_id(expr["id"]): expr for expr in expressions}
    columns = read_records(
        client,
        "account.report.column",
        record_ids(before["column_ids"]) if before else [],
        {key for column in column_settings for key in column} | {"expression_label", "write_date"},
    )
    old_lines = keyed(lines, "code")
    removals: list[RpcRecord] = []

    def line_commands(node: RpcRecord, parent_code: str | None = None) -> list[list[object]]:
        old = old_lines.get(text(node["code"]))
        values = {k: v for k, v in node.items() if k not in ("children", "expressions")}
        parent = old_lines.get(parent_code) if parent_code is not None else None
        if parent_code is None:
            values["parent_id"] = False
        elif parent:
            values["parent_id"] = parent["id"]
        payload = scalar_changes(old, values) if old else values
        expr_commands = commands(
            object_records(node.get("expressions", []), "Invalid expressions"),
            [expressions_by_id[expr_id] for expr_id in record_ids(old["expression_ids"])]
            if old
            else [],
            "label",
            removals,
        )
        if expr_commands:
            payload["expression_ids"] = expr_commands
        children = [
            command
            for child in object_records(node.get("children", []), "Invalid children")
            for command in line_commands(child, text(node["code"]))
        ]
        if children:
            payload["children_ids"] = children
        result: list[list[object]] = (
            [[1, old["id"], payload]] if old and payload else ([[0, 0, payload]] if not old else [])
        )
        if old and parent_code is not None and parent is None:
            result.append([4, old["id"], 0])
        return result

    line_updates = [command for node in spec["lines"] for command in line_commands(node)]
    desired_codes = {text(node["code"]) for node in nodes}
    obsolete = {
        integer_id(line["id"]): line for line in lines if text(line["code"]) not in desired_codes
    }
    for line_id, line in obsolete.items():
        removals.append({"model": "account.report.line", "id": line_id, "key": line["code"]})
        if relation_id_optional(line["parent_id"]) not in obsolete:
            line_updates.append([2, line_id, 0])
    values = scalar_changes(before, report_settings) if before else dict(report_settings)
    column_updates = commands(column_settings, columns, "expression_label", removals)
    if column_updates:
        values["column_ids"] = column_updates
    if line_updates:
        values["line_ids"] = line_updates
    action = menu = parent_menu = None
    if "menu" in spec:
        parent_menu = read_records(
            client, "ir.ui.menu", [integer_id(spec["menu"]["parent_id"])], ["name", "group_ids"]
        )[0]
        if "group_ids" in spec["menu"]:
            read_records(client, "res.groups", record_ids(spec["menu"]["group_ids"]), [])
        if report_id:
            candidates = client.search(
                "ir.actions.client",
                [["tag", "=", "account_report"], ["context", "ilike", str(report_id)]],
                ["id", "name", "tag", "context", "write_date"],
                limit=LIMIT + 1,
            )
            require(len(candidates) <= LIMIT, "Too many candidate report actions")
            actions = [
                item for item in candidates if action_context(item).get("report_id") == report_id
            ]
            require(len(actions) <= 1, "Multiple actions reference this report")
            action = actions[0] if actions else None
            if action:
                menu = client.one(
                    "ir.ui.menu",
                    [["action", "=", f"ir.actions.client,{action['id']}"]],
                    ["id", "name", "parent_id", "sequence", "group_ids", "action", "write_date"],
                )
        if menu is None:
            collision = client.one(
                "ir.ui.menu",
                [
                    ["parent_id", "=", parent_menu["id"]],
                    ["name", "=", spec["menu"].get("name", report_settings["name"])],
                ],
                ["id", "name"],
            )
            require(collision is None, "A different menu already uses the requested name")
    publication = publication_plan(spec, report_id, action, menu, parent_menu)
    return {
        "report_id": report_id,
        "values": values,
        "removals": removals,
        "before": {
            "report": before,
            "lines": lines,
            "expressions": expressions,
            "columns": columns,
            "action": action,
            "menu": menu,
        },
        "parent_menu": parent_menu,
        "publication": publication,
    }


def publication_plan(
    spec: ReportSpec,
    report_id: int | None,
    action: RpcRecord | None,
    menu: RpcRecord | None,
    parent: RpcRecord | None,
) -> PublicationPlan | None:
    if "menu" not in spec:
        return None
    context = action_context(action)
    action_values: RpcRecord = {
        "name": spec["report"]["name"],
        "tag": "account_report",
        "context": repr({**context, "report_id": report_id or "<new report>"}),
    }
    menu_values: RpcRecord = {
        "name": spec["menu"].get("name", spec["report"]["name"]),
        "parent_id": spec["menu"]["parent_id"],
        "action": f"ir.actions.client,{action['id']}" if action else "<new action>",
        "sequence": spec["menu"].get("sequence", 100),
    }
    if not menu:
        if parent is None:
            raise ValueError("Missing parent menu")
        menu_values["group_ids"] = [[6, 0, spec["menu"].get("group_ids", parent["group_ids"])]]
    elif "group_ids" in spec["menu"] and menu["group_ids"] != spec["menu"]["group_ids"]:
        menu_values["group_ids"] = [[6, 0, spec["menu"]["group_ids"]]]
    return {
        "action": scalar_changes(action, action_values) if action else action_values,
        "menu": scalar_changes(menu, menu_values) if menu else menu_values,
    }


def create_record(
    client: RpcClient,
    model: str,
    values: RpcRecord,
    journal: ReportJournal,
    save: Callable[[], None],
) -> int:
    journal["inflight_create"] = model
    save()
    record_id = client.call(model, "create", [values])
    if type(record_id) is not int or record_id <= 0:
        raise ValueError(f"Invalid {model} create response")
    journal["created"].append({"model": model, "id": record_id})
    del journal["inflight_create"]
    save()
    return record_id


def publish_menu(
    client: RpcClient,
    spec: ReportSpec,
    plan: ReportPlan,
    report_id: int,
    journal: ReportJournal,
    save: Callable[[], None],
) -> None:
    if "menu" not in spec:
        return
    before = plan["before"]
    action = before["action"]
    action_values: RpcRecord = {"name": spec["report"]["name"], "tag": "account_report"}
    context = action_context(action)
    action_values["context"] = repr({**context, "report_id": report_id})
    if action:
        changes = scalar_changes(action, action_values)
        if changes:
            require(
                client.call("ir.actions.client", "write", [[action["id"]], changes]) is True,
                "Action write was not confirmed",
            )
        action_id = integer_id(action["id"])
    else:
        action_id = create_record(client, "ir.actions.client", action_values, journal, save)
    menu = before["menu"]
    requested: RpcRecord = {
        "name": spec["menu"].get("name", spec["report"]["name"]),
        "parent_id": spec["menu"]["parent_id"],
        "action": f"ir.actions.client,{action_id}",
        "sequence": spec["menu"].get("sequence", 100),
    }
    if not menu:
        parent_menu = plan["parent_menu"]
        if parent_menu is None:
            raise ValueError("Missing parent menu")
        requested["group_ids"] = [[6, 0, spec["menu"].get("group_ids", parent_menu["group_ids"])]]
    elif "group_ids" in spec["menu"] and menu["group_ids"] != spec["menu"]["group_ids"]:
        requested["group_ids"] = [[6, 0, spec["menu"]["group_ids"]]]
    if menu:
        changes = scalar_changes(menu, requested)
        if changes:
            require(
                client.call("ir.ui.menu", "write", [[menu["id"]], changes]) is True,
                "Menu write was not confirmed",
            )
        menu_id = integer_id(menu["id"])
    else:
        menu_id = create_record(client, "ir.ui.menu", requested, journal, save)
    journal.update(action_id=action_id, menu_id=menu_id)


def apply_plan(
    client: RpcClient,
    args: ReportArgs,
    value: object,
    plan: ReportPlan,
) -> ReportJournal:
    spec = validate_spec(value)
    require(
        not plan["removals"] or args.allow_delete,
        "Removing existing configuration requires --allow-delete; review the preview",
    )
    journal: ReportJournal = {
        "target_url": args.target_url,
        "target_db": args.target_db,
        "spec": spec,
        "plan": plan,
        "status": "prepared",
        "created": [],
    }
    journal_path = args.journal
    if journal_path is None:
        raise ValueError("Applying requires a recovery journal")
    with open(
        journal_path, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as stream:
        json.dump(journal, stream, indent=2)

    def save() -> None:
        journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    try:
        require(
            build_plan(client, spec, args.report_id) == plan,
            "Report changed since preview; rerun with a new journal",
        )
        journal["status"] = "write_started"
        save()
        report_id = plan["report_id"]
        if report_id is None:
            report_id = create_record(client, "account.report", plan["values"], journal, save)
        elif plan["values"]:
            require(
                client.call("account.report", "write", [[report_id], plan["values"]]) is True,
                "Report write was not confirmed",
            )
        journal["report_id"] = report_id
        save()
        publish_menu(client, spec, plan, report_id, journal, save)
        remaining = build_plan(client, spec, report_id)
        require(
            not remaining["values"] and not remaining["removals"],
            "Report readback differs from JSON; inspect recovery journal",
        )
        publication = remaining["publication"]
        require(
            publication is None or not any(publication.values()),
            "Menu/action readback differs from JSON; inspect recovery journal",
        )
        options = object_record(
            client.call(
                "account.report",
                "get_options",
                [[report_id], {"selected_variant_id": report_id}],
            ),
            "Invalid native report options",
        )
        require(options["report_id"] == report_id, "Native report routed to a different report")
        information = object_record(
            client.call(
                "account.report",
                "get_report_information",
                [[report_id], options],
            ),
            "Invalid native report information",
        )
        rendered_lines = information["lines"]
        if not is_object_list(rendered_lines):
            raise ValueError("Invalid native report lines")
        journal.update(
            status="verified",
            rendered_lines=len(rendered_lines),
            no_report_change=plan["report_id"] is not None and not plan["values"],
        )
        save()
    except Exception:
        journal["status"] = "requires_review"
        save()
        if plan["report_id"] is None and not journal.get("inflight_create"):
            for record in reversed(journal["created"]):
                try:
                    require(
                        client.call(record["model"], "unlink", [[record["id"]]]) is True,
                        "Rollback was not confirmed",
                    )
                except Exception:
                    logger.exception("Could not remove newly created configuration: %s", record)
                    journal["status"] = "rollback_failed"
                    save()
            if journal["status"] != "rollback_failed":
                journal["status"] = "rolled_back"
                save()
        logger.exception(
            "odoo_report_operation_failed journal=%s; inspect before retrying", args.journal
        )
        raise
    return journal


def main(
    argv: Sequence[str] | None = None, *, connection_provider: ConnectionProvider | None = None
) -> None:
    args = parse_args(argv)
    spec = validate_spec(load_spec(args.spec))
    connect = get_odoo_models if connection_provider is None else connection_provider
    models, db, uid, password = connect(
        key_file=args.key_file,
        target_url=args.target_url,
        target_db=args.target_db,
        rpc_protocol="jsonrpc",
    )
    require(db == args.target_db, "Connected database differs from explicit target")
    client = RpcClient(models, db, uid, password)
    plan = build_plan(client, spec, args.report_id)
    output = (
        apply_plan(client, args, spec, plan)
        if args.apply
        else {
            "applied": False,
            "target_url": args.target_url,
            "target_db": db,
            **plan,
        }
    )
    json.dump(output, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
