# s02_tool_use.py 深度解析

> "Give the model hands" —— 让模型拥有双手

---

## 一、设计思想

### 1.1 核心哲学

```python
# Agent 循环本身没变 -- 我们只是加了一张调度表
TOOL_HANDLERS = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
}

# stop_reason == "tool_use" 时，从调度表查函数，执行，把结果塞回去
if finish_reason == "tool_calls":
    result = TOOL_HANDLERS[tool_name](**tool_input)
    messages.append({"role": "tool", "content": result})
```

**核心认知**：Agent 的能力边界不是由代码复杂度决定的，而是由**工具集**决定的。模型只是推理引擎，工具才是真正的手脚。

### 1.2 双表架构

```
┌─────────────────────────────────────────────────────────────┐
│                      双表分离设计                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   TOOLS 数组           →   告诉模型 "你有哪些工具可用"       │
│   (传给 API 的 schema)                                       │
│                                                             │
│   TOOL_HANDLERS 字典   →   告诉代码 "收到调用时执行什么函数"  │
│   (本地调度表)                                               │
│                                                             │
│   两者通过 name 字段关联                                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**设计意图**：
- **解耦**：模型看到的是声明式 schema，代码执行的是命令式函数
- **可扩展**：新增工具只需两步——添加 schema + 注册 handler
- **类型安全**：schema 定义了参数类型，模型必须遵守

### 1.3 循环嵌套策略

```python
while True:  # 外层：用户输入循环
    messages.append({"role": "user", "content": user_input})
    
    while True:  # 内层：工具调用循环
        response = client.chat.completions.create(...)
        
        if finish_reason == "stop":
            break  # 跳出内层，等待下一次用户输入
            
        elif finish_reason == "tool_calls":
            # 执行工具，追加结果
            messages.append({"role": "tool", "content": result})
            continue  # 继续内层，让模型看到结果
```

**设计意图**：模型可能连续调用多个工具（如：先读文件，再编辑），内层循环确保所有工具执行完毕后才返回用户。

### 1.4 安全优先原则

```python
# 路径穿越防护
def safe_path(raw: str) -> Path:
    target = (WORKDIR / raw).resolve()
    if not str(target).startswith(str(WORKDIR)):
        raise ValueError("Path traversal blocked")
    return target

# 危险命令过滤
dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]

# 输出截断
MAX_TOOL_OUTPUT = 50000
```

**设计意图**：工具赋予模型强大能力的同时，也带来风险。安全措施是必须的，而非可选的。

---

## 二、实现机制

### 2.1 工具定义 Schema

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",           # 工具名（与 handler 关联）
            "description": "...",      # 告诉模型何时使用
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default 30.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    # ... 其他工具
]
```

**机制**：
- `name`: 唯一标识，连接 schema 和 handler
- `description`: 模型决策的关键——描述清晰度直接影响使用正确性
- `parameters`: JSON Schema 格式，模型自动生成符合规范的参数

### 2.2 工具调用流程

```
┌──────────────────────────────────────────────────────────────────┐
│                        工具调用完整流程                           │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. 模型决策                                                     │
│     └─> finish_reason == "tool_calls"                           │
│     └─> tool_calls = [{"id": "call_xxx", "function": {...}}]    │
│                                                                  │
│  2. 参数解析                                                     │
│     └─> function_args = json.loads(tool_call.function.arguments) │
│                                                                  │
│  3. 调度执行                                                     │
│     └─> handler = TOOL_HANDLERS[function_name]                  │
│     └─> result = handler(**function_args)                       │
│                                                                  │
│  4. 结果回传                                                     │
│     └─> messages.append({                                       │
│             "role": "tool",                                      │
│             "tool_call_id": tool_call.id,                        │
│             "content": result                                    │
│         })                                                       │
│                                                                  │
│  5. 继续推理                                                     │
│     └─> 内层循环继续，模型看到工具结果后决定下一步              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 四个核心工具

#### 2.3.1 bash - 命令执行

```python
def tool_bash(command: str, timeout: int = 30) -> str:
    # 安全检查
    dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
    for pattern in dangerous:
        if pattern in command:
            return f"Error: Refused to run dangerous command"
    
    # 执行
    result = subprocess.run(
        command, shell=True, 
        capture_output=True, text=True,
        timeout=timeout, cwd=str(WORKDIR)
    )
    
    # 返回 stdout + stderr + exit_code
    return truncate(output)
