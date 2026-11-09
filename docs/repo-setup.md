# Repository setup

Configuration that lives in GitHub, not in this repository. Recreate
this manually if the repo is ever rebuilt.

## Branch ruleset: main protection

Settings > Rules > Rulesets

- Enforcement: Active
- Target: default branch (main)
- Bypass list: empty

Rules enabled:

- Require a pull request before merging
- Require status checks to pass
  - Required checks: `checks` and `integration`, two of the three jobs
    in .github/workflows/ci.yml. `integration` is added to the ruleset
    by the owner by hand. The third job, `image`, is not a required
    check: a pull request can merge with it red until the owner adds it.
  - Require branches to be up to date before merging
- Block force pushes
- Restrict deletions

Effect: main cannot be pushed to directly, cannot be merged into with
CI red, cannot be force-pushed or deleted. The bypass list is empty on
purpose, so the rule applies to the repo owner too.

## After cloning

Hooks are not installed automatically. `pre-commit`, and the pytest the
pre-push hook runs, come from the development requirements, so install
them first, in the venv:

    pip install -r requirements-dev.txt
    pre-commit install
    pre-commit install --hook-type pre-push

## Requirements

Rulesets on private repositories require GitHub Pro or higher. On the
Free plan they are only available for public repositories.

## SEE

- Managing rulesets for a repository:
  https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets
- Available rules for rulesets:
  https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- pre-commit: https://pre-commit.com/