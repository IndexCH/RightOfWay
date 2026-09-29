"""把整个实验（布置 → 人改 → AI 调整 → 检查）拼成一段 C#，在 Unity 里一次跑完。

用来验证 Unity 这一侧的观察代码（面、指纹、按面恢复）本身对不对，不经过 Python 运行时。
运行时那一侧的逻辑已经在 Blender 上验证过；两边通过同一个 SharedSession 连接。
"""
from __future__ import annotations

import json

from .bridge import TEMPLATE_PATH

_SELFTEST = r'''
    static string STEP;

    object SelfTest(Dictionary<string, object> args, ExecutionResult result)
    {
        var prevActive = SceneManager.GetActiveScene();
        for (int i = 0; i < SceneManager.sceneCount; i++)            // 上次中断留下的实验场景不能当作"原来的场景"
        {
            var sc = SceneManager.GetSceneAt(i);
            if (sc.isLoaded && !sc.GetRootGameObjects().Any(g => g.name == MARKER)) { prevActive = sc; break; }
        }
        var setup = (Dictionary<string, object>)Setup(args);
        var a = new Dictionary<string, object> {
            { "scene_handle", setup["scene_handle"] }, { "granularity", args["granularity"] }, { "protect", args["protect"] },
            { "protected", new Dictionary<string, object>() }, { "known", new Dictionary<string, object>() }, { "watch_outside", true },
            { "protect_selected", args.ContainsKey("protect_selected") && (bool)args["protect_selected"] } };
        STEP = "build";
        var r1 = (Dictionary<string, object>)RunAgent(a, result);
        a["known"] = Known((Dictionary<string, object>)r1["after"]);
        STEP = "human";
        Exec(a, result);
        STEP = "adjust";
        var r2 = (Dictionary<string, object>)RunAgent(a, result);
        var before = (Dictionary<string, object>)r2["before"];
        var after = (Dictionary<string, object>)r2["after"];
        Func<object, string> name = id => {
            var d = after.ContainsKey((string)id) ? after : before;
            return d.ContainsKey((string)id) ? (string)((Dictionary<string, object>)d[(string)id])["name"] : (string)id; };
        GameObject G(string n) {
            foreach (var r in TARGET.GetRootGameObjects())
                foreach (var t in r.GetComponentsInChildren<Transform>(true)) if (t.name == n) return t.gameObject;
            return null; }
        var s = new Dictionary<string, object>();
        s["unity"] = Application.unityVersion;
        s["leaf3_y"] = G("Leaf_3").transform.localPosition.y;
        s["leaf1_y"] = G("Leaf_1").transform.localPosition.y;
        s["leaf3_color"] = G("Leaf_3").GetComponent<Renderer>().sharedMaterial.color.ToString();
        s["leaf1_color"] = G("Leaf_1").GetComponent<Renderer>().sharedMaterial.color.ToString();
        s["cube_y"] = G("Cube") == null ? -999f : G("Cube").transform.localPosition.y;
        s["rock2_exists"] = G("Rock_2") != null;
        s["trunk_scale"] = G("Trunk").transform.localScale.ToString();
        s["human_since"] = ((List<object>)r2["human_since"]).Select(name).ToList();
        var merged = new Dictionary<string, object>();
        foreach (var kv in (Dictionary<string, object>)r2["merged"]) merged[name(kv.Key)] = kv.Value;
        s["merged"] = merged;
        s["restored"] = ((List<object>)r2["restored"]).Select(name).ToList();
        s["fallback"] = r2["fallback"];
        s["inexact"] = ((List<object>)r2["inexact"]).Select(name).ToList();
        s["outside"] = r2["outside"];
        s["tracked_objects"] = before.Count;
        s["faces_on_leaf3_transform"] = ((Dictionary<string, object>)((Dictionary<string, object>)before.Values
            .Cast<Dictionary<string, object>>().First(r => (string)r["name"] == "Leaf_3/Transform"))["aspects"]).Count;
        s["seconds_adjust"] = r2["seconds"];
        var l3 = before.Values.Cast<Dictionary<string, object>>().First(r => (string)r["name"] == "Leaf_3/Transform");
        s["leaf3_values_seen"] = l3.ContainsKey("values") ? l3["values"] : null;     // 人改过的面带回了值
        s["selected"] = ((List<object>)r2["selected"]).Select(name).ToList();
        // 清理：关掉实验场景，恢复原来的活动场景
        if (!(args.ContainsKey("keep") && (bool)args["keep"]))
        {
            SceneManager.SetActiveScene(prevActive);
            EditorSceneManager.CloseScene(TARGET, true);
        }
        return s;
    }

    static Dictionary<string, object> Known(Dictionary<string, object> recs)
    {
        var k = new Dictionary<string, object>();
        foreach (var kv in recs)
        {
            var r = (Dictionary<string, object>)kv.Value;
            k[kv.Key] = new Dictionary<string, object> { { "fp", r["fp"] }, { "aspects", r["aspects"] } };
        }
        return k;
    }

    // ---------------- 场景 ----------------'''


def build_selftest(build: str, human: str, adjust: str, granularity: str = "aspect",
                   protect: bool = True, keep: bool = False, protect_selected: bool = False) -> str:
    src = TEMPLATE_PATH.read_text(encoding="utf-8")
    agent = ("        switch (STEP)\n        {\n"
             f"            case \"build\": {{\n{build}\n            }} break;\n"
             f"            case \"human\": {{\n{human}\n            }} break;\n"
             f"            case \"adjust\": {{\n{adjust}\n            }} break;\n"
             "        }")
    args = json.dumps({"granularity": granularity, "protect": protect, "keep": keep,
                       "protect_selected": protect_selected}).replace('"', '""')
    src = src.replace("__FN__", "selftest").replace("__ARGS__", args).replace("__AGENT__", agent)
    src = src.replace('                default: throw new Exception("unknown fn " + FN);',
                      '                case "selftest": output = SelfTest(args, result); break;\n'
                      '                default: throw new Exception("unknown fn " + FN);')
    src = src.replace("    // ---------------- 场景 ----------------", _SELFTEST, 1)
    return src
