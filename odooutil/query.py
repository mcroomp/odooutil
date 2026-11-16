"""Run bounded, read-only queries against Odoo and print JSON results."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from odoobase import get_odoo_models
from odoobase.rpc import MODEL_NAME_PATTERN

MAX_ROWS = 1000
DEFAULT_FIELD_ATTRIBUTES = ["string", "type", "relation", "required", "readonly"]


class QueryArgs(argparse.Namespace):
    key_file: Path | None
    target_url: str | None
    target_db: str | None
    command: str
    model: str
    domain: list[object]
    fields: list[str]
    limit: int
    offset: int
    order: str | None
    attributes: list[str]


def _domain(value: str) -> list[object]:
    try:
        domain = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"domain must be valid JSON: {exc.msg}") from exc
    if not isinstance(domain, list):
        raise argparse.ArgumentTypeError("domain must be a JSON array")
    return cast(list[object], domain)


def _model_name(value: str) -> str:
    if not MODEL_NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "model must contain only letters, numbers, underscores, and dots"
        )
    return value


def _row_limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= MAX_ROWS:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_ROWS}")
    return limit


def parse_args(argv: Sequence[str] | None = None) -> QueryArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--target-url")
    parser.add_argument("--target-db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search-read", help="Search for and read records")
    search_parser.add_argument("model", type=_model_name)
    search_parser.add_argument("--domain", type=_domain, default=[])
    search_parser.add_argument("--fields", nargs="+", required=True)
    search_parser.add_argument("--limit", type=_row_limit, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--order")

    count_parser = subparsers.add_parser("search-count", help="Count matching records")
    count_parser.add_argument("model", type=_model_name)
    count_parser.add_argument("--domain", type=_domain, default=[])

    fields_parser = subparsers.add_parser("fields-get", help="Describe model fields")
    fields_parser.add_argument("model", type=_model_name)
    fields_parser.add_argument(
        "--attributes",
        nargs="+",
        default=DEFAULT_FIELD_ATTRIBUTES,
    )

    args = parser.parse_args(argv, namespace=QueryArgs())
    if (args.target_url is None) != (args.target_db is None):
        parser.error("--target-url and --target-db must be provided together")
    if getattr(args, "offset", 0) < 0:
        parser.error("--offset must be zero or greater")
    return args


def execute_query(args: QueryArgs) -> object:
    models, db, uid, password = get_odoo_models(
        key_file=args.key_file,
        target_url=args.target_url,
        target_db=args.target_db,
    )
    if args.command == "search-read":
        kwargs: dict[str, object] = {
            "fields": args.fields,
            "limit": args.limit,
            "offset": args.offset,
        }
        if args.order:
            kwargs["order"] = args.order
        return models.execute_kw(
            db,
            uid,
            password,
            args.model,
            "search_read",
            [args.domain],
            kwargs,
        )
    if args.command == "search-count":
        return models.execute_kw(
            db,
            uid,
            password,
            args.model,
            "search_count",
            [args.domain],
        )
    if args.command == "fields-get":
        return models.execute_kw(
            db,
            uid,
            password,
            args.model,
            "fields_get",
            [],
            {"attributes": args.attributes},
        )
    raise ValueError(f"Unsupported read-only command: {args.command}")


def main(argv: Sequence[str] | None = None) -> None:
    result = execute_query(parse_args(argv))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
