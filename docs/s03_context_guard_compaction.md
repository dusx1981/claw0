# S03 ContextGuard 压缩机制详解

## 一、概述

### 1.1 ContextGuard 定位

`ContextGuard` 是 `SessionStore` 的配套组件，负责保护 agent 免受上下文窗口溢出。它实现了**三阶段渐进式压缩策略**，在 API 调用失败时自动尝试恢复。

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **渐进式恢复** | 从最小代价到最大代价依次尝试 |
| **保留关键信息** | 优先保留最近的消息和关键决策 |
| **透明性** | 用户无需手动干预，自动处理溢出 |

### 1.3 配置参数

```python
CONTEXT_SAFE_LIMIT = 180000  # 最大 token 限制
MAX_TOOL_OUTPUT = 50000      # 工具输出最大字符数
```

---

## 二、三阶段压缩策略

### 2.1 整体流程

```
┌─────────────────────────────────────────────────────────────┐
│                    guard_api_call                           │
│                                                             │
│  第 0 次尝试：正常调用 API                                   │
│       │                                                     │
│       ├─ 成功 → 返回结果                                    │
│       │                                                     │
│       └─ 失败（context overflow）                           │
│              │                                              │
│              ▼                                              │
│  第 1 次尝试：截断过大的 tool_result                         │
│       │                                                     │
│       ├─ 成功 → 返回结果                                    │
│       │                                                     │
│       └─ 失败（still overflow）                             │
│              │                                              │
│              ▼                                              │
│  第 2 次尝试：LLM 摘要压缩历史                               │
│       │                                                     │
│       ├─ 成功 → 返回结果                                    │
│       │                                                     │
│       └─ 失败 → 抛出 RuntimeError                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心代码

```python
def guard_api_call(
    self,
    api_client: OpenAI,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_retries: int = 2,
) -> Any:
    current_messages = messages
    base_messages = [{"role": "system", "content": system}]

    for attempt in range(max_retries + 1):
        try:
            # 尝试 API 调用
            result = api_client.chat.completions.create(...)
            return result

        except Exception as exc:
            error_str = str(exc).lower()
            is_overflow = ("context" in error_str or "token" in error_str)

            if not is_overflow or attempt >= max_retries:
                raise  # 非溢出错误或重试耗尽，直接抛出

            if attempt == 0:
                # 第 1 次重试：截断 tool_result
                current_messages = self._truncate_large_tool_results(...)
            elif attempt == 1:
                # 第 2 次重试：压缩历史
                current_messages = self.compact_history(...)
```

---

## 三、阶段一：正常调用

### 3.1 行为

直接调用 API，不做任何处理。

### 3.2 触发条件

所有 API 调用的初始尝试。

### 3.3 成功条件

上下文未超过限制，API 正常返回。

---

## 四、阶段二：截断 tool_result

### 4.1 触发条件

- 第 0 次尝试失败
- 错误信息包含 "context" 或 "token"

### 4.2 核心逻辑

```python
def truncate_tool_result(self, result: str, max_fraction: float = 0.3) -> str:
    """在换行边界处只保留头部进行截断。"""
    max_chars = int(self.max_tokens * 4 * max_fraction)  # 180000 * 4 * 0.3 = 216000 字符
    if len(result) <= max_chars:
        return result
    
    # 在换行边界处截断
    cut = result.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    head = result[:cut]
    return head + f"\n\n[... truncated ({len(result)} chars total, showing first {len(head)}) ...]"
```

### 4.3 截断策略

| 参数 | 值 | 说明 |
|------|-----|------|
| `max_fraction` | 0.3 | 工具结果最大占上下文的 30% |
| 截断边界 | 换行符 | 在换行处截断，保持内容完整性 |
| 截断标记 | `[... truncated ...]` | 告知用户内容被截断 |

### 4.4 示例场景

**原始 tool_result**（300,000 字符的大文件内容）：

```
第一章 简介
...（大量内容）...
第十章 总结
```

**截断后**（216,000 字符限制）：

```
第一章 简介
...（部分内容）...

[... truncated (300000 chars total, showing first 215800) ...]
```

### 4.5 为什么这是第一阶段？

| 特点 | 说明 |
|------|------|
| **代价最小** | 只修改工具输出，不改变对话结构 |
| **信息损失可控** | 保留头部内容，通常包含最关键信息 |
| **可逆性** | 用户可以要求重新读取完整文件 |

---

## 五、阶段三：LLM 摘要压缩历史

### 5.1 触发条件

- 第 1 次重试仍然失败
- 说明截断 tool_result 不足以解决问题

### 5.2 核心逻辑

```python
def compact_history(self, messages: list[dict],
                    api_client: OpenAI, model: str) -> list[dict]:
    """
    将前 50% 的消息压缩为 LLM 生成的摘要。
    保留最后 N 条消息 (N = max(4, 总数的 20%)) 不变。
    """
    total = len(messages)
    if total <= 4:
        return messages  # 太少，不压缩

    keep_count = max(4, int(total * 0.2))      # 保留最近 20%（最少 4 条）
    compress_count = max(2, int(total * 0.5))  # 压缩前 50%（最少 2 条）
    compress_count = min(compress_count, total - keep_count)

    if compress_count < 2:
        return messages

    old_messages = messages[:compress_count]
    recent_messages = messages[compress_count:]

    # 调用 LLM 生成摘要
    old_text = _serialize_messages_for_summary(old_messages)
    summary = self._generate_summary(api_client, model, old_text)

    # 构建压缩后的消息
    compacted = [
        {"role": "user", "content": "[Previous conversation summary]\n" + summary},
        {"role": "assistant", "content": [{"type": "text", "text": "Understood, I have the context..."}]},
    ]
    compacted.extend(recent_messages)
    return compacted
