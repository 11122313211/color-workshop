"""PDF/X-4 封装与印前预检「渲染验证防线」对照与回归测试。

覆盖：
1. 人工空白页 PDF：结构全合格但页面空白 → 触发 blank_page_suspect（warning），不静默通过、不删除产物；
2. 封装中被破坏的样本（完全丢失内容变白 / 部分墨量丢失 > 5%）→ 触发 ink_drift_suspect（rejected），记录每页数值；
3. 正常含内容 CMYK PDF（单页/多页）→ verified / ok 不误报；
4. 可配置阈值（blank_threshold / drift_threshold）；
5. 大页数（> 20 页）首尾必检 + 中间均匀抽样策略；
6. 报警信息进入预检报告、PDF/X API 响应与签样/放行门禁（verified / warning / rejected 分级一致）。
"""

from __future__ import annotations

import base64
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    FloatObject,
    NameObject,
)

from app.services import pdfx as pdfx_service
from app.services.render_check import (
    MAX_FULL_RENDER_PAGES,
    compare_pdf_render,
    select_page_indices,
    verify_pdf_render,
)

pytestmark = pytest.mark.api


def _vector_cmyk_pdf(page_ops: list[bytes]) -> bytes:
    """构造结构齐全的 CMYK 矢量 PDF（含 MediaBox / TrimBox / BleedBox，无未嵌入字体与 RGB）。"""
    mm = 72.0 / 25.4
    a4_w, a4_h = 210.0 * mm, 297.0 * mm
    pad = 3.0 * mm
    media = [-pad, -pad, a4_w + pad, a4_h + pad]

    writer = PdfWriter()
    for ops in page_ops:
        page = writer.add_blank_page(width=media[2] - media[0], height=media[3] - media[1])
        page[NameObject("/MediaBox")] = ArrayObject([FloatObject(v) for v in media])
        page[NameObject("/BleedBox")] = ArrayObject([FloatObject(v) for v in media])
        page[NameObject("/TrimBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(a4_w), FloatObject(a4_h),
        ])
        if ops:
            content = DecodedStreamObject()
            content.set_data(ops)
            page[NameObject("/Contents")] = writer._add_object(content)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _post_pdfx(api_client, process_id: str, pdf_bytes: bytes, **params):
    return api_client.post(
        "/api/files/pdfx",
        params={"process_id": process_id, "filename": "job.pdf", **params},
        content=pdf_bytes,
        headers={"Content-Type": "application/pdf"},
    )


def _decode_pdf(result: dict) -> bytes:
    assert result["file_b64"].startswith("data:application/pdf;base64,")
    return base64.b64decode(result["file_b64"].split(",", 1)[1])


# ---------------------------------------------------------------------------
# 1. 人工空白页对照测试（防检查器变摆设）
# ---------------------------------------------------------------------------


