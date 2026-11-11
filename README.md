# thebar

A small society of autonomous research agents running on one Mac. Two agents, Ardor (a post-Bayesian statistician) and Ada (a frontier scaling researcher), work on a shared research question. They read papers, talk in public threads and private messages, run code in a Docker sandbox, and write insights and proposals. A local web app shows everything they do.

The design, and what was simplified from the original plan, is in [docs/01_two_agents_local.md](docs/01_two_agents_local.md).

## Setup

You need [uv](https://docs.astral.sh/uv/), Docker Desktop, and [Ollama](https://ollama.com).

```bash
uv sync                      # Python environment
make sandbox-image           # Docker image agents' code runs in
brew install ollama
ollama pull qwen3.5:9b
make ollama                  # starts Ollama with a 16k context; leave it running
```

## Run

```bash
make dev                     # app at http://localhost:8000
```

Then create and start a run in the browser (Runs tab), or:

```bash
uv run society run start
uv run society run list
uv run society run pause 1
uv run society run resume 1
uv run society run stop 1
uv run society agent wake ada          # start Ada's next activity now
uv run society agent pause ardor
```

Each agent works about every 30 minutes while the app runs. Closing the browser does not stop them; stopping the app (Ctrl-C) or sleeping the Mac does. Runs that were running resume when the app starts again.

## Configure

Edit these files directly or in the Config tab. Changes apply from each agent's next activity.

| File | Contents |
|---|---|
| `config/society.yaml` | Research question, activity interval and limits, sandbox limits |
| `config/models.yaml` | Models. Set an agent's `model:` to use a different one |
| `config/agents/*.yaml` | One file per agent: name, model, prompt, paper folder, workspace |
| `config/prompts/*.md` | Shared research norms, agent identities, per-activity instructions |

To add an agent, add a YAML file in `config/agents/` and an identity prompt, then create a new run.

## Where things are

| Path | Contents |
|---|---|
| `data/papers/<collection>/` | PDFs and the agents' reading notes (`notes_*.md`) |
| `artifacts/insights/<agent>/` | Insights |
| `proposals/` | Proposals |
| `artifacts/experiments/<id>/` | Experiment code, results, and output |
| `agents/<agent>/memory/` | Each agent's memory and its history |
| `agents/<agent>/scratch/`, `tools/` | The agent's sandbox files |
| `state/` | SQLite databases (not in Git) |
| `src/thebar/` | Code |

## Test

```bash
make test     # scripted fake model; no Docker or Ollama needed
make smoke    # real Docker sandbox, plus one activity with the Ollama model
```

## Troubleshooting

- **"Ollama is not reachable" or "model is not installed" when starting a run**: start Ollama with `make ollama` and run `ollama pull qwen3.5:9b`. The app does not fall back to another model.
- **Activities fail with a Docker error**: start Docker Desktop and run `make sandbox-image`.
- **Activities often end as `failed` with a usage limit error**: the model used its 12 requests without returning its update. Raise `max_model_requests` in `config/society.yaml`.
- **Context errors or the model losing track of its instructions**: make sure Ollama was started with `OLLAMA_CONTEXT_LENGTH=16384` (`make ollama` does this) and that `context_tokens` in `config/models.yaml` matches.
- **The Mac runs out of memory**: lower the sandbox `memory_limit`, or the context length in both places above.
- **Agents need a Python package**: add it to `sandbox/Dockerfile` and run `make sandbox-image`. Sandboxes have no network access.
