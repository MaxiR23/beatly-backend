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
   - Through the agent loop: refine-issue, plan-issue, approve the plan,
     implement-issue. See the Workflow section of CLAUDE.md.
   - Answer any blocking questions the refinement raises before planning.
     The plan will refuse to start otherwise.
   - Tests and implementation ship in the same branch.
   - See the definition of done in CLAUDE.md.

5. **Verify locally**
   - `ruff check .`
   - `ruff format --check .`
   - `pytest`

6. **Review before committing**
   - Run review-changes, then verify-findings if it reports blocking or
     important findings.
   - Fix what verify-findings confirms, then run the gate again.

7. **Commit**
   - Conventional commits: `type: short description`, lowercase, one line.
     No body: the reasoning goes in the pull request.

8. **Push and open the PR**
   - Body: what it does, decisions taken, risks worth knowing about,
     how to test, `Closes #N`.

9. **Merge and later could delete the branch**