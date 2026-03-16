# OpenClaw 网关配置完整技术文档

> 生产级 AI Agent 网关配置指南：认证、路由、模型与 Agent 管理

---

## 目录

1. [概述](#1-概述)
2. [网关核心配置](#2-网关核心配置)
3. [认证机制](#3-认证机制)
4. [ACP (Agent Communication Protocol) 配置](#4-acp-agent-communication-protocol-配置)
5. [模型供应配置](#5-模型供应配置)
6. [Agent 管理配置](#6-agent-管理配置)
7. [GatewayServer 实现详解](#7-gatewayserver-实现详解)
8. [配置最佳实践](#8-配置最佳实践)
9. [故障排查](#9-故障排查)

---

## 1. 概述

OpenClaw 网关是消息路由的核心枢纽，负责将来自不同渠道（WebSocket、HTTP API、CLI 等）的入站消息分发到正确的 Agent，并管理认证、模型选择和会话上下文。

### 1.1 架构定位

```
+------------------- 外部系统 -------------------+
|                                                |
|   WebSocket 客户端    HTTP API 客户端    CLI    |
|        │                 │              │       |
+--------│-----------------│--------------│-------+
         │                 │              │
         ▼                 ▼              ▼
+--------------------------------------------------+
|              OpenClaw Gateway                    |
|  ┌────────────────────────────────────────────┐  |
|  │  认证层 (Token/Auth Profile Rotation)       │  |
|  └────────────────────────────────────────────┘  |
|  ┌────────────────────────────────────────────┐  |
|  │  ACP (Agent Communication Protocol)        │  |
|  │  - backend: acpx                           │  |
|  │  - defaultAgent: codex                     │  |
|  │  - allowedAgents: [pi, claude, codex, ...] │  |
|  └────────────────────────────────────────────┘  |
|  ┌────────────────────────────────────────────┐  |
|  │  路由层 (5 层绑定表)                          │  |
|  │  peer > guild > account > channel > default │  |
|  └────────────────────────────────────────────┘  |
+--------------------------------------------------+
         │
         ▼
+--------------------------------------------------+
|              Agent 管理层                          |
|  ┌────────────────────────────────────────────┐  |
|  │  AgentConfig: id, name, personality, model │  |
|  │  dm_scope: 会话隔离粒度                     │  |
|  └────────────────────────────────────────────┘  |
+--------------------------------------------------+
          │
          ▼
+--------------------------------------------------+
|              模型供应层                           |
|  ┌────────────────────────────────────────────┐  |
|  │  OpenAI Compatible API (VolcEngine)        │  |
|  │  Primary: volcengine-plan/ark-code-latest   │  |
|  └────────────────────────────────────────────┘  |
+--------------------------------------------------+
```

### 1.2 核心配置结构

完整网关配置 JSON 结构：

```json
{
  "gateway": {
    "port": 7777,
    "mode": "local",
    "auth": {
      "mode": "token",
      "token": "__OPENCLAW_REDACTED__"
    }
  },
  "acp": {
    "enabled": true,
    "backend": "acpx",
    "defaultAgent": "codex",
    "allowedAgents": ["pi", "claude", "codex", "opencode", "gemini", "kimi"]
  },
  "models": {
    "providers": {
      "volcengine-plan": {
        "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "apiKey": "__REDACTED__",
        "api": "openai-completions",
        "models": [
          { "id": "ark-code-latest", "name": "ark-code-latest", "contextWindow": 200000, "maxTokens": 32000 },
          { "id": "doubao-seed-2.0-code", "name": "doubao-seed-2.0-code", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "doubao-seed-2.0-pro", "name": "doubao-seed-2.0-pro", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "doubao-seed-2.0-lite", "name": "doubao-seed-2.0-lite", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "minimax-m2.5", "name": "minimax-m2.5", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "glm-4.7", "name": "glm-4.7", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "deepseek-v3.2", "name": "deepseek-v3.2", "contextWindow": 128000, "maxTokens": 32000 },
          { "id": "kimi-k2.5", "name": "kimi-k2.5", "contextWindow": 200000, "maxTokens": 32000 }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "volcengine-plan/ark-code-latest"
      },
      "models": {
        "volcengine-plan/ark-code-latest": {},
        "volcengine-plan/doubao-seed-2.0-code": {},
        "volcengine-plan/doubao-seed-2.0-pro": {},
        "volcengine-plan/doubao-seed-2.0-lite": {},
        "volcengine-plan/minimax-m2.5": {},
        "volcengine-plan/glm-4.7": {},
        "volcengine-plan/deepseek-v3.2": {},
        "volcengine-plan/kimi-k2.5": {}
      },
      "compaction": { "mode": "safeguard" },
      "maxConcurrent": 4,
      "subagents": { "maxConcurrent": 8 }
    },
    "list": [
      { "id": "main" },
      { "id": "ecommerce-agent", "name": "ecommerce-agent" }
    ]
  }
}
```

---

## 2. 网关核心配置

### 2.1 端口配置 (port)

```json
"gateway": {
  "port": 7777
}
```

**说明**：
- `port`: WebSocket 服务器监听端口
- 默认值：`8765`（开发环境）
- 生产环境推荐：`7777`

**代码参考**：[`s05_gateway_routing.py:399-406`](../sessions/zh/s05_gateway_routing.py#L399-L406)

```python
class GatewayServer:
    def __init__(self, mgr: AgentManager, bindings: BindingTable,
                 host: str = "localhost", port: int = 8765) -> None:
        self._mgr = mgr
        self._bindings = bindings
        self._host, self._port = host, port
        # ...
```

**配置建议**：

| 环境 | 端口 | 说明 |
|------|------|------|
| 开发 | 8765 | 避免与生产环境冲突 |
| 生产 | 7777 | 便于记忆和防火墙配置 |
| 容器化 | 8080 | 符合云原生惯例 |

### 2.2 运行模式 (mode)

```json
"gateway": {
  "mode": "local"
}
```

**可用模式**：

| 模式 | 说明 | 使用场景 |
|------|------|----------|
| `local` | 仅本地访问 (`localhost`) | 开发、调试 |
| `public` | 监听所有网卡 (`0.0.0.0`) | 生产部署 |
| `proxy` | 反向代理模式 (信任 X-Forwarded-For) | 位于 Nginx/Envoy 后方 |

**代码扩展示例**：

```python
def get_bind_address(mode: str) -> str:
    if mode == "local":
        return "localhost"
    elif mode == "public":
        return "0.0.0.0"
    elif mode == "proxy":
        return "0.0.0.0"  # 配合反向代理使用
    else:
        return "localhost"
```

### 2.3 完整网关配置示例

```json
{
  "gateway": {
    "port": 7777,
    "mode": "local",
    "host": "localhost",
    "ssl": {
      "enabled": false,
      "certPath": "/path/to/cert.pem",
      "keyPath": "/path/to/key.pem"
    },
    "cors": {
      "enabled": true,
      "allowedOrigins": ["*"]
    },
    "rateLimit": {
      "enabled": true,
      "requestsPerMinute": 60
    }
  }
}
```

---

## 3. 认证机制

### 3.1 Token 认证模式

```json
"gateway": {
  "auth": {
    "mode": "token",
    "token": "__OPENCLAW_REDACTED__"
  }
}
```

**认证流程**：

```
客户端请求                          网关
    │                                │
    │  ┌────────────────────────┐    │
    │  │ Authorization: Bearer  │    │
    │  │ __OPENCLAW_REDACTED__  │    │
    │  └────────────────────────┘    │
    │───────────────────────────────>│
    │                                │
    │                        ┌───────▼───────┐
    │                        │ 验证 Token    │
    │                        │ 匹配配置？    │
    │                        └───────┬───────┘
    │                                │
    │            ┌───────────────────┼───────────────────┐
    │            │                   │                   │
    │            ▼                   ▼                   ▼
    │      ✓ 认证成功           ✗ 认证失败          ✗ Token 缺失
    │      处理请求              返回 401             返回 401
    │                            {"error":            {"error":
    │                             "Invalid token"}     "Missing token"}
    │                                │
    │<───────────────────────────────┘
    │
```

**代码参考**：认证中间件实现（伪代码）

```python
class AuthMiddleware:
    def __init__(self, config: dict):
        self.mode = config.get("mode", "token")
        self.token = config.get("token")
    
    async def authenticate(self, request: dict) -> bool:
        if self.mode == "none":
            return True
        
        if self.mode == "token":
            auth_header = request.get("headers", {}).get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return False
            
            provided_token = auth_header[7:]  # 移除 "Bearer " 前缀
            return provided_token == self.token
        
        return False
```

### 3.2 安全最佳实践

**⚠️ 环境变量存储**

```bash
# .env 文件（不要提交到版本控制）
OPENCLAW_AUTH_TOKEN=sk-your-secret-token-here

# config.json
{
  "gateway": {
    "auth": {
      "mode": "token",
      "tokenEnv": "OPENCLAW_AUTH_TOKEN"
    }
  }
}
```

**⚠️ Token 生成建议**

```python
import secrets

# 生成强随机 Token
secure_token = secrets.token_urlsafe(32)
print(f"OPENCLAW_AUTH_TOKEN={secure_token}")
# 输出示例：OPENCLAW_AUTH_TOKEN=xK9mN2pQ5rT8vW1yZ4aB7cD0eF3gH6iJ
```

**Token 安全要求**：
- 长度至少 32 字符
- 使用加密安全的随机数生成器
- 包含大小写字母、数字、符号
- 定期轮换（生产环境建议每 90 天）

### 3.3 认证错误响应

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32001,
    "message": "Authentication failed: Invalid token"
  },
  "id": 1
}
```

**错误码定义**：

| 错误码 | 说明 |
|--------|------|
| -32001 | 认证失败：Token 无效 |
| -32002 | 认证失败：Token 已过期 |
| -32003 | 认证失败：Token 缺失 |
| -32004 | 授权失败：权限不足 |

---

## 4. ACP (Agent Communication Protocol) 配置

### 4.1 ACP 核心配置

```json
"acp": {
  "enabled": true,
  "backend": "acpx",
  "defaultAgent": "codex",
  "allowedAgents": ["pi", "claude", "codex", "opencode", "gemini", "kimi"]
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `enabled` | boolean | 是 | 是否启用 ACP 协议 |
| `backend` | string | 是 | 后端实现 (`acpx` | `native`) |
| `defaultAgent` | string | 是 | 默认 Agent 标识符 |
| `allowedAgents` | array | 是 | 允许使用的 Agent 列表 |

### 4.2 Backend 选项

**`acpx` 模式**：
- 扩展协议支持
- 支持多 Agent 协作
- 推荐用于生产环境

**`native` 模式**：
- 内置简化实现
- 适合开发和测试
- 功能有限

### 4.3 Agent 白名单机制

```json
"allowedAgents": ["pi", "claude", "codex", "opencode", "gemini", "kimi"]
```

**Agent 标识符规范**：

```python
import re

VALID_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

def validate_agent_id(agent_id: str) -> bool:
    """验证 Agent ID 是否符合规范"""
    return bool(VALID_AGENT_ID_RE.match(agent_id))

# 有效示例
validate_agent_id("codex")           # ✓ True
validate_agent_id("personal-assist") # ✓ True
validate_agent_id("ai_bot_001")      # ✓ True

# 无效示例
validate_agent_id("My-Bot")          # ✗ False (包含大写字母)
validate_agent_id("bot@home")        # ✗ False (包含 @)
validate_agent_id("")                # ✗ False (空字符串)
```

### 4.4 默认 Agent 回退

当路由系统未匹配到任何规则时，使用 `defaultAgent`：

```python
def resolve_with_fallback(bindings: BindingTable, acp_config: dict,
                          channel: str, peer_id: str) -> str:
    agent_id, matched = bindings.resolve(channel=channel, peer_id=peer_id)
    
    if not agent_id:
        # 无匹配规则，使用默认 Agent
        agent_id = acp_config.get("defaultAgent", "main")
        print(f"No binding matched, using default: {agent_id}")
    
    # 验证 Agent 在白名单中
    allowed = acp_config.get("allowedAgents", [])
    if allowed and agent_id not in allowed:
        raise ValueError(f"Agent '{agent_id}' not in allowed list")
    
    return agent_id
```

---

## 5. 模型供应配置

### 5.1 模型配置结构

```json
"models": {
  "providers": {
    "volcengine-plan": {
      "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
      "apiKey": "__REDACTED__",
      "api": "openai-completions",
      "models": [
        {
          "id": "ark-code-latest",
          "name": "ark-code-latest",
          "contextWindow": 200000,
          "maxTokens": 32000
        },
        {
          "id": "doubao-seed-2.0-code",
          "name": "doubao-seed-2.0-code",
          "contextWindow": 200000,
          "maxTokens": 128000
        },
        {
          "id": "doubao-seed-2.0-pro",
          "name": "doubao-seed-2.0-pro",
          "contextWindow": 200000,
          "maxTokens": 128000
        },
        {
          "id": "doubao-seed-2.0-lite",
          "name": "doubao-seed-2.0-lite",
          "contextWindow": 200000,
          "maxTokens": 128000
        },
        {
          "id": "minimax-m2.5",
          "name": "minimax-m2.5",
          "contextWindow": 200000,
          "maxTokens": 128000
        },
        {
          "id": "glm-4.7",
          "name": "glm-4.7",
          "contextWindow": 200000,
          "maxTokens": 128000
        },
        {
          "id": "deepseek-v3.2",
          "name": "deepseek-v3.2",
          "contextWindow": 128000,
          "maxTokens": 32000
        },
        {
          "id": "kimi-k2.5",
          "name": "kimi-k2.5",
          "contextWindow": 200000,
          "maxTokens": 32000
        }
      ]
    }
  }
}
```

### 5.2 Agent 模型映射

```json
"agents": {
  "defaults": {
    "model": {
      "primary": "volcengine-plan/ark-code-latest"
    },
    "models": {
      "volcengine-plan/ark-code-latest": {},
      "volcengine-plan/doubao-seed-2.0-code": {},
      "volcengine-plan/doubao-seed-2.0-pro": {},
      "volcengine-plan/doubao-seed-2.0-lite": {},
      "volcengine-plan/minimax-m2.5": {},
      "volcengine-plan/glm-4.7": {},
      "volcengine-plan/deepseek-v3.2": {},
      "volcengine-plan/kimi-k2.5": {}
    }
  }
}
```

### 5.2 默认模型配置

```json
"agents": {
  "defaults": {
    "model": {
      "primary": "volcengine-plan/ark-code-latest"
    }
  }
}
```

**代码参考**：[`s05_gateway_routing.py:46-50`](../sessions/zh/s05_gateway_routing.py#L46-L50)（教学代码使用 `claude-sonnet-4-20250514`，生产环境使用 `volcengine-plan/ark-code-latest`）

```python
MODEL_ID = os.getenv("MODEL_ID", "claude-sonnet-4-20250514")  # 教学代码默认
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
```

### 5.3 模型回退机制

生产环境应配置回退模型，防止单点故障：

```python
class ModelRouter:
    def __init__(self, config: dict):
        # 从 agents.defaults.model.primary 获取默认模型
        primary_model = config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "volcengine-plan/ark-code-latest")
        self.default = primary_model
        self.max_attempts = config.get("maxRetries", 3)
    
    def get_model(self, use_fallback: bool = False) -> str:
        if use_fallback and self.attempts < self.max_attempts:
            self.attempts += 1
            return self.fallback
        return self.default
    
    def reset(self):
        self.attempts = 0
```

### 5.4 提供商配置

**阿里云百炼 (DashScope)**：

```json
{
  "providers": {
    "dashscope": {
      "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "apiKeyEnv": "DASHSCOPE_API_KEY",
      "models": {
        "qwen-turbo": {"cost": 0.002, "context": 32000},
        "qwen-plus": {"cost": 0.004, "context": 32000},
        "qwen-max": {"cost": 0.02, "context": 32000}
      }
    }
  }
}
```

**环境变量配置**：

```bash
# .env
VOLCENGINE_API_KEY=ark-xxxxxxxxxxxxxxxx
# 使用 Volcano Engine Ark 作为默认 provider
```

---

## 6. Agent 管理配置

### 6.1 Agent 基础配置

```json
"agents": {
  "defaults": {
    "model": {
      "primary": "volcengine-plan/ark-code-latest"
    },
    "models": {
      "volcengine-plan/ark-code-latest": {},
      "volcengine-plan/doubao-seed-2.0-code": {},
      "volcengine-plan/doubao-seed-2.0-pro": {}
    },
    "compaction": {
      "mode": "safeguard"
    },
    "maxConcurrent": 4,
    "subagents": {
      "maxConcurrent": 8
    }
  },
  "list": [
    { "id": "main" },
    { "id": "ecommerce-agent", "name": "ecommerce-agent" }
  ]
}
```

**字段说明**：

| 字段 | 说明 | 示例值 |
|------|------|--------|
| `defaults.model.primary` | 默认模型 | `volcengine-plan/ark-code-latest` |
| `defaults.compaction.mode` | 上下文压缩模式 | `safeguard` |
| `defaults.maxConcurrent` | 最大并发数 | `4` |
| `agentsDir` | Agent 配置目录 | `./workspace/.agents` |

### 6.2 AgentConfig 数据结构

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

### 6.3 Agent 注册示例

```python
from s05_gateway_routing import AgentManager, AgentConfig

mgr = AgentManager()

# 注册客服机器人
mgr.register(AgentConfig(
    id="customer-support",
    name="客服小助手",
    personality="友好、耐心、专业。擅长处理客户投诉和咨询。",
    model="qwen-max",
    dm_scope="per-peer"
))

# 注册公告机器人
mgr.register(AgentConfig(
    id="announcement-bot",
    name="公告机器人",
    personality="正式、简洁。只发布重要通知。",
    dm_scope="main"  # 所有人共享会话
))

# 注册技术专家
mgr.register(AgentConfig(
    id="tech-expert",
    name="技术专家",
    personality="直接、分析性强、简洁。偏好事实而非观点。",
    model="claude-sonnet-4-20250514",
    dm_scope="per-channel-peer"  # 按平台隔离
))
```

### 6.4 dm_scope 会话隔离

**四级隔离粒度**：

| dm_scope | 会话键格式 | 使用场景 |
|----------|-------------|----------|
| `main` | `agent:{id}:main` | 公共公告板、协作工具 |
| `per-peer` | `agent:{id}:direct:{peer}` | 个人助理（默认） |
| `per-channel-peer` | `agent:{id}:{ch}:direct:{peer}` | 多平台代理 |
| `per-account-channel-peer` | `agent:{id}:{ch}:{acc}:direct:{peer}` | 多租户服务 |

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

### 6.5 AgentManager 管理

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
        # 创建 Agent 工作目录
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

---

## 7. GatewayServer 实现详解

### 7.1 类结构

**代码参考**：[`s05_gateway_routing.py:399-506`](../sessions/zh/s05_gateway_routing.py#L399-L506)

```python
class GatewayServer:
    """WebSocket 网关服务器，实现 JSON-RPC 2.0 协议"""
    
    def __init__(self, mgr: AgentManager, bindings: BindingTable,
                 host: str = "localhost", port: int = 8765) -> None:
        self._mgr = mgr              # Agent 管理器
        self._bindings = bindings    # 路由绑定表
        self._host, self._port = host, port
        self._clients: set[Any] = set()      # 已连接的客户端
        self._start_time = time.monotonic()  # 启动时间戳
        self._server: Any = None             # WebSocket 服务器实例
        self._running = False                # 运行状态标志
```

### 7.2 启动与停止

```python
async def start(self) -> None:
    """启动 WebSocket 服务器"""
    try:
        import websockets
    except ImportError:
        print("websockets not installed. pip install websockets")
        return
    
    self._start_time = time.monotonic()
    self._running = True
    self._server = await websockets.serve(
        self._handle, self._host, self._port
    )
    print(f"Gateway started ws://{self._host}:{self._port}")

async def stop(self) -> None:
    """停止 WebSocket 服务器"""
    if self._server:
        self._server.close()
        await self._server.wait_closed()
        self._running = False
```

### 7.3 连接处理

```python
async def _handle(self, ws: Any, path: str = "") -> None:
    """处理单个 WebSocket 连接"""
    self._clients.add(ws)
    try:
        async for raw in ws:
            resp = await self._dispatch(raw)
            if resp:
                await ws.send(json.dumps(resp))
    except Exception:
        pass  # 静默处理客户端断开
    finally:
        self._clients.discard(ws)
```

### 7.4 JSON-RPC 2.0 分发

```python
async def _dispatch(self, raw: str) -> dict | None:
    """解析并分发 JSON-RPC 请求"""
    try:
        req = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error"},
            "id": None
        }
    
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})
    
    # 方法映射表
    methods = {
        "send": self._m_send,
        "bindings.set": self._m_bind_set,
        "bindings.list": self._m_bind_list,
        "sessions.list": self._m_sessions,
        "agents.list": self._m_agents,
        "status": self._m_status,
    }
    
    handler = methods.get(method)
    if not handler:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
            "id": rid
        }
    
    try:
        result = await handler(params)
        return {"jsonrpc": "2.0", "result": result, "id": rid}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": str(exc)},
            "id": rid
        }
```

### 7.5 核心方法实现

#### 7.5.1 send - 发送消息

```python
async def _m_send(self, p: dict) -> dict:
    """处理消息发送请求"""
    text = p.get("text", "")
    if not text:
        raise ValueError("text is required")
    
    channel = p.get("channel", "websocket")
    peer_id = p.get("peer_id", "ws-client")
    
    # 路由解析
    if p.get("agent_id"):
        aid = normalize_agent_id(p["agent_id"])
        agent = self._mgr.get_agent(aid)
        dm_scope = agent.dm_scope if agent else "per-peer"
        sk = build_session_key(aid, channel=channel, peer_id=peer_id, dm_scope=dm_scope)
    else:
        aid, sk = resolve_route(self._bindings, self._mgr, channel, peer_id)
    
    # 运行 Agent
    reply = await run_agent(self._mgr, aid, sk, text, on_typing=self._typing_cb)
    
    return {
        "agent_id": aid,
        "session_key": sk,
        "reply": reply
    }
```

#### 7.5.2 bindings.set - 设置绑定

```python
async def _m_bind_set(self, p: dict) -> dict:
    """添加路由绑定规则"""
    binding = Binding(
        agent_id=normalize_agent_id(p.get("agent_id", "")),
        tier=int(p.get("tier", 5)),
        match_key=p.get("match_key", "default"),
        match_value=p.get("match_value", "*"),
        priority=int(p.get("priority", 0))
    )
    self._bindings.add(binding)
    return {"ok": True, "binding": binding.display()}
```

#### 7.5.3 status - 获取状态

```python
async def _m_status(self, p: dict) -> dict:
    """获取服务器运行状态"""
    return {
        "running": self._running,
        "uptime_seconds": round(time.monotonic() - self._start_time, 1),
        "connected_clients": len(self._clients),
        "agent_count": len(self._mgr.list_agents()),
        "binding_count": len(self._bindings.list_all())
    }
```

### 7.6 Typing 指示器

```python
def _typing_cb(self, agent_id: str, typing: bool) -> None:
    """向所有客户端广播 typing 状态"""
    msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "typing",
        "params": {"agent_id": agent_id, "typing": typing}
    })
    for ws in list(self._clients):
        try:
            asyncio.ensure_future(ws.send(msg))
        except Exception:
            self._clients.discard(ws)
```

### 7.7 WebSocket 客户端示例

```javascript
// 连接网关
const ws = new WebSocket('ws://localhost:7777');

// 认证握手
ws.onopen = () => {
    console.log('Connected to Gateway');
};

// 发送消息
function sendMessage(text, channel = 'websocket', peerId = 'client-1') {
    ws.send(JSON.stringify({
        jsonrpc: "2.0",
        method: "send",
        params: {
            text: text,
            channel: channel,
            peer_id: peerId
        },
        id: Date.now()
    }));
}

// 接收响应
ws.onmessage = (event) => {
    const response = JSON.parse(event.data);
    
    if (response.method === 'typing') {
        const { agent_id, typing } = response.params;
        console.log(`${agent_id} is ${typing ? 'typing...' : 'stopped'}`);
        return;
    }
    
    if (response.result) {
        console.log('Reply:', response.result.reply);
    }
    
    if (response.error) {
        console.error('Error:', response.error.message);
    }
};

// 监听 Typing 状态
ws.addEventListener('message', (event) => {
    const data = JSON.parse(event.data);
    if (data.method === 'typing') {
        updateTypingIndicator(data.params);
    }
});
```

---

## 8. 配置最佳实践

### 8.1 环境分离

**开发环境配置**：

```json
{
  "gateway": {
    "port": 8765,
    "mode": "local",
    "auth": {
      "mode": "token",
      "token": "dev-token-not-for-production"
    }
  },
  "acp": {
    "enabled": true,
    "backend": "native",
    "defaultAgent": "main",
    "allowedAgents": ["main", "test"]
  }
}
```

**生产环境配置**：

```json
{
  "gateway": {
    "port": 7777,
    "mode": "proxy",
    "auth": {
      "mode": "token",
      "tokenEnv": "OPENCLAW_AUTH_TOKEN"
    },
    "ssl": {
      "enabled": true,
      "certPath": "/etc/ssl/certs/openclaw.crt",
      "keyPath": "/etc/ssl/private/openclaw.key"
    },
    "rateLimit": {
      "enabled": true,
      "requestsPerMinute": 60
    }
  },
  "acp": {
    "enabled": true,
    "backend": "acpx",
    "defaultAgent": "codex",
    "allowedAgents": ["codex", "pi", "claude"]
  }
}
```

### 8.2 安全配置清单

- [ ] 使用环境变量存储 Token
- [ ] 生产环境启用 SSL/TLS
- [ ] 配置速率限制
- [ ] 限制 `allowedAgents` 白名单
- [ ] 定期轮换认证 Token
- [ ] 记录所有认证失败尝试
- [ ] 禁用调试日志输出

### 8.3 路由规则设计原则

1. **具体规则优先**
   ```json
   [
     {"tier": 1, "match_key": "peer_id", "match_value": "admin-001"},
     {"tier": 4, "match_key": "channel", "match_value": "telegram"},
     {"tier": 5, "match_key": "default", "match_value": "*"}
   ]
   ```

2. **设置兜底规则**
   ```python
   # 必须始终存在 default 规则
   bt.add(Binding(agent_id="main", tier=5, match_key="default", match_value="*"))
   ```

3. **同层优先级控制**
   ```json
   [
     {"tier": 1, "match_value": "vip-user", "priority": 10},
     {"tier": 1, "match_value": "regular-user", "priority": 1}
   ]
   ```

### 8.4 会话隔离选择指南

```python
# 场景：个人 AI 助理
AgentConfig(id="personal-assistant", dm_scope="per-peer")

# 场景：公共知识库
AgentConfig(id="knowledge-base", dm_scope="main")

# 场景：多平台客服
AgentConfig(id="support-bot", dm_scope="per-channel-peer")

# 场景：企业多租户
AgentConfig(id="enterprise-bot", dm_scope="per-account-channel-peer")
```

### 8.5 监控与日志

**建议监控指标**：

| 指标 | 告警阈值 | 说明 |
|------|----------|------|
| 连接数 | > 1000 | 并发客户端过多 |
| 请求延迟 | > 5s | API 响应缓慢 |
| 认证失败率 | > 10% | 可能存在攻击 |
| Agent 错误率 | > 5% | 模型调用异常 |

**日志配置示例**：

```json
{
  "logging": {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": "/var/log/openclaw/gateway.log",
    "maxSize": "100MB",
    "backupCount": 7
  }
}
```

---

## 9. 故障排查

### 9.1 常见问题

**问题 1：无法连接 WebSocket**

```
错误：Connection refused
原因：端口配置不匹配
解决：确认客户端连接端口与 gateway.port 一致
```

**问题 2：认证失败**

```json
{"error": {"code": -32001, "message": "Invalid token"}}
```

解决步骤：
1. 检查 Authorization 头格式：`Bearer <token>`
2. 确认 token 与配置一致
3. 检查环境变量是否正确加载

**问题 3：Agent 未找到**

```json
{"error": {"message": "agent 'xxx' not found"}}
```

解决步骤：
1. 调用 `agents.list` 确认 Agent 已注册
2. 检查 `allowedAgents` 白名单
3. 验证 Agent ID 格式规范

### 9.2 调试命令

```javascript
// 获取服务器状态
ws.send(JSON.stringify({
    jsonrpc: "2.0",
    method: "status",
    id: 1
}));

// 列出所有 Agent
ws.send(JSON.stringify({
    jsonrpc: "2.0",
    method: "agents.list",
    id: 2
}));

// 列出所有绑定
ws.send(JSON.stringify({
    jsonrpc: "2.0",
    method: "bindings.list",
    id: 3
}));
```

### 9.3 性能调优

**并发控制**：

```python
# 调整信号量大小
_agent_semaphore = asyncio.Semaphore(8)  # 默认 4，根据 API 限流调整
```

**连接池配置**：

```python
# 优化 HTTP 客户端连接池
client = OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=30.0,
    max_retries=3,
)
```

---

## 附录 A：完整配置示例

```json
{
  "gateway": {
    "port": 7777,
    "mode": "local",
    "auth": {
      "mode": "token",
      "token": "__OPENCLAW_REDACTED__"
    }
  },
  "acp": {
    "enabled": true,
    "backend": "acpx",
    "defaultAgent": "codex",
    "allowedAgents": ["pi", "claude", "codex", "opencode", "gemini", "kimi"]
  },
  "models": {
    "providers": {
      "volcengine-plan": {
        "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "apiKey": "__OPENCLAW_REDACTED__",
        "api": "openai-completions",
        "models": [
          { "id": "ark-code-latest", "name": "ark-code-latest", "contextWindow": 200000, "maxTokens": 32000 },
          { "id": "doubao-seed-2.0-code", "name": "doubao-seed-2.0-code", "contextWindow": 200000, "maxTokens": 128000 },
          { "id": "doubao-seed-2.0-pro", "name": "doubao-seed-2.0-pro", "contextWindow": 200000, "maxTokens": 128000 }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "volcengine-plan/ark-code-latest"
      },
      "compaction": { "mode": "safeguard" },
      "maxConcurrent": 4,
      "subagents": { "maxConcurrent": 8 }
    },
    "list": [
      { "id": "main" },
      { "id": "ecommerce-agent", "name": "ecommerce-agent" }
    ]
  }
}
```

---

## 附录 B：参考文档

- [s05_gateway_routing.py](../sessions/zh/s05_gateway_routing.py) - 完整实现代码
- [网关路由与会话隔离](./gateway_routing_isolation.zh.md) - 路由机制详解
- [s05 网关分析](./s05_gateway_routing_analysis.md) - 代码深度解析

---

> **最后更新**: 2026-03-16
> **文档版本**: 1.0
> **适用版本**: claw0 s05+