```

**特点**：
- 危险命令黑名单
- 超时控制（默认30秒）
- stdout/stderr 合并返回
- 工作目录限制

#### 2.3.2 read_file - 文件读取

```python
def tool_read_file(file_path: str) -> str:
    target = safe_path(file_path)  # 路径穿越防护
    if not target.exists():
        return f"Error: File not found"
    content = target.read_text(encoding="utf-8")
    return truncate(content)
```

**特点**：
- 相对路径（相对于 WORKDIR）
- 自动截断大文件
- UTF-8 编码

#### 2.3.3 write_file - 文件写入

```python
def tool_write_file(file_path: str, content: str) -> str:
    target = safe_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)  # 自动创建父目录
    target.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content)} chars"
```

**特点**：
- 自动创建父目录
- 覆盖模式（非追加）
- 成功返回确认信息

#### 2.3.4 edit_file - 精确替换

```python
def tool_edit_file(file_path: str, old_string: str, new_string: str) -> str:
    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    
    if count == 0:
        return "Error: old_string not found. Make sure it matches exactly."
    if count > 1:
        return "Error: old_string found multiple times. Must be unique."
    
    new_content = content.replace(old_string, new_string, 1)
    target.write_text(new_content, encoding="utf-8")
    return f"Successfully edited {file_path}"
```

**特点**：
- **唯一性要求**：old_string 必须只出现一次
- 精确匹配（包括空白字符）
- 这是与 OpenClaw edit 工具一致的语义

### 2.4 消息格式转换

```python
# 工具调用时需要特殊格式的消息
# OpenAI API 要求：

# 1. assistant 消息包含 tool_calls
{
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "call_xxx",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path": "test.txt"}'
        }
    }]
}

# 2. 工具结果消息
{
    "role": "tool",
    "tool_call_id": "call_xxx",
    "content": "file contents..."
}
```

**关键**：`tool_call_id` 用于关联调用和结果，模型通过它知道哪个结果对应哪个调用。

---

## 三、主要功能

### 3.1 工具清单

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| `bash` | 执行 shell 命令 | command, timeout |
| `read_file` | 读取文件内容 | file_path |
| `write_file` | 写入文件 | file_path, content |
| `edit_file` | 精确替换 | file_path, old_string, new_string |

### 3.2 使用示例

#### 示例 1：查看文件列表
```
You > 列出当前目录的文件

[tool_calls: 1]
[tool: bash] ls -la
Assistant: 当前目录包含以下文件：
- README.md (文档)
- main.py (主程序)
...
```

#### 示例 2：读取并修改文件
```
You > 读取 config.json 并把 debug 改成 false

[tool_calls: 1]
[tool: read_file] config.json
[tool_calls: 1]
[tool: edit_file] config.json (replace 5 chars)
Assistant: 已将 config.json 中的 "debug": true 改为 "debug": false
```

#### 示例 3：创建新文件
```
You > 创建一个 hello.py，输出 Hello World

