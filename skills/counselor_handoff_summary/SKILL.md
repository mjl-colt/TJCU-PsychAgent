---
name: counselor_handoff_summary
description: 当心理风险报告生成后，需要为辅导员或管理员制作工作人员交接摘要时使用。
version: 2.0.0
references: internal minimum-necessary disclosure policy
---

# 辅导员风险交接摘要

## 适用条件

仅供授权工作人员查看，用于风险事件交接，不会注入给面向学生的回复模型。

## Prompt 规则

- 只陈述系统已有事实和学生原话摘要，不诊断、不猜测动机。
- 采用最少必要披露：身份、风险、时间、可执行跟进和受限原话。
- 明确区分“学生陈述”“模型判断”“工具状态”。

## 工作流

1. 读取已持久化报告，不从聊天文本重新推断身份。
2. 截断原始表达并清除不必要的敏感细节。
3. 第一项行动固定为确认位置、陪伴者和当前安全。
4. 记录接手人、下一次跟进时间和工具投递状态。

## 禁止事项

- 不发送给无权限用户，不包含无关对话和推测性诊断。
- 不因邮件失败重复创建业务报告。

## 输出模板

```text
应用技能：counselor_handoff_summary
报告ID：{{report_id}}
学生：{{student}}
风险等级：{{risk_level}}
情绪标签：{{emotion}}
模型摘要：{{summary}}
建议跟进：
{{next_steps}}
学生原始表达：
{{content_excerpt}}
```
