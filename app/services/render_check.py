"""PDF 渲染后验证防线（空白页检测 + 封装前后墨量漂移对比）。

设计动机：
    结构级预检（OutputIntent / XMP / 页面盒 / 字体嵌入）只能证明 PDF 对象结构合法，
    不能证明页面渲染后实际存在可见内容。本模块基于既有依赖 ``pypdfium2`` 将页面光栅化为
    低分辨率位图，按非白像素占比做墨量级核验：

    1. 空白页判定：单页非白像素占比低于 ``blank_threshold``（默认 0.1%）→ 标记
       ``blank_page_suspect``，显式报警但不静默通过、不删除产物；
    2. 墨量漂移对比：PDF/X-4 封装前（原始 PDF）与封装后逐页对比非白像素占比，相对偏差
       超过 ``drift_threshold``（默认 5%）→ 标记 ``ink_drift_suspect`` 并记录每页数值；
    3. 门禁分级：沿用五规则测量门禁的 ``verified / warning / rejected`` 口径：
       - ``verified``：全部抽检页有可见墨量且（对比模式下）封装前后无显著漂移；
       - ``warning``：存在空白页嫌疑（源文件本就疑似空白）或渲染后端不可用，需人工复核；
       - ``rejected``：封装前后出现显著墨量漂移、页数变化或产物无法渲染，高危拦截。

性能与大文件抽样策略：
    - 默认渲染分辨率 ``DEFAULT_RENDER_DPI = 72.0``（scale = 1.0），在保证能识别常规文字、
      线条与图像墨量的前提下避免高 DPI 内存/CPU 开销；
    - 当 PDF 页数超过 ``MAX_FULL_RENDER_PAGES``（默认 20 页）时启用确定性分层抽样：
      **首 2 页 + 尾 2 页必检**（覆盖封面、封二、封三、封底等高风险页），中间页均匀抽取
      ``max_pages - 4`` 页，结果中如实标注 ``sampled=True`` 与抽检页码列表。

边界声明：
    本模块仅做墨量级渲染验证（不做像素级视觉 diff），亦不替代 Acrobat Preflight /
    callas pdfToolbox / PitStop 等商业 PDF/X 合规认证。
"""

from __future__ import annotations

import numpy as np

#: 默认渲染分辨率（DPI）；72 DPI 对应 pypdfium2 scale=1.0
DEFAULT_RENDER_DPI = 72.0

#: 判定“非白像素”的 RGB 通道阈值：任一通道值 < 250 视为有墨（过滤抗锯齿与极轻微白底误差）
WHITE_PIXEL_THRESHOLD = 250

#: 空白页默认判定阈值：非白像素占比 < 0.1%（0.001）
DEFAULT_BLANK_PAGE_THRESHOLD = 0.001

#: 封装前后墨量相对偏差默认阈值：相对偏差 > 5%（0.05）
DEFAULT_INK_DRIFT_THRESHOLD = 0.05

#: 全量逐页渲染页数上限；超出后触发“首2 + 尾2 + 中间均匀抽样”策略
MAX_FULL_RENDER_PAGES = 20

#: 与 measurement_qa.py 保持一致的门禁严重度排序
VERDICT_ORDER = {"rejected": 0, "warning": 1, "verified": 2}


def select_page_indices(total_pages: int, max_pages: int = MAX_FULL_RENDER_PAGES) -> tuple[list[int], bool]:
    """确定性页面抽样策略：页数不超过上限时全量渲染；超出时首尾必检 + 中间均匀抽样。

    参数:
        total_pages: PDF 总页数。
        max_pages: 最大渲染页数上限（至少 4 页以保证首 2 + 尾 2）。

    返回:
        (0-based 页码列表, 是否触发抽样 sampled)
    """
    if total_pages <= 0:
        return [], False
    limit = max(4, int(max_pages))
    if total_pages <= limit:
        return list(range(total_pages)), False

    # 首 2 页（封面/封二）与尾 2 页（封三/封底）必检，中间区间均匀抽取剩余名额
    must_check = {0, 1, total_pages - 2, total_pages - 1}
    mid_quota = limit - len(must_check)
    if mid_quota > 0 and total_pages > 4:
        mid_indices = np.linspace(2, total_pages - 3, num=mid_quota)
        for idx in mid_indices:
            must_check.add(int(round(float(idx))))
    return sorted(must_check), True


