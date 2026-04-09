# FlagOS 自动化框架 — 项目级指令

> 此文件由 Claude Code 自动加载，提供 Skill 路由、工作流定义和自动决策规则。

---

## 自动初始化（每次会话启动时检查）

**在执行任何用户任务之前，先静默完成以下初始化**（不需要告知用户）：

检查 `.claude/settings.local.json` 是否存在，如果不存在则自动从项目根目录复制：

```bash
[ -f .claude/settings.local.json ] || (mkdir -p .claude && cp settings.local.json .claude/settings.local.json)
```

此操作将权限预批准配置部署到位，使后续 `docker exec`、`curl` 等命令无需反复确认。

---

## Skill 路由表

| 触发词 | Skill 名称 | SKILL.md 路径 |
|--------|-----------|---------------|
| 容器准备 / prepare container / 环境准备 | flagos-container-preparation | `skills/flagos-container-preparation/SKILL.md` |
| 环境检查 / inspect environment / 服务前检查 | flagos-pre-service-inspection | `skills/flagos-pre-service-inspection/SKILL.md` |
| 启动服务 / start service / 健康检查 | flagos-service-startup | `skills/flagos-service-startup/SKILL.md` |
| 性能测试 / benchmark / vllm bench | flagos-performance-testing | `skills/flagos-performance-testing/SKILL.md` |
| 算子替换 / operator replacement / 算子优化 | flagos-operator-replacement | `skills/flagos-operator-replacement/SKILL.md` |
| 精度评测 / eval correctness / accuracy test / 远端评测 / FlagRelease / flageval / 综合评测 / comprehensive eval / 本地评测 / quick 评测 / evalscope / GPQA | flagos-eval-comprehensive | `skills/flagos-eval-comprehensive/SKILL.md` |
| 日志分析 / analyze logs | flagos-log-analyzer | `skills/flagos-log-analyzer/SKILL.md` |
| 提交 issue / submit issue / report bug / 自动报告 | flagos-issue-reporter | `skills/flagos-issue-reporter/SKILL.md` |
| 组件安装 / install component / 安装 FlagGems / 安装 FlagTree / 升级 FlagGems / flag upgrade | flagos-component-install | `skills/flagos-component-install/SKILL.md` |
| 发布 / 镜像上传 / 镜像打包 / 模型发布 / release / publish / image upload / package image | flagos-release | `skills/flagos-release/SKILL.md` |

---

## 工作流（新模型迁移发布）

**用户提供容器/镜像 + 模型名后，①-⑤ 全自动执行，零交互。**

```
① 下载模型+容器准备 → 镜像/容器就绪 + 权重检查 + 环境检测 + 工具部署
② 启服务           → V1(native) + V2(flagos) 启动验证 → 异常自动 issue
③ 精度评测         → V1/V2 GPQA Diamond 对比 → 异常自动 issue + ≤3 轮算子优化
④ 性能评测         → V1/V2 4k1k benchmark 对比 → 异常自动 issue + ≤3 轮算子优化
⑤ 自动发布         → 打包 + 上传 → qualified 公开 / 不合格私有
```

### V1/V2/V3 定义

- **V1**：不开启 flaggems 算子替换的版本，作为精度和性能基线。plugin 环境若关闭 flaggems 后无法启动服务，则标记"无 V1"，跳过 V1 基线测试
- **V2**：初始环境的 flaggems 状态（已开启部分或全部算子），经过精度验证达标后的版本。服务启动后以 `flaggems_enable_oplist.txt` 或 `gems.txt` 记录的算子为准
- **V3**：在 V2 基础上经过性能算子调优后的版本。若 V2 性能已达标（≥80% of V1），则**不存在 V3**，报告中说明"V2 已达标，无需 V3"

### 步骤② 启服务异常处理

```
FlagGems 模式启动失败：
  → 保存日志 → 提交 operator-crash issue（含 flaggems.enable 代码）
  → 排除操作失误：native 模式也失败 → 环境问题，需人工介入
  → 确认是 FlagGems 问题 → workflow.service_ok = false
  → 跳过③④ → 直接到⑤发布（私有）
```

