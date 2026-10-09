# 相关工作对照

调研日期：2026-09-28；2c 节补充于 2026-10-04；2d 节补充于 2026-10-08。范围：论文（2023–2026，重点 2025–2026）、协议与平台功能、开源项目与产品。

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
2026-10-08 复查（2d 节）：仍然没有。新发现的部分匹配（Doop、ATR、Zed 的按作者记账）都缺一块关键的：人优先、不改应用、或者核对应用的实际状态。

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

## 2c AI 现在怎么操作 Blender、Unity（2026-10-04 补充）

起因：v0.5 的讨论里假设"AI 一步只改少数几个属性"。查下来，实际做法是：

**Blender**

- **接口**：社区的 BlenderMCP（ahujasid，约 2.9 万星，36 个工具）；Blender 官方的 Blender Lab MCP Server（v1.0.3，要求 Blender 5.1 以上），2026 年 4 月成为 Claude 的官方连接器，Anthropic 同时加入 Blender 开发基金。两者真正改场景都靠执行任意 Python：BlenderMCP 用 `execute_blender_code`；官方版在 Blender 里执行模型生成的代码，"没有任何保护措施"，建议在虚拟机或不含敏感数据的系统里使用。其余工具用来读场景（`get_scene_info`、`get_object_info`、视口截图）、查 API 文档，或者从素材库下载、用 AI 生成模型（Poly Haven、Sketchfab、Poly Pizza、Hyper3D、混元 3D、Tripo）。
- **习惯**：
  1. **先看**：BlenderMCP 的提示词要求动手前先调用 `get_scene_info`，改之前、改之后各截一张视口图核对。
  2. **一段脚本做一类事**：`execute_blender_code` 的说明要求"一步一步、拆成小块"；NousResearch 的 Blender 技能写"一次调用做一个逻辑步骤：先加物体，再上材质，再做动画"，理由是避免连接超时。工具作者需要反复这样要求，说明模型默认倾向写大段脚本（这是推断）。即使拆开，一个"逻辑步骤"通常也覆盖很多对象，比如给所有叶子上材质。
  3. **清空重建**：同一份技能把"全选、删除"列为常用片段，同时提醒"不要假设场景是空的"。
  4. **多轮**：写脚本 → 截图或渲染 → 再改，常常要好几轮。一篇实测博客里，做一个甜甜圈场景用了 2 小时、4–5 轮。
- **研究系统把程序当作场景**：SceneCraft、VIGA、BlenderGym 每一轮都生成整段程序再重新执行，场景从头重建。LL3M 让一份脚本逐步变长，每次修改后整段重新执行，人可以通过程序里暴露的参数来改。人在 Blender 界面里直接做的修改不在程序里，重新执行就没了。

**Unity**

- **官方 Unity AI Assistant**：
  - 1.x 的 `/run` 生成一段 C# 脚本，先显示摘要和代码，人点"运行"才执行，之后可以在"撤销历史"里撤回。
  - 2.x 分 Ask 和 Agent 两种模式：Agent 调用 Unity 的工具，按权限设置请求人批准。每次发提示词都会自动建一个 git 快照（检查点）；恢复检查点会撤销之后所有的修改，"不管是 Assistant 做的还是你做的"。
  - 外部 AI 通过 Unity MCP 连接，第一次连接要人在项目设置里批准。我们用过的 `Unity_RunCommand` 就是执行一段 C#。
- **社区最常用的 CoplayDev unity-mcp**（约 1.5 万星，v10.0.0，2026-06；对应论文是 SIGGRAPH Asia 2025 技术短文 "MCP-Unity"）：
  - 约 47 个类型化工具，比如 `manage_gameobject` 的 create / modify / delete / duplicate、`manage_components`、`manage_scene`、`script_apply_edits`。没有执行任意 C# 的工具。
  - `batch_execute` 一次最多 25 条命令（可以调到 100），称比逐条调用快 10–100 倍；它"不是事务：后面的命令失败，前面的不会回滚"。
  - 官方技能的建议：先读资源再动手；一个任务通常分 2–3 批；从零搭场景先新建一个场景；改已有对象"用 update，而不是删掉重建"；改脚本后刷新、看控制台、截图核对。
