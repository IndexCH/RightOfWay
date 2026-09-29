"""D 类：复制与来源（复制产生新对象；副本由人创建，属于人碰过）。"""
from cowork import OpStatus
from helpers import AGENT, HUMAN, make_runtime, submit


def test_S_D1_appropriate_intermediate_result():
    """S-D1 拿走中间结果自己用：AI 继续改原件，不影响副本；AI 也不能改副本。"""
    rt = make_runtime()
    rt.create_object("card_set", "figma_node", "cards-v1", AGENT)
    submit(rt, "op", targets=["card_set"])

    copy = rt.copy_object(HUMAN, "card_set", "card_copy")
    assert copy.provenance == {"derivedFrom": "card_set", "version": 1}

    assert rt.complete("op", writes={"card_set": "cards-v2"}).status == OpStatus.COMMITTED
    assert rt.objects["card_copy"].content == "cards-v1"
    assert submit(rt, "op2", targets=["card_copy"]).status == OpStatus.REJECTED_HUMAN_TOUCHED


def test_S_D2_take_over_a_copy():
    """S-D2 复制一份进行中的作品另外改：改副本不会占用原件。"""
    rt = make_runtime()
    rt.create_object("page", "figma_node", "page-v1", AGENT)
    rt.copy_object(HUMAN, "page", "page_copy")
    rt.human_edit(HUMAN, "page_copy", "my-version")
    assert rt.objects["page"].occupancy is None
    assert submit(rt, "op", targets=["page"]).status == OpStatus.EXECUTING
