#!/usr/bin/env python3
"""
toggle_flaggems.py — 可靠的 FlagGems 开关切换

替代脆弱的 sed 行号操作，使用正则匹配 + 自动备份。

Usage:
    python3 toggle_flaggems.py --action enable    # 启用 FlagGems
    python3 toggle_flaggems.py --action disable   # 关闭 FlagGems
    python3 toggle_flaggems.py --action status    # 查看当前状态
    python3 toggle_flaggems.py --action rollback  # 回滚到备份版本
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 共享模块导入（兼容本地开发和容器内扁平部署）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "shared"))

from env_utils import env_to_inline


# FlagGems 相关的代码模式
FLAGGEMS_PATTERNS = [
    re.compile(r"^(\s*)(import flag_gems.*)$"),
    re.compile(r"^(\s*)(from flag_gems.*)$"),
    re.compile(r"^(\s*)(flag_gems\.\w+.*)$"),
]

COMMENTED_PATTERNS = [
    re.compile(r"^(\s*)#\s*(import flag_gems.*)$"),
    re.compile(r"^(\s*)#\s*(from flag_gems.*)$"),
    re.compile(r"^(\s*)#\s*(flag_gems\.\w+.*)$"),
]

BACKUP_SUFFIX = ".flaggems_backup"


def detect_plugin_mode():
    """检测是否为 plugin 场景"""
    try:
        import vllm_fl
        return True
    except ImportError:
        return False


def generate_env_vars(action):
    """Plugin 场景：生成环境变量字典（不再写文件）"""
    env = {}
    if action == "enable":
        env["USE_FLAGGEMS"] = "1"
        env["VLLM_FL_PREFER_ENABLED"] = "true"
    elif action == "disable":
        env["USE_FLAGGEMS"] = "0"
        env["VLLM_FL_PREFER_ENABLED"] = "false"
    return env


def find_model_runner_files():
    """自动扫描所有 model_runner.py 文件"""
    candidates = []
    search_dirs = [
        "/usr/local/lib",
        "/usr/lib",
        "/opt",
    ]
    # 也通过 Python 路径查找
    try:
        import vllm
        vllm_path = Path(vllm.__path__[0])
        search_dirs.append(str(vllm_path.parent))
    except ImportError:
        pass
    try:
        import sglang
        sgl_path = Path(sglang.__path__[0])
        search_dirs.append(str(sgl_path.parent))
    except ImportError:
        pass

    for search_dir in search_dirs:
        search_path = Path(search_dir)
        if not search_path.exists():
            continue
        for py_file in search_path.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if "flag_gems" in content:
                    candidates.append(str(py_file))
            except (PermissionError, OSError):
                continue

    return sorted(set(candidates))


def get_file_status(filepath):
    """检查单个文件的 FlagGems 状态"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except Exception as e:
        return {"file": filepath, "error": str(e)}

    lines = content.split("\n")
    active_lines = []
    commented_lines = []

    for i, line in enumerate(lines, 1):
        for pat in FLAGGEMS_PATTERNS:
            if pat.match(line):
                active_lines.append({"line": i, "content": line.strip()})
                break
        for pat in COMMENTED_PATTERNS:
            if pat.match(line):
                commented_lines.append({"line": i, "content": line.strip()})
                break

    status = "unknown"
    if active_lines and not commented_lines:
        status = "enabled"
    elif commented_lines and not active_lines:
        status = "disabled"
    elif active_lines and commented_lines:
        status = "mixed"
    elif not active_lines and not commented_lines:
        status = "not_found"

    has_backup = Path(filepath + BACKUP_SUFFIX).exists()

    return {
        "file": filepath,
        "status": status,
        "active_lines": active_lines,
        "commented_lines": commented_lines,
        "has_backup": has_backup,
    }


def backup_file(filepath):
    """备份文件"""
    backup_path = filepath + BACKUP_SUFFIX
    shutil.copy2(filepath, backup_path)
    return backup_path


