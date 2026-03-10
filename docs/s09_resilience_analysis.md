# S09 Resilience 设计文档：三层重试洋葱

## 一、设计思想

### 1.1 核心理念

> "When one call fails, rotate and retry." —— 当一次调用失败，轮换并重试。

s09_resilience 构建了一个**三层重试洋葱（3-Layer Retry Onion）**架构，为每次 agent 执行提供全方位的故障容错能力。每一层处理不同类别的失败场景：

```
┌─────────────────────────────────────────────────────────────┐
│                    Layer 1: 认证轮换                          │
│          在多个 API Key 配置之间轮转，跳过冷却中的配置          │
├─────────────────────────────────────────────────────────────┤
│                    Layer 2: 溢出恢复                          │
│            上下文溢出时压缩消息，保持对话连续性                  │
├─────────────────────────────────────────────────────────────┤
│                    Layer 3: 工具调用循环                       │
│           标准的 while True + stop_reason 核心循环            │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 架构意图

```
                         用户请求
                             │
                             ▼
┌────────────────────────────────────────────────────────────────┐
│                    ResilienceRunner.run()                       │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │              Layer 1: Auth Rotation (认证轮换)              │ │
│  │  ┌─────────────────────────────────────────────────────┐   │ │
│  │  │           Layer 2: Overflow Recovery (溢出恢复)      │   │ │
│  │  │  ┌───────────────────────────────────────────────┐  │   │ │
│  │  │  │       Layer 3: Tool-Use Loop (工具循环)        │  │   │ │
│  │  │  │                                                 │  │   │ │
│  │  │  │   while True:                                  │  │   │ │
│  │  │  │       response = api_call()                    │  │   │ │
│  │  │  │       if stop: return                          │  │   │ │
│  │  │  │       if tool_calls: execute, continue         │  │   │ │
│  │  │  │                                                 │  │   │ │
│  │  │  └───────────────────────────────────────────────┘  │   │ │
│  │  │           │ overflow error                          │   │ │
│  │  │           ▼                                          │   │ │
│  │  │       compact_history() → retry                     │   │ │
│  │  └─────────────────────────────────────────────────────┘   │ │
│  │           │ auth/rate/timeout error                        │ │
│  │           ▼                                                │ │
│  │       mark_failure() → next profile                       │ │
│  └───────────────────────────────────────────────────────────┘ │
│              │ all profiles exhausted                           │
│              ▼                                                  │
│         fallback models (备选模型)                              │
└────────────────────────────────────────────────────────────────┘
                             │
                             ▼
                        返回结果 / 抛出异常
```

### 1.3 设计原则

1. **故障分类驱动策略** —— 不同的错误类型触发不同的恢复动作
2. **冷却感知轮换** —— 失败的配置进入冷却期，自动跳过
3. **渐进式降级** —— 从主配置到备选配置，从主模型到备选模型
4. **可观测性** —— 提供详细的统计和状态信息

---

## 二、实现机制

### 2.1 失败分类系统

#### FailoverReason 枚举

```python
class FailoverReason(Enum):
    rate_limit = "rate_limit"   # 速率限制 (429)
    auth = "auth"               # 认证失败 (401)
    timeout = "timeout"         # 请求超时
    billing = "billing"         # 配额/计费问题 (402)
    overflow = "overflow"       # 上下文窗口溢出
    unknown = "unknown"         # 未知错误
```

#### 分类逻辑

```python
def classify_failure(exc: Exception) -> FailoverReason:
    """通过异常消息字符串进行分类。"""
    msg = str(exc).lower()
    
    if "rate" in msg or "429" in msg:
        return FailoverReason.rate_limit
    if "auth" in msg or "401" in msg or "key" in msg:
        return FailoverReason.auth
    if "timeout" in msg or "timed out" in msg:
        return FailoverReason.timeout
    if "billing" in msg or "quota" in msg or "402" in msg:
        return FailoverReason.billing
    if "context" in msg or "token" in msg or "overflow" in msg:
        return FailoverReason.overflow
    
    return FailoverReason.unknown
