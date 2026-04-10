---
name: flagos-container-preparation
description: 多入口容器准备，支持已有容器/已有镜像/ModelScope URL，通过 setup_workspace.sh 一次性部署所有工具
version: 5.0.0
triggers:
  - container preparation
  - prepare container
  - 容器准备
  - 环境准备
depends_on: []
next_skill: flagos-pre-service-inspection
provides:
  - container.name
  - container.status
  - model.name
  - model.local_path
  - gpu.vendor
  - gpu.count
  - entry.type
---

# 容器准备 Skill

支持三种入口，自动识别用户输入类型。容器就绪后通过 `setup_workspace.sh` 一次性部署所有工具脚本。

---

# 用户输入

| 入口 | 用户提供 | 系统做什么 |
|------|---------|-----------|
| **已有容器** | 容器名称 | 跳过创建，直接验证 |
| **已有镜像** | 镜像地址 + 模型名 + 宿主机模型路径 | docker run 创建容器 |
| **ModelScope URL** | URL | API 解析 → docker pull → docker run |

---

# 工作流程

## 步骤 1 — 本地权重检查与自动下载

```bash
python3 skills/flagos-container-preparation/tools/check_model_local.py \
    --model "<用户输入的模型名或URL>" --output-json
```

- 找到本地权重 → 记录 `model.local_path`，docker run 直接挂载
- 未找到 → 自动从 ModelScope 下载到 `/mnt/data/models/<model_name>`，下载后校验
- 下载成功 → 记录下载路径为 `model.local_path`
- 下载失败或纯模型名无 org → 要求用户提供 `org/model` 格式或手动指定路径
- `--no-download` 可禁用自动下载（仅搜索本地）
- `--download-dir` 可指定下载目录（默认 `/mnt/data/models`）

## 入口 1 — 已有容器

```bash
docker inspect <container_name> --format '{{.State.Status}}'
docker start <container_name>  # 如果停止
```

自动检测 GPU、模型路径、创建/验证 `/flagos-workspace` 目录。

## 入口 2 — 已有镜像

1. 自动检测 GPU 厂商
2. **根据 GPU 厂商选择对应模板**，填充变量后生成 docker run 命令并自动执行
3. 验证容器状态

### docker run 命令模板

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `${CONTAINER_NAME}` | 容器名称 | `<model_name>_flagos` |
| `${MODEL_PATH}` | 宿主机模型路径 | 用户提供 |
| `${CONTAINER_MODEL_PATH}` | 容器内模型路径 | 用户提供 |
| `${WORKSPACE_PATH}` | 宿主机工作目录 | `/data/flagos-workspace` |
| `${SHM_SIZE}` | 共享内存 | `64g` |
| `${IMAGE}` | 镜像地址 | 用户提供 |

#### 模板 A：NVIDIA

```bash
docker run -d --name ${CONTAINER_NAME} \
    --net=host --ipc=host --privileged \
    --gpus all --shm-size=${SHM_SIZE:-64g} \
    --ulimit memlock=-1 --security-opt=seccomp=unconfined \
    -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} \
    -v ${WORKSPACE_PATH:-/data/flagos-workspace}:/flagos-workspace \
    ${IMAGE} sleep infinity
```

#### 模板 B：Ascend（华为昇腾）

```bash
docker run -d --name ${CONTAINER_NAME} \
    --net=host --ipc=host --privileged \
    --shm-size=${SHM_SIZE:-64g} \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /usr/local/dcmi:/usr/local/dcmi \
    -v /usr/local/sbin:/usr/local/sbin \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} \
    -v ${WORKSPACE_PATH:-/data/flagos-workspace}:/flagos-workspace \
    -e PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256 \
    ${IMAGE} sleep infinity
```

#### 模板 C：Moore Threads（摩尔线程）

```bash
docker run -d --name ${CONTAINER_NAME} \
    --net=host --ipc=host --privileged \
    --shm-size=${SHM_SIZE:-16g} \
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    --tmpfs /tmp:exec \
    -e MTHREADS_VISIBLE_DEVICES=all \
    -e MTHREADS_DRIVER_CAPABILITIES=all \
    -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} \
    -v ${WORKSPACE_PATH:-/data/flagos-workspace}:/flagos-workspace \
    ${IMAGE} sleep infinity
```

