#!/bin/bash
# start_service.sh — 从 context.yaml 读取配置并启动 vllm/sglang 服务
#
# 供 operator_search.py 的 --service-startup-cmd 调用。
# 在容器内执行，读取 /flagos-workspace/shared/context.yaml 获取启动参数。
#
# 用法:
#   bash /flagos-workspace/scripts/start_service.sh
#   bash /flagos-workspace/scripts/start_service.sh --mode flagos
#   bash /flagos-workspace/scripts/start_service.sh --mode native

set -euo pipefail

CONTEXT_YAML="/flagos-workspace/shared/context.yaml"
MODE="${1:-}"
if [ "$MODE" = "--mode" ]; then
    MODE="${2:-flagos}"
else
    MODE="flagos"
fi

# 从 context.yaml 读取启动参数
read_context() {
    PATH=/opt/conda/bin:$PATH python3 -c "
import yaml, json, sys
with open('${CONTEXT_YAML}') as f:
    ctx = yaml.safe_load(f)

model_path = ctx.get('model', {}).get('container_path', '')
model_name = ctx.get('model', {}).get('name', '').split('/')[-1]
port = ctx.get('service', {}).get('port', 8000)
tp_size = ctx.get('runtime', {}).get('tp_size', 0)
gpu_count = ctx.get('runtime', {}).get('gpu_count', ctx.get('gpu', {}).get('count', 0))
max_model_len = ctx.get('service', {}).get('max_model_len', 8192)
framework = ctx.get('runtime', {}).get('framework', 'vllm')
cuda_visible = ctx.get('runtime', {}).get('cuda_visible_devices', '')
thinking = ctx.get('runtime', {}).get('thinking_model', False)

# TP fallback: 如果为 0，使用 GPU 数量
if tp_size <= 0:
    tp_size = gpu_count if gpu_count > 0 else 1

print(json.dumps({
    'model_path': model_path,
    'model_name': model_name,
    'port': port,
    'tp_size': tp_size,
    'max_model_len': max_model_len,
    'framework': framework,
    'cuda_visible': cuda_visible,
    'thinking': thinking,
}))
"
}

CONFIG_JSON=$(read_context)

MODEL_PATH=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['model_path'])")
MODEL_NAME=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['model_name'])")
PORT=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['port'])")
TP_SIZE=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tp_size'])")
MAX_MODEL_LEN=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['max_model_len'])")
FRAMEWORK=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['framework'])")
CUDA_VISIBLE=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['cuda_visible'])")
THINKING=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['thinking'])")

if [ -z "$MODEL_PATH" ]; then
    echo "ERROR: model.container_path 为空，无法启动服务" >&2
    exit 1
fi

# 设置 CUDA_VISIBLE_DEVICES（如有）
if [ -n "$CUDA_VISIBLE" ]; then
    export CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE"
fi

# 确保 conda 环境在 PATH 中
export PATH=/opt/conda/bin:$PATH

LOG_FILE="/flagos-workspace/logs/startup_${MODE}.log"

# 构建启动命令
if [ "$FRAMEWORK" = "vllm" ]; then
    CMD="vllm serve ${MODEL_PATH} \
        --host 0.0.0.0 \
        --port ${PORT} \
        --served-model-name ${MODEL_NAME} \
        --tensor-parallel-size ${TP_SIZE} \
        --max-model-len ${MAX_MODEL_LEN} \
        --trust-remote-code"

    # Thinking model 添加 reasoning parser
    if [ "$THINKING" = "True" ]; then
        # 根据模型名推断 parser
        MODEL_LOWER=$(echo "$MODEL_NAME" | tr '[:upper:]' '[:lower:]')
        if echo "$MODEL_LOWER" | grep -qE 'qwen3|qwq'; then
            CMD="$CMD --reasoning-parser qwen3"
        elif echo "$MODEL_LOWER" | grep -qE 'deepseek'; then
            CMD="$CMD --reasoning-parser deepseek_r1"
        fi
    fi
else
    # sglang
    CMD="python3 -m sglang.launch_server \
        --model-path ${MODEL_PATH} \
        --host 0.0.0.0 \
        --port ${PORT} \
        --tp ${TP_SIZE} \
        --trust-remote-code"
fi

echo "[start_service.sh] mode=${MODE}, framework=${FRAMEWORK}, port=${PORT}, tp=${TP_SIZE}"
echo "[start_service.sh] CMD: ${CMD}"

# 后台启动，日志写入文件
nohup bash -c "cd /flagos-workspace && ${CMD}" > "${LOG_FILE}" 2>&1 &
echo "[start_service.sh] PID=$!, log=${LOG_FILE}"
