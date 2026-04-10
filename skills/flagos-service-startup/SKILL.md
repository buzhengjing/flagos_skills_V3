---
name: flagos-service-startup
description: 在容器内启动推理服务（支持 default/native/flagos 模式切换），使用 toggle_flaggems.py 和 wait_for_service.sh
version: 5.0.0
license: internal
triggers:
  - service startup
  - start service
  - 启动服务
  - health check
  - 健康检查
depends_on:
  - flagos-pre-service-inspection
next_skill: flagos-performance-testing
provides:
  - service.cluster
  - service.external_ip
  - service.host
  - service.port
  - service.healthy
  - service.model_id
  - service.log_path
  - service.gems_txt_path
  - service.initial_operator_list
  - service.max_model_len
  - runtime.gpu_count
  - runtime.flaggems_enabled
  - runtime.framework
  - runtime.thinking_model
  - environment.initial_env_verified
---

# 服务启动 Skill

支持 default/native/flagos 三种模式，基于 `flaggems_control` 探测结果动态决定启停方式。

**启动模式**：
- **default** — 不修改任何 FlagGems 状态，以容器现有配置原样启动。用于步骤③验证初始环境可用性。
- **native** — 关闭 FlagGems，纯原生环境。对应 V1 版本。
- **flagos** — 启用全量 FlagGems。对应 V2 版本。

**工具脚本**（已由 setup_workspace.sh 部署到容器）:
- `calc_tp_size.py` — TP 自动推算（根据模型大小和 GPU 显存）
- `toggle_flaggems.py` — FlagGems 开关切换（替代 sed）
- `wait_for_service.sh` — 服务就绪检测（指数退避）

---

# 统一工作目录

所有服务启动操作在 `/flagos-workspace` 目录下执行。

```
容器内: /flagos-workspace/logs/ ← 服务日志（按模式命名）
  startup_default.log  — 步骤③ 初始服务启动
  startup_native.log   — 步骤④⑤ 中关闭 FlagGems 的 native 模式
  startup_flagos.log   — 步骤④⑤ 中开启 FlagGems 的 flagos 模式
宿主机: /data/flagos-workspace/<model_name>/logs/ ← 实时同步
```

---

# 上下文集成

## 从 shared/context.yaml 读取

```yaml
container:
  name: <来自 container-preparation>
model:
  name: <来自 container-preparation>
  container_path: <来自 container-preparation>
execution:
  cmd_prefix: <来自 pre-service-inspection>
flaggems_control:
  enable_method: <来自 pre-service-inspection>
  disable_method: <来自 pre-service-inspection>
  integration_type: <来自 pre-service-inspection>
environment:
  env_type: <来自 pre-service-inspection>       # native | vllm_flaggems | vllm_plugin_flaggems
  flaggems_txt_path: <来自 pre-service-inspection>  # vllm_flaggems 场景的 txt 路径
  gems_txt_auto_detect: <来自 pre-service-inspection>
```

## 写入 shared/context.yaml

```yaml
service:
  cluster: <集群标识>
  external_ip: <宿主机 IP>
  host: <服务主机>
  port: <服务端口>
  healthy: true|false
  model_id: <模型标识符>
  log_path: <日志路径>
  gems_txt_path: <gems.txt 路径>
  enable_oplist_path: <flaggems_enable_oplist.txt 路径，无则为空>
  enable_oplist_count: <oplist 中算子数量，无则为 0>
  initial_operator_list: [...]
  max_model_len: <服务实际的 max_model_len>
runtime:
  framework: <vllm|sglang>
  gpu_count: <GPU 数量>
  flaggems_enabled: true|false        # 根据 oplist 文件是否存在判断，而非启动模式
  thinking_model: true|false            # 是否为 thinking model（传递给后续评测 Skill）
```

---

# 工作流程

## 步骤 1 — 停止现有服务

