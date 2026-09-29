# Cowork Runtime：人机同时协作协议的参考实现

这是《人机同时协作协议》（暂名 Cowork Protocol）的参考实现，当前对应规范 **v0.4**（`spec/spec_v0.4.md`）。
第一次看，先读 `docs/overview.md`：用最少的术语把整个协议从头讲一遍。

- `cowork/`：运行时本体，纯 Python 实现规范第 6 节的规则。没有第三方依赖，只有测试需要 `pytest`。
- `cowork/blender/`：**Blender 接入**。不修改 Blender，也不修改它现成的 MCP 插件。
- `cowork/unity/`：**Unity 接入**。不修改 Unity，也不修改 Unity 自带的 MCP。运行时一侧和 Blender 用的是同一份代码。
- `experiments/`：实验 A、B、C、D，和一键重跑。

## v0.4 改了什么

1. **两种"上次看到的"分开记**：运行时的（判断哪些变化是人做的）和每个 AI 的（它上次看到的样子）。AI 每次读场景或执行，都会被告知它错过了什么，**带具体的值**：

   ```
   你上次看场景之后，发生了这些变化：
   - 人 删除了 Rock_2
   - 人 修改了 Leaf_3：Location (-0.647, 0.47, 1.4) → (-0.647, 0.47, 2.2)
   ```
   值的文字按 Blender、Unity 自己的类型信息自动生成，不需要为应用写任何定义。
2. **多个 AI**：人优先于所有 AI；AI 之间先提交的生效，后到的那个按旧印象改到了别人刚改过的地方，这部分不生效，告诉它现在是多少，并给它 30 秒预留重试。
3. **选中即占用**（可选，默认关）：你选中的对象 AI 不能动，取消选中就解除。另外，AI 的脚本不会再改变你的选中状态。
4. **方式二改成"各自一个窗口，实时同步"**：AI 用 computer use 时在自己的 Blender 里改自己的一份，但两个 Blender 都通过 MCP 连着运行时，**不用存盘，也不用"文件 → 恢复"**。
5. **文件合并降为兜底**，只给没有接口的应用用；合并时把"第几版"写进文件，存盘时直接读，读不到再推断。

| | 方式一：同一份 | 方式二：各自一个窗口 | 文件兜底 |
|---|---|---|---|
| AI 怎么操作 | 发脚本（MCP） | 点鼠标（computer use） | 点鼠标，应用没有接口 |
| 什么时候检查规则 | AI 每执行一段脚本 | AI 每做完一步，同步一次 | 任何一方存盘 |
| 你要做什么 | 什么都不用 | 什么都不用 | 反复存盘，点"文件 → 恢复" |
| 代码 | `shared_session.py` | `live_sync.py` | `filemerge.py` |

## 目录

```
cowork-runtime/
├── cowork/                    运行时本体
│   ├── model.py               数据模型（规范第 5 节）
│   ├── runtime.py             规则 R1–R21（规范第 6 节）
│   ├── merge.py / membership.py
│   ├── blender/               Blender 接入（规范第 9 节）
│   │   ├── blender_side.py    在 Blender 里运行的代码：编号、指纹、值、保护/恢复、同步、合并
│   │   ├── bridge.py          怎么把代码送进 Blender：插件 socket / 本进程 bpy / 无界面 blender.exe
│   │   ├── shared_session.py  方式一：同一份。每个 AI 的视图、告知（R22）、多个 AI（R23）、选中即占用
│   │   ├── live_sync.py       方式二：各自一个窗口，实时同步
│   │   ├── filemerge.py       文件兜底：存盘后按对象合并，起点编号 + 推断
│   │   ├── changes.py         把变化写成带值的文字（不认识任何面的名字）
│   │   ├── local_server.py    用 pip 装的 bpy 冒充"开着 MCP 插件的 Blender"（云端测试用）
│   │   ├── records.py         比较两次"列出对象"的结果
│   │   └── evaluate.py        实验指标
│   └── unity/                 Unity 接入
│       ├── UnitySide.cs.txt   在 Unity 里运行的 C#：面和值来自 Unity 自己的序列化
│       ├── bridge.py          生成 C#、解析结果、连接 Unity 的 MCP 中继
│       └── selftest.py        把整个实验拼成一段 C#，在 Unity 里一次跑完
├── experiments/
│   ├── scenario_tree.py       实验任务（Blender）
│   ├── scenario_unity.py      同一个任务的 Unity 版（C#）
│   ├── exp_a_shared.py        实验 A：方式一，同一份
│   ├── exp_d_multi.py         实验 D：两个 AI + 一个人，同一份
│   ├── exp_b_live.py          实验 B：方式二，各自一个窗口，实时同步
│   ├── exp_b_files.py         实验 B（文件兜底）：存盘后合并
│   ├── exp_c_unity.py         实验 C：Unity，同一份
│   ├── run_all.py             一次重跑全部测试和实验，最后汇总
│   └── results/results.csv    每次运行追加一行（Excel 可以直接打开）
├── tests/                     96 个自动测试
├── examples/demo_game_assets.py  带中文解说的运行时演示（不需要 Blender）
├── docs/overview.md           从头梳理（入门）
├── spec/spec_v0.4.md          协议规范（v0.2、v0.3 保留作对照）
├── spec/related_work.md       相关工作对照
└── scenarios/scenarios_v0.md  测试场景
```

