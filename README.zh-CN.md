# RightOfWay

**人的修改永远优先：由运行时强制执行，而不是靠提示词。**

RightOfWay 是一个协议和它的参考运行时，用于人和 AI Agent 同时修改同一批东西：一个 Blender 场景、一个 Unity 场景、一份文档。运行时站在 Agent 和应用之间。Agent 的工作和人刚做的修改撞在一起时，保留人的修改，撤回 Agent 冲突的那部分，并准确地告诉 Agent 发生了什么。

它不要求应用做任何修改，只用应用已经有的东西：现成的 MCP 服务器（Blender MCP、Unity MCP），或者普通文件。

> **现状：研究预览。** 规则、运行时、Blender 和 Unity 接入都已经能用并有测试，但实验里的 Agent 还是写好的脚本，不是真的大模型。能让你把 RightOfWay 挡在 Claude 或其他 Agent 前面的 MCP 代理是下一步。规范目前是中文，英文版在计划中。
>
> English: [README.md](README.md)

## 要解决的问题

直接改你文件或场景的 Agent，依据的是它一段时间前看到的样子。你在这期间改了什么，它的下一步仍然按旧样子来，悄悄把东西改回它记得的样子。你挪了一片叶子，Agent"整理"整棵树时又把它挪回去；你删了一块石头，Agent 发现少了一块，又建了回来。

