# 通过 `dm_scope` 实现会话隔离

> 通过四个粒度级别控制对话上下文隔离。

---

## 概述

`dm_scope` 参数决定了代理如何为不同用户维护独立的对话上下文（会话）。当消息到达时，系统使用 `dm_scope` 构建唯一的 `session_key`。具有相同键的消息共享上下文；不同的键表示隔离的对话。

```
入站消息 (channel, account_id, peer_id, text)
       |
       v
[ 路由 ] -> agent_id
       |
       v
build_session_key(agent_id, channel, account_id, peer_id, dm_scope)
       |
       v
会话键: "agent:luna:telegram:direct:user123"
       |
       v
[ 会话存储 ] -> 加载/保存 messages[]
```

---

## 什么是机器人

在本系统中，**机器人**（Bot）是指运行在即时通讯平台（如 Telegram、Discord、飞书等）上的自动化程序。它通过一个**账户**（Account）连接到平台，能够接收用户消息、处理请求并发送回复。

### 机器人的关键要素

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

### 示例说明

**示例 1：Telegram 客服机器人**
- **账户**: `@CompanySupportBot`（在 Telegram 平台注册的应用）
- **平台**: Telegram
- **用户**: `@alice`（在 Telegram 上发送消息的客户）
- **会话**: 每个客户与 `@CompanySupportBot` 的聊天都是独立的

**示例 2：Discord 社区机器人**
- **账户**: `GameHelper#1234`（Discord 应用账户）
- **平台**: Discord
- **用户**: `Alice#5678` 和 `Bob#9999`（服务器成员）
- **会话**: 通过私信（DM）与机器人的对话是隔离的

**示例 3：多平台部署**
- 同一个智能助理同时连接到：
  - Telegram: `@SmartAssistantBot`
  - Discord: `SmartAssistant#8888`
  - 飞书: `智能助手`
- **同一个用户**在不同平台与机器人对话时，上下文可以独立隔离

### 为什么需要会话隔离

机器人需要记住与每个用户的对话历史才能提供连贯的回复。`dm_scope` 决定了**哪些用户共享同一个记忆**：

- **所有人共享** → 像公告板，所有人看到相同内容
- **每个用户独立** → 像私人办公室，保护隐私
- **每个平台独立** → 同一个用户在不同平台有不同身份

---

## 四种隔离级别

### 1. `main` — 全局共享会话

**会话键格式**: `agent:{id}:main`

**使用场景**: 所有用户共享一个单一的对话上下文。

```python
def build_session_key(agent_id, dm_scope="main", ...):
    return f"agent:{aid}:main"
```

**示例**:
- 用户 A 说: "记住我最喜欢的颜色是蓝色"
- 用户 B 问: "我最喜欢的颜色是什么？"
- 回复: "蓝色" (可以看到用户 A 的消息)

**何时使用**:
- 协作头脑风暴代理
- 公共知识库
- 共享游戏状态
- 测试/调试场景

---

### 2. `per-peer` — 按用户隔离（默认）

**会话键格式**: `agent:{id}:direct:{peer_id}`

**使用场景**: 每个用户与代理拥有自己私有的对话。

```python
def build_session_key(agent_id, peer_id, dm_scope="per-peer", ...):
    if peer_id:
        return f"agent:{aid}:direct:{peer_id}"
    return f"agent:{aid}:main"
```

**示例**:
- 用户 A (ID: user123) 说: "我正在计划去东京旅行"
- 用户 B (ID: user456) 说: "我在计划什么？"
- 对用户 B 的回复: "我没有之前的任何信息..."

**何时使用**:
- 个人助理代理（默认）
- 支持工单系统
- 私人辅导机器人
- 保密咨询

---

### 3. `per-channel-peer` — 按平台隔离

**会话键格式**: `agent:{id}:{channel}:direct:{peer_id}`

**使用场景**: 同一用户在不同平台上拥有不同的对话。

```python
def build_session_key(agent_id, channel, peer_id, dm_scope="per-channel-peer", ...):
    if peer_id:
        return f"agent:{aid}:{channel}:direct:{peer_id}"
    return f"agent:{aid}:main"
```

**示例**:
- 用户 A 在 Telegram 上: "我的 Discord 用户名是 CoolCat"
- 用户 A 在 Discord 上: "我的 Discord 用户名是什么？"
- 回复: "我不知道" (不同的会话)

**何时使用**:
- 上下文应该是平台特定的多平台代理
- 每个平台不同的人格
- 跨渠道测试不同行为
- 平台特定的功能支持

---

### 4. `per-account-channel-peer` — 最大隔离

