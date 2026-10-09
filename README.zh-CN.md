# RightOfWay

**人的修改永远优先：由运行时强制执行，而不是靠提示词。**

RightOfWay 是一个协议和它的参考运行时，用于人和 AI Agent 同时修改同一批东西：一个 Blender 场景、一个 Unity 场景、一份文档。运行时站在 Agent 和应用之间。Agent 的工作和人刚做的修改撞在一起时，保留人的修改，撤回 Agent 冲突的那部分，并准确地告诉 Agent 发生了什么。

它不要求应用做任何修改，只用应用已经有的东西：现成的 MCP 服务器（Blender MCP、Unity MCP），或者普通文件。

> **现状：研究预览。** 规则、运行时、Blender 和 Unity 接入都已经能用并有测试；MCP 代理（把 RightOfWay 挡在 Claude Desktop 等 MCP 客户端前面）也能用了，并和真的 BlenderMCP 服务器联调过。但实验里的 Agent 还是写好的脚本，还没接真的大模型。规范目前是中文，英文版在计划中。
>
> English: [README.md](README.md)

## 要解决的问题

直接改你文件或场景的 Agent，依据的是它一段时间前看到的样子。你在这期间改了什么，它的下一步仍然按旧样子来，悄悄把东西改回它记得的样子。你挪了一片叶子，Agent"整理"整棵树时又把它挪回去；你删了一块石头，Agent 发现少了一块，又建了回来。

