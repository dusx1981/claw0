# s04_channels.py 深度解析

> 通道 —— "同一大脑，多个嘴巴"

---

## 一、设计思想

### 1.1 核心哲学

```
Telegram ----.                          .---- sendMessage API
Feishu -------+-- InboundMessage ---+---- im/v1/messages
CLI (stdin) --'    Agent Loop        '---- print(stdout)
```

**核心认知**：Channel 封装了平台差异，使 agent 循环只看到统一的 `InboundMessage`。添加新平台 = 实现 `receive()` + `send()`；循环逻辑完全不变。

### 1.2 抽象统一原则

```python
@dataclass
class InboundMessage:
    """所有通道都规范化为此结构。Agent 循环只看到 InboundMessage。"""
    text: str
    sender_id: str
    channel: str = ""
    account_id: str = ""
    peer_id: str = ""
    is_group: bool = False
    media: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
```

**设计意图**：无论消息来自 Telegram、飞书还是 CLI，经过 Channel 转换后，agent 循环处理的是统一的数据结构。这实现了**平台无关的业务逻辑**。

### 1.3 策略模式应用

```python
class Channel(ABC):
    name: str = "unknown"

    @abstractmethod
    def receive(self) -> InboundMessage | None: ...

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool: ...
```

**设计意图**：
- 定义统一的接收/发送接口
- 每个平台实现自己的 Channel 子类
- ChannelManager 管理多个 Channel 实例
- 完全遵循**开闭原则**：扩展开放（新平台），修改封闭（核心循环）

### 1.4 异步与同步的平衡

```python
# Telegram 使用后台线程轮询
def telegram_poll_loop(tg, queue, lock, stop):
    while not stop.is_set():
        msgs = tg.poll()
        if msgs:
            with lock:
                queue.extend(msgs)

# 主循环使用队列消费
with q_lock:
    tg_msgs = msg_queue[:]
    msg_queue.clear()
```

**设计意图**：Telegram 长轮询是阻塞操作，放入后台线程执行；主循环通过线程安全的队列消费消息。这种设计避免了阻塞用户 CLI 输入。

---

## 二、实现机制

### 2.1 数据流架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        外部平台                                  │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────────────┐   │
│  │ Telegram │    │  Feishu  │    │  CLI (stdin/stdout)      │   │
│  └────┬─────┘    └────┬─────┘    └────────────┬─────────────┘   │
└───────┼───────────────┼───────────────────────┼─────────────────┘
        │               │                       │
        ▼               ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Channel 抽象层                               │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │ TelegramChannel │  │  FeishuChannel  │  │   CLIChannel    │  │
│  │  - poll()       │  │  - parse_event()│  │   - receive()   │  │
│  │  - _parse()     │  │  - send()       │  │   - send()      │  │
│  │  - send()       │  │                 │  │                 │  │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘  │
└───────────┼─────────────────────┼─────────────────────┼──────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                     InboundMessage                              │
│  {text, sender_id, channel, account_id, peer_id, is_group, ...} │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Agent 核心循环                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  run_agent_turn(inbound, conversations, mgr)            │   │
│  │    ↓                                                    │   │
│  │  build_session_key() → 查找/创建会话                    │   │
│  │    ↓                                                    │   │
│  │  messages.append(user) → API 调用                       │   │
│  │    ↓                                                    │   │
│  │  tool_calls? → 执行工具 → 继续                          │   │
│  │    ↓                                                    │   │
│  │  stop? → channel.send(response)                         │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 CLIChannel 实现

```python
class CLIChannel(Channel):
    name = "cli"

    def receive(self) -> InboundMessage | None:
        try:
            text = input(f"{CYAN}{BOLD}You > {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not text:
            return None
        return InboundMessage(
            text=text, sender_id="cli-user", channel="cli",
            account_id=self.account_id, peer_id="cli-user",
        )

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        print_assistant(text)
        return True
```

**机制**：最简单的通道实现，直接使用 stdin/stdout，用于开发测试。

### 2.3 TelegramChannel 实现

#### 2.3.1 长轮询机制

```python
def poll(self) -> list[InboundMessage]:
    result = self._api("getUpdates", offset=self._offset, timeout=30,
                       allowed_updates=["message"])
    # ...
```

**机制**：使用 Bot API 的 `getUpdates` 方法，设置 30 秒超时实现长轮询。返回的是批量的消息列表。

