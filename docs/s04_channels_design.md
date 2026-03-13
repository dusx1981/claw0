# S04 Channels 设计思想与核心机制详解

## 一、设计思想

### 1.1 核心理念

> "同一大脑，多个嘴巴"

**目标**：将平台差异封装在 Channel 层，使 agent 循环只处理统一的 `InboundMessage`。

```
    Telegram ----.                          .---- sendMessage API
    Feishu -------+-- InboundMessage ---+---- im/v1/messages
    CLI (stdin) --'    Agent Loop        '---- print(stdout)
                        (same brain)
```

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **平台无关性** | Agent 循环不知道消息来自哪个平台 |
| **接口最小化** | Channel 只需实现 `receive()` + `send()` |
| **消息统一化** | 所有平台消息归一化为 `InboundMessage` |
| **可扩展性** | 添加新平台 = 实现新 Channel，无需修改核心逻辑 |

### 1.3 架构分层

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Loop (核心)                         │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  run_agent_turn(inbound: InboundMessage)              │  │
│  │    ├─ 构建会话键                                       │  │
│  │    ├─ 调用 LLM API                                    │  │
│  │    ├─ 处理工具调用                                     │  │
│  │    └─ 发送回复（通过 Channel.send()）                  │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ InboundMessage / send(to, text)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Channel Layer (平台适配)                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ CLIChannel  │  │TelegramChnl │  │FeishuChnl   │         │
│  │ receive()   │  │ receive()   │  │ receive()   │         │
│  │ send()      │  │ send()      │  │ send()      │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Platform APIs                             │
│  stdin/stdout    Telegram Bot API    Feishu Open API        │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、核心数据结构

### 2.1 InboundMessage

所有平台消息归一化为统一格式：

```python
@dataclass
class InboundMessage:
    text: str                    # 消息文本内容
    sender_id: str               # 发送者 ID
    channel: str = ""            # 通道名称: "cli", "telegram", "feishu"
    account_id: str = ""         # 接收消息的 bot 账号 ID
    peer_id: str = ""            # 会话标识（DM=用户ID，群组=群ID）
    is_group: bool = False       # 是否群组消息
    media: list = field(default_factory=list)  # 媒体附件
    raw: dict = field(default_factory=dict)    # 原始平台数据
```

### 2.2 peer_id 编码规则

`peer_id` 决定了会话的范围和隔离：

| 上下文 | peer_id 格式 | 示例 |
|--------|-------------|------|
| CLI | `cli-user` | 固定值 |
| Telegram 私聊 | `user_id` | `123456789` |
| Telegram 群组 | `chat_id` | `-1001234567890` |
| Telegram 话题 | `chat_id:topic:thread_id` | `-100123:topic:456` |
| 飞书单聊 | `user_id` | `ou_xxxxx` |
| 飞书群组 | `chat_id` | `oc_xxxxx` |

### 2.3 ChannelAccount

每个 bot 的配置，支持同类型多实例：

```python
@dataclass
class ChannelAccount:
    channel: str           # 通道类型
    account_id: str        # 账号标识
    token: str = ""        # API Token
    config: dict = field(default_factory=dict)  # 额外配置
```

---

## 三、Channel 抽象基类

### 3.1 接口契约

```python
class Channel(ABC):
    name: str = "unknown"

    @abstractmethod
    def receive(self) -> InboundMessage | None:
        """接收消息，无消息时返回 None"""
        ...

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        """发送消息到指定 peer_id，返回是否成功"""
        ...

    def close(self) -> None:
        """清理资源（可选）"""
        pass
```

### 3.2 设计意义

| 方法 | 职责 | 平台差异 |
|------|------|---------|
| `receive()` | 从平台获取消息 | Telegram 长轮询、CLI 阻塞输入、飞书 webhook |
| `send()` | 发送回复到平台 | API 调用方式、消息分块、格式转换 |
| `close()` | 释放资源 | HTTP 连接关闭、文件句柄释放 |

