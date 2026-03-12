# S03 `_rebuild_history` 方法中 `tool_result` 判断条件处理场景详解

## 一、概述

### 1.1 方法定位

`_rebuild_history` 是 `SessionStore` 类的核心方法，负责将 JSONL 格式的会话记录重建为 API 兼容的消息列表。其中 `tool_result` 的判断条件处理是实现正确消息重建的关键逻辑。

### 1.2 设计背景

根据 Anthropic API 规则：
- 消息必须 `user`/`assistant` 严格交替
- `tool_use` 块属于 `assistant` 消息
- `tool_result` 块属于 `user` 消息

当 assistant 调用多个工具时，会产生多个 `tool_result`，这些结果需要合并到同一个 `user` 消息中。

### 1.3 关键前提：JSONL 写入顺序

> ⚠️ **理解本文档的关键**：`_rebuild_history` 的输入（JSONL 记录）由 `agent_loop` 的输出逻辑决定。

**`save_tool_result` 的写入逻辑**（lines 182-199）：

```python
def save_tool_result(self, tool_use_id: str, name: str,
                     tool_input: dict, result: str) -> None:
    ts = time.time()
    # 先写入 tool_use 记录
    self.append_transcript(self.current_session_id, {
        "type": "tool_use",
        ...
    })
    # 紧接着写入 tool_result 记录
    self.append_transcript(self.current_session_id, {
        "type": "tool_result",
        ...
    })
```

**`agent_loop` 的调用逻辑**（lines 854-866）：

```python
for tool_call in tool_calls:  # 按顺序逐个处理
    result = process_tool_call(...)
    store.save_tool_result(tool_call.id, ...)  # 每次写入 tool_use + tool_result 对
```

**结论**：JSONL 写入顺序是 `tool_use, tool_result` **交替出现**，而非所有 `tool_use` 先写、所有 `tool_result` 后写。

---

## 二、核心代码分析

### 2.1 `tool_result` 处理代码

```python
elif rtype == "tool_result":
    result_block = {
        "type": "tool_result",
        "tool_use_id": record["tool_use_id"],
        "content": record["content"],
    }
    # 将连续的 tool_result 合并到同一个 user 消息中
    if (messages and messages[-1]["role"] == "user"
            and isinstance(messages[-1]["content"], list)
            and messages[-1]["content"]
            and isinstance(messages[-1]["content"][0], dict)
            and messages[-1]["content"][0].get("type") == "tool_result"):
        messages[-1]["content"].append(result_block)
    else:
        messages.append({
            "role": "user",
            "content": [result_block],
        })
```

### 2.2 `tool_use` 处理代码

```python
elif rtype == "tool_use":
    block = {
        "type": "tool_use",
        "id": record["tool_use_id"],
        "name": record["name"],
        "input": record["input"],
    }
    if messages and messages[-1]["role"] == "assistant":
        content = messages[-1]["content"]
        if isinstance(content, list):
            content.append(block)
        else:
            messages[-1]["content"] = [
                {"type": "text", "text": str(content)},
                block,
            ]
    else:
        messages.append({
            "role": "assistant",
            "content": [block],
        })
```

### 2.3 `tool_result` 判断条件分解

复合条件包含 **6 个子条件**，全部为 `True` 时执行合并操作：

| 序号 | 条件表达式 | 含义 |
|------|-----------|------|
| C1 | `messages` | 消息列表存在且不为空 |
| C2 | `messages[-1]["role"] == "user"` | 最后一条消息是 user 角色 |
| C3 | `isinstance(messages[-1]["content"], list)` | content 字段是列表类型 |
| C4 | `messages[-1]["content"]` | content 列表不为空 |
| C5 | `isinstance(messages[-1]["content"][0], dict)` | content 第一个元素是字典 |
| C6 | `messages[-1]["content"][0].get("type") == "tool_result"` | 第一个元素的 type 是 tool_result |

### 2.4 条件链的短路求值

```
C1 (messages 非空)
  └─ False → 创建新消息
  └─ True ↓
C2 (最后消息是 user)
  └─ False → 创建新消息
  └─ True ↓
C3 (content 是列表)
  └─ False → 创建新消息
  └─ True ↓
C4 (content 非空)
  └─ False → 创建新消息
  └─ True ↓
C5 (第一个元素是字典)
  └─ False → 创建新消息
  └─ True ↓
C6 (type 是 tool_result)
  └─ False → 创建新消息
  └─ True → 合并到现有消息
```

