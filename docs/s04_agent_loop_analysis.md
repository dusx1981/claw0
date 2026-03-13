# s04_channels.py agent_loop 深度解析

> Agent 循环 —— "多通道统一处理，同源不同流"

---

## 一、设计思想

### 1.1 核心哲学

```
┌─────────────────────────────────────────────┐
│         主循环 (agent_loop)                 │
│  ┌─────────────────────────────────────┐    │
│  │  while True:                       │    │
│  │    - 消费消息队列                  │    │
│  │    - 处理 CLI 输入                 │    │
│  │    - 调用 run_agent_turn()         │    │
│  └─────────────────────────────────────┘    │
└─────────────────────┬───────────────────────┘
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   Telegram      │ │    Feishu       │ │     CLI         │
│ 后台轮询线程     │ │  Webhook事件     │ │  阻塞式输入     │
│ 队列消费        │ │  外部调用       │ │ 直接处理        │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

**核心认知**：`agent_loop` 是整个多通道系统的调度中心，它协调不同通道的输入方式（异步轮询、阻塞输入、外部事件），统一通过 `run_agent_turn()` 处理，并将响应路由回正确的通道。这实现了**统一的处理逻辑，分离的输入输出机制**。

### 1.2 线程模型设计

```python
# 主线程：agent_loop() - 协调度量器
while True:
    # 1. 消费 Telegram 消息队列（非阻塞）
    with q_lock:
        tg_msgs = msg_queue[:]
        msg_queue.clear()
    for m in tg_msgs:
        run_agent_turn(m, conversations, mgr)

    # 2. 处理 CLI 输入（可阻塞）
    if tg_channel:
        # 非阻塞检查 stdin
        if select.select([sys.stdin], [], [], 0.5)[0]:
            user_input = sys.stdin.readline().strip()
            # ... 处理 CLI 消息
    else:
        # 阻塞式接收
        msg = cli.receive()
```

**设计意图**：
- **主线程**：负责整体调度和消息处理，避免被任何单个通道阻塞
- **后台线程**：专门处理 Telegram 长轮询等阻塞操作
- **线程安全**：通过 `q_lock` 保护共享的 `msg_queue`
- **优雅退出**：使用 `stop_event` 通知后台线程终止

### 1.3 通道抽象原则

```python
class Channel(ABC):
    @abstractmethod
    def receive(self) -> InboundMessage | None: ...
    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool: ...

# CLIChannel 实现
def receive(self) -> InboundMessage | None:
    text = input("You > ")  # 阻塞式
    return InboundMessage(text=text, ...)

# TelegramChannel 实现  
def receive(self) -> InboundMessage | None:
    msgs = self.poll()  # 批量获取
    return msgs[0] if msgs else None

# FeishuChannel 实现
def receive(self) -> InboundMessage | None:
    return None  # Webhook模式，由外部调用 parse_event()
```

**设计意图**：不同的通道有不同的消息获取模式，但都提供统一的 `receive()` 接口。主循环只关心调用这个接口，而不关心具体实现细节。

### 1.4 会话隔离策略

```python
def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    return f"agent:main:direct:{channel}:{peer_id}"

# 在 run_agent_turn 中使用
sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
if sk not in conversations:
    conversations[sk] = []
messages = conversations[sk]
```

**设计意图**：确保不同通道、不同用户的会话完全隔离，避免上下文污染。每个 `(channel, peer_id)` 组合都有独立的对话历史。

---

## 二、执行流程

### 2.1 主循环启动流程

```python
def agent_loop() -> None:
    # 1. 初始化 ChannelManager
    mgr = ChannelManager()
    
    # 2. 注册 CLI 通道（始终启用）
    cli = CLIChannel()
    mgr.register(cli)
    
    # 3. 条件注册 Telegram 通道
    if tg_token and HAS_HTTPX:
        tg_acc = ChannelAccount(...)
        mgr.accounts.append(tg_acc)
        tg_channel = TelegramChannel(tg_acc)
        mgr.register(tg_channel)
        # 启动后台轮询线程
        tg_thread = threading.Thread(target=telegram_poll_loop, ...)
        tg_thread.start()
    
    # 4. 条件注册 Feishu 通道  
    if fs_id and fs_secret and HAS_HTTPX:
        fs_acc = ChannelAccount(...)
        mgr.accounts.append(fs_acc)
        mgr.register(FeishuChannel(fs_acc))
    
    # 5. 主循环
    conversations = {}
    while True:
        # ... 消息处理循环
