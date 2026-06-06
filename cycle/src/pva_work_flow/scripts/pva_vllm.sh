#!/usr/bin/env bash
set -euo pipefail

# Clean vLLM runner for multi-root, multi-candidate greedy-chain steps.
# Edit the variables below, or override them from the shell:
#   ROUND=2 bash pva_vllm.sh
#   ROUND=2 REGENERATE=1 bash pva_vllm.sh
#   ROUND=2 CHAIN_ROOT_IDS="R1-04 R1-03 R1-07" bash pva_vllm.sh

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$_SCRIPT_DIR/../../.."
echo "[STAGE 1/4] Entered cycle workspace: $(pwd)"

# conda activate myenv

OUT_DIR="${OUT_DIR:-./src/sft_qwen3_14b_out}"
ROUND="${ROUND:-2}"
REGENERATE="${REGENERATE:-}"
SEED="${SEED:-7}"

VLLM_BASE_URL="${VLLM_BASE_URL:-http://localhost:8000/v1}"
VLLM_MODEL_NAME="${VLLM_MODEL_NAME:-qwen3-14b-sft}"
VLLM_TIMEOUT_S="${VLLM_TIMEOUT_S:-600}"

# Diversity-first small main experiment roots:
# R1-02=DMSO, R1-03=sodium hyaluronate, R1-04=CMC/best COF,
# R1-06=mucin, R1-07=photo/UV network.
CHAIN_ROOT_IDS="${CHAIN_ROOT_IDS:-R1-02 R1-03 R1-04 R1-06 R1-07}"
CHAIN_ROOT_ID="${CHAIN_ROOT_ID:-}"
if [ -n "$CHAIN_ROOT_ID" ]; then
  CHAIN_ROOT_IDS="$CHAIN_ROOT_ID"
fi
CHAIN_CANDIDATES="${CHAIN_CANDIDATES:-3}"
CHAIN_SELECT="${CHAIN_SELECT:-$CHAIN_CANDIDATES}"
CHAIN_ACCEPT_DELTA="${CHAIN_ACCEPT_DELTA:--1e-6}"

FORMULATION_RAG_DB="${FORMULATION_RAG_DB:-}"

CONV_COF_MAX="${CONV_COF_MAX:-0.02}"
CONV_MODULUS_MIN="${CONV_MODULUS_MIN:-1.5}"
CONV_MODULUS_MAX="${CONV_MODULUS_MAX:-2.5}"
CONV_STABLE_PROPORTION="${CONV_STABLE_PROPORTION:-0.6}"
CONV_STICK_SLIP_MAX="${CONV_STICK_SLIP_MAX:-0.2}"
CONV_COF_TREND_DELTA="${CONV_COF_TREND_DELTA:-0.005}"
CONV_COF_TREND_ROUNDS="${CONV_COF_TREND_ROUNDS:-2}"

case "$REGENERATE" in
  ""|0|false|FALSE|no|NO) REGENERATE_ACTIVE="" ;;
  *) REGENERATE_ACTIVE="1" ;;
esac

RAG_ARGS=()
if [ -n "$FORMULATION_RAG_DB" ]; then
  RAG_ARGS=(--formulation_rag_db "$FORMULATION_RAG_DB")
fi

