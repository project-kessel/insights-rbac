# Pre-commit Setup Guide

This project uses [pre-commit](https://pre-commit.com/) to run automated checks before each commit, including [Gitleaks](https://github.com/gitleaks/gitleaks) for secret detection.

## Prerequisites

- Python 3.12+

## Installation

### 1. Install pre-commit

```bash
pip install pre-commit
```

### 2. Install the git hooks

From the project root directory, run:

```bash
pre-commit install
```

This registers the pre-commit hooks with your local git repository. From now on, all configured checks (including Gitleaks) will run automatically on every `git commit`.

### 3. Verify the installation

Run all hooks against the entire codebase to confirm everything works:

```bash
pre-commit run --all-files
```

## ⚠️ Warning: Pre-commit Must Be Installed Locally

Pre-commit is a **local tool** — it only works if each developer runs `pip install pre-commit` and `pre-commit install` on their machine. **If these steps are skipped, no hooks will run** — commits will go through without any secret scanning, and Gitleaks will NOT protect you.

**How to tell if pre-commit is not installed:**
- You do **not** see `Detect hardcoded secrets` in your `git commit` output
- Running `pre-commit --version` returns `command not found`

**To mitigate this risk:**
- Make pre-commit setup part of your **onboarding checklist** for new developers.
- The project's CI pipeline (`.github/workflows/pre-commit.yml`) runs all pre-commit hooks — including Gitleaks — on every pull request, acting as a **safety net** to catch secrets even if a developer hasn't installed pre-commit locally.

## Verifying Gitleaks Ran

When you run `git commit`, pre-commit prints the status of each hook. Look for the Gitleaks line in the output:

```
Trim Trailing Whitespace.................................................Passed
Fix End of Files.........................................................Passed
Debug Statements (Python)................................................Passed
django-upgrade...........................................................Passed
Detect hardcoded secrets.................................................Passed
```

- **Passed** — no secrets detected.
- **Failed** — a potential secret was found. Review the output for details on which file and line triggered the detection.

If you don't see the `Detect hardcoded secrets` line at all, the hooks are not installed. Run `pre-commit install` to fix this.

## How It Works

- On every `git commit`, pre-commit scans your **staged changes** using all configured hooks.
- If **Gitleaks detects a secret** (API key, password, token, etc.), the commit is **blocked** and the secret never enters git history.
- Fix the issue (remove the secret), re-stage your changes, and commit again.

## Running Hooks Manually

Run all hooks:

```bash
pre-commit run --all-files
```

Run only Gitleaks:

```bash
pre-commit run gitleaks --all-files
```

## Troubleshooting

### `command not found: pre-commit`

The `pre-commit` binary is not on your PATH. Either:

- Use the full path (e.g., `~/.local/bin/pre-commit install`)
- Add the Python bin directory to your PATH in `~/.zshrc` or `~/.bashrc`

### Updating hooks to the latest version

```bash
pre-commit autoupdate
```

### Skipping hooks (use sparingly)

If you need to bypass hooks for a specific commit:

```bash
git commit --no-verify
```

**Warning:** This skips all pre-commit checks. Only use this when you are certain there are no secrets in your staged changes.
