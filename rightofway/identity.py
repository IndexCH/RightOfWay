"""认回同一个对象（design_v0.5.md 12.4–12.6，按 prior_art_solutions.md 第 2 项加固）。

一次执行里 Agent 删掉了一些东西、又新建了一些东西（清空重建）。同一个父对象下名字和类型对得上的，按同一个东西处理：
新的接过旧的编号，人改过的面照样保留。规则只用名字、类型、父对象，每个应用都有（P4）。

配对按层级从上往下，一对一，有歧义就不配（宁可明确失败，也不悄悄绑错）：
  1. exact   同一个父对象下（父对象已经配上的，按配上的算），名字完全相同、类型相同；
  2. suffix  同一个父对象下，去掉应用自动加的重名后缀（例如 Blender 的 ".001"）后名字相同、类型相同；
  3. global  不管父对象，名字完全相同、类型相同，并且两边都只有这一个（父对象被换掉的情况）。
每配上一个父对象，它的子对象就可能在第 1、2 级配上，所以反复做到没有新的配对为止。
配上的等级就是置信等级，运行时记在认回记录里。

"旧的"有三种，用的是同一条规则（接入代码的 _identity）：
  gone    这次执行里 Agent 删掉的；
  rebind  Agent 之前想删、因为人改过而保留下来的（清空和重建分两次执行时，第二次新建的认成它）；
  tomb    人删掉、这个 Agent 还没看到的（墓碑：新建的是按旧印象补回来的，删掉，告诉它）。

重名后缀不手写：运行时在连接时让应用新建两个同名的东西，看第二个叫什么（probe），由 suffix_pattern 学出来。
应用允许重名（例如 Unity）就没有后缀，只做第 1、3 级。

运行时不只信接入代码：verify 用执行前、执行后的记录按同一条规则重算一遍，结果必须完全一样。

这个文件只用 Python 标准库：bridge.build_call 把它的源码放在 blender_side.py 前面一起发进 Blender，
接入代码和运行时用的是同一份规则。
"""
import re


def suffix_pattern(first, second):
    """应用给两个同名的东西起的名字（例如 "Probe" 和 "Probe.001"）→ 去掉后缀用的正则；没有后缀返回 None。"""
    if not first or not second or first == second or not second.startswith(first):
        return None
    tail = second[len(first):]
    parts = re.split(r"(\d+)", tail)
    pattern = "".join(r"\d+" if (i % 2 and p) else re.escape(p) for i, p in enumerate(parts))
    return "(?:" + pattern + ")$"


def strip_suffix(name, pattern):
    if not pattern or name is None:
        return name
    return re.sub(pattern, "", name)


def name_matches(old, new, pattern):
    """两个名字按规则算不算同一个：完全相同，或者去掉后缀后相同。"""
    return old == new or (pattern is not None and strip_suffix(old, pattern) == strip_suffix(new, pattern))


def match_identities(gone, fresh, pattern=None):
    """gone：执行前在、执行后没了的 {编号: {"name", "type", "parent"}}；
    fresh：这次执行里新出现的 {编号: {"name", "type", "parent"}}（parent 是新那边的编号）。
    返回 {旧编号: [新编号, 等级]}，等级是 "exact"、"suffix" 或 "global"。"""
    matched = {}
    used = set()

    def mapped(parent):
        return matched[parent][0] if parent in matched else parent

    def unique_pairs(key_old, key_new):
        by_old, by_new = {}, {}
        for o, r in gone.items():
            if o not in matched:
                by_old.setdefault(key_old(r), []).append(o)
        for n, r in fresh.items():
            if n not in used:
                by_new.setdefault(key_new(r), []).append(n)
        return [(olds[0], by_new[k][0]) for k, olds in sorted(by_old.items(), key=lambda kv: str(kv[0]))
                if len(olds) == 1 and len(by_new.get(k, ())) == 1]

    levels = [("exact", lambda r: r.get("name"))]
    if pattern:
        levels.append(("suffix", lambda r: strip_suffix(r.get("name"), pattern)))
    while True:
        for level, name_key in levels:
            while True:
                pairs = unique_pairs(lambda r: (mapped(r.get("parent")), name_key(r), r.get("type")),
                                     lambda r: (r.get("parent"), name_key(r), r.get("type")))
                for o, n in pairs:
                    matched[o] = [n, level]
                    used.add(n)
                if not pairs:
                    break
        pairs = unique_pairs(lambda r: (r.get("name"), r.get("type")), lambda r: (r.get("name"), r.get("type")))
        for o, n in pairs:
            matched[o] = [n, "global"]
            used.add(n)
        if not pairs:                  # 新配上的父对象可能让它的子对象在第 1、2 级配上，所以有新配对就再来一轮
            return matched


def verify(before, raw, pairs, fresh_rows, wanted=(), tombs=None, pattern=None):
    """运行时核对接入代码交回的配对：只用执行前、执行后（恢复前）的记录，自己重算一遍，结果必须完全一样。

    before / raw：{对象编号: {"name", "type", "parent", "kind", "users"}}，raw 里配上的旧编号已经是新对象的内容；
    pairs：接入代码交回的 {旧编号: [新编号, 等级]}；
    fresh_rows：配上的新对象在接过编号之前的样子 {新编号: {"name", "type", "parent"}}；
    wanted：可以重新绑定的旧对象（Agent 之前想删、因为人改过而保留下来的）；
    tombs：墓碑 {编号: {"name", "type", "parent"}}（人删掉、这个 Agent 还没看到的）。
    返回 (按规则应有的配对, 不一致的说明)。"""
    tombs = tombs or {}
    wanted = set(wanted)
    olds = {}
    for g, r in before.items():
        if g in pairs or g in wanted or g not in raw:
            olds[g] = r
        elif r.get("kind") == "data" and (r.get("users") or 0) > 0 and (raw[g].get("users") or 0) == 0:
            olds[g] = r                # 数据块：执行前有人用、执行后没人用了，也算删掉
    for g, r in tombs.items():
        if g not in before:
            olds[g] = r

    def pre(pid):                      # 接过编号之前，父对象的编号
        return pairs[pid][0] if pid in pairs else pid

    fresh, errors = {}, []
    for g, r in raw.items():
        if g not in before and g not in pairs:
            fresh[g] = {"name": r.get("name"), "type": r.get("type"), "parent": pre(r.get("parent"))}
    for old, (new, _level) in pairs.items():
        row = fresh_rows.get(new)
        if row is None:
            errors.append(f"{old}：接入代码没有交回新对象 {new} 原来的样子")
            continue
        fresh[new] = row
        got = raw.get(old)
        if got is not None and got.get("type") != row.get("type"):
            errors.append(f"{old}：接过编号的对象类型是 {got.get('type')}，不是新对象的 {row.get('type')}")
    expected = match_identities(olds, fresh, pattern)
    for old in sorted(set(expected) | set(pairs)):
        e, p = expected.get(old), pairs.get(old)
        if e != (list(p) if p is not None else None):
            errors.append(f"{old}：按规则应为 {e or '不配对'}，接入代码交回 {list(p) if p is not None else '不配对'}")
    return expected, errors