```bash
# 推荐方式：docker restart 确保资源完全释放（避免僵尸进程占用显存）
docker restart $CONTAINER
sleep 5
```

备选方式（仅当不能重启容器时）：
```bash
docker exec $CONTAINER bash -c "pkill -f 'vllm\|sglang\|flagscale' 2>/dev/null; sleep 3"
```

## 步骤 2 — 切换 FlagGems 状态（按 env_type 分路径）

根据 `environment.env_type` 和启动模式决定 FlagGems 开关方式：

**Default 模式**（不修改任何状态）：
不调用 `toggle_flaggems.py`，直接跳到步骤 3。用于步骤③验证初始环境可用性。所有 env_type 通用。

---

### env_type = native（纯 vllm 原生）

无 FlagGems 可切换，无需调用 `toggle_flaggems.py`。
- Native 模式 / FlagOS 模式均直接启动标准 vllm 命令
- 跳过算子列表记录（步骤 5）
- 不执行 V2/V3 相关步骤

---

### env_type = vllm_flaggems（通过代码控制 FlagGems 开关）

通过注释/取消注释 `import flag_gems` 相关代码行来控制 FlagGems 是否加载。

**Native 模式**（关闭 FlagGems）：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action disable --json"
```
注释掉 `import flag_gems` / `flag_gems.enable()` 等代码行，服务启动时 FlagGems 不加载。

**FlagOS 模式**（启用 FlagGems）：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action enable --json"
```
取消注释 `import flag_gems` / `flag_gems.enable()` 等代码行，服务启动时 FlagGems 自动生效。

**算子列表获取**（启动后）：
- 读取 `environment.flaggems_txt_path`（由 pre-service-inspection 步骤 2.6 写入）
- 如果 `gems_txt_auto_detect: true`（代码解析未找到路径），启动后调用：
  ```bash
  docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action find-gems-txt --json"
  ```
  从输出的 `recommended` 字段获取路径，回写 `context.yaml` 的 `environment.flaggems_txt_path`

---

### env_type = vllm_plugin_flaggems（通过环境变量控制 FlagGems 开关）

**Native 模式**（关闭 FlagGems）：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action disable --integration-type plugin --json"
```
输出 JSON 包含 `env_vars` 和 `env_inline` 字段，在启动命令中使用 `env_inline` 作为内联前缀。

**FlagOS 模式**（启用 FlagGems）：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action enable --integration-type plugin --json"
```

**算子列表获取**（启动后）：
- 检查 `/tmp/flaggems_enable_oplist.txt`（plugin 架构下的权威算子列表）

## 步骤 2.5 — TP 自动推算

