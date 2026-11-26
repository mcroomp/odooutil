"""Manage Odoo schema and record values with preview-first, journaled writes."""

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard
from urllib.parse import urlsplit
from xml.etree import ElementTree

from odoobase import ConnectionProvider, RpcRecord, get_odoo_models, record_dict, record_list
from odoobase.relations import relation_id_optional
from odoobase.rpc import CONTEXT, MODEL_NAME_PATTERN, RpcClient, load_spec, require


class SchemaArgs(argparse.Namespace):
    key_file: Path | None = None
    target_url: str = ""
    target_db: str = ""
    command: str = ""
    model: str = ""
    name: str = ""
    spec: Path | None = None
    apply: bool = False
    confirm_db: str | None = None
    journal: Path | None = None
    allow_data_loss: bool = False
    record_id: int = 0
    record_ids: list[int]
    chatter: str = "quiet"
    lang: str | None = None
    allow_automation: bool = False
    file: Path | None = None

    def __init__(self) -> None:
        super().__init__()
        self.record_ids = []


class ReadPlan(TypedDict):
    action: Literal["read"]
    kind: str
    before: RpcRecord


class SchemaPlan(TypedDict):
    action: Literal["create", "update", "delete"]
    kind: Literal["model", "field", "view", "action"]
    before: RpcRecord | None
    rpc_model: str
    values: RpcRecord
    requested: RpcRecord | None
    side_effect_warning: NotRequired[str]


class RecordPlan(TypedDict):
    action: Literal["create", "update"]
    kind: Literal["record", "records"]
    before: RpcRecord | list[RpcRecord] | None
    rpc_model: str
    values: RpcRecord
    requested: RpcRecord
    columns: list[str]
    fields: dict[str, RpcRecord]
    context: RpcRecord
    audit_chatter: bool
    automation_acknowledged: bool
    side_effect_warning: str
    primary_attachment: NotRequired[RpcRecord]


class AttachmentSource(TypedDict):
    name: str
    file_size: int
    checksum: str
    sha256: str


class AttachmentPlan(TypedDict):
    action: Literal["create", "reuse"]
    kind: Literal["attachment"]
    before: RpcRecord | None
    rpc_model: str
    values: RpcRecord
    requested: None
    source: AttachmentSource
    target: RpcRecord
    context: RpcRecord
    audit_chatter: bool
    automation_acknowledged: bool
    side_effect_warning: str


Plan = ReadPlan | SchemaPlan | RecordPlan | AttachmentPlan


def object_dict(value: object, message: str = "Expected an object") -> RpcRecord:
    try:
        return record_dict(value)
    except ValueError as exc:
        raise ValueError(message) from exc


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def object_list(value: object, message: str = "Expected a list") -> list[object]:
    if not is_object_list(value):
        raise ValueError(message)
    return value


def text(value: object, message: str = "Expected a string") -> str:
    if not isinstance(value, str):
        raise ValueError(message)
    return value


def integer(value: object, message: str = "Expected an integer") -> int:
    if type(value) is not int:
        raise ValueError(message)
    return value


def positive_ids(value: object, message: str, *, nonempty: bool = False) -> list[int]:
    items = object_list(value, message)
    require(
        (bool(items) or not nonempty) and all(type(item) is int and item > 0 for item in items),
        message,
    )
    return [integer(item, message) for item in items]


def connection_values(db: object, uid: object, password: object) -> tuple[str, int, str]:
    if not isinstance(db, str) or not isinstance(uid, int) or not isinstance(password, str):
        raise ValueError("Odoo connection returned invalid database/user/credential types")
    return db, uid, password


FIELD_TYPES = {
    "char",
    "text",
    "html",
    "boolean",
    "integer",
    "float",
    "monetary",
    "date",
    "datetime",
    "binary",
    "selection",
    "many2one",
    "many2many",
    "one2many",
}


FIELD_KEYS = {
    "field_description",
    "ttype",
    "help",
    "required",
    "readonly",
    "copied",
    "relation",
    "relation_field",
    "currency_field",
    "on_delete",
    "groups",
    "selection",
    "related",
    "store",
    "relation_table",
    "column1",
    "column2",
    "compute",
    "depends",
}


FIELD_COLUMNS = [
    "id",
    "name",
    "model",
    "state",
    "field_description",
    "ttype",
    "help",
    "required",
    "readonly",
    "copied",
    "relation",
    "relation_field",
    "currency_field",
    "on_delete",
    "groups",
    "related",
    "store",
    "compute",
    "depends",
]


MODEL_COLUMNS = ["id", "name", "model", "state", "access_ids"]

CHATTER_MODEL_COLUMNS = [*MODEL_COLUMNS, "is_mail_thread", "is_mail_activity"]

CHATTER_RELATIONS = {
    "message_ids": ("one2many", "mail.message"),
    "message_follower_ids": ("one2many", "mail.followers"),
    "message_partner_ids": ("many2many", "res.partner"),
}


ACCESS_COLUMNS = ["id", "name", "group_id", "perm_read", "perm_write", "perm_create", "perm_unlink"]


VIEW_KEYS = {"inherit_id", "arch_db", "priority", "active", "type"}


VIEW_COLUMNS = [
    "id",
    "name",
    "model",
    "inherit_id",
    "mode",
    "arch_db",
    "priority",
    "active",
    "type",
]


ACTION_COLUMNS = [
    "id",
    "name",
    "model_id",
    "binding_model_id",
    "binding_type",
    "binding_view_types",
    "state",
    "usage",
    "code",
    "group_ids",
]


class SchemaClient(RpcClient):
    def field(self, model: str, name: str) -> RpcRecord | None:
        record = self.one(
            "ir.model.fields", [["model", "=", model], ["name", "=", name]], FIELD_COLUMNS
        )
        if record and record["ttype"] == "selection":
            selections = self.search(
                "ir.model.fields.selection",
                [["field_id", "=", record["id"]]],
                ["id", "value", "name", "sequence"],
                limit=1001,
            )
            require(len(selections) <= 1000, "Selection exceeds supported 1000-option limit")
            record["selection_records"] = selections
            record["selection"] = [
                [s["value"], s["name"]]
                for s in sorted(
                    selections, key=lambda s: (integer(s["sequence"]), integer(s["id"]))
                )
            ]
        return record