```

**分类与处理策略对应表**：

| 失败类型 | 重试策略 | 冷却时间 |
|---------|---------|---------|
| overflow | 压缩消息后用相同配置重试 | 不冷却 |
| auth | 跳过此配置，尝试下一个 | 300s |
| rate_limit | 跳过此配置，尝试下一个 | 120s |
| timeout | 跳过此配置，尝试下一个 | 60s |
| billing | 跳过此配置，尝试下一个 | 300s |
| unknown | 跳过此配置，尝试下一个 | 120s |

### 2.2 AuthProfile -- 认证配置管理

#### 数据结构

```python
@dataclass
class AuthProfile:
    """表示一个可轮换使用的 API Key。"""
    name: str              # 可读标签，如 "main-key"
    provider: str          # LLM 提供商，如 "anthropic"
    api_key: str           # 实际的 API Key 字符串
    cooldown_until: float = 0.0    # 冷却结束时间戳
    failure_reason: str | None = None  # 上次失败原因
    last_good_at: float = 0.0      # 上次成功时间戳
```

#### ProfileManager 核心方法

```python
class ProfileManager:
    """管理 AuthProfile 池，支持冷却感知的选择。"""
    
    def select_profile(self) -> AuthProfile | None:
        """返回第一个冷却已过期的配置。"""
        now = time.time()
        for profile in self.profiles:
            if now >= profile.cooldown_until:
                return profile
        return None
    
    def mark_failure(self, profile: AuthProfile, 
                     reason: FailoverReason,
                     cooldown_seconds: float = 300.0) -> None:
        """在失败后将配置置入冷却。"""
        profile.cooldown_until = time.time() + cooldown_seconds
        profile.failure_reason = reason.value
    
    def mark_success(self, profile: AuthProfile) -> None:
        """清除失败状态并记录成功时间。"""
        profile.failure_reason = None
        profile.last_good_at = time.time()
```

### 2.3 ContextGuard -- 上下文保护（简化版）

s09 中的 ContextGuard 是 s03 的简化版本，专门用于 Layer 2 溢出恢复：

```python
class ContextGuard:
    """轻量级上下文溢出保护，用于弹性运行器。"""
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """粗略估算：每 4 个字符约 1 个 token。"""
        return len(text) // 4
    
    def truncate_tool_results(self, messages: list[dict]) -> list[dict]:
        """截断过大的 tool_result 块。"""
    
    def compact_history(self, messages: list[dict],
                        api_client: OpenAI, model: str) -> list[dict]:
        """将前 50% 的消息压缩为 LLM 生成的摘要。"""
```

**压缩策略**：
- 保留最后 20%（最少 4 条）消息不变
- 将前 50% 消息压缩为摘要
- 压缩后的消息格式：
  ```python
  compacted = [
      {"role": "user", "content": "[Previous conversation summary]\n" + summary},
      {"role": "assistant", "content": [{"type": "text", "text": "Understood..."}]},
  ]
  compacted.extend(recent_messages)
  ```

### 2.4 ResilienceRunner -- 三层重试洋葱核心

#### 初始化配置

```python
class ResilienceRunner:
    def __init__(
        self,
        profile_manager: ProfileManager,
        model_id: str,
        fallback_models: list[str] | None = None,
        context_guard: ContextGuard | None = None,
        simulated_failure: SimulatedFailure | None = None,
    ):
        self.profile_manager = profile_manager
        self.model_id = model_id
        self.fallback_models = fallback_models or []
        self.guard = context_guard or ContextGuard()
        
        # 动态计算最大迭代次数
        num_profiles = len(profile_manager.profiles)
        self.max_iterations = min(
            max(BASE_RETRY + PER_PROFILE * num_profiles, 32),
            160,
        )
