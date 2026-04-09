---
name: flagos-performance-testing
description: 三版性能基准测试（V1 Native / V2 Full FlagGems / V3 Optimized FlagGems），四档策略（quick/fast/comprehensive/fixed）、可选 final-burst、per-test-case 超时、标准 markdown 输出格式
version: 7.1.0
triggers:
  - 性能测试
  - benchmark
  - vllm bench
  - 吞吐量测试
  - performance test
depends_on:
  - flagos-service-startup
next_skill: null
provides:
  - native_perf.result_path
  - native_perf.output_throughput
  - native_perf.total_throughput
  - flagos_full_perf.result_path
  - flagos_optimized_perf.result_path
---

# 性能测试 Skill

支持三版自动化性能测试：V1 (Native) → V2 (Full FlagGems) → V3 (Optimized FlagGems)（如需优化），标准 markdown 三列表格输出。

**四档测试策略**（`--strategy` 参数）：

| strategy | 含义 | 用例选择 | 并发行为 | 样本量 |
|----------|------|----------|----------|--------|
| `quick` | 烟雾测试 | 只跑 `4k_input_1k_output` + max | 预热 2 请求 + 所有 levels 到 256，不早停 + final-burst | `num_prompts=concurrency` |
| `fast` | 饱和即停（默认） | 所有 enabled 用例 | 按 `early_stop` 配置决定 | `num_prompts=concurrency` |
| `comprehensive` | 全跑 | 所有 enabled 用例 | 所有并发全跑，强制不早停 | `num_prompts=concurrency` |
| `fixed` | 固定并发 | 只跑有 `fixed_concurrency` 的用例 | 只跑配置的固定并发级别 | `num_prompts=concurrency` |

**所有档统一 `num_prompts=concurrency`**，因此 quick 产出的 `4k_input_1k_output` 数据可直接复用，全量测试无需重跑该用例。

**策略选择**：在流程开始前询问用户选择 strategy，一旦选定，整个流程的所有性能测试统一使用同一策略。

**Fixed 策略**：用于快速测试特定场景，跳过并发搜索。在 `perf_config.yaml` 中为测试用例添加 `fixed_concurrency` 字段即可：

```yaml
- name: "1k_input_1k_output"
  input_len: 1024
  output_len: 1024
  enabled: true
  fixed_concurrency: 64  # fixed 策略时只跑并发 64

- name: "32k_input_1k_output"
  input_len: 32768
  output_len: 1024
  enabled: true
  fixed_concurrency: 1   # fixed 策略时只跑并发 1
```

**Final Burst**：默认不跑。用户显式传入 `--final-burst` 才追加无限制并发大规模测试。一旦用户选择了 `--final-burst`，后续同流程的所有性能测试都加此 flag。

**三版结果文件**：
- `native_performance.json` — V1 (Native，无 FlagGems)
- `flagos_performance.json` — V2 (Full FlagGems，全量算子)
- `flagos_optimized.json` — V3 (Optimized FlagGems，≥80% 组合，如需优化才产出)

**工具脚本**（已由 setup_workspace.sh 部署到容器）:
- `benchmark_runner.py` — 性能测试（`--strategy quick/fast/comprehensive/fixed` + 可选 `--final-burst`）
- `performance_compare.py` — 性能对比（`--format markdown` 标准三列表格输出）

---

## 强制约束

**只能通过 `benchmark_runner.py` 执行性能测试**，禁止直接运行 `vllm bench serve`。

**启动前互斥检查**：性能测试启动前，必须确认没有正在运行的精度评测进程。并发执行会互相抢占 GPU 资源，导致结果不可信。

```bash
# 检查是否有评测进程在运行（eval_aime.py / eval_erqa.py / eval_monitor.py）
docker exec $CONTAINER bash -c "pgrep -f 'eval_aime\|eval_erqa\|eval_monitor' && echo 'BLOCKED: 评测进程运行中，等待结束' && exit 1 || echo 'OK: 无评测进程'"
```

如果检测到评测进程，**必须等待其结束后再启动性能测试**，禁止强杀评测进程。

**策略触发规则**：
- 用户说"快速测试"/"走通流程"/"smoke test"/"验证流程"→ `--strategy quick`
- 用户说"完整测试"/"全量"/"所有并发"→ `--strategy comprehensive`
- 用户说"只测试特定场景"/"固定并发"/"1k-1k-64"→ `--strategy fixed`
- 默认（或用户说"正常测试"/"标准"）→ `--strategy fast`

---

# Triton Cache 保护

**警告**：在算子替换后重启服务时，Triton JIT cache 可能导致旧的 kernel 被使用。

