# Gomoku Project Conventions

Gomoku is a desktop five-in-a-row game (Python + PySide6 client, FastAPI + WebSocket server).
This file constrains Claude Code's behavior in this repository and intentionally keeps only the
minimum required set of rules.

## 1. Commit

- Always use Conventional Commits: `feat|fix|docs|chore|ci|refactor|test(scope): summary`.
- The summary is imperative, lowercase, and has no trailing period. Omit the scope when it adds no
  value (`feat: ...`).
- One commit does one thing. For breaking changes, append `!` to the summary and describe the
  migration in the body.
- Use the body for `Context / Changes / Impact` when useful; never restate the diff line by line.

Example:

```
feat(client): add check-for-updates entry to the main menu

Fetch the latest version from GitHub Releases and link to the download page.
```

## 2. Push

- Before pushing, make sure the working tree is clean and that the staged content matches the
  intended change only. Never include unrelated files (build artifacts, caches, `.coverage`).
- Before pushing, state the change summary in the conversation: what changed, what it affects, and
  how it was verified.
- Push only the current branch. No force pushes, no direct pushes to `main`. Rebase onto the remote
  when it has moved ahead.

## 3. Create PR

- The PR title follows the same format as the commit title. The body must contain these three
  sections:

```markdown
## Changes
(What this PR does, 1–3 bullets)

## Impact
(Client / server / protocol / deployment config; write "None" if not applicable)

## Verification
(Which tests or commands were run, plus manual steps to reproduce)
```

- Reference issues at the end of the body with `Closes #123`.
- Open PRs against `main`, and keep one PR scoped to a single problem.

## 4. Release a New Version

Releasing a new version must follow the full chain — bump the version, tag, and let CI publish the
Release — and the resulting Release must be marked as latest.

Steps:

1. Set `APP_VERSION` in `gomoku/client/constants.py` to the new version (`X.Y.Z`, no `v` prefix).
2. Update the "当前版本" paragraph at the top of `README.md` with what the new version adds. The
   release notes are generated from this paragraph.
3. Commit and merge into `main`.
4. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

5. `.github/workflows/release.yml` runs: it builds the Windows client, pushes the GHCR image, and
   creates the Release with `gh release create --latest`, titled with the version number.

Notes:

- The tag must match `APP_VERSION`, otherwise the pipeline fails immediately.
- The release is complete only after the pipeline succeeds. Verify with `gh release list` /
  `gh run list` that the Release exists and is marked as latest.
- Do not bypass the pipeline with a local `gh release create`.