在启动服务前，自动推算最小可用 `--tensor-parallel-size`：

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/calc_tp_size.py --model-path $MODEL_PATH --json"
```

输出示例：
```json
{
  "recommended_tp": 1,
  "gpu_count": 8,
  "gpu_memory_gb": 80.0,
  "model_size_gb": 15.2,
  "estimated_required_gb": 18.2,
  "reason": "模型 15.2GB，单卡 80GB 显存充足，推荐 TP=1"
}
```

**使用规则**：
- 读取 `recommended_tp` 作为 `${TP_SIZE}` 的值
- 如果脚本执行失败（退出码非 0），fallback 到 GPU 总数
- 如果推荐 TP 启动后 OOM，自动翻倍重试（TP×2），直到 GPU 总数

将推荐值写入 context.yaml 的 `runtime.tp_size` 和 `runtime.tp_reason`。

## 步骤 2.6 — max_model_len 决策

`--max-model-len` 直接决定模型单次请求能处理的最大 token 数。**确定后写入 context.yaml，后续每次启动复用同一值。**

**决策逻辑**：

1. 读取 `service.max_model_len`
   - **已有值（>0）** → 直接复用，不重新计算
   - **为 0（首次）** → 按下方规则计算后写入

2. 首次计算规则：

| 模型类型 | max_model_len | 原因 |
|---------|---------------|------|
| Thinking model（Qwen3/QwQ/DeepSeek-R1/R2） | **32768** | thinking chain 需要大量 token，需留余量给 prompt |
| 标准模型 | **8192** | 非 thinking 模型评测和性能测试够用 |

3. **显存约束**：如果启动 OOM，降级 `max_model_len`（thinking model 最低 16384），并更新 context.yaml

4. **验证**：启动后 `wait_for_service.sh` 输出实际 `max_model_len`，确认与预期一致

## 步骤 3 — 启动服务

**非 plugin 场景**：
```bash
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && <startup_command> > /flagos-workspace/logs/startup_<mode>.log 2>&1"
```

**Plugin 场景**（内联环境变量前缀）：
```bash
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH <env_inline> <startup_command> > /flagos-workspace/logs/startup_<mode>.log 2>&1"
```

其中 `<mode>` 为 `default`、`native` 或 `flagos`，`<env_inline>` 来自 `toggle_flaggems.py --json` 输出的 `env_inline` 字段。

### Plugin 场景 vllm 服务启动模板

Plugin 环境下服务启动命令统一使用标准 vllm 格式，FlagGems 控制通过**内联环境变量**注入，与启动命令分离。

```bash
vllm serve ${MODEL_PATH} \
    --host 0.0.0.0 \
    --port ${PORT:-8000} \
    --served-model-name ${MODEL_NAME} \
    --tensor-parallel-size ${TP_SIZE} \
    --max-num-batched-tokens ${MAX_BATCHED_TOKENS:-16384} \
    --max-num-seqs ${MAX_NUM_SEQS:-256} \
    --max-model-len ${MAX_MODEL_LEN:-8192} \
    --trust-remote-code
```

**可选参数**（按需添加）：

| 参数 | 场景 | 示例 |
|------|------|------|
| `--pipeline-parallel-size` | 多机或超大模型 | `--pipeline-parallel-size 2` |
| `--gpu-memory-utilization` | 需限制显存占用 | `--gpu-memory-utilization 0.8` |
| `--reasoning-parser` | Thinking model | `--reasoning-parser qwen3` |

**四种模式启动方式**：

```bash
# Default（不修改环境，原样启动）
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH vllm serve ... > /flagos-workspace/logs/startup_default.log 2>&1"

# Native（关闭 FlagGems）
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH USE_FLAGGEMS=0 VLLM_FL_PREFER_ENABLED=false vllm serve ... > /flagos-workspace/logs/startup_native.log 2>&1"

# FlagOS Full（全量 FlagGems）
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true vllm serve ... > /flagos-workspace/logs/startup_flagos.log 2>&1"

# FlagOS Optimized（自定义 blacklist）
docker exec -d $CONTAINER bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH USE_FLAGGEMS=1 VLLM_FL_PREFER_ENABLED=true VLLM_FL_FLAGOS_BLACKLIST='mm,softmax' vllm serve ... > /flagos-workspace/logs/startup_flagos.log 2>&1"
```

四种模式差异仅在内联环境变量前缀（由 `toggle_flaggems.py` 或 `apply_op_config.py` 的 JSON 输出中的 `env_inline` 提供）。

**模板使用规则**：
- 具体参数值从容器 README / 用户输入 / context.yaml 获取
- `--served-model-name` 默认使用模型目录名
- `--tensor-parallel-size` 默认使用 `calc_tp_size.py` 的推荐值（基于模型大小和单卡显存自动推算），fallback 到 GPU 总数
- 业务环境变量（`VLLM_USE_MODELSCOPE` 等）按需在 docker exec 中追加，不写入模板
- `--max-model-len` 使用 context.yaml 中 `service.max_model_len` 的值（由步骤 2.6 决策）

## 步骤 4 — 等待服务就绪

```bash
# Native 模式 / Default 模式
docker exec $CONTAINER bash -c "/flagos-workspace/scripts/wait_for_service.sh --port $PORT --model-name '$MODEL_NAME' --timeout 300"

