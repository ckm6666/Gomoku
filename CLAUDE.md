# Gomoku Project Conventions

Gomoku is a desktop five-in-a-row game (Python + PySide6 client, FastAPI + WebSocket server).
This file constrains two things only: **commit and PR text is written in Chinese**, and
**releases happen automatically**.

## 1. Commit (Chinese text)

- Follow Conventional Commits: `feat|fix|docs|chore|ci|refactor|test(scope): summary`.
- The type and scope stay in English; the summary and body are written in Chinese. No trailing
  period in the summary, and no space before the colon.
- One commit does one thing. For breaking changes, append `!` to the type and describe the
  migration in the body.
- Use the body for 背景 / 改动 / 影响 when useful; never restate the diff line by line.

Example:

```
feat(client): 主菜单新增检查更新入口

从 GitHub Releases 检索最新版本并跳转下载页。
```

## 2. Pull request (Chinese text)

- PR titles and bodies are written in Chinese; their content and structure are unconstrained.

## 3. Push

- Push only the current branch. No force pushes, no direct pushes to `main`. Rebase onto the remote
  when it has moved ahead.
- Before pushing, make sure the working tree is clean and the staged content matches the intended
  change only (no build artifacts, caches, `.coverage`).
- Before pushing, state the change summary in the conversation: what changed, what it affects, and
  how it was verified.

## 4. Automatic release

**Trigger**: a PR that changes `APP_VERSION` in `gomoku/client/constants.py` is a release PR. Once
that PR is merged into `main`, carry out the steps below without waiting for a separate
instruction, and report the resulting Release URL. Pushing the release tag is pre-authorized by
this rule.

**Steps** (the version bump and the README update belong to that PR — do not repeat them here):

1. Confirm `APP_VERSION` is `X.Y.Z` and the tag will be `vX.Y.Z`; the two must match.
2. Confirm the README has a line starting with `当前版本：` that states what the new version adds —
   the release notes are generated from that line.
3. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. `.github/workflows/release.yml` builds the Windows client, pushes the GHCR image, and creates
   the Release with `--latest`.

**If a PR does not change `APP_VERSION`**: ask the user whether to release. Do not bump the version
yourself unless asked.

Notes:

- The tag must match `APP_VERSION`, otherwise the pipeline fails immediately.
- The release is complete only after the pipeline succeeds. Verify with `gh release list` /
  `gh run list` that the Release exists and is marked as latest.
- Do not bypass the pipeline with a local `gh release create`.
