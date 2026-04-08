#!/usr/bin/env python3
"""
vLLM 性能基准测试工具

新增:
- --strategy: 四档测试强度 (quick/fast/comprehensive/fixed)
- --final-burst: 显式 opt-in 最终无限制并发测试 (num_prompts=1000)
- --output-name: 指定输出文件名
- --output-dir: 指定输出目录
- --mode: 标记测试模式 (native/flagos_initial/flagos_optimized)
- per-test-case 预热: 消除冷启动开销
- 所有档统一 num_prompts=concurrency

向后兼容别名:
- --quick → --strategy quick
- --concurrency-search → --strategy fast

Usage:
    python benchmark_runner.py --config config.yaml --strategy fast
    python benchmark_runner.py --config config.yaml --strategy quick
    python benchmark_runner.py --config config.yaml --strategy comprehensive
    python benchmark_runner.py --config config.yaml --strategy fixed
    python benchmark_runner.py --config config.yaml --strategy fast --final-burst
    python benchmark_runner.py --config config.yaml --quick          # 向后兼容
    python benchmark_runner.py --config config.yaml --concurrency-search  # 向后兼容
    python benchmark_runner.py --output-name native_performance
    python benchmark_runner.py --test-case 1k_input_1k_output
    python benchmark_runner.py --dry-run
"""

import sys

# IO 缓冲修复：确保容器内实时输出
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
else:
    import functools
    print = functools.partial(print, flush=True)

import argparse
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# =============================================================================
# 配置加载
# =============================================================================

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "perf_config.yaml"

# 超时已禁用（设为 None），benchmark 将等待子进程自然结束
DEFAULT_TIMEOUT = None


def load_yaml(path: Path) -> Dict[str, Any]:
    """加载 YAML 文件"""
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """加载配置文件，缺失字段自动从 context.yaml 回填"""
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    config = load_yaml(cfg_path)

    if "server" not in config:
        config["server"] = {"host": "", "port": 8000}
    if "model" not in config:
        config["model"] = {"name": "", "tokenizer_path": ""}

    # Fallback: 从 context.yaml 补充缺失的 server.host / model.tokenizer_path
    if not config["server"].get("host") or not config["model"].get("tokenizer_path"):
        ctx_path = Path("/flagos-workspace/shared/context.yaml")
        if ctx_path.exists():
            try:
                ctx = load_yaml(ctx_path)
                svc = ctx.get("service", {})
                if not config["server"].get("host"):
                    config["server"]["host"] = svc.get("host", "127.0.0.1")
                if not config["server"].get("port") or config["server"]["port"] == 8000:
                    port = svc.get("port")
                    if port:
                        config["server"]["port"] = port
                if not config["model"].get("tokenizer_path"):
                    config["model"]["tokenizer_path"] = ctx.get("model", {}).get("path", "")
                if not config["model"].get("name"):
                    config["model"]["name"] = ctx.get("model", {}).get("name", "")
                print(f"[INFO] 从 context.yaml 补充了缺失配置")
            except Exception as e:
                print(f"[WARN] 读取 context.yaml 失败: {e}")

    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """验证配置完整性"""
    errors = []

    if not config.get("server", {}).get("host"):
        errors.append("server.host 未配置 (检查 shared/context.yaml)")
    if not config.get("model", {}).get("tokenizer_path"):
        errors.append("model.tokenizer_path 未配置 (检查 shared/context.yaml)")
    if not config.get("test_matrix"):
        errors.append("test_matrix 为空")
    if not config.get("concurrency", {}).get("levels"):
        errors.append("concurrency.levels 未配置")

    for err in errors:
        print(f"ERROR: {err}")

    return len(errors) == 0


# =============================================================================
# 输出解析
# =============================================================================

