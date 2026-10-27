"""Selectors copied from the three actively registered WeChat 4.1 engines."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WeChatProfile:
    search_depth: int
    results_depth: int
    send_depth: int
    toolbar_depth: int | None
    anchored: bool = False


PROFILES = {
    "微信 4.1.15.56": WeChatProfile(0, 0, 0, None, anchored=True),
    "微信 4.1.15.8": WeChatProfile(15, 5, 21, 18),
    "微信 4.1.9.21": WeChatProfile(14, 4, 21, 18),
    "微信 4.1.8.107": WeChatProfile(15, 4, 20, None),
}
DEFAULT_VERSION = "微信 4.1.15.56"
