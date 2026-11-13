# S3 行为 Evidence 展示界面

沿用 S2 展示台的本机前后端方式，独立展示 `te-dialogue-evidence.v1` 真实任务。

## 打开与启动

浏览器打开 <http://127.0.0.1:52131>。如服务未运行，在项目根目录执行：

```bash
bash "my-doc/泰康-对话打分优化/develop/S3/展示界面/start.sh"
```

使用项目 `.venv`、根目录 `.env` 与现有 PostgreSQL，无需 npm 安装或构建。
S2 默认端口 52130，S3 默认端口 52131，可同时使用。

可选变量：

- `TE_EVIDENCE_PORT`：默认 52131。
- `TE_EVIDENCE_HOST`：默认 127.0.0.1，仅支持本机地址。
- `TE_EVIDENCE_TENANT_ID`：默认 default，查询受租户 RLS 隔离。
- `TEAMEVOLVER_PG_SCHEMA`：默认 teamevolver。
- `OPENVIKING_DATA_ROOT`：可指定 OV 的 compile_tasks / compile_workspaces 所在目录。
- `OPENVIKING_PROJECT_ROOT`：默认本机 OpenViking 项目路径，观测目录为其 `.local-data/data/bot`。

## 使用

- 运行历史列出当前租户最近 500 个未删除的 S3 任务，初次优先选活动任务。
- 活动任务默认打开“运行过程”；结束任务默认打开“提取成果”，可随时切换。
- 每 5 秒自动刷新。手动选择的视图、案例、搜索条件和展开记录会保留。
- 运行过程展示阶段、有效单元验收进度、当前 Compile、单元状态、责任句数、语义修复、传输退避和容量拆分。
- 可用时展示最近 40 次 Compile 的本机 OV 状态、调用阶段、耗时、token 和工具名称。观测缺失不推断为零调用；不展示指令或工具参数。
- 提取成果区分行为、发生场景、触发情境、观察条件、推断建议、客户回应和不确定性，支持搜索、案例与覆盖状态筛选。
- 点击句子编号或“原文与引用”打开完整案例原文，按 Unicode code point 坐标准确高亮；坐标无效明确提示，不做模糊定位。
- 运行中的成果标为中间成果。等待执行的案例不会显示为失败；终态保留完整、零结果、部分、失败、停止等覆盖结论。
- 最终交付校验 manifest 身份、文件摘要、字节数、JSONL 行数和案例句子索引。缺少清单时不宣称完整交付。

## 数据边界

后端只读 `skill_mining_jobs`、`dialogue_dimension_state` 与 `skill_mining_files`；
每次使用 repeatable-read 只读事务，单次状态与成果合计上限 64 MiB。
PG 为运行状态权威源，本机 OV 文件仅补充观测。不会提交模型任务、修改配置或改变运行中的任务。

本目录是独立查看器，不向主应用注册路由。当前真实 20 条任务为
`ovmine-6696be8bf22cec398abba63f9fe0bb65`，是否结束以页面实时状态为准。

## 验证

```bash
.venv/bin/python -m pytest "my-doc/泰康-对话打分优化/develop/S3/展示界面/tests" -q
node --test "my-doc/泰康-对话打分优化/develop/S3/展示界面/tests/frontend.test.cjs"
node --check "my-doc/泰康-对话打分优化/develop/S3/展示界面/frontend/app.js"
.venv/bin/ruff check --no-respect-gitignore "my-doc/泰康-对话打分优化/develop/S3/展示界面"
```

覆盖实时与终态视图选择、运行中覆盖状态、零 Evidence 完整交付、摘要篡改、
原文与清单身份冲突、诊断脱敏、运行接口字段白名单以及 emoji / 无效引用高亮。
