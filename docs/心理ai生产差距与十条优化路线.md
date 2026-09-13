# 心理ai生产差距与十条优化路线

更新于 2026-09-12：新请求使用 workflow-v2，event-v1 仅恢复历史 checkpoint。此次自动验证使用 SQLite + mock，未重新执行真实 MySQL 故障演练或模型质量评测。

## 先说结论

当前版本已经不是“用四个类模拟 Agent”的 Demo，而是一个可恢复、可审计、有安全边界的单体 Agent Runtime。此次代码优化已经完成当前仓库内能可靠完成的部分；仍然缺少的内容主要依赖真实学校制度、生产基础设施和长期数据，不能靠多写几个类伪装完成。

最准确的上线状态是：

```text
可以进入生产验收、压测和安全评审
≠
无需配置就能直接承担真实心理危机服务
```

## 路线 1：让 checkpoint 真正表示整轮状态

### 原问题

旧逻辑到 `READY_FOR_GENERATION` 就把 `completed` 设为 true，但最终回复还没生成。生成中崩溃时，启动恢复扫描可能认为已经结束。

### 已完成

- `READY_FOR_GENERATION`、`GENERATING` 和 `FINALIZING_RESPONSE` 都不是终态；
- 新流程以当前步骤、任务收据和显式转换规则推进，不再使用请求内 Event 队列；
- 每个任务结果单独提交收据，收齐后只经过一道合并屏障；业务合并与下一步任务同事务保存；
- 合法最终文本先进入 FINALIZING_RESPONSE checkpoint；业务收尾失败可直接复用文本；
- 只有最终回答保存并写入 `GENERATION_COMPLETED + TURN_COMPLETED` 后，权威阶段才变成 `COMPLETED`；
- 删除 Blackboard 中可由阶段推导的 `flow.completed`，checkpoint 表只保留从阶段生成的索引列，避免两个状态源互相矛盾；
- 启动扫描把已批准但未生成的请求统计为 `awaiting_generation`；
- 生成中断退回 `READY_FOR_GENERATION`，相同 requestId 可以重试；
- checkpoint 与一批事件在同一 MySQL 事务提交；
- 持久化默认 fail-closed，提交失败就不继续对外承诺；
- SSE 使用独立数据库 Session 和标量身份快照，修复了 Docker 联调发现的 detached ORM 故障。

### 还缺什么

- Alembic 等正式 schema migration；
- MySQL 主从/集群、备份和故障恢复演练；
- 对外部模型/工具副作用的完整 fencing 或提供方幂等；当前已经在 checkpoint 写事务内校验有效 owner 并续租，但不能把数据库状态保护等同于所有外部动作恰好一次。

## 路线 2：把幂等从“事件不重复”扩展到完整业务

### 已完成

```text
requestId → 同一轮用户消息、报告、最终回复只物化一次
eventId   → 事件日志唯一
commandId → Agent 命令恢复时身份不变
lease     → 同一 requestId 同时只由一个实例执行
reportId + tool kind → Excel、个案、告警业务幂等
```

同一 requestId 复用时必须是同一用户、同一会话、同一脱敏输入。API Harness 已验证连续请求两次只落两条聊天消息（用户一条、助手一条）。

### 还缺什么

- 正在执行的重复请求目前返回 409；更好的体验是等待 owner 完成后直接订阅结果；
- 跨地域部署要明确全局 requestId 规则和数据库一致性级别。

## 路线 3：建立多层 Prompt 注入防护

### 已完成

- 用户输入、历史、记忆摘要、RAG 内容都被标成 `UNTRUSTED`；
- 用 JSON 数据记录代替可被 `</tag>` 提前闭合的伪 XML 包装；
- 检测中英文直接注入、角色冒充、系统提示词提取和工具强制调用；
- 检测 Base64、Hex、URL 编码、零宽字符、字母间插空和典型乱序拼写；
- signals 写进 Blackboard、事件日志和运行指标；
- RAG 入库前拒绝可疑文档，检索出口再次清洗；
- 工具权限由代码 allowlist、参数策略和业务对象决定，不由 Prompt 决定；
- 最终输出阻止内部提示词泄露。

### 为什么仍不能说“绝对防住”

Prompt 注入不是 SQL 注入那样能靠一种转义完全解决。模型会理解自然语言，攻击也会不断变化。正确目标是：即使模型被诱导，仍拿不到额外权限、敏感数据和未经授权的工具能力。

OWASP 推荐结构化隔离、外部内容清洗、输出验证、最小权限、监控和对抗测试组合使用，当前实现按这个思路落地：[OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)、[OWASP RAG Security](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)。

### 还缺什么

- 独立的专用注入分类器/Guard 模型；
- 多模态图片、PDF 隐藏层和 OCR 注入检测；
- 基于真实攻击流量持续更新的红队语料；
- 注入告警的处置工单和封禁联动。

## 路线 4：让四个 Agent 的失败方式可控

### 已完成