---

## 三、实际运行场景分析

> 基于 `agent_loop` 的实际输出逻辑，分析 `_rebuild_history` 会遇到的真实场景。

### 3.1 agent_loop 的三种 finish_reason

```python
finish_reason = response.choices[0].finish_reason

if finish_reason == "stop":
    # 场景 1：正常结束（纯文本回复）
    ...
elif finish_reason == "tool_calls" and tool_calls:
    # 场景 2：工具调用
    ...
else:
    # 场景 3：其他情况
    ...
```

---

### 3.2 场景一：finish_reason == "stop"（纯文本回复）

**触发条件**：Assistant 完成回复，无需调用工具。

**数据流**：

```
API Response                          JSONL 记录                     
─────────────                         ───────────                    
{                                     {"type": "assistant",          
  "finish_reason": "stop",              "content": [{"type": "text",    
  "message": {                           "text": "您好！"}],            
    "content": "您好！",                 "ts": 1705312200               
    "role": "assistant"                }                                  
  }                                                                        
```

**重建结果**：
```python
messages = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": [{"type": "text", "text": "您好！"}]}
]
```

**说明**：此场景不涉及 `tool_result`，重建逻辑简单直接。

---

### 3.3 场景二：finish_reason == "tool_calls"（工具调用）

#### 3.3.1 单工具调用

**JSONL 写入顺序**（由 `save_tool_result` 产生）：

```json
{"type": "tool_use", "tool_use_id": "call_001", "name": "read_file", ...}
{"type": "tool_result", "tool_use_id": "call_001", "content": "文件内容", ...}
```

**重建过程追踪**：

```
初始状态: messages = [之前的对话...]

步骤 1: 处理 tool_use call_001
  ├─ rtype == "tool_use"
  ├─ messages[-1]["role"] 可能是 user 或 assistant
  │   └─ 若是 assistant → 合并到现有消息
  │   └─ 若是 user → 创建新 assistant 消息
  └─ 结果: messages 包含 assistant 消息（含 tool_use_001）

步骤 2: 处理 tool_result call_001
  ├─ rtype == "tool_result"
  ├─ C2 检查: messages[-1]["role"] == "assistant" ✗
  │   └─ C2 = False → 创建新 user 消息
  └─ 结果: messages 包含 user 消息（含 tool_result_001）
```

**最终 messages 结构**：
```python
messages = [
    ...,  # 之前的对话
    {"role": "assistant", "content": [
        {"type": "text", "text": "我来读取文件"},
        {"type": "tool_use", "id": "call_001", "name": "read_file", ...}
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_001", "content": "文件内容"}
    ]}
]
```

---

#### 3.3.2 多工具并行调用

> ⚠️ **关键场景**：展示 `tool_use` 和 `tool_result` 交替写入的实际行为。

**API Response**：
```json
{
  "finish_reason": "tool_calls",
  "message": {
    "tool_calls": [
      {"id": "call_001", "function": {"name": "read_file", ...}},
      {"id": "call_002", "function": {"name": "read_file", ...}},
      {"id": "call_003", "function": {"name": "get_current_time", ...}}
    ]
  }
}
```

**JSONL 写入顺序**（由 `agent_loop` 循环 + `save_tool_result` 产生）：

```json
{"type": "tool_use", "tool_use_id": "call_001", ...}
{"type": "tool_result", "tool_use_id": "call_001", ...}
{"type": "tool_use", "tool_use_id": "call_002", ...}
{"type": "tool_result", "tool_use_id": "call_002", ...}
{"type": "tool_use", "tool_use_id": "call_003", ...}
{"type": "tool_result", "tool_use_id": "call_003", ...}
```

**重建过程逐步追踪**：

假设初始状态：`messages = [assistant 文本消息]`

