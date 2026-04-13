#!/bin/bash
# FlagOS 全自动迁移流程 — 一键启动脚本（V1+V2，无 V3）
#
# 用法:
#   bash prompts/run_pipeline.sh <容器名或镜像地址> <模型名> <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]
#
# 自动识别：第一参数若为已有容器则走容器模式，否则视为镜像地址
# 模型路径：仅需模型名，自动搜索宿主机路径；未找到则容器内自动下载
#
# 示例:
#   bash prompts/run_pipeline.sh qwen3-8b-test Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass
#   bash prompts/run_pipeline.sh harbor.baai.ac.cn/flagrelease/qwen3:latest Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass
#
# 向后兼容（已弃用）:
#   bash prompts/run_pipeline.sh --image <镜像地址> <模型名> [<宿主机模型路径>] <tokens...>
#
# 前置条件:
#   - Claude Code CLI 已安装 (claude 命令可用)
#   - Docker daemon 正在运行
#   - 当前目录为项目根目录 (flagos_skills_V3/)

set -euo pipefail

# ========== Docker 前置检查 ==========
# 用 docker ps 代替 docker info，避免 authZ 插件拦截 docker info 导致误判
if ! docker ps &>/dev/null; then
    echo "错误: Docker daemon 未运行或无权限，请检查 Docker 状态"
    exit 1
fi

# ========== 参数解析与自动识别 ==========
IMAGE_MODE=false
MODEL_PATH=""
FILTER_FLAGS=""