```

#### 三层重试核心逻辑

```python
def run(self, system: str, messages: list[dict], tools: list[dict]):
    """执行三层重试洋葱。"""
    
    # ========== LAYER 1: Auth Rotation ==========
    for _rotation in range(len(self.profile_manager.profiles)):
        profile = self.profile_manager.select_profile()
        if profile is None:
            break  # 所有配置都在冷却中
        
        api_client = create_client(profile.api_key)
        
        # ========== LAYER 2: Overflow Recovery ==========
        for compact_attempt in range(MAX_OVERFLOW_COMPACTION):
            try:
                # ========== LAYER 3: Tool-Use Loop ==========
                result, messages = self._run_attempt(
                    api_client, self.model_id, system, messages, tools
                )
                self.profile_manager.mark_success(profile)
                return result, messages
                
            except Exception as exc:
                reason = classify_failure(exc)
                
                if reason == FailoverReason.overflow:
                    # 溢出：压缩后重试 Layer 2
                    messages = self.guard.truncate_tool_results(messages)
                    messages = self.guard.compact_history(messages, api_client, model)
                    continue
                
                else:
                    # 其他错误：标记失败，跳出 Layer 2，进入下一个 Layer 1 迭代
                    self.profile_manager.mark_failure(profile, reason, cooldown)
                    break
    
    # ========== Fallback Models ==========
    # 所有主配置耗尽后，尝试备选模型
    for fallback_model in self.fallback_models:
        # ... 尝试备选模型 ...
    
    raise RuntimeError("All profiles and fallback models exhausted.")
```

#### Layer 3 工具调用循环

```python
def _run_attempt(self, api_client, model, system, messages, tools):
    """Layer 3: 标准工具调用循环。"""
    while iteration < self.max_iterations:
        response = api_client.chat.completions.create(
            model=model,
            max_tokens=8096,
            system=system,
            tools=tools,
            messages=current_messages,
        )
        
        current_messages.append({
            "role": "assistant",
            "content": response.choices[0].message.content,
        })
        
        if response.choices[0].finish_reason == "stop":
            return response, current_messages
        
        elif response.choices[0].finish_reason == "tool_calls":
            # 执行工具，追加结果，继续循环
            tool_results = []
            for block in response.choices[0].message.content:
                if block.type == "tool_calls":
                    result = process_tool_call(block.name, block.input)
                    tool_results.append({...})
            current_messages.append({"role": "user", "content": tool_results})
            continue
    
    raise RuntimeError(f"Tool-use loop exceeded {self.max_iterations} iterations")
```

### 2.5 SimulatedFailure -- 模拟故障测试

这是一个创新的设计，允许用户在 REPL 中触发模拟的 API 错误，用于测试三层重试洋葱的行为：

```python
class SimulatedFailure:
    """持有一个待触发的模拟失败，在下次 API 调用时触发。"""
    
    TEMPLATES: dict[str, str] = {
        "rate_limit": "Error code: 429 -- rate limit exceeded",
        "auth": "Error code: 401 -- authentication failed, invalid API key",
        "timeout": "Request timed out after 30s",
        "billing": "Error code: 402 -- billing quota exceeded",
        "overflow": "Error: context window token overflow, too many tokens",
        "unknown": "Error: unexpected internal server error",
    }
    
    def arm(self, reason: str) -> str:
        """为下次 API 调用装备一个失败。"""
        self._pending = reason
        return f"Armed: next API call will fail with '{reason}'"
    
    def check_and_fire(self) -> None:
        """如果已装备，抛出模拟错误并解除装备。"""
        if self._pending is not None:
            reason = self._pending
            self._pending = None
            raise RuntimeError(self.TEMPLATES[reason])
```

**使用方式**：
```
You > /simulate-failure rate_limit
  [resilience] Armed: next API call will fail with 'rate_limit'

You > 你好
  [resilience] Profile 'main-key' -> cooldown 120s (reason: rate_limit)
  [resilience] Rotating to profile 'backup-key'
  Assistant: 你好！...