#### 2.3.2 偏移量持久化

```python
def save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset))

def load_offset(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:
        return 0
```

**机制**：将已处理消息的 offset 保存到文件，程序重启后不会重复处理消息。

#### 2.3.3 媒体组缓冲（500ms 窗口）

```python
def _buf_media(self, msg: dict, update: dict) -> None:
    mgid = msg["media_group_id"]
    if mgid not in self._media_groups:
        self._media_groups[mgid] = {"ts": time.monotonic(), "entries": []}
    self._media_groups[mgid]["entries"].append((msg, update))

def _flush_media(self) -> list[InboundMessage]:
    now = time.monotonic()
    expired = [k for k, g in self._media_groups.items() 
               if (now - g["ts"]) >= 0.5]
    # ...
```

**机制**：Telegram 会将相册拆分成多个消息，通过 `media_group_id` 关联。使用时间窗口缓冲，500ms 静默后合并发出。

#### 2.3.4 文本合并（1s 窗口）

```python
def _buf_text(self, inbound: InboundMessage) -> None:
    key = (inbound.peer_id, inbound.sender_id)
    now = time.monotonic()
    if key in self._text_buf:
        self._text_buf[key]["text"] += "\n" + inbound.text
        self._text_buf[key]["ts"] = now
    else:
        self._text_buf[key] = {"text": inbound.text, "msg": inbound, "ts": now}
```

**机制**：Telegram 会将长文本拆分成多个片段。使用 1 秒时间窗口合并同一用户发送的连续消息。

#### 2.3.5 消息分块发送

```python
MAX_MSG_LEN = 4096

def _chunk(self, text: str) -> list[str]:
    if len(text) <= self.MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= self.MAX_MSG_LEN:
            chunks.append(text); break
        cut = text.rfind("\n", 0, self.MAX_MSG_LEN)
        if cut <= 0:
            cut = self.MAX_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
```

**机制**：Telegram 单条消息限制 4096 字符，超长消息按换行符分块发送。

#### 2.3.6 论坛话题支持

```python
def _parse(self, msg: dict, raw_update: dict) -> InboundMessage | None:
    # ...
    if chat_type == "private":
        peer_id = user_id
    elif is_group and is_forum and thread_id is not None:
        peer_id = f"{chat_id}:topic:{thread_id}"  # 格式: 123456:topic:789
    else:
        peer_id = chat_id
```

**机制**：Telegram 群组论坛模式下，每个话题是独立的会话。使用 `chat_id:topic:thread_id` 格式区分。

### 2.4 FeishuChannel 实现

#### 2.4.1 租户令牌管理

```python
def _refresh_token(self) -> str:
    if self._tenant_token and time.time() < self._token_expires_at:
        return self._tenant_token
    resp = self._http.post(
        f"{self.api_base}/auth/v3/tenant_access_token/internal",
        json={"app_id": self.app_id, "app_secret": self.app_secret},
    )
    data = resp.json()
    self._tenant_token = data.get("tenant_access_token", "")
    self._token_expires_at = time.time() + data.get("expire", 7200) - 300
    return self._tenant_token
```

**机制**：飞书 API 使用 tenant_access_token 认证，有效期 2 小时。提前 5 分钟刷新避免过期。

#### 2.4.2 事件回调解析

```python
def parse_event(self, payload: dict, token: str = "") -> InboundMessage | None:
    if "challenge" in payload:  # 首次配置时的验证
        print_info(f"[feishu] Challenge: {payload['challenge']}")
        return None
    # ...
```

**机制**：飞书使用 Webhook 推送事件，而非轮询。`receive()` 方法返回 None，由外部 Web 服务调用 `parse_event()`。

#### 2.4.3 群聊 @ 机器人检测

```python
def _bot_mentioned(self, event: dict) -> bool:
    for m in event.get("message", {}).get("mentions", []):
        mid = m.get("id", {})
        if isinstance(mid, dict) and mid.get("open_id") == self._bot_open_id:
            return True
    return False
```

**机制**：群聊中只有 @ 机器人的消息才需要处理，避免响应所有群消息。

#### 2.4.4 富文本解析

```python
def _parse_content(self, message: dict) -> tuple[str, list]:
    msg_type = message.get("msg_type", "text")
    # text / post / image 等类型的解析逻辑
```