```bash
# 清除 Triton cache（在每次算子配置变更后）
${CMD_PREFIX} rm -rf ~/.triton/cache/ 2>/dev/null
${CMD_PREFIX} rm -rf /tmp/triton_cache/ 2>/dev/null
```

**何时需要清除**：
- 算子替换后重启服务前
- FlagGems 升级后重启服务前
- 性能测试结果异常时排查

---

# Plugin 场景的算子覆盖率检查

当 `vllm_plugin_installed=true` 时，在性能测试前检查算子覆盖率：

```bash
# 检查 FlagGems 实际覆盖了多少 aten 算子
${CMD_PREFIX} python3 -c "
import json
try:
    import flag_gems
    flag_gems.enable()
    ops = list(flag_gems.all_registered_ops()) if hasattr(flag_gems, 'all_registered_ops') else []
    print(json.dumps({'covered_ops': len(ops), 'ops': sorted(ops)}))
except Exception as e:
    print(json.dumps({'error': str(e)}))
"
```

如果覆盖率很低（< 20 个算子），FlagOS 加速效果可能有限，应在报告中注明。

---

# 工作流程

## 核心原则：三版测试 + 按需优化

新工作流在步骤⑤（快速性能评测）中按固定顺序执行：
1. **V1 Native** — 关闭 FlagGems 的基线性能
2. **V2 FlagGems** — 启用 FlagGems 的性能（使用步骤④精度达标后的算子列表）
3. **V3 Optimized FlagGems** — 仅在 V2 不达标时，通过算子优化找到每个用例每个并发级别均 ≥80% 的组合

**算子列表必录**：只要 FlagGems 处于启用状态，必须记录算子列表到 ops_list.json，这是算子优化的基础。

最终需要三个结果文件：
1. **native_performance.json** — V1 性能
2. **flagos_performance.json** — V2 性能
3. **flagos_optimized.json** — V3 性能（仅在 V2 不达标时产出）

## 步骤 0：策略确定

策略由流程阶段自动决定，不在此处单独询问：

- **主流程步骤⑤**：自动使用 `--strategy quick`，无需询问用户
- **独立调用全量性能测试**：使用 `fast`（推荐）或 `comprehensive`

| 选项 | 说明 | 触发时机 |
|------|------|----------|
| `quick` | 只跑 4k_input_1k_output + max，快速验证 | 主流程步骤⑤自动使用 |
| **`fast`**（推荐） | 所有用例，饱和即停 | 独立调用默认推荐 |
| `comprehensive` | 所有用例，所有并发全跑 | 用户要求"完整测试" |

**Final Burst**：默认不跑。仅在独立调用全量测试、用户显式要求时启用。

一旦选定，该阶段内所有性能测试统一使用同一策略。

## 步骤 1：同步配置

从 `shared/context.yaml` 读取服务信息，写入 `/flagos-workspace/perf/config/perf_config.yaml`。

同时将配置快照保存到 `config/` 目录：
```bash
docker exec $CONTAINER cp /flagos-workspace/perf/config/perf_config.yaml /flagos-workspace/config/perf_config.yaml
```

**per-test-case 超时配置**：在 perf_config.yaml 中为不同用例设置不同超时：

```yaml
test_matrix:
  - name: 1k_input_1k_output
    input_len: 1024
    output_len: 1024
    timeout: 600        # 默认 600s 足够
  - name: 32k_input_1k_output
    input_len: 32768
    output_len: 1024
    timeout: 1800       # 32k 输入需要更长时间
  - name: 1k_input_4k_output
    input_len: 1024
    output_len: 4096
    timeout: 900        # 长输出需要更多时间
```

## 步骤 2：判断当前 FlagGems 状态

从 `shared/context.yaml` 的 `flaggems_control.integration_type` 和 `inspection` 字段判断当前环境中 FlagGems 是否已启用。

判断依据（按优先级）：
1. `flaggems_control.enable_method` 是否为 `auto`（plugin 自动启用）
2. 环境变量 `USE_FLAGGEMS=1` / `USE_FLAGOS=1`
3. 代码中是否有 `flag_gems.enable()` 被调用
4. 服务启动日志中是否有 FlagGems 相关输出

```
当前状态判定:
  ├── FlagGems 已启用 → 走路径 A（先测 V2）
  └── FlagGems 未启用 → 走路径 B（先测 V1）
```

---

## 步骤 3：运行 V1 基线测试

此时服务已以 native 模式启动（FlagGems 关闭）。

```bash
docker exec $CONTAINER bash -c "cd /flagos-workspace && python scripts/benchmark_runner.py \
  --config perf/config/perf_config.yaml \
  --strategy fast \
  --output-name native_performance \
  --output-dir /flagos-workspace/results/ \
  --mode native"
```

## 步骤 4：启用 FlagGems，切换到 FlagOS 模式

