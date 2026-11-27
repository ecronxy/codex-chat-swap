# Codex /swap

Sign in to a second account once with `codex-swap add second`. The launcher registers the current Codex login as `main`.

Open `codex-swap` for a new chat or `codex-swap resume` for an existing one. Enter `/swap` to switch to the next saved account. The selected model needs to be available on both accounts.

Add a third account with `codex-swap add third` and reopen the launcher to include it in the pool. Before sending a message, the launcher switches away from an account with 0% of its five-hour limit remaining when another account has usable quota.

The launcher uses the original `CODEX_HOME` for chats and configuration. The xswap bridge runs a separate local app-server and changes its in-memory login between turns. It handles `/swap` before the command can reach the model or a shell.

The terminal command is in [the Codex 0.172.0 patch](codex-0.172.0.patch). The bridge builds on [xswap 0.9.3](https://github.com/intellieffect/xswap/tree/v0.9.3) with [local launcher changes](xswap-0.9.3.patch).
