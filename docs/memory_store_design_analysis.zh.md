# MemoryStore 设计分析与实现详解

## 文档信息

- **源文件**: `sessions/zh/s06_intelligence.py`
- **类名**: `MemoryStore`
- **代码行数**: 288-584 行 (约 296 行)
- **分析日期**: 2026-03-17

---

## 一、设计思想

### 1.1 核心设计理念

MemoryStore 是 s06 章节"Intelligence (智能)"的核心组件，其设计遵循**"赋予灵魂, 教会记忆"**的教学目标。它实现了 Agent 框架中的记忆系统，解决了一个关键问题：**如何让 AI Agent 具备长期记忆能力？**

### 1.2 两层存储架构

```
┌─────────────────────────────────────────────────────────────┐
│                    MemoryStore 存储架构                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  第一层: 长期记忆 (Evergreen)                                │
│  ├── 位置: workspace/MEMORY.md                              │
│  ├── 特点: 手动维护, 持久化, 跨会话                         │
│  ├── 内容: 用户偏好, 关键事实, 系统配置                     │
│  └── 格式: Markdown 段落                                    │
│                                                             │
│  第二层: 每日日志 (Daily Log)                               │
│  ├── 位置: workspace/memory/daily/{YYYY-MM-DD}.jsonl        │
│  ├── 特点: 自动写入, 时间序列, 可搜索                       │
│  ├── 内容: Agent 学习到的用户事实                           │
│  └── 格式: JSON Lines (每行一条 JSON)                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**设计考量**:

- **长期记忆**: 适合存放不常变更的核心信息，由开发者或用户手动维护
- **每日日志**: 适合存放动态学习的短期记忆，通过 `memory_write` 工具自动写入
- **时间衰减**: 较新的记忆在搜索时获得更高权重，模拟人类记忆的遗忘曲线

### 1.3 混合搜索管道

MemoryStore 采用了**多阶段搜索管道**设计，模拟现代 RAG (Retrieval-Augmented Generation) 系统的检索流程：

```
用户查询 → 关键词搜索 ──┐
                       ├── 结果融合 → 时间衰减 → MMR 重排序 → 最终结果
         → 向量搜索 ───┘
```

**设计优势**:

- **关键词搜索**: 精确匹配，召回率高
- **向量搜索**: 语义相似，泛化能力强
- **时间衰减**: 优先返回近期记忆
- **MMR 重排序**: 保证结果多样性，避免重复

---

## 二、实现机制详解

### 2.1 数据结构设计

#### 2.1.1 记忆块 (Chunk) 结构

```python
# 来自长期记忆
{
    "path": "MEMORY.md",           # 来源标识
    "text": "用户喜欢 Python"      # 记忆内容
}

# 来自每日日志
{
    "path": "2025-03-17.jsonl [category]",  # 文件名 + 类别
    "text": "用户今天学会了新技能"          # 记忆内容
}
```

#### 2.1.2 记忆条目结构 (JSONL)

```python
{
    "ts": "2026-03-17T10:30:00+00:00",  # ISO 8601 时间戳
    "category": "preference",             # 记忆类别
    "content": "用户喜欢暗色主题"          # 记忆内容
}
```

### 2.2 核心算法实现

#### 2.2.1 分词器 (Tokenizer)

```python
@staticmethod
def _tokenize(text: str) -> list[str]:
    """分词: 小写英文 + 单个 CJK 字符, 过滤短 token."""
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())
    return [t for t in tokens if len(t) > 1 or "\u4e00" <= t <= "\u9fff"]
```

**实现细节**:

- 正则表达式 `[a-z0-9\u4e00-\u9fff]+` 匹配英文数字和 CJK 字符
- 英文 token 过滤长度 ≤1 的词（如 "a", "I"）
- 中文字符单独保留（每个汉字都是有效 token）
- 全部转小写，实现大小写不敏感匹配

#### 2.2.2 TF-IDF 计算

```python
def tfidf(tokens: list[str]) -> dict[str, float]:
    """计算 TF-IDF 向量"""
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    # IDF = log((N + 1) / (df + 1)) + 1  (平滑处理)
    return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1) 
            for t, c in tf.items()}
