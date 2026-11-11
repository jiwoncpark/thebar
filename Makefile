export PYDANTIC_AI_NO_BANNER=1

.PHONY: dev test smoke sandbox-image ollama

dev:  ## Start the app at http://localhost:8000
	uv run uvicorn thebar.api:app --port 8000 --timeout-graceful-shutdown 5

test:  ## Unit and integration tests (mocked model, no Docker)
	uv run pytest

smoke:  ## Tests with the real Docker sandbox and Ollama model
	uv run pytest -m live -s

sandbox-image:  ## Build the image agents' code runs in
	docker build -t thebar-sandbox:latest sandbox

ollama:  ## Start Ollama with the context length set in config/models.yaml
	OLLAMA_CONTEXT_LENGTH=16384 ollama serve
