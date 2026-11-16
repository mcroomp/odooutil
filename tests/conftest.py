import socket

import pytest


@pytest.fixture(autouse=True)
def isolate_network_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ODOO_USERNAME", "ODOO_API_KEY", "ODOO_URL", "ODOO_DB"):
        monkeypatch.delenv(name, raising=False)

    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must inject an RPC transport, not access the network")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
