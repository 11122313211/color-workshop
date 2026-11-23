"""V0.6 M4 主观评价测试：模板结构、打分校验、统计（mean/std/一致性）与存储。

对拍锚点（合成打分）：
- [5, 4] → mean 4.5，样本标准差 0.707（n−1）
- 综合分 = 各指标均值的均值
- std > 0.8 → 一致性预警
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.services.evaluations import (
    EvaluationStore,
    SCORE_SCALE,
    TEMPLATES,
    evaluation_stats,
    template_items,
    validate_scores,
)


@pytest.fixture()
def store():
    # Windows 沙箱下 pytest tmp_path 会创建 ACL 受限目录；使用隔离数据目录内普通路径。
    path = Path(os.environ["COLORFORGE_DATA_DIR"]) / f"eval-{uuid.uuid4().hex[:8]}.json"
    return EvaluationStore(path)


# --- 模板 -----------------------------------------------------------------------


def test_templates_structure():
    assert set(TEMPLATES) == {"book", "paper_pack", "plastic_pack"}
    for key, tpl in TEMPLATES.items():
        assert tpl["name"]
        assert tpl["groups"], key
        for g in tpl["groups"]:
            assert g["group"] and g["items"]
    # 三套模板的成型维度按材质差异化
    book_items = template_items("book")
    paper_items = template_items("paper_pack")
    plastic_items = template_items("plastic_pack")
    assert "装订牢固无散页" in book_items and "裁切整齐无毛边" in paper_items
    assert "封边整齐无锯齿漏封" in plastic_items
    assert "墨层附着无脱落" in plastic_items and "墨层附着无脱落" not in paper_items


def test_score_scale():
    assert set(SCORE_SCALE) == {"1", "2", "3", "4", "5"}
    assert SCORE_SCALE["5"] == "非常好"


# --- 打分校验 ---------------------------------------------------------------------


def test_validate_scores_accepts_template_items():
    clean = validate_scores("book", {"文字清晰不发虚": 5, "装订牢固无散页": "4"})
    assert clean == {"文字清晰不发虚": 5, "装订牢固无散页": 4}


def test_validate_scores_rejects_unknown_item():
    with pytest.raises(ValueError, match="指标不在模板中"):
        validate_scores("book", {"不存在指标": 5})


def test_validate_scores_rejects_out_of_range():
    with pytest.raises(ValueError, match="分值超出"):
        validate_scores("book", {"文字清晰不发虚": 9})
    with pytest.raises(ValueError, match="分值必须为"):
        validate_scores("book", {"文字清晰不发虚": "abc"})


def test_validate_scores_rejects_empty():
    with pytest.raises(ValueError, match="打分不能为空"):
        validate_scores("book", {})


# --- 统计 -------------------------------------------------------------------------


def _make_eval(store, scores_per_evaluator):
    doc = store.create_evaluation("paper_pack", "测试评价")
    for name, scores in scores_per_evaluator:
        doc = store.add_entry(doc["id"], name, scores)
    return doc


def test_stats_mean_and_sample_std(store):
    doc = _make_eval(store, [
        ("甲", {"颜色无明显偏色": 5, "裁切整齐无毛边": 4}),
        ("乙", {"颜色无明显偏色": 4, "裁切整齐无毛边": 4}),
    ])
    st = evaluation_stats(store.get(doc["id"]))
    assert st["entries"] == 2
    row = next(r for r in st["rows"] if r["item"] == "颜色无明显偏色")
    assert row["n"] == 2
    assert row["mean"] == pytest.approx(4.5)
    assert row["std"] == pytest.approx(0.707, abs=1e-3)
    # 综合分 = 各指标均值的均值 = (4.5+4.0)/2
    assert st["overall"] == pytest.approx(4.25)
    assert st["consistency_warn"] == []


def test_stats_consistency_warn(store):
    doc = _make_eval(store, [
        ("A", {"颜色无明显偏色": 5}),
        ("B", {"颜色无明显偏色": 2}),
    ])
    st = evaluation_stats(store.get(doc["id"]))
    row = next(r for r in st["rows"] if r["item"] == "颜色无明显偏色")
    assert row["std"] == pytest.approx(2.121, abs=1e-3)
    assert st["consistency_warn"] == ["颜色无明显偏色"]


def test_stats_single_entry_no_std(store):
    doc = _make_eval(store, [("甲", {"颜色无明显偏色": 4})])
    st = evaluation_stats(store.get(doc["id"]))
    row = next(r for r in st["rows"] if r["item"] == "颜色无明显偏色")
    assert row["std"] is None
    assert st["consistency_warn"] == []


def test_stats_missing_items_excluded(store):
    doc = _make_eval(store, [("甲", {"颜色无明显偏色": 4})])
    st = evaluation_stats(store.get(doc["id"]))
    scored = [r for r in st["rows"] if r["mean"] is not None]
    assert len(scored) == 1  # 未打分指标不计入综合分


# --- 存储 -------------------------------------------------------------------------


def test_store_create_get_delete_roundtrip(store):
    doc = store.create_evaluation("plastic_pack", "塑料袋抽检", "p1")
    assert doc["template_name"] == "塑料包装"
    assert store.get(doc["id"])["title"] == "塑料袋抽检"
    listed = store.list_evaluations()
    assert any(e["id"] == doc["id"] for e in listed)
    assert store.delete_evaluation(doc["id"]) is True
    assert store.get(doc["id"]) is None
    assert store.delete_evaluation(doc["id"]) is False


def test_store_add_entry_unknown_evaluation(store):
    with pytest.raises(KeyError):
        store.add_entry("ev_missing", "甲", {"颜色无明显偏色": 5})


def test_store_create_unknown_template(store):
    with pytest.raises(ValueError, match="未知评价模板"):
        store.create_evaluation("nope")


def test_store_entry_requires_evaluator_name(store):
    doc = store.create_evaluation("book")
    with pytest.raises(ValueError, match="评价人"):
        store.add_entry(doc["id"], "  ", {"文字清晰不发虚": 5})


def test_store_entry_appends_and_keeps_order(store):
    doc = store.create_evaluation("book")
    doc = store.add_entry(doc["id"], "A", {"文字清晰不发虚": 5})
    doc = store.add_entry(doc["id"], "B", {"文字清晰不发虚": 3})
    stored = store.get(doc["id"])
    assert [e["evaluator"] for e in stored["entries"]] == ["A", "B"]
    assert len(template_items("book")) >= 10  # 完整指标数覆盖
