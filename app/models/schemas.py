"""Pydantic 数据模型：定义各模块的请求/响应结构。"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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
    rgb: list[float] = Field(..., description="测量得到的 RGB 值")
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
    image_bytes: str = Field(..., description="base64 编码的图像")
    expected_patches: int = 24
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
    rgb: list[float]
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
    measured_rgb: list[float]
    target_rgb: list[float]
    space: str = "srgb"
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = 2.0


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
    rgb1: list[float]
    rgb2: list[float]
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
    lab1: list[float] = Field(..., description="CIELAB [L, a, b]")
    lab2: list[float] = Field(..., description="CIELAB [L, a, b]")
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = 2.0


# ---------------------------------------------------------------------------
# 闭环验证与迭代
# ---------------------------------------------------------------------------

class ClosedLoopRequest(BaseModel):
    device_id: str
    target_rgb: list[float]
    initial_measured_rgb: list[float]
    space: str = "srgb"
    method: DeltaEMethod = DeltaEMethod.cie2000
    tolerance: float = 2.0
    max_iterations: int = 10


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
    icc_size: int = 0
    icc_original_name: str = ""
    icc_meta: dict = {}
    created_at: str = ""


class RecipeTarget(BaseModel):
    label: str = ""
    lab: tuple[float, float, float] = Field(..., description="目标 CIELAB [L, a, b]")


class RecipePredictRequest(BaseModel):
    process_id: str
    targets: list[RecipeTarget] = Field(..., max_length=50, description="目标色列表（单次最多 50 个）")
    tolerance: float = 2.0
    method: DeltaEMethod = DeltaEMethod.cie2000
    max_rounds: int = 6


class RecipeResult(BaseModel):
    label: str = ""
    target_lab: list[float]
    predicted_lab: list[float]
    recipe: dict[str, float] = Field(..., description="配方：通道名 -> 百分比")
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
    image_b64: str
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
    image_b64: str
    process_id: str
    intent: str = "relative_colorimetric"


class FileConvertResponse(BaseModel):
    preview_b64: str
    file_b64: str
    file_format: str
    output_mode: str
    source_profile: str = ""