def field_values(
    client: SchemaClient, spec: RpcRecord, before: RpcRecord | None, model: str
) -> RpcRecord:
    require(
        not spec.keys() - FIELD_KEYS,
        f"Unsupported field settings: {sorted(spec.keys() - FIELD_KEYS)}",
    )
    if before:
        require(
            "ttype" not in spec or spec["ttype"] == before["ttype"],
            "Field type changes are unsafe; create a new field and migrate explicitly",
        )
        require(
            "relation" not in spec or spec["relation"] == before["relation"],
            "Changing the related model requires an explicit migration",
        )
        require(
            "relation_field" not in spec or spec["relation_field"] == before.get("relation_field"),
            "Changing the inverse field requires an explicit migration",
        )
        require(
            "currency_field" not in spec or spec["currency_field"] == before.get("currency_field"),
            "Changing the currency field requires an explicit migration",
        )
        for key in ("related", "store"):
            require(
                key not in spec or spec[key] == before.get(key),
                f"Changing {key} requires an explicit migration",
            )
        for key in ("relation_table", "column1", "column2"):
            require(key not in spec, f"Changing {key} requires an explicit migration")
    else:
        require(
            "ttype" in spec and "field_description" in spec,
            "Create needs ttype and field_description",
        )
    field_type = spec.get("ttype", before["ttype"] if before else None)
    require(field_type in FIELD_TYPES, f"Unsupported field type: {field_type}")
    related = spec.get("related", before.get("related") if before else None)
    compute = spec.get("compute", before.get("compute") if before else None)
    related_target: RpcRecord | None = None
    if compute:
        require(not related, "Computed names cannot also be related fields")
        require(
            before is not None and before["name"] == "x_name" and field_type == "char",
            "Computed fields are limited to an existing manual-model x_name char field",
        )
        effective = {**(before or {}), **spec}
        require(
            not effective.get("required", False),
            "Computed names must not be required inputs; require their source fields instead",
        )
        require(
            effective.get("readonly") is True
            and effective.get("store") is True
            and effective.get("copied") is False,
            "Computed names require readonly=true, store=true, copied=false",
        )
        code = text(compute)
        require(bool(code.strip()), "Computed names require nonempty Python code")
        compile(code, "<computed-name>", "exec")
        depends = text(effective.get("depends"))
        validate_compute_dependencies(client, model, depends)
        if before and before.get("compute"):
            require(
                code == before["compute"] and depends == before.get("depends"),
                "Changing an existing computation requires an explicit migration",
            )
        else:
            require(
                client.call(model, "search_count", [[]], context={"active_test": False}) == 0,
                "Adding a computation to populated models requires an explicit migration",
            )
    elif related:
        effective = {**(before or {}), **spec}
        require(
            effective.get("readonly") is True
            and type(effective.get("store")) is bool
            and effective.get("copied") is False
            and effective.get("required") is False,
            "Related fields require readonly=true, explicit boolean store, copied=false, "
            "required=false",
        )
        related_target = validate_related_field(
            client,
            model,
            text(related),
            field_type=text(field_type),
            relation=(
                text(effective.get("relation"))
                if field_type in ("many2one", "one2many", "many2many")
                else None
            ),
        )
    else:
        require(
            not {"related", "store", "compute", "depends"} & spec.keys(),
            "related/store settings require a related field",
        )
    for key in (
        "field_description",
        "help",
        "relation",
        "relation_field",
        "currency_field",
        "on_delete",
    ):
        if key in spec:
            require(isinstance(spec[key], str), f"{key} must be a string")
    if "field_description" in spec:
        require(
            bool(text(spec["field_description"]).strip()), "field_description must not be empty"
        )
    for key in ("required", "readonly", "copied"):
        if key in spec:
            require(isinstance(spec[key], bool), f"{key} must be boolean")
    relation = spec.get("relation", before.get("relation") if before else None)
    if field_type in ("many2one", "many2many", "one2many"):
        require(
            isinstance(relation, str) and MODEL_NAME_PATTERN.fullmatch(relation) is not None,
            "Relational fields need a valid relation",
        )
        require(
            client.one("ir.model", [["model", "=", relation]], ["id"]) is not None,
            f"Related model does not exist: {relation}",
        )
        if "on_delete" in spec:
            require(field_type == "many2one", "on_delete requires many2one")
            require(spec["on_delete"] in ("set null", "restrict", "cascade"), "Invalid on_delete")
    else:
        require(
            not {"relation", "on_delete"} & spec.keys(),
            "Relation settings require a relational field",
        )
    if field_type == "one2many":
        inverse_name = spec.get("relation_field", before.get("relation_field") if before else None)
        require(
            isinstance(inverse_name, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*", inverse_name) is not None,
            "one2many needs a valid relation_field",
        )
        if related_target is not None:
            require(
                inverse_name == related_target.get("relation_field"),
                "Related one2many inverse must match the target field",
            )
        else:
            inverse = client.field(text(relation), text(inverse_name))
            require(
                inverse is not None
                and inverse["ttype"] == "many2one"
                and inverse["relation"] == model
                and inverse.get("store") is True
                and not inverse.get("related")
                and not inverse.get("compute"),
                "one2many inverse must be an existing stored, non-related, non-computed "
                "many2one pointing to the owning model",
            )
    else:
        require("relation_field" not in spec, "relation_field requires one2many")
    if field_type == "monetary":
        currency_name = spec.get("currency_field", before.get("currency_field") if before else None)
        require(
            isinstance(currency_name, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*", currency_name) is not None,
            "monetary needs an explicit valid currency_field",
        )
        currency = client.field(model, text(currency_name))
        require(
            currency is not None
            and currency["ttype"] == "many2one"
            and currency["relation"] == "res.currency",
            "currency_field must be an existing many2one to res.currency on the owning model",
        )
    else:
        require("currency_field" not in spec, "currency_field requires monetary")
    table_keys = {"relation_table", "column1", "column2"}
    stored_many2many = field_type == "many2many" and (
        not related or spec.get("store", before.get("store") if before else None) is True
    )
    if stored_many2many and before is None:
        require(
            table_keys <= spec.keys(), "many2many create needs relation_table, column1 and column2"
        )
        for key in table_keys:
            value = spec[key]
            require(
                isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is not None,
                f"{key} must be a lowercase SQL identifier of at most 63 characters",
            )
        require(text(spec["relation_table"]).startswith("x_"), "relation_table must start with x_")
        require(spec["column1"] != spec["column2"], "many2many columns must differ")
        require(
            not client.search(
                "ir.model.fields", [["relation_table", "=", spec["relation_table"]]], ["id"]
            ),
            "Relation table already belongs to another field",
        )
    elif not stored_many2many:
        require(not table_keys & spec.keys(), "Table settings require stored many2many")
    values = {key: value for key, value in spec.items() if key not in ("selection", "groups")}
    if "groups" in spec:
        groups = positive_ids(
            spec["groups"], "groups must be a list of at most 1000 positive Odoo group IDs"
        )
        require(
            len(groups) <= 1000, "groups must be a list of at most 1000 positive Odoo group IDs"
        )
        existing = client.search(
            "res.groups", [["id", "in", groups]], ["id"], limit=len(groups) + 1
        )
        require({integer(r["id"]) for r in existing} == set(groups), "Unknown access group ID")
        values["groups"] = [(6, 0, groups)]
    if field_type == "selection" and not related:
        require(
            before is not None or "selection" in spec, "Selection create needs selection choices"
        )
        if "selection" in spec:
            choices = [
                object_list(choice, "selection must contain [value, label] pairs")
                for choice in object_list(
                    spec["selection"], "selection must contain [value, label] pairs"
                )
            ]
            require(
                0 < len(choices) <= 1000
                and all(
                    len(c) == 2 and all(isinstance(v, str) and v.strip() for v in c)
                    for c in choices
                ),
                "selection must contain [value, label] pairs",
            )
            pairs = [(text(choice[0]), text(choice[1])) for choice in choices]
            require(len({c[0] for c in pairs}) == len(pairs), "Duplicate selection values")
            old = (
                {text(s["value"]): s for s in record_list(before.get("selection_records", []))}
                if before
                else {}
            )
            require(
                not old.keys() - {c[0] for c in pairs},
                "Removing selection values risks data loss; migrate explicitly first",
            )
            values["selection_ids"] = [
                (1, old[key]["id"], {"name": label, "sequence": index})
                if key in old
                else (0, 0, {"value": key, "name": label, "sequence": index})
                for index, (key, label) in enumerate(pairs)
            ]
    else:
        require("selection" not in spec, "selection choices require a non-related selection field")
    return values


def validate_compute_dependencies(client: SchemaClient, model: str, depends: str) -> None:
    paths = [path.strip() for path in depends.split(",")]
    require(
        0 < len(paths) <= 20
        and all(re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,7}", p) for p in paths),
        "depends must contain 1-20 comma-separated field paths",
    )
    for path in paths:
        current_model = model
        parts = path.split(".")
        for index, name in enumerate(parts):
            require(
                current_model != model or name != "x_name",
                "Computed names cannot depend on themselves",
            )
            fields = object_dict(
                client.call(current_model, "fields_get", [[name]], attributes=["type", "relation"])
            )
            require(name in fields, f"Unknown or inaccessible dependency: {current_model}.{name}")
            field = object_dict(fields[name])
            if index < len(parts) - 1:
                require(
                    field.get("type") == "many2one" and bool(field.get("relation")),
                    "Dependencies must traverse only Many2one fields",
                )
                current_model = text(field["relation"])


def validate_related_field(
    client: SchemaClient,
    model: str,
    path: str,
    *,
    field_type: str,
    relation: str | None,
) -> RpcRecord:
    require(
        re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,7}", path) is not None,
        "Related path must contain 1-8 field names",
    )
    parts = path.split(".")
    for index, name in enumerate(parts):
        fields = object_dict(
            client.call(
                model, "fields_get", [[name]], attributes=["type", "relation", "relation_field"]
            )
        )
        require(name in fields, f"Unknown or inaccessible related field: {model}.{name}")
        field = object_dict(fields[name])
        if index == len(parts) - 1:
            require(field["type"] == field_type, f"Related target must be {field_type}")
            if field_type in ("many2one", "one2many", "many2many"):
                require(
                    field.get("relation") == relation,
                    "Related target model must match relation",
                )
            return field
        require(
            field["type"] == "many2one" and bool(field.get("relation")),
            "Related path must traverse only Many2one fields",
        )
        model = text(field["relation"])
    raise ValueError("Related path has no target field")


def validate_xml(arch: str) -> None:
    require(bool(arch.strip()), "arch_db must contain XML")
    require(
        "<!DOCTYPE" not in arch.upper() and "<!ENTITY" not in arch.upper(),
        "View XML must not contain DTDs or entities",
    )
    try:
        ElementTree.fromstring(arch)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid view XML: {exc}") from exc


def view_values(
    client: SchemaClient, model: str, spec: RpcRecord, before: RpcRecord | None
) -> RpcRecord:
    require(
        not spec.keys() - VIEW_KEYS, f"Unsupported view settings: {sorted(spec.keys() - VIEW_KEYS)}"
    )
    if not before:
        require(
            "arch_db" in spec and ("inherit_id" in spec or "type" in spec),
            "View create needs arch_db and inherit_id or a manual-model primary type",
        )
    if "arch_db" in spec:
        validate_xml(text(spec["arch_db"]))
    parent_id = spec.get(
        "inherit_id", relation_id_optional(before["inherit_id"]) if before else None
    )
    if before:
        require(
            bool(parent_id) == bool(relation_id_optional(before["inherit_id"])),
            "Changing between primary and inherited views requires a new view",
        )
    if parent_id:
        require(type(parent_id) is int and parent_id > 0, "inherit_id must be a positive integer")
        require("type" not in spec, "Inherited views derive their type from their parent")
        if before:
            require(parent_id != before["id"], "A view cannot inherit itself")
        parent = client.one("ir.ui.view", [["id", "=", parent_id]], ["id", "model"])
        require(
            parent is not None and parent["model"] == model,
            "Parent view must exist on the same model",
        )
    else:
        require("inherit_id" not in spec, "Primary views must omit inherit_id")
        owner = client.one("ir.model", [["model", "=", model]], ["id", "state"])
        require(
            model.startswith("x_") and owner is not None and owner["state"] == "manual",
            "Primary views are supported only on manual x_ models",
        )
        kind = spec.get("type", before.get("type") if before else None)
        require(kind in ("form", "list"), "Primary view type must be form or list")
        if before:
            require(
                before["mode"] == "primary" and kind == before["type"],
                "Primary view type/mode cannot change",
            )
        if "arch_db" in spec:
            require(
                ElementTree.fromstring(text(spec["arch_db"])).tag == kind,
                "Primary view XML root must match its type",
            )
    if "active" in spec:
        require(isinstance(spec["active"], bool), "active must be boolean")
    if "priority" in spec:
        priority = spec["priority"]
        require(type(priority) is int and priority >= 0, "priority must be nonnegative")
    return spec.copy()


def model_values(client: SchemaClient, model: str, name: str, spec: RpcRecord) -> RpcRecord:
    require(
        re.fullmatch(r"x_[a-z][a-z0-9_]*", model) is not None,
        "Manual model names must start with x_ and use lowercase letters/digits/underscores",
    )
    require(not spec.keys() - {"access"}, "Model spec supports only explicit access rules")
    access = record_list(spec.get("access"))
    require(bool(access), "Model create requires explicit access rules")
    groups: set[int] = set()
    names: set[str] = set()
    for rule in access:
        require(
            set(rule) == set(ACCESS_COLUMNS) - {"id"},
            "Each access rule needs name, group_id and all four boolean permissions",
        )
        rule_name = rule["name"]
        require(
            isinstance(rule_name, str) and bool(rule_name.strip()),
            "Access rule name must not be empty",
        )
        group = integer(rule["group_id"], "Access rules require positive group IDs")
        require(type(group) is int and group > 0, "Access rules require positive group IDs")
        require(
            group not in groups and text(rule_name) not in names, "Duplicate access group or name"
        )
        require(
            client.one("res.groups", [["id", "=", group]], ["id"]) is not None,
            f"Unknown access group: {group}",
        )
        require(
            all(type(rule[key]) is bool for key in ACCESS_COLUMNS if key.startswith("perm_")),
            "Access permissions must be booleans",
        )
        require(bool(rule["perm_read"]), "Each configured group must have read access")
        groups.add(group)
        names.add(text(rule_name))
    require(
        any(rule["perm_create"] and rule["perm_write"] for rule in access),
        "Model requires a group with create and write access",
    )
    return {
        "name": name,
        "model": model,
        "state": "manual",
        "access_ids": [(0, 0, rule.copy()) for rule in access],
    }


def action_values(client: SchemaClient, model_id: int, spec: RpcRecord) -> RpcRecord:
    require(
        not spec.keys() - {"code", "binding_view_types", "group_ids"},
        "Action spec supports only code, binding_view_types and group_ids",
    )
    code = spec.get("code")
    require(isinstance(code, str) and bool(code.strip()), "Action create needs Python code")
    compile(text(code), "<server-action>", "exec")
    views = spec.get("binding_view_types", "list,form")
    require(
        isinstance(views, str)
        and bool(views)
        and all(view in {"list", "form"} for view in views.split(",")),
        "Action binding views must be list and/or form",
    )
    groups = positive_ids(
        spec.get("group_ids"), "Action create requires nonempty positive group_ids", nonempty=True
    )
    require(len(groups) == len(set(groups)), "Duplicate action access group")
    for group in groups:
        require(
            client.one("res.groups", [["id", "=", group]], ["id"]) is not None,
            f"Unknown action access group: {group}",
        )
    return {
        "model_id": model_id,
        "binding_model_id": model_id,
        "binding_type": "action",
        "binding_view_types": views,
        "state": "code",
        "usage": "ir_actions_server",
        "code": code,
        "group_ids": [(6, 0, groups)],
    }


QUIET_CONTEXT = {
    **CONTEXT,
    "mail_post_autofollow": False,
    "mail_notify_force_send": False,
}

MUTATION_WARNING = (
    "Context suppresses standard chatter only; custom hooks and automation may still run. "
    "Audits cannot prove no external effects."
)


def mutation_context(client: RpcClient, args: SchemaArgs) -> RpcRecord:
    if args.chatter == "quiet":
        automation = client.search(
            "ir.module.module",
            [["name", "=", "base_automation"], ["state", "=", "installed"]],
            ["id"],
        )
        require(
            not automation or args.allow_automation,
            "Automation is installed; review triggers and use --allow-automation to acknowledge",
        )
        return {**QUIET_CONTEXT, "lang": client.context["lang"]}
    return {"active_test": False, "lang": client.context["lang"]}


ATTACHMENT_COLUMNS = [
    "id",
    "name",
    "type",
    "res_model",
    "res_id",
    "res_field",
    "public",
    "checksum",
    "file_size",
]


def attachment_source(path: Path | None) -> tuple[bytes, AttachmentSource]:
    if path is None:
        raise ValueError("Attachment upload requires a source file")
    content = path.read_bytes()
    require(bool(content), "Attachment source must not be empty")
    return content, {
        "name": path.name,
        "file_size": len(content),
        "checksum": hashlib.sha1(content).hexdigest(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def attachment_plan(client: RpcClient, args: SchemaArgs) -> AttachmentPlan:
    require(
        MODEL_NAME_PATTERN.fullmatch(args.model) is not None
        and type(args.record_id) is int
        and args.record_id > 0,
        "Attachment target needs a model and positive existing record ID",
    )
    _, source = attachment_source(args.file)
    target_fields = object_dict(
        client.call(
            args.model,
            "fields_get",
            [["id", "write_date", "message_ids"]],
            attributes=["type"],
        )
    )
    require("id" in target_fields, "Target ID field is inaccessible")
    target = client.one(
        args.model,
        [["id", "=", args.record_id]],
        ["id", *(["write_date"] if "write_date" in target_fields else [])],
    )
    if target is None or target["id"] != args.record_id:
        raise ValueError("Attachment target does not exist or is inaccessible")
    fields = object_dict(
        client.call(
            "ir.attachment",
            "fields_get",
            [[*ATTACHMENT_COLUMNS, "raw"]],
            attributes=["type", "readonly"],
        )
    )
    require(
        set(ATTACHMENT_COLUMNS) | {"raw"} <= fields.keys(),
        "Native attachment metadata or raw upload field is inaccessible",
    )
    raw = object_dict(fields["raw"])
    require(
        raw.get("type") == "binary" and not raw.get("readonly"),
        "Native raw binary uploads are not supported by this target",
    )
    duplicates = client.search(
        "ir.attachment",
        [
            ["res_model", "=", args.model],
            ["res_id", "=", args.record_id],
            ["res_field", "=", False],
            ["checksum", "=", source["checksum"]],
        ],
        ATTACHMENT_COLUMNS,
        limit=2,
    )
    require(len(duplicates) <= 1, "Multiple checksum matches require duplicate review")
    before = duplicates[0] if duplicates else None
    require(
        before is None or before.get("public") is False,
        "Existing checksum match is public; review its permissions before proceeding",
    )
    context = {**mutation_context(client, args), "image_no_postprocess": True}
    return {
        "action": "reuse" if before is not None else "create",
        "kind": "attachment",
        "rpc_model": "ir.attachment",
        "before": before,
        "requested": None,
        "source": source,
        "target": target,
        "values": {
            "name": source["name"],
            "type": "binary",
            "res_model": args.model,
            "res_id": args.record_id,
            "public": False,
        },
        "context": context,
        "audit_chatter": args.chatter == "quiet" and "message_ids" in target_fields,
        "automation_acknowledged": args.allow_automation,
        "side_effect_warning": MUTATION_WARNING,
    }


def attachment_payload(args: SchemaArgs, plan: AttachmentPlan) -> RpcRecord:
    content, source = attachment_source(args.file)
    require(source == plan["source"], "Attachment source changed since preview")
    return {**plan["values"], "raw": base64.b64encode(content).decode("ascii")}


def verify_attachment(
    client: RpcClient,
    args: SchemaArgs,
    plan: AttachmentPlan,
    result: object,
) -> RpcRecord:
    record_id = integer(result, "Unexpected attachment response")
    after = client.one("ir.attachment", [["id", "=", record_id]], [*ATTACHMENT_COLUMNS, "raw"])
    if after is None or after["id"] != record_id:
        raise ValueError("Saved attachment was not found")
    raw = after.pop("raw")
    if not isinstance(raw, str):
        raw = object_dict(raw).get("content")
    content = base64.b64decode(text(raw, "Attachment content must be base64 text"), validate=True)
    expected_content, source = attachment_source(args.file)
    require(source == plan["source"], "Attachment source changed during upload")
    require(content == expected_content, "Saved attachment bytes mismatch")
    expected = {
        **plan["values"],
        "res_field": False,
        "checksum": source["checksum"],
        "file_size": source["file_size"],
    }
    if plan["before"] is not None:
        expected["name"] = plan["before"]["name"]
    for name, value in expected.items():
        require(after.get(name) == value, f"Saved attachment setting mismatch: {name}")
    return after


RECORD_TYPES = {
    "char",
    "text",
    "html",
    "boolean",
    "integer",
    "float",
    "monetary",
    "date",
    "datetime",
    "selection",
    "many2one",
    "json",
    "reference",
}


def reference_parts(value: object) -> tuple[str, int]:
    model, separator, identifier = text(
        value, "References must use model,positive_id or false"
    ).partition(",")
    require(
        bool(separator)
        and MODEL_NAME_PATTERN.fullmatch(model) is not None
        and re.fullmatch(r"[1-9][0-9]*", identifier) is not None,
        "References must use model,positive_id or false",
    )
    return model, int(identifier)


def record_value(field: RpcRecord, value: object, *, reading: bool = False) -> object:
    kind = field["type"]
    require(
        isinstance(kind, str) and kind in RECORD_TYPES, f"Unsupported record field type: {kind}"
    )
    if reading and kind == "many2one":
        value = relation_id_optional(value) or False
    if kind == "boolean":
        require(type(value) is bool, "Boolean values must be true or false")
        return value
    if value is False:
        require(
            kind not in ("integer", "float", "monetary"),
            "Numeric fields require a number, not false",
        )
        require(reading or not field.get("required"), "Cannot clear a required field")
        return False
    if kind == "reference":
        model, _ = reference_parts(value)
        require(
            model in {text(object_list(item)[0]) for item in object_list(field["selection"])},
            f"Unknown reference model: {model}",
        )
        return value
    if kind == "json":
        require(isinstance(value, dict), "JSON values must be objects or false")
        require(
            json.loads(json.dumps(value, allow_nan=False)) == value,
            "JSON objects must round-trip without changing keys or values",
        )
        require(
            bool(value) or reading or not field.get("required"), "Cannot clear a required field"
        )
        return value
    if kind in ("char", "text", "html", "selection", "date", "datetime"):
        if not isinstance(value, str):
            raise ValueError(f"{kind} values must be strings or false")
        if not value:
            require(reading or not field.get("required"), "Cannot clear a required field")
            return False
        if kind == "selection":
            require(
                value in {text(object_list(item)[0]) for item in object_list(field["selection"])},
                f"Unknown selection value: {value}",
            )
        if kind == "date":
            require(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None, "Dates must use YYYY-MM-DD"
            )
            date.fromisoformat(value)
        if kind == "datetime":
            require(
                re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value) is not None,
                "Datetimes must use YYYY-MM-DD HH:MM:SS in UTC",
            )
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return value
    if kind == "integer":
        require(
            type(value) is int and -(2**31) <= value < 2**31,
            "Integer values must fit a signed 32-bit integer",
        )
    elif kind in ("float", "monetary"):
        require(
            isinstance(value, (int, float))
            and type(value) in (int, float)
            and abs(value) <= sys.float_info.max
            and math.isfinite(value),
            "Float/monetary values must be finite numbers",
        )
    elif kind == "many2one":
        require(type(value) is int and value > 0, "many2one needs a positive record ID or false")
    return value


def record_plan(client: RpcClient, args: SchemaArgs, spec: RpcRecord | None) -> RecordPlan:
    require(
        args.model
        not in {
            "ir.model",
            "ir.model.fields",
            "ir.model.fields.selection",
            "ir.ui.view",
            "ir.model.data",
        },
        "Use field/view CRUD for schema metadata, not record commands",
    )
    if getattr(args, "lang", None) is not None:
        require(
            client.context["lang"] == args.lang, "Client language differs from requested language"
        )
        require(
            client.one("res.lang", [["code", "=", args.lang], ["active", "=", True]], ["id"])
            is not None,
            f"Language is not installed or accessible: {args.lang}",
        )
    creating = args.command == "record-create"
    allowed = {"values"} if creating else {"values", "expected"}
    if not isinstance(spec, dict) or spec.keys() - allowed:
        raise ValueError(
            "Record creation spec supports only values"
            if creating
            else "Record spec supports only values and expected"
        )
    values = spec.get("values")
    expected = spec.get("expected", {})
    values = object_dict(values, "values must be a nonempty object")
    if not values:
        raise ValueError("values must be a nonempty object")
    expected = object_dict(expected, "expected must be an object")
    names = set(values) | set(expected)
    require(
        all(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in names),
        "Use field names, not dotted paths",
    )
    raw_fields = object_dict(
        client.call(
            args.model,
            "fields_get",
            [sorted(names | {"write_date", "message_ids"})],
            attributes=[
                "type",
                "readonly",
                "required",
                "selection",
                "relation",
                "store",
                "related",
            ],
        )
    )
    fields = {name: object_dict(field) for name, field in raw_fields.items()}
    require(
        names <= fields.keys(), f"Unknown or inaccessible fields: {sorted(names - fields.keys())}"
    )
    normalized: RpcRecord = {}
    primary_attachment: RpcRecord | None = None
    for name in sorted(names):
        field = fields[name]
        if name == "message_main_attachment_id":
            require(
                args.command == "record-set" and name in values and name in expected,
                "Primary attachment requires record-set with an explicit expected current value",
            )
            require(
                field["type"] == "many2one" and field.get("relation") == "ir.attachment",
                "Primary attachment must be a native ir.attachment relation",
            )
            require(
                values[name] is not False,
                "Primary attachment assignment requires an existing attachment ID",
            )
        require(
            name
            not in {
                "id",
                "create_uid",
                "create_date",
                "write_uid",
                "write_date",
                "message_ids",
                "message_follower_ids",
            },
            f"System/chatter field is not supported: {name}",
        )
        if name in values:
            require(
                not field.get("readonly")
                and bool(field.get("store", True))
                and not field.get("related"),
                f"Cannot set readonly/non-stored/related field: {name}",
            )
            normalized[name] = record_value(field, values[name])
            if field["type"] == "many2one" and normalized[name] is not False:
                related = client.one(
                    text(field["relation"]),
                    [["id", "=", normalized[name]]],
                    ATTACHMENT_COLUMNS if name == "message_main_attachment_id" else ["id"],
                )
                if related is None:
                    raise ValueError(f"Unknown or inaccessible related record for {name}")
                if name == "message_main_attachment_id":
                    require(
                        related["res_model"] == args.model
                        and type(related["res_id"]) is int
                        and related["res_id"] == args.record_id
                        and related["res_field"] is False
                        and related["public"] is False
                        and related["type"] == "binary",
                        "Primary attachment must be a private binary attachment owned by the target record",
                    )
                    primary_attachment = related
            if field["type"] == "reference" and normalized[name] is not False:
                model, identifier = reference_parts(normalized[name])
                require(
                    client.one(model, [["id", "=", identifier]], ["id"]) is not None,
                    f"Unknown or inaccessible referenced record for {name}",
                )
    columns = ["id", *sorted(names)]
    if "write_date" in fields:
        columns.append("write_date")
    batch = args.command == "record-set-batch"
    before: RpcRecord | list[RpcRecord] | None = None
    if not creating:
        ids = args.record_ids if batch else [args.record_id]
        records = client.search(args.model, [["id", "in", ids]], columns, limit=len(ids) + 1)
        require([row["id"] for row in records] == ids, "Record does not exist or is inaccessible")
        for row in records:
            for name, value in expected.items():
                require(
                    record_value(fields[name], row[name], reading=True)
                    == record_value(fields[name], value),
                    f"Expected current value mismatch: {name} (record {row['id']})",
                )
        before = records if batch else records[0]
    context = mutation_context(client, args)
    plan: RecordPlan = {
        "action": "create" if creating else "update",
        "kind": "records" if batch else "record",
        "rpc_model": args.model,
        "before": before,
        "values": normalized,
        "requested": spec,
        "columns": columns,
        "fields": {name: fields[name] for name in sorted(names)},
        "context": context,
        "audit_chatter": args.chatter == "quiet" and "message_ids" in fields,
        "automation_acknowledged": args.allow_automation,
        "side_effect_warning": MUTATION_WARNING,
    }
    if primary_attachment is not None:
        plan["primary_attachment"] = primary_attachment
    return plan


def chatter_snapshot(
    client: RpcClient, args: SchemaArgs, *, record_id: int | None = None
) -> dict[str, list[int]]:
    snapshot: dict[str, list[int]] = {}
    for model in ("mail.message", "mail.mail"):
        ids = (
            args.record_ids
            if args.command == "record-set-batch"
            else [args.record_id if record_id is None else record_id]
        )
        domain = [["model", "=", args.model], ["res_id", "in", ids]]
        records = client.search(model, domain, ["id"], limit=1001)
        require(len(records) <= 1000, f"{model} exceeds the chatter audit limit")
        snapshot[model] = [integer(record["id"]) for record in records]
    return snapshot


logger = logging.getLogger(__name__)


def build_plan(client: SchemaClient, args: SchemaArgs, spec: RpcRecord | None) -> Plan:
    if args.command == "attachment-upload":
        require(spec is None, "Attachment upload does not accept a spec")
        return attachment_plan(client, args)
    if args.command in {"record-create", "record-set", "record-set-batch"}:
        return record_plan(client, args, spec)
    if args.command == "model-enable-chatter":
        require(spec is None, "Enabling chatter does not accept a spec")
        require(
            re.fullmatch(r"x_[a-z][a-z0-9_]*", args.model) is not None,
            "Chatter can be enabled only on manual x_ models",
        )
        before = client.one("ir.model", [["model", "=", args.model]], CHATTER_MODEL_COLUMNS)
        require(
            before is not None and before["state"] == "manual",
            "Chatter can be enabled only on an existing manual x_ model",
        )
        if before is None:
            raise ValueError(f"Model does not exist: {args.model}")
        require(before["is_mail_thread"] is False, "Chatter is already enabled or unavailable")
        return {
            "action": "update",
            "kind": "model",
            "rpc_model": "ir.model",
            "before": before,
            "values": {"is_mail_thread": True},
            "requested": None,
            "side_effect_warning": "Enabling chatter affects all records and cannot be disabled "
            "through Odoo's model settings. Activities and access rules are unchanged. "
            "Add a chatter element through a separate inherited form view.",
        }
    kind, action = args.command.split("-")
    if action not in ("read", "create", "update", "delete"):
        raise ValueError(f"Unknown schema action: {action}")
    if kind == "model":
        before = client.one("ir.model", [["model", "=", args.model]], MODEL_COLUMNS)
        if action == "read":
            if before is None:
                raise ValueError(f"Model does not exist: {args.model}")
            return {"action": action, "kind": kind, "before": before}
        require(before is None, f"Model already exists: {args.model}")
        if spec is None:
            raise ValueError("Missing model spec")
        return {
            "action": action,
            "kind": kind,
            "rpc_model": "ir.model",
            "before": None,
            "values": model_values(client, args.model, args.name, spec),
            "requested": spec,
        }
    owner = client.one("ir.model", [["model", "=", args.model]], ["id", "model", "state"])
    if owner is None:
        raise ValueError(f"Model does not exist: {args.model}")
    if kind == "action":
        before = client.one(
            "ir.actions.server",
            [
                ["model_id", "=", owner["id"]],
                ["name", "=", args.name],
            ],
            ACTION_COLUMNS,
        )
        if action == "read":
            if before is None:
                raise ValueError(f"action does not exist: {args.name}")
            return {"action": action, "kind": kind, "before": before}
        require(before is None, f"action already exists: {args.name}")
        if spec is None:
            raise ValueError("Missing action spec")
        values = action_values(client, integer(owner["id"]), spec)
        values["name"] = args.name
        return {
            "action": action,
            "kind": kind,
            "rpc_model": "ir.actions.server",
            "before": None,
            "values": values,
            "requested": spec,
        }
    if kind == "field":
        before = client.field(args.model, args.name)
        rpc_model = "ir.model.fields"
    else:
        before = client.one(
            "ir.ui.view", [["model", "=", args.model], ["name", "=", args.name]], VIEW_COLUMNS
        )
        rpc_model = "ir.ui.view"
    if action == "read":
        if before is None:
            raise ValueError(f"{kind} does not exist: {args.name}")
        return {"action": action, "kind": kind, "before": before}
    if kind not in ("field", "view"):
        raise ValueError(f"Unknown schema kind: {kind}")
    if kind == "field":
        require(
            re.fullmatch(r"x_[a-z][a-z0-9_]*", args.name) is not None,
            "Custom field names must start with x_ and use lowercase letters/digits/underscores",
        )
        require(before is None or before["state"] == "manual", "Only manual fields may be changed")
    else:
        require(
            before is None
            or (bool(before["inherit_id"]) and before["mode"] == "extension")
            or (
                args.model.startswith("x_")
                and owner["state"] == "manual"
                and not before["inherit_id"]
                and before["mode"] == "primary"
                and before["type"] in ("form", "list")
            ),
            "Only inherited extension views or manual-model primary views may be changed",
        )
        if before:
            module_ids = client.search(
                "ir.model.data",
                [
                    ["model", "=", "ir.ui.view"],
                    ["res_id", "=", before["id"]],
                    ["module", "!=", "__export__"],
                ],
                ["module", "name"],
            )
            require(
                not module_ids,
                f"Do not modify module-owned views; create your own inherited view: {module_ids}",
            )
    require(
        (before is None) == (action == "create"),
        f"{kind} {'already exists' if action == 'create' else 'does not exist'}: {args.name}",
    )
    values: RpcRecord = {}
    if action in ("create", "update"):
        if spec is None:
            raise ValueError("Missing spec")
        if kind == "field" and (
            {"compute", "depends"} & spec.keys() or (before and before.get("compute"))
        ):
            require(
                owner["state"] == "manual" and args.model.startswith("x_"),
                "Computed names require a manual x_ model",
            )
        values = (
            field_values(client, spec, before, args.model)
            if kind == "field"
            else view_values(client, args.model, spec, before)
        )
        if action == "create":
            values.update(
                {"name": args.name, "model_id": owner["id"], "state": "manual"}
                if kind == "field"
                else {
                    "name": args.name,
                    "model": args.model,
                    "mode": "extension" if values.get("inherit_id") else "primary",
                },
            )
    if action == "delete":
        if before is None:
            raise ValueError(f"{kind} does not exist: {args.name}")
        if kind == "field":
            if before["ttype"] == "many2one":
                if before["relation"] == "res.currency":
                    monetary_references = client.search(
                        "ir.model.fields",
                        [
                            ["model", "=", args.model],
                            ["ttype", "=", "monetary"],
                            ["currency_field", "=", args.name],
                        ],
                        ["id", "model", "name"],
                        limit=1001,
                    )
                    require(
                        not monetary_references,
                        f"Remove dependent monetary fields first: {monetary_references}",
                    )
                inverse_references = client.search(
                    "ir.model.fields",
                    [
                        ["model", "=", before["relation"]],
                        ["ttype", "=", "one2many"],
                        ["relation", "=", args.model],
                        ["relation_field", "=", args.name],
                    ],
                    ["id", "model", "name"],
                    limit=1001,
                )
                require(
                    not inverse_references,
                    f"Remove dependent one2many fields first: {inverse_references}",
                )
            candidates = client.search(
                "ir.model.fields",
                [["related", "ilike", args.name]],
                ["id", "model", "name", "related"],
                limit=1001,
            )
            require(len(candidates) <= 1000, "Related-field dependency check exceeds 1000 fields")
            dependencies = [
                row for row in candidates if args.name in text(row["related"]).split(".")
            ]
            require(
                not dependencies,
                f"Remove potentially dependent related fields first: {dependencies}",
            )
            references = client.search(
                "ir.ui.view",
                [
                    ["model", "=", args.model],
                    ["arch_db", "ilike", args.name],
                ],
                ["id", "name", "active"],
                limit=1001,
            )
            require(not references, f"Remove references from views first: {references}")
        else:
            children = client.search(
                "ir.ui.view", [["inherit_id", "=", before["id"]]], ["id", "name"]
            )
            require(not children, f"Remove dependent views first: {children}")
    return {
        "action": action,
        "kind": kind,
        "rpc_model": rpc_model,
        "before": before,
        "values": values,
        "requested": spec,
    }


def verify(
    client: SchemaClient, args: SchemaArgs, plan: Plan, result: object
) -> RpcRecord | list[RpcRecord] | None:
    if plan["action"] == "read":
        raise ValueError("A read plan cannot be verified as a mutation")
    if plan["kind"] == "attachment":
        return verify_attachment(client, args, plan, result)
    values = plan["values"]
    requested = plan["requested"] or {}
    if plan["kind"] == "model":
        if args.command == "model-enable-chatter":
            before = object_dict(plan["before"])
            after = client.one("ir.model", [["model", "=", args.model]], CHATTER_MODEL_COLUMNS)
            if after is None:
                raise ValueError("Saved model was not found")
            for key in (*MODEL_COLUMNS, "is_mail_activity"):
                require(after[key] == before[key], f"Saved model setting changed: {key}")
            require(after["is_mail_thread"] is True, "Chatter was not enabled")
            fields = object_dict(
                client.call(
                    args.model,
                    "fields_get",
                    [list(CHATTER_RELATIONS)],
                    attributes=["type", "relation"],
                )
            )
            for name, (kind, relation) in CHATTER_RELATIONS.items():
                require(name in fields, f"Chatter field was not created: {name}")
                field = object_dict(fields[name])
                require(
                    field.get("type") == kind and field.get("relation") == relation,
                    f"Chatter field schema mismatch: {name}",
                )
            return after
        after = client.one("ir.model", [["model", "=", args.model]], MODEL_COLUMNS)
        if after is None or after["id"] != result:
            raise ValueError("Saved model ID mismatch")
        for key in ("name", "model", "state"):
            require(after[key] == values[key], f"Saved model setting mismatch: {key}")
        access = record_list(requested["access"])
        rules = client.search(
            "ir.model.access", [["model_id", "=", result]], ACCESS_COLUMNS, limit=len(access) + 1
        )
        actual_rules: list[RpcRecord] = [
            {**rule, "group_id": relation_id_optional(rule["group_id"]) or False} for rule in rules
        ]
        actual_rules = [
            {key: value for key, value in rule.items() if key != "id"} for rule in actual_rules
        ]
        require(
            sorted(actual_rules, key=lambda rule: text(rule["name"]))
            == sorted(access, key=lambda rule: text(rule["name"])),
            "Saved model access rules mismatch",
        )
        after["access"] = actual_rules
        return after
    if plan["kind"] == "action":
        after = client.one("ir.actions.server", [["id", "=", result]], ACTION_COLUMNS)
        if after is None:
            raise ValueError("Saved action was not found")
        require(after["id"] == result, "Saved action ID mismatch")
        expected = {**values, "group_ids": requested["group_ids"]}
        for name, value in expected.items():
            actual = after[name]
            if name in {"model_id", "binding_model_id"}:
                actual = relation_id_optional(actual) or False
            if name == "group_ids":
                require(
                    set(positive_ids(actual, "Saved action access groups must be IDs"))
                    == set(positive_ids(value, "Action access groups must be IDs")),
                    "Saved action access groups mismatch",
                )
            else:
                require(actual == value, f"Saved action setting mismatch: {name}")
        return after
    if plan["kind"] == "record" or plan["kind"] == "records":
        ids = (
            [integer(result, "Unexpected create response")]
            if plan["action"] == "create"
            else args.record_ids
            if plan["kind"] == "records"
            else [args.record_id]
        )
        records = client.search(
            args.model, [["id", "in", ids]], plan["columns"], limit=len(ids) + 1
        )
        require([row["id"] for row in records] == ids, "Saved record was not found")
        for row in records:
            for name, value in values.items():
                actual = record_value(plan["fields"][name], row[name], reading=True)
                require(actual == value, f"Saved field value mismatch: {name} (record {row['id']})")
        if "primary_attachment" in plan:
            attachment = client.one(
                "ir.attachment",
                [["id", "=", plan["primary_attachment"]["id"]]],
                ATTACHMENT_COLUMNS,
            )
            require(
                attachment == plan["primary_attachment"],
                "Primary attachment changed during the update; inspect the journal and live state",
            )
        return records if plan["kind"] == "records" else records[0]
    after = (
        client.field(args.model, args.name)
        if plan["kind"] == "field"
        else client.one(
            "ir.ui.view",
            [["model", "=", args.model], ["name", "=", args.name]],
            VIEW_COLUMNS,
        )
    )
    if plan["action"] == "delete":
        require(after is None, "Deletion verification failed")
        return None
    if after is None:
        raise ValueError("Saved record was not found")
    expected_id = result if plan["action"] == "create" else object_dict(plan["before"])["id"]
    require(after["id"] == expected_id, "Saved record ID mismatch")
    if (
        plan["kind"] == "field"
        and after["ttype"] == "many2many"
        and (not after.get("related") or after.get("store") is True)
    ):
        settings = client.one(
            "ir.model.fields",
            [["id", "=", after["id"]]],
            ["id", "relation_table", "column1", "column2"],
        )
        if settings is None:
            raise ValueError("Saved many2many metadata was not found")
        after.update(settings)
    for key, value in requested.items():
        actual = after.get(key)
        if key == "inherit_id":
            actual = relation_id_optional(actual) or False
        if key == "groups":
            actual = object_list(actual, "Saved access groups must be a list")
            require(
                set(positive_ids(actual, "Saved access groups must be IDs"))
                == set(positive_ids(value, "Access groups must be IDs")),
                "Saved access groups mismatch",
            )
        elif key == "arch_db":
            if not isinstance(actual, str):
                raise ValueError("Saved view XML must be a string")
            require(
                ElementTree.tostring(ElementTree.fromstring(actual))
                == ElementTree.tostring(ElementTree.fromstring(text(value))),
                "Saved view XML mismatch",
            )
        elif key == "help" and not value:
            require(not actual, "Saved help text was not cleared")
        else:
            require(actual == value, f"Saved setting mismatch: {key}")
    return after


def apply_plan(client: SchemaClient, args: SchemaArgs, plan: Plan) -> RpcRecord:
    if plan["action"] == "read":
        raise ValueError("A read plan cannot be applied")
    journal: RpcRecord = {
        "target_url": args.target_url,
        "target_db": args.target_db,
        "model": args.model,
        "name": args.name,
        "plan": plan,
        "status": "prepared",
    }
    # Exclusive creation prevents accidentally overwriting a previous recovery record.
    journal_path = args.journal
    if journal_path is None:
        raise ValueError("Applying requires a journal path")
    with open(
        journal_path, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as stream:
        json.dump(journal, stream, indent=2)
        stream.write("\n")

    def save() -> None:
        journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    try:
        # Recheck immediately before the single write transaction; do not apply a stale plan.
        current = build_plan(client, args, plan["requested"])
        require(current == plan, "Schema or record changed since preview; rerun with a new journal")
        if plan.get("audit_chatter"):
            journal["chatter_before"] = (
                {"mail.message": [], "mail.mail": []}
                if plan["action"] == "create" and plan["kind"] == "record"
                else chatter_snapshot(client, args)
            )
            save()
        journal["status"] = "write_started"
        save()
        record_rows = (
            (
                record_list(plan["before"])
                if plan["kind"] == "records"
                else [object_dict(plan["before"])]
            )
            if plan["kind"] in {"record", "records"} and plan["action"] != "create"
            else []
        )
        values = plan["values"]
        fields = plan["fields"] if plan["kind"] == "record" or plan["kind"] == "records" else {}
        unchanged = {
            integer(row["id"])
            for row in record_rows
            if all(
                record_value(fields[name], row[name], reading=True) == value
                for name, value in values.items()
            )
        }
        if plan["kind"] == "attachment" and plan["before"] is not None:
            result = plan["before"]["id"]
            journal["no_change"] = True
        elif record_rows and len(unchanged) == len(record_rows):
            result = True
            journal["no_change"] = True
        elif plan["action"] == "create":
            result = client.call(
                plan["rpc_model"],
                "create",
                [attachment_payload(args, plan) if plan["kind"] == "attachment" else values],
                **(
                    {"context": plan["context"]}
                    if plan["kind"] == "record" or plan["kind"] == "attachment"
                    else {}
                ),
            )
            require(type(result) is int and result > 0, "Unexpected create response")
        else:
            result = client.call(
                plan["rpc_model"],
                "unlink" if plan["action"] == "delete" else "write",
                [[object_dict(plan["before"])["id"]]]
                if plan["action"] == "delete"
                else [
                    [row["id"] for row in record_rows if integer(row["id"]) not in unchanged],
                    values,
                ]
                if record_rows
                else [[object_dict(plan["before"])["id"]], values],
                **(
                    {"context": plan["context"]}
                    if plan["kind"] == "record" or plan["kind"] == "records"
                    else {}
                ),
            )
            require(result is True, "Odoo did not confirm the mutation")
        journal.update(status="write_confirmed", result=result)
        save()
        journal["after"] = verify(client, args, plan, result)
        if plan.get("audit_chatter"):
            journal["chatter_after"] = chatter_snapshot(
                client,
                args,
                record_id=integer(result)
                if plan["action"] == "create" and plan["kind"] == "record"
                else None,
            )
            save()
            require(
                journal["chatter_after"] == journal["chatter_before"],
                "Chatter/mail changed during quiet write; inspect the journal and live state",
            )
        journal["status"] = "verified"
        save()
    except Exception as exc:
        journal["status"] = "requires_review"
        save()
        logger.error(
            "odoo_schema_operation_failed model=%s name=%s journal=%s error_type=%s; "
            "inspect live state before retrying; no automatic destructive rollback",
            args.model,
            args.name,
            args.journal,
            type(exc).__name__,
        )
        raise
    return journal


def parse_args(argv: Sequence[str] | None = None) -> SchemaArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--target-db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for kind in ("model", "field", "view", "action"):
        actions = (
            ("read", "create")
            if kind in {"model", "action"}
            else ("read", "create", "update", "delete")
        )
        for action in actions:
            command = commands.add_parser(f"{kind}-{action}")
            command.add_argument("model")
            command.add_argument("name")
            if action in ("create", "update"):
                command.add_argument(
                    "--spec",
                    type=Path,
                    required=True,
                    help="JSON object containing field, view or action settings",
                )
            if action != "read":
                command.add_argument("--apply", action="store_true", help="Otherwise preview only")
                command.add_argument("--confirm-db", help="Must equal --target-db when applying")
                command.add_argument(
                    "--journal", type=Path, help="New local recovery journal for apply"
                )
            if action == "delete":
                command.add_argument(
                    "--allow-data-loss",
                    action="store_true",
                    help="Acknowledge irreversible field-data/view deletion",
                )
    chatter = commands.add_parser(
        "model-enable-chatter",
        help="Irreversibly enable chatter on an existing manual x_ model; preview by default",
    )
    chatter.add_argument("model")
    chatter.add_argument("--apply", action="store_true")
    chatter.add_argument("--confirm-db")
    chatter.add_argument("--journal", type=Path)
    upload = commands.add_parser(
        "attachment-upload",
        help="Upload or reuse a private attachment on an existing record",
    )
    upload.add_argument("model")
    upload.add_argument("record_id", type=int)
    upload.add_argument("--file", type=Path, required=True)
    upload.add_argument("--chatter", choices=("quiet", "normal"), default="quiet")
    upload.add_argument("--allow-automation", action="store_true")
    upload.add_argument("--apply", action="store_true")
    upload.add_argument("--confirm-db")
    upload.add_argument("--journal", type=Path)
    record = commands.add_parser("record-set", help="Set values on one existing record")
    record.add_argument("model")
    record.add_argument("record_id", type=int)
    record.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="JSON object with values and optional expected current values",
    )
    record.add_argument("--chatter", choices=("quiet", "normal"), default="quiet")
    record.add_argument("--lang", help="Installed language code for reads, writes and verification")
    record.add_argument(
        "--allow-automation",
        action="store_true",
        help="Acknowledge reviewed installed automation in quiet mode",
    )
    record.add_argument("--apply", action="store_true")
    record.add_argument("--confirm-db")
    record.add_argument("--journal", type=Path)
    create = commands.add_parser("record-create", help="Create one record; preview by default")
    create.add_argument("model")
    create.add_argument("--spec", type=Path, required=True, help="JSON object with values")
    create.add_argument("--chatter", choices=("quiet", "normal"), default="quiet")
    create.add_argument("--lang", help="Installed language code for reads, writes and verification")
    create.add_argument("--allow-automation", action="store_true")
    create.add_argument("--apply", action="store_true")
    create.add_argument("--confirm-db")
    create.add_argument("--journal", type=Path)
    batch = commands.add_parser(
        "record-set-batch", help="Set identical values on up to 1000 records"
    )
    batch.add_argument("model")
    batch.add_argument("record_ids", type=int, nargs="+")
    batch.add_argument("--spec", type=Path, required=True)
    batch.add_argument("--chatter", choices=("quiet", "normal"), default="quiet")
    batch.add_argument("--lang", help="Installed language code for reads, writes and verification")
    batch.add_argument("--allow-automation", action="store_true")
    batch.add_argument("--apply", action="store_true")
    batch.add_argument("--confirm-db")
    batch.add_argument("--journal", type=Path)
    args = SchemaArgs()
    parser.parse_args(argv, namespace=args)
    if args.command in {"model-enable-chatter", "record-create"}:
        args.name = args.model
    if args.lang is not None and not args.lang.strip():
        parser.error("--lang must not be empty")
    if args.command == "record-set-batch":
        if (
            not 1 <= len(args.record_ids) <= 1000
            or any(record_id <= 0 for record_id in args.record_ids)
            or len(set(args.record_ids)) != len(args.record_ids)
        ):
            parser.error("Batch requires 1-1000 unique positive record IDs")
        args.record_ids.sort()
        args.name = ",".join(map(str, args.record_ids))
    if args.command in {"record-set", "attachment-upload"}:
        if args.record_id <= 0:
            parser.error("record_id must be positive")
        args.name = str(args.record_id)
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
    if not MODEL_NAME_PATTERN.fullmatch(args.model):
        parser.error("Invalid Odoo model name")
    if not args.name.strip():
        parser.error("name must not be empty")
    if getattr(args, "apply", False):
        if args.confirm_db != args.target_db:
            parser.error("--apply requires --confirm-db matching --target-db")
        if not args.journal:
            parser.error("--apply requires a new --journal path")
        if args.command.endswith("-delete") and not args.allow_data_loss:
            parser.error("Applying deletion requires --allow-data-loss")
    return args


def main(
    argv: Sequence[str] | None = None, *, connection_provider: ConnectionProvider | None = None
) -> None:
    args = parse_args(argv)
    spec = load_spec(args.spec) if args.spec is not None else None
    connect = get_odoo_models if connection_provider is None else connection_provider
    models, db, uid, password = connect(
        key_file=args.key_file,
        target_url=args.target_url,
        target_db=args.target_db,
    )
    require(db == args.target_db, "Connected database differs from explicit target")
    db, uid, password = connection_values(db, uid, password)
    client = SchemaClient(models, db, uid, password, lang=getattr(args, "lang", None) or "en_US")
    plan = build_plan(client, args, spec)
    if getattr(args, "apply", False):
        output = apply_plan(client, args, plan)
    else:
        output = {"target_url": args.target_url, "target_db": db, "applied": False, **plan}
    json.dump(output, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