```

---

## 三、主要功能

### 3.1 故障转移功能

#### 3.1.1 认证配置轮换

```python
# 配置多个 API Key（演示用同一个 key）
profiles = [
    AuthProfile(name="main-key", provider="anthropic", api_key=api_key),
    AuthProfile(name="backup-key", provider="anthropic", api_key=api_key),
    AuthProfile(name="emergency-key", provider="anthropic", api_key=api_key),
]
```

当主配置失败时，自动切换到备用配置。

#### 3.1.2 备选模型降级

```python
fallback_models = [
    "claude-haiku-4-20250514",  # 更便宜/更快的备选模型
]
```

当所有配置对主模型都失败时，尝试备选模型。

### 3.2 REPL 命令系统

| 命令 | 功能 | 示例 |
|------|------|------|
| `/profiles` | 显示所有配置状态 | `/profiles` |
| `/cooldowns` | 显示活动冷却 | `/cooldowns` |
| `/simulate-failure <r>` | 触发模拟失败 | `/simulate-failure rate_limit` |
| `/fallback` | 显示备选模型链 | `/fallback` |
| `/stats` | 显示弹性统计 | `/stats` |
| `/help` | 显示帮助 | `/help` |
| `quit` / `exit` | 退出程序 | `quit` |

### 3.3 统计信息

```python
def get_stats(self) -> dict[str, Any]:
    return {
        "total_attempts": self.total_attempts,    # 总尝试次数
        "total_successes": self.total_successes,  # 成功次数
        "total_failures": self.total_failures,    # 失败次数
        "total_compactions": self.total_compactions,  # 压缩次数
        "total_rotations": self.total_rotations,  # 配置轮换次数
        "max_iterations": self.max_iterations,    # 最大迭代次数
    }
```

### 3.4 工具功能

本节工具集（简化版）：

| 工具 | 功能 | 参数 |
|------|------|------|
| `bash` | 执行 shell 命令 | `command`, `timeout` |
| `read_file` | 读取文件内容 | `file_path` |

---

## 四、与前面章节的演进关系

### 4.1 代码行数演进

```
s01_agent_loop:   ~170 行  ──→  基础循环
s02_tool_use:     ~450 行  ──→  +工具系统
s03_sessions:     ~910 行  ──→  +会话持久化 + 上下文保护
s04_channels:     ~780 行  ──→  +多渠道接入
s05_gateway:      ~625 行  ──→  +路由绑定
s06_intelligence: ~750 行  ──→  +智能体架构
s07_heartbeat:    ~660 行  ──→  +心跳与定时任务
s08_delivery:     ~870 行  ──→  +可靠消息投递
s09_resilience:   ~1130 行 ──→  +三层重试洋葱
```

### 4.2 功能演进对比

| 特性 | s01 | s02 | s03 | s09 |
|------|-----|-----|-----|-----|
| Agent 循环 | ✅ | ✅ | ✅ | ✅ |
| 工具调用 | ❌ | ✅ | ✅ | ✅ |
| 会话持久化 | ❌ | ❌ | ✅ | ❌ (简化版) |
| 上下文保护 | ❌ | ❌ | ✅ | ✅ (简化版) |
| 认证轮换 | ❌ | ❌ | ❌ | ✅ |
| 失败分类 | ❌ | ❌ | ❌ | ✅ |
| 备选模型 | ❌ | ❌ | ❌ | ✅ |
| 模拟测试 | ❌ | ❌ | ❌ | ✅ |

### 4.3 核心代码继承关系

#### 从 s01/s02 继承的 Agent 循环

```python
# s01 基础循环
while True:
    response = client.chat.completions.create(...)
    if finish_reason == "stop":
        break

# s02 工具循环
while True:
    response = client.chat.completions.create(tools=TOOLS, ...)
    if finish_reason == "stop":
        break
    elif finish_reason == "tool_calls":
        # 执行工具，继续循环
        continue

