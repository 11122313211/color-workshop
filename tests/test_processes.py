"""工艺库与印前文件服务测试。"""

import io
import os
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.files import convert_image, preflight
from app.services.processes import ProcessStore, sample_process_gamut
from app.services.visualization import spectrum_locus

WIN_COLOR_DIR = Path(r"C:/Windows/System32/spool/drivers/color")

# CIE 1931 2° 标准观察者光谱轨迹标准值
# 来源：CIE 15 表（蓝/绿区与 Wikipedia 表一致）+ 红区由标准 CMF 手工复算
# （x̄(600)=1.0622, ȳ(600)=0.6310 → xy=(0.6270, 0.3725) 等，经交叉验证）
LOCUS_ANCHORS = {
    380: (0.1741, 0.0050),
    460: (0.1440, 0.0297),
    500: (0.0082, 0.5384),
    520: (0.0743, 0.8338),
    560: (0.3731, 0.6245),
    600: (0.6270, 0.3725),
    640: (0.7190, 0.2809),
    700: (0.7347, 0.2653),
}


def test_spectrum_locus_anchors():
    """光谱轨迹科学性：逐波长 xy 必须匹配 CIE 1931 2° 标准值（±0.004）。"""
    data = spectrum_locus()
    by_wl = {p["wavelength"]: (p["x"], p["y"]) for p in data["locus"]}
    for wl, (ex, ey) in LOCUS_ANCHORS.items():
        # 后端采样 2nm 步长，取最近采样点
        near = min(by_wl, key=lambda w: abs(w - wl))
        gx, gy = by_wl[near]
        assert abs(gx - ex) <= 0.004 and abs(gy - ey) <= 0.004, (
            f"{wl}nm 光谱轨迹偏差过大: got ({gx:.4f}, {gy:.4f}), expected ({ex}, {ey})"
        )
    # y 最大值应出现在绿区 ~520nm 且 ≈0.8338
    ymax_p = max(data["locus"], key=lambda p: p["y"])
    assert 515 <= ymax_p["wavelength"] <= 525 and abs(ymax_p["y"] - 0.8338) <= 0.003
    # 每个轨迹点都有合法的光谱显示色
    import re
    for p in data["locus"]:
        assert re.fullmatch(r"#[0-9a-f]{6}", p["hex"]), f"{p['wavelength']}nm hex 非法: {p['hex']}"
    # 紫线连接 380nm 与 700nm 端点
    assert data["purple_line"][0] == [data["locus"][0]["x"], data["locus"][0]["y"]]
    assert data["purple_line"][1] == [data["locus"][-1]["x"], data["locus"][-1]["y"]]
    print("[PASS] 光谱轨迹锚点与 CIE 1931 2° 标准值一致（8 个波长 ±0.005）")


def _load_cmyk_icc() -> bytes:
    for name in ("RSWOP.icm", "JapanColor2001Coated.icc"):
        p = WIN_COLOR_DIR / name
        if p.exists():
            return p.read_bytes()
    import pytest
    pytest.skip("系统无 CMYK ICC profile，跳过工艺库测试")


def test_process_create_and_meta():
    """工艺创建：ICC 落盘 + 元数据解析。"""
    icc = _load_cmyk_icc()
    store = ProcessStore()
    proc = store.create("测试工艺-CMYK", icc, paper="铜版纸", ink="四色", filename="RSWOP.icm")
    assert proc["icc_meta"]["device_space"] == "CMYK"
    assert proc["icc_meta"]["channels"] == 4
    assert proc["icc_meta"]["description"]
    assert (Path(__file__).resolve().parents[1] / "data" / "processes" / proc["icc_file"]).exists()

    got = store.get(proc["id"])
    assert got and got["name"] == "测试工艺-CMYK"
    assert store.delete(proc["id"]) is True
    print(f"[PASS] 工艺创建与元数据（{proc['icc_meta']['description'][:30]}）")


def test_process_duplicate_icc_same_hash():
    """同一 ICC 文件的哈希指纹一致（缓存与色域复用的基础）。"""
    icc = _load_cmyk_icc()
    store = ProcessStore()
    p1 = store.create("哈希1", icc)
    p2 = store.create("哈希2", icc)
    assert p1["icc_sha256"] == p2["icc_sha256"]
    store.delete(p1["id"])
    store.delete(p2["id"])
    print("[PASS] 同一 ICC 哈希指纹一致")


def test_process_gamut_sampling():
    """工艺色域采样：xy 点集 + Lab 包围盒体积。"""
    icc = _load_cmyk_icc()
    store = ProcessStore()
    proc = store.create("色域测试", icc)
    g = sample_process_gamut(proc, levels=6)
    assert len(g["xy_points"]) == 6 ** 4
    assert g["volume"] > 0
    assert 0.0 <= g["L_range"][0] <= g["L_range"][1] <= 100.0
    # xy 色度点应落在合理范围
    for x, y in g["xy_points"][:10]:
        assert 0.0 <= x <= 0.8 and 0.0 <= y <= 0.8
    store.delete(proc["id"])
    print(f"[PASS] 工艺色域采样（{g['sampled']} 点，体积 {g['volume']:.0f}）")


def _tiny_image(icc_embed: bytes | None = None) -> bytes:
    img = Image.new("RGB", (48, 48), (180, 130, 85))
    buf = io.BytesIO()
    save_kwargs = {"format": "PNG"}
    if icc_embed:
        save_kwargs["icc_profile"] = icc_embed
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def test_preflight_no_icc_warns():
    """预检：无嵌入 ICC 应给出 warn 与建议。"""
    report = preflight(_tiny_image(), "demo.png")
    assert report["has_embedded_icc"] is False
    assert report["status"] == "warn"
    assert any(i["level"] == "warn" for i in report["issues"])
    assert report["size"] == [48, 48]
    print("[PASS] 预检：无嵌入 ICC 触发 warn")


def test_preflight_with_icc():
    """预检：嵌入 ICC 应解析出描述。"""
    icc = _load_cmyk_icc()
    report = preflight(_tiny_image(icc_embed=icc), "demo-icc.png")
    assert report["has_embedded_icc"] is True
    assert report["embedded_icc"].get("device_space") == "CMYK"
    assert report["status"] == "pass"
    print("[PASS] 预检：嵌入 ICC 解析")


def test_convert_image_to_cmyk():
    """印前转换：sRGB 图 → CMYK TIFF + 软打样预览。"""
    icc = _load_cmyk_icc()
    result = convert_image(_tiny_image(), icc, "CMYK")
    assert result["output_mode"] == "CMYK"
    assert result["file_format"] == "TIFF"
    assert result["preview_b64"].startswith("data:image/png")
    assert result["file_b64"].startswith("data:image/tiff")
    # 成品文件可还原解析
    raw = io.BytesIO(__import__("base64").b64decode(result["file_b64"].split(",", 1)[1]))
    out_img = Image.open(raw)
    assert out_img.mode == "CMYK"
    print("[PASS] 印前转换（CMYK TIFF + 软打样预览）")


if __name__ == "__main__":
    print("=" * 50)
    print("工艺库与印前文件测试")
    print("=" * 50)
    test_process_create_and_meta()
    test_process_duplicate_icc_same_hash()
    test_process_gamut_sampling()
    test_preflight_no_icc_warns()
    test_preflight_with_icc()
    test_convert_image_to_cmyk()
    test_spectrum_locus_anchors()
    print("全部测试通过!")