**机制**：飞书支持多种消息类型（文本、富文本、图片等），需要分别解析。

### 2.5 ChannelManager 实现

```python
class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.accounts: list[ChannelAccount] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel
        print_channel(f"  [+] Channel registered: {channel.name}")

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)
```

**机制**：统一管理所有通道实例，支持动态注册和查询。

### 2.6 会话键生成

```python
def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    return f"agent:main:direct:{channel}:{peer_id}"
```

**机制**：将 `(channel, account_id, peer_id)` 三元组转换为唯一的会话标识符，确保不同通道的用户有独立的会话。

### 2.7 Agent 回合处理

```python
def run_agent_turn(
    inbound: InboundMessage,
    conversations: dict[str, list[dict]],
    mgr: ChannelManager,
) -> None:
    sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
    if sk not in conversations:
        conversations[sk] = []
    messages = conversations[sk]
    messages.append({"role": "user", "content": inbound.text})
    # ... API 调用和工具处理 ...
    # 最终通过正确的通道发送回复
    ch = mgr.get(inbound.channel)
    if ch:
        ch.send(inbound.peer_id, assistant_text)
```

**机制**：
1. 根据 `inbound` 生成会话键
2. 获取或创建对应的会话历史
3. 执行 agent 循环（工具调用等）
4. 通过正确的通道回复

---

## 三、主要功能

### 3.1 多通道同时运行

**示例**：启动时自动检测环境变量，注册可用通道

```python
# CLI 通道总是可用
mgr.register(cli)

# Telegram 通道（可选）
if tg_token and HAS_HTTPX:
    mgr.register(TelegramChannel(tg_acc))

# 飞书通道（可选）
if fs_id and fs_secret and HAS_HTTPX:
    mgr.register(FeishuChannel(fs_acc))
```

**效果**：一个 agent 实例可以同时处理来自多个平台的消息。

### 3.2 独立会话隔离

**示例**：
- Telegram 用户 `@alice` 和 CLI 用户有各自的会话
- Telegram 群 `general` 和群 `random` 有各自的会话
- 同一群的不同论坛话题有各自的会话

**实现**：通过 `build_session_key()` 生成的唯一键隔离。

### 3.3 工具调用（Memory）

```python
TOOLS = [
    {"type": "function", "function": {
        "name": "memory_write", 
        "description": "Save a note to long-term memory.",
        ...
    }},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "Search through saved memory notes.",
        ...
    }},
]
```

**功能**：
- `memory_write`: 将笔记追加到 `workspace/MEMORY.md`
- `memory_search`: 在 MEMORY.md 中搜索关键词

### 3.4 REPL 命令

| 命令 | 功能 |
|------|------|
| `/channels` | 列出所有已注册的通道 |
| `/accounts` | 列出所有账号配置（token 已脱敏） |
| `/help` | 显示帮助信息 |
| `quit`/`exit` | 退出程序 |

### 3.5 Telegram "正在输入" 指示

```python
if inbound.channel == "telegram":
    tg = mgr.get("telegram")
    if isinstance(tg, TelegramChannel):
        tg.send_typing(inbound.peer_id.split(":topic:")[0])
```

**功能**：处理 Telegram 消息时，先发送 "typing" 动作，提升用户体验。

### 3.6 配置管理

**环境变量**：

| 变量 | 用途 | 必需 |
|------|------|------|
| `DASHSCOPE_API_KEY` | API 密钥 | 是 |
| `MODEL_ID` | 模型名称 | 否（默认 qwen） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | 否 |
| `TELEGRAM_ALLOWED_CHATS` | 允许的聊天 ID | 否 |
| `FEISHU_APP_ID` | 飞书应用 ID | 否 |
| `FEISHU_APP_SECRET` | 飞书应用密钥 | 否 |
| `FEISHU_ENCRYPT_KEY` | 飞书加密密钥 | 否 |
| `FEISHU_BOT_OPEN_ID` | 飞书机器人 ID | 否 |
| `FEISHU_IS_LARK` | 是否使用 Lark | 否 |

---

## 四、与前面章节的演进关系

### 4.1 架构演进对比

