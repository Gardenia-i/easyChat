# Development guide

The active product entries are `sender.py` and `server.py`. They receive prepared text and optional base64 images for one Windows desktop WeChat group. Comparison of source documents, scheduling, and automatic replies are outside this project path. The HTTP service also serves `web/index.html` for integration testing.

Layers: `easychat_sender/contracts.py` validates text and image input; `delay.py` applies independent 2–10 second action waits and an additional 2–6 second pre-send wait per message; `profiles.py` stores version strategies (4.1.15.56 by default); `driver.py` operates UI Automation and pastes images as CF_DIB; `uia_wake.py` invokes the pinned upstream gate activator when the mmui tree is absent; `service.py` serializes access and stores request-id states in SQLite.

The original GUI and version modules are retained as legacy reference. See `README.legacy.md` and `CLAUDE.legacy.md`. The new sender needs only `requirements-sender.txt` and Python 3.10+.

Run `python -m pytest tests/test_sender.py`. These tests do not send to WeChat. UI Automation selectors and @ completion require validation on a logged-in interactive Windows desktop with the actual selected WeChat version. `submitted` means the send button was clicked, not that delivery was acknowledged.

`server.py` configures console and rotating UTF-8 file logs at `logs/easychat.log`. Use `--log-level DEBUG` for sampled waits. Logs include request IDs, stage transitions and foreground process/control classes, but never message text or base64 image bytes. A code change requires a server restart; request IDs already present in SQLite cannot be reused for a retry.
