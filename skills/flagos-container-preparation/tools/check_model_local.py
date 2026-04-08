#!/usr/bin/env python3
"""
check_model_local.py — 宿主机本地模型权重搜索、校验与自动下载

在容器准备之前，先在宿主机搜索是否已有模型权重。
如果未找到，自动从 ModelScope 下载到指定目录。

用法:
    python3 check_model_local.py --model "Qwen/Qwen3-8B" --output-json
    python3 check_model_local.py --model "https://modelscope.cn/models/Qwen/Qwen2.5-7B"
    python3 check_model_local.py --model "Qwen/Qwen3-8B" --download-dir /mnt/data/models
    python3 check_model_local.py --model "Qwen3-8B" --no-download --output-json

退出码: 0=找到有效权重(本地或下载), 1=未找到, 2=参数错误
"""

import argparse
import json
import os
import re
import subprocess
import sys

DEFAULT_SEARCH_PATHS = ["/data", "/nfs", "/share", "/models", "/home"]
DEFAULT_MAX_DEPTH = 4
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", ".cache", ".trash"}

# 权重文件排除模式
EXCLUDE_BIN = re.compile(r"^(optimizer|training_args|scheduler)", re.IGNORECASE)

# 单个权重文件最小合理大小（1 MB），低于此阈值视为下载中断的残文件
MIN_WEIGHT_FILE_SIZE = 1 * 1024 * 1024


def parse_model_identifier(model_input: str) -> dict:
    """从用户输入解析模型名称和组织信息。"""
    result = {"model_name": "", "org": "", "input_type": "name", "raw": model_input}

    model_input = model_input.strip().rstrip("/")

    # ModelScope URL
    ms_match = re.match(r"https?://modelscope\.cn/models/([^/]+)/([^/]+)", model_input)
    if ms_match:
        result["org"] = ms_match.group(1)
        result["model_name"] = ms_match.group(2)
        result["input_type"] = "modelscope_url"
        return result

    # HuggingFace URL
    hf_match = re.match(r"https?://huggingface\.co/([^/]+)/([^/]+)", model_input)
    if hf_match:
        result["org"] = hf_match.group(1)
        result["model_name"] = hf_match.group(2)
        result["input_type"] = "huggingface_url"
        return result

    # org/name 格式
    if "/" in model_input and not model_input.startswith("http"):
        parts = model_input.rsplit("/", 1)
        result["org"] = parts[0]
        result["model_name"] = parts[1]
        return result

    # 纯模型名
    result["model_name"] = model_input
    return result


def read_config_model_name(dir_path: str) -> str:
    """读取目录下 config.json 的 _name_or_path 字段，提取模型名。"""
    config_path = os.path.join(dir_path, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        # _name_or_path 通常是 "org/model-name" 或绝对路径
        name_or_path = config.get("_name_or_path", "")
        if name_or_path:
            # 取最后一段路径作为模型名
            return name_or_path.rstrip("/").rsplit("/", 1)[-1]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return ""


def has_weight_files(dir_path: str) -> bool:
    """快速检查目录是否包含权重文件（不做完整校验）。"""
    try:
        for entry in os.listdir(dir_path):
            entry_lower = entry.lower()
            if entry_lower.endswith(".safetensors") or entry_lower.endswith(".bin"):
                if not entry_lower.startswith(("training_args", "optimizer", "scheduler")):
                    return True
    except PermissionError:
        pass
    return False


def search_model_dirs(model_name: str, search_paths: list, max_depth: int) -> list:
    """在宿主机路径下搜索目录名匹配的模型目录。

    三种匹配策略（按优先级）：
    1. 精确匹配：目录名 == model_name（大小写不敏感）
    2. 包含匹配：目录名包含 model_name
    3. config 匹配：目录名不匹配，但 config.json 中 _name_or_path 包含模型名
    """
    exact_matches = []
    contain_matches = []
    config_matches = []
    model_lower = model_name.lower()

    for root_path in search_paths:
        if not os.path.isdir(root_path):
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            # 计算当前深度
            depth = dirpath[len(root_path):].count(os.sep)
            if depth >= max_depth:
                dirnames.clear()
                continue

            # 跳过隐藏目录和排除目录
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS
            ]

            for d in dirnames:
                d_lower = d.lower()
                full_path = os.path.join(dirpath, d)
                if d_lower == model_lower:
                    exact_matches.append(full_path)
                elif model_lower in d_lower:
                    contain_matches.append(full_path)

            # 策略 3：当前目录有 config.json + 权重文件，检查 config 内容
            # 仅对目录名未匹配的目录执行（避免重复）
            dir_basename = os.path.basename(dirpath).lower()
            if dir_basename != model_lower and model_lower not in dir_basename:
                if "config.json" in filenames and has_weight_files(dirpath):
                    config_name = read_config_model_name(dirpath)
                    if config_name and model_lower in config_name.lower():
                        config_matches.append(dirpath)

    return exact_matches, contain_matches, config_matches


