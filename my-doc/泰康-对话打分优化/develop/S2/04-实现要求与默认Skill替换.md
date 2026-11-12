# S2 实现要求与默认 Skill 替换

日期：2026-11-12。本文是开发与部署要求，不是已完成记录。新正文见 [Skill](skills/taikang-dialogue-dimensions-s2/SKILL.md)。

## 1. 实现顺序与完成边界

1. 实现 v2 数据协议、持久化状态与兼容分发，建立阶段裁剪器。
2. 实现单 job 提取、索引、匹配、集中登记、audit、有限修订及 rubric。
3. 接通现有 OV Compile，验证真实 FileResponse、任务目录和批量 Map。
4. 更新网页配置协议与进度、结果展示。
5. 通过离线与小规模真实验证后，替换默认资源并执行目标配置迁移。
6. 按精确 PID/受管入口重启对应服务，验证网页与新任务实际快照。

不能先把不兼容的新正文设为线上默认，再让旧 extract/merge 执行器尝试运行。旧运行任务保持受理时的协议和正文，新任务使用 S2；这不是要求用户重新运行旧流程。

## 2. 现有代码落点

路径均相对仓库根目录。以下文件已存在；新增模块建议不代表当前能力已实现。

| 现有位置 | 必要改动 |
| --- | --- |
| `team_miner/skill_miner/settings.py` | 默认正文、协议识别、默认摘要/版本、描述、reset 与迁移后的有效值 |
| `team_miner/skill_miner/knowledge_compiler.py` | 当前被 settings 当作默认来源；解除这一隐式绑定，不把知识点编译器符号悄悄改成对话编译器 |
| `team_miner/skill_miner/job_operations.py` | 按明确协议受理；支持原始对话准备子阶段；冻结正文/配置；拒绝不兼容数据 |
| `team_miner/skill_miner/core_repository_part0.py` | 对话分支也必须存 compiler_body_snapshot，不能只有非 dialogue 分支保存 |
| `team_miner/skill_miner/engine.py` | S2 执行读取任务冻结正文，不能在各阶段重新读取当前全局配置 |
| `team_miner/skill_miner/dialogue_dimensions/__init__.py` | v1/v2 明确分发；不能仅把 PROTOCOL 字符串替换后让旧任务失去路由 |
| `team_miner/skill_miner/dialogue_dimensions/input.py`、`dataset.py`、`preparation.py` | 保留原文解析，新增紧凑模型视图，纳入同 job 的 prepare 阶段 |
| `team_miner/skill_miner/dialogue_dimensions/orchestration.py` | v1 仅处理已冻结旧任务；v2 使用新状态机，不调用 plan_merge |
| `team_miner/skill_miner/dialogue_dimensions/validation.py`、`assembly.py` | v1 保留兼容；新增 v2 明确 Schema、稳定维度版本与覆盖装配 |
| `team_miner/skill_miner/dialogue_dimensions/state.py`、`recovery.py` | 新状态/分页/CAS/任务恢复；不允许新任务导入 S0 分析回执 |
| `team_miner/skill_miner/dialogue_dimensions/transport.py` | 复用来源校验与知识制品写入能力，限定包大小，区分宿主 deferred 写入与 Compile 完成 |
| `team_miner/skill_miner/frontend/MiningWhiteboxPanel.tsx` | 默认/有效正文、协议与版本，S2 参数语义、保存和恢复默认 |
| `team_miner/skill_miner/frontend/MiningView.tsx`、`MiningJobDetail.tsx` | S2 单任务进度、评分标准入口、局部问题，不显示必须产生 Skill Candidate 的误导状态 |
| `team_miner/skill_miner/frontend/types.ts`、`miningApi.ts` | 同步响应字段与前端类型 |

建议在 `dialogue_dimensions/v2/` 内按职责拆分 schemas、input_view、orchestration、registry、matching、audit、rubric、repository、executor、delivery 等模块；只创建实际实现所需文件。每个手写生产文件不超过 1000 行。不要复制整套 v1 后留下两套模糊分支。

新增业务表按领域追加新的 schema 版本 SQL，不修改已发布版本；受理、登记和消费必须符合 RLS、revision CAS、Worker fence。临时文件只用于执行和调试，不作为任务权威状态。

## 3. 默认 Skill 替换是必做项

### 3.1 生产资源与唯一默认入口

将本设计包中的 Skill 复制为生产资源：

```text
team_miner/skill_miner/compiler_skills/taikang-dialogue-dimensions-s2/SKILL.md
```

新增职责明确的默认编译器导出模块，例如 `default_compiler.py`，从此资源加载 `SKILL_BODY / PROTOCOL / VERSION / SHA256`。`settings.py` 的默认值和 reset 使用同一个导出，S2 执行器冻结来源也一致。

此路径为待新增路径，不在本次文档交付时创建生产资源或替换运行代码。实施时用摘要比较文档包与生产资源，避免复制后漏更新。

以下均须得到新 Skill 全文：

1. 尚未保存编译正文时，网页 textarea 的有效默认值。
2. “查看默认编译指令”的正文。
3. “恢复默认”接口返回值和持久化后的再次读取值。
4. 新任务受理时的完整 Skill 快照。
5. Worker 按阶段裁剪所使用的基准正文。

不得只改前端占位文本，不得仅添加一个可选模板，不得继续从旧知识点 v8 或 S0 资源返回默认值。

### 3.2 已保存配置的处理

当前有效正文逻辑是 `mining.prompts.ov_compile` 优先于内置默认。因此只改 fallback 无法让已有泰康页面直接生效。

本次目标泰康租户/部署需执行一次可审计配置迁移：

