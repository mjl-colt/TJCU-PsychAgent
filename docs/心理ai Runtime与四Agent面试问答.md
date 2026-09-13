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

> 心理ai是四个业务 Agent 加一个确定性 Coordinator 的心理支持工作流。新请求使用 workflow-v2：先并行理解和安全评估，每个任务结果先保存为 checkpoint 收据；收齐后统一校验合并 Blackboard，再按显式转换规则选择 CHAT、CONSULT 或 RISK。需要时由 Context 整理历史、RAG 和 Skill，Response 组装带版本的 Prompt，Safety 审核同一版本，通过后才生成。Event 只记录执行事实，不再承担调度。最终文本通过输出安全门后先保存待收尾 checkpoint，再落业务消息和工具任务，支持中断恢复与幂等。当前 104 项单元测试和六套 mock 工程 Harness 通过，但不代表真实模型正确率或生产压测完成。

Coordinator 不调用 LLM，因此这里是“四个业务 Agent + 一个协调器”，不要说成五个 LLM Agent。

---

## 问题一：系统怎样决定下一步？Event 还驱动流程吗？

新流程的决策输入是“当前步骤 + 当前步骤任务收据 + 已校验的 Blackboard 业务数据”，不是 Event 类型。

```text
Harness：输入、Session、requestId、租约
→ ANALYZING：Understanding + Safety 并行
→ 单个结果返回就保存 execution.tasks[commandId].outcome
→ 当前步骤所有任务都有结果：唯一一道完成屏障
→ 校验并批量合并 Blackboard
→ Coordinator 根据 intent/risk 选路线
→ CHAT+LOW 跳过 Context；其余进入 RETRIEVING
→ PREPARING_RESPONSE：Response 组装 Prompt
→ PROMPT_REVIEW：Safety 审核
   不通过 → REVISING_RESPONSE → 再审，达到预算则失败
   同版本通过 → READY_FOR_GENERATION
→ GENERATING：外层 SSE 服务调用最终模型并检查输出
→ FINALIZING_RESPONSE：已检查文本进入 checkpoint
→ 消息保存和工具任务派发
→ COMPLETED
```

Runtime 直接 await Dispatcher 的异步任务，不轮询 Blackboard、不通过 asyncio.Queue 调度。Dispatcher 只负责执行命令、并发、超时、重试和降级；它不决定业务路线。

Event 仍会追加 TURN_STARTED、AGENT_STARTED、AGENT_COMPLETED/FAILED、STATE_UPDATED 和生成生命周期等审计记录，但不会再次唤醒 Coordinator，也不需要 AGENT_BATCH_REQUESTED 来传递命令。历史 event-v1 checkpoint 才走保留的旧事件 Runtime。

### 决策规则

| 当前步骤 | 判断依据 | 下一步 |
|---|---|---|
| 初始 RECEIVED | 尚无活动任务 | 创建理解、安全两条命令 |
| ANALYZING | 所有任务有收据且结果合法 | HIGH 风险或 RISK 意图走 RISK；否则 CONSULT 意图或 MEDIUM 风险走 CONSULT；否则 CHAT |
| RETRIEVING | Context 合法 | PREPARING_RESPONSE |
| PREPARING_RESPONSE / REVISING_RESPONSE | 新 Prompt 合法 | PROMPT_REVIEW |
| PROMPT_REVIEW | 批准且审查版本匹配 | READY_FOR_GENERATION |
| PROMPT_REVIEW | 拒绝、审查失败或版本不匹配 | 预算内修订，否则 FAILED |

运行位置仍使用 flow.current_stage，避免另造第二个游标。完整逐步参数见《Runtime整体流程例子梳理》。

### 面试回答

> 我把控制流程从事件处理里收敛成显式工作流。Coordinator 定义步骤和转换规则，Runtime 负责执行和持久化，Dispatcher 负责调用 Agent。Blackboard 保存业务状态，checkpoint 额外保存任务收据，Event 只做审计。这样看当前步骤和结果就能解释下一步，也能从 checkpoint 直接恢复。

---

## 问题二：LangChain、LangGraph、AutoGen、CrewAI 有什么区别？为什么项目选择自研 Runtime？

### 五者分别解决什么

| 名称 | 通俗理解 | 最适合的场景 | 核心心智模型 |
|---|---|---|---|
| LangChain | Agent 开发工具箱和高层 Agent API | 快速接模型、Prompt、Tool、Retriever，快速做常见工具调用 Agent | 模型在工具循环中思考和行动 |
| LangGraph | 低层有状态编排 Runtime | 长流程、可恢复执行、人工审批、确定性步骤和 Agent 步骤混合 | node + edge + state |
| AutoGen | 消息驱动的多 Agent 框架 | 多 Agent 对话、协作研究、分布式 Agent、代码执行 | Agent 通过消息和 Runtime 协作 |
| CrewAI | 角色化 Agent 团队加工作流 | 研究员/分析师/写作者等角色协作，或用 Flow 控制业务步骤 | Crew/Task/Process 或 event-driven Flow |
| 心理ai Runtime | 项目内的专用执行器 | 固定四 Agent、高风险安全路由、版本审查和业务幂等 | Step + Task Receipt + 强类型 Blackboard |

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
- Understanding 和 Safety 的同批结果必须按命令输入 revision 校验和原子合并；
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

