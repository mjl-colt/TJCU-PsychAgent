# 心理ai Runtime 整体流程：用一次咨询从头走到结束

这份文档只讲一次请求实际怎样运行。例子中的 ID 为了方便阅读做了简化。

小林发送：

> 我最近连续几天睡不好，白天没有精神，想到考试就很焦虑，今晚可以先做什么？

```text
用户 ID：2
会话 ID：session-xiaolin-01
请求 ID：req-sleep-001
```

一次完整执行如下：

```text
浏览器提交消息
→ 后端校验、脱敏并取得请求租约
→ 新建或恢复 Blackboard
→ TURN_STARTED
→ Understanding 与 Safety 并行
→ 根据两者结果选择 CONSULT
→ Context 读取记忆、压缩历史、检索 RAG、选择 Skill
→ Response 组装 Prompt v1
→ Safety 审查 Prompt v1
→ Response 确认 Prompt
→ READY_FOR_GENERATION
→ SSE 生成最终回复
→ 保存助手消息和工具任务
→ COMPLETED
```

当前 Runtime 没有额外的 Inbox 状态，也没有 `flow.completed` 这种可由阶段推导的重复字段。请求有没有开始、正在做什么、是否完成，直接看 `flow.current_stage`；批次进度看 `active_batch`；并发处理权看数据库租约。但要特别注意：`current_stage` 只是“此刻在哪里”，不会自己产生下一次状态变化；Coordinator 必须收到 Event，才知道“刚刚发生了什么”，并据此计算新 Blackboard。

---

## 1. 浏览器提交请求

请求内容可以简化为：

```json
{
  "sessionId": "session-xiaolin-01",
  "requestId": "req-sleep-001",
  "message": "我最近连续几天睡不好，白天没有精神，想到考试就很焦虑，今晚可以先做什么？"
}
```

`requestId` 是这次处理的唯一编号。网络重连、刷新页面或者前端重试时，继续使用 `req-sleep-001`。如果每次重试都换新编号，后端就会把它们当成多次新咨询。

后端首先执行：

1. 去掉输入首尾空白；
2. 检查输入是否为空、是否超过长度上限；
3. 使用 `PrivacySanitizer` 生成发给模型的 `model_input`；
4. 调用 Prompt 注入扫描，记录“忽略之前规则”“泄露 system prompt”等可疑信号。

用户原文是业务聊天内容；`model_input` 是脱敏后供 Agent 和模型使用的内容。原文从一开始就不会进入 Blackboard，Runtime checkpoint 只能保存 `model_input`；聊天业务表仍会保存原文，二者用途不同。

---

## 2. 数据库租约防止两个进程同时处理

后端为 `req-sleep-001` 获取一份带有效期的数据库租约：

```text
request_id = req-sleep-001
owner_id = 当前进程和本次调用
lease_until = 当前时间 + 120 秒（默认值）
```

可以把租约理解成“这张请求单当前归谁处理”。如果 A 进程持有未过期租约，B 进程同时收到相同 requestId，会得到 409，不会再运行一遍 Agent。

SSE 可能持续较长时间，因此生成过程中会续租，投递后置工具前也会续租。正常结束后释放；进程突然退出时无法释放，但租约到期后其他进程可以接管。

租约负责并发处理权，`flow` 负责执行进度，两者职责不同。

### 为什么项目没有只用 Redis 分布式锁

先纠正一个容易说错的概念：数据库租约本身也是一种分布式协调机制。多个应用实例访问同一个 MySQL，都通过 `request_id` 唯一行竞争执行权。因此这里真正比较的是：

```text
方案 A：Redis 中的一把短期分布式锁
方案 B：MySQL 中带 owner_id、lease_until 的可续期租约
```

心理ai的一轮 Agent 请求不是几十毫秒的临界区。它可能经历并行模型调用、RAG、Prompt 审查、最终生成、SSE 和工具投递，持续数秒甚至更久；进程崩溃后还必须从同一 requestId 的 checkpoint 恢复。当前 checkpoint、事件、业务物化收据和幂等约束都以 MySQL 为权威数据，因此执行所有权也放在 MySQL 更容易形成一套可观察的恢复依据。

| 对比点 | 只用 Redis 锁 | 当前 MySQL 租约 |
|---|---|---|
| 主要优势 | 抢锁快，适合高频短临界区 | 所有权与 checkpoint 位于同一权威存储 |
| 进程崩溃 | 等 Redis TTL 后重新抢锁 | 等 `lease_until` 后接管并读取 checkpoint |
| Redis 重启或故障 | 锁可能不可用或丢失，需要额外处理 | 核心恢复不依赖 Redis；Redis 当前只做缓存/限流 |
| 运维排查 | 还要把 Redis 锁和 MySQL 状态对起来 | 查 requestId 就能看到租约、checkpoint、事件和业务收据 |
| 一致性难点 | 锁在 Redis、业务状态在 MySQL，存在双写窗口 | 仍需幂等，但少了一套“锁与业务状态”的跨存储协调 |
| 性能 | 更适合大量瞬时竞争 | 适合当前低冲突、长任务、以恢复为重点的场景 |

