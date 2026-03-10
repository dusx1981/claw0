# S08 Delivery 设计文档分析

> "Write to disk first, then try to send" — s08_delivery.py

---

## 1. 设计思想

### 1.1 核心设计理念

s08_delivery 实现了**可靠消息投递队列**，解决了分布式系统中的经典问题：

```
消息丢失 ← 网络故障 ← 进程崩溃 ← 系统重启
```

核心哲学：**预写日志（Write-Ahead Log）+ 指数退避**

```
         传统方式                         Delivery 队列方式
    ┌─────────────────┐              ┌─────────────────────────┐
    │  Agent 生成消息  │              │   Agent 生成消息         │
    │        ↓        │              │          ↓              │
    │   直接发送 API   │              │   写入磁盘（预写日志）    │
    │        ↓        │              │          ↓              │
    │   发送失败？     │              │   后台线程异步投递        │
    │        ↓        │              │          ↓              │
    │   消息丢失！     │              │   失败？退避重试         │
    └─────────────────┘              │          ↓              │
                                     │   崩溃？重启恢复         │
                                     └─────────────────────────┘
```

### 1.2 架构意图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        消息来源层                                    │
│   Agent Reply / Heartbeat / Cron / Channel Message                  │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     chunk_message()                                  │
│                按平台限制分片（Telegram 4096, Discord 2000...）       │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   DeliveryQueue.enqueue()                            │
│                    写入磁盘（预写日志）                               │
│                                                                      │
│   workspace/delivery-queue/                                          │
│   ├── <uuid>.json      ← 待投递                                      │
│   └── failed/          ← 重试耗尽                                    │
│       └── <uuid>.json                                                │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   DeliveryRunner（后台线程）                         │
│                                                                      │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐            │
│   │  加载待处理  │ -> │  尝试投递    │ -> │  成功/失败   │            │
│   └─────────────┘    └─────────────┘    └─────────────┘            │
│                              │                                       │
│                     ┌────────┴────────┐                             │
│                     ▼                 ▼                             │
│                 success            failure                           │
│                    │                 │                               │
│                    ▼                 ▼                               │
│               ack() 删除       fail() + backoff                      │
│                                    │                                 │
│                           ┌────────┴────────┐                       │
│                           ▼                 ▼                       │
│                      重试等待          移入 failed/                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 三大设计原则

| 原则 | 实现方式 | 解决的问题 |
|------|----------|------------|
| **持久化优先** | 发送前先写磁盘 | 进程崩溃不丢消息 |
| **异步解耦** | 后台线程投递 | 不阻塞主流程 |
| **优雅降级** | 指数退避 + 失败队列 | 网络故障自动恢复 |

---

## 2. 实现机制

### 2.1 QueuedDelivery — 队列条目数据结构

```python
@dataclass
class QueuedDelivery:
    id: str                    # 唯一标识（UUID 前 12 位）
    channel: str               # 投递渠道：telegram, discord, whatsapp...
    to: str                    # 目标地址：用户 ID、群组 ID...
    text: str                  # 消息内容
    retry_count: int = 0       # 重试次数
    last_error: str | None     # 最后一次错误信息
    enqueued_at: float         # 入队时间戳
    next_retry_at: float       # 下次重试时间
```

**设计要点**：

1. **精简字段**：只保留投递必需的信息
2. **序列化友好**：`to_dict()` / `from_dict()` 支持磁盘存储
3. **重试控制**：`retry_count` + `next_retry_at` 实现退避逻辑

### 2.2 DeliveryQueue — 磁盘持久化队列

#### 核心方法

```python
class DeliveryQueue:
    def __init__(self, queue_dir: Path | None = None):
        self.queue_dir = queue_dir or QUEUE_DIR       # workspace/delivery-queue/
        self.failed_dir = self.queue_dir / "failed"   # 失败队列
        self._lock = threading.Lock()                  # 线程安全
```

#### 原子写入机制

