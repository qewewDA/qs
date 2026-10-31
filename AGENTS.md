# Development

- Use the existing checkout in this isolated cloud task; do not create a worktree unless requested.
- Python >=3.11. Install with `uv sync --frozen --extra dev --extra mcp`.
- Run `uv run pytest -q`, `uv run ruff check src tests`, and `uv build` for relevant changes.
- Windows input is opt-in; `demo` is a replay simulation, never an actual game test.
- Do not silently default unknown save fields to zero/peace or choose the first country.
- Models select configured action IDs. Never allow model text to become console code or UI coordinates.
- Log acknowledgements are telemetry. Verify fresh save postconditions and retain ambiguous operations.
- Mod syntax needs real-game validation for the user's exact build; document the evidence and limits.
- Do not print credentials or include them in configuration, fixtures, or provider errors.
