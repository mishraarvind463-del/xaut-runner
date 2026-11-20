# XAUT Runner — Current Status

> Automatically refreshed after pushes to `main`. This is commit metadata, not a security or workflow validation result.

- Update timestamp (UTC): 2026-11-20T11:08:24+05:30
- Branch: `main`
- Commit: `ccd9e2dcf3711a164820418dadf2d14a7f4053fe`
- Commit message: Reject duplicate trade IDs across v4 active and legacy outcomes
- Commit author: mishraarvind463-del
- Context review: **REQUIRED — runner source/config/workflow changed.** Review the diff and confirm the private bot ref, paper default, IP-whitelist wait and secrets usage.
- Triggering workflow: https://github.com/mishraarvind463-del/xaut-runner/actions/runs/44682365137

## Changed paths
- `M	scripts/validate_recovery_state.py`

## Required before changing runner behaviour
1. Review `.github/workflows/setup-a.yml` and the exact diff.
2. Confirm private checkout stays on `ai-managed-trading-v1` unless the user approves otherwise.
3. Keep live trading default false and preserve manual dispatch / IP whitelist safeguards unless explicitly approved.
4. Do not start the Delta-connected trading workflow as part of documentation maintenance.
