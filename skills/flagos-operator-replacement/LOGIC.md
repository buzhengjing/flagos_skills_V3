# 算子替换 — 整体工作逻辑

## 1. 概述

算子替换模块的目标：在 FlagGems 全量替换 PyTorch 原生算子后，如果性能不达标（< 80% of Native），通过搜索找到最优的算子子集，使性能恢复到 ≥ 80%，同时保留尽可能多的 FlagGems 算子。

核心文件：
- `operator_optimizer.py` — 搜索状态机（init/next/update 三步循环）
- `operator_search.py` — 搜索编排（自动化 next→重启→benchmark→update 循环）
- `apply_op_config.py` — 环境变量配置生成
- `diagnose_ops.py` — 搜索前快速诊断（崩溃日志/精度/性能热点）
- `ops_constants.py` — 共享常量（算子分组、风险分级、名称映射）

---

## 2. 算子控制机制

### 2.1 两层控制

| 层级 | 环境变量 | 控制对象 | 说明 |
|------|---------|---------|------|
| OOT 层 | `VLLM_FL_OOT_BLACKLIST` | 高层算子（fused_moe, rms_norm, rotary_embedding, silu_and_mul, attention_backend） | 独立于 flagos 层 |
| FlagOS 层 | `VLLM_FL_FLAGOS_WHITELIST` 或 `VLLM_FL_FLAGOS_BLACKLIST` | 底层 torch 算子（addmm, softmax, cos 等） | 白名单优先，黑名单兜底 |

基础开关：
- `USE_FLAGGEMS=1` + `VLLM_FL_PREFER_ENABLED=true` → 启用 FlagGems
- `USE_FLAGGEMS=0` + `VLLM_FL_PREFER_ENABLED=false` → 关闭 FlagGems（Native 模式）

### 2.2 白名单 vs 黑名单

| 特性 | 白名单 (WHITELIST) | 黑名单 (BLACKLIST) |
|------|-------------------|-------------------|
| 语义 | 列出要**启用**的算子 | 列出要**禁用**的算子 |
| FlagGems 底层实现 | `enable()` 函数 | `unused=` 参数 |
| 版本要求 | >= 4.2.1rc0 | 所有版本 |
| 计算公式 | `search_ops - disabled_ops` | `disabled_ops ∪ (all_ops - search_ops)` |

版本检测逻辑（`_supports_whitelist()`）：
```
容器内 import flag_gems → 读取 __version__
  → packaging.version.Version 比较 >= 4.2.1rc0
  → 失败则 regex 回退比较 major.minor.patch >= 4.2.1
  → 任何异常 → 返回 False（降级用黑名单）
```

### 2.3 黑名单完整性

黑名单必须包含两部分：
1. **搜索中被排除的算子** (`disabled_ops`)
2. **注册表中有但运行时 txt 中没有的算子** (`all_ops - search_ops`)

公式：`BLACKLIST = disabled_ops ∪ (all_ops - search_ops)`

白名单天然不需要这个补集——未列出的算子自动不启用。

---

## 3. 算子列表来源

### 3.1 两个列表

| 列表 | 来源 | 用途 |
|------|------|------|
| `all_ops` | 算子注册表 JSON（由 inspect_env.py 生成） | 全量已注册算子 |
| `search_ops` | 运行时 txt 文件 `flaggems_enable_oplist.txt`（FlagGems 启动后自动生成） | 实际被调用的算子子集 |

搜索只在 `search_ops` 范围内进行，`all_ops - search_ops` 的算子不参与搜索。

### 3.2 运行时 txt 文件发现

`find_ops_list_file()` 搜索优先级：
1. `/tmp/flaggems_enable_oplist.txt`（运行时生成，最权威）
2. `/tmp/flaggems_oplist.txt`
3. `flag_gems` 安装目录下的 .txt 文件（通过内容特征识别）

---

## 4. 搜索状态机

### 4.1 状态流转

```
not_started → [init] → in_progress → [next/update 循环] → completed / failed
```

核心状态字段：
```json
{
  "status": "in_progress",
  "search_phase": "oot | progressive | group | linear | oot_verify | done",
  "search_mode": "progressive | group | linear",
  "search_direction": "forward | reverse",
  "plugin_mode": true,
  "use_whitelist": true,
  "all_ops": ["全量注册算子"],
  "search_ops": ["运行时算子子集"],
  "enabled_ops": ["当前启用的算子"],
  "disabled_ops": ["当前禁用的算子"],
  "oot_blacklist": ["OOT 层黑名单"],
  "flagos_blacklist": ["FlagOS 层黑名单"],
  "flagos_whitelist": ["FlagOS 层白名单"],
  "native_throughput": 1000.0,
  "target_ratio": 0.8,
  "current_ratio": 0.0
}
```