def _non_white_ratio_from_array(arr: np.ndarray, white_threshold: int = WHITE_PIXEL_THRESHOLD) -> float:
    """从位图数组计算非白像素占比（0.0 ~ 1.0）。"""
    if arr.size == 0:
        return 0.0
    if arr.ndim == 3:
        # pypdfium2 默认输出 BGR/BGRA，前 3 通道对称判断任一通道 < 阈值即可
        rgb = arr[..., :3]
        non_white = np.any(rgb < white_threshold, axis=-1)
    else:
        non_white = arr < white_threshold
    return float(np.mean(non_white))


def _render_pdf_ink_ratios(
    pdf_bytes: bytes,
    *,
    page_indices: list[int] | None = None,
    dpi: float = DEFAULT_RENDER_DPI,
    white_threshold: int = WHITE_PIXEL_THRESHOLD,
    max_pages: int = MAX_FULL_RENDER_PAGES,
) -> tuple[int, list[tuple[int, float]], bool]:
    """用 pypdfium2 渲染 PDF 指定页并计算每页非白像素占比。

    返回:
        (总页数, [(1-based 页码, 非白像素占比), ...], 是否触发抽样)
    """
    import pypdfium2 as pdfium

    scale = max(float(dpi), 18.0) / 72.0
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        total_pages = len(doc)
        if page_indices is None:
            indices, sampled = select_page_indices(total_pages, max_pages=max_pages)
        else:
            indices = [i for i in page_indices if 0 <= i < total_pages]
            sampled = len(indices) < total_pages

        results: list[tuple[int, float]] = []
        for idx in indices:
            page = doc[idx]
            bitmap = None
            try:
                bitmap = page.render(scale=scale)
                arr = bitmap.to_numpy()
                ratio = _non_white_ratio_from_array(arr, white_threshold=white_threshold)
                results.append((idx + 1, ratio))
            finally:
                if bitmap is not None:
                    try:
                        bitmap.close()
                    except Exception:
                        pass
                try:
                    page.close()
                except Exception:
                    pass
        return total_pages, results, sampled
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _unavailable_result(
    *,
    dpi: float,
    blank_threshold: float,
    drift_threshold: float | None,
    reason: str,
) -> dict:
    return {
        "verdict": "warning",
        "code": "unavailable",
        "flags": ["render_unavailable"],
        "checked": False,
        "dpi": round(float(dpi), 1),
        "white_pixel_threshold": WHITE_PIXEL_THRESHOLD,
        "blank_threshold": float(blank_threshold),
        "drift_threshold": float(drift_threshold) if drift_threshold is not None else None,
        "total_pages": 0,
        "checked_pages": 0,
        "sampled": False,
        "blank_pages": [],
        "drift_pages": [],
        "max_relative_drift": None,
        "pages": [],
        "reasons": [reason],
    }


def _error_result(
    *,
    dpi: float,
    blank_threshold: float,
    drift_threshold: float | None,
    reason: str,
) -> dict:
    return {
        "verdict": "rejected",
        "code": "render_error",
        "flags": ["render_error"],
        "checked": False,
        "dpi": round(float(dpi), 1),
        "white_pixel_threshold": WHITE_PIXEL_THRESHOLD,
        "blank_threshold": float(blank_threshold),
        "drift_threshold": float(drift_threshold) if drift_threshold is not None else None,
        "total_pages": 0,
        "checked_pages": 0,
        "sampled": False,
        "blank_pages": [],
        "drift_pages": [],
        "max_relative_drift": None,
        "pages": [],
        "reasons": [reason],
    }