- **IvanMurzak Unity-MCP**：几十到上百个类型化工具，外加 `script-execute`（用 Roslyn 编译执行任意 C#）。
- **命令行工具**：Unity 论坛 2026 年 3–6 月的讨论里，有开发者从 MCP 改用命令行。例如 unityctl 用 `script execute 文件.cs` 执行带 `Main()` 的多行 C#。还有人在 AGENTS.md 里规定"编辑器状态不安全、或有未保存的修改时，不要改场景、预制体和序列化资源"。

**其他应用和总体趋势**

- **Roblox** 官方 Studio MCP 的核心是 `run_code`（执行 Luau）和 `insert_model`，2026 年 4 月起改为 Studio 内置。**Unreal** 社区 MCP（ChiR24，约 800 星）有 23 个类型化工具，外加执行 Python。
- **趋势是写代码，而不是逐个调用工具**：
  - Cloudflare 的 "Code Mode"（2025-09）认为模型见过大量代码，却很少见过工具调用；
  - Anthropic 的 "Code execution with MCP"（2025-11）报告，把一个任务的 token 从 15 万降到了 2 千。

  可以预期 AI 的脚本会越来越长，一次做的事越来越多。
- **用户反馈**：Replit 论坛（2026-03）有用户说，Agent "把场景当成代码文本，而不是结构化的 3D 世界"，加新物体时会改坏附近的物体，还会丢失对已有物体的跟踪。

**对我们的影响**

1. **撞上是常态**：AI 的一步就是一段脚本或一批命令，通常改很多对象，每个对象改好几个属性。人和 AI 在同一块区域工作时，改动重叠是常态，不是例外。"有冲突就整步退回、重新生成"会频繁触发，代价很高；按属性部分生效反而更重要。
2. **重建会让对象换编号**："清空重建""整段程序重新执行"在运行时看来是"删了 N 个、又新建了 N 个"。结果是：人改过的对象被拒绝删除并恢复，AI 重建的同名对象又被加进来，变成两份。需要能把重建的对象认回原来的对象（按名字、层级、类型等）。对于"程序就是场景"的系统，人的修改要作为一层覆盖，在每次重新执行后重新套上。
3. **现有的保护都不处理"同时改"**：现在能用的手段只有存盘或虚拟机（Blender）、执行前预览和批准、撤销历史、整个项目回滚（Unity；回滚会把人的修改一起撤掉），以及"编辑器有未保存修改时不让 AI 动"这条实践约定。
4. **类型化命令可以事先拒绝**：CoplayDev 的批量命令在执行前就知道要改哪个对象的哪个属性，可以逐条事先拒绝，不需要事后恢复。

实验 H 量过了这几条（`design_v0.5.md` 第 12 节）：人改一处，被真实写法的 AI 这一步撞上的概率是 76%–100%（精确修改约 30%）；Blender 上清空重建让人的每一处修改都变成违规、多出重复对象，"认回同一个对象"（按名字和类型）的原型把它降到 0；批量命令的预检查在恢复不了删除的模拟 Unity 接入上也没有违规。2026-10-09 起认回同一个对象默认打开（同一个父对象下从上往下配对、重名后缀由探测学出、运行时核对），并拦下"补回人删掉的对象"（`design_v0.5.md` 第 13 节）。

**BlenderMCP 2.x（2026-10-09 补充，读的是 PyPI 上 mcp-for-blender 2.1.9 的源码）**