以进程崩溃为例：

```text
10:00:00  实例 A 写入 owner=A，lease_until=10:02:00
10:00:20  A 已完成 Understanding，outcome 和 checkpoint 已进 MySQL
10:00:30  A 进程崩溃，来不及主动释放
10:01:00  实例 B 尝试获取，发现租约未过期，返回 409
10:02:01  B 原子更新为 owner=B，取得执行权
10:02:02  B 读取同一 MySQL 中的 checkpoint 和 outcome
10:02:03  B 不再调用已完成的 Understanding，只续跑缺失步骤
```

如果只使用 Redis 锁，B 取得新锁后仍然必须回到 MySQL 判断执行到了哪里，还要处理“Redis 加锁成功但 MySQL 写 checkpoint 失败”“Redis 锁已过期但旧实例仍在运行”等跨存储状态。Redis 锁不能代替 checkpoint、唯一约束和业务幂等。

当前租约的安全细节是：

1. `request_id` 在 `agent_runtime_leases` 中唯一，两个实例不能同时插入两行；
2. 获取时只有租约已过期或 owner 仍是自己才可更新；
3. 续租和释放都必须同时匹配 `request_id + owner_id`，旧实例不能删除新 owner 的租约；
4. SSE 期间按 TTL 的约三分之一续租，续租失败立即停止继续生成；
5. 租约只实现“同一时刻尽量只有一个执行者”，不承诺 exactly-once；最终仍由 commandId、eventId、业务唯一键和 materialization 防止重复副作用。

所以选择结论不是“数据库技术比 Redis 高级”，而是当前场景更看重长任务恢复、一致的权威数据和较低的竞争量。以后并发量增大，可以在前面增加 Redis 短锁快速挡住热点重复请求，但 MySQL 租约和业务幂等仍作为最终执行依据。

---

## 3. 判断新执行还是恢复执行

Harness 检查三件事：

1. `session-xiaolin-01` 是否属于用户 2；
2. MySQL 是否已有 `req-sleep-001` 的 checkpoint；
3. 已有 checkpoint 中的用户、会话、脱敏输入是否与本次调用一致。

同一个 requestId 不能换用户、换会话或换输入：

```text
第一次：req-sleep-001 + “想到考试就很焦虑”
第二次：req-sleep-001 + “帮我写 Python”
结果：拒绝复用 requestId
```

没有 checkpoint 就是新执行；有 checkpoint 就恢复已有 Blackboard。新会话会先写入 `chat_sessions`，随后进入 Runtime。

---

## 4. 在内存中创建 Blackboard revision 0

新请求先创建强类型 Blackboard：

```yaml
revision: 0
request:
  request_id: req-sleep-001
  user_id: 2
  session_id: session-xiaolin-01
  model_input: 我最近连续几天睡不好……
  prompt_injection_signals: []
flow:
  current_stage: RECEIVED
  active_batch: null
understanding: null
safety: null
context: null
response: null
```

revision 0 此时只在内存中存在。Runtime 另外创建一个 `RuntimeEvent(type=TURN_STARTED)`，再把这个事件放进进程内的 `asyncio.Queue`，事件到达后才唤醒 Coordinator。

`TURN_STARTED` 不是 `BlackboardState` 的字段。虽然 `RuntimeEventType` 和 `BlackboardState` 的类型定义目前都放在 `blackboard.py`，但它们是两个不同对象：

```text
BlackboardState
  保存：当前状态快照
  例如：current_stage=RECEIVED

RuntimeEvent
  保存：刚刚发生的事情
  例如：type=TURN_STARTED、actor=Runtime
```

因此在 checkpoint 的 `state_json` 里找不到一个叫 `TURN_STARTED` 的 Blackboard 字段，这是正常的。事件消费后会单独写入 MySQL `agent_runtime_events`；变化后的 Blackboard 则写入 `agent_runtime_checkpoints`。

如果进程在这里崩溃，没有调用任何 Agent，也没有产生业务副作用。租约过期后，相同 requestId 从头创建即可。

### 为什么已经有 current_stage，还需要 Event

一次状态转移不是“只看 current_stage”，而是：

```text
新 Blackboard = Coordinator.handle(旧 Blackboard, 本次 Event)
```

两者分工如下：

