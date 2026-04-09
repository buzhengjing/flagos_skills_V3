# Skills 概览

本文档整理了 FlagOS GPU 性能测试自动化框架中所有 Skill 的功能说明和执行顺序。

**支持双执行模式**：
- **Host 模式**：Claude Code 在宿主机，通过 `docker exec` 操作容器
- **Container 模式**：Claude Code 在容器内，直接执行命令

**支持多入口**：
- 已有容器 → 直接接入
- 已有镜像 → 创建容器
- README 链接 → 解析后创建容器

---

## 统一工作目录

**核心设计**：所有操作在统一挂载的 `/flagos-workspace` 目录下进行，宿主机可实时访问日志和结果。

```
宿主机: /data/flagos-workspace/<model_name>/
                      ↓ 挂载
容器内: /flagos-workspace/
    ├── scripts/              # 自动化脚本
    │   ├── benchmark_runner.py
    │   ├── performance_compare.py
    │   ├── operator_optimizer.py
    │   ├── operator_search.py
    │   ├── eval_monitor.py
    │   └── ...
    ├── results/              # 最终交付物
    │   ├── native_performance.json
    │   ├── flagos_performance.json
    │   ├── flagos_optimized.json
    │   ├── gpqa_native.json
    │   ├── gpqa_flagos.json
    │   ├── operator_config.json
    │   └── performance_compare.csv
    ├── traces/               # 每步留痕（JSON）
    ├── logs/                 # 运行日志
    ├── config/               # 使用的配置快照
    ├── perf/                 # 性能测试配置
    └── shared/
        └── context.yaml
```

---

## 工作流程图

### 新模型迁移发布

```
① container-preparation       容器准备（镜像/容器 + 本地权重检查 + 自动下载）
        ↓
② pre-service-inspection      环境检测（判定 env_type + flaggems 控制方式）
        ↓
③ service-startup             启动服务（验证初始环境可用）
        ↓
④ eval-comprehensive          快速精度评测（V1 基线 → V2 精度 → 算子调优直到精度达标）
        ↓
⑤ performance-testing         快速性能评测（V1 基线 → V2 性能 → 算子调优直到性能达标）
        ↓
⑥ flagos-release (image)      打包镜像（commit → tag → push Harbor）
        ↓
⑦ flagos-release (publish)    上传权重发布（ModelScope + HuggingFace）
        ↓
→ 报告整理收尾
```

**三版结果文件**：
- `native_performance.json` — V1 (Native，无 FlagGems)
- `flagos_performance.json` — V2 (FlagGems)
- `flagos_optimized.json` — V3 (Optimized FlagGems，仅 V2 不达标时产出)

**自动化**：步骤①~⑦全自动执行，零交互。仅网络失败时需用户介入：
- 网络失败时（pip 自动加阿里云镜像重试，其他操作询问代理）
- ⑥⑦打包发布凭证通过环境变量自动读取（Harbor 登录、`MODELSCOPE_TOKEN`、`HF_TOKEN`）

---

## Skills 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     主流程 (顺序执行)                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ① container-preparation    多入口容器准备（已有容器/镜像/README）     │
│         ↓                                                           │
│  ② pre-service-inspection   环境检测 + FlagGems 深度探测 + 报告       │
│         ↓                                                           │
│  ③ service-startup          启动服务（支持 native/flagos 模式切换）    │
│         ↓                                                           │
│  ④ eval-comprehensive       精度评测（GPQA + V1/V2 对比 + 算子排查）  │
│         ↓                                                           │
│  ⑤ performance-testing      性能测试（并发搜索+早停+自动对比）         │
│         ↓                                                           │
│  ⑥ flagos-release (image)   打包镜像（commit → tag → push Harbor）   │
│         ↓                                                           │
│  ⑦ flagos-release (publish) 上传权重发布（ModelScope + HuggingFace）  │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                     独立工具 (按需调用)                               │
├─────────────────────────────────────────────────────────────────────┤
│  operator-replacement       算子替换 + 分组二分搜索优化               │
│  component-install          组件安装/升级（FlagGems、FlagTree）       │
│  log-analyzer               日志分析 + 失败恢复指引                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Skills 详细说明

### ① flagos-container-preparation (多入口容器准备)

| 属性 | 说明 |
|------|------|
| **功能** | 自动识别入口类型（容器名/镜像/URL），检测 GPU，创建或接入容器 |
| **依赖** | 无 (流程起点) |
| **触发词** | `container preparation`, `prepare container`, `容器准备`, `环境准备` |

