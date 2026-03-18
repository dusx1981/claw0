# SkillsManager 设计分析与实现详解

## 文档信息

- **源文件**: `sessions/zh/s06_intelligence.py`
- **类名**: `SkillsManager`
- **代码行数**: 188-276 行 (约 88 行)
- **分析日期**: 2026-03-17

---

## 一、设计思想

### 1.1 核心设计理念

SkillsManager 是 s06 章节"Intelligence (智能)"的关键组件，实现了 **"技能即插件"** 的设计理念。它让 Agent 能够动态发现和加载能力模块，无需修改核心代码即可扩展功能。

**设计目标**:

- **模块化**: 每个技能独立封装，包含元数据和指令
- **可发现**: 自动扫描多个目录，按需加载
- **可覆盖**: 支持技能覆盖机制，允许定制化
- **结构化**: 使用 Frontmatter 定义技能元信息
- **有限性**: 限制最大技能数量，避免提示词溢出

### 1.2 技能定义模型

```
┌─────────────────────────────────────────────────────────────────┐
│                        技能 (Skill)                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  📁 技能目录结构                                                │
│  └── workspace/skills/example-skill/                            │
│      └── SKILL.md          ← 核心定义文件                        │
│                                                                 │
│  📄 SKILL.md 格式                                               │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ ---                                                    │    │
│  │ name: skill-name          ← 技能标识符                  │    │
│  │ description: What it does ← 功能描述                    │    │
│  │ invocation: @skill-name   ← 调用方式                    │    │
│  │ ---                                                    │    │
│  │                                                        │    │
│  │ ## Instructions                                        │    │
│  │ 详细的使用说明...                                       │    │
│  │                                                        │    │
│  │ ### Examples                                           │    │
│  │ - 示例 1                                               │    │
│  │ - 示例 2                                               │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**关键设计**:

- **Frontmatter**: YAML 格式的元数据头部，定义技能的基本信息
- **Markdown 主体**: 详细的使用说明、示例、约束条件
- **调用约定**: 通过 `invocation` 字段定义的特定格式触发

### 1.3 多层目录扫描策略

```
┌─────────────────────────────────────────────────────────────────┐
│                    技能目录优先级 (低到高)                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. workspace/skills/           ← 内置技能 (最低优先级)          │
│     框架自带的基础技能                                          │
│                                                                 │
│  2. workspace/.skills/          ← 托管技能                       │
│     用户或团队托管的技能库                                      │
│                                                                 │
│  3. workspace/.agents/skills/   ← 个人 Agent 技能               │
│     特定 Agent 的专属技能                                       │
│                                                                 │
│  4. ./.agents/skills/           ← 项目 Agent 技能               │
│     当前项目目录下的 Agent 技能                                 │
│                                                                 │
│  5. ./skills/                   ← 工作区技能 (最高优先级)        │
│     当前工作目录的技能，可覆盖上述所有                           │
│                                                                 │
│  🔧 覆盖规则: 同名技能，后发现的覆盖先发现的                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**设计考量**:

- **分层管理**: 不同来源的技能分开放置，便于维护
- **可定制**: 高优先级目录可以覆盖低优先级的同名技能
- **灵活性**: 支持通过 `extra_dirs` 参数添加额外扫描路径
- **安全性**: 隐藏目录 (`.` 开头) 的技能不会误被删除

---

## 二、实现机制详解

### 2.1 数据结构

#### 2.1.1 技能对象结构

```python
{
    "name": "example-skill",              # 技能唯一标识
    "description": "Does something useful", # 简短描述
    "invocation": "@example",              # 调用触发词
    "body": "## Instructions\n...",        # 完整指令内容
    "path": "/path/to/skill/dir"          # 文件系统路径
}
```

#### 2.1.2 类属性

```python
class SkillsManager:
    workspace_dir: Path                    # 工作区根目录
    skills: list[dict[str, str]]          # 已发现的技能列表
```

### 2.2 核心方法实现

#### 2.2.1 Frontmatter 解析器

```python
def _parse_frontmatter(self, text: str) -> dict[str, str]:
    """解析简单的 YAML frontmatter, 不依赖 pyyaml."""
    meta: dict[str, str] = {}
    
    # 检查是否以 --- 开头
    if not text.startswith("---"):
        return meta
    
    # 分割出 frontmatter 部分
    parts = text.split("---", 2)
    if len(parts) < 3:
        return meta
    
    # 逐行解析 key: value
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        meta[key.strip()] = value.strip()
    
    return meta
```

