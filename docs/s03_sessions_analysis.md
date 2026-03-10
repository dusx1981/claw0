# S03 Sessions 设计文档：会话与上下文保护

## 一、设计思想

### 1.1 核心理念

> "会话是 JSONL 文件。写入时追加，读取时重放。过大时进行摘要压缩。"

s03_sessions 在 s02_tool_use 的基础上，引入了两个关键机制：

1. **SessionStore** - 会话持久化层
   - 采用 JSONL（JSON Lines）格式存储对话历史
   - 写入时追加（append-only），保证数据完整性
   - 读取时重放（replay），完整恢复对话上下文

2. **ContextGuard** - 上下文溢出保护层
   - 三阶段重试机制应对上下文窗口限制
   - 智能压缩策略保留关键信息

### 1.2 架构意图

```
┌─────────────────────────────────────────────────────────────┐
│                        用户交互层                            │
│                     (REPL 命令处理)                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    SessionStore (持久化)                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ sessions.json│  │ xxx.jsonl   │  │ 消息重建逻辑        │  │
│  │ (索引文件)   │  │ (对话记录)  │  │ _rebuild_history()  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   ContextGuard (溢出保护)                    │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ 第0次尝试 │→│ 截断工具结果 │→│ LLM摘要压缩历史(50%)  │  │
│  │ 正常调用  │  │              │  │                       │  │
│  └──────────┘  └──────────────┘  └───────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Agent 循环核心                          │
│              (继承自 s02_tool_use)                           │
│     while True + stop_reason + 工具调度表                    │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 设计原则

1. **数据优先** - 会话数据先写入磁盘，再进行 API 调用
2. **渐进降级** - 上下文溢出时，依次尝试截断、压缩，最后才失败
3. **用户可控** - 提供丰富的 REPL 命令管理会话和上下文

---

## 二、实现机制

### 2.1 SessionStore - JSONL 会话持久化

#### 2.1.1 存储结构

```
workspace/.sessions/agents/{agent_id}/
├── sessions.json          # 会话索引
└── sessions/
    ├── abc123def456.jsonl # 会话文件（UUID 前12位命名）
    ├── xyz789ghi012.jsonl
    └── ...
```

**索引文件 (sessions.json)** 结构：
```json
{
  "abc123def456": {
    "label": "初始会话",
    "created_at": "2024-01-15T10:30:00+00:00",
    "last_active": "2024-01-15T14:22:00+00:00",
    "message_count": 45
  }
}
```

#### 2.1.2 JSONL 记录格式

每条记录是一个独立的 JSON 对象，支持以下类型：

```json
{"type": "user", "content": "你好", "ts": 1705312200.0}
{"type": "assistant", "content": [{"type": "text", "text": "你好！"}], "ts": 1705312201.5}
{"type": "tool_use", "tool_use_id": "call_001", "name": "read_file", "input": {"file_path": "test.txt"}, "ts": 1705312202.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "文件内容...", "ts": 1705312202.5}
```

#### 2.1.3 消息重建逻辑

`_rebuild_history()` 方法将 JSONL 记录重建为 API 格式的消息列表：

```python
def _rebuild_history(self, path: Path) -> list[dict]:
    """
    Anthropic API 规则决定了重建方式:
      - 消息必须 user/assistant 交替
      - tool_use 块属于 assistant 消息
      - tool_result 块属于 user 消息
    """
```

**关键重建规则**：

1. **tool_use 处理**：将连续的 tool_use 合并到同一个 assistant 消息
2. **tool_result 处理**：将连续的 tool_result 合并到同一个 user 消息
3. **内容格式转换**：将字符串内容转换为 API 要求的块格式

### 2.2 ContextGuard - 三阶段溢出保护

#### 2.2.1 Token 估算

```python
@staticmethod
def estimate_tokens(text: str) -> int:
    return len(text) // 4  # 简单估算：4字符 ≈ 1 token
