"""
Green Agent — main entry point.

Boots the Telegram bot and the weekly APScheduler on the same
asyncio event loop. Both run concurrently until interrupted.

Conflict-prevention strategy  ("last one wins")
------------------------------------------------
1. Kill all siblings  — on startup, find every other python process
   running main.py and send SIGTERM (then SIGKILL if needed).
2. Force-claim via curl — BEFORE run_polling, call getUpdates via a
   blocking HTTP request (no asyncio) so we break any active session.
3. Webhook cleanup    — deleteWebhook(drop_pending_updates=True) is
   called inside _post_init.
4. Conflict handler   — if a 409 still arrives the bot shuts down
   gracefully instead of spamming the log.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import json

from config import TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN  # noqa: F401
from services.telegram_bot import build_application

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------
_PID_FILE = os.path.join(tempfile.gettempdir(), "city_council_bot.pid")


# ---------------------------------------------------------------------------
# Sibling-killer helpers
# ---------------------------------------------------------------------------

_SCRIPT_PATH = os.path.realpath(__file__)


def _ancestor_pids(parent_of: dict[int, int]) -> set[int]:
    """Return our own PID plus every ancestor (e.g. the launching shell)."""
    pids: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        pid = parent_of.get(pid, 0)
    return pids


def _process_cwd(pid: int) -> str | None:
    """Return the working directory of *pid* (via lsof), or None if unknown."""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return None


def _runs_this_script(pid: int, args: str) -> bool:
    """True if *args* is a python process running THIS project's main.py."""
    if "python" not in args.lower():
        return False
    for token in args.split()[1:]:
        if os.path.basename(token) != "main.py":
            continue
        if os.path.isabs(token):
            if os.path.realpath(token) == _SCRIPT_PATH:
                return True
        else:
            cwd = _process_cwd(pid)
            if cwd and os.path.realpath(os.path.join(cwd, token)) == _SCRIPT_PATH:
                return True
    return False


def _list_processes() -> list[tuple[int, int, str]]:
    """Return (pid, ppid, args) for every process."""
    import subprocess
    procs: list[tuple[int, int, str]] = []
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="],
        capture_output=True, text=True, timeout=5,
    )
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            procs.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return procs


def _find_sibling_pids() -> list[int]:
    """Return PIDs of other processes running this project's main.py.

    Our own ancestors are skipped: the launching shell (e.g. ``zsh -c
    "python main.py"``) also matches and must not be killed.
    """
    siblings: list[int] = []
    try:
        procs = _list_processes()
        own = _ancestor_pids({pid: ppid for pid, ppid, _ in procs})
        for pid, _ppid, args in procs:
            if pid not in own and _runs_this_script(pid, args):
                siblings.append(pid)
    except Exception as exc:
        logger.warning("Could not list sibling processes: %s", exc)

    return siblings


def _kill_siblings() -> None:
    """Kill every other bot instance."""
    siblings = _find_sibling_pids()

    # Also check the PID file
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as fh:
                pid_from_file = int(fh.read().strip())
            # Only trust the PID file if that PID is still running this
            # script — a stale file may point at a reused, unrelated PID.
            if pid_from_file != os.getpid() and pid_from_file not in siblings:
                args = next(
                    (a for p, _, a in _list_processes() if p == pid_from_file), ""
                )
                if _runs_this_script(pid_from_file, args):
                    siblings.append(pid_from_file)
        except Exception:
            pass

    if not siblings:
        logger.info("No sibling bot instances found.")
    else:
        logger.warning(
            "Found %d sibling bot instance(s): PIDs %s — terminating …",
            len(siblings), siblings,
        )
        for pid in siblings:
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("  SIGTERM → PID %s", pid)
            except ProcessLookupError:
                logger.info("  PID %s already gone.", pid)
            except PermissionError:
                logger.error("  No permission to kill PID %s.", pid)

        time.sleep(3)
        for pid in siblings:
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                logger.warning("  SIGKILL → PID %s (did not exit in time)", pid)
            except ProcessLookupError:
                pass

        logger.info("All local sibling instances terminated.")

    try:
        os.remove(_PID_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------

def _write_pid() -> None:
    with open(_PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))
    logger.info("PID written to %s.", _PID_FILE)