### 步骤③ 精度评测详情

精度全部完成后才进入性能测试，不交替进行。

```
1. 关闭 flaggems → 启动服务 → GPQA Diamond V1 精度基线 → 停服务
2. 开启 flaggems → 启动服务 → GPQA Diamond V2 精度
3. V1 vs V2 精度对比（偏差阈值 5%）
4. 出现问题时自动处理：
   ├── 服务崩溃 → 提交 operator-crash issue → diagnose_ops.py 定位 → 禁用 → 重启重测
   ├── 精度偏差 >5% → 提交 accuracy-degraded issue → 算子优化
   │   优化范围：仅在 gems.txt 记录的已替换算子中排查
   │   控制方式：plugin 用 BLACKLIST 环境变量 / 非 plugin 用 toggle_flaggems.py
   └── 最多 3 轮优化，超限标记 workflow.accuracy_ok=false，进入④
5. 3 轮内达标 → workflow.accuracy_ok=true（即使提交了 issue 也算合格）
```

### 步骤④ 性能评测详情

```
1. 关闭 flaggems → 启动服务 → benchmark 4k_input_1k_output V1 性能基线 → 停服务
2. 开启 flaggems（使用③精度达标后的算子列表）→ 启动服务 → benchmark V2 性能
3. V2/V1 性能对比，每个并发级别 ≥ 80%?
   ├── 全部达标 → 结束，当前即为最终版本（不存在 V3）
   └── 不达标 → 提交 performance-degraded issue → 算子优化
                优化范围：仅在 gems.txt 记录的已替换算子中排查
                → 最多 3 轮优化，超限标记 workflow.performance_ok=false
                → 3 轮内达标 → workflow.performance_ok=true
4. 记录禁用算子及原因（性能不佳）
```

### 步骤⑤ 发布条件判定

```
qualified = service_ok AND accuracy_ok AND performance_ok

if qualified:
    publish.private = false   → 公开发布
else:
    publish.private = true    → 私有发布（记录不达标原因）
```

### native 场景工作流简化

纯原生环境无 FlagGems，工作流简化为：
①容器准备 → ②服务启动 → 精度评测 → 性能测试 → 发布
跳过所有 FlagGems 相关步骤（toggle、V2 对比、算子优化）。只产出单版结果。

### NV 重点场景

`vllm + flagtree + flaggems`（无 plugin）是当前 NV 模型发布的优先场景，推荐版本组合：`vllm>=0.7.3 + flaggems>=5.1.0 + flagtree>=0.5.0`。plugin 场景存在诸多问题，优先采用此方案。

---

## 环境场景定义

环境检测（步骤①）自动分类为以下场景之一，核心判定依据是 flaggems 是否存在（FlagOS 的核心组件）：

| env_type | 判定条件 | FlagGems 控制 | 算子列表来源 | 算子优化 |
|----------|---------|--------------|-------------|---------|
| `native` | 无 flaggems | 无 | 无 | 跳过 |
| `vllm_flaggems` | 有 flaggems，无 plugin | 代码注释/取消注释 | enable() 中的 txt 路径 | `toggle_flaggems.py --action modify-enable` |
| `vllm_plugin_flaggems` | 有 flaggems + plugin | 环境变量 | `/tmp/flaggems_enable_oplist.txt` | 黑白名单环境变量 |

FlagTree：仅记录 `has_flagtree`，不影响场景分类（FlagTree 是 triton 的替代，有无不影响 FlagGems 使用）。

### vllm_flaggems 场景关键差异

- FlagGems 开关通过 `toggle_flaggems.py --action enable/disable` 注释/取消注释代码实现
- 需要扫描代码找到 `import flag_gems` 和 `flag_gems.enable()` 调用
- 从 enable() 参数中提取算子记录 txt 路径（如 `/root/gems.txt`）
- 如果代码解析不到路径，启动服务后调用 `toggle_flaggems.py --action find-gems-txt` 搜索兜底
- 算子优化通过 `toggle_flaggems.py --action modify-enable` 脚本化修改代码
- 替换算子数和生效算子以该 txt 文件为准

