# Section 10: Concurrency 设计文档

> "Named lanes serialize the chaos" —— 命名通道将混乱序列化

## 1. 设计思想

### 1.1 核心理念

s10_concurrency.py 的核心设计理念是将**单一互斥锁**升级为**命名通道系统**，实现更精细化的并发控制。这一设计解决了以下问题：

1. **通道隔离**：不同类型的工作流（用户交互、心跳、定时任务）独立排队，互不阻塞
2. **并发控制**：每个通道可配置最大并发数，支持从串行（max=1）到并行（max=N）
3. **优雅降级**：通过 generation 机制支持重启恢复，过期任务不会影响新任务

### 1.2 架构意图

```
┌─────────────────────────────────────────────────────────────────┐
│                        CommandQueue                              │
│                     (中央调度器)                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   main       │  │    cron      │  │     heartbeat        │  │
│  │   max=1      │  │   max=1      │  │       max=1          │  │
│  │   FIFO       │  │   FIFO       │  │       FIFO           │  │
│  │  [用户对话]   │  │  [定时任务]   │  │     [心跳检查]       │  │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬────────────┘  │
│         │                 │                    │                │
│         ▼                 ▼                    ▼                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ Future 结果   │  │ Future 结果   │  │    Future 结果        │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 与 s07 的演进对比

| 维度 | s07: Heartbeat & Cron | s10: Concurrency |
|------|----------------------|------------------|
| 并发控制 | 单个 `threading.Lock` | 命名 `LaneQueue` 系统 |
| 阻塞策略 | 用户阻塞获取，心跳非阻塞尝试 | 每个 lane 独立 FIFO 队列 |
| 并发粒度 | 全局互斥（同一时间仅一个任务） | 每个 lane 可配置并发数 |
| 结果获取 | 直接执行，无 Future | `concurrent.futures.Future` |
| 恢复机制 | 无 | generation 计数器 |

---

## 2. 实现机制

### 2.1 LaneQueue —— 单个命名通道

```python
class LaneQueue:
    """命名 FIFO 队列，最多并行运行 max_concurrency 个任务"""
    
    def __init__(self, name: str, max_concurrency: int = 1):
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self._deque: deque[tuple[Callable, Future, int]] = deque()
        self._condition = threading.Condition()
        self._active_count = 0
        self._generation = 0
```

**关键属性**：

| 属性 | 类型 | 作用 |
|------|------|------|
| `name` | str | 通道名称，用于日志和调试 |
| `max_concurrency` | int | 最大并发任务数 |
| `_deque` | deque | FIFO 任务队列，存储 (callable, future, generation) |
| `_condition` | Condition | 条件变量，用于线程同步 |
| `_active_count` | int | 当前活跃任务数 |
| `_generation` | int | 代计数器，用于重启恢复 |

#### 2.1.1 入队机制

```python
def enqueue(self, fn: Callable, generation: int | None = None) -> Future:
    """将 callable 加入队列，返回 Future"""
    future = Future()
    with self._condition:
        gen = generation if generation is not None else self._generation
        self._deque.append((fn, future, gen))
        self._pump()  # 尝试启动任务
    return future
```

**执行流程**：
1. 创建 Future 对象用于结果传递
2. 获取当前 generation（或使用传入值）
3. 将任务入队
4. 调用 `_pump()` 尝试启动任务

#### 2.1.2 泵送机制 (_pump)

```python
def _pump(self) -> None:
    """从 deque 弹出任务并运行，直到 active >= max_concurrency"""
    while self._active_count < self.max_concurrency and self._deque:
        fn, future, gen = self._deque.popleft()
        self._active_count += 1
        t = threading.Thread(target=self._run_task, args=(fn, future, gen), daemon=True)
        t.start()
```

**核心逻辑**：
- 只要活跃任务数 < 最大并发数，就从队列取出任务启动
- 每个任务在独立线程中执行
- 线程名格式：`lane-{name}`

#### 2.1.3 任务完成回调 (_task_done)

```python
def _task_done(self, gen: int) -> None:
    """递减活跃计数，仅在 generation 匹配时重新泵送"""
    with self._condition:
        self._active_count -= 1
        if gen == self._generation:
            self._pump()  # 只有当代数匹配才继续泵送
        self._condition.notify_all()