METRIC_PATTERNS = {
    'Successful requests': r'Successful requests:\s+(\d+)',
    'Failed requests': r'Failed requests:\s+(\d+)',
    'Benchmark duration (s)': r'Benchmark duration \(s\):\s+([\d.]+)',
    'Total input tokens': r'Total input tokens:\s+(\d+)',
    'Total generated tokens': r'Total generated tokens:\s+(\d+)',
    'Request throughput (req/s)': r'Request throughput \(req/s\):\s+([\d.]+)',
    'Output token throughput (tok/s)': r'Output token throughput \(tok/s\):\s+([\d.]+)',
    'Total token throughput (tok/s)': r'Total [Tt]oken throughput \(tok/s\):\s+([\d.]+)',
    'Peak output token throughput (tok/s)': r'Peak output token throughput \(tok/s\):\s+([\d.]+)',
    'Peak concurrent requests': r'Peak concurrent requests:\s+(\d+)',
    'Mean TTFT (ms)': r'Mean TTFT \(ms\):\s+([\d.]+)',
    'Median TTFT (ms)': r'Median TTFT \(ms\):\s+([\d.]+)',
    'P99 TTFT (ms)': r'P99 TTFT \(ms\):\s+([\d.]+)',
    'Mean TPOT (ms)': r'Mean TPOT \(ms\):\s+([\d.]+)',
    'Median TPOT (ms)': r'Median TPOT \(ms\):\s+([\d.]+)',
    'P99 TPOT (ms)': r'P99 TPOT \(ms\):\s+([\d.]+)',
    'Mean ITL (ms)': r'Mean ITL \(ms\):\s+([\d.]+)',
    'Median ITL (ms)': r'Median ITL \(ms\):\s+([\d.]+)',
    'P99 ITL (ms)': r'P99 ITL \(ms\):\s+([\d.]+)',
}


def parse_output(output: str) -> Dict[str, Any]:
    """从 vllm bench 输出中提取指标"""
    metrics = {}
    for key, pattern in METRIC_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            val = match.group(1)
            metrics[key] = float(val) if '.' in val else int(val)
        else:
            metrics[key] = None
    return metrics


# =============================================================================
# 基准测试执行
# =============================================================================

def build_command(config: Dict[str, Any], test_case: Dict[str, Any]) -> List[str]:
    """构建 vllm bench 命令"""
    server = config["server"]
    model = config["model"]
    bench = config.get("benchmark", {})

    cmd = [
        "vllm", "bench", "serve",
        "--host", server["host"],
        "--port", str(server["port"]),
        "--model", model["name"],
        "--tokenizer", model["tokenizer_path"],
        "--dataset-name", bench.get("dataset_name", "random"),
        "--random-input-len", str(test_case["input_len"]),
        "--random-output-len", str(test_case["output_len"]),
        "--endpoint", bench.get("endpoint", "/v1/completions"),
    ]

    if bench.get("ignore_eos", True):
        cmd.append("--ignore-eos")
    if bench.get("trust_remote_code", True):
        cmd.append("--trust-remote-code")

    return cmd


