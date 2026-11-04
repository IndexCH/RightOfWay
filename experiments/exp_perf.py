"""实验 Q：3D 接入的性能（design_v0.5.md 第 16 节）。

在无界面的 Blender（pip 装的 bpy）里搭 N 个对象的场景（每个一个 32×16 的球、20 种材质轮流用），
用和平时一样的 SharedSession 走一遍，量每一步：
    连接          probe + 第一次观察
    轮询（没变）  人什么也没做时轮询一次
    精确的一步    AI 改一个对象（Obj_10 往 x 挪 1）
    整体的一步    人先挪了 Obj_3，AI 不知道，把所有对象往上抬 0.1（先重开许可单，再执行；Obj_3 的位置保住）
    再做 20 步    AI 每步改一个对象：账本增长多少
每一步记：总用时、Blender 被占住的时间（每次调用在 Blender 里执行的时间之和）、发过去和收回来的字节数、
调用了哪些接入函数；整体那一步还记每个对象算了几遍指纹（应该只算一次）；最后记账本文件的大小和存一次盘的用时。

    python -m experiments.exp_perf                        50、200、500、2000 个对象
    python -m experiments.exp_perf 50 500 --repo DIR      量另一份代码（例如修复之前的提交，用来对比）
    python -m experiments.exp_perf --socket               只量 SocketBridge 收一条大消息（以前耗时随大小平方增长）

需要 Python 3.11 + pip install bpy。结果追加到 experiments/results/perf.jsonl。
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "experiments" / "results" / "perf.jsonl"


def _setup_path(repo: str | None) -> None:
    if repo:
        sys.path.insert(0, str(Path(repo).resolve()))
    elif str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def make_scene(n: int, u: int = 32, v: int = 16, materials: int = 20) -> None:
    import bpy
    import bmesh
    from rightofway.blender.bridge import InProcessBridge
    InProcessBridge.reset()
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=u, v_segments=v, radius=0.5)
    base = bpy.data.meshes.new("base")
    bm.to_mesh(base)
    bm.free()
    mats = [bpy.data.materials.new(f"Mat_{i}") for i in range(materials)]
    scene = bpy.context.scene
    for i in range(n):
        me = base.copy()
        me.materials.append(mats[i % materials])
        o = bpy.data.objects.new(f"Obj_{i}", me)
        o.location = (i % 50, i // 50, 0)
        scene.collection.objects.link(o)
    bpy.data.meshes.remove(base)


def timed_bridge():
    from rightofway.blender.bridge import InProcessBridge

    class TimedBridge(InProcessBridge):
        """每次在 Blender 里执行都计时（Blender 被占住的时间），记下发过去和收回来的大小。"""

        def __init__(self):
            super().__init__()
            self.log = []
            self.fn = "?"

        def call(self, fn, args):
            self.fn = fn
            return super().call(fn, args)

        def execute(self, code: str) -> str:
            t = time.perf_counter()
            out = super().execute(code)
            self.log.append({"exec": time.perf_counter() - t, "req": len(code.encode("utf-8")),
                             "resp": len(out.encode("utf-8")), "fn": self.fn})
            return out
    return TimedBridge()


def summarize(log: list, since: int) -> dict:
    calls = log[since:]
    return {"calls": [c["fn"] for c in calls], "blender_s": round(sum(c["exec"] for c in calls), 4),
            "req_kb": round(sum(c["req"] for c in calls) / 1024, 1),
            "resp_kb": round(sum(c["resp"] for c in calls) / 1024, 1)}


def count_faces():
    """数一下每个对象这一步算了几次记录（增量观察时应该是每个变了的对象一次）。旧代码没有这个钩子就返回 None。"""
    code_ns = getattr(sys.modules.get("_rightofway_code"), "ns", None)
    if not code_ns or "_rec_obj" not in code_ns:
        return None
    counts: dict[str, int] = {}
    orig = code_ns["_rec_obj"]

    def wrapped(o, cache, *a):
        counts[o.name] = counts.get(o.name, 0) + 1
        return orig(o, cache, *a)
    code_ns["_rec_obj"] = wrapped

    def done():
        code_ns["_rec_obj"] = orig
        return counts
    return done


def bench(n: int) -> dict:
    import bpy
    from rightofway.blender.shared_session import SharedSession
    from rightofway.runtime import Runtime
    make_scene(n)
    verts = sum(len(o.data.vertices) for o in bpy.data.objects)
    br = timed_bridge()
    rt = Runtime()
    s = SharedSession(rt, br)
    params = inspect.signature(SharedSession.__init__).parameters
    row = {"objects": n, "verts": verts, "incremental": "incremental" in params}
    folder = tempfile.mkdtemp(prefix="rightofway_perf_")
    ledger = os.path.join(folder, f"ledger_{n}.json")

    t0 = time.perf_counter()
    s.start(ledger=ledger)
    row["start"] = {"total_s": round(time.perf_counter() - t0, 3), **summarize(br.log, 0)}
    row["ledger_kb_start"] = round(os.path.getsize(ledger) / 1024, 1)

    def step(label, fn):
        i = len(br.log)
        t = time.perf_counter()
        out = fn()
        row[label] = {"total_s": round(time.perf_counter() - t, 4), **summarize(br.log, i)}
        return out

    step("poll", s.poll)
    step("precise", lambda: s.run_agent("import bpy\nbpy.data.objects['Obj_10'].location.x += 1.0\n", label="precise"))
    row["ledger_kb_precise"] = round(os.path.getsize(ledger) / 1024, 1)
    bpy.data.objects["Obj_3"].location.z = 2.2              # 人挪了 Obj_3，运行时还没看到（代理模式：没有后台轮询）
    done = count_faces()
    rep = step("global", lambda: s.run_agent("import bpy\nfor o in bpy.data.objects:\n    o.location.z += 0.1\n",
                                             label="global"))
    counts = done() if done else None
    row["global"]["status"] = rep.status
    row["global"]["obj3_z"] = round(bpy.data.objects["Obj_3"].location.z, 3)        # 人的修改保住了：2.2
    if counts is not None:
        row["global"]["faces_per_object_max"] = max(counts.values()) if counts else 0
        row["global"]["objects_recomputed"] = len(counts)
    size0 = os.path.getsize(ledger)
    t = time.perf_counter()
    for k in range(20):
        s.run_agent(f"import bpy\nbpy.data.objects['Obj_{(k * 7) % n}'].location.y += 0.5\n", label=f"more{k}")
    row["more20_s"] = round(time.perf_counter() - t, 3)
    row["ledger_kb"] = round(size0 / 1024, 1)
    row["ledger_kb_after_20_more"] = round(os.path.getsize(ledger) / 1024, 1)
    obs = [f for f in os.listdir(folder) if ".obs." in f]           # 观察缓存（场景的记录）：单独一个文件，整份重写时才写
    row["obs_kb"] = round(sum(os.path.getsize(os.path.join(folder, f)) for f in obs) / 1024, 1)
    t = time.perf_counter()
    s._save()
    row["save_ms"] = round((time.perf_counter() - t) * 1000, 2)
    if row["incremental"]:                                  # 全量核对（兜底）一次的代价
        i = len(br.log)
        t = time.perf_counter()
        br.call("poll", s._dargs(verify=True))
        row["verify"] = {"total_s": round(time.perf_counter() - t, 3), **summarize(br.log, i)}
    return row


def socket_bench(mb: float = 13.5) -> dict:
    """SocketBridge 收一条大回复：以前每收 64 KB 就把收到的整段解析一遍（耗时随大小平方增长），现在只在结尾是 } 时解析。"""
    from rightofway.blender import bridge as b
    payload = json.dumps({"status": "success", "result": {"result": "x" * int(mb * 1024 * 1024)}}).encode()

    class FakeSock:
        def __init__(self, data: bytes, chunk: int = 65536):
            self.data, self.pos, self.chunk = data, 0, chunk

        def recv(self, k):
            k = min(k, self.chunk)
            out = self.data[self.pos:self.pos + k]
            self.pos += len(out)
            return out

    def old_receive(sock):                                 # 修复之前的做法（c362cd5）
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    out = {"mb": mb}
    t = time.perf_counter()
    assert b.receive_json(FakeSock(payload))["status"] == "success"
    out["new_s"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    assert old_receive(FakeSock(payload))["status"] == "success"
    out["old_s"] = round(time.perf_counter() - t, 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sizes", nargs="*", type=int, default=[50, 200, 500, 2000])
    ap.add_argument("--repo", default=None, help="量另一份代码（目录），例如修复之前的提交")
    ap.add_argument("--socket", action="store_true", help="只量 SocketBridge 收大消息")
    ap.add_argument("--label", default="", help="写进结果里的标签")
    args = ap.parse_args()
    _setup_path(args.repo)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = [socket_bench()] if args.socket else []
    if not args.socket:
        for n in args.sizes:
            rows.append(bench(n))
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    else:
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    with open(OUT, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"label": args.label, "repo": args.repo or "", "time": time.strftime("%Y-%m-%d %H:%M"),
                                 **r}, ensure_ascii=False) + "\n")
    if not args.socket:
        print("\n对象数 │ 精确一步 总/Blender │ 整体一步 总/Blender │ 轮询  │ 账本 KB：连接后 / 精确一步后 / 整体一步后 / 再 20 步后")
        for r in rows:
            p, g = r["precise"], r["global"]
            print(f"{r['objects']:6d} │ {p['total_s']:7.3f} / {p['blender_s']:7.3f} │ {g['total_s']:7.3f} / "
                  f"{g['blender_s']:7.3f} │ {r['poll']['total_s']:6.3f} │ {r.get('ledger_kb_start', '-')} / "
                  f"{r.get('ledger_kb_precise', '-')} / {r['ledger_kb']} / {r['ledger_kb_after_20_more']}")
    sys.stdout.flush()
    os._exit(0)                                            # pip 装的 bpy：直接退出，不等它收尾


if __name__ == "__main__":
    main()
