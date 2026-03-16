# 网关路由与会话隔离：完整技术文档

> 五层绑定路由机制 × 四级会话隔离 × 生产级 Python 实现

---

## 1. 引言

在多 Agent 系统中，消息路由是核心枢纽。每条来自不同渠道（Telegram、Discord、飞书、CLI 等）的入站消息，都需要被准确分发到最合适的 Agent，并为其分配正确的会话上下文。

本系统通过**五层绑定表**和**四级 dm_scope 隔离**实现精确路由：

```
入站消息 (channel, account_id, peer_id, text)
       |
       v
+--------------+
|   Gateway     |  ← WebSocket / REPL 接入
+------+--------+
       |
       v
+--------------+
|   Routing     |  ← 五层绑定解析：peer > guild > account > channel > default
+------+--------+
       |
       v
(agent_id, binding)
       |
       v
+--------------+
| AgentManager  |  ← 根据 dm_scope 构建 session_key
+------+--------+
       |
       v
session_key: "agent:luna:telegram:direct:123456"
       |
       v
[会话存储] → 加载/保存 messages[]
```

**文档结构**：

- 第 2 章：定义核心概念（Bot、Channel、Account、Peer、Agent）
- 第 3 章：详解五层绑定路由机制，每层都关联代码
- 第 4 章：详解四级 dm_scope 会话隔离，每级都关联代码
- 第 5 章：端到端消息流程追踪
- 第 6 章：逐组件代码实现详解
- 第 7 章：配置示例
- 第 8 章：最佳实践

---

## 2. 核心概念

### 2.1 Bot（机器人）

运行在即时通讯平台上的自动化程序，通过一个**账户**连接到平台。

```
┌─────────────────────────────────────────────────────────────┐
│                     机器人 (Bot)                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │   账户配置    │  │   平台渠道    │  │    用户交互       │   │
│  │  Account ID  │  │   Channel    │  │    Peer ID       │   │
│  │  API Token   │  │  Telegram    │  │   @username      │   │
│  │  Bot Token   │  │   Discord    │  │   user123        │   │
│  └──────────────┘  │   Feishu     │  └──────────────────┘   │
│                    └──────────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

**示例**：
- Telegram 客服机器人：`@CompanySupportBot`
- Discord 社区机器人：`GameHelper#1234`

### 2.2 Channel（渠道）

消息来源平台，如 `telegram`、`discord`、`feishu`、`cli`。

