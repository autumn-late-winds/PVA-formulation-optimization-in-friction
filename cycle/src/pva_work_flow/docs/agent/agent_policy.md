# Agent Policy

The outer operation agent manages workflow state and tool calls. It does not replace the constrained PVA formulation workflow.

Hard rules:

- Do not directly edit `R{N}_candidates.json`.
- Do not bypass `constrained_doe.py` for R2+ generation.
- Do not introduce new materials automatically.
- Do not mix multiple parent nodes when `target_parent_id` is set.
- Do not treat `root-*` tree labels as `parent_candidate_id`.
- Keep `audit_status` separate from `experimental_status`.
- Use project experimental results before literature priors.
- Ask for human confirmation before generation, regeneration, new material exploration, convergence termination, or threshold changes.