```
s01-s03: 单通道 CLI 架构
┌──────────────────────────────────────┐
│            CLI (stdin)               │
│                 │                     │
│                 ▼                     │
│          messages[]                  │
│                 │                     │
│                 ▼                     │
│            LLM API                   │
│                 │                     │
│                 ▼                     │
│            print()                   │
└──────────────────────────────────────┘

s04: 多通道架构
┌─────────────────────────────────────────────────────┐
│  Telegram ──┬── Feishu ──┬── CLI ──┬── (未来平台)  │
│             │            │         │               │
│             ▼            ▼         ▼               │
│     ┌─────────────────────────────────────┐        │
│     │       InboundMessage 统一结构        │        │
│     └─────────────────┬───────────────────┘        │
│                       │                            │
│                       ▼                            │
│     ┌─────────────────────────────────────┐        │
│     │  conversations[channel:peer]        │        │
│     └─────────────────┬───────────────────┘        │
│                       │                            │
│                       ▼                            │
│                    LLM API                         │
│                       │                            │
│                       ▼                            │
│     ┌─────────────────────────────────────┐        │
│     │   Channel.send(peer, response)      │        │
│     └─────────────────────────────────────┘        │
└─────────────────────────────────────────────────────┘
```

### 4.2 代码复用与变化

| 组件 | s01-s03 | s04 | 变化说明 |
|------|---------|-----|----------|
| Agent 循环 | 单一 while True | `run_agent_turn()` | 提取为函数，接受 InboundMessage |
| 消息来源 | `input()` | Channel.receive() | 抽象为接口 |
| 消息发送 | `print()` | Channel.send() | 抽象为接口 |
| 会话存储 | 单一 messages[] | `conversations[key]` | 按通道+对端隔离 |
| 工具系统 | 文件操作工具 | memory 工具 | 更适合多通道场景 |

### 4.3 关键演进点

```python
# s01-s03: 直接输入
user_input = input("You > ")

# s04: 通过通道接收
msg = cli.receive()  # 或 tg.poll() 或 fs.parse_event()
if msg is None:
    break
user_input = msg.text
```

**演进意义**：输入源从固定的 stdin 变成了可扩展的 Channel 接口。

```python
# s01-s03: 直接输出
print_assistant(assistant_text)

# s04: 通过通道发送
ch = mgr.get(inbound.channel)
if ch:
    ch.send(inbound.peer_id, assistant_text)
```

**演进意义**：输出从固定的 stdout 变成了路由到正确的通道。

---

## 五、代码结构图

```
s04_channels.py (~810 行)
│
├── 配置模块 (L18-53)
│   ├── 环境变量加载
│   ├── OpenAI 客户端初始化
│   ├── 工作目录设置
│   └── SYSTEM_PROMPT 定义
│
├── ANSI 颜色与输出函数 (L56-73)
│   ├── print_assistant()
│   ├── print_tool()
│   ├── print_info()
│   └── print_channel()
│
├── 数据结构 (L76-98)
│   ├── @dataclass InboundMessage
│   └── @dataclass ChannelAccount
│
├── 工具函数 (L101-160)
│   ├── build_session_key()
│   ├── save_offset()
│   └── load_offset()
│
├── Channel 抽象层 (L163-351)
│   ├── class Channel(ABC)
│   │   ├── @abstractmethod receive()
│   │   ├── @abstractmethod send()
│   │   └── close()
│   │
│   ├── class CLIChannel(Channel)
│   │   ├── receive()
│   │   └── send()
│   │
│   └── class TelegramChannel(Channel)
│       ├── __init__()
│       ├── _api()              # API 调用封装
│       ├── send_typing()
│       ├── poll()              # 长轮询入口
│       ├── _flush_all()
│       ├── _buf_media()        # 媒体组缓冲
│       ├── _flush_media()
│       ├── _buf_text()         # 文本合并缓冲
│       ├── _flush_text()
│       ├── _parse()            # 消息解析
│       ├── receive()
│       ├── send()
│       ├── _chunk()            # 消息分块
│       └── close()
│
├── FeishuChannel 实现 (L354-494)
│   └── class FeishuChannel(Channel)
│       ├── __init__()
│       ├── _refresh_token()    # 令牌管理
│       ├── _bot_mentioned()    # @ 检测
│       ├── _parse_content()    # 富文本解析
│       ├── parse_event()       # Webhook 事件解析
│       ├── receive()           # 返回 None
│       ├── send()
│       └── close()
│
├── 工具系统 (L497-547)
│   ├── MEMORY_FILE
│   ├── tool_memory_write()
│   ├── tool_memory_search()
│   ├── TOOLS schema
│   ├── TOOL_HANDLERS
│   └── process_tool_call()
│
├── ChannelManager (L550-570)
│   └── class ChannelManager
│       ├── __init__()
│       ├── register()
│       ├── list_channels()
│       ├── get()
│       └── close_all()
│
├── 后台线程 (L573-588)
│   └── telegram_poll_loop()    # Telegram 轮询线程
│
├── REPL 命令 (L591-608)
│   └── handle_repl_command()
│
├── Agent 核心逻辑 (L611-698)
│   └── run_agent_turn()
│       ├── 构建会话键
│       ├── 获取/创建会话
│       ├── API 调用循环
│       ├── 工具处理
│       └── 通道回复
│
├── 主循环 (L701-796)
│   └── agent_loop()
│       ├── 初始化 ChannelManager
│       ├── 注册 CLI 通道
│       ├── 注册 Telegram 通道（可选）
│       ├── 注册 Feishu 通道（可选）
│       ├── 启动 Telegram 轮询线程
│       └── 主消息循环
│           ├── 消费 Telegram 队列
│           └── 处理 CLI 输入
│
└── 入口 (L799-810)
    └── main()
```

