"""Synchronize seed knowledge and build the persistent Chroma projection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the 心理ai persistent Chroma knowledge index.")
    parser.add_argument(
        "--local-sqlite",
        action="store_true",
        help="use data/rag-local.sqlite3 instead of the configured production database",
    )
    args = parser.parse_args()
    if args.local_sqlite:
        database_path = (PROJECT_ROOT / "data" / "rag-local.sqlite3").as_posix()
        os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{database_path}"

    # Configuration and the SQLAlchemy engine are imported only after command
    # line overrides are applied.
    from app.core.bootstrap import create_schema, seed_data
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.knowledge import KnowledgeService

    settings = get_settings()
    create_schema()
    with SessionLocal() as db:
        seed_data(db)
        service = KnowledgeService(db, settings)
        indexed = service.rebuild_vector_index()
        status = service.status()
    print(
        json.dumps(
            {
                "indexedChunks": indexed,
                "embeddingProvider": status["embeddingProvider"],
                "embeddingModel": status["embeddingModel"],
                "databaseSources": status["databaseSources"],
                "databaseChunks": status["databaseChunks"],
                "cachedEmbeddingChunks": status["currentEmbeddingCacheChunks"],
                "vectorChunks": status["vectorChunks"],
                "chromaPersistDir": status["chromaPersistDir"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
