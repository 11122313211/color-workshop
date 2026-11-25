"""路由公共 HTTP 辅助：流式请求体限流、安全 Content-Disposition、ICC 缺失转换。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request

MAX_ICC_BYTES = 20 * 1024 * 1024
MAX_MEASUREMENT_BYTES = 2_000_000
MAX_DISPLAY_MEASUREMENT_BYTES = 5_000_000
MAX_EVAL_JSON_BYTES = 256 * 1024

_CTRL_OR_QUOTE_RE = re.compile(r'[\r\n\x00-\x1f"\\;]+')


async def read_body_limited(
    request: Request,
    max_bytes: int,
    *,
    too_large_detail: str | None = None,
) -> bytes:
    """流式读取请求体并在超限时立即拒绝（413），防止超大载荷导致 OOM。"""
    detail = too_large_detail or f"请求体过大（>{max_bytes // 1024 // 1024 or 1}MB）"
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(413, detail)
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, detail)
        chunks.append(chunk)
    return b"".join(chunks)


def content_disposition(filename: str, fallback: str = "download.bin") -> str:
    """构造安全的 Content-Disposition 头（ASCII fallback + RFC 5987 UTF-8 编码）。"""
    base = Path(str(filename or "")).name
    clean = _CTRL_OR_QUOTE_RE.sub("_", base).strip(" .") or fallback
    ascii_name = clean.encode("ascii", "ignore").decode("ascii").strip(" .")
    if not ascii_name:
        suffix = Path(clean).suffix.encode("ascii", "ignore").decode("ascii")
        ascii_name = f"{Path(fallback).stem}{suffix or Path(fallback).suffix}"
    encoded = quote(clean, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def key_error_msg(exc: KeyError, fallback: str = "资源不存在") -> str:
    """提取 KeyError 原始消息，避免 str(KeyError('...')) 携带多余单引号。"""
    if exc.args and isinstance(exc.args[0], str) and exc.args[0].strip():
        return exc.args[0]
    return fallback


def read_icc_or_409(process_store, pid: str) -> bytes:
    """读取工艺 ICC 字节；若文件缺失/不可读返回结构化 409 而非 500。"""
    try:
        return process_store.icc_bytes(pid)
    except KeyError as exc:
        raise HTTPException(404, "工艺不存在") from exc
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(
            409,
            detail={
                "code": "MISSING_ICC",
                "message": "工艺对应的 ICC 文件缺失或不可读，请重新上传 ICC 或执行完整性检查",
            },
        ) from exc
