# ccs-plus

<p align="center">
  <strong>cc-switch provider manager · multi-agent CLI launcher · fullscreen TUI</strong><br/>
  <sub>Claude · Codex · Grok · OpenCode — one command, one pane, zero context switching</sub>
</p>

<p align="center">
  <img src="docs/assets/tui-launcher.png" alt="ccs-plus interactive launcher TUI" width="920"/>
</p>

<p align="center">
  <sub>Interactive launcher — app badges, provider picker, directory-scoped sessions, per-app permission presets</sub>
</p>

---

`ccs-plus` 管理 [cc-switch](https://github.com/farion1231/cc-switch) SQLite 中的 Claude、Codex、Grok、OpenCode provider，并用选定 provider 启动对应原生 CLI。只做 provider 管理、运行配置和启动，不实现 GUI、本地代理或请求转换。

## 安装

从 [GitHub Release](https://github.com/leoninew/ccs-plus/releases) 下载二进制（推荐）或 `.whl`。开发安装用 `make deps && make install`。本机还需要已安装的 CLI（`claude` / `codex` / `grok` / `opencode`）和可访问的 cc-switch 数据库。`ccs-plus` 与 `ccsp` 是同一入口。

```bash
uv tool install ./ccs_plus-x.y.z-py3-none-any.whl   # wheel，需 Python 3.12+
cp .env.example .env                                 # 填入 Fernet key
```

| 安装方式 | `settings.yaml` / `.env` |
| --- | --- |
| 二进制 | exe 同目录；没有文件时使用内嵌默认配置 |
| wheel | 当前工作目录 |
| 源码 | 仓库根目录 |

## 使用

无参进入 TUI。二进制把下面的 `ccsp` 换成 `ccs-plus`。完整参数看 `ccsp --help`；`provider` / `launch` / `run` 可简写为 `p` / `l` / `r`。

### TUI

| 操作 | 按键 |
| --- | --- |
| 切换面板 | `tab` / `s-tab`：`app → sessions → provider → permissions → buttons` |
| 列表移动 | `↑↓`、`j/k`、鼠标滚轮 |
| 点选 | 鼠标点击 |
| 过滤 provider / sessions | `/` 后输入 |
| 会话范围 this dir ↔ all | sessions 面板焦点下按 `a` |
| Launch ↔ Cancel | 按钮焦点下 `←`/`→` |
| 启动 / 取消 | `Ctrl+Enter`（直接 Launch）· `enter`（逐步导航 / 按钮 Launch）· `esc` |
| 数字跳转 | `1`–`9` |

顶栏 cwd 是启动时的当前目录，TUI 内不能改。Resume 使用会话自身的 cwd；sessions 面板按 `a` 可在「当前目录 / 全部项目」之间切换。

### 启动

```bash
ccsp
ccsp run x2
ccsp run o1
ccsp launch codex --provider "<provider-name>"
ccsp launch opencode --provider "<provider-name>" --cwd "<directory>"
```

`run` 使用 `provider list` 里的编号（`c1` / `x2` / `g1` / `o1`）。`launch --cwd` 指定工作目录；`--model` 和 `--effort` 只覆盖本次启动。

### 管理 provider

```bash
ccsp provider list
ccsp provider list --app claude
ccsp provider show "<provider-name>"
ccsp provider add claude --name "<provider-name>" --endpoint "https://api.example.com/v1" --api-key "<api-key>" --model "<model-id>"
ccsp provider add opencode --name "<provider-name>" --endpoint "https://api.example.com/v1" --api-key "<api-key>" --model "provider-id/model-id"
```

`list` 的 Alias 按 app 分组编号。`show` 会打印可复用的 `provider add` 命令，默认把 API Key 打成 `xxxx`；只有在可信终端才用 `--show-secret`。同一 app 内名称唯一，大小写不敏感。

### 备份与清理

```bash
ccsp provider export
ccsp provider import "data/providers-all-<timestamp>.json"
ccsp provider reset
ccsp provider delete claude "<provider-name>" --yes
```

`export` / `import` / `reset` 省略 app 时覆盖 Claude、Codex、Grok、OpenCode；后面跟 app 名则只处理一个。未指定路径时，备份写到 `data/providers-all-<timestamp>.json`。恢复必须使用生成备份时的同一个 `encryption_key`。`reset` 默认只预览，加 `--yes` 才删除非官方 provider。

## 开发

`make check` / `make test`。推送 `v*` tag 会先发 wheel，再把各平台二进制挂到同一 Release。
