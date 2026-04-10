#!/usr/bin/env python3
"""
install_component.py — FlagOS 生态组件统一安装/升级/卸载

支持组件：flaggems, flagscale, flagcx, flagtree
FlagTree 操作委托给 install_flagtree.sh。

FlagGems 安装策略（三级降级）：
  1. pip install flag-gems（优先）
  2. git clone + pip install .（pip 失败时）
  3. 输出宿主机操作指令（容器无网络时）

Usage:
    # FlagGems 安装（三级降级）
    python install_component.py --component flaggems --action install --json
    python install_component.py --component flaggems --action install --version 4.2.1rc0 --json

    # FlagGems 升级（同样三级降级）
    python install_component.py --component flaggems --action upgrade --branch main --json

    # FlagTree 安装
    python install_component.py --component flagtree --action install --vendor nvidia --json

    # FlagTree 卸载
    python install_component.py --component flagtree --action uninstall --json

    # FlagTree 验证
    python install_component.py --component flagtree --action verify --json

    # 带代理
    python install_component.py --component flaggems --action install --proxy http://proxy:port --json
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

# error_writer 集成
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from error_writer import write_last_error, write_checkpoint
except ImportError:
    def write_last_error(*a, **kw): pass
    def write_checkpoint(*a, **kw): pass


# 默认仓库地址
DEFAULT_REPOS = {
    "flaggems": "https://github.com/FlagOpen/FlagGems.git",
    "flagscale": "https://github.com/FlagOpen/FlagScale.git",
    "flagcx": "https://github.com/FlagOpen/FlagCX.git",
}

# pip 包名映射
PACKAGE_NAMES = {
    "flaggems": "flag-gems",
    "flagscale": "flag-scale",
    "flagcx": "flagcx",
}

# 可能需要的构建依赖
BUILD_DEPS = ["setuptools>=64.0", "scikit-build-core", "wheel"]

# install_flagtree.sh 路径（容器内）
FLAGTREE_SCRIPT = str(Path(__file__).resolve().parent / "install_flagtree.sh")
if not os.path.isfile(FLAGTREE_SCRIPT):
    # 容器内扁平部署路径
    FLAGTREE_SCRIPT = "/flagos-workspace/scripts/install_flagtree.sh"


def run_cmd(cmd, timeout=300, env=None):
    """运行命令，返回 (returncode, stdout, stderr)"""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, env=merged_env
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as e:
        return -1, "", str(e)


def check_network(proxy=None):
    """检测网络连通性"""
    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    # 尝试多个目标
    for url in ["https://pypi.org", "https://github.com"]:
        code, out, err = run_cmd(
            f"curl --connect-timeout 5 -s -o /dev/null -w '%{{http_code}}' {url}",
            timeout=10, env=env
        )
        if code == 0 and out.strip("'\"") in ["200", "301", "302"]:
            return True
    return False


def get_current_version(component):
    """获取当前安装版本"""
    pkg_name = PACKAGE_NAMES.get(component, component)
    code, out, err = run_cmd(f"pip show {pkg_name} 2>/dev/null")
    if code == 0:
        for line in out.split("\n"):
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return None


def check_build_deps(proxy=None):
    """检查并安装构建依赖"""
    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    missing = []
    for dep in BUILD_DEPS:
        pkg = dep.split(">=")[0].split("==")[0].replace("-", "_")
        code, _, _ = run_cmd(f"python3 -c 'import {pkg}' 2>/dev/null")
        if code != 0:
            missing.append(dep)

    if missing:
        deps_str = " ".join(f'"{d}"' for d in missing)
        code, out, err = run_cmd(f"pip install {deps_str}", env=env)
        if code != 0:
            return False, missing, err
    return True, [], ""


# =============================================================================
# FlagGems/FlagScale/FlagCX 安装（三级降级）
# =============================================================================

def pip_install(component, version=None, proxy=None):
    """第一级：pip install"""
    pkg_name = PACKAGE_NAMES.get(component, component)
    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    if version:
        cmd = f"pip install {pkg_name}=={version}"
    else:
        cmd = f"pip install {pkg_name}"

    code, out, err = run_cmd(cmd, timeout=300, env=env)
    if code == 0:
        return True, f"pip install 成功: {cmd}"
    return False, f"pip install 失败: {err}"


def source_install(component, repo_url, branch="main", proxy=None, work_dir="/tmp"):
    """第二级：git clone + pip install ."""
    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    clone_path = os.path.join(work_dir, repo_name)

    # 清理旧目录
    if os.path.exists(clone_path):
        run_cmd(f"rm -rf {clone_path}")

    # 检查构建依赖
    check_build_deps(proxy)

    # 克隆
    code, out, err = run_cmd(
        f"git clone --depth 1 --branch {branch} {repo_url} {clone_path}",
        timeout=120, env=env
    )
    if code != 0:
        # branch 可能不存在，尝试不指定 branch
        if "not found" in err or "Could not find" in err:
            code, out, err = run_cmd(
                f"git clone --depth 1 {repo_url} {clone_path}",
                timeout=120, env=env
            )
        if code != 0:
            return False, f"git clone 失败: {err}"

    # 安装
    code, out, err = run_cmd(
        f"cd {clone_path} && pip install .",
        timeout=600, env=env
    )
    if code != 0:
        # 尝试 --no-build-isolation
        code, out, err = run_cmd(
            f"cd {clone_path} && pip install --no-build-isolation .",
            timeout=600, env=env
        )
        if code != 0:
            return False, f"源码安装失败: {err}"

    return True, f"源码安装成功: {clone_path}"


def generate_host_instructions(component, repo_url, branch, container_name="$CONTAINER"):
    """第三级：生成宿主机执行指令"""
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    return {
        "type": "host_fallback",
        "message": "容器内无网络，请在宿主机执行以下命令",
        "commands": [
            f"cd /tmp && git clone --depth 1 --branch {branch} {repo_url}",
            f"docker cp /tmp/{repo_name} {container_name}:/tmp/{repo_name}",
            f"docker exec {container_name} bash -c 'cd /tmp/{repo_name} && pip install .'",
        ],
        "component": component,
        "repo_url": repo_url,
        "branch": branch,
    }


def install_or_upgrade_component(component, action, version=None, branch="main",
                                  repo_url=None, proxy=None, container_name="$CONTAINER"):
    """FlagGems/FlagScale/FlagCX 安装/升级（三级降级）"""
    repo_url = repo_url or DEFAULT_REPOS.get(component, "")
    old_version = get_current_version(component)

    result = {
        "component": component,
        "action": action,
        "previous_version": old_version,
        "current_version": None,
        "install_method": None,
        "success": False,
        "message": "",
        "timestamp": datetime.now().isoformat(),
    }

    # 第一级：pip install
    success, msg = pip_install(component, version=version, proxy=proxy)
    if success:
        result["success"] = True
        result["install_method"] = "pip"
        result["message"] = msg
        result["current_version"] = get_current_version(component)
        return result

    pip_error = msg

    # 第二级：源码安装
    has_network = check_network(proxy)
    if has_network:
        success, msg = source_install(component, repo_url, branch=branch, proxy=proxy)
        if success:
            result["success"] = True
            result["install_method"] = "source"
            result["message"] = msg
            result["current_version"] = get_current_version(component)
            return result
        source_error = msg
    else:
        source_error = "容器无网络，跳过源码安装"

    # 第三级：宿主机降级
    instructions = generate_host_instructions(component, repo_url, branch, container_name)
    result["success"] = False
    result["install_method"] = "host_fallback"
    result["message"] = f"pip 失败: {pip_error}; 源码失败: {source_error}"
    result["fallback"] = instructions

    return result


# =============================================================================
# FlagTree 操作（委托给 install_flagtree.sh）
# =============================================================================

def handle_flagtree(action, vendor=None, version=None, source=False, branch=None, json_output=False):
    """FlagTree 操作委托给 install_flagtree.sh"""
    if not os.path.isfile(FLAGTREE_SCRIPT):
        return {
            "component": "flagtree",
            "action": action,
            "success": False,
            "message": f"install_flagtree.sh 不存在: {FLAGTREE_SCRIPT}",
            "timestamp": datetime.now().isoformat(),
        }

    # 构建命令
    cmd_parts = [f"bash {FLAGTREE_SCRIPT}", action]
    if vendor:
        cmd_parts.extend(["--vendor", vendor])
    if version:
        cmd_parts.extend(["--version", version])
    if source:
        cmd_parts.append("--source")
    if branch:
        cmd_parts.extend(["--branch", branch])

    cmd = " ".join(cmd_parts)
    code, out, err = run_cmd(cmd, timeout=600)

    result = {
        "component": "flagtree",
        "action": action,
        "success": code == 0,
        "output": out,
        "timestamp": datetime.now().isoformat(),
    }

    if code != 0:
        result["error"] = err or out

    # verify 输出包含 JSON，尝试解析
    if action == "verify" and out:
        try:
            # 找到 JSON 部分
            start = out.index("{")
            end = out.rindex("}") + 1
            verify_data = json.loads(out[start:end])
            result["verify"] = verify_data
        except (ValueError, json.JSONDecodeError):
            pass

    return result


# =============================================================================
# API 兼容性检查（FlagGems 安装/升级后）
# =============================================================================

def check_flaggems_api():
    """检查 FlagGems API 兼容性"""
    code, out, err = run_cmd("""python3 -c "
