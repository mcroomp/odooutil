# odoobase

Typed live Odoo infrastructure, independent of applications and local caches.
Requires Python 3.14.10+ and Pydantic 2. Development uses uv and pins Python 3.14.10.

- `odoobase`: connection factory, RPC protocols and dynamic record validation.
- `odoobase.models.v19_4`: Odoo 19.4 accounting, partner, analytic, product,
  attachment and budget TypedDicts with standard fields only.
- `odoobase.relations`: strict Many2one parsing and reference-integrity checks.
- `odoobase.rpc`: typed RPC client and common operational validation helpers.
- `odoobase.testing`: stateful, schema-configurable fake RPC transport.

```python
from odoobase import get_odoo_models
from odoobase.models.v19_4 import RawPartner as BaseRawPartner
from odoobase.rpc import RpcClient


class Partner(BaseRawPartner, total=False):
    x_employee_status: str


client = RpcClient(*get_odoo_models())
partners = client.search("res.partner", [], ["id", "name"], limit=20)
```

The example performs a live read only when executed. Configure all four
`ODOO_USERNAME`, `ODOO_API_KEY`, `ODOO_URL`, `ODOO_DB` environment variables,
or pass an explicit four-line `key_file` (username/key/HTTPS origin/database)
to `get_odoo_models`. No implicit file or application data path is consulted.
Use `rpc_protocol="jsonrpc"` for native report APIs whose results contain nulls;
XML-RPC remains the default. Authentication and malformed responses raise.
Unknown mutation outcomes must be investigated, never blindly retried.

Native record types describe projections: only `id` is universally required.
Fields supplied by optional Odoo modules are not guaranteed on every database.
No field inventories are fetched automatically, and localization-specific fields
belong downstream. Decorated contracts preserve extra columns under strict
Pydantic validation, including custom and dynamic analytic-plan columns.
Types are not normalized business models or automatic ORM field proxies.
Different Odoo versions can have different native contracts. Import the explicit
version module in applications; future model sets will live alongside `v19_4`,
not change its classes. The `odoobase.models` namespace has no unversioned model
exports. Only 19.4 is provided currently.

For version-matrix tests, use an explicit selector as the local `models` alias:

```python
import pytest

from odoobase.models import SUPPORTED_VERSIONS, for_version
from odooutil.testing import FakeOdooTransport, FakeSchema


@pytest.fixture(params=SUPPORTED_VERSIONS)
def server(request: pytest.FixtureRequest) -> FakeOdooTransport:
    models = for_version(request.param)
    schema = FakeSchema.from_models(models.MODELS, odoo_version=models.ODOO_VERSION)
    return FakeOdooTransport(schema=schema)
```

`for_version("19.4")` selects the same model inventory explicitly. Unsupported
versions raise rather than falling back; no global alias or environment state is
changed. Each version module exposes a read-only `MODELS` inventory keyed by
native Odoo model name. Tests can pass a subset or overlay matching,
version-specific extended TypedDicts before calling `from_models`. Production
type imports remain explicit: selecting a fake's model set does not rebase an
application's already-defined TypedDict extensions onto another Odoo version.

`relation_id_optional` accepts positive integer IDs and `[ID, label]`/tuple
pairs. Only `None` and `False` mean absence. Booleans as IDs, numeric strings,
nonpositive IDs and malformed pairs raise. `relation_id_required` additionally
rejects absence. `optional_record`/`required_record` separately reject dangling
references in a supplied index.

The library does not configure logging handlers, hold a global connection,
cache records or manage application files. Native `x_*` schemas, model-fetch
policies, business logic and snapshot migrations remain the consumer's concern.

## Testing without Odoo

`FakeOdooTransport` implements the same RPC protocol as a live connection, without
network access, credential loading, pytest dependencies or application snapshots:

```python
from odoobase.rpc import RpcClient
from odoobase.testing import FakeOdooTransport

server = FakeOdooTransport(
    records={"res.partner": [{"id": 7, "name": "Example"}]},
    fields={"res.partner": {"name": {"type": "char"}}},
)
client = RpcClient(*server.connection())
assert client.search("res.partner", [], ["name"]) == [{"id": 7, "name": "Example"}]
```

For a suite, define model configuration once and keep individual tests about data:

```python
from odoobase.models.v19_4 import ODOO_VERSION, RawPartner
from odooutil.testing import FakeOdooTransport, FakeSchema


class Partner(RawPartner, total=False):
    x_reference: str


PARTNER_SCHEMA = FakeSchema.from_models({"res.partner": Partner}, odoo_version=ODOO_VERSION)
server = FakeOdooTransport(schema=PARTNER_SCHEMA)
```