**实现细节**:

- **无依赖设计**: 不使用 PyYAML，纯 Python 实现
- **严格格式**: 必须以 `---` 开头和分隔
- **简单解析**: 仅支持 `key: value` 格式，不支持嵌套
- **容错处理**: 格式错误返回空 dict，不抛出异常

**Frontmatter 示例**:

```yaml
---
name: code-review
description: Review code for best practices and bugs
invocation: @review
---
```

#### 2.2.2 目录扫描器

```python
def _scan_dir(self, base: Path) -> list[dict[str, str]]:
    """扫描单个目录，返回发现的技能列表."""
    found: list[dict[str, str]] = []
    
    # 目录存在性检查
    if not base.is_dir():
        return found
    
    # 遍历子目录 (按字母顺序)
    for child in sorted(base.iterdir()):
        # 只处理目录
        if not child.is_dir():
            continue
        
        # 查找 SKILL.md
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue
        
        # 解析 frontmatter
        meta = self._parse_frontmatter(content)
        if not meta.get("name"):
            continue  # name 是必填字段
        
        # 提取 body (frontmatter 之后的内容)
        body = ""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()
        
        # 构建技能对象
        found.append({
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "invocation": meta.get("invocation", ""),
            "body": body,
            "path": str(child),
        })
    
    return found
```

**扫描规则**:

1. **目录遍历**: 只扫描直接子目录，不递归深层目录
2. **SKILL.md 必需**: 每个技能目录必须包含 SKILL.md 文件
3. **name 必填**: frontmatter 中必须有 name 字段
4. **排序**: 使用 `sorted()` 确保扫描顺序可预测
5. **容错**: 任何读取/解析错误都跳过，不中断扫描

#### 2.2.3 技能发现引擎

```python
def discover(self, extra_dirs: list[Path] | None = None) -> None:
    """按优先级扫描技能目录; 同名技能后者覆盖前者."""
    
    # 构建扫描顺序 (低优先级 → 高优先级)
    scan_order: list[Path] = []
    if extra_dirs:
        scan_order.extend(extra_dirs)
    scan_order.append(self.workspace_dir / "skills")
    scan_order.append(self.workspace_dir / ".skills")
    scan_order.append(self.workspace_dir / ".agents" / "skills")
    scan_order.append(Path.cwd() / ".agents" / "skills")
    scan_order.append(Path.cwd() / "skills")
    
    # 收集技能 (后发现的覆盖先发现的)
    seen: dict[str, dict[str, str]] = {}
    for d in scan_order:
        for skill in self._scan_dir(d):
            seen[skill["name"]] = skill  # 覆盖同名技能
    
    # 限制最大技能数，转换为列表
    self.skills = list(seen.values())[:MAX_SKILLS]
```

**优先级策略**:

```
覆盖强度: extra_dirs < skills < .skills < .agents/skills < ./.agents/skills < ./skills

示例:
  workspace/skills/git.md           → name: git (基础版本)
  workspace/.skills/git.md          → name: git (覆盖基础版)
  ./skills/git.md                   → name: git (最终版本，最高优先级)
```

**限制机制**:

```python
MAX_SKILLS = 150        # 最多加载 150 个技能
MAX_SKILLS_PROMPT = 30000  # 技能提示词块最大 30000 字符
```

#### 2.2.4 提示词格式化器

```python
def format_prompt_block(self) -> str:
    """将技能格式化为系统提示词的 Skills 区块."""
    if not self.skills:
        return ""
    
    lines = ["## Available Skills", ""]
    total = 0
    
    for skill in self.skills:
        # 格式化单个技能块
        block = (
            f"### Skill: {skill['name']}\n"
            f"Description: {skill['description']}\n"
            f"Invocation: {skill['invocation']}\n"
        )
        
        # 添加 body (如果存在)
        if skill.get("body"):
            block += f"\n{skill['body']}\n"
        block += "\n"
        
        # 检查长度限制
        if total + len(block) > MAX_SKILLS_PROMPT:
            lines.append(f"(... more skills truncated)")
            break
        
        lines.append(block)
        total += len(block)
    
    return "\n".join(lines)
```

