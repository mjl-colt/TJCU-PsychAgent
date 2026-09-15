# 心理ai Runtime 整体流程：显式工作流 v2

> 更新于 2026-09-12。本文描述当前新请求使用的 workflow-v2。event-v1 只负责恢复历史 checkpoint，不再是新请求主链路。项目没有引入 LangGraph 或 Kafka。

## 1. 先记住驱动逻辑

当前步骤执行完 → 校验并保存每个任务结果 → 合并 Blackboard → Coordinator 根据明确规则选择下一步骤 → 提交 checkpoint → 执行下一步骤。

新流程不靠 Event 出入队调度，也不轮询 Blackboard。Runtime 直接 await 当前任务；Event 是这次执行留下的审计记录。

- WorkflowCoordinator：定义每一步执行谁、满足什么条件可以去下一步。
- WorkflowRuntime：执行步骤、收集并保存任务结果、校验合并、持久化。
- Dispatcher：接收 AgentCommand，控制并发、超时、重试和降级。
- Agent：读取命令和 Blackboard 快照，返回局部 AgentStateUpdate。
- Blackboard：保存本轮业务数据；采用创建新对象的方式更新。
- Checkpoint：保存最新 Blackboard、执行位置和当前步骤的任务收据。
- Event：追加保存开始、完成、失败和路由事实，用于审计与指标。

## 2. 正常链路

```text
Harness 校验、脱敏、Session、requestId、租约
  ↓
ANALYZING：Understanding + Safety 并行
  ↓
路由：CHAT + LOW → PREPARING_RESPONSE
      CONSULT/RISK → RETRIEVING → PREPARING_RESPONSE
  ↓
PROMPT_REVIEW
  ├─ 未通过或版本不一致，仍可修订 → REVISING_RESPONSE → PROMPT_REVIEW
  ├─ 修订超限 → FAILED
  └─ 通过且版本一致 → READY_FOR_GENERATION
  ↓
Harness 落地用户消息、报告、Trace、业务幂等收据
  ↓
GENERATING：ChatService 调模型；按风险执行输出检查
  ↓
FINALIZING_RESPONSE：已检查的最终回答先写入 checkpoint
  ↓
Harness 保存助手消息、记忆和工具投递状态
  ↓
COMPLETED；向前端完成输出
```

普通 CHAT 可直接流 token；支持类通常先完整缓冲，HIGH 必须缓冲并做语义复审。模型生成、SSE 传输仍在 ChatService，状态由 GenerationLifecycle 写回。

## 3. 新请求初始数据

用户输入：“最近考试压力很大，晚上睡不好。”

Harness 生成脱敏 model_input，取得 requestId 对应的数据库租约后，入口创建：

```yaml
workflow_version: workflow-v2
revision: 0
request:
  request_id: req-001
  user_id: 1001
  session_id: session-001
  model_input: 最近考试压力很大，晚上睡不好
  prompt_injection_signals: []
understanding: null
safety: null
context: null
response: null
flow:
  current_stage: RECEIVED
  route: null
  active_batch: null
  review_attempts: 0
execution: null
```

这是结构示意，省略了展示用 agent_status 等默认字段。原始用户消息保存在业务表，Blackboard 的 request 只保存脱敏输入。

为兼容现有 API，唯一执行位置仍叫 flow.current_stage。没有另外增加 execution.step。flow.active_batch 在 v2 恒为空，旧字段只为历史数据保留。

## 4. 第一次调度与 checkpoint

Runtime 调用 coordinator.start(state)。Coordinator 确认 current_stage=RECEIVED 且没有 execution，按 STEP_TASKS 创建两个任务：

```yaml
flow:
  current_stage: ANALYZING
  active_batch: null
execution:
  tasks:
    cmd-understanding:
      command:
        command_id: cmd-understanding
        batch_id: batch-001
        agent: UnderstandingAgent
        action: UNDERSTAND
        state_revision: 1
      started: false
      outcome: null
    cmd-safety:
      command:
        command_id: cmd-safety
        batch_id: batch-001
        agent: SafetyAgent
        action: ASSESS_RISK
        state_revision: 1
      started: false
      outcome: null
revision: 1
```

Runtime 在同一事务保存这份 checkpoint 和 TURN_STARTED、STATE_UPDATED 审计记录。提交成功后才继续，不创建 AGENT_BATCH_REQUESTED。

## 5. 调用前与逐任务保存

调用前，Runtime 将 pending 任务的 started 改为 true，checkpoint revision 增加一次，与 AGENT_STARTED 一起保存。然后直接调用：

```python
dispatcher.iter_outcomes(commands, input_snapshot)
```

两任务共享同一份业务输入的独立副本。Agent 看不到 execution 中的中间收据。

假设 Understanding 先完成：

```yaml
execution:
  tasks:
    cmd-understanding:
      started: true
      outcome:
        command: 完整的原AgentCommand
        success: true
        degraded: false
        attempts: 1
        update:
          section: understanding
          data:
            intent: CONSULT
            topic: mental_health_support
            reason: 用户正在寻求心理支持
    cmd-safety:
      started: true
      outcome: null
understanding: null
safety: null
```

