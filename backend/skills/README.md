# 金融角色 Skills

启动加载五份 `SKILL.md`：通用金融服务、技术排障、支付费用、基金产品、人工工单。`agents` 约束注入角色；文件用于业务语气、核验顺序与资料引用指导。

Skills 不是权限边界。真实归属检查、金额校验、工单写入限制与最终输出处理分别位于 `finance/store.py`、`finance/tools.py`、`finance/policy.py`。管理员可调用 `/skills/reload` 重载，普通用户无法借聊天重载或修改 Skills。
