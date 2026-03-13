# S04 finish_reason / stop_reason 详解

## 一、核心问题

**`finish_reason` / `stop_reason` 是大模型输出的，还是代码实现的？**

**答案：是大模型 API 返回的，不是代码实现的。**

---

## 二、两个 API 的区别

### 2.1 Anthropic API vs OpenAI API

当前代码使用的是 **OpenAI 兼容 API**（阿里云百炼 DashScope），但文档示例展示的是 Anthropic 风格。

| 特性 | Anthropic API | OpenAI API |
|------|--------------|------------|
| 字段名 | `stop_reason` | `finish_reason` |
| 响应结构 | `response.stop_reason` | `response.choices[0].finish_reason` |
| 正常结束值 | `"end_turn"` | `"stop"` |
| 工具调用值 | `"tool_use"` | `"tool_calls"` |

### 2.2 API 响应结构对比

#### Anthropic API 响应

```json
{
  "id": "msg_xxx",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "您好！有什么可以帮助您的？"}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 10, "output_tokens": 20}
}
```

#### OpenAI API 响应

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "您好！有什么可以帮助您的？"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20}
}
```

---

## 三、finish_reason 的值与含义

### 3.1 OpenAI API finish_reason 值

| 值 | 含义 | 代码处理 |
|---|------|---------|
| `"stop"` | 模型正常结束回复 | 发送回复给用户，退出循环 |
| `"tool_calls"` | 模型请求调用工具 | 执行工具，追加结果，继续循环 |
| `"length"` | 达到 max_tokens 限制 | 发送部分回复，退出循环 |
| `"content_filter"` | 内容被安全过滤 | 退出循环 |
| `"function_call"` | 旧版工具调用格式 | 按普通响应处理 |

### 3.2 Anthropic API stop_reason 值

| 值 | 含义 | 代码处理 |
|---|------|---------|
| `"end_turn"` | 模型结束当前回合 | 发送回复给用户，退出循环 |
| `"tool_use"` | 模型请求调用工具 | 执行工具，追加结果，继续循环 |
| `"max_tokens"` | 达到 token 限制 | 退出循环 |
| `"stop_sequence"` | 遇到停止序列 | 退出循环 |

---

## 四、代码实现分析

### 4.1 实际代码（OpenAI API）

```python
# s04_channels.py, lines 630-698
def run_agent_turn(...):
    while True:
        response = client.chat.completions.create(
            model=MODEL_ID,
            max_tokens=8096,
            tools=TOOLS,
            messages=api_messages,
        )

        # 从 API 响应中获取 finish_reason
        finish_reason = response.choices[0].finish_reason  # ← API 返回的值

        if finish_reason == "stop":
            # 正常结束，发送回复
            ch.send(inbound.peer_id, assistant_text)
            break

        elif finish_reason == "tool_calls" and tool_calls:
            # 工具调用，执行后继续循环
            for tool_call in tool_calls:
                result = process_tool_call(...)
                messages.append({"role": "tool", ...})
            # continue → 下一轮循环

        else:
            # 其他情况（length, content_filter 等）
            break
```

### 4.2 文档示例代码（Anthropic 风格）

```python
# s04_channels.md 中展示的伪代码
while True:
    response = client.messages.create(...)

    if response.stop_reason == "end_turn":
        # 正常结束
        break
    elif response.stop_reason == "tool_use":
        # 工具调用
        ...
```

**注意**：文档展示的是 Anthropic API 风格，但实际代码使用 OpenAI 兼容 API。

---

## 五、finish_reason 的生成机制

### 5.1 模型如何决定 finish_reason？

```
┌─────────────────────────────────────────────────────────────┐
│                      LLM 推理过程                            │
│                                                             │
│  输入: messages[] + tools[] + system_prompt                 │
│                                                             │
│  模型内部决策:                                               │
│  ├─ 是否需要调用工具？                                       │
│  │   ├─ 是 → 生成 tool_calls → finish_reason = "tool_calls" │
│  │   └─ 否 → 继续生成文本                                   │
│  │                                                          │
│  ├─ 是否达到 max_tokens 限制？                               │
│  │   └─ 是 → finish_reason = "length"                       │
│  │                                                          │
│  ├─ 是否自然结束（生成了 EOS token）？                       │
│  │   └─ 是 → finish_reason = "stop"                         │
│  │                                                          │
│  └─ 内容是否触发安全过滤？                                    │
│      └─ 是 → finish_reason = "content_filter"               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 关键点

