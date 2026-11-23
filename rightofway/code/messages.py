"""代码这边给 AI 和给人的话（spec/design_code.md 3.6）。只从账本生成（P9）：哪些行、谁改的、为什么不能改由核心给
（core.explain、core.positional），这里只写成"第 2 行"、带上现在的内容（行的文字由这一侧给）。"""
from __future__ import annotations

import os
from typing import Optional

from ..core import explain as X
from ..core import rules as R
from ..core.positional import ranges
from . import lines as L

MAX_SHOW = 12                                                          # 说明里每一段最多列几行内容


def who(led, mark: str) -> str:
    if mark == R.KEEP_LOCKED:
        return "RightOfWay 的配置（只有人能改）"
    author = (led.marks.get(mark) or {}).get("author", "")
    return who_author(led, author)


def who_author(led, author: str) -> str:
    cat = X.author_category(author)
    if cat == R.HUMAN:
        return "人"
    if author == R.UNATTRIBUTED:
        return "来源不明（说不准是谁改的，按人的修改保护）"
    if cat == R.EXTERNAL:
        return "来源不明"
    if cat == R.GIT:
        return "git 操作（切换分支、拉取等）"
    return (led.agents.get(author) or {}).get("name") or author


def _span(r: tuple[int, int]) -> str:
    return f"第 {r[0]} 行" if r[0] == r[1] else f"第 {r[0]}–{r[1]} 行"


def _excerpt(led, path: str, r: tuple[int, int]) -> str:
    data = led.content(path)
    rows = L.split_lines(data)
    out = []
    for k in range(r[0] - 1, min(r[1], r[0] - 1 + MAX_SHOW)):
        if k < len(rows):
            out.append(f"{k + 1:>5}| " + rows[k].decode("utf-8", "replace").rstrip("\r\n"))
    if r[1] - r[0] + 1 > MAX_SHOW:
        out.append(f"     | ……（还有 {r[1] - r[0] + 1 - MAX_SHOW} 行）")
    return "\n".join(out)


def _point_text(p: dict) -> str:
    if p.get("file"):
        return "整个文件被删掉了"
    head = f"第 {p['pos']} 行之后" if p["pos"] else "文件开头"
    gone = "；".join(t for t in p.get("text") or () if t.strip())
    return f"{head}删掉了 {p.get('count', '?')} 行" + (f"（{gone[:120]}）" if gone else "")


def region_lines(led, path: str, d: dict, with_text: bool = True) -> list[str]:
    out = []
    units = d.get("units") or []
    for r in ranges(units) if isinstance((units or [None])[0], int) else units:
        out.append(f"- {path} {_span(r)}" + ("，现在是：\n" + _excerpt(led, path, r) if with_text else ""))
    for p in d.get("points") or ():
        out.append(f"- {path}：{_point_text(p)}")
    return out


def refusal_text(led, agent: str, path: str, hits: dict[str, dict], tool: str) -> str:
    items = X.hit_items(led.core, hits)
    parts = [f"这次 {tool} 没有执行（整次拒绝，一行都没写）：它会改到按规则不能动的行。"]
    for it in items:
        if it["reason"] == "stale":
            parts.append(f"{who(led, it['mark'])} 在你上次读 {path} 之后改了这些行（你还没看过，不能按旧印象改）：")
        else:
            parts.append(f"{who(led, it['mark'])}改过、还没交还的：")
        parts += region_lines(led, path, {"units": it["units"], "points": it["points"]})
    if any(it["reason"] == "stale" for it in items):
        parts.append("先重新读一遍这个文件，再只改别的部分。")
    if any(it["reason"] != "stale" for it in items):
        lo = min((r[0] for it in items for r in it["units"]), default=None)
        hi = max((r[1] for it in items for r in it["units"]), default=None)
        where = (f"{path}:{lo}" if lo == hi else f"{path}:{lo}-{hi}") if lo else path
        parts.append(f"以现在的内容为准，写进你之后的计划；只改别的部分。确实需要改这些行，请人交还（人可以在对话里输入：交还 {where}）。")
    return "\n".join(parts)


def loop_text(led, path: str, hits: dict[str, dict], loops: list[str]) -> str:
    spans = "、".join(_span(r) for m in loops for r in ranges(hits[m]["units"])) or "删掉的地方"
    lo = [r for m in loops for r in ranges(hits[m]["units"])]
    where = (f"{path}:{lo[0][0]}" if lo[0][0] == lo[-1][1] else f"{path}:{lo[0][0]}-{lo[-1][1]}") if lo else path
    return (f"RightOfWay：AI 已经 3 次想改你改过的 {path} {spans}，都被拦下了。"
            f"如果你同意它改，在对话里输入：交还 {where}")


