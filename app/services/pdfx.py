"""PDF/X-4 结构封装（V0.9 · P0-2）。

在客户 PDF 已是印刷色彩空间的前提下，写入 PDF/X-4 交付所需的**结构要素**：

1. ``/OutputIntents``：目标工艺 ICC（``/S /GTS_PDFX``，含 ``/DestOutputProfile``）；
2. XMP 元数据：``pdfx:GTS_PDFXVersion = PDF/X-4``；
3. 页面盒：缺失时补 ``TrimBox``（成品线）与 ``BleedBox``（出血边界）。

**合规边界（口径诚实）**：本模块完成结构级封装并执行硬门禁（字体嵌入、RGB 图像、
加密/损坏），但**不做 RIP 端全量 PDF/X 校验**（字体子集完整性、色彩空间封闭性、
透明度拼合等）。交付前仍需 Acrobat Preflight / callas pdfToolbox 复核。
"""

from __future__ import annotations

import base64
import hashlib
import io
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from .files import preflight
from .render_check import (
    DEFAULT_BLANK_PAGE_THRESHOLD,
    DEFAULT_INK_DRIFT_THRESHOLD,
    DEFAULT_RENDER_DPI,
    MAX_FULL_RENDER_PAGES,
    compare_pdf_render,
)

PDFX_VERSION = "PDF/X-4"

#: 上传上限（字节）
MAX_PDF_BYTES = 40 * 1024 * 1024

_CHANNEL_BY_SPACE = {"CMYK": 4, "RGB": 3, "GRAY": 1}
_DEVICE_BY_CHANNEL = {4: "/DeviceCMYK", 3: "/DeviceRGB", 1: "/DeviceGray"}


class PdfxError(ValueError):
    """封装失败（阻断项在 blockers 中逐条给出）。"""

    def __init__(self, message: str, blockers: list[str] | None = None) -> None:
        super().__init__(message)
        self.blockers = blockers or []


