from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    agent_runtime_max_events: int = 64
    agent_workflow_max_steps: int = 32
    agent_runtime_idle_timeout_seconds: float = 30.0
    agent_runtime_max_concurrency: int = 4
    agent_runtime_timeout_seconds: float = 12.0
    agent_runtime_safety_timeout_seconds: float = 6.0
    agent_runtime_max_retries: int = 1
    agent_runtime_retry_backoff_seconds: float = 0.15
    agent_runtime_max_prompt_revisions: int = 2
    agent_runtime_persistence_enabled: bool = True
    agent_runtime_persistence_required: bool = True
    agent_runtime_recovery_enabled: bool = True
    agent_runtime_recovery_scan_limit: int = 100
    agent_runtime_lease_enabled: bool = True
    agent_runtime_lease_ttl_seconds: float = 120.0
    agent_max_input_chars: int = 4000
    agent_max_prompt_chars: int = 12000
    agent_support_output_guardrail_enabled: bool = True
    agent_rag_citations_required: bool = True
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_understanding_provider: str = ""
    agent_model_understanding_model: str = ""
    agent_model_safety_provider: str = ""
    agent_model_safety_model: str = ""
    agent_model_context_provider: str = ""
    agent_model_context_model: str = ""
    agent_model_response_provider: str = ""
    agent_model_response_model: str = ""
    agent_model_fallback_provider: str = ""
    agent_model_fallback_model: str = ""
    agent_model_circuit_breaker_failures: int = 3
    agent_model_circuit_breaker_reset_seconds: float = 30.0
    auth_rate_limit_per_minute: int = 60
    chat_rate_limit_per_minute: int = 30
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_provider: str = "ollama"
    ollama_embedding_model: str = "qwen3-embedding:0.6b"
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    knowledge_candidate_k: int = 16
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_hybrid_vector_weight: float = 0.65
    knowledge_hybrid_bm25_weight: float = 0.35
    knowledge_rrf_k: int = 60
    knowledge_rerank_enabled: bool = True
    knowledge_min_relevance_score: float = 0.45
    knowledge_domain_gate_enabled: bool = True
    knowledge_max_ingest_chars: int = 500_000
    knowledge_max_file_bytes: int = 5_242_880
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "mindbridge_knowledge"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 60.0
    embedding_batch_size: int = 16
    rag_eval_dataset: str = "app/rag_eval/mindbridge-rag-eval.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_max_chars: int = 500
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[心理ai 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_worker_enabled: bool = True
    tool_queue_backend: str = "redis_stream"
    tool_queue_stream: str = "mindbridge:tool-jobs"
    tool_queue_stream_maxlen: int = 100000
    tool_queue_consumer_group: str = "mindbridge-tool-workers"
    tool_queue_consumer_name: str = ""
    tool_queue_stream_block_ms: int = 1000
    tool_queue_claim_idle_seconds: float = 60.0
    tool_queue_running_timeout_seconds: float = 120.0
    tool_queue_reconcile_interval_seconds: float = 30.0
    tool_queue_outbox_batch_size: int = 100
    tool_queue_outbox_publish_lease_seconds: float = 30.0
    tool_queue_mcp_enabled: bool = True
    tool_queue_mcp_timeout_seconds: float = 30.0
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def embedding_model_name(self) -> str:
        provider = self.embedding_provider.strip().lower()
        return self.openai_embedding_model if provider == "openai" else self.ollama_embedding_model

    @property
    def embedding_model_id(self) -> str:
        return f"{self.embedding_provider.strip().lower()}:{self.embedding_model_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
