# 相关工作对照

调研日期：2026-09-28。范围：论文（2023–2026，重点 2025–2026）、协议与平台功能、开源项目与产品。

我们的七个主张，下文用编号指代：

| 编号 | 主张 |
|---|---|
| ① | 运行时**强制**执行规则，AI 的所有修改都经过它（不是靠提示词让模型自觉遵守） |
| ② | 人的修改优先：人改过的对象 AI 不能改；AI 的脚本改到了，就恢复人的版本，并告诉 AI 哪些没被采用 |
| ③ | 人正在改的对象被占用，依赖它的 AI 步骤要等人释放 |
| ④ | 基于旧观察的 AI 写入作废（版本号） |
| ⑤ | 不要求应用适配：用文件和现成的 MCP 服务器观察人的修改，可以挡在现成 MCP 前面做代理 |
| ⑥ | computer use：AI 用自己的一份，存盘时按对象三方合并，基准版本自动推断 |
| ⑦ | 按"面"判断冲突，面从应用自己的反射/序列化数据里自动读出（Blender RNA、Unity SerializedProperty） |

## 1 结论

**没有找到完全重合的工作。** 没有任何论文、协议、项目或产品同时做到 ①②③⑤，也没有任何工作做 ⑦。

各个部件分别有人做过：

- **④ 旧写入作废**：做得最多，但几乎都是 **AI 和 AI 之间**（STORM、S-Bus、PlanFence、CoAgent），或者**整份文件级**（Claude Code、OpenClaw），或者要求应用建在特定框架上（Fluid Framework）。
- **⑥ AI 用自己的一份**：TClone、UFO²、Windows Agent Workspace、各家编程工具的 git worktree 都做了"隔离"，但合并要么没有，要么是文件级或 git 文本合并，没有按对象、自动推断基准的三方合并。
- **② 人优先**：现有做法都是**提示词层面**（CLEO、Cursor、Claude Code 的系统提示），或者**让人逐条审核**（Google Docs、Tiptap 的建议模式），或者**对称合并**（CRDT 把 AI 当成普通协作者，最坏情况下后写的赢）。
- **③ 占用**：现有的锁都是 AI 之间的、建议性的（靠提示词让 AI 遵守），或者是整个浏览器的轮流接管。

**这个方向在 2026 年明显变热**，光 2026 年 9 月就出现了：Yu、Fang、Chen 等人的两篇"Agent-Integrated Software"立场论文、amicode 的 Paper Mode 设计（和我们的方式二非常像，但只针对 LaTeX 文本、还没实现）、Cursor 官方承认"不覆盖人的修改"在模型层面"只是指导，不是硬规则"。窗口期不会太长。

## 2 最接近的工作（按重合程度）

