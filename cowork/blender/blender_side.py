"""在 Blender 里运行的代码。

这个文件有两种用法，所以它只能 import bpy 和 Python 标准库，不能 import cowork 包里的任何东西：

1. 方式一（同一份，实时）：整个文件的源码通过现成 Blender MCP 插件的 execute_code 发进
   正在运行的 Blender，末尾再加一行调用（见 bridge.py 的 build_call）。
2. 方式二（各自一份，存盘合并）：在无界面的 Blender 里执行同样的源码，打开 .blend 文件后
   列出对象、合并、保存。

对外的入口函数只有这几个，参数和返回值都是可以转成 JSON 的 dict：
    poll(args)        列出当前场景的对象和指纹                     方式一
    run_agent(args)   原子地完成：列对象 → 保护 → 执行 AI 脚本 → 比较 → 恢复   方式一
    cleanup_scenes(args) 删掉之前实验留下的场景                         方式一
    export_objects(args) 把指定对象写进一个临时 .blend（或 base64）     方式二（实时同步）
    apply_sync(args)  把另一边导出的对象按计划放进当前场景（带核对）    方式二（实时同步）
    new_file(args)    新建一个空的 .blend                            方式二
    dump_file(args)   打开一个 .blend，列出对象和指纹                方式二
    edit_file(args)   打开一个 .blend，执行一段脚本，保存（模拟 AI 用 computer use 改自己的副本）
    merge_file(args)  打开人的 .blend，按合并计划换入/删除对象，保存到新路径   方式二
结果用 _cowork_out() 打印在两个标记之间，调用方从标准输出里取出来。
"""
import array
import base64
import contextlib
import math
import hashlib
import io
import json
import os
import re
import tempfile
import time
import traceback
import uuid

import bpy

ID_KEY = "cowork_id"                 # 对象的系统 ID，存在对象的自定义属性里
UID_KEY = "cowork_uid"               # 盖章时的 session_uid，用来区分复制出来的对象（Shift+D 会连自定义属性一起复制）
CANDIDATE_KEY = "cowork_candidate"   # AI 候选版本：不参与追踪
CANDIDATE_COLLECTION = "Cowork_AI候选"
MARK_BEGIN = "<<<COWORK_JSON>>>"
MARK_END = "<<<COWORK_END>>>"
DIGITS = 5                           # 浮点数保留的小数位，避免无意义的微小差别

# ---------------------------------------------------------------------------
# 面：由 Blender 自己的数据描述（RNA）自动得到，不针对任何对象类型手工定义。
#
#   一个对象的"面" = 它的每一个顶层属性（location、scale、data、modifiers、material_slots……），
#   外加一个"所在集合"（Blender 把集合归属存在集合那边，对象的属性里没有）。
#   面的名字和显示名都来自 Blender，换一种对象类型、换一个 Blender 版本都不用改代码。
#
# 观察代码只需要知道一件和应用有关的事：哪些属性不是"内容"，只是查看状态或者派生出来的值。
# 下面这几条规则就是全部（按 Blender 的命名习惯，不是按对象类型）。
# ---------------------------------------------------------------------------
_VIEW_NAMES = {"rna_type", "name_full", "select", "hide", "show_expanded", "is_active"}
_VIEW_PREFIXES = ("bl_", "select_", "active_")        # 选中、界面里"当前激活"的项
_ID_BOOKKEEPING = None                                 # 所有数据块共有的记账属性（引用计数、标记……），首次用到时从 RNA 读出


def _id_bookkeeping():
    global _ID_BOOKKEEPING
    if _ID_BOOKKEEPING is None:
        _ID_BOOKKEEPING = {p.identifier for p in bpy.types.ID.bl_rna.properties} - {"name"}
    return _ID_BOOKKEEPING


def _content_props(struct):
    """一个结构体里算作"内容"的属性。"""
    is_id = isinstance(struct, bpy.types.ID)
    skip_id = _id_bookkeeping() if is_id else ()
    for p in struct.bl_rna.properties:
        pid = p.identifier
        if pid in skip_id or pid in _VIEW_NAMES or pid.startswith(_VIEW_PREFIXES):
            continue
        if pid.endswith("_index") and not p.is_animatable:
            continue                                   # 界面列表里选中第几项
        if p.type in ("POINTER", "COLLECTION"):
            yield p
            continue
        if p.is_readonly:
            continue                                   # 只读的都是派生值或记账
        if p.type == "FLOAT" and not p.is_animatable and getattr(p, "array_length", 0) >= 3:
            continue                                   # 派生的矩阵、尺寸（matrix_world、dimensions）
        yield p


def _base_name(name):
    """去掉 Blender 自动加的 .001 后缀。"""
    return re.sub(r"\.\d{3}$", "", name)


def _ref(idb):
    """对另一个数据块的引用：只记它是谁。"""
    if idb is None:
        return None
    if isinstance(idb, bpy.types.Object):
        return "obj:" + str(idb.get(ID_KEY) or idb.name)
    return "id:" + type(idb).__name__ + ":" + _base_name(idb.name)


def _val(v):
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return round(v, DIGITS)
    if isinstance(v, (set, frozenset)):
        return sorted(str(x) for x in v)
    try:
        return [_val(x) for x in v]
    except TypeError:
        return str(v)


