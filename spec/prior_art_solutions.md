# 别人拆好了零件，人优先仍是空白

> 调研日期：2026-10-08。问题：别人怎么解决 RightOfWay 的四个主要问题（`design_v0.5.md` 第 12 节、`spec_v0.4.md` 第 15 节第 21–25 条）。五路检索后汇总，摘要见 `related_work.md` 2d 节。标了"二手"的来源引用前请再核对。

**没有找到和 RightOfWay 目标完全相同的项目。** 截至 2026-10-08，没有任何产品、开源项目或论文同时做到这几件事：不改应用，让人和 AI 同时编辑同一份实时场景，由运行时按属性强制人的修改优先，并核对应用的实际状态。不过四个问题各自都有成熟的近邻做法，结论也高度一致。

- **恢复不了的改动：** Atomix、Cordon、Magentic-UI 这类系统都在执行前分类，然后拦截或暂存，没有一个依赖事后恢复。RightOfWay 对任意代码"执行后恢复"，正处在它们刻意回避的区间。在不改应用的约束下，能做的是缩小"恢复不了"的范围（类型化预检查、删除前备份、可选的影子执行），而不是放弃事后核对。
- **清空重建：** 控制不了生成过程的系统，最后都按名字认身份，例如 OpenUSD 的路径、Blender 自己的库链接、Omniverse 的同名收养。所以"按名字和类型认回"的方向是对的，但要加上父对象限定、一对一、孤儿保留，以及人删除对象的墓碑。
- **共用材质：** Unreal Concert、USD、Blender Multi-user 都把共用数据块当成独立单位。Unity 文档写明 `Renderer.material` 会自动克隆共用材质，这和 RightOfWay 遇到的分叉是同一现象。修法是按"拥有这个属性的数据块"记面，并原地恢复。
- **MCP 代理：** 十几个网关都能在调用前拦截，但参数到资源的映射全靠人手写，也没有一个在调用后检查应用状态。这正是 RightOfWay 可以用能力探测和事后核对补上的空白。

建议的顺序是：先修共用数据块和认回，因为它们纠正的是实验 H 已经测到的错误；再补结构化告知、建 MCP 代理；影子执行只作试验。最不能照搬的有三样：默认审批（违反 P6）、把工具注解或提示词当保证（违反 P2）、按工具手写参数映射（违反 P4）。

本文不重复 `related_work.md` 已覆盖的 TClone、Fluid、CLEO、CoplayDev unity-mcp 等条目，只在需要时提到它们。内部数据来自 `design_v0.5.md`。

## 问题一：事后恢复正处在业界刻意回避的区间

现在的情况是：对 AI 的任意代码，运行时只能在执行后按许可单恢复、再核对。恢复不了的改动（例如 Unity 里被删掉的对象）会成为违规；派生改动和意图冲突也留在场景里。实验 H 测到，人改一处，有 76%–100% 的概率被真实写法下 AI 的这一步撞上（`design_v0.5.md` 第 12.2 节）。

### 成熟系统先给效果分类，恢复不了的一律挡在执行前

2024–2026 年的 Agent 运行时把防线叠成三层：

1. 能分类的，在执行前确定性地拦下；
2. 能缓冲的，先暂存，验证后再提交；
3. 剩下的，才靠补偿或快照回滚。

各系统的具体做法如下：

