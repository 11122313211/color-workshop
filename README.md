# 色彩工坊 · 印前色彩管理闭环平台

> 面向印前的色彩管理小软件 · V0.9（预检深水区 · PDF/X-4 封装 · 签样放行 · 显示器软打样门禁）
> 以 ΔE / CIEDE2000 为统一度量：**目标 LAB → 工艺 ICC 反查最优 CMYK 配方 → 打印实测 → 闭环修正 → 工艺级累积学习**，
> 且所有测量数据先过**方法学门禁**（测前登记 → 校准确认 → 测量 → 复测一致性 → 交叉校验 → 可信度分级 → 分级数据流向）。

## 定位与核心工作流

产品围绕两条业务主线：

1. **配方预测（旗舰）**：客户给定目标 LAB（色样/色卡标准值），基于工艺 ICC
   反查最优墨量配方。一次反查 = 大量正向预测迭代（粗查表 8.3 万点 + 局部精搜），
   ΔE2000 收敛后输出 C/M/Y/K 百分比与预测色差。
2. **实测验证闭环**：配方打印后用分光光度计实测 LAB 回填，系统做模型误差补偿
   （p' = t - (m - p)）并再次反查，输出修正配方。

工艺（打印机 + 纸张 + 墨水组合）是一等实体，**不同工艺绑定不同的 ICC 特性文件**，
预测与转换均以工艺为上下文。

## 技术栈

| 层级 | 技术 | 用途 |
|---|---|---|
| 算法层 | lcms2（经 Pillow ImageCms）+ colour-science + coloraide + scipy | ICC CMM 正向预测、色差、色域采样、Qhull 凸包 |
| 反查引擎 | 自研（`app/services/recipe.py`） | 粗查表 + 最近邻 + 坐标下降 + **多线性插值精搜层**（插值排序、真实 ΔE 定夺），通道数泛化（CMYK/RGB/GRAY） |
| 补偿模型 | 自研（`app/services/compensation.py`） | 实测闭环沉淀工艺级墨量偏置（EMA 累积 + sha 失效），预测时自动应用；**V0.5 起受 verified 门禁约束** |
| 测量可靠性 | 自研（`cgats.py` / `measurement_qa.py` / `measurements.py` / `density.py`） | CGATS.17 / Status-T 解析（编码容错）、五条校验规则分级（verified/warning/rejected）、GATF 油墨指标与 TVI |
| QC 诊断与评价 | 自研（`diagnosis.py` / `qc_compare.py` / `evaluations.py` / `testform.py`） | 规则型诊断引擎（R1-R8 行业标准口径）、双会话指标 Δ 对比与工艺趋势、GB/T 34053 主观评价检查单、双点样张 + 目视诊断页 |
| 印刷校准 | 自研（`calibration.py` + `testform.py` 校准版，V0.7） | ISO 12647-7 风格编号校准版、双基准报告（ICC 预期 + ISO 12647-2 固体参考）、逐通道墨量建议（写入补偿模型）、Lab 有效网点反演校正曲线、特性数据 CGATS 导出 |
| 印前预检与输出 | 自研（`files.py` / `pdfx.py` / `signoffs.py` / `displays.py`，V0.9） | PDF 字体真嵌入 / TrimBox 出血 / 有效 DPI（内容流 CTM）/ 标准开本；PDF/X-4 结构封装（OutputIntent + XMP + 盒子）；签样状态机与放行门禁（ICC 变更自动失效）；显示器校准档案与软打样门禁、屏幕测量 CGATS 导入/导出 |
| 后端 | FastAPI + JSON 落盘（`data/`） | API 服务，工艺库/配方记录/补偿模型/测量会话重启不丢（损坏自动隔离备份） |
| 前端 | 原生 ES Module + Canvas + three.js（vendored，无构建） | iOS 扁平风 SPA，hash 路由，亮/暗主题，2D/真 3D（WebGL）色域组件 |

## 项目结构