```
步骤 1: 处理 tool_use call_001
  ├─ messages[-1]["role"] == "assistant" ✓
  ├─ 合并到现有 assistant 消息
  └─ messages: [assistant(text, tool_use_001)]

步骤 2: 处理 tool_result call_001
  ├─ messages[-1]["role"] == "assistant" ✗
  ├─ C2 = False → 创建新 user 消息
  └─ messages: [assistant(...), user(tool_result_001)]

步骤 3: 处理 tool_use call_002
  ├─ messages[-1]["role"] == "user" ✗
  ├─ 不满足合并条件（最后消息不是 assistant）
  ├─ 创建新 assistant 消息
  └─ messages: [..., user(...), assistant(tool_use_002)]

步骤 4: 处理 tool_result call_002
  ├─ messages[-1]["role"] == "assistant" ✗
  ├─ C2 = False → 创建新 user 消息
  └─ messages: [..., assistant(...), user(tool_result_002)]

步骤 5-6: 处理 call_003（同理）
  └─ 创建新 assistant 消息，再创建新 user 消息
```

**实际重建结果**：

```python
messages = [
    {"role": "assistant", "content": [
        {"type": "text", "text": "我来读取这些文件"},
        {"type": "tool_use", "id": "call_001", ...}
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_001", ...}
    ]},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_002", ...}
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_002", ...}
    ]},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_003", ...}
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_003", ...}
    ]}
]
```

**核心结论**：

由于 JSONL 写入顺序是 `tool_use, tool_result` 交替，重建后的消息列表中 `assistant` 和 `user` **也是交替出现**的，每个 `tool_use` 后紧跟一个 `tool_result`。

---

### 3.4 与 OpenAI API 的兼容性

上述交替格式**符合 OpenAI API 的消息模式**：

| 消息位置 | OpenAI 格式 | 对应 JSONL 记录 |
|---------|------------|----------------|
| 第 N 条 | `{"role": "assistant", "tool_calls": [...]}` | `tool_use` 记录 |
| 第 N+1 条 | `{"role": "tool", "tool_call_id": "...", "content": "..."}` | `tool_result` 记录 |

**agent_loop 中实际使用的 OpenAI 格式**（lines 869-887）：

```python
# 添加 assistant 消息（含 tool_calls）
messages.append({
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": tool_call.function.arguments,
        },
    }],
})

# 添加 tool 消息（含结果）
messages.append({
    "role": "tool",
    "tool_call_id": tool_call.id,
    "content": result,
})
```

**设计意义**：虽然 JSONL 存储格式参考 Anthropic 风格，但 `_rebuild_history` 重建后的消息结构实际上更接近 OpenAI 的 `tool_calls` 模式，保证了与运行时 API 调用的一致性。

---

## 四、边界情况分析

> 以下场景在实际运行中较少出现，但 `_rebuild_history` 通过防御性编程处理这些情况。

### 4.1 场景矩阵

| 场景 | C1 | C2 | C3 | C4 | C5 | C6 | 处理方式 | 触发条件 |
|------|----|----|----|----|----|----|----------|----------|
| A | ❌ | - | - | - | - | - | 创建新消息 | 消息列表为空 |
| B | ✅ | ❌ | - | - | - | - | 创建新消息 | 最后消息是 assistant |
| C | ✅ | ✅ | ❌ | - | - | - | 创建新消息 | user 消息的 content 是字符串 |
| D | ✅ | ✅ | ✅ | ❌ | - | - | 创建新消息 | content 是空列表 |
| E | ✅ | ✅ | ✅ | ✅ | ❌ | - | 创建新消息 | content 首元素不是字典 |
| F | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 创建新消息 | 首元素不是 tool_result |

---

### 4.2 场景 A：消息列表为空

**条件状态**：`C1 = False`

**JSONL 输入**：
```json
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312200.0}
```

**处理结果**：
```python
messages = [
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_001", "content": "结果1"}
    ]}
]
```

**说明**：JSONL 文件以 `tool_result` 开头（异常情况），直接创建新的 user 消息。

---

### 4.3 场景 B：最后消息是 assistant（实际运行中常见）

**条件状态**：`C1 = True, C2 = False`

**这是实际运行中最常见的场景**：处理 `tool_result` 时，上一条记录是 `tool_use`（已合并到 assistant 消息）。

**JSONL 输入**：
```json
{"type": "tool_use", "tool_use_id": "call_001", ...}
{"type": "tool_result", "tool_use_id": "call_001", ...}
```

**处理过程**：
```python
# 处理 tool_use 后
messages = [
    {"role": "assistant", "content": [{"type": "tool_use", ...}]}
]

# 处理 tool_result 时
# C2: messages[-1]["role"] == "assistant" ✗
# → 创建新 user 消息
```

