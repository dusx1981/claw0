# s05_gateway_routing.py 深度解析

> 网关与路由 —— "每条消息都能找到归宿"

---

## 一、设计思想

### 1.1 核心哲学

```
入站消息 (channel, account_id, peer_id, text)
       |
+------v------+     +----------+
|   Gateway    | <-- | WS/REPL  |  JSON-RPC 2.0
+------+------+
       |
+------v------+
|   Routing    |  5层: peer > guild > account > channel > default
+------+------+
       |
 (agent_id, session_key)
       |
+------v------+
| AgentManager |  每个 agent 的配置 / 工作区 / 会话
+------+------+
       |
    LLM API
```

**核心认知**：Gateway 是消息枢纽，路由系统是一个**五层绑定表**，从最具体到最通用进行匹配。每条入站消息最终解析为 `(agent_id, session_key)`。

### 1.2 五层路由设计

```
第1层: peer_id    -- 将特定用户路由到某个 agent (最具体)
第2层: guild_id   -- guild/服务器级别
第3层: account_id -- bot 账号级别
第4层: channel    -- 整个通道 (如所有 Telegram)
第5层: default    -- 兜底 (最通用)
```

**设计意图**：
- 层级越低越具体，优先匹配
- 同层内按 priority 排序
- 第一个匹配的规则获胜

### 1.3 会话隔离策略

```python
# dm_scope 控制私聊隔离粒度
main                      -> agent:{id}:main
per-peer                  -> agent:{id}:direct:{peer}
per-channel-peer          -> agent:{id}:{ch}:direct:{peer}
per-account-channel-peer  -> agent:{id}:{ch}:{acc}:direct:{peer}
```

**设计意图**：不同隔离粒度支持不同业务场景：
- `main`: 单一会话，所有用户共享上下文
- `per-peer`: 每个用户独立会话（默认）
- `per-channel-peer`: 同一用户在不同平台有独立会话
- `per-account-channel-peer`: 最细粒度，支持多 bot 账号场景

---

## 二、实现机制

### 2.1 绑定表结构

```python
@dataclass
class Binding:
    agent_id: str           # 目标 agent
    tier: int               # 层级 1-5，越小越优先
    match_key: str          # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str        # 匹配值，如 "telegram:12345", "discord"
    priority: int = 0       # 同层优先级，越大越优先

class BindingTable:
    def add(self, binding: Binding) -> None:
        self._bindings.append(binding)
        # 按 tier 升序，priority 降序排序
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))
```

**机制**：
1. 绑定按 `(tier, -priority)` 排序
2. 遍历时从最具体规则开始匹配
3. 第一个匹配立即返回

### 2.2 路由解析算法

```python
def resolve(self, channel, account_id, guild_id, peer_id) -> tuple[str | None, Binding | None]:
    for b in self._bindings:
        if b.tier == 1 and b.match_key == "peer_id":
            # peer_id 支持两种格式: "12345" 或 "telegram:12345"
            if ":" in b.match_value:
                if b.match_value == f"{channel}:{peer_id}":
                    return b.agent_id, b
            elif b.match_value == peer_id:
                return b.agent_id, b
        elif b.tier == 2 and b.match_key == "guild_id" and b.match_value == guild_id:
            return b.agent_id, b
        elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
            return b.agent_id, b
        elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
            return b.agent_id, b
        elif b.tier == 5 and b.match_key == "default":
            return b.agent_id, b
    return None, None
```

**算法复杂度**：O(n) 遍历，n 为绑定数量。实际场景 n 很小，性能不是瓶颈。

### 2.3 Session Key 构建

```python
def build_session_key(agent_id, channel="", account_id="", peer_id="", dm_scope="per-peer"):
    aid = normalize_agent_id(agent_id)
    ch = (channel or "unknown").strip().lower()
    acc = (account_id or "default").strip().lower()
    pid = (peer_id or "").strip().lower()
    
    if dm_scope == "per-account-channel-peer" and pid:
        return f"agent:{aid}:{ch}:{acc}:direct:{pid}"
    if dm_scope == "per-channel-peer" and pid:
        return f"agent:{aid}:{ch}:direct:{pid}"
    if dm_scope == "per-peer" and pid:
        return f"agent:{aid}:direct:{pid}"
    return f"agent:{aid}:main"
```