def disable_flaggems(filepath):
    """注释掉 FlagGems 相关代码"""
    content = Path(filepath).read_text(encoding="utf-8")
    lines = content.split("\n")
    modified = False

    new_lines = []
    for line in lines:
        commented = False
        for pat in FLAGGEMS_PATTERNS:
            match = pat.match(line)
            if match:
                indent = match.group(1)
                code = match.group(2)
                new_lines.append(f"{indent}# {code}")
                commented = True
                modified = True
                break
        if not commented:
            new_lines.append(line)

    if modified:
        backup_file(filepath)
        Path(filepath).write_text("\n".join(new_lines), encoding="utf-8")

    return modified


def enable_flaggems(filepath):
    """取消注释 FlagGems 相关代码"""
    content = Path(filepath).read_text(encoding="utf-8")
    lines = content.split("\n")
    modified = False

    new_lines = []
    for line in lines:
        uncommented = False
        for pat in COMMENTED_PATTERNS:
            match = pat.match(line)
            if match:
                indent = match.group(1)
                code = match.group(2)
                new_lines.append(f"{indent}{code}")
                uncommented = True
                modified = True
                break
        if not uncommented:
            new_lines.append(line)

    if modified:
        backup_file(filepath)
        Path(filepath).write_text("\n".join(new_lines), encoding="utf-8")

    return modified


def rollback_file(filepath):
    """从备份恢复文件"""
    backup_path = filepath + BACKUP_SUFFIX
    if not Path(backup_path).exists():
        return False
    shutil.copy2(backup_path, filepath)
    return True


def verify_change(filepath, expected_status):
    """验证修改后状态是否正确"""
    status = get_file_status(filepath)
    return status["status"] == expected_status


def analyze_flaggems_code():
    """分析所有含 flag_gems 的文件，提取 enable() 调用和 txt 路径"""
    files = find_model_runner_files()
    result = {
        "files": files,
        "enable_calls": [],
        "gems_txt_path": None,
        "auto_detect_needed": False,
    }

    if not files:
        result["auto_detect_needed"] = True
        return result

    enable_pattern = re.compile(r"flag_gems\.\w*enable\w*\s*\(")

    for filepath in files:
        try:
            content = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            if enable_pattern.search(line):
                # 拼接多行调用（到闭合括号）
                call_text = line
                paren_depth = line.count("(") - line.count(")")
                for j in range(i, min(i + 10, len(lines))):
                    if paren_depth <= 0:
                        break
                    call_text += "\n" + lines[j]
                    paren_depth += lines[j].count("(") - lines[j].count(")")

                call_stripped = call_text.strip()
                txt_path = _extract_txt_path(call_stripped)

                entry = {
                    "file": filepath,
                    "line": i,
                    "call": call_stripped,
                    "txt_path": txt_path,
                }
                result["enable_calls"].append(entry)

                if txt_path and not result["gems_txt_path"]:
                    result["gems_txt_path"] = txt_path

    if not result["gems_txt_path"]:
        result["auto_detect_needed"] = True

    return result


def _extract_txt_path(call_content):
    """从 flag_gems.enable() 调用中提取 txt 文件路径"""
    patterns = [
        # 关键字参数: unused="/root/gems.txt", record_log="/tmp/gems.txt"
        r"""(?:unused|record_log|log_file|output)\s*=\s*["']([^"']*\.txt)["']""",
        # 位置参数中的 .txt 路径
        r"""["'](/[^"']*\.txt)["']""",
    ]
    for pattern in patterns:
        m = re.search(pattern, call_content)
        if m:
            return m.group(1)
    return None