```

配置常量：
- `CONTEXT_SAFE_LIMIT = 180000` - 安全上下文限制
- `MAX_TOOL_OUTPUT = 50000` - 单次工具输出最大字符数

#### 2.2.2 三阶段重试流程

```
┌─────────────────────────────────────────────────────────────┐
│                      guard_api_call()                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐                                             │
│  │ 第 0 次尝试  │──成功──→ 返回结果                          │
│  │ 正常调用     │                                             │
│  └─────────────┘                                             │
│         │                                                    │
│      溢出错误                                                 │
│         ▼                                                    │
│  ┌─────────────────────────────┐                             │
│  │ 第 1 次尝试                  │──成功──→ 返回结果           │
│  │ _truncate_large_tool_results │                             │
│  │ (截断过大的工具结果)          │                             │
│  └─────────────────────────────┘                             │
│         │                                                    │
│      仍然溢出                                                 │
│         ▼                                                    │
│  ┌─────────────────────────────┐                             │
│  │ 第 2 次尝试                  │──成功──→ 返回结果           │
│  │ compact_history()            │                             │
│  │ (LLM摘要压缩前50%历史)        │                             │
│  └─────────────────────────────┘                             │
│         │                                                    │
│      仍然溢出                                                 │
│         ▼                                                    │
│      抛出异常                                                 │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

#### 2.2.3 工具结果截断策略

```python
def truncate_tool_result(self, result: str, max_fraction: float = 0.3) -> str:
    """在换行边界处只保留头部进行截断。"""
    max_chars = int(self.max_tokens * 4 * max_fraction)
    if len(result) <= max_chars:
        return result
    cut = result.rfind("\n", 0, max_chars)  # 找最后一个换行符
    if cut <= 0:
        cut = max_chars
    head = result[:cut]
    return head + f"\n\n[... truncated ({len(result)} chars total, showing first {len(head)}) ...]"
```

**截断原则**：在换行边界处截断，保持内容可读性。

#### 2.2.4 历史压缩策略

```python
def compact_history(self, messages: list[dict], 
                    api_client: OpenAI, model: str) -> list[dict]:
    """
    将前 50% 的消息压缩为 LLM 生成的摘要。
    保留最后 N 条消息 (N = max(4, 总数的 20%)) 不变。
    """
```

**压缩比例**：
- 压缩前 50% 消息为摘要
- 保留后 20%（最少4条）消息不变

**压缩后格式**：
```python
compacted = [
    {
        "role": "user",
        "content": "[Previous conversation summary]\n" + summary_text,
    },
    {
        "role": "assistant", 
        "content": [{"type": "text", "text": "Understood, I have the context..."}],
    },
]
compacted.extend(recent_messages)
```

### 2.3 REPL 命令系统

| 命令 | 功能 | 示例 |
|------|------|------|
| `/new [label]` | 创建新会话 | `/new 项目讨论` |
| `/list` | 列出所有会话 | `/list` |
| `/switch <id>` | 切换会话（支持前缀匹配） | `/switch abc123` |
| `/context` | 显示上下文使用情况 | `/context` |
| `/compact` | 手动压缩历史 | `/compact` |
| `/help` | 显示帮助 | `/help` |
| `quit` / `exit` | 退出程序 | `quit` |

---

## 三、主要功能

### 3.1 会话管理功能

#### 3.1.1 创建会话
```python
sid = store.create_session("标签名称")
# 生成 UUID 前12位作为会话ID
# 创建空的 JSONL 文件
# 更新索引文件
```

#### 3.1.2 加载会话
```python
messages = store.load_session(session_id)
# 从 JSONL 文件读取记录
# 重建为 API 格式的消息列表
# 设置为当前会话
```

#### 3.1.3 保存对话轮次
```python
# 保存用户/助手消息
store.save_turn("user", "你好")

# 保存工具调用及结果
store.save_tool_result(
    tool_use_id="call_001",
    name="read_file",
    tool_input={"file_path": "test.txt"},
    result="文件内容..."
)
```

#### 3.1.4 会话列表
```python
sessions = store.list_sessions()
# 按最后活跃时间倒序排列
# 返回 [(session_id, metadata), ...]
```

### 3.2 上下文保护功能