# FlagGems 模式（CUDA graph 编译慢，需更长超时）
docker exec $CONTAINER bash -c "/flagos-workspace/scripts/wait_for_service.sh --port $PORT --model-name '$MODEL_NAME' --timeout 600"
```

自动轮询（2s→4s→5s 快速收敛，最大间隔 5s），超时自动分析日志。

**启动后校验**：检查 `wait_for_service.sh` 输出的 `max_model_len`：
- 如果是 thinking model 且 `max_model_len < 32768` → 警告，建议重启并加大 `--max-model-len`
- 如果 `max_model_len < 8192` → 评测可能出问题，必须修正

## 步骤 5 — 服务验证

```bash
curl -s http://localhost:$PORT/v1/models
curl -s http://localhost:$PORT/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<model_name>", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}'
```

## 步骤 6 — 探测宿主机 IP 和输出连接信息

```
============================================================
服务连接信息
============================================================
<集群, IP, 服务端口, 模型名称>
评测接口: http://${EXTERNAL_IP}:${PORT}/v1/chat/completions
启动模式: native / flagos
============================================================
```

## 步骤 7 — 检查算子列表（每次启动后，强制）

**每次服务启动后（无论 default/native/flagos 模式），都必须检查 `flaggems_enable_oplist.txt`。**

该文件是 FlagGems 运行时自动生成的**唯一权威算子列表**，路径默认为 `/tmp/flaggems_enable_oplist.txt`。

```bash
# 检查 oplist 文件
docker exec $CONTAINER bash -c "
if [ -f /tmp/flaggems_enable_oplist.txt ]; then
    echo 'OPLIST_FOUND: /tmp/flaggems_enable_oplist.txt'
    echo 'OPLIST_MTIME:' \$(stat -c %Y /tmp/flaggems_enable_oplist.txt)
    echo 'OPLIST_COUNT:' \$(wc -l < /tmp/flaggems_enable_oplist.txt)
    cat /tmp/flaggems_enable_oplist.txt
else
    echo 'OPLIST_NOT_FOUND'
fi
"
```

**判断逻辑**：

| 文件状态 | 含义 | 后续操作 |
|----------|------|----------|
| 存在且有内容 | FlagGems 实际在运行 | 以此文件内容为当前生效算子列表，同步到 `results/ops_list.json` |
| 不存在或为空 | FlagGems 未启用 | 不依赖任何缓存的算子列表 |

**文件存在时**：

```bash
# 以 oplist 文件为准，同步保存到 results/ops_list.json
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/operator_optimizer.py discover \
  --save-ops /flagos-workspace/results/ops_list.json"
```

**关键原则**：
- `flaggems_enable_oplist.txt` = 当前实际生效的算子列表，**唯一权威来源**
- `results/ops_list.json` 是此文件的持久化副本，供后续对比和报告使用
- 如果启动模式为 flagos 但文件不存在 → 异常，需排查（服务可能未正确加载 FlagGems）
- 如果启动模式为 native 但文件存在 → 可能是上次 flagos 的残留，不应作为当前算子列表
- 每次 FlagGems 重新启动都会**重新生成**此文件，内容反映最新的算子配置（含 blacklist 生效后的结果）

**Native 模式残留检测**：

native 模式启动后，如果发现 `flaggems_enable_oplist.txt` 仍然存在，执行以下检查：

```bash
# 获取 oplist 文件修改时间和服务启动时间
OPLIST_MTIME=$(${CMD_PREFIX} stat -c %Y /tmp/flaggems_enable_oplist.txt 2>/dev/null || echo 0)
SERVICE_START=$(${CMD_PREFIX} stat -c %Y /proc/1/cmdline 2>/dev/null || echo 999999999)