```python
def _write_entry(self, entry: QueuedDelivery) -> None:
    """通过 tmp + os.replace() 实现原子写入"""
    final_path = self.queue_dir / f"{entry.id}.json"
    tmp_path = self.queue_dir / f".tmp.{os.getpid()}.{entry.id}.json"
    
    # 1. 写入临时文件
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())  # 强制刷盘
    
    # 2. 原子重命名（POSIX 保证原子性）
    os.replace(str(tmp_path), str(final_path))
```

**为什么原子写入很重要？**

```
场景：进程在写入过程中崩溃

非原子写入：
  写入一半 → 崩溃 → 文件损坏 → 无法恢复

原子写入：
  写入临时文件 → 崩溃 → 临时文件不完整 → 正式文件完好
  原子重命名 → 要么成功，要么不变
```

#### 入队流程

```python
def enqueue(self, channel: str, to: str, text: str) -> str:
    """创建队列条目并原子写入磁盘"""
    delivery_id = uuid.uuid4().hex[:12]
    entry = QueuedDelivery(
        id=delivery_id,
        channel=channel,
        to=to,
        text=text,
        enqueued_at=time.time(),
        next_retry_at=0.0,  # 立即可投递
    )
    self._write_entry(entry)
    return delivery_id
```

#### 确认与失败处理

```python
def ack(self, delivery_id: str) -> None:
    """投递成功 — 删除队列文件"""
    file_path = self.queue_dir / f"{delivery_id}.json"
    file_path.unlink()  # 删除

def fail(self, delivery_id: str, error: str) -> None:
    """投递失败 — 递增重试计数，计算下次重试时间"""
    entry = self._read_entry(delivery_id)
    entry.retry_count += 1
    entry.last_error = error
    
    if entry.retry_count >= MAX_RETRIES:  # 5 次
        self.move_to_failed(delivery_id)   # 移入 failed/
    else:
        backoff_ms = compute_backoff_ms(entry.retry_count)
        entry.next_retry_at = time.time() + backoff_ms / 1000.0
        self._write_entry(entry)
```

### 2.3 指数退避策略

```python
BACKOFF_MS = [5_000, 25_000, 120_000, 600_000]  # [5s, 25s, 2min, 10min]
MAX_RETRIES = 5

def compute_backoff_ms(retry_count: int) -> int:
    """指数退避，加 +/- 20% 抖动以避免惊群效应"""
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[idx]
    jitter = random.randint(-base // 5, base // 5)  # ±20%
    return max(0, base + jitter)
```

**退避时间表**：

| 重试次数 | 基础等待 | 抖动范围 | 实际范围 |
|---------|---------|---------|---------|
| 1 | 5s | ±1s | 4-6s |
| 2 | 25s | ±5s | 20-30s |
| 3 | 2min | ±24s | 1m36s-2m24s |
| 4 | 10min | ±2min | 8-12min |
| 5 | 10min | ±2min | 移入 failed/ |

**为什么需要抖动（Jitter）？**

```
场景：大量消息同时失败

无抖动：
  消息 A、B、C... 同时失败 → 同时重试 → 再次同时失败 → 惊群效应

有抖动：
  消息 A 4.2s 后重试
  消息 B 5.8s 后重试
  消息 C 4.9s 后重试
  → 错开重试时间，避免服务器压力峰值
```

### 2.4 消息分片机制

```python
CHANNEL_LIMITS: dict[str, int] = {
    "telegram": 4096,
    "telegram_caption": 1024,
    "discord": 2000,
    "whatsapp": 4096,
    "default": 4096,
}

def chunk_message(text: str, channel: str = "default") -> list[str]:
    """将消息按平台限制分片。两级拆分：段落，然后硬切"""
    if not text:
        return []
    
    limit = CHANNEL_LIMITS.get(channel, CHANNEL_LIMITS["default"])
    
    # 短消息直接返回
    if len(text) <= limit:
        return [text]
    
    chunks: list[str] = []
    
    # 第一级：按段落分割
    for para in text.split("\n\n"):
        # 尝试合并到上一个 chunk
        if chunks and len(chunks[-1]) + len(para) + 2 <= limit:
            chunks[-1] += "\n\n" + para
        else:
            # 第二级：硬切超长段落
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            if para:
                chunks.append(para)
    
    return chunks or [text[:limit]]
```