Runtime 先检查任务身份、写分区和 Schema，再把 Outcome 与完成审计事件写入数据库。此时 understanding 业务分区仍为空：结果先作为任务收据保存，等整个步骤满足完成条件后才统一合并。

如果保存失败，执行器取消并等待剩余异步任务结束，不继续路由。取消协程不保证远端供应商已经停止推理或计费。

## 6. 只有一套步骤完成判断

```python
all(task.outcome is not None for task in execution.tasks.values())
```

这是任务收齐的判断。收齐以后还必须检查 success 和降级策略。有效 fallback 是 success=true、degraded=true；Schema 非法或兜底也失败的结果是失败收据。

v2 不再用 completed_command_ids 逐条计算第二次屏障。flow.agent_status 是兼容展示摘要，不用于判断任务是否收齐。

两任务结果齐全后：

1. 基于同一输入版本校验并合并 understanding、safety；
2. Coordinator.advance() 根据 current_stage 选择对应转换规则；
3. 根据 intent/risk 选择路由；
4. 创建下一步骤的任务；
5. 把业务合并、下一步骤、任务单和审计记录一次提交。

如果这次事务失败，数据库仍保留上一个步骤的完整任务收据，恢复时可以再次合并并路由，无须再次调用已保存结果的模型。

## 7. 两种版本含义不要混淆

- command.state_revision：这一步任务绑定的输入快照版本；同一步所有 Command 相同。
- Blackboard.revision：最新 checkpoint 的状态版本；保存任务开始、单个 Outcome、下一步骤时都会增加。

比如任务输入版本一直是 1，而开始标记和两个任务收据已经把 checkpoint 推到 4，这是合法的。中途只更新 execution，业务输入尚未改变。恢复时仍按 Command 的输入版本构造快照并校验结果。

不能把“保存同批另一个任务的收据”误认为“业务输入已变化”，否则会错误拒绝正常并行结果。Runtime 同时核对任务 key、commandId、batchId、agent、action 和输入版本；旧 checkpoint 不允许覆盖更高 revision。

## 8. 所有业务转换规则

| 当前步骤 | 判断依据 | 下一步骤 |
|---|---|---|
| ANALYZING | Understanding 与 Safety 有可用结果；HIGH 或 RISK 意图 | route=RISK，进入 RETRIEVING |
| ANALYZING | CONSULT 意图或 MEDIUM 风险 | route=CONSULT，进入 RETRIEVING |
| ANALYZING | CHAT + LOW | route=CHAT，进入 PREPARING_RESPONSE |
| RETRIEVING | Context 有有效或允许降级的结果 | PREPARING_RESPONSE |
| PREPARING_RESPONSE / REVISING_RESPONSE | 合法候选 Prompt 存在；修订版本必须递增 | PROMPT_REVIEW |
| PROMPT_REVIEW | 审核成功、approved=true、版本一致 | READY_FOR_GENERATION |
| PROMPT_REVIEW | 审核拒绝、异常或版本不一致，仍有修订预算 | REVISING_RESPONSE |
| PROMPT_REVIEW | 超过默认 2 次修订上限 | FAILED |

非审核步骤若彻底失败或结果非法，进入 FAILED。审核失败有专门的修订路径。进入 READY_FOR_GENERATION 后仍由固定门禁校验同版本审核和生成状态。

Response 的 FINALIZE_RESPONSE 不再是 v2 的独立任务；批准后的状态确认合并到确定性生成门禁。旧动作仍保留用于 event-v1 恢复。

## 9. 最终生成和业务收尾

Harness 在开始 SSE 生成前，用 agent_turn_materializations 防止重复落地用户消息、心理报告与 Trace。

ChatService 通过 GenerationLifecycle.started() 保存 GENERATING，然后生成回复。支持类经过输出规则检查，HIGH 额外做语义复审。审核后的最终文字先通过 output_ready() 写入 response.final_response，阶段变为 FINALIZING_RESPONSE。

然后保存助手消息、更新 Redis、投递必要工具任务并标记投递状态；成功后调用 completed()，保存 COMPLETED。工具投递成功表示已入持久队列，不表示所有后台通知都已经送达。

生成途中异常或零 token：保存 READY_FOR_GENERATION，可用同一 requestId 重试；零 token 会给出错误，不再假报 done。
最终文字已保存而业务收尾失败：停在 FINALIZING_RESPONSE；重连复用这段文字，继续幂等收尾，不重新生成。
流程已 FAILED：返回错误，不调用最终 LLM 绕过审核。

## 10. 究竟存在哪里

