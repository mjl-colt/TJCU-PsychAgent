# 心理ai RAG、Skill 与模型效果量化说明

> 2026-09-12 工作流更新：Context 的 RAG/Skill 逻辑保持不变，调用方改为显式 workflow-v2；Event 只审计。此次 104 项单元测试及六套 SQLite + mock Harness 通过，固定 68 条 RAG 指标保持不变，报告见 target/harness-workflow-v2-20260912/harness-report.json。下文历史模型/向量指标不代表此次重跑了真实模型 A/B。

这份文档只用一个问题，把知识库放在哪里、怎么分块、如何召回、Skill 为什么存在、危险规则如何判断，以及微调模型怎么测清楚。

示例学生输入：

> 我明天要答辩，最近一想到这件事就心慌，晚上也睡不着，我该怎么办？

## 一、知识原文怎样变成可检索数据

### 1. 项目里看到的 md 是种子原文

仓库中的原文放在：

```text
app/knowledge/*.md
```

当前共有 18 个来源、约 17,490 个可见字符、43,228 字节。它们覆盖危机安全、焦虑落地、低落、睡眠、学业、人际、适应、转介、隐私和证据边界。这里仍然只是随程序发布的种子库，不应冒充某所学校已经验收的完整知识库。学校自己的电话、地址、值班时间、预约规则和危机制度必须由校方提供后再入库，系统不能编造。

### 2. 启动时什么时候切块

FastAPI 启动后，`app/core/bootstrap.py` 的 `seed_data()` 逐个读取 md，并调用 `KnowledgeService.ensure_source()`。

以“急性压力与落地练习”为例，旧方法先把整篇文档压成一行，再每 512 字符硬切，可能让“何时升级支持”标题和正文分开。现在的顺序是：

```text
读取 grounding-and-acute-stress-practice.md
→ 识别 Markdown 标题
→ 按空行识别段落
→ 标题和本节正文尽量放在同一个 chunk
→ 只有某一个段落超过 512 字符，才使用 512/64 滑窗
→ 超长段落的每个碎片都重新带上所属标题
```

`64` 是相邻长片段重复的字符数，用于保护正好落在边界附近的短语。当前 18 篇种子文档最终生成 79 个 chunk。

### 3. 分块后到底存在哪里

生产主链的权威副本在 MySQL 表 `knowledge_chunks`。每一行保存：

```text
id              数据库 chunk id
source          原文件名或管理员上传时指定的来源名
source_index    这个 chunk 在来源内的顺序
content         chunk 正文
embedding_json  embedding 模型名、正文 hash 和向量缓存
```

例如可能得到：

```text
id=125
source=grounding-and-acute-stress-practice.md
source_index=1
content="## 一个简短流程 ... 把双脚放在地面 ..."
embedding_json={
  "model":"ollama:qwen3-embedding:0.6b",
  "contentHash":"正文的 SHA-256",
  "vector":[...]
}
```

为什么 MySQL 里还保存一份向量缓存：重建 Chroma 时，如果 provider、模型名和正文 hash 都没变，就不必再次生成 embedding；任何一项变化，旧向量都不复用。

Chroma 默认持久化到：

```text
data/chroma
```

Chroma 保存用于近邻搜索的向量、chunk id、source 和 source_index。它是派生索引，不是唯一真相；目录丢失后可从 MySQL 重建。管理员主动备份的 Chroma 快照放到 `data/chroma-snapshots`，默认只保留最近五份。

当前已经实际完成：安装 Chroma 1.5.9，下载约 639MB 的 `qwen3-embedding:0.6b`，生成 79 条 1024 维向量；`data/chroma` 当前约 1.97MB。重新安装时只需：

```bash
ollama pull qwen3-embedding:0.6b
python scripts/setup_rag_vector.py --local-sqlite
```

`--local-sqlite` 用于不启动 MySQL 的本机验证。正式 Docker 环境会在 app 启动时自动同步 MySQL 和 Chroma，也可以调用 `POST /api/admin/knowledge/rebuild-vector` 手动重建。这些都是离线建库，不属于聊天请求。

Engineering Harness 不碰生产 MySQL。它把相同 chunk 写入：

```text
target/harness/mindbridge-harness.sqlite3
```

并关闭真实向量调用，用确定性 BM25 跑回归。因此“测试 SQLite 文件大小”不能被说成“生产向量库大小”。

