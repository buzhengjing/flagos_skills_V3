#!/bin/bash
# FlagOS 全自动迁移流程 — 一键启动脚本（V1+V2+V3 算子调优）
#
# 用法:
#   完整流程:
#   bash prompts/run_pipeline.sh <容器名或镜像地址> <模型名> <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]
#
#   简化验证流程 (plugin+flaggems+flagtree):
#   bash prompts/run_pipeline.sh --mode verify <容器名或镜像地址> <模型名> <模型路径> [GITHUB_TOKEN] [--verbose]
#
# 自动识别：第一参数若为已有容器则走容器模式，否则视为镜像地址
# 模型路径：仅需模型名，自动搜索宿主机路径；未找到则容器内自动下载
#
# 示例:
#   bash prompts/run_pipeline.sh qwen3-8b-test Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass
#   bash prompts/run_pipeline.sh harbor.baai.ac.cn/flagrelease/qwen3:latest Qwen3-8B ms_xxx hf_xxx ghp_xxx harbor_user harbor_pass
#   bash prompts/run_pipeline.sh --mode verify qwen3-8b-test Qwen3-8B /data/models/Qwen3-8B
#   bash prompts/run_pipeline.sh --mode verify qwen3-8b-test Qwen3-8B /data/models/Qwen3-8B ghp_xxx
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
WORKFLOW_MODE="full"  # full | verify

