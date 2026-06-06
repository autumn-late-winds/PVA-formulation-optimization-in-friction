import argparse
from pathlib import Path
from .llm_engines import MockLLM, TransformersLLM, VllmOpenAIChatLLM
from .generator import run_generator
from .audit import run_auditor_rulebased
from .workflow import run_prepare_wetlab, run_diagnose, run_text_only_diagnose
from .utils import ensure_dir, read_json, write_json, load_allowed_materials, parse_bruker_csv, discriminate_pattern, plot_fx_vs_t, build_results_from_bruker_csvs
from .artifact_store import RunWorkspace
from .simulation import simulate_results
from .config import GenerationMode, BUDGET, CONVERGENCE as _DEFAULT_CONVERGENCE
from .budget_manager import count_completed_formulas, infer_stage, recommend_round_shape, budget_exhaustion_warnings, get_remaining_budget
from .experiment_notes import write_notes_template, load_notes, known_error_codes
from .tree_naming import find_tree_dir_for_parent, resolve_target_parent_id
import re
import csv
import sys
import os

# -------------------- Helper functions --------------------
def _run_csv_analysis(csv_path_str: str, out_dir: Path) -> None:
    csv_path = Path(csv_path_str)
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")
    print(f"[INFO] Analyzing: {csv_path}")
    parsed = parse_bruker_csv(csv_path)
    result = discriminate_pattern(parsed)

    print(f"\n{'='*50}")
    print(f"  Friction Pattern Analysis")
    print(f"{'='*50}")
    print(f"  Pattern:     {result['pattern'].upper()}")
    print(f"  Confidence:  {result['confidence']:.3f}")
    print(f"{'='*50}")
    d = result["details"]
    print(f"  Full cycles:       {d['n_full_cycles']}")
    print(f"  Half cycles:       {d['n_half_cycles']}")
    print(f"  Stable cycles:     {d['n_stable_half_cycles']}/{d['n_half_cycles']} ({d['stable_proportion']:.1%})")
    print(f"  Mean plateau:      {d['mean_plateau_ratio']:.3f}  (pos={d['pos_plateau_ratio']:.3f}, neg={d['neg_plateau_ratio']:.3f})")
    print(f"  Pos/Neg amplitude: {d['pos_amplitude']:.3f} / {d['neg_amplitude']:.3f} N  (asymmetry={d['asymmetry']:.3f})")
    print(f"  CV amplitude:      {d['cv_amplitude']:.3f}")
    print(f"  Amplitude trend:   {d['amplitude_trend']:.3f}")
    print(f"  Stick-slip score:  {d['stick_slip_score']:.3f}")
    print(f"{'='*50}")

    plot_path = out_dir / f"{csv_path.stem}_analysis.png"
    saved = plot_fx_vs_t(parsed, title=f"{csv_path.name}", save_path=plot_path)
    print(f"\n[OK] Plot saved to: {saved}")

    report = {"csv_file": str(csv_path), "result": result}
    report_path = out_dir / f"{csv_path.stem}_analysis.json"
    write_json(report_path, report)
    print(f"[OK] Report saved to: {report_path}")


