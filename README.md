# 色彩工坊 · 印前色彩管理闭环平台（V0.11 已实现）

> 面向印前的色彩管理小软件 · V0.11 已实现（2026-11-23）
> 以 ΔE / CIEDE2000 为统一度量，以**数值优化 + 光谱证据**为主线：
> **目标 LAB → 工艺 ICC 反查最优分色值（设备值）→ 打印实测 → 闭环修正 → 校准收敛 → 工艺护照留痕**，
> 且所有测量数据先过**方法学门禁**（测前登记 → 校准确认 → 测量 → 复测一致性 → 交叉校验 → 可信度分级 → 分级数据流向）。
>
> V0.11 在 V0.10 证据链上完成**归档优先、完整性修复、护照历史与可用性修复**；
> 不训练模型、不实现基础油墨 KM 调墨，仍以**数值优化 + 光谱证据**为对外定位。

## 定位与核心工作流

产品围绕两条业务主线：

1. **配方预测（旗舰）**：客户给定目标 LAB（色样/色卡标准值），基于工艺 ICC
   反查最优分色值（设备值）。一次反查 = 大量正向预测迭代（粗查表 8.3 万点 + 局部精搜），
   ΔE2000 收敛后输出 C/M/Y/K 百分比与预测色差。
2. **实测验证闭环**：配方打印后用分光光度计实测 LAB 回填，系统做模型误差补偿
   （p' = t - (m - p)）并再次反查，输出修正分色值（设备值）。
3. **工艺证据与风险（V0.10）**：把 ICC、补偿、校准轮次、测量会话、配方统计与
   光谱覆盖汇总为不可变的**工艺护照**；用现有 CGATS 光谱数据计算多光源同色异谱风险，
   并以 `passport_hash` 把签样放行绑定到明确的证据版本。
4. **生命周期与完整性（V0.11）**：删除改为**归档优先**；归档工艺只读、可恢复，
   只有显式永久清理才删除派生记录。测量/评价解绑保留、签样标记孤儿；完整性审计
   默认 Dry Run，显式 apply 才移动未引用 ICC 和修复孤儿数据。

工艺（打印机 + 纸张 + 墨水组合）是一等实体，**不同工艺绑定不同的 ICC 特性文件**，
预测与转换均以工艺为上下文。V0.11 当前定位是**印前色彩数值优化 + 光谱证据平台**：
以 ICC / CMM / ΔE00 / 补偿与校准数值计算为骨架，以光谱与测量方法学作为可追溯证据；
不把模型能力作为当前卖点。

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
| 工艺护照与光谱证据 | 自研（`passport.py` / `spectral.py`，V0.10） | 版本化护照（证据覆盖 + 警告 + 不可变快照 + sha256）、M0/M1/M2 记录、四光源光谱积分与工程同色异谱风险 |
| 生命周期与完整性 | 自研（`lifecycle.py` / `integrity.py`，V0.11） | 归档/恢复/永久清理、依赖计数、只读门禁、孤儿扫描与 Dry Run 修复、未引用 ICC 隔离 |
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
│   │   ├── passport.py         # 工艺护照：证据汇总 / 状态 / sha256 / 版本（V0.10）
│   │   ├── spectral.py         # 多光源光谱积分 / 同色异谱风险 / 会话比较（V0.10）
│   │   ├── lifecycle.py        # 归档 / 恢复 / 依赖计数 / 永久清理（V0.11）
│   │   ├── integrity.py        # 孤儿扫描 / Dry Run / 修复 / ICC 隔离（V0.11）
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
│   │   ├── passport.py         # /api/processes/{pid}/passport*（V0.10）
│   │   ├── spectral.py         # /api/spectral*（V0.10）
│   │   ├── integrity.py        # /api/integrity/report|repair（V0.11）
│   │   └── main.py              # V0.1 端点（设备/测量/闭环/可视化…）
│   └── main.py                  # 应用入口（/ 重定向前端）
├── static/
│   ├── index.html               # 外壳（侧边栏 + 底部标签栏）
│   ├── css/app.css              # iOS 扁平设计令牌 + 组件
│   ├── vendor/three/            # three.js r187 vendored（ESM，含 sha512 溯源 README）
│   └── js/                      # api / router / icons / components / gamut-view / gamut-three / views/*
├── scripts/                     # 探针、冒烟、e2e、审计 CLI、V0.11 截图
├── data/                        # 运行时数据（工艺 ICC、配方记录、补偿模型、passports.json）
└── tests/                       # pytest 260 项（233 基线 + 27 新增：归档/完整性/护照历史/UI）
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
python scripts/audit_data.py --dry-run   # 完整性审计（默认不写盘）
python scripts/shoot_v11.py              # V0.11 截图，需服务运行中
```

> 跨平台提示：测试与 E2E 统一使用仓库内置
> `tests/fixtures/icc/testbed_cmyk.icc`（MIT 许可），不读取 Windows 系统 profile。
> E2E 可用 `COLORFORGE_E2E_BASE_URL` 指向隔离实例；截图可用
> `COLORFORGE_V11_BASE_URL` 指向目标实例。

## API 一览

### 工艺库
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/processes` | POST | 创建工艺（raw bytes body = ICC 文件，query 传名称/纸张/墨水） |
| `/api/processes` | GET | 工艺列表；`?include_archived=1` 包含归档；`summary=1` 附带色域摘要与配方达标率 |
| `/api/processes/{id}` | GET/DELETE | 详情（含补偿模型）/ **归档**（V0.11 起不再硬删除） |
| `/api/processes/{id}` | PATCH | 编辑工艺（名称/纸张/墨水/备注） |
| `/api/processes/{id}/icc` | GET/PUT | 下载 ICC / 替换 ICC（sha 变更后色域与变换缓存自动失效、补偿模型重置） |
| `/api/processes/{id}/gamut` | GET | 色域采样：xy/Lab 点集、凸包索引、Qhull 表面三角、sRGB 覆盖率、Lab 包围盒体积 |
| `/api/processes/{id}/gamut/compare` | GET | 双工艺色域对比：凸包归属 + xy 交集多边形与面积占比 |
| `/api/processes/{id}/archive` | POST | 归档（body `{reason, actor}`）；已归档时幂等 |
| `/api/processes/{id}/restore` | POST | 恢复为 active |
| `/api/processes/{id}/permanent?confirm={id}` | DELETE | 仅已归档工艺；永久清理派生记录，测量/评价解绑、签样标孤儿 |
| `/api/processes/{id}/dependencies` | GET | 配方/补偿/校准/护照/测量/评价/签样计数与 blockers |
| `/api/processes/{id}/compensation` | DELETE | 重置工艺级误差补偿模型 |

