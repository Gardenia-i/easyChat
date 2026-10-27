"""Invoke the pinned upstream WeChat UIA gate activator only when needed."""

import logging
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger("easychat.uia_wake")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_SCRIPT = PROJECT_ROOT / "vendor" / "wechatauto_replica" / "wake_uia.py"
GATE_CACHE = PROJECT_ROOT / "diagnostics" / "gate_cache.json"


def wake_wechat_uia(request_id: str | None) -> tuple[bool, str]:
    """Try to expose mmui controls; the upstream tool verifies the UIA tree."""
    GATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-X", "utf8",
        str(UPSTREAM_SCRIPT),
        "--cache", str(GATE_CACHE),
        "--timeout", "5",
    ]
    logger.info("uia_wake_start request_id=%s", request_id)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.exception("uia_wake_exception request_id=%s", request_id)
        return False, f"热激活脚本未能完成：{type(exc).__name__}"

    output = (completed.stdout + "\n" + completed.stderr).strip()
    logger.log(
        logging.INFO if completed.returncode == 0 else logging.WARNING,
        "uia_wake_done request_id=%s exit_code=%s output=%s",
        request_id, completed.returncode, output,
    )
    if completed.returncode == 0:
        return True, "mmui 控件树已激活"
    last_line = next((line for line in reversed(output.splitlines()) if line.strip()), "没有诊断输出")
    return False, last_line[:300]