```

**公式说明**:

- **TF (词频)**: 单词在文档中出现次数
- **IDF (逆文档频率)**: `log((N + 1) / (df + 1)) + 1`
  - N: 文档总数
  - df: 包含该词的文档数
  - +1 平滑处理避免除零
- **TF-IDF**: TF × IDF，衡量单词在文档中的重要性

#### 2.2.3 余弦相似度

```python
def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """计算两个稀疏向量的余弦相似度"""
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)  # 点积
    na = math.sqrt(sum(v * v for v in a.values()))  # 向量 A 模长
    nb = math.sqrt(sum(v * v for v in b.values()))  # 向量 B 模长
    return dot / (na * nb) if na and nb else 0.0
```

**数学原理**:

```
cos(θ) = (A · B) / (||A|| × ||B||)
```

- 返回值范围: [0, 1]
- 1 表示完全相似，0 表示完全不相关

#### 2.2.4 模拟向量嵌入 (Simulated Vector Embedding)

```python
@staticmethod
def _hash_vector(text: str, dim: int = 64) -> list[float]:
    """基于哈希的随机投影模拟向量嵌入"""
    tokens = MemoryStore._tokenize(text)
    vec = [0.0] * dim
    for token in tokens:
        h = hash(token)
        for i in range(dim):
            bit = (h >> (i % 62)) & 1
            vec[i] += 1.0 if bit else -1.0
    # L2 归一化
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
```

**设计巧思**:

- **无外部依赖**: 不调用 OpenAI/Claude API，纯 Python 实现
- **哈希投影**: 利用 Python 内置 `hash()` 函数生成确定性向量
- **位运算**: `(h >> (i % 62)) & 1` 提取哈希值的第 i 位
- **教学目的**: 演示向量搜索的概念，而非生产级实现

#### 2.2.5 时间衰减算法

```python
@staticmethod
def _temporal_decay(results: list[dict], decay_rate: float = 0.01) -> list[dict]:
    """基于时间的指数衰减: score *= exp(-decay_rate * age_days)"""
    now = datetime.now(timezone.utc)
    for r in results:
        path = r["chunk"].get("path", "")
        # 从文件名提取日期 (YYYY-MM-DD)
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", path)
        if date_match:
            chunk_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
            age_days = (now - chunk_date).total_seconds() / 86400.0
            r["score"] *= math.exp(-decay_rate * age_days)
```

**数学模型**:

```
score_new = score_old × exp(-λ × t)

其中:
- λ = 0.01 (decay_rate)
- t = 记忆年龄(天)
- exp(-0.01 × 30) ≈ 0.74 (30 天衰减约 26%)
- exp(-0.01 × 100) ≈ 0.37 (100 天衰减约 63%)
```

#### 2.2.6 MMR 重排序 (Maximal Marginal Relevance)

```python
@staticmethod
def _mmr_rerank(results: list[dict], lambda_param: float = 0.7) -> list[dict]:
    """MMR = λ × relevance - (1-λ) × max_similarity_to_selected"""
    tokenized = [MemoryStore._tokenize(r["chunk"]["text"]) for r in results]
    selected: list[int] = []
    remaining = list(range(len(results)))
    reranked: list[dict] = []

    while remaining:
        best_idx = -1
        best_mmr = float("-inf")
        for idx in remaining:
            relevance = results[idx]["score"]
            # 计算与已选结果的最大相似度
            max_sim = 0.0
            for sel_idx in selected:
                sim = MemoryStore._jaccard_similarity(tokenized[idx], tokenized[sel_idx])
                if sim > max_sim:
                    max_sim = sim
            # MMR 分数 = λ * 相关性 - (1-λ) * 冗余度
            mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx
        selected.append(best_idx)
        remaining.remove(best_idx)
        reranked.append(results[best_idx])
    return reranked
