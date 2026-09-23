# AGENTS.md

Windows-only Flet desktop app (Python 3.14 + uv) that scrapes Polish job boards and evaluates offers against CVs using LLM models.

## Commands

```powershell
uv sync                      # install deps (CI uses: uv sync --locked --group dev)
uv run job-sniffer           # run the app (console script -> job_sniffer.app:run)
uv run pytest                # full suite (~6 s, hermetic)
uv run pytest tests/test_prompts.py -k markdown   # single file / single test
uv run ruff check .          # lint (CI-enforced)
uv run ruff format --check . # format check (CI-enforced; line length 100)
uv run mypy                  # strict; NOT in CI; 2 pre-existing errors (see below)
uv run flet build windows    # desktop build -> build/windows (needs Windows + Developer Mode)
```

## Architecture

- `src/` layout; package `job_sniffer`. Two entry points, both calling `app.run()`: the `job-sniffer` console script and `src/main.py` (used by `flet build`; don't remove it).
- `ui/shell.py` builds the shell and wires all views (scan, offers, profiles, sources, llm), `EvaluationService`, and a single-worker scan `ThreadPoolExecutor`.
- `sources/` — one module per job board implementing the `JobSource` protocol (`search()`); `sources/registry.py` holds `SOURCE_DEFINITIONS` with status flags (LinkedIn disabled). Scrapers use undetected-chromedriver with per-source Chrome profile dirs (`.pracuj-profile/`, `.olx-profile/`, ...) created in the CWD — gitignored, they hold login state; leave them alone.
- `llm/` — `catalog.py` (selectable GGUF models), `downloader.py` (fetches llama-server binaries + GGUF into `models_dir` under the platformdirs user data dir), `runner.py` (llama-server subprocess lifecycle + `/health` polling), `ollama.py` / `openai_compat.py` providers, `prompts.py`, `hardware.py` (GPU/Vulkan/CUDA detection).
- `services.py` — `EvaluationService` coordinates CV parsing, model download, llama-server lifecycle, and evaluations.
- `database.py` — SQLAlchemy 2.0 ORM on SQLite. App DB path is CWD-relative `job_sniffer.sqlite` (`ui/offers_view.py`): running the app from the repo root creates it there (gitignored).
- `config.py` — JSON config in the platformdirs user config dir, not the repo.

## Conventions

- Python 3.14 only (`>=3.14,<3.15`).
- All code and comments must be in English (variable/function names, docstrings, inline comments, type annotations).
- UI strings, LLM prompts, and test assertions are in Polish — keep new user-facing/prompt/test text Polish.
- Strict typing expected (mypy `strict = true`, `py.typed`).
- Conventional commit style (`feat:`, `fix:`); default branch `master`.
- Tests are hermetic unit tests: mocks for subprocess/GPU, `tmp_path` for DB and config files, monkeypatched `config_file_path`. No network, browser, or LLM needed — don't add tests that require them.
- License is GPL-3.0-only; check `THIRD_PARTY_NOTICES.md` when adding dependencies.
