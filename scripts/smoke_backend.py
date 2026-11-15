"""后端冒烟：完整走通 工艺→配方预测→实测验证→色域→预检→转换（自清理）。"""
import io
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from app.services.processes import ProcessStore, sample_process_gamut
from app.services.recipe import RecipeStore, predict_recipe, run_verification
from app.services.files import preflight, convert_image

WIN = Path(r"C:/Windows/System32/spool/drivers/color")
RSWOP = (WIN / "RSWOP.icm").read_bytes()

# 1. 建工艺
store = ProcessStore()
proc = store.create("冒烟工艺-SWOP", RSWOP, paper="铜版纸", ink="标准四色", filename="RSWOP.icm")
print(f"[1] 工艺创建: {proc['name']} space={proc['icc_meta']['device_space']} ch={proc['icc_meta']['channels']}")
assert proc["icc_meta"]["device_space"] == "CMYK"

# 2. 配方预测（三个工艺色域内的目标色）
icc = store.icc_bytes(proc["id"])
targets = [
    ([50.0, 20.0, 5.0], "中红"),
    ([50.0, -20.0, -25.0], "青"),
    ([60.0, 30.0, 40.0], "橙"),
]
results = []
for lab, label in targets:
    r = predict_recipe(icc, "CMYK", lab, tolerance=2.0)
    results.append(r)
    print(f"[2] {label} 目标LAB{lab} -> 配方 {r['recipe']} 预测LAB{r['predicted_lab']} "
          f"dE={r['delta_e']} 收敛={r['converged']} 迭代={r['iterations']} 预测数={r['predictions']}")
    assert r["delta_e"] < 3.0, f"{label} 在色域内，预测 ΔE 应优于 3"

# 3. 配方入库 + 实测验证闭环
rstore = RecipeStore()
doc = rstore.create(proc, {**results[0], "method": "cie2000", "tolerance": 2.0}, "冒烟红")
# 模拟打印实测：给"中红"的预测值加一点偏移
p0 = results[0]["predicted_lab"]
measured = [p0[0] + 3.0, p0[1] - 2.0, p0[2] + 1.5]
ver = run_verification(icc, "CMYK", doc, measured)
rstore.append_verification(doc["id"], ver)
print(f"[3] 实测 {measured} -> dE实测={ver['delta_e_measured']} 模型误差={ver['delta_e_model_error']} "
      f"修正后dE={ver['delta_e_corrected']} 改善={ver['improved']}")
assert ver["delta_e_corrected"] < ver["delta_e_measured"], "修正应改善 ΔE"

# 4. 色域采样
g = sample_process_gamut(proc, levels=6)
print(f"[4] 色域: {g['sampled']} 点 volume={g['volume']:.0f} L范围={g['L_range']} xy样本前3={g['xy_points'][:3]}")
assert g["sampled"] > 0 and len(g["xy_points"]) == 6 ** 4

# 5. 印前预检 + 转换（无嵌入 ICC 的测试图）
img = Image.new("RGB", (64, 64), (180, 130, 85))
buf = io.BytesIO()
img.save(buf, format="PNG")
report = preflight(buf.getvalue(), "test.png")
print(f"[5] 预检: status={report['status']} issues={len(report['issues'])} 主导色={report['dominant_colors'][:2]}")
assert report["has_embedded_icc"] is False
conv = convert_image(buf.getvalue(), icc, "CMYK")
print(f"    转换: format={conv['file_format']} mode={conv['output_mode']} 源={conv['source_profile']} "
      f"预览{(len(conv['preview_b64']) // 1024)}KB 文件{(len(conv['file_b64']) // 1024)}KB")
assert conv["output_mode"] == "CMYK"

# 6. 清理
rstore.delete(doc["id"])
store.delete(proc["id"])
print("[6] 清理完成")
print("SMOKE OK")