### 4. 平时聊天会不会重新分块和向量化

不会。启动时先同步 18 个来源并一次性构建文档向量；管理员上传时只更新对应来源；聊天请求只把 Context 改写后的一个 query 临时变成向量，然后查询 Chroma。查询热路径已经移除全库扫描和懒重建，不会每次拿 79 个 chunk 重新 embedding。

### 5. 管理员上传文件和增量更新

管理员上传 Markdown、txt 或 PDF 后，系统先检查文件大小、来源名、内容长度和 Prompt 注入信号。PDF 由 pypdf 抽取文本。通过后仍然走同一个分块函数，但不会粗暴删除整个来源：

```text
同名来源内容完全没变 → 跳过数据库和向量更新
某些 chunk 正文没变   → 保留原 id 和 embedding，只更新顺序 metadata
新增或修改的 chunk     → 只计算这些正文的 embedding，再 upsert Chroma
已经删除的 chunk       → 同时从 MySQL 和 Chroma 删除
换 provider/模型/正文   → hash 校验失败，重新生成对应向量
```

Embedding 默认每批 16 条、单批超时 60 秒，避免首次 79 条一起提交导致超时。被识别为恶意指令的知识不会进入 RAG。

状态接口会直接告诉管理员来源数、chunk 数、字符数、每个来源的 chunk 数、切块策略、embedding 模型、Chroma 目录和索引数量：

```text
GET /api/admin/knowledge/status
```

## 二、示例问题是怎样检索的

### 1. 先决定是否应该查知识库

Understanding 和 Safety 并行完成后，这个示例被路由为 CONSULT，所以 Coordinator 才发布 Context 命令。普通的“Python 列表推导式是什么”走 CHAT，不执行心理知识检索。

ContextAgent 会尝试把问题改写为较短的检索 query，例如：

```text
答辩焦虑 心慌 失眠 睡眠支持
```

若原输入有心理领域信号，但模型把 query 改成不相关内容，系统丢弃改写结果，退回原输入。领域门还会拒绝天气、快递、显卡、历史等无关查询。

### 2. 两路召回

有 embedding 服务时：

```text
query → qwen3-embedding:0.6b → Chroma 取最多 16 个向量候选
query → 中文词项和 bigram → MySQL 全部 chunk 的 BM25 → 取最多 16 个关键词候选
```

Ollama、Chroma 或向量请求失败，且 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，只保留 BM25 路线；如果配置为 required，则不允许悄悄降级。也可以设置 `EMBEDDING_PROVIDER=openai` 和 API Key，切换到 OpenAI embedding。

### 3. 为什么用 RRF，而不是直接加分

向量 cosine 和 BM25 的原始分数不是一个量纲。系统现在按名次融合：

```text
RRF 分数 = 0.65 / (60 + 向量名次) + 0.35 / (60 + BM25 名次)
```

某个睡眠 chunk 若在向量榜第 2、BM25 榜第 1，它的未归一化分数约为：

```text
0.65 / 62 + 0.35 / 61 = 0.01622
```

两路都靠前的证据会得到更稳定的排名。`k=60` 是常见默认值，Microsoft 对混合搜索 RRF 的说明也使用 `1/(rank+k)` 并给出 60 的经验值：https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking

融合后还会用 query 词项相似度、覆盖率和短语命中做本地 rerank，低于 0.45 的结果丢弃，最终最多保留 4 条。第一条命中的相邻 chunk 会一起展开，避免答案只拿到半段上下文。

### 4. 怎样防止“检索到了但模型瞎编”

Context 把四条证据编号为 `[K1]` 到 `[K4]`。Response Prompt 明确要求：知识性结论只能来自这些标签，引用必须放在相应句末；资料不足就说无法从现有资料确认。

最终输出还有一次代码门禁：

- 使用了 RAG 却没有任何 K 引用，拦截。
- 输出了本轮不存在的 `[K9]`，拦截。
- 高风险回复缺少当前安全和现实支持，拦截。
- 出现诊断或药物剂量表达，拦截或安全替换。

引用存在仍不等于事实一定正确。主流 RAG 评测会把检索相关性、回答正确性、回答相关性和 groundedness 分开评估，项目下一步也应增加回答级评测。参考：https://docs.langchain.com/langsmith/evaluate-rag-tutorial

## 三、4 个 Skill 到底是什么

