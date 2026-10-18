import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Store(Base):
    __tablename__ = "stores"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="store")


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    aliases: Mapped[list["ProductAlias"]] = relationship(back_populates="product")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="product")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (
        UniqueConstraint("sku", "alias_name", name="uq_product_aliases_sku_alias_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(Text, ForeignKey("products.sku"))
    alias_name: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    product: Mapped[Product] = relationship(back_populates="aliases")


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'COMPLETED', 'REJECTED_HEADER', 'FAILED')",
            name="ck_import_batches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(CHAR(64), unique=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    rows_total: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_accepted: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # report_link is derived (/api/v1/imports/{id}/errors.csv), not stored.

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="batch")
    row_errors: Mapped[list["ImportRowError"]] = relationship(back_populates="batch")
    payload: Mapped["ImportPayload | None"] = relationship(back_populates="batch")


class ImportRowError(Base):
    __tablename__ = "import_row_errors"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "row_number", name="uq_import_row_errors_batch_id_row_number"
        ),
        CheckConstraint("row_number >= 2", name="ck_import_row_errors_row_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("import_batches.id"))
    row_number: Mapped[int] = mapped_column(Integer)
    column_name: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    # Not a foreign key: the rejected row's transaction was never stored.
    transaction_id: Mapped[str | None] = mapped_column(Text)

    batch: Mapped[ImportBatch] = relationship(back_populates="row_errors")


class ImportPayload(Base):
    """An uploaded CSV staged for a background worker; survives process restarts."""

    __tablename__ = "import_payloads"
    __table_args__ = (
        CheckConstraint(
            "received_size_bytes >= 0", name="ck_import_payloads_received_size_bytes"
        ),
    )

    # Primary key and foreign key at once: at most one payload per batch.
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("import_batches.id"), primary_key=True
    )
    csv_gzip: Mapped[bytes] = mapped_column(LargeBinary)  # gzip of original bytes
    received_size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    batch: Mapped[ImportBatch] = relationship(back_populates="payload")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("quantity <> 0", name="ck_transactions_quantity_nonzero"),
        CheckConstraint(
            "unit_price_paise >= 0", name="ck_transactions_unit_price_non_negative"
        ),
        CheckConstraint(
            "discount_paise >= 0", name="ck_transactions_discount_non_negative"
        ),
        CheckConstraint(
            "payment_mode IN ('CASH', 'UPI', 'CARD')",
            name="ck_transactions_payment_mode",
        ),
        CheckConstraint(
            "is_return = (quantity < 0)",
            name="ck_transactions_is_return_matches_quantity",
        ),
    )

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    store_code: Mapped[str] = mapped_column(Text, ForeignKey("stores.code"))
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sku: Mapped[str] = mapped_column(Text, ForeignKey("products.sku"))
    product_name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    discount_paise: Mapped[int] = mapped_column(BigInteger)
    payment_mode: Mapped[str] = mapped_column(Text)
    original_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("transactions.transaction_id")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("import_batches.id"))
    line_total_paise: Mapped[int] = mapped_column(BigInteger)
    is_return: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    store: Mapped[Store] = relationship(back_populates="transactions")
    product: Mapped[Product] = relationship(back_populates="transactions")
    batch: Mapped[ImportBatch] = relationship(back_populates="transactions")
    original_transaction: Mapped["Transaction | None"] = relationship(
        back_populates="returns", remote_side=[transaction_id]
    )
    returns: Mapped[list["Transaction"]] = relationship(
        back_populates="original_transaction"
    )