```

**generation 机制的作用**：
- 当调用 `reset_all()` 后，所有 lane 的 generation 递增
- 旧任务完成时，检测到 generation 不匹配，不会触发新任务
- 这确保了重启后旧任务不会"污染"新周期

#### 2.1.4 空闲等待

```python
def wait_for_idle(self, timeout: float | None = None) -> bool:
    """阻塞直到队列为空且无活跃任务"""
    deadline = (time.monotonic() + timeout) if timeout else None
    with self._condition:
        while self._active_count > 0 or len(self._deque) > 0:
            remaining = deadline - time.monotonic() if deadline else None
            if remaining and remaining <= 0:
                return False
            self._condition.wait(timeout=remaining)
        return True
```

### 2.2 CommandQueue —— 中央调度器

```python
class CommandQueue:
    """中央调度器，将 callable 路由到命名的 LaneQueue"""
    
    def __init__(self):
        self._lanes: dict[str, LaneQueue] = {}
        self._lock = threading.Lock()
```

**核心方法**：

| 方法 | 功能 |
|------|------|
| `get_or_create_lane(name, max_concurrency)` | 惰性创建 lane |
| `enqueue(lane_name, fn)` | 将任务路由到指定 lane |
| `reset_all()` | 递增所有 generation |
| `wait_for_all(timeout)` | 等待所有 lane 空闲 |
| `stats()` | 获取所有 lane 统计信息 |

#### 2.2.1 路由机制

```python
def enqueue(self, lane_name: str, fn: Callable) -> Future:
    """将 callable 路由到指定 lane"""
    lane = self.get_or_create_lane(lane_name)
    return lane.enqueue(fn)
```

**惰性创建**：
- lane 在首次使用时创建
- 默认 max_concurrency = 1（串行）
- 可通过 `get_or_create_lane(name, max=N)` 预设更高并发

#### 2.2.2 重置机制

```python
def reset_all(self) -> dict[str, int]:
    """递增所有 lane 的 generation，用于重启恢复"""
    result = {}
    with self._lock:
        for name, lane in self._lanes.items():
            with lane._condition:
                lane._generation += 1
                result[name] = lane._generation
    return result
```

**应用场景**：
- 服务重启时调用
- 防止旧周期的任务继续触发新任务
- 确保干净的启动状态

### 2.3 HeartbeatRunner —— 心跳运行器

s10 中的 HeartbeatRunner 与 s07 的关键区别：

```python
# s07: 使用 Lock 的非阻塞获取
acquired = self.lane_lock.acquire(blocking=False)
if not acquired:
    return

# s10: 使用 lane 的活跃计数检测
lane_stats = self.command_queue.get_or_create_lane(LANE_HEARTBEAT).stats()
if lane_stats["active"] > 0:
    return  # lane 已忙，跳过
```

**改进点**：
1. 不再依赖全局 Lock
2. 通过 lane 状态判断是否应该运行
3. 任务通过 Future 返回结果
4. 使用 callback 处理结果

### 2.4 CronService —— 定时任务服务

```python
def _enqueue_job(self, job: dict, now: float) -> None:
    def _do_cron() -> str:
        return run_agent_single_turn(message, sys_prompt)
    
    future = self.command_queue.enqueue(LANE_CRON, _do_cron)
    
    def _on_done(f: Future) -> None:
        result = f.result()
        # 处理结果...
    
    future.add_done_callback(_on_done)
```

**特点**：
- 所有 cron 任务进入 `cron` lane
- 独立于用户对话和心跳
- 支持错误计数和自动禁用

---

## 3. 主要功能

### 3.1 功能列表

| 功能 | 命令 | 说明 |
|------|------|------|
| 查看通道状态 | `/lanes` | 显示所有 lane 的队列深度、活跃数、generation |
| 查看待处理 | `/queue` | 显示各 lane 的排队情况 |
| 手动入队 | `/enqueue <lane> <message>` | 向指定 lane 提交任务 |
| 调整并发 | `/concurrency <lane> <N>` | 动态调整 lane 的最大并发数 |
| 查看代数 | `/generation` | 显示各 lane 的 generation 计数器 |
| 模拟重启 | `/reset` | 递增所有 generation，模拟重启恢复 |
| 心跳状态 | `/heartbeat` | 显示心跳运行状态 |
| 强制心跳 | `/trigger` | 立即触发心跳检查 |
| 定时任务 | `/cron` | 列出所有 cron 任务 |

### 3.2 使用示例

#### 3.2.1 查看通道状态

```
You > /lanes
  main          active=[*]  queued=0  max=1  gen=0
  cron          active=[.]  queued=2  max=1  gen=0
  heartbeat     active=[.]  queued=0  max=1  gen=0
