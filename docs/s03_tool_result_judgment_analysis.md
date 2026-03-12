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

---

## 二、核心代码分析

### 2.1 完整代码

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

### 2.2 判断条件分解

复合条件包含 **6 个子条件**，全部为 `True` 时执行合并操作：

| 序号 | 条件表达式 | 含义 |
|------|-----------|------|
| C1 | `messages` | 消息列表存在且不为空 |
| C2 | `messages[-1]["role"] == "user"` | 最后一条消息是 user 角色 |
| C3 | `isinstance(messages[-1]["content"], list)` | content 字段是列表类型 |
| C4 | `messages[-1]["content"]` | content 列表不为空 |
| C5 | `isinstance(messages[-1]["content"][0], dict)` | content 第一个元素是字典 |
| C6 | `messages[-1]["content"][0].get("type") == "tool_result"` | 第一个元素的 type 是 tool_result |

### 2.3 条件链的短路求值

Python 的 `and` 运算符采用短路求值，条件按顺序依次检查：

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

## 三、场景矩阵与处理结果

### 3.1 场景分类

根据 6 个条件的组合，共有 **64 种理论组合**，但实际有效的场景如下：

| 场景 | C1 | C2 | C3 | C4 | C5 | C6 | 处理方式 | 典型情况 |
|------|----|----|----|----|----|----|----------|----------|
| 场景 A | ❌ | - | - | - | - | - | 创建新消息 | 消息列表为空 |
| 场景 B | ✅ | ❌ | - | - | - | - | 创建新消息 | 最后消息是 assistant |
| 场景 C | ✅ | ✅ | ❌ | - | - | - | 创建新消息 | user 消息的 content 是字符串 |
| 场景 D | ✅ | ✅ | ✅ | ❌ | - | - | 创建新消息 | user 消息的 content 是空列表 |
| 场景 E | ✅ | ✅ | ✅ | ✅ | ❌ | - | 创建新消息 | content 首元素不是字典 |
| 场景 F | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 创建新消息 | 首元素不是 tool_result |
| 场景 G | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 合并消息 | 连续的 tool_result |

### 3.2 场景详解

---

#### 场景 A：消息列表为空

**条件状态**：`C1 = False`（`messages` 为空列表）

**JSONL 输入**：
```json
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312200.0}
```

**当前 messages 状态**：
```python
messages = []
```

**处理结果**：
```python
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：JSONL 文件以 tool_result 开头（异常但可能），直接创建新的 user 消息。

---

#### 场景 B：最后消息是 assistant

**条件状态**：`C1 = True, C2 = False`

**JSONL 输入**：
```json
{"type": "assistant", "content": [{"type": "text", "text": "我来帮您查询"}], "ts": 1705312200.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312201.0}
```

**处理过程**：
1. 处理 assistant 记录后：
```python
messages = [
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "我来帮您查询"}]
    }
]
```

2. 处理 tool_result：`C2` 检查 `messages[-1]["role"]` 为 `"assistant"`，条件为 False

**最终 messages**：
```python
messages = [
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "我来帮您查询"}]
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：标准的单工具调用流程，assistant 回复后跟随 tool_result。

---

#### 场景 C：user 消息的 content 是字符串

**条件状态**：`C1 = True, C2 = True, C3 = False`

**JSONL 输入**：
```json
{"type": "user", "content": "普通用户消息", "ts": 1705312200.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312201.0}
```

**处理过程**：
1. 处理 user 记录后：
```python
messages = [
    {
        "role": "user",
        "content": "普通用户消息"  # 字符串类型
    }
]
```

2. 处理 tool_result：`C3` 检查 `isinstance(messages[-1]["content"], list)` 为 False

