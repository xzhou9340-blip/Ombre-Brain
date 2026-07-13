# 更新日志 / Changelog

本项目版本号见根目录 `VERSION` 文件，Docker 镜像 tag 与之对应（`p0luz/ombre-brain:<VERSION>`）。

## Unreleased

### 新增 / Added

- 共读（read-along）接入：新增 5 个 MCP 工具 `reading_progress` / `reading_text` / `reading_search` / `reading_annotate` / `reading_annotations`（实现在 `src/tools/reading/`），包装同机部署的 read-along 后端的门禁与批注端点。未解锁章节连标题都取不到——防剧透门禁是 read-along 服务端硬约束，工具层只转译不绕过。
- 后端地址可用环境变量 `READING_API_BASE` 覆盖（默认 `http://127.0.0.1:18004`，本机直连、不走 nginx 认证层），每次调用现读，改配置即时生效。
- 新增 `deploy/read-along/`：VPS 部署脚本（clone 到 /opt/read-along + npm install + pm2 常驻 + DRY-RUN 推送）、nginx 片段模板（随机路径访问控制 + 可选 Basic Auth + `client_max_body_size 50m`）、Docker 网络覆盖示例与完整验收清单。

### 测试 / Tests

- 新增 `tests/test_reading_tools.py`：用进程内假 read-along 后端覆盖 5 个工具的 URL 拼接、门禁语义（未解锁内容绝不出现）、409/404 指引转译、回复署名 `ai`、连接失败排查文案。

## 2.4.10

### 新增 / Added

- GitHub 同步现在会在同一次 commit 中写入 `_ombre_backup_manifest.json`，记录备份生成时间、文件数、总字节数、每个 bucket markdown 的大小和 sha256。
- 从 GitHub 导入/恢复时会读取 manifest 摘要并返回给调用方，后续可用于恢复前校验和备份选择。

### 测试 / Tests

- 新增 `tests/test_github_backup_manifest.py` 覆盖 manifest 生成、同步写入和恢复读回。
- 更新 zero-commit 空仓库同步测试，确认首次提交也包含 manifest。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.10。

## 2.4.9

### 新增 / Added

- Dashboard 历史对话导入新增上传前预检：选中文件后先显示识别格式、轮次、分块数、预计 API 调用、文件大小、首个分块预览和警告，再由用户确认开始导入。
- 新增 `POST /api/import/preflight`，复用导入解析/分块逻辑做只读预检，不写 bucket、不启动后台任务。
- 新增 `preview_import()` 纯函数，便于后续把导入体验继续拆成更明确的预检查项。

### 测试 / Tests

- 新增 `tests/test_import_preflight.py` 覆盖导入预检纯函数和 API 路由。
- 新增 `tests/test_dashboard_import_preflight.py` 覆盖 Dashboard 预检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.9。

## 2.4.8

### 新增 / Added

- Dashboard 设置页新增“系统体检”面板，可一键查看数据目录、记忆桶统计、脱水/打标 LLM、向量化、GitHub 备份、访问控制和运行时状态。
- 新增 `GET /api/system/diagnostics` 只读接口，返回结构化 `ok` / `warning` / `error` 检查项；体检不主动请求外部 API，避免设置页被慢网络卡住。

### 测试 / Tests

- 新增 `tests/test_system_diagnostics.py` 覆盖诊断接口和缺配置告警。
- 新增 `tests/test_dashboard_diagnostics_panel.py` 覆盖 Dashboard 体检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.8。

## 2.4.7

### 修复 / Fixed

- 修复 GitHub 新建空仓库（Zero Commit，首页仍是 Quick setup）首次同步时报 `409 Conflict` 的问题。现在 Ombre 会在空仓库中创建初始 tree/commit，并创建 `refs/heads/<branch>`，无需用户先手动添加 README。
- 从空 GitHub 仓库导入时返回“暂无可导入文件”，不再把空仓库 409 当作异常。

### 测试 / Tests

- 新增 `tests/test_github_sync_zero_commit.py` 覆盖 zero-commit 仓库首次存档 bootstrap 流程。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.7。

## 2.4.6

### 优化 / Improved

- Dashboard 批量导入的 LLM 抽取结果解析改为宽松 JSON 清洗：支持 DeepSeek 等模型在 JSON 数组/对象前后附带说明文字，减少 `Import extraction JSON parse failed`。
- 抽出通用 `clean_llm_json()`，让导入解析与 grow/dehydrator 的 JSON 解析共用同一套 code fence/JSON 片段提取逻辑。

### 测试 / Tests

- 新增 `tests/test_import_extraction_json.py` 覆盖模型回复包含说明文字时的导入解析回归。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.6。

## 2.4.5

### 优化 / Improved

- 新增 LLM / embedding 请求超时配置：`dehydration.timeout_seconds`、`embedding.timeout_seconds`，以及环境变量 `OMBRE_COMPRESS_TIMEOUT_SECONDS`、`OMBRE_EMBED_TIMEOUT_SECONDS`。
- 写记忆时的脱水/打标、原生 Gemini、OpenAI 兼容 embedding 请求都会使用配置的超时时长，方便国内自托管服务器连接云端 API 较慢时调大等待时间。

### 测试 / Tests

- 新增 `tests/test_api_timeout_config.py` 覆盖 config/env 覆盖和运行时对象 timeout 传递。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.5。

## 2.4.4

### 修复 / Fixed

