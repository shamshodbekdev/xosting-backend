# xosting.net backend — developer & operations entry points.
# Run `make help` for the list of targets.

SHELL         := /usr/bin/env bash
.SHELLFLAGS   := -eu -o pipefail -c
.DEFAULT_GOAL := help

ENV_FILE  ?= .env
COMPOSE   := docker compose -f infrastructure/docker-compose.yml --env-file $(ENV_FILE)
REGISTRY  ?= ghcr.io/shamshodbekdev
TAG       ?= latest
K8S_DIR   := infrastructure/k8s
NAMESPACE := xosting

PYTHON_SERVICES  := auth-service billing-service hosting-service domain-service support-service
GO_SERVICES      := api-gateway notification-service
SERVICES         := $(PYTHON_SERVICES) $(GO_SERVICES)
ALEMBIC_SERVICES := hosting-service domain-service support-service
DEPLOYMENTS      := api-gateway auth-service billing-service billing-worker billing-beat \
                    hosting-service domain-service support-service notification-service

# Optionally narrow test/build/logs to one service: `make test SERVICE=auth-service`
SERVICE ?=
TARGETS := $(or $(SERVICE),$(SERVICES))

.PHONY: help env up up-observability down clean restart logs ps migrate seed superuser \
        test lint fmt build push deploy-k8s k8s-render k8s-status k8s-delete

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m [SERVICE=name] [TAG=x]\n\n"} \
	/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

env: ## Create .env from .env.example (if missing)
	@if [ ! -f $(ENV_FILE) ]; then cp .env.example $(ENV_FILE); echo "Created $(ENV_FILE) from .env.example"; fi

# ------------------------------------------------------------- local stack ---
up: env ## Build and start the full stack with Docker Compose
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  API gateway : http://localhost:8080"
	@echo "  Mailpit UI  : http://localhost:8025"
	@echo "  Next step   : make migrate"

up-observability: env ## Start the stack plus Prometheus (:9090) and Jaeger (:16686)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 $(COMPOSE) --profile observability up -d --build

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) --profile observability down --remove-orphans

clean: ## Stop the stack and DELETE all data volumes
	$(COMPOSE) --profile observability down --volumes --remove-orphans

restart: ## Restart one service: make restart SERVICE=auth-service
	$(COMPOSE) restart $(SERVICE)

logs: ## Follow logs (all services, or SERVICE=name)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

ps: ## Show container status
	$(COMPOSE) ps

migrate: ## Apply database migrations for every service and seed the plan catalogue
	$(COMPOSE) exec -T auth-service python manage.py migrate --noinput
	$(COMPOSE) exec -T billing-service python manage.py migrate --noinput
	$(COMPOSE) exec -T billing-service python manage.py seed_plans
	@for svc in $(ALEMBIC_SERVICES); do \
		echo "==> $$svc: alembic upgrade head"; \
		$(COMPOSE) exec -T $$svc alembic upgrade head; \
	done

seed: ## (Re)load the default billing plans
	$(COMPOSE) exec -T billing-service python manage.py seed_plans

superuser: ## Create a Django admin user in auth-service
	$(COMPOSE) exec auth-service python manage.py createsuperuser

# ------------------------------------------------------------- quality -------
test: ## Lint + test every service inside Docker (no local toolchain needed)
	@for svc in $(TARGETS); do \
		echo ""; echo "==> testing $$svc"; \
		DOCKER_BUILDKIT=1 docker build --target test --progress=plain \
			-f infrastructure/docker/$$svc.Dockerfile . ; \
	done
	@echo ""; echo "All test suites passed: $(TARGETS)"

lint: ## Lint locally (requires ruff and go on PATH)
	@for svc in $(PYTHON_SERVICES); do (cd services/$$svc && ruff check . && ruff format --check .); done
	cd shared && ruff check . && ruff format --check . && go vet ./...
	@for svc in $(GO_SERVICES); do (cd services/$$svc && test -z "$$(gofmt -l .)" && go vet ./...); done

fmt: ## Format all code locally (ruff + gofmt)
	@for svc in $(PYTHON_SERVICES); do (cd services/$$svc && ruff format . && ruff check --fix .); done
	cd shared && ruff format . && gofmt -w .
	@for svc in $(GO_SERVICES); do gofmt -w services/$$svc; done

# ------------------------------------------------------------- images --------
build: ## Build production images: make build [TAG=v1.0.0] [REGISTRY=...]
	@for svc in $(TARGETS); do \
		echo "==> building $(REGISTRY)/xosting-$$svc:$(TAG)"; \
		DOCKER_BUILDKIT=1 docker build --target runtime --build-arg VERSION=$(TAG) \
			-t $(REGISTRY)/xosting-$$svc:$(TAG) -f infrastructure/docker/$$svc.Dockerfile . ; \
	done

push: build ## Build and push images to the registry
	@for svc in $(TARGETS); do docker push $(REGISTRY)/xosting-$$svc:$(TAG); done

# ------------------------------------------------------------- kubernetes ----
k8s-render: ## Render the Kubernetes manifests to stdout
	@kubectl kustomize $(K8S_DIR) | sed -E 's#ghcr.io/shamshodbekdev/(xosting-[a-z-]+):latest#$(REGISTRY)/\1:$(TAG)#g'

deploy-k8s: ## Deploy to the current kubectl context: make deploy-k8s TAG=v1.0.0
	@test -f $(K8S_DIR)/secrets.env || { \
		echo "Missing $(K8S_DIR)/secrets.env: copy secrets.env.example and fill it in."; exit 1; }
	@echo "==> context: $$(kubectl config current-context)  namespace: $(NAMESPACE)  tag: $(TAG)"
	@# Jobs are immutable: remove the previous migration run before applying.
	kubectl -n $(NAMESPACE) delete job -l app.kubernetes.io/component=migration --ignore-not-found
	$(MAKE) --no-print-directory k8s-render | kubectl apply -f -
	kubectl -n $(NAMESPACE) wait --for=condition=complete job -l app.kubernetes.io/component=migration --timeout=600s
	@for d in $(DEPLOYMENTS); do kubectl -n $(NAMESPACE) rollout status deployment/$$d --timeout=300s; done
	@echo "Deployment complete."

k8s-status: ## Show workloads in the namespace
	kubectl -n $(NAMESPACE) get deploy,sts,po,svc,ing,hpa,jobs

k8s-delete: ## Delete every resource of the stack (PVCs are kept)
	$(MAKE) --no-print-directory k8s-render | kubectl delete --ignore-not-found -f -
