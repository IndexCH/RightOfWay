"""在 Blender 里运行的代码。

这个文件有两种用法，所以它只能 import bpy 和 Python 标准库，不能 import rightofway 包里的任何东西：

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
结果用 _rightofway_out() 打印在两个标记之间，调用方从标准输出里取出来。

方式一（poll、run_agent 带参数 data_units=True）里，对象引用的数据块（材质、网格、灯光数据……）各自是一个单元，
有自己的编号和面；对象的面里只记"引用了哪个数据块"。这样人改一次共用材质只记一次，恢复时也写回同一个数据块。
哪些数据块算数据块、它们有哪些面，同样从 RNA 读出，不手写。
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

ID_KEY = "rightofway_id"                 # 对象的系统 ID，存在对象的自定义属性里
UID_KEY = "rightofway_uid"               # 盖章时的 session_uid，用来区分复制出来的对象（Shift+D 会连自定义属性一起复制）
CANDIDATE_KEY = "rightofway_candidate"   # AI 候选版本：不参与追踪
CANDIDATE_COLLECTION = "RightOfWay_AI候选"
MARK_BEGIN = "<<<RIGHTOFWAY_JSON>>>"
MARK_END = "<<<RIGHTOFWAY_END>>>"
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
    """对另一个数据块的引用：只记它是谁。单独追踪的数据块按编号记，改名不算变。"""
    if idb is None:
        return None
    if isinstance(idb, bpy.types.Object):
        return "obj:" + str(idb.get(ID_KEY) or idb.name)
    if _is_unit_data(idb):
        return "data:" + str(idb[ID_KEY])
    return "id:" + type(idb).__name__ + ":" + _base_name(idb.name)


_DATA_UNITS = False      # 方式一：对象引用的数据块各自是一个单元（poll、run_agent 的参数 data_units）


def _set_data_units(args):
    global _DATA_UNITS
    _DATA_UNITS = bool((args or {}).get("data_units"))


def _is_unit_data(idb):
    """单独作为单元追踪的数据块：盖过章、不是对象、不是内嵌数据、不来自链接的库。"""
    try:
        return (_DATA_UNITS and isinstance(idb, bpy.types.ID) and not isinstance(idb, bpy.types.Object)
                and idb.library is None and not getattr(idb, "is_embedded_data", False) and bool(idb.get(ID_KEY)))
    except Exception:
        return False


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
            if "collect" in cache:                     # 只是找出对象引用了哪些数据块
                if v.library is None:
                    cache["collect"][v.as_pointer()] = v
                return None
            if _is_unit_data(v):
                return _ref(v)                         # 数据块单独成单元：对象这一面只记引用了哪个
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
        keys = [k for k in s.keys() if not k.startswith("rightofway_")]      # 自定义属性（几何节点的输入也在这里）
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
    """面的显示名，来自 Blender 自己的界面文字（Blender 界面是中文时就是中文）。
    方式一里再加上追踪的数据块那些类型的属性名（对象的同名属性优先）。"""
    tr = getattr(bpy.app.translations, "pgettext_iface", lambda x: x)
    labels = {p.identifier: tr(p.name) for p in bpy.types.Object.bl_rna.properties}
    labels["users_collection"] = tr("Collections")
    if _DATA_UNITS:
        seen = set()
        for idb, _ in _tracked_data():
            t = type(idb)
            if t in seen:
                continue
            seen.add(t)
            for p in idb.bl_rna.properties:
                labels.setdefault(p.identifier, tr(p.name))
        labels.setdefault("[props]", tr("Custom Properties"))
    return labels


# ---------------------------------------------------------------------------
# 数据块（方式一）：对象引用的材质、网格、灯光数据……各自是一个单元
# ---------------------------------------------------------------------------
_DATA_COLLS = None


def _data_collections():
    """bpy.data 里装数据块的集合名，从 RNA 读出。对象单独追踪，不在其中。"""
    global _DATA_COLLS
    if _DATA_COLLS is None:
        names = []
        for p in bpy.types.BlendData.bl_rna.properties:
            if p.type != "COLLECTION" or p.identifier == "objects":
                continue
            try:
                coll = getattr(bpy.data, p.identifier)
            except Exception:
                continue
            if isinstance(coll, bpy.types.bpy_prop_collection):
                names.append(p.identifier)
        _DATA_COLLS = names
    return _DATA_COLLS


def _tracked_data():
    """盖过章的数据块：[(数据块, 所在集合名)]。还在文件里就追踪（没有对象用它也一样），从 bpy.data 里移除才算删除。"""
    out = []
    for cname in _data_collections():
        for idb in getattr(bpy.data, cname):
            try:
                if (isinstance(idb, bpy.types.ID) and idb.library is None and idb.get(ID_KEY)
                        and not getattr(idb, "is_embedded_data", False)):
                    out.append((idb, cname))
            except Exception:
                continue
    return out


def _find_data(cid, exclude=()):
    for idb, cname in _tracked_data():
        if idb.get(ID_KEY) == cid and idb.as_pointer() not in exclude:
            return idb, cname
    return None, None


def _data_refs(objs):
    """这些对象直接引用的数据块（不是对象、不是内嵌数据、不来自链接的库）。和算指纹时走同一条路径。"""
    cache = {"collect": {}}
    for o in objs:
        for p in _content_props(o):
            if p.type not in ("POINTER", "COLLECTION"):
                continue
            try:
                v = getattr(o, p.identifier)
            except Exception:
                continue
            _walk(v, 0, cache, in_object=True)
    return list(cache["collect"].values())


def _ensure_data_ids(objs, prefix):
    """给对象引用的数据块盖章。复制出来的数据块（ID.copy() 连自定义属性一起复制）带着原来的编号，这里区分开。"""
    groups = {}
    for idb, _ in _tracked_data():
        groups.setdefault(idb[ID_KEY], []).append(idb)
    for cid, group in groups.items():
        if len(group) > 1:
            keeper = next((d for d in group if d.get(UID_KEY) == getattr(d, "session_uid", None)), group[0])
            for d in group:
                if d is not keeper:
                    del d[ID_KEY]
    for idb, _ in _tracked_data():
        uid = getattr(idb, "session_uid", None)
        if uid is not None and idb.get(UID_KEY) != uid:
            idb[UID_KEY] = uid
    new = []
    for idb in _data_refs(objs):
        if not idb.get(ID_KEY):
            idb[ID_KEY] = prefix + uuid.uuid4().hex[:10]
            new.append(idb[ID_KEY])
            uid = getattr(idb, "session_uid", None)
            if uid is not None:
                idb[UID_KEY] = uid
    return new


def data_faces(idb, cache):
    """数据块的每个面一个指纹：它的每个顶层属性，引用别的数据块的只记引用，外加自定义属性。"""
    faces = {}
    for p in _content_props(idb):
        try:
            v = getattr(idb, p.identifier)
        except Exception:
            continue
        if (p.type == "COLLECTION" and len(v) > 0 and isinstance(v[0], bpy.types.ID)):
            faces[p.identifier] = _md5([_ref(x) for x in v])       # 引用列表（例如网格上的材质）
            continue
        faces[p.identifier] = _md5(_walk(v, 0, cache, in_object=False))
    try:
        keys = [k for k in idb.keys() if not k.startswith("rightofway_")]
        if keys:
            faces["[props]"] = _md5(sorted((k, repr(idb[k].to_dict() if hasattr(idb[k], "to_dict") else
                                                    idb[k].to_list() if hasattr(idb[k], "to_list") else idb[k]))
                                           for k in keys))
    except Exception:
        pass
    return faces


def _data_display(idb):
    tr = getattr(bpy.app.translations, "pgettext_iface", lambda x: x)
    return f"{idb.name}（{tr(idb.bl_rna.name)}）"


def _data_record(idb, cname, cache, values, known):
    aspects = data_faces(idb, cache)
    cfp, fp = _fps(aspects)
    cid = idb[ID_KEY]
    rec = {"id": cid, "name": idb.name, "type": "data:" + type(idb).__name__, "kind": "data", "coll": cname,
           "display": _data_display(idb), "cfp": cfp, "fp": fp, "aspects": aspects, "parent": None,
           "collections": [], "editing": False, "selected": False, "users": idb.users}
    if values is not None:
        if values == "all" or cid not in values:
            want = list(aspects)
        else:
            old = values[cid]
            want = [f for f, fp_ in aspects.items() if old.get(f) != fp_]
        if want:
            rec["values"] = object_values(idb, [f for f in want if f != "[props]"])
    return rec


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
    if _DATA_UNITS and not deterministic:
        new += _ensure_data_ids(objs, prefix)
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
    if _DATA_UNITS:
        for idb, cname in _tracked_data():
            cid = idb.get(ID_KEY)
            if only is not None and cid not in only:
                continue
            out[cid] = _data_record(idb, cname, cache, values, None)
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


def _pointers():
    """追加前各类数据块的指针，_settle 用来找出新带进来的。方式一里是全部数据块集合。"""
    colls = set(_DEDUP_COLLECTIONS) | (set(_data_collections()) if _DATA_UNITS else set())
    return {c: set(x.as_pointer() for x in getattr(bpy.data, c)) for c in colls}


def _append(path, names):
    """从 path 追加指定名字的对象。返回 {文件里的名字: 新对象}、间接带进来的对象、追加前各类数据块的指针。"""
    before_objs = set(o.as_pointer() for o in bpy.data.objects)
    before = _pointers()
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
    if _DATA_UNITS:
        # 快照里带出来的数据块副本（带着同一个编号）：一律指回场景里现在的那一个。
        # 数据块自己的内容由它自己的单元管（先于对象恢复），对象这边只管"引用了哪个"。
        for coll in _data_collections():
            if coll not in before:
                continue
            pool = getattr(bpy.data, coll)
            current = {}
            for x in pool:
                if x.as_pointer() in before[coll] and x.get(ID_KEY):
                    current[x[ID_KEY]] = x
            for new in [x for x in pool if x.as_pointer() not in before[coll]]:
                sid = new.get(ID_KEY)
                if sid and sid in current:
                    new.user_remap(current[sid])
                    pool.remove(new)
    cache = {}
    for coll in _DEDUP_COLLECTIONS:
        pool = getattr(bpy.data, coll)
        for new in [x for x in pool if x.as_pointer() not in before[coll]]:
            if new.users == 0:
                pool.remove(new)
                continue
            for old in pool:
                if (old.as_pointer() in before[coll] and _base_name(old.name) == _base_name(new.name)
                        and old.get(ID_KEY) == new.get(ID_KEY)          # 单独追踪的数据块不和别的合并
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
        for k in [k for k in dst.keys() if not k.startswith("rightofway_")]:
            del dst[k]
        for k in src.keys():
            if not k.startswith("rightofway_"):
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
    elif face == "[props]":
        for k in [k for k in dst.keys() if not k.startswith("rightofway_")]:
            del dst[k]
        for k in src.keys():
            if not k.startswith("rightofway_"):
                dst[k] = src[k]
    else:
        p = dst.bl_rna.properties.get(face)
        if p is None:
            raise CopyError(face)
        _copy_value(dst, p, getattr(src, face), scene, 0)


_FACE_ORDER = {"data": 0}     # 先换数据块，它会连带改变别的面（例如材质槽），后面的面再按需要改回来


def apply_faces(scene, dst, want, faces_fn=None):
    """want = {面: (来源对象, 来源记录, 期望的指纹)}。
    先把和期望不同的面从各自的来源复制过来；再核对一遍，被连带改掉的面再复制一次。
    返回仍然对不上的面（调用方据此决定是否整个换回）。faces_fn：算面的指纹的函数（数据块用 data_faces）。"""
    faces_fn = faces_fn or object_faces

    def mismatched():
        now = faces_fn(dst, {})
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


def _restore_data(scene, snap_path, before, after, full, partial, restored, merged, fallback, report):
    """恢复要保持的数据块，写回同一个数据块，不分叉：
    - 只有部分面要保持：把这些面从快照里的副本复制到现在的数据块上；
    - 整个要保持（或者按面复制没能还原）：现在的数据块的所有使用者改指向快照里的副本（user_remap），
      副本接过编号和名字，原来的删掉。所以任何时候都只有一个数据块带着这个编号，用它的对象全部跟着走。"""
    by_coll = {}
    for cid in list(full) + list(partial):
        by_coll.setdefault(before[cid]["coll"], []).append(cid)
    bef = _pointers()
    with bpy.data.libraries.load(snap_path, link=False) as (src, dst):
        for coll, cids in by_coll.items():
            avail = set(getattr(src, coll))
            setattr(dst, coll, [before[c]["name"] for c in cids if before[c]["name"] in avail])
    loaded = {}
    for coll, cids in by_coll.items():
        pool = getattr(bpy.data, coll)
        fresh = [x for x in pool if x.as_pointer() not in bef.get(coll, set())]
        for c in cids:
            loaded[c] = next((x for x in fresh if x.get(ID_KEY) == c), None)

    def replace(cid, tmp):
        cur, coll = _find_data(cid, exclude={tmp.as_pointer()})
        if cur is not None:
            cur.user_remap(tmp)
            getattr(bpy.data, coll).remove(cur)
        tmp[ID_KEY] = cid
        uid = getattr(tmp, "session_uid", None)
        if uid is not None:
            tmp[UID_KEY] = uid
        tmp.name = before[cid]["name"]
        restored.append(cid)

    for cid in full:
        tmp = loaded.get(cid)
        if tmp is not None:
            replace(cid, tmp)
    for cid, asp in partial.items():
        tmp = loaded.get(cid)
        if tmp is None:
            continue
        cur, coll = _find_data(cid, exclude={tmp.as_pointer()})
        if cur is None:
            replace(cid, tmp)
            continue
        ai_copy = cur.copy()                         # AI 的版本：被连带改掉的面从这里取回
        want = {}
        for face in before[cid]["aspects"]:
            if face in asp:
                want[face] = (tmp, before[cid], before[cid]["aspects"][face])
            elif face in after[cid]["aspects"]:
                want[face] = (ai_copy, after[cid], after[cid]["aspects"][face])
        left = apply_faces(scene, cur, want, faces_fn=data_faces)
        getattr(bpy.data, coll).remove(ai_copy)
        if not left:
            merged[cid] = {"restored": list(asp),
                           "kept": sorted(x for x in before[cid]["aspects"]
                                          if after[cid]["aspects"].get(x) != before[cid]["aspects"][x] and x not in asp)}
            getattr(bpy.data, coll).remove(tmp)
        else:                                        # 按面复制没能完全还原：整个换回
            replace(cid, tmp)
            fallback.append({"id": cid, "faces": left})
    _settle(scene, [], bef, report, tidy=False)


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
    """args.known = {编号: {面: 指纹}}：只带回和它不一样的面的值；没有 known 时带回全部值。
    args.data_units：方式一里数据块单独作为单元。"""
    _set_data_units(args)
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


# ---------------------------------------------------------------------------
# 认回同一个对象（design_v0.5.md 12.4、12.5）。配对规则在 identity.py（bridge 把它放在这个文件前面一起发进来，
# 运行时用的是同一份），这里只照许可单做，不做别的判断。
# ---------------------------------------------------------------------------
PROBE_NAME = "RightOfWayProbe"


def probe(args):
    """连接时的自测（design_v0.5.md 第 4 节）。能做到什么不手写，而是测出来：
    - 应用给两个同名的东西起什么名字（运行时据此学出重名后缀）；
    - 在一个临时场景里，用和平时完全一样的 run_agent / begin_agent / finish_agent 试一遍：
      改掉的面能不能按面恢复、删掉的对象能不能恢复、两次调用之间的修改（不透明的工具调用）能不能恢复。
    临时场景和里面的东西最后都删掉，你的场景不受影响（文件会被标成"已修改"）。"""
    base = PROBE_NAME + "_" + uuid.uuid4().hex[:6]
    a = bpy.data.objects.new(base, None)
    b = bpy.data.objects.new(base, None)
    names = [a.name, b.name]
    bpy.data.objects.remove(a)
    bpy.data.objects.remove(b)
    out = {"names": names, "identity": True, "transfer_ids": True, "notify": True, "atomic": True, "measured": True}
    out.update(_self_test(base))
    return out


def _self_test(base):
    """改一个面、删掉对象、在 begin/finish 之间改，三种情况各试一次，看要保持的部分最后是不是和原来一样。"""
    saved = _DATA_UNITS
    sc = bpy.data.scenes.new(base)
    made = []
    result = {"restore_face": False, "restore_deleted": False, "around": False}
    try:
        def fresh():
            for o in list(sc.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            me = bpy.data.meshes.new(base + "_mesh")
            o = bpy.data.objects.new(base + "_obj", me)
            sc.collection.objects.link(o)
            made.append(me)
            common = {"scene": sc.name, "prefix": "p-", "watch_outside": False, "undo_push": False,
                      "restore_selection": False, "defer_if_editing": False}
            cid = next(iter(poll(common)["records"]))
            return o.name, cid, common

        def same(res, cid):
            return cid in res["after"] and res["after"][cid]["cfp"] == res["before"][cid]["cfp"]

        name, cid, common = fresh()
        move = f"o = bpy.data.objects[{name!r}]\no.location.x += 1\n"
        res = run_agent({**common, "code": move, "protected": {cid: ["location"]}})
        result["restore_face"] = same(res, cid)
        name, cid, common = fresh()
        res = run_agent({**common, "code": f"bpy.data.objects.remove(bpy.data.objects[{name!r}])",
                         "protected": {cid: ["*"]}})
        result["restore_deleted"] = same(res, cid)
        name, cid, common = fresh()
        b = begin_agent({**common, "protected": {cid: ["location"]}})
        bpy.data.objects[name].location.x += 1                # 两次调用之间的修改（代理转发的工具调用）
        res = finish_agent({"token": b["token"]})
        result["around"] = same(res, cid)
    except Exception:
        traceback.print_exc()
    finally:
        for o in list(sc.objects):
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.scenes.remove(sc)
        for me in list(bpy.data.meshes):
            if me.name.startswith(base) and me.users == 0:
                bpy.data.meshes.remove(me)
        _set_data_units({"data_units": saved})
    return result


def notify(args):
    """给人的提醒（违规、AI 反复改改不动的东西、认回核对不一致）：在 Blender 界面里弹出提示；没有界面时只打印。"""
    text = str((args or {}).get("text", ""))
    lines = []
    for para in text.splitlines() or [""]:
        while len(para) > 60:
            lines.append(para[:60])
            para = para[60:]
        lines.append(para)
    shown = False
    try:
        wm = bpy.context.window_manager
        if wm.windows:
            def draw(menu, _context):
                for line in lines:
                    menu.layout.label(text=line)
            with bpy.context.temp_override(window=wm.windows[0]):
                wm.popup_menu(draw, title="RightOfWay", icon="ERROR")
            shown = True
    except Exception:
        shown = False
    print("RightOfWay 提醒：" + text)
    return {"shown": shown}


def _row(x):
    if isinstance(x, bpy.types.Object):
        return {"name": x.name, "type": x.type, "parent": (x.parent.get(ID_KEY) if x.parent else None)}
    return {"name": x.name, "type": "data:" + type(x).__name__, "parent": None}


def _stamp(x, cid):
    x[ID_KEY] = cid
    uid = getattr(x, "session_uid", None)
    if uid is not None:
        x[UID_KEY] = uid


def _unstamp(x):
    for k in (ID_KEY, UID_KEY):
        if k in x:
            del x[k]


def _identity(scene, before, created, args):
    """认回同一个对象。三种旧对象和这次新建的对象配对（identity.match_identities：同一个父对象下从上往下，
    先比完整名字、再比去掉重名后缀的名字，类型相同，一对一，有歧义就不配）：
      gone    这次执行里 Agent 删掉的（数据块：从文件里删掉，或者执行前有对象用、执行后没有了）；
      rebind  Agent 之前想删、因为人改过而保留下来的（许可单的 rebind）；
      tomb    人删掉、这个 Agent 还没看到的（许可单的 no_recreate，墓碑）。
    配上的新对象接过旧编号，之后的保护步骤照常把人改过的面恢复到它身上：
      rebind、还留在文件里的 gone 数据块（没人用了）：引用旧的（子对象、修改器……）改指向新的，删掉旧的；
      tomb：新对象在恢复之后删掉（_remove_recreated）。
    去掉后缀才配上的，名字还给它（后缀只是因为和旧的重名，旧的已经不在了）。
    先配对象，再配数据块（删掉旧对象之后，它用的数据块才没人用）。
    返回 (交给运行时核对的 {"pairs", "kinds", "fresh"}, 要删掉的墓碑编号)。"""
    rule = args.get("reidentify") or {}
    pattern = rule.get("suffix")
    out = {"pairs": {}, "kinds": {}, "fresh": {}}
    tombs = []
    for phase in ("object", "data"):
        objs = {o.get(ID_KEY): o for o in _tracked(scene) if o.get(ID_KEY)}
        datas = {d.get(ID_KEY): (d, c) for d, c in _tracked_data()} if _DATA_UNITS else {}
        olds, kinds = {}, {}
        for cid, r in before.items():
            is_data = r.get("kind") == "data"
            if is_data != (phase == "data") or cid in out["pairs"]:
                continue
            row = {"name": r["name"], "type": r["type"], "parent": r.get("parent")}
            if is_data:
                d = datas.get(cid)
                if d is None or (r.get("users", 0) > 0 and d[0].users == 0):
                    olds[cid], kinds[cid] = row, "gone"
            elif cid not in objs:
                olds[cid], kinds[cid] = row, "gone"
        for cid in sorted(args.get("rebind") or {}):
            r = before.get(cid)
            if (r is not None and cid not in olds and (r.get("kind") == "data") == (phase == "data")
                    and (cid in objs or cid in datas)):
                olds[cid], kinds[cid] = {"name": r["name"], "type": r["type"], "parent": r.get("parent")}, "rebind"
        if phase == "object":
            for cid, row in sorted((args.get("no_recreate") or {}).items()):
                if cid not in before and cid not in objs:
                    olds[cid], kinds[cid] = ({"name": row.get("name"), "type": row.get("type"),
                                              "parent": row.get("parent")}, "tomb")
        fresh, made = {}, {}
        for cid in created:
            x = objs.get(cid) if phase == "object" else (datas.get(cid) or (None,))[0]
            if x is not None:
                fresh[cid], made[cid] = _row(x), x
        pairs = match_identities(olds, fresh, pattern) if olds and fresh else {}
        for old, (new, level) in sorted(pairs.items()):
            x, kind = made[new], kinds[old]
            pool = bpy.data.objects if phase == "object" else getattr(bpy.data, datas[new][1])
            if kind == "rebind" or (kind == "gone" and old in datas):
                # 保留下来的旧对象、没人用了的旧数据块：引用改指向新的，删掉旧的。一个编号只对应一个东西，
                # 旧的也不再占着名字（没人用的数据块存盘时 Blender 本来也会丢掉）
                k = objs[old] if phase == "object" else datas[old][0]
                if phase == "object":
                    for c in list(k.users_collection):
                        c.objects.unlink(k)             # 所在集合由新对象自己决定，不跟着换
                k.user_remap(x)                         # 子对象、修改器……原来引用旧对象的，改指向新对象
                (bpy.data.objects if phase == "object" else getattr(bpy.data, datas[old][1])).remove(k)
            _stamp(x, old)
            if kind == "tomb":
                tombs.append(old)
            elif level == "suffix":
                want = before[old]["name"]
                if x.name != want and pool.get(want) is None:
                    x.name = want
            out["pairs"][old] = [new, level]
            out["kinds"][old] = kind
            out["fresh"][new] = fresh[new]
    return out, tombs


def _remove_recreated(scene, tombs, created):
    """拦下"补回人删掉的对象"（design_v0.5.md 12.5）：删掉配上墓碑的新对象，
    以及这次新建、只给它用的数据块（删完之后没人用了的）。"""
    removed = []
    for cid in tombs:
        o = _find(scene, cid)
        if o is None:
            continue
        refs = [d for d in _data_refs([o]) if d.get(ID_KEY) in created]
        bpy.data.objects.remove(o, do_unlink=True)
        removed.append(cid)
        for d in refs:
            if d.users == 0:
                x, cname = _find_data(d.get(ID_KEY))
                if x is not None:
                    getattr(bpy.data, cname).remove(x)
    return removed


def run_agent(args):
    """原子地执行 AI 的一段脚本。Blender 是单线程的，这个函数执行期间人不可能同时操作。

    这里只照运行时开的许可单执行，自己不判断哪些该保护（design_v0.5.md 第 2 节，P9）。

    args:
      code         AI 的脚本
      protected    许可单里要保持的部分 {编号: [面]}，"*" 表示整个对象
      keep_alive   许可单里不许删、但面可以改的对象（要保持的对象的祖先）：被删了就整个放回
      expected     运行时以为的执行前状态 {编号: 指纹}；和实际不一样（人刚改过、刚新建或删除了对象），
                   就不执行，返回 status "replan"，由运行时先记下人的修改、重开许可单
      expected_selection  运行时以为人选中的对象（"选中即占用"选项开启时才给）；不一样同样返回 "replan"
      known        这个 AI 上次看到的 {编号: {"fp", "aspects"}}：只用来决定带回哪些面的值
      reidentify   认回同一个对象的规则（许可单给的 {"rule", "suffix"}）：删掉又新建的同一个对象接过旧编号（_identity）
      rebind       许可单的 rebind：Agent 之前想删、因为人改过而保留下来的对象，又新建了同一个的，认成它
      no_recreate  许可单的墓碑：人删掉、这个 Agent 还没看到的对象，Agent 新建同一个的，恢复之后删掉
      granularity  "aspect"（默认，按面）或 "object"（整个对象）
      protect      False 表示对照组：不保护
      scene        场景名（可选）
    """
    st = _begin(args)
    if "status" in st:
        return st                                    # replan / deferred：没有执行
    out = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(out):
            exec(args["code"], {"bpy": bpy, "__name__": "__rightofway_agent__"})
    except Exception:
        error = traceback.format_exc(limit=3)
    return _finish(st, error, out.getvalue(), "RightOfWay: AI 脚本")


def _pending():
    """begin_agent 和 finish_agent 之间的状态（跨两次调用）。每次调用都在新的命名空间里执行，所以放在 sys.modules 里
    （bpy.app.driver_namespace 也能跨调用，但 pip 装的 bpy 退出时会因此卡住）。"""
    import sys
    import types
    mod = sys.modules.get("_rightofway_state")
    if mod is None:
        mod = sys.modules["_rightofway_state"] = types.ModuleType("_rightofway_state")
        mod.pending = {}
    return mod.pending


def begin_agent(args):
    """把一次不是脚本的修改（例如 MCP 代理转发给应用 MCP 服务器的类型化工具调用）包起来：前一半。
    和 run_agent 一样核对、快照，但不执行代码；返回 {"status": "begun", "token"}，之后调用 finish_agent。
    两次调用之间应用里发生的一切都算这个 Agent 做的（不是原子的；代理在两次调用之间只做那一次工具调用）。"""
    st = _begin(args)
    if "status" in st:
        return st
    token = uuid.uuid4().hex
    _pending()[token] = st
    return {"status": "begun", "token": token}


def finish_agent(args):
    """后一半：和 run_agent 执行完代码之后一样，认回、恢复、核对，返回同样的结果。"""
    st = _pending().pop(args["token"], None)
    if st is None:
        raise RuntimeError("找不到 begin_agent 的状态（Blender 重启过，或者已经 finish 过）")
    return _finish(st, args.get("error"), "", "RightOfWay: AI 工具")


def _begin(args):
    """执行前：盖章、列对象、核对和运行时以为的一样、快照要保持的部分。返回状态 dict；不执行时返回带 status 的结果。"""
    t0 = time.perf_counter()
    _set_data_units(args)
    scene = _scene(args)
    ensure_ids(scene, prefix=args.get("prefix", "h-"))
    known = args.get("known", {})
    before = records(scene, values={cid: k["aspects"] for cid, k in known.items()})
    editing = sorted(cid for cid, r in before.items() if r["editing"])
    if editing and args.get("defer_if_editing", True):
        return {"status": "deferred", "reason": "edit_mode", "editing": editing, "before": before}
    selected = sorted(cid for cid, r in before.items() if r["selected"])
    expected = args.get("expected")
    if expected is not None:
        changed = sorted((set(before) ^ set(expected))
                         | {cid for cid in before if cid in expected and before[cid]["fp"] != expected[cid]})
        sel = args.get("expected_selection")
        if changed or (sel is not None and sorted(sel) != selected):
            return {"status": "replan", "changed": changed, "before": before, "selected": selected,
                    "labels": face_labels()}

    protected = {cid: set(v) for cid, v in args.get("protected", {}).items()}
    protected = {cid: a for cid, a in protected.items() if cid in before and a}
    keep_alive = [c for c in args.get("keep_alive", []) if c in before and c not in protected]
    do_protect = args.get("protect", True)
    snap_path = None
    if (protected or keep_alive) and do_protect:
        snap_path = os.path.join(tempfile.gettempdir(), f"rightofway_snap_{uuid.uuid4().hex[:8]}.blend")
        ids = set(o for o in _tracked(scene) if o.get(ID_KEY) in protected or o.get(ID_KEY) in keep_alive)
        if _DATA_UNITS:
            ids |= set(d for d, _ in _tracked_data() if d.get(ID_KEY) in protected)
        bpy.data.libraries.write(snap_path, ids, fake_user=False)
    outside_before = _other_objects(scene) if args.get("watch_outside", True) else None
    vl = _view_layer(scene)
    sel_state = _selection_state(scene, vl)

    return {"t0": t0, "args": args, "before": before, "selected": selected, "protected": protected,
            "keep_alive": keep_alive, "do_protect": do_protect, "snap_path": snap_path,
            "outside_before": outside_before, "sel_state": sel_state}


def _finish(st, error, stdout, undo_message):
    """执行后：认回、恢复要保持的部分、核对，返回交给运行时的结果。"""
    args, before, selected, protected = st["args"], st["before"], st["selected"], st["protected"]
    keep_alive, do_protect, snap_path = st["keep_alive"], st["do_protect"], st["snap_path"]
    outside_before, sel_state, t0 = st["outside_before"], st["sel_state"], st["t0"]
    _set_data_units(args)
    scene = _scene(args)
    vl = _view_layer(scene)
    ai_created = ensure_ids(scene, prefix="a-")
    identity, tombs = ({}, [])
    if args.get("reidentify") and do_protect:
        identity, tombs = _identity(scene, before, ai_created, args)
    taken = {new for new, _ in identity.get("pairs", {}).values()}
    ai_created = [c for c in ai_created if c not in taken]
    before_faces = {cid: r["aspects"] for cid, r in before.items()}
    after = records(scene, values=before_faces)

    # 哪些要整个换回，哪些只把冲突的面换回（数据块和对象分开处理，数据块先恢复）
    full, partial, data_full, data_partial = [], {}, [], {}
    for cid, asp in sorted(protected.items()):
        b, a = before[cid], after.get(cid)
        is_data = b.get("kind") == "data"
        if a is None:
            (data_full if is_data else full).append(cid)   # AI 删掉了受保护的对象或数据块
            continue
        changed = ({x for x in b["aspects"] if a["aspects"].get(x) != b["aspects"][x]}
                   | (set(a["aspects"]) - set(b["aspects"])))
        conflict = changed if "*" in asp else changed & asp
        if not conflict:
            continue                                # AI 只改了人没碰过的面：照常生效
        if conflict == changed:
            (data_full if is_data else full).append(cid)
        else:
            (data_partial if is_data else partial)[cid] = sorted(conflict)
    for cid in keep_alive:                          # 祖先：不许删，面照样可以改（被删了就整个放回）
        if cid not in after:
            full.append(cid)

    restored, merged, candidates, fallback, report = [], {}, [], [], {}
    keep = args.get("keep_candidates", False)
    if snap_path and (data_full or data_partial):
        _restore_data(scene, snap_path, before, after, data_full, data_partial, restored, merged, fallback, report)
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
    recreated = _remove_recreated(scene, tombs, set(ai_created)) if tombs else []

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
    undo_pushed = _try_undo_push(undo_message) if args.get("undo_push", True) else False
    return {
        "status": "ok", "before": before, "after_raw": after, "after": final,
        "protected": {k: sorted(v) for k, v in protected.items()},
        "selected": selected,
        "restored": restored, "merged": merged, "fallback": fallback,
        "inexact": inexact, "renamed": renamed, "dangling": report.get("dangling", []),
        "candidates": candidates, "outside": outside, "identity": identity, "recreated": recreated,
        "ai_created": ai_created, "error": error, "stdout": stdout[-4000:],
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
    path = os.path.join(tempfile.gettempdir(), f"rightofway_x_{uuid.uuid4().hex[:10]}.blend")
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
        tmp = path = os.path.join(tempfile.gettempdir(), f"rightofway_in_{uuid.uuid4().hex[:10]}.blend")
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
    undo_pushed = _try_undo_push(args.get("undo_message", "RightOfWay: 同步")) if args.get("undo_push") and applied else False
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


STAMP_KEY = "rightofway_base"          # 方式二文件兜底：这份内容是从第几版合并结果打开的（跟着内容走）


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
            exec(args["code"], {"bpy": bpy, "__name__": "__rightofway_agent__"})
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


def _rightofway_out(result):
    # 只输出 ASCII（中文转成 \uXXXX），避免 Windows 上 Blender 的标准输出编码问题
    print(MARK_BEGIN + json.dumps(result, ensure_ascii=True, default=str) + MARK_END)