```

### 5.3 消息分割策略

```
原始消息列表 (20 条):
┌─────────────────────────────────────────────────────────────┐
│ [1-10]  ← 压缩为摘要 (compress_count = 50% = 10)           │
├─────────────────────────────────────────────────────────────┤
│ [11-16] ← 保留 (keep_count = max(4, 20*0.2) = 4 条？)      │
│ [17-20] ← 实际保留 (total - compress_count = 10 条)        │
└─────────────────────────────────────────────────────────────┘

压缩后消息列表:
┌─────────────────────────────────────────────────────────────┐
│ [user]   [Previous conversation summary]\n{摘要内容}         │
│ [assistant] Understood, I have the context...               │
├─────────────────────────────────────────────────────────────┤
│ [11-20] ← 原始消息保持不变                                  │
└─────────────────────────────────────────────────────────────┘
```

### 5.4 摘要生成流程

```
┌─────────────────────────────────────────────────────────────┐
│          _serialize_messages_for_summary                    │
│                                                             │
│  原始消息:                                                   │
│    {"role": "user", "content": "帮我分析这个文件"}           │
│    {"role": "assistant", "content": [{"type": "tool_use",  │
│      "name": "read_file", "input": {"file_path": "a.txt"}}]}│
│    {"role": "user", "content": [{"type": "tool_result",    │
│      "content": "文件内容..."}]}                            │
│                                                             │
│  序列化结果:                                                 │
│    [user]: 帮我分析这个文件                                  │
│    [assistant called read_file]: {"file_path": "a.txt"}    │
│    [tool_result]: 文件内容...（截断到 500 字符）             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    LLM 摘要生成                              │
│                                                             │
│  System: You are a conversation summarizer. Be concise.     │
│  User: Summarize the following conversation concisely,      │
│        preserving key facts and decisions.                  │
│        {序列化内容}                                          │
│                                                             │
│  Output: 摘要文本（最多 2048 tokens）                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.5 示例场景

**压缩前**（20 条消息，约 150,000 tokens）：

```python
messages = [
    {"role": "user", "content": "请帮我分析这个项目的架构"},
    {"role": "assistant", "content": [{"type": "tool_use", "name": "list_directory", ...}]},
    {"role": "user", "content": [{"type": "tool_result", "content": "src/\n  main.py\n  utils.py\n..."}]},
    # ... 17 条消息 ...
    {"role": "user", "content": "继续分析第三个模块"},
    {"role": "assistant", "content": "第三个模块是..."},
]
```

**压缩后**（12 条消息，约 40,000 tokens）：

```python
messages = [
    {"role": "user", "content": "[Previous conversation summary]\n用户请求分析项目架构。已分析 src/main.py（入口文件）、src/utils.py（工具函数）。项目采用分层架构，包含数据层、业务层、表示层。用户关注点：代码质量、模块间依赖、扩展性。"},
    {"role": "assistant", "content": [{"type": "text", "text": "Understood, I have the context from our previous conversation."}]},
    # ... 最近 10 条消息保持不变 ...
    {"role": "user", "content": "继续分析第三个模块"},
    {"role": "assistant", "content": "第三个模块是..."},
]
```

### 5.6 摘要生成失败处理

```python
try:
    summary_resp = api_client.chat.completions.create(...)
    summary_text = summary_resp.choices[0].message.content or ""
except Exception as exc:
    print_warn(f"[compact] Summary failed ({exc}), dropping old messages")
    return recent_messages  # 直接丢弃旧消息
```

**失败策略**：如果 LLM 摘要生成失败，直接丢弃旧消息，只保留最近的消息。

---

## 六、Token 估算机制

### 6.1 估算方法

```python
@staticmethod
def estimate_tokens(text: str) -> int:
    return len(text) // 4  # 粗略估算：4 字符 ≈ 1 token
```

### 6.2 消息 Token 估算

```python
def estimate_messages_tokens(self, messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += self.estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        total += self.estimate_tokens(block["text"])
                    elif block.get("type") == "tool_result":
                        total += self.estimate_tokens(block.get("content", ""))
                    elif block.get("type") == "tool_use":
                        total += self.estimate_tokens(json.dumps(block.get("input", {})))
    return total
```