#### 模板 D：MetaX（沐曦）

```bash
docker run -d --name ${CONTAINER_NAME} \
    --net=host --ipc=host --privileged \
    --shm-size=${SHM_SIZE:-64g} \
    --group-add video --ulimit memlock=-1 \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    --device=/dev/dri --device=/dev/mxcd \
    -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} \
    -v ${WORKSPACE_PATH:-/data/flagos-workspace}:/flagos-workspace \
    ${IMAGE} sleep infinity
```

#### 模板 E：Cambricon（寒武纪）

```bash
docker run -d --name ${CONTAINER_NAME} \
    --net=host --pid=host --ipc=host --privileged \
    -v /usr/bin/cnmon:/usr/bin/cnmon \
    -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} \
    -v ${WORKSPACE_PATH:-/data/flagos-workspace}:/flagos-workspace \
    -v /data:/data \
    ${IMAGE} sleep infinity
```

**模板规则**：
- 业务环境变量（`USE_FLAGGEMS`、`VLLM_USE_V1` 等）不写入模板，由后续 skill 按需添加
- 所有模板统一挂载 `/flagos-workspace`
- 生成命令后自动执行，无需用户确认

## 入口 3 — ModelScope / HuggingFace URL

1. 从 URL 提取 `<owner>/<model_name>`，调用 API 获取 README：

```bash
curl -s "https://modelscope.cn/api/v1/models/<owner>/<model_name>"
```

2. 从返回的 README 中提取：镜像地址、启动参数、模型路径
3. docker pull + 按入口 2 流程创建容器

**API 访问失败时**，模型名称和路径已从 URL 推导，只需用户补充镜像地址。

## 步骤 — 部署工具脚本

容器就绪后立即执行：

```bash
bash skills/flagos-container-preparation/tools/setup_workspace.sh $CONTAINER $MODEL_NAME
```

第二参数 `$MODEL_NAME` 为模型名称，传入后自动在宿主机创建 `/data/flagos-workspace/<MODEL_NAME>/` 及其子目录（results/traces/logs/config）。

此命令会：
1. 检测容器内 `results/`、`traces/`、`logs/` 是否有上一轮数据，若有则自动归档到 `archive/<YYYYMMDD_HHMMSS>/`
2. 宿主机同步归档（如传了 MODEL_NAME）
3. 创建干净的 `results/`、`traces/`、`logs/`、`config/` 目录
4. 部署所有工具脚本

## 步骤 — 写入 context.yaml

```yaml
entry:
  type: "<existing_container|new_container|url_parse>"
container:
  name: "<容器名称>"
  status: "running"
model:
  name: "<模型名称>"
  local_path: "<宿主机路径>"
  container_path: "<容器内路径>"
gpu:
  vendor: "<nvidia|huawei|mthreads|metax|cambricon>"
  type: "<GPU 型号>"
  count: <数量>
workspace:
  host_path: "/data/flagos-workspace"
  container_path: "/flagos-workspace"
```

---

# 完成条件

- 容器已运行，GPU 可见，模型目录已确认
- 工具脚本已通过 setup_workspace.sh 部署
- 四个子目录已创建（results/、traces/、logs/、config/）
- context.yaml 已更新
- `traces/01_container_preparation.json` 已写入（记录 docker run 命令、setup_workspace 部署结果）
- `timing.workflow_start` 已写入 context.yaml（ISO 8601，流程起始时间）
- `timing.steps.container_preparation` 已更新为本步骤的 `duration_seconds`

---

# 故障排查

| 问题 | 解决方案 |
|------|----------|
| GPU 未检测到 | 检查驱动安装 |
| 镜像拉取失败 | 检查网络，或 `docker load` 导入 |
| setup_workspace.sh 失败 | 检查容器是否运行，手动 docker cp |

下一步：**flagos-pre-service-inspection**
