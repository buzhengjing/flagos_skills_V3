# 服务启动执行记录模板

每次执行容器创建和服务启动任务后，按此模板记录关键操作和结果，供他人手动复现。

---

## 基本信息

| 项目 | 值 |
|------|-----|
| 模型 | |
| 容器名 | |
| 镜像地址 | |
| GPU | ×（如 8× H20） |
| 执行日期 | |
| 最终结论 | 成功 / 失败 |

---

## 1. 容器准备

### 1.1 创建容器（已有容器跳过）

```bash
# 实际执行的 docker run 命令
docker run -itd --name=<容器名> --gpus=all --network=host --shm-size=64g \
    -v <宿主机模型路径>:<容器内模型路径> \
    -v /data/flagos-workspace/<模型名>:/flagos-workspace \
    <镜像地址>
```

结果：成功 / 失败（错误信息）

### 1.2 模型路径

| 项 | 路径 |
|----|------|
| 宿主机模型路径 | |
| 容器内模型路径 | |

### 1.3 部署工具脚本

```bash
bash skills/flagos-container-preparation/tools/setup_workspace.sh <容器名>
```

结果：成功 / 失败（错误信息）

---

## 2. 环境检测

```bash
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/inspect_env.py --output-json"
```

关键输出：

| 项 | 值 |
|----|-----|
| env_type | native / vllm_flaggems / vllm_plugin_flaggems |
| torch | |
| vllm | |
| flaggems | |
| has_plugin | true / false |

---

## 3. 服务启动

### 3.1 GPU 检测

```bash
docker exec <容器名> bash -c "nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader"
```

GPU 信息：

### 3.2 TP 推算

```bash
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/calc_tp_size.py --model-path <模型路径> --json"
```

推荐 TP：

### 3.3 FlagGems 切换（env_type 非 native 时）

```bash
# 实际执行的 toggle 命令
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py --action <enable/disable> [--integration-type plugin] --json"
```

目标模式：native / flagos

### 3.4 启动服务

```bash
# 实际执行的启动命令（记录完整参数）
docker exec -d <容器名> bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH \
    [环境变量前缀] vllm serve <模型路径> \
    --host 0.0.0.0 \
    --port <端口> \
    --served-model-name <模型名> \
    --tensor-parallel-size <TP> \
    --max-model-len <值> \
    --trust-remote-code \
    [其他参数] \
    > /flagos-workspace/logs/startup_<mode>.log 2>&1"
```

启动参数：

| 参数 | 值 |
|------|-----|
| 端口 | |
| TP | |
| max-model-len | |
| 环境变量 | |

### 3.5 等待服务就绪

```bash
docker exec <容器名> bash -c "bash /flagos-workspace/scripts/wait_for_service.sh --port <端口> --model-name '<模型名>' --timeout 300"
```

结果：成功 / 超时 / 失败（错误信息）

---

## 4. 服务验证

### 4.1 检查模型列表

```bash
curl -s http://localhost:<端口>/v1/models
```

返回：成功 / 失败（错误信息）

### 4.2 推理测试

```bash
curl -s http://localhost:<端口>/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"<模型名>","messages":[{"role":"user","content":"hello"}],"max_tokens":10}'
```

返回：成功 / 失败（错误信息）

### 4.3 算子列表检查（FlagGems 模式）

```bash
docker exec <容器名> bash -c "cat /tmp/flaggems_enable_oplist.txt 2>/dev/null | wc -l || echo 'OPLIST_NOT_FOUND'"
```

算子数量：

---

## 5. Issue 生成（服务启动失败时）

### 5.1 生成 Issue 文件

服务启动失败时，自动生成 issue markdown 文件：

```bash
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/issue_reporter.py full \
    --type operator-crash \
    --log-path /flagos-workspace/logs/startup_<mode>.log \
    --context-yaml /flagos-workspace/shared/context.yaml \
    --repo flagos-ai/FlagGems \
    --output-dir /flagos-workspace/results/ \
    --json"
```

Issue 类型：

| 场景 | type 参数 |
|------|----------|
| 服务启动崩溃 | `operator-crash` |
| 环境缺失 flaggems/plugin/flagtree | `flagtree-error` |
| 推理请求失败 | `operator-crash` |

### 5.2 提交到 GitHub（可选）

```bash
# 加 --submit 参数自动提交（需要 GITHUB_TOKEN 环境变量）
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/issue_reporter.py full \
    --type operator-crash \
    --log-path /flagos-workspace/logs/startup_<mode>.log \
    --context-yaml /flagos-workspace/shared/context.yaml \
    --repo flagos-ai/FlagGems \
    --output-dir /flagos-workspace/results/ \
    --submit \
    --json"
```

生成的 Issue 文件：

| 文件 | 路径 |
|------|------|
| Issue markdown | `/flagos-workspace/results/issue_<type>_<repo>_<timestamp>.md` |

---

## 异常记录

| 步骤 | 异常现象 | 处理方式 | Issue 文件 |
|------|---------|---------|-----------|
| | | | |

---

## 关键日志位置

| 日志 | 路径 |
|------|------|
| 服务启动日志 | `/flagos-workspace/logs/startup_<mode>.log` |
| 宿主机工作目录 | `/data/flagos-workspace/<模型名>/` |
| 算子列表 | `/tmp/flaggems_enable_oplist.txt` |

---

## 复现命令清单

> 将上述实际执行的命令按顺序整理，供他人直接复制执行。

```bash
# 1. 创建容器
docker run -itd --name=<容器名> ...

# 2. 部署工具
bash skills/flagos-container-preparation/tools/setup_workspace.sh <容器名>

# 3. 环境检测
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/inspect_env.py --output-json"

# 4. FlagGems 切换（如需）
docker exec <容器名> bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/toggle_flaggems.py ..."

# 5. 启动服务
docker exec -d <容器名> bash -c "cd /flagos-workspace && PATH=/opt/conda/bin:\$PATH vllm serve ..."

# 6. 等待就绪
docker exec <容器名> bash -c "bash /flagos-workspace/scripts/wait_for_service.sh --port <端口> --model-name '<模型名>' --timeout 300"

# 7. 验证
curl -s http://localhost:<端口>/v1/models
curl -s http://localhost:<端口>/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"<模型名>","messages":[{"role":"user","content":"hello"}],"max_tokens":10}'
```
