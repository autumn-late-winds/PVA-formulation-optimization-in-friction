# Agent Tools

| Tool | Risk | Writes artifacts | Confirmation |
|---|---|---:|---:|
| `inspect_workspace` | low | no | no |
| `refresh_reports` | low | yes | no |
| `build_failure_memory` | low | yes | no |
| `build_vector_index` | low | yes | no |
| `sync_results` | medium | yes | yes |
| `diagnose_round` | medium | yes | yes |
| `prepare_wetlab` | medium | yes | yes |
| `generate_round` | high | yes | yes |

The outer agent may recommend all tools, but it should only execute low-risk tools automatically.