---

## 四、CLIChannel 实现

### 4.1 最简实现

```python
class CLIChannel(Channel):
    name = "cli"

    def receive(self) -> InboundMessage | None:
        text = input("You > ").strip()
        if not text:
            return None
        return InboundMessage(
            text=text, sender_id="cli-user", channel="cli",
            account_id="cli-local", peer_id="cli-user",
        )

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        print_assistant(text)
        return True
```

### 4.2 设计意图

- **开发调试**：无需外部依赖即可测试 agent 逻辑
- **基准实现**：作为其他 Channel 的参考模板
- **REPL 模式**：支持 `/channels`、`/accounts` 等调试命令

---

## 五、TelegramChannel 核心机制

### 5.1 长轮询机制

```python
def poll(self) -> list[InboundMessage]:
    result = self._api("getUpdates", 
                       offset=self._offset, 
                       timeout=30,  # 30 秒长轮询
                       allowed_updates=["message"])
    # ...
```

**流程**：

```
┌─────────────────────────────────────────────────────────────┐
│  getUpdates(offset=N, timeout=30)                           │
│                                                             │
│  Telegram Server:                                           │
│    ├─ 有新消息？ → 立即返回 [{update_id: N, message: ...}]   │
│    └─ 30s 内无消息？ → 返回 []                               │
│                                                             │
│  Client:                                                    │
│    ├─ 收到消息 → offset = max(update_id) + 1                │
│    │              → 持久化 offset 到磁盘                     │
│    └─ 立即发起下一次 getUpdates(offset=N+1)                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 Offset 持久化

**目的**：程序重启后不会重复处理已确认的消息。

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

**存储位置**：`workspace/.state/telegram/offset-{account_id}.txt`

### 5.3 媒体组缓冲（Media Group Buffer）

**问题**：Telegram 相册会拆分成多个独立消息（同一 `media_group_id`）。

**解决方案**：500ms 缓冲窗口，合并同一 `media_group_id` 的消息。

```python
def _buf_media(self, msg: dict, update: dict) -> None:
    mgid = msg["media_group_id"]
    if mgid not in self._media_groups:
        self._media_groups[mgid] = {"ts": time.monotonic(), "entries": []}
    self._media_groups[mgid]["entries"].append((msg, update))

def _flush_media(self) -> list[InboundMessage]:
    now = time.monotonic()
    expired = [k for k, g in self._media_groups.items() 
               if (now - g["ts"]) >= 0.5]  # 500ms 窗口
    # 合并 captions 和 media...
```

### 5.4 文本合并缓冲（Text Merge Buffer）

**问题**：Telegram 会将长粘贴拆分成多个片段。

**解决方案**：1s 静默窗口，合并同一用户连续发送的文本。

```python
def _buf_text(self, inbound: InboundMessage) -> None:
    key = (inbound.peer_id, inbound.sender_id)
    now = time.monotonic()
    if key in self._text_buf:
        self._text_buf[key]["text"] += "\n" + inbound.text
        self._text_buf[key]["ts"] = now  # 重置计时器
    else:
        self._text_buf[key] = {"text": inbound.text, "msg": inbound, "ts": now}

def _flush_text(self) -> list[InboundMessage]:
    now = time.monotonic()
    expired = [k for k, b in self._text_buf.items() 
               if (now - b["ts"]) >= 1.0]  # 1s 静默
    # 返回合并后的消息...
```

### 5.5 消息分块发送

**问题**：Telegram 消息最大 4096 字符。

**解决方案**：在换行边界处智能分块。

```python
def _chunk(self, text: str) -> list[str]:
    if len(text) <= 4096:
        return [text]
    chunks = []
    while text:
        cut = text.rfind("\n", 0, 4096)  # 在换行处分割
        if cut <= 0:
            cut = 4096
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
```

### 5.6 白名单过滤

```python
raw = account.config.get("allowed_chats", "")
self.allowed_chats = {c.strip() for c in raw.split(",") if c.strip()}

