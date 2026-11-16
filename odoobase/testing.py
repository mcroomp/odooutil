"""Strict, stateful RPC fake over independent copies of native fixture records."""

import logging
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from itertools import count
from pathlib import Path
from time import perf_counter
from typing import Literal, NamedTuple, cast, get_type_hints, is_typeddict

from odoobase.relations import relation_id_optional
from odoobase.transport import RpcConnection

logger = logging.getLogger(__name__)
_transport_ids = count(1)
_QUERY_METHODS = frozenset({"read", "search", "search_read", "search_count", "fields_get"})
_CRUD_METHODS = frozenset({"create", "write", "unlink"})


class RpcCall(NamedTuple):
    model: str
    method: str
    args: list[object]
    kwargs: dict[str, object]


@dataclass
class _Failure:
    model: str
    method: str
    error: Exception
    ids: tuple[int, ...] | None
    after: bool


@dataclass
class _Reply:
    model: str
    method: str
    result: object
    ids: tuple[int, ...] | None


type Handler = Callable[["FakeOdooTransport", RpcCall], object]
type Effect = Callable[["FakeOdooTransport", RpcCall], None]
type ReplyTransform = Callable[["FakeOdooTransport", RpcCall, object], object]
type ChildFilter = Callable[[Mapping[str, object]], bool]


def _merge_models[Value](
    base: Mapping[str, Mapping[str, Value]],
    overrides: Mapping[str, Mapping[str, Value]],
) -> dict[str, dict[str, Value]]:
    return {
        model: {**base.get(model, {}), **overrides.get(model, {})}
        for model in base.keys() | overrides.keys()
    }


@dataclass(frozen=True)
class FakeSchema:
    """Reusable model configuration; each transport copies its mutable state."""

    fields: Mapping[str, Mapping[str, Mapping[str, object]]] = dataclass_field(
        default_factory=lambda: dict[str, Mapping[str, Mapping[str, object]]](),
    )
    defaults: Mapping[str, Mapping[str, object]] = dataclass_field(
        default_factory=lambda: dict[str, Mapping[str, object]](),
    )
    known_fields: Mapping[str, Collection[str]] = dataclass_field(
        default_factory=lambda: dict[str, Collection[str]](),
    )
    relations: Mapping[str, Mapping[str, str]] = dataclass_field(
        default_factory=lambda: dict[str, Mapping[str, str]](),
    )
    children: Mapping[str, Mapping[str, tuple[str, str]]] = dataclass_field(
        default_factory=lambda: dict[str, Mapping[str, tuple[str, str]]](),
    )
    child_filters: Mapping[str, Mapping[str, ChildFilter]] = dataclass_field(
        default_factory=lambda: dict[str, Mapping[str, ChildFilter]](),
    )
    cascades: Mapping[str, Sequence[tuple[str, str]]] = dataclass_field(
        default_factory=lambda: dict[str, Sequence[tuple[str, str]]](),
    )
    label_fields: tuple[str, ...] | None = None
    odoo_version: str | None = None

    @classmethod
    def from_models(
        cls,
        models: Mapping[str, type[object]],
        *,
        odoo_version: str | None = None,
    ) -> FakeSchema:
        """Use extended native TypedDicts as the field-name contract, including inheritance."""
        for model, row_type in models.items():
            if not is_typeddict(row_type):
                raise TypeError(f"Fake model {model} must use a native TypedDict")
        return cls(
            odoo_version=odoo_version,
            known_fields={
                model: tuple(get_type_hints(row_type)) for model, row_type in models.items()
            },
        )

    def extend(self, overrides: FakeSchema) -> FakeSchema:
        """Overlay model fields, union field names/cascades, and retain other models."""
        if (
            self.odoo_version is not None
            and overrides.odoo_version is not None
            and self.odoo_version != overrides.odoo_version
        ):
            raise ValueError(
                f"Cannot mix Odoo model schemas {self.odoo_version} and {overrides.odoo_version}",
            )
        return FakeSchema(
            odoo_version=(
                self.odoo_version if overrides.odoo_version is None else overrides.odoo_version
            ),
            fields=_merge_models(self.fields, overrides.fields),
            defaults=_merge_models(self.defaults, overrides.defaults),
            known_fields={
                model: set(self.known_fields.get(model, ()))
                | set(overrides.known_fields.get(model, ()))
                for model in self.known_fields.keys() | overrides.known_fields.keys()
            },
            relations=_merge_models(self.relations, overrides.relations),
            children=_merge_models(self.children, overrides.children),
            child_filters=_merge_models(self.child_filters, overrides.child_filters),
            cascades={
                model: tuple(
                    dict.fromkeys(
                        (
                            *self.cascades.get(model, ()),
                            *overrides.cascades.get(model, ()),
                        )
                    )
                )
                for model in self.cascades.keys() | overrides.cascades.keys()
            },
            label_fields=(
                self.label_fields if overrides.label_fields is None else overrides.label_fields
            ),
        )


