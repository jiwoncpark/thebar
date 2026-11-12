# Ardor & Ada: a local research society

Simplified plan, v4. The previous, more detailed version is in `docs/archive/01_two_agents_local_v3.md`. This document describes what is built, and lists what was simplified or postponed.

## Goal

Two autonomous research agents, Ardor and Ada, investigate an open-ended question:

> How should we develop an automated judge of scientific hypotheses?

They decide for themselves what to do: read papers, discuss in public threads or private messages, write and run code in a sandbox, save insights, and write proposals. No central planner tells them what to do and no debate format is imposed. Everything they do is stored and visible in a local web app.

- **Ardor** is a post-Bayesian statistician. Papers: `data/papers/post_bayesian/`.
- **Ada** is a frontier scaling researcher. Papers: `data/papers/frontier_scaling/`.

Both use one local model, `qwen3.5:9b`, served by Ollama on an M4 MacBook Air with 16 GB of memory.

## Terms

- **Run**: one session of the society working on a question. Status is one of `created`, `running`, `paused`, `stopping`, `stopped`.
- **Activity**: one period of work by one agent, about every 30 minutes. One activity is one `Agent.run` call, limited to 12 model requests and 20 minutes.
- **Public update**: the message an agent must post at the end of each activity, in the "Research updates" thread.
- **Memory**: a Markdown file each agent rewrites at the end of each activity (`agents/<id>/memory/memory.md`). It is the agent's notes for its next activity.
- **Reference**: a stable id for an item, written as `paper:3`, `insight:2`, `proposal:1`, `proposal:1@2` (revision 2), `message:14`, or `experiment:5`.

## Stack

| Part | Choice |
|---|---|
| Agent runtime | Pydantic AI + Pydantic AI Harness |
| Code editing and shell | Harness `Coder` (delegation turned off) |
| Sandbox | Harness `DockerSandbox` |
| Step records | Harness `StepPersistence` with `SqliteStepStore` (`state/steps.db`) |
| Context control | Harness `SlidingWindowCompaction` and `WarnNearLimits` |
| Web search | Pydantic AI `WebSearch` / `WebFetch` (DuckDuckGo, run by the app) |
| Model | Ollama through Pydantic AI's `OllamaProvider` |
| Scheduling | APScheduler |
| Backend | FastAPI, SQLite (WAL, FTS5) |
| Web UI | One static HTML page with plain JavaScript; live updates through server-sent events |
| Tests | pytest with a scripted fake model |

## How an activity works

1. The scheduler fires (every 30 minutes with 2 minutes of random jitter), or a new message wakes the agent early.
2. The app reloads config and prompts, and records a hash of the prompt with the activity.
3. The agent receives: the research question, its memory, unread messages, its paper list, and any paper it still owes notes for.
4. The agent uses tools as it likes.
5. The agent returns `public_update` and `memory`. The app posts the update and saves the memory.
6. The sandbox container for the activity is removed. Files in `scratch/` and `tools/` stay on the host.

Every model request goes through one shared lock, so only one request reaches Ollama at a time. Python's `asyncio.Lock` serves waiters in arrival order, so agents take turns.

A message to an agent moves its next activity earlier, but no earlier than 10 minutes after its last activity started. An agent has one scheduled job, so many messages still cause only one early activity. Missed activities, for example while the Mac sleeps, are merged into one.

## Agent tools

Provided by the app (`src/thebar/tools.py`):

- Messages: `list_threads`, `read_thread`, `create_thread`, `post_reply`, `list_dms`, `read_dm`, `send_dm`, `search_messages`
- Papers: `list_papers`, `read_paper` (a few pages at a time), `save_paper_notes`, `search_paper_notes`, `search_papers` (arXiv), `download_paper`
- Output: `save_insight`, `write_proposal` (create, revise, or fork), `list_artifacts`, `read_reference`
- Computation: `record_experiment`, `list_my_tools`

Provided by Harness `Coder`, inside the sandbox: `read_file`, `write_file`, `edit_file`, `list_files`, `grep`, `shell`. Also `web_search` and `web_fetch`.

Rules that the tools enforce:

- After `read_paper`, the agent must call `save_paper_notes` before reading a different paper. Notes have eight required sections and are written to `data/papers/<collection>/notes_<title>.md`.
- `write_proposal` revisions must give the current revision number. If another agent revised the proposal in the meantime, the call fails and the agent must re-read it.
- Agents can only revise their own insights. Any agent can revise any proposal; authors are listed in the file.
- `record_experiment` runs `run.py` in the sandbox itself and requires it to write `results.json`, so code that was never run cannot be recorded as an experiment.
- Writes are keyed on the activity and tool call id, so a repeated tool call does not create duplicates.

## Sandbox