def run_benchmark(cmd: List[str], num_prompts: int, max_concurrency: Optional[int] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """执行单次基准测试"""
    full_cmd = cmd + ["--num-prompts", str(num_prompts)]
    if max_concurrency:
        full_cmd += ["--max-concurrency", str(max_concurrency)]

    if dry_run:
        print(f"  [DRY RUN] {' '.join(full_cmd)}")
        return {"dry_run": True}

    conc_str = f"concurrency={max_concurrency}" if max_concurrency else "unlimited"
    print(f"  Running: num_prompts={num_prompts}, {conc_str}")

    try:
        proc = subprocess.Popen(
            full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout_lines = []
        import threading, time

        # 后台线程读取 stderr 防止死锁
        stderr_lines = []
        def read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
        t = threading.Thread(target=read_stderr, daemon=True)
        t.start()

        # 实时逐行读取 stdout
        for line in proc.stdout:
            stdout_lines.append(line)
            stripped = line.strip()
            if stripped:
                print(f"    | {stripped}")

        proc.wait()
        t.join(timeout=5)
        full_stdout = "".join(stdout_lines)
        full_stderr = "".join(stderr_lines)

        if proc.returncode != 0:
            print(f"    FAILED (rc={proc.returncode}): {full_stderr[:200]}")
            return {"error": full_stderr}

        metrics = parse_output(full_stdout)
        throughput = metrics.get('Output token throughput (tok/s)', 'N/A')
        total_tp = metrics.get('Total token throughput (tok/s)', 'N/A')
        failed = metrics.get('Failed requests', 0)
        print(f"    OK - output={throughput} tok/s, total={total_tp} tok/s, failed={failed}")
        return metrics

    except Exception as e:
        print(f"    ERROR: {e}")
        return {"error": str(e)}




# Quick 模式最大并发
QUICK_MAX_CONCURRENCY = 256

# 预热请求数（消除 CUDA kernel 编译、KV cache 分配等冷启动开销）
WARMUP_NUM_PROMPTS = 2
WARMUP_CONCURRENCY = 2

# Quick 模式硬编码用例名
QUICK_TEST_CASE_NAME = "4k_input_1k_output"


def run_test_case(config: Dict[str, Any], test_case: Dict[str, Any],
                  dry_run: bool = False, strategy: str = "fast",
                  final_burst: bool = False) -> Dict[str, Any]:
    """运行单个测试用例的所有并发级别，返回结果中包含 _elapsed_seconds"""
    tc_start = time.time()
    base_cmd = build_command(config, test_case)

    levels = config["concurrency"]["levels"]
    final_prompts = config["concurrency"]["final_num_prompts"]
    use_early_stop = test_case.get("early_stop", True)

    if strategy == "quick":
        results = run_quick_test(base_cmd, levels, dry_run)
    elif strategy == "fixed":
        # fixed: 只跑 fixed_concurrency 指定的单个并发级别
        fixed_conc = test_case.get("fixed_concurrency")
        if fixed_conc is None:
            raise ValueError(f"Test case {test_case['name']} missing fixed_concurrency for --strategy fixed")
        results = run_single_concurrency(base_cmd, fixed_conc, dry_run)
    elif strategy == "comprehensive":
        # comprehensive: 所有并发全跑，强制不早停
        results = run_concurrency_search(base_cmd, levels, dry_run,
                                         early_stop=False)
    else:
        # fast (default): 按 early_stop 配置决定是否早停
        results = run_concurrency_search(base_cmd, levels, dry_run,
                                         early_stop=use_early_stop)

    # --final-burst opt-in: 任何 strategy 完成后追加 final burst
    if final_burst:
        results = run_final_burst(base_cmd, final_prompts, results, dry_run)

    results["_elapsed_seconds"] = round(time.time() - tc_start, 1)
    return results


def run_concurrency_search(base_cmd: List[str], levels: List[int],
                           dry_run: bool = False,
                           early_stop: bool = True) -> Dict[str, Any]:
    """
    自动搜索最优并发级别。

    增强停止条件（early_stop=True 时生效）：
    1. 连续 2 级增长 < 3% → 已饱和，停止搜索
    2. 吞吐下降 > 5% → 过拐点，停止搜索
    3. 请求失败数 > 0 → 过载，停止搜索

    early_stop=False 时所有并发级别全跑，不检查停止条件。
    """
    GROWTH_THRESHOLD = 0.03      # 3% 增长阈值
    DECLINE_THRESHOLD = 0.05     # 5% 下降阈值
    CONSECUTIVE_LOW = 2          # 连续低增长次数

    results = {}
    prev_throughput = 0.0
    best_throughput = 0.0
    best_concurrency = levels[0]
    stopped = False
    consecutive_low_growth = 0

    print(f"  [CONCURRENCY SEARCH] levels={levels}, early_stop={early_stop}, num_prompts=concurrency")
    if early_stop:
        print(f"    stop conditions: growth<{GROWTH_THRESHOLD*100}% x{CONSECUTIVE_LOW} | decline>{DECLINE_THRESHOLD*100}% | failures")

    for conc in levels:
        # num_prompts = concurrency，所有档统一
        metrics = run_benchmark(base_cmd, conc, conc, dry_run)
        results[str(conc)] = metrics

        if dry_run or "error" in metrics:
            if "error" in metrics and not dry_run:
                print(f"    Error at concurrency={conc}, stopping search: {metrics['error'][:100]}")
                stopped = True
                break
            continue

        current_throughput = metrics.get('Output token throughput (tok/s)', 0) or 0

        if current_throughput > best_throughput:
            best_throughput = current_throughput
            best_concurrency = conc

        if early_stop:
            # 检查请求失败
            failed_requests = metrics.get('Failed requests', 0)
            if failed_requests and failed_requests > 0:
                print(f"    {failed_requests} failed requests at concurrency={conc}, stopping search")
                stopped = True
                break

            # 检查停止条件
            if prev_throughput > 0 and current_throughput > 0:
                growth = (current_throughput - prev_throughput) / prev_throughput

                # 条件 2：吞吐下降超过 5%
                if current_throughput < prev_throughput * (1 - DECLINE_THRESHOLD):
                    print(f"    Growth: {growth*100:.1f}% — throughput declined >{DECLINE_THRESHOLD*100}%")
                    print(f"    Best concurrency: {best_concurrency}, stopping search")
                    stopped = True
                    break

                # 条件 1：连续低增长
                if growth < GROWTH_THRESHOLD:
                    consecutive_low_growth += 1
                    print(f"    Growth: {growth*100:.1f}% — low growth {consecutive_low_growth}/{CONSECUTIVE_LOW}")
                    if consecutive_low_growth >= CONSECUTIVE_LOW:
                        print(f"    Best concurrency: {best_concurrency}, stopping search")
                        stopped = True
                        break
                else:
                    consecutive_low_growth = 0
                    print(f"    Growth: {growth*100:.1f}%")

        prev_throughput = current_throughput

    # 记录搜索元信息
    results["_search_meta"] = {
        "best_concurrency": best_concurrency,
        "best_throughput": best_throughput,
        "tested_levels": [l for l in levels if str(l) in results],
        "all_levels_tested": not stopped,
    }

    return results


def run_quick_test(base_cmd: List[str], levels: List[int],
                   dry_run: bool = False) -> Dict[str, Any]:
    """
    快速模式：num_prompts = concurrency，并发最高到 256，不早停。

    用于流程验证和快速三版对比，不追求精确结果。
    正式测试前先发预热请求，消除冷启动开销（结果丢弃）。
    """
    # 并发上限 256
    levels = [l for l in levels if l <= QUICK_MAX_CONCURRENCY]

    # 预热：发少量请求让 GPU/vLLM 完成初始化，结果丢弃
    if not dry_run:
        print(f"  [WARMUP] Sending {WARMUP_NUM_PROMPTS} warmup requests (concurrency={WARMUP_CONCURRENCY}) ...")
        run_benchmark(base_cmd, WARMUP_NUM_PROMPTS, WARMUP_CONCURRENCY, dry_run=False)
        print(f"  [WARMUP] Done, starting benchmark")

    results = {}
    best_throughput = 0.0
    best_concurrency = levels[0]

    print(f"  [QUICK MODE] levels={levels}, num_prompts=concurrency, max_conc={QUICK_MAX_CONCURRENCY}")

    for conc in levels:
        metrics = run_benchmark(base_cmd, conc, conc, dry_run)
        results[str(conc)] = metrics

        if dry_run or "error" in metrics:
            continue

        current_throughput = metrics.get('Output token throughput (tok/s)', 0) or 0
        if current_throughput > best_throughput:
            best_throughput = current_throughput
            best_concurrency = conc

    results["_search_meta"] = {
        "best_concurrency": best_concurrency,
        "best_throughput": best_throughput,
        "tested_levels": levels,
        "all_levels_tested": True,
        "quick_mode": True,
    }

    return results


def run_single_concurrency(base_cmd: List[str], concurrency: int,
                           dry_run: bool = False) -> Dict[str, Any]:
    """
    Fixed 模式：只跑单个固定并发级别。

    用于 --strategy fixed，跳过搜索，直接测试指定并发。
    """
    print(f"  [FIXED MODE] concurrency={concurrency}")
    metrics = run_benchmark(base_cmd, concurrency, concurrency, dry_run)
    results = {str(concurrency): metrics}

    throughput = metrics.get('Output token throughput (tok/s)', 0) or 0
    results["_search_meta"] = {
        "best_concurrency": concurrency,
        "best_throughput": throughput,
        "tested_levels": [concurrency],
        "all_levels_tested": True,
        "fixed_mode": True,
    }

    return results


def run_final_burst(base_cmd: List[str], final_num_prompts: int,
                    results: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """
    Final burst: 无限制并发的大规模测试。

    仅在用户显式传入 --final-burst 时调用。
    num_prompts=final_num_prompts (默认 1000), max_concurrency=None。
    """
    print(f"  [FINAL BURST] num_prompts={final_num_prompts}, unlimited concurrency")
    results["max"] = run_benchmark(base_cmd, final_num_prompts, None, dry_run)
    return results


# =============================================================================
# 结果保存
# =============================================================================

def save_results(results: Dict[str, Any], config: Dict[str, Any],
                 output_name: Optional[str] = None,
                 output_dir: Optional[str] = None,
                 mode: Optional[str] = None,
                 timing: Optional[Dict[str, Any]] = None) -> str:
    """保存测试结果到 JSON 文件（扁平格式，不含 metadata 包装和 _search_meta）"""
    output_cfg = config.get("output", {})

    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(output_cfg.get("dir", "./output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_name:
        filepath = out_dir / f"{output_name}.json"
    else:
        filepath = out_dir / f"benchmark_{timestamp}.json"

    # 扁平格式：直接输出 {tc_name: {concurrency: metrics, ...}, ...}
    # 排除内部使用的 _search_meta
    data = {}
    for tc_name, tc_results in results.items():
        if not isinstance(tc_results, dict):
            data[tc_name] = tc_results
            continue
        data[tc_name] = {k: v for k, v in tc_results.items() if not k.startswith("_")}

    if timing:
        data["_timing"] = timing

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return str(filepath)


# =============================================================================
# 摘要输出
# =============================================================================

def print_summary(results: Dict[str, Any], mode: str = "default"):
    """打印测试结果摘要"""
    print(f"\n{'='*60}")
    print(f"性能测试摘要 (mode: {mode})")
    print(f"{'='*60}")

    for tc_name, tc_results in results.items():
        print(f"\n{tc_name}:")

        if not isinstance(tc_results, dict):
            continue

        # 找到最优并发级别的结果
        best_throughput = 0
        best_key = ""

        for key, metrics in tc_results.items():
            if key.startswith("_"):
                continue
            if not isinstance(metrics, dict) or "error" in metrics:
                continue
            throughput = metrics.get('Output token throughput (tok/s)', 0) or 0
            if throughput > best_throughput:
                best_throughput = throughput
                best_key = key

        if best_key:
            metrics = tc_results[best_key]
            print(f"  Best: concurrency={best_key}")
            print(f"  Output throughput: {metrics.get('Output token throughput (tok/s)', 'N/A')} tok/s")
            print(f"  Total throughput:  {metrics.get('Total token throughput (tok/s)', 'N/A')} tok/s")
            print(f"  Mean TTFT:         {metrics.get('Mean TTFT (ms)', 'N/A')} ms")
            print(f"  Mean TPOT:         {metrics.get('Mean TPOT (ms)', 'N/A')} ms")

        # 显示搜索元信息
        meta = tc_results.get("_search_meta")
        if meta:
            print(f"  Best concurrency:  {meta.get('best_concurrency', 'N/A')}")
            tested = meta.get('tested_levels', [])
            if not meta.get('all_levels_tested', True):
                print(f"  Tested levels:     {tested}")



# =============================================================================
# Strategy 解析
# =============================================================================

STRATEGY_CHOICES = ['quick', 'fast', 'comprehensive', 'fixed']


def resolve_strategy(args) -> str:
    """
    解析 strategy，优先级：--strategy > --quick > --concurrency-search > 默认 fast
    """
    if args.strategy:
        return args.strategy
    if args.quick:
        return "quick"
    if args.concurrency_search:
        return "fast"
    return "fast"


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="vLLM 性能基准测试 (重构版)")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--test-case", help="运行指定测试用例")
    parser.add_argument("--skip-case", action="append", default=[],
                        help="跳过指定用例（可多次使用），如 --skip-case prefill1_decode512")
    parser.add_argument("--dry-run", action="store_true", help="仅打印命令")
    parser.add_argument("--strategy", choices=STRATEGY_CHOICES,
                        help="测试策略: quick(烟雾测试) / fast(饱和即停,默认) / comprehensive(全跑) / fixed(固定并发)")
    parser.add_argument("--final-burst", action="store_true",
                        help="追加无限制并发的大规模最终测试")
    # 向后兼容别名
    parser.add_argument("--concurrency-search", action="store_true",
                        help="(向后兼容) 等同于 --strategy fast")
    parser.add_argument("--quick", action="store_true",
                        help="(向后兼容) 等同于 --strategy quick")
    parser.add_argument("--output-name", help="输出文件名（不含扩展名）")
    parser.add_argument("--output-dir", help="输出目录路径（默认 /flagos-workspace/results/）",
                        default=None)
    parser.add_argument("--mode", help="测试模式标记 (native/flagos_initial/flagos_optimized)",
                        default="default")
    args = parser.parse_args()

    # 解析 strategy
    strategy = resolve_strategy(args)

    # 加载配置
    print("加载配置...")
    config = load_config(args.config)

    if not validate_config(config):
        sys.exit(1)

    # 筛选测试用例
    test_matrix = config["test_matrix"]
    if args.test_case:
        test_matrix = [tc for tc in test_matrix if tc["name"] == args.test_case]
        if not test_matrix:
            print(f"ERROR: 测试用例 '{args.test_case}' 不存在")
            sys.exit(1)
    elif strategy == "quick":
        # quick 模式：只跑 4k_input_1k_output + 自动追加 max
        test_matrix = [tc for tc in test_matrix if tc["name"] == QUICK_TEST_CASE_NAME]
        if not test_matrix:
            print(f"WARN: 未找到 '{QUICK_TEST_CASE_NAME}' 用例，使用第一个已启用的用例")
            test_matrix = [tc for tc in config["test_matrix"] if tc.get("enabled", True)][:1]
        # quick 模式自动启用 final-burst（max 测试）
        if not args.final_burst:
            args.final_burst = True
            print("[QUICK MODE] 自动启用 --final-burst (max 测试)")
    elif strategy == "fixed":
        # fixed 模式：只跑有 fixed_concurrency 字段的用例
        test_matrix = [tc for tc in test_matrix if tc.get("enabled", True) and "fixed_concurrency" in tc]
        if not test_matrix:
            print("ERROR: --strategy fixed 需要至少一个用例配置了 fixed_concurrency 字段")
            sys.exit(1)
    else:
        # fast / comprehensive：跑所有 enabled 用例
        test_matrix = [tc for tc in test_matrix if tc.get("enabled", True)]

    # --skip-case 过滤
    if args.skip_case:
        skipped = set(args.skip_case)
        before = len(test_matrix)
        test_matrix = [tc for tc in test_matrix if tc["name"] not in skipped]
        if before > len(test_matrix):
            print(f"跳过用例: {', '.join(skipped)}")

    if not test_matrix:
        print("ERROR: 无可运行的测试用例（全部被跳过或未启用）")
        sys.exit(1)

    print(f"将运行 {len(test_matrix)} 个测试用例")

    strategy_labels = {
        "quick": "[策略] quick — 烟雾测试（num_prompts=concurrency, 只跑 4k_input_1k_output + max）",
        "fast": "[策略] fast — 智能搜索（num_prompts=concurrency, 饱和即停）",
        "comprehensive": "[策略] comprehensive — 全量测试（num_prompts=concurrency, 所有并发全跑）",
        "fixed": "[策略] fixed — 固定并发（只跑配置的 fixed_concurrency 级别）",
    }
    print(strategy_labels[strategy])
    if args.final_burst:
        print("[选项] --final-burst 已启用，每个用例完成后追加无限制并发测试")

    # 执行测试
    all_results = {}
    tc_timings = {}
    total_start = time.time()
    for tc in test_matrix:
        print(f"\n{'='*50}")
        print(f"测试用例: {tc['name']} (input={tc['input_len']}, output={tc['output_len']})")
        print('='*50)
        all_results[tc["name"]] = run_test_case(
            config, tc, args.dry_run,
            strategy=strategy, final_burst=args.final_burst
        )
        tc_timings[tc["name"]] = all_results[tc["name"]].get("_elapsed_seconds", 0)
    total_elapsed = round(time.time() - total_start, 1)

    # 打印摘要
    if not args.dry_run:
        print_summary(all_results, args.mode)

    # 保存结果
    if not args.dry_run:
        timing = {
            "total_seconds": total_elapsed,
            "per_test_case": tc_timings,
            "timestamp_start": datetime.fromtimestamp(total_start).isoformat(),
            "timestamp_end": datetime.now().isoformat(),
        }
        output_path = save_results(
            all_results, config,
            output_name=args.output_name,
            output_dir=args.output_dir,
            mode=args.mode,
            timing=timing,
        )
        print(f"\n结果已保存: {output_path} (耗时 {total_elapsed}s)")

    return all_results


if __name__ == "__main__":
    main()