- 改名为 mcp-for-blender，`uvx blender-mcp` 仍可用（兼容包装）。连接 Blender 的地址可以用 `BLENDER_HOST`、`BLENDER_PORT` 设置；和插件之间一直开着一个连接，连续发多条命令。
- 插件里新加了"人的操作记录"（`UserEditRecorder`）：AI 的命令执行期间发生的算 AI 的，其余的操作和撤销按人的记下来，用于采集轨迹数据（"AI 之后人又做了几步"）。它只记录，不保护：这是我们看到的第一个在 Blender 里区分人和 AI 操作的现成实现。
- 遥测和轨迹采集现在要用户同意才开（插件偏好里的勾选项，或客户端支持时弹出的确认框），也可以用 `DISABLE_TELEMETRY` 等环境变量关掉；同意后会上传提示词、生成的代码和截图。
- 有"安全模式"（环境变量开启）：执行前检查脚本，只许导入 bpy 等模块，不许 exec、open、读写文件、注册类等。开着安全模式时，RightOfWay 的代理没法经过它的执行代码工具发自己的代码（里面要 exec AI 的脚本、写快照文件），只能用 `--bridge socket` 直接连插件。
- 我们的代理和它联调过：AI 客户端 → RightOfWay 代理 → mcp-for-blender 2.1.9 → 用 bpy 冒充的插件（`design_v0.5.md` 13.5）。

## 2d 别人怎么解决我们的四个问题（2026-10-08 补充）

起因：梳理 v0.5 架构时找到四个主要问题（`design_v0.5.md` 第 12 节、`spec_v0.4.md` 第 15 节第 21–25 条）。这一节看别的系统怎么处理它们。完整报告（113 处引用，按问题逐条列出做法、局限、能借鉴什么、和原则的冲突）：`prior_art_solutions.md`。

**问题一：对任意代码只能执行后撤回，不能事先阻止**

- **业界的通行做法是三层，没有一家对已知不可逆的效果依赖事后恢复**：执行前确定性拦截（Progent、Magentic-UI 的 ActionGuard、Codex 的沙盒和审批），在影子状态里执行、验证后再提交（Cordon、Atomix、GoEx），剩下的才靠补偿或快照回滚（SagaLLM、编程 Agent 的检查点）。
- **Atomix**（2026-02）的对照最贴近我们：检查点回滚和完整事务的任务成功率差不多，但 1,200 封预期邮件实际发出 1,351 封，151 封是重试造成的重复。回滚加重试会复制恢复不了的效果，和 Leaf_3.001 是同一种病。
- **编程 Agent 的检查点**（Claude Code、Cursor、Copilot、Windsurf、Replit、Junie）都是文件级快照，回滚会连用户之后的修改一起撤掉。唯一按作者记账的是 **Zed**：每处修改标 Agent 或 User，但拒绝 AI 的修改时会覆盖重叠范围里人的文字。
- **"过期就拒绝"会死循环**：Claude Code 的"读过之后文件又被改了"检查，让 Agent 卡在 5 次以上的读—改循环（issue #33856）。人和人实时协作里，时间和位置都接近的编辑只占 2.7%–18.7%（13,871 个 Etherpad 文档），我们测到的 76%–100% 高出一个数量级，冲突处理就是常规路径。
- **派生改动、意图冲突没人自动解决**：RIR 把可能失效的信息"标记待核实"，ATR 只重查受影响的条件（条件要人手写）。
- **能借的**：能力探测得出效果分类（不手写）；删除前备份，Unity 的预制体实例按 `GlobalObjectId` 重新实例化；告知里加"失效的事实"和循环检测；"影子执行"作为试验——AI 的脚本先在另一个无界面进程里跑，按面取差异，再经方式二现成的写入路径（写前核对）写进人这边，受保护对象的删除根本不会发生。

**问题二：清空重建让对象换编号**

- **控制不了生成过程的系统，最后都按名字认对象**：OpenUSD 的 prim 路径、Blender 自己的库链接；Omniverse 实时会话里两个客户端新建同名节点时，输的一方删掉自己的、收养对方的。React 的协调规则（位置 + 类型 + 兄弟间唯一的 key）几乎就是我们的"名字 + 类型"。
- **CAD 的拓扑命名**（FreeCAD 1.0）靠创建历史命名，前提是确定性重放，AI 重建不满足；维护者说问题"没有完全解决"，丢了引用就报错，不悄悄猜。
- **孤儿**（认不回的人的修改）：USD 静默忽略，Blender 库覆盖在重新同步时删除，Unity 把"未使用的覆盖"留在场景里、目标回来时重新套上。
- **能借的**：在已认回的父对象下配对、一对一；去后缀的规则靠探测学出（不手写 `.001`）；孤儿跨执行保留、出现匹配就重新绑定（Unity 式），永远不静默丢弃；人删除的对象记成"墓碑"（USD 的停用意见），对应 `design_v0.5.md` 12.5。