# s09 三层重试洋葱
for profile in profiles:  # Layer 1
    for compact_attempt in range(3):  # Layer 2
        while True:  # Layer 3
            response = client.chat.completions.create(...)
            if finish_reason == "stop":
                return
            elif finish_reason == "tool_calls":
                continue
```

#### 从 s03 继承的 ContextGuard

s09 使用了 s03 中 ContextGuard 的简化版本：

```python
# s03 完整版 ContextGuard
class ContextGuard:
    def guard_api_call(self, ...):  # 三阶段重试包装
        # 第0次：正常调用
        # 第1次：截断工具结果
        # 第2次：压缩历史
    def truncate_tool_result(self, ...):  # 单个结果截断
    def truncate_tool_results(self, ...):  # 批量截断
    def compact_history(self, ...):  # 历史压缩

# s09 简化版 ContextGuard
class ContextGuard:
    def truncate_tool_results(self, ...):  # 批量截断
    def compact_history(self, ...):  # 历史压缩
    # 没有 guard_api_call 包装方法
```

### 4.4 新增组件详解

```
s09 新增代码分布：

ResilienceRunner 类 (~270 行)
├── __init__                      # 初始化与配置
├── run()                         # 三层重试洋葱（核心）
├── _run_attempt()                # Layer 3 工具循环
└── get_stats()                   # 统计信息

ProfileManager 类 (~65 行)
├── select_profile()              # 冷却感知选择
├── select_all_available()        # 获取所有可用配置
├── mark_failure()                # 标记失败并冷却
├── mark_success()                # 标记成功
└── list_profiles()               # 列出状态

AuthProfile 数据类 (~20 行)
└── 字段定义

FailoverReason 枚举 (~10 行)
└── 失败类型定义

classify_failure() (~25 行)
└── 异常分类逻辑

SimulatedFailure 类 (~40 行)
├── arm()                         # 装备模拟失败
├── check_and_fire()              # 触发模拟失败
└── TEMPLATES                     # 错误模板

ContextGuard 简化版 (~160 行)
├── estimate_tokens()             # Token 估算
├── truncate_tool_results()       # 截断工具结果
└── compact_history()             # 历史压缩

REPL 命令处理 (~90 行)
└── handle_repl_command()         # 命令分发
```

---

## 五、代码结构图

### 5.1 文件整体结构

```
s09_resilience.py (1126 行)
│
├── 导入与配置 (第1-82 行)
│   ├── 标准库导入
│   ├── 第三方库导入 (OpenAI, dotenv)
│   ├── 环境配置 (MODEL_ID)
│   └── 常量定义
│       ├── BASE_RETRY = 24
│       ├── PER_PROFILE = 8
│       ├── MAX_OVERFLOW_COMPACTION = 3
│       ├── CONTEXT_SAFE_LIMIT = 180000
│       └── MAX_TOOL_OUTPUT = 50000
│
├── ANSI 颜色工具 (第83-124 行)
│   └── 打印辅助函数
│
├── FailoverReason 枚举 (第125-166 行)
│   └── classify_failure()
│
├── AuthProfile 数据类 (第167-191 行)
│   └── @dataclass 定义
│
├── ProfileManager 类 (第192-262 行)
│   ├── __init__()
│   ├── select_profile()
│   ├── select_all_available()
│   ├── mark_failure()
│   ├── mark_success()
│   └── list_profiles()
│
├── ContextGuard 类 (第263-424 行)
│   ├── estimate_tokens()
│   ├── estimate_messages_tokens()
│   ├── truncate_tool_results()
│   └── compact_history()
│
├── 工具实现 (第425-551 行)
│   ├── safe_path()
│   ├── truncate()
│   ├── tool_bash()
│   ├── tool_read_file()
│   ├── TOOLS schema
│   ├── TOOL_HANDLERS 分发表
│   └── process_tool_call()
│
├── SimulatedFailure 类 (第552-600 行)
│   ├── TEMPLATES
│   ├── arm()
│   ├── check_and_fire()
│   └── 属性访问器
│
├── ResilienceRunner 类 (第601-888 行)
│   ├── __init__()
│   ├── run()                    # 三层重试洋葱
│   ├── _run_attempt()           # Layer 3
│   └── get_stats()
│
├── REPL 命令处理 (第889-985 行)
│   └── handle_repl_command()
│
├── Agent 循环 (第986-1110 行)
│   └── agent_loop()
│
└── 入口函数 (第1111-1126 行)
    └── main()
