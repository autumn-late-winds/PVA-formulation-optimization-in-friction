"""Markdown reports for the outer operation agent."""

from __future__ import annotations

from typing import Any


def render_agent_report(advice: dict[str, Any]) -> str:
    state = advice["state"]
    lines: list[str] = []
    lines.append("# PVA Operation Agent Report")
    lines.append("")
    lines.append(f"- Run directory: `{state['out_dir']}`")
    lines.append(f"- State: `{state['state']}`")
    lines.append(f"- Latest round: `{state['round']}`")
    lines.append(f"- Recommended action: `{state['recommended_action']}`")
    lines.append(f"- Risk level: `{advice['risk_level']}`")
    lines.append(f"- Requires confirmation: `{advice['requires_confirmation']}`")
    lines.append(f"- Can auto execute: `{advice['can_auto_execute']}`")
    lines.append("")
    lines.append("## Why")
    for item in advice.get("reasoning", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Evidence")
    evidence = state.get("evidence") or ["No evidence recorded."]
    for item in evidence:
        lines.append(f"- {item}")
    if state.get("missing"):
        lines.append("")
        lines.append("## Missing")
        for item in state["missing"]:
            lines.append(f"- {item}")
    if state.get("warnings"):
        lines.append("")
        lines.append("## Warnings")
        for item in state["warnings"]:
            lines.append(f"- {item}")
    lines.append("")
    lines.append("## Suggested Command")
    lines.append("")
    lines.append("```bash")
    lines.append(str(advice.get("command", "")))
    lines.append("```")
    budget = state.get("budget") or {}
    if budget:
        lines.append("")
        lines.append("## Budget")
        lines.append(
            f"- Used: `{budget.get('completed', '?')}/{budget.get('total', '?')}`"
        )
        lines.append(f"- Remaining: `{budget.get('remaining', '?')}`")
        lines.append(f"- Stage: `{budget.get('stage', '?')}`")
        for warning in budget.get("warnings", []):
            lines.append(f"- {warning}")
    lines.append("")
    lines.append("## Policy Reminders")
    for item in advice.get("policy_reminders", []):
        lines.append(f"- {item}")
    return "\n".join(lines)