**V0.11 生命周期**：`DELETE` 变为归档；归档工艺的 PATCH、ICC 替换、预测/验证、
测量导入、评价创建/打分、签样创建/迁移、护照生成、文件转换/PDF-X 均返回
409 且 `detail.code=PROCESS_ARCHIVED`。读取、gamut、ICC 下载与护照历史保持可用。
永久清理必须先归档并传入匹配的 `confirm`。

### 配方预测
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/recipes/predict` | POST | 批量 LAB→CMYK 反查（配方/ΔE/迭代轨迹/插值精搜评估数/补偿应用状态） |
| `/api/recipes/verify` | POST | 实测验证闭环（模型误差补偿 + 修正分色值 + 补偿模型沉淀） |
| `/api/recipes/forward` | POST | CMYK→LAB 正查（与反查共用同一变换） |
| `/api/recipes/stats` | GET | ΔE 监控聚合：总量/达标率/mean/P95/时间序列/分工艺漂移告警 |
| `/api/recipes` | GET | 配方历史（可按工艺过滤） |
| `/api/recipes/{rid}/export` | GET | V0.10：导出配方单 JSON/Markdown（`format=json\|md`） |

**V0.10 配方语义**：`RecipeResult.recipe` 保持原字段与顺序，新增
`recipe_kind="device_values"` 与 `recipe_meta`（unit/basis/note）。界面称“分色值（设备值）”，
不再把设备通道百分比误导为油墨调配配方；旧记录读取时默认 `recipe_kind=device_values`。

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

### 工艺护照（V0.10）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/processes/{pid}/passport` | POST | 生成/刷新护照；内容未变时返回现有版本，不新增版本 |
| `/api/processes/{pid}/passport` | GET | 当前最新护照 |
| `/api/processes/{pid}/passport/versions` | GET | 版本列表（每工艺最多保留 50 版） |
| `/api/processes/{pid}/passport/{version}` | GET | 指定版本护照详情（不可变快照） |
| `/api/processes/{pid}/passport/{version}/export` | GET | 导出 JSON/Markdown（`format=json\|md`） |

**V0.11 版本历史**：`GET /passport/versions` 每行新增 `is_latest`、
`referenced_by_signoffs`（id/state/title）与 `referenced_count`；签样引用旧版本时
即使护照继续刷新，旧版仍保留并显示引用。归档工艺的历史版本可只读查看。