```
color-mgmt/
├── app/
│   ├── algorithms/core.py       # 色彩转换、色差（含批量 ΔE）、ICC、色域映射
│   ├── ai/color_checker.py      # 色卡检测、闭环迭代（保留能力）
│   ├── models/schemas.py        # Pydantic 模型
│   ├── services/
│   │   ├── filedb.py            # JSON 落盘存储（原子写 + 损坏隔离备份）
│   │   ├── processes.py         # 工艺库：CRUD/ICC 替换/色域采样（凸包/覆盖率/Qhull 表面）
│   │   ├── recipe.py            # LAB→CMYK 反查引擎（插值精搜层）+ 配方记录 + 实测验证
│   │   ├── compensation.py      # 工艺级误差补偿模型（EMA 累积学习 + verified 门禁）
│   │   ├── cgats.py             # 测量导出解析（CGATS.17 / Status-T TSV，编码容错，CIE 积分）
│   │   ├── measurement_qa.py    # 五条可靠性规则 → verified|warning|rejected
│   │   ├── measurements.py      # 测量会话存储与导入编排（每次导入全会话重分级）
│   │   ├── density.py           # GATF 四指标 + TVI + 反差率 K / 叠印率 / 灰平衡 / 均匀性（V0.6）
│   │   ├── calibration.py       # 印刷校准闭环：双基准报告 / 墨量建议 / 校正曲线 / 特性数据导出（V0.7）
│   │   ├── calibration_history.py # 校准轮次存储 / 轮次对比 / 收敛判定（V0.8）
│   │   ├── signoffs.py         # 签样状态机 / 放行门禁 / 交接清单（V0.9 P0）
│   │   ├── pdfx.py             # PDF/X-4 结构封装（OutputIntent / XMP / 盒子，V0.9 P0）
│   │   ├── displays.py         # 显示器档案 / 校准有效期 / 软打样门禁 / 屏幕测量导入导出（V0.9 P0）
│   │   ├── diagnosis.py         # 规则型印刷诊断引擎 R1-R8（行业标准口径，V0.6）
│   │   ├── qc_compare.py        # 双会话指标 Δ 对比 + 工艺趋势时间序列（V0.6）
│   │   ├── evaluations.py       # 主观评价检查单（GB/T 34053 方法论，V0.6）
│   │   ├── testform.py          # A4 CMYK 验证样张（双点重复 + 75% 梯级 + 目视诊断页，PDF/TIFF）
│   │   ├── files.py             # 印前预检（图像 + PDF 字体/出血/有效 DPI/开本）+ ICC 转换（软打样）
│   │   ├── store.py             # 设备/测量/工作流（V0.1 保留）
│   │   └── visualization.py     # CIE 色度图、色域数据
│   ├── routers/
│   │   ├── processes.py         # /api/processes（CRUD/PATCH/PUT icc/summary/色域/对比/补偿）
│   │   ├── recipes.py           # /api/recipes（predict / verify / forward / stats / 历史）
│   │   ├── files.py             # /api/files（preflight / pdfx / convert）
│   │   ├── signoffs.py          # /api/signoffs（签样状态机 / 门禁 / 交接清单）
│   │   ├── displays.py          # /api/displays（显示器档案 / 校准状态 / 软打样门禁 / 测量导入导出）
│   │   └── main.py              # V0.1 端点（设备/测量/闭环/可视化…）
│   └── main.py                  # 应用入口（/ 重定向前端）
├── static/
│   ├── index.html               # 外壳（侧边栏 + 底部标签栏）
│   ├── css/app.css              # iOS 扁平设计令牌 + 组件
│   ├── vendor/three/            # three.js r187 vendored（ESM，含 sha512 溯源 README）
│   └── js/                      # api / router / icons / components / gamut-view / gamut-three / views/*
├── scripts/                     # 探针、冒烟、e2e、清理脚本
├── data/                        # 运行时数据（工艺 ICC、配方记录、补偿模型）
└── tests/                       # pytest 214 项（API 层 + 精度基线门禁 + 测量可靠性 + 诊断 + 评价 + 校准 + 轮次收敛 + 预检/PDF-X/签样/显示器 + fixtures）
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
cd color-mgmt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. 访问
#   应用:    http://127.0.0.1:8765/          （自动跳转前端）
#   API 文档: http://127.0.0.1:8765/docs

# 4. 测试（内置 MIT 许可测试 ICC，跨平台可跑，不再依赖 Windows 系统 profile）
python -m pytest tests/ -q
python scripts/e2e_http.py   # HTTP 端到端（需服务运行中）
```