**关键设计**：
- Session Key 是字符串，方便持久化和索引
- 包含完整路由信息，可逆向解析

### 2.4 Agent ID 标准化

```python
VALID_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
INVALID_CHARS_RE = re.compile(r"[^a-z0-9_-]+")
DEFAULT_AGENT_ID = "main"

def normalize_agent_id(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return DEFAULT_AGENT_ID
    if VALID_ID_RE.match(trimmed):
        return trimmed.lower()
    # 清理非法字符，截断到64字符
    cleaned = INVALID_CHARS_RE.sub("-", trimmed.lower()).strip("-")[:64]
    return cleaned or DEFAULT_AGENT_ID
```

**目的**：
- 确保文件系统安全（agent_id 用于目录名）
- 统一大小写，避免歧义
- 提供默认值

### 2.5 Agent 管理器

```python
@dataclass
class AgentConfig:
    id: str
    name: str
    personality: str = ""      # 人格描述
    model: str = ""            # 指定模型（可选）
    dm_scope: str = "per-peer" # 会话隔离粒度

class AgentManager:
    def __init__(self, agents_base: Path | None = None):
        self._agents: dict[str, AgentConfig] = {}
        self._agents_base = agents_base or AGENTS_DIR
        self._sessions: dict[str, list[dict]] = {}  # session_key -> messages
    
    def register(self, config: AgentConfig) -> None:
        # 创建 agent 工作区目录
        agent_dir = self._agents_base / aid
        (agent_dir / "sessions").mkdir(parents=True, exist_ok=True)
```

**职责**：
- 管理 Agent 配置
- 管理会话状态
- 创建 Agent 工作区

### 2.6 共享事件循环

```python
_event_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

def get_event_loop() -> asyncio.AbstractEventLoop:
    global _event_loop, _loop_thread
    if _event_loop is not None and _event_loop.is_running():
        return _event_loop
    _event_loop = asyncio.new_event_loop()
    def _run():
        asyncio.set_event_loop(_event_loop)
        _event_loop.run_forever()
    _loop_thread = threading.Thread(target=_run, daemon=True)
    _loop_thread.start()
    return _event_loop

def run_async(coro):
    loop = get_event_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()
```

**设计意图**：
- REPL 是同步的，但 Agent 运行需要异步
- 后台线程运行事件循环，主线程通过 `run_async` 提交协程
- 避免每次调用都创建新循环

### 2.7 Gateway 服务器

```python
class GatewayServer:
    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self._host, self._port)
    
    async def _dispatch(self, raw: str) -> dict | None:
        # JSON-RPC 2.0 分发
        rid, method, params = req.get("id"), req.get("method", ""), req.get("params", {})
        methods = {
            "send": self._m_send,
            "bindings.set": self._m_bind_set,
            "bindings.list": self._m_bind_list,
            "sessions.list": self._m_sessions,
            "agents.list": self._m_agents,
            "status": self._m_status,
        }
```

**协议**：JSON-RPC 2.0
- 请求格式：`{"jsonrpc": "2.0", "method": "send", "params": {...}, "id": 1}`
- 响应格式：`{"jsonrpc": "2.0", "result": {...}, "id": 1}`

**API 方法**：

| 方法 | 功能 |
|------|------|
| `send` | 发送消息，返回 agent 回复 |
| `bindings.set` | 设置路由绑定 |
| `bindings.list` | 列出所有绑定 |
| `sessions.list` | 列出会话 |
| `agents.list` | 列出所有 agent |
| `status` | 获取服务器状态 |

---

## 三、主要功能

### 3.1 REPL 命令

