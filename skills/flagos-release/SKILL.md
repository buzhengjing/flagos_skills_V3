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
│   ├── config.py                # 配置管理（YAML 加载 + 自动填充）
│   ├── chip_detector.py         # 芯片检测（9 厂商 SMI 解析 + 镜像 tag 生成）
│   ├── utils.py                 # 工具函数
│   └── stages/
│       ├── base.py              # Stage 基类（命令执行、结果记录）
│       ├── verify.py            # 验证阶段（下载权重→启动容器→启动服务→API 验证）
│       └── publish.py           # 发布阶段（commit→tag→push→README→上传）
├── templates/
│   └── README_TEMPLATE.md       # README 模板
└── config/
    ├── release_config.yaml              # 配置模板（从镜像开始）
    └── release_config_container.yaml    # 配置模板（从容器开始）
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

## 从 shared/context.yaml 读取

```yaml
container:
  name: <来自 container-preparation>
model:
  name: <来自 container-preparation>
service:
  healthy: <来自 service-startup>
gpu:
  vendor: <来自 container-preparation>
```

## 写入 shared/context.yaml

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

## 准备配置文件

从模板创建配置（二选一）：

```bash
# 从镜像开始（完整流程）
cp skills/flagos-release/tools/config/release_config.yaml my_release.yaml

# 从已有容器开始（跳过下载权重和启动容器）
cp skills/flagos-release/tools/config/release_config_container.yaml my_release.yaml
```

**用户必填字段**：
- `model_info.source_of_model_weights` — 模型来源（如 `Qwen/Qwen3-8B`）
- `container_name` 或 `image_path` — 容器名 / 镜像路径

**自动推断字段**（无需手动填写）：
- `output_name` — `{Model}-{vendor}`（nvidia 不加后缀）
- `flagrelease_name` — `{output_name}-FlagOS`
- `image_target_tag` — 完整镜像 tag（自动检测环境生成）
- `modelscope_model_id` / `huggingface_repo_id` — `FlagRelease/{flagrelease_name}`
- 芯片信息、驱动版本、SDK 版本、Python/PyTorch 版本等

## 执行流水线

```bash
cd skills/flagos-release/tools

# 完整流水线（验证 + 发布）
python main.py --config my_release.yaml

# 只运行验证阶段
python main.py --config my_release.yaml --stages verify

# 只运行发布阶段
python main.py --config my_release.yaml --stages publish

# 从容器开始
python main.py --config my_release.yaml --input-type container --container-name mycontainer

# 只生成 README
python main.py --config my_release.yaml --stages publish --only-readme

# 干运行（只验证配置，不实际执行）
python main.py --config my_release.yaml --dry-run
```

## 阶段详情

### 阶段 A — 验证（Verify）

| 步骤 | 操作 | 可跳过 |
|------|------|--------|
| A1 | 下载模型权重（HF/ModelScope/HTTP） | input_type=container 时跳过 |
| A2 | 启动容器（docker run） | input_type=container 时跳过 |
| A3 | 启动推理服务（后台 nohup） | input_type=container 时跳过 |
| A4 | API 健康检查（/v1/models，5 次重试） | 可配置跳过 |

### 阶段 B — 发布（Publish）

| 步骤 | 操作 | 可跳过 |
|------|------|--------|
| B0 | 容器 commit 为镜像（input_type=container 时） | 有 existing_harbor_image 时跳过 |
| B1 | 镜像打 tag（自动生成标准命名） | 可配置跳过 |
| B2 | 推送到 Harbor（流式输出进度） | 可配置跳过 |
| B3 | 生成 README（模板填充） | 可配置跳过 |
| B4 | 发布到 ModelScope（SDK + CLI 降级） | 可配置跳过 |
| B5 | 发布到 HuggingFace（SDK + CLI 降级） | 可配置跳过 |

---

# 镜像命名规范

## Tag 格式

```
{registry}/flagrelease-{vendor}-release-model_{model}-tree_{tree}-gems_{gems}-scale_{scale}-cx_{cx}-python_{python}-torch_{backend}-{torch_version}-pcp_{sdk}-gpu_{gpu_code}-arc_{arch}-driver_{driver}:{YYYYMMDDHHMM}
```

## 示例

```
harbor.baai.ac.cn/flagrelease-public/flagrelease-nvidia-release-model_qwen3-8b-tree_none-gems_4.2.1rc0-scale_none-cx_none-python_3.12.3-torch_cuda-2.9.0-pcp_cuda13.1-gpu_nvidia003-arc_amd64-driver_570.158.01:202603301143
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
- `traces/06_image_package.json` 已写入（记录 commit/tag/push 命令）
- `traces/07_publish.json` 已写入（记录 README 路径、ModelScope/HuggingFace URL）
- `results/release_info.json` 已保存（Harbor URL、ModelScope URL、HuggingFace URL）
- `timing.steps.release` 已更新为本步骤的 `duration_seconds`

---

# 故障排查

| 问题 | 解决方案 |
|------|----------|
| 芯片检测失败 | 在配置中手动指定 `chip.vendor` |
| Harbor 推送失败 | 检查 `docker login harbor.baai.ac.cn` |
| ModelScope 上传失败 | 检查 `MODELSCOPE_TOKEN` 环境变量 |
| HuggingFace 上传失败 | 检查 `HF_TOKEN` 环境变量 |
| 镜像 tag 生成异常 | 使用 `--dry-run` 检查自动生成的配置 |
| 已有 Harbor 镜像 | 配置 `publish.existing_harbor_image` 跳过 commit/tag/push |
| 权重文件过大 | 上传自动重试（5 次，指数退避） |