> 提示：本机无 CMYK ICC 时，可从 `C:\Windows\System32\spool\drivers\color\`
> 上传系统自带的 RSWOP.icm / JapanColor2001Coated.icc 创建演示工艺。

## API 一览

### 工艺库
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/processes` | POST | 创建工艺（raw bytes body = ICC 文件，query 传名称/纸张/墨水） |
| `/api/processes` | GET | 工艺列表；`?summary=1` 附带色域摘要（体积/sRGB 覆盖率/凸包）与配方达标率 |
| `/api/processes/{id}` | GET/DELETE | 详情（含补偿模型）/ 删除（级联清理配方记录与补偿模型） |
| `/api/processes/{id}` | PATCH | 编辑工艺（名称/纸张/墨水/备注） |
| `/api/processes/{id}/icc` | GET/PUT | 下载 ICC / 替换 ICC（sha 变更后色域与变换缓存自动失效、补偿模型重置） |
| `/api/processes/{id}/gamut` | GET | 色域采样：xy/Lab 点集、凸包索引、Qhull 表面三角、sRGB 覆盖率、Lab 包围盒体积 |
| `/api/processes/{id}/gamut/compare` | GET | 双工艺色域对比：凸包归属 + xy 交集多边形与面积占比 |
| `/api/processes/{id}/compensation` | DELETE | 重置工艺级误差补偿模型 |

### 配方预测
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/recipes/predict` | POST | 批量 LAB→CMYK 反查（配方/ΔE/迭代轨迹/插值精搜评估数/补偿应用状态） |
| `/api/recipes/verify` | POST | 实测验证闭环（模型误差补偿 + 修正配方 + 补偿模型沉淀） |
| `/api/recipes/forward` | POST | CMYK→LAB 正查（与反查共用同一变换） |
| `/api/recipes/stats` | GET | ΔE 监控聚合：总量/达标率/mean/P95/时间序列/分工艺漂移告警 |
| `/api/recipes` | GET | 配方历史（可按工艺过滤） |

### 印前文件
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/files/preflight` | POST | 预检：图像（嵌入 ICC / 模式 / DPI / 主导色）；PDF（页数/色彩空间/专色/输出意图/缩略图 + **字体真嵌入/TrimBox 出血/有效 DPI/标准开本**） |
| `/api/files/convert` | POST | 经工艺 ICC 转换（CMYK 输出 TIFF + 软打样预览 PNG）；PDF 仅预检不支持转换；响应附 `softproof` 显示器校准门禁 |
| `/api/files/pdfx` | POST | PDF/X-4 结构封装（raw PDF body）：写入 OutputIntent + XMP `GTS_PDFXVersion` + TrimBox/BleedBox；严格模式拒绝未嵌入字体 / 未标定 RGB 图像 |

### 签样与放行（V0.9 P0）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/signoffs` | POST/GET | 创建草稿（关联工艺 + 证据）/ 列表（按工艺、状态过滤） |
| `/api/signoffs/gate` | GET | 放行门禁：最新 approved/locked 签样 + ICC 指纹一致性 |
| `/api/signoffs/release-manifest` | GET | 交接清单；无签样或 ICC 已变更返回 409 |
| `/api/signoffs/{id}` | GET/DELETE | 详情（含历史）/ 删除（仅草稿） |
| `/api/signoffs/{id}/submit` | POST | 提交确认（至少一项依据） |
| `/api/signoffs/{id}/approve` | POST | 批准 |
| `/api/signoffs/{id}/reject` | POST | 驳回（理由必填） |
| `/api/signoffs/{id}/withdraw` | POST | 撤回草稿 |
| `/api/signoffs/{id}/lock` | POST | 锁定（终态）+ 生成放行令牌 |

