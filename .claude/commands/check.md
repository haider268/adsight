Run the adsight quality checks from the project root, in this order, and keep
going even if one fails:

1. `uv run ruff check`
2. `uv run ruff format --check`
3. `uv run mypy src`
4. `uv run pytest`

Report each one as PASS or FAIL with the key line of output (for pytest: the
test count and the coverage TOTAL line). For any failure, show the relevant
error lines. Do not fix anything unless asked.
