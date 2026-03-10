# s01_agent_loop.py 深度解析

> Agent 循环 —— 整个项目的基石

---

## 一、设计思想

### 1.1 核心哲学

```python
while True:
    user_input = input()
    messages.append({"role": "user", "content": user_input})
    response = client.chat.completions.create(messages=messages)
    
    if response.finish_reason == "stop":
        print(response.content)
    elif response.finish_reason == "tool_calls":
        # 执行工具... (下一节)
```

**核心认知**：Agent 的本质就是 `while True` + `stop_reason`。这个极简循环是后续所有复杂功能（工具、会话、路由、智能等）的基础。

### 1.2 状态累积模式

```python
messages: list[dict] = []  # 唯一的状态存储

# 每轮追加
messages.append({"role": "user", "content": user_input})
# ... API调用 ...
messages.append({"role": "assistant", "content": assistant_text})
```

**设计意图**：`messages[]` 是唯一的对话状态。每次 API 调用时，LLM 都能看到完整历史，实现多轮对话记忆。

### 1.3 预留扩展点

```python
elif finish_reason == "tool_calls":
    print_info("[finish_reason=tool_calls] 本节没有可用工具。")
    print_info("参见 s02_tool_use.py 了解工具支持。")
```

**设计意图**：代码结构已预留 `tool_calls` 分支，第 02 节只需填充此分支，无需改动循环结构。

---

## 二、实现机制

### 2.1 REPL 交互循环

```python
def agent_loop() -> None:
    messages: list[dict] = []
    
    while True:
        # Step 1: 获取用户输入
        user_input = input(colored_prompt()).strip()
        
        # Step 2: 追加到历史
        messages.append({"role": "user", "content": user_input})
        
        # Step 3: 调用 LLM
        response = client.chat.completions.create(...)
        
        # Step 4: 处理响应
        if finish_reason == "stop":
            print_assistant(assistant_text)
            messages.append({"role": "assistant", "content": assistant_text})
```

**机制**：标准 REPL（Read-Eval-Print-Loop），支持 `quit`/`exit` 和 `Ctrl+C` 退出。

### 2.2 API 调用封装

```python
# 系统提示词作为第一条消息
api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

response = client.chat.completions.create(
    model=MODEL_ID,
    max_tokens=8096,
    messages=api_messages,
)
```

**关键**：使用 OpenAI SDK 调用千问 API，系统提示词动态插入消息列表头部

### 2.3 错误恢复

```python
try:
    response = client.chat.completions.create(...)
except Exception as exc:
    print(f"API Error: {exc}")
    messages.pop()  # 回滚
    continue
```

**机制**：API 失败时弹出最后一条消息，保持状态一致性

### 2.4 响应处理分支

```python
finish_reason = response.choices[0].finish_reason

if finish_reason == "stop":
    # 正常结束
elif finish_reason == "tool_calls":
    # 预留工具分支
else:
    # 其他情况
```

**分支**：三种停止原因对应三种处理，每种都将回复追加到 messages

---

## 三、主要功能

### 3.1 多轮对话

**示例**：
```
You > 法国的首都是哪里?
Assistant: 法国的首都是巴黎。

You > 它的人口是多少?
Assistant: 巴黎的人口约为216万（2020年数据）。
```

**实现**：messages 列表累积所有对话历史，每轮 API 调用都发送完整历史。

### 3.2 上下文记忆

**记忆范围**：从程序开始到当前的所有对话，受限于 max_tokens 和模型上下文窗口。

### 3.3 优雅退出

- 输入 `quit` 或 `exit`
- 按 `Ctrl+C`
- 按 `Ctrl+D` (EOF)

### 3.4 配置管理

**配置项**：
- `DASHSCOPE_API_KEY`: 阿里云百炼 API 密钥
- `MODEL_ID`: 模型名称（默认 qwen-plus）
- `DASHSCOPE_BASE_URL`: API 端点（可选）

---

## 四、代码结构

```
s01_agent_loop.py
├── 模块级配置（导入、环境变量、客户端初始化）
├── ANSI颜色定义（终端美化）
├── agent_loop()          # 核心循环
│   ├── 初始化messages
│   ├── 打印欢迎信息
│   └── while True循环
│       ├── 获取输入
│       ├── 追加用户消息
│       ├── 调用API
│       └── 处理响应
│           ├── stop: 正常回复
│           ├── tool_calls: 预留
│           └── 其他: 部分回复
└── main()                # 入口函数
    └── 检查API密钥 → 启动循环
```

---

## 五、与后续章节的关联

| 本节 | 后续章节 | 演进说明 |
|------|----------|----------|
| `messages[]` | s03 Sessions | 从内存到 JSONL 持久化 |
| `tool_calls`分支 | s02 Tool Use | 填充工具调用逻辑 |
| `SYSTEM_PROMPT` | s06 Intelligence | 从硬编码到动态组装 |
| 错误处理 | s09 Resilience | 简单 try/except → 3层重试洋葱 |

---

## 六、关键认知

1. **Agent 的本质**：不是复杂的 AI 算法，而是循环+状态管理
2. **消息即状态**：messages[] 是唯一的真相来源
3. **可扩展性**：简单的循环结构可以承载无限复杂的功能
4. **错误隔离**：API 失败只影响当前轮次，不破坏整体状态

> **作者注**：这一节是整个项目的基石。理解了这个循环，就能理解后续所有章节的演进都是在这个基础上添加层，而不是改变基础结构。