#### 3.2.1 自动保护
在 `guard_api_call()` 中自动处理溢出：

```python
response = guard.guard_api_call(
    api_client=client,
    model=MODEL_ID,
    system=SYSTEM_PROMPT,
    messages=messages,
    tools=TOOLS,
)
```

#### 3.2.2 手动压缩
```python
# 用户可通过 /compact 命令手动触发
new_messages = guard.compact_history(messages, client, MODEL_ID)
```

#### 3.2.3 上下文监控
```python
# /context 命令显示使用情况
estimated = guard.estimate_messages_tokens(messages)
pct = (estimated / guard.max_tokens) * 100
# 可视化进度条：[######----------] 35.2%
```

### 3.3 工具功能

本节工具集（相比 s02 有所简化）：

| 工具 | 功能 | 参数 |
|------|------|------|
| `read_file` | 读取文件 | `file_path` |
| `list_directory` | 列出目录内容 | `directory` (可选) |
| `get_current_time` | 获取当前 UTC 时间 | 无 |

**安全机制**：所有文件操作限制在 `WORKSPACE_DIR` 内，防止路径穿越攻击。

---

## 四、与前面章节的演进关系

### 4.1 代码行数演进

```
s01_agent_loop:   ~170 行  ──→  基础循环
s02_tool_use:     ~450 行  ──→  +工具系统
s03_sessions:     ~910 行  ──→  +会话持久化 + 上下文保护
```

### 4.2 功能演进对比

| 特性 | s01 | s02 | s03 |
|------|-----|-----|-----|
| Agent 循环 | ✅ | ✅ | ✅ |
| 工具调用 | ❌ | ✅ | ✅ |
| 会话持久化 | ❌ | ❌ | ✅ |
| 多会话管理 | ❌ | ❌ | ✅ |
| 上下文保护 | ❌ | ❌ | ✅ |
| REPL 命令 | 仅 quit | 仅 quit | 完整命令集 |
| 程序崩溃恢复 | ❌ | ❌ | ✅ |

### 4.3 核心代码继承

#### Agent 循环结构（继承自 s01/s02）

```python
# s01 基础结构
while True:
    user_input = input()
    messages.append({"role": "user", "content": user_input})
    response = client.chat.completions.create(...)
    # 处理响应

# s02 增加工具循环
while True:
    # ...获取用户输入...
    while True:  # 内层：处理工具调用链
        response = client.chat.completions.create(tools=TOOLS, ...)
        if finish_reason == "stop":
            break
        elif finish_reason == "tool_calls":
            # 执行工具，继续循环
            continue

# s03 增加持久化和保护
store = SessionStore()
guard = ContextGuard()
messages = store.load_session(sid)  # 恢复历史

while True:
    # ...REPL 命令处理...
    store.save_turn("user", user_input)  # 持久化
    
    while True:
        response = guard.guard_api_call(...)  # 保护层包装
        # ...工具调用处理...
        store.save_tool_result(...)  # 持久化
```

### 4.4 新增组件详解

```
s03 新增代码分布：

SessionStore 类 (~170 行)
├── __init__, _load_index, _save_index      # 索引管理
├── create_session, load_session            # 会话生命周期
├── save_turn, save_tool_result             # 写入操作
├── append_transcript                        # 底层写入
├── _rebuild_history                         # 消息重建（核心）
└── list_sessions                            # 列表查询

ContextGuard 类 (~190 行)
├── __init__, estimate_tokens               # 配置与估算
├── estimate_messages_tokens                # 消息 token 计算
├── truncate_tool_result                    # 截断策略
├── compact_history                         # 压缩策略（核心）
├── _truncate_large_tool_results            # 批量截断
└── guard_api_call                          # 三阶段重试（核心）

REPL 命令处理 (~90 行)
└── handle_repl_command                     # 命令分发

辅助函数
├── _serialize_messages_for_summary         # 消息序列化
└── safe_path                               # 路径安全检查
```

---

## 五、代码结构图

### 5.1 文件整体结构

