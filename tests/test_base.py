import io
import json
import xmlrpc.client
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal
from urllib.request import Request

import pytest
from pydantic import TypeAdapter, ValidationError

from odoobase import get_odoo_models, record_dict, record_list, search_read, transport
from odoobase.models.v19_4 import MODELS, RawPartner
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


@pytest.mark.parametrize("move_id", [False, None, [12, "Entry"]])
def test_native_payment_projection_preserves_optional_journal_entry(move_id: object) -> None:
    adapter = TypeAdapter(MODELS["account.payment"])
    row = {
        "id": 1,
        "amount": 42.0,
        "state": "paid",
        "move_id": move_id,
        "outstanding_account_id": False,
        "invoice_ids": [12],
    }
    assert adapter.validate_python(row) == row
    with pytest.raises(ValidationError):
        adapter.validate_python({**row, "invoice_ids": ["12"]})
    with pytest.raises(ValidationError):
        adapter.validate_python({**row, "amount": "42.0"})


@pytest.mark.parametrize("model", ["account.move", "account.move.line"])
def test_native_bank_statement_relations_are_validated(model: str) -> None:
    adapter = TypeAdapter(MODELS[model])
    row = {"id": 1, "statement_line_id": [2, "Statement item"]}
    assert adapter.validate_python(row) == row
    assert adapter.validate_python({"id": 1, "statement_line_id": False}) == {
        "id": 1,
        "statement_line_id": False,
    }
    with pytest.raises(ValidationError):
        adapter.validate_python({**row, "statement_line_id": {"id": 2}})


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


@pytest.mark.parametrize("protocol", ["xmlrpc", "jsonrpc"])
def test_configured_uid_skips_authentication_and_forwards_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: Literal["xmlrpc", "jsonrpc"],
) -> None:
    path = tmp_path / "credentials"
    path.write_text(
        "file-user\nfile-key\nhttps://file.example.test\nfile-db\n23\n", encoding="utf-8"
    )
    urls: list[str] = []
    credentials: list[tuple[str, int, str]] = []

    class Models:
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
            credentials.append((db, uid, password))
            assert (model, method, args) == ("res.partner", "search_count", [[]])
            return 3

    def proxy(url: str, **kwargs: object) -> Models:
        urls.append(url)
        return Models()

    def urlopen(request: Request, *, timeout: float) -> io.BytesIO:
        assert isinstance(request.data, bytes)
        payload = record_dict(json.loads(request.data))
        params = record_dict(payload["params"])
        assert params == {
            "service": "object",
            "method": "execute_kw",
            "args": ["file-db", 23, "file-key", "res.partner", "search_count", [[]], {}],
        }
        return io.BytesIO(json.dumps({"id": payload["id"], "result": 3}).encode())

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", proxy)
    monkeypatch.setattr(transport, "urlopen", urlopen)
    models, db, uid, password = get_odoo_models(key_file=path, rpc_protocol=protocol)
    assert (db, uid, password) == ("file-db", 23, "file-key")
    assert models.execute_kw(db, uid, password, "res.partner", "search_count", [[]]) == 3
    if protocol == "xmlrpc":
        assert urls == ["https://file.example.test/xmlrpc/2/object"]
        assert credentials == [("file-db", 23, "file-key")]
    else:
        assert urls == []


@pytest.mark.parametrize("uid_text", ["0", "-1", "True", "1.5", "+17", "\u0661", "17\nextra"])
def test_invalid_configured_uid_fails_before_connecting(tmp_path: Path, uid_text: str) -> None:
    path = tmp_path / "credentials"
    path.write_text(
        f"file-user\nfile-key\nhttps://file.example.test\nfile-db\n{uid_text}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="user ID"):
        get_odoo_models(key_file=path)


@pytest.mark.parametrize(
    "target_url,target_db",
    [
        ("https://clone.example.test", "clone"),
        ("https://file.example.test", "file-db"),
    ],
)
def test_target_override_authenticates_instead_of_using_configured_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target_url: str, target_db: str
) -> None:
    path = tmp_path / "credentials"
    path.write_text(
        "file-user\nfile-key\nhttps://file.example.test\nfile-db\n23\n", encoding="utf-8"
    )
    auth = Authentication(17)

    def proxy(url: str, **kwargs: object) -> Authentication:
        return auth

    monkeypatch.setattr(xmlrpc.client, "ServerProxy", proxy)
    _, db, uid, _ = get_odoo_models(key_file=path, target_url=target_url, target_db=target_db)
    assert (db, uid) == (target_db, 17)
    assert auth.calls == [(target_db, "file-user", "file-key")]


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
@pytest.mark.parametrize("configured_uid", [False, True])
def test_jsonrpc_null_and_malformed_envelopes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, configured_uid: bool
) -> None:
    configure_environment(monkeypatch)
    key_file = None
    if configured_uid:
        key_file = tmp_path / "credentials"
        key_file.write_text(
            "test-user\ntest-key\nhttps://example.test\nexample\n17\n", encoding="utf-8"
        )

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
    models, db, uid, password = get_odoo_models(key_file=key_file, rpc_protocol="jsonrpc")
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