## 问题三：Blackboard 怎么更新？并行怎么避免冲突？

Blackboard 是一次 requestId 的强类型工作状态，不是全局变量：

```text
request / understanding / safety / context / response：业务数据
flow.current_stage：当前执行位置
execution.tasks：当前步骤的命令、started 标记、outcome 收据
workflow_version：新旧执行语义
revision：checkpoint 状态版本
```

Agent 只能读取隔离的快照并返回自己分区的 AgentStateUpdate。Dispatcher 将结果包成 AgentExecutionOutcome，Runtime 验证命令身份、输入版本、字段类型和分区写权限。

并行结果不是返回一个就直接覆盖业务分区：

1. 每个结果返回后，先写 execution.tasks 中对应的收据并提交 checkpoint。
2. 当前步骤所有任务有收据后，统一校验、合并业务分区。
3. Coordinator 计算下一步；合并后的业务数据与下一步命令在同一次 checkpoint 提交。
4. 提交成功后才执行下一步任务。

“Blackboard 不可变”指更新时创建新模型并让 Runtime 的局部 state 引用指向它；不是原地改旧对象。数据库按 requestId 更新最新 checkpoint 行，事件表则追加记录。嵌套容器并非语言级深冻结，所以 Dispatcher 还给每个 Agent 独立深拷贝快照。

### 两种版本不要混淆

假设两个命令都绑定 input revision=1。理解结果先落收据后 checkpoint revision 可以变为 3，但 Safety 仍合法地返回基于 revision=1 的结果。收据保存没有改变这批任务读取的业务输入。

因此不能简单用“命令版本不等于最新 checkpoint 版本”拒绝结果。Runtime 保留命令绑定的输入版本，恢复时重建同一业务快照；真正不属于本步骤、本命令或输入版本的结果才被拒绝。

---

## 问题四：checkpoint、Event、Harness、租约和幂等是什么关系？

| 对象 | 职责 |
|---|---|
| Blackboard | 当前业务状态、执行位置和任务收据 |
| agent_runtime_checkpoints | 每个 requestId 最新持久快照；v2 恢复权威 |
| agent_runtime_events | 追加审计事实；v2 不携带整份 state_projection，也不靠它调度或重建 |
| agent_runtime_leases | requestId 执行所有者和有效期 |
| agent_turn_materializations | 用户消息、报告、最终回复、工具派发的业务落地收据 |
| tool_jobs / dead_letter_records | 后台工具执行与失败补偿 |

生产配置使用 MySQL；此次自动回归使用隔离 SQLite。Redis 仍是短期会话记忆，不是 v2 流程队列。显式关闭持久化的 Null store 不具备跨进程恢复保证。

### checkpoint 保存时间点

| 时机 | 保存什么 |
|---|---|
| 初始调度 | ANALYZING + 两个任务命令；提交后才允许调用 Agent |
| 调用前 | 对应任务 started=true |
| 每个任务返回 | 该任务 outcome 收据；无需等同伴返回 |
| 步骤收齐 | 业务合并 + 下一阶段 + 下一阶段任务，一次提交 |
| Prompt 审核通过 | READY_FOR_GENERATION |
| 最终模型调用前 | GENERATING |
| 合法完整输出就绪 | FINALIZING_RESPONSE + final_response |
| 业务消息与工具派发确认后 | COMPLETED |

一次 checkpoint 与对应审计事件同事务提交。v2 保存失败必须停止，不能在没存稳任务的情况下继续调用后续 Agent。

### 恢复直接看什么

- started=true 但 outcome=null：沿用 commandId 重跑此任务。
- outcome 已保存：直接复用；同批其他任务缺失就只补缺失任务。
- 全部收据已保存但尚未合并：直接合并推进，不再调用这些 Agent。
- GENERATING 中断：回到 READY_FOR_GENERATION，重新生成完整文本，不拼接半截 token。
- FINALIZING_RESPONSE：复用已经检查并保存的文本，补消息和工具任务派发，不再调用最终模型。
- COMPLETED：重放已有结果。

模型已经返回、收据尚未提交就崩溃时，仍可能再次调用模型。这是可恢复的至少一次执行，不是外部模型调用 exactly-once。

### 为什么使用数据库租约

执行权、checkpoint 和业务收据在同一个数据库中，便于同事务校验。编排和 SSE 都周期续租；checkpoint 写入必须在事务内确认 owner 与未过期租约。失去租约会停止执行，旧 owner 不能继续提交状态。租约过期后新 owner 可以接管，但外部模型或邮件仍需各自的幂等与补偿策略；当前实现不等于外部副作用的完整 fencing 协议。

### Harness 的边界