| 内容 | 回答的问题 | 例子 |
|---|---|---|
| `flow.current_stage` | 现在处于什么阶段、这个事件在当前阶段是否合法 | `ANALYZING` |
| `flow.active_batch` | 当前批次等哪些命令、哪些已经完成 | 等 Understanding 和 Safety |
| `RuntimeEvent` | 刚刚发生了什么、谁完成了、结果属于哪个命令 | `AGENT_COMPLETED(command-understanding-01)` |

以并行分析为例，Understanding 完成后和 Safety 完成前，`current_stage` 都是 `ANALYZING`：

```yaml
# Understanding 完成前
flow:
  current_stage: ANALYZING
  active_batch:
    completed_command_ids: []

# 收到 Understanding 的 AGENT_COMPLETED 以后
flow:
  current_stage: ANALYZING
  active_batch:
    completed_command_ids: [command-understanding-01]
```

只看 `current_stage=ANALYZING`，无法知道是两个 Agent 都没完成，还是已经完成一个。`AGENT_COMPLETED` 事件会携带 `batch_id`、`command_id`、Agent outcome、重试次数和耗时，Coordinator 才能把正确的 commandId 加进屏障。

随后 Safety 的完成事件到达：

```text
AGENT_COMPLETED(command-safety-01)
→ Coordinator 检查两个预期 commandId 都已完成
→ 才把 current_stage 从 ANALYZING 改为 ROUTED
```

如果完全删除 Event，只保留 `current_stage`，Runtime 要么不断轮询 Blackboard 猜“有没有变化”，要么让 Agent 直接调用 Coordinator。前者重新引入轮询，后者把 batchId、commandId、去重、重放和审计隐藏在方法调用里，恢复时也不知道哪个模型结果已经完成。

Event 在当前 Runtime 中具体解决五件事：

1. Queue 有新事件才唤醒 Coordinator，不轮询 Blackboard；
2. 携带 Agent outcome，让状态机知道应该合并什么结果；
3. 用 eventId、commandId 和 batchId 去重并校验结果归属；
4. outcome 已持久化时可以重放，不重复调用已经完成的模型；
5. event journal 保留“为什么变成这个状态”的审计过程。

`TURN_STARTED` 单独来看可以改成一次直接的 `Coordinator.start()` 调用，但保留它可以让“首次执行”和“从 RECEIVED checkpoint 恢复”走同一个事件入口。即使以后精简这个启动事件，`AGENT_COMPLETED`、`AGENT_FAILED` 和重试事件仍然不能只靠 `current_stage` 替代。

---

## 5. Coordinator 创建首批命令，第一次保存 checkpoint

Coordinator 消费 `TURN_STARTED`，把阶段改为 `ANALYZING`，revision 从 0 变成 1，并创建同一批次的两个命令：

```text
batch_id = batch-01

command-understanding-01 → UnderstandingAgent / UNDERSTAND
command-safety-01 → SafetyAgent / ASSESS_RISK
```

Blackboard 现在包含：

```yaml
revision: 1
flow:
  current_stage: ANALYZING
  active_batch:
    batch_id: batch-01
    expected:
      command-understanding-01: UnderstandingAgent
      command-safety-01: SafetyAgent
    completed_command_ids: []
```

此时 Runtime 在一次 MySQL 事务中写入：

- `TURN_STARTED` 事件；
- `STATE_UPDATED` 事件；
- revision 1 的完整 checkpoint；
- 最后一条事件上的隐私安全状态投影和 hash。

这是新请求的第一个持久恢复点。状态和事件要么一起提交，要么一起回滚。

随后 Runtime 把 `AGENT_BATCH_REQUESTED` 放入事件队列。下一步要执行谁可以直接从 `active_batch.expected` 看出，不再额外保存 `next_agents`。真正触发执行的是命令和批次事件。

---

## 6. 调用模型前先保存“已开始”事件

Runtime 消费 `AGENT_BATCH_REQUESTED` 后，先把这些事件写入 MySQL：

```text
AGENT_BATCH_REQUESTED(batch-01)
AGENT_STARTED(command-understanding-01)
AGENT_STARTED(command-safety-01)
```

然后才调用 Agent。这样进程在模型请求中崩溃时，恢复器能看出哪些命令已经调度但还没有结果。

调用前还会执行固定门禁：

- Blackboard 已经是终态时禁止继续派发；
- 批次不能为空；
- commandId 不能重复；
- 一次调度中的命令必须属于同一 batchId；
- 命令必须绑定当前 Blackboard revision。

这些规则直接属于 Runtime，不能通过配置关闭。

---

## 7. Understanding 和 Safety 真正并行

Dispatcher 使用 `asyncio.gather` 并行执行两个 Agent：

```text
0ms   Understanding ────────────── 约 80ms 完成
0ms   Safety        ─────────────────── 约 120ms 完成
总等待接近 120ms，而不是 200ms
```