Skill 存在 `skills/<name>/SKILL.md`，不存 MySQL，也不是新的 Agent。RAG 提供“可以引用的事实”，Skill 提供“回复应该按什么方法组织”。原来的焦虑、睡眠、学业、低落、人际、适应和转介分别占一个文件，容易一次加载很多重复规则；现在合并成 4 个职责清楚的 Skill：

| Skill | 什么时候用 | 实际作用 |
| --- | --- | --- |
| supportive_response_baseline | 除 `CHAT + LOW` 外的学生支持/危机路径 | 共同的共情、非诊断和简洁行动规则 |
| campus_support_toolkit | 不属于危机但需要支持的路径 | 集中放焦虑、睡眠、学业、低落、人际、适应和转介方法；每次只选一到两个 |
| high_risk_safety_plan | `intent=RISK` 或 `risk=HIGH` | 固定共情、当前安全、现实支持、一个安全问题的顺序 |
| counselor_handoff_summary | 后台报告后处理 | 给授权工作人员渲染最少必要交接摘要，不给学生看 |

选择不再读取用户中文关键词，也不让模型自行决定加载哪个文件。ContextAgent 用 Understanding 已保存的原始 `intent` 和 Safety 已保存的 `risk` 查下面这张完整表：

| intent \ risk | LOW | MEDIUM | HIGH |
| --- | --- | --- | --- |
| CHAT | 无 | baseline + toolkit | baseline + high_risk |
| CONSULT | baseline + toolkit | baseline + toolkit | baseline + high_risk |
| RISK | baseline + high_risk | baseline + high_risk | baseline + high_risk |

可以把规则记成三句话：

1. `CHAT + LOW` 是普通闲聊，Coordinator 直接去 Response，不调用 Context，也不加载 Skill。
2. 只要 `intent=RISK` 或 `risk=HIGH`，就用 baseline + high_risk；两个分类结果不一致时按更安全的一边处理。
3. 剩下需要帮助的路径统一用 baseline + toolkit。

例如用户说“考试快到了，我连续失眠，不知道怎么办”，Understanding 得到 `CONSULT`，Safety 得到 `LOW`，因此只加载：

```text
supportive_response_baseline
campus_support_toolkit
```

工具箱收到后不会把七类建议全部输出，而是依据问题只选择“睡眠 + 学业”一到两个方法。具体电话、地址、开放时间等事实仍必须来自 RAG 的 `[K]` 证据，Skill 不能编造。

每个 Skill 文档包含适用条件、Prompt 规则、人工工作流、禁止事项、version 和参考资料，但模型只接收 `## Prompt 规则` 小节。这样人看到完整治理规则，模型收到的上下文仍然短。后台 counselor handoff 不属于九宫格，不进入学生回复 Prompt。

## 四、四个 Agent 还有没有自己的私有记忆

没有四份重复的 Redis 私有仓库，这是刻意设计，不是把记忆能力删掉。

系统只有一份按 session 隔离的短期会话记忆：Redis 最多 40 条、默认 TTL 24 小时；Redis 不可用时从 MySQL `chat_messages` 恢复。超过上下文预算时，Context 保留最近 8 条，并为更早内容生成受限摘要。Blackboard 的 context 分区保存本轮真正交给下游的 memory brief、消息视图、RAG 证据和 Skill 版本。

不同 Agent 得到不同的最小视图：Safety 最多查看最近 8 条且每条限长；Context 负责完整的加载与压缩；Response 只消费 Context 已整理的视图；Understanding 主要看当前输入。这样既有角色隔离，又避免四份私有记忆互相矛盾、写放大、无法统一删除和恢复。

## 五、危险判断只是枚举词汇吗

旧实现确实近似“只要包含自杀、自残等任意词就 HIGH”，误报风险较高。现在规则集中在 `app/services/safety_policy.py`，版本是 `safety-signals-v3`，返回规则 id，而不是把命中的敏感原文到处复制。

示例：