def check_index_completeness(dir_path: str, weight_files: list, weight_format: str) -> dict:
    """对比 index.json 分片清单，检查权重文件是否齐全。

    返回 {"complete": bool, "missing": [...], "index_file": str or None}
    """
    # 确定 index 文件名
    if weight_format == "safetensors":
        index_name = "model.safetensors.index.json"
    elif weight_format == "pytorch_bin":
        index_name = "pytorch_model.bin.index.json"
    else:
        return {"complete": True, "missing": [], "index_file": None}

    index_path = os.path.join(dir_path, index_name)
    if not os.path.isfile(index_path):
        # 无 index 文件说明是单文件模型或非分片格式，跳过检查
        return {"complete": True, "missing": [], "index_file": None}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"complete": True, "missing": [], "index_file": index_name}

    # weight_map 的值是分片文件名，取唯一集合
    weight_map = index_data.get("weight_map", {})
    expected_files = set(weight_map.values())
    actual_files = set(weight_files)
    missing = sorted(expected_files - actual_files)

    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "index_file": index_name,
    }


def check_truncated_files(dir_path: str, weight_files: list) -> list:
    """检查是否有疑似下载中断的残文件（< MIN_WEIGHT_FILE_SIZE）。"""
    truncated = []
    for fname in weight_files:
        full_path = os.path.join(dir_path, fname)
        try:
            size = os.path.getsize(full_path)
            if size < MIN_WEIGHT_FILE_SIZE:
                truncated.append({"file": fname, "size_bytes": size})
        except OSError:
            truncated.append({"file": fname, "size_bytes": -1})
    return truncated


def validate_model_dir(dir_path: str) -> dict:
    """校验目录是否包含有效模型权重。"""
    result = {
        "valid": False,
        "config_json": False,
        "weight_format": "none",
        "weight_files": [],
        "weight_count": 0,
        "total_size_gb": 0.0,
        "tokenizer": False,
        "completeness": {},
        "truncated_files": [],
    }

    try:
        entries = os.listdir(dir_path)
    except PermissionError:
        return result

    entries_lower = {e.lower(): e for e in entries}

    # config.json
    result["config_json"] = "config.json" in entries_lower

    # tokenizer
    result["tokenizer"] = any(
        k in entries_lower
        for k in ("tokenizer.json", "tokenizer_config.json", "tokenizer.model")
    )

    # 权重文件
    safetensors = []
    bins = []
    total_size = 0

    for entry in entries:
        entry_lower = entry.lower()
        full_path = os.path.join(dir_path, entry)

        if entry_lower.endswith(".safetensors") and not entry_lower.startswith("training_args"):
            safetensors.append(entry)
            try:
                total_size += os.path.getsize(full_path)
            except OSError:
                pass

        elif entry_lower.endswith(".bin") and not EXCLUDE_BIN.match(entry):
            bins.append(entry)
            try:
                total_size += os.path.getsize(full_path)
            except OSError:
                pass

    # 优先 safetensors
    if safetensors:
        result["weight_format"] = "safetensors"
        result["weight_files"] = sorted(safetensors)
    elif bins:
        result["weight_format"] = "pytorch_bin"
        result["weight_files"] = sorted(bins)

    result["weight_count"] = len(result["weight_files"])
    result["total_size_gb"] = round(total_size / (1024 ** 3), 2)

    # 完整性校验：对比 index.json 分片清单
    result["completeness"] = check_index_completeness(
        dir_path, result["weight_files"], result["weight_format"]
    )

    # 残文件检测：单文件 < 1MB
    result["truncated_files"] = check_truncated_files(dir_path, result["weight_files"])

    # valid = config.json + 至少一个权重文件 + 分片齐全 + 无残文件
    result["valid"] = (
        result["config_json"]
        and result["weight_count"] > 0
        and result["completeness"]["complete"]
        and len(result["truncated_files"]) == 0
    )

    return result


