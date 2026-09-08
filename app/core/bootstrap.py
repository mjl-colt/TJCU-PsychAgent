from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_runtime_json_capacity()
    _drop_obsolete_columns()


def _drop_obsolete_columns(bind=engine) -> None:
    """Apply small, explicit compatibility migrations without Alembic."""

    inspector = inspect(bind)
    table = "psychological_reports"
    column = "confidence"
    if table not in inspector.get_table_names():
        return
    if column not in {item["name"] for item in inspector.get_columns(table)}:
        return
    preparer = bind.dialect.identifier_preparer
    quoted_table = preparer.quote(table)
    quoted_column = preparer.quote(column)
    with bind.begin() as connection:
        connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_column}")


def _ensure_runtime_json_capacity() -> None:
    """One-time compatibility upgrade for pre-projection MySQL tables."""

    if engine.dialect.name != "mysql":
        return
    inspector = inspect(engine)
    targets = {
        "agent_runtime_checkpoints": "state_json",
        "agent_runtime_events": "payload_json",
    }
    with engine.begin() as connection:
        for table, column in targets.items():
            columns = {item["name"]: item for item in inspector.get_columns(table)}
            current = columns.get(column)
            if current is None or "LONGTEXT" in str(current["type"]).upper():
                continue
            connection.exec_driver_sql(f"ALTER TABLE `{table}` MODIFY `{column}` LONGTEXT NOT NULL")


def seed_data(db: Session) -> None:
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    service = KnowledgeService(db, get_settings())
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        # First synchronize authoritative MySQL chunks.  Embeddings are built
        # once after all sources are ready instead of once per source.
        service.ensure_source(file.name, file.read_text(encoding="utf-8"), vectorize=False)
    service.synchronize_vector_index()
