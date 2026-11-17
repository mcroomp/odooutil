# odooutil

Live query, schema and financial-report CLIs built on `odoobase`.
No caching, snapshot files or organization-specific definitions are included.

## Application integration

Each module's `main(argv=None, *, connection_provider=None)` accepts a typed
`odoobase.ConnectionProvider`. `query.execute_query` accepts the same keyword.
An application can supply its credential/configuration boundary without changing
arguments, monkeypatching globals or duplicating utility orchestration:

```python
from odooutil.schema import main as schema_main
from my_application.odoo import get_odoo_models


def main(argv=None):
    schema_main(argv, connection_provider=get_odoo_models)
```

The provider receives parsed `key_file`, `target_url` and `target_db` keyword
arguments and returns an `odoobase.RpcConnection`. Reports additionally request
`rpc_protocol="jsonrpc"`. Preserve these explicit target and protocol selections.
Without a provider, the ordinary environment/explicit-file factory is used.
Provider errors propagate without fallback or retry. Parsing, target/database
confirmation, journals and verification remain the utility's responsibility.

## Query

```bash
odoo-query fields-get res.partner
odoo-query search-count res.partner --domain '[["active", "=", true]]'
odoo-query search-read res.partner --fields id name --limit 20 --offset 0
```

Reads require explicit fields, default to 20 records and are capped at 1000 per
page. JSON domains, nonnegative offsets and paired target overrides are validated
before connecting. `--target-url` and `--target-db` precede the subcommand.

## Schema and record values

```bash
odoo-schema --target-url https://example.odoo.com --target-db example \
  field-create res.partner x_reference --spec field.json
```

Example `field.json`:

```json
{"ttype": "char", "field_description": "External reference"}
```

This only previews. After reviewing it, append:

```bash
--apply --confirm-db example --journal private/new-journal.json
```

The journal parent must already exist; the file must not. Journals are created
exclusively with owner-only permissions on POSIX. The utility rechecks the plan
before writing and verifies saved values afterwards. These are client-side
checks, not an atomic compare-and-swap or a multi-request database transaction.

Supported commands:

| Resource | Commands |
| --- | --- |
| Manual models with explicit group ACLs | `model-create`, `model-read`, `model-enable-chatter` |
| Manual fields | `field-create`, `field-read`, `field-update`, `field-delete` |
| Inherited views and manual-model form/list views | `view-create`, `view-read`, `view-update`, `view-delete` |
| Contextual server-action definitions | `action-create`, `action-read` |
| New scalar/Many2one/JSON records | `record-create` |
| Existing scalar/Many2one/JSON record values | `record-set`, `record-set-batch` |

Schema specs use native Odoo names. Manual model/field names require `x_`.
Model creation needs explicit `access` rules with name, positive verified
`group_id`, and boolean `perm_read`, `perm_write`, `perm_create`, `perm_unlink`.
Every rule needs read permission; at least one needs create and write.
Inherited-view specs use `inherit_id` and `arch_db`; primary views are limited
to manual models. Module-owned views and built-in fields remain protected.
Field type/relation migrations and removing selection values are refused.
Deletion requires `--allow-data-loss` and passes dependency checks first.
Action creation checks code syntax and group access but never executes the action.

An existing `x_name` char field on an empty manual `x_` model can be made a
stored computed display name with `field-update`. Supply `compute` (reviewed
Python code), `depends` (comma-separated field paths), `store: true`,
`readonly: true` and `copied: false`. For example:

```json
{
  "compute": "for record in self:\n    record['x_name'] = '%s - %s' % (record['x_date'], record['x_partner_id'].name)",
  "depends": "x_date,x_partner_id.name",
  "store": true,
  "readonly": true,
  "copied": false
}
```

Dependency paths are checked against accessible fields and may traverse only
Many2one relations. Self-dependencies, computation changes and conversions of
populated models (including archived records) are refused. Existing computations
remain read-only, stored and non-copyable. Python syntax is checked locally;
Odoo validates its safe-eval restrictions when saving. The code runs in Odoo
when the field is computed; review it before applying. This does not install or
require an automation module. Native dependency recomputation handles changes to
the referenced fields. Other computed fields remain outside this utility's scope.

`model-enable-chatter MODEL` enables chatter only on an existing manual `x_`
model, without a spec. It previews by default and uses the same apply,
database-confirmation and exclusive-journal requirements. This affects all
records of the model and Odoo does not permit disabling it afterwards.
Activities and access rules are not changed. Readback verifies the model flag,
unchanged model settings and the native message/follower relation fields.
Adding `<chatter/>` to the form is a separate `view-create` operation using an
inherited view; separate operations are not atomic. If the view step fails,
the model remains chatter-enabled. Inspect the journals before taking further
action; do not retry an uncertain mutation.

