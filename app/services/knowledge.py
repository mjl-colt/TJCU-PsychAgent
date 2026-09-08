from __future__ import annotations

import json
import hashlib
import logging
import math
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Hashable

from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.prompt_security import PromptSecurityService
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL, ChromaKnowledgeStore


logger = logging.getLogger(__name__)


# These terms express how a user asks rather than what the user asks about.
# Keeping them in a tiny explicit stop-list prevents Chinese character/bigram
# matching from turning an out-of-domain query into a confident RAG hit.
RETRIEVAL_STOP_TERMS = {
    "一下", "一个", "什么", "怎么", "怎样", "如何", "可以", "帮我", "给我", "今天", "明天", "等于",
    "please", "tell", "about", "what", "when", "where", "which", "how", "the", "and",
}

PSYCHOLOGY_DOMAIN_TERMS = {
    "心理", "情绪", "焦虑", "紧张", "惊恐", "抑郁", "低落", "绝望", "压力", "应激", "倦怠",
    "失眠", "睡眠", "作息", "呼吸", "放松", "求助", "支持", "倾诉", "陪伴", "咨询", "辅导员",
    "心理中心", "可信任", "安全", "危险", "风险", "危机", "自杀", "自伤", "伤害", "轻生", "不想活",
    "活不下去", "撑不下去", "结束生命", "杀死自己",
    "药物", "开药", "诊断", "治疗", "专业", "转介", "保密", "隐私", "伦理", "适应", "过渡", "宿舍",
    "学生", "同学", "室友", "家人", "关系", "冲突", "分手", "孤独", "考试", "学习", "拖延", "注意力",
    "grounding", "breathing", "anxiety", "depression", "sleep", "stress", "burnout", "counselor",
    "professional", "risk", "emergency", "self-harm", "suicide", "routine", "journaling", "support",
    "trusted", "human", "help", "serious", "persistent", "prescribe", "medication",
}


class KnowledgeSecurityError(ValueError):
    """Raised when an uploaded RAG source contains instruction-injection signals."""


