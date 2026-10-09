"""方式二：各自一个窗口，实时同步（给 computer use 用）。

AI 用 computer use 时要点鼠标，必须有自己的屏幕，所以它在自己的 Blender 里改自己的一份。
但两边都不用存盘、也不用"文件 → 恢复"：两个 Blender 都装着现成的 MCP 插件，运行时直接读写它们。

    你的 Blender（MCP 插件，端口 9876）  ←→  运行时  ←→  AI 的 Blender（MCP 插件，端口 9877）

每次同步（sync，一般在 AI 做完一步之后调用，也可以定时调用）：
  1. 看你这边：自上次以来你改了什么，记为"人碰过"（和方式一一样）。
  2. 看 AI 那边：和上次同步后的状态（base）比，AI 改了什么。
  3. 按规则决定 AI 的每个改动要不要：只有 AI 改的面照常生效；你改过（或正在改、正选中）的面不要，
     你删掉的对象上的修改也不要。
  4. 把要的那部分写进你的场景。写之前再核对一次：你在这期间刚好又改了的，这一项跳过。
  5. 把你的场景原样同步给 AI 那边（包括它没被采用的部分恢复成你的），用文字告诉它变了什么。

运行时一直同时看着两边，每一边最后同步到了哪里它自己清楚，所以不需要推断"起点"，也不需要版本号。
文件合并（filemerge.py）只留给没有 MCP、也没有脚本接口的应用兜底。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..model import Operation, OpStatus, Target
from ..runtime import Runtime
from .changes import Values, describe_changes
from .records import ASPECT, DELETED, OBJECT, Records, base_name, diff_records, label, split_unit, units
from .shared_session import CAPABILITY, OBJECT_TYPE, R_HUMAN, R_SELECTED, SharedSession

R_HUMAN_DELETED = "human_deleted"
R_CHANGED = "changed_meanwhile"


@dataclass
class SyncReport:
    index: int = 0
    committed: list[str] = field(default_factory=list)       # AI 的改动里生效的单元
    skipped: list[dict] = field(default_factory=list)         # 没生效的：[{id, name, reason}]
    partial: dict[str, dict] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)          # AI 新建、已同步到你那边的对象
    deleted: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    to_ai: list[str] = field(default_factory=list)            # 同步进 AI 窗口的变化（来自你）
    human_changes: list[str] = field(default_factory=list)    # 这次同步发现的你的修改
    resurrections: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    consistent: bool = True                                    # 同步后两边是否一致
    seconds: float = 0.0
    undo_pushed: bool = False
    protected: bool = True
    human_final: Records = field(default_factory=dict)
    text_for_ai: str = ""
    text_for_human: str = ""


class LiveSyncSession:
    def __init__(self, runtime: Runtime, human_bridge, ai_bridge, human: str = "yuan", agent: str = "agent",
                 human_scene: Optional[str] = None, ai_scene: Optional[str] = None, granularity: str = ASPECT,
                 occupy_selection: bool = False, inline: bool = False) -> None:
        """inline=True：两个 Blender 不在同一台机器上时，对象通过 base64 传，而不是临时文件路径。"""
        self.rt, self.ai, self.ai_scene, self.g, self.inline = runtime, ai_bridge, ai_scene, granularity, inline
        # 实时同步按对象搬运（export_objects / apply_sync），数据块还没有单独作为单元（design_v0.5.md 第 11 节第 5 步）
        self.hs = SharedSession(runtime, human_bridge, human=human, agent=agent, scene=human_scene,
                                granularity=granularity, occupy_selection=occupy_selection, prefix="h-",
                                data_units=False)
        self.base: Records = {}                # 上次同步后两边共同的状态（也就是 AI 窗口里当时的样子）
        self.base_versions: dict[str, int] = {}
        self.ai_values: Values = {}
        self._n_op = 0
        self._n_sync = 0

    @property
    def human(self) -> str:
        return self.hs.human

    @property
    def agent(self) -> str:
        return self.hs.agent

    @property
    def labels(self) -> dict:
        return self.hs.labels

    def _ai_args(self, **kw) -> dict:
        if self.ai_scene:
            kw["scene"] = self.ai_scene
        kw.setdefault("prefix", "a-")
        return kw

    def _learn_ai(self, recs: Records) -> None:
        for cid, r in recs.items():
            if r.get("values"):
                self.ai_values.setdefault(cid, {}).update(r["values"])

    def _source(self, ex: dict) -> dict:
        return {"data": ex["data"]} if "data" in ex else {"path": ex["path"], "remove_source": True}

    # ------------------------------------------------------------------
    def start(self) -> Records:
        """开始协作：AI 那边变成和你这边一样（分叉出 AI 的一份）。"""
        self.hs.start()
        res = self.ai.call("poll", self._ai_args())
        a = res["records"]
        self._learn_ai(a)
        self.base, _, errors = self._push_to_ai(a)
        if errors:
            raise RuntimeError("开始同步失败：" + "；".join(errors))
        self.base_versions = {uid: o.version for uid, o in self.rt.objects.items()}
        return self.base

    def ai_execute(self, code: str) -> str:
        """在 AI 那个 Blender 里执行（模拟 AI 用 computer use 操作它自己的窗口）。不经过任何保护。"""
        return self.ai.execute(code)

    # ------------------------------------------------------------------
    def _push_to_ai(self, a: Records) -> tuple[Records, list[str], list[str]]:
        """让 AI 那边变成和你这边（self.hs.view）一样。返回同步后 AI 那边的记录、变化的描述、错误。"""
        h = self.hs.view
        items, need = [], []
        for cid, hr in h.items():
            if cid not in a:
                items.append({"id": cid, "op": "create"})
                need.append(cid)
            elif a[cid]["fp"] != hr["fp"]:
                faces = sorted(f for f in hr["aspects"] if a[cid]["aspects"].get(f) != hr["aspects"][f])
                op = "whole" if self.g == OBJECT else "faces"
                items.append({"id": cid, "op": op, "faces": faces, "guard": a[cid]["aspects"]})
                need.append(cid)
        for cid in a:
            if cid not in h:
                items.append({"id": cid, "op": "delete", "guard": a[cid]["aspects"]})
        if not items:
            return a, [], []
        source, exported = {}, {}
        if need:
            ex = self.hs.bridge.call("export_objects", self.hs._args(ids=need, inline=self.inline))
            exported = ex["records"]
            source = self._source(ex)
            for it in items:
                if it["id"] in exported:
                    it["name"] = ex["names"][it["id"]]
                    it["record"] = exported[it["id"]]
            items = [it for it in items if it["op"] == "delete" or "record" in it]
        res = self.ai.call("apply_sync", self._ai_args(items=items, known={c: r["aspects"] for c, r in a.items()},
                                                       **source))
        # 按面复制没能完全对上的，整个换一次
        retry = [e["id"] for e in res.get("face_errors", [])]
        if retry and need:
            ex = self.hs.bridge.call("export_objects", self.hs._args(ids=retry, inline=self.inline))
            items2 = [{"id": c, "op": "whole", "name": ex["names"][c], "record": ex["records"][c]}
                      for c in retry if c in ex["records"]]
            res = self.ai.call("apply_sync", self._ai_args(items=items2, known={c: r["aspects"] for c, r in a.items()},
                                                           **self._source(ex)))
        a2 = res["records"]
        old_values = {cid: dict(v) for cid, v in self.ai_values.items()}
        self._learn_ai(a2)
        errors = []
        target = {**{c: r for c, r in h.items()}, **exported}
        for cid in set(target) | set(a2):
            t, got = target.get(cid), a2.get(cid)
            if t is None or got is None:
                if (t is None) != (got is None):
                    errors.append(f"两边不一致：{(t or got)['name']} 只在{'AI' if t is None else '你'}那边")
            elif t["cfp"] != got["cfp"]:
                bad = [label(f, self.labels) for f in t["aspects"] if got["aspects"].get(f) != t["aspects"][f]]
                errors.append(f"两边不一致：{t['name']} 的{'、'.join(bad) or '内容'}")
        lines = describe_changes(a, a2, old_values, self.ai_values, self.labels,
                                 who=self.hs._who_for(self.agent))
        return a2, lines, errors

    # ------------------------------------------------------------------
    def sync(self, protect: bool = True, label_: str = "") -> SyncReport:
        """同步一次。protect=False 是对照组：AI 的改动一律覆盖你的（谁后同步谁生效）。"""
        t0 = time.perf_counter()
        self._n_sync += 1
        rep = SyncReport(index=self._n_sync, protected=protect)
        hs, g = self.hs, self.g

        # 1. 你这边
        hd = hs.poll()
        rep.human_changes = hd.describe()
        h = hs.view
        # 2. AI 那边
        res = self.ai.call("poll", self._ai_args(known={c: r["aspects"] for c, r in self.base.items()}))
        a = res["records"]
        self._learn_ai(a)
        ub, ua = units(self.base, g), units(a, g)
        da = diff_records(ub, ua)
        created = [c for c in a if c not in self.base]
        deleted = [c for c in self.base if c not in a]
        step_id = f"sync-{self._n_sync}" + (f"-{label_}" if label_ else "")
        self.rt.add_step(step_id, inputs=[], outputs=sorted(da.ids))
        selected = hs.selected if (hs.occupy_selection and protect) else set()

        def submit(uid: str) -> tuple[Optional[str], Optional[dict]]:
            """AI 对一个单元的修改记成一个操作，基准版本是上次同步时的版本。返回 (操作编号, 拒绝原因)。"""
            self._n_op += 1
            op_id = f"sync-op-{self._n_op}"
            r = self.rt.submit(Operation(op_id, self.agent, CAPABILITY,
                                         targets=[Target(uid, self.base_versions.get(uid, -1))], step_id=step_id))
            if r.status.is_rejection:
                return None, hs._reason(uid, self.agent)
            return op_id, None

        items: dict[str, dict] = {}
        pending: dict[str, str] = {}          # 单元 → 操作编号（执行后再确认）
        name_of = lambda cid: (a.get(cid) or self.base.get(cid) or h.get(cid) or {}).get("name", cid)  # noqa: E731
        for cid in created:
            items[cid] = {"id": cid, "op": "create"}
        for cid in deleted:
            if cid not in h:
                continue                                   # 两边都删了
            obj_units = [u for u in ub if split_unit(u)[0] == cid]
            ops, why = [], None
            if cid in selected:
                why = {"reason": R_SELECTED}
            else:
                for uid in obj_units:
                    op_id, reason = submit(uid)
                    if op_id:
                        ops.append((uid, op_id))
                    elif why is None:
                        why = reason
            if why is None or not protect:
                items[cid] = {"id": cid, "op": "delete", "guard": h[cid]["aspects"] if protect else None}
                pending.update(dict(ops))
            else:
                for _, op_id in ops:
                    self.rt.complete(op_id, failed=True)
                rep.skipped += [{"id": u, "name": name_of(cid), **why} for u in obj_units]
        for uid in sorted(da.modified):
            cid, face = split_unit(uid)
            if cid in created or cid in deleted:
                continue
            if cid not in h:
                rep.skipped.append({"id": uid, "name": name_of(cid), "reason": R_HUMAN_DELETED})
                continue
            if cid in selected:
                rep.skipped.append({"id": uid, "name": name_of(cid), "reason": R_SELECTED})
                continue
            op_id, why = submit(uid)
            if why is not None and protect:
                rep.skipped.append({"id": uid, "name": name_of(cid), **why})
                continue
            if g == ASPECT:
                it = items.setdefault(cid, {"id": cid, "op": "faces", "faces": [], "guard": {}})
                it["faces"].append(face)
                if protect:
                    it["guard"][face] = h[cid]["aspects"].get(face)
            else:
                items[cid] = {"id": cid, "op": "whole", "guard": h[cid]["aspects"] if protect else None}
            if op_id:
                pending[uid] = op_id

        # 3. 把要的部分写进你的场景（写之前再核对一次）
        h_applied, skipped_ids = h, {}
        if items:
            need = [c for c, it in items.items() if it["op"] != "delete"]
            source = {}
            if need:
                ex = self.ai.call("export_objects", self._ai_args(ids=need, inline=self.inline))
                source = self._source(ex)
                for c in need:
                    items[c]["name"] = ex["names"].get(c, a[c]["name"])
                    items[c]["record"] = ex["records"].get(c, a[c])
            res = hs.bridge.call("apply_sync", hs._args(
                items=list(items.values()), known=hs._known(h), undo_push=True,
                undo_message="RightOfWay: AI 的修改", **source))
            h_applied = res["records"]
            rep.undo_pushed = res.get("undo_pushed", False)
            skipped_ids = {s["id"]: s["reason"] for s in res["skipped"]}
            for e in res.get("face_errors", []):
                rep.errors.append(f"{name_of(e['id'])} 的这些面没能同步：" + "、".join(label(f, self.labels) for f in e["faces"]))
            rep.errors += [f"悬空引用：{n}" for n in res.get("dangling", [])]
        hs._learn(h_applied)

        # 4. 确认 AI 的每个操作是否真的生效
        uh = units(h_applied, g)
        for uid, op_id in pending.items():
            cid = split_unit(uid)[0]
            want = ua.get(uid)
            ok = cid not in skipped_ids and (uh.get(uid, {}).get("fp") == want["fp"] if want else uid not in uh)
            if ok:
                r = self.rt.complete(op_id, writes={uid: want or {**DELETED, "name": name_of(cid)}})
                if r.status in (OpStatus.COMMITTED, OpStatus.MERGED):
                    rep.committed.append(uid)
                    continue
            else:
                self.rt.complete(op_id, failed=True)
            rep.skipped.append({"id": uid, "name": name_of(cid), "reason": R_CHANGED})
        if not protect:
            done = {s["id"] for s in rep.skipped if s["reason"] == R_CHANGED}
            rep.committed += [u for u in da.modified if split_unit(u)[0] in h and u not in pending and u not in done]
        deleted_names = {base_name(n) for n in hs.human_deleted.values()}
        for cid in created:
            if cid in skipped_ids or cid not in h_applied:
                rep.skipped += [{"id": u, "name": name_of(cid), "reason": R_CHANGED} for u in units({cid: a[cid]}, g)]
                continue
            for uid, content in units({cid: a[cid]}, g).items():
                self._n_op += 1
                op_id = f"sync-op-{self._n_op}"
                self.rt.submit(Operation(op_id, self.agent, CAPABILITY, produces=[uid], step_id=step_id))
                self.rt.complete(op_id, produces={uid: content}, produce_types={uid: OBJECT_TYPE})
                rep.committed.append(uid)
            if base_name(a[cid]["name"]) in deleted_names:
                rep.resurrections.append(a[cid]["name"])

        # 5. 写入期间你刚好又改了的（不属于 AI 的改动），记成你的修改
        expected = {}
        for cid, r in h.items():
            expected[cid] = dict(r, aspects=dict(r["aspects"]))
        for uid in rep.committed:
            cid, face = split_unit(uid)
            if cid in created:
                expected[cid] = a[cid]
            elif cid in a and cid in expected:
                if g == ASPECT:
                    expected[cid]["aspects"][face] = a[cid]["aspects"][face]
                else:
                    expected[cid] = a[cid]
        for cid in deleted:
            if cid in items and items[cid]["op"] == "delete" and cid not in skipped_ids:
                expected.pop(cid, None)
        hs._absorb_human(expected, h_applied)
        hs.view = h_applied

        # 6. 把你的场景原样同步给 AI 那边
        a2, to_ai, errors = self._push_to_ai(a)
        attempted = {(split_unit(u)[0], split_unit(u)[1]) for u in da.ids}
        rep.to_ai = [x for x in to_ai]
        rep.errors += errors
        rep.consistent = not errors
        self.base = a2
        self.base_versions = {uid: o.version for uid, o in self.rt.objects.items()}
        rep.human_final = h_applied

        # 7. 说明
        by_obj: dict[str, dict] = {}
        for uid in rep.committed:
            by_obj.setdefault(split_unit(uid)[0], {"ok": [], "no": []})["ok"].append(uid)
        for s in rep.skipped:
            by_obj.setdefault(split_unit(s["id"])[0], {"ok": [], "no": []})["no"].append(s)
        rep.created = sorted(name_of(c) for c in created if c in by_obj and by_obj[c]["ok"])
        rep.deleted = sorted(name_of(c) for c in deleted if c in by_obj and by_obj[c]["ok"] and not by_obj[c]["no"])
        rep.modified = sorted(name_of(c) for c, v in by_obj.items() if v["ok"] and not v["no"]
                              and c not in created and c not in deleted)
        if g == ASPECT:
            for c, v in by_obj.items():
                if v["ok"] and v["no"]:
                    rep.partial[name_of(c)] = {"kept": sorted(split_unit(u)[1] for u in v["ok"]),
                                               "restored": sorted(split_unit(s["id"])[1] for s in v["no"])}
        rep.seconds = round(time.perf_counter() - t0, 3)
        rep.text_for_ai, rep.text_for_human = self._describe(rep, by_obj, name_of, attempted)
        return rep

    # ------------------------------------------------------------------
    def _why(self, reason: str) -> str:
        return {R_HUMAN: "人改过，保留人的", R_SELECTED: "人正选中着，没有改",
                R_HUMAN_DELETED: "人已经删掉了这个对象", R_CHANGED: "同步时人刚好又改了，保留人的"}.get(reason, "没有采用")

    def _describe(self, rep: SyncReport, by_obj: dict, name_of, attempted) -> tuple[str, str]:
        ai = [f"同步完成（第 {rep.index} 次，用时 {rep.seconds:.2f} 秒）。"]
        done = []
        if rep.created:
            done.append("新建 " + "、".join(rep.created))
        if rep.modified:
            done.append("修改 " + "、".join(rep.modified))
        if rep.deleted:
            done.append("删除 " + "、".join(rep.deleted))
        ai.append("你的修改已同步到人那边：" + ("；".join(done) if done else "没有"))
        if rep.partial and rep.protected:
            ai.append("部分采用：" + "；".join(
                f"{n}（{'、'.join(label(a, self.labels) for a in p['kept'])} 已同步；"
                f"{'、'.join(label(a, self.labels) for a in p['restored'])} 没有采用）" for n, p in sorted(rep.partial.items())))
        refused: dict[str, set] = {}
        for c, v in by_obj.items():
            if v["no"] and not v["ok"]:
                refused.setdefault(self._why(v["no"][0]["reason"]), set()).add(name_of(c))
        for why, names in sorted(refused.items()):
            ai.append(f"没有采用（{why}）：" + "、".join(sorted(names)))
        if rep.to_ai:
            ai.append("你的窗口已更新成和人那边一样，变化如下（没被采用的部分已恢复成人的版本）：")
            ai += ["- " + x for x in rep.to_ai]
        if rep.resurrections:
            ai.append("注意：你新建的 " + "、".join(rep.resurrections) + " 和人删掉的对象同名，人可能不想要它。")
        if rep.errors:
            ai.append("同步检查发现问题：" + "；".join(rep.errors))

        human = [f"已同步（第 {rep.index} 次）。"]
        if rep.human_changes:
            human.append("你的修改：" + "；".join(rep.human_changes))
        if done:
            human.append("AI 的修改已经出现在你的场景里：" + "；".join(done))
        if rep.partial and rep.protected:
            human.append("你和 AI 改了同一个对象的不同部分，都保留：" + "；".join(
                f"{n}（AI 的{'、'.join(label(a, self.labels) for a in p['kept'])}，你的{'、'.join(label(a, self.labels) for a in p['restored'])}）"
                for n, p in sorted(rep.partial.items())))
        if refused and rep.protected:
            human.append("你改过的、没有被 AI 覆盖：" + "、".join(sorted({n for ns in refused.values() for n in ns})))
        if not rep.protected:
            human.append("【对照组，没有保护】AI 的修改一律覆盖了你的。")
        if rep.undo_pushed:
            human.append("按 Ctrl+Z 可以撤掉这次同步进来的 AI 修改。")
        if rep.errors:
            human.append("同步检查发现问题：" + "；".join(rep.errors))
        return "\n".join(ai), "\n".join(human)
