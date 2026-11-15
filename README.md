# 色彩工坊 · 印前色彩管理闭环平台

> 面向印前的色彩管理小软件 · V0.5（可信测量闭环：数据可靠性门禁）
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
│   │   ├── density.py           # GATF 油墨四指标 + Murray–Davies 网点面积 + TVI
│   │   ├── testform.py          # A4 CMYK 验证样张（双点重复布局，PDF/TIFF）
│   │   ├── files.py             # 印前预检（图像 + PDF/pypdf）+ 经工艺 ICC 转换（软打样）
│   │   ├── store.py             # 设备/测量/工作流（V0.1 保留）
│   │   └── visualization.py     # CIE 色度图、色域数据
│   ├── routers/
│   │   ├── processes.py         # /api/processes（CRUD/PATCH/PUT icc/summary/色域/对比/补偿）
│   │   ├── recipes.py           # /api/recipes（predict / verify / forward / stats / 历史）
│   │   ├── files.py             # /api/files（preflight / convert）
│   │   └── main.py              # V0.1 端点（设备/测量/闭环/可视化…）
│   └── main.py                  # 应用入口（/ 重定向前端）
├── static/
│   ├── index.html               # 外壳（侧边栏 + 底部标签栏）
│   ├── css/app.css              # iOS 扁平设计令牌 + 组件
│   ├── vendor/three/            # three.js r187 vendored（ESM，含 sha512 溯源 README）
│   └── js/                      # api / router / icons / components / gamut-view / gamut-three / views/*
├── scripts/                     # 探针、冒烟、e2e、清理脚本
├── data/                        # 运行时数据（工艺 ICC、配方记录、补偿模型）
└── tests/                       # pytest 106 项（API 层 + 精度基线门禁 + 测量可靠性 + 样张 + fixtures）
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
| `/api/files/preflight` | POST | 预检：图像（嵌入 ICC / 模式 / DPI / 主导色）与 PDF（页数/色彩空间/专色/输出意图/缩略图） |
| `/api/files/convert` | POST | 经工艺 ICC 转换（CMYK 输出 TIFF + 软打样预览 PNG）；PDF 仅预检不支持转换 |

### QC 测量可靠性闭环（V0.5）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/qc/measurements/import` | POST | 导入测量导出文件（raw bytes；会话登记 instrument/condition M0-M2/calibrated_at/backing/repeat_mode）；每次导入全会话重分级 |
| `/api/qc/measurements` | GET | 会话列表 / 会话详情（`?session_id=`，可按 reliability 过滤） |
| `/api/qc/measurements` | DELETE | 删除会话 / 单条记录（删后重分级） |
| `/api/qc/ink-quality` | GET | GATF 油墨四指标 + Murray–Davies TVI（**仅消费 verified 且密度通道可用**） |
| `/api/qc/testform` | POST | A4 CMYK 验证样张（设备值直出 + 双点重复布局，PDF/TIFF + mm 级配对坐标表） |

**可靠性分级与数据流向**：每条记录输出 `verified / warning / rejected` + 原因 +
`density_ok` 通道标记。五条规则：①复测一致性（同标签 ΔE≤0.4）②纸白校验
（本征合理性 + 工艺 ICC 纸白比对）③交叉校验（Lab↔XYZ 跨条件探测 D50/D65 ×
2°/10°，全条件失配 → rejected；光谱↔Lab 积分复算）④密度/色度物理量程
⑤梯尺 3σ(MAD) 离群。**verified → QC 指标/ΔE 分量/补偿沉淀；warning → 仅展示；
rejected → 可查不可用**。verify 端点可携带 `measurement_session_id` 关联测量会话
（补偿门禁）。

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
  Murray–Davies 数值经课程实验组手算对拍。
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
- **V0.5（当前）**：可信测量闭环 —— 实验方法七要素审计（三份课程报告：
  复测缺失/校准未记录/转录链断裂/口径漂移均转化为产品对策）、CGATS.17 +
  Status-T 双解析器（编码容错 + CIE 积分自实现）、五条可靠性规则分级
  （verified/warning/rejected + density_ok 通道标记）、测量会话登记与导入向导、
  GATF 油墨四指标 + TVI 曲线 + GATF 色轮（只消费 verified）、A4 CMYK 双点
  重复样张生成器、ΔE 分量分解（dL/da/db/dC/dh/dE76）+ 解读文案、
  补偿模型 verified 门禁。实施记录见 `docs/V0.5-实施规划.md`

## 许可

[MIT](LICENSE)（第三方资产许可单独注明：测试 ICC 见
`tests/fixtures/icc/THIRD-PARTY-LICENSES.md`；内嵌 three.js r187 见
`static/vendor/three/`（MIT，含 sha512 溯源记录））

## 许可

[MIT](LICENSE)（第三方测试 ICC 资产的许可单独见 `tests/fixtures/icc/THIRD-PARTY-LICENSES.md`）