def _run_build_results(build_dir: str, out_dir: Path) -> None:
    from collections import defaultdict
    from .io_artifacts import read_results_filled

    base_dir = Path(build_dir)
    if not base_dir.is_dir():
        raise SystemExit(f"Not a directory: {base_dir}")

    round_dirs: dict[int, Path] = {}
    for sub in sorted(base_dir.iterdir()):
        m = re.match(r"^R(\d+)$", sub.name)
        if m and sub.is_dir():
            rn = int(m.group(1))
            if list(sub.glob("*-*.csv")):
                round_dirs[rn] = sub

    if not round_dirs:
        raise SystemExit(f"No Rn/ directories with {{id}}-{{repeat}}.csv files found in {base_dir}")

    print(f"[INFO] Found rounds: {sorted(round_dirs.keys())}")

    for rn in sorted(round_dirs.keys()):
        csv_dir = round_dirs[rn]
        print(f"\n--- Round {rn}: {csv_dir.name} ---")

        groups: dict[str, list[Path]] = defaultdict(list)
        csv_pattern = re.compile(r"^(\d+)-(\d+)\.csv$")
        for f in sorted(csv_dir.glob("*.csv")):
            m = csv_pattern.match(f.name)
            if m:
                groups[m.group(1)].append(f)
            else:
                print(f"  [SKIP] {f.name}")

        if not groups:
            print(f"  [WARN] No valid CSV files, skipping round {rn}")
            continue

        candidate_csv_map = {}
        for sample_id, paths in sorted(groups.items()):
            cid = f"R{rn}-{int(sample_id):02d}"
            candidate_csv_map[cid] = sorted(paths)
            print(f"  [INFO] {cid}: {len(paths)} repeats -> {[p.name for p in candidate_csv_map[cid]]}")

        compression_map: dict[str, Path] = {}
        comp_dir = base_dir / f"R{rn}_compression"
        if comp_dir.is_dir():
            for comp_file in comp_dir.glob("*.csv"):
                m = re.match(r"^(\d+)", comp_file.stem)
                if m:
                    cid = f"R{rn}-{int(m.group(1)):02d}"
                    compression_map[cid] = comp_file
                    print(f"  [INFO] compression: {cid} <- {comp_file.name}")

        results_path = build_results_from_bruker_csvs(out_dir, rn, candidate_csv_map, compression_map)
        print(f"  [OK] {results_path.name}")

        rows = read_results_filled(results_path)
        print(f"  {'candidate_id':<12} {'cof_mean':>10} {'cof_std':>10}")
        print(f"  {'-'*34}")
        for r in rows:
            print(f"  {r['candidate_id']:<12} {r.get('cof_steady_mean','-'):>10} {r.get('cof_std','-'):>10}")

    try:
        from .formula_tree import build_tree
        from .tree_statistics import build_tree_statistics
        from .chain_memory import build_chain_memory
        from .tree_reports import build_tree_reports
        from .tree_visualizer import build_tree_diagram

        build_tree(out_dir)
        build_tree_statistics(out_dir)
        build_chain_memory(out_dir)
        build_tree_reports(out_dir)
        build_tree_diagram(out_dir)
        print("  [OK] Updated formula_tree.md, cross-tree statistics, chain memory, tree reports, and tree diagram")
    except Exception as e:
        print(f"  [WARN] Could not update tree statistics: {e}")


