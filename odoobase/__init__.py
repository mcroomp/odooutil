"""Typed live Odoo infrastructure without application state or caching."""

from odoobase.transport import (
    RpcConnection as RpcConnection,
)
from odoobase.transport import (
    RpcModels as RpcModels,
)
from odoobase.transport import (
    RpcRecord as RpcRecord,
)
from odoobase.transport import (
    execute_kw_none_ok as execute_kw_none_ok,
)
from odoobase.transport import (
    get_odoo_models as get_odoo_models,
)
from odoobase.transport import (
    record_dict as record_dict,
)
from odoobase.transport import (
    record_list as record_list,
)
from odoobase.transport import (
    search_read as search_read,
)
