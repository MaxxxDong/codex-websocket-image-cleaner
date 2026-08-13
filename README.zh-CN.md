# Codex WebSocket Image Cleaner 中文说明

**用于修复 Codex 因图片 payload 撑爆 session 而出现的 `Reconnecting...`、WebSocket 断流、stream disconnected、上下文爆炸等问题。**

Codex WebSocket Image Cleaner 是一个备份优先的 Codex skill 和本地修复脚本。它专门处理 Codex Desktop / Codex CLI 的某一类隐藏问题：会话的 `rollout-*.jsonl` 文件因为内嵌 base64 图片而变得巨大，导致线程打开慢、重连、compact 失败、发送失败或模型上下文溢出。

典型症状包括：

```text
Reconnecting... 1/5
stream disconnected before completion
failed to send websocket request
Codex ran out of room in the model's context window
Invalid image in your last message. Please remove it and try again.
```

很多人遇到这类问题时，会先排查 VPN、代理、TUN 模式、DNS、WebSocket 或 SSE fallback。这些确实可能是原因。但还有一种很容易被忽略的情况：**不是网络先坏了，而是当前 session 太大，尤其是图片 payload 太大。**

英文默认文档：[README.md](README.md)

## 为什么会这样

Codex 会把会话历史保存为本地 JSONL rollout 文件，通常位于：

```text
~/.codex/sessions
```

如果线程里频繁使用截图、图片生成、图片编辑、视觉 QA、`view_image` 等能力，rollout 里可能出现大量内嵌图片数据：

```text
data:image/png;base64,...
input_image
image_url
```

这些图片不是几行普通日志，而可能是几 MB 一行的 base64。几个图片就能让 session 从几 MB 膨胀到几十甚至上百 MB。

## 外部信号

公开反馈里能看到两类问题重叠在一起。

### WebSocket / 代理导致的重连