# 在 poll() 中过滤
if self.allowed_chats and inbound.peer_id not in self.allowed_chats:
    continue  # 跳过未授权的消息
```

---

## 六、FeishuChannel 核心机制

### 6.1 Webhook 模式

与 Telegram 不同，飞书使用 **被动接收** 模式：

```python
def receive(self) -> InboundMessage | None:
    return None  # 飞书不主动轮询

def parse_event(self, payload: dict, token: str = "") -> InboundMessage | None:
    """解析飞书事件回调"""
    # ...
```

**实际部署时**：需要 HTTP 服务器接收飞书推送的事件。

### 6.2 Tenant Token 刷新

```python
def _refresh_token(self) -> str:
    if self._tenant_token and time.time() < self._token_expires_at:
        return self._tenant_token  # 缓存未过期
    
    resp = self._http.post(
        f"{self.api_base}/auth/v3/tenant_access_token/internal",
        json={"app_id": self.app_id, "app_secret": self.app_secret},
    )
    data = resp.json()
    self._tenant_token = data.get("tenant_access_token", "")
    self._token_expires_at = time.time() + data.get("expire", 7200) - 300  # 提前 5 分钟
    return self._tenant_token
```

### 6.3 @提及检测

**问题**：群组中 bot 只应响应 @提及的消息。

```python
def _bot_mentioned(self, event: dict) -> bool:
    for m in event.get("message", {}).get("mentions", []):
        mid = m.get("id", {})
        if isinstance(mid, dict) and mid.get("open_id") == self._bot_open_id:
            return True
    return False

# 在 parse_event() 中过滤
if is_group and self._bot_open_id and not self._bot_mentioned(event):
    return None
```

### 6.4 多消息类型解析

```python
def _parse_content(self, message: dict) -> tuple[str, list]:
    msg_type = message.get("msg_type", "text")
    content = json.loads(message.get("content", "{}"))
    
    if msg_type == "text":
        return content.get("text", ""), []
    if msg_type == "post":
        # 解析富文本
        texts = []
        for para in content.get("content", []):
            for node in para:
                if node.get("tag") == "text":
                    texts.append(node.get("text", ""))
        return "\n".join(texts), []
    if msg_type == "image":
        return "[image]", [{"type": "image", "key": content.get("image_key")}]
    # ...
```

---

## 七、ChannelManager

### 7.1 注册中心

```python
class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.accounts: list[ChannelAccount] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def close_all(self) -> None:
        for ch in self.channels.values():
            ch.close()
```

### 7.2 设计意义

- **统一管理**：所有通道的注册和获取
- **生命周期管理**：统一的 `close_all()` 清理
- **账号追踪**：`accounts` 列表用于 `/accounts` 命令

---

## 八、Agent Turn 核心流程

### 8.1 会话键构建

```python
def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    return f"agent:main:direct:{channel}:{peer_id}"
```

**设计意图**：同一用户在不同通道有独立的会话。

### 8.2 run_agent_turn 完整流程

```python
def run_agent_turn(inbound: InboundMessage, conversations: dict, mgr: ChannelManager):
    # 1. 构建会话键，获取/创建消息历史
    sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
    messages = conversations.setdefault(sk, [])
    
    # 2. 追加用户消息
    messages.append({"role": "user", "content": inbound.text})
    
    # 3. Telegram 输入指示器
    if inbound.channel == "telegram":
        tg.send_typing(inbound.peer_id)
    
    # 4. 工具循环
    while True:
        response = client.chat.completions.create(...)
        finish_reason = response.choices[0].finish_reason
        
        if finish_reason == "stop":
            # 正常结束 → 发送回复
            ch.send(inbound.peer_id, assistant_text)
            break
        elif finish_reason == "tool_calls":
            # 工具调用 → 执行后继续
            for tool_call in tool_calls:
                result = process_tool_call(...)
                messages.append({"role": "tool", ...})
            continue
        else:
            # 其他情况 → 退出
            break