**代码参考**：[`s05_gateway_routing.py:283-299`](../sessions/zh/s05_gateway_routing.py#L283-L299)

```python
def resolve_route(bindings: BindingTable, mgr: AgentManager,
                  channel: str, peer_id: str,
                  account_id: str = "", guild_id: str = "") -> tuple[str, str]:
    agent_id, matched = bindings.resolve(
        channel=channel, account_id=account_id,
        guild_id=guild_id, peer_id=peer_id,
    )
    # ...
```

### 2.3 Account（账户）

同一个 Agent 可以绑定多个 Bot 账户。例如 `tg-primary` 和 `tg-secondary` 都是 Telegram 渠道下的不同账户。

### 2.4 Peer（对话方）

发送消息的用户标识，可以是用户 ID、用户名或邮箱。

**示例**：
- Telegram: `123456`
- Discord: `user#5678`
- 飞书：`alice@company.com`

### 2.5 Agent（智能代理）

具有特定人格和功能的 AI 代理，由配置文件定义。

**代码参考**：[`s05_gateway_routing.py:167-185`](../sessions/zh/s05_gateway_routing.py#L167-L185)

```python
@dataclass
class AgentConfig:
    id: str
    name: str
    personality: str = ""
    model: str = ""
    dm_scope: str = "per-peer"

    @property
    def effective_model(self) -> str:
        return self.model or MODEL_ID

    def system_prompt(self) -> str:
        parts = [f"You are {self.name}."]
        if self.personality:
            parts.append(f"Your personality: {self.personality}")
        parts.append("Answer questions helpfully and stay in character.")
        return " ".join(parts)
```

---

## 3. 五层绑定路由机制

### 3.1 设计原则

**最具体的规则优先**。路由系统按以下顺序匹配：

| 层级 | 名称 | 匹配键 | 示例值 | 说明 |
|------|------|--------|--------|------|
| 1 | **peer** | `peer_id` | `telegram:12345` | 特定用户 |
| 2 | **guild** | `guild_id` | `discord:987654` | 群组/服务器 |
| 3 | **account** | `account_id` | `tg-primary` | 特定机器人账号 |
| 4 | **channel** | `channel` | `telegram` | 整个消息通道 |
| 5 | **default** | `default` | `*` | 兜底规则 |

### 3.2 Binding 数据结构

**代码参考**：[`s05_gateway_routing.py:87-98`](../sessions/zh/s05_gateway_routing.py#L87-L98)

```python
@dataclass
class Binding:
    agent_id: str
    tier: int           # 1-5, 越小越具体
    match_key: str      # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str    # 例如 "telegram:12345", "discord", "*"
    priority: int = 0   # 同层内，越大越优先

    def display(self) -> str:
        names = {1: "peer", 2: "guild", 3: "account", 4: "channel", 5: "default"}
        label = names.get(self.tier, f"tier-{self.tier}")
        return f"[{label}] {self.match_key}={self.match_value} -> agent:{self.agent_id} (pri={self.priority})"
```

**字段说明**：
- `tier`：层级，1-5，越小越优先
- `match_key`：匹配维度
- `match_value`：匹配值
- `priority`：同层内优先级，越大越优先

### 3.3 BindingTable 类

**代码参考**：[`s05_gateway_routing.py:100-138`](../sessions/zh/s05_gateway_routing.py#L100-L138)

```python
class BindingTable:
    def __init__(self) -> None:
        self._bindings: list[Binding] = []

    def add(self, binding: Binding) -> None:
        self._bindings.append(binding)
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))

    def remove(self, agent_id: str, match_key: str, match_value: str) -> bool:
        before = len(self._bindings)
        self._bindings = [
            b for b in self._bindings
            if not (b.agent_id == agent_id and b.match_key == match_key
                    and b.match_value == match_value)
        ]
        return len(self._bindings) < before

    def list_all(self) -> list[Binding]:
        return list(self._bindings)

    def resolve(self, channel: str = "", account_id: str = "",
                guild_id: str = "", peer_id: str = "") -> tuple[str | None, Binding | None]:
        """遍历第 1-5 层，第一个匹配的获胜。返回 (agent_id, matched_binding)。"""
        for b in self._bindings:
            if b.tier == 1 and b.match_key == "peer_id":
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

**关键逻辑**：
1. `add()` 方法自动按 `(tier, -priority)` 排序，确保遍历时按优先级顺序
2. `resolve()` 方法按层级顺序遍历，第一个匹配立即返回
3. 支持 `peer_id` 的两种格式：纯 ID（`12345`）或带渠道前缀（`telegram:12345`）

### 3.4 路由解析算法

```
入站消息 → 遍历绑定表 → 按层级检查
    ├─ Tier 1: peer_id 匹配？→ 是 → 返回 agent
    │                          │
    │                          否
    │                          ↓
    ├─ Tier 2: guild_id 匹配？→ 是 → 返回 agent
    │                          │
    │                          否
    │                          ↓
    ├─ Tier 3: account_id 匹配？→ 是 → 返回 agent
    │                          │
    │                          否
    │                          ↓
    ├─ Tier 4: channel 匹配？→ 是 → 返回 agent
    │                          │
    │                          否
    │                          ↓
    └─ Tier 5: default → 总是匹配 → 返回 agent
```

### 3.5 resolve_route() 函数

**代码参考**：[`s05_gateway_routing.py:283-299`](../sessions/zh/s05_gateway_routing.py#L283-L299)

```python
def resolve_route(bindings: BindingTable, mgr: AgentManager,
                  channel: str, peer_id: str,
                  account_id: str = "", guild_id: str = "") -> tuple[str, str]:
    agent_id, matched = bindings.resolve(
        channel=channel, account_id=account_id,
        guild_id=guild_id, peer_id=peer_id,
    )
    if not agent_id:
        agent_id = DEFAULT_AGENT_ID
        print(f"  {DIM}[route] No binding matched, default: {agent_id}{RESET}")
    elif matched:
        print(f"  {DIM}[route] Matched: {matched.display()}{RESET}")
    agent = mgr.get_agent(agent_id)
    dm_scope = agent.dm_scope if agent else "per-peer"
    sk = build_session_key(agent_id, channel=channel, account_id=account_id,
                           peer_id=peer_id, dm_scope=dm_scope)
    return agent_id, sk
```

**功能**：
1. 调用 `bindings.resolve()` 解析路由
2. 若无匹配，使用默认 Agent（`main`）
3. 获取 Agent 配置的 `dm_scope`
4. 构建会话键并返回

### 3.6 示例：演示环境绑定

**代码参考**：[`s05_gateway_routing.py:512-527`](../sessions/zh/s05_gateway_routing.py#L512-L527)

```python
def setup_demo() -> tuple[AgentManager, BindingTable]:
    mgr = AgentManager()
    mgr.register(AgentConfig(
        id="luna", name="Luna",
        personality="warm, curious, and encouraging. You love asking follow-up questions.",
    ))
    mgr.register(AgentConfig(
        id="sage", name="Sage",
        personality="direct, analytical, and concise. You prefer facts over opinions.",
    ))
    bt = BindingTable()
    bt.add(Binding(agent_id="luna", tier=5, match_key="default", match_value="*"))
    bt.add(Binding(agent_id="sage", tier=4, match_key="channel", match_value="telegram"))
    bt.add(Binding(agent_id="sage", tier=1, match_key="peer_id",
                   match_value="discord:admin-001", priority=10))
    return mgr, bt
```

**绑定规则解读**：

| 层级 | 匹配键 | 匹配值 | Agent | 说明 |
|------|--------|--------|-------|------|
| 1 | `peer_id` | `discord:admin-001` | `sage` | 特定管理员用户优先路由到 Sage |
| 4 | `channel` | `telegram` | `sage` | 所有 Telegram 消息默认路由到 Sage |
| 5 | `default` | `*` | `luna` | 其他所有消息路由到 Luna |

**路由示例**：

```
消息 1: channel=telegram, peer_id=user123
  → 检查 Tier 1: 无 peer_id 匹配
  → 检查 Tier 2: 无 guild_id
  → 检查 Tier 3: 无 account_id
  → 检查 Tier 4: channel=telegram ✓ → 返回 sage

消息 2: channel=discord, peer_id=admin-001
  → 检查 Tier 1: peer_id=discord:admin-001 ✓ → 返回 sage

消息 3: channel=discord, peer_id=regular-user
  → 检查 Tier 1: 无匹配
  → 检查 Tier 2-4: 无匹配
  → 检查 Tier 5: default ✓ → 返回 luna
```

---

## 4. 会话隔离机制

### 4.1 dm_scope 概述

`dm_scope` 参数决定代理如何为不同用户维护独立的对话上下文。系统使用 `dm_scope` 构建唯一的 `session_key`：

```
入站消息 (channel, account_id, peer_id, text)
       |
       v
[路由] → agent_id
       |
       v
build_session_key(agent_id, channel, account_id, peer_id, dm_scope)
       |
       v
会话键："agent:luna:telegram:direct:user123"
       |
       v
[会话存储] → 加载/保存 messages[]
```

具有相同键的消息共享上下文；不同的键表示隔离的对话。

### 4.2 四级隔离

| dm_scope | 会话键格式 | 隔离依据 | 使用场景 |
|----------|-------------|----------|----------|
| `main` | `agent:{id}:main` | 无（共享） | 协作/公共代理 |
| `per-peer` | `agent:{id}:direct:{peer}` | 用户 ID | 个人助理（默认） |
| `per-channel-peer` | `agent:{id}:{ch}:direct:{peer}` | 用户 + 平台 | 多平台代理 |
| `per-account-channel-peer` | `agent:{id}:{ch}:{acc}:direct:{peer}` | 用户 + 平台 + 账户 | 多租户服务 |

### 4.3 build_session_key() 函数

**代码参考**：[`s05_gateway_routing.py:149-161`](../sessions/zh/s05_gateway_routing.py#L149-L161)

```python
def build_session_key(agent_id: str, channel: str = "", account_id: str = "",
                      peer_id: str = "", dm_scope: str = "per-peer") -> str:
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

**参数说明**：
- `agent_id`：目标 Agent 标识符
- `channel`：平台渠道（如 `telegram`、`discord`）
- `account_id`：机器人账户标识符
- `peer_id`：用户标识符
- `dm_scope`：隔离粒度

**返回**：用于查找/创建会话的字符串键

### 4.4 隔离级别详解

#### 4.4.1 main — 全局共享会话

**会话键格式**：`agent:{id}:main`

**示例**：
```python
build_session_key("luna", dm_scope="main")
# → "agent:luna:main"
```

**使用场景**：
- 协作头脑风暴代理
- 公共知识库
- 共享游戏状态
- 测试/调试场景

**行为**：所有用户共享同一个对话上下文，任何人都能看到其他人的消息。

#### 4.4.2 per-peer — 按用户隔离（默认）

**会话键格式**：`agent:{id}:direct:{peer_id}`

**示例**：
```python
build_session_key("luna", channel="telegram", peer_id="user123", dm_scope="per-peer")
# → "agent:luna:direct:user123"
```

**使用场景**：
- 个人助理代理（默认）
- 支持工单系统
- 私人辅导机器人
- 保密咨询

**行为**：每个用户与代理拥有自己私有的对话，其他人无法看到。

#### 4.4.3 per-channel-peer — 按平台隔离

**会话键格式**：`agent:{id}:{channel}:direct:{peer_id}`

**示例**：
```python
build_session_key("luna", channel="telegram", peer_id="user123", dm_scope="per-channel-peer")
# → "agent:luna:telegram:direct:user123"

build_session_key("luna", channel="discord", peer_id="user123", dm_scope="per-channel-peer")
# → "agent:luna:discord:direct:user123"
```

**使用场景**：
- 上下文应该是平台特定的多平台代理
- 每个平台不同的人格
- 跨渠道测试不同行为
- 平台特定的功能支持

**行为**：同一用户在不同平台上拥有不同的对话上下文。

#### 4.4.4 per-account-channel-peer — 最大隔离

**会话键格式**：`agent:{id}:{channel}:{account_id}:direct:{peer_id}`

**示例**：
```python
build_session_key("luna", channel="telegram", account_id="tg-primary",
                  peer_id="user123", dm_scope="per-account-channel-peer")
# → "agent:luna:telegram:tg-primary:direct:user123"
```

**使用场景**：
- 多租户部署
- 白标机器人服务
- 隔离的企业账户
- 最大隐私要求

**行为**：支持多个机器人账户，每个账户拥有完全隔离的会话。

### 4.5 AgentManager 类

**代码参考**：[`s05_gateway_routing.py:187-214`](../sessions/zh/s05_gateway_routing.py#L187-L214)

```python
class AgentManager:
    def __init__(self, agents_base: Path | None = None) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._agents_base = agents_base or AGENTS_DIR
        self._sessions: dict[str, list[dict]] = {}

    def register(self, config: AgentConfig) -> None:
        aid = normalize_agent_id(config.id)
        config.id = aid
        self._agents[aid] = config
        agent_dir = self._agents_base / aid
        (agent_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (WORKSPACE_DIR / f"workspace-{aid}").mkdir(parents=True, exist_ok=True)

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(normalize_agent_id(agent_id))

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def get_session(self, session_key: str) -> list[dict]:
        if session_key not in self._sessions:
            self._sessions[session_key] = []
        return self._sessions[session_key]

    def list_sessions(self, agent_id: str = "") -> dict[str, int]:
        aid = normalize_agent_id(agent_id) if agent_id else ""
        return {k: len(v) for k, v in self._sessions.items()
                if not aid or k.startswith(f"agent:{aid}:")}
```

**关键方法**：
- `register()`：注册 Agent 配置，创建工作目录
- `get_agent()`：根据 ID 获取 Agent 配置
- `get_session()`：根据会话键获取或创建会话列表
- `list_sessions()`：列出所有会话及消息数量

---

## 5. 端到端流程

### 5.1 消息处理完整链路

```
1. 入站消息到达
   channel="telegram", account_id="tg-primary", peer_id="12345", text="你好"
   |
   v
2. 调用 resolve_route()
   [s05_gateway_routing.py:283-299]
   |
   v
3. BindingTable.resolve() 遍历绑定表
   [s05_gateway_routing.py:120-138]
   ├─ Tier 1: peer_id 匹配？
   ├─ Tier 2: guild_id 匹配？
   ├─ Tier 3: account_id 匹配？
   ├─ Tier 4: channel 匹配？
   └─ Tier 5: default 匹配？
   |
   v
4. 返回 (agent_id, matched_binding)
   agent_id="sage", matched=<Binding: tier=4, channel=telegram>
   |
   v
5. 获取 Agent 配置
   agent = mgr.get_agent("sage")
   dm_scope = agent.dm_scope  # "per-peer"
   |
   v
6. 构建会话键
   build_session_key("sage", channel="telegram", account_id="tg-primary",
                     peer_id="12345", dm_scope="per-peer")
   → "agent:sage:direct:12345"
   |
   v
7. 获取会话
   messages = mgr.get_session("agent:sage:direct:12345")
   |
   v
8. 调用 LLM
   run_agent(mgr, "sage", "agent:sage:direct:12345", "你好")
   |
   v
9. 返回回复
   "你好！我是 Sage，有什么可以帮助你的？"
```

### 5.2 代码追踪示例

假设收到一条 Telegram 消息：

```python
# 入站消息参数
channel = "telegram"
account_id = "tg-primary"
peer_id = "12345"
text = "推荐一本好书"

# 步骤 1: 路由解析
agent_id, session_key = resolve_route(
    bindings=bt,
    mgr=mgr,
    channel=channel,
    peer_id=peer_id,
    account_id=account_id
)

# 控制台输出:
# [route] Matched: [channel] channel=telegram -> agent:sage (pri=0)

# 步骤 2: 会话键
# → "agent:sage:direct:12345"

# 步骤 3: 获取会话
messages = mgr.get_session(session_key)

# 步骤 4: 运行 Agent
reply = run_async(run_agent(mgr, agent_id, session_key, text))

# 步骤 5: 返回回复
print(reply)  # "我推荐《思考，快与慢》..."
```

### 5.3 多用户并发场景

```
用户 A (telegram:12345) ──→ agent:sage:direct:12345 ──→ 会话 A
用户 B (telegram:67890) ──→ agent:sage:direct:67890 ──→ 会话 B
用户 C (discord:11111) ──→ agent:luna:direct:11111 ──→ 会话 C

# 三个会话完全隔离，互不影响
```

---

## 6. 代码实现详解

### 6.1 导入与配置

**代码参考**：[`s05_gateway_routing.py:32-59`](../sessions/zh/s05_gateway_routing.py#L32-L59)

```python
import os, re, sys, json, time, asyncio, threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent / "workspace"
AGENTS_DIR = WORKSPACE_DIR / ".agents"
```

### 6.2 Agent ID 标准化

**代码参考**：[`s05_gateway_routing.py:65-77`](../sessions/zh/s05_gateway_routing.py#L65-L77)

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
    cleaned = INVALID_CHARS_RE.sub("-", trimmed.lower()).strip("-")[:64]
    return cleaned or DEFAULT_AGENT_ID
```

**功能**：
- 确保 Agent ID 符合规范（小写字母、数字、下划线、连字符）
- 非法字符替换为连字符
- 空值返回默认值 `main`

### 6.3 工具系统

**代码参考**：[`s05_gateway_routing.py:217-256`](../sessions/zh/s05_gateway_routing.py#L217-L256)

```python
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read the contents of a file.",
        "parameters": {"type": "object", "required": ["file_path"],
                       "properties": {"file_path": {"type": "string", "description": "Path to the file."}}}}},
    {"type": "function", "function": {
        "name": "get_current_time", "description": "Get the current date and time in UTC.",
        "parameters": {"type": "object", "properties": {}}}},
]

TOOL_HANDLERS: dict[str, Any] = {
    "read_file": lambda file_path: _tool_read(file_path),
    "get_current_time": lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
}

def process_tool_call(name: str, inp: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Error: Unknown tool '{name}'"
    try:
        return handler(**inp)
    except Exception as exc:
        return f"Error: {name} failed: {exc}"
```

### 6.4 Agent 运行器

**代码参考**：[`s05_gateway_routing.py:302-393`](../sessions/zh/s05_gateway_routing.py#L302-L393)

```python
_agent_semaphore: asyncio.Semaphore | None = None

async def run_agent(mgr: AgentManager, agent_id: str, session_key: str,
                    user_text: str, on_typing: Any = None) -> str:
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(4)
    agent = mgr.get_agent(agent_id)
    if not agent:
        return f"Error: agent '{agent_id}' not found"
    messages = mgr.get_session(session_key)
    messages.append({"role": "user", "content": user_text})
    async with _agent_semaphore:
        if on_typing:
            on_typing(agent_id, True)
        try:
            return await _agent_loop(agent.effective_model, agent.system_prompt(), messages)
        finally:
            if on_typing:
                on_typing(agent_id, False)
```

**关键特性**：
- 使用信号量控制并发（最多 4 个并发请求）
- Typing 回调通知客户端
- 自动追加用户消息到会话历史

### 6.5 Gateway WebSocket 服务器

**代码参考**：[`s05_gateway_routing.py:397-505`](../sessions/zh/s05_gateway_routing.py#L397-L505)

```python
class GatewayServer:
    def __init__(self, mgr: AgentManager, bindings: BindingTable,
                 host: str = "localhost", port: int = 8765) -> None:
        self._mgr = mgr
        self._bindings = bindings
        self._host, self._port = host, port
        self._clients: set[Any] = set()
        self._start_time = time.monotonic()
        self._server: Any = None
        self._running = False

    async def start(self) -> None:
        try:
            import websockets
        except ImportError:
            print(f"{RED}websockets not installed. pip install websockets{RESET}"); return
        self._start_time = time.monotonic()
        self._running = True
        self._server = await websockets.serve(self._handle, self._host, self._port)
        print(f"{GREEN}Gateway started ws://{self._host}:{self._port}{RESET}")
```

**JSON-RPC 2.0 方法**：
- `send`：发送消息
- `bindings.set`：添加绑定规则
- `bindings.list`：列出所有绑定
- `sessions.list`：列出会话
- `agents.list`：列出 Agent
- `status`：服务状态

### 6.6 REPL 命令行界面

**代码参考**：[`s05_gateway_routing.py:531-664`](../sessions/zh/s05_gateway_routing.py#L531-L664)

**可用命令**：
- `/bindings`：显示所有绑定规则
- `/route <channel> <peer_id>`：测试路由解析
- `/agents`：列出所有 Agent
- `/sessions`：列出所有会话
- `/switch <agent_id>`：强制指定 Agent
- `/gateway`：启动 WebSocket 服务

---

## 7. 配置示例

### 7.1 基础配置

```python
# 定义两个 Agent
mgr = AgentManager()
mgr.register(AgentConfig(
    id="customer-support",
    name="客服小助手",
    personality="友好、耐心、专业",
    dm_scope="per-peer"  # 每个用户独立会话
))
mgr.register(AgentConfig(
    id="announcement-bot",
    name="公告机器人",
    personality="正式、简洁",
    dm_scope="main"  # 所有人共享会话
))
```

### 7.2 路由绑定配置

```python
bt = BindingTable()

# 特定 VIP 用户路由到专属客服
bt.add(Binding(
    agent_id="customer-support",
    tier=1,
    match_key="peer_id",
    match_value="telegram:vip-001",
    priority=10
))

# 所有 Telegram 消息路由到客服
bt.add(Binding(
    agent_id="customer-support",
    tier=4,
    match_key="channel",
    match_value="telegram"
))

# Discord 群组消息路由到公告机器人
bt.add(Binding(
    agent_id="announcement-bot",
    tier=2,
    match_key="guild_id",
    match_value="discord:community-server"
))

# 默认兜底
bt.add(Binding(
    agent_id="customer-support",
    tier=5,
    match_key="default",
    match_value="*"
))
```

### 7.3 多租户配置

```python
# 企业 A 专属机器人
mgr.register(AgentConfig(
    id="enterprise-a-bot",
    name="企业 A 助手",
    dm_scope="per-account-channel-peer"  # 最大隔离
))

# 绑定企业 A 的专用账户
bt.add(Binding(
    agent_id="enterprise-a-bot",
    tier=3,
    match_key="account_id",
    match_value="enterprise-a-tg"
))
```

### 7.4 跨平台助理配置

```python
# 同一个助理，不同平台不同人格
mgr.register(AgentConfig(
    id="personal-assistant",
    name="个人助理",
    personality="专业、高效",
    dm_scope="per-channel-peer"  # 按平台隔离
))

# Telegram 上更随意
bt.add(Binding(
    agent_id="personal-assistant",
    tier=4,
    match_key="channel",
    match_value="telegram"
))

# Discord 上更正式
bt.add(Binding(
    agent_id="personal-assistant",
    tier=4,
    match_key="channel",
    match_value="discord"
))
```

---

## 8. 最佳实践

### 8.1 选择合适的 dm_scope

```python
# ❌ 对于私人助理来说范围太宽
AgentConfig(id="therapist", dm_scope="main")

# ✅ 适合个人 AI
AgentConfig(id="therapist", dm_scope="per-peer")

# ❌ 对于协作工具来说范围太窄
AgentConfig(id="poll-master", dm_scope="per-account-channel-peer")

# ✅ 适合群组活动
AgentConfig(id="poll-master", dm_scope="main")
```

### 8.2 优先考虑隐私

```python
# 默认为最私密的设置
DEFAULT_DM_SCOPE = "per-peer"

# 仅在明确需要时才放宽
if agent_config.get("shared_context", False):
    scope = "main"
else:
    scope = "per-peer"
```

### 8.3 记录配置理由

```python
AGENT_MANIFEST = {
    "id": "support-bot",
    "dm_scope": "per-peer",
    "scope_rationale": "每个员工的工单都是私密的",
    "data_retention": "每个会话保留 30 天"
}
```

### 8.4 测试会话隔离

```python
def test_session_isolation():
    """验证不同用户不共享上下文。"""
    agent = AgentConfig(id="test", dm_scope="per-peer")

    key1 = build_session_key("test", peer_id="user1", dm_scope="per-peer")
    key2 = build_session_key("test", peer_id="user2", dm_scope="per-peer")

    assert key1 != key2, "不同用户应该拥有不同的会话键"
    assert key1 == "agent:test:direct:user1"
    assert key2 == "agent:test:direct:user2"
```

### 8.5 避免常见陷阱

**陷阱 1：忘记设置 dm_scope**
```python
# ❌ 问题：当你想要 "main" 时使用了默认的 "per-peer"
AgentConfig(id="public-announcement")

# ✅ 解决方案：显式设置范围
AgentConfig(id="public-announcement", dm_scope="main")
```

**陷阱 2：跨部署范围不一致**
```python
# 生产配置
dm_scope = "per-account-channel-peer"

# 开发配置（忘记更新）
dm_scope = "per-peer"

# 结果：会话在开发和生产环境表现不同
```

**陷阱 3：对话中途更改范围**
```python
# 用户一直使用 dm_scope="per-peer" 聊天
# 键："agent:helper:direct:alice"

# 突然更改为 dm_scope="main"
# 键变为："agent:helper:main"

# 结果：Alice 丢失了之前会话的所有上下文
```

### 8.6 路由规则设计原则

1. **具体规则优先**：将 peer_id 规则放在前面
2. **设置合理优先级**：同层内用 priority 区分
3. **必须设置兜底**：确保 default 规则存在
4. **定期审查规则**：删除过期绑定

---

## 9. 总结

### 9.1 核心机制

本系统通过两个核心机制实现精确的消息路由和会话管理：

**五层绑定路由**：
```
peer (Tier 1) > guild (Tier 2) > account (Tier 3) > channel (Tier 4) > default (Tier 5)
```

**四级会话隔离**：
```
main → per-peer → per-channel-peer → per-account-channel-peer
```

### 9.2 设计优势

| 特性 | 优势 |
|------|------|
| 分层匹配 | 从最具体到最通用，业务规则可精细控制 |
| 优先级控制 | 同层内可设置优先级，解决规则冲突 |
| 会话隔离 | dm_scope 机制允许自定义上下文范围 |
| 可扩展接入 | 支持 WebSocket 和 REPL，便于集成与调试 |
| 代码简洁 | 核心路由逻辑不到 20 行 |

### 9.3 关键代码文件

- [`s05_gateway_routing.py`](../sessions/zh/s05_gateway_routing.py)：完整实现
- [`Binding` 数据类](../sessions/zh/s05_gateway_routing.py#L87-L98)：绑定规则
- [`BindingTable.resolve()`](../sessions/zh/s05_gateway_routing.py#L120-L138)：路由解析
- [`build_session_key()`](../sessions/zh/s05_gateway_routing.py#L149-L161)：会话键构建
- [`resolve_route()`](../sessions/zh/s05_gateway_routing.py#L283-L299)：端到端路由

### 9.4 下一步

掌握本系统后，你可以：

1. 为不同业务场景配置合适的路由规则
2. 为不同 Agent 选择合适的 dm_scope
3. 通过 REPL 调试路由逻辑
4. 通过 WebSocket 集成外部系统
5. 扩展到更多渠道和 Agent

该路由模型可作为多 Agent 系统的核心枢纽，为后续功能（如负载均衡、动态 Agent 切换）奠定坚实基础。

---

> **关键洞察**：路由不仅是技术决策，更是业务决策。正确的路由规则和会话隔离策略，能够确保用户获得他们期望的上下文体验，无论是个人连续性还是协作感知。
