"""Central async subprocess runner with validation and structured logging.

Provides `run_command(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]`.
All callers should use this instead of `shell=True` or ad-hoc `create_subprocess_*`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Tuple, List

logger = logging.getLogger("aihomecloud.subproc")


async def run_command(cmd: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    """Run a command safely with `shell=False` semantics.

    - `cmd` must be a non-empty list of program + args.
    - returns (returncode, stdout, stderr) as strings.
    - logs a structured warning on non-zero exit.

    No metacharacter filtering on args: create_subprocess_exec never invokes a shell, so a token
    like "a;b" or "$(whoami)" is passed to the program as one literal argument — never parsed or
    executed as shell syntax — regardless of its contents. A prior version rejected tokens
    containing `;&|`$` defensively, but that was dead-weight on top of exec's own safety and a
    real functional bug waiting to happen: a legitimate arg containing one of those characters
    (e.g. an nmcli Wi-Fi password with a `$`) would raise ValueError instead of running.
    """
    if not cmd or not isinstance(cmd, list):
        raise ValueError("cmd must be a non-empty list of strings")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            logger.warning("cmd_timeout", extra={"cmd": cmd, "timeout": timeout})
            return -1, "", "timeout"

        rc = proc.returncode
        s_out = stdout.decode().strip() if stdout else ""
        s_err = stderr.decode().strip() if stderr else ""

        if rc != 0:
            # Structured log for non-zero exit
            logger.warning("cmd_failed", extra={"cmd": cmd, "rc": rc, "stderr": s_err})

        return rc, s_out, s_err

    except FileNotFoundError:
        logger.warning("cmd_not_found", extra={"cmd": cmd})
        return -1, "", "not_found"
    except ValueError:
        raise
    except Exception as e:
        logger.error("cmd_error", extra={"cmd": cmd, "error": str(e)})
        return -1, "", str(e)