| 名称 | 类型 · 时间 | 做了什么 | 重合 | 缺什么 |
|---|---|---|---|---|
| **TClone**（UCSD / GenseeAI，arXiv 2605.17320） | 论文 · 2026-05 | 把用户正在用的桌面（进程、文件、界面）快速分叉给 computer-use AI，人继续在原环境工作，选中的分支再提交回来 | ⑥ 较强，① 部分 | 冲突"用现有的文件级合并，由策略或人批准"；没有按对象合并、没有自动推断基准、没有人优先 |
| **Agent-Integrated Software**（Yu, Fang, Chen，arXiv 2609.11381）及其安全篇（arXiv 2609.23226） | 立场论文 · 2026-09 | 明确提出问题：用户"在委托的执行进行中修改目标、操作共享对象"；提议交互契约、带版本的依赖、宿主边界上的准入检查 | ①④ 问题层面 | 没有实现和评估；要求应用重新设计（和 ⑤ 相反）；没有人优先的恢复、占用、computer use 合并。**最适合在论文里当问题背景引用，也最可能成为直接竞争者** |
| **Fluid Framework SharedTree + ai-collab**（Microsoft） | 框架 · 2.x，ai-collab 为 alpha | 事务约束 `nodeInDocument` / `noChange`：约束被违反，整个事务作废；AI 在分支上编辑再合并；合并语义按 schema 的节点和字段 | ④ 较强，⑥⑦ 部分 | 应用必须建在 SharedTree 上（和 ⑤ 相反）；没有人优先、恢复、占用 |
| **CLEO**（KAIST 等，arXiv 2603.02050） | 论文 · 2026-03 | Figma 里 AI 和设计师同时工作，比较画布快照发现用户的修改，判断是反馈还是独立工作，调整计划 | ② 精神上最接近，⑦ 部分（属性级比较） | 靠提示词，不强制；没有恢复、锁、版本（已在规范第 2 节引用） |
| **harmoniqs/amicode Paper Mode**（GitHub issue #1620 等） | 设计 · 2026-09-28 开 | LaTeX 编辑器：草稿有递增版本号，AI 每次读最新版；以 AI 读到的版本为基准三方合并；重叠的改动成为冲突块由人解决；"人的文字不会被悄悄删掉" | ②④⑥ 部分 | 只针对文本；要人手动解决冲突；和 AI 在同一个编辑器进程里；0/10 子任务完成 |
| **STORM**（arXiv 2605.20563）、**S-Bus**（2605.17076）、**PlanFence**（2609.03340）、**CoAgent**（2606.15376） | 论文 · 2026 | 多个 AI 共用工作区，按版本在写入时检测冲突，被拒的 AI 拿到差异重新规划 | ④ 较强 | **只有 AI，没有人**；文件或分片粒度；CoAgent 要求工具登记读写范围和逆操作（和 ⑤ 相反） |
| **Claude Code** 的编辑工具 | 产品 · 现行 | v2.1.208 之前：文件读过之后在磁盘上变了就拒绝编辑；之后放宽为"要替换的内容仍然精确唯一匹配就允许" | ④ 部分（整份文件，而且在放宽） | 只管它自己的编辑工具（Bash 绕得过）；只有文本文件；检查点不记录人的修改 |
| **UFO²**（Microsoft，arXiv 2504.14603）/ **Windows Agent Workspace**（2025-11 起预览） | 论文 / 系统功能 | AI 有自己的桌面（画中画虚拟桌面 / 独立账户），和人并行工作 | ⑥ 隔离部分 | Agent Workspace 能访问"文档、下载、桌面、图片"等共享文件夹，**文档里没有同一文件被双方修改时怎么处理**——这正是方式二要填的空 |
| **Cursor**（论坛帖 158451） | 产品 · 2026-04 | 用户报告"Agent 把我手动改的改回去了"；官方回复：模型层面"只是指导，不是硬规则"，截至 2026-08 没有修复计划 | ② 仅提示词 | 这是"问题真实存在、而且没有强制机制"的直接证据 |
| **CRDT 把 AI 当协作者**：Liveblocks、Electric/Yjs、Tiptap、tldraw、Agent-Native | 产品 · 2026 | AI 作为普通协作者实时编辑；Agent-Native 文档明写：同一区域同时重写时后写的赢，"可能冲掉人正在进行的编辑" | ⑦ 部分（细粒度合并） | 人和 AI 对称，没有人优先；应用必须建在这些引擎上 |
| **MCP 代理 / 网关**：mcp-compensator、HoldGate、mcp-guard 等 | 开源 · 2026 | 挡在 MCP 服务器前面做审批、策略、撤销 | ①⑤ 部分（只到工具调用层） | 不知道对象状态和人的修改；mcp-compensator 的撤销会"覆盖中间人做的修改" |
| **CHAP**（Brightbeam AI，arXiv 2606.09751） | 协议草案 · 2026-06 | 人机决策的审计协议：批准、覆盖、交接，带哈希链；可作为 MCP 服务器或 A2A agent | ②⑤ 部分（流程和审批层面） | 不处理并发编辑、锁、旧写入 |
| **Unity AI Assistant** 检查点 | 产品 · 现行 | 每次提问前存检查点；恢复时"撤回之后的所有修改，不论是 AI 还是手动做的" | — | 和 ② 正好相反：恢复 AI 的修改会连人的一起撤掉 |
| **Unity Smart Merge（UnityYAMLMerge）** | 工具 · 非 AI | 场景和预制体文件的语义三方合并 | ⑥⑦ 的合并部分 | 离线版本控制工具，和 AI 无关；可以当方式二的对比基线 |