Dispatcher 还负责并发限制、超时、重试、退避和降级。两个 Agent 都只返回局部结果，不直接修改 Blackboard。

本例可能得到：

```yaml
UnderstandingAgent:
  intent: CONSULT
  topic: 考试压力和睡眠问题
  reason: 压力、焦虑和睡眠求助

SafetyAgent:
  risk_level: LOW
  emotion: ANXIETY
  assessment_method: MODEL_WITH_POLICY_FALLBACK:safety-signals-v3
```

Safety 会先执行自杀、自伤等确定性硬规则。模型异常且没有命中硬规则时至少降级为 `MEDIUM`；硬规则命中时为 `HIGH`。

四 Agent 主流程现在完全不使用置信度。Understanding 模型只返回 `intent + reason`；Safety 模型只返回 `emotion + emotionScore + risk + summary`。原因是 LLM 自己填写的 0.82 没有经过真实标注集校准，硬规则和 fallback 过去又使用写死的 0.95、0.60，把它们放在一个字段里会制造“精确概率”的假象。分类输出非法、超时或无法解析时直接保守进入 `CONSULT`；高风险由独立并行的 Safety 硬规则和模型负责，不依赖置信度阈值。Runtime 只保存最终 `intent`、`risk_level`、`reason`、`risk_signals` 和 `assessment_method`。

旧 checkpoint 中的 `intent_confidence`、`risk_confidence` 会在 Pydantic 兼容读取时丢弃，不会重新写入新 checkpoint。应用启动时还会删除旧 `psychological_reports.confidence` 数据库列；管理员 API、Excel 台账、交接 Skill 和告警邮件都不再显示这个数字。已有 Excel 首次再次写入时会自动移除旧 confidence 列。

---

## 8. Runtime 校验局部结果并合并成一个版本

Dispatcher 收齐这一批结果后，Runtime 检查：

1. 两个结果是否基于当前 revision；
2. Understanding 是否只写 `understanding`；
3. Safety 是否只写 `safety`；
4. 返回数据是否符合 Pydantic 类型；
5. 同一批是否重复写同一个分区。

所有有效结果一次合并，Blackboard 只产生一个新 revision。Agent 不会互相覆盖共享状态。

如果某个 Agent 返回非法结构，该结果会变成明确的失败 outcome。其他有效结果可以保留；Coordinator 会根据失败或保守降级状态继续处理，不会把缺少 Safety 结论的情况当成正常低风险。

合并后的 checkpoint 和 `AGENT_COMPLETED/AGENT_FAILED` 先写入 MySQL，提交成功后完成事件才进入 Coordinator。恢复时可以重放已有 outcome，不必再次调用已经完成的模型。

---

## 9. 两个完成事件构成并行屏障

Understanding 先完成时，Coordinator 只把它的 commandId 加入 `completed_command_ids`，不会立即路由。Safety 完成后，两个预期 commandId 都齐全，屏障才打开。

本例的路由顺序是：

```text
HIGH 风险或风险意图 → RISK
CONSULT 意图或 MEDIUM 风险 → CONSULT
其余 CHAT + LOW → CHAT
```

小林的结果是 `CONSULT + LOW`，因此进入 Context。普通 `CHAT + LOW` 会跳过完整 RAG，直接准备 Response Prompt。

---

## 10. Context 读取记忆并记录压缩生命周期

Coordinator 创建 `command-context-01`。在 ContextAgent 执行前，Runtime 先保存：

```text
AGENT_STARTED(command-context-01)
CONTEXT_COMPACTION_STARTED(command-context-01)
```

Context 先从 Redis 读取本会话近期对话。Redis 没有数据时，从 MySQL `chat_messages` 加载并回填 Redis。

这里使用的是“用户与助手真实说过什么”的会话记忆，不是每个 Agent 自己记录结论的私有记忆。旧的四 Agent 私有记忆已经删除：Safety/Context 原来只写不读，Understanding/Response 保存的内容又与当前 Blackboard 重复，还可能把旧判断带入新请求。

---

## 11. 历史太长时怎样压缩

假设历史有 30 条，Context 会保留最近消息，把较早内容压缩成摘要：

```text
较早 22 条 → memory_brief 摘要
最近 8 条 → 保留原消息
当前输入 → 始终保留
```

结果携带审计信息：

```yaml
compaction_id: command-context-01
source_message_count: 30
retained_message_count: 8
compacted: true
summary_chars: 286
summary_hash: 8c2f...  # 示例
```

完成后记录 `CONTEXT_COMPACTION_COMPLETED`。失败时记录 `CONTEXT_COMPACTION_FAILED`。如果只出现 STARTED 而没有结束事件，恢复器会识别中断，并沿用相同 commandId 重做。

---

## 12. Context 执行 RAG 和 Skill 选择