def _md5(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


BULK = 64          # 超过这么多项的集合（网格顶点等）用 foreach_get 整块取值
MAX_DEPTH = 4


def _bulk(coll):
    """大集合：对每个数值属性整块取出来算哈希。"""
    n = len(coll)
    h = hashlib.md5(f"n{n}".encode())
    first = coll[0]
    for p in _content_props(first):
        if p.type not in ("FLOAT", "INT", "BOOLEAN"):
            continue
        width = max(1, getattr(p, "array_length", 0) or 1)
        if getattr(p, "is_array", False):
            try:                                       # 抽查几项：长度不固定的数组（例如多边形的顶点列表）跳过，由别的属性覆盖
                if any(len(getattr(coll[i], p.identifier)) != width for i in {0, n // 4, n // 2, 3 * n // 4, n - 1}):
                    continue
            except TypeError:
                continue
        try:
            if p.type == "BOOLEAN":
                buf = [False] * (n * width)
                coll.foreach_get(p.identifier, buf)
                h.update(p.identifier.encode() + bytes(bytearray(1 if x else 0 for x in buf)))
            else:
                buf = array.array("f" if p.type == "FLOAT" else "i", [0]) * (n * width)
                coll.foreach_get(p.identifier, buf)
                h.update(p.identifier.encode() + buf.tobytes())
        except Exception:
            continue                                   # 长度不固定的数组等，跳过
    return "bulk:" + h.hexdigest()[:12]


def _walk(v, depth, cache, in_object):
    """把一个属性值变成可以比较的结构。
    in_object=True 表示在对象自己的面里：引用到的数据块（网格、材质）连内容一起算，
    这样 AI 改了共用材质的颜色，用这个材质的对象的"材质"面会变。
    在数据块内容里再遇到的引用只记它是谁，避免重复计算。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return _val(v)
    if isinstance(v, bpy.types.ID):
        if getattr(v, "is_embedded_data", False):
            return _id_content(v, cache)               # 内嵌数据（材质的节点树）算作内容
        if in_object and not isinstance(v, bpy.types.Object):
            return [_ref(v), _id_content(v, cache)]
        return _ref(v)
    if isinstance(v, bpy.types.bpy_prop_collection):
        if len(v) == 0:
            return []
        if len(v) > BULK:
            return _bulk(v)
        return [_walk_struct(x, depth + 1, cache, in_object) for x in v
                if not str(getattr(x, "name", "")).startswith(".")]      # 以 . 开头的是内部数据（例如选中状态）
    if isinstance(v, bpy.types.bpy_struct):
        if depth >= 1:
            for key in ("identifier", "name"):         # 嵌套结构里指向别处的指针（例如节点连线）只记名字
                if key in v.bl_rna.properties:
                    return f"{type(v).__name__}:{getattr(v, key)}"
        return _walk_struct(v, depth + 1, cache, in_object)
    return _val(v)


def _walk_struct(s, depth, cache, in_object):
    if depth > MAX_DEPTH:
        return None
    out = {}
    is_id = isinstance(s, bpy.types.ID)
    for p in _content_props(s):
        if is_id and p.identifier == "name":
            continue      # 数据块的名字属于"它是谁"（由引用记录），不属于内容
        try:
            v = getattr(s, p.identifier)
        except Exception:
            continue
        if (p.type == "COLLECTION" and isinstance(s, bpy.types.ID) and len(v) > 0
                and isinstance(v[0], bpy.types.ID)):
            continue      # 数据块里的"引用列表"（例如网格的材质列表）在拥有它的对象那一面上已经算过
        out[p.identifier] = _walk(v, depth, cache, in_object)
    try:
        keys = [k for k in s.keys() if not k.startswith("cowork_")]      # 自定义属性（几何节点的输入也在这里）
        if keys:
            out["[props]"] = sorted((k, repr(s[k].to_dict() if hasattr(s[k], "to_dict") else
                                            s[k].to_list() if hasattr(s[k], "to_list") else s[k])) for k in keys)
    except Exception:
        pass
    return out


def _id_content(idb, cache):
    key = ("content", idb.as_pointer())
    if key not in cache:
        cache[key] = _md5(_walk_struct(idb, 1, cache, in_object=False))
    return cache[key]


_EXTRA_FACES = ("users_collection",)   # 对象属性里没有、但属于对象状态的：所在的集合


def object_faces(o, cache):
    """对象的每个面一个指纹。"""
    faces = {}
    for p in _content_props(o):
        try:
            v = getattr(o, p.identifier)
        except Exception:
            continue
        faces[p.identifier] = _md5(_walk(v, 0, cache, in_object=True))
    faces["users_collection"] = _md5(sorted(c.name for c in o.users_collection))
    return faces


def face_labels():
    """面的显示名，来自 Blender 自己的界面文字（Blender 界面是中文时就是中文）。"""
    tr = getattr(bpy.app.translations, "pgettext_iface", lambda x: x)
    labels = {p.identifier: tr(p.name) for p in bpy.types.Object.bl_rna.properties}
    labels["users_collection"] = tr("Collections")
    return labels


# ---------------------------------------------------------------------------
# 面的值：写成一小段人能读懂的文字，用来告诉 AI"原来是多少、现在是多少"。
# 只看 RNA 的类型（数字、向量、枚举、引用、列表……），不看属性名，所以对任何对象类型都一样。
# ---------------------------------------------------------------------------
MAX_SHOW = 60


def _fmt(x):
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _show_value(p, v):
    tr = getattr(bpy.app.translations, "pgettext_iface", lambda x: x)
    t = p.type
    if t == "POINTER":
        if v is None:
            return tr("None")
        if isinstance(v, bpy.types.ID):
            return v.name
        return None                                    # 嵌套结构：只说"变了"
    if t == "COLLECTION":
        n = len(v)
        names = [str(getattr(x, "name", "") or "") for x in list(v)[:5]]
        names = [x for x in names if x and not x.startswith(".")]
        return f"{n} 项" + (("：" + "、".join(names) + ("…" if n > 5 else "")) if names else "")
    if t == "ENUM":
        if getattr(p, "is_enum_flag", False):
            return "、".join(sorted(str(x) for x in v)) or tr("None")
        item = p.enum_items.get(v) if isinstance(v, str) else None
        return tr(item.name) if item is not None else str(v)
    if t == "STRING":
        return str(v)
    dims = [d for d in (getattr(p, "array_dimensions", None) or ()) if d]
    is_arr = getattr(p, "is_array", False) and (getattr(p, "array_length", 0) or 0) > 0
    rot = getattr(p, "unit", "") == "ROTATION"

    def one(x):
        if t == "BOOLEAN":
            return "是" if x else "否"
        if t == "FLOAT":
            return _fmt(math.degrees(x)) + "°" if rot else _fmt(x)
        return str(x)
    if is_arr:
        if len(dims) > 1:
            return f"{math.prod(dims)} 个数"
        vals = list(v)
        if len(vals) > 4:
            return f"{len(vals)} 个数"
        return "(" + ", ".join(one(x) for x in vals) + ")"
    return one(v)


def object_values(o, faces):
    """对象指定几个面的值（文字）。取不到的面不返回，调用方只说"变了"。"""
    out = {}
    for face in faces:
        try:
            if face == "users_collection":
                out[face] = "、".join(sorted(c.name for c in o.users_collection)) or "无"
                continue
            p = o.bl_rna.properties.get(face)
            if p is None:
                continue
            text = _show_value(p, getattr(o, face))
            if text is not None:
                out[face] = text if len(text) <= MAX_SHOW else text[:MAX_SHOW] + "…"
        except Exception:
            continue
    return out


def _fps(faces):
    """内容指纹（不含名字）和完整指纹。"""
    keys = sorted(faces)
    return (_md5([faces[k] for k in keys if k != "name"]), _md5([faces[k] for k in keys]))


# ---------------------------------------------------------------------------
# 对象编号
# ---------------------------------------------------------------------------
def _scene(args):
    name = (args or {}).get("scene")
    if name:
        sc = bpy.data.scenes.get(name)
        if sc is None:
            raise RuntimeError(f"找不到场景：{name}")
        return sc
    return bpy.context.scene


def _tracked(scene):
    """这个场景里需要追踪的对象：本地对象，不是 AI 候选，不在候选集合里。"""
    cand = bpy.data.collections.get(CANDIDATE_COLLECTION)
    cand_objs = set(o.as_pointer() for o in cand.all_objects) if cand else set()
    return [o for o in scene.objects
            if o.library is None and not o.get(CANDIDATE_KEY) and o.as_pointer() not in cand_objs]


def ensure_ids(scene, prefix="h-", deterministic=False):
    """给没有编号的对象盖章。复制出来的对象会带着原对象的编号，这里把它们区分开。

    deterministic=True 用于方式二：同一个文件无论打开几次，没盖章的对象都得到同样的编号
    （由名字算出）。前缀区分是人那一份（h-）还是 AI 那一份（a-）新建的，避免两边同名对象撞号。
    """
    objs = sorted(_tracked(scene), key=lambda o: o.name)
    groups = {}
    for o in objs:
        cid = o.get(ID_KEY)
        if cid:
            groups.setdefault(cid, []).append(o)
    for cid, group in groups.items():
        if len(group) > 1:
            keeper = next((o for o in group if o.get(UID_KEY) == getattr(o, "session_uid", None)), group[0])
            for o in group:
                if o is not keeper:
                    del o[ID_KEY]
    new = []
    for o in objs:
        if not o.get(ID_KEY):
            suffix = hashlib.md5(o.name.encode("utf-8")).hexdigest()[:10] if deterministic else uuid.uuid4().hex[:10]
            o[ID_KEY] = prefix + suffix
            new.append(o[ID_KEY])
        if not deterministic:
            uid = getattr(o, "session_uid", None)
            if uid is not None and o.get(UID_KEY) != uid:
                o[UID_KEY] = uid
    return new


def _view_layer(scene):
    try:
        if bpy.context.scene == scene and bpy.context.view_layer is not None:
            return bpy.context.view_layer
    except Exception:
        pass
    return scene.view_layers[0] if len(scene.view_layers) else None


def _selected(o, vl):
    try:
        return bool(o.select_get(view_layer=vl)) if vl is not None else bool(o.select_get())
    except Exception:
        return False


def records(scene, values=None, only=None):
    """列出对象：{编号: {名字、类型、指纹……}}。编辑模式中的对象先把网格同步出来再算指纹。

    values：None 不带面的值；"all" 带上所有面的值；
            {编号: {面: 指纹}}（上次看到的）则只带和上次不一样的面的值（新对象带全部），这样每次传的数据很少。
    only：只列这些编号的对象。"""
    cache = {}
    out = {}
    vl = _view_layer(scene)
    for o in _tracked(scene):
        cid = o.get(ID_KEY)
        if not cid or (only is not None and cid not in only):
            continue
        editing = o.mode == "EDIT"
        if editing:
            try:
                o.update_from_editmode()
            except Exception:
                pass
        aspects = object_faces(o, cache)
        cfp, fp = _fps(aspects)
        rec = {
            "id": cid, "name": o.name, "type": o.type, "cfp": cfp, "fp": fp, "aspects": aspects,
            "parent": (o.parent.get(ID_KEY) if o.parent else None),
            "collections": sorted(c.name for c in o.users_collection),
            "editing": editing, "selected": _selected(o, vl),
        }
        if values is not None:
            if values == "all" or cid not in values:
                want = list(aspects)
            else:
                old = values[cid]
                want = [f for f, fp_ in aspects.items() if old.get(f) != fp_]
            if want:
                rec["values"] = object_values(o, want)
        out[cid] = rec
    return out


def _find(scene, cid, exclude=()):
    for o in _tracked(scene):
        if o.get(ID_KEY) == cid and o.as_pointer() not in exclude:
            return o
    return None


# ---------------------------------------------------------------------------
# 从另一个 .blend 换入对象（保护/恢复、合并都用它）
# ---------------------------------------------------------------------------
# 顺序有讲究：先处理网格等数据，再处理材质，这样被删掉的临时网格用过的材质也能被清理掉
_DEDUP_COLLECTIONS = ("meshes", "curves", "lights", "cameras", "materials", "node_groups", "images")


def _content_fp(idblock, cache):
    """去重用：内容相同的数据块合并成一个。网格还要比较它挂着的材质，否则合并后会用错材质。"""
    mats = ([[_ref(m), _id_content(m, cache) if m else None] for m in idblock.materials]
            if isinstance(idblock, (bpy.types.Mesh, bpy.types.Curve)) else [])
    return _md5([_id_content(idblock, cache), mats])


def _append(path, names):
    """从 path 追加指定名字的对象。返回 {文件里的名字: 新对象}、间接带进来的对象、追加前各类数据块的指针。"""
    before_objs = set(o.as_pointer() for o in bpy.data.objects)
    before = {c: set(x.as_pointer() for x in getattr(bpy.data, c)) for c in _DEDUP_COLLECTIONS}
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        wanted = [n for n in names if n in src.objects]
        dst.objects = list(wanted)
    loaded = {n: o for n, o in zip(wanted, dst.objects) if o is not None}
    loaded_ptrs = set(o.as_pointer() for o in loaded.values())
    extras = [o for o in bpy.data.objects if o.as_pointer() not in before_objs and o.as_pointer() not in loaded_ptrs]
    return loaded, extras, before


def _settle(scene, extras, before, report, tidy=True):
    """处理追加带来的副作用：
    - 间接带进来的对象（例如父对象）：如果场景里有同编号的对象，就把引用改指向它，然后删掉副本；
      找不到的记为"悬空引用"。
    - 重复的材质、网格：内容相同就合并成一个，避免出现一堆 .001。"""
    for extra in extras:
        cid = extra.get(ID_KEY)
        canonical = _find(scene, cid, exclude={e.as_pointer() for e in extras}) if cid else None
        if canonical is not None:
            extra.user_remap(canonical)
        else:
            report.setdefault("dangling", []).append(extra.name)
        bpy.data.objects.remove(extra, do_unlink=True)
    cache = {}
    for coll in _DEDUP_COLLECTIONS:
        pool = getattr(bpy.data, coll)
        for new in [x for x in pool if x.as_pointer() not in before[coll]]:
            if new.users == 0:
                pool.remove(new)
                continue
            for old in pool:
                if (old.as_pointer() in before[coll] and _base_name(old.name) == _base_name(new.name)
                        and _content_fp(old, cache) == _content_fp(new, cache)):
                    new.user_remap(old)
                    pool.remove(new)
                    break
        # 被换下来、已经没人用的旧数据块删掉（存盘时本来也会丢掉），把新数据块的 .001 后缀去掉。
        # 只在合并文件时做；在人正在用的 Blender 里（方式一）不动人的孤立数据。
        if not tidy:
            continue
        for x in [x for x in pool if x.users == 0 and not x.use_fake_user]:
            pool.remove(x)
        for x in pool:
            base = _base_name(x.name)
            if base != x.name and base not in pool:
                x.name = base


def _link(scene, obj, collections):
    linked = False
    for cname in collections:
        coll = bpy.data.collections.get(cname)
        if cname == scene.collection.name:
            coll = scene.collection
        if coll is not None and obj.name not in coll.objects:
            coll.objects.link(obj)
            linked = True
    if not linked and not obj.users_collection:
        scene.collection.objects.link(obj)


def _set_collections(scene, obj, collections):
    wanted = set(collections)
    for coll in list(obj.users_collection):
        if coll.name not in wanted:
            coll.objects.unlink(obj)
    _link(scene, obj, collections)


def _put_in_place(scene, new, cid, name, collections, keep_old_as_candidate=False):
    """用 new 替换场景里编号为 cid 的对象（没有就新放进去）。替换时子对象、
    修改器里的引用都转到 new 上；所在集合按 collections 设置。
    keep_old_as_candidate=True 时，被替换下来的旧对象不删，放进候选集合。返回是否留了候选。"""
    existing = _find(scene, cid)
    new[ID_KEY] = cid
    uid = getattr(new, "session_uid", None)
    if uid is not None:
        new[UID_KEY] = uid
    kept = False
    if existing is not None:
        existing.user_remap(new)
        if keep_old_as_candidate:
            _put_candidate(scene, existing, cid, name)
            kept = True
        else:
            bpy.data.objects.remove(existing, do_unlink=True)
    _set_collections(scene, new, collections)
    new.name = name
    return kept


class CopyError(Exception):
    pass


def _plain(v):
    if hasattr(v, "copy") and not isinstance(v, (str, bpy.types.bpy_struct)):
        return v.copy()                                # Vector、Matrix、Euler……
    if isinstance(v, bpy.types.bpy_prop_array):
        return list(v)
    return v


def _copy_struct(dst, src, scene, depth=0):
    """把 src 里算作内容的属性逐个复制到 dst。能复制多少复制多少，最后由指纹核对。"""
    if depth > MAX_DEPTH or dst is None or src is None:
        return
    props = sorted(_content_props(dst), key=lambda p: (p.type != "ENUM", p.type in ("FLOAT", "INT")))
    for p in props:
        if p.identifier not in src.bl_rna.properties:
            continue
        try:
            _copy_value(dst, p, getattr(src, p.identifier), scene, depth)
        except Exception:
            pass
    try:
        for k in [k for k in dst.keys() if not k.startswith("cowork_")]:
            del dst[k]
        for k in src.keys():
            if not k.startswith("cowork_"):
                dst[k] = src[k]
    except Exception:
        pass


def _copy_value(dst, p, v, scene, depth):
    pid = p.identifier
    if p.type == "POINTER":
        if p.is_readonly:
            sub = getattr(dst, pid)
            if isinstance(sub, bpy.types.ID) and not getattr(sub, "is_embedded_data", False):
                return
            _copy_struct(sub, v, scene, depth + 1)
            return
        if isinstance(v, bpy.types.Object) and v.get(ID_KEY):
            v = _find(scene, v.get(ID_KEY)) or v       # 指向场景里同一个对象，而不是追加进来的副本
        setattr(dst, pid, v)
    elif p.type == "COLLECTION":
        _copy_collection(getattr(dst, pid), v, scene, depth + 1)
    else:
        setattr(dst, pid, _plain(v))


def _copy_collection(dcoll, scoll, scene, depth):
    if len(dcoll) == len(scoll):
        for d, s_ in zip(dcoll, scoll):
            _copy_struct(d, s_, scene, depth)
        return
    # 项数不同：按 Blender 集合常见的 new/remove/clear 接口重建；接口不认识就放弃，由调用方整个换回
    if not hasattr(dcoll, "new"):
        raise CopyError("集合不能重建")
    if hasattr(dcoll, "clear"):
        dcoll.clear()
    else:
        for x in list(dcoll):
            dcoll.remove(x)
    for s_ in scoll:
        made = None
        name, typ = getattr(s_, "name", None), getattr(s_, "type", None)
        for attempt in ((lambda: dcoll.new(name=name, type=typ)), (lambda: dcoll.new(name, typ)),
                        (lambda: dcoll.new(type=typ)), (lambda: dcoll.new(typ)),
                        (lambda: dcoll.new(name=name)), (lambda: dcoll.new())):
            try:
                made = attempt()
                break
            except Exception:
                continue
        if made is None:
            raise CopyError("集合项不能新建")
        _copy_struct(made, s_, scene, depth)


def _copy_face(scene, dst, face, src, src_rec):
    if face == "users_collection":
        _set_collections(scene, dst, src_rec.get("collections", []))
    elif face == "name":
        dst.name = src_rec["name"]
    else:
        p = dst.bl_rna.properties.get(face)
        if p is None:
            raise CopyError(face)
        _copy_value(dst, p, getattr(src, face), scene, 0)


_FACE_ORDER = {"data": 0}     # 先换数据块，它会连带改变别的面（例如材质槽），后面的面再按需要改回来


def apply_faces(scene, dst, want):
    """want = {面: (来源对象, 来源记录, 期望的指纹)}。
    先把和期望不同的面从各自的来源复制过来；再核对一遍，被连带改掉的面再复制一次。
    返回仍然对不上的面（调用方据此决定是否整个换回）。"""
    def mismatched():
        now = object_faces(dst, {})
        return [f for f, (_, _, fp) in want.items() if now.get(f) != fp]

    for _ in range(2):
        todo = mismatched()
        if not todo:
            return []
        for face in sorted(todo, key=lambda f: (_FACE_ORDER.get(f, 1), f == "name", f)):
            src, rec, _fp = want[face]
            try:
                _copy_face(scene, dst, face, src, rec)
            except Exception:
                pass
    return mismatched()


def _put_candidate(scene, new, cid, name):
    coll = bpy.data.collections.get(CANDIDATE_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(CANDIDATE_COLLECTION)
        scene.collection.children.link(coll)
        coll.hide_viewport = True
        coll.hide_render = True
    for old in list(coll.objects):
        if old.get(CANDIDATE_KEY) == cid:
            bpy.data.objects.remove(old, do_unlink=True)   # 只保留最新的候选
    if ID_KEY in new:
        del new[ID_KEY]
    new[CANDIDATE_KEY] = cid
    coll.objects.link(new)
    new.name = f"{name} [AI候选]"
    return new


# ---------------------------------------------------------------------------
# 方式一：同一份，实时
# ---------------------------------------------------------------------------
def poll(args):
    """args.known = {编号: {面: 指纹}}：只带回和它不一样的面的值；没有 known 时带回全部值。"""
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"))
    known = args.get("known")
    return {"records": records(scene, values=known if known is not None else "all"),
            "time": time.time(), "labels": face_labels()}


def _other_objects(scene):
    """场景以外的对象（名字 → 指纹）。用来发现 AI 改到了追踪范围以外的东西（R18：不能静默漏报）。"""
    inside = set(o.as_pointer() for o in scene.objects)
    cache, out = {}, {}
    for o in bpy.data.objects:
        if o.as_pointer() in inside or o.library is not None or o.get(CANDIDATE_KEY):
            continue
        out[o.name] = _fps(object_faces(o, cache))[1]
    return out


def _try_undo_push(message):
    """AI 执行完推一个撤销步，让人按 Ctrl+Z 时能单独撤掉这一段。无界面模式下没有窗口，会失败。"""
    try:
        wm = bpy.context.window_manager
        if not wm.windows:
            return False
        with bpy.context.temp_override(window=wm.windows[0]):
            bpy.ops.ed.undo_push(message=message)
        return True
    except Exception:
        return False


def _selection_state(scene, vl):
    """人的选中状态：按对象编号记（对象被整个换回后是一个新的 Python 对象，编号不变）。"""
    sel = {o.get(ID_KEY): _selected(o, vl) for o in _tracked(scene) if o.get(ID_KEY)}
    try:
        act = vl.objects.active if vl is not None else None
        active = act.get(ID_KEY) if act is not None else None
    except Exception:
        active = None
    return sel, active


def _restore_selection(scene, vl, state):
    sel, active = state
    for o in _tracked(scene):
        want = sel.get(o.get(ID_KEY), False)          # AI 新建的对象不选中
        try:
            if _selected(o, vl) != want:
                o.select_set(want, view_layer=vl) if vl is not None else o.select_set(want)
            if active is not None and o.get(ID_KEY) == active and vl is not None and vl.objects.active != o:
                vl.objects.active = o
        except Exception:
            continue


def run_agent(args):
    """原子地执行 AI 的一段脚本。Blender 是单线程的，这个函数执行期间人不可能同时操作。

    args:
      code         AI 的脚本
      protected    {编号: [要保护的面]}，"*" 表示整个对象（人碰过或正被占用的部分）
      known        运行时上次看到的 {编号: {"fp", "aspects"}}；和现在不同的，说明人刚改过，也要保护
      granularity  "aspect"（默认，按面保护）或 "object"（整个对象）
      protect      False 表示对照组：不保护
      protect_selected  True 表示人选中的对象也不能改（"选中即占用"选项）
      scene        场景名（可选）
    known 是这个 AI 上次看到的场景。多个 AI 时每个 AI 各有一份：别人（人或别的 AI）在它看过之后改的，
    这次也受保护，它的脚本改到了就恢复成别人的版本（后到的让先到的）。
    """
    t0 = time.perf_counter()
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"))
    known = args.get("known", {})
    before = records(scene, values={cid: k["aspects"] for cid, k in known.items()})
    editing = sorted(cid for cid, r in before.items() if r["editing"])
    if editing and args.get("defer_if_editing", True):
        return {"status": "deferred", "reason": "edit_mode", "editing": editing, "before": before}

    by_object = args.get("granularity", "aspect") == "object"
    protected = {cid: set(v) for cid, v in args.get("protected", {}).items()}
    selected = sorted(cid for cid, r in before.items() if r["selected"])
    if args.get("protect_selected"):
        for cid in selected:
            protected[cid] = {"*"}
    human_since = []
    for cid, rec in before.items():
        k = known.get(cid)
        if k is None:
            protected[cid] = {"*"}                  # 上次观察之后人新建的对象：整个保护
            human_since.append(cid)
        elif k["fp"] != rec["fp"]:
            changed = {"*"} if by_object else {a for a in rec["aspects"] if k["aspects"].get(a) != rec["aspects"][a]}
            protected.setdefault(cid, set()).update(changed)
            human_since.append(cid)
    protected = {cid: a for cid, a in protected.items() if cid in before and a}
    do_protect = args.get("protect", True)
    snap_path = None
    if protected and do_protect:
        snap_path = os.path.join(tempfile.gettempdir(), f"cowork_snap_{uuid.uuid4().hex[:8]}.blend")
        bpy.data.libraries.write(snap_path, set(o for o in _tracked(scene) if o.get(ID_KEY) in protected),
                                 fake_user=False)
    outside_before = _other_objects(scene) if args.get("watch_outside", True) else None
    vl = _view_layer(scene)
    sel_state = _selection_state(scene, vl)

    out = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(out):
            exec(args["code"], {"bpy": bpy, "__name__": "__cowork_agent__"})
    except Exception:
        error = traceback.format_exc(limit=3)
    ai_created = ensure_ids(scene, prefix="a-")
    before_faces = {cid: r["aspects"] for cid, r in before.items()}
    after = records(scene, values=before_faces)

    # 哪些要整个换回，哪些只把冲突的面换回
    full, partial = [], {}
    for cid, asp in sorted(protected.items()):
        b, a = before[cid], after.get(cid)
        if a is None:
            full.append(cid)                        # AI 删掉了受保护的对象
            continue
        changed = {x for x in b["aspects"] if a["aspects"].get(x) != b["aspects"][x]}
        conflict = changed if "*" in asp else changed & asp
        if not conflict:
            continue                                # AI 只改了人没碰过的面：照常生效
        if conflict == changed:
            full.append(cid)
        else:
            partial[cid] = sorted(conflict)

    restored, merged, candidates, fallback, report = [], {}, [], [], {}
    keep = args.get("keep_candidates", False)
    if snap_path and (full or partial):
        loaded, extras, bef = _append(snap_path, [before[c]["name"] for c in full + list(partial)])
        for cid in full:
            obj = loaded[before[cid]["name"]]
            if _put_in_place(scene, obj, cid, before[cid]["name"], before[cid]["collections"],
                             keep_old_as_candidate=keep):
                candidates.append(cid)
            restored.append(cid)
        for cid, asp in partial.items():
            tmp, cur = loaded[before[cid]["name"]], _find(scene, cid)
            ai_copy = cur.copy()                     # AI 的版本：保留下来的面从这里取
            if keep:
                cand = cur.copy()
                if cur.data is not None:
                    cand.data = cur.data.copy()
                _put_candidate(scene, cand, cid, before[cid]["name"])
                candidates.append(cid)
            want = {}
            for face in before[cid]["aspects"]:
                if face in asp:
                    want[face] = (tmp, before[cid], before[cid]["aspects"][face])
                else:
                    want[face] = (ai_copy, after[cid], after[cid]["aspects"][face])
            left = apply_faces(scene, cur, want)
            bpy.data.objects.remove(ai_copy, do_unlink=True)
            if not left:
                merged[cid] = {"restored": asp,
                               "kept": sorted(x for x in before[cid]["aspects"]
                                              if after[cid]["aspects"][x] != before[cid]["aspects"][x]
                                              and x not in asp)}
                bpy.data.objects.remove(tmp, do_unlink=True)
            else:                                    # 按面复制没能完全还原：退回到整个换回
                _put_in_place(scene, tmp, cid, before[cid]["name"], before[cid]["collections"])
                restored.append(cid)
                fallback.append({"id": cid, "faces": left})
        _settle(scene, extras, bef, report, tidy=False)
    if snap_path:
        try:
            os.remove(snap_path)
        except OSError:
            pass

    if do_protect and args.get("restore_selection", True):
        _restore_selection(scene, vl, sel_state)     # 选中状态是人的界面状态，AI 的脚本改了就还原
    final = records(scene, values=before_faces)
    inexact = [cid for cid in restored if final.get(cid, {}).get("cfp") != before[cid]["cfp"]]
    renamed = [cid for cid in restored if cid not in inexact and final[cid]["name"] != before[cid]["name"]]
    outside = {}
    if outside_before is not None:
        outside_after = _other_objects(scene)
        outside = {"created": sorted(set(outside_after) - set(outside_before)),
                   "deleted": sorted(set(outside_before) - set(outside_after)),
                   "modified": sorted(n for n in set(outside_before) & set(outside_after)
                                      if outside_before[n] != outside_after[n])}
    undo_pushed = _try_undo_push("Cowork: AI 脚本") if args.get("undo_push", True) else False
    return {
        "status": "ok", "before": before, "after_raw": after, "after": final,
        "human_since": sorted(human_since), "protected": {k: sorted(v) for k, v in protected.items()},
        "selected": selected,
        "restored": restored, "merged": merged, "fallback": fallback,
        "inexact": inexact, "renamed": renamed, "dangling": report.get("dangling", []),
        "candidates": candidates, "outside": outside,
        "ai_created": ai_created, "error": error, "stdout": out.getvalue()[-4000:],
        "undo_pushed": undo_pushed, "seconds": round(time.perf_counter() - t0, 4), "labels": face_labels(),
    }


def cleanup_scenes(args):
    """删掉之前实验留下的场景，以及只属于这些场景的对象和它们用过的数据。只动名字以 prefix 开头的场景。"""
    prefix = args["prefix"]
    doomed = [sc for sc in bpy.data.scenes if sc.name.startswith(prefix)]
    keep = [sc for sc in bpy.data.scenes if not sc.name.startswith(prefix)]
    if not doomed:
        return {"scenes": [], "objects": 0}
    if not keep:
        keep = [bpy.data.scenes.new("Scene")]
    for w in bpy.context.window_manager.windows:
        if w.scene in doomed:
            w.scene = keep[0]
    objs, datas, mats, colls = set(), set(), set(), set()
    for sc in doomed:
        for o in sc.objects:
            objs.add(o)
        for c in sc.collection.children_recursive:
            colls.add(c)
    names = [sc.name for sc in doomed]
    for sc in doomed:
        bpy.data.scenes.remove(sc)
    removed = 0
    for o in objs:
        if not o.users_scene:
            if o.data is not None:
                datas.add(o.data)
            for sl in o.material_slots:
                if sl.material is not None:
                    mats.add(sl.material)
            bpy.data.objects.remove(o, do_unlink=True)
            removed += 1
    for pool, items in ((bpy.data.meshes, datas), (bpy.data.materials, mats), (bpy.data.collections, colls)):
        for x in list(items):
            try:
                if x.users == 0:
                    pool.remove(x)
            except Exception:
                pass
    for x in list(datas):
        for pool in (bpy.data.lights, bpy.data.curves, bpy.data.cameras):
            try:
                if x.users == 0 and x.name in pool and pool[x.name] == x:
                    pool.remove(x)
            except Exception:
                pass
    return {"scenes": names, "objects": removed}


# ---------------------------------------------------------------------------
# 方式二：各自一个窗口，实时同步。人和 AI 各开一个 Blender，运行时通过两边的 MCP 插件在它们之间搬对象。
# ---------------------------------------------------------------------------
def export_objects(args):
    """把指定编号的对象（连同网格、材质等依赖）写进一个临时 .blend。
    inline=True 时读成 base64 放进返回值（两个 Blender 不在同一台机器上时用），否则返回文件路径。"""
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"))
    ids = set(args["ids"])
    objs = [o for o in _tracked(scene) if o.get(ID_KEY) in ids]
    recs = records(scene, only=ids)
    path = os.path.join(tempfile.gettempdir(), f"cowork_x_{uuid.uuid4().hex[:10]}.blend")
    inline = bool(args.get("inline"))
    bpy.data.libraries.write(path, set(objs), fake_user=False, compress=inline)
    out = {"records": recs, "names": {o.get(ID_KEY): o.name for o in objs}}
    if inline:
        with open(path, "rb") as f:
            out["data"] = base64.b64encode(f.read()).decode("ascii")
        os.remove(path)
    else:
        out["path"] = path
    return out


def apply_sync(args):
    """把另一边导出的对象按计划放进当前场景。

    args:
      path / data   export_objects 的结果（文件路径，或 base64）
      items         [{"id", "op": create|whole|faces|delete, "name": 源文件里的名字,
                      "faces": [...], "record": 源记录, "guard": {面: 指纹}}]
                    guard 是规划时看到的这一边的指纹：执行时如果已经变了（人刚好又改了），这一项跳过。
      known         {编号: {面: 指纹}}，返回的记录只带和它不同的面的值
      undo_push     推一个撤销步（人那一边）
    """
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"))
    path, tmp = args.get("path"), None
    if args.get("data"):
        tmp = path = os.path.join(tempfile.gettempdir(), f"cowork_in_{uuid.uuid4().hex[:10]}.blend")
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(args["data"]))
    cur = records(scene)
    applied, skipped, face_errors, report = [], [], [], {}
    todo = []
    for it in args.get("items", []):
        cid, rec = it["id"], cur.get(it["id"])
        if it["op"] == "create":
            if rec is not None:
                skipped.append({"id": cid, "reason": "exists"})
                continue
        else:
            if rec is None:
                skipped.append({"id": cid, "reason": "missing"})
                continue
            if rec["editing"]:
                skipped.append({"id": cid, "reason": "editing"})
                continue
            guard = it.get("guard") or {}
            if any(rec["aspects"].get(f) != fp for f, fp in guard.items()):
                skipped.append({"id": cid, "reason": "changed"})
                continue
        todo.append(it)
    for it in todo:
        if it["op"] == "delete":
            o = _find(scene, it["id"])
            if o is not None:
                bpy.data.objects.remove(o, do_unlink=True)
            applied.append(it["id"])
    need = [it for it in todo if it["op"] in ("create", "whole", "faces")]
    if need:
        loaded, extras, bef = _append(path, [it["name"] for it in need])
        temps = []
        # 先放整个换入的（新建的对象可能是别的对象的父对象），再按面复制
        for it in sorted(need, key=lambda x: x["op"] == "faces"):
            src = loaded.get(it["name"])
            if src is None:
                skipped.append({"id": it["id"], "reason": "not_in_source"})
                continue
            rec = it["record"]
            if it["op"] in ("create", "whole"):
                _put_in_place(scene, src, it["id"], rec["name"], rec.get("collections", []))
                applied.append(it["id"])
                continue
            temps.append(src)
            dst = _find(scene, it["id"])
            base_copy = dst.copy()
            temps.append(base_copy)
            want = {f: (base_copy, cur[it["id"]], fp) for f, fp in cur[it["id"]]["aspects"].items()}
            for f in it["faces"]:
                if f in rec["aspects"]:
                    want[f] = (src, rec, rec["aspects"][f])
            left = apply_faces(scene, dst, want)
            if left:
                face_errors.append({"id": it["id"], "faces": left})
            applied.append(it["id"])
        for t in temps:
            try:
                bpy.data.objects.remove(t, do_unlink=True)
            except Exception:
                pass
        _settle(scene, extras, bef, report, tidy=False)
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if args.get("path") and args.get("remove_source"):
        try:
            os.remove(args["path"])
        except OSError:
            pass
    undo_pushed = _try_undo_push(args.get("undo_message", "Cowork: 同步")) if args.get("undo_push") and applied else False
    known = args.get("known")
    return {"applied": applied, "skipped": skipped, "face_errors": face_errors,
            "dangling": report.get("dangling", []), "undo_pushed": undo_pushed,
            "records": records(scene, values=known if known is not None else "all"), "labels": face_labels()}


# ---------------------------------------------------------------------------
# 方式二：各自一份，存盘后合并
# ---------------------------------------------------------------------------
def _open(path):
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)


def new_file(args):
    """新建一个没有任何对象的 .blend 文件。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=args["path"], check_existing=False)
    return {"path": args["path"]}


STAMP_KEY = "cowork_base"          # 方式二文件兜底：这份内容是从第几版合并结果打开的（跟着内容走）


def dump_file(args):
    _open(args["path"])
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"), deterministic=True)
    stamp = scene.get(STAMP_KEY)
    return {"records": records(scene), "labels": face_labels(),
            "stamp": stamp if isinstance(stamp, str) else None}


def edit_file(args):
    """打开文件、执行脚本、保存到 save_to。用来模拟 AI 用 computer use 改自己那一份：
    脚本不会给新对象盖章，和真实的界面操作一样。"""
    _open(args["path"])
    error = None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(args["code"], {"bpy": bpy, "__name__": "__cowork_agent__"})
    except Exception:
        error = traceback.format_exc(limit=3)
    bpy.ops.wm.save_as_mainfile(filepath=args.get("save_to", args["path"]), check_existing=False)
    return {"error": error, "stdout": out.getvalue()[-4000:]}


def merge_file(args):
    """打开 base（人那一份），按计划换入、删除对象，保存到 out。

    args:
      base      作为起点的文件（人那一份的拷贝）
      out       输出路径
      delete    [编号]
      take      [{"id", "source", "name", "collections", "candidate": bool, "aspects": [...], "record": {...}}]
                source 是对象所在的文件（AI 那一份，或者上一版合并结果）；
                有 aspects 时只把这几个面复制到人那一份的同一个对象上，否则整个换入
      prefix    base 里没盖章的新对象用的前缀（人那一份用 h-）
    """
    _open(args["base"])
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"), deterministic=True)
    report = {}
    removed = []
    for cid in args.get("delete", []):
        o = _find(scene, cid)
        if o is not None:
            bpy.data.objects.remove(o, do_unlink=True)
            removed.append(cid)
    takes = args.get("take", [])
    # 1. 整个换入（以及候选）
    by_source = {}
    for t in takes:
        if not t.get("aspects"):
            by_source.setdefault(t["source"], []).append(t)
    for source, items in by_source.items():
        loaded, extras, bef = _append(source, [t["name"] for t in items])
        for t in items:
            obj = loaded.get(t["name"])
            if obj is None:
                report.setdefault("missing", []).append(t["id"])
            elif t.get("candidate"):
                _put_candidate(scene, obj, t["id"], t["name"])
            else:
                _put_in_place(scene, obj, t["id"], t["name"], t.get("collections", []))
        _settle(scene, extras, bef, report)

    # 2. 按面合并：同一个对象的几个面可能分别来自 AI 那一份、上一版合并结果，其余面保持人那一份
    by_obj = {}
    for t in takes:
        if t.get("aspects"):
            by_obj.setdefault(t["id"], []).append(t)
    if by_obj:
        cache = {}
        base_recs = records(scene)
        appended = []                                  # (extras, before)
        temps = []
        sources = {}
        for source in sorted({t["source"] for ts in by_obj.values() for t in ts}):
            names = [t["name"] for ts in by_obj.values() for t in ts if t["source"] == source]
            loaded, extras, bef = _append(source, names)
            sources[source] = loaded
            appended.append((extras, bef))
            temps += list(loaded.values())
        for cid, ts in by_obj.items():
            cur = _find(scene, cid)
            if cur is None or cid not in base_recs:
                report.setdefault("missing", []).append(cid)
                continue
            base_copy = cur.copy()                     # 人那一份的原样：不换的面从这里取
            temps.append(base_copy)
            want = {f: (base_copy, base_recs[cid], fp) for f, fp in base_recs[cid]["aspects"].items()}
            for t in ts:
                src = sources[t["source"]].get(t["name"])
                if src is None:
                    report.setdefault("missing", []).append(cid)
                    continue
                for face in t["aspects"]:
                    want[face] = (src, t["record"], t["record"]["aspects"][face])
            left = apply_faces(scene, cur, want)
            if left:
                report.setdefault("face_errors", []).append({"id": cid, "faces": left})
        for tmp in temps:
            bpy.data.objects.remove(tmp, do_unlink=True)
        for extras, bef in appended:
            _settle(scene, extras, bef, report)
    result = records(scene)
    if args.get("stamp") is not None:
        scene[STAMP_KEY] = str(args["stamp"])
    bpy.ops.wm.save_as_mainfile(filepath=args["out"], check_existing=False)
    return {"records": result, "removed": removed, "dangling": report.get("dangling", []),
            "missing": report.get("missing", []), "face_errors": report.get("face_errors", []),
            "labels": face_labels()}


def _cowork_out(result):
    # 只输出 ASCII（中文转成 \uXXXX），避免 Windows 上 Blender 的标准输出编码问题
    print(MARK_BEGIN + json.dumps(result, ensure_ascii=True, default=str) + MARK_END)
