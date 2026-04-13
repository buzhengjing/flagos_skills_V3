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

### context.yaml 使用规则（多任务隔离）

- `shared/context.template.yaml` 是模板文件，仅用于 `setup_workspace.sh` 初始化容器，**禁止直接读写**
- 运行时 context 位于容器内 `/flagos-workspace/shared/context.yaml`，每个容器独立，互不干扰
- 读取 context：`docker exec <container> cat /flagos-workspace/shared/context.yaml`
- 写入 context：通过 `docker exec <container>` 在容器内操作
- 宿主机快照：`/data/flagos-workspace/<model>/config/context_snapshot.yaml`（只读归档，由步骤⑥和兜底同步写入）
- 宿主机最终状态：`/data/flagos-workspace/<model>/config/context_final.yaml`（全流程结束时回传）

### 会话恢复检测

初始化完成后，检测是否存在未完成的流程。通过 `docker exec <container> cat /flagos-workspace/shared/context.yaml` 读取容器内 context，如果 `workflow.all_done != true` 且 `container.name` 非空：

1. 运行 `diagnose_failure.py --json` 获取诊断：
   ```bash
   docker exec <container> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/diagnose_failure.py --json"
   ```
2. 输出诊断摘要给用户（中断位置、错误原因、恢复建议）
3. 根据诊断结果从中断点恢复（不从头重跑）

如果容器不存在或已停止，提示用户当前状态并询问是否重新开始。

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

**用户提供目标（容器名或镜像地址）+ 模型名后，①-⑥ 全自动执行，零交互。**
**自动识别**：含 `:` 或 `/` 的目标视为镜像地址，否则通过 `docker inspect --type=container` 判断是否为已有容器。模型路径自动搜索，无需手动指定。

```
① 容器准备           → 自动识别容器/镜像 + 模型权重搜索/下载 + 工具部署
② 环境检测           → inspect_env.py 场景分类 + FlagGems 集成分析
③ 启服务             → V1(native) + V2(flagos) 启动验证 → 异常自动 issue
④ 精度评测           → V1/V2 GPQA Diamond 对比 → 异常自动 issue
⑤ 性能评测           → V1/V2 4k1k benchmark 对比 → 异常自动 issue
⑥ 自动发布           → 打包 + 上传 → qualified 公开 / 不合格私有
```

### V1/V2 定义

- **V1**：不开启 flaggems 算子替换的版本，作为精度和性能基线。plugin 环境若关闭 flaggems 后无法启动服务，则标记"无 V1"，跳过 V1 基线测试
- **V2**：初始环境的 flaggems 状态（已开启部分或全部算子）。服务启动后以 `flaggems_enable_oplist.txt` 或 `gems.txt` 记录的算子为准

### 步骤③ 启服务异常处理

```
FlagGems 模式启动失败：
  → 保存日志 → 提交 operator-crash issue（含 flaggems.enable 代码）
  → 排除操作失误：native 模式也失败 → 环境问题，需人工介入
  → 确认是 FlagGems 问题 → workflow.service_ok = false
  → 跳过④⑤ → 直接到⑥发布（私有）
```

### 步骤④ 精度评测详情

精度全部完成后才进入性能测试，不交替进行。

```
1. 关闭 flaggems → 启动服务 → GPQA Diamond V1 精度基线 → 停服务
2. 开启 flaggems → 启动服务 → GPQA Diamond V2 精度
3. V1 vs V2 精度对比（偏差阈值 5%）
4. 出现问题时：
   ├── 服务崩溃 → 提交 operator-crash issue
   ├── 精度偏差 >5% → 提交 accuracy-degraded issue → 标记 workflow.accuracy_ok=false
   └── 继续进入⑤性能评测
```

### 步骤⑤ 性能评测详情

```
1. 关闭 flaggems → 启动服务 → benchmark 4k_input_1k_output V1 性能基线 → 停服务
2. 开启 flaggems → 启动服务 → benchmark V2 性能
3. V2/V1 性能对比，每个并发级别 ≥ 80%?
   ├── 全部达标 → 标记 workflow.performance_ok=true
   └── 不达标 → 提交 performance-degraded issue → 标记 workflow.performance_ok=false
4. 继续进入⑥发布
```