**问题三：共用数据被拆成每个对象的面**

- **Unreal Concert、USD、Blender 的 Multi-user 插件、Blender Studio 的 Asset Pipeline 都把材质这类共用数据当独立单位**：在 USD 里改共用材质就是改一个属性；Concert 里材质第一次被改就上锁。
- **Unity 文档写明了同一个分叉**：读 `Renderer.material` 会克隆共用材质并从此使用克隆。分叉都出现在"所有权边界切过共用数据"的地方（Asset Pipeline 里跨所有者共用的物体数据也会被复制）。
- **能借的**：面的键改成"拥有这个属性的数据块 + 属性路径"，物体只保留"哪个槽指向哪个材质"；恢复时写回同一个数据块。"这个值是不是对另一个数据块的引用"在 Blender RNA 和 Unity SerializedProperty 里都读得出来，不用手写（推断，待验证）。

**问题四：在 AI 客户端和应用的 MCP 服务器之间加策略层**

- **2026 年的 MCP 网关**（Docker、ToolHive、IBM ContextForge、Invariant、Cerbos、TrueFoundry、Kong、Pomerium 等）都能在调用前拦截，但参数到资源的映射都是按工具手写的，而且**没有一个检查调用之后应用的实际状态**。
- **标准层面**：SEP-1862（`tools/resolve`：带着具体参数取回这一次调用的注解）、SEP-2848（审批绑定参数摘要，执行时重新验证）、OpenID 的 COAZ-MCP 草案（工具在 schema 里声明哪个参数是资源）。没有资源版本、ETag、条件写入或锁的提案。工具注解默认悲观、来自不可信的服务器，不能当保证。
- **代码模式在扩大**：Cloudflare 的 MCP 门户可以把所有工具换成"执行代码"一个工具，按工具的预检查覆盖面只会越来越小。
- **能借的**：调用前、调用后两个钩子（ContextForge 的 `tool_pre_invoke` / `tool_post_invoke`）；参数映射用 schema 启发给候选、能力探测确认；只对"这次连接恢复不了"的效果映射不出就拒，其余放行、事后核对；被拒的以 `isError` 结构化返回；执行代码的工具和元工具一律走事后核对。

**DCC 里的 AI 助手**：Unreal 5.8 的实验性 MCP 串行执行，要求客户端不要重叠调用，实测者在 AI 工作时没法继续编辑；Unity AI Assistant 2.11 按操作设 Allow / Ask / Deny；Figma 的 MCP 已能写画布，Agent 的修改只能用聊天里的按钮整体撤回；Roblox 的计划要人点 Build。没有一个支持人在 AI 改场景时继续编辑。

**和我们最不一样的地方**：Kubernetes Server-Side Apply 是最完整的多写者字段所有权模型，但默认让自动化赢——控制器按文档"总是强制"，人的 `kubectl edit` 在下一次调和时被收回（Flux、Argo CD 同理）。RightOfWay 把这个默认反了过来。"账本 = 场景"这种核对实际状态的不变量，在调研到的 AI 工具和多人编辑系统里没有对应物。

**不借的**：默认审批、建议模式（违反 P6）；把注解或提示词当保证（P2）；按工具手写参数映射（P4）；过期就整步拒绝（在我们的碰撞率下 AI 会一直提交不了）；对称的"最后写入者赢"（P5）；恢复整份文件的检查点（会连人的修改一起撤掉）。

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
- **名字冲突**（已处理）：原暂名 "Cowork" 和 Anthropic 的产品 Claude Cowork 同名，GitHub 上还有一个无关的 "COWORK Protocol"（kamesh231/cowork-protocol）。项目已改名为 RightOfWay。
- STORM / S-Bus 的实验方法（同一个任务，对比有无冲突检测）和评估指标可以借鉴到我们的真人实验里；"Guard Precision"（arXiv 2609.29522）报告只看版本新旧的检查会误拦 92–95% 其实无害的并发，这正好支持我们按面判断的做法。