| 内容 | 位置 | 更新方式 |
|---|---|---|
| 当前业务状态、唯一执行位置、当前任务收据、workflow_version | MySQL agent_runtime_checkpoints.state_json | 同一 requestId 更新最新快照 |
| revision、stage、completed | checkpoint 表索引列 | 从当前状态投影，方便查询 |
| 开始、结果、失败、路由、生成事件 | MySQL agent_runtime_events | 按 event_id 追加和去重 |
| 请求所有权 | MySQL agent_runtime_leases | owner + 过期时间；执行中和提交时续租/校验 |
| 用户与助手正式消息 | MySQL chat_messages | 通过业务收据防重复 |
| 心理报告、Trace | psychological_reports、agent_run_traces | 按业务流程保存 |
| 消息、报告、回复与工具投递关联 | agent_turn_materializations | requestId 唯一 |
| 工具任务 | tool_jobs + tool_outbox + Redis Stream | MySQL 状态权威、Outbox 防丢、Stream 至少一次投递 |
| 近期会话 | Redis | 按 Session 保存，不承担工作流恢复 |
| RAG 权威知识、向量索引 | MySQL knowledge_chunks、Chroma | Context 通过服务读取 |

v2 事件不再附带完整 state_projection；checkpoint 是恢复执行的权威来源，事件负责审计。读取 checkpoint 出错默认直接失败，不能假装查无记录后重新开始。测试可显式关闭持久化使用 NullRuntimeStore，此模式不保证跨进程恢复。

## 11. 故障点与恢复行为

| 中断位置 | 数据库已有内容 | 恢复方式 |
|---|---|---|
| 首次 checkpoint 前 | 尚无 Agent 调用 | 重新开始 |
| 任务单已保存、尚未调用 | 当前步骤和固定 Command | 执行 pending 任务 |
| Understanding 已保存，Safety 未完成 | 一份 Outcome + 一份 pending 任务 | 复用 Understanding，只重试 Safety |
| 两个 Outcome 已保存，合并/跳转未提交 | 当前步骤的全部收据 | 合并并选择下一步 |
| 下一步骤已提交，尚未执行 | 新步骤和任务单 | 执行新步骤 |
| GENERATING 中断且无最终文字 | 审核通过的 Prompt | 回到 READY_FOR_GENERATION，重试生成 |
| FINALIZING_RESPONSE 中断 | 已检查的最终回答 | 复用回答，继续业务收尾 |
| COMPLETED | 最终回答及业务记录 | 回放结果 |

只有已经提交的结果才保证可以复用。模型已经执行但结果尚未落库时，恢复仍可能重算；外部副作用仍需幂等。

## 12. 新旧版本共存

新请求由 AgentRuntimeService 创建 workflow-v2 状态，进入 WorkflowRuntime。没有 workflow_version 字段的历史 checkpoint 默认解释为 event-v1，继续由 BlackboardEventRuntime 和旧 Coordinator 恢复。

event-v1 可以继续使用 EventBus、active_batch、事件 Outcome 重放和状态投影；不能把这些旧机制当成 v2 的当前主流程。一个未完成请求不能中途改变 workflow_version，未知版本或损坏 checkpoint 会拒绝加载。

无需删除旧表或用户记录：新增执行信息写在已有 JSON 列中，新阶段仍使用原 stage 字符串列。旧版本程序不能直接读取 v2 字段；回滚发布需保留 v2 兼容代码或先排空 v2 请求。

请求内 Agent 执行期间有租约心跳，checkpoint 提交事务核对未过期 owner 并续租；失去租约后停止提交。跨服务工具的 fencing、高可用与真实 MySQL 故障演练仍需部署验证。

## 13. 从哪里读代码

```text
app/agents/workflow.py                步骤任务表和全部条件转换
app/agents/workflow_runtime.py        执行、逐任务保存、合并和推进
app/agents/dispatcher.py              并发、超时、重试、降级、取消
app/agents/blackboard.py              业务状态与当前任务收据 Schema
app/agents/runtime_store.py           checkpoint + 审计事务
app/agents/event_driven_runtime.py    AgentRuntimeService，按版本选择执行器
app/agents/harness.py                 输入处理、租约和业务物化
app/agents/generation_lifecycle.py    生成与收尾状态持久化
app/services/chat.py                 模型、输出检查、SSE
app/agents/recovery.py                启动恢复入口
app/agents/blackboard_runtime.py      仅历史 event-v1 事件执行器
tests/test_workflow_runtime.py        v2 路由、故障注入、迁移、收尾回归
```

## 14. 验证与面试口径

本次 104 项 unittest 通过，六套 Harness 通过。独立报告位于 target/harness-workflow-v2-20260912/harness-report.json；验证环境为 SQLite + mock，向量关闭，没有把这些结果当成真实模型质量或 MySQL 集群压测。

```bash
python -m unittest discover -s tests -q
python -m app.harness.runner --suite all --output-dir target/harness-workflow-v2-check
```

面试可以说：

> 我把四 Agent 编排重构成显式持久化工作流。Coordinator 根据当前步骤和已校验的业务结果选择下一步；Dispatcher 并行执行任务，Runtime 每收到一个任务结果就先持久化，收齐后统一合并并推进。Blackboard 保存业务数据，checkpoint 保存当前步骤和任务收据，Event 只记录审计。高风险路由、同版本 Prompt 审核、输出检查、租约与业务幂等仍保留。服务中断后复用已保存的任务结果，生成后的业务收尾失败也不需要重新生成回答。