DEFAULT_DOWNLOAD_DIR = "/mnt/data/models"


def download_from_modelscope(model_id: str, download_path: str) -> dict:
    """从 ModelScope 下载模型权重。

    Args:
        model_id: ModelScope 模型 ID，格式 "org/model_name"
        download_path: 本地下载目标路径

    Returns:
        {"success": bool, "path": str, "error": str}
    """
    result = {"success": False, "path": download_path, "error": ""}

    os.makedirs(download_path, exist_ok=True)

    cmd = ["modelscope", "download", "--model", model_id, "--local_dir", download_path]
    print(f"\n>>> 本地未找到权重，开始从 ModelScope 下载: {model_id}")
    print(f"    目标路径: {download_path}")
    print(f"    命令: {' '.join(cmd)}")

    try:
        proc = subprocess.run(cmd, timeout=7200, capture_output=False)
        if proc.returncode == 0:
            result["success"] = True
            print(f"\n✓ 下载完成: {download_path}")
        else:
            result["error"] = f"modelscope download 退出码 {proc.returncode}"
            print(f"\n✗ 下载失败: 退出码 {proc.returncode}", file=sys.stderr)
    except FileNotFoundError:
        result["error"] = "modelscope CLI 未安装，请先执行: pip install modelscope"
        print(f"\n✗ {result['error']}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        result["error"] = "下载超时（2小时）"
        print(f"\n✗ {result['error']}", file=sys.stderr)
    except Exception as e:
        result["error"] = str(e)
        print(f"\n✗ 下载异常: {e}", file=sys.stderr)

    return result


def main():
    parser = argparse.ArgumentParser(description="宿主机本地模型权重搜索与校验")
    parser.add_argument("--model", required=True, help="模型名 / ModelScope URL / HuggingFace URL")
    parser.add_argument("--search-paths", default=None, help="搜索根目录，逗号分隔")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="搜索目录深度")
    parser.add_argument("--output-json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--download-dir", default=DEFAULT_DOWNLOAD_DIR, help="自动下载目标目录")
    parser.add_argument("--no-download", action="store_true", help="禁用自动下载，仅搜索本地")
    args = parser.parse_args()

    # 解析模型标识
    parsed = parse_model_identifier(args.model)
    if not parsed["model_name"]:
        print("Error: 无法解析模型名称", file=sys.stderr)
        sys.exit(2)

    # 搜索路径
    if args.search_paths:
        search_paths = [p.strip() for p in args.search_paths.split(",") if p.strip()]
    else:
        search_paths = DEFAULT_SEARCH_PATHS

    # 搜索
    exact_matches, contain_matches, config_matches = search_model_dirs(
        parsed["model_name"], search_paths, args.max_depth
    )

    # 校验所有候选
    candidates = []
    for path in exact_matches:
        info = validate_model_dir(path)
        candidates.append({
            "path": path,
            "match_type": "exact",
            **info,
        })
    for path in contain_matches:
        info = validate_model_dir(path)
        candidates.append({
            "path": path,
            "match_type": "contains",
            **info,
        })
    for path in config_matches:
        info = validate_model_dir(path)
        candidates.append({
            "path": path,
            "match_type": "config",
            **info,
        })

    # 选择 best_match: valid=true 中，exact > contains > config，权重大小优先
    MATCH_PRIORITY = {"exact": 0, "contains": 1, "config": 2}
    valid_candidates = [c for c in candidates if c["valid"]]
    best_match = None
    if valid_candidates:
        valid_candidates.sort(
            key=lambda c: (MATCH_PRIORITY.get(c["match_type"], 9), -c["total_size_gb"])
        )
        best_match = valid_candidates[0]["path"]

    output = {
        "model_input": args.model,
        "parsed": parsed,
        "found": best_match is not None,
        "candidates": candidates,
        "best_match": best_match,
        "download": None,
    }

    # 未找到本地权重时，自动从 ModelScope 下载
    if best_match is None and not args.no_download:
        model_id = ""
        if parsed["org"]:
            model_id = f"{parsed['org']}/{parsed['model_name']}"
        elif "/" in args.model and not args.model.startswith("http"):
            model_id = args.model

        if model_id:
            download_path = os.path.join(args.download_dir, parsed["model_name"])
            dl_result = download_from_modelscope(model_id, download_path)
            output["download"] = dl_result

            if dl_result["success"]:
                # 校验下载结果
                dl_validation = validate_model_dir(download_path)
                if dl_validation["valid"]:
                    output["found"] = True
                    output["best_match"] = download_path
                    output["candidates"].append({
                        "path": download_path,
                        "match_type": "downloaded",
                        **dl_validation,
                    })
                else:
                    print(f"  警告: 下载完成但校验未通过", file=sys.stderr)
                    output["download"]["validation"] = dl_validation
        else:
            print(f"\n✗ 无法自动下载: 缺少组织名，请使用 org/model 格式（如 Qwen/Qwen3-8B）", file=sys.stderr)
            output["download"] = {"success": False, "error": "缺少组织名，无法构建 ModelScope model ID"}

    if args.output_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # 人类可读输出
        print(f"Model: {parsed['model_name']} (org: {parsed['org'] or 'N/A'})")
        print(f"Input type: {parsed['input_type']}")
        print(f"Search paths: {', '.join(search_paths)}")
        print(f"Candidates found: {len(candidates)}")
        if best_match:
            print(f"\n✓ Best match: {best_match}")
            # 从 candidates 中找到对应的 best_match 记录
            best_info = next((c for c in candidates if c["path"] == best_match), None)
            if best_info:
                print(f"  Format: {best_info['weight_format']}, Files: {best_info['weight_count']}, Size: {best_info['total_size_gb']} GB")
                print(f"  Tokenizer: {'yes' if best_info['tokenizer'] else 'no'}")
                if best_info.get("match_type") == "downloaded":
                    print(f"  Source: ModelScope (自动下载)")
        else:
            print("\n✗ No valid model weights found.")
            if candidates:
                print("  Partial matches:")
                for c in candidates[:5]:
                    issues = []
                    if not c["config_json"]:
                        issues.append("missing config.json")
                    if c["weight_count"] == 0:
                        issues.append("no weight files")
                    if not c["completeness"].get("complete", True):
                        missing = c["completeness"]["missing"]
                        issues.append(f"missing {len(missing)} shard(s): {', '.join(missing[:3])}")
                    if c["truncated_files"]:
                        names = [t["file"] for t in c["truncated_files"]]
                        issues.append(f"truncated: {', '.join(names[:3])}")
                    print(f"    {c['path']} — {'; '.join(issues) or 'unknown'}")

    sys.exit(0 if best_match else 1)


if __name__ == "__main__":
    main()