# 检查是否为 verify 模式
if [[ "${1:-}" == "--mode" ]] && [[ "${2:-}" == "verify" ]]; then
    WORKFLOW_MODE="verify"
    shift 2  # 移除 --mode verify
    # verify 模式参数：<容器名或镜像> <模型名> <模型路径> [GITHUB_TOKEN] [--verbose]
    if [ $# -lt 3 ]; then
        echo "用法: $0 --mode verify <容器名或镜像地址> <模型名> <模型路径> [GITHUB_TOKEN] [--verbose]"
        echo ""
        echo "简化验证流程：仅验证 plugin+flaggems+flagtree 环境能否正常启动服务并完成推理"
        echo ""
        echo "参数:"
        echo "  <容器名或镜像地址>  已有容器名或镜像地址"
        echo "  <模型名>            模型标识名（如 Qwen3-8B）"
        echo "  <模型路径>          模型权重路径（宿主机或容器内绝对路径）"
        echo "  [GITHUB_TOKEN]      GitHub Token（可选，用于 issue 自动提交）"
        echo ""
        echo "示例:"
        echo "  $0 --mode verify qwen3-8b-test Qwen3-8B /data/models/Qwen3-8B"
        echo "  $0 --mode verify qwen3-8b-test Qwen3-8B /data/models/Qwen3-8B ghp_xxx"
        echo "  $0 --mode verify harbor.baai.ac.cn/flagrelease/qwen3:latest Qwen3-8B /data/models/Qwen3-8B ghp_xxx"
        exit 1
    fi
    TARGET="$1"
    MODEL="$2"
    MODEL_PATH="$3"
    CONTAINER_MODEL_PATH="${MODEL_PATH}"
    MODEL_FOUND_ON_HOST=true  # verify 模式用户已明确提供路径
    export GITHUB_TOKEN="${4:-}"
    # verify 模式不需要其他 token
    export MODELSCOPE_TOKEN=""
    export HF_TOKEN=""
    export HARBOR_USER=""
    export HARBOR_PASSWORD=""
    if [[ "${5:-}" == "--verbose" ]] || [[ "${4:-}" == "--verbose" ]]; then
        FILTER_FLAGS="--verbose"
    fi

    # 自动识别容器/镜像
    if [[ "$TARGET" == *":"* ]] || [[ "$TARGET" == *"/"* ]]; then
        IMAGE_MODE=true
        IMAGE="$TARGET"
    elif docker inspect --type=container "$TARGET" &>/dev/null; then
        IMAGE_MODE=false
        CONTAINER="$TARGET"
    else
        IMAGE_MODE=true
        IMAGE="$TARGET"
    fi
elif [[ "${1:-}" == "--image" ]]; then
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

    MODEL_FOUND_ON_HOST=false
    if [ -n "$MODEL_PATH" ]; then
        echo "  ✓ 找到: ${MODEL_PATH}"
        MODEL_FOUND_ON_HOST=true
    else
        MODEL_SHORT=$(echo "${MODEL}" | sed 's|.*/||')
        MODEL_PATH="/data/models/${MODEL_SHORT}"
        mkdir -p "${MODEL_PATH}"
        echo "  ⚠ 宿主机未找到模型，预创建挂载目录: ${MODEL_PATH}"
    fi
    CONTAINER_MODEL_PATH="${MODEL_PATH}"
fi

# ========== Banner ==========
echo "============================================================"
if [ "$WORKFLOW_MODE" = "verify" ]; then
    echo "  FlagOS 简化验证流程 (plugin+flaggems+flagtree)"
else
    echo "  FlagOS 全自动迁移流程"
fi
echo "============================================================"
if $IMAGE_MODE; then
    echo "  目标: ${IMAGE} (镜像，自动识别)"
else
    echo "  目标: ${CONTAINER} (容器，自动识别)"
fi
echo "  模型: ${MODEL}"
if [ "$WORKFLOW_MODE" = "verify" ]; then
    echo "  模型路径: ${MODEL_PATH} (用户指定)"
elif $IMAGE_MODE; then
    if $MODEL_FOUND_ON_HOST; then
        echo "  模型路径: ${MODEL_PATH} (自动检测)"
    else
        echo "  模型路径: ${MODEL_PATH} (预创建，容器内下载)"
    fi
fi
if [ "$WORKFLOW_MODE" = "verify" ]; then
    echo "  模式: 简化验证（①容器准备 → ②环境检测 → ③启服务+curl验证）"
else
    echo "  模式: V1 + V2 + V3（不达标时自动算子优化）"
fi
echo "  权限: --permission-mode auto + settings.local.json allowlist (78 rules)"
echo "============================================================"
echo ""

# ========== 构造 Prompt ==========
# 公共部分：tokens、执行模式、进度输出要求、步骤②-⑥
COMMON_TOKENS=$(cat <<TOKENS_EOF

**容器内 Token**（已通过 setup_workspace.sh 写入容器 /flagos-workspace/.env，脚本自动加载；docker exec -e 仍建议保留作为双保险）：
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
   - skills/flagos-operator-replacement/SKILL.md
   - skills/flagos-release/SKILL.md
PLAN_EOF
)

COMMON_PLAN_STEPS=$(cat <<PLAN_STEPS_EOF
2. 生成 execution_plan.md，写入 /data/flagos-workspace/${MODEL}/config/execution_plan.md
   - 包含每步的完整命令（变量已替换为实际值：容器名、模型名、端口等）
   - 包含每步的输入/输出文件路径
   - 包含每步的 context.yaml 读写字段清单
   - 包含每步的校验检查项
3. 每个步骤开始前，Read execution_plan.md 中对应段落刷新记忆
4. 每个步骤开始前，通过 docker exec 读取容器内 /flagos-workspace/shared/context.yaml 获取最新状态

全自动执行，步骤间无需询问。
**算子调优**：精度偏差>5%或性能ratio<80%时，按 CLAUDE.md 步骤⑦⑧自动触发算子调优。
**进度输出**：步骤开始/完成时输出 [步骤X] 标记，关键命令后输出 ✓/✗ 结果摘要。按 CLAUDE.md 流水线执行日志规范输出。
PLAN_STEPS_EOF
)


# ========== Verify 模式 Prompt 构造 ==========
if [ "$WORKFLOW_MODE" = "verify" ]; then

VERIFY_PLAN_FIRST=$(cat <<'VPLAN_EOF'

**执行模式：计划优先（Plan-First）**

在执行任何操作之前，先完成规划阶段：
1. 依次读取以下 SKILL.md 文件，提取每步的关键命令、参数、文件路径：
   - skills/flagos-container-preparation/SKILL.md
   - skills/flagos-pre-service-inspection/SKILL.md
   - skills/flagos-service-startup/SKILL.md
   - skills/flagos-issue-reporter/SKILL.md
VPLAN_EOF
)

VERIFY_PLAN_STEPS=$(cat <<VPLAN_STEPS_EOF
2. 生成 execution_plan.md，写入 /data/flagos-workspace/${MODEL}/config/execution_plan.md
   - 包含每步的完整命令（变量已替换为实际值：容器名、模型名、端口等）
   - 包含每步的输入/输出文件路径
   - 包含每步的 context.yaml 读写字段清单
   - 包含每步的校验检查项
3. 每个步骤开始前，Read execution_plan.md 中对应段落刷新记忆
4. 每个步骤开始前，通过 docker exec 读取容器内 /flagos-workspace/shared/context.yaml 获取最新状态

全自动执行，步骤间无需询问。
**简化验证流程**：仅执行 ①容器准备 → ②环境检测 → ③启服务+curl验证，不执行精度评测、性能评测、算子调优、发布。
**任何步骤失败都通过 issue_reporter.py 生成 issue 文件**（容器准备失败除外，因为容器不可用无法运行 issue_reporter）。
**进度输出**：步骤开始/完成时输出 [步骤X] 标记，关键命令后输出 ✓/✗ 结果摘要。按 CLAUDE.md 简化验证流程规范输出。
VPLAN_STEPS_EOF
)

    if $IMAGE_MODE; then
        VERIFY_STEP1=$(cat <<VSTEP1_EOF
① 容器准备（从镜像创建）：
   - 模型路径（用户指定）: ${MODEL_PATH}
   - 检测 GPU 厂商（nvidia-smi / npu-smi 等），选择 SKILL.md 中对应的 docker run 模板
   - **NVIDIA 模板（严格执行，仅替换变量值，禁止增删参数）**：
     docker run -itd --name=\${CONTAINER_NAME} --gpus=all --network=host -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} -v /data/flagos-workspace:/flagos-workspace ${IMAGE}
   - **降级策略**：模板失败 → 检查变量值修正后重试 → 仍失败则 docker inspect 借鉴已有容器挂载配置重试一次 → 仍失败则终止
   - 容器名自动生成为 <model_short_name>_flagos（如 Qwen3-8B_flagos）
   - 如同名容器已存在，追加时间戳：<model_short_name>_flagos_<MMDD_HHMM>
   - 镜像模式下禁止复用已有容器，必须 docker run 新建
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh \${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml：
     model.container_path=${CONTAINER_MODEL_PATH}, model.local_path=${MODEL_PATH}, model.name=${MODEL}
     entry.type=new_container, image.name=${IMAGE}, workflow.mode=verify
   - 写入 traces/01_container_preparation.json
VSTEP1_EOF
        )
        PROMPT_VERIFY="镜像: ${IMAGE}，模型名: ${MODEL}

**容器内 Token**（已通过 setup_workspace.sh 写入容器 /flagos-workspace/.env，脚本自动加载）：
  GITHUB_TOKEN=${GITHUB_TOKEN}
${VERIFY_PLAN_FIRST}
${VERIFY_PLAN_STEPS}

${VERIFY_STEP1}

② 环境检测：按 CLAUDE.md 简化验证流程执行。确认 env_type=vllm_plugin_flaggems 且 has_flagtree=true。
   环境不符合预期 → 调用 issue_reporter.py full --type flagtree-error 生成 issue 文件 → 终止流程。

③ 启服务 + curl 验证：按 CLAUDE.md 简化验证流程执行。
   3a. default 模式启动服务（start_service.sh + wait_for_service.sh）
   3b. curl /v1/models 健康检查
   3c. curl /v1/chat/completions 推理验证
   任何子步骤失败 → 调用 issue_reporter.py full --type operator-crash 生成 issue 文件。

GITHUB_TOKEN=${GITHUB_TOKEN}（issue 生成时通过 docker exec -e 传入）。
完成后确保 context_snapshot.yaml 已同步到宿主机 /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml。
全部通过后输出简化验证报告（按 CLAUDE.md 简化验证流程的报告格式）。"
    else
        VERIFY_STEP1=$(cat <<VSTEP1_EOF
① 容器准备：
   - 验证容器 ${CONTAINER} 运行状态（docker inspect + docker start）
   - 模型路径（用户指定）: ${MODEL_PATH}
     直接使用此路径，不搜索不下载。写入容器内 /flagos-workspace/shared/context.yaml 的 model.container_path
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh ${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml：
     model.container_path=${MODEL_PATH}, model.name=${MODEL}, workflow.mode=verify
   - 写入 traces/01_container_preparation.json
VSTEP1_EOF
        )
        PROMPT_VERIFY="容器名: ${CONTAINER}，模型名: ${MODEL}

**容器内 Token**（已通过 setup_workspace.sh 写入容器 /flagos-workspace/.env，脚本自动加载）：
  GITHUB_TOKEN=${GITHUB_TOKEN}
${VERIFY_PLAN_FIRST}
${VERIFY_PLAN_STEPS}

${VERIFY_STEP1}

② 环境检测：按 CLAUDE.md 简化验证流程执行。确认 env_type=vllm_plugin_flaggems 且 has_flagtree=true。
   环境不符合预期 → 调用 issue_reporter.py full --type flagtree-error 生成 issue 文件 → 终止流程。

③ 启服务 + curl 验证：按 CLAUDE.md 简化验证流程执行。
   3a. default 模式启动服务（start_service.sh + wait_for_service.sh）
   3b. curl /v1/models 健康检查
   3c. curl /v1/chat/completions 推理验证
   任何子步骤失败 → 调用 issue_reporter.py full --type operator-crash 生成 issue 文件。

GITHUB_TOKEN=${GITHUB_TOKEN}（issue 生成时通过 docker exec -e 传入）。
完成后确保 context_snapshot.yaml 已同步到宿主机 /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml。
全部通过后输出简化验证报告（按 CLAUDE.md 简化验证流程的报告格式）。"
    fi
fi

# ========== 根据模式构造步骤① ==========
if $IMAGE_MODE; then
    if $MODEL_FOUND_ON_HOST; then
        MODEL_NOTE="宿主机模型路径已自动检测: ${MODEL_PATH}"
        DOWNLOAD_NOTE=""
    else
        MODEL_NOTE="宿主机未找到模型 ${MODEL}，已预创建挂载目录: ${MODEL_PATH}"
        DOWNLOAD_NOTE="
   - 容器创建后，在容器内搜索+下载模型（下载到已挂载的 ${CONTAINER_MODEL_PATH}）：
     python3 skills/flagos-container-preparation/tools/check_model_local.py --model \"${MODEL}\" --mode container --container \${CONTAINER} --container-model-path ${CONTAINER_MODEL_PATH} --output-json
     从输出 JSON 中提取 final_container_path 和 final_host_path，记录到容器内 /flagos-workspace/shared/context.yaml"
    fi
    STEP1=$(cat <<STEP1_EOF
① 容器准备（从镜像创建）：
   - ${MODEL_NOTE}
   - 检测 GPU 厂商（nvidia-smi / npu-smi 等），选择 SKILL.md 中对应的 docker run 模板
   - **NVIDIA 模板（严格执行，仅替换变量值，禁止增删参数）**：
     docker run -itd --name=\${CONTAINER_NAME} --gpus=all --network=host -v ${MODEL_PATH}:${CONTAINER_MODEL_PATH} -v /data/flagos-workspace:/flagos-workspace ${IMAGE}
   - **降级策略**：模板失败 → 检查变量值修正后重试 → 仍失败则 docker inspect 借鉴已有容器挂载配置重试一次 → 仍失败则终止
   - 容器名自动生成为 <model_short_name>_flagos（如 Qwen3-8B_flagos）
   - 如同名容器已存在，追加时间戳：<model_short_name>_flagos_<MMDD_HHMM>
   - 镜像模式下禁止复用已有容器，必须 docker run 新建${DOWNLOAD_NOTE}
   - bash skills/flagos-container-preparation/tools/setup_workspace.sh \${CONTAINER} ${MODEL} 部署工具脚本
   - 写入容器内 /flagos-workspace/shared/context.yaml（entry.type=new_container, image.name=${IMAGE}）+ traces/01_container_preparation.json
STEP1_EOF
    )
    PROMPT_SEG1="镜像: ${IMAGE}，模型名: ${MODEL}
${COMMON_TOKENS}
${COMMON_PLAN_FIRST}
${COMMON_PLAN_STEPS}

${STEP1}

步骤②③ 按 CLAUDE.md 工作流定义执行。GITHUB_TOKEN=${GITHUB_TOKEN}（issue 提交时通过 docker exec -e 传入）。
完成步骤③后，确保 context_snapshot.yaml 已同步到宿主机 /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml。"
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
    PROMPT_SEG1="容器名: ${CONTAINER}，模型名: ${MODEL}
${COMMON_TOKENS}
${COMMON_PLAN_FIRST}
${COMMON_PLAN_STEPS}

${STEP1}

步骤②③ 按 CLAUDE.md 工作流定义执行。GITHUB_TOKEN=${GITHUB_TOKEN}（issue 提交时通过 docker exec -e 传入）。
完成步骤③后，确保 context_snapshot.yaml 已同步到宿主机 /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml。"
fi

# ========== 部署权限白名单 ==========
[ -f .claude/settings.local.json ] || (mkdir -p .claude && cp settings.local.json .claude/settings.local.json)

# ========== 动态注入模型特定权限 ==========
# auto mode 可能被降级为 default mode（mco-4 等非官方模型 ID），
# default mode 下通配符规则对链式命令不生效，需要精确匹配的模型特定规则
python3 -c "
import json, sys
model = sys.argv[1]
with open('.claude/settings.local.json') as f:
    cfg = json.load(f)
rules = cfg.setdefault('permissions', {}).setdefault('allow', [])
# 模型特定的目录操作权限
for d in ['logs', 'config', 'results', 'traces']:
    rule = f'Bash(mkdir -p /data/flagos-workspace/{model}/{d})'
    if rule not in rules:
        rules.append(rule)
# 模型特定的文件读取权限
for rule in [
    f'Read(//data/flagos-workspace/{model}/**)',
    f'Bash(cat /data/flagos-workspace/{model}/*)',
    f'Bash(find /data/flagos-workspace/{model}/*)',
    f'Bash(tail /data/flagos-workspace/{model}/*)',
]:
    if rule not in rules:
        rules.append(rule)
with open('.claude/settings.local.json', 'w') as f:
    json.dump(cfg, f, indent=2)
" "${MODEL}"
echo "  ✓ 已注入 ${MODEL} 模型特定权限规则"

# ========== 启动 Claude Code ==========
if [ "$WORKFLOW_MODE" = "verify" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 Claude Code 简化验证流程..."
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 Claude Code 全自动流程..."
fi
echo ""

# ========== 宿主机历史数据全量归档 ==========
# 在创建任何新文件之前，统一归档上一轮的全部产出（results/traces/logs/config）
# setup_workspace.sh 中的宿主机归档作为独立调用时的兜底
HOST_BASE="/data/flagos-workspace/${MODEL}"
if [ -d "${HOST_BASE}" ]; then
    HOST_HAS_HISTORY=0
    for d in results traces logs config; do
        if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
            HOST_HAS_HISTORY=1; break
        fi
    done
    if [ "${HOST_HAS_HISTORY}" = "1" ]; then
        ARCHIVE_TS="$(date +%Y%m%d_%H%M%S)"
        HOST_ARCHIVE="${HOST_BASE}/archive/${ARCHIVE_TS}"
        mkdir -p "${HOST_ARCHIVE}"
        for d in results traces logs config; do
            if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
                mv "${HOST_BASE}/${d}" "${HOST_ARCHIVE}/${d}"
            fi
        done
        echo "  宿主机历史数据已归档到: ${HOST_ARCHIVE}/"
    fi
fi

for d in logs config results traces; do
    mkdir -p "/data/flagos-workspace/${MODEL}/${d}"
done

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

# ===== 段间状态传递函数 =====
read_context() {
    local MODEL_ARG="$1"
    local CTX="/data/flagos-workspace/${MODEL_ARG}/config/context_snapshot.yaml"
    if [ ! -f "${CTX}" ]; then
        echo "ERROR: context_snapshot.yaml 不存在，前段可能未完成" >&2
        return 1
    fi
    # 输出 CONTAINER_NAME|ENV_TYPE|LAST_COMPLETED_STEP
    python3 -c "
import yaml
with open('${CTX}') as f:
    ctx = yaml.safe_load(f)
ctr = ctx.get('container',{}).get('name','')
env = ctx.get('environment',{}).get('env_type','')
ledger = ctx.get('workflow_ledger',{}).get('steps',[])
last = ''
for s in ledger:
    if s.get('status') == 'success':
        last = s.get('step','')
print(f'{ctr}|{env}|{last}')
" 2>/dev/null
}

if [ "$WORKFLOW_MODE" = "verify" ]; then
# ===== Verify 模式：单段执行 ①②③ =====
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 简化验证流程: 容器准备+环境检测+启服务+curl验证 ====="
claude -p "${PROMPT_VERIFY}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.verify" \
    --max-turns 60 \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" > "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" ${FILTER_FLAGS} || true

else
# ===== Full 模式：段1 ①②③ (容器准备 + 环境检测 + 服务启动) =====
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 段1: 容器准备+环境检测+服务启动 ====="
claude -p "${PROMPT_SEG1}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg1" \
    --max-turns 60 \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" > "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" ${FILTER_FLAGS} || true

# 段间检查
echo ""
echo "[段1完成] 检查状态..."
CTX_INFO=$(read_context "${MODEL}") || { echo "错误：段1未产出 context_snapshot.yaml，终止"; exit 1; }
SEG_CTR=$(echo "$CTX_INFO" | cut -d'|' -f1)
SEG_ENV=$(echo "$CTX_INFO" | cut -d'|' -f2)
SEG_LAST=$(echo "$CTX_INFO" | cut -d'|' -f3)

if [ -z "$SEG_CTR" ]; then
    echo "错误：段1未产出容器名，终止"
    exit 1
fi

echo "  容器名: ${SEG_CTR}"
echo "  环境类型: ${SEG_ENV}"
echo "  最后完成步骤: ${SEG_LAST}"
echo ""

# ===== 段2: ④⑦⑤⑧ (精度评测 + 精度调优 + 性能评测 + 性能调优) =====
PROMPT_SEG2="容器名: ${SEG_CTR}，模型名: ${MODEL}，env_type: ${SEG_ENV}
${COMMON_TOKENS}

按 CLAUDE.md 工作流定义执行步骤④精度评测、步骤⑦精度算子调优（如需）、步骤⑤性能评测、步骤⑧性能算子调优（如需）。

**执行前**：
1. 读取容器内 /flagos-workspace/shared/context.yaml 确认当前状态
2. 读取容器内 /flagos-workspace/config/execution_plan.md 中步骤④⑦⑤⑧段落
3. 读取 skills/flagos-operator-replacement/SKILL.md 了解算子调优工具用法

**算子调优**：
- 步骤④完成后如 accuracy_ok=false → 立即执行步骤⑦（⑦完成后再进入⑤）
- 步骤⑤完成后如 performance_ok=false → 执行步骤⑧（elimination 逐删策略）
- 调优后产出 V3 结果（flagos_optimized.json），更新 context.yaml

**进度输出**：步骤开始/完成时输出 [步骤X] 标记，关键命令后输出 ✓/✗ 结果摘要。

GITHUB_TOKEN=${GITHUB_TOKEN}（issue 提交时通过 docker exec -e 传入）。
完成后确保 context_snapshot.yaml 已同步到宿主机 /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml。"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 段2: 精度评测+精度调优+性能评测+性能调优 ====="
claude -p "${PROMPT_SEG2}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg2" \
    --max-turns 150 \
    2>&1 | tee -a "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --start-step 4 ${FILTER_FLAGS} || true

# 段间检查
echo ""
echo "[段2完成] 检查状态..."
CTX_INFO=$(read_context "${MODEL}") || { echo "错误：段2未更新 context_snapshot.yaml"; }
SEG_CTR=$(echo "$CTX_INFO" | cut -d'|' -f1)
echo "  容器名: ${SEG_CTR}"
echo ""

# ===== 段3: ⑥ (打包发布) =====
PROMPT_SEG3="容器名: ${SEG_CTR}，模型名: ${MODEL}
${COMMON_TOKENS}

按 CLAUDE.md 工作流定义执行步骤⑥自动发布。

**执行前**：读取容器内 /flagos-workspace/shared/context.yaml 确认 workflow 状态。
如果 context.yaml 显示步骤④或⑤未完成（status 非 success），将对应的 workflow.accuracy_ok 或 workflow.performance_ok 标记为 false，然后继续发布（私有）。
**进度输出**：步骤开始/完成时输出 [步骤⑥] 标记，关键命令后输出 ✓/✗ 结果摘要。

发布工具: python3 skills/flagos-release/tools/main.py --from-context /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml
完成后将最终 context 回传到宿主机 /data/flagos-workspace/${MODEL}/config/context_final.yaml。

全流程结束后输出完整的 FlagOS 迁移报告（含精度、性能、发布信息、耗时统计、问题记录摘要）。"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 段3: 打包发布 ====="
claude -p "${PROMPT_SEG3}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg3" \
    --max-turns 40 \
    2>&1 | tee -a "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --start-step 6 ${FILTER_FLAGS} || true

fi  # end of full/verify mode branch

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

    if [ "$WORKFLOW_MODE" = "full" ]; then
    # ========== 兜底：Harbor 发布（如 Claude 未执行） ==========
    CONTEXT_SNAP="${HOST_BASE}/config/context_snapshot.yaml"
    if [ -f "${CONTEXT_SNAP}" ]; then
        HARBOR_DONE=$(python3 -c "
import yaml, sys
try:
    with open('${CONTEXT_SNAP}') as f:
        ctx = yaml.safe_load(f)
    tag = ctx.get('image', {}).get('registry_url', '') or ctx.get('image', {}).get('harbor_tag', '')
    model_name = ctx.get('model', {}).get('name', '')
    # 简单校验：registry_url 非空，且包含当前模型关键词（排除其他模型的残留 tag）
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

    # ========== 兜底：V3 性能对比（如有 flagos_optimized.json） ==========
    OPTIMIZED_PERF="${HOST_BASE}/results/flagos_optimized.json"
    COMPARE_V3_CSV="${HOST_BASE}/results/performance_compare_v3.csv"
    if [ -f "${NATIVE_PERF}" ] && [ -f "${OPTIMIZED_PERF}" ] && [ ! -f "${COMPARE_V3_CSV}" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 兜底生成 V3 性能对比文件..."
        python3 skills/flagos-performance-testing/tools/performance_compare.py \
            --native "${NATIVE_PERF}" \
            --flagos-full "${FLAGOS_PERF}" \
            --flagos-optimized "${OPTIMIZED_PERF}" \
            --output "${COMPARE_V3_CSV}" 2>&1 && \
            echo "  ✓ performance_compare_v3.csv 已生成" || echo "  ⚠ V3 性能对比文件生成失败"
    fi
    fi  # end of full-mode-only fallbacks
fi

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Claude Code 流程结束"
echo "日志文件:"
echo "  原始事件流: ${LOG_FILE}"
echo "  可读执行记录: ${FULL_LOG}"
echo "  流水线日志: ${PIPELINE_LOG}"
echo "  内部 debug: ${DEBUG_FILE}"
