"""3D 这边给 AI 和给人的话（design_v0.5.md 2.5、13.4）。只从账本生成（P9）：生效了什么、保持了什么、为什么、
现在的值和它想改成的值，都来自核心的结算（core.keyed.settle）和核心算出的事实（core.explain）；
这里只写成"Leaf_3 的位置"这样的说法，名字、面的说法、值的文字由应用侧给（records、labels、values）。
sess 是 SharedSession（取名字、面的说法、AI 的名字用）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..core import rules as R
from .records import Records, display, label, split_unit

if TYPE_CHECKING:
    from .changes import Values
    from .shared_session import AgentReport

R_HUMAN, R_SELECTED, R_OTHER_AI, R_RESERVED = R.KEEP_HUMAN, R.KEEP_SELECTED, R.KEEP_OTHER_AGENT, R.KEEP_RESERVED
R_REVERTED = "reverted"
BASELINE_ACTOR = R.BASELINE
SOURCES = R.SOURCES


def display_actor(sess, actor: str, recipient: Optional[str]) -> str:
    if actor == sess.human:
        return "人 "
    if actor.startswith("external:"):
        return "来源不明（" + SOURCES.get(actor, actor) + "） "
    if actor == BASELINE_ACTOR:
        return ""
    if recipient is not None and actor == recipient:
        return "你 "
    if len(sess.agent_names) <= 1:
        return "AI "
    return f"AI「{sess.agent_names.get(actor, actor)}」 "


def why_text(sess, s: dict) -> str:
    r = s["reason"]
    if s.get("ancestor"):
        return "下面有人改过的对象，不能删除"
    if r == R_HUMAN and s.get("source"):
        return "来源不明（" + SOURCES.get(s["source"], s["source"]) + "），按人的修改保护"
    if r == R_HUMAN:
        return "人改过，保留人的"
    if r == R_SELECTED:
        return "人正选中着，没有改"
    if r == R_OTHER_AI:
        return f"{display_actor(sess, s['by'], None).strip()} 在你上次看之后改过，保留它的"
    if r == R_RESERVED:
        return f"{display_actor(sess, s['by'], None).strip()} 正在重试，暂时留给它"
    if r == R_REVERTED:
        return "应用把它改回去了"
    return "没有采用"


def fact_text(sess, f: dict) -> str:
    who = "人" if f["owner"] == "human" else (display_actor(sess, f["owner"].removeprefix("agent:"), None).strip() or "别的 AI")
    if f["fact"] == "value":
        return f"{f['object']} 的{f['label']} = {f['value']}（{who}定的）"
    if f["fact"] == "exists":
        return f"{f['object']} 还在场景里（" + ("下面有人改过的对象" if f.get("ancestor") else f"{who}改过") + "，不能删除）"
    if f["fact"] == "deleted":
        return f"{f['object']} 已被人删除（请从计划里去掉，不要再新建）"
    return f"{f['object']} 整个保持{who}的版本"


def describe(sess, rep: AgentReport, before: Records, after: Records,
              before_values: Values, raw_values: Values) -> str:
    """回给 AI 的说明。这段文字会出现在 AI 的上下文里。"""
    by_obj: dict[str, dict[str, list]] = {}
    for uid in rep.committed:
        by_obj.setdefault(split_unit(uid)[0], {"ok": [], "no": []})["ok"].append(uid)
    for s in rep.skipped:
        by_obj.setdefault(split_unit(s["id"])[0], {"ok": [], "no": []})["no"].append(s)
    name = lambda cid: display(after.get(cid) or before.get(cid), cid)  # noqa: E731
    is_data = lambda cid: (after.get(cid) or before.get(cid) or {}).get("kind") == "data"  # noqa: E731
    created_ids = [c for c, v in by_obj.items() if v["ok"] and not v["no"] and c not in before and c in after]
    created = sorted(name(c) for c in created_ids if not is_data(c))
    created_data = [c for c in created_ids if is_data(c)]
    modified = sorted(name(c) for c, v in by_obj.items() if v["ok"] and not v["no"] and c in before and c in after)
    deleted = sorted(name(c) for c, v in by_obj.items() if v["ok"] and not v["no"] and c in before and c not in after)
    lines = [f"执行完成，用时 {rep.seconds:.2f} 秒。"]
    if rep.missed:
        lines.append("你上次看场景之后（这段脚本执行之前），发生了这些变化：")
        lines += ["- " + x for x in rep.missed]
    parts = []
    if created:
        parts.append("新建 " + "、".join(created))
    if created_data:
        if len(created_data) <= 3:
            parts.append("新建数据块 " + "、".join(sorted(name(c) for c in created_data)))
        else:                                    # 新对象各自带的网格之类：只给个数，免得说明太长
            kinds: dict[str, int] = {}
            for c in created_data:
                t = str((after.get(c) or {}).get("type", "")).removeprefix("data:") or "?"
                kinds[t] = kinds.get(t, 0) + 1
            parts.append(f"新建 {len(created_data)} 个数据块（"
                         + "、".join(f"{t} {n}" for t, n in sorted(kinds.items())) + "）")
    if modified:
        parts.append("修改 " + "、".join(modified))
    if deleted:
        parts.append("删除 " + "、".join(deleted))
    lines.append("已生效：" + ("；".join(parts) if parts else "没有"))

    def values_note(cid: str, face: Optional[str]) -> str:
        if face is None:
            return ""
        now, want = before_values.get(cid, {}).get(face), raw_values.get(cid, {}).get(face)
        if now is not None and want is not None and now != want:
            return f"现在是 {now}，你改成的 {want} 没有采用"
        return ""

    def face_note(cid: str, s: dict) -> str:
        face = split_unit(s["id"])[1]
        note = values_note(cid, face)
        return f"{label(face, sess.labels)} {why_text(sess, s)}" + (f"（{note}）" if note else "")

    def refused_note(cid: str, items: list[dict]) -> str:
        notes = []
        for s in items[:3]:
            face = split_unit(s["id"])[1]
            note = values_note(cid, face)
            if note:
                notes.append(f"{label(face, sess.labels)} {note}")
        return name(cid) + (f"（{'；'.join(notes)}）" if notes else "")

    partial_objs = [c for c, v in by_obj.items() if v["ok"] and v["no"]]
    if partial_objs and rep.protected:
        items = []
        for cid in sorted(partial_objs, key=name):
            kept = [label(split_unit(u)[1], sess.labels) for u in by_obj[cid]["ok"]]
            notes = [face_note(cid, s) for s in by_obj[cid]["no"] if not s.get("whole_object")]
            if any(s.get("whole_object") for s in by_obj[cid]["no"]):
                notes.append("删除没有生效：" + ("下面有人改过的对象，不能删除"
                                                 if any(s.get("ancestor") for s in by_obj[cid]["no"])
                                                 else "人改过的对象不能删除"))
            items.append(f"{name(cid)}（{'、'.join(kept)} 已生效；" + "；".join(notes) + "）")
        lines.append("部分生效：" + "；".join(items))
    elif rep.partial and not rep.protected:
        items = [f"{n} 的{'、'.join(label(a, sess.labels) for a in p['restored'])}" for n, p in sorted(rep.partial.items())]
        lines.append("【对照组，没有保护】这些部分人改过，按规则不该改，但已经被改掉了：" + "；".join(items))
    refused: dict[str, list[str]] = {}
    refused_ids: dict[str, list[str]] = {}
    for cid in sorted(by_obj, key=name):
        v = by_obj[cid]
        if v["no"] and not v["ok"]:
            refused.setdefault(why_text(sess, v["no"][0]), []).append(
                refused_note(cid, v["no"]) if rep.protected else name(cid))
            refused_ids.setdefault(why_text(sess, v["no"][0]), []).append(cid)
    pre = {split_unit(s["id"])[0] for s in rep.skipped if s.get("precheck")}
    no_delete = {c["id"] for c in rep.conflicts if c["delete_refused"]}
    for why, names in sorted(refused.items()):
        ids = refused_ids.get(why, [])
        if not rep.protected:
            lines.append("【对照组，没有保护】这些对象人改过，按规则不该改，但已经被改掉了：" + "、".join(names))
        elif why.startswith("人改过") and ids and all(c in pre and c in no_delete for c in ids):
            lines.append("没有执行（人改过的对象不能删除）：" + "、".join(names))
        elif why.startswith("人改过") and ids and all(c in pre for c in ids):
            lines.append("没有执行（人改过，保留人的版本，请不要再改）：" + "、".join(names))
        elif why.startswith("人改过") and all(c not in raw_values for c in ids):
            lines.append("没有生效（人改过的对象不能删除，已恢复）：" + "、".join(names))
        elif why.startswith("人改过"):
            lines.append("没有生效（人改过，已恢复成人的版本，请不要再改）：" + "、".join(names))
        elif why.startswith("人正选中"):
            lines.append("没有生效（人正选中着，可能马上要改；人取消选中后可以再改）：" + "、".join(names))
        elif why.startswith("下面有人改过"):
            lines.append(("没有执行（" if ids and all(c in pre for c in ids) else "没有生效（已恢复；")
                         + "下面有人改过的对象，不能删除）：" + "、".join(names))
        else:
            lines.append(f"没有生效（{why}）：" + "、".join(names))
    if rep.breach:
        objs: dict[str, list[str]] = {}
        for uid in rep.breach:
            objs.setdefault(split_unit(uid)[0], []).append(uid)
        parts = []
        for cid, us in sorted(objs.items(), key=lambda kv: name(kv[0])):
            gone = all(rep.breach_detail[u].get("deleted") for u in us)
            parts.append(f"{name(cid)}（{'被你删除了' if gone else '被你改了'}，没能恢复）")
        lines.append("违规：这些是人改过、按规则不能动的，但应用没能恢复，现在的状态已照实记下：" + "；".join(parts))
        if rep.suspended:
            lines.append("你已被暂停：等人处理之后才能继续。")
    def with_level(names: list[str]) -> str:
        return "、".join(n + ("" if rep.identity_levels.get(n) == "同名" else f"（{rep.identity_levels[n]}）")
                        if n in rep.identity_levels else n for n in names)
    if rep.reidentified:
        lines.append("你删掉又新建的这些对象，按同一个对象处理（按名字、类型、父对象推断的，人可以撤销；"
                     "编号不变，人改过的部分照样保留）：" + with_level(rep.reidentified))
    if rep.rebound:
        lines.append("你新建的这些对象，就是你想删、但因为人改过而保留下来的那几个，按同一个对象处理"
                     "（按名字、类型、父对象推断的，人可以撤销；人改过的部分照样保留，没有出现两份）："
                     + with_level(rep.rebound))
    if rep.recreate_blocked:
        lines.append("人删掉了这些对象，你新建的同名对象已删掉，没有补回来：" + "、".join(rep.recreate_blocked)
                     + "。人交还之前不能重建，请从计划里去掉；确实需要的话，请人交还。")
    if rep.facts:
        lines.append("以现在的值为准（请当作新的目标，写进你之后的计划；下次重建或调整时直接用这些值）：")
        lines += ["- " + fact_text(sess, f) for f in rep.facts[:12]]
        if len(rep.facts) > 12:
            lines.append(f"- ……另有 {len(rep.facts) - 12} 条")
        if rep.committed:
            lines.append("你这一步里其余已生效的修改，如果是按这些部分原来的值算出来的（相对位置、对齐、间距……），"
                         "请按上面的值重新核对。")
    for d in rep.derived:
        if d["kind"] == "copy":
            of = d["of"]
            lines.append(f"注意：你新建的 {d['object']} 是 {of} 的一份副本。用它的对象之后不再跟着 {of} 变"
                         f"（人改 {of} 也不会跟着变）；如果你只是想改 {of}，请直接改它。")
        elif d["kind"] == "fewer_users":
            lines.append(f"注意：人改过 {d['object']}，这一步之后用它的对象从 {d['before']} 个变成了 {d['after']} 个："
                         "人在它上面的修改，在换掉的那些对象上看不到了。如果不是有意的，请换回来。")
    if rep.loops:
        lines.append(f"你最近 {sess.rt.loop_window} 次执行里，反复想改这些部分，都没有生效：" + "、".join(
            f"{x['object']} 的{x['label']}（{x['times']} 次）" if x["face"] else f"{x['object']}（{x['times']} 次）"
            for x in rep.loops[:8]) + "。请不要再改它们；确实需要改的话，先请人把它交还给你。")
    if rep.side_effects:
        lines.append("另外，场景里还有这些连带变化（已记下）：" + "、".join(
            sorted({name(split_unit(u)[0]) for u in rep.side_effects})))
    if rep.reserved:
        lines.append(f"被别的 AI 抢先改过的部分，给你预留了 {sess.reserve_seconds:.0f} 秒：请看上面的最新情况后重新决定要不要改。")
    if not rep.protected and rep.human_since:
        lines.append("【对照组，没有保护】执行前人刚改过：" + "；".join(rep.human_since.describe()))
    if rep.resurrections:
        lines.append("注意：你新建的 " + "、".join(rep.resurrections) + " 和人删掉的对象同名，人可能不想要它。")
    out = [x for k in ("created", "modified", "deleted") for x in rep.outside.get(k, [])]
    if out:
        lines.append("注意：你的脚本还改到了当前场景以外的对象（运行时不追踪，也没有保护）：" + "、".join(out[:10])
                     + (" 等" if len(out) > 10 else ""))
    if rep.error:
        lines.append("脚本出错（出错前的修改仍按上面的规则处理）：\n" + rep.error.strip().splitlines()[-1])
    return "\n".join(lines)


def command_lines(sess, rep: AgentReport) -> list[str]:
    """类型化命令：哪些执行前就没有执行（许可单不允许），哪些执行失败了。"""
    out = []
    for e in rep.commands:
        c = e["command"]
        what = {"modify": "修改", "create": "新建", "delete": "删除"}.get(c.get("action"), c.get("action"))
        target = c.get("target") or c.get("name")
        why = (why_text(sess, {"reason": e.get("reason"), "by": e.get("by", ""), "ancestor": e.get("ancestor")})
               if e.get("reason") else "")
        faces = [label(f, sess.labels) for f in e.get("faces", []) if f != "*"]
        if e["status"] == "refused":
            out.append(f"第 {e['index'] + 1} 条（{what} {target}）没有执行：{why}")
        elif e["status"] == "partial":
            out.append(f"第 {e['index'] + 1} 条（{what} {target}）只执行了一部分：{'、'.join(faces)} {why}")
        elif e["status"] == "failed":
            out.append(f"第 {e['index'] + 1} 条（{what} {target}）执行失败：{e.get('error', '')}")
    return out


def alert(sess, rep: AgentReport, before: Records, raw: Records) -> str:
    """有违规（或者接入代码认回的结果和规则不一致）时给人的提醒（只从运行时的核对结果生成）。"""
    notes = []
    if rep.identity_errors:
        notes.append("注意：接入代码认回同一个对象的结果和运行时的规则不一致（现在的状态已照实记下）："
                     + "；".join(rep.identity_errors[:3]) + ("……" if len(rep.identity_errors) > 3 else ""))
    if rep.loop_alert:
        who = display_actor(sess, rep.agent, None).strip() or "AI"
        notes.append(f"注意：{who} 在反复改你改过的部分，都没有生效（最近 {sess.rt.loop_window} 次执行里）："
                     + "、".join(rep.loop_alert) + "。它可能卡住了；需要的话把这部分交还给它，或者告诉它别再改。")
    if rep.breach:
        notes.insert(0, breach_alert(sess, rep))
    return "\n".join(notes)


def breach_alert(sess, rep: AgentReport) -> str:
    objs: dict[str, list[str]] = {}
    for uid in rep.breach:
        objs.setdefault(split_unit(uid)[0], []).append(uid)
    deleted = [sess._group_name(c) for c, us in sorted(objs.items())
               if all(rep.breach_detail[u].get("deleted") for u in us)]
    changed = [sess._group_name(c) for c, us in sorted(objs.items())
               if not all(rep.breach_detail[u].get("deleted") for u in us)]
    who = display_actor(sess, rep.agent, None).strip() or "AI"
    what = "；".join(x for x in (("删除了 " + "、".join(deleted)) if deleted else "",
                                 ("改了 " + "、".join(changed)) if changed else "") if x)
    undo = ("可以在应用里按 Ctrl+Z 撤销 AI 刚才这一步。" if rep.undo_pushed
            else "这一步不一定能用 Ctrl+Z 撤销。")
    tail = (f"{who} 已暂停，处理好之后让它继续（resume_agent）。" if rep.suspended else "")
    return f"注意：{who} {what}。这是你改过、按规则不能动的，但没能自动恢复；现在的状态已照实记下。{undo}{tail}"
