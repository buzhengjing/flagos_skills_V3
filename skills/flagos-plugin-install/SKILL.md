---
name: flagos-plugin-install
description: vllm-plugin-FL 组件安装/验证/卸载，安装后复用 V1 基线进行精度性能验证
version: 1.0.0
triggers:
  - 安装 plugin
  - install plugin
  - plugin 安装
  - vllm-plugin
depends_on: []
provides:
  - plugin_install.installed
  - plugin_install.version
  - plugin_install.success
---

# Plugin 安装 Skill

在 flaggems+flagtree 环境精度性能双达标后，安装 vllm-plugin-FL 组件并验证。

**工具脚本**（已由 setup_workspace.sh 部署到容器）：
- `install_plugin.py` — plugin 安装/验证/卸载

**前置条件**：
- flaggems + flagtree 环境已就绪
- 精度性能双达标（`workflow.accuracy_ok=true && workflow.performance_ok=true`）

**目标仓库**：`https://github.com/flagos-ai/vllm-plugin-FL`

---

# 上下文集成

## 从容器内 /flagos-workspace/shared/context.yaml 读取

```yaml
container:
  name: <来自 container-preparation>
model:
  name: <来自 container-preparation>
gpu:
  vendor: <来自 container-preparation>
workflow:
  accuracy_ok: <来自 eval-comprehensive>
  performance_ok: <来自 performance-testing>
environment:
  has_plugin: <来自 pre-service-inspection>
inspection:
  vllm_plugin_installed: <来自 pre-service-inspection>
```

## 写入容器内 /flagos-workspace/shared/context.yaml

```yaml
plugin_install:
  installed: true|false
  version: "<version>"
  repo_url: "<仓库地址>"
  install_method: "source|editable"
  success: true|false
  timestamp: "<ISO 8601>"
```

---

# 工作流程

## 步骤 1 — 检查前置条件

确认 flaggems+flagtree 环境精度性能双达标：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
ctx = yaml.safe_load(open('/flagos-workspace/shared/context.yaml'))
wf = ctx.get('workflow', {})
print(f'accuracy_ok={wf.get(\"accuracy_ok\")}, performance_ok={wf.get(\"performance_ok\")}')
\""
```

## 步骤 2 — 检查当前 plugin 状态

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py --action verify --json"
```

## 步骤 3 — 安装 plugin

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py \
    --action install --json"
```

指定分支：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py \
    --action install --branch main --json"
```

Editable 安装（开发调试用）：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py \
    --action install --editable --json"
```

带代理：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py \
    --action install --proxy http://proxy:port --json"
```

## 步骤 4 — 验证安装

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py --action verify --json"
```

## 步骤 5 — 安装后验证流程

安装成功后，复用已有 V1(native) 基线，只跑 plugin 版本：

1. 启动服务（plugin 模式）
2. 精度评测 — 与 V1 基线对比
3. 性能评测 — 与 V1 基线对比

遇到 plugin 相关报错时，调用 issue_reporter 提交到 `flagos-ai/vllm-plugin-FL`：
```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/issue_reporter.py full \
    --type plugin-error \
    --log-path /flagos-workspace/logs/startup_flagos.log \
    --context-yaml /flagos-workspace/shared/context.yaml \
    --repo flagos-ai/vllm-plugin-FL \
    --output-dir /flagos-workspace/results/ \
    --json"
```

## 步骤 6 — 卸载（如需回退）

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/install_plugin.py --action uninstall --json"
```

---

# 完成条件

- plugin 安装成功，版本已确认
- context.yaml `plugin_install` 字段已更新
- 安装后服务可正常启动
- 精度/性能与 V1 基线对比完成
- 遇到 plugin 报错时 issue 已提交到 `flagos-ai/vllm-plugin-FL`

---

# 故障排查

| 问题 | 解决方案 |
|------|----------|
| git clone 失败 | 检查网络，使用 `--proxy` 参数 |
| pip install 编译失败 | 确认 `--no-build-isolation` 已使用，检查构建依赖 |
| import 失败 | 检查 Python 环境，确认 conda 环境激活 |
| 服务启动后 plugin 未生效 | 检查 `VLLM_FL_PREFER_ENABLED` 环境变量 |
| plugin 与 flaggems 冲突 | 卸载 plugin 回退到 flaggems+flagtree 环境 |