**输出格式示例**:

```markdown
## Available Skills

### Skill: git-commit
Description: Generate conventional commit messages
Invocation: @commit

## Instructions
When the user types @commit, analyze the git diff and generate a commit message following conventional commits format.

### Format
- type(scope): subject
- body (optional)
- footer (optional)

### Types
- feat: New feature
- fix: Bug fix
- docs: Documentation
...

### Skill: code-review
Description: Review code for issues
Invocation: @review
...
```

---

## 三、主要功能

### 3.1 API 方法一览

| 方法 | 功能 | 输入 | 输出 |
|------|------|------|------|
| `__init__` | 初始化管理器 | workspace_dir: Path | SkillsManager 实例 |
| `_parse_frontmatter` | 解析 YAML frontmatter | text: str | 元数据字典 |
| `_scan_dir` | 扫描单个目录 | base: Path | 技能列表 |
| `discover` | 发现并加载技能 | extra_dirs: list[Path] | None (更新 self.skills) |
| `format_prompt_block` | 格式化为提示词 | - | Markdown 字符串 |

### 3.2 技能发现流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     技能发现流程图                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. 初始化                                                      │
│     └── SkillsManager(workspace_dir)                           │
│                                                                 │
│  2. 发现技能                                                    │
│     └── discover(extra_dirs=optional)                          │
│         ├── 构建扫描路径列表                                    │
│         │   ├── extra_dirs (如果有)                            │
│         │   ├── workspace/skills/                              │
│         │   ├── workspace/.skills/                             │
│         │   ├── workspace/.agents/skills/                      │
│         │   ├── ./.agents/skills/                              │
│         │   └── ./skills/                                      │
│         │                                                      │
│         ├── 遍历每个路径                                        │
│         │   └── _scan_dir(path)                                │
│         │       ├── 遍历子目录                                 │
│         │       ├── 查找 SKILL.md                              │
│         │       ├── 解析 frontmatter                           │
│         │       └── 构建技能对象                               │
│         │                                                      │
│         └── 合并与限制                                          │
│             ├── 同名覆盖 (后发现的优先)                        │
│             └── 限制 MAX_SKILLS (默认 150)                     │
│                                                                 │
│  3. 格式化输出                                                  │
│     └── format_prompt_block()                                  │
│         ├── 遍历已发现技能                                     │
│         ├── 格式化为 Markdown                                  │
│         └── 限制 MAX_SKILLS_PROMPT (默认 30000 字符)           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 使用示例

#### 初始化与发现

```python
from pathlib import Path
from s06_intelligence import SkillsManager

# 初始化
workspace = Path("./workspace")
skills_mgr = SkillsManager(workspace)

# 发现技能 (扫描默认路径)
skills_mgr.discover()

# 或添加额外扫描路径
extra = [Path("./custom-skills"), Path("/shared/skills")]
skills_mgr.discover(extra_dirs=extra)

print(f"发现 {len(skills_mgr.skills)} 个技能")
```

#### 格式化提示词

```python
# 生成系统提示词的技能区块
skills_block = skills_mgr.format_prompt_block()

# 集成到完整系统提示词
system_prompt = build_system_prompt(
    mode="full",
    bootstrap=bootstrap_data,
    skills_block=skills_block,  # 注入技能
    memory_context=memory_context,
)
```

#### 列出现有技能

```python
for skill in skills_mgr.skills:
    print(f"  {skill['invocation']}  {skill['name']}")
    print(f"    {skill['description']}")
    print(f"    path: {skill['path']}")
```

---

## 四、关键设计决策

### 4.1 为什么选择目录结构而非单文件?

| 方案 | 优点 | 缺点 | 选择理由 |
|------|------|------|----------|
| **目录+SKILL.md** | 扩展性强，可包含附件 | 文件多 | 未来可扩展 |
| **单 JSON/YAML** | 集中管理 | 不易维护 | 不适合教学 |
| **数据库** | 结构化查询 | 复杂度高 | 过度设计 |

### 4.2 为什么使用 Frontmatter?

- **标准化**: 遵循 Jekyll/Hugo 等静态站点生成器的惯例
- **分离关注点**: 元数据和内容分离
- **人类可读**: Markdown 格式，易于编辑
- **工具友好**: 简单解析器即可提取元数据

