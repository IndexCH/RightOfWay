# 实验的详细步骤

这里是 README 里"跑全部实验"的展开。所有命令都在仓库根目录运行。Windows 上把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

## 在 VS Code 里打开（Windows）

1. 安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"。
2. 在 VS Code 里用"文件 → 打开文件夹"打开仓库。
3. 右下角弹出"安装推荐扩展"时点安装。
4. `Ctrl+Shift+P` → `Tasks: Run Task` → "初始化环境（创建 .venv 并安装依赖）"。
5. `Ctrl+Shift+P` → `Python: Select Interpreter`，选 `.venv` 里的 Python。
6. 左侧烧瓶图标 → 运行全部测试。应该没有失败，有一些会跳过：需要 `import bpy` 的（要 Python 3.11 + bpy）、检查 C# 语法的（要 tree-sitter）、MCP 代理和连接 MCP 中继的（要 `pip install -e ".[proxy]"`，初始化任务已经装了）、接真的 BlenderMCP 服务器的那一个（要设环境变量 `RIGHTOFWAY_BLENDER_MCP`）。

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
| E | AI 删掉人刚改过的对象：模拟 Unity 接入、Blender 按面、Blender 按对象、Unity | 模拟那一项不用准备；其余同 A、C |
| H | 按真实 AI 的写法重跑 A、D、E（整体调整、清空重建、类型化批量命令）；人改一处撞上的概率；不认回同一个对象的对照 | 模拟那几项不用准备；Blender 那几项同 A |
| P | 经过 MCP 代理的同一份：AI 客户端只连代理 | `.[proxy]`；上游是假的应用 MCP 服务器，不用别的软件 |
| S | 影子执行（试验）：AI 的脚本先在另一个 Blender 里执行，只把允许的同步过来 | 同 B |

- 没准备好的组会跳过并说明原因。
- 旧的 `experiments/results/results.csv` 会改名备份，这次的结果写进新的；加 `--keep-results` 就接着追加。
- 最后打印汇总表并逐行判定：有保护的行，"人的修改被覆盖""AI 的修改丢失""出错"都应该是 0；对照组的行至少有一个不是 0。
- "Agent 不先看提示"那一行：Agent 没读运行时的提示，不知道 Rock_2 是人故意删的，脚本会把它补回来。现在运行时把补回的删掉并告诉它（`design_v0.5.md` 13.3），所以"被删对象被重建"是 0，备注里写"补回人删掉的对象被拦下 1 个"。
- **"不变量违反"一列**：有保护的实验每一步之后都检查三条不变量（`rightofway/invariants.py`），填的是出问题的对象个数，应该是 0：
  - I1 账本 = 场景；
  - I2 要保持的部分要么没变，要么记为违规；
  - I3 告诉 AI 的和实际一致。
  对照组不检查（填"—"）；文件兜底还没接上（也是"—"）。