def find_gems_txt_files():
    """在容器内搜索 FlagGems 生成的算子 txt 文件"""
    import subprocess as sp

    search_dirs = ["/root", "/tmp", "/opt", "/var/tmp"]
    # 也搜索 flag_gems 包目录
    try:
        import flag_gems
        search_dirs.append(os.path.dirname(flag_gems.__file__))
    except ImportError:
        pass

    # 常见算子名模式（用于匹配 txt 文件内容）
    op_keywords = ["aten::", "torch.", "addmm", "softmax", "layer_norm", "rms_norm",
                   "mm", "bmm", "cross_entropy", "gelu", "silu", "relu"]

    found_files = []
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        try:
            # 搜索 .txt 文件
            proc = sp.run(
                f"find {search_dir} -maxdepth 3 -name '*.txt' -size +0c 2>/dev/null",
                shell=True, capture_output=True, text=True, timeout=10
            )
            for fpath in proc.stdout.strip().split("\n"):
                fpath = fpath.strip()
                if not fpath or not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(4096)
                    # 检查是否包含算子名模式
                    matches = sum(1 for kw in op_keywords if kw in content)
                    if matches >= 2:
                        lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
                        found_files.append({
                            "path": fpath,
                            "line_count": len(lines),
                            "sample_lines": lines[:5],
                            "keyword_matches": matches,
                        })
                except Exception:
                    continue
        except Exception:
            continue

    # 按匹配度排序
    found_files.sort(key=lambda x: x["keyword_matches"], reverse=True)

    recommended = found_files[0]["path"] if found_files else None

    return {
        "found_files": found_files,
        "recommended": recommended,
    }