1. **finish_reason 是模型推理结果的一部分**，不是代码生成的
2. **模型会根据上下文和工具定义决定是否调用工具**
3. **API 服务端在响应中包含 finish_reason**

### 5.3 代码的角色

代码**不生成** finish_reason，只**响应**它：

```
模型输出 finish_reason → API 返回 → 代码读取 → 决定下一步动作
```

---

## 六、完整流程示例

### 6.1 场景：用户请求读取文件

```
┌─────────────────────────────────────────────────────────────┐
│ 第 1 轮: 用户输入                                            │
│   messages = [{"role": "user", "content": "读取 a.txt"}]    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ API 请求                                                     │
│   POST /v1/chat/completions                                 │
│   {                                                         │
│     "model": "qwen-plus",                                   │
│     "messages": [...],                                      │
│     "tools": [{"type": "function", "function": {...}}]      │
│   }                                                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 模型决策: 需要调用 read_file 工具                            │
│                                                             │
│ API 响应:                                                   │
│   {                                                         │
│     "choices": [{                                           │
│       "message": {                                          │
│         "role": "assistant",                                │
│         "content": null,                                    │
│         "tool_calls": [{"id": "call_001", ...}]             │
│       },                                                    │
│       "finish_reason": "tool_calls"  ← 模型决定             │
│     }]                                                      │
│   }                                                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 代码处理 (finish_reason == "tool_calls")                    │
│   1. 提取 tool_calls                                        │
│   2. 执行 read_file("a.txt")                                │
│   3. 追加工具结果到 messages                                │
│   4. continue → 进入下一轮循环                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 第 2 轮: 工具结果                                            │
│   messages = [                                              │
│     {"role": "user", "content": "读取 a.txt"},              │
│     {"role": "assistant", "tool_calls": [...]},             │
│     {"role": "tool", "content": "文件内容..."}              │
│   ]                                                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ API 请求 (第二轮)                                            │
│   ... 包含工具结果的 messages ...                            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 模型决策: 可以回答用户了                                     │
│                                                             │
│ API 响应:                                                   │
│   {                                                         │
│     "choices": [{                                           │
│       "message": {                                          │
│         "role": "assistant",                                │
│         "content": "文件 a.txt 的内容是..."                 │
│       },                                                    │
│       "finish_reason": "stop"  ← 模型决定正常结束           │
│     }]                                                      │
│   }                                                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ 代码处理 (finish_reason == "stop")                          │
│   1. 提取 assistant_text                                    │
│   2. 发送给用户                                              │
│   3. break → 退出循环                                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 七、文档与代码的差异

### 7.1 差异总结

| 文件 | API 风格 | 字段名 | 结束值 |
|------|---------|--------|--------|
| s04_channels.md | Anthropic | `stop_reason` | `"end_turn"` |
| s04_channels.py | OpenAI | `finish_reason` | `"stop"` |

### 7.2 原因

- **文档**：展示的是通用设计思路，使用 Anthropic 风格作为示例
- **代码**：实际运行需要 OpenAI 兼容 API（阿里云百炼 DashScope）

### 7.3 建议

文档应更新为与实际代码一致，或明确标注两种 API 的差异。

---

## 八、总结

### 8.1 核心结论

| 问题 | 答案 |
|------|------|
| finish_reason 由谁生成？ | **大模型 API 返回**，不是代码实现 |
| 代码的角色是什么？ | 读取并响应，决定下一步动作 |
| 不同 API 有何差异？ | Anthropic 用 `stop_reason`，OpenAI 用 `finish_reason` |

### 8.2 设计意义

```
模型拥有"主动权":
  ├─ 决定何时结束 (stop)
  ├─ 决定何时调用工具 (tool_calls)
  └─ 决定何时因限制停止 (length)

代码拥有"执行权":
  ├─ 响应模型的决策
  ├─ 执行工具调用
  └─ 处理边界情况
```

这种分工使 agent 循环成为可能——模型可以连续多次调用工具，代码只需响应其决策。

---

*文档生成时间：2026-03-12*