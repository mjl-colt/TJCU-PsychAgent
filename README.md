# 心理ai

## 核心能力

- 学生端 SSE 流式聊天，前端可展示打字机式输出。
- Basic Auth 登录，支持学生和管理员角色隔离；密码使用带随机盐的 PBKDF2，并保留旧 SHA-256 登录后自动升级。
- 显式多 Agent 工作流：四个业务 Agent + 确定性 Coordinator，通过步骤、任务收据、强类型 Blackboard 和安全审查协作；Event 只审计。
- 动态路由 RAG：先判断 `CHAT / CONSULT / RISK`，普通问题不查知识库，咨询和风险场景才进入检索增强。
- Chroma 向量 RAG 知识库：支持 Markdown、txt、PDF 文件上传，自动切块，默认使用本地 Ollama `qwen3-embedding:0.6b` 写入向量库；向量与 BM25 候选通过加权 RRF 融合，再进入本地 reranker 和相关度门，向量不可用时保留 BM25 兜底。
- 可追溯 RAG：领域门拒绝无关 query，embedding 缓存绑定模型与正文 hash，证据使用 K 标签，最终输出拦截漏引和伪造引用。
- 心理风险评估：版本化确定性危机策略优先、严格 JSON 模型评估、模型故障时保守降级；不再用“包含任意危险词就 HIGH”的简单词典碰撞。
- 后台报告：记录情绪标签、情绪分数、风险等级和摘要，但学生端不展示后台评估结果。未经校准的模型自报置信度不进入 Blackboard、checkpoint、报告、API、Excel 或告警邮件。
- 数据闭环：咨询/风险消息完整写入 MySQL，短期上下文写入 Redis，高风险消息写入 Excel 台账并通过邮件发送预警。
- 本地微调模型接入：支持通过 Ollama 加载心理ai专用微调模型。
- OpenAI-compatible API 接入：也可切换到云端模型。
- MCP 工具服务：暴露 Excel 报告写入和风险通知工具，后端高风险后处理通过 MCP client 调用这些工具。
- RAG 评测：正样本 Recall@K、Precision@K、MRR、NDCG@K、HitRate，以及负样本拒绝率和检索决策准确率。
- Prompt 注入防护：输入/历史/RAG 不可信隔离、编码与混淆检测、RAG 入库拒绝、最终输出门禁和工具最小权限。
- 生产韧性：checkpoint/outcome 恢复、requestId 幂等、数据库租约、模型主备熔断、Redis 限流和 readiness 检查。
- Runtime 基础能力：状态迁移门禁、每个任务结果独立持久化、单一合并屏障、生成收尾恢复；事件投影与 hash 仅保留给历史 event-v1 恢复。
- Runtime JSON 持久化在 MySQL 使用 `LONGTEXT`，启动时自动兼容升级旧 `TEXT` 列，避免完整状态投影触碰 64KB 上限。

## 前端演示

只想查看界面、不启动任何服务时，直接双击项目根目录的 [`frontend-preview.html`](frontend-preview.html)。该文件提供登录首页、学生端和管理端三个可切换视图，并内置场景选择、模拟对话和 Agent 流程动画；不会连接后端或发送数据。

服务启动后访问 `http://127.0.0.1:8080`。首页提供两个可直接选择的演示身份：

- 学生演示：`student / student123`，体验场景快捷输入、SSE 流式对话和 Agent 协作过程。
- 管理端演示：`admin / admin123`，查看风险个案、报告、会话档案与 RAG 知识库维护。

学生端右侧的协作视图展示 Coordinator、Understanding、Safety、Context 和 Response 的请求路径，但不会向学生暴露后台风险标签、内部 Prompt 或敏感 trace。使用默认 `AI_PROVIDER=mock` 即可完整演示交互，无需配置云端 API Key。

## 学习文档