## 在 VS Code 里打开（Windows）

1. 安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"。
2. 解压这个文件夹，在 VS Code 里用"文件 → 打开文件夹"打开它。
3. 右下角弹出"安装推荐扩展"时点安装。
4. 按 `Ctrl+Shift+P` → `Tasks: Run Task` → "初始化环境（创建 .venv 并安装依赖）"。
5. 按 `Ctrl+Shift+P` → `Python: Select Interpreter`，选 `.venv` 里的 Python。
6. 左侧烧瓶图标 → 运行全部测试。你的电脑上应该**没有失败**，有一些会跳过：需要 `import bpy` 的那组（要 Python 3.11 + bpy）、检查 C# 语法的那组（要 tree-sitter）、连接 MCP 中继的那个（要 `pip install mcp`）。这些在云端都跑过，全部 96 个通过。

## 跑实验

### 准备（只做一次）

1. **安装 Blender 4.2 LTS 或更高**（推荐 4.5 LTS）。
2. **安装现成的 Blender MCP 插件**（[ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)，插件名 "MCP for Blender"）：
   - 在 GitHub 仓库里打开 `addon.py`，点右上角的下载按钮（Download raw file）。
   - Blender → 编辑 → 偏好设置 → 插件 → 右上角下拉箭头 → 从磁盘安装 → 选 `addon.py`。
   - 在插件列表里搜 "MCP for Blender"，勾选启用。搜不到就重启 Blender。
   - 每次重启 Blender 后，在 3D 视图里按 `N` 打开侧栏 → "BlenderMCP" 标签 → 点 "Start MCP Server"。
   - 不需要安装 uv，也不需要配置 Claude Desktop：实验程序直接连这个插件。实验期间不要同时让 Claude 操作 Blender。
   - 这个插件会执行发到本机端口的任何 Python 代码，不做实验时建议关掉服务器。
3. **实验 B（实时同步）要开两个 Blender**：
   - 第一个是你的，端口保持 9876，点 Start MCP Server。
   - 再开一个 Blender（代表 AI 的屏幕），在 BlenderMCP 面板里把 **Port 改成 9877**，再点 Start MCP Server。如果你的插件版本面板里没有 Port，把报错发给我。