### 显示器档案与软打样门禁（V0.9）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/displays` | POST/GET | 创建（可带首次校准确认）/ 列表（含校准状态） |
| `/api/displays/active-status` | GET | 当前显示器门禁状态（ok/expiring/expired/uncalibrated/none） |
| `/api/displays/{id}` | GET/PATCH/DELETE | 详情 / 更新目标值 / 删除 |
| `/api/displays/{id}/activate` | POST | 设为当前显示器（互斥） |
| `/api/displays/{id}/calibrations` | POST | 追加校准确认（日期/有效期/实测亮度/白点/ICC 名称） |
| `/api/displays/{id}/status` | GET | 校准状态与到期天数 |
| `/api/displays/{id}/report` | GET | 校准档案报告（JSON 留痕） |
| `/api/displays/{id}/measurements` | POST | 导入屏幕测量（raw CGATS.17 / TSV，需含 XYZ 白/黑色块）→ 白点 xy / 对比度 / 绝对亮度 |
| `/api/displays/{id}/measurements/latest` | GET | 原样导出最近测量文件（附 `X-Content-SHA256` 短摘要） |

### QC 测量可靠性闭环（V0.5 / V0.6）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/qc/measurements/import` | POST | 导入测量导出文件（raw bytes；会话登记 instrument/condition M0-M2/calibrated_at/backing/repeat_mode）；每次导入全会话重分级 |
| `/api/qc/measurements` | GET | 会话列表 / 会话详情（`?session_id=`，可按 reliability 过滤） |
| `/api/qc/measurements` | DELETE | 删除会话 / 单条记录（删后重分级） |
| `/api/qc/ink-quality` | GET | GATF 四指标 + TVI + 反差率 K / 叠印率 / 灰平衡偏移 / 实地均匀性 + **诊断结论**（仅消费 verified 且密度通道可用） |
| `/api/qc/testform` | POST | A4 CMYK 验证样张（设备值直出 + 双点重复布局 + 75% 梯级；第二页目视诊断区：星标/细线/套准） |
| `/api/qc/compare` | GET | 双会话指标 Δ 表（K/TVI/色强度/灰度/叠印率，Δ=主−对比） |
| `/api/qc/trend` | GET | 工艺绑定会话的时间序列（每会话现场计算，不落库） |
| `/api/qc/evaluations/templates` | GET | 主观评价模板（书刊/纸包装/塑料包装）与 5 级打分制说明 |
| `/api/qc/evaluations` | POST/GET/DELETE | 创建 / 列表 / 详情（含统计）/ 删除主观评价 |
| `/api/qc/evaluations/entries` | POST | 录入评价人打分（JSON body `{指标: 1-5}`） |
| `/api/qc/calibration/form` | POST | V0.7 校准版样张（编号色块矩阵）+ 预期值参考表（ICC 预期 Lab + ISO 固体参考列） |
| `/api/qc/calibration` | GET | 双基准校准报告（逐 patch ΔE + 分量、逐通道墨量建议、校正曲线、导出文本） |
| `/api/qc/calibration/apply` | POST | 校准建议写入补偿模型（预测立即联动；ICC 替换自动失效）；V0.8 起落库为校准轮次（同会话幂等） |
| `/api/qc/calibration/history` | GET | V0.8 工艺校准轮次历史 + 收敛判定（converged/improving/worsening/flat；仅计入当前 ICC sha 一致的轮次） |

**可靠性分级与数据流向**：每条记录输出 `verified / warning / rejected` + 原因 +
`density_ok` 通道标记。五条规则：①复测一致性（同标签 ΔE≤0.4；**V0.6 起纯密度
记录按滤色密度通道配对，差 ≤0.05**）②纸白校验（本征合理性 + 工艺 ICC 纸白比对）
③交叉校验（Lab↔XYZ 跨条件探测 D50/D65 × 2°/10°，全条件失配 → rejected；
光谱↔Lab 积分复算）④密度/色度物理量程 ⑤梯尺 3σ(MAD) 离群。
**verified → QC 指标/ΔE 分量/补偿沉淀；warning → 仅展示；rejected → 可查不可用**。
verify 端点可携带 `measurement_session_id` 关联测量会话（补偿门禁）。