### 步骤⑥ 自动发布（flagos-release skill）

发布步骤通过 `flagos-release` skill 的宿主机工具统一执行，**禁止手动拼 docker commit/tag/push 命令**：

```bash
# 宿主机执行（不是 docker exec），先同步 context 到宿主机再调用
docker cp <container>:/flagos-workspace/shared/context.yaml /data/flagos-workspace/<model>/config/context_snapshot.yaml
python3 skills/flagos-release/tools/main.py --from-context /data/flagos-workspace/<model>/config/context_snapshot.yaml
```

工具自动完成：
1. 从 context_snapshot.yaml 读取 `workflow.qualified`（= service_ok AND accuracy_ok AND performance_ok）判定发布可见性（公开/私有）
2. docker commit → docker tag（自动生成标准命名）→ docker push Harbor
3. 生成 README（含评测结果、环境信息、启动命令）
4. 发布到 ModelScope / HuggingFace（SDK 优先，CLI 降级，Token 从环境变量读取）
5. 数据回传宿主机（docker cp results/traces/logs）

工具执行完成后，编排层仍需完成：
- 写入 `traces/06_release.json`（记录工具输出、发布 URL、耗时）
- 更新容器内 `/flagos-workspace/shared/context.yaml` 的 `image`、`release` 字段和 `workflow_ledger`
- 更新 `timing.steps.release`

**附加选项**：
- `--dry-run`：只验证配置，不实际执行（调试用）
- `--only-readme`：只生成 README，跳过镜像和上传步骤
- 详见 `skills/flagos-release/SKILL.md`

### native 场景工作流简化

纯原生环境无 FlagGems，工作流简化为：
①容器准备 → ②环境检测 → ③服务启动 → ④精度评测 → ⑤性能测试 → ⑥发布
跳过所有 FlagGems 相关步骤（toggle、V2 对比）。只产出单版结果。

### NV 重点场景

`vllm + flagtree + flaggems`（无 plugin）是当前 NV 模型发布的优先场景，推荐版本组合：`vllm>=0.7.3 + flaggems>=5.1.0 + flagtree>=0.5.0`。plugin 场景存在诸多问题，优先采用此方案。

---

## 环境场景定义

环境检测（步骤②）自动分类为以下场景之一，核心判定依据是 flaggems 是否存在（FlagOS 的核心组件）：

| env_type | 判定条件 | FlagGems 控制 | 算子列表来源 |
|----------|---------|--------------|-------------|
| `native` | 无 flaggems | 无 | 无 |
| `vllm_flaggems` | 有 flaggems，无 plugin | 代码注释/取消注释 | enable() 中的 txt 路径 |
| `vllm_plugin_flaggems` | 有 flaggems + plugin | 环境变量 | `/tmp/flaggems_enable_oplist.txt` |

FlagTree：仅记录 `has_flagtree`，不影响场景分类（FlagTree 是 triton 的替代，有无不影响 FlagGems 使用）。

### vllm_flaggems 场景关键差异

- FlagGems 开关通过 `toggle_flaggems.py --action enable/disable` 注释/取消注释代码实现
- 需要扫描代码找到 `import flag_gems` 和 `flag_gems.enable()` 调用
- 从 enable() 参数中提取算子记录 txt 路径（如 `/root/gems.txt`）
- 如果代码解析不到路径，启动服务后调用 `toggle_flaggems.py --action find-gems-txt` 搜索兜底
- 替换算子数和生效算子以该 txt 文件为准

### vllm_plugin_flaggems 场景

- FlagGems 开关通过内联环境变量控制（`USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true`）
- 算子列表以 `/tmp/flaggems_enable_oplist.txt` 为权威来源
- **注意**：plugin 环境关闭 flaggems 后可能无法启动服务，此时标记"无 V1"

---

## 自动决策规则（零交互默认值）

以下决策**直接执行，不询问用户**：

