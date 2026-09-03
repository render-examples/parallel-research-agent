"""Render Workflow tasks for the Parallel research agent.

Four tasks, arranged as a fan-out / fan-in pipeline:

  research_agent  — orchestrator: plans, fans out, fans in
  plan_research   — splits the question into independent sub-questions
  investigate     — Claude tool-use loop over Parallel Search and Extract
  synthesize      — reconciles branch findings into one cited report

Each `investigate` branch runs as its own workflow run on its own instance.
A rate limit or timeout in one branch retries that branch alone instead of
restarting the whole investigation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import anthropic
from parallel import Parallel
from render import Retry, TaskContext, Workflows

from shared.formatters import format_extract_results, format_search_results

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=5000, backoff_scaling=2.0),
    default_timeout=300,
)

# ---------------------------------------------------------------------------
# Tool definitions — these describe the Parallel APIs to Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "parallel_search",
        "description": (
            "Search the live web for information. Returns ranked excerpts "
            "from relevant pages, optimized for LLM consumption. Use this "
            "to find facts, sources, and leads. You can call it multiple "
            "times with different queries to triangulate a topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": (
                        "A natural-language description of what you are "
                        "trying to find. Be specific about the angle."
                    ),
                },
                "search_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "2-4 keyword search queries. Each should target "
                        "a different facet of the objective."
                    ),
                },
            },
            "required": ["objective", "search_queries"],
        },
    },
    {
        "name": "parallel_extract",
        "description": (
            "Extract clean, readable content from specific URLs. Use this "
            "after searching to read full articles, documentation, or "
            "reports that looked promising in the search excerpts. Handles "
            "JavaScript-rendered pages and PDFs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs to extract content from (max 5).",
                },
                "objective": {
                    "type": "string",
                    "description": (
                        "What to focus on when extracting. Helps Parallel "
                        "prioritize relevant sections."
                    ),
                },
            },
            "required": ["urls"],
        },
    },
]

PLANNER_PROMPT = """\
You are a research planner. Break the user's question into independent \
sub-questions that can each be researched on their own, at the same time.

Rules:
- Produce 3-5 sub-questions. Fewer if the question is narrow.
- Each must stand alone. A researcher assigned one sub-question will not \
see the findings of the others, so none may depend on another's answer.
- Cover distinct facets — different angles, timeframes, stakeholders, or \
competing viewpoints. Do not restate the same question in other words.
- Together they should cover everything needed to answer the original.

Output a JSON array of strings and nothing else.\
"""

RESEARCH_SYSTEM_PROMPT = """\
You are a research analyst investigating one specific sub-question that is \
part of a larger inquiry. Other analysts are covering the other parts, so \
stay on your assigned sub-question — do not try to answer the whole thing.

Approach:
1. Start with a broad search to map what exists.
2. Read the most relevant results using extract.
3. Follow up with targeted searches to fill gaps or verify claims.
4. When you have enough material, write up what you found.

Your output is raw material for a synthesis step, not a finished report:
- Lead with the direct answer to your sub-question.
- State each substantive finding with its source as [Source Title](URL).
- Quote specific figures, dates, and names rather than summarizing loosely.
- Flag where sources disagree, and say which you find more credible and why.
- Say plainly what you could not determine. Do not fill gaps with guesses.\
"""

SYNTHESIS_PROMPT = """\
You are a research editor. Several analysts investigated different facets of \
one question in parallel. Turn their findings into a single cited report.

Your job is reconciliation, not summarizing:
- Merge overlapping findings. The same source may appear in several briefs; \
cite it once.
- Where analysts contradict each other, surface the conflict explicitly and \
weigh the sources rather than silently picking one.
- Note where coverage is thin or a sub-question came back unresolved.
- Add no information that is not in the findings.

Report format:
- Lead with a concise summary that answers the original question in 2-3 \
sentences.
- Organize findings under clear headings.
- Cite sources inline as [Source Title](URL).
- End with a "Sources" list of every URL referenced.

