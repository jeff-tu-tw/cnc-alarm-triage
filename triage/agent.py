"""The triage agent: a plain tool-use loop over the DeepSeek API.

Usage:
    python -m triage.agent "Why does M03 keep throwing SP-101?"
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .tools import TOOL_SPECS, ToolRunner

SYSTEM_PROMPT = """You are an alarm triage assistant for maintenance engineers at a CNC machining plant.
You answer questions about machine alarms, maintenance history and tooling using the tools provided.

How to work:
- Ground every claim in data from the tools. Never invent numbers, dates, IDs or events.
- If you are unsure where data lives, call get_schema first.
- For "why" / root-cause questions, work like an experienced reliability engineer:
  1. Quantify the pattern: when did it start, how often, which machines, which shift or axis.
  2. Compare against a baseline: the same machine before the change, and similar machines (same model) over the same period.
  3. Look for what changed around the onset: maintenance plans (pm_plan), work orders, tool changes, notes.
  4. Check the manual for the alarm code's known causes.
  5. Consider common-mode causes: if many machines alarm at the same moment, suspect something shared (power, site, supplier).
- Separate evidence from hypothesis and state your confidence (high / medium / low).
- Cite evidence inline with record IDs: work orders (WO-...), PM plans (PM-...), tool changes (TC-...), alarm codes, or counts from your queries.
- If the data cannot answer the question (metric not recorded, machine does not exist, time outside the data range), say so plainly and say what data would be needed. Do not guess.
  If you offer a related metric that does exist (e.g. alarm counts instead of a sensor reading), label it as a proxy and apply the baseline rule below before drawing any conclusion from it.
- Before calling something a trend, rise or anomaly, compare it against a baseline: other machines (same model first) over the same period, or the same machine's earlier history. If it is within the range of that baseline, say it looks like normal background variation.
- Single-digit counts per month, or averages over one or two events, are too small to show a trend. Report them as numbers, not as a pattern, and do not recommend action on them alone.

Answer format:
- Simple factual questions: answer directly in a few sentences with the number or fact.
- Root-cause questions: use these sections — Summary, Evidence, Likely cause (with confidence), Recommended next steps.
- Reply in the same language as the user's question (English or Traditional Chinese)."""

REWRITE_PROMPT = ("Your answer was cut off by the output length limit. Rewrite the complete answer more concisely, "
                  "keeping the conclusion, the key evidence with record IDs, and the next steps.")
TRUNCATION_WARNING = "\n\n⚠️ [Answer truncated by the output length limit / 答案因長度上限被截斷，內容不完整]"
# Look-alike hyphens the model sometimes emits (e.g. "WO‑260140" with U+2011). Mapped to ASCII so record IDs
# can be copied into the CMMS or searched. En and em dashes are left alone: they are punctuation, not hyphens.
_HYPHENS = str.maketrans({c: "-" for c in "‐‑‒−﹣－"})


@dataclass
class Step:
    tool: str
    input: dict
    output: str
    is_error: bool
    latency_ms: int


@dataclass
class AgentResult:
    question: str
    answer: str
    steps: list[Step] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0  # part of output_tokens, if the API reports it
    latency_ms: int = 0
    hit_step_limit: bool = False
    truncated: bool = False  # final answer was still cut off by max_tokens after one rewrite
    finish_reasons: list[str] = field(default_factory=list)  # one per model call
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class TriageAgent:
    def __init__(self, runner: ToolRunner | None = None, client=None, model: str = config.MODEL,
                 max_steps: int = config.MAX_STEPS, trace_path: Path | None = config.TRACE_PATH):
        self.runner = runner or ToolRunner()
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=config.API_KEY,
                base_url=config.BASE_URL,
            )
        self.client = client
        self.model = model
        self.max_steps = max_steps
        self.trace_path = trace_path

    def _tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["input_schema"],
                },
            }
            for spec in TOOL_SPECS
        ]

    def ask(self, question: str, history: list[dict] | None = None) -> AgentResult:
        """history: prior turns as [{"role": "user"|"assistant", "content": str}, ...]"""
        t0 = time.monotonic()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(dict(message) for message in (history or []))
        messages.append({"role": "user", "content": question})
        result = AgentResult(question=question, answer="", model=self.model)
        rewriting = False

        for step_no in range(self.max_steps + 1):
            final_turn = step_no == self.max_steps
            if final_turn:
                result.hit_step_limit = True
                messages.append({
                    "role": "user",
                    "content": "Step limit reached. Answer now with the evidence gathered so far and say what remains unverified.",
                })

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self._tools(),
                tool_choice="none" if final_turn or rewriting else "auto",
                max_tokens=2048,
            )
            usage = getattr(response, "usage", None)
            result.input_tokens += getattr(usage, "prompt_tokens", 0) or 0
            result.output_tokens += getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "completion_tokens_details", None)
            result.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise RuntimeError("DeepSeek returned no response choices.")

            message = choices[0].message
            finish_reason = getattr(choices[0], "finish_reason", None) or ""
            result.finish_reasons.append(finish_reason)
            tool_calls = getattr(message, "tool_calls", None) or []
            messages.append(message.model_dump(exclude_none=True))

            if not tool_calls:
                answer = (getattr(message, "content", None) or "").strip().translate(_HYPHENS)
                if finish_reason == "length":
                    # Never hand back a silently cut-off answer: ask for one shorter rewrite, then flag it.
                    if not rewriting and not final_turn:
                        rewriting = True
                        messages.append({"role": "user", "content": REWRITE_PROMPT})
                        continue
                    result.truncated = True
                    answer += TRUNCATION_WARNING
                if not answer:
                    raise RuntimeError("DeepSeek response contained neither text nor a tool call.")
                result.answer = answer
                break

            for tool_call in tool_calls:
                arguments = json.loads(tool_call.function.arguments or "{}")
                start = time.monotonic()
                output = self.runner.execute(tool_call.function.name, arguments)
                result.steps.append(Step(
                    tool=tool_call.function.name,
                    input=arguments,
                    output=output.content,
                    is_error=output.is_error,
                    latency_ms=int((time.monotonic() - start) * 1000),
                ))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output.content,
                })

        result.latency_ms = int((time.monotonic() - t0) * 1000)
        self._trace(result)
        return result

    def _trace(self, result: AgentResult) -> None:
        if not self.trace_path:
            return
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **result.to_dict()},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass  # tracing must never break an answer


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python -m triage.agent "your question"')
        sys.exit(1)
    res = TriageAgent().ask(" ".join(sys.argv[1:]))
    for step in res.steps:
        print(f"  -> {step.tool}({json.dumps(step.input, ensure_ascii=False)[:120]})"
              f"{' [error]' if step.is_error else ''}")
    print(f"\n{res.answer}\n")
    print(f"[{len(res.steps)} tool calls, {res.input_tokens}+{res.output_tokens} tokens, {res.latency_ms} ms]")


if __name__ == "__main__":
    main()