### vllm_plugin_flaggems 场景

- FlagGems 开关通过内联环境变量控制（`USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true`）
- 算子优化通过 `VLLM_FL_FLAGOS_BLACKLIST` / `VLLM_FL_FLAGOS_WHITELIST` 环境变量
- 算子列表以 `/tmp/flaggems_enable_oplist.txt` 为权威来源
- **注意**：plugin 环境关闭 flaggems 后可能无法启动服务，此时标记"无 V1"

---

## 自动决策规则（零交互默认值）

以下决策**直接执行，不询问用户**：

| 决策项 | 默认值 | 说明 |
|--------|--------|------|
| docker run | 按 GPU 模板自动生成并执行 | 不需确认 |
| 精度评测 | 始终执行 V1 和 V2 | 不询问是否跳过 |
| 算子搜索未达标 | 自动继续（上限 3 轮，超限标记不合格进入下一步） | 不询问是否继续 |
| FlagGems 仓库地址 | `https://github.com/FlagOpen/FlagGems.git` | 无需用户提供 |
| 性能目标 | 每个用例的每个并发级别均 ≥ 80% of V1 | 不询问"目标是多少" |
| pip install 模式 | `pip install .`（非 editable） | 避免 `-e .` 在容器中的问题 |
| pip 国内镜像 | `-i https://mirrors.aliyun.com/pypi/simple/` | pip 失败时自动加镜像重试 |
| 服务端口 | 从 README/容器配置中提取 | 不询问端口号 |
| GPU 设备 | 使用全部可见 GPU | 不询问使用哪些卡 |
| Harbor 仓库地址 | `harbor.baai.ac.cn/flagrelease-public` | 无需用户提供 |
| 模型仓库命名 | `FlagRelease/{Model}-{vendor}-FlagOS` | 自动生成 |
| 仓库可见性 | 条件发布：qualified=true 公开 / 不合格私有 | 由 workflow 状态自动判定 |

---

## 用户交互规则

**①-⑤ 全自动执行，零交互。** 全流程仅网络失败时需要用户介入：

1. **网络失败**（详见"网络问题处理策略"）— pip 失败先自动加阿里云镜像重试，其他网络操作失败或 pip 镜像也失败时询问代理

**⑤ 打包发布**所需凭证均通过环境变量提供，脚本自动读取：
- Harbor：`HARBOR_USER` / `HARBOR_PASSWORD` 环境变量（脚本自动登录，未设置则需手动 `docker login`）
- ModelScope：`MODELSCOPE_TOKEN` 环境变量
- HuggingFace：`HF_TOKEN` 环境变量
- GitHub Issue：`GITHUB_TOKEN` 环境变量（issue 自动提交，需 `public_repo` 权限）

---

## 工具脚本部署

容器准备阶段（步骤①完成后），通过 `setup_workspace.sh` 一次性部署所有工具：

```bash
# 宿主机执行，一次性复制所有脚本到容器
bash skills/flagos-container-preparation/tools/setup_workspace.sh $CONTAINER
```

部署的脚本清单：
- `inspect_env.py` — 环境检查（替代 10+ 次 docker exec）
- `toggle_flaggems.py` — FlagGems 开关切换（替代 sed）
- `wait_for_service.sh` — 服务就绪检测（指数退避）
- `benchmark_runner.py` — 性能测试
- `performance_compare.py` — 性能对比
- `operator_optimizer.py` — 算子优化
- `operator_search.py` — 算子搜索编排
- `diagnose_ops.py` — 算子快速诊断（崩溃日志解析、精度分组测试、性能热点预扫描）
- `eval_monitor.py` — 评测监控
- `install_component.py` — 组件统一安装/升级/卸载（FlagGems 三级降级、FlagTree 委托）
- `install_flagtree.sh` — FlagTree 安装/卸载/验证（支持 11 个后端）
- `issue_reporter.py` — 问题自动收集/格式化/提交（五种 issue 类型，三级降级提交）
- `log_analyzer.py` — 日志分析与诊断（错误分类、服务状态推断、FlagGems 检测）