先把“知识到底在哪”说清楚。`app/knowledge/*.md` 是随代码发布的 18 份种子原文；服务启动时 `seed_data()` 优先按 Markdown 标题和段落切块，只有超长段落才按 512 字符、64 字符重叠滑窗，当前得到 79 个 chunk。chunk 权威副本存入 MySQL `knowledge_chunks`；`embedding_json` 同时绑定 embedding 模型名、正文 hash 和向量缓存。Chroma 的 `data/chroma` 是可重建的近邻索引，不是唯一数据源。Harness 改用 `target/harness/mindbridge-harness.sqlite3` 并关闭真实向量，所以不能把测试 SQLite 当生产向量库。

Context 将口语问题改写为检索词：

```text
考试压力 焦虑 连续失眠 今晚应对
```

检索不是“只要搜到东西就塞给模型”，而是按下面顺序执行：

```text
1. Query 改写防漂移
   如果原问题有心理领域词，而模型改写后把这些词丢了，就退回原问题

2. 领域门
   “考试压力、焦虑、失眠”允许进入心理知识库
   “北京明天天气”直接返回空，不让中文常用字制造假命中

3. 候选召回
   Chroma 最多取 16 条 + BM25 最多取 16 条

4. 加权 RRF 融合
   不直接相加 cosine 和 BM25 原始分数，而按两路排名位置融合
   默认向量权重 0.65、BM25 权重 0.35、RRF k=60

5. 本地 rerank
   综合 RRF、词项相似度、query 覆盖率和完整短语命中

6. 证据门
   分数低于 0.45 的结果丢弃，最终最多保留 Top 4

7. 上下文与安全
   扩展第一名的相邻 chunk，清除间接 Prompt 注入，为每条证据编号 K1、K2……
```

为什么改成 RRF：向量相似度和 BM25 的原始分数不是同一种量纲，直接归一化相加容易受本批候选极值影响；RRF 只看各自排名，更适合融合异构检索结果。Microsoft 的混合检索同样使用 RRF，公式核心是 `1/(rank+k)`：[RRF 官方说明](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)。

知识库内容仍被当成不可信资料；上传时不仅扫描正文，也拒绝带路径、换行、标签或注入指令的来源名；检索出口会再次清除“忽略系统规则”“调用工具”等间接 Prompt 注入内容。

本例可能选择：

```yaml
skills:
  - supportive_response_baseline
  - campus_support_toolkit
knowledge:
  - K1: sleep-routine-self-care.md，睡前降低刺激和强光
  - K2: academic-stress-and-burnout.md，先选择一个最小学习步骤
  - K3: anxiety-panic-grounding.md，简短呼吸和落地练习
```

为什么只有两个 Skill：本例的原始结果是 `intent=CONSULT`、`risk=LOW`，Context 直接查九宫格得到 `baseline + toolkit`。原来的焦虑、睡眠、学业等七个主题 Skill 已合并进 `campus_support_toolkit`；工具箱本轮只挑相关的一到两个方法，不会把所有模块全部输出。

完整九宫格如下：

| intent \ risk | LOW | MEDIUM | HIGH |
| --- | --- | --- | --- |
| CHAT | 无 | baseline + toolkit | baseline + high_risk |
| CONSULT | baseline + toolkit | baseline + toolkit | baseline + high_risk |
| RISK | baseline + high_risk | baseline + high_risk | baseline + high_risk |

其中 `high_risk` 是 `high_risk_safety_plan`。只要 `intent=RISK` 或 `risk=HIGH` 就按危机路径处理；`counselor_handoff_summary` 只在后台报告后处理时使用，不属于这张学生回复九宫格。

embedding 缓存绑定 `provider + embedding model + chunk 正文 SHA-256`。例如以后从本地 `qwen3-embedding:0.6b` 切到 OpenAI 模型，或者正文被修改，旧向量会被判定失效并重新生成，避免“新 query 向量查询旧文档向量”。

Context 是唯一直接检索 RAG、选择 Skill 的 Agent。它返回 `context` 分区，Runtime 校验、合并和持久化后继续。

---

## 13. Response 组装 Prompt v1

ResponseAgent 读取已经结构化的数据：

```text
意图：CONSULT
风险：LOW
情绪：焦虑
当前问题：考试压力和连续失眠
记忆摘要：此前临近考试也出现入睡困难
RAG：K1、K2、K3 三条带来源标签的证据
Skill：睡眠、焦虑、学业压力支持规则
```

它先生成供最终模型使用的消息：

```yaml
response:
  prompt_version: 1
  generation_status: WAITING_FOR_SAFETY_REVIEW
  prompt_hash: <Prompt 的 SHA-256>
  messages:
    - system: 心理支持边界和回答要求
    - context: 风险、记忆、RAG、Skill
    - user: 脱敏后的当前输入
```