| Agent | 正常路径 | 模型/依赖失败后的行为 |
|---|---|---|
| Understanding | 高风险硬规则 + 严格 JSON 分类，不使用未经校准的自报置信度 | 保守分类 fallback |
| Safety | 版本化危机规则 + 历史 + 结构化风险评估 | 危机策略命中为 HIGH，否则至少 MEDIUM |
| Context | 历史压缩 + 查询改写 + RAG + Skill | 只保留当前输入继续，知识证据为空 |
| Response | 按预算组装 Prompt vN | 只生成系统内置安全 fallback Prompt |
| Safety Review | 确定性审查同一 Prompt version | 只批准内置 safe_fallback，不放行普通 Prompt |

Dispatcher 有分 Agent 超时、重试、指数退避、并发上限和 degraded 结果。Runtime 在原子合并前检查 Agent 写权限、Pydantic schema 和命令绑定的输入 revision。任务收据写入也会增加 checkpoint revision，不能误把这个新版本当作 Agent 输入版本。

### 还缺什么

- 真实中文校园场景大规模标注集；
- 按年龄、语言、校园政策分层的安全阈值；
- Safety 独立模型和专业人员共同校准；
- 跨请求/跨进程的全局优先级调度，保证高风险任务不被普通生成挤压。

## 路线 5：统一模型网关和最终生成

### 原问题

内部 Agent 已经过主备模型网关，但最后 SSE 生成仍直接调用原始客户端；供应商故障时最后一步没有熔断或备用模型。

### 已完成

- 四 Agent 和最终生成统一经过 `AgentModelGatewayClient`；
- 每个 Agent 可单独选择 provider/model/temperature/max_tokens；
- 主模型失败可调用备用 provider/model；
- 连续失败达到阈值后打开进程级熔断，窗口结束再试探恢复；
- 流式生成只有在主模型尚未输出任何 token 时才能切备用；已输出后失败会中止并等待整轮重试，防止两家模型文字拼接。

### 还缺什么

- Redis/网关服务共享的跨实例熔断状态；
- 全局 RPM、TPM、并发、成本和用户预算；
- 模型健康探测、按版本灰度与自动回滚；
- 单 GPU Ollama 的真实排队、显存和长上下文压测。

## 路线 6：把记忆做成受控数据，而不是无限聊天记录

### 已完成

- 会话短期记忆只保存真实对话；删除无独立消费者的四 Agent 私有字符串记忆，避免写放大和旧判断污染；
- 会话历史按 session 隔离，四 Agent 不再维护私有 key；
- 内容写入前隐私脱敏，单条截断，数量有界；
- Redis 启动不可用时使用跨 Runtime 对象的进程级共享 fallback；
- Redis 运行时断开会自动切换 fallback，不再静默丢写；
- Understanding 只读本轮输入；Safety 使用受限真实会话历史，Context 整理摘要并重新检索，Response 消费当前 Blackboard，不读取旧私有结论。

### 还缺什么

- 用户查看、纠正、删除和导出记忆；
- 明确同意、用途、保留期和自动删除；
- 跨会话长期记忆的事实/偏好/敏感信息分类；
- memory poisoning 专项评测和撤销机制。

## 路线 7：让 RAG 有入口、召回和出口三道治理

### 已完成

```text
入口：文件/字符大小限制 + 正文/来源名注入扫描 + 管理员权限
召回：领域门 + 向量/BM25 + 加权 RRF + rerank + 0.45 最低相关度
出口：K1～Kn 证据标签 + 间接注入清洗 + Prompt 引用约束 + 最终引用校验
```

Skill 同时记录显式 version 或内容 hash。Dockerfile 已补上 `COPY skills ./skills`，修复了容器里 Agent 状态显示 0 个 Skill 的问题。

这轮优化解决了四个原来会被面试官追问的问题：

1. cosine 与 BM25 的原始分数不可直接比较：现在用排名位置做加权 RRF，默认 `k=60`。
2. 只有正样本会让指标虚高：现在加入 8 条编程、天气、硬件、数学等负样本，必须不召回。
3. “给了来源”不代表模型真引用：现在 Prompt 使用 K 标签，最终输出拒绝漏引和伪造引用，完成事件记录实际引用 id。
4. 换 embedding 模型可能继续使用旧向量：缓存绑定模型名和 chunk 正文 hash，不一致就重建。

当前确定性评测 68 条，其中正样本 60 条、负样本 8 条：

```text
Recall@K                   100.00%
Precision@K                 65.83%
MRR                         96.11%
NDCG@K                      94.99%
Hit Rate                   100.00%
Negative Rejection Rate    100.00%
Retrieval Decision Accuracy 100.00%
```

