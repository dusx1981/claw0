# 第 04 节: Agent 循环

> 多通道协调 -- "一个大脑, 多个嘴巴"

## 架构

```
                 主线程 (agent_loop)
                 ┌───────────────────────────────────┐
                 │ while True:                      │
                 │   - 消费 Telegram 队列           │
                 │   - 处理 CLI 输入               │
                 │   - 调用 run_agent_turn()        │
                 └──────────────────┬────────────────┘
                                    │
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ Telegram 通道     │  │  飞书通道         │  │   CLI 通道        │
│ 后台线程          │  │  Webhook 事件     │  │ 阻塞式 stdin      │  
│ 长轮询 API        │  │ 外部调用          │  │ 直接处理          │
└───────────────────┘  └───────────────────┘  └───────────────────┘
```

## 本节要点

- **agent_loop()**: 主调度器，处理来自多个通道的消息接收并分发到 `run_agent_turn()`。
- **线程模型**: 主线程负责协调，后台线程处理阻塞操作（Telegram 长轮询）。
- **通道抽象**: 每个平台实现相同的 `Channel` 接口，但使用不同的 I/O 策略。
- **消息队列**: 线程安全的队列，用于在后台线程和主线程之间传递消息。
- **会话隔离**: 每个 `(channel, peer_id)` 组合都有独立的对话历史。

## 核心代码走读

### 1. 主循环结构

`agent_loop()` 函数是中央调度器，管理所有通道：

```python
def agent_loop() -> None:
    mgr = ChannelManager()
    cli = CLIChannel()
    mgr.register(cli)

    # 条件注册 Telegram 通道
    if tg_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        tg_acc = ChannelAccount(channel="telegram", account_id="tg-primary", token=tg_token)
        mgr.accounts.append(tg_acc)
        tg_channel = TelegramChannel(tg_acc)
        mgr.register(tg_channel)
        # 启动后台轮询线程
        tg_thread = threading.Thread(target=telegram_poll_loop, daemon=True, args=(tg_channel, msg_queue, q_lock, stop_event))
        tg_thread.start()

    # 条件注册飞书通道  
    if fs_id := os.getenv("FEISHU_APP_ID"):
        if fs_secret := os.getenv("FEISHU_APP_SECRET"):
            fs_acc = ChannelAccount(channel="feishu", account_id="feishu-primary", config={"app_id": fs_id, "app_secret": fs_secret})
            mgr.accounts.append(fs_acc)
            mgr.register(FeishuChannel(fs_acc))

    conversations = {}
    while True:
        # 处理 Telegram 队列中的消息
        with q_lock:
            tg_msgs = msg_queue[:]
            msg_queue.clear()
        for m in tg_msgs:
            run_agent_turn(m, conversations, mgr)

        # 处理 CLI 输入 (Telegram 活跃时使用非阻塞模式)
        if tg_channel:
            import select
            if select.select([sys.stdin], [], [], 0.5)[0]:
                user_input = sys.stdin.readline().strip()
                if user_input.lower() in ("quit", "exit"):
                    break
                # 处理 CLI 消息
                msg = InboundMessage(text=user_input, sender_id="cli-user", channel="cli", account_id="cli-local", peer_id="cli-user")
                run_agent_turn(msg, conversations, mgr)
        else:
            # 无 Telegram 时，使用阻塞式输入
            msg = cli.receive()
            if not msg:
                break
            run_agent_turn(msg, conversations, mgr)
```

### 2. 线程和消息队列

系统使用简单但有效的生产者-消费者模式：

```python
# 共享数据结构
msg_queue: list[InboundMessage] = []
q_lock = threading.Lock()
stop_event = threading.Event()

# 生产者: Telegram 后台线程
def telegram_poll_loop(tg, queue, lock, stop):
    while not stop.is_set():
        msgs = tg.poll()
        if msgs:
            with lock:
                queue.extend(msgs)

# 消费者: 主线程
with q_lock:
    tg_msgs = msg_queue[:]
    msg_queue.clear()
for m in tg_msgs:
    run_agent_turn(m, conversations, mgr)
```

### 3. 非阻塞 I/O 设计

当 Telegram 活跃时，CLI 输入使用非阻塞 I/O 以避免遗漏消息：

```python
if tg_channel:
    import select
    # 使用 500ms 超时检查 stdin
    if select.select([sys.stdin], [], [], 0.5)[0]:
        user_input = sys.stdin.readline().strip()
        # 处理输入
else:
    # 无 Telegram，使用阻塞式输入
    msg = cli.receive()
```

### 4. 会话管理

每个对话都通过唯一的会话键进行隔离：

```python
def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    return f"agent:main:direct:{channel}:{peer_id}"

# 在 run_agent_turn() 中
sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
messages = conversations.setdefault(sk, [])
messages.append({"role": "user", "content": inbound.text})
```

### 5. 优雅关闭

系统正确处理清理工作：

```python
# 退出时
stop_event.set()  # 通知后台线程停止
if tg_thread and tg_thread.is_alive():
    tg_thread.join(timeout=3.0)  # 最多等待 3 秒
mgr.close_all()  # 关闭所有通道连接
```

## 试一试

```sh
# 仅 CLI (不需要额外的环境变量)
python zh/s04_agent_loop.py

# 启用 Telegram -- 在 .env 中添加:
# TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

# 启用飞书 -- 在 .env 中添加:
# FEISHU_APP_ID=cli_xxxxx
# FEISHU_APP_SECRET=xxxxx

# REPL 命令正常工作:
# You > /channels      (列出已注册的通道)
# You > /accounts      (显示 bot 账号)  
# You > quit           (优雅退出)
```

## OpenClaw 中的对应实现

| 方面            | claw0 (本文件)                   | OpenClaw 生产代码                        |
|-----------------|----------------------------------|------------------------------------------|
| 主循环          | 单一 `agent_loop()` 函数         | 相同模式 + 额外监控                      |
| 线程模型        | 手动线程管理                     | 线程池 + async/await 混合                |
| 消息队列        | 带锁的简单列表                   | 专用队列库 + 背压控制                    |
| 会话存储        | 内存字典                         | 带 TTL 的持久化存储                      |
| 错误处理        | 基本的 try/catch                 | 全面的错误恢复 + 告警                    |