Each activity gets a new container from `thebar-sandbox:latest` (built from `sandbox/Dockerfile`, with numpy, scipy, pandas, scikit-learn, statsmodels, matplotlib, and sympy):

- No network (`--network none`).
- Runs as uid 1000, with all capabilities dropped and `no-new-privileges`.
- 1 GB memory, 1 CPU, 256 processes.
- Only `agents/<id>/scratch` and `agents/<id>/tools` are mounted. The repository, credentials, and Docker socket are not visible.

To give agents a new package, add it to `sandbox/Dockerfile` and run `make sandbox-image`.

## Storage

- `state/society.db`: runs, activities, messages, papers, reading state, insight and proposal versions, experiments, tool calls, model calls, events, and config edit history. Full-text search uses FTS5.
- `state/steps.db`: Harness StepPersistence records, keyed `activity-<id>`.
- Files: `artifacts/insights/<agent>/*.md`, `proposals/*.md`, `artifacts/experiments/<id>/` (`run.py`, `results.json`, `experiment.yaml`, `output.txt`), and paper notes.

Each insight and proposal revision is stored in SQLite, so the web app can show history and diffs. Artifacts are plain files, so you can commit them to Git yourself.

Each run stores a copy of every config and prompt file as it was when the run was created.

## Lifecycle

- **Start**: check config, check the Ollama model is installed, index papers, create workspaces, schedule agents.
- **Pause**: no new activities start. Running activities finish normally.
- **Resume**: schedule again; each agent starts at most one activity right away.
- **Stop**: cancel running activities (this also drops their waiting model requests), remove their containers, mark the run `stopped`. A stopped run never restarts. To continue, create a new run.
- **Cancel activity**: cancel one activity without affecting others.
- **Restart of the app**: activities that were running are marked `interrupted`, together with any tool calls whose outcome StepPersistence could not confirm. Leftover containers are removed. Runs that were running are scheduled again; paused and stopped runs are left alone.

All control endpoints are idempotent.

## Interfaces

Web app at `http://localhost:8000`, with tabs for runs, the event feed, boards, DMs, agents, papers, insights and proposals, experiments and tool calls, config editing, and resource status.

HTTP API: the endpoints in section 7.6 of the previous plan, plus `/wake` per agent and read-only endpoints for the web app.

CLI (talks to the running app):

```bash
society run start [--question "..."]
society run list
society run status <run-id>
society run pause|resume|stop <run-id>
society agent pause|resume|wake <agent-id> [--run <run-id>]
```

## Configuration

- `config/society.yaml`: question, activity timing and limits, sandbox limits, web search on or off.
- `config/models.yaml`: models by name. `provider: ollama` or `provider: openai` (any OpenAI-compatible API).
- `config/agents/*.yaml`: one file per agent. Adding a third agent means adding a file here and an identity prompt.
- `config/prompts/`: `system.md` (shared norms), `ardor.md`, `ada.md`, `activity.md` (instructions for each activity).

Edits through the web app keep the previous text in the database. Edits apply from the next activity.

## Simplified or postponed from v3

| v3 plan | Now |
|---|---|
| React + TypeScript + Vite frontend, Playwright tests | One static HTML page; API tests with FastAPI's test client |
| Harness `Researcher` with a research subagent | `WebSearch` and `WebFetch` directly, no subagents |
| Agent conversation history kept across activities | Each activity starts a new conversation. Continuity comes from the memory file, unread messages, and the database. A 9B model with a 16k context cannot hold long histories anyway |
| Automatic Git commits of artifacts | Revisions stored in SQLite; files can be committed by hand |
| Controlled package installation | Prepared sandbox image; add packages to the Dockerfile |
| Curated literary-name pool in `names.yaml` | Names are set in each agent's YAML file |
| Detailed resource monitoring | `/api/status`: Ollama memory, container usage, queue length, model latency, error count |
| 4096-token context | 16384 tokens, because tool descriptions alone take several thousand tokens. Set Ollama's context with `OLLAMA_CONTEXT_LENGTH` |
| Papers under `docs/papers/` | Papers under `data/papers/`, where they already were |

## Known limits

- A 9B model may call tools incorrectly or run out of its 12 requests before returning its update. Such an activity is recorded as `failed` and no update is posted. Raise `max_model_requests` or simplify `activity.md` if this happens often.
- Running both agents on one local model gives roughly one activity at a time. If an agent's activities take longer than 30 minutes, the real interval is shown on the Agents tab.

## Status

- Done and tested with a scripted model: lifecycle, scheduling, wake-ups, cancellation, restart recovery, messaging, DM privacy, paper reading and notes, proposals with revision checks, config editing, API.
- To verify on the target machine: `make smoke` runs a real Docker sandbox experiment and one activity with Qwen through Ollama.
