"""Local low-risk web console and HTTP API for the operation agent."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import os

from pva_work_flow.agent.planner import build_agent_advice
from pva_work_flow.agent.policy import DEFAULT_POLICY
from pva_work_flow.agent.reports import render_agent_report
from pva_work_flow.agent.tools import TOOL_REGISTRY, run_low_risk_tool
from pva_work_flow.artifacts.artifact_store import RunWorkspace
from pva_work_flow.core.utils import read_json, write_json
from pva_work_flow.core.llm_engines import VllmOpenAIChatLLM
from pva_work_flow.memory.vector_rag import ensure_project_vector_index, query_vector_index


DEFAULT_OUT_DIR = "run_out"


def build_state_payload(out_dir: Path) -> dict[str, Any]:
    """Return read-only workflow and agent state for the dashboard."""

    ws = RunWorkspace(out_dir)
    advice = build_agent_advice(out_dir)
    return {
        "out_dir": str(out_dir),
        "exists": out_dir.exists(),
        "workflow": {
            "statuses": ws.all_statuses(),
            "next_action": ws.next_action(),
            "status_report": ws.format_status_report(),
        },
        "agent": advice,
        "low_risk_tools": list_low_risk_tools(out_dir),
        "recent_artifacts": list_recent_artifacts(out_dir),
    }


def list_low_risk_tools(out_dir: Path) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in DEFAULT_POLICY.low_risk_auto_actions:
        tool = TOOL_REGISTRY.get(name)
        if tool is None:
            continue
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "risk_level": tool.risk_level,
                "requires_confirmation": tool.requires_confirmation,
                "writes_artifacts": tool.writes_artifacts,
                "command": tool.command_template.replace("<run_dir>", str(out_dir)),
                "executable": tool.executor is not None
                and tool.risk_level == "low"
                and not tool.requires_confirmation,
            }
        )
    return tools


def build_agent_report_payload(out_dir: Path) -> dict[str, Any]:
    advice = build_agent_advice(out_dir)
    return {
        "out_dir": str(out_dir),
        "advice": advice,
        "markdown": render_agent_report(advice),
    }


def build_tree_payload(out_dir: Path) -> dict[str, Any]:
    tree_files = [
        "formula_tree.md",
        "SIMPLE_TREE.md",
        "TREE_DIAGRAM.md",
        "GLOBAL_TREE_SUMMARY.md",
        "tree_statistics.md",
        "tree_statistics.json",
    ]
    files: list[dict[str, Any]] = []
    for name in tree_files:
        path = out_dir / name
        if not path.exists():
            files.append({"name": name, "exists": False})
            continue
        item: dict[str, Any] = {
            "name": name,
            "exists": True,
            "path": str(path),
            "size_bytes": path.stat().st_size,
        }
        if path.suffix == ".json":
            try:
                item["json"] = read_json(path)
            except Exception as exc:
                item["error"] = str(exc)
        else:
            item["text_preview"] = _read_text_preview(path)
        files.append(item)
    return {"out_dir": str(out_dir), "files": files}


def build_logs_payload(out_dir: Path, limit: int = 200) -> dict[str, Any]:
    limit = max(1, min(limit, 2000))
    log_path = out_dir / "run.log"
    result_paths = sorted(out_dir.glob("agent_*_result.json")) if out_dir.exists() else []
    return {
        "out_dir": str(out_dir),
        "run_log": {
            "path": str(log_path),
            "exists": log_path.exists(),
            "tail": _tail_text(log_path, limit) if log_path.exists() else "",
        },
        "agent_results": [
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "json": _read_json_or_error(path),
            }
            for path in result_paths[-10:]
        ],
    }


def answer_project_question(
    out_dir: Path,
    question: str,
    *,
    vllm_base_url: str,
    vllm_api_key: str,
    vllm_model_name: str,
) -> dict[str, Any]:
    """Answer with the fine-tuned Qwen model grounded in local project evidence."""

    question = question.strip()
    if not question:
        raise ValueError("Question is required")
    index = ensure_project_vector_index(out_dir) if out_dir.exists() else {}
    hits = query_vector_index(index, question, top_k=4)
    evidence = "\n\n".join(
        f"[{i + 1}] {hit.get('source_type')}: {hit.get('text')}"
        for i, hit in enumerate(hits)
    ) or "No matching local project evidence was retrieved. State this limitation clearly."
    llm = VllmOpenAIChatLLM(
        base_url=vllm_base_url,
        api_key=vllm_api_key,
        model_name=vllm_model_name,
        max_tokens=1200,
        temperature=0.2,
        timeout_s=120.0,
    )
    answer = llm.generate(
        "You are the PVA formulation optimization research assistant. Answer in the user's language. "
        "Use the supplied local evidence as the primary basis, do not invent measured results, and explicitly label uncertainty. "
        "End with a concise, practical next step when appropriate.",
        f"Question:\n{question}\n\nLocal evidence:\n{evidence}",
    )
    return {
        "question": question,
        "answer": answer,
        "sources": hits,
        "doc_count": index.get("doc_count", 0),
        "model": vllm_model_name,
    }


def execute_low_risk_tool_payload(out_dir: Path, tool_name: str) -> dict[str, Any]:
    if tool_name not in DEFAULT_POLICY.low_risk_auto_actions:
        raise PermissionError(f"{tool_name} is not listed as a low-risk auto action")
    result = run_low_risk_tool(tool_name, out_dir)
    if tool_name != "inspect_workspace":
        out_dir.mkdir(parents=True, exist_ok=True)
        result_path = out_dir / f"agent_{tool_name}_result.json"
        write_json(result_path, result)
        result["result_path"] = str(result_path)
    return {
        "ok": True,
        "tool": tool_name,
        "out_dir": str(out_dir),
        "result": result,
        "state": build_state_payload(out_dir),
    }


def list_recent_artifacts(out_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not out_dir.exists():
        return []
    files = [p for p in out_dir.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.name,
            "path": str(path),
            "size_bytes": path.stat().st_size,
        }
        for path in files[:limit]
    ]


def create_handler(default_out_dir: Path, qwen_config: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class AgentServerHandler(BaseHTTPRequestHandler):
        server_version = "PVAAgentServer/0.1"

        def do_OPTIONS(self) -> None:
            self._send_empty(HTTPStatus.NO_CONTENT)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            out_dir = _resolve_out_dir(query, default_out_dir)
            try:
                if parsed.path in {"/", "/dashboard"}:
                    self._send_text(DASHBOARD_HTML, "text/html; charset=utf-8")
                elif parsed.path == "/api/state":
                    self._send_json(build_state_payload(out_dir))
                elif parsed.path == "/api/agent/report":
                    self._send_json(build_agent_report_payload(out_dir))
                elif parsed.path == "/api/tools":
                    self._send_json({"out_dir": str(out_dir), "tools": list_low_risk_tools(out_dir)})
                elif parsed.path == "/api/tree":
                    self._send_json(build_tree_payload(out_dir))
                elif parsed.path == "/api/logs":
                    limit = _int_query(query, "limit", 200)
                    self._send_json(build_logs_payload(out_dir, limit))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {parsed.path}")
            except Exception as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                out_dir = Path(str(body.get("out_dir") or default_out_dir))
                if parsed.path == "/api/tools/execute":
                    tool_name = str(body.get("tool") or "")
                    if not tool_name:
                        self._send_error(HTTPStatus.BAD_REQUEST, "Missing tool")
                        return
                    self._send_json(execute_low_risk_tool_payload(out_dir, tool_name))
                elif parsed.path == "/api/qa":
                    question = str(body.get("question") or "")
                    self._send_json(answer_project_question(out_dir, question, **qwen_config))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {parsed.path}")
            except PermissionError as exc:
                self._send_error(HTTPStatus.FORBIDDEN, str(exc))
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[agent-server] {self.address_string()} - {format % args}")

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self._send_common_headers("application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_text(self, text: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = text.encode("utf-8")
            self.send_response(status)
            self._send_common_headers(content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_empty(self, status: HTTPStatus) -> None:
            self.send_response(status)
            self._send_common_headers("text/plain; charset=utf-8")
            self.end_headers()

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"ok": False, "error": message}, status)

        def _send_common_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    return AgentServerHandler


def run_server(
    host: str,
    port: int,
    out_dir: Path,
    vllm_base_url: str = "http://localhost:8000/v1",
    vllm_api_key: str | None = None,
    vllm_model_name: str = "qwen3-14b-sft",
) -> None:
    handler = create_handler(out_dir, {
        "vllm_base_url": vllm_base_url,
        "vllm_api_key": vllm_api_key or os.environ.get("PVA_VLLM_API_KEY", "token-abc123"),
        "vllm_model_name": vllm_model_name,
    })
    server = ThreadingHTTPServer((host, port), handler)
    print(f"[agent-server] serving http://{host}:{port}/")
    print(f"[agent-server] default out_dir={out_dir}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local low-risk PVA agent console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out_dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--vllm_base_url", default="http://localhost:8000/v1")
    parser.add_argument("--vllm_model_name", default="qwen3-14b-sft")
    parser.add_argument("--vllm_api_key", default="")
    parser.add_argument("--check", action="store_true", help="Print state JSON and exit.")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if args.check:
        print(json.dumps(build_state_payload(out_dir), ensure_ascii=False, indent=2))
        return
    run_server(args.host, args.port, out_dir, args.vllm_base_url, args.vllm_api_key, args.vllm_model_name)


def _resolve_out_dir(query: dict[str, list[str]], default_out_dir: Path) -> Path:
    values = query.get("out_dir") or []
    return Path(values[0]) if values and values[0] else default_out_dir


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name) or []
    if not values:
        return default
    try:
        return int(values[0])
    except ValueError:
        return default


def _read_text_preview(path: Path, max_chars: int = 12000) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text[:max_chars]


def _tail_text(path: Path, limit: int) -> str:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-limit:])


def _read_json_or_error(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except Exception as exc:
        return {"error": str(exc)}


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PVA Formulation Lab</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8f5;
      --panel: #ffffff;
      --ink: #202423;
      --muted: #65706b;
      --line: #d9dfd8;
      --accent: #16615a;
      --accent-2: #b84a35;
      --soft: #eef3ee;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 16px;
      padding: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 14px;
      min-width: 0;
    }
    h2 { margin: 0 0 10px; font-size: 15px; }
    .stack { display: grid; gap: 16px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    label { color: var(--muted); font-size: 13px; }
    input {
      min-width: 280px;
      flex: 1;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 4px;
      font-size: 14px;
    }
    textarea { width: 100%; margin: 6px 0 10px; padding: 10px; resize: vertical; border: 1px solid var(--line); border-radius: 4px; font: inherit; line-height: 1.45; }
    .muted { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .sources { display: grid; gap: 6px; margin-top: 10px; }
    .source { padding: 8px; border: 1px solid var(--line); border-radius: 4px; font-size: 12px; color: var(--muted); }
    .loop { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .loop-step { position: relative; min-height: 102px; padding: 12px; border: 1px solid var(--line); border-radius: 6px; background: #f8faf7; }
    .loop-step strong { display: block; margin-bottom: 5px; font-size: 13px; }
    .loop-step span { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .loop-step.current { border-color: var(--accent); background: var(--soft); }
    .loop-step.current::after { content: "NEXT"; position: absolute; top: 9px; right: 9px; color: var(--accent); font-size: 10px; font-weight: 700; }
    .demo-heading { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin: 14px 0 8px; }
    .demo-heading strong { font-size: 13px; }
    .demo-tag { color: var(--accent-2); font-size: 11px; font-weight: 700; }
    .formula-demo { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .formula-card { padding: 11px; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
    .formula-card strong { display: block; font-size: 13px; }
    .formula-card p { margin: 7px 0; color: var(--muted); font-size: 12px; line-height: 1.4; }
    .formula-card small { color: var(--accent); font-size: 11px; font-weight: 700; }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      padding: 8px 11px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 14px;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
    }
    .warn { color: var(--accent-2); }
    pre {
      margin: 0;
      padding: 10px;
      background: #f2f4f1;
      border: 1px solid var(--line);
      border-radius: 4px;
      overflow: auto;
      white-space: pre-wrap;
      font-size: 12px;
      line-height: 1.45;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 7px 6px;
      text-align: left;
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 700; }
    .grid2 {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }
    @media (max-width: 900px) {
      main, .grid2 { grid-template-columns: 1fr; }
      input { min-width: 100%; }
      .loop { grid-template-columns: 1fr 1fr; }
      .formula-demo { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>PVA Formulation Lab <span class="badge">local-only</span></h1>
    <div class="row">
      <label for="outDir">out_dir</label>
      <input id="outDir" value="run_out">
      <button id="refreshBtn">Refresh</button>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>Research Q&amp;A</h2>
        <p class="muted">Searches the local experiment and formulation-memory index only.</p>
        <label for="question">Question</label>
        <textarea id="question" rows="4" placeholder="Which factors were associated with low friction in prior rounds?"></textarea>
        <div class="row"><button id="askBtn">Ask project memory</button><span id="qaStatus" class="muted"></span></div>
        <pre id="answer">Ask a question to search the selected run directory.</pre>
        <div id="sources" class="sources"></div>
      </section>
      <section>
        <h2>Experiment formulation iteration</h2>
        <div class="row">
          <span id="stateBadge" class="badge">loading</span>
          <span id="riskBadge" class="badge">risk</span>
        </div>
        <p id="recommendation"></p>
        <pre id="command"></pre>
      </section>
      <section>
        <h2>Iteration utilities</h2>
        <div id="tools" class="stack"></div>
      </section>
      <section>
        <h2>Recent Artifacts</h2>
        <pre id="artifacts"></pre>
      </section>
    </div>
    <div class="stack">
      <section>
        <h2>Workflow Rounds</h2>
        <div id="rounds"></div>
      </section>
      <div class="grid2">
        <section>
          <h2>Tree Preview</h2>
          <pre id="tree"></pre>
        </section>
        <section>
          <h2>Run Log</h2>
          <pre id="logs"></pre>
        </section>
      </div>
    </div>
  </main>
  <script>
    const outDirInput = document.getElementById("outDir");
    const refreshBtn = document.getElementById("refreshBtn");
    const stateBadge = document.getElementById("stateBadge");
    const riskBadge = document.getElementById("riskBadge");
    const recommendation = document.getElementById("recommendation");
    const command = document.getElementById("command");
    const rounds = document.getElementById("rounds");
    const tools = document.getElementById("tools");
    const artifacts = document.getElementById("artifacts");
    const tree = document.getElementById("tree");
    const logs = document.getElementById("logs");
    const question = document.getElementById("question");
    const askBtn = document.getElementById("askBtn");
    const answer = document.getElementById("answer");
    const sources = document.getElementById("sources");
    const qaStatus = document.getElementById("qaStatus");

    async function getJson(path) {
      const outDir = encodeURIComponent(outDirInput.value || "run_out");
      const sep = path.includes("?") ? "&" : "?";
      const res = await fetch(`${path}${sep}out_dir=${outDir}`);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    async function refresh() {
      refreshBtn.disabled = true;
      try {
        const state = await getJson("/api/state");
        renderState(state);
        const treeData = await getJson("/api/tree");
        renderTree(treeData);
        const logData = await getJson("/api/logs?limit=120");
        renderLogs(logData);
      } catch (err) {
        logs.textContent = String(err);
      } finally {
        refreshBtn.disabled = false;
      }
    }

    function renderState(data) {
      const agent = data.agent || {};
      const st = agent.state || {};
      stateBadge.textContent = st.state || "unknown";
      riskBadge.textContent = agent.risk_level || "unknown";
      recommendation.textContent = `Recommended: ${st.recommended_action || agent.recommended_tool || "inspect"}`;
      command.textContent = agent.command || "";
      artifacts.textContent = (data.recent_artifacts || []).map(a => `${a.name} (${a.size_bytes} bytes)`).join("\n") || "No artifacts";
      renderRounds((data.workflow || {}).statuses || []);
      renderTools(data.low_risk_tools || []);
    }

    function renderRounds(items) {
      if (!items.length) {
        rounds.innerHTML = `<p class="muted">Virtual workflow preview — it will be replaced by your real run data after the first iteration.</p>
          <div class="loop">
            <div class="loop-step current"><strong>01 · Define target</strong><span>Set friction, modulus, materials and process constraints.</span></div>
            <div class="loop-step"><strong>02 · Generate candidates</strong><span>Qwen proposes constrained formulations and rationale.</span></div>
            <div class="loop-step"><strong>03 · Audit &amp; prepare</strong><span>Check feasibility, export DOE and result templates.</span></div>
            <div class="loop-step"><strong>04 · Wet-lab test</strong><span>Collect friction and compression measurements.</span></div>
            <div class="loop-step"><strong>05 · Diagnose evidence</strong><span>Analyze CSVs, failures and candidate trade-offs.</span></div>
            <div class="loop-step"><strong>06 · Iterate or validate</strong><span>Feed evidence into the next round or verify convergence.</span></div>
          </div>
          <div class="demo-heading"><strong>Candidate formulation preview</strong><span class="demo-tag">DEMO ONLY · NOT PROJECT DATA</span></div>
          <div class="formula-demo">
            <article class="formula-card"><strong>Demo-A</strong><p>PVA 10.0 wt% · glycerol 2.0 wt%<br>Freeze–thaw: 2 cycles</p><small>Screening candidate</small></article>
            <article class="formula-card"><strong>Demo-B</strong><p>PVA 12.0 wt% · citrate 0.5 wt%<br>Freeze–thaw: 3 cycles</p><small>Audit pending</small></article>
            <article class="formula-card"><strong>Demo-C</strong><p>PVA 11.0 wt% · PEG 1.0 wt%<br>Freeze–thaw: 1 cycle</p><small>Comparison candidate</small></article>
          </div>`;
        return;
      }
      const rows = items.map(r => `<tr>
        <td>R${r.round}</td>
        <td>${yes(r.candidates)}</td>
        <td>${yes(r.audits)}</td>
        <td>${r.raw_friction_csv_count}</td>
        <td>${yes(r.results_filled)}</td>
        <td>${yes(r.diagnosis)}</td>
        <td>${r.doe_plan_kind}</td>
        <td>${r.recommended_next}</td>
      </tr>`).join("");
      rounds.innerHTML = `<table>
        <thead><tr><th>Round</th><th>Candidates</th><th>Audits</th><th>CSV</th><th>Results</th><th>Diagnosis</th><th>DOE</th><th>Next</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function renderTools(items) {
      tools.innerHTML = "";
      items.filter(t => t.executable && t.name !== "inspect_workspace").forEach(t => {
        const wrap = document.createElement("div");
        wrap.className = "row";
        const btn = document.createElement("button");
        btn.textContent = t.name;
        btn.onclick = () => executeTool(t.name);
        const desc = document.createElement("span");
        desc.textContent = t.description;
        desc.style.color = "var(--muted)";
        wrap.append(btn, desc);
        tools.appendChild(wrap);
      });
    }

    async function executeTool(tool) {
      if (!confirm(`Run low-risk tool: ${tool}?`)) return;
      const res = await fetch("/api/tools/execute", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({tool, out_dir: outDirInput.value || "run_out"})
      });
      const data = await res.json();
      logs.textContent = JSON.stringify(data.result || data, null, 2);
      await refresh();
    }

    async function askQuestion() {
      const prompt = question.value.trim();
      if (!prompt) { question.focus(); return; }
      askBtn.disabled = true;
      qaStatus.textContent = "Searching local memory…";
      try {
        const res = await fetch("/api/qa", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({question: prompt, out_dir: outDirInput.value || "run_out"})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Question failed");
        answer.textContent = data.answer;
        sources.innerHTML = (data.sources || []).map((source, index) => {
          const meta = source.metadata || {};
          const label = meta.candidate_id || meta.case_id || meta.factor_id || source.doc_id;
          return `<div class="source"><strong>${index + 1}. ${label}</strong> · ${source.source_type} · relevance ${source.score}</div>`;
        }).join("") || "<div class=\"source\">No source records matched.</div>";
        qaStatus.textContent = `${data.doc_count || 0} local records indexed`;
      } catch (err) {
        answer.textContent = `Error: ${err}`;
        sources.innerHTML = "";
        qaStatus.textContent = "";
      } finally {
        askBtn.disabled = false;
      }
    }

    function renderTree(data) {
      const file = (data.files || []).find(f => f.exists && f.text_preview) || {};
      tree.textContent = file.text_preview || "No tree report found.";
    }

    function renderLogs(data) {
      const runLog = data.run_log || {};
      logs.textContent = runLog.tail || "No run.log found.";
    }

    function yes(value) {
      return value ? "yes" : "no";
    }

    refreshBtn.onclick = refresh;
    askBtn.onclick = askQuestion;
    question.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") askQuestion();
    });
    refresh();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