```

#### 3.2.2 手动入队

```
You > /enqueue main 帮我分析一下今天的日程
  Enqueueing into 'main': 帮我分析一下今天的日程...
[main] result: 好的，根据您的日历...
```

#### 3.2.3 调整并发

```
You > /concurrency cron 3
  cron: max_concurrency 1 -> 3
```

#### 3.2.4 模拟重启

```
You > /reset
  Generation incremented on all lanes:
    main: generation -> 1
    cron: generation -> 1
    heartbeat: generation -> 1
  Stale tasks from the old generation will be ignored.
```

### 3.3 标准通道

| 通道名 | 用途 | 默认并发 |
|--------|------|----------|
| `main` | 用户对话交互 | 1（串行，保证顺序） |
| `cron` | 定时任务执行 | 1（串行，避免资源争抢） |
| `heartbeat` | 心跳检查 | 1（串行，避免重复检查） |

---

## 4. 与前面章节的演进关系

### 4.1 依赖链

```
s01 (Agent Loop) ──► s02 (Tools) ──► s03 (Sessions)
                          │
                          ▼
                    s06 (Intelligence)
                          │
                          ▼
                    s07 (Heartbeat & Cron) ──► s10 (Concurrency)
```

### 4.2 从 s07 到 s10 的演进

#### 4.2.1 问题：s07 的局限性

s07 使用单个 `threading.Lock` 实现 lane 互斥：

```python
# s07: 全局锁
lane_lock = threading.Lock()

# 用户对话：阻塞获取
lane_lock.acquire()
# ... 执行 ...

# 心跳：非阻塞尝试
acquired = lane_lock.acquire(blocking=False)
if not acquired:
    return  # 跳过
```

**局限性**：
1. 所有工作流共享同一把锁
2. 心跳和 cron 无法并行执行
3. 无法区分不同类型的工作优先级
4. 无结果传递机制

#### 4.2.2 解决：s10 的命名通道

```python
# s10: 命名通道
cmd_queue = CommandQueue()
cmd_queue.get_or_create_lane("main", max_concurrency=1)
cmd_queue.get_or_create_lane("cron", max_concurrency=1)
cmd_queue.get_or_create_lane("heartbeat", max_concurrency=1)

# 路由到不同通道
future = cmd_queue.enqueue("main", user_turn_fn)
future = cmd_queue.enqueue("heartbeat", heartbeat_fn)
future = cmd_queue.enqueue("cron", cron_fn)
```

**优势**：
1. 各通道独立运行，互不阻塞
2. 每个通道可配置不同并发策略
3. 通过 Future 获取执行结果
4. generation 机制支持优雅重启

### 4.3 关键改进对比

| 方面 | s07 实现 | s10 实现 |
|------|----------|----------|
| 互斥机制 | `threading.Lock` | `LaneQueue` + `Condition` |
| 任务队列 | 无（直接执行） | FIFO deque |
| 结果传递 | 无 | `concurrent.futures.Future` |
| 并发控制 | 全局串行 | 每个 lane 可配置 |
| 恢复支持 | 无 | generation 计数器 |
| 监控能力 | 仅检查锁状态 | 完整的 stats() API |

---

## 5. 代码结构图

```
s10_concurrency.py (900 行)
│
├── 配置与常量 (50-94)
│   ├── load_dotenv, MODEL_ID, client, WORKSPACE_DIR
│   └── ANSI 颜色定义, LANE_* 常量
│
├── LaneQueue 类 (100-204)
│   ├── __init__(name, max_concurrency)
│   ├── enqueue(fn, generation) -> Future
│   ├── _pump()                     # 启动任务
│   ├── _run_task(fn, future, gen)  # 执行任务
│   ├── _task_done(gen)             # 完成回调
│   ├── wait_for_idle(timeout)      # 等待空闲
│   └── stats() -> dict             # 统计信息
│
├── CommandQueue 类 (211-268)
│   ├── __init__()
│   ├── get_or_create_lane(name, max_concurrency) -> LaneQueue
│   ├── enqueue(lane_name, fn) -> Future
│   ├── reset_all() -> dict         # 重置 generation
│   ├── wait_for_all(timeout) -> bool
│   ├── stats() -> dict
│   └── lane_names() -> list
│
├── SoulSystem 类 (275-288)
│   ├── load() -> str
│   └── build_system_prompt(extra) -> str
│
├── MemoryStore 类 (291-325)
│   ├── load_evergreen() -> str
│   ├── write_memory(content) -> str
│   └── search_memory(query) -> str
│
├── run_agent_single_turn() (333-343)
│   └── 单轮 LLM 调用，无工具
│
├── HeartbeatRunner 类 (351-492)
│   ├── __init__(workspace, command_queue, interval, active_hours)
│   ├── should_run() -> (bool, str)
│   ├── heartbeat_tick()            # 通过 CommandQueue 入队
│   ├── _loop()                     # 后台循环
│   ├── start() / stop()
│   ├── drain_output() -> list
│   └── status() -> dict
│
├── CronService 类 (499-603)
│   ├── __init__(cron_file, command_queue)
│   ├── load_jobs()
│   ├── cron_tick()                 # 通过 CommandQueue 入队
│   ├── _enqueue_job(job, now)
│   ├── drain_output() -> list
│   └── list_jobs() -> list
│
├── REPL 命令处理 (611-811)
│   ├── /lanes, /queue, /enqueue, /concurrency
│   ├── /generation, /reset
│   ├── /heartbeat, /trigger, /cron
│   └── 用户对话处理
│
└── agent_loop() + main() (626-900)
    ├── 创建 CommandQueue 和默认 lanes
    ├── 初始化 HeartbeatRunner, CronService
    ├── 启动后台线程
    └── REPL 主循环