通过 `toggle_flaggems.py` 启用 FlagGems，重启服务。

## 步骤 5：记录算子列表（强制）

**FlagGems 启用状态下，必须先记录算子列表。**

```bash
${CMD_PREFIX} python3 -c "
import json, flag_gems
flag_gems.enable()
ops = list(flag_gems.all_registered_ops()) if hasattr(flag_gems, 'all_registered_ops') else list(flag_gems.all_ops())
with open('/flagos-workspace/results/ops_list.json', 'w') as f:
    json.dump(sorted(ops), f, indent=2)
print(f'已记录 {len(ops)} 个算子到 ops_list.json')
"
```

## 步骤 6：运行 V2 FlagGems 性能测试

```bash
docker exec $CONTAINER bash -c "cd /flagos-workspace && python scripts/benchmark_runner.py \
  --config perf/config/perf_config.yaml \
  --strategy fast \
  --output-name flagos_performance \
  --output-dir /flagos-workspace/results/ \
  --mode flagos"
```

## 步骤 7：性能对比（强制执行）

**强制规则**：V1 和 V2 性能测试完成后，必须调用 `performance_compare.py` 生成全量并发级别对比。禁止跳过此步骤或手动计算单一并发级别的比值。

```bash
docker exec $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH python3 scripts/performance_compare.py \
  --native results/native_performance.json \
  --flagos-full results/flagos_performance.json \
  --output results/performance_compare.csv \
  --target-ratio 0.8 \
  --format markdown"
```

**输出解读**：
- 工具会输出包含所有并发级别的 markdown 表格，必须完整保留到报告中
- 返回码 `0`：每个用例的每个并发级别均 ≥ 80%，V2 已达标，不存在 V3，跳到步骤 9
- 返回码 `1`：有不达标的用例或并发级别，触发步骤 8

**禁止行为**：不得自行从 JSON 中只提取某一个并发级别的数据作为性能对比结果，必须使用 `performance_compare.py` 的全量输出。

## 步骤 8：[自动] 触发算子优化

前置条件：`ops_list.json` 已存在（步骤 5 中已记录）。

调用 `flagos-operator-replacement` 分组二分搜索。优化过程中使用已记录的算子列表作为搜索空间。自动继续（上限 5 轮，超限取当前最优）。

优化完成后重测：

```bash
docker exec $CONTAINER bash -c "cd /flagos-workspace && python scripts/benchmark_runner.py \
  --config perf/config/perf_config.yaml \
  --strategy fast \
  --output-name flagos_optimized \
  --output-dir /flagos-workspace/results/ \
  --mode flagos_optimized"
```

## 步骤 9：性能对比 + 报告

```bash
docker exec $CONTAINER bash -c "cd /flagos-workspace && python scripts/performance_compare.py \
  --native results/native_performance.json \
  --flagos-optimized results/flagos_optimized.json \
  --flagos-full results/flagos_performance.json \
  --output results/performance_compare.csv \
  --target-ratio 0.8 \
  --format markdown"
```

当 V2 已达标（不存在 V3）时，只传 `--flagos-full`，不传 `--flagos-optimized`。

## 步骤 10：写入 context.yaml

**强制规则**：必须将全量并发级别的对比数据写入 context.yaml，不得只取单一并发级别。

写入字段：

```yaml
perf:
  strategy: quick          # 使用的测试策略
  test_case: 4k_input_1k_output
  per_concurrency:         # 全量并发级别对比（必须包含所有级别）
    - concurrency: "1"
      v1_total_tps: 1911.2
      v2_total_tps: 1270.2
      ratio_pct: 66.5
      pass: false
    - concurrency: "2"
      v1_total_tps: 3735.6
      v2_total_tps: 2370.7
      ratio_pct: 63.5
      pass: false
    # ... 所有并发级别
  summary:
    total_levels: 10       # 总并发级别数
    pass_levels: 0         # 达标级别数
    fail_levels: 10        # 未达标级别数
    best_ratio_pct: 78.5   # 最佳比值
    worst_ratio_pct: 25.0  # 最差比值
    avg_ratio_pct: 56.8    # 平均比值
    overall_pass: false     # 是否全部达标
  # 保留最佳并发的摘要（向后兼容）
  v1_output_tps: 6598.2
  v1_total_tps: 32991.0
  v2_output_tps: 5178.5
  v2_total_tps: 25892.5
  ratio_pct: 78.5          # 最佳并发的比值
```

**禁止行为**：不得只写入单一并发级别的数据到 context.yaml，必须包含 `per_concurrency` 全量列表和 `summary` 统计。

---

## 阶段性反馈格式

每次性能测试完成后，必须向用户输出以下格式的总结：