@dataclass
class SearchResult:
    chunk_id: int | None
    source: str
    content: str
    score: float


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.vector_store = ChromaKnowledgeStore(settings)

    def count(self) -> int:
        return self.db.query(KnowledgeChunk).count()

    def ensure_source(self, source: str, content: str, *, vectorize: bool = True) -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        existing = [
            chunk.content
            for chunk in self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == source)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        ]
        if existing == chunks:
            return len(existing)
        return self.ingest(source, content, vectorize=vectorize)

    def status(self) -> dict:
        vector_chunks = None
        vector_error = getattr(self.vector_store, "error", "")
        if self.vector_store.can_embed:
            try:
                vector_chunks = self.vector_store.count()
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"
        rows = self.db.query(KnowledgeChunk).all()
        source_counts = dict(sorted(Counter(row.source for row in rows).items()))
        source_names = list(source_counts)
        dialect = self.db.get_bind().dialect.name
        current_embedding_cache = sum(
            1
            for row in rows
            if parse_embedding(
                row.embedding_json,
                expected_model=self.settings.embedding_model_id,
                expected_content=row.content,
            )
            is not None
        )
        return {
            "retrievalOrder": [
                PRIMARY_RETRIEVAL_LABEL,
                f"{FALLBACK_RETRIEVAL_LABEL} when the configured embedder/Chroma is unavailable",
            ],
            "primaryRetrieval": PRIMARY_RETRIEVAL_LABEL,
            "fallbackRetrieval": FALLBACK_RETRIEVAL_LABEL,
            "databaseChunks": self.count(),
            "databaseSources": len(source_names),
            "databaseContentChars": sum(len(row.content) for row in rows),
            "sourceChunkCounts": source_counts,
            "authoritativeStore": f"{dialect} knowledge_chunks",
            "bootstrapSources": "app/knowledge/*.md synchronized at application startup",
            "chunking": {
                "strategy": "markdown heading/paragraph aware; fixed-character sliding fallback for oversized blocks",
                "sizeChars": self.settings.knowledge_chunk_size,
                "overlapChars": self.settings.knowledge_chunk_overlap,
            },
            "currentEmbeddingCacheChunks": current_embedding_cache,
            "vectorEnabled": self.settings.knowledge_vector_enabled,
            "vectorAvailable": self.vector_store.can_embed,
            "vectorRequired": self.settings.knowledge_vector_required,
            "embeddingProvider": self.settings.embedding_provider,
            "embeddingModel": self.settings.embedding_model_name,
            "embeddingModelId": self.settings.embedding_model_id,
            "vectorChunks": vector_chunks,
            "chromaPersistDir": self.settings.chroma_persist_dir,
            "chromaCollectionName": self.settings.chroma_collection_name,
            "chromaSnapshotDir": self.settings.chroma_snapshot_dir,
            "candidateK": self.settings.knowledge_candidate_k,
            "hybridVectorWeight": self.settings.knowledge_hybrid_vector_weight,
            "hybridBm25Weight": self.settings.knowledge_hybrid_bm25_weight,
            "rrfK": self.settings.knowledge_rrf_k,
            "rerankEnabled": self.settings.knowledge_rerank_enabled,
            "minRelevanceScore": self.settings.knowledge_min_relevance_score,
            "domainGateEnabled": self.settings.knowledge_domain_gate_enabled,
            "vectorError": vector_error,
        }

    def rebuild_vector_index(self) -> int:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        self._sync_vector_chunks(rows)
        self.db.commit()
        return len(rows)

    def synchronize_vector_index(self) -> int:
        """Build or repair the Chroma projection outside the chat hot path."""
        if not self.vector_store.can_embed:
            return 0
        rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        if not rows:
            return 0
        self._sync_vector_chunks(rows)
        self.db.commit()
        return len(rows)

    def backup_vector_index(self) -> str:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        snapshot = self.vector_store.snapshot()
        if snapshot is None:
            raise RuntimeError("Chroma 持久化目录不存在，无法生成快照")
        return snapshot

    def ingest(self, source: str, content: str, *, vectorize: bool = True) -> int:
        source, content = validate_knowledge_content(source, content, self.settings)
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        existing_rows = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == source)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        reusable: dict[str, deque[KnowledgeChunk]] = defaultdict(deque)
        for row in existing_rows:
            reusable[row.content].append(row)

        rows: list[KnowledgeChunk] = []
        retained_ids: set[int] = set()
        for index, chunk_content in enumerate(chunks):
            if reusable[chunk_content]:
                row = reusable[chunk_content].popleft()
                row.source_index = index
                if row.id is not None:
                    retained_ids.add(row.id)
            else:
                row = KnowledgeChunk(source=source, source_index=index, content=chunk_content)
                self.db.add(row)
            rows.append(row)

        stale_rows = [row for row in existing_rows if row.id not in retained_ids]
        stale_ids = [int(row.id) for row in stale_rows if row.id is not None]
        self._delete_vector_chunks(stale_ids)
        for row in stale_rows:
            self.db.delete(row)
        self.db.flush()
        if vectorize:
            # Cached vectors of unchanged chunks are reused.  Only new or
            # changed content is sent to the embedding provider, while every
            # retained row is upserted so moved source_index metadata stays current.
            self._index_vector_chunks(rows)
        self.db.commit()
        return len(chunks)

    def ingest_file(self, filename: str, data: bytes) -> int:
        max_bytes = max(1, int(getattr(self.settings, "knowledge_max_file_bytes", 5_242_880)))
        if len(data) > max_bytes:
            raise ValueError(f"知识文件不能超过 {max_bytes} 字节")
        lower = filename.lower()
        if lower.endswith(".pdf"):
            text = extract_pdf(data)
        else:
            text = data.decode("utf-8", errors="ignore")
        return self.ingest(filename, text)

    def retrieve(self, query: str, top_k: int | None = None) -> list[SearchResult]:
        top_k = top_k or self.settings.knowledge_top_k
        candidate_k = self._candidate_k(top_k)
        chunks = self.db.query(KnowledgeChunk).all()
        # Primary retrieval now uses hybrid recall: semantic vector candidates
        # plus BM25 keyword candidates, followed by deterministic local rerank.
        vector_results = self._retrieve_vector(query, candidate_k)
        domain_allowed = (
            not getattr(self.settings, "knowledge_domain_gate_enabled", True)
            or has_domain_query_signal(query)
        )
        bm25_results = self._retrieve_bm25(query, candidate_k, chunks) if domain_allowed else []
        if not domain_allowed and not vector_results:
            return []
        ranked = self._fuse_and_rerank(query, vector_results, bm25_results, top_k)
        min_score = max(0.0, float(getattr(self.settings, "knowledge_min_relevance_score", 0.45)))
        ranked = [item for item in ranked if item.score >= min_score]
        if ranked:
            return self._expand_best(ranked, top_k)
        return []

    def _retrieve_bm25(self, query: str, top_k: int, chunks: list[KnowledgeChunk] | None = None) -> list[SearchResult]:
        chunks = chunks if chunks is not None else self.db.query(KnowledgeChunk).all()
        scores = bm25_scores(query, chunks)
        ranked = [
            SearchResult(chunk.id, chunk.source, chunk.content, scores.get(chunk.id, 0.0))
            for chunk in chunks
            if chunk.id is not None and scores.get(chunk.id, 0.0) > 0
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_k]

    def _fuse_and_rerank(
        self,
        query: str,
        vector_results: list[SearchResult],
        bm25_results: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        candidates: dict[Hashable, SearchResult] = {}
        for item in [*vector_results, *bm25_results]:
            key = result_key(item)
            candidates.setdefault(key, item)

        if not candidates:
            return []

        vector_weight = max(0.0, self.settings.knowledge_hybrid_vector_weight) if vector_results else 0.0
        bm25_weight = max(0.0, self.settings.knowledge_hybrid_bm25_weight)
        if vector_weight == 0.0 and bm25_weight == 0.0:
            bm25_weight = 1.0
        total_weight = vector_weight + bm25_weight

        # Vector cosine similarity and BM25 have incompatible score ranges.
        # Weighted reciprocal-rank fusion uses position rather than raw score,
        # so one retriever cannot dominate merely because of its scale.
        rrf_k = max(1, int(getattr(self.settings, "knowledge_rrf_k", 60)))
        vector_ranks = {result_key(item): rank for rank, item in enumerate(vector_results, start=1)}
        bm25_ranks = {result_key(item): rank for rank, item in enumerate(bm25_results, start=1)}
        best_possible = total_weight / (rrf_k + 1)

        fused = []
        for key, candidate in candidates.items():
            score = 0.0
            if key in vector_ranks:
                score += vector_weight / (rrf_k + vector_ranks[key])
            if key in bm25_ranks:
                score += bm25_weight / (rrf_k + bm25_ranks[key])
            score = score / best_possible if best_possible > 0 else 0.0
            fused.append(replace_score(candidate, score))

        fused.sort(key=lambda item: item.score, reverse=True)
        fused = fused[:self._candidate_k(top_k)]
        return self._rerank(query, fused, top_k)

    def _rerank(self, query: str, candidates: list[SearchResult], top_k: int) -> list[SearchResult]:
        if not self.settings.knowledge_rerank_enabled:
            return candidates[:top_k]
        reranked = [
            replace_score(item, rerank_score(query, item.content, item.score))
            for item in candidates
        ]
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked[:top_k]

    def _candidate_k(self, top_k: int) -> int:
        return max(top_k, self.settings.knowledge_candidate_k)

    def _retrieve_vector(self, query: str, top_k: int) -> list[SearchResult]:
        if not self.vector_store.can_embed:
            return []
        try:
            # Document vectors are synchronized during startup or ingestion.
            # A chat request embeds only its rewritten query; it never rebuilds
            # the complete document index on the hot path.
            query_embedding = self.vector_store.embed_texts([query])[0]
            hits = self.vector_store.query(query_embedding, top_k)
        except Exception as exc:
            self._handle_vector_error("retrieve", exc)
            return []
        results = []
        for hit in hits:
            chunk = self.db.get(KnowledgeChunk, hit.chunk_id) if hit.chunk_id is not None else None
            results.append(
                SearchResult(
                    chunk.id if chunk is not None else hit.chunk_id,
                    chunk.source if chunk is not None else hit.source,
                    chunk.content if chunk is not None else hit.content,
                    hit.score,
                )
            )
        return results

    def _delete_vector_chunks(self, chunk_ids: list[int]) -> None:
        if not self.vector_store.can_embed or not chunk_ids:
            return
        try:
            self.vector_store.delete_chunk_ids(chunk_ids)
        except Exception as exc:
            self._handle_vector_error("delete_chunks", exc)

    def _index_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = serialize_embedding(
                    embedding,
                    self.settings.embedding_model_id,
                    chunk.content,
                )
            self.vector_store.upsert_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("index", exc)

    def _sync_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = serialize_embedding(
                    embedding,
                    self.settings.embedding_model_id,
                    chunk.content,
                )
            self.vector_store.sync_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("sync", exc)

    def _embeddings_for_chunks(self, chunks: list[KnowledgeChunk]) -> list[list[float]]:
        embeddings: list[list[float] | None] = []
        missing_indexes = []
        missing_texts = []
        for index, chunk in enumerate(chunks):
            embedding = parse_embedding(
                chunk.embedding_json,
                expected_model=self.settings.embedding_model_id,
                expected_content=chunk.content,
            )
            embeddings.append(embedding)
            if embedding is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.content)
        if missing_texts:
            new_embeddings = self.vector_store.embed_texts(missing_texts)
            for index, embedding in zip(missing_indexes, new_embeddings):
                embeddings[index] = embedding
        resolved = [embedding for embedding in embeddings if embedding is not None]
        if len(resolved) != len(chunks):
            raise ValueError("Embedding response count did not match knowledge chunks.")
        return resolved

    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        if self.settings.knowledge_vector_required:
            raise exc
        logger.warning(
            "%s %s failed; falling back to %s: %s",
            PRIMARY_RETRIEVAL_LABEL,
            action,
            FALLBACK_RETRIEVAL_LABEL,
            exc,
        )

    def _expand_best(self, ranked: list[SearchResult], top_k: int) -> list[SearchResult]:
        if not ranked:
            return []
        best = ranked[0]
        expanded = self._expand(best)
        results = [expanded]
        for item in ranked[1:]:
            if item.chunk_id != expanded.chunk_id and len(results) < top_k:
                results.append(item)
        return results

    def _expand(self, result: SearchResult) -> SearchResult:
        if result.chunk_id is None:
            return result
        chunk = self.db.get(KnowledgeChunk, result.chunk_id)
        if chunk is None:
            return result
        neighbors = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source == chunk.source)
            .filter(KnowledgeChunk.source_index >= max(0, chunk.source_index - 1))
            .filter(KnowledgeChunk.source_index <= chunk.source_index + 1)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        return SearchResult(chunk.id, chunk.source, "\n\n".join(item.content for item in neighbors), result.score)


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    """Split knowledge on semantic boundaries before using a sliding window.

    Markdown headings begin a new section and are copied into every oversized
    fragment from that section. Plain text/PDF input still benefits from blank
    paragraph boundaries. Only a single oversized paragraph is cut by character
    count, where ``overlap`` protects phrases near the cut.
    """
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    size = max(64, int(size))
    overlap = min(max(0, int(overlap)), size - 1)
    blocks = [
        re.sub(r"[ \t\n]+", " ", block).strip()
        for block in re.split(r"\n\s*\n|(?=^#{1,6}\s+)", normalized, flags=re.MULTILINE)
        if block.strip()
    ]
    chunks: list[str] = []
    current = ""
    active_heading = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for block in blocks:
        if re.match(r"^#{1,6}\s+", block):
            flush()
            active_heading = block
            current = block
            continue

        candidate = f"{current}\n{block}".strip() if current else block
        if len(candidate) <= size:
            current = candidate
            continue

        # A heading is context for its first paragraph, not a useful standalone
        # retrieval unit when that paragraph must be windowed.
        if current == active_heading:
            current = ""
        else:
            flush()
        prefix = f"{active_heading}\n" if active_heading else ""
        available = max(64, size - len(prefix))
        if len(block) <= available:
            current = prefix + block
            continue

        step = max(1, available - overlap)
        for start in range(0, len(block), step):
            fragment = block[start:start + available]
            chunks.append((prefix + fragment).strip())
            if start + available >= len(block):
                break

    flush()
    return chunks


