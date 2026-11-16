"""Shared RPC access and validation helpers for operational utilities."""

import json
import re
from collections.abc import Sequence
from pathlib import Path

from odoobase.transport import RpcModels, RpcRecord, record_dict, record_list

MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
CONTEXT = {
    "active_test": False,
    "lang": "en_US",
    "tracking_disable": True,
    "mail_notrack": True,
    "mail_create_nolog": True,
    "mail_create_nosubscribe": True,
    "mail_auto_subscribe_no_notify": True,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_spec(path: Path) -> RpcRecord:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    try:
        spec = record_dict(value)
    except ValueError as exc:
        raise ValueError("Spec must be a nonempty JSON object") from exc
    require(bool(spec), "Spec must be a nonempty JSON object")
    return spec


class RpcClient:
    def __init__(self, models: RpcModels, db: str, uid: int, password: str, *, lang: str = "en_US"):
        self.models, self.db, self.uid, self.password = models, db, uid, password
        self.context: RpcRecord = {**CONTEXT, "lang": lang}

    def call(self, model: str, method: str, args: Sequence[object], **kwargs: object) -> object:
        return self.models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            method,
            args,
            {"context": self.context, **kwargs},
        )

    def search(
        self, model: str, domain: Sequence[object], fields: list[str], limit: int = 2
    ) -> list[RpcRecord]:
        return record_list(
            self.call(model, "search_read", [domain], fields=fields, limit=limit, order="id asc")
        )

    def one(self, model: str, domain: Sequence[object], fields: list[str]) -> RpcRecord | None:
        records = self.search(model, domain, fields)
        require(len(records) <= 1, f"Ambiguous {model} match; use a unique name")
        return records[0] if records else None