# -------------------- Main --------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out_dir", default="run_out", help="Output directory")
    ap.add_argument("--engine", choices=["mock", "transformers", "vllm"], default="mock")
    ap.add_argument("--model_path", default="", help="Local HF model path (for transformers engine)")
    ap.add_argument("--rounds", type=int, default=3, help="Number of rounds (full mode)")
    ap.add_argument("--round", type=int, default=1, help="Round index (single-round mode)")
    ap.add_argument("--mode", choices=["full", "generate", "prepare", "diagnose"], default="full")
    ap.add_argument("--n_candidates", type=int, default=None, help="Candidates per round (auto: R1=tree_initial_roots, R2+=4)")
    ap.add_argument("--n_select", type=int, default=None, help="Selected for wet lab (auto: =n_candidates)")
    ap.add_argument("--target_parent_id", default="", help="R2+ tree mode: optimize one parent formula node, e.g. R1-04; root-04 is accepted as a convenience alias")
    ap.add_argument("--chain_search", action="store_true", help="R2+ greedy chain mode: accept improving child, otherwise retry the current parent")
    ap.add_argument("--chain_root_id", default="", help="Root candidate for --chain_search, e.g. R1-01 or root-01. If empty, use best measured R1 root.")
    ap.add_argument("--chain_accept_delta", type=float, default=-1e-6, help="Accept a child when child_COF - parent_COF is below this value.")
    ap.add_argument("--tree_initial_roots", type=int, default=10, help="Suggested R1 root count for tree-mode studies")
    ap.add_argument("--simulate_results", action="store_true", help="Simulate wet-lab results (for workflow testing)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--vllm_base_url", default="http://localhost:8000/v1", help="vLLM OpenAI server base_url, must include /v1")
    ap.add_argument("--vllm_api_key", default="token-abc123", help="API key set when starting vLLM server")
    ap.add_argument("--vllm_model_name", default="qwen3-14b-sft", help="served model name (use --served-model-name when launching vLLM)")
    ap.add_argument("--vllm_timeout_s", type=float, default=120.0)
    ap.add_argument("--formulation_rag_db", default="", help="Optional SQLite path for external formulation-literature RAG")
    ap.add_argument("--disable_formulation_rag", action="store_true", help="Disable external formulation-literature RAG prompt injection")
    ap.add_argument("--use_external_r1",action="store_true",help="If set, R1 will use existing R1_candidates.json instead of generating by model when running in full/generate mode",)
    ap.add_argument("--analyze_csv", default="", help="Path to a Bruker UMT CSV file to analyze and plot friction data (Step 2)")
    ap.add_argument("--build_results", default="", help="Directory of Bruker CSV files to auto-build results_filled.csv from {id}-{repeat}.csv pattern")
    ap.add_argument("--sync_results", default="", help="Run directory containing Rn/ and Rn_compression/ folders; writes Rn_results_filled.csv back into the same directory")
    ap.add_argument("--status", action="store_true", help="Show run workspace status and recommended next action")
    ap.add_argument("--regenerate_round", type=int, default=0, help="Archive generated artifacts for this round, then regenerate it")
    ap.add_argument("--archive_old", action="store_true", help="Required with --regenerate_round; moves old generated artifacts into archive/")
    ap.add_argument("--compression_dir", default="", help="Directory of compression-test CSV files for modulus (matched by first digit of filename)")
    ap.add_argument("--write_notes_template", type=int, default=0, help="Write R{N}_experiment_notes.json template for round N")
    ap.add_argument("--list_error_codes", action="store_true", help="List all known experiment error codes")
    # ---- Convergence criteria (override defaults in config.py) ----
    ap.add_argument("--conv_cof_max", type=float, default=None, help="Convergence: max COF to declare convergence (default 0.02)")
    ap.add_argument("--conv_modulus_min", type=float, default=None, help="Convergence: min compression modulus MPa (default 1.5)")
    ap.add_argument("--conv_modulus_max", type=float, default=None, help="Convergence: max compression modulus MPa (default 2.5)")
    ap.add_argument("--conv_stable_proportion", type=float, default=None, help="Convergence: min stable_proportion (default 0.6)")
    ap.add_argument("--conv_stick_slip_max", type=float, default=None, help="Convergence: max stick_slip_score (default 0.2)")
    ap.add_argument("--conv_cof_trend_delta", type=float, default=None, help="Convergence: max round-to-round COF delta to consider flat (default 0.005)")
    ap.add_argument("--conv_cof_trend_rounds", type=int, default=None, help="Convergence: consecutive flat rounds needed (default 2)")
    args = ap.parse_args()

    # Build convergence criteria: CLI overrides > config defaults
    convergence = dict(_DEFAULT_CONVERGENCE)
    _conv_map = [
        ("cof_max", args.conv_cof_max),
        ("modulus_min_mpa", args.conv_modulus_min),
        ("modulus_max_mpa", args.conv_modulus_max),
        ("stable_proportion_min", args.conv_stable_proportion),
        ("stick_slip_max", args.conv_stick_slip_max),
        ("cof_trend_delta", args.conv_cof_trend_delta),
        ("cof_trend_consecutive", args.conv_cof_trend_rounds),
    ]
    for key, val in _conv_map:
        if val is not None:
            convergence[key] = val
    print(f"[CONVERGENCE] {convergence}")

    if args.disable_formulation_rag:
        os.environ["PVA_FORMULATION_RAG_ENABLED"] = "0"
        print("[FORMULATION_RAG] disabled")
    else:
        os.environ["PVA_FORMULATION_RAG_ENABLED"] = "1"
        if args.formulation_rag_db:
            os.environ["PVA_FORMULATION_RAG_DB"] = args.formulation_rag_db
        try:
            from .formulation_rag import resolve_formulation_rag_db

            print(f"[FORMULATION_RAG] db={resolve_formulation_rag_db()}")
        except Exception as e:
            print(f"[FORMULATION_RAG] db resolution unavailable: {e}")

    out_dir = Path(args.out_dir)

    if args.list_error_codes:
        print("Known experiment error codes:")
        for code in known_error_codes():
            from .experiment_notes import error_label
            print(f"  {code}: {error_label(code)}")
        return

    if args.status:
        ws = RunWorkspace(out_dir)
        print(ws.format_status_report())
        return

    if args.sync_results:
        run_dir = Path(args.sync_results)
        _run_build_results(str(run_dir), run_dir)
        # Apply experiment notes if present
        from .experiment_notes import apply_notes_to_candidates
        for r in RunWorkspace(run_dir).existing_rounds():
            cand_path = run_dir / f"R{r}_candidates.json"
            if cand_path.exists():
                obj = read_json(cand_path)
                apply_notes_to_candidates(obj.get("candidates", []), r, run_dir)
        print(RunWorkspace(run_dir).format_status_report())
        return

    if args.write_notes_template:
        r = args.write_notes_template
        cand_path = out_dir / f"R{r}_candidates.json"
        if not cand_path.exists():
            raise SystemExit(f"{cand_path} not found. Generate round {r} first.")
        cands = read_json(cand_path).get("candidates", [])
        cids = [c.get("candidate_id") for c in cands if c.get("candidate_id")]
        path = write_notes_template(out_dir, r, cids)
        print(f"[OK] Notes template written to {path}")
        print(f"     Fill in error_codes and free_text for each candidate, then re-run --sync_results.")
        return

    ensure_dir(out_dir)

    # ---- CSV analysis mode ----
    if args.analyze_csv:
        _run_csv_analysis(args.analyze_csv, out_dir)
        return

    # ---- Build results from Bruker CSV directory ----
    if args.build_results:
        _run_build_results(args.build_results, out_dir)
        return

    if args.regenerate_round:
        if not args.archive_old:
            raise SystemExit("--regenerate_round requires --archive_old so old generated artifacts are not silently overwritten.")
        archive_root = out_dir
        target_parent_id = resolve_target_parent_id(args.target_parent_id)
        if target_parent_id:
            tree_dir = find_tree_dir_for_parent(out_dir, target_parent_id)
            if tree_dir:
                archive_root = tree_dir
                print(f"[REGENERATE] Routed R{args.regenerate_round} archive to tree workspace: {archive_root}")
            else:
                print(f"[REGENERATE] No tree workspace found for {target_parent_id}; archiving in root out_dir.")
        archive_dir = RunWorkspace(archive_root).archive_round_outputs(args.regenerate_round)
        print(f"[REGENERATE] Archived generated R{args.regenerate_round} artifacts to {archive_dir}")
        args.mode = "generate"
        args.round = args.regenerate_round

    # ---- Tee stdout/stderr 到日志文件 ----
    log_path = out_dir / "run.log"

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                s.write(data)
                s.flush()
        def flush(self):
            for s in self.streams:
                s.flush()

    log_f = log_path.open("a", encoding="utf-8")
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    sys.stdout = Tee(sys.__stdout__, log_f)
    sys.stderr = Tee(sys.__stderr__, log_f)
    # ---- Tee 结束 ----

    try:
        # load allowed materials from repo materials folder
        materials_csv = Path(__file__).resolve().parents[2] / "materials" / "materials_en.csv"
        allowed_materials, material_info = load_allowed_materials(materials_csv)
        print(f"[DEBUG] allowed_materials: {allowed_materials[:10]}")

        if not allowed_materials:
            print(f"[WARN] No materials loaded from {materials_csv}")

        if args.engine == "mock":
            llm = MockLLM(seed=args.seed)

        elif args.engine == "transformers":
            if not args.model_path:
                raise SystemExit("Transformers engine requires --model_path")
            llm = TransformersLLM(args.model_path)

        elif args.engine == "vllm":
            llm = VllmOpenAIChatLLM(
                base_url=args.vllm_base_url,
                api_key=args.vllm_api_key,
                model_name=args.vllm_model_name,
                max_tokens=8192,
                temperature=0.2,
                top_p=0.95,
                timeout_s=args.vllm_timeout_s,
        )
        else:
            raise SystemExit(f"Unknown engine: {args.engine}")

        kpi_log_path = out_dir / "kpi_log.json"
        kpi_log = read_json(kpi_log_path) if kpi_log_path.exists() else []

        def one_round(r: int) -> bool:
            # ---- Tree directory routing ----
            # Tree directories use root-* labels; candidate links keep R*-* IDs.
            target_parent_id = resolve_target_parent_id(args.target_parent_id)
            if args.chain_search and r > 1 and not target_parent_id:
                from .chain_search import resolve_chain_parent, write_chain_state

                chain_root_id = resolve_target_parent_id(args.chain_root_id)
                chain_state = resolve_chain_parent(
                    out_dir,
                    root_id=chain_root_id,
                    accept_delta=args.chain_accept_delta,
                )
                write_chain_state(out_dir, chain_state)
                target_parent_id = chain_state["current_parent_id"]
                print(
                    f"[CHAIN] root={chain_state['root_id']} current_parent={target_parent_id} "
                    f"cof={chain_state.get('current_parent_cof')} trace_steps={len(chain_state['trace'])}"
                )
            tree_dir = None
            if target_parent_id:
                _candidate_tree = find_tree_dir_for_parent(out_dir, target_parent_id)
                if _candidate_tree:
                    tree_dir = _candidate_tree
                    print(f"[TREE] R{r} output routed to {tree_dir}")
            round_out = tree_dir if tree_dir else out_dir

            # Bootstrap tree dir: copy root_candidate.json as R1_candidates.json
            if tree_dir and r >= 2:
                _r1 = tree_dir / "R1_candidates.json"
                _root = tree_dir / "root_candidate.json"
                if not _r1.exists() and _root.exists():
                    import shutil
                    shutil.copy2(str(_root), str(_r1))

            cand_path = round_out / f"R{r}_candidates.json"
            results_filled = round_out / f"R{r}_results_filled.csv"

            # ---- 迭代元信息：父轮次 + 历史实验 / 审计信息 ----
            generation_mode = GenerationMode.FALLBACK
            parent_round_idx: int | None = None
            last_valid_experimental_round: int | None = None
            last_failed_audit_round: int | None = None

            if r > 1:
                parent_round_idx = r - 1
                prev_results = round_out / f"R{parent_round_idx}_results_filled.csv"
                prev_diag = round_out / f"R{parent_round_idx}_diagnosis.json"

                for rr in range(parent_round_idx, 0, -1):
                    rf = round_out / f"R{rr}_results_filled.csv"
                    if rf.exists():
                        last_valid_experimental_round = rr
                        break

                for rr in range(parent_round_idx, 0, -1):
                    audits_path = round_out / f"R{rr}_audits.json"
                    if not audits_path.exists():
                        continue
                    audits_obj = read_json(audits_path)
                    audits = audits_obj.get("audits", [])
                    if any(a.get("decision") == "FAIL" for a in audits):
                        last_failed_audit_round = rr
                        break

                # generation_mode 规则（修正为：只要历史上有任意真实结果，就视为 result_driven）
                if last_valid_experimental_round is not None:
                    generation_mode = GenerationMode.RESULT_DRIVEN
                elif prev_diag.exists():
                    generation_mode = GenerationMode.DIAGNOSIS_DRIVEN
                else:
                    generation_mode = GenerationMode.FALLBACK

            # Auto-compute candidate counts from round if not explicitly set
            if args.n_candidates is not None:
                n_candidates = args.n_candidates
            elif r == 1:
                n_candidates = args.tree_initial_roots
            else:
                n_candidates = 4  # constrained DOE: 1 baseline + 2 single_factor + 1 local_opt
            if args.n_select is not None:
                n_select = args.n_select
            else:
                n_select = n_candidates

            if args.mode in ("full", "generate"):
                if r == 1 and getattr(args, "use_external_r1", False):
                    # 用户要求第一轮用外部候选
                    if cand_path.exists():
                        print(f"[R1] use_external_r1=TRUE, use existing candidates: {cand_path}")
                    else:
                        raise SystemExit(
                            f"use_external_r1 is set but {cand_path} does not exist. "
                            f"Please put your external R1_candidates.json in the out_dir before running."
                        )
                else:
                    if cand_path.exists():
                        print(f"[R{r}] candidates already exist: {cand_path}, skip generation.")
                    else:
                        # ---- Budget / stage awareness ----
                        completed = count_completed_formulas(out_dir)
                        remaining = get_remaining_budget(completed, BUDGET["total_formula_budget"])
                        stage = infer_stage(completed)
                        shape = recommend_round_shape(stage, remaining)
                        for w in budget_exhaustion_warnings(completed, BUDGET["total_formula_budget"]):
                            print(f"[BUDGET] {w}")
                        print(
                            f"[BUDGET] round={r} stage={stage} "
                            f"completed={completed}/{BUDGET['total_formula_budget']} "
                            f"remaining={remaining} shape={shape['round_size']}candidates "
                            f"exploration={'on' if shape['allow_limited_exploration'] else 'off'}"
                        )
                        target_parent_id = target_parent_id or resolve_target_parent_id(args.target_parent_id)
                        if target_parent_id and r <= 1:
                            raise SystemExit("--target_parent_id is only valid for R2+ tree optimization.")
                        # Guard: require results_filled.csv with actual data before generating R3+
                        if r > 2:
                            prev_results = round_out / f"R{r-1}_results_filled.csv"
                            if not prev_results.exists():
                                raise SystemExit(
                                    f"R{r} generation blocked: {prev_results.name} not found. "
                                    f"Complete wet-lab experiments for R{r-1} and fill results_filled.csv first."
                                )
                            try:
                                with open(prev_results, encoding="utf-8-sig", newline="") as _fh:
                                    _rows = list(csv.DictReader(_fh))
                                _has_data = any(
                                    (_r.get("cof_steady_mean") or "").strip()
                                    for _r in _rows
                                )
                                if not _has_data:
                                    raise SystemExit(
                                        f"R{r} generation blocked: {prev_results.name} exists but has no COF data. "
                                        f"Fill in experimental results for R{r-1} before generating R{r}."
                                    )
                            except SystemExit:
                                raise
                            except Exception:
                                pass  # CSV parse error; let it proceed with a warning
                            print(f"[GUARD] R{r-1} results confirmed — proceeding to generate R{r}.")
                        print(f"[AUTO] R{r}: n_candidates={n_candidates}, n_select={n_select}")
                        if target_parent_id:
                            m_parent = re.match(r"^R(\d+)-\d+$", target_parent_id)
                            if m_parent:
                                parent_round_idx = int(m_parent.group(1))
                            print(
                                f"[TREE] R{r}: optimizing one parent node only: {target_parent_id}. "
                                f"n_candidates={n_candidates} uses local single-step branches."
                            )
                        cand_path = run_generator(
                            llm,
                            round_out,
                            r,
                            n_candidates,
                            allowed_materials=allowed_materials,
                            material_info=material_info,
                            generation_mode=generation_mode,
                            parent_round_idx=parent_round_idx,
                            last_valid_experimental_round=last_valid_experimental_round,
                            last_failed_audit_round=last_failed_audit_round,
                            target_parent_id=target_parent_id,
                        )
            if args.mode in ("full", "prepare"):
                if not cand_path.exists():
                    raise SystemExit(f"Missing {cand_path}. Run --mode generate first.")
                audits_path, selected = run_auditor_rulebased(round_out, r, cand_path, n_select)

                if not selected:
                    print(f"[R{r}] 0 candidates passed strict audit; using all candidates as fallback selection.")
                    cands = read_json(cand_path)["candidates"]
                    all_ids = [c.get("candidate_id") for c in cands if c.get("candidate_id")]
                    selected = all_ids[: n_select] if all_ids else []

                if not selected:
                    print(f"[R{r}] no usable candidates; text-only diagnosis.")
                    diag_path = run_text_only_diagnose(round_out, r, cand_path, audits_path)
                    print(f"[R{r}] text-only diagnosis -> {diag_path.name}")
                    return False

                doe_path, tmpl_path = run_prepare_wetlab(round_out, r, cand_path, selected)
                print(f"[R{r}] candidates={cand_path.name} audits={audits_path.name} DOE={doe_path.name} template={tmpl_path.name}")

                if args.simulate_results:
                    cands = read_json(cand_path)["candidates"]
                    simulate_results(results_filled, selected, cands, seed=args.seed + r)
                    print(f"[R{r}] simulated results -> {results_filled.name}")

            if args.mode in ("full", "diagnose"):
                if not cand_path.exists():
                    raise SystemExit(f"Missing {cand_path}. Run --mode generate first.")
                if not results_filled.exists():
                    raise SystemExit(f"Missing {results_filled}. Fill results_template and save as results_filled.")
                # pass allowed_materials to diagnose if it needs to validate materials in results
                diag_path, kpi = run_diagnose(llm, round_out, r, cand_path, results_filled, convergence=convergence)
                print(f"[R{r}] diagnosis={diag_path.name} KPIs={kpi}")

                # update KPI log (replace same round)
                kpi_log[:] = [x for x in kpi_log if x.get("round") != r] + [kpi]
                write_json(kpi_log_path, kpi_log)

            try:
                from .formula_tree import build_tree
                from .tree_statistics import build_tree_statistics
                from .chain_memory import build_chain_memory
                from .tree_reports import build_tree_reports
                from .tree_visualizer import build_tree_diagram

                build_tree(round_out)
                build_tree_statistics(round_out)
                build_chain_memory(round_out)
                build_tree_reports(round_out)
                build_tree_diagram(round_out)
                if round_out != out_dir:
                    build_tree(out_dir)
                    build_tree_statistics(out_dir)
                    build_chain_memory(out_dir)
                    build_tree_reports(out_dir)
                    build_tree_diagram(out_dir)
                print(f"[TREE_REPORTS] refreshed reports for {round_out}")
            except Exception as e:
                print(f"[TREE_REPORTS] unavailable: {e}")
            return True

        # single-round modes
        # multi-round in full mode, single-round otherwise
        if args.mode == "full":
            for r in range(1, args.rounds + 1):
                if not one_round(r):
                    print(f"[STOP] Pipeline stopped after R{r}; no usable candidates for the next round.")
                    break
        else:
            one_round(args.round)

    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        log_f.close()

    if kpi_log_path.exists():
        print(f"[OK] KPI log -> {kpi_log_path}")

if __name__ == "__main__":
    main()