def _release_pid_lock() -> None:
    try:
        with open(_PID_FILE) as fh:
            if int(fh.read().strip()) == os.getpid():
                os.remove(_PID_FILE)
                logger.info("PID lock released.")
    except (FileNotFoundError, ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Synchronous Telegram session claim (no asyncio — runs before PTB's loop)
# ---------------------------------------------------------------------------

def _sync_claim_session(token: str) -> None:
    """Break any active polling session using plain urllib (synchronous).

    Calls deleteWebhook then getUpdates with timeout=0 in a retry loop
    until we get two consecutive 200 OK responses.  This runs BEFORE
    run_polling so PTB's event loop is not yet open.
    """
    base = f"https://api.telegram.org/bot{token}"

    def _post(endpoint: str, data: dict | None = None) -> dict:
        url = f"{base}/{endpoint}"
        payload = json.dumps(data or {}).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # Step 1: delete webhook
    logger.info("[sync] Deleting webhook …")
    try:
        _post("deleteWebhook", {"drop_pending_updates": True})
        logger.info("[sync] Webhook deleted.")
    except Exception as exc:
        logger.warning("[sync] deleteWebhook failed: %s", exc)

    # Step 2: claim session — retry until 2 consecutive 200 OK
    logger.info("[sync] Claiming Telegram polling session …")
    consecutive_ok = 0
    attempt = 0

    while consecutive_ok < 2:
        attempt += 1
        try:
            result = _post("getUpdates", {"offset": -1, "timeout": 0, "limit": 1})
            if result.get("ok"):
                consecutive_ok += 1
                logger.info("[sync] Claim attempt %d: 200 OK (%d/2) ✓", attempt, consecutive_ok)
            else:
                consecutive_ok = 0
                logger.warning("[sync] Claim attempt %d: non-ok response — retrying …", attempt)
                time.sleep(2)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                consecutive_ok = 0
                logger.warning(
                    "[sync] Claim attempt %d: 409 Conflict — another poller active, "
                    "retrying in 3 s …", attempt,
                )
                time.sleep(3)
            else:
                logger.warning("[sync] Claim attempt %d: HTTP %s — retrying …", attempt, exc.code)
                time.sleep(2)
        except Exception as exc:
            consecutive_ok = 0
            logger.warning("[sync] Claim attempt %d: %s — retrying …", attempt, exc)
            time.sleep(2)

    logger.info("[sync] Session claimed after %d attempt(s).", attempt)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Initialise and start all subsystems."""

    # 0a. Kill every other LOCAL bot instance
    _kill_siblings()

    # 0b. Register our PID
    _write_pid()

    # Register cleanup for both normal exit and SIGTERM
    import atexit
    atexit.register(_release_pid_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # 0c. Claim the Telegram session synchronously BEFORE PTB starts its loop
    _sync_claim_session(TELEGRAM_BOT_TOKEN)

    # 1. Build the Telegram bot application
    app = build_application()

    # 3. Register lifecycle hooks
    async def _post_init(application) -> None:  # noqa: ANN001

        from config import TELEGRAM_CHAT_ID
        try:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    "🌿 *Hello! I am Green Agent.* 🌿\n\n"
                    "I have just been activated. I'm here to help you monitor city air quality "
                    "and provide environmental insights. You can ask me anything about "
                    "pollution or type 'report' to see a summary of recent changes.\n\n"
                    "How can I assist you today?"
                ),
                parse_mode="Markdown",
            )
            logger.info("Initial greeting sent to %s", TELEGRAM_CHAT_ID)
        except Exception as e:
            logger.warning(
                "Could not send initial greeting: %s. "
                "The user may need to message the bot first.", e
            )

    async def _post_shutdown(application) -> None:  # noqa: ANN001
        pass

    async def _error_handler(update, context) -> None:  # noqa: ANN001
        from telegram.error import Conflict
        if isinstance(context.error, Conflict):
            logger.critical(
                "Conflict during normal polling — restarting the bot will reclaim the session."
            )
            _release_pid_lock()
            os.kill(os.getpid(), signal.SIGINT)
        else:
            logger.error("Unhandled update error: %s", context.error, exc_info=context.error)

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    app.add_error_handler(_error_handler)

    # 4. Start polling
    logger.info("Handing control to run_polling …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