**分片策略**：

```
输入文本（6000 字符，Telegram 渠道）

第一级：段落分割
  段落 1 (2000 字符) → Chunk 1
  段落 2 (3500 字符) → 超过 4096，需要第二级
  段落 3 (500 字符)  → 合并到 Chunk 2

第二级：硬切
  段落 2 (3500 字符) → Chunk 2 (3500 字符)

最终结果：3 个 chunk
```

### 2.5 DeliveryRunner — 后台投递线程

```python
class DeliveryRunner:
    def __init__(
        self,
        queue: DeliveryQueue,
        deliver_fn: Callable[[str, str, str], None],
    ):
        self.queue = queue
        self.deliver_fn = deliver_fn          # 实际发送函数
        self._stop_event = threading.Event()   # 停止信号
        self._thread: threading.Thread | None = None
        self.total_attempted = 0
        self.total_succeeded = 0
        self.total_failed = 0
```

#### 启动与恢复

```python
def start(self) -> None:
    """运行恢复扫描，然后启动后台投递线程"""
    self._recovery_scan()  # 检查上次崩溃遗留的消息
    self._thread = threading.Thread(
        target=self._background_loop,
        daemon=True,
        name="delivery-runner",
    )
    self._thread.start()

def _recovery_scan(self) -> None:
    """启动时统计待处理和失败条目"""
    pending = self.queue.load_pending()
    failed = self.queue.load_failed()
    # 输出恢复信息：5 pending, 2 failed
```

#### 后台循环

```python
def _background_loop(self) -> None:
    while not self._stop_event.is_set():
        try:
            self._process_pending()
        except Exception as exc:
            print_error(f"Delivery loop error: {exc}")
        self._stop_event.wait(timeout=1.0)  # 1 秒轮询

def _process_pending(self) -> None:
    """处理所有 next_retry_at <= now 的待处理条目"""
    pending = self.queue.load_pending()
    now = time.time()
    
    for entry in pending:
        if self._stop_event.is_set():
            break
        if entry.next_retry_at > now:
            continue  # 还在退避期
        
        self.total_attempted += 1
        try:
            self.deliver_fn(entry.channel, entry.to, entry.text)
            self.queue.ack(entry.id)
            self.total_succeeded += 1
        except Exception as exc:
            self.queue.fail(entry.id, str(exc))
            self.total_failed += 1
```

### 2.6 HeartbeatRunner — 心跳定时器

s08 中的 HeartbeatRunner 与 s07 不同：**输出通过 DeliveryQueue 投递**

```python
class HeartbeatRunner:
    def __init__(
        self,
        queue: DeliveryQueue,   # 注入队列
        channel: str,
        to: str,
        interval: float = 60.0,
    ):
        self.queue = queue
        # ...

    def trigger(self) -> None:
        """生成 heartbeat 文本并入队投递"""
        heartbeat_text = (
            f"[Heartbeat #{self.run_count}] "
            f"System check at {time.strftime('%H:%M:%S')} -- all OK."
        )
        chunks = chunk_message(heartbeat_text, self.channel)
        for chunk in chunks:
            self.queue.enqueue(self.channel, self.to, chunk)
```

**与 s07 的区别**：

| 特性 | s07 Heartbeat | s08 Heartbeat |
|------|---------------|---------------|
| 输出方式 | 直接 print | 通过队列投递 |
| 可靠性 | 进程崩溃丢失 | 崩溃恢复保留 |
| 分片支持 | 无 | 有 |
| 重试机制 | 无 | 继承队列退避 |

### 2.7 MockDeliveryChannel — 模拟投递渠道

用于测试和演示：

```python
class MockDeliveryChannel:
    def __init__(self, name: str, fail_rate: float = 0.0):
        self.name = name
        self.fail_rate = fail_rate  # 模拟失败率
        self.sent: list[dict] = []   # 发送历史

    def send(self, to: str, text: str) -> None:
        if random.random() < self.fail_rate:
            raise ConnectionError(f"[{self.name}] Simulated failure")
        self.sent.append({"to": to, "text": text, "time": time.time()})
```

