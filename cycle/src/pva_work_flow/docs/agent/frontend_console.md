# Agent Frontend Console

This is the first low-risk frontend implementation from the layered frontend plan.

It intentionally exposes only:

1. Read-only workflow state.
2. Read-only agent advice.
3. Read-only tree and log summaries.
4. Low-risk agent tools:
   - `refresh_reports`
   - `build_failure_memory`
   - `build_vector_index`

It does not expose generation, wet-lab preparation, diagnosis, regeneration, deletion, vLLM launch, or convergence confirmation.

## Start

From the `cycle` workspace:

```bash
python -m pva_work_flow.agent_server --out_dir src/sft_qwen3_14b_out
```

Or through the existing CLI:

```bash
python -m pva_work_flow.cli --agent_server --out_dir src/sft_qwen3_14b_out
```

Then open:

```text
http://127.0.0.1:8765/
```

## Read-Only API

```text
GET /api/state?out_dir=...
GET /api/agent/report?out_dir=...
GET /api/tools?out_dir=...
GET /api/tree?out_dir=...
GET /api/logs?out_dir=...
```

## Low-Risk Execution API

```text
POST /api/tools/execute
```

Body:

```json
{
  "out_dir": "src/sft_qwen3_14b_out",
  "tool": "refresh_reports"
}
```

The server checks the tool against `DEFAULT_POLICY.low_risk_auto_actions` and then routes execution through `run_low_risk_tool`.

## Boundary

This console is an operation surface, not a scientific decision engine. Medium-risk and high-risk workflow steps should still use the existing CLI or scripts with explicit human confirmation.
