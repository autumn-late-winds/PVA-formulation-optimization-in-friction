#!/usr/bin/env bash
set -euo pipefail

# Clean vLLM runner for multi-root PVA optimization steps.
# Edit the variables below, or override them from the shell:
#   ROUND=5 REGENERATE=0 bash pva_vllm.sh
#   ROUND=5 REGENERATE=1 bash pva_vllm.sh
#   ROUND=5 CHAIN_ROOT_IDS="R1-04 R1-03 R1-07" bash pva_vllm.sh
#   ROUND=5 RUN_MODE=direct bash pva_vllm.sh  # use each tree's R3 results as parent evidence

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$_SCRIPT_DIR/../../.."
echo "[STAGE 1/4] Entered cycle workspace: $(pwd)"

# conda activate myenv

OUT_DIR="${OUT_DIR:-./src/sft_qwen3_14b_out}"
ROUND="${ROUND:-6}"
REGENERATE="${REGENERATE:-0}"
SEED="${SEED:-7}"
PREPARE_HISTORY="${PREPARE_HISTORY:-light}"

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

# RUN_MODE controls parent selection:
# - chain: greedy chain search from the root id; may route back to R1 if no accepted child exists.
# - direct: run inside each trees/root-XX directory; R4 naturally uses R3 results/notes.
# - auto: use direct for R4+ and chain for R2/R3.
RUN_MODE="${RUN_MODE:-auto}"
if [ "$RUN_MODE" = "auto" ]; then
  if [ "$ROUND" -ge 4 ]; then
    EFFECTIVE_RUN_MODE="direct"
  else
    EFFECTIVE_RUN_MODE="chain"
  fi
else
  EFFECTIVE_RUN_MODE="$RUN_MODE"
fi

# Binary-midpoint local search defaults. Override from shell for multiplicative runs:
#   PVA_CONSTRAINED_STEP_STRATEGY=multiplicative \
#   PVA_CONSTRAINED_NUMERIC_DECREASE_FACTOR=0.75 \
#   PVA_CONSTRAINED_NUMERIC_INCREASE_FACTOR=1.25 bash pva_vllm.sh
export PVA_CONSTRAINED_STEP_STRATEGY="${PVA_CONSTRAINED_STEP_STRATEGY:-binary}"
export PVA_CONSTRAINED_NUMERIC_DECREASE_FACTOR="${PVA_CONSTRAINED_NUMERIC_DECREASE_FACTOR:-0.5}"
export PVA_CONSTRAINED_NUMERIC_INCREASE_FACTOR="${PVA_CONSTRAINED_NUMERIC_INCREASE_FACTOR:-2.0}"
export PVA_CONSTRAINED_FREEZE_THAW_STEP="${PVA_CONSTRAINED_FREEZE_THAW_STEP:-2}"
export PVA_CONSTRAINED_CHANGE_MAGNITUDE="${PVA_CONSTRAINED_CHANGE_MAGNITUDE:-binary_midpoint}"
export PVA_POST_SOAK_RESCUE_FACTOR="${PVA_POST_SOAK_RESCUE_FACTOR:-0.5}"

FORMULATION_RAG_DB="${FORMULATION_RAG_DB:-}"
if [ -z "$FORMULATION_RAG_DB" ] && [ -f "../数据库/formulation_optimization_cases_agent_reviewed/formulation_rag_agent_reviewed.sqlite" ]; then
  FORMULATION_RAG_DB="../数据库/formulation_optimization_cases_agent_reviewed/formulation_rag_agent_reviewed.sqlite"
fi

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