### 6.3 估算准确性

| 方法 | 准确性 | 说明 |
|------|--------|------|
| `len(text) // 4` | 粗略 | 简单快速，适用于英文 |
| tiktoken | 精确 | 需要 tokenizer，适用特定模型 |
| 当前实现 | 保守 | 倾向于高估，触发更早的压缩 |

---

## 七、完整场景示例

### 7.1 场景：读取大文件导致溢出

```
┌─────────────────────────────────────────────────────────────┐
│  用户: 请读取 big_data.csv 并分析                           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  第 0 次尝试：正常调用                                       │
│  API Error: context_length_exceeded (200K > 180K limit)     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  第 1 次尝试：截断 tool_result                               │
│  big_data.csv 内容从 500K 字符截断到 216K 字符              │
│  API Error: context_length_exceeded (still 190K)            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  第 2 次尝试：压缩历史                                       │
│  前 50% 消息压缩为摘要                                       │
│  摘要: "用户请求分析 big_data.csv，文件包含销售数据..."      │
│  压缩后: 12 条消息 → 60K tokens                             │
│  API Success!                                               │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 场景：长期对话积累导致溢出

```
┌─────────────────────────────────────────────────────────────┐
│  50 轮对话后，累计 200K tokens                               │
│  第 0 次尝试：正常调用 → 失败                                │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  第 1 次尝试：截断 tool_result                               │
│  无 tool_result 可截断（或截断后仍超限）                     │
│  API Error: context_length_exceeded                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  第 2 次尝试：压缩历史                                       │
│  前 25 条消息 → 摘要（约 2000 字符）                         │
│  压缩后: 27 条消息 → 50K tokens                              │
│  API Success!                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## 八、设计决策分析

### 8.1 为什么是三阶段？

| 阶段 | 代价 | 信息损失 | 适用场景 |
|------|------|---------|---------|
| 正常调用 | 无 | 无 | 大多数情况 |
| 截断 tool_result | 低 | 部分工具输出 | 单个大文件导致溢出 |
| 压缩历史 | 高 | 部分对话细节 | 长期对话积累 |

**渐进式策略的优势**：
1. 优先尝试最小代价方案
2. 只有在必要时才损失信息
3. 每个阶段都是独立的恢复点

### 8.2 为什么压缩前 50%？

| 参数 | 值 | 理由 |
|------|-----|------|
| `compress_count` | 50% | 平衡压缩效果和保留上下文 |
| `keep_count` | 20%（最少 4 条） | 保留最近的交互上下文 |

### 8.3 为什么用 LLM 生成摘要？

| 方法 | 优点 | 缺点 |
|------|------|------|
| 直接丢弃 | 简单 | 信息完全丢失 |
| 关键词提取 | 快速 | 丢失语义 |
| **LLM 摘要** | **保留关键信息** | **需要额外 API 调用** |

### 8.4 潜在改进方向

| 改进 | 说明 |
|------|------|
| 精确 token 计算 | 使用 tiktoken 替代估算 |
| 滑动窗口压缩 | 只压缩超出部分 |
| 分层摘要 | 对摘要再压缩 |
| 向量检索 | 用语义搜索替代历史 |

---

## 九、与 agent_loop 的集成

### 9.1 调用位置

```python
# agent_loop 内层循环 (lines 812-818)
response = guard.guard_api_call(
    api_client=client,
    model=MODEL_ID,
    system=SYSTEM_PROMPT,
    messages=messages,
    tools=TOOLS,
)
```

### 9.2 用户可见效果

```
[guard] Context overflow detected, truncating large tool results...
[compact] 25 messages -> summary (1823 chars)
```

### 9.3 手动压缩命令

```python
# /compact 命令 (lines 723-730)
elif cmd == "/compact":
    if len(messages) <= 4:
        print_info("Too few messages to compact (need > 4).")
    else:
        print_session("Compacting history...")
        new_messages = guard.compact_history(messages, client, MODEL_ID)
        print_session(f"{len(messages)} -> {len(new_messages)} messages")
```

---

## 十、总结

### 10.1 核心要点

1. **三阶段渐进式策略**：正常调用 → 截断 tool_result → 压缩历史
2. **优先保留最近信息**：压缩前 50%，保留最近 20%
3. **LLM 生成摘要**：保留关键决策和事实
4. **防御性编程**：摘要失败时直接丢弃旧消息

### 10.2 流程图

```
API 调用
    │
    ├─ 成功 → 返回结果
    │
    └─ 失败（overflow）
         │
         ├─ 尝试 1: 截断 tool_result
         │    ├─ 成功 → 返回结果
         │    └─ 失败
         │         │
         │         └─ 尝试 2: 压缩历史
         │              ├─ 成功 → 返回结果
         │              └─ 失败 → RuntimeError
```

---

*文档生成时间：2026-03-12*