**会话键格式**: `agent:{id}:{channel}:{account_id}:direct:{peer_id}`

**使用场景**: 支持多个机器人账户，每个账户拥有完全隔离的会话。

```python
def build_session_key(agent_id, channel, account_id, peer_id,
                      dm_scope="per-account-channel-peer"):
    if peer_id:
        return f"agent:{aid}:{channel}:{account_id}:direct:{pid}"
    return f"agent:{aid}:main"
```

**示例**:
- 机器人账户 1 在 Telegram 上的用户 A: 上下文 A1
- 机器人账户 2 在 Telegram 上的用户 A: 上下文 A2 (完全独立)

**何时使用**:
- 多租户部署
- 白标机器人服务
- 隔离的企业账户
- 最大隐私要求

---

## 对比表

| `dm_scope` | 会话键 | 隔离依据 | 使用场景 |
|------------|-------------|-------------|----------|
| `main` | `agent:{id}:main` | 无（共享） | 协作/公共代理 |
| `per-peer` | `agent:{id}:direct:{peer}` | 用户 ID | 个人助理（默认） |
| `per-channel-peer` | `agent:{id}:{ch}:direct:{peer}` | 用户 + 平台 | 多平台代理 |
| `per-account-channel-peer` | `agent:{id}:{ch}:{acc}:direct:{peer}` | 用户 + 平台 + 账户 | 多租户服务 |

---

## 配置

### 代理配置

每个代理在其配置中指定其 `dm_scope`:

```python
@dataclass
class AgentConfig:
    id: str
    name: str
    personality: str = ""
    model: str = ""
    dm_scope: str = "per-peer"  # 在此配置

# 示例代理
luna = AgentConfig(
    id="luna",
    name="Luna",
    personality="乐于助人且友好",
    dm_scope="per-peer"  # 每个用户拥有私有会话
)

sage = AgentConfig(
    id="sage",
    name="Sage",
    personality="睿智且富有哲理",
    dm_scope="main"  # 所有人共享一个会话
)
```

### 运行时行为

```python
class AgentManager:
    def get_session(self, agent_id: str, session_key: str) -> list:
        """为此键加载或创建会话。"""
        if session_key not in self._sessions:
            self._sessions[session_key] = []
        return self._sessions[session_key]

# 用法
agent = mgr.get_agent(agent_id)
session_key = build_session_key(
    agent_id=agent.id,
    channel="telegram",
    account_id="bot1",
    peer_id="user123",
    dm_scope=agent.dm_scope  # 使用代理配置的 scope
)
messages = mgr.get_session(agent_id, session_key)
```

---

## 可视化示例

### 场景：公司支持机器人

```
代理: "support-bot" 使用 dm_scope="per-peer"

用户 Alice (ID: alice@company.com)
   └─ 会话: "agent:support-bot:direct:alice@company.com"
      ├─ "我需要 VPN 方面的帮助"
      ├─ "它无法连接"
      └─ "尝试重启客户端"

用户 Bob (ID: bob@company.com)
   └─ 会话: "agent:support-bot:direct:bob@company.com"
      ├─ "我的邮箱无法正常工作"
      └─ "你检查垃圾邮件文件夹了吗？"

Alice 和 Bob 无法看到彼此的消息。
```

### 场景：群组头脑风暴代理

```
代理: "brainstorm" 使用 dm_scope="main"

会话: "agent:brainstorm:main"
   ├─ Alice: "让我们讨论一下新产品名称"
   ├─ Bob: "'Nexus' 怎么样？"
   ├─ Carol: "我更喜欢 'Apex'"
   ├─ Alice: "'Nexus' 听起来不错"
   └─ Bob: "那我们就用 Nexus 吧"

所有人都能看到完整的对话线程。
```

### 场景：多平台助理

```
代理: "assistant" 使用 dm_scope="per-channel-peer"

Alice 在 Telegram 上
   └─ 会话: "agent:assistant:telegram:direct:alice"
      ├─ "提醒我关于会议的事"
      └─ "已记录下午 3 点的会议"

Alice 在 Discord 上（同一个人，不同上下文）
   └─ 会话: "agent:assistant:discord:direct:alice"
      ├─ "我的日程表上有什么？"
      └─ "我没有任何预定的活动"

上下文是完全独立的。
```

---

## 实现细节

### 会话键构造

