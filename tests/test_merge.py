"""三方合并小工具的单元测试。"""
from rightofway.merge import three_way_merge

BASE = "一\n\n二\n\n三"


def test_merge_different_paragraphs():
    assert three_way_merge(BASE, "一\n\n二改\n\n三", "一\n\n二\n\n三改") == "一\n\n二改\n\n三改"


def test_merge_same_change_on_both_sides():
    assert three_way_merge(BASE, "一改\n\n二\n\n三", "一改\n\n二\n\n三") == "一改\n\n二\n\n三"


def test_merge_conflict_returns_none():
    assert three_way_merge(BASE, "一\n\n二甲\n\n三", "一\n\n二乙\n\n三") is None


def test_merge_paragraph_count_changed_returns_none():
    assert three_way_merge(BASE, "一\n\n二\n\n三\n\n四", "一\n\n二\n\n三改") is None