[tool_calls: 1]
[tool: write_file] hello.py
Assistant: 已创建 hello.py，内容为 print("Hello World")
```

### 3.3 安全特性

| 特性 | 实现方式 | 防护目标 |
|------|----------|----------|
| 路径穿越 | `safe_path()` 检查 | 防止访问工作目录外文件 |
| 危险命令 | 黑名单过滤 | 防止 rm -rf /, mkfs 等 |
| 输出截断 | 50000 字符限制 | 防止大输出撑爆上下文 |
| 超时控制 | subprocess timeout | 防止命令无限挂起 |

### 3.4 错误传递

```python
# 错误通过返回值传递给模型
return f"Error: File not found: {file_path}"
return f"Error: Command timed out after {timeout}s"
return f"Error: old_string not found in file"
```

**设计**：工具错误不抛异常，而是返回错误信息字符串。模型会看到错误并决定如何处理（如重试或报告用户）。

---

## 四、与 s01 的演进关系

### 4.1 代码对比

| 方面 | s01 Agent Loop | s02 Tool Use |
|------|----------------|--------------|
| 循环结构 | 单层 while | 双层 while（外层用户，内层工具） |
| API 参数 | 无 tools | `tools=TOOLS` |
| finish_reason 分支 | 仅处理 "stop" | 新增 "tool_calls" 分支 |
| 消息类型 | user, assistant | 新增 tool, tool_calls |
| 消息累积 | 线性追加 | 工具结果追加后继续循环 |
| 功能 | 纯对话 | 可执行命令、操作文件 |

### 4.2 改动量分析

```python
# s01 → s02 新增代码量
新增函数: 5 个
  - safe_path()
  - truncate()
  - tool_bash()
  - tool_read_file()
  - tool_write_file()
  - tool_edit_file()
  - process_tool_call()

新增数据: 2 个
  - TOOLS (schema)
  - TOOL_HANDLERS (dispatch table)

修改函数: 1 个
  - agent_loop() 新增内层循环和工具调用分支
```

**核心洞察**：约 300 行新增代码，但 `agent_loop()` 的主体结构完全保留。这是"渐进式演进"的最佳示范。

### 4.3 架构图对比

**s01 架构**:
```
User --> LLM --> stop_reason == "stop"?
                        |
                     打印回复
```

**s02 架构**:
```
User --> LLM --> finish_reason == "stop"?
                        |
                   "tool_calls"?
                        |
                TOOL_HANDLERS[name](**input)
                        |
                tool_result --> back to LLM
                        |
                 finish_reason == "stop"?
                        |
                     打印回复
```

### 4.4 演进模式总结

```
┌─────────────────────────────────────────────────────────────┐
│                   渐进式演进的核心原则                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. 保留骨架：agent_loop() 的 while True 结构不变           │
│                                                             │
│  2. 添加分支：原有 finish_reason 分支保留，新增分支         │
│                                                             │
│  3. 扩展状态：messages[] 新增 tool 角色消息                 │
│                                                             │
│  4. 模块化工具：工具函数独立，通过调度表连接                │
│                                                             │
│  这种模式将在后续章节重复：                                 │
│  - s03 添加持久化层，不改循环                               │
│  - s04 添加通道层，不改循环                                 │
│  - s06 添加智能层，不改循环                                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、代码结构图

