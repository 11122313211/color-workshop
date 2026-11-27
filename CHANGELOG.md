# 更新日志（Changelog）

本项目所有重要变更均记录于此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，并遵循语义化版本约定。

## [Unreleased]

### 新增（Added）
- **仓库工程化与 CI 维护**：新增 GitHub Actions 自动化测试工作流（`.github/workflows/ci.yml`，Ubuntu + Python 3.12）、`.editorconfig` 统一编码与缩进规范、`CHANGELOG.md` 及 README 实时 CI/技术栈徽章。
- **跨项目印前联动（`dcaeb29`）**：打通 PrintGuard 机读预检报告消费接口（`POST /api/files/preflight/printguard`）、工艺护照 ICC 指纹门禁与专色色域 $\Delta E_{00}$ 核验（`POST /api/files/spot-check`）。
- **PDF 渲染内容核验与签样门禁（`cc3e6e2`）**：基于 `pypdfium2` 72 DPI 光栅化新增空白页告警（`blank_page_suspect`）及 PDF/X-4 封装墨量漂移门禁（`ink_drift_suspect`）。
- **模块启动入口（`9a36700`）**：新增 `python -m app.main` 启动方式，并将默认服务端口统一调整为 `2354`（支持 `COLORFORGE_HOST` / `COLORFORGE_PORT` 配置）。

### 变更（Changed）
- **全项目审计与性能优化（`609c456`）**：加固存储层跨实例路径锁与原子持久化、向量化色彩核心算法并消除警告、完善 ICC 纸白校验/复测 QA/校准幂等与漂移公式、补齐路由流控与 N+1 查询优化，并修正前端 Display-P3 OETF 与内存泄漏。
- **隐私脱敏与文档重构（`136ca0c`）**：全仓库隐私脱敏检查并重写 `README.md`。

### 修复（Fixed）
- **工艺色域采样与凸包修复（`3dfb8b7`）**：修复工艺色域采样越界与凸包错位问题，并开放多参考色域与渲染意图切换。

---

## [0.11.0] - 2026-11-23

### 新增（Added）
- **归档优先生命周期（`2bba635`）**：工艺删除改为归档优先（只读可恢复，写操作统一返回 `409 PROCESS_ARCHIVED`）；显式永久清理支持派生数据删除、测量/评价解绑保留及签样孤儿标记。
- **数据完整性审计与修复**：新增完整性扫描与 Dry Run 预览/修复能力（API 端点、前端界面与 `scripts/audit_data.py` CLI）。
- **护照历史与签样引用**：保留版本化工艺护照历史快照并建立与签样记录的可追溯引用。
- **前端无障碍增强**：补充键盘焦点管理、语义化标签与窄屏响应式交互优化。

---

## [0.10.0] - 2026-11-23

### 新增（Added）
- **工艺护照与光谱证据闭环（`536f83e`）**：引入版本化不可变工艺护照（规范化 JSON SHA-256 指纹与 `valid/incomplete/stale/invalid` 状态机）、明确 `recipe_kind="device_values"` 分色语义、新增四光源（D50 / D65 / A / CIE F11，2°）工程同色异谱风险评估与签样护照门禁。

---

## [0.9.0] - 2026-11-18

### 新增（Added）
- **印前 P0 闭环（`1578b74`）**：新增 PDF 深度预检（字体真嵌入、TrimBox/BleedBox 出血、CTM 有效 DPI、标准开本）、PDF/X-4 结构封装（OutputIntent + XMP + 页面盒）、签样状态机与放行门禁，以及显示器校准档案与软打样可信门禁。

---

## [0.7.0] - 2026-11-15

### 新增（Added）
- **印刷校准闭环（`24b85a0`，含 V0.7–V0.8 能力）**：新增 ISO 12647-7 风格编号校准样张生成、双基准（工艺 ICC 预期 + ISO 12647-2 涂布纸固体参考）差距报告、Lab 有效网点反演校正曲线（ISO 10128 变体）、逐通道墨量修正建议与多轮收敛追踪。

---

## [0.6.0] - 2026-11-15

### 新增（Added）
- **印刷 QC 闭环后半环（`915b796`）**：补全 GATF 油墨指标与 Murray–Davies TVI / 反差率 $K$ / 叠印率 $T$、R1–R8 规则诊断引擎、双会话对比与趋势分析、主观评价检查单。

### 修复（Fixed）
- 修复网格容器内 `.card` 与 `stat-grid` 首卡上边距对齐偏差（`41a85ec`、`da25d36`）。

---

## [0.5.0] - 2026-11-15

### 新增（Added）
- **可信测量管线与五规则门禁（`9a8e8e4` – `b64fee8`）**：支持 CGATS.17 与 Status-T 密度 TSV 解析、五规则测量可靠性分级（`verified` / `warning` / `rejected`）、仅消费 `verified` 的密度与油墨质量面板、A4 双点验证样张生成器、$\Delta E$ 分量分解及补偿模型 `verified` 门禁。

### 修复（Fixed）
- 修复 `rejected` 复测隔离逻辑与工艺卡片指标行高对齐（`ef4aa84`、`b64fee8`）。

---

## [0.4.0] - 2026-11-15

### 新增（Added）
- **配方反查与 3D 色域升级（`6225c2a` – `3d197b0`）**：引入多线性插值精搜层（打破 8-bit 平台期）、工艺级 EMA 补偿模型沉淀与重置、本地 vendored Three.js r187 真 3D 色域体积（Qhull 凸包 + $L^*$ 切片 + WebGL 回退链）、$\Delta E$ 监控面板与多工艺色域对比，以及基于 `pypdf` / `pypdfium2` 的 PDF 预检与缩略图。

### 修复（Fixed）
- 修复 V0.3 遗留凸包索引错位问题（`3b63848`）。

---

## [0.3.0] - 2026-11-15

### 新增（Added）
- **工程底座与测试体系（`f9c8550` – `6a6077c`）**：建立工艺库与 ICC 视觉交互基建、去平台化测试体系（内置 MIT 测试 ICC 与精度基线回归门禁）、API 层集成测试，并补充 MIT 开源许可证。
