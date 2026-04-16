#!/usr/bin/env bash
# wait_for_service.sh — 统一的服务就绪检测（指数退避）
#
# 替代手动 curl 轮询，支持指数退避和超时自动诊断。
#
# Usage:
#   ./wait_for_service.sh --port 9010 --model-name "RoboBrain2.0-7B" --timeout 300
#   ./wait_for_service.sh --port 8000 --timeout 180

set -euo pipefail

# 默认值
PORT=8000
HOST="127.0.0.1"
MODEL_NAME=""
TIMEOUT=300
LOG_PATH=""

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --model-name) MODEL_NAME="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --log-path) LOG_PATH="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

BASE_URL="http://${HOST}:${PORT}"
MODELS_URL="${BASE_URL}/v1/models"

echo "等待服务就绪..."
echo "  地址: ${BASE_URL}"
echo "  超时: ${TIMEOUT}s"
if [ -n "$MODEL_NAME" ]; then
    echo "  模型: ${MODEL_NAME}"
fi
echo ""

# 快速轮询 + 低上限退避
# 服务检测是轻量 curl，不需要 30s 那么保守的退避
# 初始 2s，上限 5s，确保服务就绪后最多 5s 内感知
INTERVAL=2
MAX_INTERVAL=5
ELAPSED=0

while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
    # 尝试请求 /v1/models
    RESPONSE=$(curl -s --connect-timeout 3 "${MODELS_URL}" 2>/dev/null || true)

    if [ -n "$RESPONSE" ]; then
        # 检查是否有模型数据
        HAS_DATA=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('data', [])
    if models:
        for m in models:
            mid = m.get('id', '')
            print(mid)
except:
    pass
" 2>/dev/null || true)

        if [ -n "$HAS_DATA" ]; then
            MODEL_ID=$(echo "$HAS_DATA" | head -1)

            # 如果指定了模型名，检查是否匹配
            if [ -n "$MODEL_NAME" ] && ! echo "$HAS_DATA" | grep -qi "$MODEL_NAME"; then
                echo "[${ELAPSED}s] 服务已响应，但模型名不匹配: 期望=${MODEL_NAME}, 实际=${MODEL_ID}"
            fi

            # 获取模型详情
            MAX_MODEL_LEN=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for m in data.get('data', []):
        mml = m.get('max_model_len', 'unknown')
        print(mml)
        break
except:
    print('unknown')
" 2>/dev/null || echo "unknown")

            echo ""
            echo "=========================================="
            echo "服务就绪！"
            echo "=========================================="
            echo "  耗时: ${ELAPSED}s"
            echo "  模型: ${MODEL_ID}"
            echo "  max_model_len: ${MAX_MODEL_LEN}"
            echo "  端点: ${BASE_URL}/v1/chat/completions"
            echo "=========================================="

            # 输出 JSON 供程序读取
            echo ""
            echo "JSON_RESULT:"
            python3 -c "
import json, sys
print(json.dumps({
    'success': True,
    'elapsed_seconds': int(sys.argv[1]),
    'model_id': sys.argv[2],
    'max_model_len': sys.argv[3],
    'endpoint': sys.argv[4],
}, indent=2))
" "${ELAPSED}" "${MODEL_ID}" "${MAX_MODEL_LEN}" "${BASE_URL}"
            exit 0
        fi
    fi

    echo "[${ELAPSED}s] 服务未就绪，${INTERVAL}s 后重试..."
    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))

    # 指数退避
    INTERVAL=$((INTERVAL * 2))
    if [ "$INTERVAL" -gt "$MAX_INTERVAL" ]; then
        INTERVAL=$MAX_INTERVAL
    fi
done

# 超时 — 自动诊断
echo ""
echo "=========================================="
echo "ERROR: 服务启动超时（${TIMEOUT}s）"
echo "=========================================="

# 写入 _last_error.json
python3 -c "
import json, os
from datetime import datetime
log_dir = '/flagos-workspace/logs' if os.path.isdir('/flagos-workspace/logs') else '/tmp'
record = {
    'tool': 'wait_for_service.sh',
    'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
    'exit_code': 1,
    'error_type': 'service_timeout',
    'error_message': '服务启动超时 (${TIMEOUT}s), 端口 ${PORT} 无响应',
    'context': {'port': ${PORT}, 'host': '${HOST}', 'timeout': ${TIMEOUT}},
}
with open(os.path.join(log_dir, '_last_error.json'), 'w') as f:
    json.dump(record, f, ensure_ascii=False, indent=2)
with open(os.path.join(log_dir, '_error_history.jsonl'), 'a') as f:
    f.write(json.dumps(record, ensure_ascii=False) + '\n')
" 2>/dev/null || true

# 检查进程是否还在
echo ""
echo "进程状态:"
ps -ef | grep -E "vllm|sglang|flagscale" | grep -v grep || echo "  无相关进程"

# 检查端口
echo ""
echo "端口状态:"
ss -tlnp | grep ":${PORT}" || echo "  端口 ${PORT} 未监听"

# 输出日志尾部
if [ -n "$LOG_PATH" ] && [ -f "$LOG_PATH" ]; then
    echo ""
    echo "最后 20 行日志:"
    tail -20 "$LOG_PATH"
else
    # 尝试自动查找日志
    LOG_FILES=$(find /flagos-workspace/logs -name "*.log" -newer /proc/1/cmdline 2>/dev/null | head -3)
    if [ -n "$LOG_FILES" ]; then
        echo ""
        echo "自动发现的日志文件:"
        for lf in $LOG_FILES; do
            echo "--- $lf (最后 10 行) ---"
            tail -10 "$lf"
        done
    fi
fi

# 输出失败 JSON
echo ""
echo "JSON_RESULT:"
python3 -c "
import json, sys
print(json.dumps({
    'success': False,
    'elapsed_seconds': int(sys.argv[1]),
    'error': 'timeout',
    'endpoint': sys.argv[2],
}, indent=2))
" "${TIMEOUT}" "${BASE_URL}"

exit 1