- **Progent** 作为代理，在每次工具调用执行前用 JSON 策略判定。判定是符号化、确定性的，不依赖信任模型 ([Progent](https://arxiv.org/html/2504.11703v2))。
- **Magentic-UI** 让开发者把每个动作标成"总是 / 可能 / 从不"不可逆。"总是"的要人批准，"可能"的交给 LLM 判官 ActionGuard 判断 ([Magentic-UI](https://arxiv.org/html/2507.22358v1))。
- **Atomix** 把效果分成三类 ([Atomix](https://arxiv.org/html/2602.14849v1))：
  - 可缓冲：提交前别人看不见；
  - 外部化：立即执行，中止时补偿；
  - 不可逆：必须事先放行，例如经人批准。
- **Cordon** 让本地修改在影子状态里执行，把外部动作排进"效果发件箱"，提交前把血缘、权限、暂存状态和待发效果放在一起验证 ([Cordon](https://arxiv.org/html/2606.17573v1))。
- **GoEx** 建议删除前先留一份本地副本。对邮件这类发出就撤不回的动作，只能限制损害范围 ([GoEx](https://arxiv.org/html/2404.06921v1))。

**调研到的系统里，没有一个对已知不可逆的效果依赖事后恢复。**

引擎和 DCC 工具的分法相同：

- **Unreal 多人编辑：** 有人拖动物体时，他独占这些物体，其他人对它们的冲突修改被立即撤回。这和 RightOfWay 的"执行后恢复"是同一种机制，只是只用在能恢复的属性修改上。材质、网格、蓝图这类资源则在第一次修改时上锁 ([Unreal Multi-User Editing](https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-editing-overview-for-unreal-engine))。
- **Unity Smart Locks（版本控制）：** 专门用于合并不了的资源，先锁再改，而且只能锁最新版本 ([Unity Smart Locks](https://docs.unity.com/en-us/unity-version-control/smart-locks))。

2026 年新出的 AI 助手则干脆避开并发：

- **Unreal 5.8 的实验性 MCP** 在游戏线程上串行执行工具调用，要求客户端不要重叠调用 ([Epic: Unreal MCP](https://dev.epicgames.com/documentation/en-us/unreal-engine/unreal-mcp-in-unreal-editor))。实测者发现，AI 处理请求时无法在同一个项目里继续工作 ([Puget Systems](https://www.pugetsystems.com/blog/2026/07/09/unreal-engine-mcp-hands-on-testing-ai-inside-the-editor/))。
- **Roblox** 的计划"从不自动执行"，要人点 Build ([Roblox Assistant](https://create.roblox.com/docs/assistant/guide))。
- **Blender MCP Secure** 的 Normal 模式禁用任意 Python，回滚也只覆盖结构化批处理 ([Blender MCP Secure](https://superhivemarket.com/products/blender-mcp-secure))。
- **SideFX 的 Apex Script MCP**（预览版）在运行前用校验器检查生成的代码 ([Jon Peddie Research](https://www.jonpeddie.com/news/sidefx-and-nvidia-bring-mcp-powered-ai-agents-to-houdini-22s-rigging-workflow-at-siggraph-2026/))。

厂商要么把 AI 限制在类型化操作里，要么让人在 AI 工作时停手。**调研到的 DCC AI 助手和官方 MCP 服务器，没有一个支持人在 AI 改场景的同时继续编辑。**

### 拦截省下的是违规和重复，不是任务成功率

Atomix 的对照实验最贴近 RightOfWay 的处境，因为它的"检查点回滚"基线就是"执行，出错就回滚再重试"。实验在每次调用注入 30% 的故障 ([Atomix](https://arxiv.org/html/2602.14849v1))：

| 指标 | 完整事务 | 检查点回滚 |
|---|---|---|
| WebArena 任务成功率 | 57.2% | 53.2% |
| OSWorld 任务成功率 | 37.0% | 37.1% |
| τ-bench 任务成功率 | 53.5% | 41.0% |
| 预期 1,200 封邮件，实际情况 | 泄漏 0 封 | 发出 1,351 封，其中 151 封是重试造成的重复 |

成功率只在 τ-bench 上拉开差距，真正的差别在副作用。**回滚加重试会复制恢复不了的效果。** 实验 H 里，人的 Leaf_3 被恢复后，AI 又新建了一个 Leaf_3，结果是 Leaf_3.001，这是同一种病。

Cordon 的数据说明，只恢复一部分状态会留下谁都没想要的混合结果 ([Cordon](https://arxiv.org/html/2606.17573v1))：

- 在 45 个自建的危险工作流里，Cordon 全部在提交前拦下；把现有防御改造后接入，只拦下 14 个。
- 回滚 15 次：Cordon 残留 0 处改动，15 次都能继续；git restore/reset 残留 73 处，0 次能继续。

两者的局限恰好卡在 RightOfWay 的约束上：

- **Cordon** 写明，保证只覆盖经过它的运行时、效果可观察的操作，不透明的插件落在保护之外；影子副本的启动时间和工作区大小成正比；评估只有一个运行时、一个模型、45 个自建工作流 ([Cordon](https://arxiv.org/html/2606.17573v1))。
- **Atomix** 是约 2 千行的单进程 Python 库，推测执行只在模拟的 WebArena 上评估过 ([Atomix](https://arxiv.org/html/2602.14849v1))。

一段在 Blender 或 Unity 进程里原子执行的脚本，对 RightOfWay 来说就是"不透明插件"：不改应用，就没法在操作这一级插进去。所以，**对任意代码做通用的"事先阻止"，在 P3 下做不到。**

拦截的价值在 RightOfWay 里也要换个算法。违规会暂停 AI（`design_v0.5.md` 第 6 节），所以拦截的收益直接体现为暂停次数，而不是任务成功率。实验 H 里，清空重建让人的每一处修改都变成违规、AI 被暂停，这就是不拦的代价。

### 可借的是缩小"恢复不了"的范围，事后核对不能撤

**第一件可借的：用测出来的结果做效果分类。** Atomix 的分类由每个适配器手写，而 MCP 的工具注解又不能用：执行代码的工具按默认值就是"破坏性、开放世界"，规范还明说，客户端不应依据不可信服务器的注解做决定 ([MCP schema](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2025-11-25/schema.ts))。

RightOfWay 已规划的能力探测（`design_v0.5.md` 第 4 节：在临时对象上试改、试删、试恢复）正好是不手写的 Atomix 式分类：

- 测出"这次连接恢复不了删除"，删除就归入"不可逆"；
- 类型化调用遇到不可逆的效果，事先拒绝；
- 任意代码只能执行后核对，并如实报告。

写盘、存场景、导入资源、进 Play 模式这类开放世界的改动，按 Atomix 和 Cordon 的做法，也应归入事先拦截或限制范围。

**第二件可借的：删除前做备份。** GoEx 的"删除前留副本"，就是 `design_v0.5.md` 第 5 节第 3 层（执行前复制成隐藏备份）。调研补充了一个更忠实的变体：

- Unity 的 `GlobalObjectId` 对预制体实例记录"源对象的 fileID + 预制体实例的 fileID" ([Unity GlobalObjectId](https://docs.unity3d.com/ScriptReference/GlobalObjectId.html))；
- Smart Merge 按 (fileID, guid, propertyPath) 记录预制体修改 ([Unity Smart Merge](https://docs.unity3d.com/Manual/SmartMerge.html))。

如果被删的是预制体实例，可以重新实例化预制体，再套回记下的覆盖，而不是复制一份断了预制体关联的克隆。第 3 层自己标为"恢复不完全"的，正是预制体关联。这一点是推断，没有实测。另外，Unity 6.6 已把 `GetInstanceID` 标为过时，改用 `GetEntityId` ([Unity Object.GetInstanceID](https://docs.unity3d.com/ScriptReference/Object.GetInstanceID.html))，第 3 层的编号映射要跟着改。

**第三件可借的：影子执行。** 这是对任意代码真正做到"事先阻止"的唯一路线。

先看 Terraform 的 plan/apply：先干跑出差异；应用时核对状态的 lineage 和 serial 没变，变了就报 "Saved plan is stale" ([Terraform apply](https://developer.hashicorp.com/terraform/cli/commands/apply)；[HashiCorp 论坛，二手](https://discuss.hashicorp.com/t/question-on-error-saved-plan-is-stale/52912))。

放到 RightOfWay 上：

1. 把 AI 的脚本先在一个无界面、和真场景同步的应用实例里执行；
2. 按面取差异，当作"计划"；
3. 写之前核对指纹，只把许可单允许的面写进真场景。

受保护对象的删除根本不会写进真场景，也就没有违规。

`design_v0.5.md` 第 12.9 节不把暂存当默认，理由有两条：同一个文件里复制场景仍共用材质和网格数据块；复制整个文件代价高。第一条对"同一进程里的场景副本"成立，对"另一个进程"不成立。而由另一个进程把差异按面写进人这边、写前核对，正是方式二（实时同步）已经在做的事：能力表里，方式二"人改过的不写入"，被删的对象"人那份不受影响"。**所以影子执行可以理解为：让写代码的 AI 也走方式二，复用现成的写入和核对。** 代价是失去一次执行的原子性。

它的成本没人量过：

- Omniverse 能全程暂存，是因为 USD 的会话层以最强意见叠在不动的基础层之上 ([Omniverse Layers](https://docs.omniverse.nvidia.com/extensions/latest/ext_core/ext_layers.html))。Blender 和 Unity 没有这种合成层。
- DeltaBox 的毫秒级检查点靠文件系统、进程转储这类操作系统钩子 ([DeltaBox，alphaXiv 自动摘要，二手](https://www.alphaxiv.org/abs/2605.22781))，搬不到 GUI 应用的内存场景里。

所以它应作为可选能力，先在 Blender 无界面实例上量代价，而不是默认开启。

**最后，撤回这件事本身也要做对。** 几个多人编辑系统的经验：

- Ubisoft Mixer 的文档强烈建议不要用撤销，因为它可能撤掉别的参与者的修改 ([Mixer](https://ubisoft-mixer.readthedocs.io/en/latest/getting-started/features.html))。
- Figma Agent 的修改只能用聊天里短暂出现的按钮撤回，官方说"没有选择性撤回"，还有用户报告按钮消失了 ([Figma 论坛](https://forum.figma.com/ask-the-community-7/does-figma-ai-agent-support-undo-55401))。
- Unreal 和 Omniverse 都只允许每人撤销自己的操作 ([Unreal](https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-editing-overview-for-unreal-engine)；[Omniverse FAQ](https://docs.omniverse.nvidia.com/extensions/latest/ext_core/ext_live/workflow_faq.html))。

RightOfWay 在单个 Blender 或 Unity 实例里，人和 AI 共用一个原生撤销栈。人按 Ctrl+Z，撤掉的可能是 AI 的整步（连同恢复）。按现在的观察规则推断，这些变化会被记成人的修改，打上"人碰过"。这类系统的共同做法是：给每次 AI 执行登记一个命名的撤销步骤（参照 Unity 的 Undo 组、Roblox 的 `ChangeHistoryService` ([Roblox](https://create.roblox.com/docs/reference/engine/classes/ChangeHistoryService)))，同时用账本实现每个 AI 自己的撤回（R14）。

### 派生改动和意图冲突至今无人自动解决

**没有一个系统能自动重算派生改动。** 已有的做法都只走了一部分：

- **Atomix** 靠"未提交的值别人看不见"来避免连锁中止 ([Atomix](https://arxiv.org/html/2602.14849v1))。
- **Cordon** 在分发点记录血缘边，作为提交时的证据，但上游被撤回后，没有让下游失效的流程 ([Cordon](https://arxiv.org/html/2606.17573v1))。
- **SagaLLM** 用依赖图决定补偿顺序，并"只重新规划受影响的部分"，但没给出算法 ([SagaLLM](https://arxiv.org/html/2503.11951v3))。
- **RIR** 把可能被撤回动作作废的信息"标记为待重新核实" ([RIR，alphaXiv，二手](https://www.alphaxiv.org/abs/2609.18304))。

**意图冲突同样没有自动检测。**

- Magentic-UI 让人随时暂停、直接操作再继续，重新规划时保留已完成的前缀 ([Magentic-UI](https://arxiv.org/html/2507.22358v1))。
- 最接近的新机制是 ATR ([ATR](https://arxiv.org/abs/2609.08015))。它区分"版本冲突"和"决策冲突"：记录支撑一个待执行动作的可执行条件；变化发生时，只重查受影响的条件，再决定保留、刷新、重新规划还是阻止。在 21 万次运行里，结果都符合开发者指定的预期；每次变化平均只查 0.6 个条件（对照组 6.0 个）。但条件要人手写，也不区分人和 AI。
- CodeCRDT 显示，即使语法层的合并零失败，多个 AI 之间仍有 5–10% 的语义冲突 ([CodeCRDT](https://arxiv.org/abs/2510.18893))。RightOfWay 的部分生效保住了每一个面，却保证不了整步的意图。

AgentRewind 的消融实验给了一个量化的提醒 ([AgentRewind，alphaXiv 自动摘要，二手](https://www.alphaxiv.org/abs/2608.14380))：

| 回滚方式 | 成功率 |
|---|---|
| 上下文和环境一起回到检查点 | 87.8% |
| 只回滚环境，上下文不动 | 65.9% |
| 完全不回滚 | 62.2% |
| 只回滚上下文，环境不动 | 43.9% |

RightOfWay 的处境接近第二行：场景里人的面被恢复了，AI 的上下文却以为自己的修改都生效了。它和完全同步之间还差二十多个百分点，差的就是把恢复结果同步进 AI 的认知。R22 已经在做这件事，可以做得更彻底。

业界的通行做法是把拒绝当作普通工具结果返回，附上原因和下一步建议：

- MCP 规范说，工具执行错误（`isError`）"包含模型可以用来自我纠正的可操作反馈" ([MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools))。
- Kubernetes Server-Side Apply 冲突时返回 409，每个冲突字段一条 cause，写明 "conflict with <manager>" ([apimachinery conflict.go](https://raw.githubusercontent.com/kubernetes/apimachinery/master/pkg/util/managedfields/internal/conflict.go))。
- Flux 有两种处理 ([Flux](https://fluxcd.io/flux/components/kustomize/kustomizations/))：Strip 把别人拥有的字段从本次写入里拿掉，其余照常应用；Adopt 把集群里的现值抄进自己的期望状态。
- Terraform 的 `-refresh-only` 只把带外修改记进状态，不改基础设施 ([Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan))。

R22 已经告诉 AI 哪些没生效、现在的值、它原本想改成的值。可以再补两样：

1. **"失效的事实"。** 明确告诉 AI：Leaf_3 的高度以人的值为准，你按旧值算出的其余位置要重新核对（RIR 式）。同时把人的值说成新的目标，而不只是"被挡住了"（Terraform 接受漂移的思路，相当于 Flux 的 Adopt）。
2. **循环检测。** Claude Code 的过期写入检查曾让 AI 在 5 次以上的"读—改"循环里卡死，只有开新会话才解开 ([Claude Code #33856](https://github.com/anthropics/claude-code/issues/33856))。RIR 用"重复动作、无效步骤、状态循环"来触发干预。RightOfWay 可以直接从账本算出"面 P 在 K 步内被保留了 N 次"，超过阈值就提醒人或暂停 AI。

"过期就整步拒绝"在 RightOfWay 的场景里行不通。Terraform、Claude Code、Kubernetes 的 Apply 都是整体拒绝：Apply 只要碰到别人拥有的字段就整体失败 ([K8s SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/))。它们负担得起，是因为冲突罕见：维基媒体 13,871 个 Etherpad 文档里，时间和位置都接近的编辑只占 2.72%–18.70%，随窗口从 10 字符/5 秒到 800 字符/60 秒变化 ([D'Angelo et al., CSCW 2018](https://upsilon.cc/zack/research/publications/cscw-2018-rtce.pdf))。RightOfWay 测到的碰撞率是 76%–100%，高出一个数量级，冲突处理就是常规路径。在这种碰撞率下，整步拒绝会让 AI 一直提交不了，所以第 12.7 节把"部分生效"作为默认，有了外部证据支持。

至于意图，ATR 式的条件可以作为可选功能：由 AI 自己声明"这一步依赖哪些值"，或者声明"这一步要么全做、要么不做"。

### 审批和手写扫描规则过不了 P6 和 P4

| 可借的做法 | 来源 | 和原则的关系 |
|---|---|---|
| 执行前人工批准 | Unity AI Assistant 的 Ask Permission、Roblox 的 Build、Magentic-UI 的"总是"类 | **违反 P6**（逐条批准）；只能作为人主动开启的选项 |
| 静态扫描脚本里的删除调用 | Progent 思路、第 5 节第 2 层 | 要为每个应用手写删除 API 清单，**和 P4 冲突**；动态写法扫不到，只能当优化，由 P1 兜底 |
| 用 MCP 注解判断是否破坏性 | ToolAnnotations | 注解来自服务器、不可信，**不能当 P2 意义上的保证**，只能用来分流 |
| 用效果分类决定事先拦截 | Atomix | 符合原则，前提是分类由能力探测测出（P4） |
| 删除前备份、预制体重新实例化 | GoEx、Unity GlobalObjectId | 符合 P3；隐藏备份要满足 P3 的"看不见、可清除" |
| 影子执行 | Terraform、Cordon | 符合 P3、P6；失去原子性，靠写入前核对补上；代价未知 |
| 告知失效事实、循环检测 | RIR、K8s 409、Flux Adopt | 符合；这属于 P8 告知，不是安全手段 |
| AI 自己声明意图条件，或"全有或全无" | ATR、第 12.7 节 | 依赖 AI 配合，**只能用于效率和意图，不能用来保护人的修改**（P2） |

## 问题二：名字是唯一能熬过 AI 重写的身份

AI 清空重建时，运行时看到的是"删了 N 个、又新建了 N 个"：恢复出来的人的对象和 AI 新建的同名对象并存（Leaf_3.001），父对象断开。"按名字和类型认回"的原型在实验 H 里把所有清空重建的情况都降到 0 重复、0 违规，但目前默认关闭，Unity 一侧也还没实现（`design_v0.5.md` 第 12.4 节）。

### 控制不了生成过程的系统，最后都按名字认对象

**CAD 的拓扑命名问题是最老的同类问题。** 几种代表做法：

- FreeCAD 1.0 采用了社区贡献的 realthunder 算法，用编码了创建历史的元素名（如 `Face6;:M2;FUS;:T1:5:F`）跟踪重算后的面和边 ([realthunder wiki](https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm))。
- OpenCASCADE 的 OCAF 记录每个形状的演变（新建、生成、修改、删除），重建后沿演变链找回引用 ([OCCT OCAF](https://occt3d.com/dev/doc/overview/html/occt_user_guides__ocaf.html))。
- Onshape 用"由某操作创建""包含某点"这样的查询代替直接引用 ([Onshape FeatureScript](https://cad.onshape.com/FsDoc/modeling.html))。

代价和局限都写在文档里：

- 元素映射让一个 148 个对象的模型重算时间从 40 秒变成 52 秒 ([realthunder wiki](https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm))。
- 维护者在 1.0 发布时说，这个问题"没有完全解决" ([Hackster，二手](https://hackster.io/news/freecad-hits-1-0-after-two-decades-finally-sees-an-end-to-the-toponaming-problem-7e7ac2df3e63))。
- 被引用的边一旦被修改或删除，FreeCAD 直接报 "invalid edge link"。猜测式修复被放在单独的工具里，文档说"猜得不总是可靠" ([realthunder wiki](https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm))。

**这些办法都依赖确定性的重放**：同一棵特征树重算一遍，"它是怎么造出来的"才是稳定的键。AI 清空重建是重新创作，不是重放，所以历史命名搬不过来。

**名字式身份的代表是 OpenUSD。**

- 路径就是身份。人的修改作为更强一层里的 "over" 叠在生成内容上，只要路径不变，下层怎么重新生成都不受影响。
- 底层的 prim 一旦被移动或改名，over 就"孤立"并被静默忽略 ([OpenUSD FAQ](https://openusd.org/release/usdfaq.html))。
- USD 的补救有两个：命名空间编辑器在改名时改写已知的引用，并留下 relocates 映射；删除则记成一条不破坏原数据的意见——停用，或者 relocate 到 `<>`，之后这个路径不能再定义任何 prim ([OpenUSD Namespace Editing](https://openusd.org/release/user_guides/namespace_editing.html)；[OpenUSD 术语表](https://openusd.org/release/glossary.html))。

其他系统的做法：

- **Omniverse 实时会话**给每个节点一个随机 64 位编号，改名和换父对象都只是普通的字段修改。两个客户端新建同名节点时，服务器忽略第二个；**输的一方删掉自己的节点，收养远端那个** ([Omniverse Live Layer Details](https://docs.omniverse.nvidia.com/kit/docs/usd_resolver/latest/docs/live-layers-details.html))。这是"按名字认回同一个对象"在商用系统里的直接先例。
- **Blender 自己的库链接也按名字**：开发者说，给被链接的数据块改名"从来就没指望能正常工作" ([Blender T77437](https://developer.blender.org/T77437))。
- **程序化工具把"序号"和"身份"分开。** Houdini 的 `@id` 和 Blender 几何节点的 `id` 都是显式属性，只有生成器把它传下去才有效 ([SideFX](https://www.sidefx.com/docs/houdini/model/attributes.html)；[Blender 手册：ID 节点](https://docs.blender.org/manual/en/latest/modeling/geometry_nodes/geometry/read/id.html))。
- **React 的协调规则**是"在树中的位置、类型、在兄弟间唯一的 key"。类型一变，整棵子树重置；用序号当 key "常常导致微妙的 bug" ([react.dev](https://react.dev/learn/preserving-and-resetting-state)；[react.dev 列表](https://react.dev/learn/rendering-lists))。RightOfWay 的"名字 + 类型"几乎就是这条规则，区别是作用域是全局，不是兄弟之间。

"程序即场景"的系统有两条路：

- **把人的直接修改写回源程序。** Sketch-n-Sketch 靠值的追踪和来源标记，小改动自动推断，有多种解释时列出候选让人选 ([PLDI 2016](https://ar5iv.labs.arxiv.org/html/1507.02988)；[UIST 2019](https://ar5iv.labs.arxiv.org/html/1907.10699))。Lovable 在编译时给每个 JSX 元素打稳定编号，直接改语法树，不经过 AI ([Lovable](https://lovable.dev/en/blog/visual-edits))。
- **锁住不让 AI 改。** v0 用文件级的 `locked` 标记，生成时 AI 不改被锁的文件 ([v0 API](https://v0.app/docs/api/v1/guides/lock-files-from-ai-changes))。但它的 Design Mode 把人的直接修改发给 AI 重新生成 ([v0 Design Mode](https://v0.app/docs/design-mode))，等于让模型重新解释人的修改，正好和"人优先"相反。

2026 年的场景生成研究开始避免整体重建：

- HDSL 只让模型重写相关子树，再做确定性的三方合并，编辑时的 token 用量少 5.22 倍（摘要没写对照基线） ([HDSL](https://arxiv.org/abs/2606.09738))。
- MUSE 用局部操作，在 240 个编辑案例上报告保留率 99.9、意外改动率 0.6 ([MUSE](https://arxiv.org/abs/2606.14168))。

但这两篇都没有讨论人在界面里直接做的修改。

### 原型还没碰到同名兄弟、跨两次执行和改名

名字式身份怎么失败，前人已经踩过：

- USD 接受"改名会让 over 孤立"。
- Blender 的库覆盖在重新同步时，"如果原库里改了，被编辑过的覆盖会被删掉" ([Blender 手册：Library Overrides](https://docs.blender.org/manual/en/latest/files/linked_libraries/library_overrides.html))。
- Speckle 的内容哈希按设计让内容相同的对象得到相同的哈希 ([Speckle](https://docs.speckle.systems/developers/data-schema/object-schema))，所以几何签名分不开一模一样的叶子。

对照第 12.4 节的原型，实验 H 还没覆盖这些情况：

- 很多对象合法地共用一个基础名（`Leaf`、`Leaf.001`……），去掉后缀后的键不唯一；
- AI 改名，而不是重建；
- "清空"和"重建"分在两次执行里，而原型只在一次执行内配对；
- 父对象被重建，导致子对象的路径变化；
- 同名，但换了类型；
- 数据块级别的同名：人改过的 `LeafMat` 被重建成 `LeafMat.001`（见问题三）；
- AI 把几个物体合并成一个，或把一个拆成几个。

Unity 一侧更难：

- `GlobalObjectId` 能跨保存和重新加载保持不变，但场景至少要保存过一次；对象换了场景会变；删掉再重建也会变 ([Unity GlobalObjectId](https://docs.unity3d.com/ScriptReference/GlobalObjectId.html))。所以清空重建之后，仍然要靠名字配对。
- GameObject 的名字不要求唯一（调研笔记标为常识，本轮没有核对）。名字加类型的歧义会比 Blender 多得多。

### 借一条分层的配对规则，再加 Unity 式的孤儿保留

综合这些系统，认回可以写成一条通用的级联。每一级都不需要为某个应用手写规则：

1. **应用给的活句柄。** 同一个句柄就是同一个对象。句柄不变而名字变了，就记一条改名（仿 USD 的 relocates）。
2. **在已认回的父对象下，按完整名字加类型配对，从上往下做。** React 的 key 只在兄弟间唯一；父对象被重建时，先认回父对象，再认子对象。
3. **去掉后缀的名字加类型。** 只在同一父对象下、并且一对一时接受。
4. **可选的显式键。** 如果 AI 愿意重新写出一个稳定的自定义属性，就优先用它（类似 Houdini 的 `@id`），但不能要求 AI 这样做。
5. **几何或面的指纹只用来打破平局。** 只在已有候选之间比较，不能单独用来配对。

仍有歧义的，**什么都不转移**，把人的修改标为孤儿，并列出候选。这是 Bidarra 等人和 Sketch-n-Sketch 的共同做法：宁可明确失败，也不悄悄绑错 ([Bidarra et al. 2005](https://publications-cgv01.ewi.tudelft.nl/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsibWVzc2FnZSI6IkJBaHBBamNQIiwiZXhwIjpudWxsLCJwdXIiOiJibG9iX2lkIn19--22a685d9ec14b4f8b036f8ee495000df0f671784/BNB05b.pdf))。

去后缀的规则本身不能手写成 "`.001`"。可以在能力探测里经 MCP 新建两个同名对象，看应用怎么去重，从而学出这个模式。如果 AI 把物体合并或拆开，可以借 C3D 内核的词汇来规定人的修改的去向 ([C3D Labs](https://c3dlabs.com/doc/class_mb_attribute.html))：附在模型对象上的属性，拆分时可以复制到每一块、释放，或交给回调处理；合并时可以移到吸收方。按属性类别声明"复制到每一块 / 留给吸收方 / 放弃并报告"即可。

**孤儿怎么处理，各家差别最大：**

| 系统 | 孤儿的处理方式 |
|---|---|
| USD | 静默忽略 |
| Blender 库覆盖 | 重新同步时删除 |
| FreeCAD | 报硬错误 |
| Unity | 把"未使用的覆盖"保存在场景文件里，目标回来时自动重新套上；删除要人显式执行，并记日志 ([Unity Unused Overrides](https://docs.unity3d.com/Manual/UnusedOverrides.html)) |

RightOfWay 应该取 Unity 的做法：

- 按最后的身份保留人的修改；
- 后续执行里出现匹配的对象，就重新绑定，这正好解决"清空和重建分两次执行"的情况；
- 持续告诉人和 AI，永远不静默丢弃。

**人删除的对象，要像 USD 那样记成一条意见（墓碑）**，否则清空重建会把人删掉的东西复活。第 12.5 节的"不许重建"就是这条，调研支持把它和认回一起实现。

两点设计选择需要明确：

- **记绝对值还是增量。** Houdini 的 Edit SOP 在有参考几何体时，按差值记录编辑 ([SideFX Edit SOP](https://www.sidefx.com/docs/houdini/nodes/sop/edit.html))。一篇厂商博客说，UE 5.8 的 PCG 手动编辑也是"记在生成结果上的持久增量" ([StraySpark，二手厂商博客](https://www.strayspark.studio/blog/ue5-8-pcg-manual-artist-edits))，而 Epic 官方只说手动编辑"不破坏程序化" ([UE 5.8](https://www.unrealengine.com/en-US/news/unreal-engine-5-8-is-now-available))。RightOfWay 和 USD 一样记绝对值。AI 整体挪动布局时，绝对值会把人改过的对象钉在原处，增量则会跟着走。"人优先"在重建之后到底意味着哪一种，要明确写下来。
- **把人的修改写回给 AI。** 以"程序层面的事实"告诉 AI，例如："`Leaf_3.location` 归人所有 = (x, y, z)，请在你的程序里保留"。下一次重建就会自带这些值。这是 Sketch-n-Sketch 和 Lovable "写回源程序"的思路，只是经由 AI 转达而不是解析器，所以不依赖具体的语言和应用。

认回本质上是猜测，应该像 FreeCAD 那样单独成步，记录置信等级（唯一的完整匹配 / 去后缀匹配），人和 AI 都能查看和撤销。评估可以直接借用 MUSE 的保留率和意外改动率。

### 请人裁决和手写去后缀规则过不了 P6 和 P4

| 做法 | 来源 | 和原则的关系 |
|---|---|---|
| 按创建历史命名 | FreeCAD、OCAF、Onshape | 前提是确定性重放，AI 重建不满足，不适用 |
| 有歧义时请人选 | Bidarra、Sketch-n-Sketch | **违反 P6**；改为不转移、报告孤儿，人可以处理但不会被阻塞 |
| 要求 AI 写出稳定键 | 类比 Houdini `@id` | 依赖 AI 配合，**只能作为可选、优先使用的通道**（P2） |
| 去后缀规则 | Blender 的 `.001` | 手写即**违反 P4**；改为用探测学出 |
| 往对象上写编号 | `rightofway_id`、Lovable 的编译期编号 | P3 允许，但要看不见、可一键清除（第 11 节第 6 步还没做） |
| Unity 用 `GlobalObjectId` | Unity 文档 | 新场景要先存盘；替人存盘是人没做过的操作，不应强制，用 `GetEntityId` 兜底 |
| 认错对象 | — | 会把人的值套到 AI 心里的"另一个东西"上；符合 P5，但损害 AI 的意图，所以只接受一对一 |
| 孤儿静默忽略或删除 | USD、Blender 库覆盖 | **违反 P1**；必须列出来 |

## 问题三：共用数据在每个系统里都是独立单位

Blender 和 Unity 的数据模型会经由每个物体暴露共用材质的属性。人改一次材质，被记成每个物体各改一次；恢复时，材质分叉了。

### 别人把材质的修改记在材质上，不记在每个物体上

各系统的做法：

- **Unreal Concert** 分两层：关卡里 actor 的修改实时同步；材质、网格、蓝图这类资源是独立单位，第一次修改时自动上锁，存盘时整体同步，其他编辑器热重载 ([Unreal Multi-User Editing](https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-editing-overview-for-unreal-engine))。材质的修改从不被拆成 N 个 actor 的修改。
- **USD** 里材质是一个 prim，几何体通过只有一个目标的 `material:binding` 关系指向它 ([UsdShadeMaterialBindingAPI](https://openusd.org/release/api/class_usd_shade_material_binding_a_p_i.html))，所以改共用材质就是改一个属性。
- **Blender 的 Multi-user 插件**（2026-09-01 发布 0.8.2，支持 Blender 4.5 LTS 及以上）以数据块为复制和权限的单位。每个数据块属于一个用户，或者是公共的；Reset 把本地数据块原地恢复成服务器版本 ([Multi-user](https://slumber.gitlab.io/multi-user/getting_started/how-to-manage.html)；[Blender Extensions](https://extensions.blender.org/add-ons/multi-user/versions/))。
- **Blender Studio 的 Asset Pipeline** 规定得最明确 ([Asset Pipeline](https://studio.blender.org/tools/addons/asset_pipeline))：
  - 每个共享 ID（几何节点组、图像）只归一个任务层所有，其他层可以引用，不能修改；
  - 材质默认归着色层，所有者可以"放弃"，让别人认领；
  - 一个物体数据块如果被不同所有者的物体共用，推送和拉取时就会被复制。

**分叉恰好出现在所有权边界切过共用数据的地方**，和 RightOfWay 的情况一样。

**Unity 文档把这个分叉写成了 API 行为。** 读 `Renderer.material` 时，如果材质是共用的，它会"克隆共用材质，并从此使用克隆"，克隆要调用方自己销毁 ([Unity Renderer.material](https://docs.unity3d.com/ScriptReference/Renderer-material.html))。Unity 同时给了两种不分叉的写法：

- `sharedMaterial`：直接写共用的源；
- `MaterialPropertyBlock`：给单个物体加属性覆盖，不复制材质，但和 SRP Batcher 不兼容 ([Unity MaterialPropertyBlock](https://docs.unity3d.com/ScriptReference/MaterialPropertyBlock.html))。

**设计工具和 DCC 的实例覆盖是同一个分层模型。** Figma 组件实例、Unity 预制体、Blender 库覆盖都是：源提供默认值；实例上按属性做的本地覆盖优先，源更新时保留；重置也按属性进行 ([Figma](https://help.figma.com/hc/en-us/articles/360039150733-Apply-overrides-to-instances)；[Unity 预制体覆盖](https://docs.unity3d.com/Manual/PrefabInstanceOverrides.html)；[Blender 手册](https://docs.blender.org/manual/en/latest/files/linked_libraries/library_overrides.html))。

**Kubernetes 把"单位是什么"交给 schema 声明。** 列表可以是 atomic（整体一个所有者）、set，或按键的 map ([K8s SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/))。声明为 atomic 的列表会让字段级保护失效：Flux 的 Merge 模式会保留别的工具加的字段，但 `.spec.tolerations` 这类 atomic 列表仍被整体改回 ([Flux](https://fluxcd.io/flux/components/kustomize/kustomizations/))。

### 单位放错的后果两头都有：锁太大，或者被复制

- **锁太大：** 2026 年 9 月有报告说，UEFN 里改一个 Scene Graph 实体，就会把所有实体和整个关卡对协作者锁住 ([Unreal 论坛](https://forums.unrealengine.com/t/modifying-a-single-scene-graph-entity-locks-all-entities-and-the-level-for-collaborators/2835681))。
- **切得太碎：** Asset Pipeline 的复制、Unity 的克隆，都是这一类。

另外两个局限要注意：

- Unity 预制体覆盖对集合元素按序号记录，源集合一变，覆盖就可能落到别的元素上 ([Unity 预制体覆盖](https://docs.unity3d.com/Manual/PrefabInstanceOverrides.html))。
- Blender 库覆盖在库的层级变化时重新同步，可能删掉被编辑过的覆盖。

还有一个信号来自 AI 本身：有测试者发现，Figma 的 Agent 如果不被提示，就会硬编码数值，而不是使用设计系统里的变量和组件 ([Bitovi，二手](https://www.bitovi.com/blog/figma-just-opened-the-canvas-to-agents.-heres-what-actually-happens))。AI 默认按"每个对象一份"去写共用的定义，和 RightOfWay 测到的情况同源。

### 面的键改成"拥有它的数据块 + 属性路径"

问题的本质是**投影**：应用的数据模型沿"物体 → 材质槽 → 材质 → 属性"这条路径，把一个材质属性暴露成 N 个物体上的面，但写入实际落在同一个共用数据块上。调研到的系统都按拥有者记，没有一个按访问路径记。

RightOfWay 的改法：

- 面的键改成"拥有这个属性的数据块的编号 + 属性路径"；
- 材质、网格、节点树各自带 `rightofway_id` 和自己的面；
- 物体的面只保留"哪个槽指向哪个材质"这条引用。

这样，人改一次共用材质只记一次，记在材质上。恢复时把值写回同一个材质数据块，不从快照重新创建或追加。分叉的具体来源要对照接入代码确认——是从快照重建了材质，还是经过了会克隆的按物体访问器。不管是哪种，修法相同：恢复必须写回同一个数据块。

这条规则可以不手写。Blender 的 RNA 和 Unity 的 `SerializedProperty` 本来就区分"引用另一个数据块的属性"和普通值，所以"值是对另一个数据块的引用"就是通用的判别条件。这一点是推断，需要在两边的反射数据上验证。

其他几项可借的做法：

- **集合按稳定的名字或编号作键，不按序号。** 参照 K8s 的 `listType=map` 加 `list-map-keys`。材质槽、修改器栈都属于这一类（Blender 的修改器有跨会话稳定的 `persistent_uid` ([Blender PR #117347](https://projects.blender.org/blender/blender/pulls/117347))），Unity 的组件数组也是。
- **AI 一步里新建的材质实例要记为派生改动，并报告出来**，例如脚本用了 `renderer.material`。RightOfWay 不知道 AI 是想给一片叶子单独换色，还是无意中克隆了材质。
- **换材质也要检查所有权。** AI 把某个槽换成另一个材质，虽然没写人改过的面，却让人的材质修改从这个物体上消失了。这需要单独检查，类比 Blender 库覆盖在重新同步时删除覆盖。
- **所有权策略可以借 Concert 的"第一次修改即上锁"，或 Asset Pipeline 的"单一所有者、可放弃"。** 人一旦改了共用材质，AI 仍可以把这个材质指给物体，但不能修改它，直到人交还（R7）。默认最好仍保留材质内部按面的粒度，比如单个节点输入值，整份上锁只作为选项。

### 整份上锁和手写"哪些是共用的"过不了 P7 和 P4

| 做法 | 来源 | 和原则的关系 |
|---|---|---|
| 资源第一次被改就整份上锁 | Unreal Concert | 比面粗，**和 P7 冲突**；只作为选项，用来避免"一半是人改的、一半是 AI 改的"节点树 |
| 共享 ID 单一所有者、可放弃 | Asset Pipeline | "放弃"是人的操作，对应 R7 交还，只能是可选的（P6） |
| 判断哪些属性是共用的 | — | 手写"材质是共用的"**违反 P4**；必须从反射里的引用关系推出来 |
| 用 `MaterialPropertyBlock` 做不分叉的单物体覆盖 | Unity | 只适用于 Unity，还会改变渲染批处理；RightOfWay 不应替 AI 改写法，只报告分叉 |
| 记账从 N 次变成 1 次 | — | 符合 P1：告知里写"人改了材质 X，影响 N 个物体"，比 N 条记录更真实 |

## 问题四：网关只判断请求，从不看调用后的场景

RightOfWay 还没有 MCP 代理。代理要预检查类型化的工具调用，就得知道每个参数对应哪个对象、哪些面；手写这种对应违反 P4（`design_v0.5.md` 第 12.8 节）。

### 十几个网关都能事先拦截，参数映射却全靠人写

2026 年的 MCP 网关都能在调用到达服务器之前拒绝它，区别在于看得多细：

- **Docker** 的 Cedar 策略默认拒绝。`context.args` 里有工具参数；工具的四个注解作为属性提供，但只是"建议"；`@requireApproval` 可以要求审批 ([Docker MCP policy](https://docs.docker.com/ai/sandboxes/governance/reference/mcp-policy/))。
- **ToolHive** 把每个参数变成 `arg_<name>` 属性，但对象和数组只剩"是否存在"，看不到内容 ([ToolHive](https://docs.stacklok.com/toolhive/reference/authz-policy-reference))。
- **IBM ContextForge** 有 `tool_pre_invoke` / `tool_post_invoke` 钩子，以及跨请求的插件状态 ([ContextForge](https://ibm.github.io/mcp-context-forge/latest/architecture/plugins/))。
- **Invariant** 的规则用 `->` 连接前后事件，检测跨工具的数据流 ([Invariant](https://github.com/invariantlabs-ai/invariant))。
- **GitHub 的 gh-aw-mcpg** 给 AI 打信息流标签，从结果里对象的元数据推出完整性等级，并在会话内跨调用保持 ([gh-aw-mcpg](https://pkg.go.dev/github.com/github/gh-aw-mcpg@v0.1.15))。
- **TrueFoundry 和 hoop.dev** 能把调用挂起，等人批准 ([TrueFoundry](https://www.truefoundry.com/blog/mcp-tool-approval-human-gate-call-path)；[hoop.dev](https://hoop.dev/labs/mcp-proxy))。
- **Kong 和 Pomerium** 只按工具名放行 ([Kong](https://developer.konghq.com/plugins/ai-mcp-proxy/)；[Pomerium](https://www.pomerium.com/docs/capabilities/mcp/limit-mcp-tools))。
- **PolicyLayer** 的登记库给 44,603 个服务器、51.5 万多个工具各标了风险等级，但没说是怎么标的 ([PolicyLayer](https://policylayer.com/tools/wangdiandao-godot-devtool/editor-undo-redo))。

Cerbos 的分析点出了网关挡在调用路径上的硬限制 ([Cerbos](https://www.cerbos.dev/blog/authorizing-mcp-tool-calls-at-the-gateway-or-inside-the-proxy.md))：

- 它能在服务器看到请求之前返回 403，但"不能只同意一部分"，也不能修改请求；
- 参数要靠团队写的 CEL 映射才能变成资源属性；
- 缺属性就拒绝，所以 schema 一变，就会变成拒绝。

**这些网关有两个共同点，决定了 RightOfWay 的位置：**

1. **参数到资源的映射总是按工具写的。** 要么由策略作者写（Docker、ToolHive、Cerbos），要么由工具自己声明（OpenID 的 COAZ-MCP 草案让工具在输入 schema 里用 CEL 声明参数怎么映射到资源 ([OpenID COAZ](https://openid.net/getting-cozy-with-coaz-securing-apis-and-ai-agents-with-standardized-authorization/))）。
2. **没有一个网关检查调用之后应用的真实状态。** 它们最多看看响应文本，做脱敏。

规范层面也只有粗信号：

- 四个注解的默认值都是悲观的（`destructiveHint` 默认 true）。MCP 官方博客说注解"不是强制手段"，客户端应把不可信服务器的注解当作不可信 ([MCP 博客](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/))。
- 草案 SEP-1862 `tools/resolve` 让客户端带着具体参数，取回这一次调用的注解。它的动机正是"多动作的工具只能声明最坏情况"；它把"作用范围"列为未来的扩展；评审者指出，它仍然不能让注解变成可强制的 ([SEP-1862](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1862))。
- 草案 SEP-2848 用"规范化参数摘要 + 主体/资源 + 新鲜度窗口"来绑定一次审批，执行时不一致就拒绝。评审者提出了 TOCTOU 问题：执行时必须重新验证状态 ([SEP-2848](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2848))。
- 截至 2026-10-08，没有找到关于资源版本、ETag、条件写入或锁的 SEP。

DCC 方面：

- CoplayDev unity-mcp 这类类型化工具已经让对象级的预检查可行。实验 H 里，模拟 Unity 接入的批量命令是 0 违规。
- Unreal 5.8 的 MCP 不会自动把工具调用包进 Unreal 事务 ([dev.to，二手](https://dev.to/gamedevtoollab/using-unreal-mcp-in-ue-58-from-codex-setup-toolsets-and-safe-editor-automation-4pon))，所以每次调用是否原子，也要靠能力探测测出来。

### 代码模式会让按工具的预检查覆盖面越来越小

按工具定策略，有一个结构性风险：代码模式。

- Cloudflare 的 MCP 门户可以把所有上游工具换成 `portal_codemode_search` 和 `portal_codemode_execute` 两个工具，在隔离的 Worker 里执行 AI 写的 JavaScript，并且可以强制开启 ([Cloudflare](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/index.md))。
- Codex 的 Tool Search 经 `call_tool` 路由内部操作，使"只允许写"的模式不再是可靠的边界 ([dev.to，二手](https://dev.to/gamedevtoollab/using-unreal-mcp-in-ue-58-from-codex-setup-toolsets-and-safe-editor-automation-4pon))。

再加上 `related_work.md` 第 2c 节记录的"写代码而不是逐个调用工具"的趋势，类型化调用在流量里的占比只会下降，按工具的预检查能覆盖的部分也跟着缩小。

另一个局限是"失败即拒绝"。ToolHive 和 Cerbos 缺属性就拒绝。套到 RightOfWay 上，凡是映射不出参数的工具都会被挡下，用户体验会很差。

### 前后两个钩子，探测学映射，只对恢复不了的失败即拒

**前后两个钩子已经是标准形状**（ContextForge 的 `tool_pre_invoke` / `tool_post_invoke`、Docker 拦截器的 before / after），RightOfWay 的"许可在前、核对在后"正好套得进去。RightOfWay 除了自己当代理，还可以做成 ContextForge 插件或 Docker 拦截器，接触已经在用这些网关的人。不过 Docker 拦截器的 after 语义和负载格式，官方文档没写清，要先验证。

**参数到对象和面的映射，是 RightOfWay 和整个网关市场不一样的地方。** 市场接受按工具手写，RightOfWay 受 P4 约束不能写。第 12.8 节规划的"在临时对象上调用每个工具，看哪些面变了"，在调研范围内没有先例。可以用通用的 schema 启发补足：

- 在 JSON Schema 2020-12 写的输入 schema 里，名字像 `name`、`path`、`target`、`id` 的字段，是候选的目标参数；
- 以属性名为键的对象，是候选的属性包；
- 枚举型的 `action` 参数（例如 `manage_gameobject` 的 create / modify / delete），可以逐个取值去探测；
- `outputSchema` 和 `structuredContent` 能在调用后告诉代理新建或影响了哪些对象 ([MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog))。

启发只给候选，由探测确认，最后仍由事后核对兜底。还可以借 Kubernetes 的规则：几个写者设了同一个值，就算共同拥有，不算冲突 ([K8s SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/))。命令写的值和人现在的值相同时，不必记成"被保留"，以免撑大告知和碰撞统计。

**映射不出来时，按能力表分级处理。**

- 对这次连接测出"恢复不了"的效果（例如 Unity 删除），映射不出目标就拒绝。这是 Atomix 对不可逆效果的做法。
- 其余的一律放行，交给执行后核对，而不是像 ToolHive、Cerbos 那样缺属性就拒绝。

**被拒或部分生效的调用，作为工具执行错误返回。** 按 SEP-1303 的精神，返回 `isError: true`，附结构化原因，因为规范认为这样模型才能自我纠正 ([MCP 2025-11-25 changelog](https://modelcontextprotocol.io/specification/2025-11-25/changelog))。格式可以照 K8s 的 409：每个面一条，写明面、所有者 = 人、时间。Progent 默认的回退也是"用一条消息代替工具输出，告诉 AI 换个工具或参数继续" ([Progent](https://arxiv.org/html/2504.11703v2))。

**等人释放对象（R9）时，需要挂起调用。** 2026-07-28 版规范把 Tasks 移成官方扩展，用 `tasks/get` 轮询代替阻塞 ([MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog))。代理可以返回一个任务句柄，对象释放后再完成。TrueFoundry 的 `_meta.approval_status: "pending"` 是非标准约定，客户端要认识它才行 ([TrueFoundry](https://www.truefoundry.com/blog/mcp-tool-approval-human-gate-call-path))。释放后、执行前，要像 SEP-2848 讨论的那样重新验证状态；RightOfWay 的许可单指纹核对已经在做这一步。

**代码模式的风险只有一个应对：** 代理把任何执行代码的工具、任何"调用别的工具"的元工具，都当作代码路径，走执行后核对。

**标准层面可以投入少量精力。** Tool Annotations 兴趣组的开放问题里，有运行时注解和"人在环中" ([IG charter](https://modelcontextprotocol.io/community/interest-groups/tool-annotations.md))。RightOfWay 的风险正是运行时的："这个调用危险，是因为人刚改了它的目标"。可以向 SEP-1862 提议，把"影响的对象和属性"作为 `tools/resolve` 的输出；向 SEP-2848 提供 TOCTOU 用例。但按 P3，不能依赖它们被采纳。

### 手写映射、依赖服务器声明、挂起等审批过不了 P4、P3、P6

| 做法 | 来源 | 和原则的关系 |
|---|---|---|
| 按工具手写参数映射 | Docker、ToolHive、Cerbos | **违反 P4**；改用 schema 启发加探测 |
| 依赖服务器声明映射或注解 | COAZ-MCP、SEP-1862、ToolAnnotations | 要求服务器作者为本协议做事，**和 P3 冲突**；有就用，不依赖 |
| 挂起等人审批 | TrueFoundry、hoop.dev、Docker `@requireApproval` | **违反 P6**；挂起只用于"等人释放对象"，不用于审批 |
| 缺映射就拒绝 | ToolHive、Cerbos | 不违反 P1，但会大量误拦；只对恢复不了的类别失败即拒 |
| 用 MRTR 或 elicitation 问人 | MCP 2026-07-28 | 问到的是 AI 客户端的用户，未必是在 Blender 里干活的那个人；也违反 P6 |
| 挂起直到人显式释放 | R10 | 显式释放本身就是人的操作。Concert 在松开鼠标时就结束独占，Collab Pro 断线即解锁 ([Blender Collab Pro](https://superhivemarket.com/products/blender-collab-pro/docs))，提示用"租约"更符合 P6 |

## 建议优先级：先修材料和身份，再建代理

排序看四件事：

1. 是否纠正已经测到的错误；
2. 外部证据有多强；
3. 和原则是否相容；
4. 依赖关系。

认回和共用数据块排在代理前面，还有一个原因：代理按对象做预检查，如果对象的身份和单位是错的，预检查拦下的就是错的对象。

| 顺序 | 做什么 | 解决 | 外部依据 | 原则 | 现状和代价 |
|---|---|---|---|---|---|
| 1 | 共用数据块升为账本单位：面按"拥有者 + 属性路径"记；恢复时原地写回；AI 一步里新建的材质实例记为派生改动 | 问题三，也减少问题二的 `.001` | Concert、USD、Multi-user、Asset Pipeline、Unity `Renderer.material` | 都符合；P4 要求从反射数据推出"引用" | 新工作，要改面的键和恢复路径 |
| 2 | 认回加固后默认打开（具体内容见表下） | 问题二 | USD、Omniverse、React、Unity 未使用的覆盖、FreeCAD、Bidarra | 符合；有歧义时不让人选（P6） | 原型已有；是第 12.10 节第 1、2 项的扩展 |
| 3 | 告知里加上"失效的事实"和"人的值就是新目标"；在账本上做循环检测，超过阈值就提醒人 | 问题一的派生改动、意图冲突、活锁 | RIR、K8s 409、Flux Adopt、Claude Code #33856 | 符合（P8）；不是安全手段 | 小，在 R22 上加 |
| 4 | MCP 代理（具体内容见表下） | 问题四，以及问题一 | 网关市场、SEP-1303、SEP-2848、代码模式 | 关键约束是 P4、P6 | 已规划（第 12.10 节第 4 项），依赖能力探测 |
| 5 | 缩小恢复不了的范围（具体内容见表下） | 问题一、问题二 | Atomix、GoEx、Smart Locks、Unity 文档 | 符合 | 已规划为第 4 步；预制体部分是新增 |
| 6 | 影子执行（试验）：先在 Blender 无界面实例里跑脚本，按面取差异，再按许可经方式二的写入路径写进真场景 | 问题一 | Terraform、Cordon、Atomix | 符合 P3、P6；失去原子性 | 新工作，先量代价 |
| 7 | 可选功能：AI 声明意图条件或"全有或全无"；给人看 AI 的预留和计划目标；把预留以工具形式提供给 AI 读 | 问题一的意图冲突；降低碰撞率 | ATR、Magentic-UI、Unity Studio、AgentRoom | 依赖 AI 配合的部分只用于效率 | 小到中 |
| 8 | 标准层面：向 SEP-1862 提议"作用范围"输出；向 SEP-2848 提供 TOCTOU 用例 | 问题四 | Tool Annotations 兴趣组章程 | 不依赖它们（P3） | 小 |

第 2、4、5 项的具体内容：

- **第 2 项，认回加固：**
  - 在父对象内从上往下配对；
  - 先比完整名字，再比去后缀的名字，并且只接受一对一；
  - 去后缀的模式靠探测学出；
  - 孤儿跨执行保留，出现匹配时重新绑定；
  - 人删除的对象记墓碑（第 12.5 节）；
  - 维护改名表；
  - 由运行时核对配对结果，并记录置信等级。
- **第 4 项，MCP 代理：**
  - 前后两个钩子；
  - 参数映射靠 schema 启发加探测；
  - 只对能力表里恢复不了的类别"失败即拒"；
  - 被拒时以 `isError` 结构化返回；
  - 元工具和代码一律走事后核对；
  - 等对象释放时用 Tasks 句柄。
- **第 5 项，缩小恢复不了的范围：**
  - 用能力探测得出效果分类；
  - Unity 删除前做备份，预制体实例按 `GlobalObjectId` 重新实例化；
  - Unity 的编号改用 `GlobalObjectId` 加 `GetEntityId`。

**第 1 项排在最前**，有三个理由：它修的是实验 H 已经测到的材质分叉；所有多人 DCC 系统都这么做，证据最强；它也是认回能处理数据块同名（`LeafMat.001`）的前提。

**第 2 项只是在已有原型上补规则**，代价小，收益直接：清空重建是真实 AI 最常见的写法之一，不认回，AI 一遇到人的修改就会被暂停。

**第 3 项几乎不花成本。** 它对应的是 AgentRewind 和 Claude Code 死循环这两类证据：AI 的认知和世界错位，比不回滚还糟。

**第 4 项是最大的一块架构工作**，依赖第 5 项里的能力探测。

**第 6 项是对任意代码真正做到"事先阻止"的唯一路线**，但成本没人量过，所以只作试验。Unity 开第二个编辑器实例代价很高，应该先在 Blender 上试。

**明确不建议照搬的做法：**

- **默认审批**：例如 Unity AI Assistant 的 Ask Permission ([Unity AI Assistant](https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.11/manual/reference/preference.html))、Roblox 的 Build、Magentic-UI 的"总是"类、TrueFoundry 的挂起。违反 P6。
- **让人在 AI 工作时停手**：例如 Unreal MCP 的串行执行；BlockNote 在 AI 生成和审阅期间把编辑器设为不可编辑 ([BlockNote](https://raw.githubusercontent.com/TypeCellOS/BlockNote/main/packages/xl-ai/src/AIExtension.ts))。违背"同时编辑"的目标本身。
- **建议模式**：例如 Google Docs 里 Gemini 的私有建议 ([Google Workspace](https://workspaceupdates.googleblog.com/2026/04/new-gemini-capabilities-in-google-docs-help-you-go-from-blank-page-to-brilliance.html))、Notion 的逐条批准 ([Notion](https://www.notion.com/releases/2026-08-28))、Tiptap 的修订标记 ([Tiptap](https://tiptap.dev/docs/content-ai/capabilities/server-ai-toolkit/agents/tracked-changes.md))。它们把 AI 的每一处修改都变成人的审批，同样违反 P6。
- **过期就整步拒绝**：在 76%–100% 的碰撞率下，AI 会一直提交不了。
- **把注解或提示词当保证**：违反 P2。
- **按创建历史命名**：不适用于 AI 的重建。
- **对称的"最后写入者赢"**：例如 Omniverse，以及 Blend Buddies 的"最后到达的修改赢" ([Blend Buddies](https://superhivemarket.com/products/blendbuddies/docs))。违反 P5。
- **恢复整份文件的检查点**：会连人的修改一起撤掉。`related_work.md` 记录的 Unity AI Assistant 检查点就是这样。

## 目标完全相同的项目：没有找到，窗口在收窄

**截至 2026-10-08，没有找到目标完全相同的项目。** 也就是说，没有任何论文、协议、开源项目或产品同时做到：人和 AI 编辑同一份实时应用状态；不改应用；由运行时按属性强制人的修改优先；并核对应用的实际状态。

这和 2026-09-28 那轮相关工作调研的结论一致。那里列出的 TClone、Agent-Integrated Software、Fluid Framework、CLEO、amicode，仍然是各自维度上最接近的。这一轮新发现的部分匹配，也都缺了关键的一块：

- **Doop**：开源的多人设计画布，AI 通过 MCP 加入，有自己的名字和光标，人和 AI 的修改都走同一个变更层。但应用是专门为此构建的，人和 AI 对等，没写优先级。报道来自 PyShine 和 Enterprise DNA，仓库没有打开 ([PyShine](https://pyshine.com/Doop-A-Multiplayer-Design-Canvas-Where-Agents-Design-Too/)；[Enterprise DNA](https://enterprisedna.co/resources/ai-pulse/ai-pulse-2026-08-23-a-multiplayer-canvas-lets-agents-design-live-alongside-human))。
- **ATR**：有选择性重新验证，但没有人的角色 ([ATR](https://arxiv.org/abs/2609.08015))。
- **Claude Code #71879**：请求用 diff3 把 AI 待定的编辑变基到外部改动上。只针对文本，双方对称 ([GitHub](https://github.com/anthropics/claude-code/issues/71879))。
- **Lehmann 等人的"多人加 AI"写作系统**：把 AI 的输出限制为评论 ([arXiv](https://arxiv.org/abs/2509.11826))。

**机制上最接近的，都来自"人和人"或"自动化和自动化"的场景。**

- **Unreal 的 Multi-User Replication** 让每个 actor 属性恰好有一个作者客户端，可以按属性改派 ([Unreal Multi-User Replication](https://dev.epicgames.com/documentation/en-us/unreal-engine/multi-user-replication-in-unreal-engine))。这是"按面所有权"在引擎里已发布的版本。
- **Kubernetes SSA** 是最完整的多写者字段所有权模型，但默认让自动化赢：人用 `kubectl edit` 做的修改总能写入，控制器按文档建议"总是强制"，下一次调和时就悄悄收回 ([K8s SSA](https://kubernetes.io/docs/reference/using-api/server-side-apply/))。Flux 默认在下一次调和时把 kubectl 的修改"全部撤回" ([Flux](https://fluxcd.io/flux/components/kustomize/kustomizations/))；Argo CD 开启自愈后，5 秒内重新同步 ([Argo CD](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/))。**RightOfWay 把这个默认反了过来**，调研范围内没有发现别人这样做。
- **Zed** 是使用场景最接近的产品：每处修改都标注 `ChangeAuthor::Agent` 或 `ChangeAuthor::User`；人在 AI 修改范围之外的编辑自动并入基线，重叠的标为冲突。但它只处理文本，靠人审阅；拒绝 AI 的修改时，会把重叠范围里人的文字一起覆盖 ([Zed action_log.rs](https://github.com/zed-industries/zed/blob/main/crates/action_log/src/action_log.rs))。
- **Figma 的 Agent** "和团队在同一个文件里"工作，但公开文档里没有任何关于并发、归属或冲突的规则 ([Figma](https://www.figma.com/blog/the-figma-agent-is-here/))。

**窗口在收窄。**

- Roblox 已宣布并行 Agent 和云端 Agent ([Roblox newsroom](https://about.roblox.com/newsroom/2026/04/roblox-studio-going-agentic))。
- Spline V2 和 Unreal 5.8 允许外部 Agent 驱动编辑器 ([Spline](https://blog.spline.design/spline-v2))。
- Figma 的 Agent 已经进入多人文件。

这些产品很快会在规模上遇到人和 AI 同时修改的问题。MCP 标准的注意力（SEP-2848、SEP-1862）目前还停在调用这一层，没有到对象这一层。

## 结论

四个问题其实有同一个根源：RightOfWay 对世界的建模，建立在真实 AI 和真实应用都会打破的几个假设上——AI 一步只改少数几处、对象身份稳定、保护的单位是物体、工具调用是类型化的。借来的修法几乎都是**让 RightOfWay 的模型贴合应用自己的模型**：按拥有者记面，按名字认身份，用探测学参数映射，用测量得出效果分类。它们都不是靠加更多关卡。原因很简单：关卡要么需要人批准（违反 P6），要么看不进脚本内部（受 P3 限制）。

调研也换了一个角度看 RightOfWay 的差异：真正稀缺的不是"拦截"，而是**"核对实际状态"**。

- 网关只判断请求；
- 多人 DCC 系统相信自己的复制——Unreal 的 UE-195262 显示，两个客户端可以永久停在不同状态，会话从不调和 ([UE-195262](https://issues.unrealengine.com/issue/UE-195262))；
- 编程 Agent 的检查点按文件恢复。

GitOps 的漂移检测最接近"核对实际状态"，但它是拿实际状态去比"期望状态"，然后改回去，并不记录谁写了什么。"账本 = 场景"这条带出处的不变量，在调研到的 AI 工具和多人编辑系统里没有对应物。既然行业在往写代码的方向走，按工具的预检查覆盖面会越来越小，长期的价值会落在三处：事后核对和如实报告、身份和单位的建模、缩小恢复不了的范围。影子执行是唯一可能把代码的"事后"变成"事前"的实验，值得在 Blender 上做一次。

最后，调研没有找到任何"人和 AI 同时编辑"的公开基准。可以把几类指标组合起来：

- Atomix 和 Cordon 的泄漏效果、重复效果和残留改动；
- MUSE 的保留率和意外改动率；
- RightOfWay 自己的碰撞率、违规率和暂停率。

用它们定义一个基准，这本身就是可以发表的贡献。

## 实施情况（2026-10-09）

按"建议优先级"的顺序做了第 1–6 项，细节和数字见 `design_v0.5.md` 第 13 节。

| 顺序 | 建议 | 做了什么 | 还没做 |
|---|---|---|---|
| 1 | 共用数据块升为账本单位 | 方式一里对象引用的数据块各自是一个单元，恢复写回同一个数据块；复制共用数据块、人改过的数据块少了使用者，告诉 AI（13.1、13.4） | 实时同步、文件兜底仍按对象；Unity |
| 2 | 认回加固后默认打开 | 父对象内从上往下、先完整名字再去后缀、一对一；后缀由探测学出；孤儿（之前想删、被保留的）跨执行重新绑定；墓碑；改名表（编号映射）；运行时重算核对，记录置信等级；默认打开（13.2、13.3） | Unity 接入；撤销一次认回；恢复不了删除时的跨执行认回 |
| 3 | 失效的事实、新的目标、循环检测 | 每个面一条冲突记录；"以现在的值为准"；循环检测提醒人（13.4） | — |
| 4 | MCP 代理 | 执行代码的工具走原子执行；类型化工具按参数里的对象名预检查、只对恢复不了的失败即拒、其余前后核对；`isError` 加结构化结果；和真的 BlenderMCP 服务器联调（13.5） | MCP Tasks 挂起；Unity；按工具探测参数映射 |
| 5 | 缩小恢复不了的范围 | 能力探测：在临时场景里实测按面恢复、恢复删除、包住工具调用（13.6） | Unity 隐藏备份、预制体、`GlobalObjectId` |
| 6 | 影子执行（试验） | 两个 bpy 进程上量了：结局和直接执行一样，每步慢 4–5 倍（13.7） | — |
| 7、8 | AI 声明意图；标准提议 | 按计划不写代码 | — |
