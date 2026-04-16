#!/usr/bin/env python3
"""
generate_report.py — FlagOS 迁移流程报告生成工具

从 context.yaml / traces / results / logs 汇总生成迁移报告。
流程完成或中途均可调用，缺失数据自动跳过对应段落。

Usage:
    python3 generate_report.py                          # 文本报告输出到 stdout
    python3 generate_report.py --json                   # JSON 报告输出到 stdout
    python3 generate_report.py --output report.md       # 文本报告写入文件
    python3 generate_report.py --json --output report.json
    python3 generate_report.py --workspace /flagos-workspace

退出码: 0=成功, 1=无数据（context.yaml 不存在）
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_yaml(path: str) -> Optional[dict]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


def read_csv_table(path: str) -> Optional[str]:
    """读取 CSV 并转为 markdown 表格。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        # performance_compare.py 可能直接输出 markdown 表格
        if content.startswith("|"):
            return content
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        if len(rows) < 2:
            return None
        header = rows[0]
        lines = ["| " + " | ".join(header) + " |"]
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
    except Exception:
        return None


def parse_issue_md(content: str) -> Dict[str, str]:
    """从 issue markdown 提取标题、类型、复现步骤等。"""
    result = {"title": "", "type": "", "steps": "", "description": "", "actual": ""}

    # 从 HTML 注释提取 type
    m = re.search(r'<!--\s*Type:\s*(\S+)\s*-->', content)
    if m:
        result["type"] = m.group(1)

    # 提取 ## Bug Report: xxx 标题
    m = re.search(r'## Bug Report:\s*(.+)', content)
    if m:
        result["title"] = m.group(1).strip()

    # 按 ### 分段提取
    sections = re.split(r'^### ', content, flags=re.MULTILINE)
    for sec in sections:
        if sec.startswith("Steps to Reproduce"):
            result["steps"] = sec.split("\n", 1)[1].strip() if "\n" in sec else ""
        elif sec.startswith("Description"):
            result["description"] = sec.split("\n", 1)[1].strip() if "\n" in sec else ""
        elif sec.startswith("Actual Behavior"):
            result["actual"] = sec.split("\n", 1)[1].strip() if "\n" in sec else ""

    return result


# =============================================================================
# 数据收集
# =============================================================================

class ReportData:
    """从工作目录收集所有可用数据。"""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self.context: Optional[dict] = None
        self.gpqa_result: Optional[dict] = None
        self.native_perf: Optional[dict] = None
        self.flagos_perf: Optional[dict] = None
        self.optimized_perf: Optional[dict] = None
        self.perf_compare_table: Optional[str] = None
        self.traces: Dict[str, dict] = {}
        self.issues: Dict[str, List[str]] = {}
        self.issue_files: List[Dict[str, str]] = []
        self.oplists: Dict[str, List[str]] = {}
        self.workflow_complete = False

    def collect(self) -> bool:
        """收集数据，返回 False 表示无 context.yaml。"""
        ctx_path = os.path.join(self.workspace, "shared", "context.yaml")
        self.context = read_yaml(ctx_path)
        if not self.context:
            # fallback: config/context_snapshot.yaml
            self.context = read_yaml(os.path.join(self.workspace, "config", "context_snapshot.yaml"))
        if not self.context:
            return False

        wf = self.context.get("workflow", {})
        self.workflow_complete = wf.get("all_done", False) is True

        r = os.path.join(self.workspace, "results")
        self.gpqa_result = read_json(os.path.join(r, "gpqa_result.json"))
        self.native_perf = read_json(os.path.join(r, "native_performance.json"))
        self.flagos_perf = read_json(os.path.join(r, "flagos_performance.json"))
        self.optimized_perf = read_json(os.path.join(r, "flagos_optimized.json"))
        self.perf_compare_table = read_csv_table(os.path.join(r, "performance_compare.csv"))

        # traces
        traces_dir = os.path.join(self.workspace, "traces")
        if os.path.isdir(traces_dir):
            for f in sorted(Path(traces_dir).glob("*.json")):
                data = read_json(str(f))
                if data:
                    self.traces[f.stem] = data

        # issue logs
        for name in ("issues_startup", "issues_accuracy", "issues_performance"):
            lines = read_lines(os.path.join(self.workspace, "logs", f"{name}.log"))
            if lines:
                self.issues[name] = lines

        # issue markdown files (含复现步骤)
        # 排除 issue_report_/issue_data_ 中间文件，只读最终的 issue_{type}_{repo}_{ts}.md
        if os.path.isdir(r):
            for f in sorted(Path(r).glob("issue_*.md")):
                if f.name.startswith(("issue_report_", "issue_data_")):
                    continue
                content = read_text(str(f))
                if content:
                    self.issue_files.append(parse_issue_md(content))

        # oplists
        for name in ("initial_oplist", "accuracy_tuned_oplist", "final_oplist"):
            lines = read_lines(os.path.join(r, f"{name}.txt"))
            if lines:
                self.oplists[name] = lines

        return True

    # helpers
    def get(self, *keys, default=None):
        """Nested dict get from context."""
        d = self.context
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k, default)
        return d

    def ledger_steps(self) -> List[dict]:
        return self.get("workflow_ledger", "steps", default=[])


