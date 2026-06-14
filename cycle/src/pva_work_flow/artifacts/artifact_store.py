"""Run workspace artifact paths, status, and safe archiving helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
from typing import Any, Dict, List

from pva_work_flow.core.utils import read_json


ROUND_OUTPUT_SUFFIXES = (
    "candidates.json",
    "audits.json",
    "audit_agent.json",
    "diagnosis.json",
    "doe.csv",
    "doe_plan.json",
    "inheritance_table.md",
    "results_template.csv",
    "results_filled.csv",
    "experiment_notes.json",
)


@dataclass
class RunWorkspace:
    """Centralized interface for one optimization run directory."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def _state_root(self) -> Path:
        state_dir = self.root / "run_state_files"
        return state_dir if state_dir.is_dir() else self.root

    def _existing_or_root(self, name: str) -> Path:
        root_path = self.root / name
        state_path = self._state_root() / name
        if root_path.exists():
            return root_path
        if state_path.exists():
            return state_path
        return root_path

    def artifact_dirs(self, include_trees: bool = True) -> List[Path]:
        """Return artifact directories in read priority order."""
        dirs: List[Path] = []
        state_dir = self.root / "run_state_files"
        if state_dir.is_dir():
            dirs.append(state_dir)
        if self.root.is_dir():
            dirs.append(self.root)
        trees_dir = self.root / "trees"
        if include_trees and trees_dir.is_dir():
            dirs.extend(sorted(p for p in trees_dir.iterdir() if p.is_dir()))
        return dirs

    def existing_round_artifact(self, round_idx: int, suffix: str, include_trees: bool = True) -> Path:
        name = f"R{round_idx}_{suffix}"
        for artifact_dir in self.artifact_dirs(include_trees=include_trees):
            path = artifact_dir / name
            if path.exists():
                return path
        return self.root / name

    def round_artifact_paths(self, round_idx: int, suffix: str, include_trees: bool = True) -> List[Path]:
        name = f"R{round_idx}_{suffix}"
        return [
            artifact_dir / name
            for artifact_dir in self.artifact_dirs(include_trees=include_trees)
            if (artifact_dir / name).exists()
        ]

    def all_round_artifact_paths(self, suffix: str, include_trees: bool = True) -> List[Path]:
        paths: List[Path] = []
        for artifact_dir in self.artifact_dirs(include_trees=include_trees):
            paths.extend(sorted(artifact_dir.glob(f"R*_{suffix}")))
        return paths

    def candidates_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "candidates.json")

    def audits_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "audits.json")

    def results_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "results_filled.csv")

    def results_template_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "results_template.csv")

    def diagnosis_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "diagnosis.json")

    def doe_plan_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "doe_plan.json")

    def inheritance_table_path(self, round_idx: int) -> Path:
        return self.existing_round_artifact(round_idx, "inheritance_table.md")

    def raw_friction_dir(self, round_idx: int) -> Path:
        return self.root / f"R{round_idx}"

    def raw_compression_dir(self, round_idx: int) -> Path:
        return self.root / f"R{round_idx}_compression"

    def existing_rounds(self) -> List[int]:
        rounds: set[int] = set()
        for artifact_dir in self.artifact_dirs(include_trees=True):
            for path in artifact_dir.glob("R*_candidates.json"):
                idx = _round_from_name(path.name)
                if idx is not None:
                    rounds.add(idx)
            for path in artifact_dir.glob("R*_results_filled.csv"):
                idx = _round_from_name(path.name)
                if idx is not None:
                    rounds.add(idx)
        for path in self.root.iterdir() if self.root.exists() else []:
            if path.is_dir():
                idx = _round_from_name(path.name)
                if idx is not None:
                    rounds.add(idx)
        return sorted(rounds)

    def round_status(self, round_idx: int) -> Dict[str, Any]:
        friction_dir = self.raw_friction_dir(round_idx)
        compression_dir = self.raw_compression_dir(round_idx)
        doe_plan_path = self.doe_plan_path(round_idx)
        skeleton_source = ""
        doe_plan_kind = "missing"
        if doe_plan_path.exists():
            try:
                skeleton_source = str(read_json(doe_plan_path).get("skeleton_source", ""))
            except Exception:
                skeleton_source = "unreadable"
            doe_plan_kind = "constrained" if skeleton_source == "code_constrained_doe" else "legacy_or_llm"

        notes_path = self.root / f"R{round_idx}_experiment_notes.json"
        notes_info = "missing"
        if notes_path.exists():
            try:
                notes_obj = read_json(notes_path)
                n_errors = sum(
                    1 for k, v in notes_obj.items()
                    if not k.startswith("_") and isinstance(v, dict) and v.get("error_codes")
                )
                notes_info = f"{n_errors} candidates with errors" if n_errors else "present (no errors)"
            except Exception:
                notes_info = "unreadable"

        status = {
            "round": round_idx,
            "candidates": self.candidates_path(round_idx).exists(),
            "audits": self.audits_path(round_idx).exists(),
            "experiment_notes": notes_info,
            "raw_friction_csv_count": len(list(friction_dir.glob("*-*.csv"))) if friction_dir.is_dir() else 0,
            "raw_compression_csv_count": len(list(compression_dir.glob("*.csv"))) if compression_dir.is_dir() else 0,
            "results_filled": self.results_path(round_idx).exists(),
            "diagnosis": self.diagnosis_path(round_idx).exists(),
            "doe_plan": doe_plan_path.exists(),
            "doe_plan_kind": doe_plan_kind,
            "skeleton_source": skeleton_source,
            "inheritance_table": self.inheritance_table_path(round_idx).exists(),
            "recommended_next": "",
        }
        status["recommended_next"] = self._recommend_next(status)
        return status

    def all_statuses(self) -> List[Dict[str, Any]]:
        rounds = self.existing_rounds()
        if not rounds:
            return []
        return [self.round_status(r) for r in rounds]

    def next_action(self) -> Dict[str, Any]:
        statuses = self.all_statuses()
        # Budget-aware recommendation
        from pva_work_flow.orchestration.budget_manager import count_completed_formulas, infer_stage, get_remaining_budget, recommend_round_shape, budget_exhaustion_warnings

        completed = count_completed_formulas(self.root)
        remaining = get_remaining_budget(completed)
        stage = infer_stage(completed)
        shape = recommend_round_shape(stage, remaining)
        warnings = budget_exhaustion_warnings(completed)

        budget_info = {
            "completed": completed,
            "remaining": remaining,
            "total": 100,
            "stage": stage,
            "warnings": warnings,
        }

        if not statuses:
            return {
                "action": "create_r1",
                "command": "Put R1_candidates.json in the run directory or run --mode full --rounds 1.",
                "budget": budget_info,
            }
        latest = statuses[-1]
        if latest["raw_friction_csv_count"] and not latest["results_filled"]:
            return {
                "action": "sync_results",
                "round": latest["round"],
                "command": "--sync_results <run_dir>",
                "budget": budget_info,
            }
        if latest["results_filled"] and not latest["diagnosis"]:
            return {
                "action": "diagnose",
                "round": latest["round"],
                "command": f"--mode diagnose --round {latest['round']} --out_dir <run_dir>",
                "budget": budget_info,
            }
        next_round = latest["round"] + 1
        return {
            "action": "generate_next_round",
            "round": next_round,
            "command": f"--mode generate --round {next_round} --n_candidates {shape['round_size']} --n_select {shape['round_size']} --out_dir <run_dir>",
            "budget": budget_info,
        }

    def archive_round_outputs(self, round_idx: int) -> Path:
        """Move generated round artifacts into archive/ without touching raw CSV dirs."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = self.root / "archive" / f"R{round_idx}_{timestamp}"
        archive_dir.mkdir(parents=True, exist_ok=False)
        moved = 0

        for suffix in ROUND_OUTPUT_SUFFIXES:
            path = self.root / f"R{round_idx}_{suffix}"
            if path.exists():
                shutil.move(str(path), str(archive_dir / path.name))
                moved += 1

        for plot_path in self.root.glob(f"R{round_idx}_R{round_idx}-*_friction.png"):
            shutil.move(str(plot_path), str(archive_dir / plot_path.name))
            moved += 1

        if moved == 0:
            marker = archive_dir / "EMPTY_ARCHIVE.txt"
            marker.write_text(f"No generated R{round_idx} artifacts were present.\n", encoding="utf-8")
        return archive_dir

    def format_status_report(self) -> str:
        lines = [f"Run workspace: {self.root}", ""]
        statuses = self.all_statuses()
        if not statuses:
            lines.append("No rounds found.")
            lines.append("Recommended next: add R1_candidates.json or run initial generation.")
            return "\n".join(lines)

        for status in statuses:
            lines.extend(
                [
                    f"R{status['round']}:",
                    f"  candidates: {'present' if status['candidates'] else 'missing'}",
                    f"  raw friction CSV: {status['raw_friction_csv_count']} files",
                    f"  compression CSV: {status['raw_compression_csv_count']} files",
                    f"  results_filled: {'present' if status['results_filled'] else 'missing'}",
                    f"  diagnosis: {'present' if status['diagnosis'] else 'missing'}",
                    f"  doe_plan: {status['doe_plan_kind']}",
                    f"  inheritance_table: {'present' if status['inheritance_table'] else 'missing'}",
                    f"  experiment_notes: {status.get('experiment_notes', 'missing')}",
                    f"  recommended next: {status['recommended_next']}",
                    "",
                ]
            )
        action = self.next_action()
        budget = action.get("budget", {})
        lines.append(f"Overall next action: {action.get('action')}")
        if budget:
            lines.append(
                f"Budget: {budget.get('completed', '?')}/{budget.get('total', 100)} used "
                f"({budget.get('remaining', '?')} remaining, stage={budget.get('stage', '?')})"
            )
            for w in budget.get("warnings", []):
                lines.append(f"  [!] {w}")
        lines.append(f"Suggested command: {action.get('command')}")
        return "\n".join(lines)

    def _recommend_next(self, status: Dict[str, Any]) -> str:
        if status["raw_friction_csv_count"] and not status["results_filled"]:
            return "run --sync_results for this run directory"
        if status["results_filled"] and not status["diagnosis"]:
            return f"run --mode diagnose --round {status['round']}"
        if status["doe_plan_kind"] == "legacy_or_llm" and status["round"] >= 2:
            return "consider --regenerate_round with --archive_old to use constrained DOE"
        if status["diagnosis"]:
            return f"generate R{status['round'] + 1} with constrained planner"
        if status["candidates"] and not status["audits"]:
            return f"run --mode prepare --round {status['round']}"
        return "inspect missing artifacts"


def _round_from_name(name: str) -> int | None:
    if not name.startswith("R"):
        return None
    digits = []
    for ch in name[1:]:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if not digits:
        return None
    return int("".join(digits))
