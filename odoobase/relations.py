"""Native relation parsing and reference-integrity checks; no cache dependency."""

from collections.abc import Mapping, Sequence
from typing import cast


def relation_id_optional(value: object, *, context: str = "Odoo relation") -> int | None:
    """Parse a positive native ID or many2one pair; only None/False are absent."""
    if value is None or value is False:
        return None
    record_id = value
    if isinstance(value, (list, tuple)):
        relation = cast(Sequence[object], value)
        if len(relation) != 2 or not isinstance(relation[1], str):
            raise ValueError(f"Invalid {context} {value!r}: expected [ID, label]")
        record_id = relation[0]
    if isinstance(record_id, int) and not isinstance(record_id, bool) and record_id > 0:
        return record_id
    raise ValueError(f"Invalid {context} {value!r}: expected a positive integer ID")


def relation_id_required(value: object, *, context: str = "Odoo relation") -> int:
    """Parse a required native relation without coercing malformed IDs."""
    record_id = relation_id_optional(value, context=context)
    if record_id is None:
        raise ValueError(f"{context}: a native Odoo relation ID is required")
    return record_id


def optional_record[Row](index: Mapping[int, Row], record_id: int | None) -> Row | None:
    """Preserve absent relations; reject invalid or dangling native references."""
    if record_id is None:
        return None
    if isinstance(record_id, bool) or record_id <= 0:
        raise ValueError(f"Invalid Odoo record ID {record_id!r}")
    try:
        return index[record_id]
    except KeyError:
        raise ValueError(f"Referenced Odoo record ID {record_id} is missing") from None


def required_record[Row](
    index: Mapping[int, Row],
    record_id: int | None,
    *,
    context: str,
) -> Row:
    record = optional_record(index, record_id)
    if record is None:
        raise ValueError(f"{context} requires a native Odoo record ID")
    return record
