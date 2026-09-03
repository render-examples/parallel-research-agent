# Parallel Research Agent

A durable web research agent powered by [Parallel](https://parallel.ai) Search, [Claude](https://anthropic.com), and [Render Workflows](https://render.com/workflows).

Submit a research question. Claude splits it into independent sub-questions, investigates all of them at the same time against Parallel's Search and Extract APIs, and reconciles the findings into one cited report — all inside a Render Workflow that retries on failure and scales to zero when idle.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/render-examples/parallel-research-agent)

## How it works

```
POST /research { "query": "..." }
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Gateway service (FastAPI)                               │
│ dispatches a run, then polls until it completes         │
└─────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ Workflow service — research_agent orchestrates:         │
│                                                         │
│ 1. plan_research                                        │
│    └── splits the question into 3-5 independent         │
│        sub-questions that don't depend on each other    │
│                                                         │
│ 2. investigate — fans out, one run per sub-question     │
│        ├──▶ investigate("sub-question 1")               │
│        ├──▶ investigate("sub-question 2")               │
│        └──▶ investigate("sub-question 3")               │
│    each branch gets its own instance and retry budget,  │
│    and runs a Claude tool loop over Parallel            │
│    Search + Extract until it can answer its piece       │
│                                                         │
│ 3. synthesize — fans in                                 │
│    └── reconciles conflicts across branches, dedupes    │
│        sources, writes the cited Markdown report        │
└─────────────────────────────────────────────────────────┘
```

The fan-out is what makes this durable rather than merely asynchronous. Every `investigate` branch is a separate workflow run on its own instance, so a Parallel rate limit or a transient timeout retries that one sub-question — the other branches keep going, and the work they've already done isn't thrown away. A single sequential loop would have to restart the entire investigation from turn one.

It also degrades instead of failing. If a branch exhausts its retries, `research_agent` synthesizes the branches that did finish and reports the shortfall in `branches_failed` rather than losing the run. And because the branches overlap, a five-thread investigation takes about as long as its slowest thread rather than the sum of all five.

## Deploy in 5 minutes

You need three API keys (all have free tiers):

| Key | Where to get it |
|---|---|
| Parallel | [platform.parallel.ai](https://platform.parallel.ai) |
| Anthropic | [console.anthropic.com](https://console.anthropic.com) |
| Render | [render.com/docs/api-keys](https://render.com/docs/api-keys) |

### Step 1 — Deploy the gateway

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/render-examples/parallel-research-agent)

Click the button and enter your three API keys when prompted. Leave `RENDER_WORKFLOW_SLUG` empty for now — you'll set it in step 3.

The button reads `render.yaml` from the repo, so if you want to customize the Blueprint, fork first and point the button at your fork by swapping the `repo` parameter in its URL.

### Step 2 — Create the Workflow service

Render Blueprints don't create Workflow services yet, so this is a manual step in the Dashboard:

1. In the [Render Dashboard](https://dashboard.render.com), click **New** → **Workflow**
2. Connect the same repo you forked
3. Set the **start command** to: `python -m workflow.main`
4. Set **plan** to Starter
5. Add these environment variables (same values as the gateway):
   - `PARALLEL_API_KEY`
   - `ANTHROPIC_API_KEY`
6. Click **Create Workflow**

### Step 3 — Wire the gateway to the workflow

1. In the Dashboard, open your new Workflow service and copy its **slug** (shown in the URL or service settings)
2. Go to the gateway service → **Environment** → set `RENDER_WORKFLOW_SLUG` to that slug
3. The gateway will redeploy automatically

### Step 4 — Try it

Open your gateway URL in a browser to use the demo form, or use curl:

```bash
# Start a research run
curl -X POST https://your-gateway.onrender.com/research \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the leading open-source alternatives to Elasticsearch in 2026?"}'

# Poll for results (use the run_id from the response above)
curl https://your-gateway.onrender.com/research/RUN_ID_HERE
```

A typical research run takes 40–70 seconds and costs about $0.45.

## Repo structure

```
parallel-research-agent/
├── render.yaml                # Blueprint — creates the gateway service
├── requirements.txt           # Python dependencies
├── gateway/
│   ├── main.py                # FastAPI — POST /research, GET /research/{id}
│   └── templates/
│       └── index.html         # Demo form UI
├── workflow/
│   ├── main.py                # Workflow entrypoint — registers tasks, starts runner
│   └── tasks.py               # research_agent, plan_research, investigate, synthesize
└── shared/
    └── formatters.py          # Helpers to format Parallel API responses for Claude
```

The two files that matter most:

- **`workflow/tasks.py`** — the four tasks, the tool definitions, the prompts, and the Parallel API calls. This is where the three technologies intersect.
- **`gateway/main.py`** — the thin HTTP layer that dispatches workflow runs and serves results.

Within `tasks.py`, `investigate` is the one to read first. It's a self-contained Claude tool-use loop against Parallel — the same loop you'd write without any workflow engine. `research_agent` only wraps it in `asyncio.gather`, which is what turns one loop into N parallel runs.

## Why explicit tool definitions instead of the Search MCP?

Parallel hosts a [Search MCP server](https://docs.parallel.ai/integrations/mcp/search-mcp) at `https://search.parallel.ai/mcp` that exposes `web_search` and `web_fetch` — the same Search and Extract APIs this agent calls, wrapped as ready-made tools. Anthropic's Messages API can connect to it natively, which would replace the `TOOLS` schemas, `_execute_tool`, and the entire turn loop inside `investigate` with a single `mcp_servers` argument:

```python
response = claude.beta.messages.create(
    model=CLAUDE_MODEL,
    max_tokens=4096,
    system=RESEARCH_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": sub_question}],
    mcp_servers=[{
        "type": "url",
        "url": "https://search.parallel.ai/mcp",
        "name": "parallel-search",
        "authorization_token": os.environ["PARALLEL_API_KEY"],
    }],
    betas=["mcp-client-2025-04-04"],
)
```

If you're adding web research to an existing chat app, use that. It's the shortest path, and the server is free to call anonymously — the API key only raises rate limits.

This project writes the tools out by hand because the MCP connector executes tool calls on Anthropic's infrastructure, outside the workflow. That trade matters here for three reasons:

- **Retries.** When Parallel returns a rate limit, the `Retry` policy on `investigate` re-runs that branch. Through the MCP connector the failure is handled inside Anthropic's request, so the workflow never sees it and can't scope the retry to one sub-question.
- **Observability.** Each search and extract is a distinct step in the workflow run, which is what makes `tool_calls_made` and `agent_turns` meaningful in the response envelope.
- **Tool descriptions.** The instruction to call search "multiple times with different queries to triangulate a topic" is what drives the multi-round behavior. The MCP server ships generic descriptions tuned for interactive chat, and caps excerpts near 25,000 characters per call in `basic` mode.

Re: [Parallel's docs](https://docs.parallel.ai/integrations/mcp/programmatic-use): reach for the MCP when you want drop-in behavior, and call the APIs directly when you need control over tool descriptions or the surrounding loop.

## Customize it

### Change the search quality

The agent uses Parallel's `fast` search mode (~700ms, balanced quality). Override via env var:

```
PARALLEL_SEARCH_MODE=advanced    # ~3s, highest quality
PARALLEL_SEARCH_MODE=turbo       # ~250ms, cheapest
```

### Change the agent's specialty

Edit `RESEARCH_SYSTEM_PROMPT` in `workflow/tasks.py`. The tool definitions stay the same. Just the instructions change:

```python
RESEARCH_SYSTEM_PROMPT = """\
You are a competitive intelligence analyst. Research the target company
using web search and extraction. Focus on: recent product launches,
pricing changes, key hires, funding rounds, and public strategy shifts.
Cite every claim with a source URL.\
"""
```

If you retarget the agent, retarget `PLANNER_PROMPT` too. The quality of the whole run depends on the split being genuinely independent — overlapping sub-questions burn budget researching the same ground three times.

### Tune the fan-out width

`MAX_SUB_QUESTIONS` in `workflow/tasks.py` caps how many branches a run can open, and `MAX_AGENT_TURNS` caps how deep each branch goes. Wider explores more ground per run; deeper follows each thread further. Cost scales with roughly the product of the two.

Render allows 20–100 concurrent runs depending on your workspace plan, and every branch consumes one. At the default of 5, expect to run 4–20 research queries simultaneously before runs start queueing. Narrow the fan-out if you need more concurrent queries instead.

### Change the Claude model

```
CLAUDE_MODEL=claude-opus-5               # harder research tasks
CLAUDE_MODEL=claude-haiku-4-5-20251001   # faster, cheaper
PLANNER_MODEL=claude-sonnet-5            # better sub-question splits
```

`PLANNER_MODEL` defaults to Haiku. Decomposition is a short, cheap call, but it sets the shape of everything downstream — worth upgrading before you upgrade `CLAUDE_MODEL`.

### Add more Parallel APIs

The agent currently uses Search and Extract. Adding more tools is straightforward — define the tool schema in `TOOLS`, add a handler in `_execute_tool`, and Claude will start using it:

- **Task API** (`parallel.tasks.run`) — for multi-hop deep research questions
- **FindAll API** — for discovering entities matching criteria (companies, people)
- **Monitor API** — for watching topics over time

## Local development

```bash
# Clone and install
git clone https://github.com/render-examples/parallel-research-agent.git
cd parallel-research-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Set env vars
cp .env.example .env
# Edit .env with your API keys

# Run the workflow locally (requires Render CLI)
render workflows dev -- python -m workflow.main

# In another terminal, start the gateway
uvicorn gateway.main:app --reload --port 8000
```

## How much does a run cost?

Assuming the default fan-out of 5 branches:

| Component | Per run |
|---|---|
| `plan_research` (Haiku, ~1K tokens) | ~$0.001 |
| Parallel Search (~2 calls × 5 branches, fast mode) | ~$0.05 |
| Parallel Extract (~1-2 calls × 5 branches) | ~$0.04 |
| `investigate` (Sonnet, ~12K tokens × 5 branches) | ~$0.30 |
| `synthesize` (Sonnet, ~15K tokens) | ~$0.08 |
| Render compute (7 runs, ~40s each, Starter plan) | ~$0.004 |
| **Total** | **~$0.45** |

Fanning out costs roughly 4× what a single sequential loop costs, and almost all of that is Anthropic tokens — you're running five research agents instead of one. What you buy is breadth per run, isolated retries, and wall-clock time that tracks the slowest branch instead of the sum of all of them.

If cost matters more than depth, the cheapest lever is `PARALLEL_SEARCH_MODE=turbo` combined with a lower `MAX_SUB_QUESTIONS`. Dropping to 3 branches cuts the total to roughly $0.28.

## License

MIT