---

### 4.4 场景 C：user 消息的 content 是字符串

**条件状态**：`C1 = True, C2 = True, C3 = False`

**JSONL 输入**：
```json
{"type": "user", "content": "普通用户消息", "ts": 1705312200.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312201.0}
```

**处理结果**：
```python
messages = [
    {"role": "user", "content": "普通用户消息"},  # 字符串
    {"role": "user", "content": [               # 新创建
        {"type": "tool_result", ...}
    ]}
]
```

**说明**：普通用户输入后出现 `tool_result`（可能是 JSONL 记录错误），创建新消息避免覆盖。

---

### 4.5 场景 D-F：数据格式异常

这些场景处理 JSONL 文件可能被手动编辑或损坏的情况：

| 场景 | 异常情况 | 处理方式 |
|------|---------|---------|
| D | content 是空列表 `[]` | 创建新消息 |
| E | content 首元素不是字典 `["纯文本"]` | 创建新消息 |
| F | 首元素 type 不是 tool_result `[{"type": "text", ...}]` | 创建新消息 |

---

## 五、设计决策分析

### 5.1 为什么 tool_result 合并条件如此复杂？

| 原因 | 说明 |
|------|------|
| **API 兼容性** | Anthropic API 要求消息严格交替，多个 `tool_result` 可能需要合并 |
| **数据完整性** | JSONL 可能损坏或被手动编辑，需要防御性检查 |
| **类型安全** | content 可能是字符串或列表，需要分别处理 |
| **向后兼容** | 支持未来可能改变写入顺序的场景 |

### 5.2 当前实现 vs 理想实现

| 维度 | 当前实现 | 理想实现（Anthropic 风格） |
|------|---------|------------------------|
| JSONL 写入顺序 | `tool_use, tool_result` 交替 | 所有 `tool_use` 先写，所有 `tool_result` 后写 |
| 重建后消息结构 | `assistant`/`user` 交替 | 单个 `assistant`（含所有 tool_use）+ 单个 `user`（含所有 tool_result） |
| API 兼容性 | OpenAI `tool_calls` 模式 | Anthropic 消息格式 |

### 5.3 潜在改进方向

如果需要实现"所有 tool_use 合并到一个 assistant，所有 tool_result 合并到一个 user"：

| 方案 | 修改点 | 工作量 |
|------|--------|--------|
| A | 修改 `save_tool_result` 写入顺序 | 低 |
| B | 修改 `_rebuild_history` 为两遍扫描 | 中 |
| C | 在 `agent_loop` 中批量收集后再写入 | 低 |

---

## 六、总结

### 6.1 核心要点

1. **写入顺序决定重建结果**：`save_tool_result` 按 `tool_use, tool_result` 交替写入，导致重建后消息也是交替的。

2. **C2 是关键条件**：实际运行中，处理 `tool_result` 时 `messages[-1]` 通常是 `assistant`（刚处理完 `tool_use`），所以 `C2 = False`，创建新 user 消息。

3. **与 OpenAI API 兼容**：交替的消息结构符合 OpenAI 的 `tool_calls` 模式，保证了运行时 API 调用的一致性。

4. **防御性编程**：6 条件判断链确保即使 JSONL 格式异常，也能安全重建。

### 6.2 数据流总览

```
┌─────────────────────────────────────────────────────────────┐
│                      agent_loop                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  for tool_call in tool_calls:                         │  │
│  │      save_tool_result(id, name, input, result)        │  │
│  │          ├─ append tool_use 记录                      │  │
│  │          └─ append tool_result 记录                   │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     JSONL 文件                               │
│  tool_use_1 → tool_result_1 → tool_use_2 → tool_result_2... │
│  （交替写入）                                                │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   _rebuild_history                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  处理 tool_use_1 → 合并/创建 assistant                 │  │
│  │  处理 tool_result_1 → C2=False → 创建 user            │  │
│  │  处理 tool_use_2 → 创建新 assistant                    │  │
│  │  处理 tool_result_2 → C2=False → 创建 user            │  │
│  │  ...                                                   │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    重建后的 messages                         │
│  [assistant, user, assistant, user, ...]                    │
│  （交替出现）                                                │
└─────────────────────────────────────────────────────────────┘
```

---

*文档生成时间：2026-03-12*