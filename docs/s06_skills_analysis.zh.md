# s06_intelligence.py 技能系统分析

## 概述

`s06_intelligence.py` 中的技能系统是一个模块化、可扩展的插件架构，允许 Agent 动态加载和使用各种功能模块。本文档深入分析技能系统的设计思想、实现机制，以及技能之间的互补关系和协作模式。

---

## 技能系统架构

### 1. 技能发现与加载流程

```
Workspace 目录结构:
workspace/
├── skills/              # 内置技能 (最高优先级)
│   └── example-skill/
│       └── SKILL.md
├── .skills/             # 托管技能
├── .agents/skills/      # 个人 Agent 技能
├── skills/ (项目级)     # 工作区技能
└── ...

扫描顺序 (优先级从高到低):
1. extra_dirs (外部指定)
2. workspace/skills/ (内置技能)
3. workspace/.skills/ (托管技能)
4. workspace/.agents/skills/ (个人 Agent 技能)
5. .agents/skills/ (项目 Agent 技能)
6. skills/ (工作区技能)
```

### 2. 技能定义格式 (SKILL.md)

每个技能由一个目录和其中的 `SKILL.md` 文件定义：

```markdown
---
name: skill-name
description: 技能描述
invocation: /command  # 用户调用方式
---
# 技能主体内容

这里是技能的详细说明和行为指令。
Agent 会根据这些指令来执行技能。
```

**Frontmatter 字段说明**：
- `name`: 技能的唯一标识符（必填）
- `description`: 技能的简短描述（用于提示词）
- `invocation`: 用户调用该技能的命令格式
- 技能主体：详细的指令和行为定义

### 3. SkillsManager 类设计

```python
class SkillsManager:
    def __init__(self, workspace_dir: Path) -> None
    def _parse_frontmatter(self, text: str) -> dict[str, str]  # 解析 YAML frontmatter
    def _scan_dir(self, base: Path) -> list[dict[str, str]]     # 扫描单个目录
    def discover(self, extra_dirs: list[Path] | None = None)    # 发现所有技能
    def format_prompt_block(self) -> str                        # 格式化为提示词块
```

**关键特性**：
- **优先级覆盖**：同名技能按扫描顺序，后发现的覆盖先发现的
- **内存限制**：最多加载 150 个技能，提示词块最多 30,000 字符
- **容错处理**：文件读取失败时静默跳过

---

## 技能互补关系分析

### 1. 技能分类体系

基于功能职责，技能可分为以下几类：

| 技能类型 | 职责 | 示例 | 互补关系 |
|---------|------|------|---------|
| **工具类技能** | 执行具体操作 | 文件读写、内存管理 | 需要与"决策类技能"配合 |
| **决策类技能** | 判断和选择 | 条件判断、路由选择 | 依赖工具类技能执行操作 |
| **信息类技能** | 查询和检索 | 搜索、知识查询 | 为其他技能提供数据支持 |
| **交互类技能** | 用户交互 | 提问、确认 | 与其他技能协作完成任务 |
| **编排类技能** | 协调多个技能 | 工作流、任务分解 | 调用其他技能组合完成复杂任务 |

### 2. 技能协作模式

#### 模式 1: 串行协作 (Sequential Cooperation)

**场景**：任务需要按顺序执行多个步骤

```
用户请求: "帮我保存这个文件并搜索相关内容"

技能流程:
1. file_write (保存文件)
2. memory_write (记录保存操作)
3. memory_search (搜索相关内容)
4. file_read (读取搜索结果)
```

**实现示例**：
```python
# Skill A: 文件保存技能
def save_file_and_remember(content, filename):
    # 1. 保存文件
    file_write(content, filename)
    # 2. 记录到记忆
    memory_write(f"保存了文件: {filename}", category="file_ops")
    # 3. 返回结果
    return f"文件 {filename} 已保存"
```

#### 模式 2: 并行协作 (Parallel Cooperation)

**场景**：多个独立任务可以同时进行

```
用户请求: "分析这个项目并检查代码质量"

技能流程 (并行):
├── code_analysis (代码分析)
├── quality_check (质量检查)
└── report_generate (生成报告)

结果合并 -> 综合报告
```

#### 模式 3: 条件协作 (Conditional Cooperation)

**场景**：根据条件选择不同的技能路径

```
用户请求: "处理这个数据"

决策流程:
if 数据格式 == "JSON":
    使用 json_parser 技能
elif 数据格式 == "CSV":
    使用 csv_parser 技能
else:
    使用 generic_parser 技能
```

#### 模式 4: 嵌套协作 (Nested Cooperation)

**场景**：一个技能内部调用其他技能

```
高级技能: "项目部署"
├── 调用: 代码检查技能
├── 调用: 测试运行技能
├── 调用: 构建技能
└── 调用: 部署技能
```

### 3. 技能依赖关系

```
基础技能层:
├── file_read/write (文件操作)
├── memory_read/write (记忆操作)
└── bash (命令执行)

中级技能层:
├── code_analysis (依赖: file_read)
├── data_processing (依赖: file_read, memory)
└── search (依赖: memory)

高级技能层:
├── project_deploy (依赖: code_analysis, bash)
├── workflow_orchestration (依赖: 多个中级技能)
└── auto_test (依赖: code_analysis, bash)
```

---

## 技能协作实例

### 实例 1: 文件管理工作流

**场景**：用户要求"保存代码片段并搜索相关文档"

