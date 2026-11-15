# 色彩工坊 · 印前色彩管理闭环平台

> 面向印前的色彩管理小软件 · V0.2（配方预测 + 工艺库 + 印前转换）
> 以 ΔE / CIEDE2000 为统一度量：**目标 LAB → 工艺 ICC 反查最优 CMYK 配方 → 打印实测 → 闭环修正**。

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
| 算法层 | lcms2（经 Pillow ImageCms）+ colour-science + coloraide | ICC CMM 正向预测、色差、色域采样、色适应 |
| 反查引擎 | 自研（`app/services/recipe.py`） | 粗查表 + 最近邻 + 坐标下降精搜，通道数泛化（CMYK/RGB/GRAY） |
| 后端 | FastAPI + JSON 落盘（`data/`） | API 服务，工艺库/配方记录重启不丢 |
| 前端 | 原生 ES Module + Canvas（无构建） | iOS 扁平风 SPA，hash 路由，亮/暗主题 |

## 项目结构

```
color-mgmt/
├── app/
│   ├── algorithms/core.py       # 色彩转换、色差、ICC、色域映射（编排封装层）
│   ├── ai/color_checker.py      # 色卡检测、闭环迭代（保留能力）
│   ├── models/schemas.py        # Pydantic 模型
│   ├── services/
│   │   ├── filedb.py            # JSON 落盘存储（原子写，多实例自动重载）
│   │   ├── processes.py         # 工艺库：ICC 上传/元数据/色域采样
│   │   ├── recipe.py            # LAB→CMYK 反查引擎 + 配方记录 + 实测验证
│   │   ├── files.py             # 印前预检 + 经工艺 ICC 转换（软打样）
│   │   ├── store.py             # 设备/测量/工作流（V0.1 保留）
│   │   └── visualization.py     # CIE 色度图、色域数据
│   ├── routers/
│   │   ├── processes.py         # /api/processes（CRUD + ICC 下载 + 色域）
│   │   ├── recipes.py           # /api/recipes（predict / verify / forward / 历史）
│   │   ├── files.py             # /api/files（preflight / convert）
│   │   └── main.py              # V0.1 端点（设备/测量/闭环/可视化…）
│   └── main.py                  # 应用入口（/ 重定向前端）
├── static/
│   ├── index.html               # 外壳（侧边栏 + 底部标签栏）
│   ├── css/app.css              # iOS 扁平设计令牌 + 组件
│   └── js/                      # api / router / icons / components / views/*
├── scripts/                     # 探针、冒烟、e2e、清理脚本
├── data/                        # 运行时数据（工艺 ICC、配方记录）
└── tests/                       # pytest（19 项）
```

## 快速开始

```bash
# 1. 安装依赖
pip install fastapi uvicorn colour-science coloraide pillow numpy

# 2. 启动
cd color-mgmt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. 访问
#   应用:    http://127.0.0.1:8765/          （自动跳转前端）
#   API 文档: http://127.0.0.1:8765/docs

# 4. 测试
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
| `/api/processes` | GET | 工艺列表 |
| `/api/processes/{id}` | GET/DELETE | 详情 / 删除（级联清理配方记录） |
| `/api/processes/{id}/icc` | GET | 下载 ICC 文件 |
| `/api/processes/{id}/gamut` | GET | 工艺色域采样（CIE xy 点集 + Lab 体积） |

### 配方预测
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/recipes/predict` | POST | 批量 LAB→CMYK 反查（返回配方/ΔE/迭代轨迹/预测数） |
| `/api/recipes/verify` | POST | 实测验证闭环（模型误差补偿 + 修正配方） |
| `/api/recipes/forward` | POST | CMYK→LAB 正查（与反查共用同一变换） |
| `/api/recipes` | GET | 配方历史（可按工艺过滤） |

### 印前文件
| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/files/preflight` | POST | 预检（嵌入 ICC / 模式 / DPI / 主导色 + 行动建议） |
| `/api/files/convert` | POST | 经工艺 ICC 转换（CMYK 输出 TIFF + 软打样预览 PNG） |

### V0.1 端点（保留，供工具箱使用）
`/api/devices`、`/api/measurements`、`/api/color-checker/detect`、`/api/convert`、
`/api/correction`、`/api/delta-e`（含 `/lab`）、`/api/closed-loop`、
`/api/visualization/gamut`、`/api/workflow`、`/api/health`、`/api/spaces`

## 精度说明

- 正向预测经 lcms2 CMM，LAB 输出为 8-bit 量化（L 步长 ≈0.39，a/b 步长 ≈1.0），
  预测 ΔE 不确定度约 ±0.4；ΔE < 0.5 时引擎提前停止精搜。
- 色域外目标不虚假收敛：返回最优可印配方并如实标注 ΔE（≤2 达标 / ≤5 可用 / >5 超差）。
- PCS 为 D50 Lab；前端 swatch 与正查 hex 均经 Bradford 色适应（D50→D65）转 sRGB 显示。

## 版本

- **V0.1**：显示校准闭环（闭环校正 / 色卡识别 / 色域可视化 / ΔE）
- **V0.2（当前）**：印前配方工作流 —— 工艺库、LAB→CMYK 反查引擎、实测验证闭环、
  印前预检与转换、iOS 扁平 UI 全面重构（亮/暗主题、渐进式披露）
