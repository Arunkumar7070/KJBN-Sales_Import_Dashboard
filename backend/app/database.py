from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

# pool_pre_ping detects connections dropped while Neon's compute was suspended;
# pool_recycle retires connections before Neon's proxy closes idle ones.
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def check_database_connection(session: Session) -> None:
    session.execute(text("SELECT 1"))