```
s03_sessions.py (912 行)
│
├── 导入与配置 (第1-68 行)
│   ├── 标准库导入
│   ├── 第三方库导入 (OpenAI, dotenv)
│   ├── 环境配置 (MODEL_ID, client)
│   └── 常量定义 (CONTEXT_SAFE_LIMIT, MAX_TOOL_OUTPUT)
│
├── ANSI 颜色工具 (第69-104 行)
│   └── 打印辅助函数
│
├── 安全辅助函数 (第105-116 行)
│   └── safe_path()
│
├── SessionStore 类 (第117-295 行)
│   ├── 索引管理方法
│   ├── 会话生命周期方法
│   ├── 写入方法
│   ├── 消息重建方法
│   └── 查询方法
│
├── 消息序列化 (第296-323 行)
│   └── _serialize_messages_for_summary()
│
├── ContextGuard 类 (第324-524 行)
│   ├── Token 估算方法
│   ├── 截断方法
│   ├── 压缩方法
│   └── 保护包装方法
│
├── 工具实现 (第525-645 行)
│   ├── tool_read_file()
│   ├── tool_list_directory()
│   ├── tool_get_current_time()
│   ├── TOOLS schema
│   ├── TOOL_HANDLERS 分发表
│   └── process_tool_call()
│
├── REPL 命令处理 (第646-743 行)
│   └── handle_repl_command()
│
├── Agent 循环 (第744-895 行)
│   └── agent_loop()
│
└── 入口函数 (第896-912 行)
    └── main()
```

### 5.2 类关系图

```
┌─────────────────────────────────────────────────────────────┐
│                        agent_loop()                          │
│  ┌─────────────────────┐    ┌─────────────────────────┐     │
│  │    SessionStore     │    │    ContextGuard         │     │
│  ├─────────────────────┤    ├─────────────────────────┤     │
│  │ - agent_id          │    │ - max_tokens            │     │
│  │ - base_dir          │    │                         │     │
│  │ - index_path        │    │ + estimate_tokens()     │     │
│  │ - _index            │    │ + estimate_messages_    │     │
│  │ - current_session_id│    │   tokens()              │     │
│  ├─────────────────────┤    │ + truncate_tool_       │     │
│  │ + create_session()  │    │   result()             │     │
│  │ + load_session()    │    │ + compact_history()    │     │
│  │ + save_turn()       │    │ + guard_api_call()     │     │
│  │ + save_tool_result()│    │                         │     │
│  │ + append_transcript │    │                         │     │
│  │ + _rebuild_history()│    │                         │     │
│  │ + list_sessions()   │    │                         │     │
│  └─────────────────────┘    └─────────────────────────┘     │
│           │                          │                       │
│           │ messages[]               │ protected API call   │
│           ▼                          ▼                       │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              OpenAI Chat Completions API             │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 数据流图

```
                    用户输入
                        │
                        ▼
┌───────────────────────────────────────────────────────────────┐
│                        REPL 命令?                              │
│   /new, /list, /switch, /context, /compact, /help            │
└───────────────────────────────────────────────────────────────┘
                        │ 否
                        ▼