import json, flag_gems
result = {
    'version': getattr(flag_gems, '__version__', 'unknown'),
    'has_enable': hasattr(flag_gems, 'enable'),
    'has_only_enable': hasattr(flag_gems, 'only_enable'),
    'has_use_gems': hasattr(flag_gems, 'use_gems'),
}
if hasattr(flag_gems, 'enable'):
    import inspect
    sig = inspect.signature(flag_gems.enable)
    result['enable_params'] = list(sig.parameters.keys())
print(json.dumps(result, indent=2))
" """)
    if code == 0:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"error": err or "无法检查 API"}


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="FlagOS 组件统一安装工具")
    parser.add_argument("--component", required=True,
                        choices=["flaggems", "flagscale", "flagcx", "flagtree"],
                        help="要操作的组件")
    parser.add_argument("--action", required=True,
                        choices=["install", "upgrade", "uninstall", "verify"],
                        help="操作类型")
    parser.add_argument("--version", help="指定版本（如 4.2.1rc0）")
    parser.add_argument("--branch", default="main", help="Git 分支（默认 main）")
    parser.add_argument("--repo", help="仓库地址（不指定则使用默认）")
    parser.add_argument("--proxy", help="代理地址")
    parser.add_argument("--vendor", help="FlagTree 后端（如 nvidia, ascend）")
    parser.add_argument("--source", action="store_true", help="FlagTree 源码编译")
    parser.add_argument("--container-name", default="$CONTAINER", help="容器名（宿主机降级用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    # FlagTree 委托给 install_flagtree.sh
    if args.component == "flagtree":
        result = handle_flagtree(
            action=args.action,
            vendor=args.vendor,
            version=args.version,
            source=args.source,
            branch=args.branch,
            json_output=args.json,
        )
    elif args.action == "verify":
        # 非 FlagTree 的 verify
        version = get_current_version(args.component)
        result = {
            "component": args.component,
            "action": "verify",
            "installed": version is not None,
            "version": version,
            "timestamp": datetime.now().isoformat(),
        }
        if args.component == "flaggems" and version:
            result["api"] = check_flaggems_api()
    elif args.action == "uninstall":
        pkg_name = PACKAGE_NAMES.get(args.component, args.component)
        code, out, err = run_cmd(f"pip uninstall -y {pkg_name}")
        result = {
            "component": args.component,
            "action": "uninstall",
            "success": code == 0,
            "message": out if code == 0 else err,
            "timestamp": datetime.now().isoformat(),
        }
    else:
        # install / upgrade
        result = install_or_upgrade_component(
            component=args.component,
            action=args.action,
            version=args.version,
            branch=args.branch,
            repo_url=args.repo,
            proxy=args.proxy,
            container_name=args.container_name,
        )
        # FlagGems 安装/升级后检查 API 兼容性
        if result.get("success") and args.component == "flaggems":
            result["api"] = check_flaggems_api()

    # 输出
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        component = result.get("component", args.component)
        action = result.get("action", args.action)
        success = result.get("success", False)

        print(f"\nFlagOS 组件操作 — {component} {action}")
        print("=" * 50)

        if action == "verify":
            if args.component == "flagtree":
                v = result.get("verify", {})
                print(f"  FlagTree: {'v' + v.get('flagtree_version', '?') if v.get('flagtree_installed') else '未安装'}")
                print(f"  Triton:   {'v' + v.get('triton_version', '?') if v.get('triton_installed') else '未安装'}")
            else:
                print(f"  版本: {result.get('version', '未安装')}")
                api = result.get("api", {})
                if api and not api.get("error"):
                    print(f"  API: enable={api.get('has_enable')}, only_enable={api.get('has_only_enable')}")
        else:
            status = "成功" if success else "失败"
            print(f"  状态: {status}")
            print(f"  方式: {result.get('install_method', '-')}")
            if result.get("previous_version"):
                print(f"  版本: {result['previous_version']} → {result.get('current_version', '?')}")
            elif result.get("current_version"):
                print(f"  版本: {result['current_version']}")
            if result.get("message"):
                print(f"  信息: {result['message'][:200]}")
            if result.get("fallback"):
                print(f"\n  宿主机降级指令:")
                for cmd in result["fallback"].get("commands", []):
                    print(f"    {cmd}")

    sys.exit(0 if result.get("success", False) else 1)


if __name__ == "__main__":
    try:
        write_checkpoint("01_container_preparation", "组件安装", "running_install_component",
                         action_detail=" ".join(sys.argv))
        main()
    except Exception as e:
        write_last_error(
            tool="install_component.py",
            error_type=type(e).__name__,
            error_message=str(e),
            traceback_str=traceback.format_exc(),
        )
        print(f"[FATAL] install_component.py 异常退出: {e}")
        traceback.print_exc()
        sys.exit(1)
