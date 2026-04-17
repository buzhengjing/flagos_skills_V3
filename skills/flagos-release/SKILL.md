---
name: flagos-release
description: FlagOS 镜像打包发布 + 模型权重上传（Harbor / ModelScope / HuggingFace）
version: 1.0.0
triggers:
  - 发布
  - 镜像上传
  - 镜像打包
  - 模型发布
  - release
  - image upload
  - package image
  - model release
  - upload model
  - publish
depends_on:
  - flagos-performance-testing
provides:
  - image.registry_url
  - image.upload_timestamp
  - release.modelscope_url
  - release.huggingface_url
---

# 发布 Skill

将验证完成的 FlagOS 环境打包为 Docker 镜像并发布到 Harbor，同时将模型权重上传到 ModelScope / HuggingFace。

**工具脚本**（宿主机执行，不部署到容器）：

```
tools/
├── main.py                      # 流水线主入口
├── requirements.txt             # Python 依赖
├── src/
│   ├── config.py                # 配置管理（从 context.yaml 加载 + 自动填充）
│   ├── chip_detector.py         # 芯片检测（9 厂商 SMI 解析 + 镜像 tag 生成）
│   ├── utils.py                 # 工具函数
│   └── stages/
│       ├── base.py              # Stage 基类（命令执行、结果记录）
│       └── publish.py           # 发布阶段（commit→tag→push→README→上传）
└── templates/
    └── README_TEMPLATE.md       # README 模板
```

**支持芯片厂商**（自动检测）：

| 厂商 | 检测命令 | SDK | GPU 编码示例 |
|------|---------|-----|-------------|
| nvidia | nvidia-smi | CUDA | nvidia001 (A100) |
| metax | mx-smi | MXMACA | metax001 (C550) |
| mthreads | mthreads-gmi | MUSA | mthreads001 (S5000) |
| iluvatar | ixsmi | IXRT | iluvatar001 (BI-V150) |
| ascend | npu-smi | CANN | ascend001 (910B) |
| hygon | hy-smi | DTK | hygon001 (BW1000) |
| kunlunxin | xpu-smi | XRE | kunlunxin001 (P800) |
| cambricon | cnmon | CNToolkit | cambricon001 (MLU590) |
| tsingmicro | tsm_smi | TSM | tsingmicro001 (REX1032) |

---

# 上下文集成

## 从容器内 /flagos-workspace/shared/context.yaml 读取

```yaml
container:
  name: <来自 container-preparation>
model:
  name: <来自 container-preparation>
service:
  healthy: <来自 service-startup>
gpu:
  vendor: <来自 container-preparation>
workflow:
  service_ok: <来自编排层>
  accuracy_ok: <来自编排层>
  performance_ok: <来自编排层>
  qualified: <来自编排层>
  skip_reason: <来自编排层>
```

## 写入容器内 /flagos-workspace/shared/context.yaml

```yaml
image:
  registry_url: <推送后的 Harbor 完整地址>
  upload_timestamp: <YYYYMMDDHHMM>
release:
  modelscope_url: <ModelScope 模型 URL>
  huggingface_url: <HuggingFace 模型 URL>
```

---

# 工作流程

## 执行流水线

从工作流共享状态 `context.yaml` 读取所有信息，无需手写配置文件：

```bash
cd skills/flagos-release/tools

# 执行发布
python main.py --from-context /flagos-workspace/shared/context.yaml

# 干运行（只验证配置，不实际执行）
python main.py --from-context /flagos-workspace/shared/context.yaml --dry-run

# 只生成 README
python main.py --from-context /flagos-workspace/shared/context.yaml --only-readme
```

`--from-context` 自动映射的字段：
- `container.name` → 容器名
- `model.name` → 模型来源
- `model.container_path` → 权重目录 + 服务启动命令
- `service.port/max_model_len` + `runtime.tp_size` → 服务启动命令
- `gpu.vendor` → 芯片厂商（自动检测填充）
- `eval.v1_score/v2_score` → 评测结果（填入 README）
- `image.tag` → 已有镜像地址（有则跳过 commit/tag/push）

## 阶段详情

### 步骤 0 — 发布条件检查

在执行任何发布操作之前，从 `context.yaml` 读取 `workflow.qualified` 判定发布可见性：

```
读取 workflow.qualified (= service_ok AND accuracy_ok AND performance_ok)

if qualified == true:
    publish.private = false   → 公开发布
    日志: "全流程达标（服务✓ 精度✓ 性能✓），公开发布"
else:
    publish.private = true    → 私有发布
    日志: "不合格，私有发布。原因: <不达标项>"
    不达标项示例:
      - service_ok=false: "服务启动失败"
      - accuracy_ok=false: "精度不达标（3 轮优化后仍超阈值）"
      - performance_ok=false: "性能不达标（3 轮优化后仍 <80%）"
```