```
性能测试结果
========================================
模式: native / flagos_full / flagos_optimized
测试用例: prefill1_decode512, 1k_input_1k_output, ...
最佳并发: 64
Output throughput: 1850 tok/s
Total throughput: 5200 tok/s
Native 基线: 6500 tok/s（首次 native 测试时不显示此行）
性能比: 80.0% — 达标(≥80%) / 不达标(<80%)
========================================
```

**反馈规则**：
- Native 模式测试时不显示"性能比"
- FlagOS 模式测试时必须与 Native 基线对比并给出达标/不达标判断
- 不达标时自动提示"建议触发算子优化"

### 性能问题日志写入

任一并发级别 V2/V1 < 80% 时，必须追加写入 `logs/issues_performance.log`：

```bash
docker exec $CONTAINER bash -c "cat >> /flagos-workspace/logs/issues_performance.log << 'ISSUE_EOF'
[$(date '+%Y-%m-%d %H:%M:%S')] <版本> | <问题摘要>
  详情: <不达标的用例+并发级别, V1 TPS, V2 TPS, ratio>
  操作: <措施，如 operator_search.py 第 N 轮优化>
  结果: <优化后 ratio，达标/不达标>
ISSUE_EOF"
```

记录场景：
- V2/V1 性能对比中任一并发级别 < 80%（记录所有不达标的并发级别）
- 算子搜索优化每轮结果（禁用了哪些算子、优化后的 ratio）
- 最终结论（V2 达标 / V3 达标 / 不达标）

---

## benchmark_runner.py 参数

| 参数 | 说明 |
|------|------|
| `--config` | 配置文件路径 |
| `--strategy` | 测试策略：`quick`(烟雾测试) / `fast`(饱和即停,默认) / `comprehensive`(全跑不早停) |
| `--final-burst` | 追加无限制并发大规模最终测试（默认不跑，显式 opt-in） |
| `--skip-case` | 跳过指定用例（可多次使用），如 `--skip-case 4k_input_1k_output`。用于复用 quick 已跑的数据 |
| `--quick` | (向后兼容别名) 等同于 `--strategy quick` |
| `--concurrency-search` | (向后兼容别名) 等同于 `--strategy fast` |
| `--output-name` | 输出文件名（不含扩展名） |
| `--output-dir` | 输出目录 |
| `--mode` | 测试模式标记 |
| `--test-case` | 运行指定测试用例 |
| `--dry-run` | 仅打印命令不执行 |

**优先级**：`--strategy` > `--quick` > `--concurrency-search` > 默认 `fast`

### 并发搜索停止条件（strategy=fast 时生效）

1. **连续 2 级增长 < 3%**：吞吐趋于饱和
2. **吞吐下降 > 5%**：已过拐点，继续加并发无意义
3. **请求失败 > 0**：服务过载

以上条件仅对 `early_stop: true` 的用例生效。`prefill1_decode512` 和 `1k_input_1k_output` 两个用例设置 `early_stop: false`，所有并发全跑，用于跨平台对比基准。quick 模式自动追加 `--final-burst`（max 测试）。

搜索结果中标注**最佳并发数**（吞吐峰值对应的并发级别），不区分是否提前停止。

### per-test-case 超时

从配置文件 `test_matrix[].timeout` 字段读取，默认 600s。长序列用例（如 32k 输入）可设置 1800s。

## performance_compare.py 参数

| 参数 | 说明 |
|------|------|
| `--native` | 原生性能 JSON（必填） |
| `--flagos-initial` | FlagOS 初始性能 |
| `--flagos-optimized` | FlagOS 优化后性能 |
| `--flagos-full` | FlagOS 全量算子性能 |
| `--output` | CSV 输出路径 |
| `--target-ratio` | 目标比率（默认 0.8） |
| `--format` | 输出格式: `text`（默认） / `markdown` |

---

## 完成条件

- 测试脚本已在容器中就绪
- **算子列表已记录**（FlagGems 启用时 ops_list.json 必须存在）
- native_performance.json 已生成
- flagos_performance.json 已生成
- 对比结果已生成（performance_compare.csv）
- 性能比率已判断（每个用例的每个并发级别均 ≥ 80%，或触发算子优化）
- 性能不达标时，`logs/issues_performance.log` 已追加写入问题记录
- 如触发优化：flagos_optimized.json 已生成
- 最终三版对比已生成（performance_compare_final.csv）
- context.yaml 已更新
- 配置快照已保存到 `config/perf_config.yaml`
- 对应 trace 文件已写入：
  - `traces/05_quick_performance.json`（V1/V2/V3 性能测试 + 对比 + 算子优化记录）
- `timing.steps.quick_performance` 已更新为本步骤的 `duration_seconds`
