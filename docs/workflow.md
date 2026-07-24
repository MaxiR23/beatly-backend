# Development workflow

Flow for every feature, fix or non-trivial change in this repo.

1. **Open an issue**
   - Title: `type: short description`
   - Body: scope, out of scope, acceptance criteria
   - Label matching the commit type: feat, fix, docs, chore, test, refactor

2. **Create the branch**
   - From updated main: `git checkout main && git pull`
   - Naming: `w_YYMMDD_type_short_description`, underscores only
   - Check the date with `date` before naming it

3. **Write a task spec, only when the issue is not enough**
   - File: `docs/tasks/NNN_short_description.md`
   - Only for work with multiple phases, decisions worth recording, or
     open questions. Most tasks skip this step.

4. **Implement**
   - Tests and implementation ship in the same branch.
   - See the definition of done in AGENTS.md.

5. **Verify locally**
   - `pytest`
   - `ruff check .` and `ruff format .`

6. **Review before committing**
   - `codex review --uncommitted`
   - Fix what it flags, run it again until clean.

7. **Commit**
   - Conventional commits: `type: short description`, lowercase, one line.

8. **Push and open the PR**
   - Body: what it does, decisions taken, how to test, `Closes #N`.

9. **Merge and later could delete the branch**