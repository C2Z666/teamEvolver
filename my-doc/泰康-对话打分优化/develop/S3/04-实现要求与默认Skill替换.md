# S3 实现要求与默认 Skill 替换

本文件是后续开发、部署的执行要求。当前文档交付没有修改生产代码、网页配置、运行任务或 OV。

## 1. 实施顺序

1. 实现 Evidence 专用协议、输入预算、单阶段状态机、回执校验及交付格式。
2. 接入现有 OV Compile，保留强制 Map，核对实际 FileResponse、预算、部分回执和恢复。
3. 完成网页协议识别、配置与进度/结果展示，保留旧任务兼容。
4. 加入生产 Skill 资源，更新代码默认入口及其所有使用者，完成工程与小规模业务验收。
5. 执行目标泰康配置的一次性 CAS 迁移，重启受影响服务，核验网页、新任务快照与 OV 实际正文一致。

禁止先把新正文交给旧 S2 执行器运行，再用失败重试“修复”。历史 S0/S2 任务继续按其冻结协议处理，不导入其提取结果作为新 S3 输入。

## 2. 模块落点

以下为当前已存在代码及待改职责；新模块路径是建议，不表示已实现。

| 位置 | 要求 |
| --- | --- |
| team_miner/skill_miner/default_compiler.py | 默认资源改为 S3，导出正确 PROTOCOL/VERSION/SKILL_BODY/SHA256；不再调用 S2 sections 验证 S3 |
| team_miner/skill_miner/settings.py | 保存/读取/reset 按明确协议验证；解除把当前默认 PROTOCOL 别名为 S2_PROTOCOL 的耦合 |
| team_miner/skill_miner/job_operations.py | 支持新协议受理，冻结正文和预算，拒绝不兼容输入；不能只用正文某处包含旧字符串就误路由 |
| team_miner/skill_miner/dialogue_dimensions/__init__.py | 扩展现有对话任务分发，明确 v1/v2/Evidence v1 分支；未知协议不回退旧执行器 |
| team_miner/skill_miner/engine.py 与 core_repository_part0.py | 检查新协议任务的快照持久化、Worker 路由和恢复；不改变历史任务 |
| team_miner/skill_miner/dialogue_dimensions/input.py、dataset.py、preparation.py、transport.py | 复用已验证的原文、定位、准备和传输能力；新职责通过明确参数或公共辅助函数承接，不导入其他 Handler |
| team_miner/skill_miner/dialogue_dimensions/v2/executor.py | 参考现有幂等、admission、部分消费与限流处理；不让 S3 沿用含 registry/多阶段的 v2 包装 |
| team_miner/skill_miner/dialogue_evidence/（建议新增） | 按职责实现 compiler、schemas、budget、orchestration、executor、validation、repository、delivery；只创建实际需要的模块 |
| team_miner/skill_miner/frontend/MiningWhiteboxPanel.tsx | 默认/有效正文、协议、版本、保存/reset 和 S3 参数说明 |
| team_miner/skill_miner/frontend/MiningView.tsx、useMiningController.ts | 接受 Evidence 协议，单任务启动，不进入旧维度或 Skill Candidate 分支 |
| team_miner/skill_miner/frontend/MiningJobDetail.tsx、types.ts、miningApi.ts | 新进度、Evidence 与场景/条件、原文定位、导出和 partial 状态 |

不要全局替换 S2_PROTOCOL 常量名称和值。历史协议必须从它自己的固定模块导入；“默认协议”和“支持的协议集合”是两个职责。现有 S2 migration 若依赖 default_compiler.SHA256，须改为引用其固定历史资源或停止重复执行，不能在切换后把 S3 hash 写成 S2 迁移记录。

新可变状态写本领域 PG Repository，Schema 只追加版本 SQL，遵守 RLS、revision CAS、Worker fence 和资源租约。复用领域状态表与新建表由实际数据契约决定，不为每个逻辑实体创建空表。Router 只做短事务受理并返回 202，长操作由 Worker 执行；不新增独立 Service 层。

## 3. OV Skill 与冻结契约

文档正文唯一来源为 [S3 Skill](skills/taikang-dialogue-evidence-s3/SKILL.md)。写法参考 S2，仅保留下面三个按顺序出现一次的分段：

```text
<!-- s3:common:start --> ... <!-- s3:common:end -->
<!-- s3:transport:start --> ... <!-- s3:transport:end -->
<!-- s3:stage:extract:start --> ... <!-- s3:stage:extract:end -->
```

实现独立的 S3 结构验证器，不能要求七个 S2 stage。保存时校验 frontmatter、协议、分段顺序与唯一性；除标题外的规则必须在分段内，不能被裁剪器静默丢弃。用户可编辑分段中的业务规则，合法正文按原样冻结，不被后台替换。

