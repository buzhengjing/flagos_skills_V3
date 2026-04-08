---
name: flagos-log-analyzer
description: 分析推理服务日志以诊断启动失败、运行时错误、GPU 问题或 FlagGems 集成问题，提供失败恢复指引
version: 2.0.0
license: internal
triggers:
  - log analysis
  - analyze logs
  - 日志分析
depends_on: []
provides:
  - diagnosis.status
  - diagnosis.errors
  - diagnosis.suggestions
---

# 日志分析 Skill

此 Skill 分析推理服务生成的日志，以识别部署或运行时问题，并提供恢复指引。

支持 `${CMD_PREFIX}` 双执行模式（宿主机或容器内）。

典型日志包括：

- vLLM 启动日志
- SGLang 服务器日志
- CUDA 运行时日志
- FlagGems 相关日志

目标是自动识别常见的部署问题并提供诊断反馈。

---

# 统一工作目录

**重要**：由于使用了统一工作目录挂载，日志文件可直接在宿主机访问，**无需 docker exec**。

```
宿主机日志路径: /data/flagos-workspace/<model_name>/
                    │
                    ├── output/           # 服务启动日志
                    │   └── <服务名>/
                    │       └── serve/
                    │           └── *.log
                    │
                    └── eval/             # 评测日志
                        └── eval_*.log
```

**宿主机直接访问日志**：
```bash
# 查找所有日志文件
find /data/flagos-workspace/<model_name> -name "*.log"

# 实时查看服务日志
tail -f /data/flagos-workspace/<model_name>/output/**/*.log
```

---

# 工作流程

按顺序执行步骤。**所有日志分析操作均在宿主机执行，无需进入容器。**

---

## 步骤 1 — 定位日志文件（宿主机直接访问）

在宿主机查找日志文件：

```bash
# 查找工作目录下所有日志
find /data/flagos-workspace/<model_name> -name "*.log" -type f

# 查看日志文件详情
ls -la /data/flagos-workspace/<model_name>/output/
```

常见日志位置：

| 类型 | 宿主机路径 |
|------|-----------|
| 服务日志 | `/data/flagos-workspace/<model>/output/<服务名>/serve/*.log` |
| 评测日志 | `/data/flagos-workspace/<model>/eval/eval_*.log` |

结果反馈必须包括：

- 检测到的日志文件路径
- 日志大小
- 最后修改时间

---

## 步骤 2 — 检查最近的日志输出

在宿主机显示最新的日志行：

```bash
# 宿主机直接查看（无需 docker exec）
tail -n 100 /data/flagos-workspace/<model_name>/output/**/*.log
```

关注：

- 启动序列
- 模型加载
- GPU 初始化
- 服务器端口绑定

结果反馈：

- 服务启动状态
- 最后的日志消息

---

## 步骤 3 — 检测常见启动错误

在宿主机搜索常见的失败关键词：

```bash
# 宿主机直接搜索（无需 docker exec）
LOG_DIR="/data/flagos-workspace/<model_name>/output"

grep -ri "error" $LOG_DIR
grep -ri "cuda" $LOG_DIR
grep -ri "oom" $LOG_DIR
grep -ri "traceback" $LOG_DIR
```

典型失败类型：

- GPU 内存问题
- CUDA 驱动不匹配
- 缺少模型文件
- Tokenizer 错误
- 依赖冲突

结果反馈：

- 检测到的错误类别
- 相关日志行

---

## 步骤 4 — 检测 FlagGems 执行

在宿主机搜索 FlagGems 执行消息：

```bash
# 宿主机直接搜索（无需 docker exec）
grep -ri "gems\|flag_gems" /data/flagos-workspace/<model_name>/output/
```

典型模式：

```
flag_gems.ops loaded
GEMS MUL
GEMS RECIPROCAL
```

这些日志表明 FlagGems 加速算子正在执行。

结果反馈：

- 是否检测到 FlagGems
- 相关日志条目

---

## 步骤 5 — 检测 GPU 或内存错误

在宿主机搜索 GPU 相关问题：

```bash
# 宿主机直接搜索（无需 docker exec）
grep -riE "CUDA out of memory|device not found|driver mismatch|OOM" /data/flagos-workspace/<model_name>/output/
```

结果反馈：

- GPU 错误状态
- 可能的原因

---

## 步骤 6 — 提供诊断

总结发现。

可能的结果：

服务启动成功
服务运行但 API 无法访问
模型加载失败
GPU 内存不足
FlagGems 未启用

根据检测到的问题提供建议。

示例：

减少张量并行大小
检查模型路径
验证 CUDA 兼容性
使用正确的参数重启服务

---

# 完成条件

日志分析完成的标志：

- 日志文件已检查
- 错误已分类
- 诊断已生成
- 可能的解决方案已建议
- 如在流程中调用，`timing.steps` 中对应步骤的耗时已更新

---

# 失败恢复指引

## 服务启动失败

```
诊断: 启动失败
  │
  ├── FlagOS 模式失败
  │   → 保存日志到 /flagos-workspace/logs/
  │   → 自动切回 Native 模式验证
  │   │
  │   ├── Native 也失败 → 环境问题，需人工介入
  │   └── Native 成功 → FlagGems 问题，触发算子优化
  │
  └── Native 模式失败
      → 检查 GPU 驱动、显存、模型路径
      → 建议调整 tensor-parallel-size
```

## Benchmark 失败

```
诊断: Benchmark 失败
  │
  ├── 单次失败 → 自动重试 1 次
  ├── 重试后仍失败 → 跳过当前 case，继续下一个
  └── 服务在测试中挂掉 → 重启服务 → 从失败的 case 继续
```

## 算子优化中途失败

```
诊断: 优化中断
  │
  ├── 进度已自动保存到 operator_config.json
  ├── 恢复上一个可用配置
  └── 支持断点继续：operator_optimizer.py next
```
