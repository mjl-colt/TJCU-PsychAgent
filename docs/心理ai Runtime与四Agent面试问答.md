# 心理ai项目面试核心十问

这份文档把原来 37 个零散问题合并成 10 个大问题。每个问题都按照下面的顺序回答：

```text
先说结论
→ 再用一个具体例子解释
→ 说明项目代码真正做到了什么
→ 最后给出可以直接复述的面试答案
```

统一使用“小林咨询考试焦虑和失眠”的例子：

> 我最近连续几天睡不好，想到考试就很焦虑，今晚可以先做什么？

---

## 先背这一分钟项目介绍

> 心理ai是一个事件驱动的多 Agent 心理支持系统。用户请求先经过脱敏和数据库租约控制，再进入强类型 Blackboard Runtime。Coordinator 同时调度 Understanding 和 Safety，Runtime 使用 batch 屏障、revision 和分区写权限校验并行结果；随后按 CHAT、CONSULT、RISK 路由，必要时由 Context 读取会话历史、压缩上下文、执行混合 RAG 并选择 Skill。Response 只组装带版本的 Prompt，Safety 必须审核同一个版本，批准后才进入最终生成。支持类文字完整缓冲，先过确定性禁止项/引用门，HIGH 再由独立 Safety 模型做结构化语义复审，通过后才以 SSE 发给用户。系统把 checkpoint 和事件日志同事务持久化，并使用 requestId、业务物化收据和工具幂等键处理重试及崩溃恢复。当前有 84 个单元测试和六套工程 Harness，定位是可以进入生产验收的单体 Agent Runtime，而不是已经完成所有学校危机制度和分布式基础设施的最终产品。

这段话如果被打断，优先讲清楚三个特点：

1. Understanding 和 Safety 真并行，但 Agent 不能直接修改共享状态。
2. checkpoint、事件和业务幂等支持中断恢复。
3. Safety 审核的 Prompt 与最终发送给模型的是同一个版本。

---

## 问题一：请画出整个系统的运行流程，为什么叫事件驱动？

### 先说结论

系统不是按固定时间轮询 Blackboard，也不是四个 Agent 互相随意聊天。Coordinator 收到事件后发布明确的 AgentCommand；Dispatcher 执行命令；Agent 完成事件再唤醒 Coordinator。

### 一次请求怎样走完

```text
浏览器发送 message + requestId
→ Harness 校验长度、隐私脱敏
→ 为 requestId 获取数据库租约
→ 新建或恢复 Blackboard
→ 发布 TURN_STARTED
→ Coordinator 创建 Understanding + Safety 同批命令
→ Dispatcher 并行执行两个 Agent
→ Runtime 校验 schema、revision 和写权限
→ 同批结果原子合并
→ batch 屏障等待两个 commandId 都完成
→ 选择 CHAT / CONSULT / RISK
→ CONSULT/RISK 执行 Context：历史、压缩、RAG、Skill
→ Response 组装 Prompt v1
→ Safety Review v1
→ Response 确认 Prompt v1
→ READY_FOR_GENERATION
→ SSE 调用最终模型
→ 输出 Guardrail
→ 保存助手消息和工具任务
→ GENERATION_COMPLETED + TURN_COMPLETED
```

普通 `CHAT + LOW` 可以跳过完整 Context/RAG；咨询和风险请求需要上下文支持。

### 为什么不是轮询

Runtime 等待的是 `asyncio.Queue.get()`：

```text
没有事件 → 协程挂起，不消耗 CPU 检查状态
事件到达 → 立即唤醒并处理
```

代码中虽然有事件消费循环，但循环是在等待事件，并且有最大事件数和空闲超时，不是每隔一秒读取 Blackboard。

### 画图时要额外标出的四类边界

| 边界 | 图上应该标什么 |
|---|---|
| 并发边界 | 两个 Agent 使用同一 revision 的不可变快照，结果批量合并 |
| 持久化边界 | 调度、outcome、阶段变化、生成开始和最终完成都持久化 |
| 安全边界 | 硬规则、Safety Review、版本门禁、输出 Guardrail |
| 副作用边界 | 消息、报告、工具分别使用业务幂等键 |

### 面试回答

> 我的事件驱动不是把 while 循环换个名字。Coordinator 只在 TURN_STARTED 或 Agent outcome 到达时运行，并通过 AgentCommand 触发下一批任务。Agent 不直接修改共享状态，只返回局部更新。Runtime 校验后原子合并，再把完成事件交给 Coordinator。这样调度、状态和 Agent 逻辑是分开的，也能明确记录每次状态推进的原因。

---

## 问题二：LangChain、LangGraph、AutoGen、CrewAI 有什么区别？为什么项目选择自研 Runtime？

### 五者分别解决什么

| 名称 | 通俗理解 | 最适合的场景 | 核心心智模型 |
|---|---|---|---|
| LangChain | Agent 开发工具箱和高层 Agent API | 快速接模型、Prompt、Tool、Retriever，快速做常见工具调用 Agent | 模型在工具循环中思考和行动 |
| LangGraph | 低层有状态编排 Runtime | 长流程、可恢复执行、人工审批、确定性步骤和 Agent 步骤混合 | node + edge + state |
| AutoGen | 消息驱动的多 Agent 框架 | 多 Agent 对话、协作研究、分布式 Agent、代码执行 | Agent 通过消息和 Runtime 协作 |
| CrewAI | 角色化 Agent 团队加工作流 | 研究员/分析师/写作者等角色协作，或用 Flow 控制业务步骤 | Crew/Task/Process 或 event-driven Flow |
| 心理ai Runtime | 项目内的专用执行器 | 固定四 Agent、高风险安全路由、版本审查和业务幂等 | Command + Event + 强类型 Blackboard |

