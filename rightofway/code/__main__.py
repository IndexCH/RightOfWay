"""python -m rightofway.code <命令>（spec/design_code.md 第 4 节）

  hook       Claude Code 调用的 hook：从标准输入读事件（JSON），输出决定（JSON）
  install    把 hooks 写进项目的 .claude/settings.local.json（只在本机生效，不提交；--shared 写进 settings.json）
  uninstall  去掉 RightOfWay 的 hooks
  status     现在哪些行受保护
  release    交还：release 路径[:起[-止]] ... 或 release all
  reset      清空这个项目的账本（RightOfWay 不再记得以前谁改了什么）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Optional

from .hooks import EDIT_TOOLS, _norm, _span, handle, who
from .ledger import CodeLedger, find_root

MARK = "rightofway.code"
EVENTS = {
    "SessionStart": (None, 60, False),
    "UserPromptSubmit": (None, 30, False),
    "PreToolUse": ("Edit|MultiEdit|Write|NotebookEdit|Bash", 60, True),
    "PostToolUse": ("Edit|MultiEdit|Write|NotebookEdit|Bash|Read", 60, False),
    "PostToolUseFailure": ("Edit|MultiEdit|Write|NotebookEdit|Bash", 60, False),
}


def _write(text: str, stream) -> None:
    stream.buffer.write(text.encode("utf-8"))
    stream.flush()


def hook() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    try:
        res = handle(payload)
    except Exception as e:                                       # noqa: BLE001
        msg = f"RightOfWay 出错：{type(e).__name__}: {e}"
        if payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_name") in EDIT_TOOLS + ("Bash",):
            # 宁可这次不执行，也不在保护失效时让 Agent 改文件（P1、P5）
            _write(msg + "。为了不丢人的修改，这次先不执行；请人检查（python -m rightofway.code status）。\n", sys.stderr)
            return 2
        _write(json.dumps({"systemMessage": msg}, ensure_ascii=True), sys.stdout)
        return 0
    if res.out:
        _write(json.dumps(res.out, ensure_ascii=True), sys.stdout)
    if res.err:
        _write(res.err, sys.stderr)
    return res.code


def _settings_path(project: str, shared: bool) -> str:
    return os.path.join(find_root(project), ".claude", "settings.json" if shared else "settings.local.json")


def _ours(entry: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in entry.get("hooks") or ())


def install(project: str, python: str, shared: bool) -> str:
    path = _settings_path(project, shared)
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    cmd = f'"{python.replace(os.sep, "/")}" -m {MARK} hook'          # 正斜杠：Windows 上 Git Bash、cmd 都认
    hooks = settings.setdefault("hooks", {})
    for event, (matcher, timeout, block) in EVENTS.items():
        entries = [e for e in hooks.get(event) or [] if not _ours(e)]
        h = {"type": "command", "command": cmd, "timeout": timeout}
        if block:
            h["onFailure"] = "block"                             # hook 起不来时也不放行（需要 Claude Code 2.1.295 以上）
        entry = {"hooks": [h]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        hooks[event] = entries + [entry]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def uninstall(project: str) -> list[str]:
    done = []
    for shared in (False, True):
        path = _settings_path(project, shared)
        try:
            with open(path, encoding="utf-8") as fh:
                settings = json.load(fh)
        except (FileNotFoundError, ValueError):
            continue
        hooks = settings.get("hooks") or {}
        changed = False
        for event in list(hooks):
            keep = [e for e in hooks[event] or [] if not _ours(e)]
            if len(keep) != len(hooks[event] or []):
                changed = True
                if keep:
                    hooks[event] = keep
                else:
                    del hooks[event]
        if changed:
            if not hooks:
                settings.pop("hooks", None)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            done.append(path)
    return done


def status(project: str) -> str:
    led = CodeLedger(find_root(project))
    with led.open():
        led.observe()
        regions = led.protected_regions()
        rows = []
        for path, rs in regions.items():
            for g in rs:
                spans = "、".join(_span(r) for r in g["lines"]) + (f"（{len(g['points'])} 处删除）" if g["points"] else "")
                rows.append(f"  {path} {spans}：{who(led, g['mark'])}")
        agents = [f"  {v.get('name', a)}" for a, v in sorted(led.agents.items())]
    out = [f"项目：{led.root}", f"账本：{led.dir}"]
    out += ["受保护的（人改过、还没交还或提交的）："] + (rows or ["  （没有）"])
    out += ["最近的 Agent："] + (agents or ["  （没有）"])
    return "\n".join(out)


def release(project: str, specs: list[str]) -> str:
    led = CodeLedger(find_root(project))
    done = []
    with led.open():
        led.observe()
        for s in specs:
            if s.lower() in ("all", "全部", "*"):
                done += led.release()
                continue
            path, _, rng = s.partition(":")
            a = b = None
            if rng:
                lo, _, hi = rng.partition("-")
                a, b = int(lo), int(hi or lo)
            rel = _norm(led, path)
            if rel is not None:
                done += led.release(rel, a, b)
        if done:
            what = "；".join(d["path"] for d in done)
            for agent in list(led.agents):
                led.note(agent, f"人把这些交还给了 AI，现在可以改了：{what}。")
    return ("已交还：" + "；".join(d["path"] + " " + "、".join(_span(r) for r in d["lines"]) for d in done)) if done \
        else "没有找到可以交还的（这些行没有受保护）。"


def reset(project: str) -> str:
    led = CodeLedger(find_root(project))
    if os.path.isdir(led.dir):
        shutil.rmtree(led.dir)
    return f"已清空 {led.dir}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rightofway.code", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    i = sub.add_parser("install")
    i.add_argument("--project", default=".")
    i.add_argument("--python", default=sys.executable)
    i.add_argument("--shared", action="store_true")
    for name in ("uninstall", "status"):
        sub.add_parser(name).add_argument("--project", default=".")
    r = sub.add_parser("release")
    r.add_argument("spec", nargs="+")
    r.add_argument("--project", default=".")
    x = sub.add_parser("reset")
    x.add_argument("--project", default=".")
    x.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        return hook()
    if args.cmd == "install":
        print(f"已写入 {install(args.project, args.python, args.shared)}。重新打开 Claude Code 的会话后生效（/hooks 可以查看）。")
    elif args.cmd == "uninstall":
        done = uninstall(args.project)
        print("已去掉：" + "、".join(done) if done else "没有找到 RightOfWay 的 hooks。")
    elif args.cmd == "status":
        print(status(args.project))
    elif args.cmd == "release":
        print(release(args.project, args.spec))
    elif args.cmd == "reset":
        if not args.yes:
            print("会清空这个项目的账本（谁改了哪些行）。确定的话加 --yes。")
            return 1
        print(reset(args.project))
    return 0


if __name__ == "__main__":
    sys.exit(main())
