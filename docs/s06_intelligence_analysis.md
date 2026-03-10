# S06 Intelligence 设计文档分析

> "赋予灵魂, 教会记忆" — s06_intelligence.py

---

## 1. 设计思想

### 1.1 核心设计理念

s06_intelligence 是整个 claw0 项目的**核心集成点**，演示了智能 Agent 的"大脑"是如何组装的。

设计哲学：

```
硬编码提示词 → 文件化配置 → 动态组装
```

在 s01-s02 中，系统提示词是硬编码的字符串：

```python
# s01/s02 风格
SYSTEM_PROMPT = "You are a helpful AI assistant."
```

而在真实的生产级 Agent 框架中，系统提示词由**多个层级动态组装**而成。每个层级都有其特定职责：

| 层级 | 文件 | 作用 | 位置 |
|------|------|------|------|
| 1. Identity | IDENTITY.md | 身份定义 | 最前 |
| 2. Soul | SOUL.md | 人格注入 | 靠前 |
| 3. Tools | TOOLS.md | 工具指南 | 中前 |
| 4. Skills | skills/*/SKILL.md | 技能能力 | 中间 |
| 5. Memory | MEMORY.md + daily/*.jsonl | 长期记忆 | 中间 |
| 6. Bootstrap | BOOTSTRAP.md 等 | 启动上下文 | 中后 |
| 7. Runtime | 动态生成 | 运行时信息 | 靠后 |
| 8. Channel | 根据渠道 | 输出适配 | 最后 |

### 1.2 架构意图

```
┌─────────────────────────────────────────────────────────────┐
│                    Bootstrap 文件层                          │
│  [SOUL.md] [IDENTITY.md] [TOOLS.md] [MEMORY.md] ...         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  BootstrapLoader                             │
│              (load, truncate, cap)                          │
│                                                              │
│  • 按文件名加载                                               │
│  • 截断超长内容 (MAX_FILE_CHARS = 20000)                     │
│  • 总量上限控制 (MAX_TOTAL_CHARS = 150000)                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                build_system_prompt()                        │
│                    (8 层组装)                                │
│                                                              │
│  每轮对话重建 → 记忆可能在上一轮被更新                        │
└──────────────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ SkillsManager │  │  MemoryStore  │  │   Agent Loop  │
│ (discover,    │  │ (write,       │  │ (per-turn     │
│  parse)       │  │  search)      │  │  rebuild)     │
└───────────────┘  └───────────────┘  └───────────────┘
```

### 1.3 三种加载模式

系统提示词支持三种加载模式，适应不同场景：

| 模式 | 加载内容 | 适用场景 |
|------|----------|----------|
| `full` | 全部8个Bootstrap文件 + 技能 + 记忆 | 主 Agent |
| `minimal` | 仅 AGENTS.md + TOOLS.md | 子 Agent / Cron 任务 |
| `none` | 空提示词 | 最小化测试 |

---

## 2. 实现机制

### 2.1 Bootstrap 文件加载器

```python
class BootstrapLoader:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
    
    def load_file(self, name: str) -> str:
        """加载单个文件，失败返回空字符串"""
        
    def truncate_file(self, content: str, max_chars: int = 20000) -> str:
        """智能截断：在行边界处截断，添加提示信息"""
        
    def load_all(self, mode: str = "full") -> dict[str, str]:
        """按模式加载所有文件，控制总量上限"""
```

**关键实现细节**：

1. **文件截断策略**：保留头部，在行边界处截断
   ```python
   cut = content.rfind("\n", 0, max_chars)  # 找最近的换行
   return content[:cut] + f"\n\n[... truncated ...]"
   ```

2. **总量控制**：150000 字符上限，防止上下文爆炸

3. **错误容忍**：文件不存在或读取失败返回空字符串，不中断流程

### 2.2 技能发现与注入系统

技能系统采用**目录扫描 + Frontmatter 解析**模式：

```
skills/
├── example-skill/
│   └── SKILL.md          # 包含 YAML frontmatter
└── another-skill/
    └── SKILL.md

SKILL.md 格式:
---
name: "skill-name"
description: "技能描述"
invocation: "/invoke-pattern"
---
技能的具体实现指南...
```

**扫描优先级**（后者覆盖前者）：

```python
scan_order = [
    workspace/skills,          # 内置技能
    workspace/.skills,         # 托管技能  
    workspace/.agents/skills,  # 个人 agent 技能
    cwd/.agents/skills,        # 项目 agent 技能
    cwd/skills,                # 工作区技能
]
```

**Frontmatter 解析**（不依赖 pyyaml）：

```python
def _parse_frontmatter(self, text: str) -> dict[str, str]:
    """轻量级 YAML frontmatter 解析"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    # 简单 key: value 解析
    for line in parts[1].strip().splitlines():
        key, _, value = line.strip().partition(":")
        meta[key.strip()] = value.strip()
```

### 2.3 记忆系统 — 两层存储架构

```
记忆存储架构:

workspace/
├── MEMORY.md              # 长期事实（手动维护）
└── memory/
    └── daily/
        ├── 2025-03-10.jsonl   # 每日日志
        ├── 2025-03-09.jsonl
        └── ...

MEMORY.md 内容示例:
# User Preferences
- Prefers dark mode
- Works on Python projects
- Uses Vim keybindings
```

**两层存储的设计考量**：

| 存储层 | 文件 | 维护方式 | 用途 |
|--------|------|----------|------|
| 长期记忆 | MEMORY.md | 手动编辑 | 不变的事实、偏好 |
| 短期记忆 | daily/*.jsonl | Agent 自动写入 | 交互中学习的动态信息 |

**记忆写入**：

```python
def write_memory(self, content: str, category: str = "general") -> str:
    """追加写入当日 JSONL 文件"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "content": content,
    }
    # 追加写入，保证持久性
```

### 2.4 混合记忆搜索 — TF-IDF + 向量 + MMR

s06 实现了完整的**混合搜索流水线**：

```
用户查询
    │
    ▼
┌─────────────────┐     ┌─────────────────┐
│  关键词搜索      │     │   向量搜索       │
│  (TF-IDF)       │     │  (哈希模拟)      │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
            ┌─────────────────┐
            │   分数融合       │
            │  (加权合并)      │
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │   时间衰减       │
            │  (越旧越低)      │
            └────────┬────────┘
                     ▼
            ┌─────────────────┐
            │   MMR 重排序     │
            │  (多样性优化)    │
            └────────┬────────┘
                     ▼
               Top-K 结果
```

**1. TF-IDF 关键词搜索**：

```python
def _keyword_search(self, query: str, chunks: list) -> list:
    """
    TF-IDF + 余弦相似度
    - 文档频率(DF): 词在多少文档出现
    - TF-IDF: 词频 × log(总文档数 / 文档频率)
    - 余弦相似度: 向量夹角
    """
```

**2. 哈希向量搜索**（无外部 API）：

```python
def _hash_vector(self, text: str, dim: int = 64) -> list[float]:
    """
    使用哈希随机投影模拟向量嵌入
    - 无需外部 embedding API
    - 教学目的：展示向量搜索模式
    """
    for token in tokens:
        h = hash(token)
        for i in range(dim):
            bit = (h >> (i % 62)) & 1
            vec[i] += 1.0 if bit else -1.0
```

**3. 分数融合**：

```python
def _merge_hybrid_results(
    vector_results, keyword_results,
    vector_weight=0.7,  # 向量搜索权重
    text_weight=0.3,    # 关键词搜索权重
):
    """加权合并两路搜索结果"""
```

**4. 时间衰减**：

```python
def _temporal_decay(results, decay_rate=0.01):
    """
    指数衰减：score *= exp(-decay_rate × age_days)
    越旧的记忆分数越低
    """
```

**5. MMR 多样性重排序**：

```python
def _mmr_rerank(results, lambda_param=0.7):
    """
    Maximal Marginal Relevance
    平衡相关性与多样性：
    MMR = λ × relevance - (1-λ) × max_similarity_to_selected
    """
```

### 2.5 系统提示词组装 — 8 层结构

```python
def build_system_prompt(
    mode: str = "full",
    bootstrap: dict[str, str] | None = None,
    skills_block: str = "",
    memory_context: str = "",
    agent_id: str = "main",
    channel: str = "terminal",
) -> str:
```

**分层构建过程**：

```python
sections = []

# 第 1 层: 身份
identity = bootstrap.get("IDENTITY.md", "")
sections.append(identity or "You are a helpful AI assistant.")

# 第 2 层: 灵魂 (仅 full 模式)
if mode == "full":
    soul = bootstrap.get("SOUL.md", "")
    sections.append(f"## Personality\n\n{soul}")

# 第 3 层: 工具指南
tools_md = bootstrap.get("TOOLS.md", "")
sections.append(f"## Tool Usage Guidelines\n\n{tools_md}")

# 第 4 层: 技能 (仅 full 模式)
if mode == "full" and skills_block:
    sections.append(skills_block)

# 第 5 层: 记忆 (仅 full 模式)
if mode == "full":
    # 长期记忆 + 本轮搜索结果
    sections.append("## Memory\n\n" + ...)

# 第 6 层: Bootstrap 上下文
for name in ["HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "USER.md"]:
    sections.append(f"## {name}\n\n{content}")

# 第 7 层: 运行时上下文
sections.append(f"## Runtime Context\n\n- Agent ID: {agent_id}...")

# 第 8 层: 渠道提示
sections.append(f"## Channel\n\n{hints.get(channel, ...)}")

return "\n\n".join(sections)
```

**关键设计决策**：

1. **每轮重建**：记忆可能在上一轮被更新，必须重建
2. **层级顺序**：越靠前的层级对模型行为影响越大
3. **模式切换**：主 agent 用 full，子 agent 用 minimal

### 2.6 Agent 循环集成

```python
def agent_loop() -> None:
    # 启动阶段 (只执行一次)
    loader = BootstrapLoader(WORKSPACE_DIR)
    bootstrap_data = loader.load_all(mode="full")
    skills_mgr = SkillsManager(WORKSPACE_DIR)
    skills_mgr.discover()  # 技能发现是启动时一次性操作
    
    while True:
        user_input = input()
        
        # 自动记忆搜索
        memory_context = _auto_recall(user_input)
        
        # 每轮重建系统提示词
        system_prompt = build_system_prompt(
            mode="full",
            bootstrap=bootstrap_data,
            skills_block=skills_block,
            memory_context=memory_context,
        )
        
        # 内循环: 处理工具调用
        while True:
            response = client.chat.completions.create(...)
            if response.finish_reason == "tool_calls":
                # 处理工具调用
                tool_results = process_tool_call(...)
                messages.append({"role": "user", "content": tool_results})
                continue
            break
```

---

## 3. 主要功能

### 3.1 功能列表

| 功能 | 命令/接口 | 描述 |
|------|-----------|------|
| 查看灵魂 | `/soul` | 显示 SOUL.md 内容 |
| 查看技能 | `/skills` | 列出所有已发现技能 |
| 记忆统计 | `/memory` | 显示记忆存储统计 |
| 搜索记忆 | `/search <query>` | 混合搜索记忆 |
| 查看提示词 | `/prompt` | 显示完整系统提示词 |
| 查看 Bootstrap | `/bootstrap` | 显示已加载的 Bootstrap 文件 |

### 3.2 记忆工具

Agent 可使用的工具：

```python
TOOLS = [
    {
        "name": "memory_write",
        "description": "Save important fact to long-term memory",
        "input_schema": {
            "properties": {
                "content": {"type": "string"},
                "category": {"type": "string"},
            }
        }
    },
    {
        "name": "memory_search",
        "description": "Search stored memories",
        "input_schema": {
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            }
        }
    }
]
```

### 3.3 自动记忆召回

每轮对话前自动搜索相关记忆：

```python
def _auto_recall(user_message: str) -> str:
    """根据用户消息自动搜索相关记忆"""
    results = memory_store.hybrid_search(user_message, top_k=3)
    return "\n".join(f"- [{r['path']}] {r['snippet']}" for r in results)
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
 │   └── JSONL 持久化 + ContextGuard
 │
 ├──────────────────────┐
 ▼                      ▼
s04: Channels    s06: Intelligence ◀── 本节
 │   └── 消息管道      │   └── 8层提示词 + 记忆 + 技能
 ▼                      │
s05: Gateway            │
 │   └── 路由绑定       │
 │                      │
 └──────────────────────┘
           │
           ▼
      s07: Heartbeat
           └── 心跳使用 soul/memory
```

### 4.2 从 s01 到 s06 的演进

| 章节 | 核心概念 | 系统提示词方式 |
|------|----------|----------------|
| s01 | while 循环 | 硬编码字符串 |
| s02 | 工具调度 | 硬编码 + 工具指南 |
| s03 | 会话持久化 | 硬编码 + 上下文保护 |
| s04 | 渠道管道 | 硬编码 + 渠道适配 |
| s05 | 网关路由 | 硬编码 + 路由信息 |
| **s06** | **智能组装** | **8层动态组装** |

### 4.3 s06 的关键创新

1. **文件化配置**
   - s01-s05: 硬编码字符串
   - s06: 文件系统即配置

2. **动态组装**
   - s01-s05: 固定提示词
   - s06: 每轮重建，记忆实时更新

3. **记忆系统**
   - s01-s05: 无记忆
   - s06: 两层存储 + 混合搜索

4. **技能系统**
   - s01-s05: 无技能
   - s06: 目录扫描 + 自动注入

### 4.4 s06 为后续章节铺垫

| 后续章节 | 依赖 s06 的功能 |
|----------|-----------------|
| s07: Heartbeat | 使用 soul/memory 构建心跳提示词 |
| s08: Delivery | 心跳输出通过消息队列发送 |
| s09: Resilience | 复用 ContextGuard 的溢出处理 |
| s10: Concurrency | 多 Agent 共享工作区配置 |

---

## 5. 代码结构图

```
s06_intelligence.py (950 行)
│
├── 导入与配置 (1-90)
│   ├── 标准库: json, math, os, re, sys, datetime, pathlib
│   ├── 第三方: dotenv, openai
│   ├── 配置常量: MODEL_ID, WORKSPACE_DIR, MAX_* 常量
│   └── ANSI 颜色定义
│
├── Bootstrap 文件加载器 (119-161)
│   └── class BootstrapLoader
│       ├── load_file(name) -> str
│       ├── truncate_file(content, max_chars) -> str
│       └── load_all(mode) -> dict[str, str]
│
├── 灵魂系统 (164-178)
│   └── load_soul(workspace_dir) -> str
│
├── 技能发现与注入 (181-276)
│   └── class SkillsManager
│       ├── _parse_frontmatter(text) -> dict
│       ├── _scan_dir(base) -> list[dict]
│       ├── discover(extra_dirs) -> None
│       └── format_prompt_block() -> str
│
├── 记忆系统 (279-583)
│   └── class MemoryStore
│       │
│       ├── 存储操作
│       │   ├── write_memory(content, category) -> str
│       │   ├── load_evergreen() -> str
│       │   └── _load_all_chunks() -> list[dict]
│       │
│       ├── 关键词搜索
│       │   ├── _tokenize(text) -> list[str]
│       │   └── search_memory(query, top_k) -> list
│       │
│       ├── 混合搜索增强 (400-572)
│       │   ├── _hash_vector(text, dim) -> list[float]
│       │   ├── _vector_cosine(a, b) -> float
│       │   ├── _bm25_rank_to_score(rank) -> float
│       │   ├── _jaccard_similarity(a, b) -> float
│       │   ├── _vector_search(query, chunks) -> list
│       │   ├── _keyword_search(query, chunks) -> list
│       │   ├── _merge_hybrid_results(...) -> list
│       │   ├── _temporal_decay(results) -> list
│       │   ├── _mmr_rerank(results) -> list
│       │   └── hybrid_search(query, top_k) -> list
│       │
│       └── get_stats() -> dict
│
├── 记忆工具 (586-603)
│   ├── tool_memory_write(content, category) -> str
│   └── tool_memory_search(query, top_k) -> str
│
├── 工具定义 (606-663)
│   ├── TOOLS: list[dict]  # 工具 schema
│   ├── TOOL_HANDLERS: dict[str, callable]
│   └── process_tool_call(name, input) -> str
│
├── 系统提示词组装 (666-746)
│   └── build_system_prompt(...) -> str
│       ├── 第1层: Identity
│       ├── 第2层: Soul
│       ├── 第3层: Tools
│       ├── 第4层: Skills
│       ├── 第5层: Memory
│       ├── 第6层: Bootstrap
│       ├── 第7层: Runtime
│       └── 第8层: Channel
│
├── Agent 循环 (749-930)
│   ├── handle_repl_command() -> bool
│   ├── _auto_recall(user_message) -> str
│   └── agent_loop() -> None
│       ├── 启动阶段: 加载 bootstrap, 发现技能
│       ├── 主循环: 用户输入 → 记忆搜索 → 提示词构建
│       └── 内循环: 工具调用处理
│
└── 入口 (933-950)
    └── main() -> None
```

---

## 6. 关键设计决策总结

### 6.1 为什么是 8 层？

| 层级 | 影响力 | 理由 |
|------|--------|------|
| Identity | 最高 | 定义"我是谁"，越靠前越基础 |
| Soul | 高 | 人格塑造，紧随身份 |
| Tools | 中高 | 行为约束 |
| Skills | 中 | 能力扩展 |
| Memory | 中 | 上下文补充 |
| Bootstrap | 中低 | 项目特定信息 |
| Runtime | 低 | 实时状态 |
| Channel | 低 | 输出适配 |

### 6.2 为什么使用 TF-IDF 而非外部向量 API？

1. **教学目的**：展示搜索原理
2. **无依赖**：不需要额外 API key
3. **透明性**：算法完全可见
4. **混合策略**：结合哈希向量实现语义搜索

### 6.3 为什么记忆分为两层？

| 层 | 特点 | 优势 |
|----|------|------|
| MEMORY.md | 手动维护 | 精确、稳定 |
| daily/*.jsonl | 自动写入 | 动态、时效 |

两层互补：静态事实 + 动态学习

### 6.4 为什么技能发现只在启动时执行？

- 技能目录变化频率低
- 避免每轮 IO 开销
- 可通过重启或命令刷新

---

## 7. 运行方式

```bash
# 进入项目根目录
cd claw0

# 运行 s06
python sessions/zh/s06_intelligence.py

# REPL 命令
/soul      # 查看人格
/skills    # 查看技能
/memory    # 记忆统计
/search <query>  # 搜索记忆
/prompt    # 查看完整提示词
/bootstrap # 查看加载的文件
quit/exit  # 退出
```

---

## 8. 文件依赖

```
workspace/
├── SOUL.md           # 人格定义
├── IDENTITY.md       # 身份描述
├── TOOLS.md          # 工具使用指南
├── USER.md           # 用户信息
├── HEARTBEAT.md      # 心跳配置
├── BOOTSTRAP.md      # 启动信息
├── AGENTS.md         # Agent 列表
├── MEMORY.md         # 长期记忆
├── skills/           # 技能目录
│   └── <skill-name>/
│       └── SKILL.md
└── memory/
    └── daily/
        └── YYYY-MM-DD.jsonl
```

---

*文档生成时间: 2025-03-10*