---

## 宿主机工作目录结构

宿主机 `/data/flagos-workspace/<model>/` 挂载到容器 `/flagos-workspace`，统一使用四个子目录：

```
/data/flagos-workspace/<model>/          ← 挂载到容器 /flagos-workspace
├── results/                              # 最终交付物
│   ├── native_performance.json              # V1 性能
│   ├── flagos_performance.json              # V2 性能
│   ├── flagos_optimized.json                # V3 性能（仅 V2 不达标时产出）
│   ├── ops_list.json
│   ├── performance_compare.csv              # 性能对比
│   ├── gpqa_native.json                     # V1 精度 (GPQA Diamond)
│   ├── gpqa_flagos.json                     # V2 精度 (GPQA Diamond)
│   ├── eval_result.json                     # 远端评测结果（可选）
│   └── release_info.json                    # 发布结果（可选）
│
├── traces/                               # 每步留痕（JSON）
│   ├── 01_container_preparation.json
│   ├── 02_environment_inspection.json
│   ├── 03_service_startup.json
│   ├── 04_quick_accuracy.json
│   ├── 05_quick_performance.json
│   ├── 06_image_package.json              # 可选
│   └── 07_publish.json                    # 可选
│
├── logs/                                 # 运行日志
│   ├── startup_default.log
│   ├── startup_native.log
│   ├── startup_flagos.log
│   └── eval_gpqa_progress.log
│
└── config/                               # 使用的配置快照
    ├── perf_config.yaml
    ├── eval_config.yaml
    └── context_snapshot.yaml             # 流程结束时的完整 context
```

目录创建时机：容器准备阶段由 `setup_workspace.sh` 自动创建。

---

## Trace 留痕规范

**强制规则**：每个 Skill 完成后，Claude 必须在 `traces/` 下写入对应步骤的 trace JSON 文件。

**计时强制规则**：
- 每个 Skill 开始时记录 `timestamp_start`（ISO 8601），结束时记录 `timestamp_end` 和 `duration_seconds`
- 完成 trace 写入后，同步更新 `context.yaml` 的 `timing.steps.<step_name>` 字段
- 步骤①开始时额外写入 `timing.workflow_start`
- 步骤⑤完成时写入 `timing.workflow_end` 和 `timing.total_duration_seconds`

### Trace JSON 统一格式

```json
{
  "step": "01_container_preparation",
  "title": "容器准备",
  "timestamp_start": "2026-03-20T15:30:00",
  "timestamp_end": "2026-03-20T15:32:00",
  "duration_seconds": 120,
  "status": "success | failed | skipped",
  "actions": [
    {
      "action": "docker_run",
      "command": "docker run -d --name xxx --gpus all ...",
      "timestamp": "2026-03-20T15:30:05",
      "status": "success",
      "output_summary": "Container abc123 started"
    }
  ],
  "result_files": ["results/native_performance.json"],
  "context_updates": {
    "container.name": "xxx",
    "gpu.count": 8
  }
}
```

**字段说明**：
- `actions[]`: 该步骤中执行的每个关键操作
- `command`: 实际执行的完整命令字符串
- `output_summary`: 关键输出摘要（不是全量 stdout）
- `result_files`: 该步骤产出的结果文件路径（相对于工作目录）
- `context_updates`: 该步骤写入 context.yaml 的字段

### 每步 trace 记录内容

| 步骤 | trace 文件 | 记录的 actions |
|------|-----------|----------------|
| ①下载模型+容器准备 | `01_container_preparation.json` | docker run 命令（含完整参数）、权重下载、环境检测、setup_workspace 部署结果 |
| ②启服务 | `02_service_startup.json` | 启动命令、env vars、健康检查结果、端口、issue 提交记录（如有） |
| ③精度评测 | `03_accuracy_eval.json` | V1/V2 精度评测命令、精度结果、算子调优记录、issue 提交记录 |
| ④性能评测 | `04_performance_eval.json` | V1/V2 性能测试命令、性能对比、算子搜索记录、issue 提交记录 |
| ⑤自动发布 | `05_release.json` | qualified 判定、commit/tag/push 命令、ModelScope/HuggingFace 上传 URL |