**QC 诊断引擎（V0.6，R1-R8）**：量化常量取自行业标准——TVI 中间调目标 16%±3%
（ISO 12647-2:2013）、反差率 K≥0.40 / ΔK±10%、叠印率 ≥60%、灰平衡 |a*|,|b*|≤1.0
（GB/T 17934.2 精神）、实地 CV≤5%；每条结论含证据/成因/建议/依据，
缺数据给 info 指引（生成 V0.6 样张即可补齐 75% 梯级/叠印块/灰平衡块）。
**主观评价（GB/T 34053 方法论）**：多评价人独立 5 级打分 → 每指标算术平均 +
标准差（σ>0.8 提示打分分歧）。

### V0.1 端点（保留，供工具箱使用）
`/api/devices`、`/api/measurements`、`/api/color-checker/detect`、`/api/convert`、
`/api/correction`、`/api/delta-e`（含 `/lab`）、`/api/closed-loop`、
`/api/visualization/gamut`、`/api/workflow`、`/api/health`、`/api/spaces`

## 精度说明

- 正向预测经 lcms2 CMM，报告的 `predicted_lab` / `delta_e` 恒为真实 8-bit 变换结果
  （L 步长 ≈0.39，a/b 步长 ≈1.0），口径诚实保守。
- **插值精搜层（V0.4）**：在 17⁴ 粗查表上做多线性插值得到连续正向估计，以更细步长
  （至 0.05% 墨量）继续下降；插值面负责排序与引导，候选逐一经真实 forward 验证、
  仅严格更优才接受——打破 8-bit 平台期掩盖的更优配方（fixture 实测某目标
  ΔE 0.322 → 0.064），并保证任何情况下不劣于粗搜结果。
- **工艺级补偿模型（V0.4）**：实测验证且修正有效时，把「修正配方 − 原配方」按 EMA
  （α=0.35）沉淀为工艺级墨量偏置（逐通道截幅 ±15%）；后续预测自动作用于搜索种子，
  换 ICC 自动失效，支持手动重置——闭环从「单次补偿」升级为「累积学习」。
- 色域外目标不虚假收敛：返回最优可印配方并如实标注 ΔE（≤2 达标 / ≤5 可用 / >5 超差）。
- 精度基线门禁：`tests/test_precision_baseline.py` + `tests/baselines/precision_baseline.json`
  （V0.4 刷新：域内 ΔE 0.064–0.344；回归阈值 ΔE ≤ 基线 +0.15、墨量分量漂移 ≤2pp）。
- **测量可靠性（V0.5）**：CIE 积分自实现（380-730nm/10nm，白点与教科书 D50/2°
  (96.42,100,82.49) 自检一致）；Lab↔XYZ 交叉校验在四个标准条件中寻找一致口径，
  全条件失配（ΔE>12）判 rejected——直接拦截转录/单位错误；GATF 四指标与
  Murray–Davies 数值经手算对拍。
- **QC 指标与诊断（V0.6）**：反差率 K / 叠印率（第二印色滤色通道口径）/ 灰平衡
  a\*b\* 偏移 / 实地均匀性（样本标准差 CV）全部经手算锚点对拍；诊断规则常量取自
  ISO 12647-2:2013 / GB/T 17934.2 / GATF 口径并在结论中标注依据；纯密度复测
  配对（密度差 ≤0.05）打通密度计工作流。
- **校准收敛（V0.8）**：apply 落库轮次（报告快照 + 应用偏置，同会话幂等，seq 保证同秒排序确定）；
  新报告自动与上一轮对比（等级序 + max ΔE 噪声地板 ±0.05 → 改善/持平/恶化）；收敛判定
  （任一轮达标 → converged；否则最近两轮趋势）；轮次保留历史但收敛统计只计入当前 ICC sha。
