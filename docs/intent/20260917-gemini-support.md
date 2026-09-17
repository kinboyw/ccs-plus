# 添加 Gemini 支持
最后修改时间: 2026-09-17 15:37:05

Flow mode: standard / 标准模式

Stage: Intent / 意图

Review status: Accepted

## Background

cc-switch 数据库中已有 `app_type = "gemini"` 的 provider。`ccsp provider show <name>` 会按名称返回所有匹配记录；当同名 provider 同时存在 Codex 和 Gemini 记录时，旧代码无法将 Gemini 行解析为 `AppKind`，导致 `show` 崩溃。

Gemini 的实际 `settings_config` 不是 Codex 的 `auth + config(TOML)` 结构，而是包含 `env` 与空 `config` 对象。其中 `env` 使用 `GOOGLE_GEMINI_BASE_URL`、`GEMINI_API_KEY` 和 `GEMINI_MODEL`。

## Goal

- 将 Gemini 纳入 `AppKind` 与 provider 仓储解析，使同名 provider 的 `show` 能完整输出所有应用记录。
- 基于 Gemini 的实际 cc-switch 配置结构显示、创建、导入、导出、删除和重置 provider。
- 支持通过 `ccsp launch gemini` 和 `ccsp run m<N>` 启动本机 Gemini CLI。
- 使用 Gemini CLI 原生环境变量和参数启动，不复用 Codex profile 或受管 TOML 配置逻辑。
- 在 CLI、TUI、设置和测试中保持 Gemini 的应用身份与权限模型一致。

## Non-goal

- 不实现 API 代理、协议转换或第三方 endpoint 的连通性验证。
- 不将 Gemini provider 伪装为 Codex provider，或调用 `ensure_managed_config`。
- 不替用户预写 Gemini CLI 的首次认证选择或代为同意其条款。
- 不实现 Gemini 历史 session 的发现与 TUI 恢复选择。

## User scenarios

1. `sub2api` 同时配置为 Codex 和 Gemini provider 时，`ccsp provider show sub2api --show-secret` 输出两条记录，不再因 Gemini 枚举缺失而崩溃。
2. 用户使用 `ccsp provider list --app gemini` 查看 Gemini provider，并用 `ccsp run m2` 启动指定记录。
3. 自定义 Gemini provider 启动时，CLI 从隔离的 `GEMINI_CLI_HOME` 读取状态，并接收该 provider 的 base URL、API key 和 model。
4. 首次使用隔离目录时，Gemini CLI 发现已有 API key 后由用户选择 `Gemini API Key`；选择类型持久化，但 provider API key 不写入该目录。

## Acceptance

- `AppKind` 可解析数据库中的 `gemini`，同名跨应用 provider 查询不崩溃也不静默过滤 Gemini。
- 新建 Gemini provider 的 `settings_config` 与观察到的 cc-switch shape 一致：`env` 含三个 Gemini 变量，`config` 为空对象。
- Gemini 启动不生成或引用 Codex/Grok profile；它清除继承的 Gemini provider 环境变量、设置 `GEMINI_CLI_HOME`，并通过 `--model` 和 `--approval-mode` 调用原生 CLI。
- `run m<N>`、`launch gemini`、provider 管理命令及 TUI 可识别 Gemini。
- 相关单元测试、格式检查与静态检查通过；真实 endpoint 请求的结果不作为本任务验收条件。

## Open questions

- Gemini CLI session 文件格式尚未研究，因此 TUI 暂不能列出或选择历史 Gemini 会话。
- 第三方 Gemini gateway 是否兼容本机 Gemini CLI 的请求协议只能通过实际请求验证，cc-switch 中保存的 endpoint 不足以证明兼容性。
- Gemini CLI 的配置 schema 和认证行为随 CLI 版本演进；升级后需要重新确认环境变量和首次认证流程。

## Decisions

- `provider show` 保留“按名称列出所有 app 匹配项”的语义；Gemini 是实际同名记录，而不是被无关查询带入。
- 使用独立 `GeminiProviderAdapter` 和 `GeminiLauncher`，仅处理已经观察到的 Gemini env-based shape。
- 使用 `data/gemini` 作为受管 `GEMINI_CLI_HOME` 根目录，避免自定义 provider 的状态、认证选择和会话与用户级 `~/.gemini` 混用。
- 将 Gemini CLI 的 API-key 认证选择保留为首次交互，由 CLI 在 `security.auth.selectedType` 中持久化；ccsp 每次启动从 provider 记录注入 API key。

## Risk

- 共用 Gemini state home 会共享 CLI 认证类型和本地会话元数据；不同 provider 的 API key 仍仅通过每次启动的进程环境传递。
- API key 与 endpoint 出现在子进程环境中，这是 Gemini CLI 的原生集成方式；不得将其写入命令行参数、日志或 SpecFlow 文档。
- 未进行真实 API 请求，启动成功只证明本地 CLI 能读取配置、完成首次认证选择并接受启动参数。
