"""pytest 全局配置：数据目录隔离 + ICC 测试资产 fixtures。

- COLORFORGE_DATA_DIR 在任何 app 模块导入之前注入，使全部 store 落盘到临时目录，
  测试不影响 data/ 真实数据；
- CMYK / RGB ICC：tests/fixtures/icc/ 下的 littlecms testbed 资产（MIT 许可，
  跨平台可用，摆脱对 C:\\Windows 系统 profile 的依赖）；
- GRAY ICC：运行时按 ICC 规范手工构造最小合法灰度 profile（约 238B）。
"""

from __future__ import annotations

import atexit
import os
import shutil
import struct
import tempfile
import uuid
from pathlib import Path

import pytest

# 必须先于任何 app.* 导入设定数据目录（conftest 先于测试模块加载）
# 注：Windows 沙箱下 tempfile.mkdtemp 会创建 ACL 受限目录，后续无法 mkdir；
# 使用普通目录创建，仍位于系统临时目录，并保留 session 隔离，退出时自动清理。
_TEST_DATA_DIR = Path(tempfile.gettempdir()) / f"colorforge-test-data-{uuid.uuid4().hex[:8]}"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["COLORFORGE_DATA_DIR"] = str(_TEST_DATA_DIR)
atexit.register(lambda p=_TEST_DATA_DIR: shutil.rmtree(p, ignore_errors=True))

FIXTURE_ICC_DIR = Path(__file__).resolve().parent / "fixtures" / "icc"


def make_gray_icc(gamma: float = 2.2) -> bytes:
    """手工构造最小合法 GRAY ICC（mntr/GRAY/XYZ，gamma 曲线）。

    设备值语义为「亮度」：0=黑，255=白（与显示器灰度 profile 一致），
    仅用于验证引擎的 1 通道泛化，不代表印刷黑墨方向。
    """
    hdr = bytearray(128)
    struct.pack_into(">I", hdr, 8, 0x04300000)  # 版本 4.3
    hdr[12:16] = b"mntr"
    hdr[16:20] = b"GRAY"
    hdr[20:24] = b"XYZ "
    hdr[24:36] = struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
    hdr[36:40] = b"acsp"
    struct.pack_into(">i", hdr, 60, 0x0000F6D6)  # D50 白点 XYZ
    struct.pack_into(">i", hdr, 64, 0x00010000)
    struct.pack_into(">i", hdr, 68, 0x0000D32D)

    curve = b"curv" + struct.pack(">II", 0, 1) + struct.pack(">H", int(gamma * 256)) + b"\x00\x00"
    xyz = b"XYZ " + struct.pack(">I", 0) + struct.pack(">iii", 0x0000F6D6, 0x00010000, 0x0000D32D)
    text = b"sRGB Gray\x00"
    mluc = b"mluc" + struct.pack(">II", 0, 1) + struct.pack(">HHII", 0x656E, 0x5553, len(text), 24) + text

    body = struct.pack(">I", 3)
    offset = 128 + len(body) + 12 * 3
    entries = b""
    data = b""
    for sig, d in ((b"desc", mluc), (b"wtpt", xyz), (b"kTRC", curve)):
        entries += sig + struct.pack(">II", offset, len(d))
        data += d
        offset += len(d)
    blob = bytes(hdr) + body + entries + data
    return struct.pack(">I", len(blob)) + blob[4:]


@pytest.fixture(scope="session")
def cmyk_icc() -> bytes:
    """CMYK 输出工艺 ICC（littlecms testbed，MIT）。"""
    p = FIXTURE_ICC_DIR / "testbed_cmyk.icc"
    if not p.exists():
        pytest.skip("缺少 CMYK 测试 profile（tests/fixtures/icc/testbed_cmyk.icc）")
    return p.read_bytes()


@pytest.fixture(scope="session")
def rgb_icc() -> bytes:
    """RGB 显示工艺 ICC（littlecms testbed，MIT）。"""
    p = FIXTURE_ICC_DIR / "ibm-t61.icc"
    if not p.exists():
        pytest.skip("缺少 RGB 测试 profile（tests/fixtures/icc/ibm-t61.icc）")
    return p.read_bytes()


@pytest.fixture(scope="session")
def gray_icc() -> bytes:
    """运行时构造的最小 GRAY ICC。"""
    return make_gray_icc()


@pytest.fixture(scope="session")
def api_client():
    """FastAPI TestClient（数据目录已隔离到临时目录）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def process_factory(cmyk_icc):
    """经服务层快速创建工艺，测试结束自动清理。"""
    from app.services.processes import ProcessStore

    store = ProcessStore()
    created: list[str] = []

    def _factory(name: str = "测试工艺", icc: bytes | None = None, **kw) -> dict:
        doc = store.create(name, icc if icc is not None else cmyk_icc, **kw)
        created.append(doc["id"])
        return doc

    yield _factory
    for pid in created:
        store.delete(pid)
