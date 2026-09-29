# 实验的详细步骤

这里是 README 里"跑全部实验"的展开。所有命令都在仓库根目录运行。Windows 上把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

## 在 VS Code 里打开（Windows）

1. 安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"。
2. 在 VS Code 里用"文件 → 打开文件夹"打开仓库。
3. 右下角弹出"安装推荐扩展"时点安装。
4. `Ctrl+Shift+P` → `Tasks: Run Task` → "初始化环境（创建 .venv 并安装依赖）"。
5. `Ctrl+Shift+P` → `Python: Select Interpreter`，选 `.venv` 里的 Python。
6. 左侧烧瓶图标 → 运行全部测试。应该没有失败，有一些会跳过：需要 `import bpy` 的（要 Python 3.11 + bpy）、检查 C# 语法的（要 tree-sitter）、连接 MCP 中继的（要 `pip install mcp`）。

左侧"运行和调试"的下拉框里有每个实验的模拟版和真人版。

## 准备（只做一次）

1. **Blender 4.2 LTS 或更高**（推荐 4.5 LTS）。
2. **现成的 Blender MCP 插件**（[ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)，插件名 "MCP for Blender"）：
   - 在 GitHub 仓库里打开 `addon.py`，点右上角的下载按钮（Download raw file）。
   - Blender → 编辑 → 偏好设置 → 插件 → 右上角下拉箭头 → 从磁盘安装 → 选 `addon.py`，然后勾选启用。
   - 每次打开 Blender 后，在 3D 视图里按 `N` 打开侧栏 → "BlenderMCP" 标签 → "Start MCP Server"（默认端口 9876）。
   - 实验程序直接连这个插件，不需要 uv，也不需要配置 Claude Desktop。实验期间不要同时让别的 AI 操作 Blender。
   - 这个插件会执行发到本机端口的任何 Python 代码，不做实验时建议关掉服务器。
