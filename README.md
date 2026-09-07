# Parallel Research Agent

A durable web-research agent built with [Parallel](https://parallel.ai), [LiteLLM](https://docs.litellm.ai/), and [Render Workflows](https://render.com/workflows).

Submit a question and the agent will split it into independent threads, research them in parallel with Search and Extract, and combine the findings into one cited report. LiteLLM supports Anthropic, OpenAI, Bedrock, and [many other model providers](https://docs.litellm.ai/docs/providers).

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/render-examples/parallel-research-agent)

## How it works

```
POST /research
      │
      ▼
FastAPI gateway ── dispatches research_agent
      │
      ▼
plan_research ── creates 3–5 independent sub-questions
      │
      ├── investigate(sub-question 1) ── Search + Extract
      ├── investigate(sub-question 2) ── Search + Extract
      └── investigate(sub-question 3) ── Search + Extract
      │
      ▼
synthesize ── reconciles findings and writes a cited report
```

Each `investigate` branch is a separate Workflow run with its own instance and retry budget. A rate limit or timeout retries only that branch. If a branch still fails, the agent synthesizes the successful results and reports the gap in `branches_failed`.

## Deploy

Before starting, create:

- a [Parallel API key](https://platform.parallel.ai)
- a model-provider API key, such as [Anthropic](https://console.anthropic.com) or [OpenAI](https://platform.openai.com)
- a [Render API key](https://render.com/docs/api-keys)

### 1. Deploy the gateway

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/render-examples/parallel-research-agent)

Enter the requested keys and leave `RENDER_WORKFLOW_SLUG` blank. The Blueprint creates the FastAPI gateway; you will add the Workflow service next.

To customize the deployment, fork this repository and replace the `repo` parameter in the button URL with your fork.

### 2. Create the Workflow service

Blueprints do not yet create Workflow services, so add one in the [Render Dashboard](https://dashboard.render.com):

1. Click **New** → **Workflow** and connect the same repository.
2. Set the start command to `python -m workflow.main` and choose the Starter plan.
3. Add `PARALLEL_API_KEY` and your model-provider credentials.
4. Click **Create Workflow**.

The default models use Anthropic. For another provider, also set `LLM_MODEL` and `PLANNER_MODEL` to [LiteLLM model strings](https://docs.litellm.ai/docs/providers).

### 3. Connect the gateway

Copy the Workflow service slug, then set `RENDER_WORKFLOW_SLUG` on the gateway. The gateway redeploys automatically.

### 4. Start a run

The Blueprint generates `API_SECRET` for the gateway. Copy it from the gateway's Environment page and send it as a bearer token:

```bash
export GATEWAY_URL=https://your-gateway.onrender.com
export API_SECRET=your-generated-secret

curl -X POST "$GATEWAY_URL/research" \
  -H "Authorization: Bearer $API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the leading open-source alternatives to Elasticsearch in 2026?"}'

# Use the returned run_id
curl "$GATEWAY_URL/research/RUN_ID_HERE"
```

The home page includes a demo form for local development, where `API_SECRET` can be left blank.

## Project structure

```
parallel-research-agent/
├── render.yaml                # Gateway Blueprint
├── gateway/
│   ├── main.py                # HTTP API
│   └── templates/index.html   # Demo UI
├── workflow/
│   ├── main.py                # Workflow runner
│   └── tasks.py               # Planning, research, and synthesis tasks
└── shared/
    └── formatters.py          # Parallel response formatters
```

Start with `workflow/tasks.py`: `investigate` contains the LLM tool loop, while `research_agent` fans that loop out with `asyncio.gather`. `gateway/main.py` dispatches runs and serves their status and results.

## Why call Parallel directly?

Parallel's [Search MCP server](https://docs.parallel.ai/integrations/mcp/search-mcp) is the simplest way to add web research to a chat app. This project calls Search and Extract directly because the Workflow needs to:

- see API failures and retry only the affected branch
- record each search and extraction in its run metrics
- control tool descriptions and the surrounding LLM loop

Use MCP for a drop-in integration; use the APIs directly when you need control over retries and execution. See [Parallel's programmatic MCP guide](https://docs.parallel.ai/integrations/mcp/programmatic-use).

## Configuration

Set models with [LiteLLM model strings](https://docs.litellm.ai/docs/providers) and provide the matching credentials:

```bash
LLM_MODEL=anthropic/claude-sonnet-5
PLANNER_MODEL=anthropic/claude-haiku-4-5-20251001
```

Other controls live in `workflow/tasks.py`:

- `PARALLEL_SEARCH_MODE` selects `turbo`, `fast` (default), or `advanced`.
- `MAX_SUB_QUESTIONS` controls breadth; `MAX_AGENT_TURNS` controls depth.
- `PLANNER_PROMPT` and `RESEARCH_SYSTEM_PROMPT` define the research specialty. Update both when retargeting the agent.
- `TOOLS` and `_execute_tool` are the extension points for adding more Parallel APIs.

## Local development

```bash
git clone https://github.com/render-examples/parallel-research-agent.git
cd parallel-research-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Add your API keys to .env; leave API_SECRET blank.

# Terminal 1 (Render CLI 2.11.0+)
render workflows dev -- .venv/bin/python -m workflow.main

# Terminal 2
set -a && source .env && set +a
RENDER_API_URL=http://localhost:8120 RENDER_WORKFLOW_SLUG=local \
  .venv/bin/uvicorn gateway.main:app --reload --port 8000
```

## Cost

A four-branch run with the default Anthropic models costs about **$0.33**, mostly in LLM tokens. Actual cost depends on the provider, model, branch count, and turn count; the main levers are `LLM_MODEL`, `MAX_SUB_QUESTIONS`, and `MAX_AGENT_TURNS`.

## License

MIT