---

## 六、关键设计模式

### 6.1 策略模式 (Strategy Pattern)

```
Channel (Strategy 接口)
    │
    ├── CLIChannel (ConcreteStrategy A)
    ├── TelegramChannel (ConcreteStrategy B)
    └── FeishuChannel (ConcreteStrategy C)
```

**应用**：每个平台的消息收发策略不同，但对外暴露统一接口。

### 6.2 适配器模式 (Adapter Pattern)

```
Telegram API ─────────┐
                      │
Feishu API ───────────┼──► InboundMessage (统一接口)
                      │
CLI stdin ────────────┘
```

**应用**：将各平台不同的数据格式适配为统一的 `InboundMessage`。

### 6.3 生产者-消费者模式

```
[Telegram 轮询线程] ──produce──► [msg_queue] ◄──consume── [主线程]
```

**应用**：Telegram 消息由后台线程生产，主线程消费，通过线程安全队列解耦。

### 6.4 模板方法模式

```python
def run_agent_turn(inbound, conversations, mgr):
    # 固定的处理流程
    sk = build_session_key(...)
    messages = conversations[sk]
    # ... API 调用 ...
    ch.send(inbound.peer_id, response)
```

**应用**：agent 处理流程固定，具体发送由 Channel 子类决定。

---

## 七、扩展指南

### 7.1 添加新通道

**步骤**：

1. 创建新的 Channel 子类：

```python
class DiscordChannel(Channel):
    name = "discord"
    
    def __init__(self, account: ChannelAccount) -> None:
        # 初始化 Discord 客户端
        pass
    
    def receive(self) -> InboundMessage | None:
        # 接收 Discord 消息，转换为 InboundMessage
        pass
    
    def send(self, to: str, text: str, **kwargs) -> bool:
        # 发送消息到 Discord
        pass
```

2. 在 `agent_loop()` 中注册：

```python
discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if discord_token:
    discord_acc = ChannelAccount(
        channel="discord", account_id="discord-primary", 
        token=discord_token,
    )
    mgr.register(DiscordChannel(discord_acc))
```

**无需修改**：`run_agent_turn()` 和其他核心逻辑完全不变。

### 7.2 添加新工具

```python
def tool_weather(city: str) -> str:
    # 查询天气
    pass

TOOLS.append({
    "type": "function",
    "function": {
        "name": "weather",
        "description": "Get weather for a city",
        "parameters": {...}
    }
})

TOOL_HANDLERS["weather"] = tool_weather
```

---

## 八、关键认知

1. **抽象的威力**：通过 `Channel` 接口和 `InboundMessage` 数据结构，将"平台差异"与"业务逻辑"彻底分离

2. **开放封闭原则**：添加新平台只需新增 Channel 子类，无需修改 agent 循环代码

3. **异步模式**：通过线程+队列实现异步消息接收，避免阻塞主循环

4. **会话隔离**：通过 `(channel, account_id, peer_id)` 三元组实现多通道会话隔离

5. **缓冲机制**：Telegram 的媒体组合并和文本合并展示了处理平台特性所需的细节处理

6. **扩展点设计**：`InboundMessage` 的 `media` 和 `raw` 字段为未来扩展预留空间