def validate_knowledge_content(source: str, content: str, settings: Settings) -> tuple[str, str]:
    clean_source = (source or "").strip()
    clean_content = (content or "").strip()
    if not clean_source or len(clean_source) > 256:
        raise ValueError("知识来源名称不能为空且不能超过 256 个字符")
    max_chars = max(1, int(getattr(settings, "knowledge_max_ingest_chars", 500_000)))
    if not clean_content:
        raise ValueError("知识内容不能为空")
    if len(clean_content) > max_chars:
        raise ValueError(f"知识内容不能超过 {max_chars} 个字符")

    security = PromptSecurityService()
    if re.search(r"[\r\n<>]", clean_source) or "/" in clean_source or "\\" in clean_source:
        raise KnowledgeSecurityError("知识来源名称包含路径、换行或标签字符，已拒绝进入 RAG")
    source_signals = security.scan(clean_source).signals
    if source_signals:
        raise KnowledgeSecurityError(
            "知识来源名称包含疑似 Prompt 注入指令，已拒绝进入 RAG：" + ", ".join(source_signals)
        )
    signals: list[str] = []
    # Bound each scan so fuzzy detection cannot turn a large but valid PDF into
    # an expensive single operation.
    for start in range(0, len(clean_content), 8_000):
        signals.extend(security.scan(clean_content[start:start + 8_000]).signals)
    unique_signals = tuple(dict.fromkeys(signals))
    if unique_signals:
        logger.warning("Rejected suspicious knowledge source=%s signals=%s", clean_source, unique_signals)
        raise KnowledgeSecurityError(
            "知识内容包含疑似 Prompt 注入指令，已拒绝进入 RAG：" + ", ".join(unique_signals)
        )
    return clean_source, clean_content


