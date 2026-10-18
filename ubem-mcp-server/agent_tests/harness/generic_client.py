"""
A minimal, generic MCP client for the agent tests.

It does what any MCP host does and nothing more: start the server over
stdio, pass the server's instructions and tool list to a model, run the tool
calls the model asks for, and stop when the model answers. The model is
reached through the OpenAI-compatible chat API that most providers and local
runtimes offer (OpenAI, Google Gemini, OpenRouter, Ollama, vLLM, ...):

    UBEM_AGENT_BASE_URL   e.g. https://api.openai.com/v1,
                          https://generativelanguage.googleapis.com/v1beta/openai,
                          http://localhost:11434/v1
    UBEM_AGENT_API_KEY    the provider's key (not needed for local runtimes)
    UBEM_AGENT_MODEL      model name

    python -m agent_tests.harness.generic_client <task_id> <run_dir> [--max-turns 60]
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_tests.harness import common

MAX_RESULT_CHARS = 20000


def openai_chat(base_url: str, api_key: Optional[str], model: str) -> Callable[[List[Dict], List[Dict]], Dict]:
    def call(messages, tools):
        body = json.dumps({"model": model, "messages": messages, "tools": tools}).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 - configured endpoint
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"model API error {e.code}: {e.read()[:300]!r}") from None
    return call


def _tool_result_text(result) -> (str, Optional[bool], Optional[str]):
    data = getattr(result, "structuredContent", None)
    if data is None:
        texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        text = "\n".join(texts)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    else:
        text = json.dumps(data)
    ok = data.get("ok") if isinstance(data, dict) else None
    err = data.get("error_type") if isinstance(data, dict) else None
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
    return text, ok, err


async def run(task_id: str, run_dir: Path, chat: Callable, model_label: str, max_turns: int = 60,
              server_command: Optional[List[str]] = None) -> Dict[str, Any]:
    t = common.task(task_id)
    env = {**os.environ, "UBEM_WORKSPACE_ROOT": str(run_dir), "UBEM_LOG_LEVEL": "WARNING",
           "UBEM_BATCH_MAX_WORKERS": os.getenv("UBEM_BATCH_MAX_WORKERS", "4")}
    cmd = server_command or [sys.executable, "-m", "ubem_mcp_server", "serve"]
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
    transcript: Dict[str, Any] = {"client": "generic-mcp-client", "model": model_label, "task": task_id,
                                  "tool_calls": [], "final_answer": None, "error": None,
                                  "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    start = time.monotonic()
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        tools = (await session.list_tools()).tools
        oa_tools = [{"type": "function", "function": {"name": tl.name, "description": tl.description or "",
                                                      "parameters": tl.inputSchema}} for tl in tools]
        system = ("You are a helpful assistant with tools from MCP servers.\n\n"
                  f"Instructions from the UBEM-MCP server:\n{init.instructions or ''}\n\n{common.CLIENT_NOTE}")
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system},
                                          {"role": "user", "content": t["prompt"]}]
        turns = 0
        try:
            while turns < max_turns:
                turns += 1
                resp = await asyncio.to_thread(chat, messages, oa_tools)
                usage = resp.get("usage") or {}
                for k in ("prompt_tokens", "completion_tokens"):
                    transcript["usage"][k] += usage.get(k, 0) or 0
                msg = resp["choices"][0]["message"]
                messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
                calls = msg.get("tool_calls") or []
                if not calls:
                    transcript["final_answer"] = msg.get("content") or ""
                    break
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    try:
                        result = await session.call_tool(name, args)
                        text, ok, err = _tool_result_text(result)
                    except Exception as e:  # noqa: BLE001 - report to the model like a client would
                        text, ok, err = f"tool call failed: {e}", False, "client"
                    transcript["tool_calls"].append({"name": name, "arguments": args, "ok": ok, "error_type": err})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            else:
                transcript["error"] = f"stopped after {max_turns} turns"
        except Exception as e:  # noqa: BLE001
            transcript["error"] = f"{type(e).__name__}: {e}"
        transcript["turns"] = turns
    transcript["duration_s"] = round(time.monotonic() - start, 1)
    return transcript


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("run_dir")
    ap.add_argument("--max-turns", type=int, default=60)
    a = ap.parse_args()
    base, key, model = (os.getenv("UBEM_AGENT_BASE_URL", "https://api.openai.com/v1"),
                        os.getenv("UBEM_AGENT_API_KEY"), os.getenv("UBEM_AGENT_MODEL"))
    if not model:
        raise SystemExit("set UBEM_AGENT_MODEL (and UBEM_AGENT_BASE_URL, UBEM_AGENT_API_KEY)")
    run_dir = common.prepare(Path(a.run_dir), a.task_id)
    transcript = asyncio.run(run(a.task_id, run_dir, openai_chat(base, key, model), model, a.max_turns))
    (run_dir / "transcript.json").write_text(json.dumps(transcript, indent=1, default=str), encoding="utf-8")
    result = common.score(run_dir, a.task_id, transcript)
    (run_dir / "score.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