# =============================================================================
# 文本报告生成
# =============================================================================

def format_duration(seconds) -> str:
    if not seconds or not isinstance(seconds, (int, float)):
        return "-"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def generate_text_report(data: ReportData) -> str:
    lines: List[str] = []

    # 流程状态警告
    if not data.workflow_complete:
        lines.append("⚠ 流程未完成 — 以下为当前已有数据的报告")
        lines.append("")

    lines.append("FlagOS 迁移报告")
    lines.append("=" * 40)

    # 基本信息
    model = data.get("model", "name", default="N/A")
    gpu_count = data.get("gpu", "count", default="N/A")
    gpu_type = data.get("gpu", "type", default="N/A")
    container = data.get("container", "name", default="N/A")
    env_type = data.get("environment", "env_type", default="N/A")

    lines.append(f"模型: {model}")
    lines.append(f"GPU: {gpu_count}x {gpu_type}")
    lines.append(f"容器: {container}")
    lines.append(f"环境: {env_type}")

    # 算子状态
    oplist_count = data.get("service", "enable_oplist_count", default=None)
    if oplist_count is not None:
        lines.append("")
        lines.append("算子状态:")
        lines.append(f"  V2 算子数: {oplist_count} 个")

    # 精度评测
    eval_sec = data.get("eval", default={})
    v1_score = eval_sec.get("v1_score") if isinstance(eval_sec, dict) else None
    v2_score = eval_sec.get("v2_score") if isinstance(eval_sec, dict) else None
    deviation = eval_sec.get("deviation") if isinstance(eval_sec, dict) else None
    threshold = eval_sec.get("threshold", 5.0) if isinstance(eval_sec, dict) else 5.0

    # 也尝试从 gpqa_result.json 补充
    if data.gpqa_result and v1_score is None:
        v1_score = data.gpqa_result.get("v1_score") or data.gpqa_result.get("native_score")
        v2_score = data.gpqa_result.get("v2_score") or data.gpqa_result.get("flagos_score")
        deviation = data.gpqa_result.get("deviation")

    if v1_score is not None or v2_score is not None:
        lines.append("")
        lines.append("精度评测 (GPQA Diamond):")
        lines.append(f"  V1: {v1_score}%" if v1_score is not None else "  V1: N/A")
        lines.append(f"  V2: {v2_score}%" if v2_score is not None else "  V2: N/A")
        if deviation is not None:
            lines.append(f"  V1 vs V2 偏差: {deviation}% (阈值 {threshold}%)")

    # 算子调优
    optimization = data.get("optimization", default={})
    excluded_acc = data.get("eval", "excluded_ops_accuracy", default=None)
    excluded_perf = data.get("operator_replacement", "excluded_ops_performance", default=None)
    # also check optimization section
    if isinstance(optimization, dict):
        excluded_acc = excluded_acc or optimization.get("excluded_ops_accuracy")
        excluded_perf = excluded_perf or optimization.get("excluded_ops_performance")

    has_tuning = excluded_acc or excluded_perf
    if has_tuning:
        lines.append("")
        lines.append("算子调优（如有）:")
        if excluded_acc:
            acc_ops = excluded_acc if isinstance(excluded_acc, list) else [excluded_acc]
            lines.append(f"  精度调优: 关闭 {len(acc_ops)} 个算子 ({', '.join(str(o) for o in acc_ops)})")
        if excluded_perf:
            perf_ops = excluded_perf if isinstance(excluded_perf, list) else [excluded_perf]
            lines.append(f"  性能调优: 关闭 {len(perf_ops)} 个算子 ({', '.join(str(o) for o in perf_ops)})")

        # 最终算子数
        if data.oplists.get("final_oplist"):
            lines.append(f"  最终启用算子: {len(data.oplists['final_oplist'])} 个")
        all_excluded = []
        if excluded_acc and isinstance(excluded_acc, list):
            all_excluded.extend(excluded_acc)
        if excluded_perf and isinstance(excluded_perf, list):
            all_excluded.extend(excluded_perf)
        if all_excluded:
            lines.append(f"  禁用算子: {', '.join(str(o) for o in all_excluded)}")

    # 性能对比
    if data.perf_compare_table:
        lines.append("")
        lines.append("性能对比:")
        lines.append(data.perf_compare_table)
    elif data.native_perf or data.flagos_perf:
        perf = data.get("performance", default={})
        min_ratio = perf.get("min_ratio") if isinstance(perf, dict) else None
        if min_ratio is not None:
            lines.append("")
            lines.append("性能对比:")
            lines.append(f"  V2/V1 min ratio: {min_ratio}%")

    # 流程耗时
    steps = data.ledger_steps()
    if steps:
        lines.append("")
        lines.append("流程耗时:")
        for s in steps:
            name = s.get("name", s.get("step", ""))
            status = s.get("status", "pending")
            dur = s.get("duration_seconds", 0)
            if status == "success":
                lines.append(f"  {name}: {format_duration(dur)}")
            elif status == "skipped":
                reason = s.get("skip_reason", "")
                lines.append(f"  {name}: 跳过" + (f" ({reason})" if reason else ""))
            elif status == "failed":
                reason = s.get("fail_reason", "")
                lines.append(f"  {name}: 失败" + (f" ({reason})" if reason else ""))
            elif status == "in_progress":
                lines.append(f"  {name}: 进行中...")
            else:
                lines.append(f"  {name}: 未开始")

    # 总耗时
    timing = data.get("timing", default={})
    if isinstance(timing, dict) and timing.get("total_duration_seconds"):
        lines.append(f"  总耗时: {format_duration(timing['total_duration_seconds'])}")

    # 发布信息
    release = data.get("release", default={})
    wf = data.get("workflow", default={})
    if isinstance(release, dict) and release:
        lines.append("")
        lines.append("发布信息:")
        if release.get("harbor_image"):
            lines.append(f"  Harbor 镜像: {release['harbor_image']}")
        if release.get("modelscope_url"):
            lines.append(f"  ModelScope: {release['modelscope_url']}")
        if release.get("huggingface_url"):
            lines.append(f"  HuggingFace: {release['huggingface_url']}")

    qualified = wf.get("qualified") if isinstance(wf, dict) else None
    if qualified is not None:
        visibility = "公开" if qualified else "私有"
        lines.append(f"  发布方式: {visibility}")
        lines.append(f"  qualified: {qualified}")

    # 问题与复现
    if data.issue_files or data.issues:
        lines.append("")
        lines.append("问题与复现:")

        # 统计
        if data.issues:
            label_map = {
                "issues_startup": "服务启动",
                "issues_accuracy": "精度",
                "issues_performance": "性能",
            }
            for key, entries in data.issues.items():
                label = label_map.get(key, key)
                count = sum(1 for e in entries if e.startswith("["))
                lines.append(f"  {label}: {count} 条记录")

        # 每个 issue 的详情和复现步骤
        type_label = {
            "operator-crash": "算子崩溃",
            "accuracy-zero": "精度归零",
            "accuracy-degraded": "精度下降",
            "performance-degraded": "性能下降",
            "flagtree-error": "FlagTree 错误",
            "plugin-error": "Plugin 错误",
        }
        for i, issue in enumerate(data.issue_files, 1):
            lines.append("")
            itype = type_label.get(issue["type"], issue["type"])
            lines.append(f"  [{i}] {issue['title'] or '未知问题'} ({itype})")
            if issue["description"]:
                first_line = issue["description"].split("\n")[0].strip()
                lines.append(f"      描述: {first_line}")
            if issue["steps"]:
                lines.append("      复现步骤:")
                for step_line in issue["steps"].splitlines():
                    if step_line.strip():
                        lines.append(f"        {step_line.strip()}")

    # 结论
    lines.append("")
    if qualified is True:
        lines.append("结论: qualified (公开发布)")
    elif qualified is False:
        lines.append("结论: 不合格 (私有发布)")
    elif not data.workflow_complete:
        lines.append("结论: 流程未完成，暂无最终判定")
    else:
        lines.append("结论: N/A")
    lines.append("=" * 40)

    return "\n".join(lines)