```

### 5.2 类关系图

```
┌─────────────────────────────────────────────────────────────────────┐
│                           agent_loop()                               │
│                                                                      │
│  ┌───────────────┐  ┌───────────────────┐  ┌──────────────────┐    │
│  │ AuthProfile   │  │  ProfileManager   │  │ SimulatedFailure │    │
│  │ (dataclass)   │──│                   │  │                  │    │
│  ├───────────────┤  ├───────────────────┤  ├──────────────────┤    │
│  │ - name        │  │ - profiles[]      │  │ - _pending       │    │
│  │ - provider    │  ├───────────────────┤  ├──────────────────┤    │
│  │ - api_key     │  │ + select_profile()│  │ + arm()          │    │
│  │ - cooldown_   │  │ + mark_failure()  │  │ + check_and_fire│    │
│  │   until       │  │ + mark_success()  │  └──────────────────┘    │
│  │ - failure_    │  │ + list_profiles() │                          │
│  │   reason      │  └───────────────────┘                          │
│  │ - last_good_at│         │                                      │
│  └───────────────┘         │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    ResilienceRunner                          │   │
│  ├─────────────────────────────────────────────────────────────┤   │
│  │ - profile_manager                                            │   │
│  │ - model_id                                                   │   │
│  │ - fallback_models[]                                          │   │
│  │ - guard (ContextGuard)                                       │   │
│  │ - simulated_failure                                          │   │
│  │ - max_iterations                                             │   │
│  │ - stats counters                                             │   │
│  ├─────────────────────────────────────────────────────────────┤   │
│  │ + run()              # 三层重试洋葱                          │   │
│  │ + _run_attempt()     # Layer 3 工具循环                     │   │
│  │ + get_stats()        # 统计信息                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                      ContextGuard                            │   │
│  ├─────────────────────────────────────────────────────────────┤   │
│  │ - max_tokens                                                 │   │
│  ├─────────────────────────────────────────────────────────────┤   │
│  │ + estimate_tokens()         # Token 估算                     │   │
│  │ + truncate_tool_results()   # 截断工具结果                   │   │
│  │ + compact_history()         # 历史压缩                       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                            │                                      │
│                            ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                  OpenAI Chat Completions API                 │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.3 三层重试洋葱数据流图