- 允许在 Dashboard 清空或修改 `AI_NAME`，避免关闭 OAuth 后仍显示旧的 AI 显示名；清空后回退为默认 `AI`。
- 统一桶元数据读取层的日期时间序列化，将 `created` / `last_active` 中的 `datetime` / `date` 归一化为 ISO 字符串，避免 `dream()`、Dashboard 首页和导入页面 JSON 序列化报错。
- 版本检查优先通过 GitHub Contents API 读取 `VERSION`，避免 raw CDN 在 push 后继续返回旧版本导致热更新检测不到新版本。

### 测试 / Tests

- 新增 `tests/test_env_config_identity.py` 覆盖 AI 显示名清空回归。
- 新增 `tests/test_datetime_metadata_normalization.py` 覆盖 YAML/frontmatter 时间戳被解析为 `datetime` 后的序列化回归。
- 新增 `tests/test_dashboard_update_source.py` 覆盖 Dashboard 版本检查的 GitHub API 优先顺序。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.4。

## 2.4.0

### 架构 / Architecture

- 将当前高级架构线统一作为对外发布版本 `2.4.0`。
- 保留内部 `src/ombrebrain/` 架构层命名：acceptance、eventsourcing、retrieval、microkernel、plugins、distributed 等模块继续作为内部深内核层存在。
- 保持 MCP tool names、bucket markdown、Dashboard existing routes、config/env 语义不变。

### 修复 / Fixed

- 修复 `tests/test_permanent_breath_regression.py` 中写死 Windows 路径分隔符的断言，改为 `os.sep`，避免 Linux / Docker / CI 下出现跨平台假失败。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.0。
- capability catalog 的 manifest version 改为读取项目版本，避免对外元数据继续暴露旧的架构草案版本号。

## 2.3.22

### 前端 / Frontend

- 写信表单「身份」下拉固定为 `user` / `AI`（对面是 AI 这点不必纠结具体模型名）；
  具体署名由用户在旁边的「署名」框自行填写。
- 写信表单的日期选择改造成拟态化「按钮」：点击主动唤起原生日期选择器（`showPicker()`
  + `focus/click` 兜底），选定后按钮显示所选日期；解决了原生小日历图标与提示文字重叠、
  以及透明输入框点击无响应的问题。
- 「服务日志」页右上角的日志文件路径只显示文件名（如 `server.log`），完整路径移到鼠标
  悬停提示，界面更干净、也不在页面上暴露本机绝对路径。

### 维护 / Chores

- VERSION + `src/VERSION` → 2.3.22。

## 2.3.21

### 新增 / Added

- **letter 署名支持自定义 AI 名称。** `letter_write` 的 `author` 不再限定
  `"user"`/`"claude"`，改为接受任意字符串署名：
  - `"user"` → 用户侧（`user_name` 逻辑不变）；
  - `"ai"`、等于 `ai_name` 的值、或历史遗留的 `"claude"` → 统一存为 `ai_name` 的值；
  - 其它任意字符串 → 原样作为署名。
  新增可选参数 `ai_name`（显式传入优先），默认取环境变量 `AI_NAME`，回退 `"AI"`。
  `letter_read` 原样返回存储的署名、不做转换；按 `author` 过滤时 `"ai"` 会同时
  命中新署名与历史 `"claude"` 信件。Dashboard 写信/筛选、SessionStart 钩子的「最近的信」
  同步适配。（`src/tools/plan/core.py`、`src/web/letters.py`、`src/web/hooks.py`、
  `src/server.py`、`frontend/dashboard.html`；回归测试 `tests/test_letter_author_regression.py`）
- 新增共享 helper `utils.get_ai_name()`：统一从环境变量 `AI_NAME` 读取 AI 显示名（回退 `"AI"`）。
- `.env.example` 新增 `AI_NAME=` 条目及说明。

### 变更 / Changed

- **全局去除面向用户文本与注释中的 "Claude" 硬编码。** 面向用户的文案（OAuth 授权页、
  Dashboard 删除确认/提示、配置项说明）改为中性的 "AI"；代码注释中的 "Claude" 统一改为
  "AI"/"LLM"。保留第三方服务/格式/文件的固有名（如 `Claude Desktop`、`claude.ai`、
  `claude_desktop_config.json`、Claude/ChatGPT 导出格式、Anthropic 模型 ID），以及 letter
  存储层对历史 `"claude"` 署名的向后兼容判断。

### 维护 / Chores

- 同步 bump `src/VERSION`（热更新读取的副本）与根 `VERSION` 至 2.3.21。

## 2.3.20

### 修复 / Fixed

- **`breath(importance_min=N)` 在高重要度桶塞满上限时，刚被 `trace` 降级的桶看似「未刷新」**
  之前 `breath(importance_min=N)` 把所有符合阈值的桶按 importance 降序排，直接截取前 20 条。当 `importance=10` 的桶超过 20 个时，一个刚用 `trace` 从 10 降到 9 的桶会被高分桶挤出列表，看起来像「trace 改了 importance 但 breath 没刷新」。
  现在改为先给每个符合阈值的 importance 档位（10、9…）各预留一条最近更新的桶，再按正常排序填满剩余名额，确保降级后的桶在其档位仍可见。
  （`src/tools/breath/importance.py` `_select_importance_buckets`；回归测试见 `tests/test_trace_importance_regression.py`）

  > 说明：`trace` 写入 importance 后，`breath` 是每次从磁盘实时重读、无缓存，本身不存在「需要额外操作触发刷新」。若 `trace` 降级看似无效，请先确认目标桶不是 `pinned`/`protected`——这类核心桶 importance 被锁定为 10，`trace` 会拒绝降级并返回提示，需先 `trace(bucket_id, pinned=0)` 再调整 importance。

### 维护 / Chores

- 修正 `.gitignore`：`docs/secrets/`（复数）此前未被忽略，补上规则，避免本地密钥/设计稿目录被纳入版本控制。
