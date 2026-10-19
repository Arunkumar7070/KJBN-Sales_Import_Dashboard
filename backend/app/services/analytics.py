"""Dashboard analytics computed with SQL aggregations over transactions.

Definitions:
- Revenue is net: the sum of line_total, so returns subtract.
- Days are IST calendar days; date ranges include both ends.
- Everything is "as of" app.clock.utc_now(): transactions sold after it are
  left out, and a missing date_to means the clock's IST date. With the
  X-Test-Clock header this shows the data as it was at that time.
- Percentages divide with NULLIF(denominator, 0), so an empty range gives
  NULL instead of a division-by-zero error, reported as 0.
- The payment split covers sales only (how customers paid for purchases), so
  shares are never negative and always add up to 100 (or are all 0).
"""

from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time

from sqlalchemy import Date, Float, and_, case, cast, func, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import ColumnElement, FunctionElement

from app.clock import utc_now
from app.models import Product, Store, Transaction
from app.schemas import (
    DailyRevenue,
    DashboardResponse,
    Money,
    PaymentSplit,
    StoreReturnRate,
    StoreRevenue,
    TopProduct,
)
from app.services.sales_rules import IST, PAYMENT_MODES

TOP_PRODUCT_COUNT = 10
PAYMENT_MODE_ORDER = ("CASH", "UPI", "CARD")
assert set(PAYMENT_MODE_ORDER) == PAYMENT_MODES


class ist_date(FunctionElement):
    """SQL: the IST calendar date of a UTC timestamp (works on PostgreSQL and SQLite)."""

    type = Date()
    inherit_cache = True


@compiles(ist_date, "postgresql")
def _ist_date_postgresql(element, compiler, **kwargs):
    # AT TIME ZONE gives IST wall-clock time whatever the session time zone is.
    return f"CAST(({compiler.process(element.clauses, **kwargs)}) AT TIME ZONE 'Asia/Kolkata' AS DATE)"


@compiles(ist_date, "sqlite")
def _ist_date_sqlite(element, compiler, **kwargs):
    return f"date({compiler.process(element.clauses, **kwargs)}, '+330 minutes')"


def start_of_ist_day(day: date) -> datetime:
    # sold_at is stored in UTC; midnight IST is 18:30 UTC the day before.
    # Converted to UTC so drivers that ignore time zones (SQLite) compare correctly.
    return datetime.combine(day, clock_time.min, tzinfo=IST).astimezone(UTC)


def ist_today() -> date:
    return utc_now().astimezone(IST).date()


def sold_within_ist_days(
    date_from: date | None, date_to: date | None
) -> list[ColumnElement[bool]]:
    """Conditions keeping transactions sold on date_from..date_to (IST, inclusive)
    and not after the clock's current time."""
    conditions = [Transaction.sold_at <= utc_now()]
    if date_from is not None:
        conditions.append(Transaction.sold_at >= start_of_ist_day(date_from))
    if date_to is not None:
        # Before the next day's midnight, so every moment of date_to is included.
        conditions.append(
            Transaction.sold_at < start_of_ist_day(date_to + timedelta(days=1))
        )
    return conditions


def percent(numerator, denominator) -> ColumnElement[float]:
    """numerator ÷ denominator × 100 in SQL; 0 instead of an error when denominator is 0."""
    return func.coalesce(
        cast(numerator, Float) * 100 / func.nullif(denominator, 0), 0.0
    )


def build_dashboard(
    session: Session, date_from: date | None, date_to: date | None
) -> DashboardResponse:
    in_range = sold_within_ist_days(date_from, date_to)
    return DashboardResponse(
        daily_revenue=daily_revenue(session, in_range, date_from, date_to),
        store_revenue=store_revenue(session, in_range),
        top_products=top_products(session, in_range),
        store_return_rates=store_return_rates(session, in_range),
        payment_split=payment_split(session, in_range),
    )