**三种入口**：

| 入口 | 用户提供什么 | 系统做什么 |
|------|-------------|-----------|
| 已有容器 | 容器名/ID | docker inspect → 验证 → 接入 |
| 已有镜像 | 镜像地址 + 模型信息 | docker run 创建 |
| README | URL 链接 | WebFetch → 解析 → docker pull + run |

---

### ② flagos-pre-service-inspection (环境检测 + 深度探测)

| 属性 | 说明 |
|------|------|
| **功能** | 执行模式检测 + 核心组件检查 + FlagGems 多维度深度探测 + 报告生成 |
| **依赖** | `flagos-container-preparation` |
| **触发词** | `pre-service inspection`, `inspect environment`, `服务前检查`, `环境检查` |

---

### ③ flagos-service-startup (服务启动 — 支持模式切换)

| 属性 | 说明 |
|------|------|
| **功能** | 生成启动命令、支持 native/flagos 模式切换、验证健康状态、失败自动恢复 |
| **依赖** | `flagos-pre-service-inspection` |
| **触发词** | `service startup`, `start service`, `启动服务`, `health check` |

**启动模式**：default（原始配置，验证初始环境）/ native（关闭 FlagGems）/ flagos（启用 FlagGems）

---

### ④ flagos-eval-comprehensive (统一精度评测)

| 属性 | 说明 |
|------|------|
| **功能** | 统一精度评测入口：本地快速评测（GPQA Diamond）+ 远端 flageval 正式评测 + V1 vs V2 精度对比（5% 阈值）+ 算子排查闭环 |
| **依赖** | `flagos-service-startup` |
| **触发词** | `精度评测`, `GPQA`, `fast gpqa`, `comprehensive eval`, `远端评测`, `FlagRelease`, `flageval` |

四个模块：
- **模块 A**：本地快速评测（fast_gpqa.py，GPQA Diamond 198 题）
- **模块 B**：远端 flageval 正式评测（6 个数据集，原 eval-correctness 功能）
- **模块 C**：V1 vs V2 精度对比（5% 阈值判定）
- **模块 D**：错误处理与算子排查（报错 → 算子替换 → 重启 → 重评）

---

### ⑤ flagos-performance-testing (性能测试 — 三版对比+优化触发)

| 属性 | 说明 |
|------|------|
| **功能** | 三版性能测试（V1 Native / V2 Full FlagGems / V3 Optimized），并发自动搜索+早停、自动优化触发 |
| **依赖** | `flagos-service-startup` |
| **触发词** | `性能测试`, `benchmark`, `vllm bench`, `吞吐量测试` |

**脚本**：
- `benchmark_runner.py`：测试入口，支持 `--strategy quick/fast/comprehensive/fixed` + 可选 `--final-burst`
- `performance_compare.py`：三版对比 + CSV 生成（`--format markdown` 标准三列表格输出）

---

### ⑥⑦ flagos-release (镜像打包 + 模型发布)

| 属性 | 说明 |
|------|------|
| **功能** | Docker 镜像 commit/tag/push Harbor + README 生成 + ModelScope/HuggingFace 上传 |
| **依赖** | `flagos-performance-testing` |
| **触发词** | `发布`, `镜像上传`, `镜像打包`, `模型发布`, `release`, `publish` |

---

### flagos-operator-replacement (算子替换 + 优化) — 独立工具

| 属性 | 说明 |
|------|------|
| **功能** | 被动排除（评测报错）+ 主动分组二分搜索优化（性能驱动） |
| **依赖** | 无 (可随时调用) |
| **触发词** | `operator replacement`, `replace operator`, `算子替换`, `算子优化` |

**脚本**：
- `operator_optimizer.py`：分组二分搜索引擎、算子列表自动发现
- `operator_search.py`：全自动搜索编排（next→toggle→restart→benchmark→update）

---

### flagos-log-analyzer (日志分析) — 独立工具

| 属性 | 说明 |
|------|------|
| **功能** | 分析日志，诊断问题，提供失败恢复指引 |
| **依赖** | 无 (可随时调用) |
| **触发词** | `log analysis`, `analyze logs`, `日志分析` |

---

## 自动化程度

### 无需人工介入的环节

| 环节 | 说明 |
|------|------|
| GPU 检测 | 自动检测 10 种 GPU 厂商 |
| 入口类型判断 | 自动识别容器名/镜像/URL |
| FlagGems 集成方式 | 运行时多维探测 |
| FlagGems 启停方法 | 从探测结果推导 |
| 性能对比判断 | 自动计算比例 |
| 是否需要算子优化 | 自动判断 < 80% 触发 |
| 算子优化搜索 | 全自动分组二分搜索 |
| 报告生成 | 自动生成 |