- **印刷校准（V0.7）**：双基准 = 工艺 ICC 预期（模型误差）+ ISO 12647-2:2013 涂布纸固体
  Lab（C/M/Y/K/CMY；非涂布数值来源无法交叉确认暂不收录）；校正曲线用 Lab 有效网点反演
  （ISO 10128 校正曲线法的 Lab 口径变体，纯色差仪无密度通道也可用）；建议写入补偿模型后
  `bias_for_predict` 立即生效；曲线反演与建议方向经手算锚点对拍（scaled-axis 构造 e(t)=0.8t）。
- **预检与签样（V0.9 P0）**：DPI 取内容流 CTM 的轴向量长度（Form XObject 递归，深度上限 4；
  同一图像多次绘制取最低值）；出血以 TrimBox 为成品线、BleedBox（缺省 MediaBox）为可出血
  边界，四边最小 <3mm 告警；字体嵌入按 FontDescriptor→FontFile* 判定（Type0 查
  DescendantFonts→CIDFont）；签样 lock 生成 sha256 放行令牌，工艺 ICC 指纹变化即判定
  原签样失效（需重新签样）。
- **PDF/X-4 封装（V0.9 P0-2）**：输出意图写工艺 ICC（`/N` 与工艺通道数一致，
  `/S /GTS_PDFX`），XMP 写 `GTS_PDFXVersion/Conformance = PDF/X-4`；缺 TrimBox/BleedBox
  时按 CropBox/MediaBox 补并告警。硬门禁拦截未嵌入字体与未标定 RGB 图像（非严格模式
  降级为 warn）；明确不替代 RIP 端全量 PDF/X 校验。
- **显示器软打样门禁（V0.9 P0-4）**：校准状态由校准日期 + 有效期（月）计算，
  `ok/expiring(≤30天)/expired/uncalibrated`；`/api/files/convert` 的 `softproof`
  字段与工具箱显示器页共用同一判定。屏幕测量可导入真实 CGATS.17 / Status-T TSV
  （须含 XYZ 白/黑色块；发光屏不接受光谱反射率积分），自动推导白点 xy、对比度与
  可选绝对亮度（查询参数或 `LUMINANCE_CD_M2` 元数据），原始文件留档并可原样导出复核。
  系统不生成显示器 ICC（外部工具生成后登记名称/指纹）。
- PCS 为 D50 Lab；前端色块与点云经 Bradford 色适应（D50→D65）转 sRGB / display-p3 显示。

## 版本

- **V0.1**：显示校准闭环（闭环校正 / 色卡识别 / 色域可视化 / ΔE）
- **V0.2**：印前配方工作流 —— 工艺库、LAB→CMYK 反查引擎、实测验证闭环、
  印前预检与转换、iOS 扁平 UI 全面重构（亮/暗主题、渐进式披露）
- **V0.3**：ICC 模块视觉升级与功能补全 —— 色域凸包 + L\* 真彩点云 +
  伪 3D 体积视图（拖拽旋转 / L\* 切片 / hover 读数）、工艺指标卡（体积/覆盖率/达标率）、
  工艺编辑与 ICC 替换、批量上传、搜索筛选排序、「工艺→预测」带参联动、
  交互基建（模态焦点圈闭 / 骨架屏 / 危险确认弹层 / 375px 适配）、
  测试体系去平台化（50 项，含 API 层与精度基线门禁）。
  路线图见 `docs/V0.3-下一阶段规划.md`
- **V0.4**：真 3D + 精度 + 监控闭环 —— three.js 真 3D 色域体积
  （Qhull 包络网格 + Lambert 光照 + L\* 切片 + WebGL 回退链）、插值精搜层
  （打破 8-bit 平台期）+ 精度基线刷新、工艺级补偿模型（累积学习）、
  ΔE 监控面板（趋势 + 漂移告警）、多工艺色域对比（凸包归属 + 交集运算）、
  PDF 印前预检（pypdf + 首页缩略图）、修复凸包索引错位遗留 bug。
  实施记录见 `docs/V0.4-实施规划.md`