```

**算法原理**:

- **λ (lambda_param)**: 平衡相关性和多样性的参数
  - λ = 0.7: 更重视相关性
  - λ = 0.3: 更重视多样性
- **贪心选择**: 每次选择 MMR 分数最高的结果
- **Jaccard 相似度**: 计算两个记忆的相似度

```
Jaccard(A, B) = |A ∩ B| / |A ∪ B|
```

### 2.3 搜索管道流程

```
┌──────────────────────────────────────────────────────────────┐
│                     完整混合搜索管道                          │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. 数据加载                                                  │
│     └── _load_all_chunks() → 长期记忆 + 每日日志              │
│                                                              │
│  2. 双通道搜索                                                │
│     ├── _keyword_search()  → TF-IDF + 余弦相似度 (Top 10)     │
│     └── _vector_search()   → 哈希向量 + 余弦相似度 (Top 10)   │
│                                                              │
│  3. 结果融合                                                  │
│     └── _merge_hybrid_results()                              │
│         ├── 向量权重: 0.7                                     │
│         └── 关键词权重: 0.3                                   │
│                                                              │
│  4. 时间衰减                                                  │
│     └── _temporal_decay() → score *= exp(-0.01 * days)        │
│                                                              │
│  5. 多样性重排序                                              │
│     └── _mmr_rerank() → λ=0.7 平衡相关性与多样性              │
│                                                              │
│  6. 结果截断                                                  │
│     └── 返回 Top K (默认 5)                                   │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、主要功能

### 3.1 API 方法一览

| 方法 | 功能 | 输入 | 输出 |
|------|------|------|------|
| `__init__` | 初始化存储 | workspace_dir: Path | MemoryStore 实例 |
| `write_memory` | 写入记忆 | content: str, category: str | 成功/失败消息 |
| `load_evergreen` | 加载长期记忆 | - | MEMORY.md 内容 |
| `search_memory` | TF-IDF 搜索 | query: str, top_k: int | 搜索结果列表 |
| `hybrid_search` | 混合搜索 | query: str, top_k: int | 融合搜索结果 |
| `get_stats` | 获取统计 | - | 记忆统计信息 |

### 3.2 写入记忆

```python
def write_memory(self, content: str, category: str = "general") -> str:
    """将记忆写入当日 JSONL 文件"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = self.memory_dir / f"{today}.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "content": content,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return f"Memory saved to {today}.jsonl ({category})"
```

**特性**:

- **追加写入**: 使用 `"a"` 模式追加到文件末尾
- **原子性**: 每行独立 JSON，损坏不影响其他条目
- **UTF-8 编码**: 支持中英文混排
- **ensure_ascii=False**: 中文字符直接存储，非 Unicode 转义

### 3.3 搜索记忆

#### 基础搜索 (TF-IDF)

```python
results = memory_store.search_memory("用户喜欢什么编程语言", top_k=5)
# 返回: [{"path": "2025-03-17.jsonl [preference]", "score": 0.85, "snippet": "..."}, ...]
```

#### 混合搜索 (推荐)

```python
results = memory_store.hybrid_search("Python 相关", top_k=5)
# 融合关键词 + 向量 + 时间衰减 + MMR
```

### 3.4 统计信息

```python
stats = memory_store.get_stats()
# 返回: {
#     "evergreen_chars": 1500,   # 长期记忆字符数
#     "daily_files": 10,          # 每日文件数量
#     "daily_entries": 250        # 每日条目总数
# }
```

---

## 四、关键设计决策

### 4.1 为什么选择 JSONL 而非数据库?

| 方案 | 优点 | 缺点 | 选择理由 |
|------|------|------|----------|
| **JSONL** | 人类可读, 版本控制友好, 无依赖 | 查询慢, 大数据性能差 | 教学项目，简单优先 |
| SQLite | 结构化查询, 性能较好 | 增加依赖, 需要 SQL | 复杂度增加 |
| 向量数据库 (Pinecone等) | 专业向量搜索 | 外部依赖, 成本 | 不适合教学 |

### 4.2 为什么选择本地文件而非远程 API?

- **隐私**: 用户数据不上传
- **离线**: 无网络也能工作
- **可控**: 完全掌控存储逻辑
- **教学**: 演示底层实现原理

### 4.3 搜索权重设计

```python
# 混合搜索权重
vector_weight = 0.7   # 向量搜索更重要 (语义理解)
text_weight = 0.3     # 关键词搜索辅助 (精确匹配)

# MMR 参数
lambda_param = 0.7    # 更重视相关性而非多样性

# 时间衰减
decay_rate = 0.01     # 每天衰减约 1%
```

---

## 五、使用示例

### 5.1 初始化

```python
from pathlib import Path
from s06_intelligence import MemoryStore

workspace = Path("./workspace")
memory = MemoryStore(workspace)
```

### 5.2 写入记忆