Output only the report.\
"""

MAX_AGENT_TURNS = 6
MAX_SUB_QUESTIONS = 5
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "claude-haiku-4-5-20251001")
SEARCH_MODE = os.getenv("PARALLEL_SEARCH_MODE", "fast")


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _execute_tool(parallel: Parallel, name: str, input_data: dict) -> str:
    """Call the appropriate Parallel API and return formatted text."""
    if name == "parallel_search":
        result = parallel.search(
            objective=input_data["objective"],
            search_queries=input_data["search_queries"],
            mode=SEARCH_MODE,
        )
        return format_search_results(result)

    if name == "parallel_extract":
        urls = input_data["urls"][:5]
        result = parallel.extract(
            urls=urls,
            objective=input_data.get("objective", ""),
        )
        return format_extract_results(result)

    return f"Unknown tool: {name}"


def _text_of(response) -> str:
    """Concatenate the text blocks of a Claude response."""
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def _write_up(claude: anthropic.Anthropic, messages: list[dict]) -> str:
    """Force a write-up from a branch that ran out of research turns.

    Without this a branch that spends every turn calling tools returns empty
    findings — it burns the budget and contributes nothing to synthesis. The
    call omits `tools` so the only move left is to answer.
    """
    nudge = {
        "type": "text",
        "text": (
            "You are out of research turns. Write up your findings now, "
            "using only what you have already gathered and following your "
            "output instructions. Do not request more tools."
        ),
    }

    # Anthropic requires tool_result blocks to lead their message, so append
    # the nudge to the trailing user turn rather than starting a new one.
    last = messages[-1] if messages else None
    if last and last["role"] == "user" and isinstance(last["content"], list):
        messages = messages[:-1] + [
            {"role": "user", "content": [*last["content"], nudge]}
        ]
    else:
        messages = [*messages, {"role": "user", "content": [nudge]}]

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=RESEARCH_SYSTEM_PROMPT,
        messages=messages,
    )
    return _text_of(response)


def _parse_sub_questions(text: str, fallback: str) -> list[str]:
    """Pull a JSON array of sub-questions out of the planner's reply.

    Falls back to the original query as a single branch, which degrades the
    pipeline to the sequential behavior rather than failing the run.
    """
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            questions = [str(q).strip() for q in parsed if str(q).strip()]
            if questions:
                return questions[:MAX_SUB_QUESTIONS]

    return [fallback]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@app.task(timeout_seconds=900)
async def research_agent(ctx: TaskContext, query: str) -> dict:
    """Plan a research strategy, investigate each thread in parallel, synthesize.

    The fan-out is the point: every sub-question becomes its own workflow
    run with its own retry budget, so one failed branch costs one branch.
    """
    sub_questions = await ctx.run(plan_research, query)

    outcomes = await asyncio.gather(
        *[ctx.run(investigate, query, sub_question) for sub_question in sub_questions],
        return_exceptions=True,
    )

    # A branch that exhausted its retries shouldn't sink the whole run —
    # synthesize whatever came back and report the shortfall.
    branches = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    if not branches:
        raise RuntimeError(
            f"All {len(sub_questions)} research branches failed for: {query}"
        )

    report = await ctx.run(synthesize, query, branches)
    report["sub_questions"] = sub_questions
    report["branches_completed"] = len(branches)
    report["branches_failed"] = len(outcomes) - len(branches)
    report["tool_calls_made"] = sum(branch["tool_calls"] for branch in branches)
    report["agent_turns"] = sum(branch["turns"] for branch in branches)
    return report


@app.task(timeout_seconds=60)
def plan_research(ctx: TaskContext, query: str) -> list[str]:
    """Split the question into independent, parallelizable sub-questions."""
    claude = anthropic.Anthropic()

    response = claude.messages.create(
        model=PLANNER_MODEL,
        max_tokens=1024,
        system=PLANNER_PROMPT,
        messages=[{"role": "user", "content": query}],
    )

    return _parse_sub_questions(_text_of(response), fallback=query)


@app.task(
    timeout_seconds=300,
    retry=Retry(max_retries=3, wait_duration_ms=5000, backoff_scaling=2.0),
)
def investigate(ctx: TaskContext, query: str, sub_question: str) -> dict:
    """Research one sub-question with a multi-turn Claude tool loop.

    This is the branch body of the fan-out. It runs the same adaptive
    search-read-follow-up loop over Parallel Search and Extract, scoped to
    a single facet of the question.
    """
    claude = anthropic.Anthropic()
    parallel = Parallel()

    messages = [
        {
            "role": "user",
            "content": (
                f"Overall research question: {query}\n\n"
                f"Your assigned sub-question: {sub_question}"
            ),
        }
    ]
    findings = ""
    tool_calls = 0
    turns = 0

    for turn in range(MAX_AGENT_TURNS):
        turns = turn + 1
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=RESEARCH_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Claude finished — no more tool calls
        if response.stop_reason == "end_turn":
            findings = _text_of(response)
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_calls += 1
            output = _execute_tool(parallel, block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )

        # Claude stopped for some other reason (hitting max_tokens, say)
        # without requesting a tool. Sending an empty user turn back is a
        # 400, so stop here and let the write-up below salvage the work.
        if not tool_results:
            break

        messages.append({"role": "user", "content": tool_results})

    if not findings:
        findings = _write_up(claude, messages)
        turns += 1

    return {
        "sub_question": sub_question,
        "findings": findings,
        "tool_calls": tool_calls,
        "turns": turns,
    }


@app.task(timeout_seconds=120)
def synthesize(ctx: TaskContext, query: str, branches: list[dict]) -> dict:
    """Merge parallel branch findings into one reconciled report."""
    claude = anthropic.Anthropic()

    briefs = "\n\n".join(
        f"--- Sub-question {i}: {branch['sub_question']} ---\n"
        f"{branch['findings'] or '(no findings returned)'}"
        for i, branch in enumerate(branches, 1)
    )

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=SYNTHESIS_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Original research question: {query}\n\n"
                    f"Findings from {len(branches)} parallel analysts:\n\n{briefs}"
                ),
            }
        ],
    )

    report_text = _text_of(response)
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)>\]]+", report_text)))

    return {
        "query": query,
        "report": report_text,
        "sources": urls[:20],
        "status": "complete",
    }
