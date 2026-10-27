"""The only module that operates the Windows WeChat user interface."""

import re
import logging
import time
from contextlib import nullcontext
from io import BytesIO

from .contracts import ImagePayload, SendError, SendRequest
from .delay import DelayPolicy
from .profiles import WeChatProfile
from .uia_wake import wake_wechat_uia
from wechat_locale import WeChatLocale


logger = logging.getLogger("easychat.driver")


class WeChatUIDriver:
    request_id = None
    sent_count = 0

    def _uses_anchors(self):
        return bool(getattr(getattr(self, "profile", None), "anchored", False))

    def __init__(
        self, profile: WeChatProfile, hotkey: str, locale: str, delays: DelayPolicy
    ):
        try:
            import pyperclip
            import uiautomation as auto
        except ImportError as exc:
            raise SendError(
                "automation_unavailable",
                "缺少桌面自动化依赖，请安装 requirements-sender.txt",
            ) from exc

        self.auto = auto
        self.pyperclip = pyperclip
        self.profile = profile
        self.hotkey = hotkey
        if locale not in WeChatLocale.getSupportedLocales():
            raise SendError("invalid_config", f"不支持的微信界面语言：{locale}")
        self.locale = WeChatLocale(locale)
        self.delays = delays
        self.send_attempted = False
        self.click_in_progress = False
        self.sent_count = 0
        self.request_id = None

    def _perform(self, operation):
        self.delays.action()
        return operation()

    def _click(self, control):
        def click_control():
            x, y = control.GetPosition()
            self.auto.Click(x, y)

        self._perform(click_control)

    @staticmethod
    def _matches_group(value: str, group_name: str) -> bool:
        value = value.strip()
        return value == group_name or bool(
            re.fullmatch(rf"{re.escape(group_name)}\s*[（(]\d+[)）]", value)
        )

    @staticmethod
    def _control_names(control, depth=2):
        names = {control.Name or ""}
        if depth:
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            for child in children:
                names.update(WeChatUIDriver._control_names(child, depth - 1))
        return names

    def _mention_candidates(self, name: str):
        """Find visible WeChat list items whose own/child name exactly matches name.

        Comparing the tree before and after typing @ prevents an existing chat row
        with the same name from being mistaken for a newly opened suggestion.
        """
        focused = self.auto.GetFocusedControl()
        process_id = getattr(focused, "ProcessId", None)
        if not process_id:
            raise SendError("mention_not_found", "无法确定微信候选列表所在的进程")

        found = {}
        stack = [(self.auto.GetRootControl(), 0)]
        inspected = 0
        while stack and inspected < 3000:
            control, depth = stack.pop()
            inspected += 1
            current_pid = getattr(control, "ProcessId", None)
            if depth and current_pid not in (process_id, 0, None):
                continue
            control_type = getattr(control, "ControlTypeName", "") or ""
            class_name = getattr(control, "ClassName", "") or ""
            item_type = getattr(self.auto, "ListItemControl", None)
            is_item = (
                (isinstance(item_type, type) and isinstance(control, item_type))
                or "ListItem" in control_type
                or "ListItem" in class_name
            )
            if is_item and name in self._control_names(control):
                try:
                    position = tuple(control.GetPosition())
                except Exception:
                    position = None
                if position is not None:
                    signature = (class_name, control.Name or "", position)
                    found[signature] = control
            if depth < 16:
                try:
                    stack.extend((child, depth + 1) for child in control.GetChildren())
                except Exception:
                    continue
        return found

    def _focus_snapshot(self):
        """Identify the foreground process and control ancestry without reading names."""
        try:
            control = self.auto.GetFocusedControl()
        except Exception as exc:
            return False, [f"focus_error:{type(exc).__name__}"], "unknown"

        classes = []
        process_id = None
        for _ in range(10):
            if control is None:
                break
            try:
                classes.append(str(control.ClassName or "<empty>"))
                process_id = process_id or getattr(control, "ProcessId", None)
                control = control.GetParentControl()
            except Exception:
                break

        process_name = "unknown"
        if process_id:
            try:
                import psutil

                process_name = psutil.Process(process_id).name()
            except Exception:
                process_name = f"pid:{process_id}"
        is_wechat = any(name.lower().startswith("mmui") for name in classes)
        is_wechat = is_wechat or process_name.lower() in ("weixin.exe", "wechat.exe")
        return is_wechat, classes, process_name

    def _open_wechat(self):
        ready, classes, process_name = self._focus_snapshot()
        logger.info(
            "activation_initial request_id=%s ready=%s foreground=%s classes=%s hotkey=%s",
            self.request_id, ready, process_name, classes, self.hotkey,
        )
        if ready:
            return

        for attempt in range(1, 6):
            # Recheck after the random wait: another action may have brought
            # WeChat forward, and sending the toggle hotkey would hide it.
            self.delays.action()
            ready, classes, process_name = self._focus_snapshot()
            if ready:
                logger.info("activation_ready_before_hotkey request_id=%s attempt=%s", self.request_id, attempt)
                return
            logger.info(
                "activation_hotkey request_id=%s attempt=%s foreground=%s classes=%s",
                self.request_id, attempt, process_name, classes,
            )
            self.auto.SendKeys(self.hotkey)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                ready, classes, process_name = self._focus_snapshot()
                if ready:
                    logger.info(
                        "activation_success request_id=%s attempt=%s foreground=%s classes=%s",
                        self.request_id, attempt, process_name, classes,
                    )
                    return
                time.sleep(0.2)
            logger.warning(
                "activation_no_focus request_id=%s attempt=%s foreground=%s classes=%s",
                self.request_id, attempt, process_name, classes,
            )
        raise SendError(
            "wechat_not_ready",
            f"无法激活微信；当前前台进程是 {process_name}，控件类是 {classes[:3]}。请核对微信登录状态和显示/隐藏窗口快捷键",
        )

    def _ensure_control_tree(self):
        """Fail early when WeChat exposes only its Qt rendering shell."""
        root = self._main_root()
        classes = []
        stack = [(root, 0)]
        while stack and len(classes) < 30:
            control, depth = stack.pop()
            classes.append(getattr(control, "ClassName", "") or "")
            if depth < 5:
                stack.extend((child, depth + 1) for child in control.GetChildren())
        logger.info(
            "control_tree_probe request_id=%s root=%s sampled_nodes=%s has_mmui=%s",
            self.request_id, classes[0] if classes else "", len(classes),
            any(name.startswith("mmui::") for name in classes),
        )
        if (
            len(classes) <= 10
            and "MMUIRenderSubWindowHW" in classes
            and not any(name.startswith("mmui::") for name in classes)
        ):
            raise SendError(
                "wechat_uia_unavailable",
                "微信只暴露渲染窗口，未提供发送所需的控件树；当前 UIA 发送驱动无法操作此微信界面",
            )

    def _main_root(self):
        """Get a fresh UIA wrapper for the active WeChat main window."""
        focused = self.auto.GetFocusedControl()
        pid = getattr(focused, "ProcessId", None)
        root = focused
        for _ in range(20):
            parent = root.GetParentControl()
            if parent is None or getattr(parent, "ProcessId", None) != pid:
                break
            root = parent

        # Hot activation can replace the provider behind the same HWND. Obtain
        # a fresh UIA element instead of traversing a pre-activation wrapper.
        hwnd = getattr(root, "NativeWindowHandle", 0)
        if hwnd and hasattr(self.auto, "ControlFromHandle"):
            root = self.auto.ControlFromHandle(hwnd)
        return root

    def _find_controls(self, predicate, max_nodes=1500):
        """Find controls by stable class/id/name instead of desktop depth."""
        matches = []
        stack = [self._main_root()]
        inspected = 0
        while stack and inspected < max_nodes:
            control = stack.pop()
            inspected += 1
            try:
                if predicate(control):
                    matches.append(control)
                stack.extend(reversed(control.GetChildren()))
            except Exception:
                continue
        return matches

    def _find_one(self, predicate, label):
        matches = self._find_controls(predicate)
        if len(matches) != 1:
            logger.warning(
                "control_anchor_mismatch request_id=%s anchor=%s matches=%s",
                self.request_id, label, len(matches),
            )
            raise SendError("wechat_ui_changed", f"找不到唯一的微信控件：{label}（{len(matches)} 个）")
        return matches[0]

    def _chat_input(self):
        return self._find_one(
            lambda c: getattr(c, "AutomationId", "") == "chat_input_field"
            and getattr(c, "ClassName", "") == "mmui::ChatInputField",
            "群聊输入框",
        )

    def _current_chat_name(self):
        matches = self._find_controls(
            lambda c: (getattr(c, "AutomationId", "") or "").endswith(
                ".current_chat_name_label"
            )
            and getattr(c, "ClassName", "") == "mmui::XTextView"
        )
        return (matches[0].Name or "") if len(matches) == 1 else ""

    def _select_group_anchored(self, group_name: str):
        current = self._current_chat_name()
        group_title = self._find_controls(
            lambda c: getattr(c, "ClassName", "") == "mmui::ChatTitleBarChatRoomView"
        )
        if self._matches_group(current, group_name) and len(group_title) == 1:
            logger.info("group_already_open request_id=%s group=%r", self.request_id, group_name)
            return

        tab = self._find_one(
            lambda c: getattr(c, "ClassName", "") == "mmui::XTabBarItem"
            and getattr(c, "Name", "") == self.locale.weixin,
            "微信聊天页",
        )
        self._click(tab)
        search = self._find_controls(
            lambda c: getattr(c, "ClassName", "") == "mmui::XValidatorTextEdit"
            and getattr(c, "Name", "") == self.locale.search
        )
        if not search:
            button = self._find_one(
                lambda c: getattr(c, "ControlTypeName", "") == "ButtonControl"
                and getattr(c, "Name", "") == self.locale.search,
                "搜索入口",
            )
            self._click(button)
            search = self._find_controls(
                lambda c: getattr(c, "ClassName", "") == "mmui::XValidatorTextEdit"
                and getattr(c, "Name", "") == self.locale.search
            )
        if len(search) != 1:
            raise SendError("wechat_ui_changed", f"找不到唯一的会话搜索框（{len(search)} 个）")
        self._click(search[0])
        self._perform(lambda: self.auto.SendKeys("{Ctrl}a"))
        self._perform(lambda: self.pyperclip.copy(group_name))
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))
        self.delays.action()

        results = self._find_one(
            lambda c: getattr(c, "AutomationId", "") == "search_list"
            and getattr(c, "ControlTypeName", "") == "ListControl",
            "群聊搜索结果",
        )
        matches = []
        section = ""
        for item in results.GetChildren():
            class_name = getattr(item, "ClassName", "")
            if class_name == "mmui::XTableCell" and getattr(item, "Name", "") == self.locale.group_chat:
                section = self.locale.group_chat
            elif class_name == "mmui::SearchContentCellView":
                if section == self.locale.group_chat and self._matches_group(
                    getattr(item, "Name", "") or "", group_name
                ):
                    matches.append(item)
        logger.info(
            "group_search_result request_id=%s group=%r matches=%s",
            self.request_id, group_name, len(matches),
        )
        if not matches:
            raise SendError("group_not_found", f"没有找到名称完全匹配的群聊：{group_name}")
        if len(matches) > 1:
            raise SendError("group_ambiguous", f"找到多个同名搜索结果：{group_name}")
        self._click(matches[0])
        self.delays.action()
        current = self._current_chat_name()
        logger.info(
            "group_opened request_id=%s expected=%r actual=%r",
            self.request_id, group_name, current,
        )
        group_title = self._find_controls(
            lambda c: getattr(c, "ClassName", "") == "mmui::ChatTitleBarChatRoomView"
        )
        if not self._matches_group(current, group_name) or len(group_title) != 1:
            raise SendError("group_ambiguous", f"搜索结果进入的会话名称不匹配：{current}")

    def _select_group(self, group_name: str):
        logger.info("group_search_start request_id=%s group=%r", self.request_id, group_name)
        self._open_wechat()
        try:
            self._ensure_control_tree()
        except SendError as exc:
            if exc.status != "wechat_uia_unavailable":
                raise
            activated, detail = wake_wechat_uia(self.request_id)
            if not activated:
                raise SendError("wechat_uia_activation_failed", detail) from exc
            try:
                self._ensure_control_tree()
            except SendError as check_error:
                raise SendError(
                    "wechat_uia_activation_failed",
                    "热激活工具报告成功，但发送进程仍未读到 mmui 控件树",
                ) from check_error
        if self._uses_anchors():
            self._select_group_anchored(group_name)
            return
        self._click(self.auto.ButtonControl(Depth=6, ClassName="mmui::XTabBarItem"))
        self._click(
            self.auto.EditControl(Depth=self.profile.search_depth, Name=self.locale.search)
        )
        self._perform(lambda: self.auto.SendKeys("{Ctrl}a"))
        self._perform(lambda: self.pyperclip.copy(group_name))
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))

        # Wait for search results, then compare names rather than selecting row one.
        self.delays.action()
        results = self.auto.ListControl(Depth=self.profile.results_depth).GetChildren()
        matches = [
            item
            for item in results
            if "XTableCell" not in (item.ClassName or "")
            and any(
                self._matches_group(name, group_name)
                for name in self._control_names(item)
            )
        ]
        logger.info(
            "group_search_result request_id=%s group=%r rows=%s matches=%s",
            self.request_id, group_name, len(results), len(matches),
        )
        if not matches:
            raise SendError("group_not_found", f"没有找到名称完全匹配的群聊：{group_name}")
        if len(matches) > 1:
            raise SendError("group_ambiguous", f"找到多个同名搜索结果：{group_name}")

        self._click(matches[0])
        self.delays.action()
        current_name = self.auto.TextControl(Depth=20).Name or ""
        logger.info(
            "group_opened request_id=%s expected=%r actual=%r",
            self.request_id, group_name, current_name,
        )
        if not self._matches_group(current_name, group_name):
            raise SendError(
                "group_ambiguous", f"搜索结果进入的会话名称不匹配：{current_name}"
            )

        # Newer 4.1 builds need this click for folded group chats.
        if self.profile.toolbar_depth is not None:
            self._click(self.auto.ToolBarControl(Depth=self.profile.toolbar_depth))

    def _mention(self, name: str):
        if self._uses_anchors():
            return self._mention_anchored(name)
        logger.info("mention_start request_id=%s member=%r", self.request_id, name)
        existing = self._mention_candidates(name)
        self._perform(lambda: self.auto.SendKeys("@"))
        self._perform(lambda: self.pyperclip.copy(name))
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))
        self.delays.action()
        candidates = self._mention_candidates(name)
        new_candidates = [
            control for signature, control in candidates.items() if signature not in existing
        ]
        logger.info(
            "mention_candidates request_id=%s member=%r before=%s after=%s new=%s",
            self.request_id, name, len(existing), len(candidates), len(new_candidates),
        )
        if not new_candidates:
            raise SendError("mention_not_found", f"@ 候选中找不到群成员：{name}")
        if len(new_candidates) > 1:
            raise SendError("mention_ambiguous", f"@ 候选中存在多个同名成员：{name}")
        self._click(new_candidates[0])
        logger.info("mention_selected request_id=%s member=%r", self.request_id, name)

    def _mention_anchored(self, name: str):
        logger.info("mention_start request_id=%s member=%r", self.request_id, name)
        self._perform(lambda: self.auto.SendKeys("@"))
        self._perform(lambda: self.pyperclip.copy(name))
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))
        self.delays.action()
        lists = self._find_controls(
            lambda c: getattr(c, "AutomationId", "") == "chat_mention_list"
            and getattr(c, "ControlTypeName", "") == "ListControl"
        )
        if len(lists) != 1:
            raise SendError("mention_not_found", f"@ 候选列表未出现：{name}")
        candidates = [
            item for item in lists[0].GetChildren()
            if getattr(item, "ClassName", "") == "mmui::XTableCell"
            and (getattr(item, "Name", "") or "").strip() == name
        ]
        logger.info(
            "mention_candidates request_id=%s member=%r matches=%s",
            self.request_id, name, len(candidates),
        )
        if not candidates:
            raise SendError("mention_not_found", f"@ 候选中找不到群成员：{name}")
        if len(candidates) > 1:
            raise SendError("mention_ambiguous", f"@ 候选中存在多个同名成员：{name}")
        self._click(candidates[0])
        self.delays.action()
        if self._find_controls(
            lambda c: getattr(c, "AutomationId", "") == "chat_mention_list"
        ):
            raise SendError("mention_selection_uncertain", f"@ 候选仍然显示，无法确认已选中：{name}")
        logger.info("mention_selected request_id=%s member=%r", self.request_id, name)

    def _paste_body(self, content: str):
        logger.info("text_paste request_id=%s chars=%s", self.request_id, len(content))
        self._perform(lambda: self.pyperclip.copy(content))
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))

    def _paste_image(self, image: ImagePayload):
        logger.info(
            "image_paste request_id=%s format=%s bytes=%s",
            self.request_id, image.format, len(image.data),
        )
        try:
            import win32clipboard
            from PIL import Image
        except ImportError as exc:
            raise SendError(
                "automation_unavailable", "图片发送需要 Pillow 和 pywin32"
            ) from exc

        with Image.open(BytesIO(image.data)) as source:
            bmp = BytesIO()
            if "A" in source.getbands() or "transparency" in source.info:
                rgba = source.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = source.convert("RGB")
            rgb.save(bmp, format="BMP")
            dib = bmp.getvalue()[14:]  # CF_DIB omits the BMP file header.

        def place_image_on_clipboard():
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
            finally:
                win32clipboard.CloseClipboard()

        if self._uses_anchors():
            self._click(self._chat_input())
        self._perform(place_image_on_clipboard)
        self._perform(lambda: self.auto.SendKeys("{Ctrl}v"))

    def _reset_draft(self):
        # The automation session owns the composer. A failed earlier request may
        # have left an unsent @ token or message there.
        logger.info("draft_reset request_id=%s", self.request_id)
        if self._uses_anchors():
            self._click(self._chat_input())
        self._perform(lambda: self.auto.SendKeys("{Ctrl}a"))
        self._perform(lambda: self.auto.SendKeys("{Delete}"))

    def _send(self):
        logger.info("send_wait request_id=%s message_index=%s", self.request_id, self.sent_count + 1)
        self.delays.before_message()
        if self._uses_anchors():
            send_button = self._find_one(
                lambda c: getattr(c, "ClassName", "") == "mmui::XOutlineButton"
                and getattr(c, "Name", "") == self.locale.send,
                "发送按钮",
            )
        else:
            send_button = self.auto.ButtonControl(
                Depth=self.profile.send_depth, Name=self.locale.send
            )
        self.send_attempted = True
        self.click_in_progress = True
        try:
            self._click(send_button)
        except Exception as exc:
            raise SendError(
                "send_uncertain", "已尝试点击发送，但无法确认是否提交；请勿直接重试"
            ) from exc
        self.click_in_progress = False
        self.sent_count += 1
        logger.info("send_clicked request_id=%s sent_messages=%s", self.request_id, self.sent_count)

    def send(self, request: SendRequest) -> dict:
        self.request_id = request.request_id
        logger.info(
            "driver_start request_id=%s text=%s images=%s mentions=%s",
            self.request_id, bool(request.content.strip()), len(request.images), len(request.at_users),
        )
        initializer = getattr(self.auto, "UIAutomationInitializerInThread", None)
        context = initializer() if initializer else nullcontext()
        with context:
            self.auto.SetGlobalSearchTimeout(1.0)
            self._select_group(request.group_name)
            self._reset_draft()
            if request.content.strip():
                for name in request.at_users:
                    self._mention(name)
                self._paste_body(request.content)
                self._send()
            for image in request.images:
                self._paste_image(image)
                self._send()
        logger.info("driver_complete request_id=%s sent_messages=%s", self.request_id, self.sent_count)
        return {
            "status": "submitted",
            "message": "所有消息均已在目标会话点击发送；当前无法确认对方是否收到",
            "mention_selection": "ui_candidate_selected" if request.at_users else "not_requested",
        }
