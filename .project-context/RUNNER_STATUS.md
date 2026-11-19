# XAUT Runner — Current Status

> Initial baseline. Automatically refreshed after pushes to runner `main`.

- Repository: `mishraarvind463-del/xaut-runner`
- Branch: `main`
- Audited HEAD: `177a0143cab2afb668cee2f08bafa5fd4084d20a`
- Private bot checkout: `mishraarvind463-del/xaut-bot`, branch `ai-managed-trading-v1`
- Main workflow: `.github/workflows/setup-a.yml` — manual Run XAUT Bot dispatch.
- Safety defaults verified at audit: live trading false; runtime choices 5/10/30/60/120/350 minutes; 90-second Delta IP-whitelist wait; 360-minute job timeout.
- This status file is updated automatically with the latest runner commit and changed paths. It does not interpret workflow semantics or validate secrets.
- Read the private bot repository's `PROJECT_CONTEXT.md` and `.project-context/HANDOVER.md` for the complete project context.

## Required review
Check the workflow diff and verify its private-bot checkout ref and all safety gates after any runner workflow/configuration change. Never dispatch the Delta-connected Run XAUT Bot workflow without explicit user approval.