```python
def build_session_key(
    agent_id: str,
    channel: str = "",
    account_id: str = "",
    peer_id: str = "",
    dm_scope: str = "per-peer"
) -> str:
    """基于隔离范围构建会话键。

    参数:
        agent_id: 目标代理标识符
        channel: 平台渠道（例如 'telegram', 'discord'）
        account_id: 机器人账户标识符
        peer_id: 用户标识符
        dm_scope: 隔离粒度

    返回:
        用于查找/创建的会话键字符串
    """
    aid = normalize_agent_id(agent_id)
    ch = (channel or "unknown").strip().lower()
    acc = (account_id or "default").strip().lower()
    pid = (peer_id or "").strip().lower()

    # 最大隔离：包含所有内容
    if dm_scope == "per-account-channel-peer" and pid:
        return f"agent:{aid}:{ch}:{acc}:direct:{pid}"

    # 平台级隔离
    if dm_scope == "per-channel-peer" and pid:
        return f"agent:{aid}:{ch}:direct:{pid}"

    # 用户级隔离（默认）
    if dm_scope == "per-peer" and pid:
        return f"agent:{aid}:direct:{pid}"

    # 全局共享会话
    return f"agent:{aid}:main"
```

### 为什么使用字符串键？

会话键是字符串而不是 UUID，原因如下:

1. **人类可读**: `agent:luna:telegram:direct:alice` 对比 `a7f3d9e2-...`
2. **可调试**: 在日志中易于追踪
3. **可逆**: 可以从键中提取元数据
4. **可排序**: 在存储系统中自然排序
5. **可移植**: 跨不同的存储后端工作

---

## 最佳实践

### 1. 选择合适的范围

```python
# ❌ 对于私人助理来说范围太广
AgentConfig(id="therapist", dm_scope="main")

# ✅ 适合个人 AI
AgentConfig(id="therapist", dm_scope="per-peer")

# ❌ 对于协作工具来说范围太窄
AgentConfig(id="poll-master", dm_scope="per-account-channel-peer")

# ✅ 适合群组活动
AgentConfig(id="poll-master", dm_scope="main")
```

### 2. 优先考虑隐私

```python
# 默认为最私密的设置
DEFAULT_DM_SCOPE = "per-peer"

# 仅在明确需要时才放宽
if agent_config.get("shared_context", False):
    scope = "main"
else:
    scope = "per-peer"
```

### 3. 记录你的选择

```python
AGENT_MANIFEST = {
    "id": "support-bot",
    "dm_scope": "per-peer",
    "scope_rationale": "每个员工的工单都是私密的",
    "data_retention": "每个会话保留 30 天"
}
```

### 4. 测试上下文隔离

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

---

## 常见陷阱

### 陷阱 1: 忘记设置 dm_scope

```python
# ❌ 问题：当你想要 "main" 时使用了默认的 "per-peer"
AgentConfig(id="public-announcement")

# ✅ 解决方案：显式设置范围
AgentConfig(id="public-announcement", dm_scope="main")
```

### 陷阱 2: 跨部署范围不一致

```python
# 生产配置
dm_scope = "per-account-channel-peer"

# 开发配置（忘记更新）
dm_scope = "per-peer"

# 结果：会话在开发和生产环境表现不同
```

### 陷阱 3: 对话中途更改范围

```python
# 用户一直使用 dm_scope="per-peer" 聊天
# 键: "agent:helper:direct:alice"

# 突然更改为 dm_scope="main"
# 键变为: "agent:helper:main"

# 结果：Alice 丢失了之前会话的所有上下文
```

### 陷阱 4: 平台特定的边缘情况

```python
# 某些平台不提供稳定的 peer_id
if not peer_id:
    # 回退到 "main" 会话！
    key = build_session_key(agent_id, dm_scope="per-peer")
    # 结果：所有未识别的用户共享一个会话
```

---

## 相关概念

| 概念 | 与 `dm_scope` 的关系 |
|---------|---------------------------|
| **路由** | `dm_scope` 在路由找到代理后应用 |
| **会话** | 不同的键 = 不同的会话存储 |
| **上下文守卫** | 限制每个会话保留多少历史记录 |
| **多代理** | 每个代理可以有自己的 `dm_scope` |
| **渠道** | `channel` 参数仅影响 `per-channel-*` 范围 |

---

## 总结

`dm_scope` 参数是你的代理的**隔离调节器**:

- **`main`**: 一个房间，所有人一起交谈
- **`per-peer`** (默认): 每个用户的私人办公室
- **`per-channel-peer`**: 不同楼层上的不同办公室
- **`per-account-channel-peer`**: 具有独立翼楼的建筑

选择符合你用例隐私要求的范围。如有疑问，为了安全起见，默认为 `per-peer`。

---

> **关键洞察**: 会话隔离不仅仅是关于隐私——它还关乎用户体验。正确的范围确保用户获得他们期望的上下文，无论是个人连续性还是协作感知。