3. **实时同步（实验 B）要第二个 Blender**：再开一个 Blender，在 BlenderMCP 面板里把 Port 改成 **9877**，再点 Start MCP Server。
4. **文件兜底要能找到 `blender.exe`**：会在 `C:\Program Files\Blender Foundation\` 和各个盘的 `SteamLibrary\steamapps\common\Blender\` 下自动找；找不到时设置环境变量 `BLENDER_EXE`，或者运行时加 `--blender "路径"`。
5. **Unity（实验 C）**：Unity 6，项目里装了 AI Assistant 包（com.unity.ai.assistant）；`pip install -e ".[dev,unity]"`。

## 一次跑全部

```bash
python -m experiments.run_all                  # 人的操作由程序模拟
python -m experiments.run_all --human real     # 真人操作，每一项开始前提示，输入 s 跳过
python -m experiments.run_all --only A,D       # 只跑某几组
python -m experiments.run_all --quick          # 每组只跑主要的一种
```

| 组 | 内容 | 要准备的 |
|---|---|---|
| T | 单元测试 | 无 |
| A | 同一份：按面、按对象、对照组、Agent 不先看提示、选中即占用 | Blender（9876） |
| D | 两个 Agent + 一个人：有保护、对照组 | Blender（9876） |
| B | 各自一个窗口，实时同步：按面、按对象、对照组 | 两个 Blender（9876、9877） |
| F | 文件兜底：按面、按对象（每种自带两个对照） | 装了 Blender |
| C | Unity 同一份：按面、按对象、对照组 | Unity + `.[unity]` |

- 没准备好的组会跳过并说明原因。
- 旧的 `experiments/results/results.csv` 会改名备份，这次的结果写进新的；加 `--keep-results` 就接着追加。
- 最后打印汇总表并逐行判定：有保护的行，"人的修改被覆盖""AI 的修改丢失""出错"都应该是 0；对照组的行至少有一个不是 0。
- "Agent 不先看提示"那一行，"被删对象被重建"是 1 属于预期：Agent 没读运行时的提示，不知道 Rock_2 是人故意删的。

没有 Blender 界面的地方（比如云端、持续集成），可以用 pip 装的 bpy 代替：

```bash
python -m experiments.run_all --a-backend inprocess --b-backend local --blender inprocess
```

## 单独运行

```bash
python -m experiments.exp_a_shared --human sim     # 实验 A：同一份
python -m experiments.exp_d_multi --human sim      # 实验 D：两个 Agent + 人
python -m experiments.exp_b_live --human sim       # 实验 B：各自一个窗口，实时同步
python -m experiments.exp_b_files --human sim      # 实验 B：文件兜底
python -m experiments.exp_c_unity --human sim      # 实验 C：Unity
```

去掉 `--human sim` 就是真人操作；加 `--no-protocol` 是对照组；加 `--granularity object` 是按整个对象判断。

- 实验 A、D、B 会在 Blender 里**新建一个场景**"RightOfWay实验_…"，不影响原来的场景。开始前会删掉之前实验留下的"RightOfWay实验_"场景（实验 A 加 `--keep-old-scenes` 可以保留）。
- 实验 C 会在 Unity 里**追加**一个空场景，原来的场景不受影响，也不保存任何东西。程序会自己启动 Unity 的 MCP 中继；连不上时，先把其他正在使用 Unity MCP 的客户端（例如 Claude Desktop）停掉再试。
- 实验 C 每次检查人的修改都要在 Unity 里编译一段 C#，所以默认 2 秒检查一次。

## 手动检查（只有在界面里才能确认）

1. 实验 A 等你操作时，在**编辑模式**里改网格（Tab 进入、挪几个点），终端会不会打印"发现人的修改"。
2. 实验 A 的 Agent 第 2 步、实验 B 的每次同步之后，按一次 **Ctrl+Z**，撤掉的是 Agent 的整段修改，还是你自己的上一步。
3. 用 **Shift+D** 复制一个对象，终端是不是报告"新建"。
4. 实验 A 加 `--occupy-selection` 跑真人版，最后按提示选中 Leaf_1，看 Agent 是不是没动它。
5. 在一个大项目里跑一次实验 A（真人），看轮询会不会让 Blender 卡顿；卡的话加 `--poll 2`。

结果和观察可以记在 `experiments/results/results.csv` 的"备注"列里（这个文件不进 git）。

## 规则和代码的对应

| 规则 | 内容 | 代码位置 |
|---|---|---|
| R1 | 身份由运行时决定 | `runtime.py`：`register_actor`、`submit`、`_require_human` |
| R2 | 所有修改经过运行时 | `runtime.py`：`human_edit`、`report_observed_change`、`external_change` |
| R3、R4 | 基准版本（这个 Agent 的视图）；过时写入作废 | `runtime.py`：`_precheck`、`_finalize`；`shared_session.py`：`run_agent`；`live_sync.py`：`sync` |
| R5 | 人的修改总能提交 | `runtime.py`：`human_edit` |
| R6 | 人碰过的部分 Agent 不再改 | `runtime.py`；`blender_side.py`：`run_agent` 的恢复步骤、`apply_sync` 的核对 |
| R7 | 交还 | `runtime.py`：`hand_back`；`shared_session.py`：`hand_back` |
| R8–R10 | 占用、阻塞、明确释放；选中即占用（选项） | `runtime.py`：`human_edit(occupy=...)`、`release`；`shared_session.py`：`occupy_selection` |
| R11–R17 | 认领、失效、集合、撤回、版本保留、确认、暂停 | `runtime.py` |
| R18 | 观察粒度、值的文字 | `blender_side.py`：`records`、`object_values`；`UnitySide.cs.txt`：`Records`、`Show` |
| R19、R20 | 违反契约、变化日志 | `runtime.py` |
| R21 | 权威副本；旧文件不覆盖新内容 | `runtime.py`；`filemerge.py`：`_base`、`infer_base` |
| R22 | 告知 Agent 它错过的变化，带值 | `shared_session.py`：`observe`、`run_agent`；`changes.py` |
| R23 | 多个 Agent：后到的让先到的，预留 | `shared_session.py`：`run_agent`、`protected(for_agent=...)` |
