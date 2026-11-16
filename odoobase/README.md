# odoobase

Typed live Odoo infrastructure, independent of applications and local caches.
Requires Python 3.14.10+ and Pydantic 2. Development uses uv and pins Python 3.14.10.

- `odoobase`: connection factory, RPC protocols and dynamic record validation.
- `odoobase.models`: native accounting, partner, analytic, product, attachment
  and budget TypedDicts with standard fields only.
- `odoobase.relations`: strict Many2one parsing and reference-integrity checks.
- `odoobase.rpc`: typed RPC client and common operational validation helpers.

```python
from odoobase import get_odoo_models
from odoobase.models import RawPartner as BaseRawPartner
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

`relation_id_optional` accepts positive integer IDs and `[ID, label]`/tuple
pairs. Only `None` and `False` mean absence. Booleans as IDs, numeric strings,
nonpositive IDs and malformed pairs raise. `relation_id_required` additionally
rejects absence. `optional_record`/`required_record` separately reject dangling
references in a supplied index.

The library does not configure logging handlers, hold a global connection,
cache records or manage application files. Native `x_*` schemas, model-fetch
policies, business logic and snapshot migrations remain the consumer's concern.