```text
“我今晚想结束自己的生命”
→ 命中第一人称 + 直接意图 + 即时性
→ CRISIS_DIRECT_SELF_HARM / CRISIS_PLAN_OR_IMMEDIACY
→ 不等模型，直接 HIGH

“我已经开始伤害自己了”
→ 命中正在或已经实施
→ CRISIS_ACTIVE_ATTEMPT
→ 直接 HIGH

“我今晚准备伤害别人”
→ CRISIS_DIRECT_HARM_TO_OTHERS
→ 直接 HIGH

“我没有想自杀，只是在了解相关知识”
→ 命中明确否定
→ 不触发硬 HIGH，仍由 Safety 模型结合上下文评估

“论文要讨论自杀预防机制”
→ 科普/研究语境，没有危险第一人称意图
→ 不触发硬 HIGH

“最近真的快撑不住了”
→ 很值得重视，但没有足够高精度的即时危险结构
→ 交给 Safety 模型；模型不可用时 fail-closed 到 MEDIUM
```

因此规则层不是诊断器，而是高精度、低延迟、可审计的安全保险。模糊语义仍需要模型和上下文，不能靠无限扩充关键词表。

## 六、RAG 量化结果是不是 mock 出来的

评测集是人工编写的固定输入和相关来源标签，可以叫“合成/离线样本”；指标不是手填的。`python -m app.harness.runner --suite rag` 会真的创建隔离数据库、同步知识、执行分块、领域门、BM25、RRF、rerank、阈值和 Top 4，再从结果计算指标。

历史 RAG 改造对比（“当前”列指该次 RAG 改造后，不是此次工作流改造的增量）：

| 指标 | 改造前 | 当前 | 变化 |
| --- | ---: | ---: | ---: |
| 知识来源 | 11 | 18 | +63.64% |
| chunk 数 | 34 | 79 | +132.35% |
| 正样本 Recall@4 | 96.67% | 100.00% | +3.33 个百分点 |
| Precision@4 | 65.42% | 65.83% | +0.41 个百分点 |
| MRR | 90.69% | 96.11% | +5.42 个百分点 |
| NDCG@4 | 89.80% | 94.99% | +5.19 个百分点 |
| HitRate | 96.67% | 100.00% | +3.33 个百分点 |
| 负样本拒绝率 | 未测 | 100.00% | 新增 8 条负样本 |
| 检索决策准确率 | 未测 | 100.00% | 新增指标 |
| 标准 Skill 文件 | 10 | 4 | 合并减少 60%，九条路径仍全部覆盖 |
| unittest | 62 | 84 | +35.48% |

上述历史基准包含 10 轮确定性异步 I/O：串行 p50 222.54ms，并行 p50 125.87ms，阶段延迟降低 43.44%，加速 1.77 倍。它只证明 Python 编排确实并行，不代表单 GPU 上两个模型请求也能获得同等加速。2026-09-12 的 workflow-v2 回归为 104 项单元测试通过、六套 Harness 通过；RAG 指标保持不变，没有重测真实模型并行收益。

改造前后正样本仍是相同的 60 条，但本次同时改变了切块、融合、阈值和知识内容，因此这是“系统版本总体提升”，不能把全部增量归功于某一个算法。Engineering Harness 为了结果可重复仍主动关闭向量，所以表中指标是 BM25 fallback，不是 embedding A/B；本机已经另外使用 `qwen3-embedding:0.6b` 建成 79 条、每条 1024 维的真实 Chroma 索引并完成查询验证。

更大的库不一定更好。内容重复会让 Top 4 被相似段落占满，Precision 反而下降。生产验收应按“主题覆盖、校方信息覆盖、更新时间、权限和回归集表现”治理，而不是追求一个漂亮的 chunk 数。

## 七、微调模型效果提升多少，怎样自动测

项目新增：

```text
app/model_eval/psychology-ai-model-eval.json
scripts/evaluate_model_quality.py
```

评测集有 12 个固定场景，覆盖危机安全、焦虑、睡眠、低落、学业、人际、适应、诊断边界、药物边界、Prompt 注入和普通问题。每题使用“概念组”和“禁止项”，没有 `expectedResponse="固定中文句子"`。例如“联系/找”表达任意命中一个都算该概念完成，因此不是碰运气比字符串。

公平比较方式：

```text
基座 qwen2.5:7b                  ┐
                                 ├→ 同一 system prompt
微调 mindbridge-qwen2.5-7b-ft    ┘  同一 12 条输入
                                    temperature=0
                                    seed=42
                                    num_predict=512
                                    每个模型重复 3 次
```

运行：

```bash
python scripts/evaluate_model_quality.py \
  --baseline-model qwen2.5:7b \
  --candidate-model mindbridge-qwen2.5-7b-ft:latest \
  --repeats 3
```