| 决策项 | 默认值 | 说明 |
|--------|--------|------|
| 目标识别 | 含 `:` 或 `/` → 镜像模式；否则 `docker inspect --type=container` 判断 | 避免镜像地址被误识别为同名容器 |
| 宿主机模型路径 | `check_model_local.py --no-download` 自动搜索 | 找到则挂载，未找到则容器内下载 |
| docker run | 按 GPU 模板自适应降级执行：NVIDIA 先用最小命令（`-itd --gpus=all --network=host -v`），authZ 拒绝则终止，其他失败再加完整参数重试 | 不需确认 |
| 精度评测 | 始终执行 V1 和 V2 | 不询问是否跳过 |
| FlagGems 仓库地址 | `https://github.com/FlagOpen/FlagGems.git` | 无需用户提供 |
| 性能目标 | 每个用例的每个并发级别均 ≥ 80% of V1 | 不询问"目标是多少" |
| pip install 模式 | `pip install .`（非 editable） | 避免 `-e .` 在容器中的问题 |
| pip 国内镜像 | `-i https://mirrors.aliyun.com/pypi/simple/` | pip 失败时自动加镜像重试 |
| 服务端口 | 默认 8000，启动前检测可用性，被占用则自动递增（+1 到 +10），不停止占用方 | 不询问端口号 |
| GPU 设备 | 启动前检测空闲 GPU（显存占用 <5%），仅使用空闲 GPU，不清理其他进程的 GPU 占用 | 不询问使用哪些卡 |
| Harbor 仓库地址 | `harbor.baai.ac.cn/flagrelease-public` | 无需用户提供 |
| 模型仓库命名 | `FlagRelease/{Model}-{vendor}-FlagOS` | 自动生成 |
| 仓库可见性 | 条件发布：qualified=true 公开 / 不合格私有 | 由 workflow 状态自动判定 |
| 容器内模型搜索路径 | `/data,/models,/root,/home,/workspace,/mnt,/opt` | 不询问搜索哪些路径 |
| 容器内模型下载目录 | 优先已挂载宿主机卷路径（/data > /mnt > /nfs > /share），fallback `/data/models/` | 避免写入 overlay |
| 镜像模式容器名冲突 | 追加时间戳后缀 `_MMDD_HHMM` 创建新容器 | 禁止复用已有容器，必须 docker run 新建 |

---

## 用户交互规则

**①-⑥ 全自动执行，零交互。** 网络失败自动尝试备选镜像源，全部失败则终止任务，不询问用户。

1. **网络失败**（详见"网络问题处理策略"）— pip 失败自动依次尝试阿里云/清华/腾讯镜像，其他网络操作自动重试一次，全部失败直接终止任务

**⑥ 打包发布**所需凭证均通过环境变量提供，脚本自动读取：
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
│   └── 06_release.json
│
├── logs/                                 # 运行日志
│   ├── pipeline.log                         # 全流程执行记录（人可读，tail -f 可跟踪）
│   ├── startup_default.log
│   ├── startup_native.log
│   ├── startup_flagos.log
│   ├── eval_gpqa_progress.log
│   ├── issues_startup.log               # 服务启动异常记录
│   ├── issues_accuracy.log              # 精度异常记录
│   └── issues_performance.log           # 性能不达标记录
│
└── config/                               # 使用的配置快照
    ├── perf_config.yaml
    ├── eval_config.yaml
    └── context_snapshot.yaml             # 流程结束时的完整 context