Harness 是本项目请求接入与业务物化层：输入、Session、租约、Runtime 调用、消息、报告和工具计划。不是“Runtime 之外一切代码都叫 Harness”；认证属于 HTTP 层，最终生成生命周期由 SSE 服务和 lifecycle 协作完成。

### 旧版兼容

历史 checkpoint 没有 workflow_version 时按 event-v1 解读，仍走旧 EventBus、active_batch 和事件投影恢复。新请求明确写 workflow-v2，不把旧状态强行迁移到新语义。

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

Safety Review 检查真实 messages 与强类型安全契约的一致性：

- policy_contract 是否与当前风险、回复模式、RAG 证据推导出的要求一致；
- Prompt 是否携带匹配的契约 SHA-256；
- 不诊断、当前安全、现实支持和紧急升级等布尔约束是否正确；
- 是否出现危险指令、超过长度或缺少约定证据标签；
- review.prompt_version 是否等于 response.prompt_version。

这些是确定性契约校验，不依赖某一句固定中文；最终 HIGH 回复另有独立语义复审。

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
| Understanding | 硬风险策略命中为 RISK；其余模型故障保守进入 CONSULT |
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

> Node 层由 Dispatcher 做超时、一次重试和指数退避，耗尽后进入强类型 fallback；模型网关另有主备和熔断。RAG 的 query 改写失败用原问题，向量失败退 BM25。Tools 使用持久队列、最多三次线性退避、依赖检查和 dead letter。进程崩溃则不靠内存重试，而是通过 checkpoint 内任务 outcome 收据、原 commandId 和数据库租约恢复。Safety 始终 fail-closed。

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
104 个 unittest（2026-09-12）
Risk Safety Harness
Agent Routing Harness
Standard Skills Harness
RAG Harness
API Harness
Tool Queue Harness
```

覆盖范围包括：硬风险、Safety 故障、模型 JSON schema、Prompt 版本错配、并行屏障、checkpoint 恢复、outcome 重放、旧 checkpoint 兼容、requestId 幂等、上下文压缩、RRF、RAG 正负样本、embedding 缓存版本、来源名注入、伪造引用、工具依赖、dead letter、SSE 生成生命周期和模型主备。

### 并行性能怎样解释

历史确定性异步 I/O 基准（不是此次 v2 真实模型压测）：

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

历史模型 A/B 报告因运行环境或模型缺失写成 `BLOCKED`；此次 v2 回归仅使用 mock，没有重新验证真实模型 A/B，所以仍不能给出真实提升百分比。这反而是正确的工程回答——评测条件没满足就不编数字。模型到位后先要求安全关键题不退化，再比较总体质量和延迟；后续可用独立 judge 模型补充语气、帮助性和 groundedness，但要保留代码规则与人工抽检。

面试可直接回答：

> 我把微调评估做成 paired offline eval，而不是看 demo。两模型固定 prompt、seed、temperature 和样本，先用确定性 rubric 检查安全边界与关键概念，再看分场景通过率、延迟和胜负。当前真实 A/B 尚未完成，所以我明确报告未完成，不虚构提升；这套脚本可以在模型交付后直接生成真实增量。

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
- 若拆成跨服务 Worker，还需 Broker 投递协议、outbox、ACK、DLQ 与外部副作用 fencing；
- 正式 embedding A/B 和 GPU 并发压测。

### 为什么现在不需要 Kafka 驱动请求

v2 没有请求内事件队列。Runtime 直接执行当前步骤，通过数据库 checkpoint 和任务收据恢复；多个实例的 requestId 接管由数据库租约控制。这不意味着每个 Agent 已支持跨机器分发。

如果未来拆出独立 Worker，才需要另行设计命令持久投递、ACK、重投、outbox 和消费者幂等。Kafka 是一种可选基础设施，不是“符合规范”的必选条件。

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

心理ai的 Command、Outcome、revision、checkpoint、最大步骤数和 Tool Governance 可以迁移为 Coding Agent Runtime。

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
| RAG Recall 100% 证明 embedding 很好、已经没有幻觉 | 当前 68 条 Harness 主要验证 BM25 兜底检索和负样本拒答；本机已有实体 Chroma 索引，但回答级 groundedness 与 embedding A/B 尚未完成 |
| Prompt 注入已经百分之百防住 | 采用纵深防御，确保模型被诱导后也没有额外权限 |
| Safety 模型肯定不会错 | 硬规则、保守降级、版本审核和输出门共同降低风险 |
| 四个 Agent 都必须有私有记忆 | 只有存在真实消费者、来源和治理时才应该保存记忆 |
| 工具失败就多重试几次 | 必须区分错误类型、限制预算、保证幂等并进入死信 |
| READY_FOR_GENERATION 就完成了 | 它只代表 Prompt 就绪，文本先进入 FINALIZING_RESPONSE，业务收尾完成后才是 COMPLETED |
| 沙箱、Java 和座舱我也做过 | 明确当前没实现，再说明可以迁移的设计能力 |
