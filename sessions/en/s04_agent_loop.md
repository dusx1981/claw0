# Section 04: Agent Loop

> Multi-channel coordination -- "one brain, multiple mouths"

## Architecture

```
                 Main Thread (agent_loop)
                 ┌───────────────────────────────────┐
                 │ while True:                      │
                 │   - Consume Telegram queue       │
                 │   - Handle CLI input             │
                 │   - Call run_agent_turn()        │
                 └──────────────────┬────────────────┘
                                    │
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ Telegram Channel  │  │  Feishu Channel   │  │   CLI Channel     │
│ Background thread │  │  Webhook events   │  │ Blocking stdin    │
│ Long-poll API     │  │ External calls    │  │ Direct processing │
└───────────────────┘  └───────────────────┘  └───────────────────┘
```

## Key Concepts

- **agent_loop()**: The main coordinator that handles message reception from multiple channels and dispatches to `run_agent_turn()`.
- **Threading Model**: Main thread coordinates, background threads handle blocking operations (Telegram long-polling).
- **Channel Abstraction**: Each platform implements the same `Channel` interface but with different I/O strategies.
- **Message Queue**: Thread-safe queue for passing messages from background threads to main thread.
- **Session Isolation**: Each `(channel, peer_id)` combination has independent conversation history.

## Key Code Walkthrough

### 1. Main Loop Structure

The `agent_loop()` function is the central coordinator that manages all channels:

```python
def agent_loop() -> None:
    mgr = ChannelManager()
    cli = CLIChannel()
    mgr.register(cli)

    # Conditionally register Telegram channel
    if tg_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        tg_acc = ChannelAccount(channel="telegram", account_id="tg-primary", token=tg_token)
        mgr.accounts.append(tg_acc)
        tg_channel = TelegramChannel(tg_acc)
        mgr.register(tg_channel)
        # Start background polling thread
        tg_thread = threading.Thread(target=telegram_poll_loop, daemon=True, args=(tg_channel, msg_queue, q_lock, stop_event))
        tg_thread.start()

    # Conditionally register Feishu channel  
    if fs_id := os.getenv("FEISHU_APP_ID"):
        if fs_secret := os.getenv("FEISHU_APP_SECRET"):
            fs_acc = ChannelAccount(channel="feishu", account_id="feishu-primary", config={"app_id": fs_id, "app_secret": fs_secret})
            mgr.accounts.append(fs_acc)
            mgr.register(FeishuChannel(fs_acc))

    conversations = {}
    while True:
        # Process Telegram messages from queue
        with q_lock:
            tg_msgs = msg_queue[:]
            msg_queue.clear()
        for m in tg_msgs:
            run_agent_turn(m, conversations, mgr)

        # Handle CLI input (non-blocking when Telegram active)
        if tg_channel:
            import select
            if select.select([sys.stdin], [], [], 0.5)[0]:
                user_input = sys.stdin.readline().strip()
                if user_input.lower() in ("quit", "exit"):
                    break
                # Process CLI message
                msg = InboundMessage(text=user_input, sender_id="cli-user", channel="cli", account_id="cli-local", peer_id="cli-user")
                run_agent_turn(msg, conversations, mgr)
        else:
            # Blocking input when no Telegram
            msg = cli.receive()
            if not msg:
                break
            run_agent_turn(msg, conversations, mgr)
```

### 2. Threading and Message Queue

The system uses a simple but effective producer-consumer pattern:

```python
# Shared data structures
msg_queue: list[InboundMessage] = []
q_lock = threading.Lock()
stop_event = threading.Event()

# Producer: Telegram background thread
def telegram_poll_loop(tg, queue, lock, stop):
    while not stop.is_set():
        msgs = tg.poll()
        if msgs:
            with lock:
                queue.extend(msgs)

# Consumer: Main thread
with q_lock:
    tg_msgs = msg_queue[:]
    msg_queue.clear()
for m in tg_msgs:
    run_agent_turn(m, conversations, mgr)
```

### 3. Non-blocking I/O Design

When Telegram is active, CLI input uses non-blocking I/O to avoid missing messages:

```python
if tg_channel:
    import select
    # Check stdin with 500ms timeout
    if select.select([sys.stdin], [], [], 0.5)[0]:
        user_input = sys.stdin.readline().strip()
        # Process input
else:
    # No Telegram, use blocking input
    msg = cli.receive()
```

### 4. Session Management

Each conversation is isolated by a unique session key:

```python
def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    return f"agent:main:direct:{channel}:{peer_id}"

# In run_agent_turn()
sk = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
messages = conversations.setdefault(sk, [])
messages.append({"role": "user", "content": inbound.text})
```

### 5. Graceful Shutdown

The system handles cleanup properly:

```python
# On exit
stop_event.set()  # Signal background threads to stop
if tg_thread and tg_thread.is_alive():
    tg_thread.join(timeout=3.0)  # Wait up to 3 seconds
mgr.close_all()  # Close all channel connections
```

## Try It

```sh
# CLI only (no additional env vars needed)
python en/s04_agent_loop.py

# With Telegram -- add to .env:
# TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

# With Feishu -- add to .env:
# FEISHU_APP_ID=cli_xxxxx
# FEISHU_APP_SECRET=xxxxx

# REPL commands work as usual:
# You > /channels      (list registered channels)
# You > /accounts      (show bot accounts)
# You > quit           (exit gracefully)
```

## How OpenClaw Does It

| Aspect          | claw0 (this file)                | OpenClaw production                      |
|-----------------|----------------------------------|------------------------------------------|
| Main Loop       | Single `agent_loop()` function   | Same pattern + additional monitoring     |
| Threading       | Manual thread management         | Thread pools + async/await hybrid        |
| Message Queue   | Simple list with lock            | Dedicated queue library + backpressure   |
| Session Storage | In-memory dictionary            | Persistent storage with TTL              |
| Error Handling  | Basic try/catch                  | Comprehensive error recovery + alerts     |