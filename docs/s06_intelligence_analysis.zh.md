# s06_intelligence.py 分析文档

## 概述

`s06_intelligence.py` 是 claw0 框架的第 6 节，实现了 Agent 的"大脑"组装系统。它演示了如何在每轮对话前动态构建系统提示词（System Prompt），通过 8 个层级的组件组合，赋予 Agent 人格、记忆、技能和上下文。

**核心概念**：系统提示词不再是硬编码的字符串，而是由多个层级动态组装而成。

---

## 设计思想

### 1. 分层架构（Layered Architecture）

系统提示词由 8 个层级组成，按影响力从强到弱排列：

```
第 1 层: 身份 (IDENTITY.md)       - 定义 Agent 的角色和边界
第 2 层: 灵魂 (SOUL.md)           - 定义 Agent 的人格和沟通风格
第 3 层: 工具指南 (TOOLS.md)      - 工具使用规范
第 4 层: 技能 (Skills)            - 可调用的技能集合
第 5 层: 记忆 (MEMORY.md + 每日日志) - 长期事实 + 自动检索的记忆
第 6 层: Bootstrap 上下文         - 其他配置文件 (HEARTBEAT, AGENTS, USER)
第 7 层: 运行时上下文             - Agent ID、模型、时间、渠道
第 8 层: 渠道提示                 - 根据不同渠道的适配提示
```

### 2. 文件即配置（File-as-Configuration）

- 所有配置都存储在 `workspace/` 目录下的 Markdown 文件中
- 通过修改文件内容即可改变 Agent 行为，无需修改代码
- 支持多语言、多环境的配置管理

### 3. 记忆系统设计

**两层存储**：
- **长期记忆**：`MEMORY.md` - 手动维护的持久化事实
- **每日记忆**：`memory/daily/{date}.jsonl` - Agent 自动写入的对话日志

**混合搜索**：
- TF-IDF + 余弦相似度（关键词搜索）
- 模拟向量嵌入（语义搜索）
- 时间衰减（新记忆权重更高）
- MMR 重排序（保证结果多样性）

### 4. 技能发现机制

- 扫描 `workspace/skills/` 目录下的子目录
- 每个技能包含 `SKILL.md` 文件，定义名称、描述、调用方式
- 支持优先级覆盖（后发现的技能覆盖同名技能）

---

## 实现机制

### 1. BootstrapLoader - 配置文件加载器

**职责**：加载和截断 Bootstrap 文件

```python
class BootstrapLoader:
    def load_file(self, name: str) -> str          # 加载单个文件
    def truncate_file(self, content: str, ...) -> str  # 截断超长内容
    def load_all(self, mode: str = "full") -> dict[str, str]  # 加载所有文件
```

**关键特性**：
- 支持 3 种加载模式：`full`（主 Agent）、`minimal`（子 Agent/Cron）、`none`（最小化）
- 自动截断超长文件（默认 20,000 字符）
- 总大小限制（默认 150,000 字符）

### 2. SkillsManager - 技能管理器

**职责**：发现、解析和格式化技能

```python
class SkillsManager:
    def _parse_frontmatter(self, text: str) -> dict[str, str]  # 解析 YAML frontmatter
    def _scan_dir(self, base: Path) -> list[dict[str, str]]     # 扫描技能目录
    def discover(self, extra_dirs: list[Path] | None = None)    # 发现技能
    def format_prompt_block(self) -> str                        # 格式化为提示词块
```

**扫描优先级**（从高到低）：
1. `extra_dirs`（外部指定）
2. `workspace/skills/`（内置技能）
3. `workspace/.skills/`（托管技能）
4. `workspace/.agents/skills/`（个人 Agent 技能）
5. `.agents/skills/`（项目 Agent 技能）
6. `skills/`（工作区技能）

### 3. MemoryStore - 记忆存储器

**职责**：管理长期记忆和每日记忆，支持混合搜索

```python
class MemoryStore:
    def write_memory(self, content: str, category: str) -> str  # 写入记忆
    def load_evergreen(self) -> str                             # 加载长期记忆
    def hybrid_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]  # 混合搜索
```

**搜索流程**：
1. **关键词搜索**（TF-IDF + 余弦相似度）
2. **向量搜索**（模拟哈希向量嵌入）
3. **结果合并**（加权分数组合）
4. **时间衰减**（新记忆权重更高）
5. **MMR 重排序**（保证多样性）
6. **返回 Top-K**

**算法细节**：
- **TF-IDF**：`score = tf * (log((n+1)/(df+1)) + 1)`
- **余弦相似度**：`dot(a,b) / (|a| * |b|)`
- **向量嵌入**：基于 Token 哈希的随机投影模拟
- **时间衰减**：`score *= exp(-decay_rate * age_days)`
- **MMR**：`lambda * relevance - (1-lambda) * max_similarity`

### 4. build_system_prompt - 系统提示词组装

**职责**：按 8 个层级组装系统提示词

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