def changes_text(led, agent: str, regions: dict[str, list[dict]]) -> str:
    out = []
    shown = 0
    for path, rs in sorted(regions.items()):
        for g in rs:
            if shown >= 8:
                out.append("- ……（还有更多，读一下相应的文件）")
                return "\n".join(out)
            tag = "（你不能改）" if g.get("protected") else ""
            for r in g["units"]:
                out.append(f"- {who(led, g['mark'])} 改了 {path} {_span(r)}{tag}：\n" + _excerpt(led, path, r))
            for p in g["points"]:
                out.append(f"- {who(led, g['mark'])}：{path} {_point_text(p)}{tag}")
            shown += 1
    return "\n".join(out)


def revert_text(led, agent: str, reps: list[dict], tool: str, files_changed: Optional[list[str]] = None) -> str:
    parts = [f"注意：这次 {tool} 改到了按规则不能动的行。下面这些文件已经整个改回执行前的样子（这些文件里你的改动都没有生效）："]
    for r in reps:
        parts.append(f"- {r['path']}" + (f"（你改的版本另存在 {r['backup']}）" if r.get("backup") else "（你删掉了它，已放回）"))
        for m, d in sorted(r["hits"].items()):
            parts.append(f"  碰到了{who(led, m)}的：" + "、".join(
                [_span(x) for x in ranges(d["units"])] + [_point_text(p) for p in d["points"]]))
    if files_changed:
        parts.append("其余文件照常生效：" + "、".join(files_changed[:12]) + ("……" if len(files_changed) > 12 else "")
                     + "。这条命令可能只生效了一半（例如改名只改了一部分），请检查。")
    parts.append("以现在的内容为准；需要的话，只改别的部分再做一次。确实需要改这些行，请人交还。")
    return "\n".join(parts)


def human_revert_text(led, reps: list[dict]) -> str:
    names = "、".join(r["path"] for r in reps)
    kept = [r["backup"] for r in reps if r.get("backup")]
    where = f"（AI 的版本另存在 {os.path.dirname(kept[0]) if len(kept) > 1 else kept[0]}）" if kept else ""
    return f"RightOfWay：AI 的操作改到了你改过的行，已把 {names} 改回去了{where}。"


def unsure_text(rep: dict) -> str:
    names = "、".join(u["path"] for u in rep["unsure"][:12]) + ("……" if len(rep["unsure"]) > 12 else "")
    return (f"注意：这条命令执行了 {rep['seconds']:.0f} 秒，这期间这些文件有变化，说不准是命令改的还是人改的"
            f"（人可能同时在存盘），已按人的修改保护：{names}。这些变化你现在不能改，除非人交还；先读一下这些文件。")


def human_unsure_text(led, rep: dict) -> str:
    parts = []
    for u in rep["unsure"]:
        if not u["hits"]:
            continue
        spans = "、".join(_span(r) for d in u["hits"].values() for r in ranges(d["units"])) or "删掉的地方"
        parts.append(f"{u['path']} {spans}" + (f"（命令执行前的版本在 {u['backup']}）" if u.get("backup") else ""))
    return (f"RightOfWay：AI 的命令执行期间（{rep['seconds']:.0f} 秒），你改过的这些地方变了：" + "；".join(parts)
            + "。说不准是你改的还是命令改的：现在的内容保留，按你的修改保护。如果不是你改的，可以用上面的版本改回去。")


def extra_text(path: str) -> str:
    return f"注意：{path} 在这次编辑期间还有别的变化（说不准是谁改的），已按人的修改保护。"


def release_summary(done: list[dict]) -> str:
    return "；".join(f"{d['path']}" + (" " + "、".join(_span(r) for r in d["units"]) if d["units"] else "")
                    + (f"（{d['points']} 处删除）" if d["points"] else "") for d in done)


def session_start_lines(regions: dict[str, list[dict]]) -> list[str]:
    lines = [
        "这个项目开着 RightOfWay（人和 AI 同时改代码时，人的修改优先，由 hooks 强制执行）：",
        "- 人改过、还没交还（也还没提交）的行，你不能改。碰到这些行的 Edit/Write 会整次被拒（一行都不写）；"
        "命令（Bash）改到了这些行，那个文件会被整个改回去。",
        "- 被拒时会告诉你是哪几行、现在是什么内容：以现在的内容为准，只改别的部分。确实需要改，请人交还"
        "（人可以在对话里输入：交还 路径:起-止）。",
        "- 别的 AI 在你上次读某个文件之后改的行，你也不能按旧印象改：先重新读一遍这个文件。",
    ]
    if regions:
        lines.append("现在受保护的：" + "；".join(
            f"{p} " + "、".join(_span(r) for g in rs for r in g["units"]) + (
                f"（另有 {sum(len(g['points']) for g in rs)} 处删除）" if any(g["points"] for g in rs) else "")
            for p, rs in sorted(regions.items())) + "。")
    return lines