chain_run_dir_for_root() {
  local root_id="$1"
  local root_label=""
  if [[ "$root_id" =~ ^R1-([0-9]+)$ ]]; then
    root_label="$(printf "root-%02d" "$((10#${BASH_REMATCH[1]}))")"
  elif [[ "$root_id" =~ ^root-([0-9]+)$ ]]; then
    root_label="$(printf "root-%02d" "$((10#${BASH_REMATCH[1]}))")"
  fi

  if [ -n "$root_label" ] && [ -d "$OUT_DIR/trees/$root_label" ]; then
    printf "%s" "$OUT_DIR/trees/$root_label"
  else
    printf "%s" "$OUT_DIR"
  fi
}

archive_round_outputs() {
  local round="$1"
  local run_dir="$2"
  local archive_dir="$run_dir/archive/R${round}_$(date +%Y%m%d_%H%M%S)"
  local files=("$run_dir"/R"${round}"_*)

  if [ ! -e "${files[0]}" ]; then
    echo "[REGENERATE] No R${round} files found in $run_dir"
    return 0
  fi

  mkdir -p "$archive_dir"
  mv "${files[@]}" "$archive_dir"/
  echo "[REGENERATE] Archived R${round} files to $archive_dir"
}

echo "[STAGE 2/4] Configuration"
echo "  out_dir=$OUT_DIR"
echo "  round=$ROUND"
echo "  regenerate=${REGENERATE_ACTIVE:-off}"
echo "  chain_root_ids=$CHAIN_ROOT_IDS"
echo "  chain_candidates=$CHAIN_CANDIDATES"
echo "  chain_select=$CHAIN_SELECT"
echo "  chain_accept_delta=$CHAIN_ACCEPT_DELTA"
echo "  vllm_model=$VLLM_MODEL_NAME"
echo "  rag_db=${FORMULATION_RAG_DB:-auto}"

run_one_root() {
  local root_id="$1"
  local chain_run_dir
  chain_run_dir="$(chain_run_dir_for_root "$root_id")"

  echo "============================================================"
  echo "[ROOT] $root_id"
  echo "  chain_run_dir=$chain_run_dir"

  if [ -n "$REGENERATE_ACTIVE" ]; then
    echo "[STAGE 2.5/4] Archive old R${ROUND} files for $root_id"
    archive_round_outputs "$ROUND" "$chain_run_dir"
  fi

  echo "[STAGE 3/4] Generate R${ROUND} chain candidates for $root_id"
  python -c "import sys; sys.path.insert(0, 'src'); import pva_work_flow.cli; pva_work_flow.cli.main()" \
    --engine vllm \
    --vllm_base_url "$VLLM_BASE_URL" \
    --vllm_model_name "$VLLM_MODEL_NAME" \
    --vllm_timeout_s "$VLLM_TIMEOUT_S" \
    --out_dir "$OUT_DIR" \
    --seed "$SEED" \
    --mode generate \
    --round "$ROUND" \
    --chain_search \
    --chain_root_id "$root_id" \
    --chain_accept_delta="$CHAIN_ACCEPT_DELTA" \
    --n_candidates "$CHAIN_CANDIDATES" \
    --n_select "$CHAIN_SELECT" \
    "${RAG_ARGS[@]}" \
    --conv_cof_max "$CONV_COF_MAX" \
    --conv_modulus_min "$CONV_MODULUS_MIN" \
    --conv_modulus_max "$CONV_MODULUS_MAX" \
    --conv_stable_proportion "$CONV_STABLE_PROPORTION" \
    --conv_stick_slip_max "$CONV_STICK_SLIP_MAX" \
    --conv_cof_trend_delta "$CONV_COF_TREND_DELTA" \
    --conv_cof_trend_rounds "$CONV_COF_TREND_ROUNDS"
  echo "[DONE] Generate R${ROUND} chain candidates for $root_id"

  echo "[STAGE 4/4] Prepare R${ROUND} wet-lab files for $root_id"
  python -c "import sys; sys.path.insert(0, 'src'); import pva_work_flow.cli; pva_work_flow.cli.main()" \
    --engine vllm \
    --vllm_base_url "$VLLM_BASE_URL" \
    --vllm_model_name "$VLLM_MODEL_NAME" \
    --vllm_timeout_s "$VLLM_TIMEOUT_S" \
    --out_dir "$OUT_DIR" \
    --mode prepare \
    --round "$ROUND" \
    --chain_search \
    --chain_root_id "$root_id" \
    --chain_accept_delta="$CHAIN_ACCEPT_DELTA" \
    --n_candidates "$CHAIN_CANDIDATES" \
    --n_select "$CHAIN_SELECT" \
    "${RAG_ARGS[@]}"
  echo "[DONE] Prepare R${ROUND} wet-lab files for $root_id"
}

for root_id in $CHAIN_ROOT_IDS; do
  run_one_root "$root_id"
done

echo "[DONE] pva_vllm.sh finished"