```python
# Agent 学习到用户偏好
memory.write_memory("用户喜欢使用 VS Code 编辑器", category="preference")
memory.write_memory("用户擅长 Python 和 TypeScript", category="skill")
memory.write_memory("用户正在开发一个 AI Agent 项目", category="context")
```

### 5.3 搜索记忆

```python
# 在 Agent Loop 中自动召回
context = "用户用什么编辑器?"
results = memory.hybrid_search(context, top_k=3)

# 结果示例:
# [
#   {
#     "path": "2025-03-17.jsonl [preference]",
#     "score": 0.92,
#     "snippet": "用户喜欢使用 VS Code 编辑器"
#   },
#   {
#     "path": "MEMORY.md",
#     "score": 0.45,
#     "snippet": "用户偏好使用 JetBrains 系列工具"
#   }
# ]
```

### 5.4 集成到 Agent Loop

```python
def _auto_recall(user_message: str) -> str:
    """自动搜索相关记忆并注入系统提示词"""
    results = memory_store.hybrid_search(user_message, top_k=3)
    if not results:
        return ""
    return "\n".join(f"- [{r['path']}] {r['snippet']}" for r in results)

# 在 Agent Loop 中使用
memory_context = _auto_recall(user_input)
system_prompt = build_system_prompt(memory_context=memory_context)
```

---

## 六、与 OpenClaw 生产版本的对比

| 特性 | claw0 (教学版) | OpenClaw (生产版) |
|------|----------------|-------------------|
| 向量存储 | 哈希模拟 (64 维) | 真实嵌入 (1536+ 维) |
| 索引结构 | 全量扫描 O(N) | HNSW/Annoy 近似索引 |
| 存储后端 | JSONL 文件 | PostgreSQL + pgvector |
| 分词器 | 简单正则 | Jieba / spaCy |
| 时间衰减 | 指数衰减 | 可配置策略 |
| 搜索结果融合 | 简单加权 | RRF / Learned Fusion |

---

## 七、总结

MemoryStore 是一个**教学友好型**的记忆系统实现，它用纯 Python 演示了现代 RAG 系统的核心概念：

1. **分层存储**: 长期记忆 + 短期记忆
2. **混合搜索**: 关键词 + 语义
3. **结果优化**: 时间衰减 + 多样性重排序
4. **零依赖设计**: 不依赖外部数据库或 API

**设计亮点**:

- ✅ 清晰的代码结构，易于理解
- ✅ 完整的搜索管道，模拟生产系统
- ✅ 中英文支持，适合中文教学
- ✅ 模块化设计，易于扩展

**改进方向** (生产环境):

- 使用真实向量嵌入 (OpenAI/Claude API)
- 引入向量索引 (HNSW, FAISS)
- 数据库持久化 (PostgreSQL + pgvector)
- 高级重排序模型 (Cross-encoder)

---

## 附录: 类图

```
┌─────────────────────────────────────────────────────────┐
│                     MemoryStore                         │
├─────────────────────────────────────────────────────────┤
│  Attributes                                             │
│  ────────────                                           │
│  - workspace_dir: Path                                  │
│  - memory_dir: Path                                     │
│                                                         │
│  Public Methods                                         │
│  ──────────────                                         │
│  + __init__(workspace_dir: Path)                        │
│  + write_memory(content: str, category: str) → str      │
│  + load_evergreen() → str                               │
│  + search_memory(query: str, top_k: int) → list[dict]   │
│  + hybrid_search(query: str, top_k: int) → list[dict]   │
│  + get_stats() → dict[str, Any]                         │
│                                                         │
│  Private Methods                                        │
│  ───────────────                                        │
│  - _load_all_chunks() → list[dict]                      │
│  - _tokenize(text: str) → list[str]                     │
│  - _hash_vector(text: str, dim: int) → list[float]      │
│  - _vector_search(query, chunks, top_k) → list[dict]    │
│  - _keyword_search(query, chunks, top_k) → list[dict]   │
│  - _merge_hybrid_results(v_results, k_results)          │
│  - _temporal_decay(results, decay_rate)                 │
│  - _mmr_rerank(results, lambda_param)                   │
│  - _jaccard_similarity(tokens_a, tokens_b) → float      │
│  - _vector_cosine(a, b) → float                         │
└─────────────────────────────────────────────────────────┘
```

---

*本文档由 AI Agent 生成，用于解析 claw0 教学项目中的 MemoryStore 设计。*