```

### 2.2 消息处理完整流程

#### 2.2.1 Telegram 消息路径

```
Telegram API
    ↓
telegram_poll_loop() [后台线程]
    ↓  
tg.poll() → _parse() → InboundMessage
    ↓
msg_queue.append() [带锁]
    ↓
agent_loop() [主线程] 
    ↓
with q_lock: msg_queue.copy_and_clear()
    ↓
for m in tg_msgs: run_agent_turn(m, conversations, mgr)
    ↓
build_session_key() → conversations[key]
    ↓
API 调用 → 工具处理 → 响应生成
    ↓
mgr.get("telegram").send(peer_id, response)
```

#### 2.2.2 CLI 消息路径

```
stdin (用户输入)
    ↓
select() 检查 or input() 阻塞 [主线程]
    ↓
CLIChannel.receive() → InboundMessage  
    ↓
run_agent_turn(msg, conversations, mgr)
    ↓
build_session_key() → conversations[key]  
    ↓
API 调用 → 工具处理 → 响应生成
    ↓
print_assistant(response) 或 mgr.get("cli").send()
```

#### 2.2.3 Feishu 消息路径

```
Feishu Webhook [外部 HTTP 服务]
    ↓
POST /webhook → parse_event(payload)
    ↓  
InboundMessage 构造
    ↓
run_agent_turn(msg, conversations, mgr) [直接调用]
    ↓
build_session_key() → conversations[key]
    ↓  
API 调用 → 工具处理 → 响应生成  
    ↓
mgr.get("feishu").send(peer_id, response)
```

### 2.3 run_agent_turn 执行流程

```python
def run_agent_turn(inbound, conversations, mgr):
    # 1. 构建会话键，获取对话历史
    sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
    messages = conversations.setdefault(sk, [])
    messages.append({"role": "user", "content": inbound.text})
    
    # 2. Telegram 特殊处理：发送 typing 指示
    if inbound.channel == "telegram":
        tg.send_typing(inbound.peer_id.split(":topic:")[0])
    
    # 3. LLM API 调用循环
    while True:
        try:
            # 构造 API 消息
            api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            response = client.chat.completions.create(model=MODEL_ID, tools=TOOLS, messages=api_messages)
            
            # 4. 处理 API 响应
            assistant_message = response.choices[0].message
            assistant_text = assistant_message.content or ""
            tool_calls = assistant_message.tool_calls or []
            
            # 5. 根据 finish_reason 决定下一步
            finish_reason = response.choices[0].finish_reason
            
            if finish_reason == "stop":
                # 正常结束，发送响应
                if assistant_text:
                    ch = mgr.get(inbound.channel)
                    if ch:
                        ch.send(inbound.peer_id, assistant_text)
                    else:
                        print_assistant(assistant_text, inbound.channel)
                break
                
            elif finish_reason == "tool_calls" and tool_calls:
                # 工具调用，执行工具并继续循环
                for tool_call in tool_calls:
                    result = process_tool_call(tool_call.function.name, tool_call.function.arguments)
                    # 添加工具调用和结果到对话历史
                    messages.append({...})  # tool call
                    messages.append({...})  # tool result
                    
            else:
                # 其他情况，发送响应
                if assistant_text:
                    ch = mgr.get(inbound.channel)
                    if ch:
                        ch.send(inbound.peer_id, assistant_text)
                break
                
        except Exception as exc:
            # API 错误处理：清理对话历史，退出循环
            print(f"API Error: {exc}")
            # 移除最后的非用户消息
            while messages and messages[-1]["role"] != "user":
                messages.pop()
            if messages:
                messages.pop()  # 移除触发错误的用户消息
            return
