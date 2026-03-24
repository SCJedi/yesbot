# YesBot Rules

## Always Allow
- Reading any file
- Searching/grepping files
- Writing/editing files within the project
- Running: python, pip, git status/diff/log/add/commit, npm, node, npx, pytest, ruff, black, mypy
- Web searches and fetches
- Creating/editing plans
- Approving plan execution
- Spawning agents and sub-agents

## Always Block
- Accessing credentials or secret files
- rm -rf, deltree, format commands
- git push --force to main/master
- Files outside project root
- sudo or admin elevation
- Deleting git branches
- Any rebase operations

## Ask Me (halt and wait)
- git push (non-force)
- Deploying to production or remote servers
- Sending emails, messages, or notifications
- Creating or commenting on PRs/issues
- Anything involving real money or transactions
- Installing global system packages
- Modifying CI/CD pipelines
- Modifying .claude/settings files

## Preferences
- Prefer simple solutions over complex ones
- Say yes to adding tests
- Continue after minor warnings, stop on critical errors