Prompt 有总长度预算。历史、知识和 Skill 超出预算时会在实际消息层截断。只要本轮有 RAG 证据，Prompt 还会明确要求：知识性结论必须在句末标注 `[K1]` 这类真实标签；资料不足时说明无法确认；不得编造 `[K9]`。

---

## 14. Safety 审查同一个 Prompt 版本

Safety 第二次执行 `REVIEW_RESPONSE`。它检查 Prompt 是否满足当前风险约束、是否越界诊断、是否缺少必要求助信息、是否超过长度、版本是否一致，以及 K1～Kn 是否完整进入 Prompt、是否存在“不得伪造引用”和“资料不足就说明”的规则。

```yaml
prompt_review:
  prompt_version: 1
  approved: true
  issues: []
```

Safety 批准 v1 后，如果 Response 又改成 v2，原批准立即失效，v2 必须重新审查。审查不通过时，Response 根据 issues 产生新版本；超过最大修订次数后本轮失败。

Response 最后执行 `FINALIZE_RESPONSE`，把状态改为 `READY_FOR_GENERATION`。

---

## 15. 固定状态迁移门禁再次检查

每次 Blackboard 状态变化都必须通过固定门禁：

- requestId、用户、会话和输入不能在中途被修改；
- revision 不能倒退；
- 进入 `READY_FOR_GENERATION`、`GENERATING` 或 `COMPLETED` 时，必须存在已批准且版本一致的 Prompt Review。

例如 Response 当前是 v2、Safety 批准的是 v1，门禁会直接拒绝状态变化，不会写入 checkpoint。

这些检查是 Runtime 必经的固定代码路径，不是可以被配置跳过的插件。事件所需的 `runtimeStage` 和 `runtimeRevision` 也由 Runtime 直接添加。

---

## 16. READY_FOR_GENERATION 还不算完成

Prompt 审查通过后的状态是：

```yaml
flow:
  current_stage: READY_FOR_GENERATION
response:
  prompt_version: 1
  generation_status: READY_FOR_GENERATION
  final_response: null
```

Runtime 保存 `TURN_READY_FOR_GENERATION`、状态投影和 checkpoint，然后把控制权交给 SSE 生成阶段。

浏览器此时断线也不会丢掉四个 Agent 的结果。使用相同 requestId 重试，可以从这个 checkpoint 继续。

---

## 17. Harness 只落地一次业务数据

SSE 开始前，Harness 查询 `AgentTurnMaterialization`，它是 requestId 的业务落地收据。

第一次执行会在一个业务事务中保存：

- 用户原文 `ChatMessage`；
- 必要时生成的 `PsychologicalReport`；
- `AgentRunTrace`；
- 关联这些记录的 `AgentTurnMaterialization`。

相同 requestId 重试时，如果收据已经存在，就不会再插入一条用户消息或再生成一份报告。事务提交后，用户消息还会追加到 Redis 短期记忆。

---

## 18. SSE 生成开始也要保存 checkpoint

流式响应使用自己的数据库 Session。浏览器先收到 `meta`，随后 `GenerationLifecycle.started()` 把状态改为：

```yaml
flow:
  current_stage: GENERATING
```

`GENERATION_STARTED`、状态投影和 checkpoint 会立即写入 MySQL。因此数据库能区分“Prompt 已准备但未生成”和“生成过程中断”。

模型网关支持主模型、备用模型和熔断。只有尚未向用户发送 token 时才能切换备用模型，避免拼接两个模型的半段回复。

CONSULT 和 RISK 会先在服务器内存中缓冲完整回答，通过最终输出检查后再发送。普通 CHAT 为降低延迟可以直接流式输出。支持类回答如果使用了 RAG，却没有引用任何 K 标签，或者引用了不存在的 K9，会被安全兜底替换；HIGH 风险以立即安全支持为优先，不强制为了格式加入引用。

HIGH 比普通支持多一道语义复审。先由代码规则拦诊断、药量、危险细节、提示词泄露和错误引用；再让独立 Safety 模型只填写七个布尔字段，检查回复含义是否完成“回应痛苦、处理眼前危险、连接现实中的人、必要时紧急升级”。这里不要求回复原样出现“安全”“可信任”等中文。语义模型超时、报错或 JSON 不符合 schema 时一律拒绝，改发内置高风险兜底；由于正文尚未发给浏览器，不会发生“先泄漏半段再撤回”。

---

## 19. 保存最终回复并进入 COMPLETED

生成完整文字后，系统依次执行：

1. 保存 assistant `ChatMessage`；
2. 写入 `AgentTurnMaterialization.final_response`；
3. 追加 Redis 短期记忆；
4. 按需向持久工具队列投递报告或通知任务；
5. 标记工具已经派发；
6. 调用 `GenerationLifecycle.completed()`。

最终状态：