Record specs contain `values` and optional recommended `expected` guards:

```json
{"values": {"x_reference": "external-123"}, "expected": {"x_reference": false}}
```

`record-create MODEL --spec FILE` creates one record. Its spec accepts only
nonempty `values`, not `expected` guards:

```json
{"values": {"name": "Example partner", "x_reference": "external-123"}}
```

It previews without creating anything. Applying uses the same `--apply`,
`--confirm-db` and new private `--journal` requirements. Omitted fields use
Odoo's server defaults; required fields without defaults must be supplied.
The journal records the returned ID and readback verifies the supplied values.
Creation is not an upsert and does not deduplicate records: after an uncertain
outcome, inspect the journal and live state instead of retrying.

`record-set MODEL ID` updates one record. `record-set-batch MODEL ID...` assigns
identical values to 1-1000 unique positive IDs with one write. Writable scalar
fields, Many2one IDs and JSON objects are supported for both creation and updates;
binary, x2many, readonly,
non-stored, related and system/chatter fields are refused. Dates use YYYY-MM-DD;
datetimes use YYYY-MM-DD HH:MM:SS in UTC. Use native `false`, not null, to clear
optional values. `--lang CODE` selects an installed language for record mutations.

Writable Reference fields accept `model,positive_id` strings or `false`. The
model must appear in the field's native selection and the target record must
exist and be accessible, including at the apply recheck. For example, an
`ir.ui.menu` record can link its `action` to an existing window action with
`"action": "ir.actions.act_window,123"`. Menu `parent_id` uses the ordinary
Many2one ID; model ACLs still govern access to the linked records.

Quiet chatter is the default, not a bypass for server hooks. Installed automation
requires explicit reviewed `--allow-automation`; `--chatter normal` opts into
normal tracking. Linked chatter/mail is audited where supported (including on
the new record after creation), with bounded reads; audits cannot prove the
absence of external effects. No-op record updates
are journaled without a write. Failures retain `requires_review` journals and do
not trigger automatic destructive rollback or retry.

## Financial reports

```bash
odoo-report --target-url https://example.odoo.com --target-db example \
  --spec report.json
```

A report spec contains `report`, nonempty `columns` and nested `lines`; optional
`menu` publishes a client action and menu beneath an explicit parent ID.
For example:

```json
{
  "report": {"name": "Example"},
  "columns": [{"name": "Balance", "expression_label": "balance"}],
  "lines": [{
    "code": "INCOME",
    "name": "Income",
    "expressions": [{
      "label": "balance", "engine": "account_codes", "formula": "-7"
    }]
  }]
}
```

Supported engines are `account_codes`, literal-list `domain` and `aggregation`.
Aggregation references use `LINE_CODE.expression_label`; undefined references
and cycles fail before writes. IDs/ownership are managed, and settings are
validated against live metadata. Existing line/expression/column identities are
preserved. Use `--report-id ID` when renaming an existing report.

Applying requires `--apply --confirm-db DATABASE --journal NEW_FILE`. Removing
omitted configuration additionally requires `--allow-delete`. Verification
includes readback and native rendering through JSON-RPC. A failed new report
attempts cleanup of confirmed newly created records only; uncertain creates
retain `requires_review`, and cleanup failures retain `rollback_failed`.
Existing report updates are not destructively rolled back. Separate RPC calls
are not a single transaction: inspect journals and live state before retrying.

Credentials use environment variables or an explicit `--key-file`; see the
[repository guide](../README.md). Never commit credentials, private specs or
recovery journals. CLI commands are also available as `python -m odooutil.NAME`;
`python -m odooutil` defaults to schema administration.

## Sensitive output

Preview and apply JSON output includes supplied values and readback data; journals
also retain plans, before/after values and record identifiers. Treat this output
as private, including terminal scrollback and redirected files. Do not send it to
shared CI logs or commit it. Journal contents are not encrypted; owner-only POSIX
permissions do not protect copied files or other platforms.

Schema failure logs include operation metadata, identifiers, journal paths and
the exception type, not exception messages or tracebacks. Original exceptions
still propagate to callers, so unhandled CLI errors and server errors can contain
record data. Protect stderr as well as stdout. Quiet chatter is not a privacy
boundary: server hooks and acknowledged automation can still send notifications
or disclose supplied values.
