# PC-Assistant

PC-Assistant is a desktop automation agent that plans tasks with an LLM, executes them on Windows, and ships an Electron UI for interactive control. The backend is FastAPI + Python, and the frontend is an Electron renderer served locally.

## Repository Layout
- `backend/` – FastAPI app, executor, LLM clients, and tests.
- `frontend/` – Electron app (main process + renderer). Development deps in `package.json`.
- `frontend_bundle/` – Packaged frontend assets (no `node_modules`).
- `demo_output/` – Sample outputs.
- `venv/` – Local virtualenv (not committed).

## Prerequisites
- Python 3.10+
- Node.js 18+ / npm
- Windows host (executor targets Windows APIs)

## Setup (Backend)
```powershell
python -m venv venv
.\venv\Scripts\activate
python -m pip install -r backend/requirements.txt
python -m pip install -r backend/requirements-dev.txt
```

Environment variables:
- `DEEPSEEK_API_KEY` (or set provider to qwen/openai via API payload).
- Optional: `EXECUTOR_ALLOWED_ROOTS` to restrict file writes.

## Run Backend Only
```powershell
.\venv\Scripts\activate
cd backend
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Key endpoints:
- `POST /api/ai/run` – Plan + execute.
- `POST /api/ai/debug_run` – Plan + execute, returning full TaskContext.
- `POST /api/ai/plan` – Plan only.

## Run Electron App
```powershell
cd frontend
npm install
npm start
```
The Electron main process auto-starts the backend via `uvicorn` in the repo root.

## Testing
Backend tests:
```powershell
.\venv\Scripts\activate
python -m pytest backend/tests
```

## Notable Behaviors
- Default LLM provider: DeepSeek (pure text). Vision/multimodal works with qwen/openai.
- File saves prefer direct `write_file` to avoid UI IME issues; working directory can be set from the UI (Settings) or via `work_dir` in API payload.
- Safety layer blocks unsafe paths/keywords and requires confirmation for destructive actions.

## Frontend Notes
- Renderer: `frontend/renderer/index.html` and `index.js`.
- Main process: `frontend/main.js` (launches backend; handles quit).
- Preload exposes `window.api.run` bridging to `/api/ai/run`.

## Troubleshooting
- If the UI shows “Backend bridge unavailable”, restart the Electron app to reload preload.
- For DeepSeek errors about `image_url`, switch provider to qwen/openai (DeepSeek is text-only).