```

目录创建时机：容器准备阶段由 `setup_workspace.sh` 自动创建。

### 历史数据归档

`setup_workspace.sh` 在每次流程启动时自动检测上一轮产出数据。若 `results/`、`traces/`、`logs/` 任一非空，自动将其移入 `archive/<YYYYMMDD_HHMMSS>/`，确保当前运行从干净状态开始。

归档范围：
- 容器内：`results/`、`traces/`、`logs/` 整目录移入 `archive/<ts>/`，`context.yaml` 复制一份
- 宿主机：`/data/flagos-workspace/<model>/` 下同步归档

归档后目录结构示例：
```
/flagos-workspace/archive/
├── 20260409_151007/          ← 第一次运行
│   ├── results/
│   ├── traces/
│   ├── logs/
│   └── context.yaml
├── 20260410_093022/          ← 第二次运行
│   ├── results/
│   ├── traces/
│   ├── logs/
│   └── context.yaml
```

---

## Trace 留痕规范

**强制规则**：每个 Skill 完成后，Claude 必须在 `traces/` 下写入对应步骤的 trace JSON 文件。

**计时强制规则**：
- 每个 Skill 开始时记录 `timestamp_start`（ISO 8601），结束时记录 `timestamp_end` 和 `duration_seconds`
- 完成 trace 写入后，同步更新 `context.yaml` 的 `timing.steps.<step_name>` 字段
- 步骤①开始时额外写入 `timing.workflow_start`
- 步骤⑥完成时写入 `timing.workflow_end` 和 `timing.total_duration_seconds`

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
  },
  "_meta": {
    "step": "步骤编号（如 01_container_preparation）",
    "title": "步骤中文名称",
    "timestamp_start": "步骤开始时间 (ISO 8601)",
    "timestamp_end": "步骤结束时间 (ISO 8601)",
    "duration_seconds": "步骤耗时（秒）",
    "status": "执行状态: success（成功）/ failed（失败）/ skipped（跳过）",
    "actions": "该步骤中执行的关键操作列表",
    "actions[].action": "操作标识（如 docker_run / v1_eval / compare）",
    "actions[].command": "实际执行的完整命令字符串",
    "actions[].timestamp": "操作执行时间 (ISO 8601)",
    "actions[].status": "操作状态: success / failed",
    "actions[].output_summary": "关键输出摘要（不是全量 stdout）",
    "result_files": "该步骤产出的结果文件路径（相对于工作目录）",
    "context_updates": "该步骤写入 context.yaml 的字段及其值"
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
| ①容器准备 | `01_container_preparation.json` | docker run 命令（含完整参数）、权重搜索/下载、setup_workspace 部署结果 |
| ②环境检测 | `02_environment_inspection.json` | inspect_env.py 命令、场景分类结果、FlagGems 集成分析 |
| ③启服务 | `03_service_startup.json` | 启动命令、env vars、健康检查结果、端口、issue 提交记录（如有） |
| ④精度评测 | `04_quick_accuracy.json` | V1/V2 精度评测命令、精度结果、issue 提交记录 |
| ⑤性能评测 | `05_quick_performance.json` | V1/V2 性能测试命令、性能对比、issue 提交记录 |
| ⑥自动发布 | `06_release.json` | qualified 判定、commit/tag/push 命令、ModelScope/HuggingFace 上传 URL |

### Trace 写入方式

由 Claude 编排层通过 shell heredoc 写 JSON 到容器内 `/flagos-workspace/traces/` 目录，例如：

```bash
docker exec $CONTAINER bash -c "cat > /flagos-workspace/traces/01_container_preparation.json << 'TRACE_EOF'
{...trace JSON...}
TRACE_EOF"
```

---

## 工作流台账维护规范

**强制规则**：编排层在每个 Skill 开始和结束时，必须实时更新 `context.yaml` 的 `workflow_ledger.steps[]` 对应条目。

### 状态流转

```
pending → in_progress → success | failed | skipped
```

- `pending`：初始状态，尚未开始
- `in_progress`：Skill 开始执行时立即设置
- `success`：Skill 正常完成
- `failed`：Skill 执行失败（记录 `fail_reason`）
- `skipped`：因前置步骤失败等原因跳过（记录 `skip_reason`）

### 更新时机

| 时机 | 更新字段 |
|------|---------|
| Skill 开始 | `status: "in_progress"`, `started_at: <ISO 8601>` |
| Skill 成功 | `status: "success"`, `finished_at`, `duration_seconds`, `notes`（关键结果摘要） |
| Skill 失败 | `status: "failed"`, `finished_at`, `duration_seconds`, `fail_reason`（一句话原因） |
| Skill 跳过 | `status: "skipped"`, `skip_reason`（如 "service_startup_failed, 跳过精度评测"） |

### notes 字段示例

```
容器准备:  "容器 qwen3_flagos 就绪, 8x H20"
环境检查:  "vllm_flaggems 场景, flaggems=5.1.0, torch=2.9.0"
启服务:    "default+native+flagos 三模式均启动成功"
精度评测:  "V1=68.2%, V2=66.5%, 偏差1.7%, 达标"
性能评测:  "V2/V1 min ratio=85.3%, 达标"
发布:      "qualified=true, 公开发布, Harbor+ModelScope+HuggingFace"
```

### 与 trace / timing 的关系

- **台账**是全局概览：一眼看出所有步骤的执行状态
- **trace JSON**是单步详情：记录每个操作的命令、参数、输出
- **timing.steps**是纯计时：台账的 `duration_seconds` 与之对应
- **pipeline.log**由 `stream_filter.py` 从 Claude 输出自动提取，编排层无需手动写入
- 四者互补，不替代。遇到步骤完成时**台账、trace、timing 三个都要更新**

---

## 流水线执行日志规范

`logs/pipeline.log` 是面向人的全流程执行记录，支持 `tail -f` 实时跟踪。

**生成方式**：由 `prompts/stream_filter.py --pipeline-log` 在 Claude 进程外部自动生成，从 Claude 输出的文本中提取关键步骤信息。不依赖 Claude 在 auto 模式下"自觉"写入，程序化保证一定产出。

### 终端输出模式

`stream_filter.py` 支持两种终端输出模式：

| 模式 | 参数 | 说明 |
|------|------|------|
| 精简模式（默认） | 无需参数 | 只显示步骤标记、✓/✗ 结果、关键命令，过滤 Claude 自言自语和探测命令 |
| 详细模式 | `--verbose` | 显示全量输出（同旧版行为），用于调试 |

额外选项：
- `--no-color`：关闭 ANSI 颜色（默认通过 `isatty()` 自动检测，管道/重定向时自动关闭）
- `--pipeline-log PATH`：同时写入 pipeline.log

精简模式过滤规则：
- 过滤纯点号占位符（`.`）和空白行
- 过滤英文填充语（"Let me..."、"Continuing..."、"Good,..." 等 Claude 自言自语）
- 过滤纯英文句子（无中文字符的大写开头句子）
- 只显示关键 Bash 命令（工具脚本、docker 生命周期、nvidia-smi、vllm serve 等）
- 隐藏 docker exec 中的探测/写入命令（ls、find、cat >、heredoc、python3 -c 等）
- 隐藏 Read/Write/Edit/Glob/Grep/TaskCreate 等非关键工具调用
- 保留包含关键信号词的行（步骤、✓、✗、达标、env_type、精度、性能等）

**pipeline.log 不受模式影响**，始终按以下规则写入：
- `[步骤①]` ~ `[步骤⑥]` 格式的行 — 步骤开始/完成/失败/跳过
- `✓` / `✗` / `⚠` 开头的行 — 关键结果摘要
- 包含 `达标` / `不达标` / `qualified` / `ratio` / `偏差` 等关键词的行
- 流程开始/结束自动写入头尾分隔线和耗时统计

**对 Claude 输出的要求**：为确保 stream_filter.py 能正确提取，编排层输出文本时应遵循以下格式约定：
- 步骤开始：`[步骤①] 容器准备 — 开始`
- 步骤完成：`[步骤①] 容器准备 — 完成 (1m 9s)`
- 步骤失败：`[步骤③] 启服务 — 失败`
- 步骤跳过：`[步骤④] 精度评测 — 跳过`
- 关键结果：`✓ env_type=vllm_flaggems, flaggems=5.1.0`
- 异常事件：`✗ V2/V1 性能比 72.1% < 80%`

### 文件位置

- 宿主机：`/data/flagos-workspace/<model>/logs/pipeline.log`（由 stream_filter.py 写入）

### 输出格式示例

**流程开始**（步骤①开始前）：

```
[2026-04-09 15:10:07] ===== FlagOS 迁移流程开始 =====
  模型: Qwen/Qwen2.5-0.5B-Instruct
  容器: nv_gems_tree
  GPU: 8x NVIDIA H20-3e
