"""配方反查引擎测试。

ICC 资产：tests/fixtures/icc/（littlecms testbed，MIT，跨平台）；
GRAY profile 由 conftest 运行时构造。运行: python -m pytest tests/test_recipe.py -v
"""

import numpy as np

from app.services.processes import detect_device_space
from app.services.recipe import RecipeStore, predict_recipe, run_verification


def test_detect_device_space(cmyk_icc, rgb_icc, gray_icc):
    """ICC 头部签名探测设备色彩空间。"""
    assert detect_device_space(cmyk_icc) == "CMYK"
    assert detect_device_space(rgb_icc) == "RGB"
    assert detect_device_space(gray_icc) == "GRAY"
    print("[PASS] 设备色彩空间探测")


def test_forward_physical_sanity(cmyk_icc):
    """正向预测物理合理性：CMYK 四原色的 Lab 方向正确。"""
    from app.services.recipe import _build_forward

    forward = _build_forward(cmyk_icc, 4)
    pts = np.array([
        [0.0, 0.0, 0.0, 0.0],  # 纸色 -> L≈100
        [0.0, 1.0, 1.0, 0.0],  # M+Y 红 -> a>0
        [1.0, 0.0, 0.0, 0.0],  # C 青 -> a<0, b<0
        [0.0, 0.0, 1.0, 0.0],  # Y 黄 -> b>0
    ])
    labs = forward(pts)
    assert labs[0][0] > 95, "纸色 L 应接近 100"
    assert labs[1][1] > 30, "红 a* 应为正且较高"
    assert labs[2][1] < 0 and labs[2][2] < 0, "青 a*/b* 应为负"
    assert labs[3][2] > 40, "黄 b* 应为正且较高"
    print("[PASS] 正向预测物理合理性")


def test_predict_recipe_cmyk(cmyk_icc):
    """LAB→CMYK 反查：色域内目标应收敛到 ΔE<3。"""
    targets = [
        [50.0, 20.0, 5.0],    # 中红
        [50.0, -20.0, -25.0],  # 青
        [60.0, 30.0, 40.0],   # 橙
        [85.0, -8.0, 30.0],   # 浅暖黄
    ]
    for target in targets:
        r = predict_recipe(cmyk_icc, "CMYK", target, tolerance=2.0)
        assert set(r["recipe"].keys()) == {"C", "M", "Y", "K"}
        assert all(0.0 <= v <= 100.0 for v in r["recipe"].values())
        assert r["delta_e"] < 3.0, f"目标 {target} 预测 ΔE={r['delta_e']} 应优于 3"
        assert r["converged"] is True
        assert len(r["iterations"]) >= 1
        assert r["predictions"] > 0
        # 轨迹应为严格递减序列（去重后）
        assert all(a > b for a, b in zip(r["iterations"], r["iterations"][1:])), (
            f"ΔE 轨迹应递减无重复: {r['iterations']}"
        )
    print("[PASS] LAB→CMYK 反查收敛（4 个色域内目标全部 ΔE<3，轨迹递减）")


def test_predict_recipe_gray(gray_icc):
    """GRAY 工艺反查（1 通道引擎泛化，P0 修复回归）。"""
    # 生成式灰度 profile 语义为「亮度」：L 目标越高 K 通道值越大
    r = predict_recipe(gray_icc, "GRAY", [60.0, 0.0, 0.0], tolerance=2.0)
    assert set(r["recipe"].keys()) == {"K"}
    assert 0.0 <= r["recipe"]["K"] <= 100.0
    assert r["delta_e"] <= 1.5, f"灰度曲线目标应精确收敛，ΔE={r['delta_e']}"
    print(f"[PASS] GRAY 工艺反查（ΔE={r['delta_e']}）")


def test_predict_recipe_rgb_process(rgb_icc):
    """RGB 工艺反查（3 通道引擎泛化）。"""
    # sRGB 色域内的目标：Lab 50,20,5 ≈ 暗红
    r = predict_recipe(rgb_icc, "RGB", [50.0, 20.0, 5.0], tolerance=2.0)
    assert set(r["recipe"].keys()) == {"R", "G", "B"}
    assert r["delta_e"] < 3.0
    print(f"[PASS] RGB 工艺反查（ΔE={r['delta_e']}）")


def test_predict_recipe_out_of_gamut(cmyk_icc):
    """色域外目标：正常返回最优可印配方，不收敛但不应崩溃。"""
    r = predict_recipe(cmyk_icc, "CMYK", [55.0, -80.0, 25.0], tolerance=2.0)  # 高饱和绿
    assert r["converged"] is False
    assert r["delta_e"] > 2.0
    assert all(0.0 <= v <= 100.0 for v in r["recipe"].values())
    print(f"[PASS] 色域外目标返回最优可印配方（ΔE={r['delta_e']}，不虚假收敛）")