**最终 messages**：
```python
messages = [
    {
        "role": "user",
        "content": "普通用户消息"
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：普通用户输入后出现 tool_result（可能是 JSONL 记录错误），创建新消息避免覆盖。

---

#### 场景 D：content 是空列表

**条件状态**：`C1 = True, C2 = True, C3 = True, C4 = False`

**JSONL 输入**：
```json
{"type": "user", "content": [], "ts": 1705312200.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312201.0}
```

**处理过程**：
1. 处理 user 记录后：
```python
messages = [
    {
        "role": "user",
        "content": []  # 空列表
    }
]
```

2. 处理 tool_result：`C4` 检查 `messages[-1]["content"]` 为空列表，布尔值为 False

**最终 messages**：
```python
messages = [
    {
        "role": "user",
        "content": []
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：虽然 content 是列表，但为空，无法检查首元素，创建新消息。

---

#### 场景 E：content 首元素不是字典

**条件状态**：`C1 = True, C2 = True, C3 = True, C4 = True, C5 = False`

**JSONL 输入**：
```json
{"type": "user", "content": ["纯文本内容"], "ts": 1705312200.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312201.0}
```

**处理过程**：
1. 处理 user 记录后：
```python
messages = [
    {
        "role": "user",
        "content": ["纯文本内容"]  # 列表中是字符串，不是字典
    }
]
```

2. 处理 tool_result：`C5` 检查 `isinstance(messages[-1]["content"][0], dict)` 为 False

**最终 messages**：
```python
messages = [
    {
        "role": "user",
        "content": ["纯文本内容"]
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：非标准格式的 user 消息，创建新消息确保安全。

---

#### 场景 F：首元素不是 tool_result

**条件状态**：`C1-5 = True, C6 = False`

**JSONL 输入**：
```json
{"type": "assistant", "content": [{"type": "text", "text": "请继续"}], "ts": 1705312200.0}
{"type": "user", "content": [{"type": "text", "text": "用户继续输入"}], "ts": 1705312201.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "结果1", "ts": 1705312202.0}
```

**处理过程**：
1. 处理完前两条后：
```python
messages = [
    {"role": "assistant", "content": [{"type": "text", "text": "请继续"}]},
    {"role": "user", "content": [{"type": "text", "text": "用户继续输入"}]}  # type 是 text，不是 tool_result
]
```

2. 处理 tool_result：`C6` 检查 `messages[-1]["content"][0].get("type") == "tool_result"` 为 False

**最终 messages**：
```python
messages = [
    {"role": "assistant", "content": [{"type": "text", "text": "请继续"}]},
    {"role": "user", "content": [{"type": "text", "text": "用户继续输入"}]},
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_001",
                "content": "结果1"
            }
        ]
    }
]
```

**说明**：前一条 user 消息是普通文本，不能合并 tool_result。

---

#### 场景 G：连续的 tool_result（核心场景）

**条件状态**：`C1-6 全部为 True`

**JSONL 输入**：
```json
{"type": "assistant", "content": [{"type": "text", "text": "我来查询"}], "ts": 1705312200.0}
{"type": "tool_use", "tool_use_id": "call_001", "name": "read_file", "input": {"file_path": "a.txt"}, "ts": 1705312201.0}
{"type": "tool_use", "tool_use_id": "call_002", "name": "read_file", "input": {"file_path": "b.txt"}, "ts": 1705312201.0}
{"type": "tool_result", "tool_use_id": "call_001", "content": "文件A内容", "ts": 1705312202.0}
{"type": "tool_result", "tool_use_id": "call_002", "content": "文件B内容", "ts": 1705312202.5}
```

**处理过程**：

1. 处理 assistant 消息后：
```python
messages = [
    {"role": "assistant", "content": [{"type": "text", "text": "我来查询"}]}
]
```

2. 处理第一个 tool_use（合并到 assistant）：
```python
messages = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来查询"},
            {"type": "tool_use", "id": "call_001", "name": "read_file", "input": {"file_path": "a.txt"}}
        ]
    }
]
```

3. 处理第二个 tool_use（继续合并）：
```python
messages = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来查询"},
            {"type": "tool_use", "id": "call_001", "name": "read_file", "input": {"file_path": "a.txt"}},
            {"type": "tool_use", "id": "call_002", "name": "read_file", "input": {"file_path": "b.txt"}}
        ]
    }
]
```

4. 处理第一个 tool_result（创建新 user 消息）：
   - `C1 = True`（messages 非空）
   - `C2 = False`（最后是 assistant）
   
```python
messages = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来查询"},
            {"type": "tool_use", "id": "call_001", "name": "read_file", "input": {"file_path": "a.txt"}},
            {"type": "tool_use", "id": "call_002", "name": "read_file", "input": {"file_path": "b.txt"}}
        ]
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_001", "content": "文件A内容"}
        ]
    }
]
```

5. 处理第二个 tool_result（**合并**到现有 user 消息）：
   - `C1 = True`（messages 非空）
   - `C2 = True`（最后是 user）
   - `C3 = True`（content 是列表）
   - `C4 = True`（content 非空）
   - `C5 = True`（首元素是字典）
   - `C6 = True`（首元素 type 是 tool_result）

```python
messages = [
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我来查询"},
            {"type": "tool_use", "id": "call_001", "name": "read_file", "input": {"file_path": "a.txt"}},
            {"type": "tool_use", "id": "call_002", "name": "read_file", "input": {"file_path": "b.txt"}}
        ]
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_001", "content": "文件A内容"},
            {"type": "tool_result", "tool_use_id": "call_002", "content": "文件B内容"}  # 合并添加
        ]
    }
]
```

**说明**：这是最核心的场景——多工具并行调用后，所有 tool_result 合并到同一个 user 消息中。

---

## 四、与 SessionStore 完整实现的关系

### 4.1 数据流向

```
用户输入 / 工具调用
        │
        ▼