### 需要人工介入的环节

1. 网络失败时（pip 自动加阿里云镜像重试，其他操作询问代理）

**注意**：⑥⑦打包发布所需凭证（Harbor 登录、ModelScope token、HuggingFace token）均通过环境变量自动读取，无需人工提供。

---

## 数据流

```
┌──────────────────────────────┐
│ container-preparation (①)    │──写入──┐
│ (多入口: 容器/镜像/README)    │        │
└──────────────────────────────┘        ↓
                                ┌─────────────────┐
                                │ context.yaml    │
                                │ (共享上下文)     │
                                └─────────────────┘
                                        ↑
┌──────────────────────────────┐        │
│ pre-service-inspection (②)  │──追加──┤
│ + env_report.md              │        │
│ + flag_gems_detection.md     │        │
└──────────────────────────────┘        │
                                        ↑
┌──────────────────────────────┐        │
│ service-startup (③ default)  │──追加──┤
│ → 初始环境验证               │        │
└──────────────────────────────┘        │
         │                              ↑
         ↓                              │
┌──────────────────────────────┐        │
│ performance-testing (⑤)      │──追加──┘
│ + benchmark_runner.py        │
│ + performance_compare.py     │
│ → native_performance.json    │
│ → flagos_performance.json    │
│ → flagos_optimized.json      │
│ → performance_compare.csv    │
└──────────────────────────────┘
         │
         ↓ (< 80% 自动触发)
┌──────────────────────────────┐
│ operator-replacement         │
│ + operator_optimizer.py      │
│ + operator_search.py         │
│ → operator_config.json       │
└──────────────────────────────┘

独立工具:
┌──────────────────┐
│ log-analyzer     │
│ + 失败恢复指引    │
└──────────────────┘
```

---

## GPU 厂商支持

| 厂商 | 检测命令 | 可见设备环境变量 |
|------|----------|------------------|
| NVIDIA | `nvidia-smi` | `CUDA_VISIBLE_DEVICES` |
| 华为 (Ascend) | `npu-smi info` | `ASCEND_RT_VISIBLE_DEVICES` |
| 海光 (Hygon) | `hy-smi` | `HIP_VISIBLE_DEVICES` |
| 摩尔线程 | `mthreads-gmi` | `MUSA_VISIBLE_DEVICES` |
| 昆仑芯 | `xpu-smi` | `XPU_VISIBLE_DEVICES` |
| 天数 | `ixsmi` | `CUDA_VISIBLE_DEVICES` |
| 沐曦 | `mx-smi` | `CUDA_VISIBLE_DEVICES` |
| 清微智能 | `tsm_smi` | `TXDA_VISIBLE_DEVICES` |
| 寒武纪 | `cnmon` | `MLU_VISIBLE_DEVICES` |
| 平头哥 | - | `CUDA_VISIBLE_DEVICES` |

---

## 关键配置文件

| 文件 | 容器内路径 | 用途 |
|------|-----------|------|
| `context.yaml` | `/flagos-workspace/shared/context.yaml` | Skill 间共享上下文 |
| `perf_config.yaml` | `/flagos-workspace/perf/config/perf_config.yaml` | 性能测试配置 |
| `operator_config.json` | `/flagos-workspace/results/operator_config.json` | 算子优化状态 |
| `skills/*/SKILL.md` | 项目目录内 | Skill 定义文件 |

---

## 宿主机常用命令

```bash
# 实时查看服务日志
tail -f /data/flagos-workspace/<model>/logs/*.log

# 查看性能测试结果
cat /data/flagos-workspace/<model>/results/native_performance.json
cat /data/flagos-workspace/<model>/results/flagos_performance.json
cat /data/flagos-workspace/<model>/results/performance_compare.csv

# 查看精度评测结果
cat /data/flagos-workspace/<model>/results/gpqa_native.json
cat /data/flagos-workspace/<model>/results/gpqa_flagos.json

# 查看流程留痕
ls /data/flagos-workspace/<model>/traces/

# 查看评测进度
tail -f /data/flagos-workspace/<model>/logs/eval_gpqa_progress.log

# 查看算子优化状态
cat /data/flagos-workspace/<model>/results/operator_config.json

# 搜索错误日志
grep -ri "error" /data/flagos-workspace/<model>/logs/
```