**演示用法**：

```
/simulate-failure  → 切换 50% 失败率
观察重试行为
```

---

## 3. 主要功能

### 3.1 功能列表

| 功能 | 命令 | 描述 |
|------|------|------|
| 查看待处理 | `/queue` | 显示队列中等待投递的消息 |
| 查看失败 | `/failed` | 显示重试耗尽的消息 |
| 重试失败 | `/retry` | 将失败消息重新入队 |
| 模拟故障 | `/simulate-failure` | 切换 50% 投递失败率 |
| 心跳状态 | `/heartbeat` | 显示心跳运行状态 |
| 触发心跳 | `/trigger` | 手动触发一次心跳 |
| 投递统计 | `/stats` | 显示投递统计信息 |

### 3.2 REPL 命令详解

#### `/queue` — 查看待处理队列

```
/queue
  Pending deliveries (3):
    a1b2c3d4... retry=0 "Hello, this is a test message..."
    e5f6g7h8... retry=2, wait 15s "Another message..."
    i9j0k1l2... retry=1 "Third message..."
```

显示：
- 消息 ID 前 8 位
- 重试次数
- 等待时间（如果在退避期）
- 消息预览（前 40 字符）

#### `/failed` — 查看失败队列

```
/failed
  Failed deliveries (1):
    a1b2c3d4... retries=5 error="Connection timeout" "Important message..."
```

显示：
- 消息 ID
- 总重试次数
- 最后错误信息
- 消息预览

#### `/retry` — 重试失败消息

```
/retry
  Moved 1 entries back to queue.
```

将 `failed/` 目录下的消息移回队列，重置重试计数。

#### `/simulate-failure` — 切换模拟故障

```
/simulate-failure
  console fail rate -> 50% (unreliable)

/simulate-failure
  console fail rate -> 0% (reliable)
```

用于测试重试和退避行为。

#### `/stats` — 投递统计

```
/stats
  Delivery stats: pending=2, failed=1, attempted=15, succeeded=12, errors=3
```

### 3.3 Agent 工具

```python
TOOLS = [
    {
        "name": "memory_write",
        "description": "Save an important fact or preference to long-term memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to remember"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_search",
        "description": "Search long-term memory for relevant facts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
]
```

### 3.4 完整使用流程

```bash
# 1. 启动 Agent
python sessions/zh/s08_delivery.py

# 2. 发送消息（自动入队）
You > Hello!

# 3. 查看队列状态
/queue
/heartbeat
/stats

# 4. 测试重试机制
/simulate-failure   # 开启 50% 失败率
# 发送几条消息，观察重试行为
/queue              # 查看待处理消息

# 5. 恢复正常
/simulate-failure   # 关闭失败率

# 6. 退出
quit
```

---

## 4. 与前面章节的演进关系

### 4.1 章节依赖图

```
s01: Agent Loop
 │   └── while True + stop_reason
 │
 ▼
s02: Tool Use
 │   └── 调度表 + 工具处理
 │
 ▼
s03: Sessions
 │   └── JSONL 持久化
 │
 ▼
s04: Channels
 │   └── 消息管道
 │
 ▼
s05: Gateway
 │   └── 路由绑定
 │
 ▼
s06: Intelligence
 │   └── 灵魂 + 记忆 + 技能
 │
 ▼
s07: Heartbeat & Cron
 │   └── 定时任务 + Lane 互斥
 │
 ▼
s08: Delivery ◀── 本节
     └── 可靠投递队列 + 退避重试
```

### 4.2 从 s07 到 s08 的演进

| 特性 | s07: Heartbeat | s08: Delivery |
|------|----------------|---------------|
| 心跳输出 | 直接 print | 通过队列投递 |
| 消息可靠性 | 进程崩溃丢失 | 崩溃恢复保留 |
| 网络故障 | 无处理 | 指数退避重试 |
| 消息分片 | 无 | 按平台限制分片 |
| 失败处理 | 无 | failed/ 目录 + 手动重试 |

### 4.3 s08 的关键创新

#### 1. 预写日志（Write-Ahead Log）

