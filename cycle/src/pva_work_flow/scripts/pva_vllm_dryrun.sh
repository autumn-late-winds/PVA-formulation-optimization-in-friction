#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# DRY-RUN for pva_vllm.sh — validates paths, args, and imports
# without touching vLLM or writing output files.
# ============================================================

_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$_SCRIPT_DIR/../../.."
echo "============================================================"
echo " DRY-RUN: pva_vllm.sh path & argument validation"
echo "============================================================"
echo ""
echo "[STEP 1/5] Workspace & script location"
echo "  script_dir = $_SCRIPT_DIR"
echo "  workspace  = $(pwd)"
echo ""

# --- variable defaults (mirrors pva_vllm.sh) ---
OUT_DIR="${OUT_DIR:-./src/sft_qwen3_14b_out}"
ROUND="${ROUND:-2}"
REGENERATE="${REGENERATE:-}"
SEED="${SEED:-7}"

VLLM_BASE_URL="${VLLM_BASE_URL:-http://localhost:8000/v1}"
VLLM_MODEL_NAME="${VLLM_MODEL_NAME:-qwen3-14b-sft}"
VLLM_TIMEOUT_S="${VLLM_TIMEOUT_S:-600}"

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

echo "[STEP 2/5] Resolved configuration"
echo "  out_dir            = $OUT_DIR"
echo "  round              = $ROUND"
echo "  regenerate         = ${REGENERATE_ACTIVE:-off}"
echo "  seed               = $SEED"
echo "  chain_root_ids     = $CHAIN_ROOT_IDS"
echo "  chain_candidates   = $CHAIN_CANDIDATES"
echo "  chain_select       = $CHAIN_SELECT"
echo "  chain_accept_delta = $CHAIN_ACCEPT_DELTA"
echo "  vllm_base_url      = $VLLM_BASE_URL"
echo "  vllm_model_name    = $VLLM_MODEL_NAME"
echo "  vllm_timeout_s     = $VLLM_TIMEOUT_S"
echo "  rag_db             = ${FORMULATION_RAG_DB:-auto}"
echo "  conv_cof_max       = $CONV_COF_MAX"
echo "  conv_modulus_min   = $CONV_MODULUS_MIN"
echo "  conv_modulus_max   = $CONV_MODULUS_MAX"
echo "  conv_stable_prop   = $CONV_STABLE_PROPORTION"
echo "  conv_stick_slip    = $CONV_STICK_SLIP_MAX"
echo "  conv_cof_trend_d   = $CONV_COF_TREND_DELTA"
echo "  conv_cof_trend_r   = $CONV_COF_TREND_ROUNDS"
echo ""

# --- chain_run_dir_for_root (mirrors pva_vllm.sh) ---
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

echo "[STEP 3/5] Root-ID → run-directory resolution"
ALL_OK=true
for root_id in $CHAIN_ROOT_IDS; do
  run_dir="$(chain_run_dir_for_root "$root_id")"
  if [ -d "$run_dir" ]; then
    status="OK"
    # Count existing ROUND files for archive preview
    r_count=$(find "$run_dir" -maxdepth 1 -name "R${ROUND}_*" 2>/dev/null | wc -l)
    extra="(${r_count} R${ROUND}_* files present)"
  else
    status="MISSING"
    ALL_OK=false
    extra=""
  fi
  printf "  %-8s → %-50s [%s] %s\n" "$root_id" "$run_dir" "$status" "$extra"
done

# Also check the OUT_DIR itself for the fallback case
echo ""
echo "  Fallback OUT_DIR: $OUT_DIR"
if [ -d "$OUT_DIR" ]; then
  echo "    [OK] exists"
else
  echo "    [MISSING] does not exist"
  ALL_OK=false
fi
echo ""

# --- Check required Python module ---
echo "[STEP 4/5] Python import check"
PYTHON_CHECK=$(python -c "
import sys
sys.path.insert(0, 'src')
try:
    import pva_work_flow.cli
    print('OK: pva_work_flow.cli imported successfully')
except Exception as e:
    print(f'FAIL: {e}')
" 2>&1)
echo "  $PYTHON_CHECK"

# --- Print the actual CLI command that would run (for one root) ---
echo ""
echo "[STEP 5/5] Sample CLI invocation (first root only)"
FIRST_ROOT="${CHAIN_ROOT_IDS%% *}"
FIRST_RUN_DIR="$(chain_run_dir_for_root "$FIRST_ROOT")"
echo "  Root:      $FIRST_ROOT"
echo "  Run dir:   $FIRST_RUN_DIR"
echo ""
echo "  --- generate command ---"
echo "  python -c \"import sys; sys.path.insert(0, 'src'); import pva_work_flow.cli; pva_work_flow.cli.main()\" \\"
echo "    --engine vllm \\"
echo "    --vllm_base_url \"$VLLM_BASE_URL\" \\"
echo "    --vllm_model_name \"$VLLM_MODEL_NAME\" \\"
echo "    --vllm_timeout_s \"$VLLM_TIMEOUT_S\" \\"
echo "    --out_dir \"$OUT_DIR\" \\"
echo "    --seed \"$SEED\" \\"
echo "    --mode generate \\"
echo "    --round \"$ROUND\" \\"
echo "    --chain_search \\"
echo "    --chain_root_id \"$FIRST_ROOT\" \\"
echo "    --chain_accept_delta=\"$CHAIN_ACCEPT_DELTA\" \\"
echo "    --n_candidates \"$CHAIN_CANDIDATES\" \\"
echo "    --n_select \"$CHAIN_SELECT\" \\"
if [ -n "$FORMULATION_RAG_DB" ]; then
  echo "    --formulation_rag_db \"$FORMULATION_RAG_DB\" \\"
fi
echo "    --conv_cof_max \"$CONV_COF_MAX\" \\"
echo "    --conv_modulus_min \"$CONV_MODULUS_MIN\" \\"
echo "    --conv_modulus_max \"$CONV_MODULUS_MAX\" \\"
echo "    --conv_stable_proportion \"$CONV_STABLE_PROPORTION\" \\"
echo "    --conv_stick_slip_max \"$CONV_STICK_SLIP_MAX\" \\"
echo "    --conv_cof_trend_delta \"$CONV_COF_TREND_DELTA\" \\"
echo "    --conv_cof_trend_rounds \"$CONV_COF_TREND_ROUNDS\""
echo ""
echo "  --- prepare command ---"
echo "  python -c \"...\" --mode prepare --round \"$ROUND\" --chain_search --chain_root_id \"$FIRST_ROOT\" ..."
echo ""

# --- Summary ---
echo "============================================================"
if [ "$ALL_OK" = true ]; then
  echo " RESULT: All path checks PASSED"
else
  echo " RESULT: One or more paths are MISSING — review above"
fi
echo "============================================================"
