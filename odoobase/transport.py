"""XML-RPC/JSON-RPC connections and validated dynamic records."""

import json
import logging
import os
import xmlrpc.client
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import TypeAdapter

type RpcRecord = dict[str, object]


class RpcModels(Protocol):
    def execute_kw(
        self,
        db: str,
        uid: int,
        password: str,
        model: str,
        method: str,
        args: Sequence[object],
        kwargs: Mapping[str, object] | None = None,
    ) -> object: ...


class _RpcCommon(Protocol):
    def authenticate(
        self,
        db: str,
        username: str,
        password: str,
        context: Mapping[str, object],
    ) -> object: ...


type RpcConnection = tuple[RpcModels, str, int, str]


class ConnectionProvider(Protocol):
    """Application-owned credential/configuration boundary for live utilities."""

    def __call__(
        self,
        *,
        key_file: Path | None = None,
        target_url: str | None = None,
        target_db: str | None = None,
        rpc_protocol: Literal["xmlrpc", "jsonrpc"] = "xmlrpc",
    ) -> RpcConnection: ...


_record_adapter = TypeAdapter(RpcRecord)
_records_adapter = TypeAdapter(list[RpcRecord])


def record_dict(value: object) -> RpcRecord:
    """Validate a dynamic RPC/spec mapping without narrowing its field inventory."""
    return _record_adapter.validate_python(value, strict=True)


def record_list(value: object) -> list[RpcRecord]:
    """Validate a dynamic RPC record collection at the transport boundary."""
    return _records_adapter.validate_python(value, strict=True)


logger = logging.getLogger(__name__)


class _JsonRpcService:
    """Preserve null report values that Odoo's XML-RPC serializer rejects."""

    def __init__(self, url: str, service: str) -> None:
        self.url = url.rstrip("/") + "/jsonrpc"
        self.service = service

    def _call(self, method: str, args: Sequence[object]) -> object:
        request_id = uuid4().hex
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": self.service, "method": method, "args": args},
            "id": request_id,
        }
        request = Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            raw: object = json.load(response)
        if not isinstance(raw, dict):
            raise ValueError("Invalid Odoo JSON-RPC response envelope or request ID")
        result = record_dict(cast(object, raw))
        if result.get("id") != request_id:
            raise ValueError("Invalid Odoo JSON-RPC response envelope or request ID")
        if "error" in result:
            error = result["error"]
            if not isinstance(error, dict):
                raise ValueError("Invalid Odoo JSON-RPC error response")
            error = record_dict(cast(object, error))
            data = error.get("data")
            message = (
                record_dict(cast(object, data)).get("message") if isinstance(data, dict) else None
            )
            message = message or error.get("message") or "Odoo JSON-RPC request failed"
            code = error.get("code", -32000)
            if not isinstance(code, int) or not isinstance(message, str):
                raise ValueError("Invalid Odoo JSON-RPC error code or message")
            raise xmlrpc.client.Fault(
                code,
                message,
            )
        if "result" not in result:
            raise ValueError("Odoo JSON-RPC response has no result")
        return result["result"]

    def authenticate(
        self,
        db: str,
        username: str,
        password: str,
        context: Mapping[str, object],
    ) -> object:
        return self._call("authenticate", [db, username, password, context])

    def execute_kw(
        self,
        db: str,
        uid: int,
        password: str,
        model: str,
        method: str,
        args: Sequence[object],
        kwargs: Mapping[str, object] | None = None,
    ) -> object:
        return self._call(
            "execute_kw",
            [db, uid, password, model, method, args, kwargs or {}],
        )


def search_read(
    models: RpcModels,
    db: str,
    uid: int,
    pw: str,
    model: str,
    domain: Sequence[object],
    fields: Sequence[str],
) -> list[RpcRecord]:
    """Typed wrapper for execute_kw search_read; always returns a list of dicts."""
    result = models.execute_kw(db, uid, pw, model, "search_read", [domain], {"fields": fields})
    return record_list(result)


def get_odoo_models(
    key_file: Path | None = None,
    target_url: str | None = None,
    target_db: str | None = None,
    rpc_protocol: Literal["xmlrpc", "jsonrpc"] = "xmlrpc",
) -> RpcConnection:
    if rpc_protocol not in ("xmlrpc", "jsonrpc"):
        raise ValueError(f"Unsupported Odoo RPC protocol: {rpc_protocol}")
    username = os.environ.get("ODOO_USERNAME")
    password = os.environ.get("ODOO_API_KEY")
    odoo_url = os.environ.get("ODOO_URL")
    db = os.environ.get("ODOO_DB")

    if key_file is not None:
        try:
            lines = key_file.read_text(encoding="utf-8").strip().splitlines()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Cannot read the explicit Odoo credential file") from exc
        if len(lines) != 4 or not all(line.strip() for line in lines):
            raise ValueError(
                "Odoo credential file needs four nonempty lines: user, key, URL, database"
            )
        username, password, odoo_url, db = (line.strip() for line in lines)
    if not all((username, password, odoo_url, db)):
        raise RuntimeError(
            "Set ODOO_USERNAME, ODOO_API_KEY, ODOO_URL and ODOO_DB, or provide an explicit key_file"
        )
    if (target_url is None) != (target_db is None):
        raise ValueError("target_url and target_db must be provided together")
    if target_url is not None:
        odoo_url = target_url.rstrip("/")
    if target_db is not None:
        db = target_db
    if username is None or password is None or odoo_url is None or db is None:
        raise RuntimeError("Incomplete Odoo credentials")
    target = urlsplit(odoo_url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.path not in ("", "/")
        or target.query
        or target.fragment
        or not db.strip()
    ):
        raise ValueError(
            "Odoo requires an HTTPS origin without credentials and a nonempty database"
        )
    odoo_url = odoo_url.rstrip("/")

    credential_source = "key_file" if key_file is not None else "environment"
    host = urlsplit(str(odoo_url)).hostname or "unknown"
    common = (
        _JsonRpcService(odoo_url, "common")
        if rpc_protocol == "jsonrpc"
        else cast(_RpcCommon, xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/common"))
    )
    try:
        uid = common.authenticate(db, username, password, {})
    except Exception:
        logger.exception(
            "odoo_authentication_error host=%s database=%s credential_source=%s",
            host,
            db,
            credential_source,
        )
        raise
    if not uid:
        logger.error(
            "odoo_authentication_rejected host=%s database=%s credential_source=%s",
            host,
            db,
            credential_source,
        )
        raise RuntimeError("Odoo authentication failed")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError("Odoo authentication returned an invalid user ID")
    logger.info(
        "odoo_authenticated host=%s database=%s uid=%s credential_source=%s",
        host,
        db,
        uid,
        credential_source,
    )

    models = (
        _JsonRpcService(odoo_url, "object")
        if rpc_protocol == "jsonrpc"
        else cast(
            RpcModels, xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/object", allow_none=True)
        )
    )
    return models, db, uid, password


def execute_kw_none_ok(
    models: RpcModels,
    db: str,
    uid: int,
    password: str,
    model: str,
    method: str,
    args: Sequence[object],
    kwargs: Mapping[str, object] | None = None,
) -> object:
    """
    Wrapper around models.execute_kw that silently ignores the Odoo SaaS quirk
    where the server-side XML-RPC marshaller fails on a None return value.
    The method DID execute; only the response serialisation fails.
    """
    call_kwargs = kwargs or {}
    try:
        return models.execute_kw(db, uid, password, model, method, args, call_kwargs)
    except xmlrpc.client.Fault as exc:
        if "marshal None" in str(exc):
            return None
        raise