```
/bindings          列出所有路由绑定
/route <ch> <peer> [account] [guild]   测试路由解析
/agents            列出所有 agent
/sessions          列出所有会话
/switch <id>       强制切换到指定 agent（/switch off 恢复路由）
/gateway           启动 WebSocket 网关服务器
```

### 3.2 路由绑定示例

```python
# 示例配置
bt = BindingTable()
bt.add(Binding(agent_id="luna", tier=5, match_key="default", match_value="*"))
bt.add(Binding(agent_id="sage", tier=4, match_key="channel", match_value="telegram"))
bt.add(Binding(agent_id="sage", tier=1, match_key="peer_id",
               match_value="discord:admin-001", priority=10))
```

**匹配逻辑**：
1. 来自 `discord:admin-001` 的消息 → `sage` (第1层匹配)
2. 来自 Telegram 任意用户的消息 → `sage` (第4层匹配)
3. 其他所有消息 → `luna` (第5层默认)

### 3.3 WebSocket 客户端示例

```javascript
const ws = new WebSocket('ws://localhost:8765');

// 发送消息
ws.send(JSON.stringify({
    jsonrpc: "2.0",
    method: "send",
    params: { text: "Hello", channel: "telegram", peer_id: "12345" },
    id: 1
}));

// 接收响应
ws.onmessage = (event) => {
    const response = JSON.parse(event.data);
    console.log(response.result.reply);
};
```

### 3.4 并发控制

```python
_agent_semaphore: asyncio.Semaphore | None = None

async def run_agent(mgr, agent_id, session_key, user_text, on_typing=None):
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(4)  # 最多4个并发
    
    async with _agent_semaphore:
        # ... API 调用 ...
```

**目的**：限制并发 API 调用数量，避免触发限流。

### 3.5 Typing 指示器

```python
def _typing_cb(self, agent_id: str, typing: bool) -> None:
    msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "typing",
        "params": {"agent_id": agent_id, "typing": typing}
    })
    for ws in list(self._clients):
        ws.send(msg)  # 广播给所有客户端
```

**用途**：当 Agent 正在生成回复时，通知客户端显示"正在输入"状态。

---

## 四、与前面章节的演进关系

### 4.1 从 s01 到 s05 的演进

```
s01: Agent Loop
  └── messages[] 内存存储
  
s02: Tool Use
  └── 工具调用循环
  
s03: Sessions
  └── messages[] → JSONL 持久化
  └── 会话键概念: session_key
  
s04: Channels
  └── InboundMessage 标准化入站消息
  └── channel, account_id, peer_id, guild_id
  
s05: Gateway & Routing
  └── 消息路由: InboundMessage → (agent_id, session_key)
  └── 多 Agent 支持
  └── WebSocket 网关
```

### 4.2 核心演进点

| 章节 | 演进点 | s05 复用情况 |
|------|--------|--------------|
| s01 | Agent Loop | `run_agent()` 完整复用 |
| s02 | Tool Use | `TOOLS` + `process_tool_call()` 复用 |
| s03 | Sessions | `session_key` 概念，内存版 `get_session()` |
| s04 | Channels | `channel`, `peer_id`, `account_id`, `guild_id` 作为路由参数 |

### 4.3 新增功能

1. **多 Agent 支持**：`AgentConfig` + `AgentManager`
2. **路由系统**：`Binding` + `BindingTable`
3. **WebSocket 网关**：`GatewayServer` + JSON-RPC 2.0
4. **并发控制**：`asyncio.Semaphore`
5. **会话隔离**：`dm_scope` 四级隔离粒度

### 4.4 后续章节依赖

```
s05 (Gateway)
  │
  ├──> s06: Intelligence (Agent 配置扩展: soul, memory, skills)
  │
  ├──> s07: Heartbeat (网关主动推送心跳消息)
  │
  └──> s08: Delivery (可靠消息投递)
```

---

## 五、代码结构图