```
传统模式：
  生成消息 → 发送 → 失败 → 丢失

s08 模式：
  生成消息 → 写入磁盘 → 发送 → 失败 → 从磁盘恢复 → 重试
```

#### 2. 异步投递

```
同步模式：
  用户输入 → LLM 生成 → 发送（阻塞）→ 返回

s08 异步模式：
  用户输入 → LLM 生成 → 入队（立即返回）
                        ↓
                  后台线程投递
```

#### 3. 渠道感知分片

```
s07:
  长消息 → 直接发送 → 可能被截断或拒绝

s08:
  长消息 → chunk_message() → 多个短消息 → 分别发送
```

#### 4. 失败队列管理

```
s07:
  发送失败 → 日志记录 → 结束

s08:
  发送失败 → 退避重试 → 重试耗尽 → 移入 failed/
                                      ↓
                                可手动 /retry 重试
```

### 4.4 s08 为后续章节铺垫

| 后续章节 | 依赖 s08 的功能 |
|----------|-----------------|
| s09: Resilience | 投递队列与重试洋葱的协调 |
| s10: Concurrency | 多 Lane 共享投递队列 |

---

## 5. 代码结构图

```
s08_delivery.py (869 行)
│
├── 导入与配置 (36-91)
│   ├── 标准库: json, os, random, sys, threading, time, uuid
│   ├── 数据类: dataclass, field
│   ├── 类型: Callable, Any
│   ├── 第三方: dotenv, openai
│   ├── 配置常量: MODEL_ID, WORKSPACE_DIR, QUEUE_DIR
│   ├── 退避配置: BACKOFF_MS, MAX_RETRIES
│   └── ANSI 颜色定义
│
├── QueuedDelivery 数据结构 (119-167)
│   └── @dataclass QueuedDelivery
│       ├── 字段: id, channel, to, text, retry_count, last_error...
│       ├── to_dict() -> dict
│       ├── from_dict(data) -> QueuedDelivery
│       └── compute_backoff_ms(retry_count) -> int
│
├── DeliveryQueue 磁盘队列 (170-304)
│   └── class DeliveryQueue
│       ├── __init__(queue_dir)
│       ├── 入队操作
│       │   ├── enqueue(channel, to, text) -> delivery_id
│       │   └── _write_entry(entry)  # 原子写入
│       ├── 读取操作
│       │   └── _read_entry(delivery_id) -> QueuedDelivery | None
│       ├── 确认/失败
│       │   ├── ack(delivery_id)  # 删除
│       │   ├── fail(delivery_id, error)  # 退避重试
│       │   └── move_to_failed(delivery_id)
│       └── 加载操作
│           ├── load_pending() -> list[QueuedDelivery]
│           ├── load_failed() -> list[QueuedDelivery]
│           └── retry_failed() -> int
│
├── 消息分片 (306-337)
│   ├── CHANNEL_LIMITS: dict[str, int]
│   └── chunk_message(text, channel) -> list[str]
│
├── DeliveryRunner 后台线程 (339-436)
│   └── class DeliveryRunner
│       ├── 生命周期
│       │   ├── start()  # 恢复扫描 + 启动线程
│       │   └── stop()   # 停止信号 + 等待结束
│       ├── 内部方法
│       │   ├── _recovery_scan()  # 启动时检查遗留消息
│       │   ├── _background_loop()  # 主循环
│       │   └── _process_pending()  # 处理待投递
│       └── get_stats() -> dict
│
├── MockDeliveryChannel (438-460)
│   └── class MockDeliveryChannel
│       ├── __init__(name, fail_rate)
│       ├── send(to, text)  # 模拟发送
│       └── set_fail_rate(rate)
│
├── Soul + Memory (462-544)
│   ├── class SoulSystem
│   │   └── get_system_prompt() -> str
│   ├── class MemoryStore
│   │   ├── write(content) -> str
│   │   └── search(query) -> str
│   └── TOOLS: list[dict]  # 工具 schema
│
├── HeartbeatRunner (546-617)
│   └── class HeartbeatRunner
│       ├── 生命周期
│       │   ├── start()
│       │   └── stop()
│       ├── 内部方法
│       │   ├── _loop()  # 定时循环
│       │   └── trigger()  # 触发心跳
│       └── get_status() -> dict
│
├── Agent 循环 + REPL (619-853)
│   ├── process_tool_call(tool_name, input, memory) -> str
│   ├── handle_repl_command(cmd, ...) -> bool
│   │   ├── /queue
│   │   ├── /failed
│   │   ├── /retry
│   │   ├── /simulate-failure
│   │   ├── /heartbeat
│   │   ├── /trigger
│   │   └── /stats
│   └── agent_loop() -> None
│       ├── 初始化: Soul, Memory, Queue, Runner, Heartbeat
│       ├── 主循环: 用户输入 → LLM 调用 → 入队投递
│       └── 清理: stop heartbeat, stop runner
│
└── 入口 (855-869)
    └── main() -> None
```

