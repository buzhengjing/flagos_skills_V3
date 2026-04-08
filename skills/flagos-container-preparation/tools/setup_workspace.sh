#!/usr/bin/env bash
# setup_workspace.sh — 一次性工作区初始化
#
# 在容器准备阶段一次性完成：创建目录、复制脚本、安装依赖。
# 替代每个阶段各自 docker cp 的重复操作。
#
# Usage:
#   bash skills/flagos-container-preparation/tools/setup_workspace.sh <container_name>
#   bash skills/flagos-container-preparation/tools/setup_workspace.sh RoboBrain2.0-7B_flagos

set -euo pipefail

CONTAINER="${1:?用法: $0 <container_name>}"

# 项目根目录（此脚本所在位置的上三级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

echo "=========================================="
echo "FlagOS 工作区初始化"
echo "=========================================="
echo "  容器: ${CONTAINER}"
echo "  项目: ${PROJECT_ROOT}"
echo ""

# 1. 创建容器内目录结构
echo "[1/4] 创建目录结构..."
docker exec "${CONTAINER}" bash -c "
    mkdir -p /flagos-workspace/{scripts,logs,results,reports,eval,perf/config,shared,output,traces,config}
"
echo "  目录创建完成"

# 2. 复制所有脚本到容器
echo "[2/4] 复制脚本到容器..."

SCRIPTS_COPIED=0

# 脚本清单：源路径（相对 PROJECT_ROOT）→ 容器目标路径
# 格式：source_relative_path:container_dest_path
SCRIPT_MAP=(
    # 环境检查
    "skills/flagos-pre-service-inspection/tools/inspect_env.py:scripts/inspect_env.py"
    # FlagGems 开关切换
    "skills/flagos-service-startup/tools/toggle_flaggems.py:scripts/toggle_flaggems.py"
    # 服务就绪检测
    "skills/flagos-service-startup/tools/wait_for_service.sh:scripts/wait_for_service.sh"
    # TP 推算
    "skills/flagos-service-startup/tools/calc_tp_size.py:scripts/calc_tp_size.py"
    # 性能测试
    "skills/flagos-performance-testing/tools/benchmark_runner.py:scripts/benchmark_runner.py"
    # 性能对比
    "skills/flagos-performance-testing/tools/performance_compare.py:scripts/performance_compare.py"
    # 算子优化
    "skills/flagos-operator-replacement/tools/operator_optimizer.py:scripts/operator_optimizer.py"
    # 算子搜索编排
    "skills/flagos-operator-replacement/tools/operator_search.py:scripts/operator_search.py"
    # 算子配置生成（Plugin 场景）
    "skills/flagos-operator-replacement/tools/apply_op_config.py:scripts/apply_op_config.py"
    # 算子快速诊断
    "skills/flagos-operator-replacement/tools/diagnose_ops.py:scripts/diagnose_ops.py"
    # 组件安装（统一入口）
    "skills/flagos-component-install/tools/install_component.py:scripts/install_component.py"
    # FlagTree 安装脚本
    "skills/flagos-component-install/tools/install_flagtree.sh:scripts/install_flagtree.sh"
    # GPQA Diamond 快速精度评测
    "skills/flagos-eval-comprehensive/tools/fast_gpqa.py:eval/fast_gpqa.py"
    "skills/flagos-eval-comprehensive/tools/fast_gpqa_config.yaml:eval/fast_gpqa_config.yaml"
    # 评测配置模板
    "skills/flagos-eval-comprehensive/tools/config.yaml:eval/config.yaml"
    # 共享模块
    "skills/shared/env_utils.py:scripts/env_utils.py"
    "skills/shared/ops_constants.py:scripts/ops_constants.py"
    # GPU 统一检测
    "shared/detect_gpu.py:scripts/detect_gpu.py"
)

for entry in "${SCRIPT_MAP[@]}"; do
    src="${PROJECT_ROOT}/${entry%%:*}"
    dest="/flagos-workspace/${entry##*:}"
    if [ -f "$src" ]; then
        docker cp "$src" "${CONTAINER}:${dest}"
        SCRIPTS_COPIED=$((SCRIPTS_COPIED + 1))
        echo "  ✓ ${entry##*:}"
    fi
done

# .sh 文件需要 +x 权限
docker exec "${CONTAINER}" bash -c "chmod +x /flagos-workspace/scripts/*.sh 2>/dev/null || true"

# 评测脚本（eval_*.py 批量复制）
for eval_script in "${PROJECT_ROOT}"/skills/flagos-eval-comprehensive/tools/eval_*.py; do
    if [ -f "$eval_script" ]; then
        docker cp "$eval_script" "${CONTAINER}:/flagos-workspace/scripts/"
        echo "  ✓ $(basename "$eval_script")"
        SCRIPTS_COPIED=$((SCRIPTS_COPIED + 1))
    fi
done

# 性能测试配置目录
if [ -d "${PROJECT_ROOT}/skills/flagos-performance-testing/config" ]; then
    docker cp "${PROJECT_ROOT}/skills/flagos-performance-testing/config/." \
        "${CONTAINER}:/flagos-workspace/perf/config/"
    echo "  ✓ perf/config/"
fi

echo "  共复制 ${SCRIPTS_COPIED} 个脚本"

# 2.5. 确保 context.yaml 存在
if ! docker exec "${CONTAINER}" test -f /flagos-workspace/shared/context.yaml 2>/dev/null; then
    if [ -f "${PROJECT_ROOT}/shared/context.yaml" ]; then
        docker cp "${PROJECT_ROOT}/shared/context.yaml" "${CONTAINER}:/flagos-workspace/shared/context.yaml"
        echo "  ✓ shared/context.yaml (从模板创建)"
    else
        docker exec "${CONTAINER}" bash -c "echo '# FlagOS context' > /flagos-workspace/shared/context.yaml"
        echo "  ✓ shared/context.yaml (空文件)"
    fi
fi

# 3. 安装脚本依赖（如需要）
echo "[3/4] 检查脚本依赖..."
docker exec "${CONTAINER}" bash -c "
    PATH=/opt/conda/bin:\$PATH python3 -c 'import yaml' 2>/dev/null || PATH=/opt/conda/bin:\$PATH pip install pyyaml -q 2>/dev/null || true
"
echo "  依赖检查完成"

# 4. 验证
echo "[4/4] 验证部署..."
SCRIPT_COUNT=$(docker exec "${CONTAINER}" bash -c "ls /flagos-workspace/scripts/*.py /flagos-workspace/scripts/*.sh 2>/dev/null | wc -l")
echo "  容器内脚本数: ${SCRIPT_COUNT}"
docker exec "${CONTAINER}" ls -la /flagos-workspace/scripts/ 2>/dev/null || true

echo ""
echo "=========================================="
echo "工作区初始化完成"
echo "=========================================="
echo "  容器: ${CONTAINER}"
echo "  脚本目录: /flagos-workspace/scripts/"
echo "  结果目录: /flagos-workspace/results/"
echo "  报告目录: /flagos-workspace/reports/"
echo "=========================================="
