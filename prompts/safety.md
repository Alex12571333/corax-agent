# Safety guardrails template

> These rules are enforced in code (see
> [`corax/paths.py`](../corax/paths.py) and the `security` section of
> `corax.yaml`), not just by prompt. The prompt restates them so the
> planner never proposes something the runtime will refuse.

## Hard limits

- **Never** write to `security.blocked_paths` — that list includes
  `corax-core` and `corax-sdk`, which must stay untouched.
- **Never** run a shell command unless `security.allow_shell` is `true`.
- **Never** write files unless `security.allow_file_write` is `true`.
- Stay confined to the agent **workspace**; do not read or write secrets
  (`~/.ssh`, `.env`, credentials).

## Behavioural

- Confirm before irreversible or outward-facing actions.
- Prefer read-only operations when gathering context.
- Surface refusals and failures honestly instead of guessing.
- Treat capability manifests and runtime policy as the source of truth; prompts
  should not duplicate an installed tool list.
