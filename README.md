# odoobase and odooutil

Python helpers for working with Odoo. Both packages ship as `odooutil`:

| Package | Responsibility |
| --- | --- |
| [odoobase](odoobase/) | Typed records, relation validation and XML-RPC/JSON-RPC |
| [odooutil](odooutil/) | Queries, schema and record changes, and financial reports |

The packages work with live Odoo data, without local caches or snapshots.
Importing them does not open a connection.

## Development

Use [uv](https://docs.astral.sh/uv/) to install and run Python 3.14.10,
pinned in [.python-version](.python-version):

```bash
uv python install
uv sync --locked --group dev
uv run --locked --group dev ruff check .
uv run --locked --group dev ruff format --check .
uv run --locked --group dev pyright
uv run --locked --group dev lint-imports --no-cache
uv run --locked --group dev pytest
uv build
```

uv manages `.venv` and `uv.lock`. CI installs the pinned Python version with
`actions/setup-python`, then uses uv to run the same checks and build the
distribution. Tests use synthetic inputs and mocked RPC, not a live Odoo instance.

Import Linter enforces the one-way dependency `odooutil -> odoobase`, including
type-checking imports. The base must never depend on the utilities.

## Usage

Set `ODOO_USERNAME`, `ODOO_API_KEY`, `ODOO_URL` and `ODOO_DB` outside source
control. The URL must be an HTTPS origin. Alternatively pass `--key-file FILE`:
four nonempty UTF-8 lines containing username, API key, URL and database.
An explicit file takes precedence over environment credentials. Credential files
are not discovered automatically.

```bash
uv run --locked odoo-query fields-get res.partner
uv run --locked odoo-query search-read res.partner --fields id name --limit 20
uv run --locked odoo-schema --help
uv run --locked odoo-report --help
```

Read-only queries require explicit fields, default to 20 records and cap each
page at 1000. Use `--offset` for pagination and discover fields before querying.

## Making changes

See the [utility guide](odooutil/README.md) before applying changes.
Previews are the default. Writes require an explicit HTTPS target and database,
matching confirmation and a new private recovery journal. Specs and journals
may contain sensitive record data: keep them outside version control.

Available features depend on the Odoo version, installed modules and permissions.
Financial reports require the native accounting-report models.

## Distribution

Build with `uv build`. The wheel includes both packages and the command-line
tools. Applications can extend the native TypedDicts in `odoobase`.

## License

This project is licensed under the [MIT License](LICENSE).
