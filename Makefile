CONTAINER_ENGINE ?= $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
IMAGE ?= ghcr.io/jelmervdm/spinq:dev
PLATFORMS ?= linux/amd64,linux/arm64
PYTHON ?= python3

.PHONY: help test lint build build-multiarch run compose-up compose-down release clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run unit and loopback integration tests
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

lint: ## Run ruff linter (requires ruff installed)
	ruff check src tests

build: ## Build local OCI container image
	$(CONTAINER_ENGINE) build -t $(IMAGE) -f Containerfile .

build-multiarch: ## Build multi-arch OCI container image (amd64 + arm64)
	docker buildx build --platform $(PLATFORMS) -t $(IMAGE) -f Containerfile .

run: ## Run spinqd locally via Python
	PYTHONPATH=src $(PYTHON) -m spinq.server

compose-up: ## Start spinqd in the background with compose
	$(CONTAINER_ENGINE) compose -f compose.yaml up -d --build

compose-down: ## Stop spinqd compose stack
	$(CONTAINER_ENGINE) compose -f compose.yaml down

release: ## Tag and push v$(VERSION) to trigger GHCR and GitHub Release
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=0.1.1" && exit 1)
	git tag -a "v$(VERSION)" -m "v$(VERSION)"
	git push origin "v$(VERSION)"

clean: ## Remove build artifacts and local caches
	rm -rf build dist *.egg-info .pytest_cache __pycache__ src/spinq/__pycache__ tests/__pycache__
