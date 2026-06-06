#!/usr/bin/env bash
set -e

API_PORT=8000 API_MODEL_NAME=14B_1207_merge llamafactory-cli api \
    --model_name_or_path /root/autodl-tmp/llama_friction/models/Qwen3-14B_1207_1w_en_merge
    --template qwen \
    --infer_backend vllm \
    --vllm_enforce_eager