输出包括总体通过率、概念覆盖率、边界通过率、各类别通过率、P50/P95 延迟、逐题 wins/ties/losses 和相对提升。

本次机器上的真实状态是：Ollama `127.0.0.1:11434` 不可连接，Docker Desktop daemon 也未运行，模型目录只有 Modelfile，没有 GGUF 权重。因此 `target/model-quality-comparison.json` 被明确写为 `BLOCKED`，当前真实模型提升百分比未知。任何“微调提升 20%”都会是伪造数据。

准备好模型后的最短步骤：

```bash
ollama pull qwen2.5:7b
ollama create mindbridge-qwen2.5-7b-ft:latest -f models/mindbridge-qwen2.5-7b-ft/Modelfile
python scripts/evaluate_model_quality.py --repeats 3
```

先看安全和边界是否退化，再看帮助性和延迟。只有候选模型在关键安全题不下降、总体提升有重复性，才应替换生产默认模型。后续可加入独立 judge 模型做语气、相关性和 groundedness 评分，但不能只让候选模型给自己打分。Ollama 支持 `seed` 和温度控制，可用于提高成对测试的可重复性：https://docs.ollama.com/modelfile

## 八、为什么测试里仍然会出现固定中文字符串

像下面这样的旧测试：

```python
self.assertEqual(completed.state.response.final_response, "先从今晚固定起床时间开始。[K1]")
```

测试目的其实是确认“传给 generation_completed 的文本，是否原样写进 Blackboard”，不是要求线上模型必须说这句话。现在测试先定义 `generated_text`，写入后再与同一个变量比较，并增加注释说明它测试的是无损持久化。

需要区分三类固定内容：

1. 单元测试 fixture：允许固定，目的是让状态变化可重复。
2. `AI_PROVIDER=mock` 的回复：只供 Harness，不是线上生成。
3. 生产安全策略：可以固定规则和边界，但不能固定最终自然语言答案。

模型质量评测已改成概念组和禁止项，不再通过背出某一句中文来判定模型好坏。

## 九、中文硬编码这次怎样治理

不是“代码里出现中文就错”。中文用户界面、人工审阅的兜底回复、中文安全模式和测试输入，本来就必须包含中文。真正危险的是用某个字面词代替语义或状态，例如“出现‘代码’就一定 CHAT”“高风险回答必须包含‘安全’两个字”。这类生产捷径已经删除。

现在四层分别负责不同工作：

1. Understanding 使用带 schema 的模型分类；高风险硬规则先行，但普通 CHAT/CONSULT 不再由中文关键词抢跑。分类结果非法或模型故障保守进入 CONSULT。
2. Response 把非诊断、眼前安置、真人支持、紧急升级、RAG 引用写成 `ResponsePolicyContract` 布尔字段，并在 Prompt 中携带契约 hash。
3. Prompt Safety Review 校验“契约是否与 Blackboard 当前风险和证据一致”，不搜索某句中文。
4. 最终 HIGH 回复由独立模型做严格 JSON 语义复审；规则层继续负责擅长的确定性禁止项和 K 引用合法性。

所有生产 Prompt 已移到 `app/prompts/*.md`，必须声明 `name`、`vN` 版本，运行时记录 `PROMPT_ID`、正文 SHA-256 和模板组合版本。模板占位符缺失或多传会直接失败，便于 code review、灰度和回滚。`AI_PROVIDER=mock` 和 unittest 仍保留固定中文，它们只负责造出可重复结果，不参与真实模型的线上质量结论。

## 十、当前结论

RAG 的存储和数据流现在已经可解释、可查询、可重建：原始种子 md → 语义分块 → MySQL 权威 chunk/embedding cache → Chroma 派生向量索引 → BM25/向量双路召回 → RRF → rerank → 证据门 → K 引用 → 输出门禁。

Skill 从 7 份简短提示扩为 10 份版本化治理文档，并采用“完整文档给人审、Prompt 规则给模型”的方式控制上下文。危险规则也从裸关键词包含改成版本化上下文模式，并用否定、科普、直接意图、实施和即时性样本回归。

目前可以诚实宣称的量化提升是检索与工程回归提升；微调模型提升尚未实测，原因和自动评测命令已经落到报告与代码中。真正上线前还需要用真实 embedding 跑同一评测集，并让心理专业人员审核匿名失败样本和校方资源内容。