- [Runtime 整体流程例子梳理](docs/心理ai%20Runtime整体流程例子梳理.md)：逐步说明 checkpoint 何时保存、存在哪里、崩溃后怎样恢复。
- [workflow-v2 详细流程与存储图](docs/心理ai%20workflow-v2详细流程与存储图.md)：直接查看完整 CONSULT 主路线成品图，以及内存、MySQL、Redis、Chroma 的数据去向。
- [四 Agent 内部完整流程例子](docs/心理ai%20四Agent内部完整流程例子.md)：逐步说明每个 Agent 的 Prompt、记忆、RAG、Skill、fallback 和安全门。
- [生产差距与十条优化路线](docs/心理ai生产差距与十条优化路线.md)：已完成项与仍需外部建设的边界。
- [项目面试核心十问](docs/心理ai%20Runtime与四Agent面试问答.md)：把架构、状态、恢复、四 Agent、模型选型、向量库规模、混合 RAG、幻觉、短长期记忆、框架横向比较、重试、安全和生产化整合成十个大问题。
- [RAG、Skill 与模型效果量化说明](docs/心理ai%20RAG、Skill与模型效果量化说明.md)：用一个问题讲清知识从 md 到 MySQL/Chroma、九条 intent/risk 路径如何选择 4 个 Skill，以及微调模型怎样做公平 A/B。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：checkpoint_workflow（workflow-v2 显式工作流）
RAG：本地知识库切块、Ollama/OpenAI Embeddings、Chroma 向量库、BM25、分数融合、本地 reranker、上下文扩展
流式输出：Server-Sent Events
文档解析：pypdf
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：Basic Auth + PBKDF2-HMAC-SHA256（生产需替换为学校 SSO/OIDC）
工具协议：MCP
```

说明：新请求使用 `workflow-v2`，转换规则在 `app/agents/workflow.py`，执行器在 `app/agents/workflow_runtime.py`。服务入口 `AgentRuntimeService` 暂留于 `app/agents/event_driven_runtime.py` 以兼容既有导入；它按 checkpoint 的 workflow_version 选择新工作流或历史 event-v1 Runtime。共享返回类型定义在 `app/agents/result.py`。RAG 默认使用 Chroma 本地持久化向量库做语义召回，同时用 BM25 做关键词召回，再融合并本地 rerank；未安装 Chroma、未配置 `OPENAI_API_KEY` 或向量服务异常时，会自动回退到本地 BM25 + `hybrid_score` reranker，避免演示环境中断。

项目对用户、页面、日志说明和文档统一称为“心理ai”。代码中少量 `mindbridge-*` 仅是既有数据库名、模型文件名、Redis key、MCP tool 和 Python 类的兼容标识；为避免破坏已部署数据、模型和外部调用协议，本轮不做无收益的物理迁移。

## 目录结构

```text
docs/                  # 架构、实现说明与面试学习文档
datasets/              # 可版本化的合成/评测数据集
skills/                # 运行时加载的标准 Skill 定义

app/
├── agents/          # 显式工作流 + 历史事件 Runtime 兼容恢复
├── api/             # FastAPI 路由
├── core/            # 配置、数据库、安全、启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── model_eval/      # 基座模型与微调模型的固定评测集
├── rag_eval/        # RAG 评测脚本和数据集
├── prompts/         # 运行时加载的版本化 Prompt 模板
├── schemas/         # Pydantic DTO
├── services/        # AI、聊天、知识库、评估、报告、工具服务
└── static/          # 原生前端页面

models/mindbridge-qwen2.5-7b-ft/
├── Modelfile        # Ollama 模型定义
└── README.md        # GGUF 模型放置说明

scripts/
├── run-dev.sh
├── start-ollama.sh
├── create-finetuned-model.sh
├── evaluate_model_quality.py
└── package-release.sh
```

## Agent 工作流

下一步由当前步骤、已保存任务收据和 Blackboard 业务结果共同决定；规则集中在 WorkflowCoordinator，不再读取 Event 类型调度。

```text
RECEIVED
→ ANALYZING：Understanding + Safety 并行
→ 每个 outcome 返回即保存 checkpoint 收据
→ 收齐后统一校验并合并（唯一一道完成屏障）
→ 按 intent/risk 路由，必要时 RETRIEVING：Context
→ PREPARING_RESPONSE：Response 组装 Prompt
→ PROMPT_REVIEW：Safety 审查同版本
   拒绝 → REVISING_RESPONSE → 再审；预算耗尽则 FAILED
   通过 → READY_FOR_GENERATION（不再额外调用 FINALIZE_RESPONSE）