┌─────────────────────────────────────┐
│        save_turn() / save_tool_result()       │
│        写入 JSONL 记录                          │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│        JSONL 文件存储                          │
│        workspace/.sessions/agents/{id}/        │
│        sessions/{session_id}.jsonl             │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│        load_session() → _rebuild_history()     │
│        重建 API 消息列表                        │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│        API 调用 (guard_api_call)               │
│        符合 Anthropic/OpenAI 格式要求           │
└─────────────────────────────────────┘
```

### 4.2 写入阶段（save_tool_result）

```python
def save_tool_result(self, tool_use_id: str, name: str,
                     tool_input: dict, result: str) -> None:
    if not self.current_session_id:
        return
    ts = time.time()
    # 先写入 tool_use 记录
    self.append_transcript(self.current_session_id, {
        "type": "tool_use",
        "tool_use_id": tool_use_id,
        "name": name,
        "input": tool_input,
        "ts": ts,
    })
    # 再写入 tool_result 记录
    self.append_transcript(self.current_session_id, {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": result,
        "ts": ts,
    })
```

**关键点**：
- `tool_use` 和 `tool_result` 作为**独立记录**写入
- 两者通过 `tool_use_id` 关联
- 时间戳相同，便于追踪

### 4.3 读取阶段（_rebuild_history）

重建时需要将独立的 JSONL 记录转换为符合 API 规范的消息结构：

```
JSONL 记录序列:
  tool_use(call_001) → tool_use(call_002) → tool_result(call_001) → tool_result(call_002)

重建后消息结构:
  assistant: [tool_use(call_001), tool_use(call_002)]
  user: [tool_result(call_001), tool_result(call_002)]
```

### 4.4 为什么需要复杂的判断条件

1. **API 兼容性**：Anthropic API 要求消息严格交替，多个 tool_result 必须合并
2. **数据完整性**：JSONL 可能损坏或被手动编辑，需要防御性检查
3. **类型安全**：content 可能是字符串或列表，需要分别处理

---

## 五、边界情况与错误处理

### 5.1 JSONL 解析错误

```python
try:
    record = json.loads(line)
except json.JSONDecodeError:
    continue  # 跳过无效行
```

### 5.2 类型字段缺失

```python
rtype = record.get("type")  # 使用 get 避免 KeyError
```

### 5.3 字段缺失处理

```python
result_block = {
    "type": "tool_result",
    "tool_use_id": record["tool_use_id"],  # 如果缺失会抛出 KeyError
    "content": record["content"],
}
```

**注意**：此处假设 JSONL 格式正确，依赖写入阶段的规范化。

---

## 六、最佳实践

### 6.1 添加调试日志

```python
def _rebuild_history(self, path: Path) -> list[dict]:
    messages: list[dict] = []
    # ...
    
    elif rtype == "tool_result":
        # 调试：输出条件检查结果
        conditions = {
            "C1_messages_nonempty": bool(messages),
            "C2_last_is_user": messages[-1]["role"] == "user" if messages else False,
            "C3_content_is_list": isinstance(messages[-1]["content"], list) if messages else False,
            # ...
        }
        if all(conditions.values()):
            messages[-1]["content"].append(result_block)
        else:
            messages.append({"role": "user", "content": [result_block]})
```

### 6.2 单元测试覆盖

```python
def test_tool_result_merge():
    """测试连续 tool_result 合并"""
    store = SessionStore()
    # 构造测试 JSONL 内容
    # ...
    messages = store._rebuild_history(test_path)
    
    # 验证：所有 tool_result 在同一个 user 消息中
    assert len(messages) == 2  # assistant + user
    assert messages[1]["role"] == "user"
    assert len(messages[1]["content"]) == 2  # 两个 tool_result
```

---

## 七、总结

### 7.1 条件判断的核心逻辑

| 最终行为 | 触发条件 |
|----------|----------|
| **合并消息** | 前 6 个条件全部为 True |
| **创建新消息** | 任意一个条件为 False |

### 7.2 设计原则

1. **防御性编程**：层层检查，避免异常
2. **API 兼容**：符合 Anthropic 消息格式规范
3. **数据安全**：即使 JSONL 损坏也能部分恢复

### 7.3 与整体架构的关系

```
SessionStore
├── 写入层：save_turn(), save_tool_result() → 独立 JSONL 记录
├── 存储层：JSONL 文件 → 持久化
└── 读取层：_rebuild_history() → API 消息重建
         └── tool_result 判断条件 → 消息合并逻辑
```

---

*文档生成时间：2026-03-12*