- **"违规"一列**：要保持的部分在应用里被改了、没能恢复的对象个数。运行时照实记账、提醒人、暂停 AI（`spec/design_v0.5.md` 第 6 节）。只有实验 E 的"模拟 Unity 接入"那一行应该是 1：这个接入恢复不了删除，判定是"违规 1 处，已如实报告"。其他有保护的行应该是 0，否则判为未通过。
- **实验 E**：Blender 那两行应该通过（Leaf_3 被恢复，告诉 AI"人改过的对象不能删除"）；模拟 Unity 那一行人的修改没保住（1），但违规如实报告、不变量全部成立。
- **实验 H 是测量，不判通过与否**，结果另外列一张表（`experiments/results/habits.csv`）。看三件事：重叠多频繁（"和人重叠""和 AI 重叠"）、重建会不会出现两份（"重复""换编号"）、不变量是不是还全部成立（应该是）。认回同一个对象默认打开：Blender 那几行的违规、重复都应该是 0；模拟 Unity 接入里只剩恢复不了的纯删除（A 清空重建的 Cube、E 的 Leaf_3）。"对照：不认回同一个对象"那几行，清空重建有重复和违规属于预期。详见 `spec/design_v0.5.md` 第 12、13 节。
- **实验 P** 判定和 A 一样：人的修改被覆盖、AI 的修改丢失、不变量违反都应该是 0。备注里写 AI 的几次调用有几次返回 isError（没生效、被拒的部分），以及补回被拦下的个数。
- **实验 S 是测量**：打印直接执行和影子执行的用时和结局对比，两种的结局应该一样（都是 0）。

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
python -m experiments.exp_e_delete --backend fake-unity   # 实验 E：不需要任何软件，模拟现在的 Unity 接入
python -m experiments.exp_e_delete                 # 实验 E：打开着的 Blender（加 --human real 由你挪 Leaf_3）
python -m experiments.exp_e_delete --backend unity # 实验 E：Unity
python -m experiments.exp_h_habits --backend fake-unity             # 实验 H：不需要任何软件
python -m experiments.exp_h_habits                                  # 实验 H：打开着的 Blender，每种组合新建一个场景
python -m experiments.exp_h_habits --backend fake-unity --sweep     # 实验 H：人改一处，撞上 AI 这一步的概率
python -m experiments.exp_h_habits --backend fake-unity --no-reidentify   # 实验 H：对照，关掉"认回同一个对象"
python -m experiments.exp_p_proxy                  # 实验 P：经过 MCP 代理（假的上游，不需要任何软件）
python -m experiments.exp_p_proxy --upstream "uvx blender-mcp"   # 实验 P：开着的 Blender + 现成的 BlenderMCP 服务器（要装 uv）
python -m experiments.exp_s_shadow --backend socket   # 实验 S：影子执行，两个 Blender（9876、9877）；不加参数是两个 bpy 进程
```

去掉 `--human sim` 就是真人操作；加 `--no-protocol` 是对照组；加 `--granularity object` 是按整个对象判断。

- 实验 A、D、B 会在 Blender 里**新建一个场景**"RightOfWay实验_…"，不影响原来的场景。开始前会删掉之前实验留下的"RightOfWay实验_"场景（实验 A 加 `--keep-old-scenes` 可以保留）。
- 实验 C 会在 Unity 里**追加**一个空场景，原来的场景不受影响，也不保存任何东西。程序会自己启动 Unity 的 MCP 中继；连不上时，先把其他正在使用 Unity MCP 的客户端（例如 Claude Desktop）停掉再试。
- 实验 C 每次检查人的修改都要在 Unity 里编译一段 C#，所以默认 2 秒检查一次。
- 实验 H 的 AI 有四种写法：精确修改（原来的脚本）、整体调整（遍历所有物体统一设置）、清空重建（全删掉再按计划重搭）、类型化批量命令（像 Unity MCP 的 `batch_execute`，每批最多 25 条，`--max-batch` 可以改小）。批量命令只在模拟应用上跑。`--sweep` 在 Blender 上要三分钟左右。
- 模拟应用的两种：`fake` 和 Blender 一样（能恢复删除、重名自动加 `.001`、编号能转交）；`fake-unity` 和 Unity 一样（恢复不了删除、允许重名、编号由应用分配，认回时记一条编号映射）。
- 实验 P 用开着的 Blender 时，也会新建一个"RightOfWay实验_代理_…"场景并切换过去，不动你原来的场景。

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
| R3、R4 | 基准版本（这个 Agent 看到的版本）；过时写入作废 | `runtime.py`：`mark_seen`、`admit`、`_target_conflict`、`_precheck`、`_finalize`；`live_sync.py`：`sync` |
| R5 | 人的修改总能提交 | `runtime.py`：`human_edit` |
| R6 | 人碰过的部分 Agent 不再改 | `runtime.py`；`blender_side.py`：`run_agent` 的恢复步骤、`apply_sync` 的核对 |
| R7 | 交还 | `runtime.py`：`hand_back`；`shared_session.py`：`hand_back` |
| R8–R10 | 占用、阻塞、明确释放；选中即占用（选项） | `runtime.py`：`human_edit(occupy=...)`、`release`、`set_selection`、`_claim_conflict` |
| R11–R17 | 认领、失效、集合、撤回、版本保留、确认、暂停 | `runtime.py` |
| R18 | 观察粒度、值的文字 | `blender_side.py`：`records`、`object_values`；`UnitySide.cs.txt`：`Records`、`Show` |
| R19、R20 | 违反契约、变化日志 | `runtime.py` |
| R21 | 权威副本；旧文件不覆盖新内容 | `runtime.py`；`filemerge.py`：`_base`、`infer_base` |
| R22 | 告知 Agent 它错过的变化，带值 | `shared_session.py`：`observe`、`run_agent`；`changes.py` |
| R23 | 多个 Agent：后到的让先到的，预留 | `runtime.py`：`admit`、`close_permit`、`_claim_conflict` |
| 许可单 | 每次 Agent 执行前由运行时决定哪些要保持；接入代码只照单执行，场景和许可单不符就 replan | `runtime.py`：`admit`；`blender_side.py`、`UnitySide.cs.txt`、`fakeapp.py`：`run_agent` |
| 核对与记账 | 按实际状态核对每个单元，账本 = 场景；删除人碰过的对象按整个对象判断；违规照实记账、提醒人、暂停 Agent | `runtime.py`：`settle`、`resume_agent`；`shared_session.py`：`_report_from`、`_alert` |
| 类型化命令的预检查 | 每批命令按许可单检查：改到要保持的面的去掉这些面，整条要保持的、删除不许删的对象的不执行 | `model.py`：`Permit.screen`；`shared_session.py`：`run_commands`、`_screen`；`fakeapp.py`：`compile_commands` |
| 认回同一个对象（默认打开） | 同一个父对象下名字（或去掉重名后缀的名字）、类型一样的，一对一认回；之前想删、被保留下来的也认；运行时重算核对 | `identity.py`；`runtime.py`：`identity_rule`、`_settle_identity`、`_track_wanted_gone`；`blender_side.py`、`fakeapp.py`：`_identity` |
| 墓碑、祖先不许删 | 人删掉、Agent 还没看到的，补回的删掉；要保持的对象的祖先不许删 | `runtime.py`：`_tombstones`、`_ancestors`；`model.py`：`Permit.no_recreate`、`keep_alive` |
| 失效的事实、循环检测 | 以现在的值为准的事实；同一个单元反复被保持就提醒人 | `shared_session.py`：`_conflicts`、`_facts`、`_derived`、`payload`；`runtime.py`：`_detect_loops` |
| 能力探测 | 连接时在临时场景里测：按面恢复、恢复删除、begin/finish、重名后缀 | `blender_side.py`、`fakeapp.py`：`probe`；`runtime.py`：`learn_app` |
| MCP 代理 | 执行代码的工具 → run_agent；类型化工具 → 按对象预检查 + begin/finish；自己的小工具 → 按面预检查 | `proxy/core.py`、`proxy/sources.py`、`proxy/server.py`；`shared_session.py`：`run_tool` |
