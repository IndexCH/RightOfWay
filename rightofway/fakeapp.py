"""一个只存在于内存里的"应用"，说和 Blender / Unity 接入代码相同的协议（poll、run_agent）。

用途：
- 不需要 Blender 或 Unity，就能测试运行时和接入层；
- 模拟某个接入代码缺少某种能力，复现只在那个应用里出现的问题。
  例如 FakeApp(restore_deleted=False) 和现在的 Unity 接入一样：Agent 删掉了受保护的对象，恢复不了。

对象有名字、类型和若干个面，面的值是任意能转成 JSON 的东西。
引用另一个东西的面（父对象 "parent"、材质 "material"……）存的是对方的编号：和真的应用一样，
被引用的删掉再新建一个同名的，是另一个编号，引用它的面就变了。被引用的删掉时，引用清空（和 Blender 一样）。

还有"数据块"（data=True），例如几片叶子共用的材质：它自己是一个单元，有自己的面；对象只记引用了哪个。

Agent 的代码是一段 Python，执行时只能用 scene：
    scene.names()                      所有对象的名字（有同名的就出现多次；不含数据块）
    scene.data_names()                 所有数据块的名字
    scene.get("Leaf_3", "location")    读一个面（对象或数据块）
    scene.set("Leaf_3", "location", (0, 0, 1.6))
    scene.create("Bird", {"location": (0, 0, 2)}, parent="Trunk", kind="MESH", refs={"material": "LeafMat"})
    scene.create("LeafMat", {"color": "绿"}, kind="MATERIAL", data=True)
    scene.link("Leaf_3", "material", "LeafMat")      把一个引用面指向另一个东西
    scene.delete("Leaf_3")
    scene.kind("Leaf_3")、scene.parent("Leaf_3")、scene.linked("Leaf_3", "material")   类型、父对象、引用的名字
按名字找时，同名的取最早的那个。

类型化命令（例如 Unity MCP 的 batch_execute）：FakeBridge.compile_commands 把命令翻译成上面的调用，
每条命令单独执行，失败了不影响别的（不是事务）。

run_agent 的保护和恢复逻辑和 blender_side.run_agent（Unity 的 RunAgent 相同）一致：
只照运行时开的许可单（protected、expected）执行，自己不判断哪些该保护。

三个开关模拟不同的应用：
    restore_deleted=False  恢复不了被删掉的对象（和现在的 Unity 接入一样）
    unique_names=True      名字不许重复，重名的自动加 ".001"（和 Blender 一样）；默认允许重名（和 Unity 一样）
    transfer_ids=False     编号由应用分配，不能交给别的对象（和 Unity 的 GetInstanceID 一样）。
                           认回同一个对象时，新对象保留自己的编号，接入代码记一条"应用编号 → 运行时编号"的映射，
                           交给会话保存，之后每次调用都带上（args["aliases"]），报给运行时的一律是运行时编号。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import re
import time
import traceback
from typing import Any, Optional

from .identity import match_identities

PROBE_NAME = "RightOfWayProbe"


def _fp(v: Any) -> str:
    return hashlib.sha1(json.dumps(v, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12]


def _show(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_show(x) for x in v) + ")"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class AgentScene:
    """Agent 的代码能看到的唯一东西。"""

    def __init__(self, app: "FakeApp") -> None:
        self._app = app

    def names(self) -> list[str]:
        return sorted(o["name"] for o in self._app.objects.values() if not o.get("data"))

    def data_names(self) -> list[str]:
        return sorted(o["name"] for o in self._app.objects.values() if o.get("data"))

    def get(self, name: str, face: str) -> Any:
        return copy.deepcopy(self._app.objects[self._app.find(name)]["faces"].get(face))

    def set(self, name: str, face: str, value: Any) -> None:
        self._app.objects[self._app.find(name)]["faces"][face] = copy.deepcopy(value)

    def create(self, name: str, faces: dict, parent: Optional[str] = None, kind: str = "FAKE",
               refs: Optional[dict] = None, data: bool = False) -> str:
        return self._app.add(name, faces, prefix="a-", kind=kind, parent=parent, refs=refs, data=data)

    def link(self, name: str, face: str, target: Optional[str]) -> None:
        self._app.link(name, face, target)

    def delete(self, name: str) -> None:
        self._app.delete(name)

    def kind(self, name: str) -> str:
        return self._app.objects[self._app.find(name)]["type"]

    def parent(self, name: str) -> Optional[str]:
        return self.linked(name, "parent")

    def linked(self, name: str, face: str) -> Optional[str]:
        tid = self._app.objects[self._app.find(name)]["faces"].get(face)
        return self._app.objects[tid]["name"] if tid in self._app.objects else None


class FakeApp:
    def __init__(self, restore_deleted: bool = True, unique_names: bool = False, transfer_ids: bool = True) -> None:
        """restore_deleted=False：和现在的 Unity 接入一样，被删的受保护对象恢复不了。
        unique_names=True：和 Blender 一样，重名的自动加后缀。transfer_ids=False：和 Unity 一样，编号不能转交。"""
        self.objects: dict[str, dict] = {}         # 应用编号 → {"name": ..., "faces": {面: 值}}
        self.selection: set[str] = set()
        self.restore_deleted = restore_deleted
        self.unique_names = unique_names
        self.transfer_ids = transfer_ids
        self._alias: dict[str, str] = {}           # 应用编号 → 运行时编号（transfer_ids=False 时，每次调用由会话传进来）
        self._pending: dict[str, dict] = {}        # begin_agent 和 finish_agent 之间的状态
        self.notifications: list[str] = []         # 给人的提醒（notify）
        self._n = 0

    # ------------------------------------------------------------------
    # 人在界面里的操作（测试和模拟用）
    # ------------------------------------------------------------------
    def add(self, name: str, faces: dict, prefix: str = "h-", kind: str = "FAKE",
            parent: Optional[str] = None, refs: Optional[dict] = None, data: bool = False) -> str:
        self._n += 1
        cid = f"{prefix}{self._n}"
        faces = copy.deepcopy(dict(faces))
        links = dict(refs or {})
        if parent is not None:
            links["parent"] = parent
        if self.unique_names:
            name = self._free_name(name, data)
        obj = {"name": name, "type": kind, "faces": faces, "refs": sorted(links)}
        if data:
            obj["data"] = True
        for face, target in links.items():
            faces[face] = self.find(target) if target is not None else None
        self.objects[cid] = obj
        return cid

    def _free_name(self, name: str, data: bool) -> str:
        """和 Blender 一样：名字被占了就加 .001、.002……（对象和数据块各算各的）。"""
        taken = {o["name"] for o in self.objects.values() if bool(o.get("data")) == data}
        if name not in taken:
            return name
        base, i = _base(name), 1
        while f"{base}.{i:03d}" in taken:
            i += 1
        return f"{base}.{i:03d}"

    def link(self, name: str, face: str, target: Optional[str]) -> None:
        o = self.objects[self.find(name)]
        o["faces"][face] = self.find(target) if target is not None else None
        if face not in o.setdefault("refs", []):
            o["refs"] = sorted(set(o["refs"]) | {face})

    def find(self, name: str) -> str:
        for cid, o in self.objects.items():
            if o["name"] == name:
                return cid
        raise KeyError(name)

    def set(self, name: str, face: str, value: Any) -> None:
        self.objects[self.find(name)]["faces"][face] = copy.deepcopy(value)

    def delete(self, name: str) -> None:
        cid = self.find(name)
        del self.objects[cid]
        self.selection.discard(cid)
        self._drop_dangling()

    def _drop_dangling(self) -> None:
        """被引用的东西不在了：引用清空（Blender 删父对象、删材质，或者恢复出来的对象原来的父对象已经不在时，都是这样）。"""
        for o in self.objects.values():
            for face in o.get("refs", ()):
                if o["faces"].get(face) is not None and o["faces"][face] not in self.objects:
                    o["faces"][face] = None

    def _remap(self, old: str, new: str) -> None:
        """引用 old 的都改成引用 new（Blender 的 user_remap）。"""
        for o in self.objects.values():
            for face in o.get("refs", ()):
                if o["faces"].get(face) == old:
                    o["faces"][face] = new

    def select(self, *names: str) -> None:
        self.selection = {self.find(n) for n in names}

    # ------------------------------------------------------------------
    # 编号：应用编号和运行时编号（transfer_ids=False 时两者可能不同）
    # ------------------------------------------------------------------
    def _set_alias(self, args: dict) -> None:
        if not self.transfer_ids:
            self._alias = dict((args or {}).get("aliases") or {})

    def _lid(self, aid: Optional[str]) -> Optional[str]:
        """应用编号 → 运行时编号。"""
        return self._alias.get(aid, aid) if aid is not None else None

    def _aid(self, lid: Optional[str]) -> Optional[str]:
        """运行时编号 → 现在场景里的应用编号（不在了就原样返回）。"""
        if lid is None:
            return None
        for a, l_ in self._alias.items():
            if l_ == lid and a in self.objects:
                return a
        return lid

    def _snap(self, lid: str) -> dict:
        """快照：引用记成运行时编号，恢复时再换回当时的应用编号（被引用的对象可能被认回成了新对象）。"""
        o = copy.deepcopy(self.objects[self._aid(lid)])
        for face in o.get("refs", ()):
            o["faces"][face] = self._lid(o["faces"].get(face))
        return o

    def _unsnap_value(self, o: dict, face: str) -> Any:
        v = copy.deepcopy(o["faces"].get(face))
        return self._aid(v) if face in o.get("refs", ()) else v

    def _unsnap(self, o: dict) -> dict:
        o = copy.deepcopy(o)
        for face in o.get("refs", ()):
            o["faces"][face] = self._aid(o["faces"].get(face))
        return o

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------
    def _users(self) -> dict[str, int]:
        users: dict[str, int] = {}
        for o in self.objects.values():
            for face in o.get("refs", ()):
                tid = o["faces"].get(face)
                if tid is not None:
                    users[tid] = users.get(tid, 0) + 1
        return users

    def records(self) -> dict[str, dict]:
        users = self._users()
        out = {}
        for aid, o in self.objects.items():
            cid = self._lid(aid)
            refs = o.get("refs", ())
            aspects = {f: _fp(self._lid(v) if f in refs else v) for f, v in o["faces"].items()}
            values = {f: _show(v) for f, v in o["faces"].items()}
            for face in refs:
                tid = o["faces"].get(face)
                values[face] = self.objects[tid]["name"] if tid in self.objects else "无"
            rec = {"id": cid, "name": o["name"], "type": o.get("type", "FAKE"),
                   "cfp": _fp(aspects), "fp": _fp([o["name"], aspects]), "aspects": aspects,
                   "parent": self._lid(o["faces"].get("parent")), "collections": [], "editing": False,
                   "selected": aid in self.selection, "values": values}
            if o.get("data"):
                rec.update(kind="data", users=users.get(aid, 0), display=f"{o['name']}（{rec['type']}）")
            out[cid] = rec
        return out

    def labels(self) -> dict[str, str]:
        faces = {f for o in self.objects.values() for f in o["faces"]}
        return {f: f.replace("_", " ").title() for f in faces}

    # ------------------------------------------------------------------
    # 接入代码的协议
    # ------------------------------------------------------------------
    def probe(self, args: dict) -> dict:
        """连接时的自测（和 blender_side.probe 一样）：新建两个同名的东西看应用怎么起名；再在一个一样配置的
        临时应用里，用同样的 run_agent / begin_agent / finish_agent 试一遍按面恢复、恢复删除、两次调用之间的恢复。"""
        a, b = self.add(PROBE_NAME, {}, prefix="p-"), self.add(PROBE_NAME, {}, prefix="p-")
        names = [self.objects[a]["name"], self.objects[b]["name"]]
        del self.objects[a], self.objects[b]
        out = {"names": names, "identity": True, "transfer_ids": self.transfer_ids, "notify": True, "atomic": True,
               "measured": True}

        def fresh():
            t = FakeApp(self.restore_deleted, self.unique_names, self.transfer_ids)
            cid = t.add(PROBE_NAME, {"location": [0, 0, 0], "scale": 1.0}, prefix="p-")
            return t, cid

        def same(res, cid):
            return cid in res["after"] and res["after"][cid]["cfp"] == res["before"][cid]["cfp"]
        t, cid = fresh()
        out["restore_face"] = same(t.run_agent({"code": f"scene.set({PROBE_NAME!r}, 'location', [1, 0, 0])",
                                                "protected": {cid: ["location"]}}), cid)
        t, cid = fresh()
        out["restore_deleted"] = same(t.run_agent({"code": f"scene.delete({PROBE_NAME!r})",
                                                   "protected": {cid: ["*"]}}), cid)
        t, cid = fresh()
        token = t.begin_agent({"protected": {cid: ["location"]}})["token"]
        t.set(PROBE_NAME, "location", [1, 0, 0])
        out["around"] = same(t.finish_agent({"token": token}), cid)
        return out

    def notify(self, args: dict) -> dict:
        """给人的提醒：假应用记下来（测试用）。"""
        self.notifications.append(str(args.get("text", "")))
        return {"shown": True}

    def poll(self, args: dict) -> dict:
        self._set_alias(args)
        return {"records": self.records(), "labels": self.labels()}

    def _identity(self, before: dict, args: dict) -> tuple[dict, list[str]]:
        """认回同一个对象（和 blender_side._identity 一样，规则在 identity.py）：
        gone（这次删掉的）、rebind（之前想删、被保留的）、tomb（人删掉、Agent 还没看到的）和这次新建的配对，
        新对象接过旧编号：transfer_ids=True 换编号；False 记一条映射。先配对象，再配数据块。"""
        pattern = (args.get("reidentify") or {}).get("suffix")
        out: dict = {"pairs": {}, "kinds": {}, "fresh": {}}
        tombs: list[str] = []
        for phase in ("object", "data"):
            users = self._users()
            live = {self._lid(a): a for a in self.objects}
            olds, kinds = {}, {}
            for cid, r in before.items():
                is_data = r.get("kind") == "data"
                if is_data != (phase == "data") or cid in out["pairs"]:
                    continue
                row = {"name": r["name"], "type": r["type"], "parent": r.get("parent")}
                if cid not in live or (is_data and r.get("users", 0) > 0 and users.get(live[cid], 0) == 0):
                    olds[cid], kinds[cid] = row, "gone"
            for cid in sorted(args.get("rebind") or {}):
                r = before.get(cid)
                if (r is not None and cid not in olds and cid in live
                        and (r.get("kind") == "data") == (phase == "data")):
                    olds[cid], kinds[cid] = {"name": r["name"], "type": r["type"], "parent": r.get("parent")}, "rebind"
            if phase == "object":
                for cid, row in sorted((args.get("no_recreate") or {}).items()):
                    if cid not in before and cid not in live:
                        olds[cid], kinds[cid] = ({"name": row.get("name"), "type": row.get("type"),
                                                  "parent": row.get("parent")}, "tomb")
            fresh = {}
            for a, o in self.objects.items():
                if self._lid(a) not in before and bool(o.get("data")) == (phase == "data"):
                    fresh[a] = {"name": o["name"], "type": o.get("type", "FAKE"),
                                "parent": self._lid(o["faces"].get("parent"))}
            pairs = match_identities(olds, fresh, pattern) if olds and fresh else {}
            for old, (new, level) in sorted(pairs.items()):
                kind = kinds[old]
                if kind == "rebind" or (kind == "gone" and old in live):
                    k = live[old]                       # 保留下来的旧对象、没人用了的旧数据块：换成新的，删掉旧的
                    self._remap(k, new)
                    del self.objects[k]
                    self.selection.discard(k)
                if self.transfer_ids:
                    self.objects[old] = self.objects.pop(new)
                    self._remap(new, old)
                else:
                    self._alias[new] = old
                if kind == "tomb":
                    tombs.append(old)
                elif level == "suffix":
                    want = before[old]["name"]
                    o = self.objects[self._aid(old)]
                    if o["name"] != want and all(x["name"] != want for x in self.objects.values()
                                                 if bool(x.get("data")) == (phase == "data")):
                        o["name"] = want
                out["pairs"][old] = [new, level]
                out["kinds"][old] = kind
                out["fresh"][new] = fresh[new]
        return out, tombs

    def _remove_recreated(self, tombs: list[str], created: set[str]) -> list[str]:
        """拦下补回人删掉的对象：删掉配上墓碑的新对象，以及这次新建、只给它用的数据块。"""
        removed = []
        for cid in tombs:
            a = self._aid(cid)
            o = self.objects.pop(a, None)
            if o is None:
                continue
            removed.append(cid)
            self.selection.discard(a)
            users = self._users()
            for face in o.get("refs", ()):
                t = o["faces"].get(face)
                if t in self.objects and self.objects[t].get("data") and self._lid(t) in created and not users.get(t):
                    del self.objects[t]
        self._drop_dangling()
        return removed

    def run_agent(self, args: dict) -> dict:
        st = self._begin(args)
        if "status" in st:
            return st
        out, error = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(out):
                exec(args["code"], {"scene": AgentScene(self), "__name__": "__rightofway_agent__"})
        except Exception:
            error = traceback.format_exc(limit=3)
        return self._finish(st, error, out.getvalue())

    def begin_agent(self, args: dict) -> dict:
        """不是脚本的修改（例如 MCP 代理转发的类型化工具调用）的前一半：核对、快照，不执行代码。"""
        st = self._begin(args)
        if "status" in st:
            return st
        self._n += 1
        token = f"t-{self._n}"
        self._pending[token] = st
        return {"status": "begun", "token": token}

    def finish_agent(self, args: dict) -> dict:
        """后一半：和 run_agent 执行完代码之后一样认回、恢复、核对。"""
        st = self._pending.pop(args["token"])
        return self._finish(st, args.get("error"), "")

    def _begin(self, args: dict) -> dict:
        t0 = time.perf_counter()
        self._set_alias(args)
        before = self.records()
        expected = args.get("expected")
        selected = lambda: sorted(self._lid(a) for a in self.selection)   # noqa: E731
        if expected is not None:                    # 和运行时以为的不一样：不执行，让运行时重开许可单
            changed = sorted((set(before) ^ set(expected))
                             | {cid for cid in before if cid in expected and before[cid]["fp"] != expected[cid]})
            sel = args.get("expected_selection")
            if changed or (sel is not None and sorted(sel) != selected()):
                return {"status": "replan", "changed": changed, "before": before,
                        "selected": selected(), "labels": self.labels()}
        protected = {cid: set(v) for cid, v in args.get("protected", {}).items()}
        protected = {cid: a for cid, a in protected.items() if cid in before and a}
        keep_alive = [c for c in args.get("keep_alive", []) if c in before and c not in protected]
        do_protect = args.get("protect", True)
        snap = {cid: self._snap(cid) for cid in list(protected) + keep_alive} if do_protect else {}
        sel_before = {self._lid(a) for a in self.selection}     # 按运行时编号记：对象被认回、换了应用编号也能对上
        return {"t0": t0, "args": args, "before": before, "protected": protected, "keep_alive": keep_alive,
                "do_protect": do_protect, "snap": snap, "sel_before": sel_before}

    def _finish(self, st: dict, error: Optional[str], stdout: str) -> dict:
        args, before, protected, keep_alive = st["args"], st["before"], st["protected"], st["keep_alive"]
        do_protect, snap, sel_before, t0 = st["do_protect"], st["snap"], st["sel_before"], st["t0"]
        selected = lambda: sorted(self._lid(a) for a in self.selection)   # noqa: E731
        identity, tombs = ({}, [])
        if args.get("reidentify") and do_protect:
            identity, tombs = self._identity(before, args)
        after_raw = self.records()

        full, partial, lost, merged = [], {}, [], {}
        if do_protect:
            for cid, asp in sorted(protected.items()):
                b, a = before[cid], after_raw.get(cid)
                if a is None:
                    (full if self.restore_deleted else lost).append(cid)
                    continue
                changed = ({x for x in b["aspects"] if a["aspects"].get(x) != b["aspects"][x]}
                           | (set(a["aspects"]) - set(b["aspects"])))
                conflict = changed if "*" in asp else changed & asp
                if not conflict:
                    continue
                if conflict == changed:
                    full.append(cid)
                else:
                    partial[cid] = (sorted(conflict), sorted(changed - conflict))
            for cid in keep_alive:                  # 祖先：不许删，面照样可以改
                if cid not in after_raw:
                    (full if self.restore_deleted else lost).append(cid)
        restored = []
        for cid in full:
            a = self._aid(cid)
            if a not in self.objects:
                a = cid                              # 恢复被删掉的对象：用运行时编号放回去
            self.objects[a] = self._unsnap(snap[cid])
            restored.append(cid)
        for cid, (faces, kept) in partial.items():
            obj = self.objects[self._aid(cid)]
            for f in faces:
                if f in snap[cid]["faces"]:
                    obj["faces"][f] = self._unsnap_value(snap[cid], f)
                else:
                    obj["faces"].pop(f, None)
            merged[cid] = {"restored": faces, "kept": kept}
        if full or partial:
            self._drop_dangling()                   # 恢复出来的对象，原来的父对象可能已经被 Agent 删掉了
        created = set(self.records()) - set(before)
        recreated = self._remove_recreated(tombs, created) if tombs else []
        if do_protect:
            self.selection = {self._aid(c) for c in sel_before if self._aid(c) in self.objects}
        final = self.records()
        inexact = [cid for cid in restored if final.get(cid, {}).get("cfp") != before[cid]["cfp"]] + lost
        return {
            "status": "ok", "before": before, "after_raw": after_raw, "after": final,
            "protected": {k: sorted(v) for k, v in protected.items()},
            "selected": selected(), "restored": restored, "merged": merged, "fallback": [],
            "inexact": inexact, "lost": lost, "renamed": [], "dangling": [], "candidates": [],
            "outside": {}, "ai_created": sorted(set(final) - set(before)), "identity": identity,
            "recreated": recreated, "aliases": self.aliases(), "error": error,
            "stdout": stdout[-4000:], "undo_pushed": False,
            "seconds": round(time.perf_counter() - t0, 4), "labels": self.labels(),
        }

    def aliases(self) -> dict[str, str]:
        """现在场景里、编号和运行时不同的对象：应用编号 → 运行时编号（交给会话保存）。"""
        return {a: l_ for a, l_ in sorted(self._alias.items()) if a in self.objects and a != l_}


COMMANDS_MARK = "<<<RIGHTOFWAY_COMMANDS>>>"


def _base(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)


class FakeBridge:
    """和 InProcessBridge 一样的接口：call(函数名, 参数) → 结果。结果经过一次 JSON 往返，和真的传输一样。"""

    def __init__(self, app: FakeApp) -> None:
        self.app = app

    @staticmethod
    def compile_commands(commands: list[dict], scene: Optional[str] = None) -> str:
        """类型化命令 → Agent 代码。命令的样子和 Unity MCP（CoplayDev）的 manage_gameobject 类似：
            {"action": "modify", "target": "Leaf_1", "set": {"location": [...], "color": "橙"}}
            {"action": "create", "name": "Rock_2", "set": {...}, "parent": "Trunk", "kind": "MESH"}
            {"action": "delete", "target": "Leaf_3"}
        每条单独执行，失败了记下来、接着执行下一条（和 batch_execute 一样不是事务）。
        结果打印在标准输出里：[[序号, "ok" 或出错原因], ...]。"""
        lines = ["import json as _json", "_out = []"]
        for i, c in enumerate(commands):
            a = c.get("action")
            if a == "modify":
                # 对象不在时第一句就出错，整条不生效
                body = "; ".join(f"scene.set({c['target']!r}, {f!r}, {v!r})" for f, v in c.get("set", {}).items()) or "pass"
            elif a == "create":
                body = (f"scene.create({c['name']!r}, {c.get('set') or {}!r}, parent={c.get('parent')!r}, "
                        f"kind={c.get('kind') or 'FAKE'!r}, refs={c.get('refs')!r}, data={bool(c.get('data'))!r})")
            elif a == "delete":
                body = f"scene.delete({c['target']!r})"
            else:
                body = f"raise ValueError({('不认识的命令：' + str(a))!r})"
            lines.append(f"try:\n    {body}\n    _out.append([{i}, 'ok'])\n"
                         f"except Exception as _e:\n    _out.append([{i}, repr(_e)])")
        lines.append(f"print({COMMANDS_MARK!r} + _json.dumps(_out, ensure_ascii=False))")
        return "\n".join(lines) + "\n"

    @staticmethod
    def command_results(stdout: str) -> dict[int, str]:
        """从执行结果的标准输出里取出每条命令的结果。"""
        for line in reversed(stdout.splitlines()):
            if line.startswith(COMMANDS_MARK):
                return {int(i): r for i, r in json.loads(line[len(COMMANDS_MARK):])}
        return {}

    def call(self, fn: str, args: Optional[dict] = None) -> dict:
        handlers = {"poll": self.app.poll, "run_agent": self.app.run_agent, "probe": self.app.probe,
                    "begin_agent": self.app.begin_agent, "finish_agent": self.app.finish_agent,
                    "notify": self.app.notify}
        if fn not in handlers:
            raise ValueError(f"FakeApp 不支持 {fn}")
        return json.loads(json.dumps(handlers[fn](args or {})))
