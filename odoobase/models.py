"""Standard native Odoo projections; extend these TypedDicts downstream.

Only IDs are universally required. Optional modules supply some models/fields;
these contracts do not imply their presence in every Odoo installation.
Unknown columns are preserved by runtime validation, not stripped.
"""

from typing import Literal, Required, TypedDict

from pydantic import ConfigDict, with_config

type Relation = int | str | list[int | str] | tuple[int | str, ...] | Literal[False] | None


@with_config(ConfigDict(strict=True, extra="allow"))
class RawMoveLine(TypedDict, total=False):
    """account.move.line."""

    id: Required[int]
    move_id: Relation
    account_id: Relation
    tax_line_id: Relation
    company_id: Relation
    journal_id: Relation
    product_id: Relation
    partner_id: Relation
    asset_ids: list[int]
    tax_ids: list[int]
    analytic_distribution: dict[str, float] | Literal[False]
    display_type: str
    parent_state: str
    date: str
    name: str | Literal[False]
    ref: str | Literal[False]
    write_date: str | Literal[False]
    balance: float
    debit: float
    credit: float
    tax_base_amount: float
    price_total: float
    price_subtotal: float
    deferred_start_date: str | Literal[False]
    deferred_end_date: str | Literal[False]


@with_config(ConfigDict(strict=True, extra="allow"))
class RawMove(TypedDict, total=False):
    """account.move."""

    id: Required[int]
    name: str | Literal[False]
    ref: str | Literal[False]
    journal_id: Relation
    company_id: Relation
    partner_id: Relation
    currency_id: Relation
    date: str
    move_type: str
    state: str
    invoice_date: str | Literal[False]
    invoice_date_due: str | Literal[False]
    amount_untaxed_signed: float
    amount_tax_signed: float
    amount_total_signed: float
    amount_residual_signed: float
    payment_state: str
    auto_post: str | Literal[False]
    asset_id: Relation
    asset_move_type: str | Literal[False]
    write_date: str | Literal[False]
    deferred_move_ids: list[int]
    deferred_original_move_ids: list[int]
    line_ids: list[int]


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAnalyticLine(TypedDict, total=False):
    """account.analytic.line; additional native plan columns are preserved."""

    id: Required[int]
    move_line_id: Relation
    account_id: Relation
    amount: float
    date: str
    name: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawPartialReconcile(TypedDict, total=False):
    """account.partial.reconcile."""

    id: Required[int]
    debit_move_id: Relation
    credit_move_id: Relation
    amount: float
    max_date: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawJournal(TypedDict, total=False):
    """account.journal."""

    id: Required[int]
    name: str
    type: str
    default_account_id: Relation


@with_config(ConfigDict(strict=True, extra="allow"))
class RawCompany(TypedDict, total=False):
    """res.company."""

    id: Required[int]
    name: str
    transfer_account_id: Relation
    fiscalyear_lock_date: str | Literal[False]


@with_config(ConfigDict(strict=True, extra="allow"))
class RawPartner(TypedDict, total=False):
    """res.partner."""

    id: Required[int]
    name: str | Literal[False]
    active: bool
    vat: str | Literal[False]
    country_id: Relation
    is_company: bool
    supplier_rank: int
    category_id: list[int]
    city: str | Literal[False]
    street: str | Literal[False]


class RawPartnerCategory(TypedDict, total=False):
    """res.partner.category."""

    id: Required[int]
    name: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAccount(TypedDict, total=False):
    """account.account."""

    id: Required[int]
    code: str
    name: str
    company_ids: list[int]


class RawBankStatement(TypedDict, total=False):
    """account.bank.statement."""

    id: Required[int]
    line_ids: list[int]


class RawBankStatementLine(TypedDict, total=False):
    """account.bank.statement.line."""

    id: Required[int]
    statement_id: Relation
    move_id: Relation


class RawTax(TypedDict, total=False):
    """account.tax."""

    id: Required[int]
    name: str
    amount: float
    amount_type: str
    type_tax_use: str
    children_tax_ids: list[int]


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAnalyticPlan(TypedDict, total=False):
    """account.analytic.plan."""

    id: Required[int]
    name: str
    parent_id: Relation
    default_applicability: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAnalyticAccount(TypedDict, total=False):
    """account.analytic.account."""

    id: Required[int]
    name: str
    code: str | Literal[False]
    plan_id: Relation
    root_plan_id: Relation
    active: bool
    partner_id: Relation


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAnalyticApplicability(TypedDict, total=False):
    """account.analytic.applicability."""

    id: Required[int]
    analytic_plan_id: Relation
    company_id: Relation
    product_categ_id: Relation
    business_domain: str
    account_prefix: str | Literal[False]
    applicability: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawProduct(TypedDict, total=False):
    """product.product."""

    id: Required[int]
    categ_id: Relation


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAttachment(TypedDict, total=False):
    """ir.attachment."""

    id: Required[int]
    name: str
    checksum: str | Literal[False]
    mimetype: str | Literal[False]
    res_model: str | Literal[False]
    res_id: int
    create_date: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAsset(TypedDict, total=False):
    """account.asset."""

    id: Required[int]
    name: str
    original_value: float
    original_move_line_ids: list[int]
    acquisition_date: str | Literal[False]
    state: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawBudgetLine(TypedDict, total=False):
    """budget.line; dynamic analytic-plan columns are preserved."""

    id: Required[int]
    name: str | Literal[False]
    sequence: int
    date_from: str | Literal[False]
    date_to: str | Literal[False]
    budget_amount: float
    achieved_amount: float
    theoritical_amount: float
    budget_analytic_id: Relation
    budget_analytic_state: str


@with_config(ConfigDict(strict=True, extra="allow"))
class RawAnalyticBudget(TypedDict, total=False):
    """budget.analytic."""

    id: Required[int]
    name: str
    date_from: str
    date_to: str
    budget_type: str
    state: str
    user_id: Relation
    company_id: Relation