def hybrid_score(query: str, content: str) -> float:
    return token_cosine(query, content) * 0.75 + keyword_score(query, content) * 0.25


def bm25_scores(query: str, chunks: list[KnowledgeChunk]) -> dict[int, float]:
    query_terms = counts(tokenize(query))
    if not query_terms or not chunks:
        return {}

    documents = []
    doc_freqs: dict[str, int] = {}
    for chunk in chunks:
        if chunk.id is None:
            continue
        token_counts = counts(tokenize(chunk.content))
        documents.append((chunk.id, token_counts, sum(token_counts.values())))
        for term in token_counts:
            doc_freqs[term] = doc_freqs.get(term, 0) + 1

    total_docs = len(documents)
    if total_docs == 0:
        return {}
    average_length = sum(length for _, _, length in documents) / total_docs or 1.0
    k1 = 1.5
    b = 0.75
    scores: dict[int, float] = {}

    for chunk_id, token_counts, doc_length in documents:
        score = 0.0
        length_norm = k1 * (1.0 - b + b * doc_length / average_length)
        for term, query_frequency in query_terms.items():
            term_frequency = token_counts.get(term, 0)
            if term_frequency == 0:
                continue
            doc_frequency = doc_freqs.get(term, 0)
            idf = math.log(1.0 + (total_docs - doc_frequency + 0.5) / (doc_frequency + 0.5))
            query_boost = 1.0 + math.log(query_frequency)
            score += idf * query_boost * (term_frequency * (k1 + 1.0)) / (term_frequency + length_norm)
        if score > 0:
            scores[chunk_id] = score
    return scores