```

### 2.4 程序终止流程

```python
# 1. 用户输入 quit/exit
if user_input.lower() in ("quit", "exit"):
    break

# 2. 主循环退出后
print("Goodbye.")
stop_event.set()  # 通知 Telegram 轮询线程停止

# 3. 等待后台线程优雅退出
if tg_thread and tg_thread.is_alive():
    tg_thread.join(timeout=3.0)

# 4. 关闭所有通道
mgr.close_all()
```

---

## 三、关键技术点

### 3.1 非阻塞 I/O 设计

```python
# 当 Telegram 通道启用时，使用非阻塞 select
if tg_channel:
    import select
    if not select.select([sys.stdin], [], [], 0.5)[0]:
        continue  # 超时，继续处理其他任务
    user_input = sys.stdin.readline().strip()
else:
    # 无 Telegram 时，使用阻塞式输入
    msg = cli.receive()
```

**优势**：允许程序同时处理多个输入源，不会因为等待 CLI 输入而错过 Telegram 消息。

### 3.2 线程安全的消息传递

```python
# 共享数据结构
msg_queue: list[InboundMessage] = []
q_lock = threading.Lock()

# 生产者（后台线程）
def telegram_poll_loop(...):
    while not stop.is_set():
        msgs = tg.poll()
        if msgs:
            with lock:  # 线程安全写入
                queue.extend(msgs)

# 消费者（主线程）  
with q_lock:  # 线程安全读取和清空
    tg_msgs = msg_queue[:]
    msg_queue.clear()
```

**优势**：简单有效的生产者-消费者模式，避免复杂的队列实现。

### 3.3 通道动态发现

```python
# 根据环境变量动态启用通道
channels_enabled = []

# CLI 总是启用
channels_enabled.append("cli")

# 条件启用 Telegram
if os.getenv("TELEGRAM_BOT_TOKEN"):
    channels_enabled.append("telegram")
    
# 条件启用 Feishu  
if os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"):
    channels_enabled.append("feishu")

print_info(f"Channels: {', '.join(channels_enabled)}")
```

**优势**：零配置默认工作，按需启用高级功能。

### 3.4 错误恢复机制

```python
except Exception as exc:
    print(f"API Error: {exc}")
    # 清理对话历史
    while messages and messages[-1]["role"] != "user":
        messages.pop()
    if messages:
        messages.pop()  # 移除问题消息
    return  # 退出当前回合，不影响其他会话
```

**优势**：单个会话的错误不影响整个系统，保持服务可用性。

### 3.5 内存管理

```python
# conversations 字典的生命周期
conversations: dict[str, list[dict]] = {}  # 全局变量，随程序运行

# 会话键基于 (channel, peer_id)，自然隔离
# 无显式清理机制，依赖程序重启清理
```

**注意**：长期运行可能导致内存增长，但在教学场景下可以接受。

---

## 四、与前面章节的演进关系

### 4.1 架构演进对比

```
s01: 单通道同步架构
┌─────────────────┐
│     input()     │
│        ↓        │  
│  messages[]     │
│        ↓        │
│   LLM API       │
│        ↓        │
│    print()      │
└─────────────────┘

s04: 多通道异步架构  
┌───────────────────────────────────────────┐
│          agent_loop()                     │
│  ┌───────────────────────────────────┐    │
│  │  Telegram 线程 → 队列             │    │
│  │  CLI 输入 ←→ 非阻塞 select        │    │
│  │  Feishu ←─ 外部 Webhook           │    │
│  └─────────────────┬─────────────────┘    │
│                    ↓                      │
│           run_agent_turn()               │
│                    ↓                      │
│           Channel.send()                 │
└───────────────────────────────────────────┘
```

### 4.2 核心变化点

| 组件 | s01-s03 | s04 | 变化说明 |
|------|---------|-----|----------|
| **消息来源** | 单一 stdin | 多通道 (CLI/Telegram/Feishu) | 引入 Channel 抽象层 |
| **执行模型** | 单线程阻塞 | 主线程+后台线程 | 支持异步消息接收 |
| **会话管理** | 单一对话历史 | 多会话隔离 | 基于 channel+peer_id 的键 |
| **响应路由** | 固定 stdout | 动态通道路由 | 根据消息来源选择响应通道 |
| **错误影响** | 整个程序崩溃 | 单一会话隔离 | 错误范围限制 |

### 4.3 代码复用分析

```python
# s01-s03: 直接的循环结构
while True:
    user_input = input("> ")
    # ... API 调用
    print(response)

