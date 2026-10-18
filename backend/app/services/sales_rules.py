"""Sales business rules for a single CSV row.

parse_sale_row converts raw CSV text into typed values; validate_sale applies
the rules. Both raise SaleRuleViolation, whose code identifies the rule and
whose column_name and message are stored in import_row_errors.

Rules:
- sold_at is "YYYY-MM-DD HH:MM:SS" in IST, stored in UTC, and not in the future.
- quantity is a non-zero integer: positive is a sale, negative is a return.
- unit_price >= 0 and 0 <= discount <= unit_price * abs(quantity), in rupees
  with up to 2 decimals, stored as integer paise.
- payment_mode is exactly CASH, UPI or CARD.
- A return has a discount of exactly 0.00, references an existing positive
  sale with the same store and SKU, and total returned quantity never exceeds
  the sale quantity. A sale must not reference an original transaction.
- line total = quantity * unit_price - discount.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

IST = timezone(timedelta(hours=5, minutes=30), "IST")
PAYMENT_MODES = frozenset({"CASH", "UPI", "CARD"})
MAX_QUANTITY = 2**31 - 1  # transactions.quantity is INT
MAX_PAISE = 2**63 - 1  # money columns are BIGINT

SOLD_AT_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
INTEGER_PATTERN = re.compile(r"[+-]?\d+")
AMOUNT_PATTERN = re.compile(r"[+-]?\d+(\.\d{1,2})?")

COLUMN_BY_CODE = {
    "INVALID_SOLD_AT": "sold_at",
    "FUTURE_TIMESTAMP": "sold_at",
    "INVALID_QUANTITY": "quantity",
    "INVALID_UNIT_PRICE": "unit_price",
    "INVALID_DISCOUNT": "discount",
    "DISCOUNT_EXCEEDS_GROSS": "discount",
    "AMOUNT_TOO_LARGE": "unit_price",
    "INVALID_PAYMENT_MODE": "payment_mode",
    "SALE_HAS_ORIGINAL": "original_transaction_id",
    "RETURN_ORIGINAL_REQUIRED": "original_transaction_id",
    "RETURN_ORIGINAL_NOT_FOUND": "original_transaction_id",
    "RETURN_ORIGINAL_NOT_A_SALE": "original_transaction_id",
    "RETURN_STORE_MISMATCH": "store_code",
    "RETURN_SKU_MISMATCH": "sku",
    "RETURN_DISCOUNT_NOT_ZERO": "discount",
    "RETURN_QUANTITY_EXCEEDS_ORIGINAL": "quantity",
    "DUPLICATE_IN_FILE": "transaction_id",
    "DUPLICATE_IN_DATABASE": "transaction_id",
    "UNKNOWN_STORE": "store_code",
}


class SaleRuleViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.column_name = COLUMN_BY_CODE[code]
        self.message = message


@dataclass(frozen=True)
class SaleRow:
    transaction_id: str
    store_code: str
    sku: str
    sold_at: datetime
    quantity: int
    unit_price_paise: int
    discount_paise: int
    payment_mode: str
    original_transaction_id: str | None = None


def parse_sale_row(values: Mapping[str, str]) -> SaleRow:
    return SaleRow(
        transaction_id=values["transaction_id"],
        store_code=values["store_code"],
        sku=values["sku"],
        sold_at=parse_sold_at(values["sold_at"]),
        quantity=parse_quantity(values["quantity"]),
        unit_price_paise=parse_paise(values["unit_price"], "INVALID_UNIT_PRICE"),
        discount_paise=parse_paise(values["discount"], "INVALID_DISCOUNT"),
        payment_mode=values["payment_mode"],
        original_transaction_id=values["original_transaction_id"] or None,
    )


def parse_sold_at(text: str) -> datetime:
    message = f"sold_at must be YYYY-MM-DD HH:MM:SS (IST); got '{text}'."
    if not SOLD_AT_PATTERN.fullmatch(text):
        raise SaleRuleViolation("INVALID_SOLD_AT", message)
    try:
        local_time = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:  # e.g. month 13 or 31 February
        raise SaleRuleViolation("INVALID_SOLD_AT", message) from error
    return local_time.replace(tzinfo=IST).astimezone(UTC)


def parse_quantity(text: str) -> int:
    if not INTEGER_PATTERN.fullmatch(text):
        raise SaleRuleViolation(
            "INVALID_QUANTITY", f"quantity must be a whole number; got '{text}'."
        )
    quantity = int(text)
    if abs(quantity) > MAX_QUANTITY:
        raise SaleRuleViolation(
            "INVALID_QUANTITY", f"quantity {quantity} is out of range."
        )
    return quantity


def parse_paise(text: str, code: str) -> int:
    column = COLUMN_BY_CODE[code]
    if not AMOUNT_PATTERN.fullmatch(text):
        raise SaleRuleViolation(
            code,
            f"{column} must be an amount in rupees with up to 2 decimals; got '{text}'.",
        )
    return int(Decimal(text) * 100)


def line_total_paise(row: SaleRow) -> int:
    return row.quantity * row.unit_price_paise - row.discount_paise


def validate_sale(
    row: SaleRow,
    *,
    clock: Callable[[], datetime],
    original: SaleRow | None = None,
    already_returned_quantity: int = 0,
) -> None:
    if row.quantity == 0:
        raise SaleRuleViolation("INVALID_QUANTITY", "quantity must not be zero.")
    if row.payment_mode not in PAYMENT_MODES:
        raise SaleRuleViolation(
            "INVALID_PAYMENT_MODE",
            f"payment_mode must be CASH, UPI or CARD; got '{row.payment_mode}'.",
        )
    if row.sold_at > clock():
        raise SaleRuleViolation("FUTURE_TIMESTAMP", "sold_at is in the future.")
    validate_amounts(row)

    if row.quantity > 0:
        if row.original_transaction_id:
            raise SaleRuleViolation(
                "SALE_HAS_ORIGINAL",
                "Only returns (negative quantity) may set original_transaction_id.",
            )
        return
    validate_return(row, original, already_returned_quantity)


def validate_amounts(row: SaleRow) -> None:
    if row.unit_price_paise < 0:
        raise SaleRuleViolation(
            "INVALID_UNIT_PRICE", "unit_price must not be negative."
        )
    if row.discount_paise < 0:
        raise SaleRuleViolation("INVALID_DISCOUNT", "discount must not be negative.")

    gross_paise = row.unit_price_paise * abs(row.quantity)
    if max(gross_paise, abs(line_total_paise(row))) > MAX_PAISE:
        raise SaleRuleViolation(
            "AMOUNT_TOO_LARGE", "Line amount is too large to store."
        )
    if row.discount_paise > gross_paise:
        raise SaleRuleViolation(
            "DISCOUNT_EXCEEDS_GROSS",
            f"discount ({format_rupees(row.discount_paise)}) exceeds the line amount "
            f"({format_rupees(gross_paise)}).",
        )


def validate_return(
    row: SaleRow, original: SaleRow | None, already_returned_quantity: int
) -> None:
    if row.discount_paise != 0:
        raise SaleRuleViolation(
            "RETURN_DISCOUNT_NOT_ZERO",
            f"A return must have a discount of 0.00; got "
            f"{format_rupees(row.discount_paise)}.",
        )
    original_id = row.original_transaction_id
    if not original_id:
        raise SaleRuleViolation(
            "RETURN_ORIGINAL_REQUIRED",
            "A return (negative quantity) must set original_transaction_id.",
        )
    if original is None:
        raise SaleRuleViolation(
            "RETURN_ORIGINAL_NOT_FOUND",
            f"Original transaction '{original_id}' not found.",
        )
    if original.quantity <= 0:
        raise SaleRuleViolation(
            "RETURN_ORIGINAL_NOT_A_SALE",
            f"Original transaction '{original_id}' is not a sale.",
        )
    if row.store_code != original.store_code:
        raise SaleRuleViolation(
            "RETURN_STORE_MISMATCH",
            f"Return store '{row.store_code}' does not match the original sale's "
            f"store '{original.store_code}'.",
        )
    if row.sku != original.sku:
        raise SaleRuleViolation(
            "RETURN_SKU_MISMATCH",
            f"Return SKU '{row.sku}' does not match the original sale's SKU "
            f"'{original.sku}'.",
        )
    returned_after = already_returned_quantity + abs(row.quantity)
    if returned_after > original.quantity:
        raise SaleRuleViolation(
            "RETURN_QUANTITY_EXCEEDS_ORIGINAL",
            f"Returning {abs(row.quantity)} would bring total returns to "
            f"{returned_after}, more than the {original.quantity} originally sold.",
        )


def format_rupees(paise: int) -> str:
    return f"₹{Decimal(paise) / 100:.2f}"
