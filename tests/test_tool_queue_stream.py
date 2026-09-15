import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.agents.harness import AgentToolPlan, MindBridgeAgentHarness
from app.core.enums import EmotionLabel, IntentType, MessageRole, RiskLevel, ToolJobStatus
from app.models.entities import (
    AgentTurnMaterialization,
    ChatMessage,
    ChatSession,
    DeadLetterRecord,
    PsychologicalReport,
    ToolJob,
    ToolOutbox,
    UserAccount,
    now,
)
from app.services.mcp_client import McpToolError, MindBridgeMcpToolClient
from app.services.tool_queue import ToolQueueService, ToolQueueWorker


class FakeRedisStream:
    def __init__(self):
        self.messages = []
        self.acks = []
        self.groups = set()

    def xgroup_create(self, stream, group, id="0-0", mkstream=True):
        key = (stream, group)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add(key)

    def xadd(self, stream, values, **_kwargs):
        message_id = f"{len(self.messages) + 1}-0"
        self.messages.append((message_id, dict(values)))
        return message_id

    def xreadgroup(self, group, consumer, streams, count=10, block=1000):
        pending = self.messages[:count]
        self.messages = self.messages[count:]
        return [(next(iter(streams)), pending)] if pending else []

    def xautoclaim(self, stream, group, consumer, min_idle_time, start_id="0-0", count=10):
        # redis-py returns a list for this Redis response.
        return ["0-0", [], []]

    def xrange(self, stream, min="-", max="+", count=None):
        matched = [item for item in self.messages if min in {"-", item[0]} and max in {"+", item[0]}]
        return matched[:count] if count else matched

    def xack(self, stream, group, message_id):
        self.acks.append(message_id)