### Trace 写入方式

由 Claude 编排层通过 shell heredoc 写 JSON 到容器内 `/flagos-workspace/traces/` 目录，例如：

```bash
docker exec $CONTAINER bash -c "cat > /flagos-workspace/traces/01_container_preparation.json << 'TRACE_EOF'
{...trace JSON...}
TRACE_EOF"
```

---

## 网络问题处理策略

### pip install 失败

1. **第一次失败** → 自动加阿里云镜像重试：
   ```bash
   pip install <package> -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
   ```
2. **镜像也失败** → 询问用户代理地址
3. 用户提供代理后写入 context.yaml `network` 字段，后续自动复用

### 其他网络操作失败（modelscope download、git clone、docker pull）

1. **第一次失败**且错误包含网络关键词（timeout、connection refused、DNS、SSL、Could not resolve host、Network unreachable）→ **立即询问用户**代理地址
2. 用户提供代理后，设置环境变量重试：
   - 容器内：`docker exec -e http_proxy=xxx -e https_proxy=xxx`
3. 将代理配置写入 context.yaml `network` 字段，后续网络下载操作自动复用，不再重复询问
4. **下载完成后立即关闭代理**（`unset http_proxy https_proxy no_proxy`），避免代理影响后续本地服务访问（如 localhost API 调用）

**禁止行为**：
- 不要在网络失败后反复重试同一操作（pip 镜像重试除外）
- 识别到网络问题就停，问用户，拿到代理再继续

---

## 标准性能对比输出格式

使用 `python performance_compare.py --format markdown` 生成标准 markdown 表格：

```
| Test Case | Concurrency | V1 TPS | V3 TPS | V3/V1      | V2 TPS     | V2/V1      |
| --------- | ----------- | ------ | ------ | ---------- | ---------- | ---------- |
| 1k→1k     | 256         | 17328  | 16800  | **97.0%**  | 17511      | **101.1%** |
```

格式规则：
- TPS 列使用 Total token throughput（input + output）
- Test Case 使用简写 `1k→1k` 而非 `1k_input_1k_output`
- Ratio 列加粗显示
- 三版列：V1 (Native) / V3 (Optimized FlagGems) / V2 (Full FlagGems)
- 当 V3 = V2（全量已达标）时，V3 列显示 "= V2"

---

## 最终报告格式

步骤⑤完成后输出最终迁移报告并收尾：

**交付物清单**：
- `results/` — 性能/精度结果文件
- `traces/` — 全流程执行留痕
- `logs/` — 服务和评测运行日志
- `config/context_snapshot.yaml` — 流程结束时的完整 context 快照

**报告同时保存两份**：容器 `/root/flagos_report/` + 宿主机 `/data/flagos-workspace/<model>/results/`

```
FlagOS 迁移报告
========================================
模型: <model_name>
GPU: <gpu_count>x <gpu_type>
容器: <container_name>
环境: <env_type>

算子状态:
  V2 算子数: XX 个
  最终启用: XX 个（V2 达标则同 V2，否则为 V3）
  禁用算子: <op1>, <op2>, ...
  禁用原因:
    服务崩溃: <op1> (CUDA error)
    精度问题: <op2> (精度偏差 >5%)
    性能问题: <op3> (禁用后性能提升 +XX%)

精度评测 (GPQA Diamond):
  V1: XX.X%
  V2: XX.X%
  V1 vs V2 偏差: X.XX% (阈值 5%)

性能对比 (4k_input_1k_output):
| Test Case | Conc | V1 TPS | V2 TPS | V2/V1     |
| --------- | ---- | ------ | ------ | --------- |
| 4k→1k     | 256  | XXXXX  | XXXXX  | **XX.X%** |
（若存在 V3，增加 V3 列）

流程耗时:
  ①下载模型+容器准备:  XXm XXs
  ②启服务:            XXm XXs
  ③精度评测:          XXm XXs
  ④性能评测:          XXm XXs
  ⑤自动发布:          XXm XXs

发布信息:
  Harbor 镜像: <full_harbor_tag>
  ModelScope: <modelscope_url>
  HuggingFace: <huggingface_url>
  发布方式: 公开 / 私有
  qualified: true / false

结论: qualified(公开发布) / 不合格(私有发布)
========================================
```