**判定细节**：
- `service_ok = true`：V1 和 V2 都能正常启动
- `accuracy_ok = true`：V1/V2 精度偏差 ≤5%，或经 ≤3 轮优化后达标
- `performance_ok = true`：V2/V1 每个并发级别 ≥80%，或经 ≤3 轮优化后达标
- 提交了 issue 但优化成功 → 仍算合格（qualified=true）
- `skip_reason` 非空时（如 `"service_startup_failed"`）→ 跳过了③④，直接私有发布

### 发布（Publish）

| 步骤 | 操作 | 可跳过 |
|------|------|--------|
| B0 | 容器 commit 为镜像（input_type=container 时） | 有 existing_harbor_image 时跳过 |
| B1 | 镜像打 tag（自动生成标准命名） | 可配置跳过 |
| B2 | 推送到 Harbor（流式输出进度） | 可配置跳过 |
| B3 | 生成 README（含发布可见性标记 + 评测结果） | 可配置跳过 |
| B4 | 发布到 ModelScope（SDK + CLI 降级，`private` 由步骤 0 决定） | 可配置跳过 |
| B5 | 发布到 HuggingFace（SDK + CLI 降级，`private` 由步骤 0 决定） | 可配置跳过 |

---

# 镜像命名规范

## Tag 格式

```
{registry}/flagrelease-{vendor}-release-model_{model}-tree_{tree}-gems_{gems}-cx_{cx}-python_{python}-torch_{backend}-{torch_version}-pcp_{sdk}-gpu_{gpu_code}-arc_{arch}-driver_{driver}:{YYYYMMDDHHMM}
```

## 示例

```
harbor.baai.ac.cn/flagrelease-public/flagrelease-nvidia-release-model_qwen3.5-8b-tree_none-gems_4.2.1rc0-cx_none-python_3.12.3-torch_cuda-2.9.0-pcp_cuda13.1-gpu_nvidia003-arc_amd64-driver_570.158.01:202603301143
```

## 规则

- GPU 型号使用编码（`nvidia001` = A100, `nvidia003` = H20, `metax001` = C550）
- 版本号中 `+` 替换为 `-`
- 模型名小写
- 日期 tag 格式 `YYYYMMDDHHMM`（12 位）
- `_-` / `-_` / `--` 等非法组合自动清理

## 模型命名规范

```
output_name:       {Model}-{vendor}          (nvidia 不加后缀)
flagrelease_name:  {output_name}-FlagOS
仓库 ID:           FlagRelease/{flagrelease_name}
```

示例：`Qwen3-8B-metax` → `Qwen3-8B-metax-FlagOS` → `FlagRelease/Qwen3-8B-metax-FlagOS`

---

# 产出目录结构

```
output/
  {flagrelease_name}/
    README.md              # 自动生成
    *.safetensors          # 权重文件（软链接）
    config.json
    tokenizer.json
    ...
```

---

# 完成条件

**发布条件检查**：
- `workflow.qualified` 已读取并决定发布可见性（公开/私有）

**镜像发布**：
- 环境信息已自动检测
- Docker 镜像已 commit + tag
- 镜像已推送到 Harbor

**模型发布**：
- README 已生成（含评测结果、环境信息、启动命令）
- 模型已上传到 ModelScope / HuggingFace
- 仓库 URL 已记录

**流程集成**：
- context.yaml 已更新（`image.registry_url`、`image.upload_timestamp`、`release.modelscope_url`、`release.huggingface_url`）
- `traces/06_release.json` 已写入（记录发布条件判定、commit/tag/push 命令、README 路径、ModelScope/HuggingFace URL）
- `results/release_info.json` 已保存（qualified 状态、Harbor URL、ModelScope URL、HuggingFace URL）
- `timing.steps.release` 已更新为本步骤的 `duration_seconds`

**容器产出同步到宿主机**（必须在输出最终报告前完成）：
- 容器内 `/flagos-workspace/{results,traces,logs}/` 已通过 `docker cp` 同步到宿主机 `/data/flagos-workspace/<model>/` 对应目录
- `context.yaml` 已同步到宿主机 `config/context_snapshot.yaml`
- 宿主机文件数量与容器内一致

---

# 故障排查

| 问题 | 解决方案 |
|------|----------|
| 芯片检测失败 | 在配置中手动指定 `chip.vendor` |
| Harbor 推送失败 | 脚本自动通过 `HARBOR_USER` / `HARBOR_PASSWORD` 环境变量登录；若未设置则需手动 `docker login` |
| ModelScope 上传失败 | 检查 `MODELSCOPE_TOKEN` 环境变量 |
| HuggingFace 上传失败 | 检查 `HF_TOKEN` 环境变量 |
| 镜像 tag 生成异常 | 使用 `--dry-run` 检查自动生成的配置 |
| 已有 Harbor 镜像 | 配置 `publish.existing_harbor_image` 跳过 commit/tag/push |
| 权重文件过大 | 上传自动重试（5 次，指数退避） |