- **V0.5**：可信测量闭环 —— 实验方法七要素审计（三份课程报告：
  复测缺失/校准未记录/转录链断裂/口径漂移均转化为产品对策）、CGATS.17 +
  Status-T 双解析器（编码容错 + CIE 积分自实现）、五条可靠性规则分级
  （verified/warning/rejected + density_ok 通道标记）、测量会话登记与导入向导、
  GATF 油墨四指标 + TVI 曲线 + GATF 色轮（只消费 verified）、A4 CMYK 双点
  重复样张生成器、ΔE 分量分解（dL/da/db/dC/dh/dE76）+ 解读文案、
  补偿模型 verified 门禁。实施记录见 `docs/V0.5-实施规划.md`
- **V0.8**：校准收敛与复测验证 —— apply 落库校准轮次（幂等/同秒确定性排序）、
  新会话报告自动对比上一轮（改善/持平/恶化 + 逐通道 Δ）、工艺收敛判定（converged/improving/
  worsening/flat）、工艺详情页校准历史卡（轮次列表 + max ΔE sparkline + 收敛 chip）、
  监控页校准状态行带轮次与收敛。实施记录见 `docs/V0.8-实施规划.md`
- **V0.9（当前）**：预检深水区 + PDF/X 封装 + 签样放行 + 显示器门禁 —— PDF 字体真嵌入
  （含 Type0/CIDFont）、TrimBox/BleedBox 出血量与标准开本、图像有效 DPI（内容流 CTM +
  Form 递归）；**PDF/X-4 结构封装**（OutputIntent + XMP + 盒子，严格模式拦截未嵌入字体 /
  RGB 图像）；签样状态机（draft/pending/approved/rejected/locked）、证据关联、驳回理由、
  ICC 变更自动失效、放行门禁与交接清单（release_token）；**显示器档案与软打样门禁**
  （目标亮度/白点/对比度、校准确认有效期、到期前 30 天提醒、未校准/过期标注「预览仅参考」；
  真实屏幕测量 CGATS/TSV 导入 → 白点/对比度推导、原样留档导出）。
  P1（拼版/专色/图像调整）与 P2（交付包）待做，路线见 `docs/印前标准-7步进度评判.md`，
  本轮记录见 `docs/V0.9-P0-实施记录.md`
- **V0.7**：印刷校准闭环 —— ISO 12647-7 风格编号校准版（28 色块 × 双点，A4 单页）、
  双基准报告（实测 vs 工艺 ICC 预期 vs ISO 12647-2 固体参考，逐 patch ΔE00 + dL/da/db/dC/dh 分量）、
  逐通道墨量建议一键写入补偿模型（预测联动）、Lab 有效网点反演校正曲线（ISO 10128 方法）、
  特性数据 CGATS 导出（实测/校正后 → 供 ArgyllCMS / i1Profiler 生成正式 ICC）、
  三步校准向导（工艺详情页）+ 监控页校准状态行。实施记录见 `docs/V0.7-实施规划.md`
- **V0.6**：闭环后半环——从分级数据走向诊断与修正 —— QC 指标补全
  （反差率 K / 油墨叠印率 / 灰平衡 a\*b\* 偏移 / 实地均匀性，样张补 75% 梯级）、
  规则型诊断引擎 R1-R8（ISO 12647-2 / GB/T 17934.2 / GATF 口径，结论含
  证据/成因/建议/依据）、双会话 Δ 对比 + 工艺趋势时间序列（定期抽检监控暗调）、
  主观评价检查单（GB/T 34053：多评价人 5 级打分 → 均值±标准差 + 一致性预警）、
  样张目视诊断页（星标 / 0.01-0.6pt 细线 / 套准标记）、纯密度复测配对
  （密度计工作流进 verified）。实施记录见 `docs/V0.6-实施规划.md`

## 许可

[MIT](LICENSE)（第三方资产许可单独注明：测试 ICC 见
`tests/fixtures/icc/THIRD-PARTY-LICENSES.md`；内嵌 three.js r187 见
`static/vendor/three/`（MIT，含 sha512 溯源记录））

## 许可

[MIT](LICENSE)（第三方测试 ICC 资产的许可单独见 `tests/fixtures/icc/THIRD-PARTY-LICENSES.md`）
