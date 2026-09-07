# Render

> Use Parallel with Render

Build durable AI agents that search and extract the live web using Parallel, orchestrated by [Render Workflows](https://render.com/docs/workflows?utm_source=parallel&utm_medium=docs&utm_campaign=integration). Works with any LLM provider — OpenAI, Anthropic, Bedrock, and more — via [LiteLLM](https://docs.litellm.ai/).

[Render](https://render.com?utm_source=parallel&utm_medium=docs&utm_campaign=integration) is a cloud platform for hosting web services, databases, and background jobs. Render Workflows extend it with a durable task execution engine — define Python or TypeScript functions as tasks, and Render runs each one on its own instance with automatic queuing, retry, and up to 24-hour execution. That makes Workflows a natural fit for AI agents that call external APIs like Parallel Search, where any individual call can be rate-limited, slow, or fail: the workflow retries the failed step instead of restarting the whole agent.

## Why Render Workflows for AI agents

- **Isolated retries.** Each task has its own retry budget. A Parallel rate limit retries one search call, not the whole investigation.
- **Fan-out / fan-in.** Run multiple research branches in parallel — each on its own instance — and merge results when they complete.
- **Graceful degradation.** If a branch exhausts its retries, the remaining branches still produce a report. The failure is surfaced, not silent.
- **Scales to zero.** No traffic, no cost. Each run spins up in under a second.

## Prerequisites

1. A Parallel API key — [platform.parallel.ai](https://platform.parallel.ai)
2. An LLM provider API key (Anthropic, OpenAI, or any provider LiteLLM supports)
3. A Render account — [render.com](https://render.com?utm_source=parallel&utm_medium=docs&utm_campaign=integration)

```bash
pip install parallel-web litellm render
```

## Example: research agent with fan-out

The snippet below shows a Render Workflow that wraps an LLM tool-use loop over Parallel Search. The orchestrator fans out one `investigate` task per sub-question, each running on its own instance with its own retry budget, then fans in to synthesize the findings.

The LLM model is a string — swap `"anthropic/claude-sonnet-5"` for `"openai/gpt-5"` or any other [LiteLLM-supported model](https://docs.litellm.ai/docs/providers) to switch providers.

```python
import asyncio
import json
from litellm import completion
from parallel import Parallel
from render import Retry, TaskContext, Workflows

app = Workflows()

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "Search the live web. Returns ranked excerpts optimized for LLMs. "
            "Call multiple times with different queries to triangulate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "What you are trying to find. Be specific.",
                },
                "search_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 keyword queries targeting different facets.",
                },
            },
            "required": ["objective", "search_queries"],
        },
    },
}

MODEL = "anthropic/claude-sonnet-5"  # or "openai/gpt-5", "bedrock/...", etc.


@app.task(
    timeout_seconds=300,
    retry=Retry(max_retries=3, wait_duration_ms=5000, backoff_scaling=2.0),
)
def investigate(ctx: TaskContext, sub_question: str) -> dict:
    """One branch of the fan-out — an LLM tool loop over Parallel Search."""
    parallel = Parallel()
    messages = [{"role": "user", "content": sub_question}]

    for _ in range(6):
        response = completion(
            model=MODEL, max_tokens=4096,
            tools=[SEARCH_TOOL], messages=messages,
        )
        choice = response.choices[0]

        if choice.finish_reason != "tool_calls":
            return {"findings": choice.message.content or ""}

        # Execute tool calls and feed results back
        messages.append(choice.message.model_dump())
        for tc in choice.message.tool_calls or []:
            args = json.loads(tc.function.arguments)
            result = parallel.search(
                objective=args["objective"],
                search_queries=args["search_queries"],
                mode="fast",
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps([
                    {"title": r.title, "url": r.url, "excerpts": r.excerpts}
                    for r in result.results
                ]),
            })

    return {"findings": ""}


@app.task(timeout_seconds=900)
async def research_agent(ctx: TaskContext, query: str) -> list[dict]:
    """Fan out one investigate task per sub-question, then collect results."""
    sub_questions = plan(query)  # Split query into independent facets
    results = await asyncio.gather(
        *[ctx.run(investigate, sq) for sq in sub_questions],
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, dict)]
```

Each `investigate` call becomes its own Render Workflow run. If Parallel returns a 429, Render retries that branch with exponential backoff — the other branches keep running. The `research_agent` orchestrator collects whatever succeeded and discards failures, so a partial result is always better than no result.

## Template repo

The full implementation — including a planner that splits questions, an Extract tool for reading full pages, a synthesis step that reconciles conflicting sources, and a FastAPI gateway with auth and rate limiting — is available as a ready-to-deploy template:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/render-examples/parallel-research-agent&utm_source=parallel&utm_medium=docs&utm_campaign=integration)

[render-examples/parallel-research-agent](https://github.com/render-examples/parallel-research-agent)

A sample run on *"What are the most promising battery chemistries for grid-scale energy storage beyond lithium-ion?"* completed in ~4 minutes with 5 parallel branches, 30 Search and Extract calls, and a 20,000-character report citing 20 sources.

## Related Resources

- [Search API Quickstart](https://docs.parallel.ai/search-api/search-quickstart)
- [Extract API Quickstart](https://docs.parallel.ai/extract/extract-quickstart)
- [Search Best Practices](https://docs.parallel.ai/search-api/best-practices)
- [OpenAI Tool Calling](https://docs.parallel.ai/integrations/openai-tool-calling)
- [Anthropic Tool Calling](https://docs.parallel.ai/integrations/anthropic-tool-calling)
- [Render Workflows documentation](https://render.com/docs/workflows?utm_source=parallel&utm_medium=docs&utm_campaign=integration)
