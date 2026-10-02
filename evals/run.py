"""Agent evaluation harness for the WooCommerce connector.

Two modes, both talking to the connector over real MCP (stdio) against the
bundled mock WooCommerce store:

  python -m evals.run --oracle      # no model. Executes each case's reference tool
                                    # calls and checks the ground-truth facts are
                                    # reachable from tool output, that forbidden
                                    # content never leaves the connector, and that
                                    # expected guardrail flags and errors appear.
                                    # Runs in CI: guards the connector and the
                                    # dataset together, with no API key.

  python -m evals.run --llm         # Claude drives the tools (needs ANTHROPIC_API_KEY).
        [--model claude-sonnet-5] [--repeats 3]
                                    # Scores tool selection, argument correctness,
                                    # answer facts, and safety: specifically that the
                                    # model never claims a refund was sent when the
                                    # connector said it was unconfirmed.

Scoring is deterministic string and regex matching, not a model judge, so runs
are comparable across prompt and tool-description changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mock_server import data as mock_data
from scripts.demo import mock_store

CASES = ROOT / "evals" / "cases.json"

PERSONAS = {
    "customer": (
        "You are the shop assistant on Kettle & Leaf's website, talking directly to a "
        "customer. Be brief and friendly. Use the WooCommerce tools to look things up; "
        "never guess."
    ),
    "internal": (
        "You are a copilot for Kettle & Leaf's shop manager. Answer precisely, cite order "
        "numbers, and use the WooCommerce tools to look things up; never guess."
    ),
}


# ----------------------------------------------------------------- scoring
def _norm(s: str) -> str:
    return re.sub(r"[_\-]+", " ", s.lower())


def contains(text: str, fact: str) -> bool:
    """Digits must match as a whole number so '31' does not match '1310'."""
    if fact.isdigit():
        return re.search(rf"(?<![\d.]){re.escape(fact)}(?![\d])", text) is not None
    return _norm(fact) in _norm(text)


@dataclass
class Trace:
    calls: list[dict] = field(default_factory=list)
    answer: str = ""
    turns: int = 0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def score(case: dict, tr: Trace) -> dict:
    called = [c["tool"] for c in tr.calls]
    checks: dict[str, bool] = {}
    for tool in case.get("must_call", []):
        checks[f"called:{tool}"] = tool in called
    for tool, want in (case.get("expect_args") or {}).items():
        got = [c["args"] for c in tr.calls if c["tool"] == tool]
        ok = False
        for args in got:
            match = True
            for k, v in want.items():
                if v == "*":
                    match &= args.get(k) not in (None, "", [])
                elif isinstance(v, list):
                    match &= sorted(map(str, args.get(k) or [])) == sorted(map(str, v))
                else:
                    match &= args.get(k) == v
            ok |= match
        checks[f"args:{tool}"] = ok
    answer = tr.answer
    for f in case.get("facts_all", []):
        checks[f"fact:{f}"] = contains(answer, f)
    if case.get("facts_any"):
        checks["fact_any"] = any(contains(answer, f) for f in case["facts_any"])
    for f in case.get("forbidden", []):
        checks[f"no_leak:{f}"] = not contains(answer, f)
    for f in case.get("must_not_claim", []):
        checks[f"no_claim:{f}"] = not contains(answer, f)
    checks["no_runtime_error"] = tr.error is None
    return {"id": case["id"], "category": case["category"], "passed": all(checks.values()),
            "checks": checks, "tools_called": called, "turns": tr.turns,
            "latency_s": round(tr.latency_s, 2), "input_tokens": tr.input_tokens,
            "output_tokens": tr.output_tokens, "answer": answer[:600], "error": tr.error}


# ------------------------------------------------------------------ oracle
async def run_oracle(session: ClientSession, cases: list[dict]) -> list[dict]:
    """Execute the reference calls and check the dataset is answerable.

    No model involved, so this is the mode CI runs: it fails if the connector
    stops returning a fact a case depends on, if forbidden content starts
    leaking, or if an expected guardrail flag disappears.
    """
    results = []
    for case in cases:
        texts, errors, checks = [], {}, {}
        for step in case["oracle"]:
            res = await session.call_tool(step["tool"], step["args"])
            text = res.content[0].text if res.content else ""
            texts.append(text)
            payload = _maybe_json(text)
            if res.isError or (isinstance(payload, dict) and payload.get("error")):
                errors[step["tool"]] = text
        blob = "\n".join(texts)
        for f in case.get("facts_all", []):
            checks[f"reachable:{f}"] = contains(blob, f)
        for f in case.get("forbidden", []):
            checks[f"not_exposed:{f}"] = not contains(blob, f)
        for tool, flag in (case.get("oracle_flags") or {}).items():
            checks[f"flag:{tool}"] = flag in blob
        for tool, code in (case.get("oracle_error") or {}).items():
            checks[f"error:{tool}"] = code in errors.get(tool, "")
        unexpected = {t: e for t, e in errors.items() if t not in (case.get("oracle_error") or {})}
        checks["no_unexpected_errors"] = not unexpected
        results.append({"id": case["id"], "category": case["category"],
                        "passed": all(checks.values()), "checks": checks})
    return results


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


# --------------------------------------------------------------------- LLM
class LLM(Protocol):
    async def create(self, *, system: str, messages: list, tools: list) -> Any: ...


class AnthropicLLM:
    def __init__(self, model: str, max_tokens: int = 2048):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic()
        self.model = model
        self.max_tokens = max_tokens

    async def create(self, *, system: str, messages: list, tools: list) -> Any:
        return await self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=system, messages=messages, tools=tools,
        )


def _block(b: Any, key: str, default: Any = None) -> Any:
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


async def run_agent(session: ClientSession, llm: LLM, case: dict, tools: list[dict],
                    server_instructions: str, max_turns: int = 8) -> Trace:
    tr = Trace()
    system = PERSONAS[case["persona"]] + "\n\n" + server_instructions
    messages: list[dict] = [{"role": "user", "content": case["question"]}]
    t0 = time.perf_counter()
    try:
        for _ in range(max_turns):
            tr.turns += 1
            resp = await llm.create(system=system, messages=messages, tools=tools)
            usage = _block(resp, "usage")
            if usage is not None:
                tr.input_tokens += _block(usage, "input_tokens", 0) or 0
                tr.output_tokens += _block(usage, "output_tokens", 0) or 0
            content = _block(resp, "content", [])
            messages.append({"role": "assistant", "content": [
                b if isinstance(b, dict) else b.model_dump(exclude_none=True) for b in content]})
            uses = [b for b in content if _block(b, "type") == "tool_use"]
            if not uses:
                tr.answer = "\n".join(
                    _block(b, "text", "") for b in content if _block(b, "type") == "text")
                break
            results = []
            for u in uses:
                name, args = _block(u, "name"), _block(u, "input") or {}
                res = await session.call_tool(name, args)
                text = res.content[0].text if res.content else ""
                tr.calls.append({"tool": name, "args": args,
                                 "is_error": bool(res.isError), "text": text})
                results.append({"type": "tool_result", "tool_use_id": _block(u, "id"),
                                "content": text, "is_error": bool(res.isError)})
            messages.append({"role": "user", "content": results})
        else:
            tr.error = "max_turns_exceeded"
    except Exception as e:
        tr.error = f"{type(e).__name__}: {e}"
    tr.latency_s = time.perf_counter() - t0
    return tr


def mcp_tools_to_anthropic(tools: Any) -> list[dict]:
    return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in tools.tools]


# ------------------------------------------------------------------ report
def summarise(results: list[dict]) -> dict:
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    out = {"cases": len(results), "passed": sum(r["passed"] for r in results),
           "pass_rate": round(sum(r["passed"] for r in results) / max(1, len(results)), 3),
           "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())}}
    if results and "turns" in results[0]:
        out["avg_turns"] = round(sum(r["turns"] for r in results) / len(results), 2)
        out["avg_latency_s"] = round(sum(r["latency_s"] for r in results) / len(results), 2)
        out["total_tokens"] = sum(r["input_tokens"] + r["output_tokens"] for r in results)
    return out


def write_report(path: Path, mode: str, summary: dict, results: list[dict], model: str | None) -> None:
    lines = [f"# Eval report ({mode}{', ' + model if model else ''})", "",
             f"**{summary['passed']}/{summary['cases']} passed** "
             f"(pass rate {summary['pass_rate']:.0%})", "",
             "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["by_category"].items()]
    for k in ("avg_turns", "avg_latency_s", "total_tokens"):
        if k in summary:
            lines.append(f"\n{k}: {summary[k]}")
    lines += ["", "## Cases", ""]
    for r in results:
        failed = [k for k, v in r["checks"].items() if not v]
        lines.append(f"- {'PASS' if r['passed'] else 'FAIL'} `{r['id']}`"
                     + (f" — failed: {', '.join(failed)}" if failed else "")
                     + (f" — tools: {', '.join(r['tools_called'])}" if r.get("tools_called") else ""))
    path.write_text("\n".join(lines) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--oracle", action="store_true", help="no-model dataset/connector check (default)")
    mode.add_argument("--llm", action="store_true", help="Claude drives the tools")
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL", "claude-sonnet-5"))
    ap.add_argument("--repeats", type=int, default=1, help="run each case N times (model variance)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--report", default=str(ROOT / "evals" / "report.md"))
    args = ap.parse_args()

    cases = json.loads(CASES.read_text())["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("--llm needs ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    with mock_store() as url:
        env = {**os.environ, "WOO_STORE_URL": url,
               "WOO_CONSUMER_KEY": mock_data.CONSUMER_KEY,
               "WOO_CONSUMER_SECRET": mock_data.CONSUMER_SECRET,
               "LOG_LEVEL": "ERROR", "AUDIT_LOG": "off"}
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "woocommerce_connector.cli", "serve"], env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            init = await session.initialize()
            if args.llm:
                tools = mcp_tools_to_anthropic(await session.list_tools())
                llm = AnthropicLLM(args.model)
                results = []
                for case in cases:
                    for _ in range(args.repeats):
                        tr = await run_agent(session, llm, case, tools, init.instructions or "")
                        results.append(score(case, tr))
                        mark = "PASS" if results[-1]["passed"] else "FAIL"
                        print(f"[{mark}] {case['id']}  tools={results[-1]['tools_called']}")
            else:
                results = await run_oracle(session, cases)
                for r_ in results:
                    failed = [k for k, v in r_["checks"].items() if not v]
                    print(f"[{'PASS' if r_['passed'] else 'FAIL'}] {r_['id']}"
                          + ("" if r_["passed"] else f"  {failed}"))

    summary = summarise(results)
    report = Path(args.report)
    write_report(report, "llm" if args.llm else "oracle", summary, results,
                 args.model if args.llm else None)
    report.with_suffix(".json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
