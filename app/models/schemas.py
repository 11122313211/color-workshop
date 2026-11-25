"""Pydantic 数据模型：定义各模块的请求/响应结构。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel as _PydanticBaseModel, ConfigDict, Field


class BaseModel(_PydanticBaseModel):
    """全局严格模型基类：禁止非标准 JSON 浮点 NaN/Infinity 进入请求与响应。"""

    model_config = ConfigDict(allow_inf_nan=False)


# Base64 请求体最大字符数（约对应 45MB 原始文件）
MAX_B64_CHARS = 60_000_000


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

class RenderingIntent(str, Enum):
    perceptual = "perceptual"
    saturation = "saturation"
    relative_colorimetric = "relative_colorimetric"
    absolute_colorimetric = "absolute_colorimetric"


class DeltaEMethod(str, Enum):
    cie1976 = "cie1976"
    cie1994 = "cie1994"
    cie2000 = "cie2000"
    cmc = "cmc"


# ---------------------------------------------------------------------------
# 设备管理
# ---------------------------------------------------------------------------

class DeviceCreate(BaseModel):
    name: str
    model: str = ""
    space: str = "srgb"
    description: str = ""


class DeviceOut(BaseModel):
    id: str
    name: str
    model: str
    space: str
    description: str
    calibrated: bool = False
    profile_created: bool = False


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    space: Optional[str] = None
    description: Optional[str] = None
    calibrated: Optional[bool] = None


# ---------------------------------------------------------------------------
# 测量与采集
# ---------------------------------------------------------------------------

class Measurement(BaseModel):
    """单次测量记录。"""
    device_id: str
    rgb: tuple[float, float, float] = Field(..., description="测量得到的 RGB 值")
    space: str = "srgb"
    conditions_ok: bool = True
    note: str = ""


class MeasurementOut(BaseModel):
    id: str
    device_id: str
    rgb: list[float]
    space: str
    lab: list[float]
    conditions_ok: bool
    note: str


# ---------------------------------------------------------------------------
# 色卡识别与标定
# ---------------------------------------------------------------------------

class ColorCheckerDetectRequest(BaseModel):
    image_bytes: str = Field(..., max_length=MAX_B64_CHARS, description="base64 编码的图像")
    expected_patches: int = Field(24, ge=1, le=256)
    space: str = "srgb"


class ColorCheckerPatch(BaseModel):
    index: int
    x: float
    y: float
    width: float
    height: float
    rgb: list[float]
    lab: list[float]


class ColorCheckerDetectResult(BaseModel):
    detected: bool
    patches_found: int
    patches: list[ColorCheckerPatch]
    profile_ready: bool
    quality: float = Field(..., description="识别质量评分 0-100")


# ---------------------------------------------------------------------------
# 色彩转换
# ---------------------------------------------------------------------------

class ConvertRequest(BaseModel):
    rgb: tuple[float, float, float]
    from_space: str
    to_space: str
    intent: RenderingIntent = RenderingIntent.relative_colorimetric


class ConvertResponse(BaseModel):
    result: list[float]
    from_space: str
    to_space: str


# ---------------------------------------------------------------------------
# 智能校正 / Profile 生成
# ---------------------------------------------------------------------------

class CorrectionRequest(BaseModel):
    device_id: str
    measured_rgb: tuple[float, float, float]
    target_rgb: tuple[float, float, float]
    space: str = "srgb"
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = Field(2.0, gt=0.0, le=100.0)


class CorrectionResponse(BaseModel):
    device_id: str
    correction_matrix: list[list[float]]
    delta_e_before: float
    delta_e_after: float
    converged: bool
    iterations: int
    status: str


# ---------------------------------------------------------------------------
# ΔE 检测
# ---------------------------------------------------------------------------

class DeltaERequest(BaseModel):
    rgb1: tuple[float, float, float]
    rgb2: tuple[float, float, float]
    space: str = "srgb"
    method: DeltaEMethod = DeltaEMethod.cie2000


class DeltaEResponse(BaseModel):
    delta_e: float
    method: str
    lab1: list[float]
    lab2: list[float]
    passed: bool
    tolerance: float = 2.0


class LabDeltaERequest(BaseModel):
    lab1: tuple[float, float, float] = Field(..., description="CIELAB [L, a, b]")
    lab2: tuple[float, float, float] = Field(..., description="CIELAB [L, a, b]")
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = Field(2.0, gt=0.0, le=100.0)


# ---------------------------------------------------------------------------
# 闭环验证与迭代
# ---------------------------------------------------------------------------

class ClosedLoopRequest(BaseModel):
    device_id: str
    target_rgb: tuple[float, float, float]
    initial_measured_rgb: tuple[float, float, float]
    space: str = "srgb"
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = Field(2.0, gt=0.0, le=100.0)
    max_iterations: int = Field(10, ge=1, le=50)


class LoopIteration(BaseModel):
    iteration: int
    measured_rgb: list[float]
    delta_e: float
    correction_applied: list[float]
    converged: bool


class ClosedLoopResponse(BaseModel):
    device_id: str
    target_rgb: list[float]
    converged: bool
    iterations: list[LoopIteration]
    final_delta_e: float
    iterations_used: int


# ---------------------------------------------------------------------------
# 色域可视化
# ---------------------------------------------------------------------------

class GamutInfo(BaseModel):
    space: str
    volume: float
    coverage_vs_srgb: float


class GamutCoverageRequest(BaseModel):
    target_space: str
    reference_space: str = "srgb"


class CIE1931Point(BaseModel):
    x: float
    y: float
    label: str = ""


class GamutVisualizationResponse(BaseModel):
    spaces: list[str]
    volumes: dict[str, float]
    coverages: dict[str, float]
    cie_spectrum: list[dict] = Field(..., description="CIE 1931 光谱轨迹点（wavelength/x/y/hex）")
    gamut_polygons: dict[str, list[list[float]]]


class SpectrumLocusResponse(BaseModel):
    locus: list[dict]
    purple_line: list[list[float]]


# ---------------------------------------------------------------------------
# 工作流
# ---------------------------------------------------------------------------

class WorkflowRun(BaseModel):
    id: str
    device_id: str
    type: str
    status: str
    result: dict
    created_at: str


# ---------------------------------------------------------------------------
# 工艺库（v0.2 印前配方工作流）
# ---------------------------------------------------------------------------

class ProcessOut(BaseModel):
    id: str
    name: str
    paper: str = ""
    ink: str = ""
    notes: str = ""
    #: V0.10：ISO 13655 观测条件类别；空串表示未登记（旧工艺默认值）
    measurement_condition: str = ""
    icc_size: int = 0
    icc_original_name: str = ""
    icc_meta: dict = {}
    created_at: str = ""
    #: V0.10：最近护照的轻量摘要（由路由按需注入，旧数据可为空）
    passport_summary: Optional[dict] = None
    #: V0.11：归档生命周期；空 archived_at 表示 active（旧数据默认 active）
    archived_at: Optional[str] = None
    archive_reason: Optional[str] = None
    archived_by: Optional[str] = None
    is_archived: bool = False


class ProcessArchiveRequest(BaseModel):
    """归档工艺请求体。"""

    reason: str = Field("manual_archive", max_length=120)
    actor: str = Field("", max_length=80)


class RecipeTarget(BaseModel):
    label: str = ""
    lab: tuple[float, float, float] = Field(..., description="目标 CIELAB [L, a, b]")


class RecipePredictRequest(BaseModel):
    process_id: str
    targets: list[RecipeTarget] = Field(..., max_length=50, description="目标色列表（单次最多 50 个）")
    tolerance: float = Field(2.0, gt=0.0, le=100.0)
    method: DeltaEMethod = DeltaEMethod.cie2000
    max_rounds: int = Field(6, ge=1, le=20)


class RecipeResult(BaseModel):
    label: str = ""
    target_lab: list[float]
    predicted_lab: list[float]
    recipe: dict[str, float] = Field(..., description="配方：通道名 -> 百分比")
    #: V0.10：这里的 recipe 明确为设备分色值，不是基础油墨调墨配方
    recipe_kind: str = "device_values"
    recipe_meta: dict = Field(
        default_factory=lambda: {
            "unit": "percent",
            "basis": "device_channels",
            "note": "分色值（设备值），不是基础油墨调墨配方",
        },
        description="配方语义：单位/基准/说明",
    )
    #: V0.10：本次预测落库后的记录 id（用于导出配方单）
    recipe_id: str = ""
    delta_e: float
    method: str
    tolerance: float
    converged: bool
    iterations: list[float]
    predictions: int
    lut_points: int
    refine_predictions: int = 0
    compensation: dict | None = Field(None, description="{n, applied} 工艺补偿模型应用状态")


class RecipePredictResponse(BaseModel):
    process_id: str
    process_name: str
    device_space: str
    results: list[RecipeResult]
    total_predictions: int
    elapsed_ms: int


class RecipeVerifyRequest(BaseModel):
    recipe_id: str
    measured_lab: tuple[float, float, float]
    #: 关联测量会话（V0.5 补偿门禁）：提供时仅当会话含 verified 记录才沉淀补偿
    measurement_session_id: str = ""


class RecipeVerifyResponse(BaseModel):
    recipe_id: str
    delta_e_measured: float
    delta_e_model_error: float
    delta_e_corrected: float
    corrected_recipe: dict[str, float]
    corrected_predicted_lab: list[float]
    improved: bool
    verification: dict
    #: V0.5：ΔE 分量分解（dL/da/db/dC/dh/dE76，实测−预测）
    components: Optional[dict] = None
    #: V0.5：补偿门禁结果 {applied, n, skipped_reason}
    compensation_status: Optional[dict] = None


class PreflightRequest(BaseModel):
    image_b64: str = Field(..., max_length=MAX_B64_CHARS)
    filename: str = ""


class PreflightIssue(BaseModel):
    level: str = Field(..., description="info / warn / error")
    message: str


class PreflightReport(BaseModel):
    filename: str = ""
    format: Optional[str] = None
    mode: str = ""
    size: list[int] = []
    dpi: Optional[float] = None
    has_embedded_icc: bool = False
    embedded_icc: dict = {}
    dominant_colors: list[str] = []
    issues: list[PreflightIssue] = []
    status: str = "pass"
    # V0.4 PDF 预检扩展（图像预检时均为缺省）
    pages: Optional[int] = None
    pdf: Optional[dict] = None
    thumbnail_b64: Optional[str] = None


class FileConvertRequest(BaseModel):
    image_b64: str = Field(..., max_length=MAX_B64_CHARS)
    process_id: str
    intent: str = "relative_colorimetric"
    display_id: str = ""


class FileConvertResponse(BaseModel):
    preview_b64: str
    file_b64: str
    file_format: str
    output_mode: str
    source_profile: str = ""
    softproof: dict = {}


# V0.9 P0-2 · PDF/X-4 结构封装
class FilePdfxResponse(BaseModel):
    file_b64: str
    filename: str
    mode: str
    status: str = "pass"
    compliant: bool = True
    checks: list[PreflightIssue] = []
    output_intent: dict = {}
    pages: int = 0
    notes: list[str] = []


# V0.9 P0-4 · 显示器档案与软打样门禁
class DisplayTargets(BaseModel):
    white_point: str = "D50"
    luminance_cd_m2: float = 120.0
    contrast_ratio: float = 1000.0
    gamut_coverage: dict = {}


class DisplayCalibrationRequest(BaseModel):
    calibrated_at: str = ""
    valid_months: int = 6
    calibrator: str = ""
    software: str = ""
    luminance_cd_m2: Optional[float] = None
    black_luminance_cd_m2: Optional[float] = None
    contrast_ratio: Optional[float] = None
    white_point: str = "D50"
    gamut_coverage: dict = {}
    profile_name: str = ""
    profile_sha256: str = ""
    notes: str = ""


class DisplayCreateRequest(BaseModel):
    name: str
    model: str = ""
    serial: str = ""
    notes: str = ""
    is_active: bool = True
    targets: DisplayTargets = Field(default_factory=DisplayTargets)
    calibration: Optional[DisplayCalibrationRequest] = None


class DisplayUpdateRequest(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    targets: Optional[dict] = None


# V0.9 签样工作流（P0-3）
class SignoffCreateRequest(BaseModel):
    process_id: str
    title: str = ""
    job_no: str = ""
    created_by: str = ""
    evidence: dict = {}
    #: V0.10：可选关联工艺护照；缺省时保持 V0.9 的 ICC 指纹门禁语义
    passport_id: str = ""
    passport_hash: str = ""


class SignoffActionRequest(BaseModel):
    actor: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# V0.10 工艺护照 / 光谱同色异谱
# ---------------------------------------------------------------------------

class PassportWarning(BaseModel):
    code: str
    level: str = "warning"
    message: str


class PassportSummary(BaseModel):
    passport_id: str
    version: int
    passport_hash: str
    generated_at: str = ""
    status: str = "incomplete"
    status_label: str = ""
    warnings: int = 0
    spectral_risk: Optional[str] = None


class PassportOut(BaseModel):
    id: str
    process_id: str
    version: int
    passport_hash: str
    generated_at: str
    created: bool = False
    process: dict = {}
    evidence: dict = {}
    warnings: list[PassportWarning] = []
    status: str = "incomplete"
    status_label: str = ""
    status_reasons: list[PassportWarning] = []


class PassportExport(BaseModel):
    passport_id: str
    version: int
    format: str
    filename: str
    content: str


class SpectralMetamerismRequest(BaseModel):
    spectral_a: dict[str, float] = Field(..., description="样本 A：波长(nm) -> 反射率")
    spectral_b: dict[str, float] = Field(..., description="样本 B：波长(nm) -> 反射率")
    label_a: str = ""
    label_b: str = ""
    conditions: list[str] = Field(
        default_factory=lambda: ["D50", "D65", "A", "FL11"],
        description="D50/D65/A/FL11（CIE F11），均为 2° 观察者",
    )


class SpectralConditionOut(BaseModel):
    condition: str
    label: str
    illuminant: str
    observer: str = "2"
    lab_a: list[float]
    lab_b: list[float]
    delta_e: float
    risk: str


class SpectralMetamerismResponse(BaseModel):
    label_a: str = ""
    label_b: str = ""
    conditions: list[SpectralConditionOut]
    max_delta_e: float
    worst_condition: str
    risk: str
    risk_label: str
    warnings: list[str] = []
    disclaimer: str = "工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI"


class SpectralCompareRequest(BaseModel):
    session_a: str
    session_b: str
    top_n: int = Field(12, ge=1, le=50)


class SpectralCompareResponse(BaseModel):
    session_a: dict
    session_b: dict
    matched: int
    top: list[dict]
    max_delta_e: Optional[float] = None
    worst_condition: str = ""
    risk: str = "none"
    risk_label: str = "无可比光谱"
    warnings: list[str] = []
    disclaimer: str = "工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI"


# ---------------------------------------------------------------------------
# V0.11 归档依赖 / 完整性审计与修复
# ---------------------------------------------------------------------------

class DependencyBlocker(BaseModel):
    code: str
    message: str


class DependencyReport(BaseModel):
    process_id: str
    name: str = ""
    archived: bool = False
    archived_at: str = ""
    archive_reason: str = ""
    archived_by: str = ""
    counts: dict = {}
    blockers: list[DependencyBlocker] = []
    can_permanent_delete: bool = False


class IntegrityRepairRequest(BaseModel):
    """默认 dry_run；actions 为空表示全部修复动作。"""

    dry_run: bool = True
    actions: list = Field(default_factory=list)


class IntegrityReport(BaseModel):
    generated_at: str = ""
    summary: dict = {}
    details: dict = {}
    actions: list[dict] = []


class IntegrityRepairResult(IntegrityReport):
    dry_run: bool = True
    applied: bool = False
    planned: dict = {}
    counts: dict = {}
    after: Optional[dict] = None
