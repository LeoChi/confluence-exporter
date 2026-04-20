# Contributing

Thanks for considering a contribution! This is a small project and PRs are welcome.

## Development setup

```bash
git clone https://github.com/LeoChi/confluence-exporter.git
cd confluence-exporter
python -m venv .venv
.venv/Scripts/activate   # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -e ".[dev,all]"
playwright install chromium   # optional, for the best PDF engine
```

## Running tests and lint

```bash
pytest
ruff check .
ruff format .
```

## Branching

- `main` is always releasable.
- Feature branches: `feat/<short-name>`
- Fix branches: `fix/<short-name>`

## Commit messages

Conventional Commits preferred (`feat:`, `fix:`, `docs:`, `chore:`, etc.), but not enforced.

## Releasing (maintainers)

1. Bump version in `pyproject.toml` and update `CHANGELOG.md`.
2. Commit and push to `main`.
3. Tag: `git tag v<x.y.z> && git push --tags`.
4. GitHub Actions will build and publish to PyPI automatically.