def verify_pdf_render(
    pdf_bytes: bytes,
    *,
    dpi: float = DEFAULT_RENDER_DPI,
    blank_threshold: float = DEFAULT_BLANK_PAGE_THRESHOLD,
    max_pages: int = MAX_FULL_RENDER_PAGES,
) -> dict:
    """单文件渲染验证（用于印前预检报告）：逐页/抽样检查是否存在空白页嫌疑。"""
    try:
        total_pages, page_ratios, sampled = _render_pdf_ink_ratios(
            pdf_bytes,
            dpi=dpi,
            max_pages=max_pages,
        )
    except ImportError:
        return _unavailable_result(
            dpi=dpi,
            blank_threshold=blank_threshold,
            drift_threshold=None,
            reason="渲染验证未执行：缺少 pypdfium2 依赖，无法确认页面实际可见内容",
        )
    except Exception as exc:  # noqa: BLE001 —— 渲染异常不得静默通过
        return _error_result(
            dpi=dpi,
            blank_threshold=blank_threshold,
            drift_threshold=None,
            reason=f"渲染验证失败（render_error）：PDF 无法通过 pypdfium2 正常光栅化（{exc}）",
        )

    blank_pages: list[int] = []
    pages_out: list[dict] = []
    reasons: list[str] = []

    for page_no, ratio in page_ratios:
        is_blank = ratio < blank_threshold
        if is_blank:
            blank_pages.append(page_no)
        pages_out.append({
            "page": page_no,
            "ink_ratio": round(ratio, 6),
            "ink_pct": round(ratio * 100.0, 4),
            "blank_suspect": is_blank,
            "status": "warning" if is_blank else "verified",
        })

    flags: list[str] = []
    verdict = "verified"
    code = "ok"

    if total_pages == 0:
        verdict = "rejected"
        code = "render_error"
        flags.append("render_error")
        reasons.append("渲染验证失败（render_error）：PDF 无可渲染页面")
    elif blank_pages:
        verdict = "warning"
        code = "blank_page_suspect"
        flags.append("blank_page_suspect")
        shown = "、".join(str(p) for p in blank_pages[:8]) + (" 等" if len(blank_pages) > 8 else "")
        min_pct = min(p["ink_pct"] for p in pages_out if p["blank_suspect"])
        reasons.append(
            f"渲染验证报警（blank_page_suspect）：第 {shown} 页非白像素占比低于阈值"
            f"（最低 {min_pct:.3f}% < {blank_threshold * 100.0:.2f}%），疑似空白页，请人工复核"
        )

    return {
        "verdict": verdict,
        "code": code,
        "flags": flags,
        "checked": True,
        "dpi": round(float(dpi), 1),
        "white_pixel_threshold": WHITE_PIXEL_THRESHOLD,
        "blank_threshold": float(blank_threshold),
        "drift_threshold": None,
        "total_pages": total_pages,
        "checked_pages": len(pages_out),
        "sampled": sampled,
        "blank_pages": blank_pages,
        "drift_pages": [],
        "max_relative_drift": None,
        "pages": pages_out,
        "reasons": reasons,
    }


def _relative_ink_drift(before: float, after: float, blank_threshold: float) -> float:
    """计算封装前后单页墨量相对偏差。

    - 若封装前非白像素占比 >= blank_threshold：按 ``|after - before| / before`` 计算；
    - 若封装前本就低于空白阈值，但封装后突增至 >= blank_threshold：按
      ``|after - before| / max(before, blank_threshold)`` 计算（必然超过 5% 报警）；
    - 若封装前后均低于空白阈值（例如人工空白页原样封装）：视为无漂移（0.0），由空白页规则报警。
    """
    if before >= blank_threshold:
        return abs(after - before) / before
    if after >= blank_threshold:
        denom = max(before, blank_threshold, 1e-9)
        return abs(after - before) / denom
    return 0.0


