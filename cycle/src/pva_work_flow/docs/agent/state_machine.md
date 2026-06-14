# Agent State Machine

The first implementation is intentionally conservative and mostly read-only.

| State | Detection | Recommended action |
|---|---|---|
| `empty_workspace` | no round artifacts | create or import R1 |
| `raw_csv_ready` | raw `R{N}/` CSV exists, no `R{N}_results_filled.csv` | `sync_results` |
| `candidates_ready` | candidates exist, audits missing | `prepare_wetlab` |
| `results_synced` | results exist, diagnosis missing | `diagnose_round` |
| `ready_for_next_round` | latest diagnosis exists and no convergence stop | `generate_round` with explicit parent choice |
| `converged_candidate_found` | diagnosis convergence flag is true | human review |
| `needs_human_review` | ambiguous or legacy artifacts | inspect or regenerate with confirmation |

Only low-risk rebuild tools should be auto-executed. Generation and regeneration require confirmation.

