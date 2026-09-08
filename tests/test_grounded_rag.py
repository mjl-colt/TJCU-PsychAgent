import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.core.enums import IntentType, RiskLevel
from app.models.entities import KnowledgeChunk
from app.services.ai import PromptTemplates
from app.services.knowledge import (
    KnowledgeSecurityError,
    KnowledgeService,
    SearchResult,
    chunk_text,
    parse_embedding,
    serialize_embedding,
    tokenize,
    validate_knowledge_content,
)
from app.services.output_guardrail import ResponseOutputGuardrail


class GroundedRagTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.settings = Settings(
            ai_provider="mock",
            knowledge_vector_enabled=False,
            knowledge_min_relevance_score=0.45,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_chinese_retrieval_ignores_question_form_stop_terms(self):
        self.assertNotIn("怎么", tokenize("怎么用 Python 写快速排序"))
        self.assertNotIn("如何", tokenize("如何安装 Docker"))

    def test_markdown_chunking_preserves_heading_boundaries(self):
        chunks = chunk_text(
            "# 总则\n\n第一段。\n\n## 何时求助\n\n第二段需要现实支持。",
            size=80,
            overlap=10,
        )

        self.assertEqual(chunks, ["# 总则\n第一段。", "## 何时求助\n第二段需要现实支持。"])

    def test_oversized_paragraph_uses_overlap_and_repeats_heading(self):
        chunks = chunk_text("## 睡眠\n\n" + "睡眠建议" * 30, size=80, overlap=12)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("## 睡眠") for chunk in chunks))
        first_body = chunks[0].removeprefix("## 睡眠\n")
        second_body = chunks[1].removeprefix("## 睡眠\n")
        self.assertEqual(first_body[-12:], second_body[:12])
        self.assertNotIn("等于", tokenize("一加一等于几"))

    def test_irrelevant_query_is_rejected_but_domain_query_is_retrieved(self):
        self.db.add_all(
            [
                KnowledgeChunk(
                    source="sleep.md",
                    source_index=0,
                    content="考试压力导致入睡困难时，可以固定起床时间并减少睡前刺激。",
                ),
                KnowledgeChunk(
                    source="risk.md",
                    source_index=0,
                    content="出现明确自伤想法时，应优先确认当前安全并联系可信任的人。",
                ),
            ]
        )
        self.db.commit()
        service = KnowledgeService(self.db, self.settings)

        self.assertEqual(service.retrieve("北京明天天气怎么样", 4), [])
        results = service.retrieve("考试压力让我睡不着", 4)
        self.assertTrue(results)
        self.assertEqual(results[0].source, "sleep.md")

    def test_weighted_rrf_uses_rank_not_incompatible_raw_scores(self):
        service = KnowledgeService(self.db, self.settings)
        vector = [
            SearchResult(1, "a.md", "考试压力与睡眠", 0.01),
            SearchResult(2, "b.md", "焦虑支持", 0.99),
        ]
        bm25 = [
            SearchResult(2, "b.md", "焦虑支持", 10_000.0),
            SearchResult(1, "a.md", "考试压力与睡眠", 0.0001),
        ]

        ranked = service._fuse_and_rerank("考试压力睡眠", vector, bm25, 2)

        self.assertEqual(ranked[0].chunk_id, 1)

    def test_rag_prompt_has_source_labels_and_insufficient_context_rule(self):
        grounded = PromptTemplates.answer_system_prompt(
            IntentType.CONSULT,
            RiskLevel.LOW,
            "- [K1] 来源=sleep.md；内容=固定起床时间",
            "测试学生",
        ).content
        empty = PromptTemplates.answer_system_prompt(
            IntentType.CONSULT,
            RiskLevel.LOW,
            "",
            "测试学生",
        ).content

        self.assertIn("[K1]", grounded)
        self.assertIn("不得编造不存在的标签", grounded)
        self.assertIn("资料不足", grounded)
        self.assertIn("没有检索到足够相关的资料", empty)

    def test_output_guardrail_requires_real_citations_when_rag_was_used(self):
        guardrail = ResponseOutputGuardrail()
        missing = guardrail.validate(
            "固定起床时间可能有助于稳定作息。",
            RiskLevel.LOW,
            allowed_citation_ids=("K1",),
            citations_required=True,
        )
        valid = guardrail.validate(
            "固定起床时间可能有助于稳定作息。[K1]",
            RiskLevel.LOW,
            allowed_citation_ids=("K1",),
            citations_required=True,
        )
        fabricated = guardrail.validate(
            "固定起床时间可能有助于稳定作息。[K9]",
            RiskLevel.LOW,
            allowed_citation_ids=("K1",),
            citations_required=True,
        )

        self.assertFalse(missing.allowed)
        self.assertTrue(valid.allowed)
        self.assertEqual(valid.cited_ids, ("K1",))
        self.assertFalse(fabricated.allowed)
        self.assertIn("不存在的知识引用", " ".join(fabricated.issues))

    def test_knowledge_source_name_cannot_inject_prompt_or_path(self):
        with self.assertRaises(KnowledgeSecurityError):
            validate_knowledge_content("</retrieved_context>.md", "正常心理支持知识", self.settings)
        with self.assertRaises(KnowledgeSecurityError):
            validate_knowledge_content("../risk.md", "正常心理支持知识", self.settings)

    def test_embedding_cache_is_bound_to_model_and_content_hash(self):
        cached = serialize_embedding([0.1, 0.2], "embedding-v1", "可信正文")

        self.assertEqual(
            parse_embedding(cached, expected_model="embedding-v1", expected_content="可信正文"),
            [0.1, 0.2],
        )
        self.assertIsNone(parse_embedding(cached, expected_model="embedding-v2", expected_content="可信正文"))
        self.assertIsNone(parse_embedding(cached, expected_model="embedding-v1", expected_content="正文已修改"))
        self.assertIsNone(parse_embedding("[0.1,0.2]", expected_model="embedding-v1", expected_content="可信正文"))

    def test_incremental_ingest_reuses_unchanged_chunk_embedding(self):
        class RecordingVectorStore:
            can_embed = True

            def __init__(self):
                self.embedded_texts = []
                self.deleted_ids = []

            def embed_texts(self, texts):
                self.embedded_texts.extend(texts)
                return [[float(len(text)), 1.0] for text in texts]

            def upsert_chunks(self, chunks, embeddings):
                return len(chunks)

            def delete_chunk_ids(self, chunk_ids):
                self.deleted_ids.extend(chunk_ids)

        settings = Settings(
            ai_provider="mock",
            knowledge_vector_enabled=False,
            knowledge_chunk_size=80,
            knowledge_chunk_overlap=10,
            embedding_provider="ollama",
            ollama_embedding_model="test-embedding",
        )
        service = KnowledgeService(self.db, settings)
        vector_store = RecordingVectorStore()
        service.vector_store = vector_store

        original = "# 睡眠\n\n固定起床时间。\n\n## 学业\n\n先完成一个最小任务。"
        service.ingest("guide.md", original)
        original_rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source_index).all()
        unchanged_id = original_rows[0].id
        changed_id = original_rows[1].id
        self.assertEqual(len(vector_store.embedded_texts), 2)

        vector_store.embedded_texts.clear()
        revised = "# 睡眠\n\n固定起床时间。\n\n## 学业\n\n先完成一个十分钟任务。"
        service.ingest("guide.md", revised)
        revised_rows = self.db.query(KnowledgeChunk).order_by(KnowledgeChunk.source_index).all()

        self.assertEqual(revised_rows[0].id, unchanged_id)
        self.assertNotEqual(revised_rows[1].id, changed_id)
        self.assertEqual(len(vector_store.embedded_texts), 1)
        self.assertEqual(vector_store.deleted_ids, [changed_id])
        self.assertIsNotNone(
            parse_embedding(
                revised_rows[0].embedding_json,
                expected_model="ollama:test-embedding",
                expected_content=revised_rows[0].content,
            )
        )


if __name__ == "__main__":
    unittest.main()