if [[ "${1:-}" == "--image" ]]; then
    # 向后兼容：旧 --image 格式
    echo "⚠ --image 标志已弃用，直接传镜像地址作为第一参数即可自动识别"
    shift
    IMAGE_MODE=true
    if [ $# -ge 8 ]; then
        # 旧格式: --image <镜像> <模型名> <宿主机模型路径> <5个token> [--verbose]
        IMAGE="$1"
        MODEL="$2"
        MODEL_PATH="$3"
        export MODELSCOPE_TOKEN="$4"
        export HF_TOKEN="$5"
        export GITHUB_TOKEN="$6"
        export HARBOR_USER="$7"
        export HARBOR_PASSWORD="$8"
        if [[ "${9:-}" == "--verbose" ]]; then
            FILTER_FLAGS="--verbose"
        fi
    elif [ $# -ge 7 ]; then
        # 新格式带 --image: --image <镜像> <模型名> <5个token> [--verbose]
        IMAGE="$1"
        MODEL="$2"
        export MODELSCOPE_TOKEN="$3"
        export HF_TOKEN="$4"
        export GITHUB_TOKEN="$5"
        export HARBOR_USER="$6"
        export HARBOR_PASSWORD="$7"
        if [[ "${8:-}" == "--verbose" ]]; then
            FILTER_FLAGS="--verbose"
        fi
    else
        echo "用法: $0 <容器名或镜像地址> <模型名> <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]"
        exit 1
    fi
else
    # 统一格式：7 个位置参数
    if [ $# -lt 7 ]; then
        echo "用法: $0 <容器名或镜像地址> <模型名> <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]"
        echo ""
        echo "自动识别：第一参数若为已有容器则走容器模式，否则视为镜像地址"
        echo ""
        echo "示例:"
        echo "  $0 qwen3-8b-test Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass"
        echo "  $0 harbor.baai.ac.cn/flagrelease/qwen3:latest Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass"
        echo "  加 --verbose 显示全量终端输出（调试用）"
        exit 1
    fi

    TARGET="$1"
    MODEL="$2"
    export MODELSCOPE_TOKEN="$3"
    export HF_TOKEN="$4"
    export GITHUB_TOKEN="$5"
    export HARBOR_USER="$6"
    export HARBOR_PASSWORD="$7"
    if [[ "${8:-}" == "--verbose" ]]; then
        FILTER_FLAGS="--verbose"
    fi

    # 自动识别：含冒号(:)或斜杠(/)的视为镜像地址，否则尝试 docker inspect 判断
    if [[ "$TARGET" == *":"* ]] || [[ "$TARGET" == *"/"* ]]; then
        # 包含冒号(tag)或斜杠(registry路径)，强制镜像模式
        IMAGE_MODE=true
        IMAGE="$TARGET"
    elif docker inspect --type=container "$TARGET" &>/dev/null; then
        IMAGE_MODE=false
        CONTAINER="$TARGET"
    else
        IMAGE_MODE=true
        IMAGE="$TARGET"
    fi
fi

# ========== 镜像模式：自动搜索宿主机模型路径 ==========
if $IMAGE_MODE && [ -z "$MODEL_PATH" ]; then
    echo "[pre-flight] 搜索宿主机模型路径: ${MODEL} ..."
    SEARCH_JSON=$(python3 skills/flagos-container-preparation/tools/check_model_local.py \
        --model "${MODEL}" --no-download --output-json 2>/dev/null) || SEARCH_JSON=""

    MODEL_PATH=$(echo "$SEARCH_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('best_match') or '')
except:
    print('')
" 2>/dev/null) || MODEL_PATH=""

    if [ -n "$MODEL_PATH" ]; then
        echo "  ✓ 找到: ${MODEL_PATH}"
    else
        echo "  ⚠ 宿主机未找到模型，将在容器创建后自动下载"
        MODEL_PATH="__AUTO_DOWNLOAD__"
    fi
fi

# ========== Banner ==========
echo "============================================================"
echo "  FlagOS 全自动迁移流程"
echo "============================================================"
if $IMAGE_MODE; then
    echo "  目标: ${IMAGE} (镜像，自动识别)"
else
    echo "  目标: ${CONTAINER} (容器，自动识别)"
fi
echo "  模型: ${MODEL}"
if $IMAGE_MODE && [ "$MODEL_PATH" != "__AUTO_DOWNLOAD__" ] && [ -n "$MODEL_PATH" ]; then
    echo "  模型路径: ${MODEL_PATH} (自动检测)"
elif $IMAGE_MODE; then
    echo "  模型路径: 容器创建后自动下载"
fi
echo "  模式: V1 + V2（无 V3 算子优化）"
echo "  权限: --permission-mode auto + settings allowlist"
echo "============================================================"
echo ""

# ========== 构造 Prompt ==========
# 公共部分：tokens、执行模式、进度输出要求、步骤②-⑥
COMMON_TOKENS=$(cat <<TOKENS_EOF

**容器内上传 Token**（⑥发布阶段在容器内 docker exec 时设置）：
  MODELSCOPE_TOKEN=${MODELSCOPE_TOKEN}
  HF_TOKEN=${HF_TOKEN}
  GITHUB_TOKEN=${GITHUB_TOKEN}
  HARBOR_USER=${HARBOR_USER}
  HARBOR_PASSWORD=${HARBOR_PASSWORD}
TOKENS_EOF
)

COMMON_PLAN_FIRST=$(cat <<'PLAN_EOF'

**执行模式：计划优先（Plan-First）**

在执行任何操作之前，先完成规划阶段：
1. 依次读取以下 SKILL.md 文件，提取每步的关键命令、参数、文件路径：
   - skills/flagos-container-preparation/SKILL.md
   - skills/flagos-pre-service-inspection/SKILL.md
   - skills/flagos-service-startup/SKILL.md
   - skills/flagos-eval-comprehensive/SKILL.md
   - skills/flagos-performance-testing/SKILL.md
   - skills/flagos-release/SKILL.md
PLAN_EOF
)

COMMON_PLAN_STEPS=$(cat <<PLAN_STEPS_EOF
2. 生成 execution_plan.md，写入 /data/flagos-workspace/${MODEL}/config/execution_plan.md
   - 包含每步的完整命令（变量已替换为实际值：容器名、模型名、端口等）
   - 包含每步的输入/输出文件路径
   - 包含每步的 context.yaml 读写字段清单（注意：context.yaml 位于容器内 /flagos-workspace/shared/context.yaml）
   - 包含每步的校验检查项
3. 每个步骤开始前，Read execution_plan.md 中对应段落刷新记忆
4. 每个步骤开始前，通过 docker exec 读取容器内 /flagos-workspace/shared/context.yaml 获取最新状态（禁止读写项目目录下的 shared/context.template.yaml）

请严格按以下 6 步执行 FlagOS 全自动迁移流程。步骤①-⑤ 全自动执行，步骤间无需询问我。仅在⑥发布阶段如需 token 再来询问。

**严格禁止**：不进行 V3 算子优化。精度或性能不达标时仅输出报告，不调用 operator_search.py / operator_optimizer.py / diagnose_ops.py 进行任何算子排查或优化。直接继续后续步骤。

**进度输出要求（-p 模式下必须遵守）**：
由于本流程在非交互模式（-p）下运行，工具调用结果不会直接显示。你必须在以下时机主动输出文本，确保用户能实时了解进度：

1. 每个步骤（①-⑥）开始时，输出：\`[步骤X] 开始 — <步骤名称>\`
2. 每个关键命令执行后，输出一行结果摘要（成功/失败 + 关键数据），例如：
   - \`  ✓ 容器 xxx 运行中，GPU 8x\`
   - \`  ✓ env_type=vllm_flaggems，flaggems=5.1.0\`
   - \`  ✓ 服务就绪，端口 8000，模型已加载\`
   - \`  ✓ V1 精度: 62.1%\`
   - \`  ✗ V2/V1 性能比 72.1% < 80%，已写入 issue\`
3. 每个步骤完成时，输出：\`[步骤X] 完成 — 耗时 Xm Xs\`
4. 遇到错误或异常时，立即输出错误摘要，不要等到步骤结束
5. 长时间操作（服务启动、精度评测、性能测试）期间，每 30 秒输出一次等待状态

不要省略这些输出。宁可多输出一行，也不要让用户看到长时间空白。
PLAN_STEPS_EOF
)

# 步骤②-⑥ 共用
COMMON_STEPS_2_TO_6=$(cat <<STEPS_EOF

② 环境检测：
   - docker exec \${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/inspect_env.py --output-json"
   - 判定 env_type（native / vllm_flaggems / vllm_plugin_flaggems）
   - 如 env_type=vllm_flaggems，额外执行 toggle_flaggems.py --action analyze
   - 写入容器内 /flagos-workspace/shared/context.yaml + traces/02_environment_inspection.json

③ 服务启动（default 模式验证）：
   - 不修改 FlagGems 状态，原样启动服务
   - wait_for_service.sh 等待就绪
   - 检查 flaggems_enable_oplist.txt
   - 验证 /v1/models 和推理测试
   - 启动失败时写入 issue 到容器内 /flagos-workspace/logs/issues_startup.log
   - 写入容器内 /flagos-workspace/shared/context.yaml + traces/03_service_startup.json

④ 精度评测（GPQA Diamond，仅 V1+V2）：
   - 先确认无性能测试进程在运行
   a) V1 精度（Native）：
      - toggle_flaggems.py --action disable → 重启服务（native 模式）
      - fast_gpqa.py 评测 → 保存 results/gpqa_native.json
   b) V2 精度（FlagGems）：
      - toggle_flaggems.py --action enable → 重启服务（flagos 模式）
      - fast_gpqa.py 评测 → 保存 results/gpqa_flagos.json
   c) V1 vs V2 精度对比（5% 阈值）：
      - 达标 → 输出"精度达标"
      - 不达标 → 写入 accuracy_anomaly issue + 输出偏差报告 + 当前算子列表，不排查算子，继续后续
   - 写入 traces/04_quick_accuracy.json

⑤ 性能评测（quick 策略 4k→1k，仅 V1+V2）：
   - 先确认无精度评测进程在运行
   a) V1 性能（Native）：
      - toggle_flaggems.py --action disable → 重启服务（native 模式）
      - benchmark_runner.py --strategy quick --output-name native_performance → 保存 results/native_performance.json
   b) V2 性能（FlagGems）：
      - toggle_flaggems.py --action enable → 重启服务（flagos 模式）
      - benchmark_runner.py --strategy quick --output-name flagos_performance → 保存 results/flagos_performance.json
   c) 性能对比：
      - performance_compare.py --native ... --flagos-full ... --format markdown
      - 达标(≥80%) → 输出"性能达标"
      - 不达标 → 写入 performance_low issue + 输出对比报告，不触发算子优化，继续后续
   - 写入 traces/05_quick_performance.json

⑥ 打包发布（使用 flagos-release skill）：
   - 先将容器内最新 context.yaml 同步到宿主机：
     docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml
   - 在宿主机执行 release 工具: python3 skills/flagos-release/tools/main.py --from-context /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml
   - 工具自动完成: qualified 判定 → docker commit/tag/push → README 生成 → ModelScope/HuggingFace 上传 → 数据回传
   - 工具完成后写入 traces/06_release.json + 更新容器内 /flagos-workspace/shared/context.yaml
   - **必须**：发布完成后（无论是否成功调用了 main.py），都要执行数据回传，确保宿主机有结果文件：
     for dir in results traces; do
       docker cp \${CONTAINER}:/flagos-workspace/\${dir}/. /data/flagos-workspace/${MODEL}/\${dir}/
     done
     docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml

全流程最后一步（报告输出前），将最终 context 回传到宿主机：
  docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_final.yaml

**重要：context.yaml 隔离规则**
- 运行时 context 位于容器内 /flagos-workspace/shared/context.yaml，每个容器独立
- 禁止读写项目目录下的 shared/context.template.yaml（那是初始化模板）
- 读取 context 统一用：docker exec \${CONTAINER} cat /flagos-workspace/shared/context.yaml
- 写入 context 统一用：docker exec \${CONTAINER} 在容器内操作

全流程结束后输出完整的 FlagOS 迁移报告（含精度、性能、发布信息、耗时统计、问题记录摘要）。
STEPS_EOF
)

# ========== 根据模式构造步骤① ==========
if $IMAGE_MODE; then
    if [ "$MODEL_PATH" = "__AUTO_DOWNLOAD__" ]; then
        # 镜像模式 + 宿主机未找到模型 → 容器创建后自动下载
        STEP1=$(cat <<STEP1_EOF
① 容器准备（从镜像创建 + 自动下载模型）：
   - 宿主机未找到模型 ${MODEL}，需在容器创建后下载
   - 检测 GPU 厂商（nvidia-smi / npu-smi 等），选择 SKILL.md 中对应的 docker run 模板
   - docker run 创建容器（镜像: ${IMAGE}，不挂载模型路径）
   - 容器名自动生成为 <model_short_name>_flagos（如 Qwen3-8B_flagos）
   - 如同名容器已存在，追加时间戳：<model_short_name>_flagos_<MMDD_HHMM>
   - 镜像模式下禁止复用已有容器，必须 docker run 新建
   - 容器创建后，在容器内搜索+下载模型：
     python3 skills/flagos-container-preparation/tools/check_model_local.py --model "${MODEL}" --mode container --container \${CONTAINER} --output-json
     从输出 JSON 中提取 final_container_path 和 final_host_path，记录到容器内 /flagos-workspace/shared/context.yaml
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh \${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml（entry.type=new_container, image.name=${IMAGE}）+ traces/01_container_preparation.json
STEP1_EOF
        )
    else
        # 镜像模式 + 宿主机找到模型路径
        STEP1=$(cat <<STEP1_EOF
① 容器准备（从镜像创建）：
   - 宿主机模型路径已自动检测: ${MODEL_PATH}
   - 检测 GPU 厂商（nvidia-smi / npu-smi 等），选择 SKILL.md 中对应的 docker run 模板
   - docker run 创建容器（镜像: ${IMAGE}，挂载宿主机模型路径: ${MODEL_PATH}）
   - 容器名自动生成为 <model_short_name>_flagos（如 Qwen3-8B_flagos）
   - 如同名容器已存在，追加时间戳：<model_short_name>_flagos_<MMDD_HHMM>
   - 镜像模式下禁止复用已有容器，必须 docker run 新建
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh \${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml（entry.type=new_container, image.name=${IMAGE}）+ traces/01_container_preparation.json
STEP1_EOF
        )
    fi
    PROMPT="镜像: ${IMAGE}，模型名: ${MODEL}
${COMMON_TOKENS}
${COMMON_PLAN_FIRST}
${COMMON_PLAN_STEPS}

${STEP1}
${COMMON_STEPS_2_TO_6}"
else
    STEP1=$(cat <<STEP1_EOF
① 下载模型+容器准备：
   - 验证容器 ${CONTAINER} 运行状态（docker inspect + docker start）
   - 搜索模型权重：python3 skills/flagos-container-preparation/tools/check_model_local.py --model "${MODEL}" --mode container --container ${CONTAINER} --output-json
     - 先在容器内搜索（/data, /models, /root, /home, /workspace, /mnt, /opt）
     - 再在宿主机搜索，检查是否已通过挂载卷映射到容器
     - 如容器内未找到 → 在容器内自动从 ModelScope 下载（优先下载到已挂载卷路径，避免写入 overlay）
     - 从输出 JSON 中提取 final_container_path 和 final_host_path，记录到容器内 /flagos-workspace/shared/context.yaml 的 model.container_path 和 model.local_path
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh ${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml + traces/01_container_preparation.json
STEP1_EOF
    )
    PROMPT="容器名: ${CONTAINER}，模型名: ${MODEL}
${COMMON_TOKENS}
${COMMON_PLAN_FIRST}
${COMMON_PLAN_STEPS}

${STEP1}
${COMMON_STEPS_2_TO_6}"
fi

# ========== 部署权限白名单 ==========
[ -f .claude/settings.local.json ] || (mkdir -p .claude && cp settings.local.json .claude/settings.local.json)

# ========== 启动 Claude Code ==========
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 Claude Code 全自动流程..."
echo ""

# ========== 宿主机日志目录归档 ==========
LOG_DIR="/data/flagos-workspace/${MODEL}/logs"
if [ -d "${LOG_DIR}" ] && [ "$(ls -A "${LOG_DIR}" 2>/dev/null)" ]; then
    ARCHIVE_TS="$(date +%Y%m%d_%H%M%S)"
    HOST_ARCHIVE="/data/flagos-workspace/${MODEL}/archive/${ARCHIVE_TS}/logs"
    mkdir -p "${HOST_ARCHIVE}"
    for f in "${LOG_DIR}"/*; do
        [ -f "$f" ] || [ -L "$f" ] || continue
        mv "$f" "${HOST_ARCHIVE}/"
    done
    echo "  宿主机 logs 已归档到: ${HOST_ARCHIVE}/"
fi

mkdir -p "/data/flagos-workspace/${MODEL}/logs"

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="/data/flagos-workspace/${MODEL}/logs"
LOG_FILE="${LOG_DIR}/claude_pipeline_${TIMESTAMP}.log"
FULL_LOG="${LOG_DIR}/claude_full_${TIMESTAMP}.log"
DEBUG_FILE="${LOG_DIR}/claude_debug_${TIMESTAMP}.log"
PIPELINE_LOG="${LOG_DIR}/pipeline.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "日志文件:"
echo "  原始事件流: ${LOG_FILE}"
echo "  可读执行记录: ${FULL_LOG}"
echo "  流水线日志: ${PIPELINE_LOG}"
echo "  内部 debug: ${DEBUG_FILE}"
echo ""

# 禁用实验性 beta 功能，避免第三方代理不支持 context_management 返回 400
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

claude -p "${PROMPT}" \
    --permission-mode auto \
    --verbose \
    --output-format stream-json \
    --debug-file "${DEBUG_FILE}" \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" > "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" ${FILTER_FLAGS} || true

# ===== Claude 退出后自动故障诊断 =====
# 从宿主机 context_snapshot 读取容器名（镜像模式下容器名由 Claude 动态创建）
# 不再 fallback 到项目级 shared/context.yaml，避免多任务冲突
DIAG_CONTAINER=""
CONTEXT_FILE="/data/flagos-workspace/${MODEL}/config/context_snapshot.yaml"
if [ -f "${CONTEXT_FILE}" ]; then
    DIAG_CONTAINER=$(python3 -c "
import yaml, sys
try:
    with open('${CONTEXT_FILE}') as f:
        ctx = yaml.safe_load(f)
    print(ctx.get('container',{}).get('name',''))
except: pass
" 2>/dev/null) || DIAG_CONTAINER=""
fi
# fallback: 容器模式下直接用脚本变量
[ -z "${DIAG_CONTAINER}" ] && DIAG_CONTAINER="${CONTAINER:-}"

if [ -n "${DIAG_CONTAINER}" ] && docker inspect --type=container "${DIAG_CONTAINER}" &>/dev/null; then
    ALL_DONE=$(docker exec "${DIAG_CONTAINER}" bash -c "
        PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
try:
    with open('/flagos-workspace/shared/context.yaml') as f:
        ctx = yaml.safe_load(f)
    print(ctx.get('workflow',{}).get('all_done', False))
except: print('False')
\"" 2>/dev/null || echo "False")

    if [ "${ALL_DONE}" != "True" ]; then
        echo ""
        echo "============================================"
        echo "⚠  Claude 进程已退出但流程未完成，自动诊断中..."
        echo "============================================"
        echo ""
        # tee: 终端打印 + 写入文件
        docker exec "${DIAG_CONTAINER}" bash -c \
          "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/diagnose_failure.py" \
          2>&1 | tee "${LOG_DIR}/failure_diagnosis.txt"
        # JSON 版本供新 Claude 会话读取
        docker exec "${DIAG_CONTAINER}" bash -c \
          "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/diagnose_failure.py --json" \
          > "${LOG_DIR}/failure_diagnosis.json" 2>/dev/null || true
        echo ""
        echo "诊断报告已保存:"
        echo "  可读版: ${LOG_DIR}/failure_diagnosis.txt"
        echo "  JSON版: ${LOG_DIR}/failure_diagnosis.json"
    fi
fi

# ========== 兜底：同步容器产出到宿主机 ==========
# 无论 Claude 是否在步骤⑥中调用了 main.py，都确保容器内产出同步到宿主机。
# setup_workspace.sh 会在流程开始时归档清空宿主机 results/traces，
# 如果 Claude 跳过了 main.py 的 _sync_to_host()，宿主机将无数据。
if [ -n "${DIAG_CONTAINER}" ] && docker inspect --type=container "${DIAG_CONTAINER}" &>/dev/null; then
    HOST_BASE="/data/flagos-workspace/${MODEL}"
    echo ""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 兜底同步：容器产出 → 宿主机..."
    for dir in results traces; do
        mkdir -p "${HOST_BASE}/${dir}"
        docker cp "${DIAG_CONTAINER}:/flagos-workspace/${dir}/." "${HOST_BASE}/${dir}/" 2>/dev/null && \
            echo "  ✓ ${dir}/ 已同步" || echo "  ⚠ ${dir}/ 同步失败或为空"
    done
    # context snapshot
    mkdir -p "${HOST_BASE}/config"
    docker cp "${DIAG_CONTAINER}:/flagos-workspace/shared/context.yaml" "${HOST_BASE}/config/context_snapshot.yaml" 2>/dev/null && \
        echo "  ✓ context_snapshot.yaml 已同步" || echo "  ⚠ context_snapshot.yaml 同步失败"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 兜底同步完成"

    # ========== 兜底：Harbor 发布（如 Claude 未执行） ==========
    CONTEXT_SNAP="${HOST_BASE}/config/context_snapshot.yaml"
    if [ -f "${CONTEXT_SNAP}" ]; then
        HARBOR_DONE=$(python3 -c "
import yaml, sys
try:
    with open('${CONTEXT_SNAP}') as f:
        ctx = yaml.safe_load(f)
    tag = ctx.get('image', {}).get('harbor_tag', '')
    model_name = ctx.get('model', {}).get('name', '')
    # 简单校验：harbor_tag 非空，且包含当前模型关键词（排除其他模型的残留 tag）
    if tag and model_name:
        # 'tiiuae/Falcon-H1-0.5B-Base' -> 'falcon-h1-0.5b-base'
        key = model_name.split('/')[-1].lower().replace('-', '').replace('_', '').replace('.', '')
        tag_norm = tag.lower().replace('-', '').replace('_', '').replace('.', '')
        if key in tag_norm:
            print('done')
            sys.exit(0)
    print('needed')
except Exception as e:
    print('needed', file=sys.stderr)
    print('needed')
" 2>/dev/null) || HARBOR_DONE="needed"

        if [ "${HARBOR_DONE}" = "needed" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 兜底发布：Claude 未完成 Harbor push，自动补执行..."
            python3 skills/flagos-release/tools/main.py --from-context "${CONTEXT_SNAP}" 2>&1 && \
                echo "  ✓ 兜底 Harbor 发布成功" || echo "  ✗ 兜底 Harbor 发布失败"
            # 重新同步 context 和 traces（main.py 可能更新了）
            docker cp "${DIAG_CONTAINER}:/flagos-workspace/shared/context.yaml" "${CONTEXT_SNAP}" 2>/dev/null
            docker cp "${DIAG_CONTAINER}:/flagos-workspace/traces/." "${HOST_BASE}/traces/" 2>/dev/null
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Harbor 发布已完成，跳过兜底"
        fi
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠ context_snapshot.yaml 不存在，无法判断 Harbor 发布状态"
    fi

    # ========== 兜底：生成性能对比文件（如缺失） ==========
    NATIVE_PERF="${HOST_BASE}/results/native_performance.json"
    FLAGOS_PERF="${HOST_BASE}/results/flagos_performance.json"
    COMPARE_CSV="${HOST_BASE}/results/performance_compare.csv"
    if [ -f "${NATIVE_PERF}" ] && [ -f "${FLAGOS_PERF}" ] && [ ! -f "${COMPARE_CSV}" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 兜底生成性能对比文件..."
        python3 skills/flagos-performance-testing/tools/performance_compare.py \
            --native "${NATIVE_PERF}" \
            --flagos-initial "${FLAGOS_PERF}" \
            --output "${COMPARE_CSV}" 2>&1 && \
            echo "  ✓ performance_compare.csv 已生成" || echo "  ⚠ 性能对比文件生成失败"
    fi
fi

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Claude Code 流程结束"
echo "日志文件:"
echo "  原始事件流: ${LOG_FILE}"
echo "  可读执行记录: ${FULL_LOG}"
echo "  流水线日志: ${PIPELINE_LOG}"
echo "  内部 debug: ${DEBUG_FILE}"
