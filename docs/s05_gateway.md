# 消息路由设计文档：五层绑定与智能分发

## 1. 引言

在多 Agent 系统中，来自不同渠道（Telegram、飞书、CLI 等）的消息需要被准确派发给最合适的 Agent 进行处理。消息路由模块（Gateway）承担这一核心职责，它通过一个**五层绑定表**，将入站消息（`channel`, `account_id`, `peer_id`, `guild_id`）映射到特定的 Agent，并生成会话隔离键，确保对话上下文的连贯性。

本文档详细阐述该路由系统的设计思想、实现机制及分发过程，并通过可视化示例说明其工作原理。

---

## 2. 设计思想

### 2.1 分层匹配，从具体到通用
路由的核心原则是**最具体的规则优先**。我们将匹配维度划分为五个层级，每一层代表一种匹配粒度：

| 层级 | 名称       | 匹配键       | 示例值                          | 说明                                   |
|------|------------|--------------|---------------------------------|----------------------------------------|
| 1    | **peer**   | `peer_id`    | `telegram:123456`               | 特定用户（可含渠道前缀）               |
| 2    | **guild**  | `guild_id`   | `discord:987654`                | 群组/服务器级别                         |
| 3    | **account**| `account_id` | `tg-primary`                    | 特定机器人账号                         |
| 4    | **channel**| `channel`    | `telegram`                       | 整个消息通道（如所有 Telegram 消息）   |
| 5    | **default**| `default`    | `*`                              | 兜底规则，无匹配时生效                 |

- **优先级**：层级数字越小越优先。同层级内可通过 `priority` 字段进一步控制（数值越大越优先）。
- **规则独立**：每条绑定规则仅在一个层级上定义，避免歧义。

### 2.2 会话隔离与上下文保持
路由不仅要找到 Agent，还要决定如何隔离对话。通过 `dm_scope` 参数控制会话键的构建粒度，可选项包括：
- `per-peer`（默认）：同一 Agent 与同一用户的私聊保持单一会话。
- `per-channel-peer`：同一 Agent 在同一渠道内与同一用户保持会话，跨渠道隔离。
- `per-account-channel-peer`：区分同一 Agent 下的不同机器人账号。
- `main`：全局单一会话（所有消息进入同一对话）。

这种设计允许 Agent 根据业务场景灵活选择上下文隔离策略。

---

## 3. 实现机制

### 3.1 绑定表数据结构
`BindingTable` 内部维护一个 `_bindings` 列表，每个 `Binding` 对象包含：
- `agent_id`：目标 Agent 标识。
- `tier`：层级（1-5）。
- `match_key`：匹配键（如 `"peer_id"`, `"channel"`）。
- `match_value`：匹配值（如 `"telegram:12345"`）。
- `priority`：同层优先级。

添加规则时自动按 `(tier, -priority)` 排序，确保解析时按优先级顺序遍历。

### 3.2 路由解析算法
`resolve()` 方法接收入站消息的四个维度：`channel`, `account_id`, `guild_id`, `peer_id`，按层级顺序检查绑定表：

```python
for b in self._bindings:
    if b.tier == 1 and b.match_key == "peer_id":
        if b.match_value == f"{channel}:{peer_id}" or b.match_value == peer_id:
            return b.agent_id, b
    elif b.tier == 2 and b.match_key == "guild_id" and b.match_value == guild_id:
        return b.agent_id, b
    elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
        return b.agent_id, b
    elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
        return b.agent_id, b
    elif b.tier == 5 and b.match_key == "default":
        return b.agent_id, b
return None, None
```

一旦匹配立即返回，后续规则不再检查。

### 3.3 会话键构建
得到 `agent_id` 后，通过 `build_session_key()` 生成会话键：
- 根据 Agent 配置的 `dm_scope`，将 `channel`, `account_id`, `peer_id` 组合成标准化字符串。
- 格式示例：`agent:luna:direct:telegram:123456`。

会话键用于在 `AgentManager` 中存储和检索对话历史，确保同一会话的消息按序处理。