┌───────────────────────────────────────────────────────────────┐
│                    SessionStore.save_turn()                    │
│                    追加到 JSONL 文件                            │
└───────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────────┐
│                   ContextGuard.guard_api_call()                │
│                                                                │
│   ┌────────────┐  ┌────────────┐  ┌────────────────────┐      │
│   │ 正常调用    │→│ 截断工具结果 │→│ LLM摘要压缩历史     │      │
│   └────────────┘  └────────────┘  └────────────────────┘      │
└───────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────────┐
│                     finish_reason?                             │
│                                                                │
│        "stop"              "tool_calls"              其他      │
│           │                      │                      │      │
│           ▼                      ▼                      ▼      │
│      打印回复              执行工具              打印信息       │
│                              │                              │
│                              ▼                              │
│                  SessionStore.save_tool_result()             │
│                              │                              │
│                              └──────────→ 继续循环 ←─────────┘│
└───────────────────────────────────────────────────────────────┘
```

### 5.4 会话持久化流程

```
┌────────────────────────────────────────────────────────────────┐
│                     启动 agent_loop()                           │
│                           │                                     │
│                           ▼                                     │
│              store.list_sessions()                              │
│                           │                                     │
│              ┌────────────┴────────────┐                       │
│              ▼                         ▼                       │
│         有历史会话                 无历史会话                   │
│              │                         │                       │
│     store.load_session()        store.create_session()        │
│              │                         │                       │
│              └────────────┬────────────┘                       │
│                           ▼                                     │
│                    进入主循环                                    │
│                           │                                     │
│         ┌─────────────────┼─────────────────┐                  │
│         ▼                 ▼                 ▼                  │
│     用户消息         助手回复          工具调用/结果             │
│         │                 │                 │                  │
│   save_turn()       save_turn()      save_tool_result()       │
│         │                 │                 │                  │
│         └─────────────────┼─────────────────┘                  │
│                           ▼                                     │
│                   append_transcript()                           │
│                           │                                     │
│                    写入 JSONL                                   │
│                    更新索引                                      │
└────────────────────────────────────────────────────────────────┘
```

---

## 六、关键实现细节

### 6.1 JSONL 格式选择原因

1. **追加友好**：每行独立，可直接 append
2. **崩溃安全**：程序中断不会损坏已有数据
3. **流式处理**：可逐行读取，无需加载全部
4. **调试便利**：文本格式，可直接查看

### 6.2 消息重建的 API 兼容性

不同 API 对消息格式有不同要求：

**Anthropic API**：
- tool_use 块在 assistant 消息内
- tool_result 块在 user 消息内
- 必须严格交替

**OpenAI API**：
- 工具调用使用 tool_calls 字段
- 工具结果使用 role="tool" 的消息

本实现采用 Anthropic 风格存储，重建时兼容两种 API。

### 6.3 Token 估算的简化

```python
@staticmethod
def estimate_tokens(text: str) -> int:
    return len(text) // 4
```

这是保守估算：
- 英文：约 4 字符 = 1 token
- 中文：约 1-2 字符 = 1 token
- 实际值可能更大，估算偏小是安全的

---

## 七、使用示例

### 7.1 基本对话

```python
# 启动
python sessions/zh/s03_sessions.py

# 输出
  Resumed session: abc123def456 (5 messages)
============================================================
  claw0  |  Section 03: Sessions & Context Guard
  Model: qwen-plus
  Session: abc123def456
  Tools: read_file, list_directory, get_current_time
  Type /help for commands, quit/exit to leave.
============================================================

You > 帮我看看 workspace 目录下有什么文件
  [tool: list_directory] .
  [tool_calls: 1]
  Assistant: workspace 目录下有以下内容：
  [dir]  .sessions
  [file] SOUL.md
  ...
```

### 7.2 会话管理

```python
You > /new 项目开发
  Created new session: xyz789ghi012 (项目开发)

You > /list
  Sessions:
    xyz789ghi012 (项目开发)  msgs=0  last=2024-01-15T14:30:00 <-- current
    abc123def456             msgs=15 last=2024-01-15T10:22:00

You > /switch abc
  Switched to session: abc123def456 (15 messages)
```

### 7.3 上下文监控

```python
You > /context
  Context usage: ~45,000 / 180,000 tokens
  [######--------------------] 25.0%
  Messages: 32
```

---

## 八、总结

s03_sessions 在保持 s02 agent 循环核心不变的前提下，引入了生产级必需的两个机制：

1. **SessionStore** 解决了"对话如何持久化"的问题
   - JSONL 格式简单可靠
   - 支持多会话管理
   - 完整的消息重建逻辑

2. **ContextGuard** 解决了"上下文溢出怎么办"的问题
   - 三阶段渐进降级
   - 智能压缩保留关键信息
   - 用户可见的监控和干预

这两个机制为后续章节奠定了基础：
- s04_channels 需要会话来关联多渠道对话
- s05_gateway 需要会话来实现 agent 绑定
- s06_intelligence 需要上下文来构建长时记忆

---

*文档生成时间：2026-03-10*