**层级组装逻辑**：
1. **身份层**：从 `IDENTITY.md` 获取，或使用默认值
2. **灵魂层**：仅在 `full` 模式下添加 `SOUL.md`
3. **工具层**：添加 `TOOLS.md` 内容
4. **技能层**：仅在 `full` 模式且有技能时添加
5. **记忆层**：添加 `MEMORY.md` 和自动检索的记忆
6. **Bootstrap 层**：添加剩余的配置文件
7. **运行时层**：添加 Agent ID、模型、时间等信息
8. **渠道层**：根据渠道添加适配提示

### 5. Agent Loop - 运行时循环

**职责**：处理用户输入、调用 LLM、执行工具

```python
def agent_loop() -> None:
    # 1. 启动阶段：加载配置、发现技能
    loader = BootstrapLoader(WORKSPACE_DIR)
    bootstrap_data = loader.load_all(mode="full")
    skills_mgr = SkillsManager(WORKSPACE_DIR)
    skills_mgr.discover()
    
    # 2. REPL 循环
    while True:
        # 自动记忆检索
        memory_context = _auto_recall(user_input)
        
        # 重建系统提示词
        system_prompt = build_system_prompt(...)
        
        # 调用 LLM
        response = client.chat.completions.create(...)
        
        # 处理工具调用
        if response.choices[0].finish_reason == "tool_calls":
            # 执行工具并继续循环
```

---

## 主要功能

### 1. REPL 命令

| 命令 | 功能 | 示例 |
|------|------|------|
| `/soul` | 查看 SOUL.md 内容 | `/soul` |
| `/skills` | 列出已发现的技能 | `/skills` |
| `/memory` | 显示记忆统计 | `/memory` |
| `/search <query>` | 搜索记忆 | `/search Python` |
| `/prompt` | 显示完整系统提示词 | `/prompt` |
| `/bootstrap` | 显示 Bootstrap 文件状态 | `/bootstrap` |

### 2. 自动记忆检索

- 每轮对话前，根据用户输入自动检索相关记忆
- 检索结果注入系统提示词的"记忆"层级
- 使用混合搜索（关键词 + 向量）保证召回率

### 3. 工具集成

**memory_write**：保存重要信息到长期记忆
```json
{
  "name": "memory_write",
  "description": "Save an important fact or observation to long-term memory.",
  "input_schema": {
    "properties": {
      "content": {"type": "string", "description": "The fact or observation to remember."},
      "category": {"type": "string", "description": "Category: preference, fact, context, etc."}
    },
    "required": ["content"]
  }
}
```

**memory_search**：搜索存储的记忆
```json
{
  "name": "memory_search",
  "description": "Search stored memories for relevant information, ranked by similarity.",
  "input_schema": {
    "properties": {
      "query": {"type": "string", "description": "Search query."},
      "top_k": {"type": "integer", "description": "Max results. Default: 5."}
    },
    "required": ["query"]
  }
}
```

### 4. 错误处理

- API 错误：打印错误信息，回滚消息历史
- 工具调用错误：返回错误信息，继续对话
- 文件加载错误：静默失败，返回空字符串

---

## 使用方法

### 运行脚本

```bash
cd claw0
python sessions/zh/s06_intelligence.py
```

### 配置环境

1. 复制 `.env.example` 为 `.env`
2. 设置 `DASHSCOPE_API_KEY` 和 `MODEL_ID`
3. 确保 `workspace/` 目录存在且包含配置文件

### 示例对话

```
You > /soul
--- SOUL.md ---
# Personality Definition

You are Luna, a warm and intellectually curious AI companion.
...

You > 你好，我是小明
  [自动召回] 找到相关记忆
Assistant: 你好，小明！很高兴再次见到你。上次你提到在做一个 AI agent 项目，现在进展如何？

You > /search Python
--- 记忆搜索: Python ---
  [0.8543] memory/2026-03-17.jsonl [general]
    Python 是一种高级编程语言，广泛用于 AI 开发...
```

---

## 架构图

```
+-------------------+
|   Workspace Files |
|   (SOUL.md,       |
|    IDENTITY.md,   |
|    TOOLS.md,      |
|    MEMORY.md...)  |
+-------------------+
         |
         v
+-------------------+        +-------------------+
|  BootstrapLoader  |        |  SkillsManager    |
|  - load_file      |        |  - discover       |
|  - truncate_file  |        |  - format_prompt  |
|  - load_all       |        +-------------------+
+-------------------+               |
         |                          v
         v                   +-------------------+
+-------------------+        |   MemoryStore     |
| build_system_prompt| <----> |  - write_memory   |
|  (8 层组装)        |        |  - hybrid_search  |
+-------------------+        +-------------------+
         |
         v
+-------------------+
|   Agent Loop      |
|  - REPL 命令      |
|  - LLM 调用       |
|  - 工具执行       |
+-------------------+
```

---

## 总结

`s06_intelligence.py` 实现了一个完整的 Agent 智能系统，具有以下特点：

1. **动态提示词组装**：8 层架构，灵活配置
2. **文件即配置**：通过修改 Markdown 文件改变 Agent 行为
3. **混合记忆搜索**：TF-IDF + 向量 + 时间衰减 + MMR
4. **技能发现机制**：自动扫描和加载技能
5. **REPL 交互**：丰富的命令支持调试和探索

该系统为后续章节（心跳、交付、弹性、并发）奠定了基础，是 claw0 框架的核心组件之一。