### 数据完整性（V0.11）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/integrity/report` | GET | 扫描 active/archived、孤儿派生/证据记录、缺失 ICC、未引用 ICC |
| `/api/integrity/repair` | POST | body `{dry_run, actions}`；默认 `dry_run=true`，显式 false 才写盘 |
| `scripts/audit_data.py --dry-run\|--apply` | CLI | 与 API 共用审计/修复服务；JSON 输出 |

修复策略：孤儿配方/补偿/校准/护照删除；孤儿测量/评价解绑保留；孤儿签样标记
`orphaned_at/orphan_reason`；未引用 ICC 移至 `data/orphaned_icc/`。审计默认不创建目录、
不写文件；修复幂等。

### 光谱 / 同色异谱（V0.10）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/spectral/metamerism` | POST | 按 patch label 匹配两组光谱，输出四光源 Lab / ΔE00 / 最大风险 / 最差光源 |
| `/api/spectral/compare` | POST | 比较两个测量会话的光谱覆盖与 patch 差异，返回 top 差异 |
| `/api/spectral/sessions/{sid}/summary` | GET | 单会话光谱摘要：patch 数、波长范围、可用/警告状态 |

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

### V0.10 兼容与机制

- **护照版本与哈希**：护照快照汇总 ICC、补偿、校准轮次、测量会话、配方统计、
  光谱覆盖与警告；快照不可变。`passport_hash` 为规范化 JSON 的 sha256，
  排除 `generated_at`、`version`、`passport_hash`、`status`、`status_reasons`、`id`，
  数值固定 4 位小数；
  内容不变时 `POST` 返回现有版本，不新增版本；版本写入使用进程内锁串行化。
- **状态判定**：ICC 缺失/不可读 → `invalid`；ICC 或补偿/校准指纹与快照不一致 →
  `stale`；无 verified 测量，或 CMYK 工艺无校准轮次 → `incomplete`；否则 `valid`。
  状态动态计算，不写入快照。
- **单边依赖**：护照不嵌入签样状态；签样以可选 `passport_id` / `passport_hash`
  引用护照，且仅允许关联 `valid` 护照；`release_manifest` 增加护照摘要。签样含
  `passport_hash` 时，`SignoffStore.gate()` 校验当前最新护照 hash 与动态状态：
  hash 不匹配返回 `passport_changed`，状态为 `incomplete/stale/invalid` 时分别返回
  `passport_incomplete/passport_stale/passport_invalid`；
  旧签样没有这两个字段时仍按 ICC 指纹放行。
- **M0 / M1 / M2**：工艺记录可选测量条件；未指定时护照不比较条件，只记录缺失警告，
  不强制回填旧数据。
- **配方语义**：原 `recipe` 保留；新增 `recipe_kind`、`recipe_meta`。CSV 仅在末尾
  增加 `recipe_kind` 列，旧列顺序不变；`ink_formula` 只保留类型位，不实现基础油墨 KM 调墨。
- **光谱口径**：支持 `D50/2°`、`D65/2°`、`A/2°`、`FL11/2°`（界面显示 CIE F11）；
  复用 `cgats.integrate_lab` / `white_xyz`。输出各光源 Lab、ΔE00、最大 ΔE、最差光源与
  风险等级（`≤1.0 low`、`≤2.0 medium`、`>2.0 high`，常量集中定义，本版不做 UI 配置）。
  明确标注这是**工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI**。
- **光谱边界**：输入限定 CGATS 积分网格 `380–730nm`；允许荧光增白纸反射率 >1（OBA），
  <0 或 >1.5 给警告；积分/Lab/ΔE 非有限时拒绝（422），不同波长网格各自独立积分，
  不强行重采样；无光谱时 `available=false` 并给 warning，不伪造曲线或 ΔE。
- **加法式兼容**：新增字段与路由全部为可选/新增，旧 API 响应字段不删不改；
  `data/passports.json` 由 `PassportStore` 管理，幂等迁移补默认字段与初始化护照库，
  不删除旧数据。破坏性变更留到 V1.x。

### V0.11 归档与完整性机制

- **归档优先**：`archived_at` 非空即只读；旧数据缺字段默认 active。归档可逆，
  且不自动删除 ICC、配方、补偿、校准或护照。
- **只读门禁**：所有会改变证据的写操作在进入业务逻辑前检查归档，统一返回
  `409 {"detail":{"code":"PROCESS_ARCHIVED"}}`；门禁 `gate` 返回
  `reason=process_archived`，`release-manifest` 返回 409。
