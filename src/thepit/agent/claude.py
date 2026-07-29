"""Calling Claude via the CLI, so it bills against the subscription.

The whole reason this shells out instead of using the `anthropic` package: on
the metered API this project costs roughly $1,400/month at five agents on a
five-minute loop, which is a 17%/yr fee on $100k of capital and would dominate
any result it produced. Through the CLI the marginal cost is zero and the
constraint becomes rate windows, which is a scheduling problem.

The one line that makes it work is popping ANTHROPIC_API_KEY out of the child
environment. If that variable is present the CLI bills per token instead.

Ported from Atlas (`atlas_bot.py::run_claude`), which has been running this way
on Windows for a while. Differences: no tools are granted here (a trading agent
must not have shell or filesystem access), and the process-tree kill is
platform-split.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# Appended to the CLI's own system prompt on every call.
#
# Without this the model answers like a chat assistant: preamble, markdown
# headings, restating the question, a closing summary. A plan came back at 3,839
# characters and a review at 2,371, almost all of it structure rather than
# content. Every one of those tokens is drawn from a shared rate-limit window,
# and none of them reach the execution engine, which reads two JSON fields.
#
# This does not ask for shorter *thinking*. It asks for shorter *output*.
TERSE_SYSTEM = """You are a component inside a trading engine, not a chat assistant.

Output rules, which override any default style:
- No preamble, no sign-off, no restating the question back.
- No markdown headings, no bold, no bullet decoration, unless the requested
  format explicitly uses them.
- When JSON is requested, emit the raw JSON object and nothing else. No prose
  before or after, no code fences.
- Prefer numbers to adjectives. "NVDA +48bp/5m, vol 9.3bp/min" beats "NVDA is
  showing strong momentum with elevated volatility".
- Never explain your reasoning process. State conclusions.
- Respect stated character limits exactly.

Terseness is a hard requirement: your output consumes a shared rate limit, and
the engine reads only the fields it asked for."""


class ClaudeUnavailable(RuntimeError):
    """The CLI is not installed or not reachable."""


@dataclass(frozen=True, slots=True)
class ClaudeResult:
    text: str
    session_id: str | None
    latency_ms: int
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    is_error: bool = False


def find_binary() -> str:
    """Locate the `claude` executable.

    `shutil.which` is tried first but **fails on this Mac**: the binary is not
    on PATH, it lives inside the desktop app bundle at a path containing
    spaces. CLAUDE_CODE_EXECPATH is set by the Claude Code harness and is the
    reliable fallback there. On Windows it is normally `claude.exe` on PATH.
    """
    found = shutil.which("claude")
    if found:
        return found

    exec_path = os.environ.get("CLAUDE_CODE_EXECPATH")
    if exec_path and Path(exec_path).exists():
        return exec_path

    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".claude" / "local" / "claude",
    ):
        if candidate.exists():
            return str(candidate)

    raise ClaudeUnavailable(
        "The `claude` CLI was not found. Install Claude Code and make sure "
        "`claude` is on PATH, or set CLAUDE_CODE_EXECPATH to its full path."
    )


def available() -> bool:
    try:
        find_binary()
        return True
    except ClaudeUnavailable:
        return False


async def ask(
    prompt: str,
    *,
    model: str = "sonnet",
    effort: str = "medium",
    session_id: str | None = None,
    timeout_s: float = 180.0,
    system: str | None = TERSE_SYSTEM,
) -> ClaudeResult:
    """Run one turn. Blocking subprocess, moved off the event loop."""
    return await asyncio.to_thread(
        _run, prompt, model, effort, session_id, timeout_s, system
    )


def _run(
    prompt: str, model: str, effort: str, session_id: str | None, timeout_s: float,
    system: str | None = TERSE_SYSTEM,
) -> ClaudeResult:
    binary = find_binary()

    env = os.environ.copy()
    # THE line. With this present, the CLI bills per token instead of against
    # the subscription.
    env.pop("ANTHROPIC_API_KEY", None)

    args = [binary, "-p", "--output-format", "json"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if session_id:
        args += ["--resume", session_id]
    if system:
        args += ["--append-system-prompt", system]

    # No tools. A trading agent has no business touching a shell or a
    # filesystem, so --dangerously-skip-permissions is deliberately not carried
    # over from Atlas. Text in, JSON out.
    args += ["--disallowed-tools", "Bash,Read,Write,Edit,WebFetch,WebSearch"]

    started = time.monotonic()
    kwargs: dict = {}
    if sys.platform != "win32":
        # Own process group, so the whole tree can be killed on timeout. A bare
        # proc.kill() leaves grandchildren holding the stdout pipe open and the
        # drain then blocks forever.
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, **kwargs,
    )

    try:
        # Prompt on stdin, not argv: a prompt starting with "-" would be parsed
        # as a flag, and long ones blow the command-line length limit.
        out, err = proc.communicate(input=prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", "timed out and would not die"
        return ClaudeResult(
            text=f"timed out after {timeout_s:.0f}s", session_id=session_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd=None, tokens_in=None, tokens_out=None, is_error=True,
        )

    latency_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return ClaudeResult(
            text=(err or out or "")[-800:], session_id=session_id,
            latency_ms=latency_ms, cost_usd=None, tokens_in=None,
            tokens_out=None, is_error=True,
        )

    try:
        data = json.loads(out)
    except ValueError:
        return ClaudeResult(
            text=f"unparseable CLI output: {out[:400]}", session_id=session_id,
            latency_ms=latency_ms, cost_usd=None, tokens_in=None,
            tokens_out=None, is_error=True,
        )

    result_text = data.get("result") or ""

    # The CLI reports "not logged in" as a normal result with is_error set,
    # which buries the one thing the operator needs to know inside a JSON blob.
    # Surface it as what it is: a setup problem with a one-line fix, not a
    # transient API failure to be retried.
    if data.get("is_error") and "not logged in" in result_text.lower():
        raise ClaudeUnavailable(
            "The `claude` CLI is installed but not logged in. Run `claude` in a "
            "terminal and complete /login, then start the session again. "
            "(Having Claude Code open in an app does not authenticate the CLI "
            "for other processes.)"
        )

    usage = data.get("usage") or {}
    return ClaudeResult(
        text=data.get("result") or "",
        session_id=data.get("session_id") or session_id,
        latency_ms=latency_ms,
        # Reported even on the subscription, so "P&L net of token spend" stays
        # measurable as a notional cost.
        cost_usd=data.get("total_cost_usd"),
        tokens_in=usage.get("input_tokens"),
        tokens_out=usage.get("output_tokens"),
        is_error=bool(data.get("is_error")),
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a reply.

    Models wrap JSON in prose and fences however they like, and a strict parse
    would throw away an otherwise good decision over a stray sentence. Tries the
    whole string, then a fenced block, then brace matching.
    """
    text = text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass

    if "```" in text:
        for chunk in text.split("```")[1::2]:
            chunk = chunk.removeprefix("json").strip()
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue

    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None
