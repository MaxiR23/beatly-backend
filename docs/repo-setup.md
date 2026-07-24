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
  - Required check: `checks` (the job in .github/workflows/ci.yml)
  - Require branches to be up to date before merging
- Block force pushes
- Restrict deletions

Effect: main cannot be pushed to directly, cannot be merged into with
CI red, cannot be force-pushed or deleted. The bypass list is empty on
purpose, so the rule applies to the repo owner too.

## After cloning

Hooks are not installed automatically:

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