4. **文件兜底要能找到 `blender.exe`**。程序会在 `C:\Program Files\Blender Foundation\` 和各个盘的 `SteamLibrary\steamapps\common\Blender\` 下自动找；找不到时设置环境变量 `BLENDER_EXE`，或者运行时加 `--blender "路径"`。
5. **实验 C（Unity）**：Unity 6 装了 AI Assistant 包（com.unity.ai.assistant）；`.venv\Scripts\python -m pip install -e ".[dev,unity]"`。

### 一次重跑全部

```powershell
.venv\Scripts\python -m experiments.run_all                  # 全部实验，人的操作由程序模拟
.venv\Scripts\python -m experiments.run_all --human real     # 真人操作，每一项开始前提示，输入 s 跳过
.venv\Scripts\python -m experiments.run_all --only A,D       # 只跑某几组：T 测试、A、B、C、D、F
.venv\Scripts\python -m experiments.run_all --quick          # 每组只跑主要的一种
```

| 组 | 实验 | 要准备的 |
|---|---|---|
| T | 单元测试 | 无 |
| A | 同一份：按面、按对象、对照组、AI 不先看提示、选中即占用 | Blender（9876） |
| D | 两个 AI + 一个人：有保护、对照组 | Blender（9876） |
| B | 各自一个窗口，实时同步：按面、按对象、对照组 | 两个 Blender（9876 和 9877） |
| F | 文件兜底：按面、按对象（每种自带两个对照） | 装了 Blender 就行 |
| C | Unity 同一份：按面、按对象、对照组 | Unity + `.[unity]` |

- 没准备好的组会跳过并说明原因。
- 旧的 `results.csv` 会改名备份，这次的结果写进新的 `results.csv`；加 `--keep-results` 就接着追加。
- 最后打印汇总表并逐行判定：有保护的行，"人的修改被覆盖""AI 的修改丢失""出错"都应该是 0；对照组的行至少有一个不是 0。
- "AI 不先看提示"那一行，"AI 重建被删对象"是 1 属于预期：AI 没读运行时的提示，不知道 Rock_2 是你故意删的。读了提示的 AI（默认）不会再补回来。

云端（无界面 bpy）跑出来的结果：

| 实验 | 做法 | 人的修改被覆盖 | AI 的修改丢失 | AI 重建被删对象 |
|---|---|---|---|---|
| A 同一份 | 有保护（按面 / 按对象 / 选中即占用） | 0 | 0 | 0 |
| A 同一份 | 对照组 | 2 | 0 | 1 |
| D 两个 AI + 人 | 有保护 | 0 | 0 | 0 |
| D 两个 AI + 人 | 对照组 | 1 | 1 | 1 |
| B 实时同步 | 按面 / 按对象 | 0 | 0 | 0 |
| B 实时同步 | 对照组：谁后同步谁生效 | 1 | 0 | 0 |
| B 文件兜底 | 合并 | 0 | 0 | 0 |
| B 文件兜底 | 对照：AI 最后存盘 / 人最后存盘 | 3 / 0 | 0 / 11（按对象 6） | — |

### 单独运行

VS Code 左侧"运行和调试"的下拉框里有每个实验的模拟版和真人版；也可以直接运行：

```powershell
.venv\Scripts\python -m experiments.exp_a_shared --human sim
.venv\Scripts\python -m experiments.exp_d_multi --human sim
.venv\Scripts\python -m experiments.exp_b_live --human sim
.venv\Scripts\python -m experiments.exp_b_files --human sim
.venv\Scripts\python -m experiments.exp_c_unity --human sim
```

去掉 `--human sim` 就是真人操作；加 `--no-protocol` 是对照组。

- 实验 A、D、B 会在 Blender 里**新建一个场景**"Cowork实验_…"，不影响你原来的场景。开始前会**删掉之前实验留下的"Cowork实验_"场景**。不想删就加 `--keep-old-scenes`（实验 A）。
- 实验 C 会在 Unity 里**追加**一个空场景，你原来的场景不受影响，也不会保存任何东西。程序会自己启动 Unity 的 MCP 中继；连不上时，先在 Claude 桌面版里把 unity-mcp 停掉再试。
- 粒度：默认"按面"。加 `--granularity object` 可以换成"整个对象"做对比。

### 顺便手动检查这几项，记在结果表的"备注"里

1. 实验 A 等你操作时，在**编辑模式**里改网格（Tab 进入、挪几个点），终端会不会打印"发现人的修改"。
2. 实验 A 的 AI 第 2 步、实验 B 的每次同步之后，按一次 **Ctrl+Z**，撤掉的是 AI 的整段修改，还是你自己的上一步。
3. 用 **Shift+D** 复制一个对象，终端是不是报告"新建"。
4. 实验 A 加 `--occupy-selection` 跑真人版：最后按提示选中 Leaf_1，看 AI 是不是没动它。
5. 在你自己的大项目里跑一次实验 A（真人），看轮询会不会让 Blender 卡顿；卡的话加 `--poll 2`。

## 已经验证的和还没验证的

**已验证**（云端，bpy 4.5.4；实验 B 用两个独立的 bpy 进程，通过和插件相同的 socket 协议连接）：
- 方式一：AI 被告知它错过的变化和具体的值；读了提示后不再补回人删掉的对象；人的修改被覆盖 0 处。
- 两个 AI：后到的 AI 改树干、Rock_1 被拦下并被告知现在的值；先到的 AI 在预留期间改不了；重试后生效。对照组里一个 AI 悄悄覆盖了另一个的修改。
- 选中即占用：选中的对象不被改，取消选中即解除；AI 的脚本改了选中状态会被还原（包括对象被整个换回之后）。
- 方式二实时同步：三次同步后两边完全一致；用临时文件和 base64 两种方式传对象都通过；对照组"谁后同步谁生效"会覆盖人的 1 处修改。
- 文件兜底：起点编号和推断的结果一致；人打开新版本后故意把 AI 的修改改回去，有编号时能正确识别（v0.3 的未决问题 7）。

**已验证**（你的 Unity 6.4，通过 Claude 的 Unity MCP 自检）：
- v0.3：Leaf_3 保留人的高度、颜色变成 AI 的秋色；Cube 保持人放的高度；实验场景以外没有改动。
- v0.4：值的文字（例如 `m_LocalPosition: (-0.647, 3.4, 0.47)`）只对变了的面返回；选中即占用：选中的 Leaf_1 位置没被 AI 改，但共用材质的颜色照常变成秋色；AI 读了提示后没有补回 Rock_2；自检结束后恢复了你原来的选中状态。追踪 57 个对象，AI 一步 0.07 秒。

**还没验证**：
- 真实 Blender 界面：两个 Blender 的实时同步、编辑模式、Ctrl+Z、大场景性能。
- 实验 C 通过 MCP 中继从 Python 连接 Unity（Unity 这一侧的代码已经在真实 Unity 里跑过）。
- 真正的大模型和真正的 computer use（下一级实验）。

## 建议的阅读顺序

1. `docs/overview.md`。
2. 跑一遍 `examples/demo_game_assets.py`，看运行时的整体效果。
3. 读 `cowork/model.py`、`cowork/runtime.py`（先看 `submit`、`_finalize`、`human_edit`）。
4. 读 `cowork/blender/shared_session.py` 的 `run_agent()`，对照 `blender_side.py` 的 `run_agent()`。
5. 读 `cowork/blender/live_sync.py` 的 `sync()`。

## 规则和代码的对应

| 规则 | 内容 | 代码位置 |
|---|---|---|
| R1 | 身份由运行时决定 | `runtime.py`：`register_actor`、`submit`、`_require_human` |
| R2 | 所有修改经过运行时 | `runtime.py`：`human_edit`、`report_observed_change`、`external_change` |
| R3、R4 | 基准版本（这个 AI 的视图）；过时写入作废；写集事后确定时按对象检查 | `runtime.py`：`_precheck`、`_finalize`；`shared_session.py`：`run_agent`；`live_sync.py`：`sync` |
| R5 | 人的修改总能提交 | `runtime.py`：`human_edit` |
| R6 | 人碰过的部分 AI 不再改 | `runtime.py`；`blender_side.py`：`run_agent` 的恢复步骤、`apply_sync` 的核对 |
| R7 | 交还 | `runtime.py`：`hand_back`；`shared_session.py`：`hand_back` |
| R8–R10 | 占用、阻塞、明确释放；选中即占用（选项） | `runtime.py`：`human_edit(occupy=...)`、`release`；`shared_session.py`：`occupy_selection` |
| R11–R17 | 认领、失效、集合、撤回、版本保留、确认、暂停 | `runtime.py` |
| R18 | 观察粒度、值的文字 | `blender_side.py`：`records`、`object_values`；`UnitySide.cs.txt`：`Records`、`Show` |
| R19、R20 | 违反契约、变化日志 | `runtime.py` |
| R21 | 权威副本；旧文件不覆盖新内容 | `runtime.py`；`filemerge.py`：`_base`、`infer_base` |
| R22 | 告知 AI 它错过的变化，带值 | `shared_session.py`：`observe`、`run_agent`；`changes.py` |
| R23 | 多个 AI：后到的让先到的，预留 | `shared_session.py`：`run_agent`、`protected(for_agent=...)` |

## 还没做的

- 运行时的 MCP 代理服务器：接真实大模型时需要。AI 连到它，它再转给 Blender MCP；AI 的读取请求经过它时，就能按 R22 告知。
- 真正的 computer use（AI 在虚拟机里操作自己的 Blender），接到实时同步上。
- 通过 AG-UI 对接界面。
- Unity 的方式二（同一个 Unity 项目不能同时在两个编辑器里打开，要先复制项目）。
