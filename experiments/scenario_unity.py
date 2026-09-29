"""实验 C 的任务（Unity 版），和 Blender 版是同一个任务：AI 布置场景 → 人删、改、加 → AI 按旧计划整体调整。

这些是填进 UnitySide.cs.txt 里 AgentCode(result) 方法体的 C# 代码。TARGET 是实验场景。
和 Blender 版一样，这里的"AI"是写好的脚本，不是大模型（证据阶梯第 1 级）。
"""

_HELPERS = r'''
        GameObject Find(string name)
        {
            foreach (var r in TARGET.GetRootGameObjects())
                foreach (var t in r.GetComponentsInChildren<Transform>(true))
                    if (t.name == name) return t.gameObject;
            return null;
        }
        GameObject Make(string name, PrimitiveType type, Vector3 pos, Vector3 scale, Material m)
        {
            var go = GameObject.CreatePrimitive(type);
            go.name = name;
            SceneManager.MoveGameObjectToScene(go, TARGET);
            go.transform.localPosition = pos;
            go.transform.localScale = scale;
            if (m != null) go.GetComponent<Renderer>().sharedMaterial = m;
            result.RegisterObjectCreation(go);
            return go;
        }
'''

AI_BUILD = _HELPERS + r'''
        // AI 第一步：布置场景
        Material Mat(string name, Color c)
        {
            var rp = UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline;
            var shader = rp != null && rp.defaultMaterial != null ? rp.defaultMaterial.shader : Shader.Find("Standard");
            var m = new Material(shader);
            m.name = name;
            m.color = c;
            return m;
        }
        var bark = Mat("Cowork_Bark", new Color(0.35f, 0.2f, 0.1f));
        var leaf = Mat("Cowork_Leaf", new Color(0.2f, 0.6f, 0.2f));
        var stone = Mat("Cowork_Stone", new Color(0.5f, 0.5f, 0.5f));
        Make("Ground", PrimitiveType.Plane, Vector3.zero, new Vector3(2, 1, 2), null);
        Make("Trunk", PrimitiveType.Cylinder, new Vector3(0, 1.2f, 0), new Vector3(0.6f, 1.2f, 0.6f), bark);
        for (int i = 0; i < 5; i++)
        {
            float a = i * 2 * Mathf.PI / 5;
            Make("Leaf_" + (i + 1), PrimitiveType.Sphere, new Vector3(Mathf.Cos(a) * 0.8f, 2.6f, Mathf.Sin(a) * 0.8f), Vector3.one * 1.2f, leaf);
        }
        for (int i = 0; i < 3; i++)
            Make("Rock_" + (i + 1), PrimitiveType.Cube, new Vector3(3 + i * 1.2f, 0.25f, -2), Vector3.one * 0.5f, stone);
        var sun = new GameObject("Sun");
        SceneManager.MoveGameObjectToScene(sun, TARGET);
        var light = sun.AddComponent<Light>();
        light.type = LightType.Directional;
        light.intensity = 3;
        sun.transform.rotation = Quaternion.Euler(50, -30, 0);
        result.RegisterObjectCreation(sun);
'''

_AI_ADJUST = _HELPERS + r'''
        // AI 第二步：按自己之前看到的场景整体调整（它不知道人中途改了什么）
        var leaf1 = Find("Leaf_1");
        if (leaf1 != null)
        {
            var leafMat = leaf1.GetComponent<Renderer>().sharedMaterial;     // 所有叶子共用这个材质
            result.RegisterObjectModification(leafMat);
            leafMat.color = new Color(0.8f, 0.45f, 0.1f);                  // 秋天：叶子变橙色
        }
        var trunk = Find("Trunk");
        result.RegisterObjectModification(trunk.transform);
        trunk.transform.localScale = new Vector3(0.78f, 1.2f, 0.78f);       // 树干加粗
        for (int i = 1; i <= 5; i++)                                         // 叶子统一抬高到同一高度
        {
            var o = Find("Leaf_" + i);
            if (o == null) continue;
            result.RegisterObjectModification(o.transform);
            var p = o.transform.localPosition; p.y = 3.0f; o.transform.localPosition = p;
        }
        Material stoneMat = null;
        var r1 = Find("Rock_1");
        if (r1 != null) stoneMat = r1.GetComponent<Renderer>().sharedMaterial;
        for (int i = 1; i <= 3; i++)                                         // 三块石头排成一圈；缺了就补上（人明确删掉的不补）
        {
            var o = Find("Rock_" + i);
            if (o == null && SKIP.Contains("Rock_" + i)) continue;
            if (o == null) o = Make("Rock_" + i, PrimitiveType.Cube, Vector3.zero, Vector3.one * 0.5f, stoneMat);
            else result.RegisterObjectModification(o.transform);
            float a = (i - 1) * 2 * Mathf.PI / 3;
            o.transform.localPosition = new Vector3(Mathf.Cos(a) * 3, 0.25f, Mathf.Sin(a) * 3);
        }
        foreach (var r in TARGET.GetRootGameObjects())                       // 其他散落的物体放到地面上
        {
            if (r.name == "Ground" || r.name == "Trunk" || r.name == "Sun" || r.name.StartsWith("Rock_")
                || r.name.StartsWith("Leaf_") || r.name.StartsWith("__cowork")) continue;
            result.RegisterObjectModification(r.transform);
            var p = r.transform.localPosition; p.y = 0.5f; r.transform.localPosition = p;
        }
'''



def ai_adjust(skip=()) -> str:
    """skip：AI 从运行时的提示里知道"人删掉了"的对象名，这些缺了也不补（模拟 AI 读了提示再动手）。"""
    names = ", ".join('"' + n.replace('"', '') + '"' for n in sorted(skip))
    return f"        var SKIP = new List<string> {{ {names} }};\n" + _AI_ADJUST


AI_ADJUST = ai_adjust()

HUMAN_SIM = _HELPERS + r'''
        // 模拟人的三个修改
        UnityEngine.Object.DestroyImmediate(Find("Rock_2"));                         // 1. 删掉一块石头
        var l3 = Find("Leaf_3").transform;                                   // 2. 把一片叶子往上挪
        var p3 = l3.localPosition; p3.y += 0.8f; l3.localPosition = p3;
        var cube = GameObject.CreatePrimitive(PrimitiveType.Cube);           // 3. 新建一个立方体，放在空中
        cube.name = "Cube";
        SceneManager.MoveGameObjectToScene(cube, TARGET);
        cube.transform.localPosition = new Vector3(-3, 1.5f, 2);
        Selection.activeGameObject = Find("Leaf_1");                         // 4. 最后选中 Leaf_1（只有打开"选中即占用"时才有影响）
'''

HUMAN_SELECT_STEP = "最后在 Hierarchy 里点一下 Leaf_1 选中它（假装你准备接着改它），保持选中状态"

HUMAN_STEPS = [
    "在 Hierarchy 里选中 Rock_2，按 Delete 删除",
    "选中 Leaf_3，在 Inspector 的 Transform 里把 Position 的 Y 改大一点（或者用移动工具往上拖）",
    "菜单 GameObject → 3D Object → Cube 新建一个立方体，在 Inspector 里把 Position 的 Y 改成 1.5 左右（悬在空中）",
]