→ GENERATING：最终模型 + 输出安全门
→ FINALIZING_RESPONSE：保存合法完整文本
→ 助手消息与工具任务派发确认
→ COMPLETED
```

- WorkflowCoordinator：确定性步骤与转换规则，不是第五个 LLM Agent。
- WorkflowRuntime：提交任务、保存结果收据、校验合并、推进和恢复。
- Dispatcher：并行执行、超时、重试和降级。
- Understanding / Safety / Context / Response：分别返回自己分区的局部更新。

Blackboard 更新采用创建新对象并替换局部 state 引用的方式；Agent 得到独立快照，不能修改共享状态。命令绑定的输入 revision 与后续收据提交的 checkpoint revision 分开处理。

checkpoint 表 agent_runtime_checkpoints 保存当前阶段、业务状态和 execution.tasks。单个 Agent 返回先保存 outcome 收据；全部收齐后，业务合并与下一步任务在同一事务提交，成功后才调用下一步。恢复只看 checkpoint：有收据复用，没收据沿用原 commandId 重跑。

Event 追加写入 agent_runtime_events，与对应 checkpoint 同事务保存，用于审计和指标。v2 不经 EventBus、不产生调度用 AGENT_BATCH_REQUESTED、不复制整份 state_projection。旧 event-v1 请求继续使用旧队列和投影恢复，避免破坏历史 checkpoint。

READY_FOR_GENERATION 只表示 Prompt 就绪；FINALIZING_RESPONSE 表示合法最终文本已保存但业务收尾尚未完成。收尾失败可以直接复用已检查文字；只有业务保存和工具任务派发确认后才提交 COMPLETED。工具任务最终执行成功与聊天请求完成是不同状态。

生产 Prompt 不再散落在 Python 字符串中，统一位于 `app/prompts/*.md`。每份模板必须声明 `name` 和 `vN` 版本，加载时计算正文 SHA-256；严格占位符缺值、多值或遗留未替换都会直接报错。运行时的 `prompt_template_version` 记录实际模板组合，Prompt 正文携带 `PROMPT_ID` 与 `PROMPT_SHA256`，可以从一次 trace 反查当时使用了哪一版。

Safety Review 也不再搜索“安全”“可信任”“不诊断”等固定中文。Response 先写入强类型 `ResponsePolicyContract`，例如 HIGH 会得到 `requires_immediate_safety_check=true`、`requires_human_support=true` 和 `requires_emergency_escalation=true`；候选 Prompt 携带该契约的 SHA-256，Safety 校验契约是否与当前 Blackboard 风险和 RAG 证据一致。最终 HIGH 回复无论普通支持输出门配置是否关闭，都必须在服务器内完整缓冲，再由独立 Safety 模型返回七个布尔字段，按语义检查情绪回应、当下安置、真人支持、紧急升级、诊断、用药和危险细节。审核模型异常或 JSON 不合 schema 时 fail closed，替换为系统内置高风险兜底文字，未审核内容不会先流给浏览器。

仍然保留中文的地方有三类：给中文用户看的回复、版本化安全策略里的语言模式、测试/mock 的确定性样本。这些内容本来就与语言有关；已经删除的是用“代码、怎么写、焦虑”等词直接决定 CHAT/CONSULT 的生产捷径。现在 Understanding 依赖结构化分类结果，分类结果非法或模型故障统一保守进入 CONSULT，高风险硬规则仍独立于模型。

客户端可为每轮请求提供稳定 `requestId`。相同 requestId 只能绑定相同用户、会话和输入；重连时会恢复或重放结果。`agent_turn_materializations` 原子绑定用户消息、心理报告、trace、助手消息和工具派发状态，避免顺序重试重复落库。

共享数据库中的 `agent_runtime_leases` 为每个 requestId 提供带过期时间的唯一 owner。它覆盖编排和 SSE 生成；长流按 TTL 的三分之一续租，进程退出后租约自动过期，恢复器才能接管。并发实例碰到仍被持有的 requestId 会返回 HTTP 409，客户端稍后使用同一 id 重试即可。

这里没有把 Redis 分布式锁作为最终执行权依据。数据库租约本身也是分布式协调方案，并且更适合当前“低冲突、执行时间长、必须按 checkpoint 恢复”的 Agent 请求：租约、checkpoint、事件和业务幂等状态都能通过同一个 MySQL 权威数据源排查与恢复，避免 Redis 锁和 MySQL 业务状态之间的双写窗口。Redis 更适合作为未来高并发时的前置快速互斥层；即使加入 Redis，MySQL 租约、唯一约束和业务幂等键仍负责最终正确性。

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 已包含：

```text
chromadb
pymysql
redis
```

新请求默认 `checkpoint_workflow / workflow-v2`。历史 checkpoint 缺少 workflow_version 时按 event-v1 恢复；版本由请求快照确定，不提供随意切换活跃请求执行语义的开关。

Runtime 生产参数：

```env
AGENT_WORKFLOW_MAX_STEPS=32
# 以下两个参数只用于历史 event-v1 Runtime
AGENT_RUNTIME_MAX_EVENTS=64
AGENT_RUNTIME_IDLE_TIMEOUT_SECONDS=30
AGENT_RUNTIME_MAX_CONCURRENCY=4
AGENT_RUNTIME_TIMEOUT_SECONDS=12
AGENT_RUNTIME_SAFETY_TIMEOUT_SECONDS=6
AGENT_RUNTIME_MAX_RETRIES=1
AGENT_RUNTIME_RETRY_BACKOFF_SECONDS=0.15
AGENT_RUNTIME_MAX_PROMPT_REVISIONS=2
AGENT_RUNTIME_PERSISTENCE_ENABLED=true
AGENT_RUNTIME_PERSISTENCE_REQUIRED=true
AGENT_RUNTIME_RECOVERY_ENABLED=true
AGENT_RUNTIME_RECOVERY_SCAN_LIMIT=100
AGENT_RUNTIME_LEASE_ENABLED=true
AGENT_RUNTIME_LEASE_TTL_SECONDS=120
AGENT_MAX_INPUT_CHARS=4000
AGENT_MAX_PROMPT_CHARS=12000
AGENT_SUPPORT_OUTPUT_GUARDRAIL_ENABLED=true
AGENT_RAG_CITATIONS_REQUIRED=true
AGENT_MODEL_FALLBACK_PROVIDER=
AGENT_MODEL_FALLBACK_MODEL=
AGENT_MODEL_CIRCUIT_BREAKER_FAILURES=3
AGENT_MODEL_CIRCUIT_BREAKER_RESET_SECONDS=30
AUTH_RATE_LIMIT_PER_MINUTE=60
CHAT_RATE_LIMIT_PER_MINUTE=30
KNOWLEDGE_RRF_K=60
KNOWLEDGE_MIN_RELEVANCE_SCORE=0.45
KNOWLEDGE_DOMAIN_GATE_ENABLED=true
KNOWLEDGE_MAX_INGEST_CHARS=500000
KNOWLEDGE_MAX_FILE_BYTES=5242880
```

## MySQL 和 Redis 配置

系统默认使用 MySQL 保存完整业务数据和完整聊天消息，使用 Redis 保存短期对话记忆。启动服务前先创建数据库：

```sql
CREATE DATABASE mindbridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mindbridge'@'%' IDENTIFIED BY 'mindbridge';
GRANT ALL PRIVILEGES ON mindbridge.* TO 'mindbridge'@'%';
FLUSH PRIVILEGES;
```

`.env` 中配置连接：

```env
DATABASE_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_MEMORY_TTL_SECONDS=86400
REDIS_MEMORY_MAX_MESSAGES=40
```

完整聊天记录写入 MySQL 的 `chat_sessions`、`chat_messages` 等表。Redis 只保存每个会话最近 `REDIS_MEMORY_MAX_MESSAGES` 条短期上下文，并通过 `REDIS_MEMORY_TTL_SECONDS` 自动过期。

四个 Agent 不各自复制一份 Redis 私有记忆。Safety、Context、Response 按职责拿到同一会话记忆的不同受限视图：Safety 最多看最近 8 条，Context 负责恢复和压缩，Response 只消费 Context 已整理的摘要与历史。这样避免四份副本冲突、写放大和删除困难，同时保留角色隔离。

## Docker Compose 一键启动

仓库提供 `Dockerfile` 和 `docker-compose.yml`，会启动：

- `mysql`：MySQL 8.0，容器内端口 `3306`，宿主机映射 `13306`
- `redis`：Redis 7，容器内端口 `6379`，宿主机映射 `16379`
- `app`：心理ai FastAPI 服务，宿主机端口 `8080`

默认配置会让应用容器访问宿主机 Ollama：

```bash
docker compose up -d --build
```

容器 readiness 会访问 `http://127.0.0.1:8080/actuator/ready`，真实检查 MySQL 和 Redis。查看状态：

```bash
docker compose ps
curl http://127.0.0.1:8080/actuator/ready
```

如果 Ollama 已经有下列模型，容器即可使用真实本地聊天模型链路：

```text
mindbridge-qwen2.5-7b-ft:latest
```

## Chroma 向量库与快照

`app/knowledge/*.md` 只是随代码发布的“种子原文”，不是在线检索时直接扫描的文件夹。当前有 18 个来源，覆盖校园心理总则、危机安全、焦虑落地、低落、睡眠、学业、人际、新生适应、转介话术、隐私和证据边界。应用启动时 `seed_data()` 读取这些文件，优先按 Markdown 标题和段落形成语义块；只有单段超过 512 字符才按 512 字符、64 字符重叠滑窗切分。当前种子库得到 79 个 chunk。权威副本写入 MySQL `knowledge_chunks`。

每个 MySQL chunk 保存 `source`、`source_index`、`content` 和 `embedding_json`。`embedding_json` 不只是一个裸向量，还绑定 embedding 模型名和正文 SHA-256，防止更换模型或改正文后误用旧向量。Chroma 是用于近邻搜索的派生索引，默认落在 `data/chroma`；它保存 chunk id、向量和必要元数据。MySQL 是权威数据，Chroma 丢失后可通过管理员接口重建。Engineering Harness 为了完全隔离生产数据，改用 `target/harness/mindbridge-harness.sqlite3` 且关闭真实向量调用。

默认向量方案是 Chroma 1.5.9 持久化索引加本机 Ollama `qwen3-embedding:0.6b`。该模型约 639MB，本机实测输出 1024 维向量，适合中文和多语言检索。也可以把 provider 切到 OpenAI。领域门先拒绝明显无关问题；相关问题分别取最多 16 条向量候选和 16 条 BM25 候选，再通过加权 RRF、本地 reranker、0.45 相关度门和 Top 4 截断。

```env
EMBEDDING_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
EMBEDDING_BATCH_SIZE=16
EMBEDDING_TIMEOUT_SECONDS=60
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RRF_K=60
KNOWLEDGE_RERANK_ENABLED=true
KNOWLEDGE_MIN_RELEVANCE_SCORE=0.45
KNOWLEDGE_DOMAIN_GATE_ENABLED=true
CHROMA_PERSIST_DIR=data/chroma
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
```

首次安装和手动重建：

```bash
ollama pull qwen3-embedding:0.6b
python scripts/setup_rag_vector.py --local-sqlite
```

`--local-sqlite` 用于不启动 MySQL 的本机学习和验证；正式 Docker 环境会在 app 启动时自动同步 MySQL 与 Chroma，不需要再执行脚本。

分块和文档向量化发生在应用启动、管理员上传或手动重建时，不发生在每次聊天中。聊天时只把 Context 改写后的一个 query 临时向量化，再查询已经存在的 Chroma。

增量更新按 `source + chunk 正文` 处理：同名来源没有变化时完全跳过；部分内容变化时保留正文相同 chunk 的数据库 id 和 embedding 缓存，只删除旧 chunk、只为新增或修改后的 chunk 调用 Embedding，然后 upsert Chroma。切块位置变化但正文没变的 chunk 也能复用。模型 provider、模型名或正文 SHA-256 任一变化时缓存自动失效；Chroma 中若检测到另一 embedding 模型创建的 collection，会删除派生索引并从 MySQL 重建，避免混合不同向量空间。

管理员接口：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/knowledge/status
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/rebuild-vector
curl -u admin:admin123 -X POST http://127.0.0.1:8080/api/admin/knowledge/backup
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，如果 Chroma 或 embedding 服务不可用，系统会降级到本地 BM25 + 词面 rerank；设为 `true` 则启动或检索失败时直接暴露错误。

## 工具队列、限流与死信

心理报告生成后，工具链不会阻塞学生端流式回复。最终助手消息、`tool_jobs` 和 `tool_outbox` 在同一 MySQL 事务中提交；独立 Worker 再把 outbox 发布到 Redis Stream：

```text
MySQL tool_jobs/tool_outbox
        ↓
Redis Stream（mindbridge:tool-jobs）
        ↓
独立 Tool Worker（Consumer Group）
        ↓
MCP Client → MCP Server → 工具实现
```

Redis Stream 采用至少一次投递；Worker 先用 MySQL 条件更新原子认领 `PENDING` 任务，再调用 MCP，正常的重复投递会被任务状态去重。Outbox 发布租约、Stream Consumer Group、`XAUTOCLAIM`、执行超时恢复、任务依赖、延迟重试和死信共同覆盖 Redis/Worker 重启。数据库工具按 `reportId` 幂等；SMTP 本身不支持事务幂等，极端情况下（邮件已发送但成功状态尚未落库时进程崩溃）可能重复发送，生产接入应使用带幂等键的消息/邮件网关。失败任务超过 `TOOL_QUEUE_MAX_ATTEMPTS` 后进入 `dead_letter_records`。

```env
TOOL_QUEUE_ENABLED=true
TOOL_QUEUE_WORKER_ENABLED=false       # Web 进程不执行后台工具
TOOL_QUEUE_BACKEND=redis_stream
TOOL_QUEUE_STREAM=mindbridge:tool-jobs
TOOL_QUEUE_STREAM_MAXLEN=100000
TOOL_QUEUE_CONSUMER_GROUP=mindbridge-tool-workers
TOOL_QUEUE_MCP_ENABLED=true
ALERT_EMAIL_RATE_LIMIT_PER_MINUTE=30
ALERT_EMAIL_DELIVERY_MODE=log
```

Docker Compose 中的 `tool-worker` 是独立 Worker 服务；Web 服务只写事务 Outbox，不轮询或执行工具。Consumer Group 支持横向扩容，但当前单文件 Excel 台账只适合单 Worker；多 Worker 部署时应把台账替换为数据库或具备并发控制的外部存储。

`ALERT_EMAIL_DELIVERY_MODE=log` 适合本地演示；生产发邮件时改为 `smtp` 并配置 SMTP。

## 邮件预警配置

高风险消息会触发心理报告，并由后端通过 MCP 工具调用完成 Excel 台账写入和邮件预警。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
ALERT_EMAIL_FROM=your-account@example.com
ALERT_EMAIL_TO=counselor@example.com,admin@example.com
ALERT_EMAIL_SUBJECT_PREFIX=[心理ai 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert_records` 中写入 `FAILED` 记录，提示缺少的配置项。

## 接入和量化本地微调 GGUF 模型

Python 版默认预留本地模型名：

```text
mindbridge-qwen2.5-7b-ft:latest
```

模型目录：

```text
models/mindbridge-qwen2.5-7b-ft/
```

需要放入的 GGUF 权重：

```text
models/mindbridge-qwen2.5-7b-ft/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM_GGUF=/path/to/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf ./scripts/create-finetuned-model.sh
```

创建 Ollama 模型：

```bash
./scripts/create-finetuned-model.sh
```

启动 Ollama：

```bash
./scripts/start-ollama.sh
```

启动 Python 服务：

```bash
AI_PROVIDER=ollama ./scripts/run-dev.sh
```

查看模型接入状态：

```bash
curl -u student:student123 http://127.0.0.1:8080/api/agent/status
```

返回结果中的 `finetunedModel.ggufExists` 和 `finetunedModel.modelfileExists` 会显示模型资产是否就绪。
同时 `agentFramework.active` 会显示当前实际使用的 Agent 编排框架：

```text
checkpoint_workflow
```

不要只用肉眼比较两段回复。项目提供 12 个固定场景的成对 A/B：基座和微调模型使用同一个 system prompt、输入、`temperature=0`、`seed=42` 和 token 上限；评分使用“概念组覆盖 + 禁止项”，不要求模型背出某一句中文答案，并同时记录分类通过率、边界通过率、P50/P95 延迟与逐题胜负。

```bash
python scripts/evaluate_model_quality.py \
  --baseline-model qwen2.5:7b \
  --candidate-model mindbridge-qwen2.5-7b-ft:latest \
  --repeats 3
```

报告写入 `target/model-quality-comparison.json`。本机 Ollama 当前已有 `mindbridge-qwen2.5-7b-ft:latest`，但仍需要准备同参数基座模型才能进行公平 A/B；缺少任一模型时报告会写成 `BLOCKED`，不会虚构“微调提升百分比”。

## 接入 OpenAI-compatible API

```bash
AI_PROVIDER=openai \
OPENAI_API_KEY=你的_API_Key \
OPENAI_MODEL=gpt-4o-mini \
EMBEDDING_PROVIDER=openai \
OPENAI_EMBEDDING_MODEL=text-embedding-3-small \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

只有明确设置 `EMBEDDING_PROVIDER=openai` 时，知识库才使用同一个 `OPENAI_API_KEY` 调用 embeddings API；默认仍使用本地 Ollama。相关配置：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
EMBEDDING_PROVIDER=openai
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true
CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=mindbridge_knowledge
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 BM25 + `hybrid_score` reranker。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE_VECTOR_REQUIRED=true`。

## 调用示例

学生流式聊天：

```bash
curl -N -u student:student123 \
  -H 'Content-Type: application/json' \
  -d '{"requestId":"client-turn-0001","message":"我最近很焦虑，晚上总是睡不着"}' \
  http://127.0.0.1:8080/api/chat/stream
```

高风险示例，会触发心理报告、风险个案创建和预警工具计划；Excel 保留为台账输出，邮件/log 是预警通道之一：

```bash
curl -N -u student:student123 \
  -H 'Content-Type: application/json' \
  -d '{"message":"我不想活了，感觉撑不下去了"}' \
  http://127.0.0.1:8080/api/chat/stream
```

管理员查看报告：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/reports
```

管理员查看 Runtime 聚合指标：

```bash
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/runtime-metrics
```

管理员追加知识库：

```bash
curl -u admin:admin123 \
  -H 'Content-Type: application/json' \
  -d '{"source":"sleep-guide","content":"失眠时可先固定起床时间，减少睡前屏幕刺激，必要时联系校心理中心。"}' \
  http://127.0.0.1:8080/api/admin/knowledge
```

追加知识库时，系统先做大小与 Prompt 注入检查；可疑内容返回 422。通过后同步写入 MySQL 分块和 Chroma 向量库；已有分块会在首次向量检索时自动补建 Chroma 索引。

## RAG 评测

```bash
AI_PROVIDER=mock python -m app.rag_eval.runner
```

评测报告输出到：

```text
target/rag-eval-report.json
```

当前离线 Harness 使用 68 条固定样本：60 条应召回问题、8 条不应召回问题；不需要人工逐条运行。最新实测 Top 4：Recall 100%、Precision 65.83%、MRR 96.11%、NDCG 94.99%、HitRate 100%、负样本拒绝率 100%、检索决策准确率 100%。Harness 为了可重复主动关闭向量，指标代表 BM25 fallback；本机另外完成了 `qwen3-embedding:0.6b + Chroma` 的 79 条实体向量构建和查询验证，但还不能把单次查询当成完整 embedding A/B。

## 单元测试

当前有 104 个 Python 标准库 `unittest`，不依赖 `pytest`。覆盖强类型/权限、模型 JSON schema、并行屏障、事件批量事务、checkpoint 恢复、历史事件投影、旧 checkpoint 兼容、v2 任务收据部分恢复、提交失败取消、租约丢失拒写、最终文本收尾恢复、废弃置信度字段和数据库列迁移、固定状态迁移门禁、上下文压缩、outcome 重放、跨进程租约、Prompt 模板版本/hash/占位符、响应策略契约、HIGH 语义安全复审、RRF、RAG 负样本拒答、embedding 缓存版本、增量向量化、引用完整性、版本化安全策略、Skill 选择、模型 A/B 评分、注入防护、密码、限流、模型主备和最终生成生命周期：

```bash
python -m unittest discover -s tests
```

## Agent Runtime Harness

线上对话通过内部 Harness 类组织一次 Agent run；对用户展示的项目名称统一为“心理ai”。Harness 不负责内部步骤转换，而是在外层统一管理：

- 输入脱敏和 session 解析。
- Agent runtime 调用和多 Agent 协作结果接入。
- 心理报告落库和工具计划生成。
- 学生与助手消息持久化。
- Agent steps、知识召回、风险结果等 trace 数据输出。

HTTP 层负责认证和 SSE 接入；Harness 负责请求准备与业务物化，ChatService 配合 lifecycle 管理最终生成和收尾。并非 Runtime 之外所有代码都叫 Harness。

## Engineering Harness

项目提供一键工程 harness，用 mock AI、临时 SQLite、内存短期记忆和本地输出验证核心链路：

- Risk Safety Harness：高风险识别、报告生成、后台元数据不外显、工具队列入队。
- Request Idempotency：同一个 `requestId` 重试时重放相同回复，且只保留一对用户/助手消息。
- Agent Routing Harness：通过内部兼容类 `MindBridgeAgentHarness` 验证 心理ai 的 CHAT / CONSULT / RISK 路由和多 Agent 步骤。
- Standard Skills Harness：验证 `skills/*/SKILL.md` 标准 Skill 加载、选择逻辑和交接摘要模板渲染。
- RAG Harness：基于 60 条正样本和 8 条负样本验证 Recall@K、Precision@K、MRR、NDCG、HitRate、无关问题拒绝率和检索决策准确率。
- API Harness：liveness/readiness、认证授权、SSE 聊天、requestId 幂等、Prompt 注入指标和 RAG 投毒拒绝。
- Tool Queue Harness：Excel / case / alert 依赖、幂等、限流和 dead letter。

```bash
python -m app.harness.runner
# 隔离输出，保留历史报告；目录必须位于项目 target/ 下
python -m app.harness.runner --suite all --output-dir target/harness-workflow-v2-20260912
```

报告输出到：

```text
target/harness/harness-report.json
target/harness/rag-eval-report.json
```

2026-09-12 上述独立输出目录中的六套 Harness 全部通过。此结果使用 SQLite、mock 和关闭向量的可重复环境，不代表真实 MySQL 并发锁语义、GPU 性能或模型正确率已完成验证。

## MCP 工具服务

MCP Python 包建议使用 Python 3.10 或 3.11 安装运行。

```bash
python -m app.mcp_tools.server
```

业务后端触发报告后处理时，只走 MySQL 事务 Outbox + Redis Stream 异步投递，再由独立 Worker 作为 MCP client 通过 stdio 启动同一个 MCP server。`TOOL_QUEUE_ENABLED=false` 仅暂停 Worker 消费，已提交的任务仍保留在 MySQL，恢复后会继续发布和执行。

暴露工具：

- `mindbridge_excel_report`
- `mindbridge_case_create`
- `mindbridge_alert_send`
- `mindbridge_alert_ack`
- `mindbridge_case_note_add`
- `mindbridge_alert_notify`

内置标准 Skills 位于 `skills/*/SKILL.md`，运行时由内部兼容类 `MindBridgeSkillRegistry` 加载：

- `supportive_response_baseline`：支持与危机回复共同使用的共情、非诊断和简洁表达底座。
- `campus_support_toolkit`：把焦虑、睡眠、学业、低落、人际、适应和现实转介合并成一个校园支持工具箱；模型每次只选最相关的一到两个方法。
- `high_risk_safety_plan`：`intent=RISK` 或 `risk=HIGH` 时使用，固定当前安全、现实支持和一个安全问题的优先顺序。
- `counselor_handoff_summary`：只给后台授权工作人员生成最少必要的个案交接摘要，不进入学生回复。

学生回复 Skill 不再按中文关键词叠加，而是只由 Understanding 的原始 `intent` 和 Safety 的 `risk` 查完整九宫格：

| intent \ risk | LOW | MEDIUM | HIGH |
| --- | --- | --- | --- |
| CHAT | 不加载 Skill | baseline + toolkit | baseline + high_risk |
| CONSULT | baseline + toolkit | baseline + toolkit | baseline + high_risk |
| RISK | baseline + high_risk | baseline + high_risk | baseline + high_risk |

`CHAT + LOW` 跳过 Context，所以不需要额外 Skill；`intent=RISK` 或 `risk=HIGH` 一律走危机组合，避免两个分类器意见不一致时漏掉安全约束；其余需要支持的路径走基础规范加校园工具箱。完整 Skill 文件供人审阅，模型只接收 `## Prompt 规则` 小节。Skill 只规定“怎样回复”，风险等级仍由 Safety 决定，知识事实仍由 RAG 提供。