这些仍然是 mock/BM25 兜底环境的确定性结果，不等于真实 embedding 的线上效果。主流 RAG 评测还应把检索和生成分开，至少看 correctness、relevance、groundedness 与 retrieval relevance：[LangSmith RAG 评测分类](https://docs.langchain.com/langsmith/evaluate-rag-tutorial)、[Microsoft RAG 生成评测](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-llm-evaluation-phase)。RRF 选型依据见 [Microsoft RRF 说明](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking)。

### 还缺什么

- 文档审批人、有效期、适用校区和撤销状态；
- 高风险知识独立索引及更严格发布流程；
- 真实 embedding 模型下的线上评测，而不只是无向量降级路径；
- 当前能验证引用 id 存在，但还缺 claim 与证据是否语义蕴含的 groundedness/事实性评测；
- 知识变化、低相关度拒答率和真实流量 query 漂移监控。

## 路线 8：工具必须是受限业务动作

### 已完成

- Agent 不能执行 shell 或任意 SQL；
- 工具调用来自代码生成的 `AgentToolPlan`，不是模型自由输出；
- 高风险报告进入有依赖关系的 ToolJob：Excel、个案、告警；
- 工具有业务幂等、重试、退避、限流、审计和死信；
- 告警任务只有个案创建成功后才能执行；
- 学生账号不能访问管理员报告和知识管理接口。

这对应 OWASP 对 Agent 最小权限、参数校验和高影响动作人工控制的方向：[OWASP Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)。

### 还缺什么

- 正式 counselor on-call 和确认 SLA；
- 高影响外部动作的人工批准/双人复核；
- 每个 MCP 服务独立身份、短期凭证、网络隔离和资源级权限；
- 工具返回内容的统一 schema 和独立注入扫描。

## 路线 9：补齐 Web/API 的基础生产安全

### 已完成

- 账号密码升级为随机盐 PBKDF2-HMAC-SHA256 600,000 次；
- 旧 SHA-256 登录成功自动升级；
- 超长密码提前拒绝，避免计算型 DoS；
- Redis 分布式固定窗口认证/聊天限流，Redis 故障时进程内有界降级；
- 聊天输入 4000 字符上限，requestId 格式/长度校验；
- 知识上传大小限制；
- 前端用 `textContent` 渲染用户与模型正文，避免把模型 HTML 直接执行；
- `/actuator/ready` 真正检查 MySQL 和 Redis；Docker app healthcheck 使用 readiness。

PBKDF2 参数参考 OWASP 当前建议；正式系统也可选择 Argon2id：[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。

### 还缺什么

- 替换 Basic Auth 和示例账号，接入学校 OIDC/SSO/MFA；
- TLS、CSRF/CORS/CSP、安全响应头和反向代理配置；
- Vault/KMS/Secret Manager，不能把生产密码写在 Compose；
- 更细粒度 RBAC、审计导出和管理员数据最小可见；
- 数据加密、数据保留/删除和合规评审。

## 路线 10：观测、评测和受控自进化

### 已完成

- 事件日志记录 actor、事件类型、batch/command、耗时、重试、降级；
- trace 保存 Prompt 模板版本、Skill 版本、Prompt hash、状态 revision、输入信任和注入 signals；
- 指标包含 Agent 延迟、失败率、恢复、生成失败、租约、注入检测和输出替换；
- 104 个单元测试通过，包括 v2 部分任务恢复、提交失败取消、失去 owner 拒写、审核失败关门和最终文本收尾恢复；
- Risk、Routing、Skills、RAG、API、Tool Queue 六套工程 Harness 通过；此次为本机 SQLite + mock 回归，报告在 target/harness-workflow-v2-20260912/harness-report.json；
- 对抗用例覆盖直接/编码/零宽/乱序注入、RAG poisoning、来源名注入、提示词泄露、伪造引用和工具策略；
- RAG 评测加入负样本拒答率、检索决策准确率和固定参数记录；
- 并行基准可重复运行。

### 受控自进化应该怎样做

```text
线上失败样本脱敏
→ 人工给出正确路由/风险/答案标准
→ 生成 Prompt/Skill 候选版本
→ 离线全套评测
→ shadow 对比
→ 人工审批
→ 1% 灰度
→ 指标异常回滚
→ 达标后设为 stable
```

Agent 不应在线自己改安全规则然后立即生效。NIST 的 GenAI 风险框架强调把治理、测量和管理贯穿整个生命周期，适合作为这条路线的外部框架：[NIST AI RMF GenAI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)。

### 还缺什么

- OpenTelemetry/Prometheus/Grafana 和集中日志平台；
- Prompt/Skill/模型正式版本注册表；
- 标注平台、shadow traffic、canary 和自动回滚控制面；
- 对告警准确率、漏报率、帮助度、拒答率和成本的长期趋势监控。
- 回答级 correctness/relevance/groundedness/completeness 评测，目前的引用校验不能代替语义事实核验；
- 真实 embedding、chunk 策略、TopK、RRF 权重和 reranker 的同集 A/B。

## 当前真正需要优先投入的三件事

如果准备真实试点，优先级不是继续增加 Agent 数量：

1. 学校危机流程、联系人、人工接手责任和隐私规则落地；
2. 真实模型 + 真实匿名数据的安全评测、GPU 并发压测和红队；
3. SSO、密钥管理、数据库高可用、迁移、遥测和告警平台。

完成这三类外部建设后，现有 Runtime 才能从“生产验收候选”进入“真实受控试点”。
