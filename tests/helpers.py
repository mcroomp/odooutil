from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from odoobase import RpcConnection, RpcRecord


@dataclass(frozen=True)
class Call:
    model: str
    method: str
    args: Sequence[object]
    kwargs: Mapping[str, object]


@dataclass
class Reply:
    model: str
    method: str
    result: object


@dataclass
class ScriptedRpc:
    """Strict ordered transport: unexpected calls and unused replies fail tests."""

    replies: deque[Reply] = field(default_factory=deque[Reply])
    calls: list[Call] = field(default_factory=list[Call])

    def queue(self, model: str, method: str, result: object) -> None:
        self.replies.append(Reply(model, method, result))

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
        assert (db, uid, password) == ("example", 17, "test-key")
        self.calls.append(Call(model, method, args, kwargs or {}))
        assert self.replies, f"Unexpected {model}.{method}"
        reply = self.replies.popleft()
        assert (model, method) == (reply.model, reply.method)
        if isinstance(reply.result, Exception):
            raise reply.result
        return reply.result

    def connection(self, **kwargs: object) -> RpcConnection:
        return self, "example", 17, "test-key"

    def assert_complete(self) -> None:
        assert not self.replies, f"Unused replies: {self.replies}"

    @property
    def mutations(self) -> list[Call]:
        return [call for call in self.calls if call.method in {"create", "write", "unlink"}]


def partner_field() -> RpcRecord:
    return {
        "id": 2,
        "name": "x_reference",
        "model": "res.partner",
        "state": "manual",
        "field_description": "Reference",
        "ttype": "char",
    }