- **永久清理**：仅 archived + `confirm=<pid>` 可执行。工艺、ICC、配方、补偿、
  校准与护照删除；测量会话/评价写 `detached_from_process/detached_at` 并解绑；
  签样不删，写 `orphaned_at/orphan_reason`。响应返回 deleted/detached/orphaned 计数。
- **完整性审计**：报告按类给出 `summary/details/actions`，每项含可操作 id/文件名；
  Dry Run 返回计划但零写盘；apply 幂等，未引用 ICC 重名时追加序号。
- **护照历史引用**：签样按 `passport_id` 或 `passport_hash` 命中版本引用（OR 语义），
  因此旧签样缺少部分字段也能被识别；历史版本快照仍不可变。
- **a11y**：模态自动生成 `aria-labelledby`/`aria-label`，背景 `inert`+`aria-hidden`，
  Tab/Shift+Tab 圈闭、Esc 关闭与焦点还原经 Playwright 键盘路径验证；光谱 Canvas
  增加 `role="img"`、摘要 `aria-label` 与指向数据表的 `aria-describedby`。

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
- **工艺护照（V0.10）**：护照状态动态判定、快照不可变；hash 对规范化 JSON
  做 sha256（排除时间/版本/hash/派生展示字段），按 4 位小数消除浮点尾差；版本幂等，
  不用护照状态反向嵌入签样，避免循环依赖。
- **光谱 / 同色异谱（V0.10）**：四个标准光源分别积分，风险阈值固定为
  1.0/2.0 ΔE00；光谱覆盖不足或 patch 无法按标签配对时如实返回不可用/警告，
  不插值造数。该指标是工程风险筛查，不是 CIE 15 特殊同色异谱指数。
- PCS 为 D50 Lab；前端色块与点云经 Bradford 色适应（D50→D65）转 sRGB / display-p3 显示。

## 版本

> **V0.11 已于 2026-11-23 实现并验收**：`pytest` **260 passed**；
> 隔离数据目录 + 8797 端口运行 `scripts/e2e_http.py` 得到 **E2E ALL OK**；
> `scripts/shoot_v11.py` 产出 7 张截图，Playwright 键盘路径通过且无页面错误。
> 实现记录见 `docs/V0.11-实施记录.md`，验收清单一见 `docs/V0.11-验收.md`。
>
> 已知限制：存储仍为 JSON；护照只导出 JSON/Markdown，不生成 PDF；归档写锁为进程内锁，
> 多 worker 部署需外置文件锁或数据库事务；完整性修复不做启动自动修复；200% 缩放使用
> CSS zoom 代理验证，完整 NVDA/VoiceOver 全页审计未完成。
>
> 当前路线图：V0.11 已交付；P1（拼版/专色/图像调整）与 P2（交付包/认证目标）
> 仍未排期，后续以 `docs/印前标准-7步进度评判.md` 的待办清单为准。

- **V0.11（当前，已实现）**：生命周期完整性 + 护照历史 + 可用性修复 ——
  `DELETE` 改为**归档优先**，默认列表排除归档，`include_archived=1` 可查看；归档
  工艺只读、可恢复，所有写操作返回 409 `PROCESS_ARCHIVED`；永久清理仅限 archived +
  匹配 confirm，删除工艺/ICC/配方/补偿/校准/护照，测量与评价解绑保留，签样标记
  orphaned；新增依赖计数、完整性 `report/repair` API 与 `scripts/audit_data.py`
  Dry Run/apply；护照版本历史新增最新标记与签样引用；模态补 `aria-labelledby`/`inert`，
  光谱 Canvas 补摘要与数据表关联，375px 卡片化；E2E 去除 Windows ICC 依赖，
  统一内置测试 profile。实施记录见 `docs/V0.11-实施记录.md`。
- **V0.10（上一版）**：工艺护照 + 配方语义 + 光谱风险 —— 新增版本化**工艺护照**
  （ICC/补偿/校准轮次/测量会话/配方统计/光谱覆盖汇总，`valid/incomplete/stale/invalid`
  动态状态，不可变快照 + 规范化 sha256，幂等版本，每工艺最多 50 版）；工艺可选
  M0/M1/M2 测量条件；配方显式标注**分色值（设备值）**，新增配方单 JSON/MD 导出与
  CSV 尾部 `recipe_kind`；签样可选引用 `passport_id/passport_hash`（仅 valid），
  护照变更或状态失效后门禁返回 `passport_changed` / `passport_stale` 等，
  旧签样保留 ICC 指纹门禁；新增四光源（D50/D65/A/FL11，2°）
  光谱同色异谱风险与双会话比较，输出各光源 Lab/ΔE00、最大风险与警告，明确不等同
  CIE 15 MI。按 4 个两周 Sprint + 1 周 hardening 规划推进并完成验收，详见
  `docs/V0.10-实施规划.md` 与 `docs/V0.10-实施记录.md`。