### 4.2 搜索阶段（Plugin 场景）

```
oot → oot_verify → progressive/group/linear → done
```

1. **OOT 阶段**：逐个测试 5 个 OOT 算子，禁用后性能提升 > 2% 的加入 OOT blacklist
2. **OOT 验证**：用累积的 OOT blacklist 做一次验证
   - 达标 → 搜索完成（无需搜索 flagos 层）
   - 不达标 → 进入 flagos 层搜索
3. **FlagOS 层搜索**：三种策略可选

---

## 5. 三种搜索策略

### 5.1 Progressive（渐进排除，默认策略）

按性能影响力分 3 轮，达标即停：

```
Round 1: 排除 high 风险算子（addmm, mm, bmm, softmax, layer_norm, rms_norm 等）
Round 2: 追加排除 medium 风险算子（embedding, gelu, silu, conv2d 等）
Round 3: 追加排除 low 风险算子（copy_, zeros, cos, sin 等）
```

每轮是累积排除：Round 2 = Round 1 排除 + medium 排除。

判定：
- ratio >= 80% → 达标，搜索完成，保留当前启用的算子
- ratio < 80% → 进入下一轮
- 3 轮都不达标 → 搜索失败

优势：最多 3 轮测试（vs group 的 ~22 轮）。

### 5.2 Group（分组二分搜索）

按功能分 5+1 组：compute / memory / math / index / reduce / other

每组两阶段：
1. **整组禁用测试**：禁用整组所有算子
   - 达标 → 整组全部禁用，进入下一组
   - 不达标 → 进入组内二分
2. **组内二分搜索**：在组内用二分法定位具体的问题算子
   - 禁用前半 [low, mid]
   - 达标 → 前半可禁用，搜索后半 [mid+1, high]
   - 不达标 → 前半有关键算子，缩小到 [low, mid-1]（单个算子时保留）

反向模式（`--reverse`）：从全禁用出发，逐组启用，定位问题算子。适合大量注册但少量运行时调用的场景。

### 5.3 Linear（线性逐个搜索）

逐个测试每个算子：禁用后达标则禁用，不达标则保留。

最简单但轮次最多（= 算子数量）。

---

## 6. 搜索循环（operator_search.py）

`operator_search.py` 封装了完整的搜索循环，避免 Claude Code 在循环中消耗 token：

```
while True:
    action = optimizer.next()          # 获取下一步操作
    if action == completed/failed:
        break

    config = apply_operator_config()   # 应用算子配置
    restart_service(env_inline=...)    # 重启服务（内联环境变量）
    verify_ops_via_txt()               # 验证运行时 txt 文件
    benchmark = run_benchmark_quick()  # 运行 quick benchmark
    optimizer.update(throughputs)      # 更新结果
```

### 6.1 环境变量传递流

```
optimizer.next()
  → action["env_vars"] = {
      "USE_FLAGGEMS": "1",
      "VLLM_FL_PREFER_ENABLED": "true",
      "VLLM_FL_OOT_BLACKLIST": "fused_moe,rms_norm",
      "VLLM_FL_FLAGOS_WHITELIST": "addmm,mm,softmax,..."  # 或 BLACKLIST
    }
  → action["env_inline"] = "USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true ..."

apply_operator_config()
  → 提取 env_inline 字符串

restart_service()
  → 执行: env_inline + startup_cmd
  → 例: "USE_FLAGGEMS=1 VLLM_FL_FLAGOS_WHITELIST=addmm,mm bash start.sh"
```

### 6.2 非 Plugin 场景

不使用环境变量，而是通过 4 层降级策略控制：
1. **Layer 1**: YAML exclude 配置文件
2. **Layer 2**: `flag_gems.only_enable(include=[...])`
3. **Layer 3**: `flag_gems.enable(unused=[...])`
4. **Layer 4**: 直接写 txt 文件

---

## 7. 环境变量配置生成（apply_op_config.py）

三种模式：

| 模式 | 说明 | 环境变量 |
|------|------|---------|
| `native` | 关闭 FlagGems | `USE_FLAGGEMS=0 VLLM_FL_PREFER_ENABLED=false` |
| `full` | 全量 FlagGems | `USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true` |
| `custom` | 自定义 | 白名单优先，黑名单兜底 + OOT blacklist |