def _xmp_packet(*, title: str, producer: str) -> bytes:
    """最小 PDF/X-4 XMP 包（GTS_PDFXVersion + 文档元数据）。"""
    return f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="色彩工坊">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/">
   <pdfx:GTS_PDFXVersion>{escape(PDFX_VERSION)}</pdfx:GTS_PDFXVersion>
   <pdfx:GTS_PDFXConformance>{escape(PDFX_VERSION)}</pdfx:GTS_PDFXConformance>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
   <pdf:Producer>{escape(producer)}</pdf:Producer>
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{escape(title)}</rdf:li></rdf:Alt></dc:title>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""".encode("utf-8")


def _output_condition(icc_meta: dict, process_name: str) -> tuple[str, str]:
    """输出条件标识与说明（优先用 ICC 描述；无法取得时用 Custom）。"""
    description = str(icc_meta.get("description") or "").strip()
    identifier = description or "Custom"
    info = description or f"{process_name} 输出条件"
    return identifier[:120], info[:300]


def _box_copy(page, key: str) -> ArrayObject | None:
    box = page.get(key)
    if box is None:
        return None
    values = [float(v) for v in box]
    if len(values) != 4:
        return None
    return ArrayObject([FloatObject(v) for v in values])


def _out_name(filename: str) -> str:
    from pathlib import Path

    stem = Path((filename or "document.pdf").strip()).name or "document.pdf"
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return f"{stem}-PDFX4.pdf"


def _serialize_pdfx(writer: PdfWriter) -> bytes:
    """将 PdfWriter 序列化为 PDF 字节（独立函数便于渲染回归对照测试注入异常产物）。"""
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def package_pdfx(
    pdf_bytes: bytes,
    icc_bytes: bytes,
    icc_meta: dict,
    *,
    process_name: str = "",
    filename: str = "",
    strict: bool = True,
    allow_rgb: bool = False,
    blank_threshold: float = DEFAULT_BLANK_PAGE_THRESHOLD,
    drift_threshold: float = DEFAULT_INK_DRIFT_THRESHOLD,
    render_dpi: float = DEFAULT_RENDER_DPI,
    max_render_pages: int = MAX_FULL_RENDER_PAGES,
) -> dict:
    """把 PDF 封装为 PDF/X-4 结构文件，并在封装后执行渲染验证防线。

    strict=True 时，字体未嵌入 / 未标定 RGB 图像等硬阻断项直接抛 ``PdfxError``；
    strict=False 时仍生成文件，但在 checks/status 中如实标注未达标项。
    封装完成后用 pypdfium2 对比封装前后页面墨量覆盖，检出空白页嫌疑或墨量漂移时
    保留产物不删除，并在 render_check / render_verification / checks 中显式报警。
    """
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PdfxError("仅支持 PDF 文件（PNG/JPEG/TIFF 请先组版或先做图像转换）")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise PdfxError("PDF 过大（>40MB），请拆分后再封装")

    try:
        # 封装后将对原始 PDF 与产物一并做渲染对比，此处关闭单文件渲染检查避免重复光栅化
        report = preflight(
            pdf_bytes,
            filename,
            render_thumbnail=False,
            run_render_check=False,
        )
    except Exception as e:  # noqa: BLE001 —— 解析失败统一转封装错误
        raise PdfxError(f"PDF 预检失败：{e}") from e
    if report.get("format") != "PDF" or report.get("status") == "error":
        raise PdfxError("PDF 已加密或无法解析，不能封装 PDF/X")

    pdf = report.get("pdf") or {}
    checks: list[dict] = []
    blockers: list[str] = []

    fonts = pdf.get("font_embedding") or {}
    missing = fonts.get("not_embedded") or []
    if missing:
        names = "、".join(sorted({f.get("name", "") for f in missing})[:4])
        blockers.append(f"{len(missing)} 个字体未嵌入（{names}）；PDF/X 要求字体全部嵌入")

    channels = _CHANNEL_BY_SPACE.get(str((icc_meta or {}).get("device_space", "")).upper(), 4)

    rgb_images = [
        im for im in (pdf.get("images") or [])
        if str(im.get("colorspace", "")).startswith(("DeviceRGB", "ICCBased(N=3"))
    ]
    if rgb_images and channels == 4 and not allow_rgb:
        blockers.append(
            f"{len(rgb_images)} 个图像为 RGB；CMYK 输出意图下需先转为 ICCBased CMYK"
            "（可用「印前转换」处理图像后重新组版）"
        )

    if pdf.get("has_output_intent"):
        checks.append({"level": "info", "message": "原有输出意图已替换为目标工艺 ICC"})
    if pdf.get("spot_colors"):
        checks.append({
            "level": "info",
            "message": "含专色（PDF/X-4 允许 Separation/DeviceN，RIP 端需有对应墨路）",
        })
    if pdf.get("transparency"):
        checks.append({
            "level": "info",
            "message": "含透明对象（PDF/X-4 允许活透明，无需预先拼合）",
        })

    if blockers:
        if strict:
            raise PdfxError("；".join(blockers), blockers)
        checks.extend({"level": "warn", "message": b} for b in blockers)

    if channels != 4:
        checks.append({
            "level": "warn",
            "message": f"输出意图为 {channels} 通道工艺；印刷交付通常使用 CMYK 输出意图",
        })

    # ---- 写 PDF/X-4 结构 ----
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter(clone_from=reader)
    except Exception as e:  # noqa: BLE001
        raise PdfxError(f"PDF 重写失败：{e}") from e

    pages = list(writer.pages)
    if not pages:
        raise PdfxError("PDF 无页面，无法封装")

    derived_trim = 0
    derived_bleed = 0
    for page in pages:
        if "/TrimBox" not in page:
            box = _box_copy(page, "/CropBox") or _box_copy(page, "/MediaBox")
            if box is None:
                raise PdfxError("页面缺少 MediaBox，无法封装")
            page[NameObject("/TrimBox")] = box
            derived_trim += 1
        if "/BleedBox" not in page:
            media = _box_copy(page, "/MediaBox")
            if media is None:
                raise PdfxError("页面缺少 MediaBox，无法封装")
            page[NameObject("/BleedBox")] = media
            derived_bleed += 1
    if derived_trim:
        checks.append({
            "level": "warn",
            "message": f"{derived_trim} 页缺少 TrimBox，已按 CropBox/MediaBox 补成品线；请确认成品尺寸",
        })
    if derived_bleed:
        checks.append({
            "level": "info",
            "message": f"{derived_bleed} 页缺少 BleedBox，已按 MediaBox 补出血边界；实际出血量以版面为准",
        })

    identifier, info = _output_condition(icc_meta or {}, process_name)
    profile = DecodedStreamObject()
    profile.set_data(icc_bytes)
    profile[NameObject("/N")] = NumberObject(channels)
    profile[NameObject("/Alternate")] = NameObject(_DEVICE_BY_CHANNEL.get(channels, "/DeviceCMYK"))
    output_intent = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"),
        NameObject("/S"): NameObject("/GTS_PDFX"),
        NameObject("/OutputConditionIdentifier"): TextStringObject(identifier),
        NameObject("/OutputCondition"): TextStringObject(info),
        NameObject("/Info"): TextStringObject(info),
        NameObject("/RegistryName"): TextStringObject("http://www.color.org"),
        NameObject("/DestOutputProfile"): writer._add_object(profile),
    })
    root = writer._root_object
    root[NameObject("/OutputIntents")] = ArrayObject([writer._add_object(output_intent)])

    metadata = DecodedStreamObject()
    metadata[NameObject("/Type")] = NameObject("/Metadata")
    metadata[NameObject("/Subtype")] = NameObject("/XML")
    metadata.set_data(_xmp_packet(
        title=filename or "未命名文档",
        producer=f"色彩工坊 · {PDFX_VERSION} 结构封装",
    ))
    root[NameObject("/Metadata")] = writer._add_object(metadata)
    writer.add_metadata({
        "/Title": filename or "未命名文档",
        "/Producer": f"色彩工坊 · {PDFX_VERSION}",
        "/Creator": "色彩工坊",
    })

    output = _serialize_pdfx(writer)

    # ---- 渲染后验证防线（封装前 vs 封装后非白像素占比 + 空白页检测）----
    render_verification = compare_pdf_render(
        pdf_bytes,
        output,
        dpi=render_dpi,
        blank_threshold=blank_threshold,
        drift_threshold=drift_threshold,
        max_pages=max_render_pages,
    )
    if render_verification.get("sampled"):
        checks.append({
            "level": "info",
            "message": (
                f"页数较多（共 {render_verification['total_pages']} 页），"
                f"渲染验证按首尾必检+均匀抽样核验 {render_verification['checked_pages']} 页"
            ),
        })
    for reason in render_verification.get("reasons") or []:
        is_reject_reason = (
            render_verification.get("verdict") == "rejected"
            and ("ink_drift_suspect" in reason or "render_error" in reason)
        )
        checks.append({
            "level": "error" if is_reject_reason else "warn",
            "message": reason,
        })

    render_verdict = render_verification.get("verdict", "verified")
    if render_verdict == "rejected":
        status = "error"
        compliant = False
    elif blockers or derived_trim or render_verdict == "warning":
        status = "warn"
        compliant = not blockers
    else:
        status = "pass"
        compliant = True

    return {
        "file_b64": "data:application/pdf;base64," + base64.b64encode(output).decode("ascii"),
        "filename": _out_name(filename),
        "mode": PDFX_VERSION,
        "status": status,
        "compliant": compliant,
        "render_check": render_verification["code"],
        "render_verification": render_verification,
        "checks": checks,
        "output_intent": {
            "identifier": identifier,
            "info": info,
            "channels": channels,
            "device_space": (icc_meta or {}).get("device_space", ""),
            "icc_sha256": hashlib.sha256(icc_bytes).hexdigest()[:16],
        },
        "pages": len(pages),
        "notes": [
            "结构级封装：已写入 OutputIntent、XMP GTS_PDFXVersion 与 TrimBox/BleedBox。",
            "已执行渲染后墨量验证（非白像素覆盖率与封装前后漂移核验）；仅作内容防丢失防线，不替代 Acrobat Preflight / callas pdfToolbox 的商业 PDF/X 合规认证。",
        ],
    }