```yaml
flow:
  current_stage: COMPLETED
response:
  final_response: 可以先固定明早起床时间，并在睡前降低刺激。[K1]……
```

Runtime 保存 `GENERATION_COMPLETED` 和 `TURN_COMPLETED`。完成事件还记录 `citedEvidenceIds: [K1]`，以后可以审计这次回答实际用了哪些证据。浏览器收到 `done`。

`COMPLETED` 表示最终文本已经生成并完成持久化；Prompt 仅完成编排时仍是 `READY_FOR_GENERATION`。

---

## 20. checkpoint 到底什么时候存、存在哪里

| 时刻 | 保存的状态或事件 | 用途 |
|---|---|---|
| Coordinator 创建首批命令 | `ANALYZING` revision 1、TURN_STARTED、STATE_UPDATED | 第一个恢复点，保存活动批次 |
| 模型调用之前 | AGENT_BATCH_REQUESTED、AGENT_STARTED | 识别调用中断 |
| Agent 批次返回 | 局部结果、outcome、合并后的 checkpoint | 已完成模型无需重调 |
| Coordinator 消费每个结果 | completed commandId、路由或下一批命令 | 保存屏障进度 |
| Context 开始与结束 | COMPACTION_STARTED/COMPLETED/FAILED | 识别半截摘要 |
| Prompt 审查和修订 | Prompt、版本、Safety Review | 恢复版本对应关系 |
| Prompt 准备完成 | READY_FOR_GENERATION | 浏览器断线后直接恢复生成 |
| SSE 开始 | GENERATING | 识别生成中断 |
| 最终文本保存 | COMPLETED、final_response | 安全重放最终回答 |

MySQL 中有两类 Runtime 数据：

```text
agent_runtime_checkpoints
  每个 requestId 最新的一份完整 Blackboard
  completed 数据库列只是从 stage 是否为 COMPLETED/FAILED 推导出的查询索引，不再复制进 Blackboard

agent_runtime_events
  按发生顺序追加的事件流水，每条 event_id 唯一
```

同一次状态推进的事件和 checkpoint 由 `save_many()` 在一个事务中提交。Blackboard 的 RequestState 只包含脱敏后的 `model_input`，没有用户原文字段；原文只由聊天业务表管理。JSON 列使用 `LONGTEXT`，避免完整状态投影接近普通 `TEXT` 上限。

Redis 主要保存近期会话和 Agent 私有短期记忆，不是 Runtime 恢复的唯一依据。

---

## 21. 状态投影怎样帮助恢复

当 revision 前进时，最后一条事件附带经过隐私处理的完整状态投影：

```yaml
state_projection:
  revision: 12
  state: <当时的完整 Blackboard>
metadata:
  stateProjectionHash: <SHA-256>
```

恢复时会：

1. 按数据库顺序读取该 requestId 的事件；
2. 检查事件是否混入其他 requestId；
3. 检查 revision 是否倒退；
4. 重新计算 hash；
5. 取得最新投影并与 checkpoint revision 比较；
6. 使用没有落后的状态继续执行。

投影链校验失败时记录错误并回退 checkpoint。管理接口只返回 revision、stage 和 hash 等摘要，不返回完整 Prompt、历史和状态投影。

---

## 22. 一次请求的六层防重复

| 位置 | 唯一依据 | 防止的问题 |
|---|---|---|
| 请求入口 | requestId | 把重连识别为同一次请求 |
| 数据库租约 | requestId + owner | 两个进程同时处理 |
| 恢复校验 | requestId + 用户 + 会话 + 输入 | requestId 被换人或换内容使用 |
| Runtime 事件 | event_id | 相同事件重复写入 |
| Agent 命令 | batch_id + command_id | 已完成 Agent 被重复调用 |
| 业务和工具 | materialization + 业务幂等键 | 重复消息、报告和工具副作用 |

---

## 23. 程序在不同位置崩溃会怎样

| 崩溃位置 | 恢复行为 |
|---|---|
| revision 0 建立后、首个 checkpoint 前 | 没有 Agent 调用；租约过期后从头执行 |
| 会话已创建、首个 checkpoint 前 | 可能留下空会话；重试可继续，后台应清理长期空会话 |
| active batch 已保存、尚未调用 Agent | 按原 batchId 和 commandId 重新派发 |
| AGENT_STARTED 后、outcome 前 | 重新执行缺少结果的命令 |
| outcome 已保存、Coordinator 尚未消费 | 重放 outcome，不再调用已完成模型 |
| Context 压缩只有 STARTED | 识别中断并以相同 commandId 重做 |
| READY_FOR_GENERATION 后断线 | 直接恢复最终生成阶段 |
| GENERATING 时进程崩溃 | 加载后退回 READY_FOR_GENERATION，再重试生成 |
| assistant 消息已保存、Runtime 终态未写 | 从 materialization 重放 final_response，并补齐终态 |
| 工具已入队、派发标记未保存 | 工具队列使用业务幂等键拒绝重复副作用 |