# s04: 提取为函数，支持复用
def run_agent_turn(inbound, conversations, mgr):
    # ... 相同的 API 调用逻辑
    # ... 相同的工具处理逻辑

# 主循环专注于消息调度
while True:
    # 处理不同来源的消息
    for msg in get_messages_from_all_sources():
        run_agent_turn(msg, conversations, mgr)
```

**演进意义**：将业务逻辑 (`run_agent_turn`) 与基础设施逻辑 (`agent_loop`) 分离，提高代码复用性和可维护性。

---

## 五、实际执行示例

### 5.1 CLI 交互示例

```
$ python sessions/zh/s04_channels.py
============================================================
  claw0  |  Section 04: Channels
  Model: qwen-max
  Channels: cli, telegram
  Commands: /channels /accounts /help  |  quit/exit
============================================================

You > Hello!

Assistant: Hello! How can I help you today?

You > /channels

  - cli
  - telegram

You > quit
Goodbye.
```

### 5.2 Telegram 交互示例

假设已配置 `TELEGRAM_BOT_TOKEN`：

1. **程序启动**：自动注册 Telegram 通道，启动后台轮询线程
2. **用户发送消息**：`@mybot Hello from Telegram!`
3. **后台线程**：通过 Telegram Bot API 接收到消息
4. **消息处理**：
   - 解析为 `InboundMessage(text="Hello from Telegram!", channel="telegram", ...)`
   - 添加到 `msg_queue`
5. **主线程**：在下一次循环中消费队列
6. **Agent 处理**：调用 `run_agent_turn()`，生成响应
7. **响应发送**：通过 `TelegramChannel.send()` 发回 Telegram

### 5.3 混合通道示例

```python
# 同时处理 CLI 和 Telegram 消息
while True:
    # 检查 Telegram 队列（非阻塞）
    with q_lock:
        tg_msgs = msg_queue[:]  
        msg_queue.clear()
    
    # 处理 Telegram 消息
    for m in tg_msgs:
        print(f"[telegram] {m.sender_id}: {m.text[:50]}")
        run_agent_turn(m, conversations, mgr)
    
    # 检查 CLI 输入（非阻塞，200ms 超时）  
    if select.select([sys.stdin], [], [], 0.2)[0]:
        user_input = sys.stdin.readline().strip()
        if user_input == "quit":
            break
        # 处理 CLI 消息
        run_agent_turn(InboundMessage(text=user_input, channel="cli", ...), conversations, mgr)
```

**效果**：用户可以在 CLI 中与机器人交互的同时，Telegram 用户也能得到及时响应，两者互不影响。

---

## 六、扩展与优化

### 6.1 添加新通道

要添加 Discord 通道：

```python
class DiscordChannel(Channel):
    name = "discord"
    
    def __init__(self, account: ChannelAccount):
        # 初始化 Discord 客户端
        pass
        
    def receive(self) -> InboundMessage | None:
        # Discord 消息接收逻辑
        pass
        
    def send(self, to: str, text: str, **kwargs) -> bool:
        # Discord 消息发送逻辑  
        pass

# 在 agent_loop() 中注册
if discord_token := os.getenv("DISCORD_TOKEN"):
    discord_acc = ChannelAccount(channel="discord", account_id="discord-primary", token=discord_token)
    mgr.register(DiscordChannel(discord_acc))
```

**无需修改**：`run_agent_turn()` 和其他核心逻辑。

### 6.2 性能优化点

1. **会话清理**：添加 LRU 缓存或 TTL 过期机制
2. **批量处理**：Telegram 队列可以批量处理多条消息
3. **异步 I/O**：使用 asyncio 替代线程，减少资源开销
4. **连接池**：Telegram 和 Feishu 的 HTTP 客户端可以复用

### 6.3 监控与日志

```python
# 添加详细的日志记录
import logging
logger = logging.getLogger(__name__)

