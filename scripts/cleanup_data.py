"""一次性清理：删除调试残留工艺 + 孤儿 ICC 文件对账。"""
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8765")
JUNK = {"E2E-SWOP", "冒烟工艺-SWOP", "dbg", "dbg2", "哈希1", "哈希2", "测试工艺-CMYK", "色域测试"}

for p in client.get("/api/processes").json():
    if p["name"] in JUNK:
        client.delete("/api/processes/" + p["id"])
        print("deleted:", p["name"], p["id"])

print("--- 剩余工艺 ---")
for p in client.get("/api/processes").json():
    print(" ", p["id"], p["name"])

# 孤儿 ICC 文件对账（注册表之外的文件）
import os
from pathlib import Path
reg = {d["icc_file"] for d in client.get("/api/processes").json()}
folder = Path(__file__).resolve().parents[1] / "data" / "processes"
orphans = [f for f in os.listdir(folder) if f not in reg]
for f in orphans:
    (folder / f).unlink()
    print("removed orphan file:", f)
print("孤儿文件:", len(orphans))