1. 读取配置及 expected_revision，保存旧有效正文、摘要、revision 和迁移标识到 PG 审计记录。
2. 将该目标配置切换为继承 S2 默认，清除旧 `ov_compile` 覆盖值；保留无关配置。
3. 以 revision CAS 写入，冲突则重新核对，不能丢弃并发保存的新内容。
4. 保存一次性迁移标记；不能在每次启动时重置用户之后编辑的新 Skill。
5. 重新读取 settings，确认 default_prompt 与 effective_prompt 均为 S2，overridden=false。

目标范围包括此前手工粘贴保存的泰康旧 Skill，不要求用户再点恢复默认。其他租户的非目标自定义配置不批量清空；它们的内置 fallback/reset 仍更新为 S2，自定义有效正文保持显式状态。

迁移属于发布流程，不通过应用启动自动建表或隐式循环覆写。原正文保存在 PG，可审计恢复；历史 job 冻结快照保持不变。

### 3.3 前后端响应与文案

保留现有 `id=ov_compile` 的编辑入口，不新增一套让用户二次启动的配置页面。复用 `/api/mining/settings` 与 `/api/mining/prompts/ov_compile/reset`，保留 expected_revision。

S2 建议新增响应字段：execution_protocol、default_version、default_sha256、effective_sha256、migration_status。它们均为待实现字段，前后端类型和测试同时更新。

当前 settings 的 candidate_mode 以 single/collection 描述 Skill Candidate。S2 应明确为不产生 Candidate 的模式（例如 candidate_mode=none），不能把缺少旧 PLAN_SCHEMA_VERSION 解释为旧版单候选警告。

文案改为“一次任务自动完成行为提取、共享维度匹配、全量检查和候选评分标准生成”。S2 隐藏或解释原“挖掘轮数上限”：用户任务固定一次，novelty/revision 是内部有界阶段参数，不能复用 max_rounds 导致业务阶段中途结束。

对非对话资料，在受理时给出数据格式不匹配说明；不得用新默认静默跑旧知识点逻辑或把任意文档当作保险对话。旧知识点资源可保留作为明确自定义兼容能力，不再充当本次网页默认。

## 4. Skill 冻结与阶段裁剪

新 Skill 为单文件完整正文，使用固定分段标记：common、transport 及 stage:extract/propose/match/register/audit/revise/rubric。

宿主在受理时验证全部标记存在且唯一，保存原文，不使用模型抽取阶段规则。执行时由确定性裁剪器组合：frontmatter + common + transport + 当前 stage。冻结并记录完整正文 hash 和阶段正文 hash，阶段不能读取其他任务或当前最新全局配置。

网页允许编辑分段内部的业务规则。缺失/重复分段、协议不支持或无法构建阶段正文时，保存/受理应返回明确错误，不能退回全量 Skill 或静默替换成默认。

新 Skill 没有必须上传的外部附件，模型不需要读取本目录开发文档。宿主通过 task packet 提供实际 stage、字段 schema、ID、版本、输出路径和预算。

普通配置保存不能改变运行中 job。自定义 S2 规则也必须经相同冻结与阶段裁剪；后端不在后台用内置正文覆盖用户已保存且合法的新规则。

## 5. 模型执行要求

OV Compile 输入使用独立目录，里面只放本次同阶段的有界 packet 文件；不能传整个案例根目录。文件 Map 每个 child 负责一个 packet，输出一份业务回执文件。

任务参数不得伪造当前 OV 不支持的重复算子或跳过 Finalize 参数。需要 Embedding 时通过平台 OV 客户端调用，校验有效 account、目标权限及模型指纹；缓存按模型指纹和规范化文本摘要隔离并存 PG。

如果已部署 OV 不支持所需接口或 FileResponse，预检给出可执行缺口并停止对应能力，不把向量召回悄悄替换为全量大模型归并。

所有批量规划考虑完整输入和预期输出，限制 Compile 总量；短任务批量提交的真实效果需要实测，不把“包数下降”直接写成“耗时下降”。

## 6. 任务进度与结果界面

展示单个 job 的：已提取案例/总案例、已归属 Evidence/总可消费 Evidence、待发现数、登记复用/新增数、audit 已完成对数/计划对数、rubric 完成数、问题数。

进度数字由宿主状态计算。读过文件不等于已分析，OV running 残留字段不能覆盖已确认的宿主终态。用户只看到一个流程，无需手动创建归并或校准任务接力。

结果分别展示评分维度、场景分支、方法实例、Evidence 与问题。提供来源定位和维度版本，突出 provisional/pending，不把可阅读结果等同于正式评分模型。

## 7. 历史兼容与删除范围

S2 新任务不走 S0 计划、不导入旧分析结果；旧任务可查看、取消或按其已冻结协议恢复，不强行迁移中间语义结果。

“原来的不要”指新流程不依赖旧分析产物，不意味着删除原始数据、历史记录、旧任务制品或其他租户配置。保留历史可追溯性，不在本需求中增加清库操作。

## 8. 发布检查

- S2 schema、后端、生产 Skill 与前端构建共同准备，相关测试通过后才切默认。
- 目标配置完成一次性 CAS 迁移，记录备份与摘要。
- 通过受管入口/精确 PID 重启服务，禁止宽匹配杀进程。
- 打开网页验证默认/有效正文，刷新后仍正确；恢复默认也返回同一摘要。
- 创建小规模新任务，核对协议、Skill 快照、阶段 hash、OV 实际计划和最终制品。
- 回滚采用已记录版本与显式配置变更；不能让 v1 worker 消费 v2 job，运行中的 v2 任务需由兼容 worker 完成或明确中止。