```
s05_gateway_routing.py (666 行)
│
├── 模块配置
│   ├── 导入 (os, re, sys, json, asyncio, threading, ...)
│   ├── 环境变量 (DASHSCOPE_API_KEY, MODEL_ID)
│   ├── 目录常量 (WORKSPACE_DIR, AGENTS_DIR)
│   └── ANSI 颜色常量
│
├── Agent ID 标准化
│   ├── VALID_ID_RE          # 正则: 有效 ID 格式
│   ├── INVALID_CHARS_RE     # 正则: 需清理字符
│   ├── DEFAULT_AGENT_ID     # 默认值: "main"
│   └── normalize_agent_id() # 标准化函数
│
├── 路由系统
│   ├── Binding              # 绑定规则 (agent_id, tier, match_key, ...)
│   │   └── display()        # 格式化显示
│   └── BindingTable         # 绑定表
│       ├── add()            # 添加绑定
│       ├── remove()         # 移除绑定
│       ├── list_all()       # 列出所有
│       └── resolve()        # 解析路由 ★
│
├── Session Key 构建
│   └── build_session_key()  # 根据参数构建 session_key
│
├── Agent 管理
│   ├── AgentConfig          # Agent 配置
│   │   ├── effective_model  # 有效模型
│   │   └── system_prompt()  # 生成系统提示词
│   └── AgentManager         # 管理器
│       ├── register()       # 注册 agent
│       ├── get_agent()      # 获取配置
│       ├── list_agents()    # 列出所有
│       ├── get_session()    # 获取会话消息
│       └── list_sessions()  # 列出所有会话
│
├── 工具系统
│   ├── TOOLS                # 工具定义 (read_file, get_current_time)
│   ├── TOOL_HANDLERS        # 工具处理函数映射
│   └── process_tool_call()  # 执行工具调用
│
├── 异步支持
│   ├── _event_loop          # 全局事件循环
│   ├── _loop_thread         # 后台线程
│   ├── get_event_loop()     # 获取/创建事件循环
│   └── run_async()          # 同步调用协程
│
├── Agent 运行器
│   ├── _agent_semaphore     # 并发信号量
│   ├── run_agent()          # 异步运行 agent ★
│   └── _agent_loop()        # API 调用循环
│
├── Gateway 服务器
│   └── GatewayServer
│       ├── start()          # 启动 WebSocket
│       ├── stop()           # 停止服务
│       ├── _handle()        # 连接处理
│       ├── _typing_cb()     # Typing 回调
│       ├── _dispatch()      # JSON-RPC 分发
│       ├── _m_send()        # 方法: send
│       ├── _m_bind_set()    # 方法: bindings.set
│       ├── _m_bind_list()   # 方法: bindings.list
│       ├── _m_sessions()    # 方法: sessions.list
│       ├── _m_agents()      # 方法: agents.list
│       └── _m_status()      # 方法: status
│
├── 演示配置
│   └── setup_demo()         # 创建 demo agents + bindings
│
├── REPL 命令
│   ├── cmd_bindings()       # /bindings
│   ├── cmd_route()          # /route <ch> <peer>
│   ├── cmd_agents()         # /agents
│   ├── cmd_sessions()       # /sessions
│   └── repl()               # 主循环 ★
│
└── 入口
    └── main()               # 检查 API Key → 启动 REPL
```

---

## 六、数据流图