```
技能协作流程:

1. 用户输入: "保存这个函数并搜索类似实现"
   
2. 决策技能分析意图:
   - 识别需要: 文件保存 + 代码搜索
   - 选择技能: file_write + memory_search

3. 执行流程:
   ├── file_write (保存函数到文件)
   ├── memory_write (记录保存操作)
   ├── memory_search (搜索类似函数)
   └── file_read (读取搜索结果文件)

4. 结果整合:
   - 返回保存状态
   - 提供搜索结果摘要
   - 建议下一步操作
```

**技能配置示例**:
```markdown
---
name: file-management-workflow
description: 文件管理工作流 - 保存并搜索
invocation: /manage-file
---
# 文件管理工作流

当用户请求保存文件并搜索相关内容时:
1. 使用 file_write 保存内容
2. 使用 memory_write 记录操作
3. 使用 memory_search 搜索相关内容
4. 返回综合结果
```

### 实例 2: 代码审查工作流

**场景**：自动代码审查和质量检查

```
技能协作流程:

1. 用户输入: "审查这个代码文件"

2. 技能组合:
   ├── code_analysis (语法检查)
   ├── quality_check (质量评估)
   ├── security_scan (安全检查)
   └── report_generate (生成报告)

3. 并行执行:
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ 语法分析    │    │ 质量评估    │    │ 安全扫描    │
   └─────────────┘    └─────────────┘    └─────────────┘
           │                  │                  │
           └────────┬─────────┴────────┬─────────┘
                    ▼                  ▼
              ┌─────────────────────────┐
              │    报告生成与整合       │
              └─────────────────────────┘

4. 输出: 综合审查报告
```

### 实例 3: 记忆增强问答

**场景**：基于记忆的智能问答

```
技能协作流程:

1. 用户提问: "我上次讨论的项目进度如何？"

2. 技能协作:
   ├── memory_search (检索相关记忆)
   ├── context_analysis (分析上下文)
   ├── knowledge_retrieval (检索知识库)
   └── response_generate (生成回答)

3. 数据流:
   用户问题
      ↓
   memory_search (查找历史对话)
      ↓
   context_analysis (理解当前上下文)
      ↓
   knowledge_retrieval (补充专业知识)
      ↓
   response_generate (生成综合回答)
```

---

## 技能系统实现细节

### 1. 提示词注入机制

SkillsManager 将技能信息格式化为系统提示词的一部分：

```python
def format_prompt_block(self) -> str:
    if not self.skills:
        return ""
    lines = ["## Available Skills", ""]
    total = 0
    for skill in self.skills:
        block = (
            f"### Skill: {skill['name']}\n"
            f"Description: {skill['description']}\n"
            f"Invocation: {skill['invocation']}\n"
        )
        if skill.get("body"):
            block += f"\n{skill['body']}\n"
        block += "\n"
        if total + len(block) > MAX_SKILLS_PROMPT:
            lines.append(f"(... more skills truncated)")
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)
```

**输出示例**:
```markdown
## Available Skills

### Skill: example-skill
Description: A sample skill for demonstration
Invocation: /example

When the user invokes /example, respond with a friendly greeting...
```

### 2. 技能发现优先级

```python
scan_order = [
    extra_dirs,                          # 外部指定 (最高优先级)
    workspace_dir / "skills",           # 内置技能
    workspace_dir / ".skills",          # 托管技能
    workspace_dir / ".agents" / "skills", # 个人 Agent 技能
    Path.cwd() / ".agents" / "skills",  # 项目 Agent 技能
    Path.cwd() / "skills",              # 工作区技能 (最低优先级)
]
```

### 3. 技能覆盖机制

```python
seen: dict[str, dict[str, str]] = {}
for d in scan_order:
    for skill in self._scan_dir(d):
        seen[skill["name"]] = skill  # 后发现的覆盖先发现的
self.skills = list(seen.values())[:MAX_SKILLS]
```

---

## 最佳实践

### 1. 技能设计原则

| 原则 | 说明 | 示例 |
|------|------|------|
| **单一职责** | 每个技能只做一件事 | `file_read` 只读文件，不修改 |
| **明确接口** | 输入输出清晰定义 | `search(query: str, top_k: int) -> list[dict]` |
| **容错处理** | 失败时提供有意义的错误信息 | 返回错误描述而非抛出异常 |
| **组合友好** | 易于与其他技能配合 | 不依赖特定上下文，可独立使用 |

### 2. 技能协作设计

```
良好设计:
├── 小技能 (原子操作)
│   ├── file_read
│   ├── file_write
│   └── memory_search
└── 组合技能 (工作流)
    └── file_management (调用多个小技能)

避免:
├── 大而全的技能 (难以复用)
└── 过度耦合 (技能间强依赖)
```

### 3. 技能目录结构

```
workspace/skills/
├── file-operations/
│   ├── SKILL.md          # 技能定义
│   └── implementation/   # 可选: 实现代码
├── memory-management/
│   └── SKILL.md
└── workflow-orchestration/
    └── SKILL.md
```

---

## 总结

`s06_intelligence.py` 的技能系统具有以下特点：

1. **模块化设计**：每个技能独立定义，易于扩展和维护
2. **动态发现**：自动扫描技能目录，支持优先级覆盖
3. **灵活协作**：支持串行、并行、条件、嵌套等多种协作模式
4. **提示词集成**：技能信息注入系统提示词，指导 Agent 行为
5. **容错机制**：文件读取失败时静默跳过，保证系统稳定性

通过合理的技能拆分和协作设计，可以构建强大的 Agent 能力体系，支持复杂的任务处理和自动化工作流。
