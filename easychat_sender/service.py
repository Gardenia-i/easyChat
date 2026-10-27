"""Validated, serialized Python function for sending one group message."""

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import SendError, SendRequest
from .delay import DelayPolicy
from .driver import WeChatUIDriver
from .profiles import DEFAULT_VERSION, PROFILES


logger = logging.getLogger("easychat.service")


@dataclass(frozen=True)
class SenderConfig:
    version: str = DEFAULT_VERSION
    hotkey: str = "{Ctrl}{Alt}w"
    locale: str = "zh-CN"
    action_delay_range: tuple[float, float] = (2.0, 10.0)
    message_delay_range: tuple[float, float] = (2.0, 6.0)
    state_db_path: str | Path = "sender_state.sqlite3"


class SenderService:
    # The desktop, foreground window and clipboard are shared by all instances.
    _desktop_lock = threading.Lock()

    def __init__(self, config: SenderConfig | None = None, *, driver_factory=None, delays=None):
        self.config = config or SenderConfig()
        if self.config.version not in PROFILES:
            raise ValueError(f"不支持的微信版本：{self.config.version}")
        if not self.config.hotkey:
            raise ValueError("hotkey 不能为空")
        self.delays = delays or DelayPolicy(
            self.config.action_delay_range, self.config.message_delay_range
        )
        self.driver_factory = driver_factory or WeChatUIDriver

    @staticmethod
    def _fingerprint(request: SendRequest) -> str:
        data = json.dumps(
            [
                request.group_name,
                request.at_users,
                request.content,
                [
                    (image.filename, hashlib.sha256(image.data).hexdigest())
                    for image in request.images
                ],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _connect_db(self):
        connection = sqlite3.connect(self.config.state_db_path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "status TEXT NOT NULL, updated_at TEXT NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP)"
            )
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _result(request_id, status: str, message: str, **extra) -> dict:
        result = {
            "request_id": request_id,
            "ok": status == "submitted",
            "status": status,
            "message": message,
        }
        result.update(extra)
        return result

    def send_group_message(self, payload: Mapping[str, Any]) -> dict:
        try:
            request = SendRequest.from_payload(payload)
        except SendError as exc:
            request_id = payload.get("request_id") if isinstance(payload, Mapping) else None
            logger.warning("request_rejected request_id=%r status=%s reason=%s", request_id, exc.status, exc)
            return self._result(request_id, exc.status, str(exc))

        with self._desktop_lock:
            logger.info(
                "request_start request_id=%s version=%s group=%r mentions=%s text_chars=%s images=%s total_messages=%s",
                request.request_id, self.config.version, request.group_name,
                len(request.at_users), len(request.content), len(request.images), request.total_messages,
            )
            try:
                if request.request_id:
                    fingerprint = self._fingerprint(request)
                    with closing(self._connect_db()) as connection, connection:
                        previous = connection.execute(
                            "SELECT fingerprint, status FROM requests WHERE request_id = ?",
                            (request.request_id,),
                        ).fetchone()
                        if previous:
                            if previous[0] != fingerprint:
                                logger.warning("request_id_conflict request_id=%s", request.request_id)
                                return self._result(
                                    request.request_id,
                                    "invalid_request",
                                    "request_id 已用于另一条消息",
                                )
                            logger.info(
                                "request_duplicate request_id=%s previous_status=%s",
                                request.request_id, previous[1],
                            )
                            return self._result(
                                request.request_id,
                                "duplicate_request",
                                "该 request_id 已处理或正在处理，不会重复发送",
                                previous_status=previous[1],
                            )
                        connection.execute(
                            "INSERT INTO requests (request_id, fingerprint, status) "
                            "VALUES (?, ?, 'in_progress')",
                            (request.request_id, fingerprint),
                        )

                driver = None
                try:
                    driver = self.driver_factory(
                        PROFILES[self.config.version],
                        self.config.hotkey,
                        self.config.locale,
                        self.delays,
                    )
                    details = driver.send(request)
                    status = details["status"]
                    message = details["message"]
                    extra = {k: v for k, v in details.items() if k not in ("status", "message")}
                except SendError as exc:
                    status = exc.status
                    if driver and getattr(driver, "sent_count", 0) and status != "send_uncertain":
                        status = "partial_send"
                    message = str(exc)
                    extra = {}
                    logger.warning(
                        "driver_rejected request_id=%s status=%s reason=%s sent_messages=%s",
                        request.request_id, status, exc, getattr(driver, "sent_count", 0),
                    )
                except Exception as exc:
                    if driver and getattr(driver, "click_in_progress", False):
                        status = "send_uncertain"
                    elif driver and getattr(driver, "sent_count", 0):
                        status = "partial_send"
                    else:
                        status = "send_uncertain" if driver and driver.send_attempted else "send_failed"
                    message = f"微信界面操作失败：{exc}"
                    extra = {}
                    logger.exception(
                        "driver_exception request_id=%s status=%s sent_messages=%s",
                        request.request_id, status, getattr(driver, "sent_count", 0),
                    )

                if driver:
                    extra["sent_messages"] = getattr(driver, "sent_count", 0)
                    extra["total_messages"] = request.total_messages

                if request.request_id:
                    try:
                        with closing(self._connect_db()) as connection, connection:
                            connection.execute(
                                "UPDATE requests SET status = ?, updated_at = CURRENT_TIMESTAMP "
                                "WHERE request_id = ?",
                                (status, request.request_id),
                            )
                    except sqlite3.Error as exc:
                        logger.exception("state_update_failed request_id=%s", request.request_id)
                        return self._result(
                            request.request_id,
                            "send_uncertain" if driver and driver.send_attempted else "state_error",
                            f"发送流程结束，但请求状态记录失败：{exc}",
                        )
                logger.info(
                    "request_done request_id=%s status=%s sent_messages=%s total_messages=%s",
                    request.request_id, status, extra.get("sent_messages", 0), request.total_messages,
                )
                return self._result(request.request_id, status, message, **extra)
            except sqlite3.Error as exc:
                logger.exception("state_open_failed request_id=%s", request.request_id)
                return self._result(
                    request.request_id, "state_error", f"请求状态存储失败：{exc}"
                )


_default_service = SenderService()


def send_group_message(payload: Mapping[str, Any]) -> dict:
    """Send one prepared message; payload is an already decoded JSON object."""
    return _default_service.send_group_message(payload)