def compare_pdf_render(
    before_pdf_bytes: bytes,
    after_pdf_bytes: bytes,
    *,
    dpi: float = DEFAULT_RENDER_DPI,
    blank_threshold: float = DEFAULT_BLANK_PAGE_THRESHOLD,
    drift_threshold: float = DEFAULT_INK_DRIFT_THRESHOLD,
    max_pages: int = MAX_FULL_RENDER_PAGES,
) -> dict:
    """PDF/X-4 封装前后渲染对比验证：同时检查封装产物空白页与封装前后墨量漂移。

    注意：即使触发 ``blank_page_suspect`` 或 ``ink_drift_suspect``，调用方也不应删除
    封装产物，而是保留文件并向 API/门禁显式报警。
    """
    try:
        before_total, before_ratios, sampled = _render_pdf_ink_ratios(
            before_pdf_bytes,
            dpi=dpi,
            max_pages=max_pages,
        )
    except ImportError:
        return _unavailable_result(
            dpi=dpi,
            blank_threshold=blank_threshold,
            drift_threshold=drift_threshold,
            reason="渲染验证未执行：缺少 pypdfium2 依赖，无法验证封装后页面实际内容",
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            dpi=dpi,
            blank_threshold=blank_threshold,
            drift_threshold=drift_threshold,
            reason=f"渲染验证失败（render_error）：原始 PDF 渲染异常（{exc}）",
        )

    checked_zero_based = [page_no - 1 for page_no, _ in before_ratios]
    try:
        after_total, after_ratios, _ = _render_pdf_ink_ratios(
            after_pdf_bytes,
            page_indices=checked_zero_based,
            dpi=dpi,
            max_pages=max_pages,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            dpi=dpi,
            blank_threshold=blank_threshold,
            drift_threshold=drift_threshold,
            reason=f"渲染验证失败（render_error）：封装产物无法被 pypdfium2 正常渲染（{exc}）",
        )

    after_map = dict(after_ratios)
    blank_pages: list[int] = []
    drift_pages: list[int] = []
    pages_out: list[dict] = []
    reasons: list[str] = []
    max_rel_drift = 0.0

    for page_no, before_ratio in before_ratios:
        after_ratio = after_map.get(page_no)
        if after_ratio is None:
            # 封装后缺少对应页面，按 0 墨量 + 100% 漂移处理
            after_ratio = 0.0
            rel_drift = 1.0
            is_blank = True
            is_drift = True
        else:
            is_blank = after_ratio < blank_threshold
            rel_drift = _relative_ink_drift(before_ratio, after_ratio, blank_threshold)
            is_drift = rel_drift > drift_threshold

        if rel_drift > max_rel_drift:
            max_rel_drift = rel_drift
        if is_blank:
            blank_pages.append(page_no)
        if is_drift:
            drift_pages.append(page_no)

        page_status = "rejected" if is_drift else ("warning" if is_blank else "verified")
        pages_out.append({
            "page": page_no,
            "before_ink_ratio": round(before_ratio, 6),
            "after_ink_ratio": round(after_ratio, 6),
            "before_ink_pct": round(before_ratio * 100.0, 4),
            "after_ink_pct": round(after_ratio * 100.0, 4),
            "relative_drift": round(rel_drift, 6),
            "relative_drift_pct": round(rel_drift * 100.0, 2),
            "blank_suspect": is_blank,
            "ink_drift_suspect": is_drift,
            "status": page_status,
        })

    flags: list[str] = []
    verdict = "verified"

    page_count_mismatch = before_total != after_total
    if page_count_mismatch:
        verdict = "rejected"
        flags.append("ink_drift_suspect")
        reasons.append(
            f"渲染验证拒绝（ink_drift_suspect）：封装前后页数不一致"
            f"（封装前 {before_total} 页 → 封装后 {after_total} 页）"
        )

    if drift_pages:
        verdict = "rejected"
        if "ink_drift_suspect" not in flags:
            flags.append("ink_drift_suspect")
        worst = max((p for p in pages_out if p["ink_drift_suspect"]), key=lambda p: p["relative_drift"])
        shown = "、".join(str(p) for p in drift_pages[:8]) + (" 等" if len(drift_pages) > 8 else "")
        reasons.append(
            f"渲染验证拒绝（ink_drift_suspect）：第 {shown} 页封装前后非白像素占比发生显著漂移"
            f"（最差第 {worst['page']} 页：{worst['before_ink_pct']:.3f}% → {worst['after_ink_pct']:.3f}%，"
            f"相对偏差 {worst['relative_drift_pct']:.2f}% > {drift_threshold * 100.0:.1f}%），"
            "疑似封装过程破坏页面内容，产物已保留供排查"
        )

    if blank_pages:
        if VERDICT_ORDER["warning"] < VERDICT_ORDER[verdict]:
            verdict = "warning"
        if "blank_page_suspect" not in flags:
            flags.append("blank_page_suspect")
        shown = "、".join(str(p) for p in blank_pages[:8]) + (" 等" if len(blank_pages) > 8 else "")
        min_pct = min(p["after_ink_pct"] for p in pages_out if p["blank_suspect"])
        reasons.append(
            f"渲染验证报警（blank_page_suspect）：第 {shown} 页非白像素占比低于阈值"
            f"（最低 {min_pct:.3f}% < {blank_threshold * 100.0:.2f}%），"
            "疑似空白页，已保留产物，请人工复核确认后再放行"
        )

    # 主状态码：若出现封装导致的内容漂移优先报 ink_drift_suspect，否则若有空白页报 blank_page_suspect
    if "ink_drift_suspect" in flags:
        code = "ink_drift_suspect"
    elif "blank_page_suspect" in flags:
        code = "blank_page_suspect"
    else:
        code = "ok"

    return {
        "verdict": verdict,
        "code": code,
        "flags": flags,
        "checked": True,
        "dpi": round(float(dpi), 1),
        "white_pixel_threshold": WHITE_PIXEL_THRESHOLD,
        "blank_threshold": float(blank_threshold),
        "drift_threshold": float(drift_threshold),
        "total_pages": after_total,
        "checked_pages": len(pages_out),
        "sampled": sampled,
        "blank_pages": blank_pages,
        "drift_pages": drift_pages,
        "max_relative_drift": round(max_rel_drift, 6),
        "pages": pages_out,
        "reasons": reasons,
    }