`from_models` derives allowed field names from native TypedDicts, including
inherited and application-specific fields, and configures empty model tables.
Normal tests only provide their records; they do not repeat field inventories.
It does not guess relation targets or Odoo field types from Python annotations:
those annotations cannot distinguish, for example, text from a date string.
Supply explicit metadata only for tests that exercise `fields_get`, relation
targets not already configured by the application, or server-side effects.
Schemas carry their declared Odoo version; composing two different declared
versions raises. Unversioned scenario overrides inherit the application version.
This is fixture configuration, not automatic live-server version detection.

`FakeSchema` groups field metadata, defaults, relations and owned-child rules.
Compose application/scenario definitions with `base.extend(overrides)`. Model
entries are merged by field name (an overriding field definition replaces that
field's metadata), known field names and cascades are unioned, and a supplied label
lookup replaces the prior lookup. Direct constructor options remain supported as
per-test overrides. Each transport copies configuration and records: changing a
test's fake does not change the reusable schema or another fake.

Records and RPC results are deep-copied; changing the fake does not change the
input fixture. Access `server["model"]` to inspect or arrange remote state, and
`server.calls` for recorded `RpcCall` values. `server.connection` also implements
`ConnectionProvider` for utility entrypoints. It accepts their connection options
without reading key files or contacting URLs, and checks an explicit target
database against the fixture's database.

The same API is available from `odooutil.testing`; the implementation stays in
`odoobase` to preserve the one-way package dependency.

Configure empty tables with `records={"model": []}`. Known fields come only from
records, `fields` metadata, `defaults` and `children`; no application schemas are
imported or guessed. Supply metadata for fields written into empty tables.
Alternatively, `known_fields={"model": ["field"]}` declares projection/write
names without inventing `fields_get` metadata; the public `known_fields` sets
can also be extended with dynamically discovered columns.
Many2one metadata uses `{"type": "many2one", "relation": "target.model"}`. Integer
relation writes resolve labels from the target's `display_name` or `name`; native
`[id, label]` writes retain their supplied labels. Referenced targets must exist.
`relations={"model": {"field_id": "target.model"}}` configures relation targets
separately from field metadata. `label_fields=("display_name", "title")` selects
the ordered label lookup for integer relation writes.

`seed_records({"model": [...]})` merges a fixture baseline once. Prearranged
remote rows override baseline fields, explicit deletions remain deleted, and
allocated IDs, hooks, counters and queued failures/replies are preserved.
Inputs are copied and invalid/duplicate IDs fail before replacing state.
Snapshot loading, cross-file conflict detection and model naming stay downstream.

Supported RPC methods are `read`, `search`, `search_read`, `search_count`,
`fields_get`, `create`, `write` and `unlink`. Searches support prefix `!`, `&`, `|`
and implicit AND; equality, membership, ordered and like/ilike comparisons;
sorting, offset/limit, projections and `active_test=False`. Other context values
are recorded but do not emulate Odoo behavior.

Many2many metadata enables commands 3/4/5/6 (unlink/link/clear/set).
`children={"parent": {"child_ids": ("child", "parent_id")}}` declares **owned**
children: reads compute inverse links, writes support commands 0/1/2/4, and
deleting a parent cascades through those links. Declare only relationships that
should cascade; this is not automatic ORM/ondelete inference.
`child_filters={"parent": {"child_ids": predicate}}` filters a particular child
projection without changing its owning relationship or deletion behavior.
`cascades={"parent": [("child", "parent_id")]}` adds owned deletion links that
should not introduce a projected child field.

Use `before()`/`after()` for scenario effects, `on()` for custom RPC methods,
`transform_reply()` to alter replies, or one-shot `reply_next()`/`fail_next()`.
Effects cannot replace the generic CRUD/query handlers. Failed RPCs roll back
records, field metadata and ID allocation. `fail_next(..., after=True)` instead
simulates a committed mutation with a lost reply. Optional `ids=` restricts
one-shot failures/replies to an exact ID batch.

This is a strict, bounded contract fake, not a full Odoo ORM or accounting engine:
unknown models/fields/methods and unsupported commands fail explicitly.
Business workflows and application fixture/cache adapters belong downstream.
DEBUG logs include call metadata and result counts, not query/record values,
credentials, context or exception messages.
