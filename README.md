# FinClario

A financial service assistant built with **FastAPI**, **Vue 3**, and tool-based agent orchestration. It supports fund product inquiries, subscription and redemption fee estimates, transaction lookups, and customer support workflows.

All products, accounts, transactions, and service tickets use simulated data. The application does not execute financial transactions.

## Capabilities

- Route requests across specialist agents with explicit tool permissions and fallback handling.
- Combine product knowledge, conversation context, and structured business records.
- Calculate fees with decimal arithmetic and manage customer-scoped, idempotent service tickets.
- Display supporting evidence, data versions, and tool traces alongside responses.

## Quick start

Requires **Python 3.11 or 3.12** and **Node.js 22.12+**. Demo mode runs locally without an API key, Redis, or ChromaDB.

From the repository root on macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-local.txt pytest
./backend/scripts/run_demo.sh
```

In a second terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open the [web app](http://127.0.0.1:5173) or [API documentation](http://127.0.0.1:8000/docs).

The current UI and demo prompts are in Chinese. Start with the built-in product and fee examples. Sample records include product `EM002`, transaction `TX1001`, and payment `PAY1001`.

Demo customers are `u1001` and `u1002`. API requests use `Authorization: Bearer demo-u1001` or `Authorization: Bearer demo-u1002`; these public tokens select demo identities and are not production credentials.

## Architecture

| Component | Responsibility |
| --- | --- |
| `backend/core` | Intent recognition, skill loading, and model response parsing |
| `backend/agents` | Agent routing, tool permissions, execution loops, and tracing |
| `backend/finance` | Business data, fee calculations, conversation state, and policy checks |
| `backend/mcp` | Tool execution, retries, caching, circuit breakers, and knowledge retrieval |
| `backend/memory` | Conversation memory interfaces; local adapters live in `finance/local_runtime.py` |
| `backend/monitor` | Runtime metrics and fallback signals |
| `backend/evaluation` | Evaluation utilities and repeatable scenario replay |
| `frontend` | Chat, evidence cards, tool traces, and operations views |

## LLM mode

Copy `backend/.env.example` to `backend/.env` and set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. Set `ANTHROPIC_BASE_URL` only when using a compatible endpoint. Keep `.env` out of version control.

Stop the demo server, then run:

```sh
cd backend
ECHOMIND_MODE=llm ECHOMIND_STORAGE=local PROMETHEUS_PORT=0 ../.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Runtime settings retain the `ECHOMIND_` prefix. LLM mode sends requests to the configured provider and may incur charges; demo mode uses a rule-based planner.

## Validation

Run from the repository root:

```sh
PYTHONPATH=backend .venv/bin/python -m pytest -q backend/tests
PYTHONPATH=backend .venv/bin/python -m evaluation.scenario_runner --mode demo --output backend/evaluation/runs/demo-check
cd frontend && npm run build
```

The recorded checks include **68 passing tests** and **31 synthetic scenarios** covering **45 turns** and **323 assertions**. Demo results validate application behavior, not LLM accuracy or production performance. See the [validation report](docs/validation.md) for evidence and remaining limits.

## Docker

```sh
docker compose up --build
```

Open <http://127.0.0.1:8080>. Compose runs demo mode, persists local data in a named volume, and binds the web app to loopback. Container build and runtime validation remain pending.

## Documentation

Detailed technical documents are currently in Chinese:

- [Architecture and scope](docs/architecture.md)
- [Evaluation guide](backend/evaluation/README.md)
- [Development stages and commit history](docs/development-history.md)

Commit dates from May through October 5, 2026 present a retrospective development sequence, not the actual implementation dates.
