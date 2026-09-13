# Continuous integration

`github-actions/ci.yml` is the GitHub Actions workflow for this project. It is
staged here rather than at `.github/workflows/ci.yml` because the credential
used to open the pull request did not carry GitHub's `workflow` OAuth scope,
and GitHub refuses any push that creates or updates a file under
`.github/workflows/` without it.

The file is complete and unmodified. Nothing else in the repository depends on
its location — the checks it runs can all be run by hand, and are below.

## Activating it

With a credential that has the `workflow` scope:

```bash
mkdir -p .github/workflows
git mv ci/github-actions/ci.yml .github/workflows/ci.yml
git rm ci/README.md
git commit -m "ci: activate the GitHub Actions workflow"
git push
```

To grant the scope to the GitHub CLI:

```bash
gh auth refresh -h github.com -s workflow
```

Alternatively, create the file through the GitHub web UI, which is not subject
to the same restriction, and paste in the contents of
`ci/github-actions/ci.yml`.

Until it is activated, the CI badge at the top of the README will not resolve.

## What the workflow runs

Every check below can be run locally; none of them need hardware.

```bash
# Python: compile and the full test suite
python3 -m py_compile bin/qnap-tsx70-lcd bin/qnap-tsx70-fancontrol
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Shell: syntax, behaviour in dry-run mode, and ShellCheck
for s in scripts/*.sh; do bash -n "$s"; done
PYTHONPATH=tests python3 -m unittest tests.test_scripts -v
shellcheck --severity=warning scripts/*.sh

# systemd units, without installing them
systemd-analyze verify systemd/*.service

# Documentation, privacy and naming checks
python3 -m unittest discover -s tests -p 'test_repo_*.py' -v
```