def rerank_score(query: str, content: str, base_score: float) -> float:
    lexical = hybrid_score(query, content)
    coverage = query_token_coverage(query, content)
    phrase = phrase_score(query, content)
    return base_score * 0.55 + lexical * 0.25 + coverage * 0.15 + phrase * 0.05


def query_token_coverage(query: str, content: str) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    content_tokens = set(tokenize(content))
    return len(query_tokens & content_tokens) / len(query_tokens)


def phrase_score(query: str, content: str) -> float:
    normalized_query = compact_text(query)
    if not normalized_query:
        return 0.0
    normalized_content = compact_text(content)
    if normalized_query in normalized_content:
        return 1.0
    return keyword_score(query, content)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def result_key(result: SearchResult) -> Hashable:
    return result.chunk_id if result.chunk_id is not None else (result.source, result.content)


def replace_score(result: SearchResult, score: float) -> SearchResult:
    return SearchResult(result.chunk_id, result.source, result.content, score)


def parse_embedding(
    raw: str | None,
    *,
    expected_model: str | None = None,
    expected_content: str | None = None,
) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        if expected_model is not None and data.get("model") != expected_model:
            return None
        if expected_content is not None and data.get("contentHash") != embedding_content_hash(expected_content):
            return None
        data = data.get("vector")
    elif expected_model is not None or expected_content is not None:
        # A legacy bare list has no model/content provenance and must not be
        # silently reused after an embedding model or source-content change.
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(item, (int, float)) for item in data):
        return None
    return [float(item) for item in data]