if [ "$OPLIST_MTIME" -lt "$SERVICE_START" ]; then
    echo "检测到旧 oplist 残留（mtime < 服务启动时间），清理中..."
    ${CMD_PREFIX} rm -f /tmp/flaggems_enable_oplist.txt
    echo "已清理残留 oplist 文件"
else
    echo "WARNING: native 模式下 oplist 文件 mtime 晚于服务启动，可能 FlagGems 未正确关闭"
fi
```

- mtime 早于本次服务启动时间 → 旧残留，清理并记录到 trace
- mtime 晚于启动时间 → 异常，FlagGems 可能未正确关闭，报告给用户

**反馈输出**：
```
# FlagGems 运行时
算子列表验证：
  oplist 文件: /tmp/flaggems_enable_oplist.txt
  当前生效算子: XX 个
  已同步到: /flagos-workspace/results/ops_list.json

# FlagGems 未运行时
算子列表检查: oplist 文件不存在，FlagGems 未启用
```

## 步骤 8 — 写入 context.yaml

写入 `environment` 字段（步骤③ default 模式时）：
```yaml
environment:
  initial_env_verified: true    # 步骤③通过后设为 true
  has_plugin: <from inspection>
```

---

# 失败恢复

如果 flagos 模式启动失败：
1. 保存失败日志
2. 自动切回 Native 验证
3. Native 也失败 → 报告环境问题；Native 成功 → 确认是 FlagGems 问题

### 问题日志写入

服务启动失败时，必须将失败信息追加写入 `logs/issues_startup.log`：

```bash
docker exec $CONTAINER bash -c "cat >> /flagos-workspace/logs/issues_startup.log << 'ISSUE_EOF'
[$(date '+%Y-%m-%d %H:%M:%S')] <启动模式> | <问题摘要>
  详情: <错误信息，如 OOM/端口占用/进程崩溃/超时>
  操作: <恢复措施，如 TP 翻倍/切回 Native/降低 max-model-len>
  结果: <恢复结果>
ISSUE_EOF"
```

记录场景：
- 启动超时（wait_for_service.sh 超时）
- 进程启动后立即退出（OOM、GPU 显存不足）
- FlagGems 模式启动失败，切回 Native
- TP 调整重试
- 端口被占用

---

# 完成条件

- 启动模式已确认（native / flagos）
- 服务进程正在运行
- API /v1/models 可访问
- 推理测试通过
- 已输出服务连接信息
- gems.txt 已检查（flagos 模式）
- context.yaml 已更新
- 对应 trace 文件已写入：
  - 步骤③初始启动 → `traces/03_service_startup.json`
  - 步骤④⑤中的 native/flagos 模式切换 → 记录在 `traces/04_quick_accuracy.json` 或 `traces/05_quick_performance.json` 的 actions 中
- 启动失败时，`logs/issues_startup.log` 已追加写入问题记录
- `timing.steps.service_startup` 已更新为本步骤的 `duration_seconds`

---

# 故障排查

| 症状 | 可能原因 | 解决方案 |
|------|----------|----------|
| 进程启动后立即退出 | GPU 显存不足 | 自动将 TP 翻倍重试（TP×2），或降低 max-model-len |
| API 无响应 | 端口被占用 | 检查 `lsof -i:$PORT` |
| FlagGems 未生效 | toggle_flaggems.py 未正确切换 | 运行 `--action status` 检查 |
| gems.txt 未生成 | FlagGems 未启用 | 确认 toggle 状态 |
| 服务启动超时 | wait_for_service.sh 会自动诊断 | 查看超时输出的日志分析 |
| Thinking model 评测分数异常低 | max_model_len 过小，推理链被截断 | 重启服务，加大 `--max-model-len` 至 32768+ |
| OOM: max_model_len 过大 | KV cache 显存预分配超限 | 降低 max-model-len（thinking model 最低 16384） |