```

### 5.1 核心类关系

```
┌─────────────────────────────────────────────────────────────────┐
│                          agent_loop()                            │
│                           (主线程)                                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
    ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
    │ HeartbeatRunner│ │  CronService  │ │   用户对话    │
    │  (心跳线程)    │ │  (定时线程)    │ │  (主线程)     │
    └───────┬───────┘ └───────┬───────┘ └───────┬───────┘
            │                 │                 │
            │    enqueue()    │    enqueue()    │
            └────────┬────────┴────────┬────────┘
                     │                 │
                     ▼                 ▼
            ┌─────────────────────────────────────────┐
            │            CommandQueue                  │
            │              (调度器)                    │
            └────────────────────┬────────────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         │                       │                       │
         ▼                       ▼                       ▼
  ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
  │ LaneQueue   │        │ LaneQueue   │        │ LaneQueue   │
  │   main      │        │    cron     │        │  heartbeat  │
  │   max=1     │        │   max=1     │        │    max=1    │
  └─────────────┘        └─────────────┘        └─────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
    [用户对话]              [定时任务]              [心跳检查]
```

### 5.2 任务执行流程

```
用户输入
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ _make_user_turn(user_msg, messages, system_prompt, handle_tool) │
│                        (创建 callable)                          │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
                    cmd_queue.enqueue("main", callable)
                                │
                                ▼
                    ┌───────────────────────┐
                    │ LaneQueue._deque      │
                    │ (加入 FIFO 队列)       │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ LaneQueue._pump()     │
                    │ (active < max?)       │
                    └───────────┬───────────┘
                                │
                    ┌───────────┴───────────┐
                    │                       │
                    ▼                       ▼
            [启动线程执行]           [等待已有任务完成]
                    │
                    ▼
            ┌───────────────────────┐
            │ _run_task(fn, future) │
            │   1. 执行 fn()        │
            │   2. 设置 future 结果  │
            │   3. _task_done()     │
            └───────────┬───────────┘
                        │
                        ▼
                future.result() 返回给调用者
```

---

## 6. 总结

### 6.1 核心价值

s10_concurrency.py 实现了一个**生产级的并发控制系统**：

1. **通道隔离**：不同工作流独立排队，避免相互阻塞
2. **灵活并发**：每个通道可配置串行或并行
3. **优雅恢复**：generation 机制确保重启后干净状态
4. **可观测性**：完整的 stats API 支持监控和调试

### 6.2 设计亮点

| 设计点 | 实现方式 | 收益 |
|--------|----------|------|
| 命名通道 | `dict[str, LaneQueue]` | 工作流隔离 |
| FIFO 队列 | `deque` + `Condition` | 公平调度 |
| Future 模式 | `concurrent.futures.Future` | 异步结果传递 |
| Generation | 整数计数器 | 重启恢复支持 |
| 惰性创建 | `get_or_create_lane` | 按需分配资源 |

### 6.3 与生产环境的对接

s10 的设计可以直接对接生产环境：

- **扩展性**：可通过 `/concurrency` 动态调整并发
- **可观测性**：`/lanes` 和 `/queue` 提供实时监控
- **容错性**：generation 机制支持优雅重启
- **灵活性**：可添加新的 lane 类型（如 `background`, `priority`）

---

> 本文档基于 s10_concurrency.py (900 行) 和 s07_heartbeat_cron.py (659 行) 分析生成