## 5 核对情况

本调研由三路检索汇总。下面这些我逐一打开原文核对过：TClone、AIS 两篇、STORM、CLEO、CHAP、Claude Code 工具文档（含 v2.1.208）、Fluid 事务约束、Cursor 论坛帖、amicode #1620、MCP 2026-07-28 更新日志、Windows Agent Workspace 文档。其余条目（S-Bus、PlanFence、CoAgent、Guard Precision、各开源项目的星数和日期等）来自检索时的页面摘要，引用前建议再打开核对。SIGGRAPH Asia 2025 的 "MCP-Unity: Protocol-Driven Framework for Interactive 3D Authoring" 已确认是 CoplayDev unity-mcp 的论文（DOI 10.1145/3757376.3771417），正文没有打开。

2c 节（2026-10-04）逐一打开核对过：BlenderMCP 的 README 和 server.py、Blender Lab MCP Server 页面、Claude 的 Blender 连接器页面、Unity AI Assistant 文档（/run、模式、检查点）、CoplayDev 的技能和工具参考、IvanMurzak Unity-MCP、Roblox Studio MCP、ChiR24 Unreal MCP、Code Mode、Code execution with MCP、SceneCraft、VIGA、LL3M、BlenderGym、EZBlender、Replit 论坛帖、Unity 论坛帖、unityctl。两篇 MindStudio 博客和 mcp.directory 的指南内容较空、可能是生成的，只作参考。