现有工具的做法：在提示词里请求模型别这么做、让你逐条审核每个修改，或者两边对称合并、后写的赢。没有一种能**保证**你的修改留下来。Cursor 的工作人员在回复这类问题时就写道：["at the model level it's still a guideline, not a hard rule"](https://forum.cursor.com/t/158451)（在模型层面这只是指导，不是硬规则）。

## RightOfWay 做什么

- **你的修改优先。** 你改过的东西，Agent 不能改。它的脚本改到了，运行时就恢复成你的版本。
- **按属性，而不是按整个对象。** 冲突按对象的"面"判断：位置、缩放、材质、网格、修改器……你挪了一片叶子，Agent 同时把所有叶子改成秋天的颜色，两边都生效。面是从应用自己的数据描述里自动读出来的（Blender 的 RNA、Unity 的序列化），不需要为每个应用手工定义。
- **你正在改的东西，Agent 不碰。** 你一开始改某个对象，它就被占用，依赖它的 Agent 步骤要等。也可以设成"选中即占用"。
- **按旧印象做的修改不生效。** Agent 基于过时观察做的修改会作废，不会覆盖更新的内容。
- **告诉 Agent 它错过了什么，带具体的值。** 它每次读取或执行时都会收到类似这样的说明：
  ```
  你上次看场景之后，发生了这些变化：
  - 人 删除了 Rock_2
  - 人 修改了 Leaf_3：Location (-0.647, 0.47, 1.4) → (-0.647, 0.47, 2.2)
  部分生效：Leaf_3（Material Slots 已生效；Location 人改过，保留人的
  （现在是 (-0.647, 0.47, 2.2)，你改成的 (-0.647, 0.47, 1.6) 没有采用））
  ```
- **可以同时有好几个 Agent。** 人优先于所有 Agent；Agent 之间先提交的生效，后到的那个冲突的部分作废，并被告知现在的值，同时得到一小段预留时间重试，避免两个 Agent 来回互相作废。
- **也支持 computer use。** 靠点鼠标操作的 Agent 需要自己的屏幕，所以它在自己的应用窗口里改自己的一份。运行时通过两边现成的 MCP 服务器实时同步两份内容：不用存盘，也不用重新打开文件。完全没有接口的应用，退回到存盘后合并文件。

## 怎么做到的

```
 Agent ──► RightOfWay 运行时 ──► 现成的 MCP 服务器 ──► 应用（Blender、Unity……）
                 ▲                                          │
                 └────────────── 观察人的修改 ◄─────────────┘
```

Agent 的每个操作都经过运行时。运行时在应用里一次性完成：给受保护的部分拍快照，执行 Agent 的代码，比较前后，只把 Agent 改到的受保护的面恢复回去。人的修改靠给每个对象的每个面算指纹、隔一会儿比较一次来发现。

| | 方式一：同一份 | 方式二：各自一个窗口，实时同步 | 文件兜底 |
|---|---|---|---|
| Agent 怎么操作 | 脚本、MCP 工具 | computer use（鼠标键盘） | computer use，应用没有接口 |
| 什么时候执行规则 | Agent 每次操作 | 每次同步（Agent 每做完一步） | 每次存盘 |
| 人要做什么 | 什么都不用 | 什么都不用 | 存盘，再重新打开文件 |

## 目前的结果

每个实验都是同一个任务：Agent 布置一个小场景（树、叶子、石头）；人删一块石头、挪一片叶子、加一个立方体；然后 Agent 按它之前看到的样子整体调整（秋天的颜色、统一叶子高度、石头排成一圈）。Agent 是写好的脚本，人的操作是模拟的。无界面 Blender（bpy 4.5），数字来自 `python -m experiments.run_all`。

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

Unity 接入在真实的 Unity 6.4 编辑器里通过 Unity 自己的 MCP 检查过：人调的叶子高度保留，Agent 的秋天颜色生效，被选中的对象没被动，实验场景以外没有任何改动（追踪 57 个对象，Agent 每步约 0.07 秒）。

**还没验证：** 真的大模型、真的 computer use Agent、两个 Blender 界面之间的实时同步、Blender 界面里的编辑模式和 Ctrl+Z、大场景。

## 快速开始

需要 Python 3.10 以上。Blender 相关的测试和云端式实验需要 Python 3.11 加 `pip install bpy`；没有的话这部分会自动跳过。

```bash
git clone https://github.com/IndexCH/RightOfWay.git
cd RightOfWay
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows：.venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m pytest                  # 96 个测试；没有 bpy 时相关的会跳过
python examples/demo_game_assets.py         # 带中文解说的运行时演示，不需要 Blender
```

跑全部实验并汇总成表：

```bash
python -m experiments.run_all               # 模拟人的操作
python -m experiments.run_all --human real  # 由你在 Blender / Unity 里做人的那几步
```

每组实验要先准备好对应的应用：Blender 装好 [Blender MCP 插件](https://github.com/ahujasid/blender-mcp) 并启动服务；实时同步还要第二个 Blender，端口 9877；Unity 实验要 Unity 6 加 AI Assistant 包。没准备好的组会跳过并说明原因。详细步骤见 [docs/experiments.md](docs/experiments.md)。

## 目录

```
rightofway/              运行时（Python 包）
├── runtime.py           协议规则 R1–R21
├── blender/             Blender 接入：不修改 Blender，也不修改它的 MCP 插件
│   ├── blender_side.py  在 Blender 里运行的代码（编号、指纹、值、保护/恢复、同步、合并）
│   ├── shared_session.py  方式一：每个 Agent 的视图、告知（R22）、多个 Agent（R23）
│   ├── live_sync.py     方式二：各自一个窗口，实时同步
│   ├── filemerge.py     文件兜底：按对象三方合并
│   └── local_server.py  一个说 Blender MCP 插件协议的 bpy 进程（无界面测试用）
└── unity/               Unity 接入，通过 Unity 自己的 MCP（Unity_RunCommand）
experiments/             实验 A、B、C（Unity）、D（两个 Agent）和 run_all.py
tests/                   96 个测试
spec/spec_v0.4.md        协议规范（草案）
spec/related_work.md     和现有论文、协议、工具的对照
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

1. **MCP 代理**：让 Claude Desktop、Claude Code 或任何 MCP 客户端连 RightOfWay，而不是直接连 Blender MCP。
2. 接真的大模型，并和"只靠提示词"的做法对比。
3. 真的 computer use Agent 在自己的桌面上操作，接到实时同步上。
4. 真人实验：人是不是更愿意插手？按属性保护是否符合人的本意？
5. 英文规范；通过现成的 MCP 服务器接入更多应用。

## 参与

项目还很早期。特别欢迎这类 issue：Agent 在什么工具、什么应用里覆盖了你的修改，当时发生了什么。对规范里规则的疑问也欢迎。已经有 MCP 服务器的其他应用的接入也欢迎，请先开一个 issue 商量做法。

## 许可证

代码采用 [Apache License 2.0](LICENSE)。`spec/` 和 `docs/` 下的规范和文档采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh-hans)。

## 作者

Yuan（[@IndexCH](https://github.com/IndexCH)）
