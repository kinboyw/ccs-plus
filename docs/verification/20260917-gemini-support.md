# Gemini 支持验证
最后修改时间: 2026-09-17 15:38:55

Flow mode: standard / 标准模式

Stage: Verification / 验证

Review status: Draft

## Intent alignment

实现覆盖已接受意图中的 provider 解析、Gemini env schema、原生 CLI 启动、`m<N>` 快捷方式、CLI/TUI 应用识别和测试要求。

`provider show` 仍按名称返回跨应用的全部匹配项；Gemini 记录被解析并显示，而非被过滤。Gemini 启动采用独立 adapter/launcher，未调用 Codex 或 Grok 的 `ensure_managed_config`。

## Spec alignment

不适用。本任务按 standard / 标准模式记录，未单独创建 Spec。

## Plan alignment

不适用。产品实现先于 SpecFlow 记录完成；用户要求直接完成验证，因此未创建独立 Plan。

## Actual diff summary

- `domain.py` 新增 `AppKind.GEMINI`、显示信息和 `GeminiRuntime`。
- `adapters.py` 使用独立 Gemini adapter，读写观察到的 cc-switch `{"env": {...}, "config": {}}` shape。
- `launcher.py` 使用 `GEMINI_CLI_HOME` 隔离状态，清理继承的 Gemini provider 环境变量，注入当前 provider env，并传递 `--model`、`--approval-mode` 和可选 `--resume`。
- `cli.py` 增加 `m<N>`、`launch gemini` 与 Gemini app 选择；`tui.py` 增加 Gemini 标签和 approval-mode presets。
- `settings.py`、`settings.yaml` 新增 Gemini state home 和默认 approval mode；README 更新受支持应用与命令示例。
- 测试覆盖 provider schema、跨应用同名查询、`m1` 启动选择、启动环境、设置和枚举。

## Expected vs actual changed files

预期变更：provider domain/adapter/launcher、CLI/TUI、设置、用户文档和相关测试。

实际变更：`README.md`、`settings.yaml`、`src/ccs_plus/{adapters,cli,domain,launcher,sessions,settings,tui}.py`、`tests/{conftest,test_adapters,test_cli,test_database,test_launcher,test_settings}.py`，以及本任务的 Intent/Verification 文档。与意图一致。

## Acceptance criteria

- [x] `AppKind` 可解析 `gemini`，跨应用同名查询不崩溃且不静默过滤。
- [x] 新建 Gemini provider 使用 `env` 三个 Gemini 变量和空 `config` 对象。
- [x] Gemini 启动不依赖 Codex/Grok profile，使用原生环境变量、`GEMINI_CLI_HOME`、`--model` 和 `--approval-mode`。
- [x] `run m<N>`、`launch gemini`、provider 管理选择和 TUI 可识别 Gemini。
- [x] 格式、lint、类型检查、完整测试集和 `provider show` 本地冒烟检查通过。
- [x] 真实 endpoint 请求未作为验收条件，符合 Intent 的 Non-goal。

## Test results

在 `D:\SourceCodes\mywork\ccs-plus` 执行：

- `uv run --locked --no-sync ruff format --check src tests`：通过，26 个文件已格式化。
- `uv run --locked --no-sync ruff check src tests`：通过。
- `uv run --locked --no-sync mypy src`：通过，14 个源文件无问题。
- `uv run --locked --no-sync pytest tests`：通过，210 passed、1 skipped。
- `uv run --locked --no-sync ccsp provider show sub2api --show-secret`：通过；输出同时含 Codex 与 Gemini 的 `sub2api` 记录。命令输出未记录密钥。
- 用户手工执行 `uv run ccsp run m2`：Gemini CLI 检测到注入的 API key，并完成首次 `Gemini API Key` 认证类型选择。未将该交互视为 API 连通性测试。
- `git diff --check` 与 `git diff --cached --check`：通过。

## Missed or expanded scope

- 未偏离意图中的产品范围。
- 新增 `GeminiSessionReader` 仅返回空列表，使既有 session-reader 调用点能够处理 Gemini；它不提供 Gemini 历史会话发现或 TUI 恢复，该限制已在 Intent 中声明。
- Gemini CLI 首次认证选择是隔离 home 的原生行为；ccsp 不预写认证类型或替用户接受条款。

## Risks

- 未验证第三方 Gemini gateway 的真实 API 兼容性、模型可用性和响应语义。
- Gemini CLI 升级可能改变配置 schema、认证流程或环境变量支持，需要重新验证。
- Gemini state home 当前在 provider 间共享认证类型和会话元数据；每次启动仍只从当前 provider 注入 API key。

## Incomplete items

- 非范围内：Gemini 历史 session 的读取、列出和恢复选择。
- 非范围内：向第三方 endpoint 发送真实请求以验证网关兼容性。

## Conclusion

自动化质量检查、同名 provider 查询和本地 Gemini CLI 首次启动路径均通过。实现满足 Intent 中可自动或本地验证的验收项；外部 endpoint 连通性与 Gemini session discovery 保留为后续工作。
