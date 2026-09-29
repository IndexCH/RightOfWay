# RightOfWay

**Human edits always win — enforced by a runtime, not by prompting.**

RightOfWay is a protocol and reference runtime for humans and AI agents editing the same things at the same time: a Blender scene, a Unity scene, a document. The runtime sits between the agent and the application. When the agent's work collides with what a person just did, the person's edit is kept, the agent's conflicting change is rolled back, and the agent is told exactly what happened.

It needs no changes to the application. It works through what the application already offers: existing MCP servers (Blender MCP, Unity MCP) or plain files.

> **Status: research preview.** The rules, the runtime, and the Blender/Unity integrations work and are tested, but the agents in the experiments are still scripted, not real LLMs. The MCP proxy that lets you put RightOfWay in front of Claude or another agent is the next milestone. The spec is currently written in Chinese; an English version is planned.
>
> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## The problem

Agents that edit your files or your scene work from a snapshot they took a while ago. If you change something in the meantime, the agent's next step is still based on the old snapshot, and it quietly puts things back the way it remembers them. You move a leaf; the agent "tidies up" the tree and moves it back. You delete a rock; the agent notices a rock is missing and rebuilds it.

Current tools handle this by asking the model nicely (put the user's edits in the prompt and hope), by making you review every change, or by merging both sides symmetrically so the last writer wins. None of them *guarantees* that your edit survives. Answering a bug report about exactly this, Cursor's staff wrote that ["at the model level it's still a guideline, not a hard rule"](https://forum.cursor.com/t/158451).

## What RightOfWay does

- **Your edits win.** Anything you changed, the agent can't change. If its script touches it anyway, the runtime restores your version.
- **Per property, not per object.** Conflicts are judged per *face* of an object: location, scale, material, mesh, modifiers… You move a leaf while the agent recolors all leaves for autumn: both changes stay. Faces are read automatically from the application's own data description (Blender RNA, Unity serialization); nothing is hand-defined per app.
- **What you're working on is off-limits.** Once you start editing an object it's occupied, and agent steps that depend on it wait. Optionally, simply selecting an object reserves it.
- **Stale writes don't land.** An agent's change based on an outdated view is voided instead of overwriting newer work.
- **The agent is told what it missed, with values.** Every time it reads or acts, it gets something like:
  ```
  Since you last looked at the scene:
  - Human deleted Rock_2
  - Human modified Leaf_3: Location (-0.647, 0.47, 1.4) → (-0.647, 0.47, 2.2)
  Partially applied: Leaf_3 (Material Slots applied; Location was changed by the human
  and kept — it is now (-0.647, 0.47, 2.2), your value (-0.647, 0.47, 1.6) was not applied)
  ```
- **Several agents at once.** The human outranks every agent. Between agents, the first to commit wins; the later one's conflicting part is voided, it is told the current values, and it gets a short reservation to retry so two agents don't keep undoing each other.
- **Computer-use agents too.** An agent that clicks around needs its own screen, so it works on its own copy in its own app window. The runtime keeps the two copies in sync live through the apps' existing MCP servers: no saving, no reopening files. Apps with no interface at all fall back to merging saved files.

## How it works

```
 agent ──► RightOfWay runtime ──► existing MCP server ──► app (Blender, Unity, …)
                 ▲                                          │
                 └────────── observes human edits ◄─────────┘
```

Every agent action goes through the runtime. Inside the app, in one atomic step, the runtime snapshots whatever is protected, runs the agent's code, compares before and after, and restores only the protected faces the agent touched. Human edits are found by fingerprinting each face of each object and comparing over time.

| | Mode 1: shared copy | Mode 2: separate windows, live sync | File fallback |
|---|---|---|---|
| Agent works through | scripts / MCP tools | computer use (mouse and keyboard) | computer use; app has no interface |
| Rules applied | on every agent action | on every sync (after each agent step) | on every save |
| What the human has to do | nothing | nothing | save, then reopen the file |

## Results so far

Same task in every experiment: the agent builds a small scene (tree, leaves, rocks); the human deletes a rock, moves a leaf, adds a cube; the agent then adjusts the whole scene based on what it saw earlier (autumn colours, uniform leaf height, rocks in a circle). Agents are scripts and the human is simulated. Headless Blender (bpy 4.5), all numbers from `python -m experiments.run_all`.

| Experiment | Setup | Human edits overwritten | Agent edits lost | Deleted object rebuilt |
|---|---|---|---|---|
| A: shared copy | with RightOfWay | 0 | 0 | 0 |
| A: shared copy | no protection (plain Blender MCP) | 2 | 0 | 1 |
| D: two agents + human | with RightOfWay | 0 | 0 | 0 |
| D: two agents + human | no protection | 1 | 1 | 1 |
| B: live sync | with RightOfWay | 0 | 0 | 0 |
| B: live sync | last sync wins | 1 | 0 | 0 |
| B: file fallback | with RightOfWay | 0 | 0 | 0 |
| B: file fallback | whoever saves last wins | 3 / 0 | 0 / 11 | — |

The Unity integration was checked in a real Unity 6.4 editor through Unity's own MCP: the human's leaf height survived, the agent's autumn colour applied, a selected object was left alone, and nothing outside the experiment scene changed (57 tracked objects, about 0.07 s per agent step).

**Not verified yet:** real LLM agents, a real computer-use agent, live sync between two Blender GUIs, edit mode and Ctrl+Z in the Blender GUI, and large scenes.

## Quick start

Requires Python 3.10+. The Blender tests and the cloud-style experiments need Python 3.11 with `pip install bpy`; everything else skips cleanly without it.

```bash
git clone https://github.com/IndexCH/RightOfWay.git
cd RightOfWay
python -m venv .venv
.venv/bin/pip install -e ".[dev]"          # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m pytest                  # 96 tests; the bpy ones skip without Blender's Python module
python examples/demo_game_assets.py         # narrated walkthrough of the runtime, no Blender needed
```

Run every experiment and get a summary table:

```bash
python -m experiments.run_all               # simulated human
python -m experiments.run_all --human real  # you do the human steps in Blender / Unity
```

Each experiment needs its application ready (Blender with the [Blender MCP add-on](https://github.com/ahujasid/blender-mcp) server started; a second Blender on port 9877 for live sync; Unity 6 with the AI Assistant package for Unity). Groups that aren't ready are skipped with the reason. Step-by-step instructions (in Chinese) are in [docs/experiments.md](docs/experiments.md).

## Repository layout

```
cowork/                  runtime (the Python package is still called `cowork`; it will be renamed)
├── runtime.py           protocol rules R1–R21
├── blender/             Blender integration: no changes to Blender or its MCP add-on
│   ├── blender_side.py  code that runs inside Blender (ids, fingerprints, values, protect/restore, sync, merge)
│   ├── shared_session.py  mode 1: per-agent views, "what you missed" (R22), multiple agents (R23)
│   ├── live_sync.py     mode 2: separate windows, live sync
│   ├── filemerge.py     file fallback: per-object three-way merge
│   └── local_server.py  a bpy process that speaks the Blender MCP add-on protocol (for headless tests)
└── unity/               Unity integration through Unity's own MCP (Unity_RunCommand)
experiments/             experiments A, B, C (Unity), D (two agents) and run_all.py
tests/                   96 tests
spec/spec_v0.4.md        protocol spec (Chinese, draft)
spec/related_work.md     how this relates to existing papers, protocols and tools
docs/overview.md         plain-language walkthrough of the protocol (Chinese)
```

## How it relates to other work

Nothing we found combines runtime-enforced human priority, occupancy triggered by the human's own edits, per-property granularity read from the app, and zero changes to the app. The pieces exist separately:

- Stale-write rejection between agents: STORM, S-Bus.
- Prompt-level "don't overwrite the user": CLEO; the system reminders in coding agents.
- Isolated desktops for computer-use agents, with file-level merge back: UFO², TClone, Windows Agent Workspace.
- Property-level last-writer-wins between humans: Figma multiplayer.

Details and sources are in [spec/related_work.md](spec/related_work.md).

## Roadmap

1. **MCP proxy**: point Claude Desktop, Claude Code or any MCP client at RightOfWay instead of at Blender MCP directly.
2. Real LLM agents in the loop, plus a comparison against prompt-only protection.
3. A real computer-use agent on its own desktop, connected to live sync.
4. A user study: do people intervene more, and does per-property protection match what they meant?
5. English spec; more applications through their existing MCP servers.

## Contributing

This is early. Issues describing where an agent overwrote your work (which tool, which app, what happened) are especially useful, as are questions about the rules in the spec. Integrations for other applications that already have an MCP server are welcome; open an issue first so we can agree on the approach.

## License

Not chosen yet. The plan is Apache-2.0 for the code and CC BY 4.0 for the spec text. Until a license file is added, all rights are reserved.

## Author

Yuan ([@IndexCH](https://github.com/IndexCH))
