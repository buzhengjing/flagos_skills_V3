#!/usr/bin/env python3
"""
共享工具函数 — 环境变量相关

供 toggle_flaggems.py / apply_op_config.py / operator_optimizer.py 等脚本复用。
"""


def env_to_inline(env_dict):
    """将 env dict 转为内联前缀字符串: VAR1=val1 VAR2=val2"""
    parts = []
    for k, v in env_dict.items():
        if " " in v or "'" in v:
            parts.append(f"{k}='{v}'")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)