```
s02_tool_use.py (577 行)
│
├── 模块级配置
│   ├── 导入 (os, sys, subprocess, pathlib, dotenv, openai)
│   ├── 环境变量加载
│   ├── API 客户端初始化
│   ├── SYSTEM_PROMPT (引导模型使用工具)
│   ├── MAX_TOOL_OUTPUT (输出截断限制)
│   └── WORKDIR (工作目录锁定)
│
├── ANSI 颜色定义
│   ├── CYAN, GREEN, YELLOW, RED, DIM, RESET, BOLD
│   ├── colored_prompt()
│   ├── print_assistant()
│   ├── print_tool()
│   └── print_info()
│
├── 安全辅助函数 (新增)
│   ├── safe_path(raw) → Path        # 路径穿越防护
│   └── truncate(text, limit) → str  # 输出截断
│
├── 工具实现 (新增)
│   ├── tool_bash(command, timeout)       # 执行命令
│   ├── tool_read_file(file_path)         # 读取文件
│   ├── tool_write_file(file_path, content) # 写入文件
│   └── tool_edit_file(file_path, old, new)  # 编辑文件
│
├── 工具定义
│   ├── TOOLS (list[dict])           # 传给 API 的 schema
│   │   ├── bash schema
│   │   ├── read_file schema
│   │   ├── write_file schema
│   │   └── edit_file schema
│   │
│   └── TOOL_HANDLERS (dict[str, Callable])  # 本地调度表
│       ├── "bash" → tool_bash
│       ├── "read_file" → tool_read_file
│       ├── "write_file" → tool_write_file
│       └── "edit_file" → tool_edit_file
│
├── 工具调用处理 (新增)
│   └── process_tool_call(name, input) → str
│       ├── 查找 handler
│       ├── 调用 handler(**input)
│       └── 异常捕获 → 返回错误字符串
│
├── 核心: Agent 循环 (扩展)
│   └── agent_loop()
│       ├── 初始化 messages[]
│       ├── 打印欢迎信息（含工具列表）
│       │
│       └── while True:  # 外层：用户输入
│           ├── 获取用户输入
│           ├── 追加 user 消息
│           │
│           └── while True:  # 内层：工具调用 (新增)
│               ├── API 调用 (tools=TOOLS)
│               ├── 获取 assistant_message
│               │
│               ├── finish_reason == "stop":
│               │   ├── 打印回复
│               │   └── break (跳出内层)
│               │
│               ├── finish_reason == "tool_calls":
│               │   ├── 解析 tool_calls
│               │   ├── for each tool_call:
│               │   │   ├── 解析参数
│               │   │   ├── process_tool_call()
│               │   │   └── 追加 tool 消息
│               │   └── continue (继续内层)
│               │
│               └── else: (其他情况)
│                   └── break
│
└── 入口
    └── main()
        └── 检查 API 密钥 → agent_loop()
```

---

## 六、关键认知

### 6.1 工具即能力

```
没有工具的 Agent = 纯文本推理
有了工具的 Agent = 可以操作世界的 Agent
```

模型的能力边界由工具集定义。添加一个工具，就扩展一份能力。

### 6.2 调度表模式

```python
# 这是一个可复用的模式
TOOL_HANDLERS = {
    "tool_name": handler_function,
    ...
}

# 调度逻辑极简
result = TOOL_HANDLERS[name](**args)
```

这个模式将在后续章节重复出现：
- s06 的 skills
- s07 的 cron jobs
- 生产环境的各种能力扩展

### 6.3 内层循环的必要性

```python
# 为什么需要内层循环？
# 因为模型可能这样思考：

# 第1轮: "让我先读取文件"
tool_calls: [read_file("config.json")]

# 第2轮: "看到了，现在我要修改"
tool_calls: [edit_file("config.json", "old", "new")]

# 第3轮: "完成了"
content: "已成功修改配置文件"
finish_reason: "stop"
```

内层循环确保多步工具调用在一次用户交互中完成。

### 6.4 错误即反馈

```python
# 不要 raise Exception
# 而是返回错误字符串
return f"Error: File not found"

# 模型会看到错误并决定：
# 1. 重试（换个路径）
# 2. 报告用户
# 3. 尝试其他方案
```

这让模型有机会自主处理错误，而非中断对话。

---

## 七、后续演进预告

| 本节元素 | 后续演进 |
|----------|----------|
| `TOOL_HANDLERS` | s06 Intelligence 扩展为 skills + prompts |
| `messages[]` | s03 Sessions 持久化到 JSONL |
| 工具执行 | s05 Gateway 添加工具调用日志 |
| 安全检查 | s09 Resilience 添加重试和容错 |
| 输出截断 | s03 Sessions 添加上下文压缩 |

---

> **作者注**：这一节的精髓在于"不变中求变"——agent_loop 的主体结构完全不变，只是新增了一个分支和一张调度表。这种渐进式演进是整个 claw0 项目的核心设计哲学。