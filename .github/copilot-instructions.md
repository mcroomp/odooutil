# Contributor rules

- Use uv for Python 3.14.10 (pinned in `.python-version`), dependencies, the
  project `.venv` and the committed `uv.lock`.
  Never commit credentials, private specs or recovery journals.
- `odoobase` is the lower layer: standard native types, validation and RPC.
  It must not import `odooutil` or application packages.
  Import Linter enforces this boundary, including type-checking imports.
- Keep organization-specific fields, custom models, deployment configuration,
  caching and snapshot migrations outside both packages.
- Preserve native records and false/null relation semantics. Extend base
  TypedDicts downstream; do not flatten, coerce or silently discard unknown fields.
- Utilities operate live, preview by default and retain explicit targets,
  database confirmation, exclusive private journals and readback verification.
  Never bypass safety refusals or retry uncertain mutations automatically.
- Keep all maintained Python, including tests, under strict Pyright, Ruff lint
  and Ruff formatting. No blanket suppressions or untyped escape hatches.
- Test with synthetic inputs and mocked transports only. Validation must not
  authenticate against or mutate a live Odoo instance.
- Both top-level Python packages ship together as one `odooutil` distribution.
- Before review, run the checks documented in the root README and build the
  distribution. Keep changes and documentation focused on the request.