class ToolQueueStreamTests(unittest.TestCase):
    def test_outbox_publishes_and_stream_worker_consumes_once(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Db = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        settings = SimpleNamespace(
            database_url="sqlite+pysqlite:///:memory:",
            tool_queue_enabled=True,
            tool_queue_worker_enabled=True,
            tool_queue_backend="redis_stream",
            tool_queue_stream="test:tool-jobs",
            tool_queue_stream_maxlen=1000,
            tool_queue_consumer_group="test-workers",
            tool_queue_consumer_name="test-consumer",
            tool_queue_stream_block_ms=1,
            tool_queue_claim_idle_seconds=60,
            tool_queue_outbox_batch_size=10,
            tool_queue_outbox_publish_lease_seconds=30,
            tool_queue_mcp_enabled=False,
            tool_queue_max_attempts=3,
            tool_queue_retry_delay_seconds=1,
            tool_queue_poll_interval_seconds=1,
            alert_email_rate_limit_per_minute=30,
            excel_path="",
            alert_email_delivery_mode="log",
            alert_email_to="",
            alert_email_from="",
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            smtp_timeout_seconds=10,
            alert_email_subject_prefix="[test]",
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            settings, "excel_path", f"{temp_dir}/risk.xlsx"
        ), patch("app.services.tool_queue.SessionLocal", Db):
            with Session(engine) as db:
                user = UserAccount(username="student", display_name="Student", password_hash="hash")
                db.add(user)
                db.flush()
                session = ChatSession(public_id="session-test", user_id=user.id, title="test")
                db.add(session)
                db.flush()
                report = PsychologicalReport(
                    user_id=user.id,
                    session_id=session.id,
                    content="高风险测试",
                    intent=IntentType.RISK.value,
                    emotion=EmotionLabel.HIGH_RISK.value,
                    emotion_score=4.0,
                    risk_level=RiskLevel.HIGH.value,
                    summary="test report",
                )
                db.add(report)
                db.commit()
                jobs = ToolQueueService(db, settings).enqueue_report(report.id, report.risk_level)
                self.assertEqual(db.query(ToolOutbox).count(), 3)

            redis = FakeRedisStream()
            worker = ToolQueueWorker(settings, redis_client=redis)
            self.assertEqual(worker._publish_pending_outbox(), 3)
            self.assertEqual(worker._consume_once(), 3)

            with Session(engine) as db:
                self.assertEqual(
                    db.query(ToolJob).filter(ToolJob.status == ToolJobStatus.SUCCESS.value).count(),
                    3,
                )
                self.assertEqual(db.query(ToolOutbox).filter(ToolOutbox.status == "PUBLISHED").count(), 3)
                self.assertEqual(db.query(ToolOutbox).count(), 3)
            self.assertEqual(len(redis.acks), 3)

    def test_final_message_and_outbox_are_one_business_transaction(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        settings = SimpleNamespace(tool_queue_max_attempts=3)
        with Session(engine) as db:
            user = UserAccount(username="atomic-user", display_name="Atomic", password_hash="hash")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="session-atomic", user_id=user.id, title="atomic")
            db.add(session)
            db.flush()
            user_message = ChatMessage(
                user_id=user.id, session_id=session.id, role=MessageRole.USER.value, content="help"
            )
            report = PsychologicalReport(
                user_id=user.id,
                session_id=session.id,
                content="help",
                intent=IntentType.RISK.value,
                emotion=EmotionLabel.HIGH_RISK.value,
                emotion_score=4.0,
                risk_level=RiskLevel.HIGH.value,
                summary="atomic report",
            )
            db.add_all([user_message, report])
            db.flush()
            db.add(AgentTurnMaterialization(
                request_id="request-atomic",
                user_id=user.id,
                session_id=session.id,
                user_message_id=user_message.id,
                report_id=report.id,
            ))
            db.commit()

            harness = MindBridgeAgentHarness.__new__(MindBridgeAgentHarness)
            harness.db, harness.settings, harness.memory = db, settings, Mock()
            harness.save_assistant_message(
                user.id,
                session.id,
                session.public_id,
                "safe final response",
                "request-atomic",
                tool_plan=AgentToolPlan(report.id, RiskLevel.HIGH.value),
            )

            materialized = db.query(AgentTurnMaterialization).filter_by(request_id="request-atomic").one()
            self.assertIsNotNone(materialized.assistant_message_id)
            self.assertTrue(materialized.tools_dispatched)
            self.assertEqual(db.query(ToolJob).count(), 3)
            self.assertEqual(db.query(ToolOutbox).count(), 3)

    def test_tool_enqueue_failure_rolls_back_final_message(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = UserAccount(username="rollback-user", display_name="Rollback", password_hash="hash")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="session-rollback", user_id=user.id, title="rollback")
            db.add(session)
            db.flush()
            user_message = ChatMessage(
                user_id=user.id, session_id=session.id, role=MessageRole.USER.value, content="help"
            )
            db.add(user_message)
            db.flush()
            db.add(AgentTurnMaterialization(
                request_id="request-rollback",
                user_id=user.id,
                session_id=session.id,
                user_message_id=user_message.id,
                report_id=1,
            ))
            db.commit()

            harness = MindBridgeAgentHarness.__new__(MindBridgeAgentHarness)
            harness.db, harness.settings, harness.memory = db, SimpleNamespace(), Mock()
            with patch("app.agents.harness.ToolQueueService.enqueue_report", side_effect=RuntimeError("outbox down")):
                with self.assertRaises(RuntimeError):
                    harness.save_assistant_message(
                        user.id,
                        session.id,
                        session.public_id,
                        "must roll back",
                        "request-rollback",
                        tool_plan=AgentToolPlan(1, RiskLevel.HIGH.value),
                    )
            db.rollback()
            self.assertEqual(db.query(ChatMessage).filter(ChatMessage.role == MessageRole.ASSISTANT.value).count(), 0)
            materialized = db.query(AgentTurnMaterialization).filter_by(request_id="request-rollback").one()
            self.assertIsNone(materialized.assistant_message_id)
            self.assertFalse(materialized.tools_dispatched)

    def test_tool_turn_without_materialization_fails_before_writing_message(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            user = UserAccount(username="missing-receipt", display_name="Missing", password_hash="hash")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="session-missing", user_id=user.id, title="missing")
            db.add(session)
            db.commit()

            harness = MindBridgeAgentHarness.__new__(MindBridgeAgentHarness)
            harness.db, harness.settings, harness.memory = db, SimpleNamespace(), Mock()
            with self.assertRaises(RuntimeError):
                harness.save_assistant_message(
                    user.id,
                    session.id,
                    session.public_id,
                    "must not be written",
                    "request-without-receipt",
                    tool_plan=AgentToolPlan(1, RiskLevel.HIGH.value),
                )

            self.assertEqual(db.query(ChatMessage).filter_by(role=MessageRole.ASSISTANT.value).count(), 0)
            self.assertEqual(db.query(ToolJob).count(), 0)

    def test_redispatch_repairs_pending_child_dependency(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        settings = SimpleNamespace(tool_queue_max_attempts=3)
        with Session(engine) as db:
            dead_case = ToolJob(
                report_id=21,
                kind="CASE_CREATE",
                status=ToolJobStatus.DEAD.value,
                attempts=3,
                max_attempts=3,
                run_after=now(),
            )
            db.add(dead_case)
            db.flush()
            pending_alert = ToolJob(
                report_id=21,
                kind="ALERT_SEND",
                status=ToolJobStatus.PENDING.value,
                attempts=0,
                max_attempts=3,
                depends_on_job_id=dead_case.id,
                run_after=now(),
            )
            db.add(pending_alert)
            db.commit()

            jobs = ToolQueueService(db, settings).enqueue_report(21, RiskLevel.HIGH.value)
            replacement = next(job for job in jobs if job.kind == "CASE_CREATE")
            alert = next(job for job in jobs if job.kind == "ALERT_SEND")
            self.assertNotEqual(replacement.id, dead_case.id)
            self.assertEqual(alert.depends_on_job_id, replacement.id)

    def test_reconciliation_republishes_only_when_stream_message_is_missing(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Db = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        settings = SimpleNamespace(
            tool_queue_stream="test:tool-jobs",
            tool_queue_consumer_group="test-workers",
            tool_queue_consumer_name="test-consumer",
            tool_queue_claim_idle_seconds=10,
            alert_email_rate_limit_per_minute=30,
        )
        redis = FakeRedisStream()
        with patch("app.services.tool_queue.SessionLocal", Db):
            with Session(engine) as db:
                job = ToolJob(
                    report_id=7,
                    kind="EXCEL_REPORT",
                    status=ToolJobStatus.PENDING.value,
                    attempts=0,
                    max_attempts=3,
                    run_after=now() - timedelta(minutes=1),
                )
                db.add(job)
                db.flush()
                message_id = redis.xadd(settings.tool_queue_stream, {"job_id": str(job.id)})
                db.add(ToolOutbox(
                    job_id=job.id,
                    status="PUBLISHED",
                    stream_message_id=message_id,
                    available_at=now() - timedelta(minutes=1),
                    published_at=now() - timedelta(minutes=1),
                ))
                db.commit()

            worker = ToolQueueWorker(settings, redis_client=redis)
            worker._reconcile_unconfirmed_outbox()
            with Session(engine) as db:
                self.assertEqual(db.query(ToolOutbox).one().status, "PUBLISHED")

            redis.messages.clear()
            worker._reconcile_unconfirmed_outbox()
            with Session(engine) as db:
                self.assertEqual(db.query(ToolOutbox).one().status, "PENDING")

    def test_dependency_wait_does_not_consume_execution_attempt(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Db = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        settings = SimpleNamespace(
            tool_queue_stream="test:tool-jobs",
            tool_queue_consumer_group="test-workers",
            tool_queue_consumer_name="test-consumer",
            alert_email_rate_limit_per_minute=30,
        )
        redis = FakeRedisStream()
        with patch("app.services.tool_queue.SessionLocal", Db):
            with Session(engine) as db:
                dependency = ToolJob(
                    report_id=9,
                    kind="CASE_CREATE",
                    status=ToolJobStatus.PENDING.value,
                    attempts=0,
                    max_attempts=3,
                    run_after=now() - timedelta(minutes=1),
                )
                db.add(dependency)
                db.flush()
                alert = ToolJob(
                    report_id=9,
                    kind="ALERT_SEND",
                    status=ToolJobStatus.PENDING.value,
                    attempts=0,
                    max_attempts=3,
                    depends_on_job_id=dependency.id,
                    run_after=now() - timedelta(minutes=1),
                )
                db.add(alert)
                db.commit()
                alert_id = alert.id

            ToolQueueWorker(settings, redis_client=redis)._process_stream_message(
                redis, "1-0", {"job_id": str(alert_id)},
            )
            with Session(engine) as db:
                alert = db.get(ToolJob, alert_id)
                self.assertEqual(alert.status, ToolJobStatus.PENDING.value)
                self.assertEqual(alert.attempts, 0)

    def test_dead_dependency_dead_letters_child_job(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Db = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        settings = SimpleNamespace(
            tool_queue_stream="test:tool-jobs",
            tool_queue_consumer_group="test-workers",
            tool_queue_consumer_name="test-consumer",
            alert_email_rate_limit_per_minute=30,
        )
        redis = FakeRedisStream()
        with patch("app.services.tool_queue.SessionLocal", Db):
            with Session(engine) as db:
                dependency = ToolJob(
                    report_id=10,
                    kind="CASE_CREATE",
                    status=ToolJobStatus.DEAD.value,
                    attempts=3,
                    max_attempts=3,
                    run_after=now(),
                )
                db.add(dependency)
                db.flush()
                alert = ToolJob(
                    report_id=10,
                    kind="ALERT_SEND",
                    status=ToolJobStatus.PENDING.value,
                    attempts=0,
                    max_attempts=3,
                    depends_on_job_id=dependency.id,
                    run_after=now() - timedelta(minutes=1),
                )
                db.add(alert)
                db.commit()
                alert_id = alert.id

            ToolQueueWorker(settings, redis_client=redis)._process_stream_message(
                redis, "1-0", {"job_id": str(alert_id)},
            )
            with Session(engine) as db:
                alert = db.get(ToolJob, alert_id)
                self.assertEqual(alert.status, ToolJobStatus.DEAD.value)
                self.assertIsNotNone(db.query(DeadLetterRecord).filter_by(job_id=alert_id).first())

    def test_running_recovery_only_requeues_expired_jobs(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        Db = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        settings = SimpleNamespace(
            tool_queue_running_timeout_seconds=120,
            tool_queue_stream="test:tool-jobs",
            tool_queue_consumer_group="test-workers",
            tool_queue_consumer_name="test-consumer",
            alert_email_rate_limit_per_minute=30,
        )
        with patch("app.services.tool_queue.SessionLocal", Db):
            with Session(engine) as db:
                fresh = ToolJob(
                    report_id=11,
                    kind="EXCEL_REPORT",
                    status=ToolJobStatus.RUNNING.value,
                    attempts=1,
                    max_attempts=3,
                    run_after=now(),
                    updated_at=now(),
                )
                stale = ToolJob(
                    report_id=12,
                    kind="EXCEL_REPORT",
                    status=ToolJobStatus.RUNNING.value,
                    attempts=1,
                    max_attempts=3,
                    run_after=now(),
                    updated_at=now() - timedelta(minutes=5),
                )
                db.add_all([fresh, stale])
                db.commit()
                fresh_id, stale_id = fresh.id, stale.id

            ToolQueueWorker(settings, redis_client=FakeRedisStream())._recover_running_jobs()
            with Session(engine) as db:
                self.assertEqual(db.get(ToolJob, fresh_id).status, ToolJobStatus.RUNNING.value)
                self.assertEqual(db.get(ToolJob, stale_id).status, ToolJobStatus.PENDING.value)
                self.assertIsNotNone(db.query(ToolOutbox).filter_by(job_id=stale_id).first())


class McpJobMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_stream_job_maps_to_one_allowlisted_mcp_tool(self):
        calls = []

        class Session:
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return SimpleNamespace(isError=False, content=[SimpleNamespace(text="success: caseId=9")])

        @asynccontextmanager
        async def session():
            yield Session()

        client = MindBridgeMcpToolClient(SimpleNamespace(tool_queue_mcp_timeout_seconds=1))
        client._session = session
        result = await client.execute_job("CASE_CREATE", 42)

        self.assertIn("success", result)
        self.assertEqual(calls, [("mindbridge_case_create", {"report_id": 42})])

    async def test_textual_not_found_result_is_a_tool_failure(self):
        class Session:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(isError=False, content=[SimpleNamespace(text="report 42 not found")])

        @asynccontextmanager
        async def session():
            yield Session()

        client = MindBridgeMcpToolClient(SimpleNamespace(tool_queue_mcp_timeout_seconds=1))
        client._session = session
        with self.assertRaises(McpToolError):
            await client.execute_job("EXCEL_REPORT", 42)


if __name__ == "__main__":
    unittest.main()