## 2b 人和人的实时协作工具（没有 AI，但机制值得借鉴）

2026-09-28 补充。这些工具都是为"人和人"设计的，双方对等，没有"人优先"，也防不了 AI 按旧印象写脚本覆盖；但它们在真实使用里总结出来的做法，直接影响了 v0.4 的设计。

| 名称 | 用在哪 | 两个人同时改怎么处理 | 现状 | 对我们的启发 |
|---|---|---|---|---|
| **Figma 多人协作** | Figma | 服务器保存每个对象每个属性的最新值；同一个属性后到服务器的赢，不同属性互不影响；本地还没确认的修改优先显示 | 产品内置 | 按属性判断冲突，和我们的"面"是同一个粒度。区别是它对称，我们人优先 |
| **Unreal Multi-User Editing** | Unreal，引擎自带 | 关卡改动立即同步；拖动时独占被拖的物体，别人对它们的修改立即撤回；改资源时临时锁住直到存盘；每人只能撤销自己的操作 | 内置 | "拖动时独占 + 撤回别人的修改"几乎就是占用 + 恢复；"只撤销自己的"对应未决问题 12 |
| **Scene Fusion**（KinematicSoup） | Unity | 谁选中一个对象，它就锁给谁 | 2026 年仍在维护，两人免费 | "选中即占用"选项（R8） |
| **Mixer**（育碧） | Blender | 多人同时编辑同一个场景 | 2021 年底停止维护，只支持 Blender 2.93、3.0 | 说明这类插件维护成本高；我们不依赖任何第三方同步插件 |
| **Multi-User**（slumber） | Blender | 多人通过网络编辑同一个 .blend | 开源，标注"仍在开发中"；没能确认支持的版本和冲突处理 | 同上 |

Photoshop：没有找到能多人同时编辑 PSD 的功能（Adobe 的说明页没能打开，未核对）。

## 3 协议和标准的现状

- **MCP（2026-07-28 版）**：资源只读，只有 `lastModified` 和新增的缓存提示；没有版本号、ETag、前置条件、锁或冲突语义；也没有找到相关的 SEP。**这一版把协议改成了无状态**（去掉会话和初始化握手），跨调用的状态要用"服务器发的句柄作为普通工具参数"传递——我们的 MCP 代理要按这个方式设计；订阅改成了 `subscriptions/listen`，可以用来接收应用的变化通知。
- **AG-UI**：共享状态用快照 + JSON Patch，文档让开发者自己"处理状态冲突"，没有机制。
- **ACP（Zed 的 Agent Client Protocol）**：v1 的 `fs/write_text_file` 没有版本和冲突处理；v2 提案**去掉了整个 `fs/*`**，AI 将直接写磁盘。
- **A2A、A2UI、MCP Apps、OpenAI Apps SDK、WebMCP**：都没有共享对象的并发编辑语义；WebMCP 还要求网站适配（和 ⑤ 相反）。

也就是说，**标准层面这一层是空的**，而且几个趋势（ACP 去掉编辑器中转、Claude Code 放宽旧文件检查、MCP 变无状态）让这一层更需要一个独立的运行时来补。

## 4 对我们的影响

**差异点**（写论文和 README 时要突出的）：

1. 人优先是**运行时强制**的，精确到对象的面，并把"哪些没被采用"告诉 AI——现有的要么靠提示词，要么靠人逐条审核，要么对称合并。
2. **人的编辑触发占用**，阻塞依赖它的 AI 步骤——现有的锁是 AI 之间的、建议性的。
3. **不改应用**：直接挡在现成的 Blender MCP、Unity MCP 前面——AIS、CoAgent、Fluid、CRDT 方案都要求应用改造或重建。
4. **面从应用自己的反射数据自动读出**——没有找到任何先例。
5. computer use 的一份**通过两边现成的接口实时同步、按面合并**（v0.4；没有接口时退回到存盘后按对象三方合并，起点优先读写进文件的编号）——TClone、UFO²、Windows Agent Workspace 都停在隔离，或者用文件级 / git 文本合并。
6. **多个 AI 加一个人**（v0.4）：AI 之间沿用 STORM 式的"先提交的赢、被拒后告知并预留"，但粒度到面、以每个 AI 自己的视图为基准，而且人对所有 AI 优先——STORM、S-Bus 都没有人。

