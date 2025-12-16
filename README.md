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
- `DEEPSEEK_API_KEY` (primary provider), `DOUBAO_API_KEY` (requires exact Ark model IDs, e.g., `doubao-seed-1-6-lite-251015`, `doubao-seed-1-6-vision-251015`, `doubao-seed-code-preview-251028`), and `QWEN_API_KEY` for fallbacks. Optional: `DOUBAO_MODEL` / `DOUBAO_TEXT_MODEL` / `DOUBAO_VISION_MODEL` / `DOUBAO_REASONING_EFFORT` / `DOUBAO_TEMPERATURE` / `DOUBAO_TOP_P` (must also be exact model IDs if set). Vision is used automatically for Doubao only when a screenshot is present **and** a vision-capable model ID is configured via `DOUBAO_VISION_MODEL` (or `DOUBAO_MODEL` points to a vision model).
- Optional: `EXECUTOR_ALLOWED_ROOTS` to restrict file writes.
- Ports:
  - Dev/Electron backend: `127.0.0.1:5004` (override with `PC_ASSISTANT_DEV_HOST` / `PC_ASSISTANT_DEV_PORT`).
  - Pytest/EXECUTOR_TEST_MODE: `127.0.0.1:5015` (override with `PC_ASSISTANT_TEST_HOST` / `PC_ASSISTANT_TEST_PORT`).

## Run Backend Only
```powershell
.\venv\Scripts\activate
cd backend
python -m backend.launch_backend
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
The Electron main process auto-starts the backend via the launcher in the repo root.
The backend listens on `http://127.0.0.1:5004` by default; if the port is busy, the launcher will clear stale assistant listeners or exit with a clear message instead of failing with WinError 10013.

To start only the backend from the repo root with the same port hygiene:
```powershell
python -m backend.launch_backend
```
Or from `frontend/` via npm:
```powershell
npm run backend
```

## Dev Manager (one-command control)
Use `scripts/dev_manager.py` for common dev tasks:
```powershell
python -m scripts.dev_manager start-backend    # launch backend with port hygiene
python -m scripts.dev_manager start-frontend   # run Electron (npm start)
python -m scripts.dev_manager stop             # stop backend/Electron processes
python -m scripts.dev_manager ports            # show dev/test port usage (5004/5015)
python -m scripts.dev_manager tail backend     # tail a log (backend|main|renderer|dev_manager)
```
This helps avoid lingering processes or port conflicts on Windows.

## Testing
Backend tests:
```powershell
.\venv\Scripts\activate
python -m pytest backend/tests
```
- Pytest sets `EXECUTOR_TEST_MODE=1` and defaults to port `5015` if a server is needed; override with `PC_ASSISTANT_TEST_PORT` to avoid clashes with any dev/Electron instance.
- Task takeover state is kept **in-memory only**. Run the FastAPI backend with a single worker (no multi-worker gunicorn/Uvicorn) to avoid losing task state.

## Notable Behaviors
- Default LLM provider: DeepSeek (pure text). Vision/multimodal works with Doubao (when `DOUBAO_VISION_MODEL` or a vision `DOUBAO_MODEL` is set) and Qwen.
- File saves prefer direct `write_file` to avoid UI IME issues; working directory can be set from the UI (Settings) or via `work_dir` in API payload.
- Safety layer blocks unsafe paths/keywords and requires confirmation for destructive actions.

## Pattern-first UIA execution (Commit 4)
- UI interactions prefer UIA patterns first (Invoke/Value/Toggle/SelectionItem) with safe rebind via runtime_id + locator_key TargetRef, then fall back to focus+click/clipboard typing as needed.
- After an `activate_window` step, the executor binds UIA searches to the activated hwnd/pid and blocks VLM/coordinate fallbacks when the preferred root is missing, reducing accidental clicks on the wrong window.

## Frontend Notes
- Renderer: `frontend/renderer/index.html` and `index.js`.
- Main process: `frontend/main.js` (launches backend; handles quit).
- Preload exposes `window.api.run` bridging to `/api/ai/run`.

## Troubleshooting
- If the UI shows “Backend bridge unavailable”, restart the Electron app to reload preload.
- For DeepSeek errors about `image_url`, switch provider to qwen/doubao (DeepSeek is text-only).
- The renderer now health-checks the backend, retries with exponential backoff, shows “Backend offline / reconnecting”, and will request the main process to restart the backend if repeated failures occur.