class FakeOdooTransport:
    """A bounded RPC contract fake, not an Odoo ORM or accounting engine.

    Fields, relations and owned children are supplied explicitly. Unknown models,
    fields, methods and unsupported operations fail rather than returning defaults.
    """

    def __init__(
        self,
        *,
        schema: FakeSchema | None = None,
        records: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        fields: Mapping[str, Mapping[str, Mapping[str, object]]] | None = None,
        defaults: Mapping[str, Mapping[str, object]] | None = None,
        children: Mapping[str, Mapping[str, tuple[str, str]]] | None = None,
        child_filters: Mapping[str, Mapping[str, ChildFilter]] | None = None,
        cascades: Mapping[str, Sequence[tuple[str, str]]] | None = None,
        known_fields: Mapping[str, Iterable[str]] | None = None,
        relations: Mapping[str, Mapping[str, str]] | None = None,
        label_fields: Sequence[str] | None = None,
        database: str = "fixture",
        user_id: int = 1,
        password: str = "fixture",
    ) -> None:
        configuration = (FakeSchema() if schema is None else schema).extend(
            FakeSchema(
                fields=fields or {},
                defaults=defaults or {},
                children=children or {},
                child_filters=child_filters or {},
                cascades=cascades or {},
                known_fields={model: tuple(names) for model, names in (known_fields or {}).items()},
                relations=relations or {},
                label_fields=None if label_fields is None else tuple(label_fields),
            )
        )
        fields, defaults, children = (
            configuration.fields,
            configuration.defaults,
            configuration.children,
        )
        child_filters, cascades = configuration.child_filters, configuration.cascades
        known_fields, relations = configuration.known_fields, configuration.relations
        label_fields = (
            ("display_name", "name")
            if configuration.label_fields is None
            else configuration.label_fields
        )
        self.trace_id = next(_transport_ids)
        self.odoo_version = configuration.odoo_version
        self.connection_count = 0
        self.request_count = 0
        self.calls: list[RpcCall] = []
        self._tables = _record_tables(records or {})
        self._initial_ids = {model: set(rows) for model, rows in self._tables.items()}
        self._seeded = False
        self.fields = {
            model: {name: deepcopy(dict(metadata)) for name, metadata in definitions.items()}
            for model, definitions in (fields or {}).items()
        }
        self._defaults = {
            model: deepcopy(dict(values)) for model, values in (defaults or {}).items()
        }
        self._children = {model: dict(links) for model, links in (children or {}).items()}
        self._child_filters = {
            model: dict(filters) for model, filters in (child_filters or {}).items()
        }
        for model, filters in self._child_filters.items():
            if filters.keys() - self._children.get(model, {}).keys():
                raise ValueError(f"Child filters require configured child fields: {model}")
        self._cascades = {model: tuple(links) for model, links in (cascades or {}).items()}
        self.known_fields = {model: set(names) for model, names in (known_fields or {}).items()}
        self._relations = {model: dict(links) for model, links in (relations or {}).items()}
        self._label_fields = tuple(label_fields)
        for model in (
            self.fields.keys()
            | self._defaults.keys()
            | self._children.keys()
            | self.known_fields.keys()
        ):
            self._tables.setdefault(model, {})
        self._identity = database, _id(user_id), password
        self._next_ids = {model: max(rows, default=0) + 1 for model, rows in self._tables.items()}
        self._handlers: dict[tuple[str, str], Handler] = {}
        self._effects: dict[tuple[str, str], Effect] = {}
        self._before: dict[tuple[str, str], Effect] = {}
        self._reply_transforms: dict[tuple[str, str], ReplyTransform] = {}
        self._failures: list[_Failure] = []
        self._replies: list[_Reply] = []
        logger.debug("fake_odoo_transport_created transport=%s", self.trace_id)

    def seed_records(
        self,
        records: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> None:
        """Merge a fixture baseline once, preserving arranged remote changes/deletions."""
        if self._seeded:
            raise ValueError("A fake transport can be seeded only once")
        tables = _record_tables(records)
        for model, rows in self._tables.items():
            table = tables.setdefault(model, {})
            for record_id in self._initial_ids.get(model, set()) - rows.keys():
                table.pop(record_id, None)
            for record_id, row in rows.items():
                table[record_id] = {**table.get(record_id, {}), **deepcopy(row)}
        self._next_ids = {
            model: max(self._next_ids.get(model, 1), max(rows, default=0) + 1)
            for model, rows in tables.items()
        }
        self._tables = tables
        self._seeded = True

    def __getitem__(self, model: str) -> dict[int, dict[str, object]]:
        if model not in self._tables:
            raise AssertionError(f"Unconfigured fake Odoo model: {model}")
        return self._tables[model]

    def connection(
        self,
        *,
        key_file: Path | None = None,
        target_url: str | None = None,
        target_db: str | None = None,
        rpc_protocol: Literal["xmlrpc", "jsonrpc"] = "xmlrpc",
    ) -> RpcConnection:
        """Provide fixture identity without reading credentials or opening a socket."""
        if target_db is not None and target_db != self._identity[0]:
            raise AssertionError(f"Unexpected fake target database: {target_db}")
        self.connection_count += 1
        logger.debug(
            "fake_odoo_connection_requested transport=%s connection_count=%s",
            self.trace_id,
            self.connection_count,
        )
        return self, *self._identity

    def on(self, model: str, method: str, handler: Handler) -> None:
        if method in _QUERY_METHODS | _CRUD_METHODS:
            raise ValueError(
                "Use records, after() or fail_next(); do not replace generic RPC behavior"
            )
        self._handlers[model, method] = handler

    def after(self, model: str, method: str, effect: Effect) -> None:
        self._effects[model, method] = effect

    def before(self, model: str, method: str, effect: Effect) -> None:
        self._before[model, method] = effect

    def transform_reply(self, model: str, method: str, transform: ReplyTransform) -> None:
        self._reply_transforms[model, method] = transform

    def reply_next(
        self, model: str, method: str, result: object, *, ids: Sequence[int] | None = None
    ) -> None:
        self._replies.append(
            _Reply(model, method, deepcopy(result), None if ids is None else tuple(ids))
        )

    def fail_next(
        self,
        model: str,
        method: str,
        error: Exception,
        *,
        ids: Sequence[int] | None = None,
        after: bool = False,
    ) -> None:
        self._failures.append(
            _Failure(model, method, error, None if ids is None else tuple(ids), after)
        )

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
        if (db, uid, password) != self._identity:
            raise AssertionError("Unexpected fake Odoo connection")
        self[model]
        call = RpcCall(model, method, deepcopy(list(args)), deepcopy(dict(kwargs or {})))
        self.calls.append(call)
        self.request_count += 1
        request_number = self.request_count
        started = perf_counter()
        try:
            result = self._execute_call(call)
        except BaseException as exc:
            self._log_call(call, request_number, started, error_type=type(exc).__name__)
            raise
        self._log_call(call, request_number, started, result=result)
        return result

    def _log_call(
        self,
        call: RpcCall,
        request_number: int,
        started: float,
        *,
        result: object = None,
        error_type: str | None = None,
    ) -> None:
        result_count = (
            len(cast(Sequence[object] | Mapping[object, object], result))
            if isinstance(result, (list, tuple, dict))
            else result
            if call.method == "search_count" and type(result) is int
            else None
        )
        logger.debug(
            "fake_odoo_rpc transport=%s request=%s model=%s method=%s "
            "result_count=%s error_type=%s elapsed_ms=%.3f",
            self.trace_id,
            request_number,
            call.model,
            call.method,
            result_count,
            error_type,
            (perf_counter() - started) * 1000,
        )

    def _execute_call(self, call: RpcCall) -> object:
        model, method = call.model, call.method
        failure = next(
            (
                item
                for item in self._failures
                if (item.model, item.method) == (model, method)
                and (item.ids is None or call.args[:1] == [list(item.ids)])
            ),
            None,
        )
        if failure is not None:
            self._failures.remove(failure)
            if not failure.after:
                raise failure.error
        tables = deepcopy(self._tables)
        next_ids, fields = self._next_ids.copy(), deepcopy(self.fields)
        try:
            if (before := self._before.get((model, method))) is not None:
                before(self, call)
            result = self._dispatch(call)
            if (effect := self._effects.get((model, method))) is not None:
                effect(self, call)
            if (transform := self._reply_transforms.get((model, method))) is not None:
                result = transform(self, call, deepcopy(result))
        except BaseException:
            self._tables, self._next_ids, self.fields = tables, next_ids, fields
            raise
        if failure is not None:
            raise failure.error
        reply = next(
            (
                item
                for item in self._replies
                if (item.model, item.method) == (model, method)
                and (item.ids is None or call.args[:1] == [list(item.ids)])
            ),
            None,
        )
        if reply is not None:
            self._replies.remove(reply)
            result = reply.result
        return deepcopy(result)

    def _dispatch(self, call: RpcCall) -> object:
        model, method, args, kwargs = call.model, call.method, call.args, call.kwargs.copy()
        context = _values(kwargs.pop("context", {}))
        if method in _QUERY_METHODS:
            return self._query(call, kwargs, context)
        if (handler := self._handlers.get((model, method))) is not None:
            return handler(self, call)
        if kwargs:
            raise AssertionError(f"Unsupported fake {method} options: {kwargs}")
        if method == "create" and len(args) == 1:
            if isinstance(args[0], Mapping):
                return self._create(model, _values(cast(Mapping[object, object], args[0])))
            return [self._create(model, _values(row)) for row in _sequence(args[0])]
        if method == "write" and len(args) == 2:
            for record_id in _ids(args[0]):
                self._write(model, record_id, _values(args[1]))
            return True
        if method == "unlink" and len(args) == 1:
            for record_id in _ids(args[0]):
                self._unlink(model, record_id)
            return True
        raise AssertionError(f"Unsupported fake RPC: {model}.{method} {args}")

    def _query(
        self, call: RpcCall, kwargs: dict[str, object], context: Mapping[str, object]
    ) -> object:
        model, method, args = call.model, call.method, call.args
        if method == "fields_get":
            attributes = kwargs.pop("attributes", None)
            requested = kwargs.pop("allfields", args[0] if args else None)
            if kwargs or len(args) > 1:
                raise AssertionError("Unsupported fake fields_get projection")
            definitions = self.fields.get(model)
            if definitions is None:
                definitions = {field: {"type": "fixture"} for field in self._known_fields(model)}
            names = list(definitions) if requested is None else _sequence(requested)
            selected = None if attributes is None else _sequence(attributes)
            return {
                str(field): {
                    key: deepcopy(value)
                    for key, value in definitions[str(field)].items()
                    if selected is None or key in selected
                }
                for field in names
                if field in definitions
            }
        if method == "search_count" and kwargs.keys() - {"limit"}:
            raise AssertionError("Unsupported fake search_count options")
        if method == "search" and "fields" in kwargs:
            raise AssertionError("Fake search does not return projected records")
        fields = kwargs.pop("fields", args[1] if method == "read" and len(args) == 2 else None)
        if fields is not None:
            selected = _sequence(fields)
            if any(
                not isinstance(field, str) or field not in self._known_fields(model)
                for field in selected
            ):
                raise AssertionError(f"Unknown fake fields for {model}: {selected}")
        if method == "read":
            if len(args) not in (1, 2) or kwargs:
                raise AssertionError("Unsupported fake read arguments")
            return [
                self._project(model, self[model][record_id], fields) for record_id in _ids(args[0])
            ]
        if method not in {"search", "search_read", "search_count"} or len(args) > 1:
            raise AssertionError(f"Unsupported fake query: {method}")
        domain = _sequence(args[0]) if args else []
        self._matches(model, None, domain)
        order = kwargs.pop("order", "id")
        offset, limit = kwargs.pop("offset", 0), kwargs.pop("limit", None)
        if (
            kwargs
            or not isinstance(order, str)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise AssertionError("Unsupported fake search options")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
        ):
            raise AssertionError("Invalid fake search limit")
        rows = [
            row
            for row in self[model].values()
            if (context.get("active_test") is False or row.get("active") is not False)
            and self._matches(model, row, domain)
        ]
        if method == "search_count":
            return min(len(rows), limit) if limit else len(rows)
        for clause in reversed(order.split(",")):
            parts = clause.split()
            if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"asc", "desc"}):
                raise AssertionError(f"Unsupported fake sort: {order}")
            field = parts[0]
            if field not in self._known_fields(model):
                raise AssertionError(f"Unknown fake sort field: {field}")
            rows.sort(
                key=lambda row: self._sort_value(self._field(model, row, field)),
                reverse=len(parts) == 2 and parts[1] == "desc",
            )
        rows = rows[offset : None if not limit else offset + limit]
        if method == "search":
            return [row["id"] for row in rows]
        return [self._project(model, row, fields) for row in rows]

    @staticmethod
    def _sort_value(value: object) -> tuple[str, str | int | float]:
        if isinstance(value, (str, int, float)):
            return ("text" if isinstance(value, str) else "number"), value
        raise AssertionError(f"Unsupported fake sort value: {value!r}")

    def _known_fields(self, model: str) -> set[str]:
        return (
            {"id"}
            | self.known_fields.get(model, set())
            | set(self._relations.get(model, {}))
            | set(self.fields.get(model, {}))
            | set(self._defaults.get(model, {}))
            | {field for row in self[model].values() for field in row}
            | set(self._children.get(model, {}))
            | {
                inverse
                for links in self._children.values()
                for child, inverse in links.values()
                if child == model
            }
        )

    def _field(self, model: str, row: Mapping[str, object], field: str) -> object:
        if field in self._children.get(model, {}):
            child, inverse = self._children[model][field]
            predicate = self._child_filters.get(model, {}).get(field)
            return [
                record_id
                for record_id, item in self[child].items()
                if relation_id_optional(item.get(inverse)) == row["id"]
                and (predicate is None or predicate(item))
            ]
        if field in row:
            return row[field]
        if field in self._defaults.get(model, {}):
            return self._defaults[model][field]
        raise AssertionError(f"Missing fake fixture field: {model}({row['id']}).{field}")

    def _project(self, model: str, row: dict[str, object], fields: object) -> dict[str, object]:
        available = (
            set(row) | set(self._defaults.get(model, {})) | set(self._children.get(model, {}))
        )
        selected = available if fields is None else {"id", *_field_names(fields)}
        unknown = selected - self._known_fields(model)
        if unknown:
            raise AssertionError(f"Unknown fake fields for {model}: {sorted(unknown)}")
        return {field: deepcopy(self._field(model, row, field)) for field in selected & available}

    def _matches(self, model: str, row: dict[str, object] | None, domain: list[object]) -> bool:
        position = 0

        def expression() -> bool:
            nonlocal position
            if position == len(domain):
                raise AssertionError("Incomplete fake search domain")
            token = domain[position]
            position += 1
            if token == "!":
                return not expression()
            if token in ("|", "&"):
                left, right = expression(), expression()
                return left or right if token == "|" else left and right
            parts = _sequence(token)
            if len(parts) != 3 or not isinstance(parts[0], str):
                raise AssertionError(f"Unsupported fake domain leaf: {token}")
            operator, expected = parts[1], parts[2]
            if parts[0] not in self._known_fields(model) or operator not in (
                "=",
                "!=",
                "in",
                "not in",
                "<",
                "<=",
                ">",
                ">=",
                "like",
                "ilike",
                "not like",
                "not ilike",
                "=like",
                "=ilike",
            ):
                raise AssertionError(f"Unsupported fake domain leaf: {token}")
            if operator in ("in", "not in"):
                _sequence(expected)
            if operator in {
                "like",
                "ilike",
                "not like",
                "not ilike",
                "=like",
                "=ilike",
            } and not isinstance(expected, str):
                raise AssertionError(f"Invalid fake text domain: {token}")
            if row is None:
                return True
            value = self._field(model, row, parts[0])
            if self._relation_target(model, parts[0]) is not None:
                value = relation_id_optional(value)
                if value is None:
                    value = False
            if operator == "=":
                return value == expected
            if operator == "!=":
                return value != expected
            if operator in {"like", "ilike", "not like", "not ilike", "=like", "=ilike"}:
                if not isinstance(expected, str):
                    raise AssertionError(f"Invalid fake text domain: {token}")
                if value is False or value is None:
                    return str(operator).startswith("not ")
                if not isinstance(value, str):
                    raise AssertionError(f"Invalid fake text domain: {token}")
                template = expected if str(operator).startswith("=") else f"%{expected}%"
                pattern = "".join(
                    ".*" if char == "%" else "." if char == "_" else re.escape(char)
                    for char in template
                )
                found = (
                    re.fullmatch(pattern, value, re.I if "ilike" in str(operator) else 0)
                    is not None
                )
                return not found if str(operator).startswith("not ") else found
            if operator in ("in", "not in"):
                expected_values = _sequence(expected)
                found = (
                    any(item in expected_values for item in cast(Sequence[object], value))
                    if isinstance(value, (list, tuple))
                    else value in expected_values
                )
                return found if operator == "in" else not found
            if operator in ("<", "<=", ">", ">="):
                if isinstance(value, (int, float)) and isinstance(expected, (int, float)):
                    return {
                        "<": value < expected,
                        "<=": value <= expected,
                        ">": value > expected,
                        ">=": value >= expected,
                    }[str(operator)]
                if isinstance(value, str) and isinstance(expected, str):
                    return {
                        "<": value < expected,
                        "<=": value <= expected,
                        ">": value > expected,
                        ">=": value >= expected,
                    }[str(operator)]
            raise AssertionError(f"Unsupported fake domain operator: {operator}")

        results: list[bool] = []
        while position < len(domain):
            results.append(expression())
        return all(results)

    def _create(self, model: str, values: dict[str, object]) -> int:
        record_id = max(self._next_ids.get(model, 1), max(self[model], default=0) + 1)
        self._next_ids[model] = record_id + 1
        self[model][record_id] = {"id": record_id}
        self._write(model, record_id, {**self._defaults.get(model, {}), **values})
        return record_id

    def _write(self, model: str, record_id: int, values: dict[str, object]) -> None:
        if "id" in values or values.keys() - self._known_fields(model):
            raise AssertionError(f"Unknown or immutable fake {model} fields: {values.keys()}")
        row = self[model][record_id]
        for field, value in values.items():
            if field in self._children.get(model, {}):
                child, inverse = self._children[model][field]
                for command in _sequence(value):
                    parts = _sequence(command)
                    if len(parts) != 3:
                        raise AssertionError(f"Unsupported fake child command: {command}")
                    if parts[0] == 0 and parts[1] == 0:
                        self._create(child, {**_values(parts[2]), inverse: record_id})
                    elif parts[0] == 4:
                        self._write(child, _id(parts[1]), {inverse: record_id})
                    elif parts[0] in (1, 2):
                        child_id = _id(parts[1])
                        if relation_id_optional(self[child][child_id].get(inverse)) != record_id:
                            raise AssertionError("Fake child commands cannot edit another parent")
                        if parts[0] == 1:
                            self._write(child, child_id, _values(parts[2]))
                        else:
                            self._unlink(child, child_id)
                    else:
                        raise AssertionError(f"Unsupported fake child command: {command}")
            elif self.fields.get(model, {}).get(field, {}).get("type") == "many2many":
                ids = _ids(row.get(field, []))
                for command in _sequence(value):
                    parts = _sequence(command)
                    if not parts:
                        raise AssertionError(f"Unsupported fake relation command: {command}")
                    if parts[0] == 6 and len(parts) == 3:
                        ids = _ids(parts[2])
                    elif parts[0] == 5 and len(parts) == 3:
                        ids = []
                    elif parts[0] in (3, 4) and len(parts) == 3:
                        linked = _id(parts[1])
                        if parts[0] == 3:
                            ids = [item for item in ids if item != linked]
                        elif linked not in ids:
                            ids.append(linked)
                    else:
                        raise AssertionError(f"Unsupported fake relation command: {command}")
                row[field] = ids
            elif (target := self._relation_target(model, field)) is not None:
                related_id = relation_id_optional(value)
                if related_id is None:
                    row[field] = False
                    continue
                related = self[target][related_id]
                if isinstance(value, int):
                    label = next(
                        (related[name] for name in self._label_fields if name in related),
                        None,
                    )
                    if not isinstance(label, str):
                        raise AssertionError(
                            f"Supply a native relation label for {target}({related_id})"
                        )
                    row[field] = [related_id, label]
                else:
                    row[field] = deepcopy(value)
            else:
                row[field] = deepcopy(value)

    def _unlink(self, model: str, record_id: int) -> None:
        del self[model][record_id]
        children = set(self._children.get(model, {}).values()) | set(self._cascades.get(model, ()))
        for child, inverse in children:
            for child_id in list(self[child]):
                if (
                    child_id in self[child]
                    and relation_id_optional(self[child][child_id].get(inverse)) == record_id
                ):
                    self._unlink(child, child_id)

    def _relation_target(self, model: str, field: str) -> str | None:
        definition = self.fields.get(model, {}).get(field, {})
        if definition.get("type") == "many2many":
            return None
        relation = definition.get("relation")
        if isinstance(relation, str):
            return relation
        if field in self._relations.get(model, {}):
            return self._relations[model][field]
        for parent, links in self._children.items():
            if (model, field) in links.values():
                return parent
        return None


def _record_tables(
    records: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, dict[int, dict[str, object]]]:
    tables: dict[str, dict[int, dict[str, object]]] = {}
    for model, rows in records.items():
        table = tables.setdefault(model, {})
        for row in rows:
            record_id = _id(row["id"])
            if record_id in table:
                raise ValueError(f"Duplicate fake record: {model}({record_id})")
            table[record_id] = deepcopy(dict(row))
    return tables


def _id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AssertionError(f"Expected a positive fake record ID: {value!r}")
    return value


def _sequence(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise AssertionError(f"Expected a fake RPC sequence: {value!r}")
    return list(cast(Sequence[object], value))


def _ids(value: object) -> list[int]:
    return [_id(item) for item in _sequence(value)]


def _field_names(value: object) -> list[str]:
    fields = _sequence(value)
    if any(not isinstance(field, str) for field in fields):
        raise AssertionError(f"Expected fake RPC field names: {value!r}")
    return cast(list[str], fields)


def _values(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AssertionError(f"Expected fake RPC field values: {value!r}")
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            raise AssertionError(f"Expected fake RPC field names: {key!r}")
        result[key] = item
    return result