def serialize_embedding(embedding: list[float], model: str, content: str) -> str:
    return json.dumps(
        {
            "model": model,
            "contentHash": embedding_content_hash(content),
            "vector": embedding,
        },
        separators=(",", ":"),
    )


def embedding_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def tokenize(text: str) -> list[str]:
    words = [
        item for item in re.findall(r"[a-zA-Z0-9_]+", text.lower())
        if len(item) >= 2 and item not in RETRIEVAL_STOP_TERMS
    ]
    compact = "".join(ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff")
    if len(compact) == 1:
        grams = [compact]
    else:
        grams = [compact[i:i + 2] for i in range(len(compact) - 1)]
    return [item for item in [*words, *grams] if item.strip() and item not in RETRIEVAL_STOP_TERMS]


def has_domain_query_signal(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in PSYCHOLOGY_DOMAIN_TERMS)


def token_cosine(left: str, right: str) -> float:
    left_counts = counts(tokenize(left))
    right_counts = counts(tokenize(right))
    if not left_counts or not right_counts:
        return 0.0
    dot = sum(value * right_counts.get(key, 0) for key, value in left_counts.items())
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return 0.0 if left_norm == 0 or right_norm == 0 else dot / (left_norm * right_norm)


def keyword_score(query: str, content: str) -> float:
    terms = [term for term in re.split(r"[\s，。！？、；：,.!?;:]+", query.lower()) if len(term) >= 2]
    if not terms:
        return 0.0
    lower = content.lower()
    matched = sum(1 for term in terms if term in lower)
    return min(1.0, matched / len(terms))


def counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def extract_pdf(data: bytes) -> str:
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