---

## 6. 文件系统结构

```
workspace/
└── delivery-queue/              # 投递队列目录
    ├── <uuid>.json              # 待投递消息
    │   ├── a1b2c3d4e5f6.json
    │   └── ...
    └── failed/                  # 重试耗尽的消息
        ├── <uuid>.json
        └── ...

消息文件格式 (a1b2c3d4e5f6.json):
{
  "id": "a1b2c3d4e5f6",
  "channel": "telegram",
  "to": "user_123",
  "text": "Hello, this is a test message...",
  "retry_count": 2,
  "last_error": "Connection timeout",
  "enqueued_at": 1709500000.123,
  "next_retry_at": 1709500100.456
}
```

---

## 7. 关键设计决策总结

### 7.1 为什么使用文件系统而不是数据库？

| 方面 | 文件系统 | 数据库 |
|------|---------|--------|
| 依赖 | 无外部依赖 | 需要 SQLite/Redis/... |
| 部署 | 零配置 | 需要安装配置 |
| 可观测性 | 直接 cat/ls | 需要查询工具 |
| 性能 | 适合中小规模 | 适合大规模 |
| 教学价值 | 原理清晰 | 抽象层多 |

### 7.2 为什么选择这些退避时间？

```
[5s, 25s, 2min, 10min]

设计考量：
1. 5s：快速重试，处理短暂网络抖动
2. 25s：等待网络恢复
3. 2min：等待服务恢复
4. 10min：长时间故障，避免过度重试
```

### 7.3 为什么使用独立线程而非 asyncio？

| 方面 | 线程 | asyncio |
|------|------|---------|
| 兼容性 | 同步代码友好 | 需要全链路异步 |
| 复杂度 | 简单 | 需要 async/await |
| 阻塞 IO | 自动处理 | 需要显式 |
| 教学价值 | 直观 | 需要额外概念 |

### 7.4 为什么心跳通过队列投递？

```
直接投递：
  心跳生成 → 发送 → 失败 → 丢失

队列投递：
  心跳生成 → 入队 → 后台发送 → 失败 → 退避重试 → 恢复
```

心跳消息也值得可靠投递：用户可能依赖心跳判断系统健康状态。

---

## 8. 运行方式

```bash
# 进入项目根目录
cd claw0

# 运行 s08
python sessions/zh/s08_delivery.py

# REPL 命令
/queue              # 查看待处理消息
/failed             # 查看失败消息
/retry              # 重试失败消息
/simulate-failure   # 切换 50% 失败率
/heartbeat          # 心跳状态
/trigger            # 手动触发心跳
/stats              # 投递统计
quit/exit           # 退出
```

---

## 9. 与生产系统的对比

| 特性 | s08 教学 | 生产系统 |
|------|---------|----------|
| 存储 | 文件系统 | Redis/RabbitMQ/Kafka |
| 并发 | 单线程轮询 | 多 Worker 并行 |
| 分片 | 按字符数 | 按消息结构（JSON/Markdown） |
| 监控 | print 输出 | Prometheus/Grafana |
| 告警 | 无 | 失败率超阈值告警 |
| 死信 | failed/ 目录 | 死信队列 + 人工处理 |

---

*文档生成时间: 2025-03-10*