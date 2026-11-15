"""探针脚本：验证 ImageCms 关键能力（LAB 变换 / CMYK 支持 / 精度 / 头部解析）。"""
import io
import sys
from pathlib import Path

from PIL import Image, ImageCms
import numpy as np

WIN_COLOR_DIR = Path(r"C:/Windows/System32/spool/drivers/color")

def icc_header(data: bytes) -> dict:
    """解析 ICC 头部：设备类(8:12)、数据色彩空间(12:16)、PCS(16:20)。"""
    return {
        "device_class": data[8:12].decode("ascii", "replace").strip(),
        "data_space": data[12:16].decode("ascii", "replace").strip(),
        "pcs": data[16:20].decode("ascii", "replace").strip(),
    }

print("== 1. ICC 头部解析 ==")
rswop = (WIN_COLOR_DIR / "RSWOP.icm").read_bytes()
japan = (WIN_COLOR_DIR / "JapanColor2001Coated.icc").read_bytes()
srgb_file = (WIN_COLOR_DIR / "sRGB Color Space Profile.icm").read_bytes()
for name, data in [("RSWOP", rswop), ("JapanColor", japan), ("sRGB", srgb_file)]:
    print(f"  {name}: {icc_header(data)}")

print("== 2. CMYK -> LAB 变换 ==")
prof_cmyk = ImageCms.getOpenProfile(io.BytesIO(rswop))
prof_lab = ImageCms.createProfile("LAB")
xform = ImageCms.buildTransform(prof_cmyk, prof_lab, "CMYK", "LAB")

# 批量正向预测：一次变换 N 个 CMYK 点（0-255 存储）
n = 5
values = [(0, 0, 0, 0), (255, 255, 255, 255), (255, 0, 0, 0), (0, 255, 0, 0), (50, 20, 80, 30)]
img = Image.new("CMYK", (n, 1))
img.putdata(values)
out = ImageCms.applyTransform(img, xform)
print("  out mode:", out.mode)
for v, lab in zip(values, list(out.getdata())):
    print(f"  CMYK{v} -> LAB{lab}")

print("== 3. RGB -> LAB 变换（sRGB 工艺路径） ==")
prof_srgb_dev = ImageCms.getOpenProfile(io.BytesIO(srgb_file))
xform2 = ImageCms.buildTransform(prof_srgb_dev, prof_lab, "RGB", "LAB")
img2 = Image.new("RGB", (3, 1))
img2.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
out2 = ImageCms.applyTransform(img2, xform2)
print("  out mode:", out2.mode, list(out2.getdata()))

print("== 4. RGB -> CMYK 整图转换（印前转换路径） ==")
xform3 = ImageCms.buildTransform(prof_srgb_dev, prof_cmyk, "RGB", "CMYK")
test_img = Image.new("RGB", (8, 8), (180, 130, 85))
out3 = ImageCms.applyTransform(test_img, xform3)
print("  out mode:", out3.mode, "px0:", out3.getpixel((0, 0)))

print("== 5. LAB 精度检查（8-bit 量化误差评估） ==")
# LAB 8-bit: L*2.55, a/b 偏移128。用灰阶扫描评估量化台阶
img4 = Image.new("CMYK", (256, 1))
img4.putdata([(k, 0, 0, 0) for k in range(256)])
out4 = ImageCms.applyTransform(img4, xform4) if False else ImageCms.applyTransform(img4, ImageCms.buildTransform(prof_cmyk, prof_lab, "CMYK", "LAB"))
lab_data = np.array(list(out4.getdata()), dtype=np.float64)
lab_l = lab_data[:, 0] / 2.55
lab_a = lab_data[:, 1] - 128.0
lab_b = lab_data[:, 2] - 128.0
print(f"  L 范围 [{lab_l.min():.1f}, {lab_l.max():.1f}]  a 范围 [{lab_a.min():.1f}, {lab_a.max():.1f}]  b 范围 [{lab_b.min():.1f}, {lab_b.max():.1f}]")

print("== 6. profile 描述元数据 ==")
for name, prof in [("RSWOP", prof_cmyk), ("sRGB", prof_srgb_dev)]:
    print(f"  {name} description: {ImageCms.getProfileDescription(prof)!r}")
    print(f"  {name} info: {ImageCms.getProfileInfo(prof)!r:.200}")

print("PROBE DONE")
