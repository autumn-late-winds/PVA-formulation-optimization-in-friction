# Agent Operator Manual

Use advisory mode to inspect a run:

```bash
python -m pva_work_flow.cli --agent --out_dir <run_dir>
```

Execute a low-risk tool:

```bash
python -m pva_work_flow.cli --agent --agent_execute refresh_reports --out_dir <run_dir>
python -m pva_work_flow.cli --agent --agent_execute build_failure_memory --out_dir <run_dir>
python -m pva_work_flow.cli --agent --agent_execute build_vector_index --out_dir <run_dir>
```

Medium and high risk actions are reported as commands but not executed by the first agent version. Review the suggested command, choose the parent node when needed, and run it explicitly.