GitHub issue [`openai/codex#19821`](https://github.com/openai/codex/issues/19821) 提到，代理环境下 Codex 可能先尝试 WebSocket，失败后出现 `Reconnecting... 1/5` 到 `5/5`，再 fallback 到 HTTP/SSE。这是传输层问题。

### rollout 文件过大和内嵌图片

GitHub issue [`openai/codex#22004`](https://github.com/openai/codex/issues/22004) 提到 Codex Desktop 在加载很大的 rollout JSONL 时崩溃，并指出图片以 base64 形式直接嵌在 rollout 里。这个问题和图片工作流高度相关。

### 大上下文、图片和上传/compact 失败

Reddit 上也有用户把 reconnect、stream disconnected、compact 失败和大上下文、大文件、图片联系起来：

- https://www.reddit.com/r/codex/comments/1t18wtj/codex_desktop_stream_disconnected_before/
- https://www.reddit.com/r/codex/comments/1svdy5h/codex_error_running_remote_compact_task_stream/
- https://www.reddit.com/r/codex/comments/1tkban6/bug/

X/Twitter 上更多是性能和稳定性讨论，可复现细节不如 GitHub issue 和本地 rollout 证据稳定。

## 这个工具做什么

它扫描目标 rollout JSONL 中的图片标记：

- `data:image/`
- `input_image`
- `image_url`

执行清理时会：

- 根据 thread id、`codex://threads/...` 或显式路径定位 rollout
- 先完整备份原文件
- 写入 `restore.ps1` 回滚脚本
- 将包含图片 payload 的 function/tool output 转成文本占位
- 移除 message 里的 `input_image`
- 清理残留的 `<image>` 文本 wrapper
- 清空 event 里的 images 数组
- 对所有 `type: "compacted"` 行逐字节原样保留
- 校验每一行都是合法 JSON
- 校验普通记录中没有真实图片 payload 后才覆盖原文件
- 校验 compact 行的有序 SHA-256 完全不变

原始备份不会被清理命令删除。

`compacted` 是 Codex 重启后恢复上下文的内部检查点，不是普通历史记录。它可能仍包含图片标记和图片字节；工具会报告这些保留内容，但不会修改。直接递归清理 `compacted.payload.replacement_history` 会让重启 hydration 回退到完整历史，并可能触发 `context_window_exceeded`。

## 它不做什么

它不是通用 Codex 加速器，也不是网络代理修复工具。

它不会：

- 修复所有 WebSocket 重连问题
- 配置 VPN、DNS、TUN 或代理环境变量
- 总结旧文本上下文
- 删除普通文字对话
- 修改 SQLite 数据库
- 删除磁盘上的生成图片文件
- 保证超长纯文本线程一定能塞进模型上下文

如果一个新开的短线程也一直 reconnect，那更像网络/代理/WebSocket 问题。如果一个纯文本老线程上下文爆了，应该用 `/clear`、`/compact` 或新开 handoff 线程。

## 使用方式

只报告，不修改：

```powershell
python scripts/imageclean.py --thread 019e4ae7-0621-7443-9181-8fcddf82995a
```

执行清理：

```powershell
python scripts/imageclean.py --thread 019e4ae7-0621-7443-9181-8fcddf82995a --apply
```

使用 Codex thread URL：

```powershell
python scripts/imageclean.py --thread codex://threads/019e4ae7-0621-7443-9181-8fcddf82995a --apply
```

显式指定 rollout 文件：

```powershell
python scripts/imageclean.py --path C:\Users\you\.codex\sessions\2026\05\21\rollout-....jsonl --apply
```

## 安装为 Codex Skill

```powershell
git clone https://github.com/MaxxxDong/codex-websocket-image-cleaner.git
Copy-Item -Recurse .\codex-websocket-image-cleaner "$env:USERPROFILE\.codex\skills\codex-websocket-image-cleaner"
```

安装新 skill 后，建议重启 Codex Desktop。

## 回滚

每次 `--apply` 都会创建备份目录：

```text
C:\Users\<user>\Documents\Codex\codex-backups\rollout-session-imageclean-YYYYMMDD-HHMMSS-<thread>
```

回滚：

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\<user>\Documents\Codex\codex-backups\<backup>\restore.ps1
```

## 真实清理案例

| 场景 | 清理前 | 清理后 | 主要原因 |
|---|---:|---:|---|
| 图片密集线程 | 130 MB | 4 MB | 内嵌 base64 图片 |
| 反复图片检查 | 119 MB | 18 MB | 79 个图片输出 |
| 视觉工作流线程 | 92 MB | 19 MB | 多轮图片追加 |
| 嵌套历史 payload | 72 MB | 12 MB | 历史记录里仍有图片 |
| 较小图片线程 | 25 MB | 0.9 MB | 9 个图片输出 |

## 推荐排查流程

遇到 Codex reconnect 或 stream disconnected，建议按这个顺序判断：

1. 看是所有 thread 都有问题，还是只有某个旧 thread 有问题。
2. 查目标 rollout 文件大小。
3. 统计 `data:image/`、`input_image`、`image_url`。
4. 如果图片占比很高，先用这个工具清理。
5. 如果没有图片但 compact/context 错误很多，用 `/clear`、`/compact` 或新线程 handoff。
6. 如果新短线程也 reconnect，再查 WebSocket、VPN、DNS、TUN、代理或服务端状态。

## 风险和限制

- 旧聊天里的可视图片内容会被移除，文字历史会保留。
- 如果需要在旧 thread 里继续查看那些图片，请先导出或归档。
- 普通记录中的嵌套图片 payload 会按 JSON 结构递归清理；compact 检查点永远不递归改写。
- 继续在同一个 thread 里处理图片，后面还可能再次膨胀。
- 工具依赖当前 Codex rollout JSONL 格式；Codex 格式变化后需要重新验证。
- 清理后先保留备份，确认 thread 正常后再考虑删除。
- 因为 compact 检查点必须保留，安全清理后的文件可能仍明显大于理论上的“全 marker 归零”大小。

## 当前版本

**v1.1.0**，更新记录见 [CHANGELOG.md](CHANGELOG.md)。