### 3.4 接入方式
Gateway 提供两种外部接入：
- **WebSocket JSON-RPC 2.0**：允许远程客户端实时发送消息和接收 Typing 广播。
- **本地 REPL**：通过命令行交互，方便调试和管理。

---

## 4. 分发过程可视化

### 4.1 示例绑定规则
假设我们配置了以下五条绑定规则：

| 层级 | 匹配键     | 匹配值               | Agent  | 优先级 |
|------|------------|----------------------|--------|--------|
| 1    | peer_id    | `telegram:1001`      | luna   | 10     |
| 1    | peer_id    | `discord:2002`       | sage   | 5      |
| 2    | guild_id   | `discord:999`        | nova   | 0      |
| 3    | account_id | `tg-primary`         | sage   | 0      |
| 4    | channel    | `telegram`           | echo   | 0      |
| 5    | default    | `*`                  | main   | 0      |

### 4.2 入站消息流
现在模拟一条来自 Telegram 的消息：
- **channel** = `telegram`
- **account_id** = `tg-primary`
- **guild_id** = `""` (私聊，无群组)
- **peer_id** = `1001` (用户 ID)

#### 步骤 1：解析路由
遍历绑定表，依次检查：

| 检查顺序 | 层级 | 匹配条件                     | 是否命中 | 结果          |
|----------|------|------------------------------|----------|---------------|
| 1        | 1    | peer_id 匹配 `telegram:1001` | ✅ 是    | 立即返回 luna |
| 2~6      | -    | 后续规则不再检查             | -        | -             |

最终命中**第1层**的 `peer_id` 规则，`agent_id = luna`。

#### 步骤 2：获取 Agent 配置
查询 `AgentManager`，得到 `luna` 的配置：
- `dm_scope = "per-channel-peer"` (假设)

#### 步骤 3：构建会话键
根据 `dm_scope` 规则：
```
agent:luna:telegram:direct:1001
```
该键将用于存储与用户 `1001` 在 Telegram 渠道上的对话历史。

#### 步骤 4：执行 Agent 处理
将消息发送给 `luna` Agent，同时传入会话键。Agent 加载历史记录，调用 LLM，最后将回复通过原渠道返回。

### 4.3 另一种情况：规则未命中
假设来自 Discord 群组 `999` 的消息：
- **channel** = `discord`
- **account_id** = `discord-bot1`
- **guild_id** = `999`
- **peer_id** = `2002`

匹配过程：
1. 第1层检查 `peer_id`：`discord:2002` 存在规则（`discord:2002` → sage），命中！直接返回 sage。
   - 注意：即使 `guild_id` 也有规则，但第1层优先，所以不会继续检查第2层。

如果 `peer_id` 没有匹配，才会进入第2层 `guild_id` 匹配，等等。

### 4.4 路由决策树
下图用文本表示决策流程（模拟树状结构）：

```
入站消息
├─ 检查 Tier1 (peer_id) ── 是否匹配？ ──是──> 返回 agent
│                              │
│                             否
│                              ↓
├─ 检查 Tier2 (guild_id) ── 是否匹配？ ──是──> 返回 agent
│                              │
│                             否
│                              ↓
├─ 检查 Tier3 (account_id) ─ 是否匹配？ ──是──> 返回 agent
│                              │
│                             否
│                              ↓
├─ 检查 Tier4 (channel) ──── 是否匹配？ ──是──> 返回 agent
│                              │
│                             否
│                              ↓
└─ 检查 Tier5 (default) ──── 总是匹配 ───────> 返回 agent
```

---

## 5. 总结

本消息路由系统通过**五层绑定表**实现了灵活、精确的消息派发，其设计特点包括：
- **分层匹配**：从最具体的 `peer_id` 到最通用的 `default`，确保业务规则可精细控制。
- **优先级控制**：同层内可设置优先级，解决规则冲突。
- **会话隔离**：`dm_scope` 机制允许 Agent 自定义上下文范围，适应多种交互场景。
- **可扩展接入**：支持 WebSocket 和 REPL，便于集成与调试。

该路由模型可作为多 Agent 系统的核心枢纽，为后续功能（如负载均衡、动态 Agent 切换）奠定坚实基础。