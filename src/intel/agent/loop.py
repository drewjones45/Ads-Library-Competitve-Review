"""Agent loop — Claude plans, calls tools, writes the briefing.

Single-prompt entry. Caller gives a goal (or one of the canned daily routines).
The model decides which tools to call, in what order, and stops when satisfied.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import anthropic

from ..config import load_settings
from .tools import TOOLS, dispatch


AGENT_SYSTEM = """You are an agentic competitive intelligence analyst.

You have tools for ingesting competitor sources, querying the local intel store, running
vision/text analysis on creative, clustering hook themes, and writing briefings.

Operating principles:
- Be data-driven. NEVER invent ads, prices, page names, or competitors that aren't returned by a tool.
- Pull fresh data only when needed. If the user asks 'what changed today,' ingest first; if they ask
  about the existing corpus, query without re-ingesting.
- When generating a briefing, prefer the `generate_briefing` tool over composing one yourself —
  it standardizes format and persists the result.
- Be concise in your own messages. The user will read your final answer; intermediate tool calls
  are operational. Don't narrate every step.
- If a tool returns an error, decide whether to retry, fall back to another tool, or report the
  limitation. Never silently ignore.
- Stop calling tools when you have enough to answer.
"""


def run_agent(
    goal: str,
    *,
    model: str | None = None,
    max_iters: int = 15,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the tool-use loop until the model stops calling tools or max_iters hit.
    Returns {'final_text': str, 'tool_calls': [...], 'stop_reason': str}."""
    settings = load_settings()
    model = model or settings.reasoning_model
    client = anthropic.Anthropic()

    messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
    tool_calls_log: list[dict[str, Any]] = []
    final_text = ""
    stop_reason = "max_iters"

    for _ in range(max_iters):
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=AGENT_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Capture any assistant text from this turn.
        assistant_text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if assistant_text and log:
            log(f"[assistant] {assistant_text}")

        # Always append assistant turn before responding to tool_use blocks.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            final_text = assistant_text
            stop_reason = resp.stop_reason or "end_turn"
            break

        # Gather every tool_use in this turn, dispatch each, return as one user turn.
        tool_results: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            name = block.name
            args = block.input or {}
            if log:
                log(f"[tool→] {name}({json.dumps(args, default=str)[:200]})")
            result = dispatch(name, args)
            tool_calls_log.append({"name": name, "input": args, "output_keys": list(result.keys())[:8] if isinstance(result, dict) else None})
            # Truncate gigantic tool outputs so they don't blow context.
            serialized = json.dumps(result, default=str)
            if len(serialized) > 60000:
                serialized = serialized[:60000] + f'\n... [truncated, {len(serialized)-60000} chars]'
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": serialized,
            })

        messages.append({"role": "user", "content": tool_results})

    return {
        "final_text": final_text,
        "tool_calls": tool_calls_log,
        "stop_reason": stop_reason,
    }