---

## 关键约束

1. **性能测试只能通过 `benchmark_runner.py` 执行**，禁止直接运行 `vllm bench serve`
2. **FlagGems 开关只能通过 `toggle_flaggems.py` 切换**，禁止手动 sed
3. **FlagGems/FlagTree 安装只能通过 `install_component.py` 执行**，禁止手动 pip install flag-gems 或 pip install flagtree
4. **所有操作在 `/flagos-workspace` 目录下执行**，产出文件按类型分目录：`results/`（交付物）、`traces/`（留痕）、`logs/`（日志）、`config/`（配置快照）
4. **context.yaml 是 Skill 间共享状态**，每个 Skill 完成后必须更新
5. **每个 Skill 完成后必须写入对应的 trace JSON**，记录实际执行的命令、参数和关键输出
6. **禁止添加 SKILL.md 未记录的 vLLM/sglang 启动参数**（如 `--enforce-eager`、`--disable-log-stats` 等），遇到启动问题应分析日志找根因，而非猜测参数绕过
7. **精度评测和性能测试严禁同时进行**。必须等一个完全结束后再启动另一个。并发执行会互相抢占 GPU 资源，导致两边结果都不可信。启动前必须检查是否有正在运行的评测/测试进程
8. **性能达标判定粒度：每个用例的每个并发级别**。不是只看平均值或最佳并发，而是 `performance_compare.py` 中所有 ratio 的最小值 ≥ 80% 才算达标。包括 quick 模式也遵循此规则
8. **算子列表以 `flaggems_enable_oplist.txt` 为唯一权威来源**。每次服务启动后必须检查该文件（默认 `/tmp/flaggems_enable_oplist.txt`）：
   - **文件存在且有内容** → FlagGems 实际在运行，以此文件内容作为当前生效的算子列表
   - **文件不存在或为空** → FlagGems 未启用，不依赖任何缓存的算子列表
   - 每次 FlagGems 重新启动都会**重新生成**此文件，内容反映 blacklist 等配置生效后的实际结果
   - 如果启动模式为 native 但文件残留 → 是上次 flagos 的旧数据，不可作为当前算子列表
   - 所有后续操作（算子替换、搜索、性能对比、报告生成）中的"当前算子列表"均以此文件为准
9. **容器内 Python 必须用 conda 环境**。所有 `docker exec` 中的 python3/pip 命令必须加 `PATH=/opt/conda/bin:$PATH` 前缀，禁止依赖容器默认 `/usr/bin/python3`（系统 Python 缺少 torch/requests/yaml 等包）

---

## 权限预配置说明

项目根目录下的 `settings.local.json` 是 Claude Code 的权限预批准配置。上方"自动初始化"步骤会在每次会话启动时自动部署，无需手动操作。

预批准的自动操作（无需每次确认）：
- 容器操作：`docker exec`、`docker cp`、`docker inspect`、`docker ps`、`docker start`、`docker logs`、`docker commit`、`docker tag`、`docker run`、`docker pull`
- 进程管理：`pkill`、`kill`
- 包管理：`pip install`、`pip3 install`、`modelscope download`
- 健康检查：`curl -s http://localhost:*`
- 宿主机只读：`nvidia-smi`、`npu-smi`、`hostname`、`df`、`free`
- 工作目录：`/data/flagos-workspace/` 下的 mkdir、ls、cat、tail、find
- Git 操作：`git clone`
- 文件操作：`cp`、`ln -s`

**保留需要用户确认的操作**（对外发布类）：
- `docker push` — 推送镜像到外部仓库
- `modelscope upload` — 上传模型到 ModelScope
- `huggingface-cli upload` — 上传模型到 HuggingFace