```
                           用户输入
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    ResilienceRunner.run()                             │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │            LAYER 1: Auth Rotation (认证轮换)                     ││
│  │                                                                  ││
│  │   profiles = [main-key, backup-key, emergency-key]              ││
│  │        │                                                         ││
│  │        ▼                                                         ││
│  │   for each non-cooldown profile:                                ││
│  │        │                                                         ││
│  │        ▼                                                         ││
│  │   ┌───────────────────────────────────────────────────────────┐ ││
│  │   │       LAYER 2: Overflow Recovery (溢出恢复)               │ ││
│  │   │                                                           │ ││
│  │   │   for compact_attempt in 0..2:                           │ ││
│  │   │        │                                                  │ ││
│  │   │        ▼                                                  │ ││
│  │   │   ┌─────────────────────────────────────────────────────┐│ ││
│  │   │   │     LAYER 3: Tool-Use Loop (工具调用循环)           ││ ││
│  │   │   │                                                     ││ ││
│  │   │   │   _run_attempt(client, model, ...)                 ││ ││
│  │   │   │        │                                            ││ ││
│  │   │   │      success                                     ││ ││
│  │   │   │        │                                            ││ ││
│  │   │   │        ▼                                            ││ ││
│  │   │   │   mark_success(profile)                            ││ ││
│  │   │   │   return result                                     ││ ││
│  │   │   │                                                     ││ ││
│  │   │   └─────────────────────────────────────────────────────┘│ ││
│  │   │        │ exception                                       │ ││
│  │   │        ▼                                                  │ ││
│  │   │   classify_failure(exc)                                  │ ││
│  │   │        │                                                  │ ││
│  │   │   ┌────┴────┐                                            │ ││
│  │   │   ▼         ▼                                            │ ││
│  │   │ overflow   auth/rate/timeout/billing/unknown            │ ││
│  │   │   │         │                                            │ ││
│  │   │   ▼         │                                            │ ││
│  │   │ compact,    mark_failure(profile, reason, cooldown)     │ ││
│  │   │ retry L2    │                                            │ ││
│  │   │             │                                            │ ││
│  │   │             └──→ break to Layer 1 (next profile)        │ ││
│  │   │                                                          │ ││
│  │   └───────────────────────────────────────────────────────────┘ ││
│  │                                                                  ││
│  │   all profiles exhausted?                                       ││
│  │        │                                                         ││
│  │        ▼                                                         ││
│  │   try fallback models                                           ││
│  │                                                                  ││
│  └──────────────────────────────────────────────────────────────────┘│
│                                                                       │
│   all fallbacks failed?                                              │
│        │                                                              │
│        ▼                                                              │
│   raise RuntimeError("All profiles and fallback models exhausted.")  │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.4 失败处理流程图

```
                           API 调用异常
                               │
                               ▼
                    classify_failure(exc)
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
     ┌─────────┐        ┌──────────┐        ┌──────────┐
     │ overflow│        │ auth/rate│        │ timeout  │
     │         │        │ /billing │        │          │
     └────┬────┘        └────┬─────┘        └────┬─────┘
          │                  │                   │
          ▼                  ▼                   ▼
     ┌──────────┐     ┌──────────────┐    ┌──────────────┐
     │ 压缩消息  │     │ 标记失败      │    │ 标记失败      │
     │ truncate │     │ cooldown=300s │    │ cooldown=60s │
     │ compact  │     │ (rate: 120s)  │    │              │
     └────┬─────┘     └──────┬───────┘    └──────┬───────┘
          │                  │                   │
          ▼                  └─────────┬─────────┘
     ┌──────────┐                     │
     │ 重试 L2  │                     ▼
     │ (最多3次) │            ┌──────────────────┐
     └────┬─────┘            │ 切换下一个配置   │
          │                  │ (Layer 1 继续)   │
          ▼                  └──────────────────┘
     成功 / 抛出异常
```

---

## 六、关键实现细节

### 6.1 冷却时间的差异化设计

不同类型失败使用不同的冷却时间：

```python
# 溢出错误 - 不冷却，立即重试（压缩后）
overflow: 0s

# 超时 - 短冷却，可能是临时网络问题
timeout: 60s

# 速率限制 - 中等冷却
rate_limit: 120s

# 认证/计费 - 长冷却，需要人工干预
auth, billing: 300s

# 未知错误 - 中等冷却
unknown: 120s
```

### 6.2 动态最大迭代次数

```python
num_profiles = len(profile_manager.profiles)
self.max_iterations = min(
    max(BASE_RETRY + PER_PROFILE * num_profiles, 32),  # 最小32
    160,  # 最大160
)

# 示例：
# 1个配置：max(24 + 8*1, 32) = 32
# 3个配置：max(24 + 8*3, 32) = 48
# 10个配置：min(max(24 + 80, 32), 160) = 160
```

### 6.3 备选模型冷却重置策略

当所有配置耗尽但需要尝试备选模型时：

```python
# 尝试重置 rate_limit 和 timeout 类型的冷却
# 因为这些可能是临时问题
for p in self.profile_manager.profiles:
    if p.failure_reason in (
        FailoverReason.rate_limit.value,
        FailoverReason.timeout.value,
    ):
        p.cooldown_until = 0.0
