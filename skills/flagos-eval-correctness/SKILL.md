---
name: flagos-eval-correctness
description: "[DEPRECATED] 已合并到 flagos-eval-comprehensive。本 Skill 仅保留重定向。"
version: 7.0.0
deprecated: true
redirect: flagos-eval-comprehensive
triggers:
  - 远端评测
  - FlagRelease 评测
  - remote eval
  - eval correctness
depends_on:
  - flagos-service-startup
---

# ⚠️ 本 Skill 已废弃

**远端评测和本地评测已统一合并到 `flagos-eval-comprehensive` Skill。**

请使用 `flagos-eval-comprehensive`，它包含：

| 模块 | 功能 |
|------|------|
| 模块 A | 本地快速评测（GPQA Diamond） |
| 模块 B | 远端 flageval 正式评测（原 eval-correctness 的全部功能） |
| 模块 C | V1 vs V2 精度对比（5% 阈值） |
| 模块 D | 错误处理与算子排查 |

**触发方式**：使用以下任意触发词即可：
- `精度评测`、`远端评测`、`FlagRelease 评测`、`flageval`、`GPQA`、`quick 评测`

**工具脚本**：`eval_monitor.py` 已迁移到 `skills/flagos-eval-comprehensive/tools/`。