---

## 24. 当前稳定状态为什么没有那些重复字段

心理ai当前“一次 HTTP 请求对应一个 Blackboard”。稳定实现只保存不能从别处直接得到、且恢复或后续决策确实会使用的数据：

| 设计 | 当前处理方式 |
|---|---|
| 上下文压缩生命周期 | 保留 STARTED、COMPLETED、FAILED，支持中断识别 |
| 事件投影 | 保留完整投影、hash 校验和 checkpoint 对照 |
| 请求 Inbox 状态 | 不保存；当前阶段表达进度，租约表达处理权 |
| 通用 Runtime 插件框架 | 不保留；安全不变量是固定门禁，审计坐标直接写事件 |
| `flow.completed` | 不保存；由 `current_stage == COMPLETED/FAILED` 推导，数据库只保留便于查询的索引列 |
| 用户原文和 `model_input` 两份副本 | Blackboard 只保存脱敏后的 `model_input` |
| `candidate_prompt` 和 `messages` 两份 Prompt | 只保存真正发给模型并被 Safety 审查的 `messages` |
| `final_context` 和 RAG 证据两份上下文 | 只保存带来源、分数的证据，Response 使用时现场组装 |
| `next_agents` | 不保存；从 `active_batch.expected` 推导 |
| HIGH 风险布尔别名 | 不保存；直接使用 `risk_level` |
| Agent `steps` 和事件各存一份 | 只保存 Runtime 事件；需要展示步骤时现场投影 |
| 可切换 Runtime 配置 | 不保留假开关；当前唯一正式实现就是事件驱动 Blackboard |

上下文压缩生命周期和事件投影继续保留，因为它们分别解决“压缩中途崩溃如何识别”和“checkpoint 损坏时如何校验、回放”的独立问题。以后只有在一个长期会话执行体需要同时接收多条消息时，才重新评估真正的多消息 Inbox；只有出现第三方可选扩展时，才设计插件协议。

---

## 25. 当前仍需解决的生产边界

1. 事件队列仍是单进程 `asyncio.Queue`，横向扩展需要消息队列、ACK、重放和死信队列。
2. checkpoint 和状态投影会增加数据库写量，需要做归档、分区和容量压测。
3. SHA-256 可以发现数据损坏，不能证明数据没有被有权限的人改动；高合规场景需要签名或不可变审计存储。
4. 普通 CHAT 直接流 token，最终输出保护弱于缓冲后的 CONSULT/RISK。
5. 模型返回零 token 时会记录生成失败，但传输层目前仍可能发送 `done`，应改成明确的可重试错误。
6. Python 层并行不代表单 GPU Ollama 能并行推理，真实吞吐仍需压测。
7. 学校危机流程、人工接管 SLA、正式身份系统、密钥管理和恢复演练属于上线前必验项。

---

## 26. 按执行顺序看代码

```text
app/services/chat.py
  HTTP/SSE 入口和最终生成

app/agents/harness.py
  输入处理、requestId、租约、业务落地幂等

app/agents/event_driven_runtime.py
  新建或恢复 Blackboard，组装四个 Agent

app/agents/blackboard_runtime.py
  事件队列、批次执行、结果合并和持久化

app/agents/runtime_guard.py
  不可关闭的批次与状态迁移门禁、事件审计坐标

app/agents/state_coordinator.py
  阶段、路由、批次屏障、Prompt 审查和生成状态

app/agents/dispatcher.py
  并行、超时、重试、退避和降级

app/agents/state_agents.py
  Understanding、Safety、Context、Response 内部逻辑

app/services/memory.py
  会话记忆和上下文压缩

app/agents/runtime_services.py
  四个 Agent 共用的受控依赖集合

app/agents/event_projection.py
  状态投影、hash 和恢复校验

app/agents/runtime_store.py
  MySQL 事件与 checkpoint 事务

app/agents/generation_lifecycle.py
  READY_FOR_GENERATION → GENERATING → COMPLETED/FAILED
```

四个 Agent 内部各自怎样组 Prompt、调用模型、使用会话记忆、RAG 和 Skill，继续阅读《心理ai 四Agent内部完整流程例子.md》。

当前流程可以理解为：一条带唯一 requestId 的消息先取得数据库处理权；Coordinator 用事件和命令推进四个 Agent；每次有效状态变化都通过固定门禁并写入事件与 checkpoint；Prompt 通过同版本 Safety 审查后才进入 SSE；最终回答用业务落地收据防重。程序中断后，系统根据 checkpoint、事件 outcome 和 materialization 判断从哪一步继续。
