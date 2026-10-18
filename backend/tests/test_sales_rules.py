"""Business rules for a single sales/return row.

Rules (agreed before implementation):
- quantity must be non-zero: positive is a sale, negative is a return.
- 0 <= discount_paise <= unit_price_paise * abs(quantity).
- sold_at must not be after the clock's current time (equal is allowed).
- payment_mode must be exactly CASH, UPI or CARD (no trimming or case folding).
- A return must have a discount of exactly 0 and must reference an original
  transaction that exists, is a positive sale, and has the same store and SKU.
- Total returned quantity against an original must not exceed its quantity.

The module under test, app.services.sales_rules, is imported lazily so every
test fails on its own until the module exists.
"""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

FIXED_NOW = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)

ORIGINAL_SALE = {
    "transaction_id": "T1",
    "store_code": "ST01",
    "sku": "SKU1",
    "sold_at": FIXED_NOW - timedelta(days=1),
    "quantity": 5,
    "unit_price_paise": 10000,
    "discount_paise": 0,
    "payment_mode": "UPI",
}


def sale(**overrides):
    fields = {
        "transaction_id": "T1",
        "store_code": "ST01",
        "sku": "SKU1",
        "sold_at": FIXED_NOW - timedelta(hours=1),
        "quantity": 2,
        "unit_price_paise": 10000,
        "discount_paise": 0,
        "payment_mode": "UPI",
    }
    return fields | overrides


def return_of(quantity, **overrides):
    defaults = {"transaction_id": "T2", "original_transaction_id": "T1"}
    return sale(**(defaults | overrides), quantity=quantity)


@pytest.fixture
def rules():
    return importlib.import_module("app.services.sales_rules")


@pytest.fixture
def validate(rules):
    def _validate(fields, *, now=FIXED_NOW, original=None, already_returned=0):
        rules.validate_sale(
            rules.SaleRow(**fields),
            clock=lambda: now,
            original=rules.SaleRow(**original) if original else None,
            already_returned_quantity=already_returned,
        )

    return _validate


@pytest.fixture
def assert_rejected(rules, validate):
    def _assert_rejected(expected_code, fields, **kwargs):
        with pytest.raises(rules.SaleRuleViolation) as excinfo:
            validate(fields, **kwargs)
        assert excinfo.value.code == expected_code

    return _assert_rejected


# Quantity


def test_smallest_positive_quantity_is_accepted(validate):
    validate(sale(quantity=1))


def test_zero_quantity_is_rejected(assert_rejected):
    assert_rejected("INVALID_QUANTITY", sale(quantity=0))


# Discount (gross for the default sale = 2 x 10000 = 20000 paise)


@pytest.mark.parametrize("discount_paise", [0, 1, 5000, 19999, 20000])
def test_discount_from_zero_up_to_gross_is_accepted(validate, discount_paise):
    validate(sale(discount_paise=discount_paise))


def test_discount_just_over_gross_is_rejected(assert_rejected):
    assert_rejected("DISCOUNT_EXCEEDS_GROSS", sale(discount_paise=20001))


def test_negative_discount_is_rejected(assert_rejected):
    assert_rejected("INVALID_DISCOUNT", sale(discount_paise=-1))


def test_return_with_zero_discount_is_accepted(validate):
    validate(return_of(-2, discount_paise=0), original=ORIGINAL_SALE)


@pytest.mark.parametrize("discount_paise", [1, 5000, 20000])
def test_return_with_any_positive_discount_is_rejected(assert_rejected, discount_paise):
    assert_rejected(
        "RETURN_DISCOUNT_NOT_ZERO",
        return_of(-2, discount_paise=discount_paise),
        original=ORIGINAL_SALE,
    )


def test_negative_discount_on_return_is_still_invalid_discount(assert_rejected):
    assert_rejected(
        "INVALID_DISCOUNT", return_of(-2, discount_paise=-1), original=ORIGINAL_SALE
    )


# Timestamp


@pytest.mark.parametrize("sold_at", [FIXED_NOW, FIXED_NOW - timedelta(microseconds=1)])
def test_timestamp_at_or_before_now_is_accepted(validate, sold_at):
    validate(sale(sold_at=sold_at))


def test_timestamp_just_after_now_is_rejected(assert_rejected):
    assert_rejected(
        "FUTURE_TIMESTAMP", sale(sold_at=FIXED_NOW + timedelta(microseconds=1))
    )


def test_future_check_uses_the_injected_clock(validate, assert_rejected):
    later = FIXED_NOW + timedelta(minutes=1)

    assert_rejected("FUTURE_TIMESTAMP", sale(sold_at=later), now=FIXED_NOW)
    validate(sale(sold_at=later), now=later)


# Payment mode


@pytest.mark.parametrize("payment_mode", ["CASH", "UPI", "CARD"])
def test_known_payment_modes_are_accepted(validate, payment_mode):
    validate(sale(payment_mode=payment_mode))


@pytest.mark.parametrize("payment_mode", ["CHEQUE", "", "upi", " UPI"])
def test_unknown_payment_modes_are_rejected(assert_rejected, payment_mode):
    assert_rejected("INVALID_PAYMENT_MODE", sale(payment_mode=payment_mode))


# Return linkage


def test_partial_return_of_matching_sale_is_accepted(validate):
    validate(return_of(-2), original=ORIGINAL_SALE)


def test_return_without_original_id_is_rejected(assert_rejected):
    fields = return_of(-2, original_transaction_id=None)

    assert_rejected("RETURN_ORIGINAL_REQUIRED", fields, original=ORIGINAL_SALE)


def test_return_with_unknown_original_is_rejected(assert_rejected):
    assert_rejected("RETURN_ORIGINAL_NOT_FOUND", return_of(-2), original=None)


def test_return_against_another_return_is_rejected(assert_rejected):
    original_return = ORIGINAL_SALE | {"quantity": -1}

    assert_rejected(
        "RETURN_ORIGINAL_NOT_A_SALE", return_of(-1), original=original_return
    )


def test_return_from_different_store_is_rejected(assert_rejected):
    assert_rejected(
        "RETURN_STORE_MISMATCH",
        return_of(-2, store_code="ST02"),
        original=ORIGINAL_SALE,
    )


def test_return_of_different_sku_is_rejected(assert_rejected):
    assert_rejected(
        "RETURN_SKU_MISMATCH", return_of(-2, sku="SKU2"), original=ORIGINAL_SALE
    )


# Cumulative return quantity (original sale quantity = 5)


def test_returns_totalling_exactly_original_quantity_are_accepted(validate):
    validate(return_of(-2), original=ORIGINAL_SALE, already_returned=3)


@pytest.mark.parametrize(
    ("already_returned", "return_quantity"),
    [(3, -3), (0, -6), (5, -1)],
)
def test_returns_beyond_original_quantity_are_rejected(
    assert_rejected, already_returned, return_quantity
):
    assert_rejected(
        "RETURN_QUANTITY_EXCEEDS_ORIGINAL",
        return_of(return_quantity),
        original=ORIGINAL_SALE,
        already_returned=already_returned,
    )
