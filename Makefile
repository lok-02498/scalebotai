# ScaleBotAI developer shortcuts (Linux/macOS/WSL/Git Bash).
DOCKERHUB_USER ?= scalebotai
IMAGE          ?= $(DOCKERHUB_USER)/scalebotai-api
TAG            ?= local
NAMESPACE      ?= scalebotai
BASE_URL       ?= https://localhost

.PHONY: help install run test lint format build up up-llm down logs scale smoke k8s-deploy k8s-status k8s-rollback load-smoke

help:  ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Create .venv and install dev dependencies
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:  ## Run the API locally with auto-reload (needs Postgres + Redis, or `make up`)
	cd src && uvicorn app.main:app --reload --port 8000

test:  ## Unit + integration tests with coverage
	pytest --cov=app --cov-report=term-missing

lint:  ## Ruff lint + format check + Bandit
	ruff check src tests && ruff format --check src tests && bandit -q -r src/app -ll -c pyproject.toml

format:  ## Auto-format
	ruff format src tests && ruff check --fix src tests

build:  ## Build the API image
	docker build -f docker/api/Dockerfile -t $(IMAGE):$(TAG) .

up:  ## Start the full stack (API, DBs, NGINX, monitoring, ELK)
	docker compose up -d --build

up-llm:  ## Start the stack with Ollama (then: docker compose exec ollama ollama pull tinyllama)
	docker compose --profile llm up -d --build

down:  ## Stop the stack (keeps volumes)
	docker compose down

logs:  ## Tail API logs
	docker compose logs -f chatbot-api

scale:  ## Run 3 API replicas behind NGINX
	docker compose up -d --scale chatbot-api=3

smoke:  ## End-to-end smoke test against BASE_URL
	INSECURE=1 bash scripts/smoke_test.sh $(BASE_URL)

k8s-deploy:  ## Apply Kubernetes base manifests
	kubectl apply -k k8s/base

k8s-status:  ## Show pods, HPA and rollout status
	kubectl -n $(NAMESPACE) get pods,svc,hpa,ingress && kubectl -n $(NAMESPACE) rollout status deployment/scalebotai-api

k8s-rollback:  ## Roll back the API deployment
	kubectl -n $(NAMESPACE) rollout undo deployment/scalebotai-api

load-smoke:  ## k6 smoke test (100 VUs)
	k6 run -e BASE_URL=$(BASE_URL) tests/load/k6/smoke.js