# =============================================================================
# JSON 报告生成
# =============================================================================

def generate_json_report(data: ReportData) -> dict:
    wf = data.get("workflow", default={}) or {}
    eval_sec = data.get("eval", default={}) or {}
    perf = data.get("performance", default={}) or {}
    release = data.get("release", default={}) or {}

    # 精度数据（context 优先，gpqa_result.json 补充）
    v1_score = eval_sec.get("v1_score")
    v2_score = eval_sec.get("v2_score")
    deviation = eval_sec.get("deviation")
    if data.gpqa_result and v1_score is None:
        v1_score = data.gpqa_result.get("v1_score") or data.gpqa_result.get("native_score")
        v2_score = data.gpqa_result.get("v2_score") or data.gpqa_result.get("flagos_score")
        deviation = data.gpqa_result.get("deviation")

    # 步骤状态
    steps_summary = []
    for s in data.ledger_steps():
        steps_summary.append({
            "step": s.get("step", ""),
            "name": s.get("name", ""),
            "status": s.get("status", "pending"),
            "duration_seconds": s.get("duration_seconds", 0),
            "notes": s.get("notes", ""),
            "fail_reason": s.get("fail_reason", ""),
            "skip_reason": s.get("skip_reason", ""),
        })

    # 问题统计
    issues_summary = {}
    for key, entries in data.issues.items():
        count = sum(1 for e in entries if e.startswith("["))
        issues_summary[key] = count

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "workflow_complete": data.workflow_complete,
        "model": {
            "name": data.get("model", "name", default=""),
            "container_path": data.get("model", "container_path", default=""),
        },
        "container": {
            "name": data.get("container", "name", default=""),
        },
        "gpu": {
            "count": data.get("gpu", "count", default=0),
            "type": data.get("gpu", "type", default=""),
            "vendor": data.get("gpu", "vendor", default=""),
        },
        "environment": {
            "env_type": data.get("environment", "env_type", default=""),
        },
        "accuracy": {
            "v1_score": v1_score,
            "v2_score": v2_score,
            "deviation": deviation,
            "threshold": eval_sec.get("threshold", 5.0),
            "ok": wf.get("accuracy_ok"),
        },
        "performance": {
            "min_ratio": perf.get("min_ratio"),
            "target_ratio": perf.get("target_ratio", 80.0),
            "ok": wf.get("performance_ok"),
        },
        "operator_tuning": {
            "excluded_accuracy": data.get("eval", "excluded_ops_accuracy", default=[]),
            "excluded_performance": data.get("operator_replacement", "excluded_ops_performance", default=[]),
            "initial_oplist_count": len(data.oplists.get("initial_oplist", [])),
            "final_oplist_count": len(data.oplists.get("final_oplist", [])) or None,
        },
        "release": {
            "qualified": wf.get("qualified"),
            "harbor_image": release.get("harbor_image", ""),
            "modelscope_url": release.get("modelscope_url", ""),
            "huggingface_url": release.get("huggingface_url", ""),
        },
        "steps": steps_summary,
        "issues": {
            "summary": issues_summary,
            "details": [
                {
                    "title": issue["title"],
                    "type": issue["type"],
                    "description": issue["description"].split("\n")[0].strip() if issue["description"] else "",
                    "steps_to_reproduce": issue["steps"],
                    "actual_behavior": issue["actual"],
                }
                for issue in data.issue_files
            ],
        },
        "_meta": {
            "generated_at": "报告生成时间 (ISO 8601)",
            "workflow_complete": "全流程是否已完成",
            "accuracy.ok": "精度是否达标（含调优后结果）",
            "performance.ok": "性能是否达标（含调优后结果）",
            "release.qualified": "综合判定 = service_ok AND accuracy_ok AND performance_ok",
            "steps[].status": "pending / in_progress / success / failed / skipped",
            "issues.summary": "各类问题日志的记录条数",
            "issues.details": "每个 issue 的标题、类型、复现步骤和实际行为",
        },
    }
    return report


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="FlagOS 迁移流程报告生成")
    parser.add_argument("--workspace", default="/flagos-workspace", help="工作目录路径")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="JSON 格式输出")
    parser.add_argument("--output", "-o", help="输出文件路径（不指定则输出到 stdout）")
    args = parser.parse_args()

    data = ReportData(args.workspace)
    if not data.collect():
        print("错误: 未找到 context.yaml，无法生成报告", file=sys.stderr)
        print(f"  已检查: {args.workspace}/shared/context.yaml", file=sys.stderr)
        print(f"  已检查: {args.workspace}/config/context_snapshot.yaml", file=sys.stderr)
        sys.exit(1)

    if args.json_mode:
        report = generate_json_report(data)
        output = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        output = generate_text_report(data)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"报告已写入: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