def test_verification_closed_loop(cmyk_icc):
    """实测验证闭环：模型误差补偿应改善 ΔE。"""
    r = predict_recipe(cmyk_icc, "CMYK", [50.0, 20.0, 5.0], tolerance=2.0)
    doc = {
        "target_lab": r["target_lab"],
        "predicted_lab": r["predicted_lab"],
        "method": "cie2000",
        "tolerance": 2.0,
    }
    # 模拟打印实测：预测值 + 偏移
    p = r["predicted_lab"]
    measured = [p[0] + 3.0, p[1] - 2.0, p[2] + 1.5]
    ver = run_verification(cmyk_icc, "CMYK", doc, measured)
    assert ver["delta_e_corrected"] < ver["delta_e_measured"], "闭环修正应改善 ΔE"
    assert ver["improved"] is True
    print(f"[PASS] 实测验证闭环（dE {ver['delta_e_measured']} -> {ver['delta_e_corrected']}）")


def test_interp_exact_on_linear_function():
    """插值精搜层单元：多线性插值对线性函数应处处精确（网格无关）。"""
    from app.services.recipe import _make_interp

    levels, channels = 5, 2
    axes = np.linspace(0.0, 1.0, levels)
    g = np.meshgrid(*([axes] * channels), indexing="ij")
    pts = np.stack([x.ravel() for x in g], axis=1)
    # 线性真值映射：Lab = M·pt + b（多线性插值对线性函数无近似误差）
    mat = np.array([[60.0, -25.0], [3.0, 7.5], [18.0, -40.0]])  # (3 Lab 维, 2 通道维)
    offset = np.array([35.0, -4.0, 2.0])
    labs = pts @ mat.T + offset

    interp = _make_interp(pts, labs, levels, channels)
    # 全部格点精确
    assert np.allclose(interp(pts), labs, atol=1e-9)
    # 非格点随机查询同样精确（线性函数被多线性插值完全重构）
    rng = np.random.default_rng(7)
    q = rng.random((200, channels))
    assert np.allclose(interp(q), q @ mat.T + offset, atol=1e-9)
    # 越界裁剪到边界值
    edge = interp(np.array([[-0.5, -0.5], [1.5, 1.5]]))
    assert np.allclose(edge[0], labs[0], atol=1e-9)
    assert np.allclose(edge[1], labs[-1], atol=1e-9)
    print("[PASS] 插值器对线性函数处处精确")


def test_refinement_never_worse(cmyk_icc):
    """插值精搜守门：开启精搜的真实 ΔE 不得劣于 V0.3 粗搜路径。"""
    import app.services.recipe as R

    targets = [
        [50.0, 20.0, 5.0],
        [50.0, -20.0, -25.0],
        [60.0, 30.0, 40.0],
        [85.0, -8.0, 30.0],
    ]
    try:
        for target in targets:
            R.REFINE_ENABLED = False
            old = R.predict_recipe(cmyk_icc, "CMYK", target)
            R.REFINE_ENABLED = True
            new = R.predict_recipe(cmyk_icc, "CMYK", target)
            assert new["delta_e"] <= old["delta_e"] + 1e-6, (
                f"目标 {target}：精搜后 ΔE {new['delta_e']} 不应劣于粗搜 {old['delta_e']}"
            )
            assert new["refine_predictions"] >= 0
    finally:
        R.REFINE_ENABLED = True
    print("[PASS] 精搜不劣化守门（4 目标全部 ≤ 粗搜基线）")


def test_recipe_store_persistence():
    """配方记录落盘与读取（COLORFORGE_DATA_DIR 隔离环境）。"""
    store = RecipeStore()
    proc = {"id": "testproc", "name": "测试工艺", "icc_meta": {"device_space": "CMYK"}}
    result = {
        "target_lab": [50.0, 20.0, 5.0],
        "predicted_lab": [49.8, 20.0, 5.0],
        "recipe": {"C": 6.2, "M": 46.9, "Y": 18.8, "K": 40.6},
        "delta_e": 0.196,
        "iterations": [0.98, 0.2],
        "predictions": 100,
        "lut_points": 83521,
        "method": "cie2000",
        "tolerance": 2.0,
        "converged": True,
    }
    doc = store.create(proc, result, label="测试")
    assert store.get(doc["id"]) is not None
    docs = store.list("testproc")
    assert any(d["id"] == doc["id"] for d in docs)
    assert store.delete(doc["id"]) is True
    assert store.get(doc["id"]) is None
    print("[PASS] 配方记录落盘存储")
