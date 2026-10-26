"""
Run an agent task in Claude Code (headless) against UBEM-MCP in a container.

Runs on the host, with the standard library only. Claude Code gets the
UBEM-MCP server and nothing else: no built-in tools (--tools ""), no other
MCP servers, no settings files, a fresh empty working folder (so no project
memory) and no saved session. The task workspace and scoring live in the
container, through `docker exec`.

    python agent_tests/harness/claude_code.py <task_id> [--model sonnet] [--container ubem-p6] [--repeat 1]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_IN_CONTAINER = "/workspace/ubem-mcp-server"
NOTE = ("Files the user mentions are in the working folder of the UBEM-MCP tools; refer to them by file "
        "name. Answer the user's questions directly and give the numbers they ask for.")


def dexec(container, *cmd, input=None):
    return subprocess.run(["docker", "exec", "-i", "-w", REPO_IN_CONTAINER, container, *cmd], input=input,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def parse_stream(lines):
    """Tool calls, final answer and usage from Claude Code's stream-json output."""
    t = {"client": "claude-code", "model": None, "tool_calls": [], "final_answer": None, "error": None,
         "usage": None, "turns": None, "used_guide": False}
    pending = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            t["model"] = ev.get("model")
            t["mcp_servers"] = ev.get("mcp_servers")
        elif ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = block["name"]
                    server, _, short = name.partition("__")[2].partition("__") if name.startswith("mcp__") \
                        else ("builtin", "", name)
                    call = {"name": short or name, "server": server, "arguments": block.get("input"), "ok": None,
                            "error_type": None}
                    if short in ("ListMcpResourcesTool", "ReadMcpResourceTool") or "guides" in json.dumps(
                            block.get("input") or {}):
                        t["used_guide"] = True
                    pending[block["id"]] = call
                    t["tool_calls"].append(call)
        elif ev.get("type") == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call = pending[block["tool_use_id"]]
                    content = block.get("content")
                    text = content if isinstance(content, str) else " ".join(
                        c.get("text", "") for c in content or [] if isinstance(c, dict))
                    try:
                        data = json.loads(text)
                        call["ok"], call["error_type"] = data.get("ok"), data.get("error_type")
                    except (ValueError, AttributeError):
                        call["ok"] = False if block.get("is_error") else None
        elif ev.get("type") == "result":
            t["final_answer"] = ev.get("result")
            t["usage"] = ev.get("usage")
            t["turns"] = ev.get("num_turns")
            t["cost_usd"] = ev.get("total_cost_usd")
            if ev.get("is_error") or ev.get("subtype") != "success":
                t["error"] = ev.get("subtype")
    return t


def run_task(task_id, model, container, max_turns, label, timeout=3600):
    run_name = f"claude-code_{label}_{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = f"{REPO_IN_CONTAINER}/agent_tests/results/runs/{run_name}"
    prep = dexec(container, "/tmp/venv/bin/python", "-m", "agent_tests.harness.common", "prepare", run_dir, task_id)
    if prep.returncode:
        raise SystemExit(prep.stderr)
    prompt = dexec(container, "/tmp/venv/bin/python", "-m", "agent_tests.harness.common", "prompt",
                   task_id).stdout.strip()
    mcp = {"mcpServers": {"ubem": {"command": "docker", "args": [
        "exec", "-i", "-w", REPO_IN_CONTAINER, "-e", f"UBEM_WORKSPACE_ROOT={run_dir}", "-e", "UBEM_LOG_LEVEL=WARNING",
        "-e", "UBEM_BATCH_MAX_WORKERS=4", container, "/tmp/venv/bin/python", "-m", "ubem_mcp_server", "serve"]}}}
    with tempfile.TemporaryDirectory(prefix="ubem_agent_") as cwd:
        cfg = Path(cwd) / "mcp.json"
        cfg.write_text(json.dumps(mcp))
        cmd = [shutil.which("claude") or "claude", "-p", prompt, "--mcp-config", str(cfg), "--strict-mcp-config", "--tools", "",
               "--allowedTools", "mcp__ubem", "--setting-sources", "", "--no-session-persistence",
               "--append-system-prompt", NOTE, "--output-format", "stream-json", "--verbose",
               "--max-turns", str(max_turns)]
        if model:
            cmd += ["--model", model]
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            # Keep and score what the run produced before the time limit.
            def text(b):
                return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
            stdout, stderr, code = text(e.stdout), text(e.stderr), None
        duration = round(time.monotonic() - start, 1)
    transcript = parse_stream(stdout.splitlines())
    transcript.update({"task": task_id, "duration_s": duration, "prompt": prompt})
    if code is None:
        transcript["error"] = f"timeout after {timeout} s"
    elif code and not transcript["error"]:
        transcript["error"] = f"exit {code}: {stderr[-500:]}"
    local = Path(__file__).resolve().parents[1] / "results" / "runs" / run_name
    local.mkdir(parents=True, exist_ok=True)
    (local / "transcript.json").write_text(json.dumps(transcript, indent=1), encoding="utf-8")
    (local / "stream.jsonl").write_text(stdout, encoding="utf-8")
    score = dexec(container, "/tmp/venv/bin/python", "-m", "agent_tests.harness.common", "check", run_dir, task_id,
                  f"{run_dir}/transcript.json")
    if score.returncode:
        raise SystemExit(score.stderr)
    (local / "score.json").write_text(score.stdout, encoding="utf-8")
    return json.loads(score.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--model", default=None)
    ap.add_argument("--label", default=None, help="name for the model in result files (default: --model)")
    ap.add_argument("--container", default="ubem-p6")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=3600, help="seconds before the client is stopped")
    a = ap.parse_args()
    for _ in range(a.repeat):
        for task_id in a.tasks:
            s = run_task(task_id, a.model, a.container, a.max_turns, a.label or a.model or "default", a.timeout)
            print(json.dumps({k: s[k] for k in ("task", "model", "passed", "checks_passed", "checks_total",
                                                "tool_calls", "failed_tool_calls", "used_guide", "duration_s")}),
                  flush=True)


if __name__ == "__main__":
    main()