### 4.3 为什么限制技能数量和大小?

```python
MAX_SKILLS = 150              # 控制内存占用
MAX_SKILLS_PROMPT = 30000     # 控制提示词长度
```

**原因**:

1. **上下文窗口限制**: LLM 有 token 上限，过多技能会溢出
2. **注意力稀释**: 技能太多会降低模型对每个技能的关注
3. **性能考虑**: 解析和格式化大量技能消耗资源
4. **教学清晰**: 限制数量强迫开发者选择最重要的技能

---

## 五、与 OpenClaw 生产版本的对比

| 特性 | claw0 (教学版) | OpenClaw (生产版) |
|------|----------------|-------------------|
| 技能格式 | Frontmatter + Markdown | JSON Schema + 代码 |
| 扫描策略 | 静态目录扫描 | 动态注册 + 热加载 |
| 覆盖机制 | 简单字典覆盖 | 版本控制 + 依赖解析 |
| 参数校验 | 无 | JSON Schema 校验 |
| 执行方式 | 文本注入提示词 | 代码执行沙箱 |
| 权限控制 | 无 | Capability-based |

---

## 六、总结

SkillsManager 是一个**轻量级、教学友好**的技能发现与管理系统，它实现了插件化架构的核心概念：

1. **约定优于配置**: 通过固定目录结构和文件命名约定实现零配置
2. **分层覆盖**: 支持多层技能目录，实现基础版→定制版的渐进式覆盖
3. **有限加载**: 限制数量和大小，保证系统稳定性
4. **纯文本技能**: 技能即文本，简单透明易于理解

**设计亮点**:

- ✅ 零依赖 (不依赖 PyYAML 等库)
- ✅ 清晰的优先级策略
- ✅ 良好的容错性 (格式错误不崩溃)
- ✅ 自动截断保护 (防止提示词溢出)

**改进方向** (生产环境):

- 技能依赖声明和自动解析
- JSON Schema 参数校验
- 技能签名验证
- 代码执行沙箱 (安全运行技能代码)
- 热加载 (无需重启 Agent)

---

## 附录: 类图

```
┌─────────────────────────────────────────────────────────┐
│                    SkillsManager                        │
├─────────────────────────────────────────────────────────┤
│  Attributes                                             │
│  ────────────                                           │
│  - workspace_dir: Path                                  │
│  - skills: list[dict[str, str]]                         │
│                                                         │
│  Public Methods                                         │
│  ──────────────                                         │
│  + __init__(workspace_dir: Path)                        │
│  + discover(extra_dirs: list[Path])                     │
│  + format_prompt_block() → str                          │
│                                                         │
│  Private Methods                                        │
│  ───────────────                                        │
│  - _parse_frontmatter(text: str) → dict[str, str]       │
│  - _scan_dir(base: Path) → list[dict[str, str]]         │
└─────────────────────────────────────────────────────────┘
```

---

## 附录: 技能定义示例

### 完整 SKILL.md 示例

```markdown
---
name: git-commit
description: Generate conventional commit messages from git diff
invocation: @commit
---

## When to Use

When the user wants to create a git commit message, invoke this skill by typing `@commit`.

## Instructions

1. Analyze the staged changes using `git diff --cached`
2. Categorize the changes into types (feat, fix, docs, style, refactor, test, chore)
3. Write a concise subject line (max 50 chars)
4. Add detailed body if needed (wrap at 72 chars)

## Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

## Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation only
- **style**: Code style (formatting, semicolons, etc)
- **refactor**: Code change that neither fixes bug nor adds feature
- **test**: Adding or correcting tests
- **chore**: Build process or auxiliary tool changes

## Examples

### Simple Feature

```
feat(auth): add JWT token validation

Implement JWT validation middleware for protected routes.
Supports RS256 and HS256 algorithms.
```

### Bug Fix with Breaking Change

```
fix(api): correct user profile update endpoint

BREAKING CHANGE: PATCH /api/users/:id now requires auth header
```

## Constraints

- Subject line must be imperative mood ("Add" not "Added")
- Don't capitalize first letter of subject
- No period at end of subject line
- Use body to explain what and why, not how
```

---

*本文档由 AI Agent 生成，用于解析 claw0 教学项目中的 SkillsManager 设计。*
