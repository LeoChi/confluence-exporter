# Publishing Guide

A step-by-step checklist to take this project from a local folder to a
published package on GitHub and PyPI.

## 0. One-time setup

### GitHub account

If you don't have one: <https://github.com/signup>

### PyPI account + Trusted Publishing (recommended)

1. Create a PyPI account at <https://pypi.org/account/register/>.
2. Enable 2FA on the account (required for uploads).
3. We'll use **Trusted Publishing** (OIDC) — no API tokens to manage.
   You set it up after the first GitHub release below.

### Local tooling

```bash
python -m pip install --upgrade pip build twine
```

---

## 1. Personalize the project

Edit these files and replace the placeholders:

| File                       | What to change                                                 |
| -------------------------- | -------------------------------------------------------------- |
| `pyproject.toml`           | `authors`, `Homepage` / `Repository` / `Issues` URLs           |
| `README.md`                | The `YOURUSER` in badge / repo URLs                            |
| `CHANGELOG.md`             | The release-tag URL at the bottom                              |
| `LICENSE`                  | The `Copyright (c) …` line (name + year)                       |
| `CONTRIBUTING.md`          | The clone URL                                                  |

`grep -r YOURUSER .` (or `Select-String YOURUSER -r` on PowerShell) finds
every remaining placeholder.

---

## 2. Push to GitHub

```bash
cd C:\Users\leano.b.chiodo\dev\priv\confluence-exporter

git init
git add .
git commit -m "chore: initial commit"

# Create the remote repo via gh CLI (install: https://cli.github.com/)
gh repo create confluence-exporter --public --source=. --push
# or, manually:
#   1) create the repo on github.com
#   2) git remote add origin https://github.com/YOURUSER/confluence-exporter.git
#   3) git branch -M main
#   4) git push -u origin main
```

CI will run on the first push — make sure tests pass before tagging a release.

---

## 3. Set up Trusted Publishing on PyPI

Once the repo exists on GitHub:

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Click **Add a new pending publisher**.
3. Fill in:
   - **PyPI project name**: `confluence-space-exporter`
   - **Owner**: your GitHub username (or org)
   - **Repository name**: `confluence-exporter`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `pypi`
4. Save.

This authorizes the `publish.yml` workflow to upload to PyPI using
GitHub's OIDC token — no secrets to copy.

> **Alternative**: if you prefer a classic API token, create one at
> `Account settings → API tokens`, add it to the repo as a secret named
> `PYPI_API_TOKEN`, and replace the `gh-action-pypi-publish` step
> accordingly.

---

## 4. Cut the first release

```bash
# Bump the version in:
#   src/confluence_exporter/__init__.py   (__version__)
#   pyproject.toml                        (version)
#   CHANGELOG.md                          (move Unreleased → [0.1.0] - yyyy-mm-dd)

git add -A
git commit -m "chore: release v0.1.0"
git tag v0.1.0
git push origin main --tags
```

The tag triggers `.github/workflows/publish.yml` — watch it on the Actions
tab. In a couple of minutes your package is live at
`https://pypi.org/project/confluence-space-exporter/`.

Test it:

```bash
pip install confluence-space-exporter
confluence-exporter --version
```

---

## 5. Publishing a patch release

```bash
# Fix bugs on main
git commit -am "fix: …"

# Bump version (PATCH only for fixes)
# Update __version__, pyproject.toml, CHANGELOG.md

git tag v0.1.1
git push origin main --tags
```

---

## 6. Manual release (fallback if CI is down)

```bash
python -m build                 # creates dist/*.whl and dist/*.tar.gz
python -m twine check dist/*
python -m twine upload dist/*   # prompts for PyPI credentials
```

---

## Tips

- **Versioning**: follow [SemVer](https://semver.org/). Breaking changes
  → bump MAJOR, new features → MINOR, fixes → PATCH.
- **Pre-releases**: tags like `v0.2.0rc1` upload as pre-releases and
  don't get installed by default (`pip install confluence-space-exporter`).
- **Yanking a bad release**: go to the PyPI project page → Releases → Options
  → Yank. The version stays visible but `pip install` skips it.
- **Deleting a release**: PyPI does NOT let you re-upload the same
  version number. Always bump the version for every upload.