- **V0.9（上一版）**：预检深水区 + PDF/X 封装 + 签样放行 + 显示器门禁 —— PDF 字体真嵌入
  （含 Type0/CIDFont）、TrimBox/BleedBox 出血量与标准开本、图像有效 DPI（内容流 CTM +
  Form 递归）；**PDF/X-4 结构封装**（OutputIntent + XMP + 盒子，严格模式拦截未嵌入字体 /
  RGB 图像）；签样状态机（draft/pending/approved/rejected/locked）、证据关联、驳回理由、
  ICC 变更自动失效、放行门禁与交接清单（release_token）；**显示器档案与软打样门禁**
  （目标亮度/白点/对比度、校准确认有效期、到期前 30 天提醒、未校准/过期标注「预览仅参考」；
  真实屏幕测量 CGATS/TSV 导入 → 白点/对比度推导、原样留档导出）。
  P1（拼版/专色/图像调整）与 P2（交付包）待做，路线见 `docs/印前标准-7步进度评判.md`，
  本轮记录见 `docs/V0.9-P0-实施记录.md`
- **V0.8**：校准收敛与复测验证 —— apply 落库校准轮次（幂等/同秒确定性排序）、
  新会话报告自动对比上一轮（改善/持平/恶化 + 逐通道 Δ）、工艺收敛判定（converged/improving/
  worsening/flat）、工艺详情页校准历史卡（轮次列表 + max ΔE sparkline + 收敛 chip）、
  监控页校准状态行带轮次与收敛。实施记录见 `docs/V0.8-实施规划.md`
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
- **V0.5**：可信测量闭环 —— 实验方法七要素审计（三份课程报告：
  复测缺失/校准未记录/转录链断裂/口径漂移均转化为产品对策）、CGATS.17 +
  Status-T 双解析器（编码容错 + CIE 积分自实现）、五条可靠性规则分级
  （verified/warning/rejected + density_ok 通道标记）、测量会话登记与导入向导、
  GATF 油墨四指标 + TVI 曲线 + GATF 色轮（只消费 verified）、A4 CMYK 双点
  重复样张生成器、ΔE 分量分解（dL/da/db/dC/dh/dE76）+ 解读文案、
  补偿模型 verified 门禁。实施记录见 `docs/V0.5-实施规划.md`
- **V0.4**：真 3D + 精度 + 监控闭环 —— three.js 真 3D 色域体积
  （Qhull 包络网格 + Lambert 光照 + L\* 切片 + WebGL 回退链）、插值精搜层
  （打破 8-bit 平台期）+ 精度基线刷新、工艺级补偿模型（累积学习）、
  ΔE 监控面板（趋势 + 漂移告警）、多工艺色域对比（凸包归属 + 交集运算）、
  PDF 印前预检（pypdf + 首页缩略图）、修复凸包索引错位遗留 bug。
  实施记录见 `docs/V0.4-实施规划.md`
- **V0.3**：ICC 模块视觉升级与功能补全 —— 色域凸包 + L\* 真彩点云 +
  伪 3D 体积视图（拖拽旋转 / L\* 切片 / hover 读数）、工艺指标卡（体积/覆盖率/达标率）、
  工艺编辑与 ICC 替换、批量上传、搜索筛选排序、「工艺→预测」带参联动、
  交互基建（模态焦点圈闭 / 骨架屏 / 危险确认弹层 / 375px 适配）、
  测试体系去平台化（50 项，含 API 层与精度基线门禁）。
  路线图见 `docs/V0.3-下一阶段规划.md`
- **V0.2**：印前配方工作流 —— 工艺库、LAB→CMYK 反查引擎、实测验证闭环、
  印前预检与转换、iOS 扁平 UI 全面重构（亮/暗主题、渐进式披露）
- **V0.1**：显示校准闭环（闭环校正 / 色卡识别 / 色域可视化 / ΔE）

## 许可

[MIT](LICENSE)（第三方资产许可单独注明：测试 ICC 见
`tests/fixtures/icc/THIRD-PARTY-LICENSES.md`；内嵌 three.js r187 见
`static/vendor/three/`（MIT，含 sha512 溯源记录））
