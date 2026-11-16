import io
import json
import xmlrpc.client
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.request import Request

import pytest
from pydantic import TypeAdapter, ValidationError

from odoobase import get_odoo_models, record_dict, record_list, search_read, transport
from odoobase.models import RawPartner
from odoobase.relations import (
    optional_record,
    relation_id_optional,
    relation_id_required,
    required_record,
)
from odoobase.rpc import RpcClient
from tests.helpers import ScriptedRpc


class ExtendedPartner(RawPartner, total=False):
    x_reference: str


def test_extended_native_contract_validates_without_dropping_extra_columns() -> None:
    adapter = TypeAdapter(ExtendedPartner)
    row = {"id": 4, "name": False, "x_reference": "ext-4", "x_other": [1, "Label"]}
    assert adapter.validate_python(row) == row
    with pytest.raises(ValidationError):
        adapter.validate_python({**row, "x_reference": 123})
    with pytest.raises(ValidationError):
        adapter.validate_python({"name": "Missing native identity"})


@pytest.mark.parametrize("value", [7, [7, "Name"], (7, "Name")])
def test_native_relations_preserve_ids(value: object) -> None:
    assert relation_id_optional(value) == 7
    assert relation_id_required(value) == 7


@pytest.mark.parametrize("value", [None, False])
def test_only_false_and_none_are_absent_relations(value: object) -> None:
    assert relation_id_optional(value) is None
    with pytest.raises(ValueError, match="required"):
        relation_id_required(value)


@pytest.mark.parametrize("value", [True, 0, -1, "7", [], [7], [7, False], [True, "X"]])
def test_malformed_relations_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        relation_id_optional(value)


def test_missing_references_are_not_optional_absence() -> None:
    row = {"id": 7}
    assert optional_record({7: row}, None) is None
    assert required_record({7: row}, 7, context="Partner") is row
    with pytest.raises(ValueError, match="missing"):
        optional_record({7: row}, 8)
    with pytest.raises(ValueError, match="requires"):
        required_record({7: row}, None, context="Partner")


@pytest.mark.parametrize("value", [None, False, [], {"id": 1, 2: "invalid key"}])
def test_dynamic_record_boundary_rejects_malformed_results(value: object) -> None:
    with pytest.raises(ValidationError):
        record_dict(value)


def test_search_read_rejects_a_malformed_response_instead_of_returning_empty() -> None:
    rpc = ScriptedRpc()
    rpc.queue("res.partner", "search_read", False)
    with pytest.raises(ValidationError):
        search_read(*rpc.connection(), "res.partner", [], ["id"])
    rpc.assert_complete()


def test_rpc_client_forwards_context_and_rejects_ambiguous_identity() -> None:
    rpc = ScriptedRpc()
    rpc.queue("res.partner", "search_read", [{"id": 1}, {"id": 2}])
    client = RpcClient(*rpc.connection(), lang="es_ES")
    with pytest.raises(ValueError, match="Ambiguous"):
        client.one("res.partner", [], ["id"])
    assert rpc.calls[0].kwargs["context"] == client.context
    assert client.context["lang"] == "es_ES"
    rpc.assert_complete()


def configure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ODOO_USERNAME": "test-user",
        "ODOO_API_KEY": "test-key",
        "ODOO_URL": "https://example.test",
        "ODOO_DB": "example",
    }.items():
        monkeypatch.setenv(name, value)


class Authentication:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def authenticate(
        self, db: str, username: str, password: str, context: Mapping[str, object]
    ) -> object:
        self.calls.append((db, username, password))
        return self.result


def test_credentials_require_explicit_configuration() -> None:
    with pytest.raises(RuntimeError, match="ODOO_USERNAME"):
        get_odoo_models()


def test_explicit_key_file_wins_and_target_overrides_are_paired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch)
    path = tmp_path / "credentials"
    path.write_text("file-user\nfile-key\nhttps://file.example.test\nfile-db\n", encoding="utf-8")
    auth = Authentication(17)
    urls: list[str] = []

    def proxy(url: str, **kwargs: object) -> object:
        urls.append(url)
        return auth

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", proxy)
    _, db, uid, _ = get_odoo_models(
        key_file=path, target_url="https://clone.example.test/", target_db="clone"
    )
    assert (db, uid) == ("clone", 17)
    assert auth.calls == [("clone", "file-user", "file-key")]
    assert urls == [
        "https://clone.example.test/xmlrpc/2/common",
        "https://clone.example.test/xmlrpc/2/object",
    ]
    with pytest.raises(ValueError, match="together"):
        get_odoo_models(target_db="clone")


@pytest.mark.parametrize("contents", ["", "one\ntwo\nthree", "one\n\nthree\nfour"])
def test_malformed_key_files_fail_before_connecting(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "credentials"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="four nonempty"):
        get_odoo_models(key_file=path)


@pytest.mark.parametrize("uid", [False, True, "17", -1, None])
def test_invalid_authentication_never_yields_a_connection(
    monkeypatch: pytest.MonkeyPatch, uid: object
) -> None:
    configure_environment(monkeypatch)
    auth = Authentication(uid)

    def proxy(url: str, **kwargs: object) -> Authentication:
        return auth

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", proxy)
    with pytest.raises((ValueError, RuntimeError)):
        get_odoo_models()


@pytest.mark.parametrize(
    "mode", ["null", "fault", "wrong_id", "missing_result", "bad_envelope", "bad_error"]
)
def test_jsonrpc_null_and_malformed_envelopes(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    configure_environment(monkeypatch)

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        assert timeout == 60
        assert isinstance(request.data, bytes)
        payload = record_dict(json.loads(request.data))
        params = record_dict(payload["params"])
        if params["method"] == "authenticate":
            response: object = {"id": payload["id"], "result": 17}
        elif mode == "bad_envelope":
            response = []
        else:
            response = {"id": "wrong" if mode == "wrong_id" else payload["id"]}
            if mode == "fault":
                response["error"] = {"code": 42, "data": {"message": "Synthetic failure"}}
            elif mode == "bad_error":
                response["error"] = "invalid"
            elif mode != "missing_result":
                response["result"] = None
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(transport, "urlopen", urlopen)
    models, db, uid, password = get_odoo_models(rpc_protocol="jsonrpc")
    if mode == "null":
        assert models.execute_kw(db, uid, password, "account.report", "get_options", []) is None
    else:
        with pytest.raises((ValueError, xmlrpc.client.Fault)):
            models.execute_kw(db, uid, password, "account.report", "get_options", [])


@pytest.mark.parametrize("protocol", ["xmlrpc", "jsonrpc"])
def test_malformed_urls_fail_before_authentication(
    monkeypatch: pytest.MonkeyPatch, protocol: Literal["xmlrpc", "jsonrpc"]
) -> None:
    configure_environment(monkeypatch)
    monkeypatch.setenv("ODOO_URL", "https://user:password@example.test/path")
    with pytest.raises(ValueError, match="HTTPS origin"):
        get_odoo_models(rpc_protocol=protocol)


def test_dynamic_record_lists_preserve_custom_fields() -> None:
    rows = [{"id": 1, "x_anything": {"unmodified": False}}]
    assert record_list(rows) == rows