`--from-state` 模式：从 `operator_config.json` 状态文件读取 whitelist/blacklist，自动生成配置。

---

## 8. 搜索前诊断（diagnose_ops.py）

三个子命令，在搜索前快速定位问题：

| 子命令 | 输入 | 输出 | 复杂度 |
|--------|------|------|--------|
| `crash-log` | 崩溃日志 | 问题算子 + 错误类型 | O(1) |
| `accuracy-groups` | 算子列表 | 按组生成 blacklist 供精度测试 | ≤5 轮 |
| `profile` | 运行中的服务 | 耗时最多的算子排名 | 1 次推理 |

---

## 9. 完成路径汇总

搜索完成时，`_compute_final_lists()` 根据 `use_whitelist` 计算并存储最终列表：

| 完成路径 | 触发位置 | disabled_ops 来源 |
|---------|---------|------------------|
| OOT 达标 | `_update_oot_result()` oot_verify 分支 | `[]`（flagos 层无需排除） |
| Progressive 达标 | `_update_progressive_result()` | `cumulative_excluded`（累积排除） |
| Progressive 完成 | `get_next_action_progressive()` phase=done | `state["disabled_ops"]` |
| Group 完成 | `get_next_action_group()` 所有组搜索完 | `state["disabled_ops"]` |
| Group 反向完成 | `get_next_action_group_reverse()` 所有组搜索完 | `state["disabled_ops"]` |
| Linear 完成 | `get_next_action_linear()` + `update_result()` | `state["disabled_ops"]` |

`_compute_final_lists()` 逻辑：
```python
if use_whitelist:
    state["flagos_whitelist"] = search_ops - disabled_ops   # 要启用的
    state["flagos_blacklist"] = []
else:
    state["flagos_blacklist"] = disabled_ops ∪ (all_ops - search_ops)  # 要禁用的
    state["flagos_whitelist"] = []
```

---

## 10. 性能判定

- **目标**：每个用例的每个并发级别下，`Output token throughput` 和 `Total token throughput` 两个指标分别计算 gems/native 比值，所有比值均 ≥ 80% 才算达标
- **数据格式**：`{"test_case|concurrency": {"output": x, "total": y}, ...}`
- **计算**：`min(output_ratio, total_ratio for all test_cases × all concurrencies)`
- **native 基线**：初始化时通过 `--native-benchmark` 从 V1 benchmark JSON 提取同格式的双指标基线，存入 `state["native_throughputs"]`
- **OOT 判定**：禁用后性能提升 > 2% → 加入 OOT blacklist（仍用粗略的 output 单指标判断）

---

## 11. 端到端数据流

```
┌─────────────────────────────────────────────────────────┐
│ 1. 初始化 (init)                                         │
│    输入: ops.json + runtime_ops.json + native_throughput  │
│         + native_benchmark.json (双指标基线)               │
│    → all_ops, search_ops, 分组, 风险分级                   │
│    → _supports_whitelist() → use_whitelist                │
│    → _extract_native_throughputs() → native_throughputs   │
│    → 输出: operator_config.json (初始状态)                 │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 2. OOT 搜索 (plugin_mode=true)                          │
│    逐个测试 5 个 OOT 算子                                 │
│    → OOT blacklist                                       │
│    → 累积验证: 达标? → 完成 / 进入 flagos 搜索             │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 3. FlagOS 搜索 (progressive/group/linear)                │
│    每轮: next → env_vars → 重启服务 → benchmark → update  │
│    → 达标即停 / 全部测完                                   │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│ 4. 完成                                                  │
│    _compute_final_lists()                                │
│    → flagos_whitelist 或 flagos_blacklist                 │
│    → operator_config.json (最终状态)                      │
│    → apply_op_config.py --from-state → 生产环境配置        │
└─────────────────────────────────────────────────────────┘
```

---

## 12. 关键设计决策

1. **白名单优先**：白名单语义更简单安全，不需要计算 `all_ops - search_ops` 补集
2. **OOT 与 FlagOS 解耦**：OOT 层独立控制，不受白名单/黑名单影响
3. **搜索范围缩小**：只搜索 `search_ops`（运行时实际调用的算子），而非全量 `all_ops`
4. **版本自动探测**：容器内直接 `import flag_gems` 获取版本，无需外部传参
5. **环境变量内联注入**：`VAR=val cmd` 方式，不污染容器全局环境
