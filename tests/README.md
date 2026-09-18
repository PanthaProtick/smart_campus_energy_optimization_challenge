# Interpreter test commands

Run the offline suite (no Gemini calls):

```sh
uv run pytest -q
```

The public-case golden tests replay the sample pack's expected structured
interpretations through the parser and deterministic validator. Explanations
are intentionally excluded from comparisons.

Run the opt-in Gemini paraphrase checks separately. These make real provider
requests and may incur charges. The test also loads the project `.env` file.

PowerShell:

```powershell
$env:GRIDWISE_RUN_LIVE_TESTS = "1"
uv run pytest -q -m live_provider tests/test_interpreter_live.py
```

Bash:

```sh
GRIDWISE_RUN_LIVE_TESTS=1 uv run pytest -q -m live_provider tests/test_interpreter_live.py
```

Set `GEMINI_API_KEY` in the shell or add it to the ignored project-root `.env`
before running the live checks. Without `GRIDWISE_RUN_LIVE_TESTS=1`, live tests
skip even if the key is configured.
