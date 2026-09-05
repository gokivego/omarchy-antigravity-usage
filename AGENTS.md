# Omarchy Antigravity usage

Omarchy Quattro shell plugin that feeds Google Antigravity (`agy`) into the stock AI toolbar widget. Scope work to `omarchy-antigravity-usage/`.

This project is a Git subtree in the Gokivego workspace.

- **Upstream remote:** `omarchy-antigravity-usage` (`https://github.com/gokivego/omarchy-antigravity-usage.git`)
- **Primary branch:** `main`
- **Subtree directory:** `omarchy-antigravity-usage/`
- **Pinned marketplace SHA:** `898f8e35d47426c3b35a0026dc5f268bbd412b4a` ([omacom/omarchy-plugin-marketplace#5026](https://github.com/omacom/omarchy-plugin-marketplace/issues/5026))

When changing this directory:

1. Test locally using [`README.md`](./README.md). `omarchy plugin validate` must pass.
2. Record meaningful changes in [`LOG.md`](./LOG.md).
3. Commit in the workspace repo.
4. Do not `git subtree push` until issue #5026 has `approved-and-verified`. That review is bound to `898f8e3`. A squash-split push would mint a new SHA on the public remote.
5. After that label lands, push upstream:
   ```bash
   git subtree push --prefix=omarchy-antigravity-usage omarchy-antigravity-usage main
   ```
6. Pull upstream:
   ```bash
   git subtree pull --prefix=omarchy-antigravity-usage omarchy-antigravity-usage main --squash
   ```