**应该引用的**：AIS 两篇（问题背景）、CLEO 和 Cursor（提示词层面的对照）、STORM / S-Bus（AI 之间的版本检测，和我们的 ④ 类似但没有人）、TClone / UFO² / Windows Agent Workspace（方式二的隔离基础设施）、Fluid（最接近的机制，但要求应用适配）、Unity Smart Merge（方式二合并的非 AI 基线）。

**要注意的**：

- **AIS 的作者团队**已经把问题说得很清楚，下一篇很可能就是实现；TClone 加上按对象合并也会和方式二重合。**越早把实现和真人实验放出来越好**。
- **名字冲突**："Cowork" 和 Anthropic 的产品 Claude Cowork 同名，GitHub 上还有一个无关的 "COWORK Protocol"（kamesh231/cowork-protocol）。**正式发布前应该改名**（规范未决问题 6）。
- STORM / S-Bus 的实验方法（同一个任务，对比有无冲突检测）和评估指标可以借鉴到我们的真人实验里；"Guard Precision"（arXiv 2609.29522）报告只看版本新旧的检查会误拦 92–95% 其实无害的并发，这正好支持我们按面判断的做法。

## 5 核对情况

本调研由三路检索汇总。下面这些我逐一打开原文核对过：TClone、AIS 两篇、STORM、CLEO、CHAP、Claude Code 工具文档（含 v2.1.208）、Fluid 事务约束、Cursor 论坛帖、amicode #1620、MCP 2026-07-28 更新日志、Windows Agent Workspace 文档。其余条目（S-Bus、PlanFence、CoAgent、Guard Precision、各开源项目的星数和日期等）来自检索时的页面摘要，引用前建议再打开核对。未能核对：SIGGRAPH Asia 2025 的 "MCP-Unity: Protocol-Driven Framework for Interactive 3D Authoring"（页面无法访问，需要手动查看，可能和 Unity 这部分相关）。

## 来源

- Figma 多人协作：https://www.figma.com/blog/how-figmas-multiplayer-technology-works/
- Unreal Multi-User Editing：https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-editing-overview-for-unreal-engine
- Scene Fusion：https://www.kinematicsoup.com/scene-fusion
- Mixer：https://github.com/ubisoft/mixer
- Blender Multi-User：https://gitlab.com/slumber/multi-user
- TClone：https://arxiv.org/abs/2605.17320
- Agent-Integrated Software：https://arxiv.org/abs/2609.11381 ；安全篇：https://arxiv.org/abs/2609.23226
- STORM：https://arxiv.org/abs/2605.20563 ；S-Bus：https://arxiv.org/abs/2605.17076 ；PlanFence：https://arxiv.org/abs/2609.03340 ；CoAgent：https://arxiv.org/abs/2606.15376 ；Guard Precision：https://arxiv.org/abs/2609.29522
- CLEO：https://arxiv.org/abs/2603.02050
- UFO²：https://arxiv.org/abs/2504.14603
- CHAP：https://arxiv.org/abs/2606.09751 ，https://github.com/BrightbeamAI/chap
- amicode Paper Mode：https://github.com/harmoniqs/amicode/issues/1620
- Claude Code 工具文档：https://code.claude.com/docs/en/tools-reference
- Fluid Framework 事务：https://fluidframework.com/docs/data-structures/tree/transactions
- Cursor 论坛：https://forum.cursor.com/t/158451
- Windows Agent Workspace：https://learn.microsoft.com/en-us/windows/security/book/operating-system-agentic-security
- MCP 2026-07-28 更新日志：https://modelcontextprotocol.io/specification/2026-07-28/changelog
- ACP：https://agentclientprotocol.com/protocol/file-system
- AG-UI 状态：https://docs.ag-ui.com/concepts/state
- Agent-Native 实时协作：https://www.agent-native.com/docs/real-time-collaboration/
- Unity AI Assistant 检查点：https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.9
- mcp-compensator：https://github.com/mohithhhh/mcp-compensator
- 名字冲突：https://github.com/kamesh231/cowork-protocol
