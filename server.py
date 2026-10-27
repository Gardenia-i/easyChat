"""Local HTTP entry point for integration testing and the send form."""

import argparse
import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from easychat_sender import SenderConfig, SenderService
from easychat_sender.logging_setup import configure_logging


PAGE = Path(__file__).resolve().parent / "web" / "index.html"
MAX_REQUEST_BYTES = 50 * 1024 * 1024
logger = logging.getLogger("easychat.http")


class SendHandler(BaseHTTPRequestHandler):
    server_version = "easyChat/1.0"

    def log_message(self, format, *args):
        logger.info("http_access client=%s %s", self.client_address[0], format % args)

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            data = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/health":
            config = self.server.sender_service.config
            self._json(200, {
                "ok": True,
                "version": config.version,
                "hotkey": config.hotkey,
                "action_delay_range": config.action_delay_range,
                "message_delay_range": config.message_delay_range,
            })
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"ok": False, "status": "not_found", "message": "路径不存在"})

    def do_POST(self):
        started = time.monotonic()
        if urlsplit(self.path).path != "/api/send":
            self._json(404, {"ok": False, "status": "not_found", "message": "路径不存在"})
            return
        required_token = self.server.api_token
        if required_token and not hmac.compare_digest(
            self.headers.get("X-API-Token", ""), required_token
        ):
            self._json(401, {"ok": False, "status": "unauthorized", "message": "接口令牌不正确"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"ok": False, "status": "invalid_request", "message": "请使用 application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"ok": False, "status": "invalid_request", "message": "缺少 Content-Length"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(413, {"ok": False, "status": "invalid_request", "message": "请求体过大"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"ok": False, "status": "invalid_request", "message": "JSON 格式错误"})
            return
        if isinstance(payload, dict):
            logger.info(
                "http_send_start request_id=%r body_bytes=%s image_count=%s",
                str(payload.get("request_id"))[:100], length,
                len(payload.get("images", [])) if isinstance(payload.get("images"), list) else "invalid",
            )
        result = self.server.sender_service.send_group_message(payload)
        http_status = {
            "submitted": 200,
            "invalid_request": 400,
            "duplicate_request": 409,
            "group_not_found": 404,
            "mention_not_found": 422,
            "mention_ambiguous": 422,
            "group_ambiguous": 422,
            "automation_unavailable": 503,
            "wechat_not_ready": 503,
            "partial_send": 409,
            "send_uncertain": 409,
        }.get(result["status"], 500)
        logger.info(
            "http_send_done request_id=%r status=%s http_status=%s elapsed_seconds=%.2f",
            str(result.get("request_id"))[:100], result.get("status"),
            http_status, time.monotonic() - started,
        )
        self._json(http_status, result)


def create_server(
    host="127.0.0.1", port=8765, *, service=None, api_token=""
) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1") and not api_token:
        raise ValueError("监听非本机地址时必须设置 EASYCHAT_API_TOKEN")
    server = ThreadingHTTPServer((host, port), SendHandler)
    server.daemon_threads = True
    server.sender_service = service or SenderService()
    server.api_token = api_token
    return server


def main():
    parser = argparse.ArgumentParser(description="easyChat 本地发送服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", default=SenderConfig.version)
    parser.add_argument("--hotkey", default=SenderConfig.hotkey)
    parser.add_argument("--locale", default=SenderConfig.locale)
    parser.add_argument("--log-level", choices=("INFO", "DEBUG"), default="INFO")
    args = parser.parse_args()
    log_path = configure_logging(args.log_level)
    token = os.environ.get("EASYCHAT_API_TOKEN", "")
    service = SenderService(SenderConfig(
        version=args.version,
        hotkey=args.hotkey,
        locale=args.locale,
    ))
    try:
        server = create_server(args.host, args.port, service=service, api_token=token)
    except ValueError as exc:
        parser.error(str(exc))
    logger.info(
        "server_started url=http://%s:%s version=%s hotkey=%s locale=%s log_file=%s",
        args.host, server.server_port, args.version, args.hotkey, args.locale, log_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