```

**步骤开始**：

```
[2026-04-09 15:11:13] [步骤①] 容器准备 — 开始
```

**步骤完成**：

```
[2026-04-09 15:12:22] [步骤①] 容器准备 — 完成 (1m 9s)
  结果: 容器 nv_gems_tree 就绪, 8x H20-3e, 工具脚本已部署
```

**步骤失败**：

```
[2026-04-09 15:45:12] [步骤③] 启服务 — 失败
  原因: FlagGems 模式启动失败, CUDA error in softmax
  操作: 提交 operator-crash issue, 标记 workflow.service_ok=false
```

**步骤跳过**：

```
[2026-04-09 15:46:00] [步骤④] 精度评测 — 跳过
  原因: 服务启动失败, 无法执行评测
```

**异常事件与处理**（精度/性能不达标）：

```
[2026-04-09 16:30:05] [步骤④] 精度异常 — V2 偏差超阈值
  详情: V1=68.2%, V2=61.5%, 偏差=6.7% (阈值 5%)
  操作: 提交 accuracy-degraded issue, 标记 workflow.accuracy_ok=false
```

**流程结束**（步骤⑥完成后）：

```
[2026-04-09 17:45:00] ===== FlagOS 迁移流程结束 =====
  qualified: true | 公开发布
  精度: V1=68.2%, V2=66.5%, 偏差=1.7%
  性能: V2/V1 min ratio=85.3%
  总耗时: 2h 35m
