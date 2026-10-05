# 开发阶段与回溯提交

整理日期：2026-10-16。下列日期是回溯编排，不代表对应功能实际在当天开发。作者/提交者时间统一使用 Asia/Shanghai 时区（UTC+08:00）。

## 月份安排及原因

| 月份 | 阶段 | 为什么排在这里 | 验收重点 |
| --- | --- | --- | --- |
| 5 月 | 项目结构、意图、角色与工具基础 | 先定义请求如何分类、由谁处理及如何调用能力 | Python 语法与模块依赖；还不是完整应用 |
| 6 月 | 知识、记忆、评测、API 与基础前端 | 基础组件齐备后组装服务，建立可交互入口 | 前端构建；外部服务集成需独立环境 |
| 7 月 | Skills、模型响应处理、语义与记忆增强 | 在已有链路上扩展行为及上下文质量 | 静态检查和接口一致性；不宣称模型效果 |
| 8 月 | 多角色协作、工具隔离、轨迹与降级 | 核心可交互后处理复合任务、可观测性及失败路径 | 编排器离线回归 |
| 9 月 | 金融领域数据、业务工具、本地模式、证据 UI | 先有领域数据，再定义边界和工具，最后贯通 API/界面 | 金额、归属、工单、多轮补参与证据断言 |
| 10 月 1—5 日 | 场景回放、部署、文档 | 以可重复的验证和清晰的能力说明收尾 | 完整测试、31 场景回放、前端构建 |

## 每一步的增量

早期阶段展示通用后端组件与最小对话前端。9 月整合当前金融代码时已包含现有修复，不人为删除修复来伪造 bug 历史。最后的测试提交补齐可重复验证。所有依赖锁定文件采用整理时可用版本，不声称还原当时安装环境。

| 序号 | 分配日期 | 短备注 | 增量 |
| --- | --- | --- | --- |
| 01 | 2026-05-08 | chore: initialize project | 建立 Python + Vue 单仓库结构。 |
| 02 | 2026-05-15 | chore: configure runtime dependencies | 准备依赖、示例配置及 Python 包目录。 |
| 03 | 2026-05-22 | feat: add intent recognition | 构建意图分类、紧急度、实体和融合投票。 |
| 04 | 2026-05-26 | feat: add agent routing | 建立通用、技术、支付及升级角色与路由。 |
| 05 | 2026-05-29 | feat: add tool management | 实现工具注册、执行、缓存、重试和降级。 |
| 06 | 2026-06-05 | feat: integrate knowledge retrieval | 增加 ChromaDB 知识导入和检索接口。 |
| 07 | 2026-06-12 | feat: add conversation memory | 接入工作记忆、历史检索、摘要及偏好接口。 |
| 08 | 2026-06-19 | feat: add evaluation and monitoring | 建立意图评测、模型评分器和运行指标。 |
| 09 | 2026-06-26 | feat: expose service APIs | 组装聊天、知识、评测和健康检查接口。此阶段需外部服务配置。 |
| 10 | 2026-06-30 | feat: add chat interface | 建立可构建的 Vue 对话页，连接 Python API。 |
| 11 | 2026-07-06 | feat: integrate domain skills | 增加 Skills 加载、匹配、提示词注入和重载。 |
| 12 | 2026-07-13 | refactor: normalize LLM responses | 统一提取模型文本并更新工具与会话调用。 |
| 13 | 2026-07-27 | feat: improve intent and memory handling | 增强语义兜底、请求上下文和角色选择。 |
| 14 | 2026-08-06 | feat: add agent collaboration | 加入主协作角色、复合请求并行和独立角色契约。 |
| 15 | 2026-08-13 | refactor: isolate agent tools | 分离工具定义，限制各角色能力，补充工具调用验证。 |
| 16 | 2026-08-20 | feat: add tool tracing | 串联共享检索与请求级工具轨迹接口。 |
| 17 | 2026-08-27 | fix: improve routing fallbacks | 更新监控惩罚下的路由回退并补充回归测试。 |
| 18 | 2026-09-04 | feat: add financial data layer | 引入版本化模拟产品、业务快照、Decimal 费用和幂等工单存储。 |
| 19 | 2026-09-09 | feat: add multi-turn policy checks | 实现费用槽位、任务切换、操作边界和最终证据校验；待下一步挂接工具。 |
| 20 | 2026-09-14 | feat: add financial service tools | 接入产品、费用、业务查询、工单工具和离线计划器。 |
| 21 | 2026-09-19 | feat: integrate financial workflows | 串联金融角色、本地资料/会话、鉴权归属和请求幂等。 |
| 22 | 2026-09-24 | feat: display financial evidence | 展示产品、费用、工单、来源版本与工具追踪；仅保留 Python 后端。 |
| 23 | 2026-09-29 | test: add financial regression coverage | 验证费用分档、越权、幂等、多轮更正、证据和知识更新。 |
| 24 | 2026-10-01 | test: add scenario replay | 加入 31 场景 45 轮的合成开发集、严格断言和真实执行报告。 |
| 25 | 2026-10-02 | fix: preserve ticket IDs during redaction | 修复长数字工单号被误脱敏后无法查询和补充的问题，增加固定编号回归；此问题是在实际整理日发现。 |
| 26 | 2026-10-03 | chore: add Docker and CI configuration | 增加仅前后端的容器配置和持续集成；保留本地演示默认值。 |
| 27 | 2026-10-05 | docs: complete project documentation | 统一运行说明、架构拆解与验证记录。 |

## 历史完整性

历史完整性检查需覆盖所有提交：日期范围与顺序、作者字段、非空增量、文件白名单、秘密信息扫描、Python 语法、当前代码与最终包一致性。检查在实际整理日执行，不能写成历史上已完成的 CI。
