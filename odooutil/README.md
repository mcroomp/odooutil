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
| Private attachments on existing records | `attachment-upload` |

Schema specs use native Odoo names. Manual model/field names require `x_`.
Model creation needs explicit `access` rules with name, positive verified
`group_id`, and boolean `perm_read`, `perm_write`, `perm_create`, `perm_unlink`.
Every rule needs read permission; at least one needs create and write.
Inherited-view specs use `inherit_id` and `arch_db`; primary views are limited
to manual models. Module-owned views and built-in fields remain protected.
Field type/relation migrations and removing selection values are refused.
Deletion requires `--allow-data-loss` and passes dependency checks first.
Action creation checks code syntax and group access but never executes the action.

### One2many fields

Create the child's Many2one field first, then expose its inverse on the parent:

```json
{
  "ttype": "one2many",
  "field_description": "Items",
  "relation": "x_example_item",
  "relation_field": "x_parent_id"
}
```

Use this spec with `field-create x_example_parent x_item_ids --spec FILE`.
The inverse must already be a stored, non-related, non-computed Many2one on
`x_example_item` pointing to `x_example_parent`. Its name need not start with
`x_` when using an existing standard-model field. These checks run during
preview and again before applying; readback verifies the saved inverse name.
Changing the related model or inverse field requires an explicit migration.
`on_delete` belongs on the child's Many2one, not the One2many.
Deleting a Many2one still referenced by a One2many is refused.

After creating the field, add it to a form tab through a separate view operation,
optionally with an embedded editable list. Schema and view operations are not
atomic; a failed view change leaves the field in place. This provisions schema
only: x2many record-value writes remain unsupported by the record commands.

### Related fields

All supported field types may be related, including scalar, selection, monetary,
Many2one, One2many and Many2many fields. Explicitly choose `store: true` or
`store: false`; related fields must be read-only, non-copyable and not required
as inputs:

```json
{
  "ttype": "many2one",
  "field_description": "Owner",
  "relation": "res.partner",
  "related": "x_parent_id.x_owner_id",
  "readonly": true,
  "store": true,
  "copied": false,
  "required": false,
  "on_delete": "restrict"
}
```

The related path must contain 1-8 fields, traverse only Many2one relations,
and end in the declared type and, for relational fields, related model.
One2many and Many2many endpoints are allowed, but traversing a collection
in the middle of a path is refused: related fields do not aggregate collections.
Preview and pre-write rechecks
validate every hop; readback verifies the requested settings. Changing an
existing field's path, storage mode, type, or related model requires an explicit
migration. Related selection choices come from the target and cannot be
overridden. Related One2many fields require the target's `relation_field`;
it points back to the target field's owner, not necessarily the new field's owner.
Stored Many2many fields require explicit unique `relation_table`, `column1` and
`column2` settings, even when related. Non-stored related Many2many fields must
omit those table settings.

### Monetary fields and currencies

Create a currency relation on the amount's model first:

```json
{
  "ttype": "many2one",
  "field_description": "Currency",
  "relation": "res.currency",
  "required": true,
  "on_delete": "restrict"
}
```

For example, use it with `field-create x_example_item x_currency_id --spec FILE`.
Then create the amount with:

```json
{
  "ttype": "monetary",
  "field_description": "Amount",
  "currency_field": "x_currency_id",
  "required": true
}
```

`currency_field` must explicitly name an existing Many2one to `res.currency` on
the same model. Standard names such as `currency_id` and custom names are both
supported; no currency is guessed or created. Related monetary fields also need
this local currency relation, which can itself be related. Ensure it represents
the same currency as the source amount. Changing the currency-field binding
requires an explicit migration; deleting a referenced currency field is refused.
These checks are repeated before writing and the saved binding is verified.

A monetary value is a JSON number, not a formatted string or an amount/currency
object. Use record create, update and batch-update commands with the amount and,
when necessary, an existing currency record ID. Currency records use native
`res.currency` IDs, not ISO-code strings. Odoo supplies currency precision,
rounding and display symbols. The utility sends finite numbers unchanged and
verifies Odoo's readback; it does not convert currencies, fetch exchange rates,
or silently round to make a mismatch pass. Supply an amount already valid for
the selected currency. A readback mismatch retains a recovery journal.
Read-only queries preserve numeric amounts, and default `fields-get` metadata
includes `currency_field` so consumers can discover the binding. Native report
specs also accept finite monetary values in writable numeric settings.

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
remain read-only, stored and non-copyable. The computed name must not have
`required: true`: manual computations are not
precomputed, so Odoo can reject creation before generating the name. Make the
source fields required instead. Python syntax is checked locally; Odoo validates
its safe-eval restrictions when saving. The code runs in Odoo
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

### Original attachments

`attachment-upload MODEL ID --file FILE` previews a private binary attachment
on an existing, accessible record. It requires the native writable `ir.attachment.raw`
field and a nonempty local file; there is no spec or public-upload option.

```bash
odoo-schema --target-url https://example.odoo.com --target-db example \
  attachment-upload res.partner 123 --file ./evidence.pdf
```

After reviewing the preview, add `--apply --confirm-db example --journal NEW_FILE`.
The command rechecks the target (including `write_date` when available), file
digests and duplicate scope immediately before applying. Matching checksums are
scoped to ordinary attachments on that target, not other records or binary fields.
One existing private match produces a verified `reuse` with no write and retains
its filename. Multiple matches or a public match require review.

New uploads use the original basename, `public: false` and native
`image_no_postprocess` context to avoid image resizing. Readback verifies the
target, filename, privacy, size, native SHA-1 checksum and exact original bytes.
Both native binary readback forms (base64 text and an object containing `content`)
are supported. Previews and private journals contain metadata and digests, never
the file bytes or base64 payload.

Quiet mode is the default, with the same automation acknowledgement and linked
chatter/mail audits as record changes. Audits concern the existing target, not
the new attachment ID. Creating a parent record and attaching a file are separate
transactions; this command neither creates the parent nor changes its schema or
primary-attachment selection. A failed/uncertain upload retains a review journal:
inspect live state before retrying; no automatic retry or rollback is attempted.
Source files and target records are not locked against concurrent changes.

### Record values

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

#### Primary attachment

`record-set MODEL ID` can explicitly designate an existing private binary
attachment as the native `message_main_attachment_id`:

```json
{
  "values": {"message_main_attachment_id": 42},
  "expected": {"message_main_attachment_id": false}
}
```

An explicit expected primary value is mandatory: use `false` only when no primary
is set, or the reviewed current attachment ID when intentionally replacing it.
The attachment must already belong to exactly that model and record, must not be
public, and must not back a binary field. Foreign, missing and URL attachments are
refused. The native relation must be writable and stored. Creation, batch
assignment and clearing the primary are not supported; other system/chatter
fields remain protected.

Previews and journals include attachment metadata, not content. Apply rechecks
the target, expected primary and attachment metadata immediately before writing,
then verifies the saved relation and unchanged attachment metadata. An already
selected attachment produces a verified no-op. Normal target confirmation,
exclusive journals, automation checks and quiet chatter audits still apply.
This only selects an existing attachment; it does not upload, move, publish,
rename or delete files. Checks are not atomic against concurrent changes.

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