```

### 格式规则

- 时间戳格式：`[YYYY-MM-DD HH:MM:SS]`
- 步骤标记：`[步骤①]` ~ `[步骤⑥]`（与 CLAUDE.md 工作流定义一致）
- 事件关键词：`开始` / `完成` / `失败` / `跳过` / `异常`
- 详情用 2 空格缩进
- 每个步骤之间空一行

### 与其他记录机制的关系

| 机制 | 用途 | 受众 |
|------|------|------|
| `logs/pipeline.log` | 全流程实时概览，`tail -f` 可跟踪 | 人 |
| `traces/*.json` | 单步详细留痕（命令、参数、输出） | 程序 / 审计 |
| `workflow_ledger` | context.yaml 中的结构化状态 | 编排层 / 下游 Skill |
| `logs/issues_*.log` | 问题专项日志（启动/精度/性能） | 人 / 问题排查 |

---

## 问题日志规范

**强制规则**：遇到服务启动异常、精度异常、性能不达标三类问题时，必须在写入 trace 的同时，将问题详情追加写入对应的 issue log 文件。

### 三个 issue log 文件

| 文件 | 写入时机 | 产出步骤 |
|------|---------|---------|
| `logs/issues_startup.log` | 服务启动失败、崩溃、超时 | ③启服务、④⑤中的模式切换启动 |
| `logs/issues_accuracy.log` | 精度偏差 >5%、评测报错、服务端错误 | ④精度评测 |
| `logs/issues_performance.log` | 任一并发级别 V2/V1 < 80% | ⑤性能评测 |

### 统一日志条目格式

```
[YYYY-MM-DD HH:MM:SS] <版本(V1/V2)> | <问题摘要>
  详情: <错误信息/数值/不达标指标>
  操作: <采取的措施>
  结果: <措施结果>
```

示例：
```
[2026-03-20 15:45:12] V2 | 服务启动失败 — OOM
  详情: CUDA out of memory, TP=4, max-model-len=32768
  操作: TP 翻倍至 8 重试
  结果: 启动成功

[2026-03-20 16:30:05] V2 | 精度偏差超阈值
  详情: V1=68.2%, V2=61.5%, 偏差=6.7% (阈值 5%)
  操作: 提交 accuracy-degraded issue, 标记 workflow.accuracy_ok=false
  结果: 继续进入⑤性能评测

[2026-03-20 17:15:33] V2 | 性能不达标
  详情: 4k→1k conc=64, V1=12500 TPS, V2=8900 TPS, ratio=71.2% (<80%)
  操作: 提交 performance-degraded issue, 标记 workflow.performance_ok=false
  结果: 继续进入⑥发布（私有）
```

### 写入方式

追加写入（`>>`），同一文件可积累多条记录：

```bash
docker exec $CONTAINER bash -c "cat >> /flagos-workspace/logs/issues_startup.log << 'ISSUE_EOF'
[2026-03-20 15:45:12] V2 | 服务启动失败 — OOM
  详情: CUDA out of memory, TP=4, max-model-len=32768
  操作: TP 翻倍至 8 重试
  结果: 启动成功
ISSUE_EOF"
```

### 与 trace 的关系

- issue log 是**面向人的快速查看**，`tail -f` 即可跟踪问题进展
- trace JSON 是**面向程序的完整留痕**，包含命令、时间戳、context_updates
- 两者互补，不替代：遇到问题时**两个都要写**

---

## 网络问题处理策略

### pip install 失败

按以下顺序自动尝试镜像源，**不询问用户**：

1. **第一次失败** → 阿里云镜像重试：
   ```bash
   pip install <package> -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
   ```
2. **阿里云也失败** → 清华镜像重试：
   ```bash
   pip install <package> -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn
   ```
3. **清华也失败** → 腾讯镜像重试：
   ```bash
   pip install <package> -i https://mirrors.cloud.tencent.com/pypi/simple/ --trusted-host mirrors.cloud.tencent.com
   ```
4. **全部失败** → 记录错误到 issue log，**直接终止当前任务**，不询问代理

### 其他网络操作失败（modelscope download、git clone、docker pull）

1. **第一次失败**且错误包含网络关键词（timeout、connection refused、DNS、SSL、Could not resolve host、Network unreachable）→ **自动重试一次**
2. **重试仍失败** → 记录错误到 issue log，**直接终止当前任务**，不询问代理

**规则**：
- 网络失败不询问用户，自动尝试备选镜像源后仍失败则终止
- pip 最多尝试 4 次（原始 + 3 个镜像），其他网络操作最多重试 1 次
- 终止时输出失败原因和已尝试的镜像源列表，便于用户自行排查

---

## 标准性能对比输出格式

使用 `python performance_compare.py --format markdown` 生成标准 markdown 表格：

```
| Test Case | Concurrency | V1 TPS | V2 TPS | V2/V1      |
| --------- | ----------- | ------ | ------ | ---------- |
| 1k→1k     | 256         | 17328  | 17511  | **101.1%** |
```

格式规则：
- TPS 列使用 Total token throughput（input + output）
- Test Case 使用简写 `1k→1k` 而非 `1k_input_1k_output`
- Ratio 列加粗显示
- 两版列：V1 (Native) / V2 (FlagGems)

---

## 最终报告格式

步骤⑥完成后输出最终迁移报告并收尾：

**交付物清单**：
- `results/` — 性能/精度结果文件
- `traces/` — 全流程执行留痕
- `logs/` — 运行日志（含 `pipeline.log` 全流程执行记录）
- `config/context_snapshot.yaml` — 流程结束时的完整 context 快照

### 容器产出同步到宿主机（强制）

**强制规则**：步骤⑥完成后、输出最终报告之前，必须将容器内 `/flagos-workspace/` 的产出文件同步到宿主机工作目录。容器的 `/flagos-workspace` 是 overlay 文件系统，不是宿主机挂载点，不同步则宿主机无数据。

```bash
CONTAINER=<container_name>
HOST_BASE=/data/flagos-workspace/<model>

# 同步三个产出目录
for dir in results traces logs; do
    docker cp ${CONTAINER}:/flagos-workspace/${dir}/. ${HOST_BASE}/${dir}/
done

# 同步 context 快照
docker cp ${CONTAINER}:/flagos-workspace/shared/context.yaml ${HOST_BASE}/config/context_snapshot.yaml
```

同步完成后验证宿主机文件数量与容器内一致。如果某个 Skill 中途失败需要人工介入，也应先执行此同步，避免已产出的数据丢失。

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

精度评测 (GPQA Diamond):
  V1: XX.X%
  V2: XX.X%
  V1 vs V2 偏差: X.XX% (阈值 5%)

性能对比 (4k_input_1k_output):
| Test Case | Conc | V1 TPS | V2 TPS | V2/V1     |
| --------- | ---- | ------ | ------ | --------- |
| 4k→1k     | 256  | XXXXX  | XXXXX  | **XX.X%** |

流程耗时:
  ①容器准备:          XXm XXs
  ②环境检测:          XXm XXs
  ③启服务:            XXm XXs
  ④精度评测:          XXm XXs
  ⑤性能评测:          XXm XXs
  ⑥自动发布:          XXm XXs

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
4. **容器内 `/flagos-workspace/shared/context.yaml` 是 Skill 间共享状态**，每个 Skill 完成后必须通过 `docker exec` 更新容器内的 context.yaml（禁止操作项目目录下的 `shared/context.template.yaml`）
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
10. **宿主机 mkdir 禁止使用花括号展开**。`mkdir -p /data/flagos-workspace/xxx/{a,b,c}` 会被 sandbox 拦截。必须拆成多条 `mkdir -p` 逐个创建，或通过 `setup_workspace.sh` 的第二参数统一创建宿主机目录
11. **流程不可中途终止**。精度不达标、性能不达标都不是终止流程的理由。编排层必须：
    - 写入对应的 issue log（`issues_accuracy.log` / `issues_performance.log`）
    - 标记 `workflow.xxx_ok=false`，继续下一步
    - 最终走到步骤⑥发布（`qualified=false` → 私有发布）
    - 唯一允许终止的情况：Claude API 本身不可用（非模型问题）
12. **workflow 状态字段必须与实际数据一致**。设置 `accuracy_ok` / `performance_ok` 时必须基于实际评测数据：
    - `accuracy_ok=true` 仅当 `eval.deviation <= eval.threshold`
    - `performance_ok=true` 仅当 `performance.min_ratio >= performance.target_ratio`
    - 禁止在数据不达标时设置 `ok=true`
13. **中间文件禁止写入项目源码目录**。执行计划、临时配置等中间文件只能写入模型工作目录（`/data/flagos-workspace/<model>/config/`）或容器内 `/flagos-workspace/config/`，禁止写入项目目录（如 `output/`、`prompts/`）
14. **工具脚本失败后必须读取错误文件**。工具脚本（fast_gpqa.py、benchmark_runner.py 等）异常退出时会自动写入 `/flagos-workspace/logs/_last_error.json`。编排层在检测到工具脚本非零退出码后，必须：
    - 读取 `_last_error.json` 获取结构化错误信息
    - 将错误同步到 `context.yaml` 的 `workflow.last_error` 字段
    - 根据错误类型决定后续操作（重试/跳过/继续）
15. **流程中断后自动诊断**。`run_pipeline.sh` 在 Claude 退出后会自动运行 `diagnose_failure.py`，将诊断结果打印到终端并保存到 `logs/failure_diagnosis.json`。新会话启动时应优先读取此文件了解中断原因
16. **编排层生成的 JSON 必须包含 `_meta` 字段说明**。Claude 通过 heredoc 写入的 JSON 文件（trace JSON、final_report.json 等）必须在顶层包含 `_meta` 对象，用中文说明关键字段含义，格式为 `{"字段名": "说明", ...}`。工具脚本（fast_gpqa.py、benchmark_runner.py、error_writer.py）已内置 `_meta` 输出，无需额外处理。所有消费方已通过 `_` 前缀约定自动跳过该字段

---

## 权限预配置说明

项目根目录下的 `settings.local.json` 是 Claude Code 的权限预批准配置。上方"自动初始化"步骤会在每次会话启动时自动部署，无需手动操作。

预批准的自动操作（无需每次确认）：
- 容器操作：`docker exec`、`docker cp`、`docker inspect`、`docker ps`、`docker start`、`docker logs`、`docker commit`、`docker tag`、`docker run`、`docker pull`、`docker push`
- 进程管理：`pkill`、`kill`
- 包管理：`pip install`、`pip3 install`、`modelscope download`、`modelscope upload`
- 健康检查：`curl -s http://localhost:*`
- 宿主机只读：`nvidia-smi`、`npu-smi`、`hostname`、`df`、`free`
- 工作目录：`/data/flagos-workspace/` 下的 mkdir、ls、cat、tail、find
- Git 操作：`git clone`
- 文件操作：`cp`、`ln -s`
- 发布上传：`docker push`、`modelscope upload`、`huggingface-cli upload`
