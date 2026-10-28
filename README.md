# AgentHub · 一站式多 Agent 协作台

本地运行的一站式 AI Agent 管理面板：应用开关、API/供应商中台（兼容 CC Switch）、协作台消息总线、
总控室（编排模式 + 头脑风暴模式）。纯 Python 标准库 + 原生 HTML/JS，**零第三方依赖**。

---

## 功能

### 1. 应用面板
- 自动扫描 `agent` 快捷方式文件夹，反解真实 exe 路径
- 一键打开 / 关闭 / 查看运行状态 / 打开工作目录
- 关闭为强杀进程树；打开时自动复原**真实用户环境**（避免被宿主改写 USERPROFILE/APPDATA 导致应用建出空档案）

### 2. API 中台（兼容 CC Switch）
- 直接读写 CC Switch（SQLite）的供应商库：**新增 / 编辑 / 删除 / 切换**，效果与 CC Switch 一致
- 供应商写入前自动备份，涉及配置文件的写入先解析校验、不通过则拒绝
- **零计费探活**：`GET /v1/models` 验证可用性；对开放余额接口的厂商（DeepSeek / Moonshot / OpenRouter）顺带查询余额
- 密钥只在本机流转，前端永远看不到明文

### 3. 协作台（消息总线）
- 本地黑板式消息中心，任何能执行命令的 agent（Claude Code / Kimi / Codex / …）都能接入：
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8733/api/hub -Method Post -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes((@{from="Claude Code";text="你好"}|ConvertTo-Json)))
  ```
- 事件驱动监视器（`monitor.py`）：长轮询，新消息立刻拉起 agent 自动回复并写回

### 4. 总控室
两种协作模式，可随时切换：

**合作模式**：总控 agent 拆解任务 → 并行/串行分发到各执行槽（CLI）→ 汇总
- 每个槽位显示**实际在用的 API 供应商**（claude/codex 系直接读 CC Switch 当前供应商）
- 项目制：每个项目独立工作空间，每次编排完整留痕（`runs/<项目>/<时间戳>.json`）
- 每个槽位可配**思考强度**（claude/codebuddy 有 `--effort`，codex 用 `model_reasoning_effort`）

**头脑风暴模式**：多 agent 对抗式收敛
- N 位参与者（2–6）× N 轮，每轮：独立提案 → 中立评审+自改 → 收敛
- 总结人每轮写 `_summary.md`，最后出 `final.md`
- 每人独立工作目录，材料目录以 junction 只读挂载
- 产物按轮归档，全程留痕

---

## 快速开始

```bat
start.bat                     :: 启动面板（自动打开 http://127.0.0.1:8733/）
start_monitor.bat             :: 启动协作台监视器（事件驱动）
```

要求：Windows + Python 3.10+（仅标准库）。

## 接入的 CLI

在总控室/头脑风暴中可作为槽位的 CLI（安装各自官方命令行工具后自动识别）：
Claude Code · Codex · CodeBuddy（腾讯）· Kimi（月之暗面）· TraeCode（字节）· Qwen Code（阿里）· DeepSeek Harness · 桩（本地测试）

## 安全说明

- 服务仅监听 `127.0.0.1`
- API Key 只写入本机（CC Switch 库或 CLI 配置），前端永不回显明文
- 写敏感配置前自动备份到 `backups/`（请勿提交该目录，见 `.gitignore`）

## License

MIT