LangChain 当前的 `create_agent` 底层也使用 LangGraph；LangGraph 可以脱离完整 LangChain 独立使用。可参考 [LangChain Agents 官方文档](https://docs.langchain.com/oss/python/langchain/agents) 和 [LangGraph 官方概览](https://docs.langchain.com/oss/python/langgraph/overview)。

AutoGen 官方把 AgentChat 定位为会话式单/多 Agent 上层 API，把 Core 定位为可扩展的事件驱动多 Agent 框架，并提供单进程和分布式 Runtime；CrewAI 则同时提供强调角色自主协作的 Crews，以及强调可预测、可审计执行路径的 Flows。参考 [AutoGen 官方概览](https://microsoft.github.io/autogen/stable/index.html)、[AutoGen Runtime 架构](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/architecture.html) 和 [CrewAI 官方介绍](https://docs.crewai.com/core-concepts/Agents)。

### “为什么用 LangGraph 不用 LangChain”这道题要怎样回答

先纠正问题：**心理ai 当前既没有使用 LangGraph，也没有使用 LangChain。** 面试时不能顺着问题假装项目用了 LangGraph。

如果面试官问的是通用选型，可以这样理解：LangChain 和 LangGraph 不是简单的二选一。LangChain 更适合快速获得模型、工具、Retriever 和通用 Agent 抽象；LangGraph 更适合自己明确控制状态、分支、并行、暂停与恢复。实际项目完全可以在 LangGraph node 内使用 LangChain 的模型或工具组件。

如果必须为心理ai选择一个现成框架，LangGraph 会比“只使用高层 LangChain Agent 循环”更接近当前需求，因为心理ai是预先规定路线的安全工作流，而不是让模型自由决定下一步。但是本项目最终选择自研专用 Runtime，原因见下一节。

### LangGraph、AutoGen、CrewAI 应该怎样选

用一个例子最容易说明。假设输入是“我今晚不想活了”：

- 用 LangGraph，可以画出 `并行理解与安全评估 → 高风险路由 → Prompt 审批 → 人工或工具动作`，并让每个节点保存状态。
- 用 AutoGen，可以让 SafetyAgent、SupportAgent 等通过消息协作；它尤其适合 Agent 数量和交互关系比较动态，或者准备扩展为分布式 Agent 的场景。
- 用 CrewAI Crews，可以把多个 Agent 建成有角色和任务的团队；如果需要严格流程，应主要使用 CrewAI Flows，而不是只依赖 Agent 自主讨论。
- 用心理ai Runtime，流程和写权限直接固化在业务代码中：Safety 不能写 Response，Response 不能修改风险，只有 Coordinator 能发命令，Runtime 才能合并。

这里没有“哪个框架绝对最好”。开放式研究和多人协作更容易发挥 AutoGen/CrewAI 的优势；复杂而可恢复的状态图适合 LangGraph；节点固定、领域安全规则多、希望最小依赖和完全控制事务语义时，专用 Runtime 有合理性。心理ai属于最后一种，但代价是持久化、可视化、分布式和生态能力都要自己维护。

### 为什么没有直接使用 LangGraph

不是因为 LangGraph 不成熟，而是项目流程明确、业务安全约束很强：

- 每个 Agent 只能写自己的 Blackboard 分区；
- Understanding 和 Safety 的同批结果必须做 revision 校验和原子合并；
- outcome 必须先落库，再交给 Coordinator；
- requestId 要贯穿租约、checkpoint、消息、报告和工具任务；
- Safety fail-closed 和同版本 Prompt 审查不能被配置绕过；
- `READY_FOR_GENERATION` 与真正完成必须严格区分。

因此项目实现了一套较小、容易审计的专用 Runtime。它的代价也必须承认：LangGraph 已经提供的 durable execution、HITL、可视化和生态集成，需要项目自己补。

### LangGraph 常见的工程坑

这些不是“LangGraph 有缺陷”，而是状态工作流本身必须处理的问题：

1. 并行 node 同时更新字段时，reducer 配错可能覆盖数据或重复追加。
2. checkpoint 解决执行恢复，不自动解决邮件、数据库写入等业务幂等。
3. `thread_id` 如果直接等同 sessionId，多轮请求和并发执行边界容易混乱。
4. interrupt 恢复时会从所在 node 开头重新执行，interrupt 前的副作用必须幂等。
5. 子图要明确选择 per-invocation、per-thread 或 stateless，保存范围错误会串状态。
6. 图暂停和 SSE 跨越 HTTP 生命周期时，旧 ORM Session 可能已经关闭。

官方文档同样强调 checkpoint/thread、pending writes 和副作用幂等：[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)。

如果没有真实上线使用过 LangGraph，不要说“我在线上踩过”。可以说自己在方案评估和原型中重点检查了这些问题。

### 面试回答

> 这个项目没有使用 LangGraph，也没有使用 LangChain。LangChain 偏高层 Agent 和生态集成，LangGraph偏低层有状态编排；AutoGen 强在消息驱动和可分布式的多 Agent，CrewAI 强在角色化 Crew，并且也有受控 Flow。心理ai的四个节点固定，但并行合并、风险降级、Prompt 版本和业务幂等语义很具体，所以我选择专用 Runtime。它让我能完全控制事务和安全门禁，但并不普遍优于现成框架；如果未来动态子图、人工审批或跨机器 Agent 大量增加，我会重新评估 LangGraph 或 AutoGen。

---

## 问题三：BlackboardState 有什么作用？为什么不用全局变量？并行怎么避免冲突？

### BlackboardState 是什么

它相当于本项目的 AgentState，是“一次 requestId 当前已经知道什么、运行到哪里”的权威状态：

```text
BlackboardState
├─ request        请求身份、脱敏输入、注入信号
├─ understanding 当前意图和主题
├─ safety        当前风险、约束、Prompt Review
├─ context       历史摘要、RAG 证据、Skill
├─ response      Prompt messages、版本、hash、最终文本
├─ flow          阶段、路由、Agent 状态、活动批次
└─ revision      状态版本
```

模型使用 Pydantic 强类型并设置为不可变。Agent 得到状态快照后只能返回 `AgentStateUpdate`，不能直接修改 Blackboard。

### 为什么不能使用全局变量

假设小林和小王同时发送消息，全局变量可能出现：

```text
小林写入 risk=LOW
→ 小王写入 risk=HIGH
→ 小林的 Response 读取到小王的 HIGH
```

除此之外，全局变量还有这些问题：

- 进程重启后消失，不能 checkpoint 恢复；
- 多进程和多容器之间根本不共享；
- 并行 Agent 会产生竞态和覆盖；
- 没有 requestId 和 revision，旧结果可能覆盖新结果；
- 测试之间容易污染；
- 心理数据串到另一位用户属于严重隐私事故。

### 并行合并怎样工作

Understanding 和 Safety 都接收 revision 1：

```text
Understanding → 只能返回 understanding 分区
Safety        → 只能返回 safety 分区
```

Runtime 收齐后检查：

1. command 是否绑定当前 revision；
2. Agent 是否写入自己的分区；
3. 数据是否符合 Pydantic schema；
4. 同一批是否重复写相同分区。

全部合法才一次合并为 revision 2。之后才让 Coordinator 消费完成事件。

### revision 解决什么

如果一个慢 Agent 基于 revision 1 运行，但当前状态已经变成 revision 3，它的结果属于过期结果，Runtime 会拒绝写入。这类似数据库的乐观锁。

### 面试回答

> BlackboardState 是一次请求的显式工作内存和状态机载体。它是强类型、不可变并且带 revision 的。每个 Agent 只读同一版本快照并返回自己的局部更新，Runtime 统一校验和合并。全局变量无法隔离用户、无法跨进程恢复，也没有版本和权限边界，在并发心理场景中可能直接造成数据串线。

---

## 问题四：checkpoint、event journal、Harness、租约和幂等到底是什么关系？

这是最容易被连续追问的大题，可以先记住一句话：

```text
租约解决“现在谁能执行”；
checkpoint 解决“执行到了哪里”；
event journal 解决“状态为什么变成这样”；
materialization 解决“业务数据是否已经落地”；
幂等键解决“重复执行会不会产生第二份副作用”。
```

### 五类数据各自负责什么

| 数据 | 作用 |
|---|---|
| `RuntimeLease` | 防止两个进程同时处理相同 requestId |
| `BlackboardState` | 当前请求的权威运行状态 |
| `agent_runtime_checkpoints` | 每个 requestId 最新 Blackboard 快照，方便快速恢复 |
| `agent_runtime_events` | 追加式事件流水，包含可重放的 Agent outcome |
| `AgentTurnMaterialization` | 用户消息、报告、最终回复的业务落地收据 |

### 为什么不用 Redis 分布式锁，而使用数据库租约

准确说法不是“项目没用分布式锁”，而是“项目没有把 Redis 锁作为最终执行权依据”。`agent_runtime_leases` 是数据库实现的分布式租约：多个实例用 `request_id` 唯一约束竞争一行，行中保存 `owner_id` 和 `lease_until`。

这个选择与业务场景有关：一轮 Agent 编排可能持续数秒到数分钟，而且必须在崩溃后根据 checkpoint 恢复。checkpoint、event journal、materialization 和业务唯一键都在 MySQL；租约也放在 MySQL，恢复器只依赖一个权威数据源就能回答“谁能执行、执行到哪、哪些结果已落地”。只用 Redis 锁会把所有权放在 Redis、执行状态放在 MySQL，增加加锁成功但数据库写失败、锁过期但旧实例仍运行、Redis 重启后锁状态丢失等跨存储协调问题。

Redis 锁更适合毫秒级、高竞争的短临界区；当前 requestId 冲突率低、任务时间长，更关注可恢复性而不是极限抢锁吞吐。租约的续期和释放都匹配 `request_id + owner_id`，SSE 期间周期续租，丢失 owner 后立即停止。租约仍不等于 exactly-once，重复副作用最终由 commandId、eventId、唯一键和 materialization 兜住。

面试时可以这样回答：

> 数据库租约本身也是分布式协调。我没有只用 Redis 锁，因为本项目是可中断恢复的长任务，执行权、checkpoint 和业务幂等状态都以 MySQL 为权威。把锁单独放进 Redis 会增加跨存储一致性窗口。当前竞争量下，MySQL 租约的性能足够，并且更便于恢复和审计；未来热点竞争升高时，可以增加 Redis 作为前置快速互斥，但不能用它代替数据库唯一约束和业务幂等。

### checkpoint 什么时候保存

不是每隔几秒保存，而是在重要状态变化时保存：

| 时机 | 保存内容 | 中断后有什么用 |
|---|---|---|
| 创建首批命令 | ANALYZING、active_batch | 知道应该执行哪两个 Agent |
| 模型调用前 | BATCH_REQUESTED、AGENT_STARTED | 知道命令已经开始但可能没结果 |
| Agent 返回后 | outcome、合并后的 state | 已完成模型不需要再调用 |
| Coordinator 消费结果 | completed commandId、下一阶段 | 保存并行屏障进度 |
| Context 压缩 | STARTED/COMPLETED/FAILED | 识别半截压缩 |
| Prompt 审查 | Prompt version、review | 恢复审核对应关系 |
| Prompt 就绪 | READY_FOR_GENERATION | 断线后直接重新生成 |
| SSE 开始 | GENERATING | 识别生成中断 |
| 最终文本保存 | COMPLETED、final_response | 重放最终答案 |

同一次状态推进的 checkpoint 和相关事件通过一个数据库事务提交，要么一起成功，要么一起回滚。

### 为什么 checkpoint 和事件都需要

如果只存事件，每次恢复都要从第一条重算；如果只存 checkpoint，只知道现在是什么，不知道哪些 Agent 已调用以及结果如何产生。

```text
checkpoint → 快速恢复
event journal → 审计、校验、重放 outcome
```

事件上的状态投影还有 SHA-256 hash。恢复器比较投影与 checkpoint revision，投影损坏时回退 checkpoint。

### 四个具体崩溃例子

#### 例一：只有 AGENT_STARTED，没有 outcome

说明模型可能在调用中崩溃。恢复时保留原 batchId 和 commandId，重新执行缺少结果的命令。

#### 例二：outcome 已保存，Coordinator 还没消费

恢复器直接重放相同 commandId 的 outcome，不重新调用已经完成的模型。

#### 例三：SSE 在 GENERATING 阶段断掉

启动或重试时回到 `READY_FOR_GENERATION`。不能从某个 token 精确续写，否则可能拼出矛盾内容，而是重新生成完整回答。

#### 例四：助手消息已经保存，但 Runtime 终态没写

从 materialization 读取已经保存的 `final_response`，不再生成第二遍，然后补齐 `GENERATION_COMPLETED` 和 `TURN_COMPLETED`。

### requestId、sessionId、commandId、eventId 不要混

```text
sessionId → 一段用户会话
requestId → 一次可以安全重试的用户请求
batchId   → 一批并行 Agent 命令
commandId → 一次 Agent 调用身份
eventId   → 一条持久事件身份
```

相同 requestId 只能用于同一用户、同一会话和同一脱敏输入。数据库租约未过期时，第二个进程得到 409；租约过期后可以由其他进程接管。

### Harness 和 Runtime 的边界

```text
Harness：输入、session、租约、调用 Runtime、业务消息、报告、工具和 SSE 接入
Runtime：Agent 命令、Blackboard、事件、批次屏障、checkpoint 和恢复
```

checkpoint 不能代替业务收据，因为 Runtime 状态与聊天消息、报告不一定在同一个事务里。

### 面试回答

> 我的恢复设计不是只保存一个 JSON。租约控制执行所有权，checkpoint 保存最新状态，event journal 保存过程和可重放 outcome，materialization 记录业务是否落地。只有 STARTED 没 outcome 就重做原 command；outcome 已保存就直接重放；最终文本已物化就补终态而不再生成。这样把执行恢复和业务幂等分开处理。

---

## 问题五：四个 Agent 内部分别做什么？Prompt、RAG 和 Skill 在哪里使用？

### 先看职责表

| Agent | 输入 | 主要工作 | 是否调用模型 | RAG/Skill | 失败后 |
|---|---|---|---|---|---|
| Understanding | 当前脱敏输入 | 判断 CHAT/CONSULT/RISK | 硬规则未决定时调用 | 不使用 | 确定性意图分类 |
| Safety | 当前输入、最多 8 条历史 | 风险评估、Prompt Review | 硬风险未命中时调用 | 不使用 | HIGH 或至少 MEDIUM |
| Context | 会话历史、意图、风险 | 压缩、query 改写、RAG、Skill | 摘要和改写调用 | 唯一检索和选 Skill | 只保留当前输入 |
| Response | 三个分区的结构化结果 | 组装 Prompt vN | 此阶段不生成最终答案 | 消费证据和 Skill | 内置安全 Prompt |

### 项目到底用了什么模型

不要只回答一个模型名，因为本项目把“对话模型”和“embedding 模型”分开了：

| 用途 | 当前代码默认值 | 实际调用位置 |
|---|---|---|
| 四 Agent 的默认对话模型 | Ollama：`mindbridge-qwen2.5-7b-ft:latest` | Understanding 分类、Safety 风险判断、Context 摘要与 query 改写 |
| 最终 SSE 回复模型 | 默认复用 ResponseAgent 的模型配置 | Prompt 审批通过后流式生成最终文本 |
| OpenAI 对话模型备选 | `gpt-4o-mini` | 把 provider 切到 OpenAI 时使用 |
| embedding 模型 | Ollama：`qwen3-embedding:0.6b` | 知识 chunk 入库和用户 query 检索时生成 1024 维向量 |
| 自动化测试模型 | `mock` | Harness 和单元测试，保证可重复且不产生模型费用 |

四个 Agent 并不是硬编码成四个不同模型。它们默认继承同一个 provider/model，但支持用 `AGENT_MODEL_UNDERSTANDING_*`、`AGENT_MODEL_SAFETY_*`、`AGENT_MODEL_CONTEXT_*`、`AGENT_MODEL_RESPONSE_*` 独立覆盖，也能配置统一 fallback 模型。这样以后可以让意图分类使用快模型、安全评估使用更稳的模型、最终回复使用质量更好的模型，而不用改 Agent 代码。

还有一个容易说错的点：Safety 的第一次风险判断可能调用模型，第二次 Prompt Review 当前是确定性代码校验，不再调用一次模型；ResponseAgent 在 Runtime 内只组装 Prompt，真正的文本生成发生在外层 SSE，但复用 ResponseAgent 的模型网关。

模型说“我会输出 JSON”不代表一定合法。Understanding 的分类结果和 Safety 的评估结果现在都先经过 Pydantic schema：枚举必须合法、emotionScore 必须在 0～4、reason/summary 有长度限制，多余字段也拒绝。非法结果由 Dispatcher 按预算重试，最后进入确定性或 fail-closed fallback，不能把模型随便吐出的字典直接写进 Blackboard。四 Agent 主流程已经删除 confidence：未经标注集校准的 LLM 自评分和写死的 fallback 分数不能冒充真实概率。

### Understanding

处理顺序：

```text
版本化危机规则（直接意图/计划/实施/伤人）→ 直接 RISK
其他情况 → 意图分类 Prompt
→ 严格解析 JSON
→ 输出非法或模型失败时保守改为 CONSULT
```

Agent 写入 Blackboard 的只有 intent、topic、reason 和 Prompt 版本，不回答用户，也不保存置信度。

### Safety 第一次执行

它与 Understanding 并行，不等 Context。先扫描自杀、自伤等硬规则；没有命中才使用模型评估 emotion 和 risk。模型结果还会经过代码校正，例如 emotion 为 HIGH_RISK 时强制风险为 HIGH。Safety 不再要求或保存未经校准的自报置信度，而是通过 `assessment_method` 和 `risk_signals` 记录判断来源。

Safety 同时生成 Response 必须遵守的约束，例如不诊断、不提供药物剂量、高风险时确认当前安全和联系现实支持。

### Context

只在 CONSULT/RISK 或中等风险路线运行：

```text
Redis 读取近期会话历史
→ Redis 没有时从 MySQL 恢复
→ 历史压缩
→ 改写 RAG query
→ 混合检索
→ 选择受控 Skill
→ 返回 memory_brief、model_history、证据和 Skill 版本
```

Context 是唯一直接执行 RAG 和选择 Skill 的 Agent。

Skill 选择是显式九宫格，不靠用户原文关键词，也不让 LLM 自由挑文件：

| intent \ risk | LOW | MEDIUM | HIGH |
| --- | --- | --- | --- |
| CHAT | 无 | baseline + toolkit | baseline + high_risk |
| CONSULT | baseline + toolkit | baseline + toolkit | baseline + high_risk |
| RISK | baseline + high_risk | baseline + high_risk | baseline + high_risk |

`baseline` 是共同的共情和边界规范，`toolkit` 合并焦虑、睡眠、学业、低落、人际、适应和转介方法，`high_risk` 固定危机回复顺序。只要 Understanding 给出 RISK，或者 Safety 给出 HIGH，就选择危机组合；这是两个分类器不一致时的保守合并。原来分散的 10 个 Skill 已收敛为 4 个文件，其中后台交接 Skill 不进入学生 Prompt。

### Response

Response 不会在这个阶段输出最终自然语言。它将这些材料组装为真正准备发送给模型的 `messages`：

```text
意图和风险
会话历史和摘要
RAG 证据
Skill 约束
Safety response_constraints
当前脱敏输入
```

然后产生 `prompt_version=1` 和实际 messages 的 SHA-256 hash。

### Safety 第二次执行

Safety Review 直接检查这组真实 messages：

- 高风险 Prompt 是否要求确认当前安全；
- 是否提供现实可信任支持和紧急渠道；
- 是否包含不诊断约束；
- 是否出现药物剂量或危险操作；
- 是否超过长度；
- review version 是否等于 response version。

v1 审批后如果 Response 变成 v2，v1 批准立即失效，v2 必须重新审。

### 为什么删除四 Agent 私有记忆

删除的不是用户会话历史，而是 Agent 自己保存的派生字符串：

| 原内容 | 问题 |
|---|---|
| Understanding 上轮 intent/topic | 旧分类可能强化下一轮误判 |
| Safety risk/summary | 只写不读；如果读取又会污染本轮安全判断 |
| Context route/retrieved count | 只写不读，没有决策价值 |
| Response version/mode/risk | Blackboard 已经存在，属于重复状态 |

因此系统保留真实会话历史、Context 摘要、RAG 证据、checkpoint 和事件，不用几个 Agent 自己写的字符串冒充长期记忆。

### 面试回答

> Understanding 只分类，Safety 先评估用户风险，Context 负责会话压缩、RAG 和 Skill，Response 只组装带版本的 Prompt。随后 Safety 再审核同一个 Prompt 版本，批准后外层 SSE 才生成最终回复。四个 Agent 都只返回自己的强类型分区，模型没有修改全局状态和调用工具的直接权限。

---

## 问题六：向量库、RAG、幻觉和短长期记忆是怎么设计的？

### RAG 的通俗原理

RAG 就是先从可信知识库找材料，再把材料交给模型回答。它降低模型完全凭参数记忆回答的概率，但检索结果仍然可能错误或被投毒，所以不能无条件相信。

### 知识入库

```text
Markdown/txt/PDF
→ 大小和 Prompt 注入检查
→ 优先按 Markdown 标题和段落形成语义块
→ 只有单段超长才按 512 字符切块、重叠 64 字符
→ 计算 embedding
→ 向量写入 Chroma
→ chunk 原文、来源、序号和 embedding 副本写入 MySQL
```

这里不是把一条知识的所有字段都向量化。真正送进 embedding 接口的只有 `KnowledgeChunk.content`，也就是知识正文：

```text
会向量化：chunk 正文 content
不会向量化：数据库 id、source 文件名、source_index、创建时间
不会入知识向量库：用户聊天记录、风险等级、意图、Skill、checkpoint
查询时临时向量化：ContextAgent 改写后的 query
```

`source`、`source_index` 等只作为 Chroma metadata 保存，命中后用于找到原文、展示来源以及扩展相邻 chunk。这样语义相似度只由正文决定，不会让文件名或数据库编号污染向量。

### 向量库到底有多大

这道题必须把“当前演示数据”和“将来生产容量”分开：

```text
当前可重复 Harness 数据：18 个知识来源、79 个 chunk
chunk 正文总量：17,310 个字符
本机真实 embedding_json：79 条
当前 data/chroma：79 条 Chroma 向量，每条 1024 维
```

当前仓库已经生成实体 Chroma 索引：本机 Ollama 使用 `qwen3-embedding:0.6b` 将 79 个 chunk 写入 `data/chroma`，实测查询能够同时返回 Chroma 与 BM25 混合结果。Engineering Harness 为了可重复仍主动关闭向量，因此 Harness 指标代表 BM25 兜底能力，不能冒充向量模型 A/B。Docker Desktop 本次未能启动，所以这次真实向量验证的权威分块临时保存在 `data/rag-local.sqlite3`；Docker 恢复后，启动同步会把同样内容写入 MySQL 并校正 Chroma。

实际磁盘大小由 chunk 数、1024 维浮点向量和 Chroma/HNSW 索引开销决定。目前代码没有声称支持百万级，只有单文件最多约 5 MiB、单次入库文本最多 50 万字符的保护；容量上升后仍需要用真实数据压测内存、索引构建时间和 P95 检索延迟。

### 用户查询

```text
“晚上脑子停不下来”
→ Context 改写为“考试压力 焦虑 连续失眠 今晚应对”
→ 改写丢失心理领域信号时退回原 query
→ 领域门拒绝天气、编程等无关 query
→ Chroma 语义召回最多 16 个候选
→ BM25 关键词召回最多 16 个候选
→ 默认按向量 0.65、BM25 0.35 做加权 RRF，k=60
→ 合并去重后先截到最多 16 个候选
→ 本地 rerank，再取 Top 4
→ 丢弃低于 0.45 的证据
→ 扩展最佳 chunk 的前后相邻块
→ 再次清除不可信指令并编号 K1、K2……
→ 交给 Response
```

语义向量适合同义表达，BM25 适合精确词、编号和专有名词，因此混合召回比只依赖一种方式更稳。

为什么从“分数归一化相加”改成 RRF：cosine 与 BM25 原始分数含义和范围不同，min-max 又会受当前这一小批候选的最大/最小值影响。RRF 只看文档在每一路的排名，用 `1/(rank+k)` 融合；同一文档如果两路都排得靠前，融合后自然更靠前。它计算便宜、可复现，适合本项目先做第一阶段融合。[Microsoft 的混合检索说明](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)也采用 RRF。

本地 rerank 不是另一个神经网络模型，而是可解释的确定性公式：

```text
rerank_score
= 融合召回分数 × 0.55
+ 词项相似度       × 0.25
+ query 覆盖率     × 0.15
+ 完整短语命中     × 0.05
```

优点是不增加模型调用、延迟低、结果可复现；缺点是理解复杂语义和反义表达的能力不如 cross-encoder reranker。生产下一步应该用中文心理语料对当前规则与 BGE reranker 等方案做同集 A/B，而不是直接替换后只看主观效果。

### 使用的 embedding

默认配置是：

```env
EMBEDDING_PROVIDER=ollama
OLLAMA_EMBEDDING_MODEL=qwen3-embedding:0.6b
```

通过 Ollama `/api/embed` 调用，模型约 639MB，本机实测输出 1024 维向量，Chroma 使用 cosine 距离。选择 0.6B 版本是因为项目以中文校园心理文本为主，需要多语言语义能力，同时希望普通开发机可以本地运行。Qwen3 Embedding 系列支持 100 多种语言并提供 0.6B、4B、8B 多种规模：[Ollama 模型页](https://ollama.com/library/qwen3-embedding)、[Ollama Embedding 文档](https://docs.ollama.com/capabilities/embeddings)。如果设置 `EMBEDDING_PROVIDER=openai`，仍可使用 `text-embedding-3-small`。

### 换 embedding 模型后旧向量怎么办

这是很容易把面试答浅的一题。不能只改环境变量，因为新 query 向量和旧文档向量可能来自不同模型，即使维数相同也不在同一语义空间。

当前 embedding 缓存同时保存：

```text
model：生成该向量的模型名
contentHash：chunk 正文 SHA-256
vector：真正的浮点向量
```

读取缓存时必须同时匹配当前模型名和当前正文 hash。模型升级或正文变化都会让缓存失效，系统重新 embedding 并同步 Chroma。历史裸数组因为没有 provenance，也不会在新校验下被静默复用。

### 有没有比较其他 embedding

当前没有完成严格的 embedding A/B，因此不能说 `qwen3-embedding:0.6b` 已经被项目证明最优。

当前 68 条 RAG Harness 包含 60 条应该召回的正样本，以及 8 条天气、编程、数学等必须返回空的负样本：

```text
Recall@K                    100.00%
Precision@K                  65.83%
MRR                          96.11%
NDCG@K                       94.99%
HitRate                     100.00%
Negative Rejection Rate     100.00%
Retrieval Decision Accuracy 100.00%
```

但 Harness 使用 mock 环境并关闭向量能力，这组数据主要验证 BM25 + 本地 rerank 的兜底链和评测程序，不是 embedding 模型效果。

真正比较应该固定 chunk、query、TopK，评测：

```text
qwen3-embedding:0.6b
text-embedding-3-large
BGE-M3 / multilingual-e5 等候选本地模型
BM25 only 基线
```

同时比较 Recall、MRR、NDCG、中文心理同义表达、危险场景漏召、p95 延迟、调用成本和索引大小。

### 向量不可用怎么办

```text
KNOWLEDGE_VECTOR_REQUIRED=false
→ embedding 或 Chroma 失败时回退 BM25 + 本地 rerank

KNOWLEDGE_VECTOR_REQUIRED=true
→ 向量失败直接报错，不伪装成完整 RAG
```

### 上下文压缩

默认采用“确定性摘要 + 最近 8 条原文”：

```text
全部历史先脱敏
→ 较早消息压成最多 500 字符摘要
→ 最近 8 条保留原文
→ 当前输入始终保留
→ Context 再尝试生成 1～3 条 memory_brief
→ 模型摘要失败时使用确定性摘要
```

确定性摘要最多选择最近 4 条用户关注、3 条助手支持和本轮关注，并限制每条长度。压缩过程记录 STARTED、COMPLETED/FAILED、原消息数、保留数和 summary hash。

优点：Prompt 成本可控、近期语境完整、模型失败可降级、可以识别压缩中断。

缺点：摘要一定会丢细节；固定最近 8 条不一定是最重要的 8 条；模型摘要可能漂移；当前还没有按来源、时间和有效期保存长期事实。

### 有没有区分短期记忆和长期记忆

有存储层面的区分，但要诚实说明：当前只有“短期工作记忆 + 持久聊天记录”，还没有真正成熟的用户长期画像记忆。

| 数据 | 存在哪里 | 保存多久/多少 | 用途 |
|---|---|---|---|
| 短期工作记忆 | Redis，按 sessionId 分 key | 默认 TTL 24 小时，最多 40 条消息 | 快速给当前会话提供上下文 |
| 持久聊天记录 | MySQL `chat_messages` | 按业务数据策略保存 | Redis 丢失时恢复历史，也用于会话记录 |
| 本轮压缩摘要 | Blackboard `context.memory_brief` 和 checkpoint | 跟随当前 requestId 状态 | 控制本轮 Prompt 长度和恢复执行 |
| 真正长期记忆 | 当前尚未建设 | 应有用户授权、来源、置信度、有效期和删除能力 | 跨会话保存稳定偏好或长期目标 |

MySQL 聊天记录“保存得久”不等于已经实现长期记忆。长期记忆需要从聊天中抽取值得保留的事实，并解决误记、过期、隐私授权、修改和删除问题。心理场景尤其不能把模型推断出的“诊断标签”自动写成永久用户画像。

### 什么情况下发生压缩

不是每轮都压缩。当前逻辑是：

```text
先从 Redis 取最多 40 条；Redis 不可用则从 MySQL 取最多 40 条并回填 Redis
→ 全部消息先脱敏
→ 若压缩关闭，或消息数 ≤ 8：原样保留，不插入摘要消息
→ 若消息数 > 8：较早消息生成确定性摘要，最近 8 条保留原文
→ ContextAgent 再尝试产生 1～3 条 memory_brief
→ 模型摘要失败：继续使用确定性摘要
```

例如历史有 14 条消息时，系统不是把 14 条全丢掉，也不是只保留 8 条：它会得到“1 条早期历史摘要 + 最近 8 条原文”。当前输入随后再追加，最终还会按 Prompt 总预算裁剪。压缩是否发生、原始条数、保留条数和 summary hash 都写入 Blackboard，随 checkpoint 保存，便于恢复和审计。

### 有没有出现幻觉，怎样缓解

会。只要最终回复由生成模型产生，就不能承诺零幻觉。当前代码能缓解一部分风险，但还不能证明已经解决所有事实性幻觉。

现在已有的措施：

1. 用 RAG 给回答提供受控知识，而不是完全依赖模型参数记忆。
2. 向量与 BM25 混合召回，并做 rerank 和来源保留，减少只靠单路召回的误命中。
3. 知识入库时拦截 Prompt 注入，检索出来后再次净化，降低 RAG 投毒。
4. Prompt 明确禁止医学诊断、药物剂量和危险操作；高风险场景必须优先现实求助。
5. Safety Review 审核同一 `prompt_version`，最终文本还经过输出 Guardrail；模型或检索失败时走保守 fallback。
6. 每条证据进入 Prompt 时编号 K1～Kn，知识性结论必须引用真实标签；漏引或编造 K9 会触发最终输出兜底，实际引用 id 写入完成事件。

现在仍然存在的缺口：

- Safety Review 主要验证安全约束，并不是逐条事实核验器。
- 当前能证明引用编号存在且属于本轮证据，但不能证明 `[K1]` 前面的每句话在语义上真的被 K1 支持。
- `0.45` 是当前固定数据集上的工程门槛，真实 embedding 与真实匿名 query 上仍要重新校准。
- 68 条评测衡量检索和负样本拒答，没有评估完整回答的 correctness、groundedness、completeness 和 helpfulness。

因此更专业的说法是：“当前通过可信 RAG、负样本拒答、证据引用、安全 Prompt、版本审查和输出 Guardrail 降低幻觉造成的风险，但没有宣称消除幻觉。”下一步应增加带标准答案与证据的回答级评测、claim-evidence 语义校验，并单独统计高风险场景的漏检率。主流 RAG 评测也会把 correctness、relevance、groundedness 和 retrieval relevance 分开衡量：[LangSmith RAG 评测](https://docs.langchain.com/langsmith/evaluate-rag-tutorial)、[Microsoft RAG 生成评测](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-llm-evaluation-phase)。

### 面试官通常会怎样连续追问

1. **为什么 Top 4？** 当前它是上下文噪声、Prompt 长度和召回率的折中，不是行业固定答案；先各取 16 个候选是为了给融合与重排留空间，最终 TopK 必须通过同一评测集 A/B。
2. **为什么不是直接把两个分数相加？** 两种分数量纲不同，所以先用加权 RRF 融合排名，再用统一公式 rerank。
3. **为什么需要负样本？** 如果测试集全是“应该搜到”的问题，永远召回一些内容也能获得高 Recall，却不知道系统会不会给天气问题塞心理知识。
4. **有引用是否等于没有幻觉？** 不等于。引用完整性是代码可验证的，claim 是否被证据支持仍需要人工标注、NLI 或独立 judge 评测。
5. **换 embedding 怎么迁移？** 新旧索引不能混用；用模型版本和正文 hash 让缓存失效，重建后再切流量，并保留可回滚快照。
6. **为什么不用大模型 reranker？** 当前规则 reranker 延迟低、无额外费用且可复现；是否换 cross-encoder 必须看中文心理语料上的增益、p95 与成本。

### 面试回答

> 当前种子知识库有 18 个来源、79 个语义 chunk，向量化对象只有 chunk 正文；source 和序号只是 metadata。主链使用 Ollama `qwen3-embedding:0.6b` + Chroma，与 BM25 各召回 16 个候选，按 0.65/0.35 做加权 RRF，再用确定性公式 rerank、0.45 证据门和 Top 4 控制噪声。本机已经建成 79 条、1024 维的实体向量索引；68 条可重复 Harness 则故意关闭向量，验证 BM25 兜底的 Recall、MRR、NDCG、负样本拒绝率分别为 100%、96.11%、94.99%、100%，不能当作 embedding A/B。短期记忆在 Redis，最多 40 条、TTL 24 小时；超过 8 条时压成早期摘要加最近 8 条。模型仍可能幻觉，目前强制使用本轮 K 标签并拦截伪造引用，但引用存在不等于语义事实正确，后续还要补回答级 groundedness 评测。

---

## 问题七：整体 fallback、失败重试和恢复是怎么设计的？

### 先区分四个概念

```text
重试     → 相同操作再次尝试
fallback → 换成更保守或更简单的路径
恢复     → 进程崩溃后从持久状态继续
幂等     → 操作做两次也不会产生两份业务副作用
```

### Agent node 重试

默认配置：

```text
Safety timeout：6 秒
其他 Agent timeout：12 秒
max_retries：1，即最多尝试 2 次
退避：0.15 × 2^(attempt-1)
并发上限：4
```

处理流程：

```text
Agent 调用
→ asyncio.wait_for 控制超时
→ 异常后指数退避
→ 使用同一 commandId 重试
→ 耗尽后 AgentFallbackPolicy 生成确定性结果
→ 标记 degraded 并写事件
→ fallback 也失败才成为 AGENT_FAILED
```

### 模型网关重试

模型网关和 Dispatcher 是两层：

```text
Model Gateway → 主 provider 失败后切备用 provider；连续 3 次失败熔断 30 秒
Dispatcher    → 对整个 Agent 命令做超时、重试和降级
```

两层重试不能无限叠加，否则会形成重试风暴。

### 各 Agent fallback

| 位置 | fallback |
|---|---|
| Understanding | 通过硬风险词、心理词和普通任务词确定 RISK/CONSULT/CHAT |
| Safety | 高精度危机策略命中为 HIGH；其他模型故障至少 MEDIUM，绝不静默 LOW |
| Context | 返回只包含当前脱敏输入的最小 Context |
| Response | 使用系统内置安全 Prompt，仍必须经过 Safety Review |
| Safety Review | 只允许内置 safe fallback 自动通过，普通 Prompt 不会因服务故障放行 |

### RAG fallback

```text
query 改写失败 → 使用当前输入前 60 字符
embedding/Chroma 失败 → 非 required 模式退 BM25
Context 整体失败 → Dispatcher 重试，再退最小 Context
```

当前 embedding HTTP 没有再单独多层盲重试，而由 Context 命令控制总重试预算。以后可以只对 429/503 做有限 jitter 重试，不能所有错误都重试。

### Tool 重试

工具使用持久化 `tool_jobs`，不在 SSE 主链路里死循环：

```text
PENDING → RUNNING → SUCCESS
                  → 失败后 PENDING
                  → 超过次数 DEAD + dead_letter_records
```

默认最多 3 次，失败后按照 `retry_delay × attempts` 线性退避。依赖未完成或邮件限流时只是重新排队，不消耗一次实际执行机会。服务重启后遗留的 RUNNING 会恢复成 PENDING。

Excel、个案、告警使用 reportId + kind 等幂等键，并有依赖顺序。达到上限后进入 dead letter，交给管理员补偿。

### SSE 和最终生成

主模型尚未发出 token 时可以切备用模型；一旦已经发出 token，就不能接上另一个模型的半段回答。异常会记录 `GENERATION_FAILED` 并回到可重试的 `READY_FOR_GENERATION`。

### 安全 fallback 的原则

```text
可用性可以降级，安全约束不能被降级掉。
fallback 也必须是强类型、可审计、可持久化的结果。
```

### 面试回答

> Node 层由 Dispatcher 做超时、一次重试和指数退避，耗尽后进入强类型 fallback；模型网关另有主备和熔断。RAG 的 query 改写失败用原问题，向量失败退 BM25。Tools 使用持久队列、最多三次线性退避、依赖检查和 dead letter。进程崩溃则不靠内存重试，而是通过 checkpoint、event outcome、原 commandId 和数据库租约恢复。Safety 始终 fail-closed。

---

## 问题八：系统怎样防 Prompt 注入、RAG 投毒和越权工具调用？

### 先说核心原则

不能只在 system prompt 里写“不要听用户的”。Prompt 只是模型行为提示，不是安全权限边界。

真正目标不是宣称百分之百识别所有攻击，而是：

```text
即使模型被诱导，也拿不到额外数据、写权限和工具权限。
```

### 输入层

- 用户原文先经过 PrivacySanitizer，Blackboard 只保存脱敏后的 `model_input`；
- 检测“忽略之前规则”、角色冒充、system prompt 提取和工具强制调用；
- 检测 Base64、Hex、URL 编码、零宽字符、字母插空和常见乱序拼写；
- 用户、历史、摘要和知识内容都以 JSON 形式标记为 UNTRUSTED 数据。

### Agent 和状态层

- Understanding 只能写 understanding；
- Safety 只能写 safety；
- Context 只能写 context；
- Response 只能写 response；
- request 身份与输入在状态迁移中不可修改；
- 过期 revision 和错误 schema 不能写入。

所以用户无法通过文本让模型把自己升级成管理员或直接改 Safety 结果。

### RAG 入口、召回和出口治理

```text
知识正文和来源名入库前扫描
→ 可疑文档直接拒绝
→ 领域门、RRF、相关度阈值拒绝无关证据
→ 检索后再次清除不可信指令
→ Response 只把 K1～Kn 当参考资料
→ 最终输出拒绝漏引和伪造引用
```

证据保留 source、chunkId 和 score，完成事件记录实际引用 id，方便审计。embedding 缓存还绑定模型名与正文 hash，防止模型升级后混用旧向量。但当前仍缺正式文档审批、签名、有效期和多模态隐藏内容检查。

### Tool Governance

工具能否运行由代码决定：

- 工具是否在 allowlist；
- 当前风险是否允许；
- 参数是否属于当前 report/用户；
- 前置任务是否成功；
- 是否已经用相同幂等键执行；
- 是否触发限流。

模型生成“调用邮件工具”这句话不会自动获得权限。

### Prompt Review 和输出层

Response 组装的 Prompt 必须由 Safety 审核同一版本。最终回答还要检查：

- 是否给出诊断结论；
- 是否擅自建议服药、停药、换药或剂量；
- 是否提供自伤、自杀危险细节；
- 是否泄露 system/developer prompt；
- HIGH 风险是否包含安全确认和现实支持。
- 使用 RAG 时是否漏掉引用、是否编造不存在的 K 标签。

命中后整段替换为内置安全回答。

### 面试回答

> 我的注入防护不是一个正则。输入和 RAG 内容先扫描、脱敏并标成不可信；Agent 有固定写权限；Prompt 必须做同版本 Safety Review；工具权限由代码 allowlist、参数和业务状态决定；最终输出再做 Guardrail。过滤器可能被绕过，但模型即使被诱导，也不能获得额外工具和数据权限。

---

## 问题九：怎样证明系统能上生产？性能、测试、缺点和自进化怎么回答？

### 当前已经验证什么

```text
84 个 unittest
Risk Safety Harness
Agent Routing Harness
Standard Skills Harness
RAG Harness
API Harness
Tool Queue Harness
```

覆盖范围包括：硬风险、Safety 故障、模型 JSON schema、Prompt 版本错配、并行屏障、checkpoint 恢复、outcome 重放、旧 checkpoint 兼容、requestId 幂等、上下文压缩、RRF、RAG 正负样本、embedding 缓存版本、来源名注入、伪造引用、工具依赖、dead letter、SSE 生成生命周期和模型主备。

### 并行性能怎样解释

确定性异步 I/O 基准：

```text
Understanding：80ms
Safety：120ms
串行 p50：222.54ms
并行 p50：125.87ms
阶段延迟降低：43.44%
加速：1.77 倍
```

这只证明 Python 调度器确实并行。单 GPU Ollama 可能在服务端排队，所以不能把它宣传成真实模型吞吐提升 1.77 倍，仍需独立 GPU 压测。

### 面试官追问：微调模型到底提升多少，怎么证明

不能拿两段聊天截图主观比较，也不能让候选模型给自己打分。项目新增了 12 个固定场景的成对 A/B，覆盖危机、心理支持、诊断/药物边界、Prompt 注入和普通问题。基座与微调版使用同一 system prompt、同一输入、`temperature=0`、`seed=42`、相同 token 上限并重复多轮。评测不是要求背出固定中文答案，而是按“必须覆盖的概念组 + 绝不能出现的表达”计分，同时统计总体/分类通过率、边界通过率、P50/P95 延迟和逐题胜负。

```bash
python scripts/evaluate_model_quality.py \
  --baseline-model qwen2.5:7b \
  --candidate-model mindbridge-qwen2.5-7b-ft:latest \
  --repeats 3
```

当前不能给出真实提升百分比：本机 Ollama 和 Docker daemon 未运行，仓库也没有 GGUF 权重，自动报告因此写成 `BLOCKED`。这反而是正确的工程回答——评测条件没满足就不编数字。模型到位后先要求安全关键题不退化，再比较总体质量和延迟；后续可用独立 judge 模型补充语气、帮助性和 groundedness，但要保留代码规则与人工抽检。

面试可直接回答：

> 我把微调评估做成 paired offline eval，而不是看 demo。两模型固定 prompt、seed、temperature 和样本，先用确定性 rubric 检查安全边界与关键概念，再看分场景通过率、延迟和胜负。当前环境缺模型，所以我明确报告未完成，不虚构提升；这套脚本可以在模型交付后直接生成真实增量。

### 能不能直接说“已经生产可用”

最准确的说法是：

```text
代码层具备生产验收基础
≠
已经可以无条件承担真实心理危机服务
```

仍缺少：

- 学校真实危机政策、人工接管责任链和 SLA；
- SSO/MFA、Secrets、正式 schema migration；
- MySQL 高可用、备份和恢复演练；
- OpenTelemetry/Prometheus、成本和告警控制面；
- 真实匿名数据上的风险漏报、误报和注入红队；
- 多实例 Broker、ACK、outbox、DLQ 和 fencing；
- 正式 embedding A/B 和 GPU 并发压测。

### 为什么没有马上接 Kafka

当前请求内 mailbox 是 `asyncio.Queue`，关键事件已持久化。换成 Kafka/Redis Streams 不只是换一个队列类，还必须一起设计：

```text
outbox
消费者组和 ACK
可见性超时和重投
顺序和分区键
dead letter
fencing token
跨服务 trace
业务幂等
```

没有这些语义就声称“分布式”，只会制造更难定位的重复副作用。

### Agent 自进化怎么做

只做受控离线进化，不允许线上模型自己修改安全规则：

```text
收集脱敏失败案例
→ 人工标注
→ 生成 Prompt/Skill 候选版本
→ 跑路由、安全、RAG、注入和工具评测
→ shadow 对比
→ 人工审批
→ 小流量灰度
→ 指标异常自动回滚
```

现有 Prompt version、Skill version、Prompt hash、trace 和 Harness 是这条路线的基础。

### 面试回答

> 我不会只用“跑通 Demo”证明生产能力。项目有单元测试和六套 Harness，覆盖故障、恢复、安全和业务幂等；并行基准证明调度层有效，但我明确区分调度数据与真实 GPU 数据。当前可以进入生产验收，但学校危机制度、真实压测、可观测性和分布式控制面仍需补齐。自进化也只允许离线产生候选版本，经过评测、人工审批、灰度和回滚后上线。

---

## 问题十：如果面试 Coding Agent、Java/Python 或智能座舱，怎样把项目经验迁移过去？

这类问题主要出现在综合 Agent 平台岗位。一定要区分“现有项目做过”和“我能设计但尚未实现”。

### Coding Agent 沙箱

心理ai没有执行用户代码，所以不能说已经实现 Codex 沙箱。一个完整闭环应该是：

```text
模型生成补丁
→ 在隔离 workspace 应用
→ 沙箱编译和测试
→ 收集 exit code、stdout、stderr
→ 清洗路径并压缩日志
→ 提取文件、行号、错误类型
→ 作为不可信 ToolOutcome 写回状态
→ 模型生成下一版补丁
→ 测试通过或达到最大轮数
```

真正的难点：

- 文件系统只能访问 workspace；
- 命令使用 allowlist；
- 网络默认关闭，下载依赖单独授权；
- 限制 CPU、内存、磁盘、进程数和时间；
- 超时后杀掉整个进程树；
- 日志必须截断，不能无限塞回模型；
- 补丁、命令、退出码和文件变化全部审计；
- 模型只能提出操作，沙箱决定是否允许执行。

心理ai的 Command、Outcome、revision、checkpoint、最大事件数和 Tool Governance 可以迁移为 Coding Agent Runtime。

### Java 和 Python 协作

当前主项目是 Python，可以设计成：

```text
Java/Spring
→ 用户、权限、网关、业务事务、订单/车辆数据和高并发接口

Python
→ Prompt、模型、RAG、Agent Runtime、评测和安全审查
```

低延迟对话使用 HTTP/gRPC，耗时副作用使用消息队列。两边必须透传 requestId、sessionId、traceId、deadline 和权限声明。

不能做跨服务“假原子事务”。更合理的是业务库 + outbox + 至少一次投递 + 消费者幂等。Java 网关、Python Runtime、模型 SDK 之间还要统一重试预算，避免三层同时重试。

### 智能座舱

座舱链路可以设计为：

```text
VAD/降噪
→ ASR 文本、置信度和 N-best
→ 车辆与驾驶状态
→ 意图和槽位
→ 歧义判断
→ 权限策略
→ 车控执行
→ 结果确认
→ 简短语音反馈
```

端侧保留唤醒、基础 ASR、常用意图和紧急规则，云端处理复杂理解。网络不可用时，只执行端侧 allowlist 中的确定性能力。

模型不能直接获得车控权限。代码需要根据速度、档位、车门状态、身份、动作风险和 ASR 置信度决定直接执行、二次确认、澄清还是拒绝。

例如：

```text
“打开车窗”置信度高、车辆静止 → 可以执行
“打开车门”置信度低、车辆行驶中 → 禁止并要求确认
“导航去公司”存在多个地址 → 先澄清
```

它与心理ai的共同原则是：

| 心理ai | 座舱/Coding Agent |
|---|---|
| 高风险硬规则 | 驾驶安全规则/沙箱权限 |
| Safety fail-closed | 不确定的高风险操作拒绝或确认 |
| AgentCommand | 代码命令或车控命令 |
| Tool allowlist | Shell/文件/车控能力 allowlist |
| checkpoint | 自动修复或任务执行进度 |
| event journal | 命令、补丁或车控操作审计 |

### 当前能力匹配度

| 方向 | 当前程度 | 应怎样回答 |
|---|---|---|
| Agent 全链路 | 强 | 直接画心理ai完整流程 |
| checkpoint/恢复 | 强 | 用四个崩溃例子解释 |
| 幂等、租约、事件 | 强 | 说明不同机制各自职责 |
| Safety fallback | 强 | 讲 fail-closed 和版本门禁 |
| RAG/压缩 | 中强 | 坦白 embedding A/B 尚未完成 |
| Coding 沙箱 | 弱 | 明确未实现，再讲迁移设计 |
| Java/Python 双端 | 弱 | 从协议、事务、幂等和重试预算回答 |
| 端侧语音/车控 | 弱 | 用置信度、端云分层和策略权限回答 |

### 面试回答

> 我的现有项目最强的是 Agent Runtime、checkpoint、幂等、安全门禁和 RAG，不会虚构已经实现代码沙箱、Java 双端或车控。但这些领域有共同的生产原则：模型负责理解和提出动作，代码负责权限；所有动作通过 Command/Outcome 表达；副作用必须幂等；失败必须可恢复；高风险不确定时 fail-closed。然后我会针对沙箱、跨服务或座舱补充各自的资源隔离和业务约束。

---

## 最后：面试时最容易说错的十句话

| 不要这样说 | 应该这样说 |
|---|---|
| 项目用了 LangGraph，或者自研 Runtime 比 LangGraph 强 | 当前没使用 LangGraph；专用 Runtime 在当前约束下更直接可控，但生态能力需要自己补 |
| 有 checkpoint 就不会重复 | checkpoint 负责恢复，副作用还需要幂等 |
| 使用 asyncio 就一定更快 | 调度并行已验证，单 GPU 是否提速要另测 |
| RAG Recall 100% 证明 embedding 很好、已经没有幻觉 | 当前 68 条 Harness 主要验证 BM25 兜底检索和负样本拒答；Chroma 尚未构建，回答级 groundedness 与 embedding A/B 尚未完成 |
| Prompt 注入已经百分之百防住 | 采用纵深防御，确保模型被诱导后也没有额外权限 |
| Safety 模型肯定不会错 | 硬规则、保守降级、版本审核和输出门共同降低风险 |
| 四个 Agent 都必须有私有记忆 | 只有存在真实消费者、来源和治理时才应该保存记忆 |
| 工具失败就多重试几次 | 必须区分错误类型、限制预算、保证幂等并进入死信 |
| READY_FOR_GENERATION 就完成了 | 它只代表 Prompt 就绪，最终文本保存后才是 COMPLETED |
| 沙箱、Java 和座舱我也做过 | 明确当前没实现，再说明可以迁移的设计能力 |
