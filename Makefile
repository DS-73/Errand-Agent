PYTHON ?= python3

.PHONY: evals test trace demo clean web-install web-api web-ui web-build web

evals:           ## Run all 30 scenarios and regenerate evals/EVAL_TABLE.md
	$(PYTHON) evals/run_evals.py

test:            ## Run the same scenarios under pytest
	$(PYTHON) -m pytest -q

trace:           ## Regenerate the timeout-after-success e2e trace
	$(PYTHON) evals/generate_trace.py

demo:            ## Regenerate the text demo transcript + demo trace
	$(PYTHON) evals/demo_capture.py

# --------------------------------------------------------------- web interface
web-install:     ## Install frontend dependencies for the Vite/React UI
	cd errand_agent/web/webapp/frontend && npm install

web-deps:     ## Install Python web dependencies
	pip install -r errand_agent/web/webapp/requirements-web.txt

web-build:     ## Build the Vite frontend into errand_agent/web/webapp/frontend/dist
	cd errand_agent/web/webapp/frontend && npm run build

web-api:     ## Run the FastAPI API server on http://127.0.0.1:8000
	$(PYTHON) -m errand_agent.web.webapp --host 127.0.0.1 --port 8000

web-ui:     ## Run the Vite dev server (proxies /api to the API server)
	cd errand_agent/web/webapp/frontend && npm run dev

web: web-deps web-install web-build     ## Install, build, then serve the UI via the API server
	$(PYTHON) -m errand_agent.web.webapp --host 127.0.0.1 --port 8000

clean:
	rm -rf __pycache__ errand_agent/__pycache__ evals/__pycache__ tests/__pycache__ \
		.pytest_cache data/*.db traces/*.jsonl errand_agent/web/webapp/frontend/dist \
		errand_agent/web/webapp/frontend/node_modules