run_cli() {
  python -c "import sys; sys.path.insert(0, 'src'); import pva_work_flow.cli; pva_work_flow.cli.main()" "$@"
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

prepare_prior_history() {
  local run_dir="$1"
  local round="$2"

  case "$PREPARE_HISTORY" in
    ""|0|false|FALSE|no|NO)
      echo "[HISTORY] Skipped prior evidence refresh (PREPARE_HISTORY=$PREPARE_HISTORY)"
      return 0
      ;;
  esac

  if [ "$round" -le 1 ]; then
    echo "[HISTORY] No prior rounds before R${round}"
    return 0
  fi

  echo "[HISTORY] Refreshing memory from existing R1..R$((round - 1)) artifacts only: $run_dir"
  run_cli --out_dir "$run_dir" --build_failure_memory
  run_cli --out_dir "$run_dir" --build_rag_vector_index "${RAG_ARGS[@]}"

  local diagnose_start=$((round - 1))
  local diagnose_end=$((round - 1))
  case "$PREPARE_HISTORY" in
    full|FULL|diagnose|DIAGNOSE)
      diagnose_start=1
      ;;
    light|LIGHT|previous|PREVIOUS)
      diagnose_start=$((round - 1))
      ;;
    *)
      echo "[HISTORY] PREPARE_HISTORY=$PREPARE_HISTORY -> memory refresh only"
      return 0
      ;;
  esac

  local prev
  for ((prev = diagnose_start; prev <= diagnose_end; prev++)); do
    if [ "$prev" -le 0 ]; then
      continue
    fi
        local results="$run_dir/R${prev}_results_filled.csv"
        local candidates="$run_dir/R${prev}_candidates.json"
        local diagnosis="$run_dir/R${prev}_diagnosis.json"
    local notes="$run_dir/R${prev}_experiment_notes.json"
    local should_diagnose=""
    if [ -f "$results" ] && [ -f "$candidates" ]; then
      if [ ! -f "$diagnosis" ]; then
        should_diagnose="1"
      elif [ -f "$notes" ] && [ "$notes" -nt "$diagnosis" ]; then
        should_diagnose="1"
      elif [ "$PREPARE_HISTORY" = "full" ] || [ "$PREPARE_HISTORY" = "FULL" ] || [ "$PREPARE_HISTORY" = "diagnose" ] || [ "$PREPARE_HISTORY" = "DIAGNOSE" ]; then
        should_diagnose="1"
      fi
    fi

    if [ -n "$should_diagnose" ]; then
      if [ -f "$diagnosis" ]; then
        local diag_archive="$run_dir/archive/R${prev}_diagnosis_$(date +%Y%m%d_%H%M%S)"
        mkdir -p "$diag_archive"
        mv "$diagnosis" "$diag_archive"/
        echo "[HISTORY] R${prev}: archived stale diagnosis to $diag_archive"
      fi
          echo "[HISTORY] R${prev}: results/notes present -> diagnose"
          run_cli \
            --engine vllm \
            --vllm_base_url "$VLLM_BASE_URL" \
            --vllm_model_name "$VLLM_MODEL_NAME" \
            --vllm_timeout_s "$VLLM_TIMEOUT_S" \
            --out_dir "$run_dir" \
            --mode diagnose \
            --round "$prev" \
            "${RAG_ARGS[@]}" \
            --conv_cof_max "$CONV_COF_MAX" \
            --conv_modulus_min "$CONV_MODULUS_MIN" \
            --conv_modulus_max "$CONV_MODULUS_MAX" \
            --conv_stable_proportion "$CONV_STABLE_PROPORTION" \
            --conv_stick_slip_max "$CONV_STICK_SLIP_MAX" \
            --conv_cof_trend_delta "$CONV_COF_TREND_DELTA" \
            --conv_cof_trend_rounds "$CONV_COF_TREND_ROUNDS"
    else
      echo "[HISTORY] R${prev}: diagnosis current"
    fi
  done
}

echo "[STAGE 2/4] Configuration"
echo "  out_dir=$OUT_DIR"
echo "  round=$ROUND"
echo "  regenerate=${REGENERATE_ACTIVE:-off}"
echo "  prepare_history=$PREPARE_HISTORY"
echo "  chain_root_ids=$CHAIN_ROOT_IDS"
echo "  chain_candidates=$CHAIN_CANDIDATES"
echo "  chain_select=$CHAIN_SELECT"
echo "  chain_accept_delta=$CHAIN_ACCEPT_DELTA"
echo "  run_mode=$EFFECTIVE_RUN_MODE"
echo "  step_strategy=$PVA_CONSTRAINED_STEP_STRATEGY"
echo "  numeric_decrease_factor=$PVA_CONSTRAINED_NUMERIC_DECREASE_FACTOR"
echo "  numeric_increase_factor=$PVA_CONSTRAINED_NUMERIC_INCREASE_FACTOR"
echo "  freeze_thaw_step=$PVA_CONSTRAINED_FREEZE_THAW_STEP"
echo "  change_magnitude=$PVA_CONSTRAINED_CHANGE_MAGNITUDE"
echo "  post_soak_rescue_factor=$PVA_POST_SOAK_RESCUE_FACTOR"
echo "  vllm_model=$VLLM_MODEL_NAME"
echo "  rag_db=${FORMULATION_RAG_DB:-auto}"

python - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, "src")
try:
    from pva_work_flow.memory.formulation_rag import formulation_rag_enabled, resolve_formulation_rag_db

    db = resolve_formulation_rag_db(os.environ.get("FORMULATION_RAG_DB") or None)
    print(f"[RAG] formulation_rag_enabled={formulation_rag_enabled()}")
    print(f"[RAG] formulation_rag_db={db}")
    print(f"[RAG] formulation_rag_db_exists={db.exists()}")
    if not db.exists():
        print("[RAG] WARNING: external formulation-literature SQLite DB is missing; only project-memory RAG can be used unless FORMULATION_RAG_DB is set.")
except Exception as exc:
    print(f"[RAG] WARNING: formulation RAG preflight failed: {exc}")
PY

run_one_root() {
  local root_id="$1"
  local chain_run_dir
  chain_run_dir="$(chain_run_dir_for_root "$root_id")"

  echo "============================================================"
  echo "[ROOT] $root_id"
  echo "  chain_run_dir=$chain_run_dir"
  if [ -f "$chain_run_dir/rag_vector_index.json" ]; then
    echo "  local_vector_rag=$chain_run_dir/rag_vector_index.json"
  else
    echo "  local_vector_rag=missing (run --build_rag_vector_index if needed)"
  fi

  if [ -n "$REGENERATE_ACTIVE" ]; then
    echo "[STAGE 2.5/4] Archive old R${ROUND} files for $root_id"
    archive_round_outputs "$ROUND" "$chain_run_dir"
  fi

  local run_out_dir="$OUT_DIR"
  local mode_args=()
  if [ "$EFFECTIVE_RUN_MODE" = "direct" ]; then
    run_out_dir="$chain_run_dir"
    mode_args=()
    echo "  direct_tree_out_dir=$run_out_dir"
    if [ "$run_out_dir" = "$OUT_DIR" ]; then
      echo "[ERROR] direct mode requires an existing tree directory for $root_id under $OUT_DIR/trees" >&2
      return 1
    fi
  elif [ "$EFFECTIVE_RUN_MODE" = "chain" ]; then
    run_out_dir="$OUT_DIR"
    mode_args=(--chain_search --chain_root_id "$root_id" --chain_accept_delta="$CHAIN_ACCEPT_DELTA")
  else
    echo "[ERROR] RUN_MODE must be auto, direct, or chain; got '$RUN_MODE'" >&2
    return 1
  fi

  echo "[STAGE 2.75/4] Refresh prior R1..R$((ROUND - 1)) evidence for $root_id"
  prepare_prior_history "$run_out_dir" "$ROUND"

  echo "[STAGE 3/4] Generate R${ROUND} candidates for $root_id ($EFFECTIVE_RUN_MODE mode)"
  run_cli \
    --engine vllm \
    --vllm_base_url "$VLLM_BASE_URL" \
    --vllm_model_name "$VLLM_MODEL_NAME" \
    --vllm_timeout_s "$VLLM_TIMEOUT_S" \
    --out_dir "$run_out_dir" \
    --seed "$SEED" \
    --mode generate \
    --round "$ROUND" \
    "${mode_args[@]}" \
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
  echo "[DONE] Generate R${ROUND} candidates for $root_id"

  echo "[STAGE 4/4] Prepare R${ROUND} wet-lab files for $root_id"
  run_cli \
    --engine vllm \
    --vllm_base_url "$VLLM_BASE_URL" \
    --vllm_model_name "$VLLM_MODEL_NAME" \
    --vllm_timeout_s "$VLLM_TIMEOUT_S" \
    --out_dir "$run_out_dir" \
    --mode prepare \
    --round "$ROUND" \
    "${mode_args[@]}" \
    --n_candidates "$CHAIN_CANDIDATES" \
    --n_select "$CHAIN_SELECT" \
    "${RAG_ARGS[@]}"
  echo "[DONE] Prepare R${ROUND} wet-lab files for $root_id"
}

for root_id in $CHAIN_ROOT_IDS; do
  run_one_root "$root_id"
done

echo "[DONE] pva_vllm.sh finished"