def modify_enable_call(files, enabled_ops=None, disabled_ops=None):
    """修改 flag_gems.enable() 调用以控制算子子集（算子优化用）

    根据 flaggems capabilities 自动选择修改方式：
    - only_enable → flag_gems.only_enable(include=[...])
    - enable_unused → flag_gems.enable(unused=[...])
    - 兜底 → 直接写 txt 文件
    """
    # 探测 capabilities
    caps = []
    try:
        import flag_gems
        if hasattr(flag_gems, "only_enable"):
            caps.append("only_enable")
        if hasattr(flag_gems, "enable"):
            import inspect as insp_mod
            sig = insp_mod.signature(flag_gems.enable)
            if "unused" in list(sig.parameters.keys()):
                caps.append("enable_unused")
    except ImportError:
        pass

    if not files:
        files = find_model_runner_files()

    results = []
    for filepath in files:
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except Exception as e:
            results.append({"file": filepath, "success": False, "error": str(e)})
            continue

        original = content
        modified = False
        method = "unknown"

        if enabled_ops is not None and "only_enable" in caps:
            # 改写为 flag_gems.only_enable(include=[...])
            ops_str = ", ".join(f'"{op}"' for op in sorted(enabled_ops))
            content, count = re.subn(
                r"flag_gems\.(?:only_)?enable\s*\([^)]*\)",
                f"flag_gems.only_enable(include=[{ops_str}])",
                content
            )
            if count == 0:
                # 尝试匹配多行
                content, count = re.subn(
                    r"flag_gems\.(?:only_)?enable\s*\(.*?\)",
                    f"flag_gems.only_enable(include=[{ops_str}])",
                    content,
                    flags=re.DOTALL
                )
            modified = count > 0
            method = "only_enable"

        elif disabled_ops is not None and "enable_unused" in caps:
            # 改写为 flag_gems.enable(unused=[...])
            ops_str = ", ".join(f'"{op}"' for op in sorted(disabled_ops))
            content, count = re.subn(
                r"flag_gems\.enable\s*\([^)]*\)",
                f"flag_gems.enable(unused=[{ops_str}])",
                content
            )
            if count == 0:
                content, count = re.subn(
                    r"flag_gems\.enable\s*\(.*?\)",
                    f"flag_gems.enable(unused=[{ops_str}])",
                    content,
                    flags=re.DOTALL
                )
            modified = count > 0
            method = "enable_unused"

        if modified and content != original:
            backup_path = backup_file(filepath)
            Path(filepath).write_text(content, encoding="utf-8")
            results.append({
                "file": filepath,
                "method": method,
                "backup": backup_path,
                "success": True,
            })
        elif not modified:
            # 兜底：写 txt 文件
            method = "txt_fallback"
            # 分析现有 enable 调用找到 txt 路径
            analysis = analyze_flaggems_code()
            txt_path = analysis.get("gems_txt_path")
            if txt_path and enabled_ops is not None:
                with open(txt_path, "w", encoding="utf-8") as f:
                    for op in sorted(enabled_ops):
                        f.write(f"{op}\n")
                results.append({
                    "file": txt_path,
                    "method": method,
                    "success": True,
                })
            else:
                results.append({
                    "file": filepath,
                    "method": method,
                    "success": False,
                    "error": "无法确定修改方式或 txt 路径",
                })

    return {
        "action": "modify-enable",
        "capabilities": caps,
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="FlagGems 开关切换工具")
    parser.add_argument(
        "--action",
        required=True,
        choices=["enable", "disable", "status", "rollback", "analyze", "find-gems-txt", "modify-enable"],
        help="操作类型",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        help="指定文件列表（不指定则自动扫描）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON 格式",
    )
    parser.add_argument(
        "--integration-type",
        choices=["auto", "plugin", "code_import"],
        default="auto",
        help="集成方式（auto=自动检测，plugin=环境变量模式）",
    )
    parser.add_argument(
        "--enabled-ops",
        help="modify-enable: 启用的算子列表（逗号分隔）",
    )
    parser.add_argument(
        "--disabled-ops",
        help="modify-enable: 禁用的算子列表（逗号分隔）",
    )
    args = parser.parse_args()

    # 新增 action: analyze
    if args.action == "analyze":
        result = analyze_flaggems_code()
        result["timestamp"] = datetime.now().isoformat()
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nFlagGems Analyze")
            print("=" * 50)
            print(f"  扫描文件数: {len(result['files'])}")
            for ec in result["enable_calls"]:
                print(f"  {ec['file']}:L{ec['line']}: {ec['call'][:80]}")
                if ec["txt_path"]:
                    print(f"    → txt 路径: {ec['txt_path']}")
            if result["gems_txt_path"]:
                print(f"\n  推荐 gems_txt_path: {result['gems_txt_path']}")
            elif result["auto_detect_needed"]:
                print(f"\n  未找到 txt 路径，需启动服务后调用 find-gems-txt")
        return

    # 新增 action: find-gems-txt
    if args.action == "find-gems-txt":
        result = find_gems_txt_files()
        result["timestamp"] = datetime.now().isoformat()
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nFlagGems TXT 文件搜索")
            print("=" * 50)
            for ff in result["found_files"]:
                print(f"  {ff['path']} ({ff['line_count']} 行, {ff['keyword_matches']} 关键词匹配)")
                for sl in ff["sample_lines"][:3]:
                    print(f"    | {sl}")
            if result["recommended"]:
                print(f"\n  推荐: {result['recommended']}")
            else:
                print(f"\n  未找到匹配的算子 txt 文件")
        return

    # 新增 action: modify-enable
    if args.action == "modify-enable":
        enabled = args.enabled_ops.split(",") if args.enabled_ops else None
        disabled = args.disabled_ops.split(",") if args.disabled_ops else None
        result = modify_enable_call(args.files or [], enabled_ops=enabled, disabled_ops=disabled)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nFlagGems Modify Enable")
            print("=" * 50)
            for r in result["results"]:
                status = "OK" if r.get("success") else "FAILED"
                print(f"  {r['file']} → {r.get('method', '?')} [{status}]")
        return

    # 确定集成方式
    is_plugin = False
    if args.integration_type == "plugin":
        is_plugin = True
    elif args.integration_type == "auto":
        is_plugin = detect_plugin_mode()

    # Plugin 模式：生成环境变量字典（不再写文件）
    if is_plugin and args.action in ("enable", "disable"):
        env_vars = generate_env_vars(args.action)
        inline = env_to_inline(env_vars)
        result = {
            "action": args.action,
            "mode": "plugin",
            "env_vars": env_vars,
            "env_inline": inline,
            "success": True,
            "message": f"Plugin 模式: 内联环境变量已生成 ({args.action})",
            "timestamp": datetime.now().isoformat(),
        }
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nFlagGems Toggle — {args.action} (Plugin 模式)")
            print("=" * 50)
            print(f"  env_vars: {env_vars}")
            print(f"  env_inline: {inline}")
            print(f"  提示: 在启动命令前添加内联环境变量")
        return

    if is_plugin and args.action == "status":
        # Plugin 模式下检查环境变量
        prefer = os.environ.get("VLLM_FL_PREFER_ENABLED", "not_set")
        use_flaggems = os.environ.get("USE_FLAGGEMS", "not_set")
        result = {
            "mode": "plugin",
            "USE_FLAGGEMS": use_flaggems,
            "VLLM_FL_PREFER_ENABLED": prefer,
            "status": "enabled" if prefer == "true" else ("disabled" if prefer == "false" else "unknown"),
        }
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\nFlagGems Toggle — status (Plugin 模式)")
            print("=" * 50)
            print(f"  USE_FLAGGEMS: {use_flaggems}")
            print(f"  VLLM_FL_PREFER_ENABLED: {prefer}")
        return

    # 非 plugin 模式：原有的源码注释/取消注释逻辑

    # 查找文件
    if args.files:
        files = args.files
    else:
        files = find_model_runner_files()

    if not files:
        result = {"success": False, "error": "未找到包含 flag_gems 的文件"}
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print("ERROR: 未找到包含 flag_gems 的文件")
        sys.exit(1)

    results = []

    if args.action == "status":
        for f in files:
            status = get_file_status(f)
            results.append(status)

    elif args.action == "disable":
        for f in files:
            before = get_file_status(f)
            if before.get("status") == "disabled":
                results.append({"file": f, "action": "skip", "reason": "already disabled"})
                continue
            modified = disable_flaggems(f)
            if modified and verify_change(f, "disabled"):
                results.append({"file": f, "action": "disabled", "success": True})
            elif not modified:
                results.append({"file": f, "action": "skip", "reason": "no active lines found"})
            else:
                results.append({"file": f, "action": "disabled", "success": False, "warning": "verification failed"})

    elif args.action == "enable":
        for f in files:
            before = get_file_status(f)
            if before.get("status") == "enabled":
                results.append({"file": f, "action": "skip", "reason": "already enabled"})
                continue
            modified = enable_flaggems(f)
            if modified and verify_change(f, "enabled"):
                results.append({"file": f, "action": "enabled", "success": True})
            elif not modified:
                results.append({"file": f, "action": "skip", "reason": "no commented lines found"})
            else:
                results.append({"file": f, "action": "enabled", "success": False, "warning": "verification failed"})

    elif args.action == "rollback":
        for f in files:
            if rollback_file(f):
                results.append({"file": f, "action": "rollback", "success": True})
            else:
                results.append({"file": f, "action": "rollback", "success": False, "reason": "no backup found"})

    # 输出
    output = {
        "action": args.action,
        "files_processed": len(results),
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }

    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"\nFlagGems Toggle — {args.action}")
        print("=" * 50)
        for r in results:
            action = r.get("action", r.get("status", "?"))
            success = r.get("success", "")
            reason = r.get("reason", "")
            warning = r.get("warning", "")
            extra = ""
            if reason:
                extra = f" ({reason})"
            if warning:
                extra = f" [WARNING: {warning}]"
            if success is True:
                extra = " [OK]"
            elif success is False:
                extra = f" [FAILED]{extra}"

            # status action has different format
            if args.action == "status":
                status = r.get("status", "?")
                active = len(r.get("active_lines", []))
                commented = len(r.get("commented_lines", []))
                backup = "有备份" if r.get("has_backup") else "无备份"
                print(f"  {r['file']}")
                print(f"    状态: {status}  活跃行: {active}  注释行: {commented}  {backup}")
                for al in r.get("active_lines", []):
                    print(f"    L{al['line']}: {al['content']}")
                for cl in r.get("commented_lines", []):
                    print(f"    L{cl['line']}: {cl['content']}")
            else:
                print(f"  {r['file']} → {action}{extra}")

        print(f"\n处理文件数: {len(results)}")


if __name__ == "__main__":
    main()