```
                    ┌─────────────────┐
                    │  External Input │
                    │ (CLI / WS / API)│
                    └────────┬────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────┐
│                    GatewayServer                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │                  _dispatch()                      │  │
│  │  JSON-RPC 2.0 → method → handler                 │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────┬───────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────┐
│                    BindingTable                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │                  resolve()                        │  │
│  │  (channel, account, guild, peer)                  │  │
│  │          ↓                                        │  │
│  │  Tier 1: peer_id     ───┐                         │  │
│  │  Tier 2: guild_id    ───┼── first match wins      │  │
│  │  Tier 3: account_id  ───┤                         │  │
│  │  Tier 4: channel     ───┤                         │  │
│  │  Tier 5: default     ───┘                         │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────┬───────────────────────────┘
                             │
                             ▼
                   (agent_id, matched_binding)
                             │
                             ▼
┌────────────────────────────────────────────────────────┐
│                   AgentManager                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │              build_session_key()                  │  │
│  │  agent_id + channel + peer + dm_scope             │  │
│  │          ↓                                        │  │
│  │  "agent:luna:direct:telegram:12345"              │  │
│  └──────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │              get_session()                       │  │
│  │  session_key → messages[]                        │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────┬───────────────────────────┘
                             │
                             ▼
                   (agent_config, session_messages)
                             │
                             ▼
┌────────────────────────────────────────────────────────┐
│                    run_agent()                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │              _agent_loop()                        │  │
│  │                                                  │  │
│  │  messages.append(user)                           │  │
│  │          ↓                                        │  │
│  │  [system] + messages → API                       │  │
│  │          ↓                                        │  │
│  │  finish_reason?                                  │  │
│  │    ├── stop → return text                        │  │
│  │    └── tool_calls → execute → continue           │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────┬───────────────────────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │   Reply to User │
                    └─────────────────┘
```

---

## 七、关键认知

### 7.1 路由即分层匹配

路由系统的核心是**层级递减匹配**：
```
用户级 (最具体) > 服务器级 > 账号级 > 平台级 > 默认 (最通用)
```

这种设计源于实际业务需求：
- 管理员需要专属 Agent
- 特定服务器需要特殊处理
- 不同平台可能需要不同人格

### 7.2 Session Key 的可读性

Session Key 采用字符串格式而非 UUID：
```
"agent:luna:direct:telegram:12345"
```

**优势**：
- 可读性强，便于调试
- 包含完整信息，可逆向解析
- 天然排序，便于索引

### 7.3 多 Agent 并发安全

```python
async with _agent_semaphore:  # 限制并发数
    if on_typing:
        on_typing(agent_id, True)  # 状态通知
    try:
        return await _agent_loop(...)
    finally:
        if on_typing:
            on_typing(agent_id, False)  # 状态恢复
```

**关键**：
- 信号量控制并发
- try/finally 确保状态清理
- 回调函数解耦网关通知

### 7.4 同步 REPL + 异步 Agent

```
主线程 (同步 REPL)          后台线程 (异步事件循环)
       │                          │
       │   run_async(coro)        │
       ├─────────────────────────►│
       │                          │ asyncio.run_coroutine_threadsafe
       │                          │
       │   await result()         │
       ├─────────────────────────►│
       │                          │
       │   ← result               │
```

这种架构让同步 REPL 能够调用异步 Agent，同时支持 WebSocket 服务器。

---

## 八、扩展建议

### 8.1 路由规则持久化

当前绑定只存在于内存中，重启丢失。可扩展为：
```python
class BindingTable:
    def save(self, path: Path) -> None:
        with open(path, 'w') as f:
            json.dump([asdict(b) for b in self._bindings], f)
    
    def load(self, path: Path) -> None:
        with open(path) as f:
            for item in json.load(f):
                self.add(Binding(**item))
```

### 8.2 动态路由规则

支持正则匹配或通配符：
```python
# 通配符
Binding(agent_id="luna", match_value="telegram:*")

# 正则
Binding(agent_id="admin", match_value="regex:^admin-.*", tier=1)
```

### 8.3 路由优先级可视化

添加调试命令显示完整匹配路径：
```
/route/trace telegram user-123

[Tier 1] peer_id=telegram:user-123 → no match
[Tier 2] guild_id= → no match  
[Tier 3] account_id= → no match
[Tier 4] channel=telegram → MATCHED: sage
```

---

> **作者注**：这一节是连接"通道"和"智能"的关键枢纽。路由系统让多 Agent 架构成为可能，而 Gateway 则是对外服务的统一入口。理解了路由的层级匹配，就能理解为什么后续章节可以轻松扩展出"心跳"、"定时任务"等主动行为——因为每条消息都有明确的归宿。