现有工具的做法：在提示词里请求模型别这么做、让你逐条审核每个修改，或者两边对称合并、后写的赢。没有一种能**保证**你的修改留下来。Cursor 的工作人员在回复这类问题时就写道：["at the model level it's still a guideline, not a hard rule"](https://forum.cursor.com/t/158451)（在模型层面这只是指导，不是硬规则）。

## RightOfWay 做什么

- **你的修改优先。** 你改过的东西，Agent 不能改。它的脚本改到了，运行时就恢复成你的版本。应用撤不回的改动，运行时必须如实说明、暂停 Agent 并提醒你，而不是假装没事（见[已知问题](#已知问题)）。
- **按属性，而不是按整个对象。** 冲突按对象的"面"判断：位置、缩放、材质、网格、修改器……你挪了一片叶子，Agent 同时把所有叶子改成秋天的颜色，两边都生效。面是从应用自己的数据描述里自动读出来的（Blender 的 RNA、Unity 的序列化），不需要为每个应用手工定义。几个对象共用的材质、网格自己是一个单元：你改一次共用材质，只记在材质上，恢复时也写回同一个材质。
- **你正在改的东西，Agent 不碰。** 你一开始改某个对象，它就被占用，依赖它的 Agent 步骤要等。也可以设成"选中即占用"。
- **按旧印象做的修改不生效。** Agent 基于过时观察做的修改会作废，不会覆盖更新的内容。
- **删掉又新建的，还是同一个对象。** Agent 常把场景清空再重建。运行时按名字、类型、父对象把新建的认回成原来的对象（应用自动加的重名后缀由连接时的自测学出；有歧义就不认），你改过的部分照样保留，不会多出一份。
- **你删掉的，Agent 不会悄悄补回来。** 它还没看到你的删除就新建了同一个对象，运行时把新建的删掉并告诉它。你改过的对象的父对象也不许删。
- **告诉 Agent 它错过了什么，带具体的值。** 它每次读取或执行时都会收到类似这样的说明：
  ```
  你上次看场景之后，发生了这些变化：
  - 人 删除了 Rock_2
  - 人 修改了 Leaf_3：Location (-0.647, 0.47, 1.4) → (-0.647, 0.47, 2.2)
  部分生效：Leaf_3（Material Slots 已生效；Location 人改过，保留人的
  （现在是 (-0.647, 0.47, 2.2)，你改成的 (-0.647, 0.47, 1.6) 没有采用））
  ```
- **没生效的部分，作为新的目标告诉它。** 说明里写明"以现在的值为准"，请它写进之后的计划、重新核对按旧值算出的修改。它反复去改改不动的东西时，运行时会提醒你。
- **可以同时有好几个 Agent。** 人优先于所有 Agent；Agent 之间先提交的生效，后到的那个冲突的部分作废，并被告知现在的值，同时得到一小段预留时间重试，避免两个 Agent 来回互相作废。
- **挡在你的 AI 客户端前面。** MCP 代理：Claude Desktop 等 MCP 客户端连 RightOfWay，RightOfWay 再连应用现成的 MCP 服务器（例如 BlenderMCP）。执行代码的工具、应用自己的类型化工具、代理提供的小工具，都走同一套规则；结果里没生效、被拒的部分标为 `isError`，并附结构化的说明。
- **也支持 computer use。** 靠点鼠标操作的 Agent 需要自己的屏幕，所以它在自己的应用窗口里改自己的一份。运行时通过两边现成的 MCP 服务器实时同步两份内容：不用存盘，也不用重新打开文件。完全没有接口的应用，退回到存盘后合并文件。

## 原则

按优先顺序排列，冲突时排在前面的优先。

1. **如实。** 应用里的实际状态、运行时的记录、告诉你和 Agent 的话，任何时候都一致。保证不了的事如实报告，绝不报成功。
2. **由运行时强制，不靠提示词。** 安全不依赖 Agent 配合；提示词只用来提高效率。
3. **不要求应用做任何改动。** 只用应用已有的东西：MCP 服务器、脚本接口、文件。在你文件里留下的标记（对象编号）必须看不见、不影响使用、能一键清除。
4. **不为单个应用手写规则。** 对象、属性、冲突，以及接入代码能保证什么，都从应用自己的数据描述里读出来，或者用对所有应用都一样的测试测出来。
5. **人的修改优先**，在以上约束之内。做不到的情况，由第 1 条兜底：如实报告违规，提醒你，暂停 Agent。
6. 你不需要额外操作：不用上锁，不用逐条批准。
7. 按属性，而不是按整个对象。
8. 告诉 Agent 发生了什么，带具体的值。

贯穿所有原则的一点：规则只在一个地方决定，就是运行时。完整的原则和每条排除了什么，见[规范](spec/spec_v0.4.md)第 0 节。

## 怎么做到的

```
 Agent ──► RightOfWay（MCP 代理 + 运行时）──► 现成的 MCP 服务器 ──► 应用（Blender、Unity……）
                 ▲                                          │
                 └────────────── 观察人的修改 ◄─────────────┘
```

Agent 的每个操作都经过运行时。运行时在应用里一次性完成：给受保护的部分拍快照，执行 Agent 的代码，比较前后，只把 Agent 改到的受保护的面恢复回去。人的修改靠给每个对象的每个面算指纹、隔一会儿比较一次来发现。接入代码能做到什么（按面恢复、恢复删除、重名后缀……）不手写，而是在连接时用一个临时场景测出来；恢复不了的调用，在执行前就拒绝。

| | 方式一：同一份 | 方式二：各自一个窗口，实时同步 | 文件兜底 |
|---|---|---|---|
| Agent 怎么操作 | 脚本、MCP 工具 | computer use（鼠标键盘） | computer use，应用没有接口 |
| 什么时候执行规则 | Agent 每次操作 | 每次同步（Agent 每做完一步） | 每次存盘 |
| 人要做什么 | 什么都不用 | 什么都不用 | 存盘，再重新打开文件 |

## 目前的结果

实验 A–D 是同一个任务：Agent 布置一个小场景（树、叶子、石头）；人删一块石头、挪一片叶子、加一个立方体；然后 Agent 按它之前看到的样子整体调整（秋天的颜色、统一叶子高度、石头排成一圈）。Agent 是写好的脚本，人的操作是模拟的。无界面 Blender（bpy 4.5），数字来自 `python -m experiments.run_all`。每个有保护的实验在每一步之后还检查三条不变量：场景和运行时的记录一致，该保留的确实保留了，告诉 Agent 的是真的。实验 E 检查 Agent 删掉你改过的对象时会怎样，包括接入代码恢复不了删除的情况（见已知问题）。

| 实验 | 做法 | 人的修改被覆盖 | Agent 的修改丢失 | 被删的对象被重建 |
|---|---|---|---|---|
| A 同一份 | 有 RightOfWay | 0 | 0 | 0 |
| A 同一份 | 没有保护（直接用 Blender MCP） | 2 | 0 | 1 |
| D 两个 Agent + 人 | 有 RightOfWay | 0 | 0 | 0 |
| D 两个 Agent + 人 | 没有保护 | 1 | 1 | 1 |
| B 实时同步 | 有 RightOfWay | 0 | 0 | 0 |
| B 实时同步 | 谁后同步谁生效 | 1 | 0 | 0 |
| B 文件兜底 | 有 RightOfWay | 0 | 0 | 0 |
| B 文件兜底 | 谁最后存盘谁生效 | 3 / 0 | 0 / 11 | — |
| E Agent 删掉你改过的对象 | 有 RightOfWay，Blender（按面） | 0 | 0 | 0 |
| E Agent 删掉你改过的对象 | 有 RightOfWay，模拟 Unity 接入（恢复不了删除） | 1，已如实报告为违规 | 0 | 0 |
| P 经过 MCP 代理 | 有 RightOfWay，假的应用 MCP 服务器 | 0 | 0 | 0（补回被拦下） |

**不变量：** A、B、D、E、P 所有有保护的运行三条都成立。E 里模拟 Unity 接入时叶子还是丢了，因为这个接入恢复不了删除；运行时把它记为违规，如实告诉 Agent 和你，并暂停 Agent。文件兜底还没接上不变量检查。

Unity 接入在真实的 Unity 6.4 编辑器里通过 Unity 自己的 MCP 检查过：人调的叶子高度保留，Agent 的秋天颜色生效，被选中的对象没被动，实验场景以外没有任何改动（追踪 57 个对象，Agent 每步约 0.07 秒）。

### 真实 AI 的写法（实验 H）

实验 A–E 里的 Agent 只精确地改几处。真实的 Agent 大多不这样：一次执行一大段脚本，把所有东西统一设置一遍（BlenderMCP、Blender 官方的 MCP）；每一轮重新生成整个场景程序（SceneCraft、LL3M）；或者一批发最多 25 条类型化命令（Unity MCP 的 `batch_execute`）。实验 H 用这几种写法重跑 A、D、E；另外让人在 Agent 这一步之前只改一处（每个对象的移动、旋转、缩放、删除，再加上改共用材质的颜色），逐一试，统计这一步撞上人的修改的概率。

| Agent 的写法 | 你改的一处被 Agent 这一步撞上（Blender） | 你的修改 |
|---|---|---|
| 精确修改 | 31% | 保住 |
| 整体调整 | 83% | 保住 |
| 清空重建 | 100% | 保住，没有重复对象：删掉又新建的认成同一个对象（默认打开）。关掉这一条时每次都是违规：恢复出来的对象和 Agent 新建的同名对象并存（`Leaf_3.001`），Agent 被暂停 |
| 类型化批量命令（模拟 Unity） | 71% | 保住：冲突的命令在执行前就被拒了，应用恢复不了删除也不会丢 |

你删掉一个对象，Agent 这一步按旧印象把它补回来的：精确修改、整体调整 11 种里 3 种，清空重建 11 种全部，现在全部被拦下。三条不变量在每一次运行里都成立（42 次场景运行、353 次逐一试）。两个结论：撞上是常态，所以"一撞上就整步退回"会让 Agent 在你工作时一直提交不了；运行时让其余部分照常生效，并告诉 Agent 具体哪些没改成、现在的值是多少。另外，清空重建需要运行时把"删掉又新建"的对象认回成同一个对象。详见[设计第 12、13 节](spec/design_v0.5.md)。

### 已知问题

- **Unity：Agent 删掉的对象还恢复不了。** 会如实报告：运行时照实记下删除，标为违规，提醒你，并暂停这个 Agent，直到你让它继续。下一步是给 Unity 加隐藏备份恢复。Unity 一侧也还没有"认回同一个对象"、包住类型化工具调用、在界面里提醒你；这些在 Blender 和模拟应用里已经做了，Unity 的自测如实报告它们没有。
- **MCP 代理转发应用自己的类型化工具时不是原子的。** 调用前后各核对一次，这期间你在应用里的改动会被算成 Agent 的。执行代码的工具和代理自己的小工具（`rightofway_set_property` 等）是原子的。
- **派生改动、意图冲突只告知，不自动处理。** 例如叶子被恢复到你放的高度，Agent 放在叶子上方的鸟悬在半空：运行时会告诉 Agent 以叶子现在的高度为准、请它重新核对，但不会自己挪鸟。

2026-10-09 已修复：清空重建破坏对象的身份（认回同一个对象，默认打开）；Agent 把你删掉的对象补回来（拦下）；你改过的对象的父对象被删、子对象跟着断开（祖先不许删）；共用材质被拆成每个对象的一份副本（数据块单独成单元）。v0.5 第 3 步已修复：删除你碰过的对象按整个对象判断，Agent 不再被误告知"部分生效"。

**还没验证：** 真的大模型、真的 computer use Agent、两个 Blender 界面之间的实时同步、Blender 界面里的编辑模式和 Ctrl+Z、大场景。

## 快速开始

需要 Python 3.10 以上。Blender 相关的测试和云端式实验需要 Python 3.11 加 `pip install bpy`；没有的话这部分会自动跳过。

```bash
git clone https://github.com/IndexCH/RightOfWay.git
cd RightOfWay
python -m venv .venv
.venv/bin/pip install -e ".[dev,proxy]"    # Windows：.venv\Scripts\pip install -e ".[dev,proxy]"
.venv/bin/python -m pytest                  # 211 个测试；没有 bpy、mcp 时相关的会跳过
python examples/demo_game_assets.py         # 带中文解说的运行时演示，不需要 Blender
```

跑全部实验并汇总成表：

```bash
python -m experiments.run_all               # 模拟人的操作
python -m experiments.run_all --human real  # 由你在 Blender / Unity 里做人的那几步
```

### 接到 Claude Desktop（MCP 代理）

装好 `.[proxy]` 之后，在 Claude Desktop 的配置（`claude_desktop_config.json`）里，把原来的 blender 服务器换成：

```json
{"mcpServers": {"blender": {"command": "C:/路径/RightOfWay/.venv/Scripts/python.exe",
                            "args": ["-m", "rightofway.proxy", "--", "uvx", "blender-mcp"]}}}
```

"--" 后面是原来启动 Blender MCP 服务器的命令，原样照搬；Blender 这边照旧装 BlenderMCP 插件、点 Start MCP Server。代理的日志写在标准错误里，违规等提醒也会在 Blender 界面里弹出。不想经过上游的执行代码工具时，加 `--bridge socket` 直接连插件。不开 Claude 也能看效果：`python -m experiments.exp_p_proxy`（假的上游），或者 `--upstream "uvx blender-mcp"`（开着的 Blender）。

每组实验要先准备好对应的应用：Blender 装好 [Blender MCP 插件](https://github.com/ahujasid/blender-mcp) 并启动服务；实时同步还要第二个 Blender，端口 9877；Unity 实验要 Unity 6 加 AI Assistant 包。没准备好的组会跳过并说明原因。详细步骤见 [docs/experiments.md](docs/experiments.md)。

## 目录

```
rightofway/              运行时（Python 包）
├── runtime.py           协议规则 R1–R21、R23；每次 Agent 执行前开许可单，执行后核对、记账
├── identity.py          认回同一个对象的规则（运行时和接入代码用同一份）
├── invariants.py        检查场景、运行时的记录、告诉 Agent 的话三者是否一致
├── fakeapp.py           内存里的假应用，说同样的协议（代码或类型化命令），不需要 Blender 或 Unity 就能测试
├── proxy/               MCP 代理：python -m rightofway.proxy -- <应用 MCP 服务器的启动命令>
├── blender/             Blender 接入：不修改 Blender，也不修改它的 MCP 插件
│   ├── blender_side.py  在 Blender 里运行的代码（编号、指纹、值、保护/恢复、同步、合并）
│   ├── shared_session.py  方式一：每个 Agent 的视图、告知（R22）、多个 Agent（R23）
│   ├── commands.py      类型化命令编成 Blender 脚本（代理的小工具用）
│   ├── live_sync.py     方式二：各自一个窗口，实时同步
│   ├── filemerge.py     文件兜底：按对象三方合并
│   └── local_server.py  一个说 Blender MCP 插件协议的 bpy 进程（无界面测试用）
└── unity/               Unity 接入，通过 Unity 自己的 MCP（Unity_RunCommand）
experiments/             实验 A、B、C（Unity）、D（两个 Agent）、E（Agent 删掉你改过的对象）、
                         H（真实 AI 的写法）、P（经过 MCP 代理）、S（影子执行试验）和 run_all.py
tests/                   211 个测试，包括不变量检查
spec/spec_v0.4.md        协议规范（草案）；第 0 节是设计原则
spec/design_v0.5.md      v0.5 架构重新设计（第 1–3 步已实现；第 13 节：按调研加固）
spec/related_work.md     和现有论文、协议、工具的对照
spec/prior_art_solutions.md  别的系统怎么解决同样的四个问题（2026-10-08 调研）
docs/overview.md         用最少的术语从头讲一遍协议
docs/experiments.md      实验的详细步骤
```

## 和其他工作的关系

我们没有找到同时做到这几点的工作：由运行时强制执行的人优先、由人自己的编辑触发的占用、从应用自己读出的按属性粒度、不改应用。这些部件分别存在于：

- Agent 之间拒绝过时写入：STORM、S-Bus。
- 提示词层面的"别覆盖用户"：CLEO；编程 Agent 里的系统提示。
- 给 computer use Agent 独立桌面，再按文件合并回来：UFO²、TClone、Windows Agent Workspace。
- 人和人之间按属性"后写的赢"：Figma 多人协作。

详细对照和来源见 [spec/related_work.md](spec/related_work.md)。

## 路线图

0. **v0.5 架构**（[设计](spec/design_v0.5.md)）：每次操作都走同一条流程（许可 → 执行 → 核对 → 提交 → 告知），并自动检查场景、记录、说明三者一致。第 1–3 步已完成；按调研加固（认回同一个对象、拦下补回、祖先不许删、共用数据块、告知失效的事实、循环检测、能力探测）也已完成（第 13 节）。接下来是 Unity 一侧：隐藏备份恢复、认回同一个对象。
1. **MCP 代理**：已实现（`python -m rightofway.proxy`）。下一步接真的大模型（Claude Desktop），并和"只靠提示词"的做法对比。
2. 影子执行（试验已做：结局一样，每步慢 4–5 倍）留给恢复不了改动的应用。
3. 真的 computer use Agent 在自己的桌面上操作，接到实时同步上。
4. 真人实验：人是不是更愿意插手？按属性保护是否符合人的本意？
5. 英文规范；通过现成的 MCP 服务器接入更多应用。

## 参与

项目还很早期。特别欢迎这类 issue：Agent 在什么工具、什么应用里覆盖了你的修改，当时发生了什么。对规范里规则的疑问也欢迎。已经有 MCP 服务器的其他应用的接入也欢迎，请先开一个 issue 商量做法。

## 许可证

代码采用 [Apache License 2.0](LICENSE)。`spec/` 和 `docs/` 下的规范和文档采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh-hans)。

## 作者

Yuan（[@IndexCH](https://github.com/IndexCH)）
