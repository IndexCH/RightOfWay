"""实验 K：人和 Claude Code 同时改同一份代码（spec/design_code.md 第 6 节）。

Agent 先读了代码；人接着在编辑器里改了 shop.py 的三处（存盘）：
  H1 把价格系数 1.1 改成 1.25；H2 删掉 discount 函数；H3 在末尾加了一个 shipping 函数。
然后 Agent 按它之前读到的样子做下一步。几种真实 Agent 常见的写法（Agent 是写好的脚本，工具行为照 Claude Code）：
  精确修改    改税率（不碰人的行）；再按旧内容改价格那一行：old_string 找不到，重新读，按它的计划（1.1）再改
  整份重写    按读到的旧内容加上自己的修改，整份 Write
  丢弃改动    先 git checkout -- shop.py 想从干净的文件开始，再改税率
  跨文件改名  用命令把 price 改名成 unit_price（shop.py、report.py 两个文件）
  两个 AI     A 改税率；B 按它读到的旧内容整份重写
每种写法跑两遍：有 RightOfWay（Claude Code hooks），和对照组（没有保护，工具直接执行）。被拒绝的 Agent 照着说明重新读、
只改别的部分（和真的模型收到这种说明之后的做法一样）。

    python -m experiments.exp_k_code
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from experiments.common import banner, say, use_utf8_console, write_row
from rightofway.code.fakeagent import FakeClaudeCode
from rightofway.code.ledger import CodeLedger

SHOP = ("def price(item):\n"
        "    return item.base * 1.1\n"
        "\n"
        "\n"
        "def tax(amount):\n"
        "    return amount * 0.2\n"
        "\n"
        "\n"
        "def total(item):\n"
        "    return price(item) + tax(price(item))\n"
        "\n"
        "\n"
        "def discount(amount, rate=0.1):\n"
        "    return amount * (1 - rate)\n")
REPORT = ("from shop import price\n"
          "\n"
          "\n"
          "def line(item):\n"
          "    return f\"{item.name}: {price(item):.2f}\"\n")
SHIPPING = "\n\ndef shipping(item):\n    return 5 if price(item) < 50 else 0\n"
GIT_ENV = {"GIT_AUTHOR_NAME": "exp", "GIT_AUTHOR_EMAIL": "exp@example.com", "GIT_COMMITTER_NAME": "exp",
           "GIT_COMMITTER_EMAIL": "exp@example.com", "GIT_CONFIG_NOSYSTEM": "1"}
RENAME = (f'"{sys.executable}" -c "import pathlib;'
          "[p.write_text(p.read_text().replace('price(', 'unit_price(').replace('import price', 'import unit_price'))"
          " for p in map(pathlib.Path, ['shop.py', 'report.py'])]\"")


def human_edits(text: str) -> str:
    text = text.replace("item.base * 1.1\n", "item.base * 1.25\n")
    text = text.replace("\n\ndef discount(amount, rate=0.1):\n    return amount * (1 - rate)\n", "")
    return text + SHIPPING


def lost_human(text: str) -> list[str]:
    text = text or ""
    lost = []
    if "item.base * 1.25" not in text:
        lost.append("H1 价格系数")
    if "def discount" in text:
        lost.append("H2 删掉的 discount 又回来了")
    if "def shipping" not in text:
        lost.append("H3 shipping")
    return lost


def setup() -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="rightofway_k_")
    repo, home = os.path.join(tmp, "repo"), os.path.join(tmp, "home")
    os.makedirs(repo)
    for name, text in (("shop.py", SHOP), ("report.py", REPORT)):
        with open(os.path.join(repo, name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    return repo, home


def human(repo: str) -> None:
    path = os.path.join(repo, "shop.py")
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(human_edits(text))


def _retry_tax(ai: FakeClaudeCode) -> None:
    """被拒之后：重新读，只改自己那一处（税率）。"""
    cur, _ = ai.read("shop.py")
    ai.edit("shop.py", "return amount * 0.2\n", "return amount * 0.21\n") if "amount * 0.2\n" in (cur or "") else None


def habit_precise(ai: FakeClaudeCode, b, notes: list) -> None:
    ai.edit("shop.py", "return amount * 0.2\n", "return amount * 0.21\n")
    r = ai.edit("shop.py", "return item.base * 1.1\n", "return round(item.base * 1.1, 2)\n")
    if r.status == "error":                                    # 旧内容找不到：重新读，按计划再改一次
        cur, _ = ai.read("shop.py")
        line = next(x for x in cur.splitlines(keepends=True) if "item.base" in x)
        r = ai.edit("shop.py", line, "    return round(item.base * 1.1, 2)\n")
    if r.status == "denied":
        notes.append("改价格那一行被拒：是人改过的")


def habit_rewrite(ai: FakeClaudeCode, b, notes: list) -> None:
    stale = ai.view.replace("amount * 0.2\n", "amount * 0.21\n").replace("item.base * 1.1\n", "round(item.base * 1.1, 2)\n")
    r = ai.write("shop.py", stale)
    if r.status == "denied":
        notes.append("整份重写被拒")
        cur, _ = ai.read("shop.py")
        ai.write("shop.py", cur.replace("amount * 0.2\n", "amount * 0.21\n"))


def habit_checkout(ai: FakeClaudeCode, b, notes: list) -> None:
    r = ai.bash("git checkout -- shop.py")
    if "改回执行前" in r.text:
        notes.append("git checkout 改到了人的行：shop.py 改回")
    _retry_tax(ai)


def habit_rename(ai: FakeClaudeCode, b, notes: list) -> None:
    r = ai.bash(RENAME)
    if "改回执行前" in r.text:
        notes.append("改名碰到人的 shipping：shop.py 改回，report.py 已改名（只生效了一半，Agent 已被告知）")


def habit_two_agents(ai: FakeClaudeCode, b: FakeClaudeCode, notes: list) -> None:
    ai.edit("shop.py", "return amount * 0.2\n", "return amount * 0.21\n")              # A
    stale = b.view.replace("rate=0.1", "rate=0.15")                                      # B 按旧内容整份重写
    r = b.write("shop.py", stale)
    if r.status == "denied":
        notes.append("B 的整份重写被拒（人的行、A 刚改的行）")
        cur, _ = b.read("shop.py")
        if "rate=0.1" in cur:
            b.write("shop.py", cur.replace("rate=0.1", "rate=0.15"))


# 跨文件改名是测量（不判通过与否）：它展示的是已知的局限——命令只能事后核对，碰到人的行的文件整个改回，别的文件照常，
# 结果可能只生效了一半（spec/design_code.md 5.3）。对照组本来就不会出问题。
HABITS = [("精确修改", habit_precise, False), ("整份重写", habit_rewrite, False), ("丢弃改动（git checkout）", habit_checkout, False),
          ("跨文件改名（命令，测量）", habit_rename, False), ("两个 AI", habit_two_agents, True)]


def run(habit, fn, two: bool, protect: bool) -> dict:
    repo, home = setup()
    try:
        ai = FakeClaudeCode(repo, home, "sess-aaaa", protect, env=GIT_ENV)
        ai.view = ai.read("shop.py")[0]
        b = None
        if two:
            b = FakeClaudeCode(repo, home, "sess-bbbb", protect, env=GIT_ENV)
            b.view = b.read("shop.py")[0]
        human(repo)
        if protect:
            ai.prompt("继续")                                  # 人的修改在下一次 hook 时被记下
        notes: list[str] = []
        fn(ai, b, notes)
        shop = ai.text("shop.py")
        lost = lost_human(shop)
        ai_lost = 1 if habit == "两个 AI" and "amount * 0.21" not in (shop or "") else 0
        if habit in ("精确修改", "整份重写", "丢弃改动（git checkout）") and "amount * 0.21" not in (shop or ""):
            ai_lost = 1
        bad = []
        if protect:
            led = CodeLedger(repo, home)
            with led.open():
                bad = [p for p in led.files if led.content(p) != led.read_disk(p)]
        broken = "unit_price" in (ai.text("report.py") or "") and "def unit_price" not in (shop or "")
        if broken:
            notes.append("report.py 引用的 unit_price 在 shop.py 里不存在")
        agents = [ai] + ([b] if b else [])
        return {"实验": "K 代码（Claude Code）",
                "做法": (f"{habit}，有 RightOfWay" if protect else f"对照：{habit}，没有保护"), "人": "模拟",
                "人的修改被覆盖": len(lost), "AI的修改丢失": ai_lost,
                "被删对象被AI重建": int(any(x.startswith("H2") for x in lost)), "恢复或合并出错": 0,
                "不变量违反": len(bad) if protect else "", "违规": 0 if protect else "",
                "AI修改生效": sum(a.applied for a in agents), "AI修改未采用": sum(a.refused for a in agents),
                "备注": "；".join(notes + (["丢了：" + "、".join(lost)] if lost else []))}
    finally:
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)


def main(argv=None) -> int:
    use_utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-write", action="store_true", help="不写结果表")
    args = ap.parse_args(argv)
    banner("实验 K：人和 Claude Code 同时改同一份代码")
    ok = True
    for habit, fn, two in HABITS:
        for protect in (True, False):
            row = run(habit, fn, two, protect)
            say("info", f"{row['做法']}：人的修改被覆盖 {row['人的修改被覆盖']}，AI 的修改丢失 {row['AI的修改丢失']}，"
                        f"不变量违反 {row['不变量违反'] or 0}" + (f"（{row['备注']}）" if row["备注"] else ""))
            if protect and (row["人的修改被覆盖"] or row["AI的修改丢失"] or row["不变量违反"]):
                ok = False
            if not args.no_write:
                write_row(row)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
