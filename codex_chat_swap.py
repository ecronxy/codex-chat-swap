"""Manual account switching for an open Codex CLI conversation."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time

from xswap.cli import main as xswap
from xswap.core.errors import LiveError, SwapError
from xswap.core.quota import window_percent
from xswap.manager import Manager
from xswap.providers.codex import codex_cli
from xswap.providers.codex.live import AccountPool, IdentityMismatch, buckets_available, failure_reason, token_emails
from xswap.providers.codex.usage import CODEX_QUOTA, normalize_limits


ROOT = Path(__file__).resolve().parent
STATE = Path.home() / ".local/share/codex-chat-swap"
MARKER = ": codex-chat-swap"


class UnverifiedAccount(IdentityMismatch):
    def __init__(self, reason):
        LiveError.__init__(self, f"Could not verify the account ({reason}).")


class ManualBridge(codex_cli.WebSocketBridge):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.turns_idle = asyncio.Event()

    async def before_turn(self, model=None):
        if self.active:
            return
        if time.monotonic() - self.checked_at >= 30:
            try:
                _, self.last_quota = await asyncio.to_thread(self.pool.prepare, self.current)
                self.checked_at = time.monotonic()
            except Exception as error:
                self.status("quota-check-failed", exc=error)
                return
        if self.five_hour_remaining(self.last_quota) == 0:
            await self.choose({self.current}, model)

    async def continue_failed(self):
        if self.five_hour_remaining(self.last_quota) == 0:
            await super().continue_failed()
        else:
            self.failed.clear()

    @staticmethod
    def five_hour_remaining(raw):
        return window_percent(normalize_limits(raw or {}), 300, CODEX_QUOTA)

    async def choose(self, excluded, model=None, excluded_ids=None):
        seen_ids = set(excluded_ids or ())
        seen_ids.add(self.current_id)
        for name in self.pool.names:
            if name in excluded:
                continue
            try:
                credentials, raw = await asyncio.to_thread(self.pool.prepare, name)
                if credentials["chatgptAccountId"] in seen_ids:
                    continue
                remaining = self.five_hour_remaining(raw)
                if remaining is None or remaining <= 0:
                    continue
                if buckets_available(normalize_limits(raw), model) is not True:
                    continue
                await self.install(name, credentials, raw)
                self.emit({"method": "account/rateLimits/updated", "params": raw})
                return True
            except Exception as error:
                self.status("candidate-unavailable", exc=error, candidate=name)
        self.status("no-available-account", reason="no other account with usable five-hour quota")
        return False

    async def verify_identity(self, credentials):
        label, reason = await super().verify_identity(credentials)
        if label is None:
            raise UnverifiedAccount(reason)
        return label, reason

    async def send(self, message):
        if message.get("method") in {"thread/list", "thread/search"}:
            params = message.get("params") or {}
            message = {**message, "params": {**params, "currentAccountOnly": False}}
        await super().send(message)

    async def on_client(self, message):
        params = message.get("params") or {}
        if message.get("method") == "thread/shellCommand" and params.get("command") == MARKER:
            await self.swap(message["id"], params.get("threadId"))
            return
        await super().on_client(message)

    async def on_server(self, message):
        was_active = bool(self.active)
        await super().on_server(message)
        if was_active and not self.active:
            self.turns_idle.set()

    async def swap(self, request_id, thread_id):
        reply = {"id": request_id}
        async with self.gate:
            try:
                if not self.initialized or thread_id not in self.loaded_threads:
                    raise LiveError("Open a local chat before using /swap.")
                if thread_id in self.active:
                    raise LiveError("Wait for the current turn to finish, then enter /swap.")
                if self.active:
                    # The terminal also starts short background turns, such as naming the chat.
                    self.turns_idle.clear()
                    try:
                        await asyncio.wait_for(self.turns_idle.wait(), timeout=20)
                    except TimeoutError:
                        raise LiveError("Codex is still working in the background. Try /swap again once it finishes.") from None
                names = self.pool.names
                candidate = names[(names.index(self.current) + 1) % len(names)]
                credentials, limits = await asyncio.to_thread(
                    self.pool.prepare, candidate, require_quota=False,
                )
                if (
                    credentials["chatgptAccountId"] == self.current_id
                    and (self.verified_identity or "").casefold() in token_emails(credentials["accessToken"])
                ):
                    raise LiveError(f"{candidate} is the same ChatGPT account. Run codex-swap login {candidate}.")
                await self.install(candidate, credentials, limits)
                if limits is not None:
                    self.emit({"method": "account/rateLimits/updated", "params": limits})
                reply["result"] = {}
            except Exception as error:
                reason = failure_reason(error).replace("xswap login", "codex-swap login")
                reply["error"] = {"code": -32000, "message": f"/swap: {reason}"}
        self.emit(reply)


def main(args):
    if args in (["-h"], ["--help"]):
        print(
            "codex-swap add NAME   sign in to another ChatGPT account once\n"
            "codex-swap login NAME   refresh a saved login\n"
            "codex-swap [resume [ID]]   open Codex, then enter /swap in the chat"
        )
        return 0

    manager = Manager(root=STATE)
    os.environ["CODEX_SWAP_HOME"] = str(manager.root)
    if "main" not in manager.read()["accounts"]:
        manager.register("main", manager.source)
    if args and args[0] in ("add", "login"):
        return xswap(args)

    names = [name for name, home in manager.enabled_accounts() if (home / "auth.json").is_file()]
    if len(names) < 2:
        raise LiveError("Sign in to the second account once with: codex-swap add second")
    if not codex_cli.interactive_args(args):
        raise LiveError("Use codex-swap for interactive chats or to resume a chat.")

    binary = ROOT / "bin/codex"
    if not binary.is_file():
        raise LiveError("The Codex client with /swap has not finished building.")
    official = manager.codex()
    pool = AccountPool(manager, names, official)
    env = manager.env(manager.source)
    env["CODEX_CHAT_SWAP"] = "1"
    run_dir = codex_cli.new_run_dir(manager)
    with tempfile.TemporaryDirectory(prefix="codex-swap-", dir="/tmp") as temporary:
        return asyncio.run(
            codex_cli.serve_cli(
                pool, str(binary), args, env, run_dir / "status.json",
                Path(temporary) / "rpc.sock", bridge_class=ManualBridge,
                resume_launcher="codex-swap", server_executable=official,
            ),
        )


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (LiveError, SwapError) as error:
        sys.exit(f"codex-swap: {error}")