```

### 6.4 消息回滚机制

当所有重试都失败时，回滚失败的用户消息：

```python
except RuntimeError as exc:
    print_error(str(exc))
    # 回滚失败的用户消息
    while messages and messages[-1]["role"] != "user":
        messages.pop()
    if messages:
        messages.pop()
```

---

## 七、使用示例

### 7.1 基本对话

```
============================================================
  claw0  |  Section 09: Resilience
  Model: qwen-plus
  Profiles: main-key, backup-key, emergency-key
  Fallback: claude-haiku-4-20250514
  Tools: bash, read_file
============================================================

You > 你好

Assistant: 你好！有什么我可以帮助你的吗？

You > /stats
  Resilience stats:
    Attempts:    1
    Successes:   1
    Failures:    0
    Compactions: 0
    Rotations:   0
    Max iter:    48
```

### 7.2 模拟失败测试

```
You > /simulate-failure rate_limit
  [resilience] Armed: next API call will fail with 'rate_limit'

You > 帮我列出当前目录
  [resilience] Profile 'main-key' -> cooldown 120s (reason: rate_limit)
  [resilience] Rotating to profile 'backup-key'
  [tool: bash] ls -la
  Assistant: 当前目录内容如下：
  total 24
  drwxr-xr-x  ...

You > /profiles
  Profiles:
    main-key          cooldown (95s remaining)  last_good=14:30:00  failure=rate_limit
    backup-key        available                 last_good=14:32:00
    emergency-key     available                 last_good=never

You > /cooldowns
  Active cooldowns:
    main-key: 95s remaining (reason: rate_limit)
```

### 7.3 模拟溢出恢复

```
You > /simulate-failure overflow
  [resilience] Armed: next API call will fail with 'overflow'

You > 继续我们的对话
  [resilience] Context overflow (attempt 1/3), compacting...
  [resilience] Compacted 15 messages -> summary (523 chars)
  Assistant: 我已理解之前的对话内容。请继续...

You > /stats
  Resilience stats:
    Attempts:    2
    Successes:   1
    Failures:    1
    Compactions: 1
    Rotations:   0
    Max iter:    48
```

---

## 八、配置说明

### 8.1 环境变量

```bash
# .env 文件
DASHSCOPE_API_KEY=sk-xxxxx           # API Key
DASHSCOPE_BASE_URL=https://...       # API 基础 URL（可选）
MODEL_ID=qwen-plus                    # 主模型
```

### 8.2 关键常量

```python
# 重试限制
BASE_RETRY = 24           # 基础重试次数
PER_PROFILE = 8           # 每个配置额外重试次数
MAX_OVERFLOW_COMPACTION = 3  # 最大溢出压缩次数

# 上下文保护
CONTEXT_SAFE_LIMIT = 180000   # 安全上下文限制（token）
MAX_TOOL_OUTPUT = 50000       # 工具输出最大字符数
```

---

## 九、总结

s09_resilience 在 claw0 系列中扮演着**生产级容错层**的角色，它：

1. **解决了"API 调用失败怎么办"的问题**
   - 认证轮换处理 API Key 相关问题
   - 溢出恢复处理上下文限制
   - 备选模型提供最后的保障

2. **提供了可测试的故障模拟机制**
   - SimulatedFailure 让开发者无需真实故障即可验证容错逻辑
   - 支持 6 种失败类型的模拟

3. **保持了架构的简洁性**
   - 三层洋葱结构清晰易懂
   - 每层职责单一
   - 失败分类驱动重试策略

4. **与前面章节形成互补**
   - 复用 s01/s02 的 Agent 循环核心
   - 复用 s03 的 ContextGuard 压缩能力
   - 为 s10 的并发控制提供稳定的执行基础

这一层的设计体现了生产级系统的核心原则：**故障是常态，而非例外**。通过三层重试洋葱，系统能够优雅地处理各种失败场景，为用户提供连续可靠的服务。