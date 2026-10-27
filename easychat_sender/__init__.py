"""Send prepared text to a desktop WeChat group."""

from .service import SenderConfig, SenderService, send_group_message

__all__ = ["SenderConfig", "SenderService", "send_group_message"]
