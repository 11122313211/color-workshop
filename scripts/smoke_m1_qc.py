"""M1 后端冒烟：导入→分级→会话统计（隔离数据目录，TestClient 进程内）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["COLORFORGE_DATA_DIR"] = tempfile.mkdtemp(prefix="cw_m1_smoke_")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

c = TestClient(app)

SPECTRAL = """LGOROWLENGTH\t6
INSTRUMENTATION\t"Eye-One Pro"
MEASUREMENT_SOURCE\t"WhiteBase=Absolute  Filter=No"
ILLUMINATION_NAME\t"D50"
OBSERVER_ANGLE\t"2"
KEYWORD\t"SampleID"
KEYWORD\t"SAMPLE_NAME"
NUMBER_OF_FIELDS\t38
BEGIN_DATA_FORMAT
SampleID\tSAMPLE_NAME\tnm380\tnm390\tnm400\tnm410\tnm420\tnm430\tnm440\tnm450\tnm460\tnm470\tnm480\tnm490\tnm500\tnm510\tnm520\tnm530\tnm540\tnm550\tnm560\tnm570\tnm580\tnm590\tnm600\tnm610\tnm620\tnm630\tnm640\tnm650\tnm660\tnm670\tnm680\tnm690\tnm700\tnm710\tnm720\tnm730
END_DATA_FORMAT
NUMBER_OF_SETS\t6
BEGIN_DATA
1\tA1\t0.0688\t0.1350\t0.2262\t0.3278\t0.4100\t0.4846\t0.5733\t0.6334\t0.6536\t0.6568\t0.6428\t0.6153\t0.5724\t0.5085\t0.4234\t0.3293\t0.2398\t0.1584\t0.0934\t0.0554\t0.0379\t0.0300\t0.0249\t0.0221\t0.0216\t0.0221\t0.0237\t0.0278\t0.0339\t0.0372\t0.0357\t0.0316\t0.0269\t0.0248\t0.0277\t0.0363
2\tA2\t0.1384\t0.1295\t0.1259\t0.1365\t0.1510\t0.1627\t0.1701\t0.1627\t0.1393\t0.1096\t0.0834\t0.0636\t0.0477\t0.0342\t0.0244\t0.0200\t0.0186\t0.0166\t0.0143\t0.0150\t0.0348\t0.1548\t0.3906\t0.5944\t0.7131\t0.7773\t0.8162\t0.8410\t0.8576\t0.8658\t0.8692\t0.8715\t0.8777\t0.8851\t0.8918\t0.8937
3\tA3\t0.0487\t0.0381\t0.0309\t0.0280\t0.0253\t0.0236\t0.0231\t0.0249\t0.0284\t0.0335\t0.0499\t0.1120\t0.2759\t0.5071\t0.6634\t0.7249\t0.7481\t0.7538\t0.7471\t0.7451\t0.7579\t0.7806\t0.7931\t0.8001\t0.8092\t0.8227\t0.8402\t0.8564\t0.8689\t0.8751\t0.8770\t0.8785\t0.8846\t0.8922\t0.8986\t0.9030
4\tA4\t0.0313\t0.0520\t0.0771\t0.1044\t0.1269\t0.1489\t0.1712\t0.1763\t0.1597\t0.1339\t0.1089\t0.0880\t0.0695\t0.0518\t0.0368\t0.0270\t0.0205\t0.0150\t0.0116\t0.0115\t0.0142\t0.0219\t0.0300\t0.0326\t0.0330\t0.0334\t0.0348\t0.0389\t0.0453\t0.0488\t0.0470\t0.0426\t0.0370\t0.0341\t0.0368\t0.0431
5\tA5\t0.0194\t0.0243\t0.0305\t0.0351\t0.0364\t0.0379\t0.0413\t0.0454\t0.0501\t0.0557\t0.0714\t0.1234\t0.2395\t0.3560\t0.3693\t0.3093\t0.2326\t0.1578\t0.0964\t0.0600\t0.0429\t0.0350\t0.0296\t0.0264\t0.0256\t0.0259\t0.0272\t0.0313\t0.0375\t0.0409\t0.0391\t0.0349\t0.0296\t0.0273\t0.0303\t0.0377
6\tA6\t0.4883\t0.5332\t0.5923\t0.7055\t0.8102\t0.8492\t0.8632\t0.8615\t0.8593\t0.8583\t0.8568\t0.8558\t0.8542\t0.8506\t0.8451\t0.8378\t0.8332\t0.8285\t0.8207\t0.8203\t0.8223\t0.8281\t0.8332\t0.8382\t0.8461\t0.8581\t0.8741\t0.8889\t0.9004\t0.9059\t0.9089\t0.9114\t0.9173\t0.9242\t0.9295\t0.9331
END_DATA
"""

GARBAGE = """LGOROWLENGTH\t5
INSTRUMENTATION\t"Eye-One Pro"
MEASUREMENT_SOURCE\t"Illumination=Emission  Filter=No"
NUMBER_OF_FIELDS\t11
BEGIN_DATA_FORMAT
SampleID\tSAMPLE_NAME\tRGB_R\tRGB_G\tRGB_B\tXYZ_X\tXYZ_Y\tXYZ_Z\tLAB_L\tLAB_A\tLAB_B
END_DATA_FORMAT
NUMBER_OF_SETS\t2
BEGIN_DATA
1\tA1\t0.00\t0.00\t0.00\t-22.69\t-23.75\t-26.41\t0.00\t8.58\t128.79
2\tA2\t10.00\t0.00\t0.00\t-22.57\t-23.66\t-26.30\t0.00\t9.55\t128.13
END_DATA
"""

TSV = (
    "Color Name\tCIE Lab\t\t\tCIE XYZ\t\t\tLab LCh\t\t\tStatus T C\tM\tY\tK\r\n"
    "c60\t73.69\t-0025.44\t-0035.77\t41.39\t46.23\t30.70\t73.69\t43.90\t234.58\t0.51\t0.19\t0.06\t0.34\r\n"
    "w\t99.36\t-0000.28\t1.26\t107.85\t98.36\t34.32\t99.36\t1.29\t102.36\t0.01\t0.01\t0.02\t0.01\r\n"
    "C\t47.98\t-0054.25\t-0065.89\t9.55\t16.78\t24.32\t47.98\t85.35\t230.53\t1.55\t0.43\t0.17\t0.78\r\n"
    "M\t57.38\t74.94\t22.80\t52.62\t25.32\t4.96\t57.38\t78.33\t16.92\t0.20\t1.56\t0.86\t0.60\r\n"
)

# --- 1. 单次测量（未声明复测）→ warning ---
r = c.post(
    "/api/qc/measurements/import",
    params={"filename": "spec1.txt", "instrument": "Eye-One Pro", "condition": "M1",
            "calibrated_at": "2026-07-06T09:00:00", "repeat_mode": "single"},
    content=SPECTRAL.encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
assert r.status_code == 200, r.text
body = r.json()
sid = body["session"]["id"]
print("1. 单次导入:", body["record"]["reliability"], body["record"]["reasons"][:2])

# --- 2. 同会话追加复测（同标签小扰动）→ 应转 verified ---
r2 = c.post(
    "/api/qc/measurements/import",
    params={"filename": "spec2.txt", "session_id": sid, "repeat_mode": "repeat"},
    content=SPECTRAL.replace("0.6536", "0.6530").replace("0.6568", "0.6560").encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
assert r2.status_code == 200, r2.text
b2 = r2.json()
print("2. 复测模式 stats:", b2["session"]["stats"], "reasons:", b2["record"]["reasons"][:2])

# --- 3. 垃圾 RGB 发射数据 → rejected ---
r3 = c.post(
    "/api/qc/measurements/import",
    params={"filename": "garbage.txt", "session_id": sid},
    content=GARBAGE.encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
b3 = r3.json()
print("3. 垃圾数据:", b3["record"]["reliability"], b3["record"]["reasons"][:2])

# --- 4. TSV 密度文件 → Lab 通道存疑但 density_ok ---
r4 = c.post(
    "/api/qc/measurements/import",
    params={"filename": "dens.txt", "session_id": sid},
    content=TSV.encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
b4 = r4.json()
print("4. TSV 密度:", b4["record"]["reliability"], "density_ok=", b4["record"]["density_ok"], b4["record"]["reasons"][:2])

# --- 5. 会话列表与详情 ---
r5 = c.get("/api/qc/measurements")
assert r5.status_code == 200 and len(r5.json()) == 1
r6 = c.get("/api/qc/measurements", params={"session_id": sid, "reliability": "rejected"})
assert r6.status_code == 1 or r6.status_code == 200
det = r6.json()
print("5. 详情: stats=", det["stats"], "rejected 过滤=", [x["id"] for x in det["records"]])

# --- 6. 解析失败 → 422 ---
r7 = c.post(
    "/api/qc/measurements/import",
    params={"filename": "bad.txt", "instrument": "X", "calibrated_at": "x"},
    content="随便写点什么".encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
assert r7.status_code == 422, r7.text
print("6. 解析失败 422 OK")

# --- 7. 缺校准时间 → 400 ---
r8 = c.post(
    "/api/qc/measurements/import",
    params={"filename": "x.txt", "instrument": "X"},
    content=SPECTRAL.encode("utf-8"),
    headers={"Content-Type": "application/octet-stream"},
)
assert r8.status_code == 400, r8.text
print("7. 校准确认缺失 400 OK")

# --- 8. 删除单条记录 → 重分级 ---
rid = b3["record"]["id"]
r9 = c.delete("/api/qc/measurements", params={"session_id": sid, "record_id": rid})
assert r9.status_code == 200 and r9.json()["status"] == "deleted"
print("8. 记录删除 OK, stats=", r9.json()["session"]["stats"])

print("\nM1 SMOKE ALL OK")