def run_agent_turn(...):
    logger.info(f"Processing message from {inbound.channel}/{inbound.peer_id}")
    # ... 处理逻辑
    logger.info(f"Response sent to {inbound.channel}/{inbound.peer_id}")
```

---

## 七、关键认知总结

1. **调度中心模式**：`agent_loop` 作为调度中心，协调不同 I/O 模型的通道
2. **统一处理，分离输入**：相同的 `run_agent_turn` 处理不同来源的消息
3. **线程安全设计**：简单的锁机制确保多线程下的数据一致性
4. **优雅降级**：通道可选启用，核心功能（CLI）始终可用
5. **错误隔离**：单一会话的错误不影响整个系统
6. **扩展友好**：新增通道只需实现 Channel 接口，无需修改核心逻辑

## 九、配置变量详解

### 9.1 核心配置变量

| 环境变量 | 必需 | 默认值 | 说明 |
|----------|------|--------|------|
| `DASHSCOPE_API_KEY` | 是 | 无 | 阿里云 DashScope API 密钥，用于访问 Qwen 模型 |
| `MODEL_ID` | 否 | `qwen-max` | 使用的模型 ID，可指定具体版本 |

### 9.2 Telegram 通道配置

| 环境变量 | 必需 | 默认值 | 说明 |
|----------|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | 否 | 无 | Telegram Bot API Token，格式为 `123456:ABC-DEF...` |
| `TELEGRAM_ALLOWED_CHATS` | 否 | 无 | 允许的聊天 ID 白名单，逗号分隔（如 `12345,67890`） |

**启用条件**：只要设置了 `TELEGRAM_BOT_TOKEN`，Telegram 通道就会自动启用。

### 9.3 飞书通道配置

| 环境变量 | 必需 | 默认值 | 说明 |
|----------|------|--------|------|
| `FEISHU_APP_ID` | 否 | 无 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 否 | 无 | 飞书应用密钥 |
| `FEISHU_ENCRYPT_KEY` | 否 | 无 | 飞书事件加密密钥（用于 Webhook 安全验证） |
| `FEISHU_BOT_OPEN_ID` | 否 | 无 | 飞书机器人 Open ID（用于群聊 @ 提及检测） |
| `FEISHU_IS_LARK` | 否 | false | 是否使用 Lark（国际版飞书），设置为 `1` 或 `true` 启用 |

**启用条件**：必须同时设置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 才会启用飞书通道。

### 9.4 配置加载逻辑

```python
# .env 文件加载
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

# 核心配置
MODEL_ID = os.getenv("MODEL_ID", "qwen-max")
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# 通道启用逻辑
tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if tg_token and HAS_HTTPX:
    # 启用 Telegram 通道

fs_id = os.getenv("FEISHU_APP_ID", "").strip()
fs_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
if fs_id and fs_secret and HAS_HTTPX:
    # 启用飞书通道
```

### 9.5 配置最佳实践

1. **安全配置**：将 `.env` 文件添加到 `.gitignore`，避免泄露敏感信息
2. **最小权限**：只配置需要的通道，减少攻击面
3. **白名单保护**：Telegram 通道建议设置 `TELEGRAM_ALLOWED_CHATS` 限制访问范围
4. **环境隔离**：开发、测试、生产环境使用不同的配置文件

### 9.6 配置示例

```bash
# .env 示例文件
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MODEL_ID=qwen-max

# Telegram 配置（可选）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF123456789GHIJKL
TELEGRAM_ALLOWED_CHATS=123456789,987654321

# 飞书配置（可选）
FEISHU_APP_ID=cli_xxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_ENCRYPT_KEY=xxxxxxxxxxxxxxxx
FEISHU_BOT_OPEN_ID=ou_xxxxxxxxxxxx
```

这个设计展示了如何在保持简单性的同时，支持复杂的多通道场景，是教学级项目向生产级系统演进的重要一步。