```

### 8.3 关键设计点

| 设计点 | 说明 |
|--------|------|
| **会话隔离** | 按 `(channel, peer_id)` 隔离对话 |
| **输入指示** | Telegram 专属的 "typing..." 状态 |
| **通道回退** | 发送时使用原通道，确保回复到正确位置 |
| **错误恢复** | API 错误时清理最后一条消息，避免状态污染 |

---

## 九、主循环架构

### 9.1 多通道并发模型

```python
def agent_loop() -> None:
    mgr = ChannelManager()
    cli = CLIChannel()
    mgr.register(cli)

    # Telegram 后台线程
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    if tg_token:
        tg_channel = TelegramChannel(tg_acc)
        mgr.register(tg_channel)
        tg_thread = threading.Thread(
            target=telegram_poll_loop,
            args=(tg_channel, msg_queue, q_lock, stop_event),
            daemon=True,
        )
        tg_thread.start()

    conversations: dict[str, list[dict]] = {}

    while True:
        # 1. 处理 Telegram 队列消息
        with q_lock:
            tg_msgs = msg_queue[:]
            msg_queue.clear()
        for m in tg_msgs:
            run_agent_turn(m, conversations, mgr)

        # 2. 处理 CLI 输入（非阻塞模式）
        if tg_channel:
            if not select.select([sys.stdin], [], [], 0.5)[0]:
                continue  # 无输入，继续轮询 Telegram
            user_input = sys.stdin.readline().strip()
        else:
            msg = cli.receive()  # 阻塞模式
            user_input = msg.text if msg else ""

        # 3. 处理 CLI 消息
        run_agent_turn(InboundMessage(...), conversations, mgr)
```

### 9.2 线程模型

```
┌─────────────────────────────────────────────────────────────┐
│                      Main Thread                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  while True:                                           │  │
│  │    ├─ 从队列获取 Telegram 消息                         │  │
│  │    ├─ run_agent_turn(telegram_msg)                    │  │
│  │    ├─ 非阻塞检查 stdin                                │  │
│  │    └─ run_agent_turn(cli_msg)                         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │ 共享队列
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Telegram Poll Thread (daemon)              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  while not stop.is_set():                             │  │
│  │    msgs = tg.poll()                                   │  │
│  │    with lock:                                         │  │
│  │      queue.extend(msgs)                               │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 9.3 并发安全

| 机制 | 说明 |
|------|------|
| `threading.Lock` | 保护 `msg_queue` 的并发访问 |
| `daemon=True` | 主线程退出时自动终止轮询线程 |
| `stop_event` | 优雅关闭信号 |
| `select.select` | 非阻塞 stdin 检查 |

---

## 十、总结

### 10.1 核心机制一览

| 机制 | 实现位置 | 目的 |
|------|---------|------|
| **InboundMessage** | 数据结构 | 统一消息格式 |
| **Channel ABC** | 抽象基类 | 定义接口契约 |
| **Offset 持久化** | TelegramChannel | 避免重复处理 |
| **媒体组缓冲** | TelegramChannel | 合并相册消息 |
| **文本合并** | TelegramChannel | 合并片段消息 |
| **消息分块** | TelegramChannel | 应对长度限制 |
| **Token 刷新** | FeishuChannel | 保持认证有效 |
| **@提及检测** | FeishuChannel | 群组消息过滤 |
| **ChannelManager** | 注册中心 | 统一管理通道 |
| **后台轮询线程** | 主循环 | 多通道并发 |

### 10.2 扩展指南

添加新平台只需：

1. 实现 `Channel` 子类
2. 定义 `name` 属性
3. 实现 `receive()` 方法
4. 实现 `send()` 方法
5. 注册到 `ChannelManager`

**agent 循环无需任何修改。**

---

*文档生成时间：2026-03-12*