受理时冻结完整 Skill、sha256、配置 revision 和输出契约版本。实际发布给 OV 的包名可以基于 `dialogue-s3-extract-<hash>` 生成，保证并发任务不会互相覆盖；正文包含 common + transport + stage:extract。记录源 Skill hash 和实际执行正文 hash，后者包含确定性输出契约注入。

一个 child 必须收到完整单阶段业务规则、所需结构和实际任务身份。宿主不把开发文档发给 OV，不让 OV 找 Codex 工具或读取仓库路径。使用实际 OV FileResponse，input IDs 和业务 IDs 明确区分。

TE instruction 与 Skill 均保留 Entry 尽早 needs_pipeline=true 的约束。scratch/content_ref 仅在实际 schema 支持时使用，不能用它掩盖一轮必须生成过多内容的问题；容量失败由 TE 拆分。

## 4. 默认编译 Skill 必须写入代码

后续实现应新增生产资源：

```text
team_miner/skill_miner/compiler_skills/taikang-dialogue-evidence-s3/SKILL.md
```

然后将 `default_compiler.py` 的 VERSION 指向 `taikang-dialogue-evidence-s3`，PROTOCOL 指向 `te-dialogue-evidence.v1`，SKILL_BODY 从上述生产资源加载，SHA256 对实际正文计算。默认加载必须走 S3 校验器。

**不是只把文件放进 develop，也不是添加下拉模板；代码返回的默认编译 Skill 必须就是 S3 全文。**文档 Skill 与生产资源实施时做字节级摘要比较。长期测试只依赖生产资源和测试 fixture，不把 my-doc 的历史实施材料作为运行依赖；开发交付检查另行比较两份正文。

以下入口必须一致：

1. 未设置覆盖值时的有效默认正文。
2. 网页“查看默认编译指令”。
3. “恢复默认”返回及刷新后的持久化结果。
4. 新任务冻结的 compiler_body_snapshot、protocol 和 hash。
5. OV 实际使用的阶段正文（允许确定性包名和 Schema 注入差异，应记录摘要链）。

同步更新默认相关测试、前端协议判断、部署包资源清单和说明，不能让 wheel/镜像遗漏新 Skill 文件。

## 5. 目标配置迁移

已保存的 `mining.prompts.ov_compile` 覆盖值优先于代码默认。只改 fallback 会使目标页面继续执行旧 Skill，因此本次实现必须提供并执行一次性、目标租户限定的配置迁移。

迁移流程：读取旧 revision 和实际有效正文 → 在 PG 备份原配置/正文/hash → 以 expected_revision CAS 清除目标旧 ov_compile 覆盖、转为继承 S3 默认 → 保留无关设置 → 保存新的独立迁移标识 → 回读核验默认/有效正文。

迁移标识建议 `taikang-dialogue-evidence-s3-default-v1`，不要复用 S2 标识。计算“旧有效正文”时使用部署前的实际默认及覆盖优先级，不能在加载新默认后把新正文误记为旧正文。CAS 冲突先重新核对，不盲目覆盖并发修改；迁移只执行一次，不在每次启动重置用户后续编辑。

目标泰康旧正文的迁移属于用户明确要求，无需让用户手工粘贴或点恢复默认。其他租户显式自定义覆盖不批量清空；它们看到的代码默认/reset 为新版，但其有效自定义配置保持可见。缺少租户模块配置仍按项目约束返回 503，不回退本机 YAML。

运行中 job 继续使用原快照。迁移不会把已经开始的 S2 流程中途剪掉，也不删除旧结果；需要新建 S3 job 从原始案例运行。

## 6. 网页和发布

复用现有 Skill 挖掘配置入口，不要求用户启动第二套提取。标题/说明使用“提取行为 Evidence、发生场景和使用条件，导出供后续聚合”，candidate_mode=none，不显示评分维度、评分进度或必须产出 Skill Candidate 的提示。

结果页区分原文事实条件和推断的适用建议，提供上下文跳转；零结果、partial、容量失败、未识别角色均需有明确表现。批次数、单元数与案例数分别展示，不能把分段进度当案例完成率。

先完成兼容代码、Schema 与生产资源，测试后再切换默认和迁移配置。按受管入口或精确 PID 重启服务，不使用宽匹配杀进程。验证安装产物中的资源、前端刷新、reset、新任务快照及 OV 计划。

回滚恢复已记录配置和兼容版本；已创建 S3 job 必须由支持新协议的 Worker 继续或明确停止，不能交给旧执行器。记录真实实施状态和验证报告后，才把 S3 README 状态改成“已实施”。