2d 节（2026-10-08）由五路检索汇总，几乎全部来自打开过的一手页面（官方文档、源代码、论文）；完整报告 `prior_art_solutions.md` 里标了"二手"的（AgentRewind、RIR、DeltaBox 的 alphaXiv 自动摘要，Hackster、StraySpark、Bitovi、dev.to、PyShine 等报道）引用前要再核对。Kubernetes 的冲突格式核对了 apimachinery 源代码，Zed 的按作者记账核对了 `action_log.rs`。

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
- BlenderMCP：https://github.com/ahujasid/blender-mcp （工具和提示词：src/blender_mcp/server.py）
- Blender Lab MCP Server：https://www.blender.org/lab/mcp-server/ ；Claude 连接器：https://claude.com/connectors/blender ；Anthropic 创意连接器报道：https://rits.shanghai.nyu.edu/ai/anthropic-plugs-claude-into-adobe-blender-and-ableton-with-nine-new-connectors/
- NousResearch Blender 技能：https://claudeskills.info/skills/nousresearch/hermes-agent/blender-mcp/
- SceneCraft：https://arxiv.org/abs/2403.01248 ；VIGA：https://arxiv.org/abs/2601.11109 ；LL3M：https://arxiv.org/abs/2508.08228 ；BlenderGym：https://arxiv.org/abs/2504.01786 ；EZBlender：https://arxiv.org/abs/2601.07143
- MindStudio 实测：https://www.mindstudio.ai/blog/claude-blender-mcp-60-percent-tokens-donut-test-results
- Unity AI Assistant：/run https://docs.unity3d.com/Packages/com.unity.ai.assistant@1.0/manual/run-overview.html ；模式 https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/assistant-modes.html ；检查点 https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/checkpoints.html
- CoplayDev unity-mcp：https://github.com/CoplayDev/unity-mcp ；技能与工作流：https://glama.ai/mcp/servers/@CoplayDev/unity-mcp （unity-mcp-skill/SKILL.md、references/workflows.md、references/tools-reference.md）
- IvanMurzak Unity-MCP：https://github.com/IvanMurzak/Unity-MCP
- unityctl：https://www.nuget.org/packages/UnityCtl.Cli ；Unity 论坛讨论：https://discussions.unity.com/t/cli-mcp-ai-ide-chatgpt-codex-cursor-antigravity-claude-code-windsurf-next-steps-for-unity-workflow-automation/1705679
- Roblox Studio MCP：https://github.com/Roblox/studio-rust-mcp-server ；Unreal MCP（ChiR24）：https://github.com/chir24/unreal_mcp
- Code Mode：https://blog.cloudflare.com/code-mode/ ；Code execution with MCP：https://www.anthropic.com/engineering/code-execution-with-mcp
- Replit 论坛：https://replit.discourse.group/t/scene-aware-editing-scene-manifest-support-and-persistent-world-memory-for-replit-agent-3d-game-development/10072
- 2d 节（完整列表见 `prior_art_solutions.md`）：
  - Atomix：https://arxiv.org/html/2602.14849v1 ；Cordon：https://arxiv.org/html/2606.17573v1 ；Progent：https://arxiv.org/html/2504.11703v2 ；Magentic-UI：https://arxiv.org/html/2507.22358v1 ；GoEx：https://arxiv.org/html/2404.06921v1 ；SagaLLM：https://arxiv.org/html/2503.11951v3 ；ATR：https://arxiv.org/abs/2609.08015
  - Zed 的按作者记账：https://github.com/zed-industries/zed/blob/main/crates/action_log/src/action_log.rs ；Claude Code 读—改循环：https://github.com/anthropics/claude-code/issues/33856 ；Etherpad 冲突率（CSCW 2018）：https://upsilon.cc/zack/research/publications/cscw-2018-rtce.pdf
  - Kubernetes Server-Side Apply：https://kubernetes.io/docs/reference/using-api/server-side-apply/ ；Flux：https://fluxcd.io/flux/components/kustomize/kustomizations/ ；Argo CD：https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/
  - OpenUSD 命名空间编辑：https://openusd.org/release/user_guides/namespace_editing.html ；Omniverse 实时层：https://docs.omniverse.nvidia.com/kit/docs/usd_resolver/latest/docs/live-layers-details.html ；FreeCAD 拓扑命名：https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm ；React：https://react.dev/learn/preserving-and-resetting-state ；Sketch-n-Sketch：https://ar5iv.labs.arxiv.org/html/1507.02988 ；Unity 未使用的覆盖：https://docs.unity3d.com/Manual/UnusedOverrides.html
  - Unreal 多人编辑：https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-editing-overview-for-unreal-engine ；Unreal 多人属性复制：https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-replication-in-unreal-engine ；Blender Multi-user：https://slumber.gitlab.io/multi-user/getting_started/how-to-manage.html ；Asset Pipeline：https://studio.blender.org/tools/addons/asset_pipeline ；Unity `Renderer.material`：https://docs.unity3d.com/ScriptReference/Renderer-material.html ；Unity `GlobalObjectId`：https://docs.unity3d.com/ScriptReference/GlobalObjectId.html
  - MCP 网关：Docker https://docs.docker.com/ai/sandboxes/governance/reference/mcp-policy/ ；ContextForge https://ibm.github.io/mcp-context-forge/latest/architecture/plugins/ ；Cerbos https://www.cerbos.dev/blog/authorizing-mcp-tool-calls-at-the-gateway-or-inside-the-proxy.md ；SEP-1862 https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1862 ；SEP-2848 https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2848 ；COAZ-MCP https://openid.net/getting-cozy-with-coaz-securing-apis-and-ai-agents-with-standardized-authorization/ ；Cloudflare MCP 门户 https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/index.md
  - Unreal MCP：https://dev.epicgames.com/documentation/en-us/unreal-engine/unreal-mcp-in-unreal-editor ；Figma Agent：https://forum.figma.com/ask-the-community-7/does-figma-ai-agent-support-undo-55401 ；Doop（二手报道）：https://pyshine.com/Doop-A-Multiplayer-Design-Canvas-Where-Agents-Design-Too/