def daily_revenue(
    session: Session,
    in_range: list[ColumnElement[bool]],
    date_from: date | None,
    date_to: date | None,
) -> list[DailyRevenue]:
    day = ist_date(Transaction.sold_at).label("day")
    rows = session.execute(
        select(day, func.sum(Transaction.line_total_paise))
        .where(*in_range)
        .group_by(day)
        .order_by(day)
    ).all()
    # SQLite returns the date as text; PostgreSQL as a date.
    revenue_by_day = {date.fromisoformat(str(d)): int(paise) for d, paise in rows}

    # Every day in the range gets an entry (0 when nothing sold) so charts have
    # no gaps. Without date_from it starts at the first sale; without date_to it
    # ends today (by the clock).
    first = date_from or min(revenue_by_day, default=None)
    last = date_to or ist_today()
    if first is None or first > last:
        return []
    return [
        DailyRevenue(day=day, revenue=Money(paise=revenue_by_day.get(day, 0)))
        for day in (first + timedelta(days=n) for n in range((last - first).days + 1))
    ]


def store_revenue(
    session: Session, in_range: list[ColumnElement[bool]]
) -> list[StoreRevenue]:
    revenue = func.coalesce(func.sum(Transaction.line_total_paise), 0)
    rows = session.execute(
        select(Store.code, Store.name, revenue)
        # Date conditions in the ON clause keep stores with no sales in range.
        .outerjoin(Transaction, and_(Transaction.store_code == Store.code, *in_range))
        .group_by(Store.code, Store.name)
        .order_by(revenue.desc(), Store.code)
    ).all()
    return [
        StoreRevenue(store_code=code, store_name=name, revenue=Money(paise=int(paise)))
        for code, name, paise in rows
    ]


def top_products(
    session: Session, in_range: list[ColumnElement[bool]]
) -> list[TopProduct]:
    net_quantity = func.sum(Transaction.quantity)
    revenue = func.sum(Transaction.line_total_paise)
    rows = session.execute(
        select(Product.sku, Product.name, net_quantity, revenue)
        .join(Transaction, Transaction.sku == Product.sku)
        .where(*in_range)
        .group_by(Product.sku, Product.name)
        .having(net_quantity > 0)  # A product that only had returns isn't a top seller.
        .order_by(net_quantity.desc(), revenue.desc(), Product.sku)
        .limit(TOP_PRODUCT_COUNT)
    ).all()
    return [
        TopProduct(
            rank=rank,
            sku=sku,
            product_name=name,
            quantity_sold=int(quantity),
            revenue=Money(paise=int(paise)),
        )
        for rank, (sku, name, quantity, paise) in enumerate(rows, start=1)
    ]


def store_return_rates(
    session: Session, in_range: list[ColumnElement[bool]]
) -> list[StoreReturnRate]:
    quantity = Transaction.quantity
    units_sold = func.coalesce(func.sum(case((quantity > 0, quantity), else_=0)), 0)
    units_returned = func.coalesce(
        func.sum(case((quantity < 0, -quantity), else_=0)), 0
    )
    rows = session.execute(
        select(
            Store.code, units_sold, units_returned, percent(units_returned, units_sold)
        )
        .outerjoin(Transaction, and_(Transaction.store_code == Store.code, *in_range))
        .group_by(Store.code)
        .order_by(Store.code)
    ).all()
    return [
        StoreReturnRate(
            store_code=code,
            units_sold=int(sold),
            units_returned=int(returned),
            return_rate_percent=round(float(rate), 2),
        )
        for code, sold, returned, rate in rows
    ]


def payment_split(
    session: Session, in_range: list[ColumnElement[bool]]
) -> list[PaymentSplit]:
    revenue = func.sum(Transaction.line_total_paise)
    # SUM(SUM(...)) OVER () is the total across all groups, in the same query.
    share = percent(revenue, func.sum(revenue).over())
    rows = session.execute(
        select(Transaction.payment_mode, func.count(), revenue, share)
        .where(*in_range, Transaction.quantity > 0)
        .group_by(Transaction.payment_mode)
    ).all()
    by_mode = {mode: (count, paise, pct) for mode, count, paise, pct in rows}
    # Every mode is listed, in a fixed order, even with no sales.
    return [
        PaymentSplit(
            payment_mode=mode,
            transaction_count=int(count),
            revenue=Money(paise=int(paise)),
            share_percent=round(float(pct), 2),
        )
        for mode in PAYMENT_MODE_ORDER
        for count, paise, pct in [by_mode.get(mode, (0, 0, 0.0))]
    ]