def test_blank_page_pdf_triggers_blank_page_suspect_without_deleting_output(
    api_client, process_factory
):
    """结构完全合法但内容空白的 PDF：不静默通过、不删除产物，预检与封装均显式报警。"""
    proc = process_factory("渲染防线-空白页工艺")
    blank_pdf = _vector_cmyk_pdf([b""])

    # 预检报告显式报警 blank_page_suspect（warning）
    pf_resp = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(blank_pdf).decode(), "filename": "blank.pdf"},
    )
    assert pf_resp.status_code == 200, pf_resp.text
    pf = pf_resp.json()
    assert pf["render_check"] == "blank_page_suspect"
    assert pf["pdf"]["render_check"] == "blank_page_suspect"
    assert pf["render_verification"]["verdict"] == "warning"
    assert pf["render_verification"]["blank_pages"] == [1]
    assert pf["status"] == "warn"
    assert any("blank_page_suspect" in i["message"] for i in pf["issues"])

    # PDF/X-4 封装：strict=1 结构门禁通过，但渲染验证触发 blank_page_suspect
    r = _post_pdfx(api_client, proc["id"], blank_pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["render_check"] == "blank_page_suspect"
    rv = data["render_verification"]
    assert rv["verdict"] == "warning"
    assert "blank_page_suspect" in rv["flags"]
    assert rv["blank_pages"] == [1]
    assert rv["drift_pages"] == []
    assert rv["pages"][0]["after_ink_ratio"] < 0.001
    # 绝不静默 pass，也不删除封装产物
    assert data["status"] == "warn"
    assert any("blank_page_suspect" in c["message"] for c in data["checks"])
    out_bytes = _decode_pdf(data)
    reader = PdfReader(io.BytesIO(out_bytes))
    assert len(reader.pages) == 1
    assert "/OutputIntents" in reader.trailer["/Root"]


# ---------------------------------------------------------------------------
# 2. 有内容但封装中被破坏的样本（ink_drift_suspect）
# ---------------------------------------------------------------------------


def test_corrupted_during_packaging_triggers_ink_drift_rejected(
    api_client, process_factory, monkeypatch
):
    """封装前正常有内容、封装写入时页面内容丢失变白 → ink_drift_suspect + rejected，产物保留供排查。"""
    proc = process_factory("渲染防线-封装破坏工艺")
    # 两块明显的 CMYK 矢量色块（约 12% 非白像素覆盖）
    rich_ops = b"0.8 0.2 0 0.1 k 50 50 220 220 re f 0 0.9 0.7 0 k 300 320 180 180 re f"
    normal_pdf = _vector_cmyk_pdf([rich_ops])

    orig_serialize = pdfx_service._serialize_pdfx

    def _corrupt_serialize(writer: PdfWriter) -> bytes:
        # 模拟结构完好（OutputIntent/XMP/盒子全部正常）但页面内容在写入阶段丢失
        for page in writer.pages:
            if "/Contents" in page:
                del page[NameObject("/Contents")]
        return orig_serialize(writer)

    monkeypatch.setattr(pdfx_service, "_serialize_pdfx", _corrupt_serialize)

    r = _post_pdfx(api_client, proc["id"], normal_pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["render_check"] == "ink_drift_suspect"
    rv = data["render_verification"]
    assert rv["verdict"] == "rejected"
    assert "ink_drift_suspect" in rv["flags"]
    assert "blank_page_suspect" in rv["flags"]
    assert rv["drift_pages"] == [1]
    assert rv["blank_pages"] == [1]
    p1 = rv["pages"][0]
    assert p1["before_ink_ratio"] > 0.05
    assert p1["after_ink_ratio"] < 0.001
    assert p1["relative_drift"] > 0.9
    assert data["status"] == "error"
    assert data["compliant"] is False
    assert any(c["level"] == "error" and "ink_drift_suspect" in c["message"] for c in data["checks"])
    # 产物仍保留不删除，可供人工排查
    assert len(PdfReader(io.BytesIO(_decode_pdf(data))).pages) == 1


def test_partial_ink_loss_triggers_ink_drift_without_blank_false_positive(
    api_client, process_factory, monkeypatch
):
    """封装后丢失一半墨量（仍非空白页）→ 仅触发 ink_drift_suspect（rejected），不误报 blank_page_suspect。"""
    proc = process_factory("渲染防线-部分墨量漂移工艺")
    two_blocks = b"0.8 0.2 0 0.1 k 50 50 220 220 re f 0 0.9 0.7 0 k 300 320 200 200 re f"
    one_block = b"0.8 0.2 0 0.1 k 50 50 220 220 re f"
    normal_pdf = _vector_cmyk_pdf([two_blocks])

    orig_serialize = pdfx_service._serialize_pdfx

    def _drop_half_ink(writer: PdfWriter) -> bytes:
        content = DecodedStreamObject()
        content.set_data(one_block)
        writer.pages[0][NameObject("/Contents")] = writer._add_object(content)
        return orig_serialize(writer)

    monkeypatch.setattr(pdfx_service, "_serialize_pdfx", _drop_half_ink)

    r = _post_pdfx(api_client, proc["id"], normal_pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    rv = data["render_verification"]
    assert data["render_check"] == "ink_drift_suspect"
    assert rv["verdict"] == "rejected"
    assert rv["drift_pages"] == [1]
    assert rv["blank_pages"] == []
    assert rv["flags"] == ["ink_drift_suspect"]
    assert rv["pages"][0]["relative_drift"] > 0.3
    assert data["compliant"] is False


# ---------------------------------------------------------------------------
# 3. 正常文件不误报 + 可配置阈值
# ---------------------------------------------------------------------------


def test_normal_multipage_cmyk_pdf_passes_without_false_positive(
    api_client, process_factory
):
    """正常有内容的双页 CMYK PDF：封装与回读预检均 verified / ok，不误报。"""
    proc = process_factory("渲染防线-正常文件工艺")
    page1 = b"0.7 0.1 0.2 0.05 k 60 60 220 220 re f"
    page2 = b"0.1 0.8 0.6 0.1 k 100 180 260 160 re f"
    normal_pdf = _vector_cmyk_pdf([page1, page2])

    r = _post_pdfx(api_client, proc["id"], normal_pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "pass"
    assert data["compliant"] is True
    assert data["render_check"] == "ok"
    rv = data["render_verification"]
    assert rv["verdict"] == "verified"
    assert rv["flags"] == []
    assert rv["blank_pages"] == []
    assert rv["drift_pages"] == []
    assert rv["checked_pages"] == 2 and rv["sampled"] is False
    assert rv["max_relative_drift"] <= 0.05
    assert all(p["status"] == "verified" and p["after_ink_ratio"] > 0.05 for p in rv["pages"])

    # 封装产物回读预检：同样 verified / pass
    pkg_bytes = _decode_pdf(data)
    pf = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(pkg_bytes).decode(), "filename": "out.pdf"},
    ).json()
    assert pf["render_check"] == "ok"
    assert pf["render_verification"]["verdict"] == "verified"
    assert pf["status"] == "pass"


def test_configurable_blank_and_drift_thresholds(api_client, process_factory):
    """空白页阈值可通过参数配置：极小色块在默认 0.1% 下通过，收紧阈值至 2% 时报警。"""
    proc = process_factory("渲染防线-可配置阈值工艺")
    # 50x50 pt 色块占 A4 面积约 0.5%（> 0.1% 但 < 2%）
    small_block = b"0 0 0 1 k 50 50 50 50 re f"
    pdf = _vector_cmyk_pdf([small_block])

    r_default = _post_pdfx(api_client, proc["id"], pdf, strict="1")
    assert r_default.status_code == 200, r_default.text
    assert r_default.json()["render_check"] == "ok"
    assert r_default.json()["render_verification"]["verdict"] == "verified"

    r_strict_thresh = _post_pdfx(
        api_client, proc["id"], pdf, strict="1", blank_threshold="0.02"
    )
    assert r_strict_thresh.status_code == 200, r_strict_thresh.text
    body = r_strict_thresh.json()
    assert body["render_check"] == "blank_page_suspect"
    assert body["render_verification"]["verdict"] == "warning"
    assert body["status"] == "warn"


# ---------------------------------------------------------------------------
# 4. 页数过多时的抽样策略
# ---------------------------------------------------------------------------


def test_large_page_count_sampling_checks_first_and_last_pages():
    """超过 20 页时触发首 2 + 尾 2 + 中间均匀抽样，且能捕获封底（末页）空白。"""
    indices, sampled = select_page_indices(40, max_pages=MAX_FULL_RENDER_PAGES)
    assert sampled is True
    assert len(indices) == MAX_FULL_RENDER_PAGES
    assert {0, 1, 38, 39}.issubset(set(indices))
    assert indices == sorted(indices)

    # 构造 24 页 PDF，前 23 页有内容、第 24 页（封底）空白
    block = b"0.6 0.2 0 0.1 k 80 80 180 180 re f"
    pdf_24 = _vector_cmyk_pdf([block] * 23 + [b""])

    rv = verify_pdf_render(pdf_24)
    assert rv["sampled"] is True
    assert rv["total_pages"] == 24
    assert rv["checked_pages"] == MAX_FULL_RENDER_PAGES
    checked_page_nos = [p["page"] for p in rv["pages"]]
    assert {1, 2, 23, 24}.issubset(set(checked_page_nos))
    assert rv["code"] == "blank_page_suspect"
    assert rv["blank_pages"] == [24]

    cmp_rv = compare_pdf_render(pdf_24, pdf_24)
    assert cmp_rv["sampled"] is True
    assert cmp_rv["code"] == "blank_page_suspect"
    assert cmp_rv["blank_pages"] == [24]
    assert cmp_rv["drift_pages"] == []


# ---------------------------------------------------------------------------
# 5. 签样 / 放行门禁可见性与分级拦截（verified / warning / rejected）
# ---------------------------------------------------------------------------


def test_signoff_gate_surfaces_warning_and_blocks_rejected_render_verification(
    api_client, process_factory
):
    """渲染验证报警进入签样/放行门禁：warning 显式可见，rejected 阻断放行与交接清单。"""
    proc_warn = process_factory("门禁-渲染警告工艺")
    sg_warn = api_client.post(
        "/api/signoffs",
        json={
            "process_id": proc_warn["id"],
            "title": "含空白衬页签样",
            "evidence": {
                "file_name": "book-PDFX4.pdf",
                "render_check": "blank_page_suspect",
                "render_verification": {
                    "verdict": "warning",
                    "code": "blank_page_suspect",
                    "flags": ["blank_page_suspect"],
                    "blank_pages": [2],
                    "reasons": ["第 2 页疑似空白页，人工确认系衬页"],
                },
            },
        },
    ).json()
    assert api_client.post(f"/api/signoffs/{sg_warn['id']}/submit", json={"actor": "印前"}).status_code == 200
    assert api_client.post(f"/api/signoffs/{sg_warn['id']}/approve", json={"actor": "主管"}).status_code == 200

    gate_warn = api_client.get(f"/api/signoffs/gate?process_id={proc_warn['id']}").json()
    assert gate_warn["allowed"] is True
    assert gate_warn["render_check"] == "blank_page_suspect"
    assert gate_warn["render_verification"]["verdict"] == "warning"
    assert gate_warn["render_verification"]["blank_pages"] == [2]

    manifest_warn = api_client.get(f"/api/signoffs/release-manifest?process_id={proc_warn['id']}")
    assert manifest_warn.status_code == 200
    assert manifest_warn.json()["render_verification"]["code"] == "blank_page_suspect"

    # rejected（例如 ink_drift_suspect）阻断放行门禁与交接清单
    proc_rej = process_factory("门禁-渲染拒绝工艺")
    sg_rej = api_client.post(
        "/api/signoffs",
        json={
            "process_id": proc_rej["id"],
            "title": "墨量漂移异常签样",
            "evidence": {
                "file_name": "broken-PDFX4.pdf",
                "render_check": "ink_drift_suspect",
                "render_verification": {
                    "verdict": "rejected",
                    "code": "ink_drift_suspect",
                    "flags": ["ink_drift_suspect"],
                    "drift_pages": [1],
                    "reasons": ["第 1 页封装前后墨量相对偏差 100% > 5.0%"],
                },
            },
        },
    ).json()
    assert api_client.post(f"/api/signoffs/{sg_rej['id']}/submit", json={"actor": "印前"}).status_code == 200
    assert api_client.post(f"/api/signoffs/{sg_rej['id']}/approve", json={"actor": "主管"}).status_code == 200

    gate_rej = api_client.get(f"/api/signoffs/gate?process_id={proc_rej['id']}").json()
    assert gate_rej["allowed"] is False
    assert gate_rej["reason"] == "render_rejected"
    assert gate_rej["render_check"] == "ink_drift_suspect"
    assert gate_rej["render_verification"]["verdict"] == "rejected"

    manifest_rej = api_client.get(f"/api/signoffs/release-manifest?process_id={proc_rej['id']}")
    assert manifest_rej.status_code == 409
    assert "渲染验证" in manifest_rej.json()["detail"]
