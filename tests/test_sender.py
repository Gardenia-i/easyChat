import base64
import json
import sys
import threading
import time
import urllib.request
from io import BytesIO

import pytest
from PIL import Image

from easychat_sender import SenderConfig, SenderService
from easychat_sender.contracts import ImagePayload, SendError, SendRequest
from easychat_sender.delay import DelayPolicy
from easychat_sender.driver import WeChatUIDriver
from easychat_sender.profiles import PROFILES
from server import create_server


PAYLOAD = {
    "request_id": "compare-001",
    "group_name": "资产核对群",
    "at_users": ["张三", "李四"],
    "content": "新增 2 条，变更 3 条。",
}


class FakeDriver:
    calls = []

    def __init__(self, profile, hotkey, locale, delays):
        self.send_attempted = False
        self.sent_count = 0

    def send(self, request):
        self.calls.append(request)
        self.send_attempted = True
        self.sent_count = request.total_messages
        return {
            "status": "submitted",
            "message": "已点击发送",
            "mention_selection": "ui_candidate_selected",
        }


def test_contract_and_persistent_duplicate_guard(tmp_path):
    FakeDriver.calls = []
    config = SenderConfig(state_db_path=tmp_path / "state.sqlite3")
    first = SenderService(config, driver_factory=FakeDriver).send_group_message(PAYLOAD)
    second = SenderService(config, driver_factory=FakeDriver).send_group_message(PAYLOAD)

    assert first["ok"] is True
    assert first["status"] == "submitted"
    assert second["status"] == "duplicate_request"
    assert second["previous_status"] == "submitted"
    assert len(FakeDriver.calls) == 1
    assert FakeDriver.calls[0] == SendRequest(
        "资产核对群", ("张三", "李四"), "新增 2 条，变更 3 条。", "compare-001"
    )

    changed = dict(PAYLOAD, content="另一条内容")
    assert SenderService(config, driver_factory=FakeDriver).send_group_message(changed)[
        "status"
    ] == "invalid_request"


def test_invalid_payload_never_calls_driver(tmp_path):
    FakeDriver.calls = []
    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=FakeDriver,
    )
    bad = dict(PAYLOAD, at_users=["张三", " 张三 "])
    assert service.send_group_message(bad)["status"] == "invalid_request"
    assert FakeDriver.calls == []


def test_failure_after_send_attempt_is_uncertain(tmp_path):
    class BrokenDriver(FakeDriver):
        def send(self, request):
            self.send_attempted = True
            raise RuntimeError("界面失去响应")

    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=BrokenDriver,
    )
    result = service.send_group_message(PAYLOAD)
    assert result["status"] == "send_uncertain"
    assert result["ok"] is False
    assert service.send_group_message(PAYLOAD)["status"] == "duplicate_request"


def test_independent_random_waits_and_extra_presend_wait():
    sampled = []
    slept = []

    def sampler(low, high):
        sampled.append((low, high))
        return low + len(sampled) / 10

    delays = DelayPolicy(sampler=sampler, sleeper=slept.append)
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.delays = delays
    driver.profile = type("Profile", (), {"send_depth": 21})()
    driver.locale = type("Locale", (), {"send": "发送"})()
    driver.auto = type(
        "Auto", (), {"ButtonControl": lambda self, **kwargs: object()}
    )()
    driver.send_attempted = False
    driver.sent_count = 0
    driver._click = lambda control: delays.action()

    delays.action()
    delays.action()
    driver._send()

    assert sampled == [(2.0, 10.0), (2.0, 10.0), (2.0, 6.0), (2.0, 10.0)]
    assert len(set(slept)) == 4
    assert driver.send_attempted is True


def test_group_name_requires_exact_match_or_member_count():
    match = WeChatUIDriver._matches_group
    assert match("资产核对群", "资产核对群")
    assert match("资产核对群 (12)", "资产核对群")
    assert match("资产核对群（12）", "资产核对群")
    assert not match("资产核对群2", "资产核对群")


def test_wechat_focus_recognizes_an_mmui_parent(monkeypatch):
    class Control:
        def __init__(self, class_name, parent=None):
            self.ClassName = class_name
            self.ProcessId = 999999
            self.parent = parent

        def GetParentControl(self):
            return self.parent

    parent = Control("mmui::XWindow")
    child = Control("Edit", parent)
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.auto = type("Auto", (), {"GetFocusedControl": lambda self: child})()
    ready, classes, process_name = driver._focus_snapshot()
    assert ready is True
    assert classes[:2] == ["Edit", "mmui::XWindow"]


def test_wechat_focus_recognizes_weixin_process(monkeypatch):
    control = type(
        "Control", (), {
            "ClassName": "Edit",
            "ProcessId": 12345,
            "GetParentControl": lambda self: None,
        },
    )()
    monkeypatch.setattr("psutil.Process", lambda pid: type("Process", (), {"name": lambda self: "Weixin.exe"})())
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.auto = type("Auto", (), {"GetFocusedControl": lambda self: control})()
    ready, classes, process_name = driver._focus_snapshot()
    assert ready is True
    assert process_name == "Weixin.exe"


def test_activation_does_not_toggle_visible_wechat():
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.request_id = "test-1"
    driver.hotkey = "{Ctrl}{Alt}w"
    driver._focus_snapshot = lambda: (True, ["Edit", "mmui::XWindow"], "Weixin.exe")
    driver.auto = type("Auto", (), {"SendKeys": lambda self, keys: pytest.fail("hotkey toggled")})()
    driver._open_wechat()


def test_group_search_never_selects_a_partial_match():
    class Control:
        def __init__(self, name, class_name="mmui::XListViewItem"):
            self.Name = name
            self.ClassName = class_name

        def GetChildren(self):
            return []

    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver._open_wechat = lambda: None
    driver._ensure_control_tree = lambda: None
    driver._click = lambda control: None
    driver._perform = lambda operation: operation()
    driver.delays = type("Delays", (), {"action": lambda self: None})()
    driver.profile = type(
        "Profile", (), {"search_depth": 15, "results_depth": 5, "toolbar_depth": 18}
    )()
    driver.locale = type("Locale", (), {"search": "搜索"})()
    driver.pyperclip = type("Clipboard", (), {"copy": lambda self, value: None})()
    driver.auto = type(
        "Auto",
        (),
        {
            "ButtonControl": lambda self, **kwargs: Control("聊天"),
            "EditControl": lambda self, **kwargs: Control("搜索"),
            "SendKeys": lambda self, keys: None,
            "ListControl": lambda self, **kwargs: type(
                "Results", (), {"GetChildren": lambda self: [Control("资产核对群2")]}
            )(),
        },
    )()

    with pytest.raises(SendError) as error:
        driver._select_group("资产核对群")
    assert error.value.status == "group_not_found"


def test_qt_render_shell_fails_before_looking_for_chat_button():
    class Control:
        def __init__(self, class_name, children=(), parent=None):
            self.ClassName = class_name
            self.ProcessId = 123
            self.children = children
            self.parent = parent
            for child in children:
                child.parent = self

        def GetParentControl(self):
            return self.parent

        def GetChildren(self):
            return self.children

    root = Control("Qt51514QWindowIcon", [
        Control("Qt51514QWindowIcon"),
        Control("MMUIRenderSubWindowHW"),
    ])
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.request_id = "test-shell"
    driver.auto = type("Auto", (), {"GetFocusedControl": lambda self: root})()
    with pytest.raises(SendError) as error:
        driver._ensure_control_tree()
    assert error.value.status == "wechat_uia_unavailable"


def test_failed_uia_wake_never_starts_group_search(monkeypatch):
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.request_id = "test-wake"
    driver._open_wechat = lambda: None
    driver._ensure_control_tree = lambda: (_ for _ in ()).throw(
        SendError("wechat_uia_unavailable", "shell only")
    )
    driver.auto = type("Auto", (), {
        "ButtonControl": lambda self, **kwargs: pytest.fail("group search started"),
    })()
    monkeypatch.setattr(
        "easychat_sender.driver.wake_wechat_uia",
        lambda request_id: (False, "未找到可用的 gate"),
    )
    with pytest.raises(SendError) as error:
        driver._select_group("资产核对群")
    assert error.value.status == "wechat_uia_activation_failed"
    assert "未找到可用的 gate" in str(error.value)


def test_41556_anchors_select_only_exact_group():
    class Control:
        def __init__(self, class_name, name="", aid="", kind="GroupControl", children=()):
            self.ClassName = class_name
            self.Name = name
            self.AutomationId = aid
            self.ControlTypeName = kind
            self.ProcessId = 123
            self.NativeWindowHandle = 0
            self.children = list(children)

        def GetParentControl(self):
            return None

        def GetChildren(self):
            return self.children

    header = Control("mmui::XTextView", "其他群", "a.current_chat_name_label")
    tab = Control("mmui::XTabBarItem", "微信", kind="ButtonControl")
    search = Control("mmui::XValidatorTextEdit", "搜索", kind="EditControl")
    partial = Control("mmui::SearchContentCellView", "资产核对群2", "search_item_partial", "ListItemControl")
    exact = Control("mmui::SearchContentCellView", "资产核对群", "search_item_exact", "ListItemControl")
    same_name_wrong_class = Control("mmui::XTableCell", "资产核对群", kind="ListItemControl")
    group_section = Control("mmui::XTableCell", "群聊", kind="ListItemControl")
    results = Control("mmui::XTableView", aid="search_list", kind="ListControl",
                      children=(group_section, partial, exact, same_name_wrong_class))
    group_title = Control("mmui::ChatTitleBarChatRoomView")
    root = Control("mmui::MainWindow", children=(tab, search, results, header, group_title))
    clicked = []
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.request_id = "test-41556"
    driver.profile = PROFILES["微信 4.1.15.56"]
    driver.locale = type("Locale", (), {
        "search": "搜索", "weixin": "微信", "group_chat": "群聊",
    })()
    driver.delays = type("Delays", (), {"action": lambda self: None})()
    driver.auto = type("Auto", (), {
        "GetFocusedControl": lambda self: root,
        "SendKeys": lambda self, keys: None,
    })()
    driver.pyperclip = type("Clipboard", (), {"copy": lambda self, value: None})()
    driver._perform = lambda operation: operation()

    def click(control):
        clicked.append(control)
        if control is exact:
            header.Name = "资产核对群"

    driver._click = click
    driver._select_group_anchored("资产核对群")
    assert clicked == [tab, search, exact]


def test_41556_mention_uses_popover_candidate():
    class Control:
        def __init__(self, class_name, name="", aid="", kind="GroupControl", children=()):
            self.ClassName = class_name
            self.Name = name
            self.AutomationId = aid
            self.ControlTypeName = kind
            self.ProcessId = 123
            self.NativeWindowHandle = 0
            self.children = list(children)

        def GetParentControl(self):
            return None

        def GetChildren(self):
            return self.children

    candidate = Control("mmui::XTableCell", "张三", kind="ListItemControl")
    mention_list = Control("mmui::XTableView", aid="chat_mention_list",
                           kind="ListControl", children=(candidate,))
    popover = Control("mmui::XPopover", aid="MentionPopover", children=(mention_list,))
    root = Control("mmui::MainWindow", children=(popover,))
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver.request_id = "test-mention-41556"
    driver.profile = PROFILES["微信 4.1.15.56"]
    driver.auto = type("Auto", (), {
        "GetFocusedControl": lambda self: root,
        "SendKeys": lambda self, keys: None,
    })()
    driver.pyperclip = type("Clipboard", (), {"copy": lambda self, value: None})()
    driver.delays = type("Delays", (), {"action": lambda self: None})()
    driver._perform = lambda operation: operation()
    clicked = []

    def click(control):
        clicked.append(control)
        root.children.remove(popover)

    driver._click = click
    driver._mention("张三")
    assert clicked == [candidate]


def test_mention_requires_new_exact_candidate():
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    typed = []
    clicked = []
    candidate = object()
    snapshots = iter([{"old": object()}, {"old": object(), "new": candidate}])
    driver._mention_candidates = lambda name: next(snapshots)
    driver._perform = lambda operation: operation()
    driver._click = clicked.append
    driver.auto = type("Auto", (), {"SendKeys": lambda self, keys: typed.append(keys)})()
    driver.pyperclip = type(
        "Clipboard", (), {"copy": lambda self, value: typed.append(value)}
    )()
    driver.delays = type("Delays", (), {"action": lambda self: None})()

    driver._mention("张三")
    assert typed == ["@", "张三", "{Ctrl}v"]
    assert clicked == [candidate]

    driver._mention_candidates = lambda name: {}
    with pytest.raises(SendError) as error:
        driver._mention("李四")
    assert error.value.status == "mention_not_found"


def test_old_draft_is_cleared_before_next_message():
    keys = []
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver._perform = lambda operation: operation()
    driver.auto = type("Auto", (), {"SendKeys": lambda self, value: keys.append(value)})()
    driver._reset_draft()
    assert keys == ["{Ctrl}a", "{Delete}"]


def test_draft_clear_key_is_supported_by_installed_uiautomation():
    from uiautomation.uiautomation import SpecialKeyNames

    assert "DELETE" in SpecialKeyNames


def test_desktop_calls_are_serialized(tmp_path):
    active = 0
    peak = 0
    guard = threading.Lock()

    class SlowDriver(FakeDriver):
        def send(self, request):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return {"status": "submitted", "message": "已点击发送"}

    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=SlowDriver,
    )
    results = []
    threads = [
        threading.Thread(
            target=lambda index=index: results.append(
                service.send_group_message(dict(PAYLOAD, request_id=f"id-{index}"))
            )
        )
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1
    assert len(results) == 3
    assert all(result["ok"] for result in results)


def make_png(color="red"):
    output = BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def test_image_input_and_image_only_request():
    data = make_png()
    payload = {
        "group_name": "测试群",
        "at_users": [],
        "content": "",
        "images": [{"filename": "结果.png", "data_base64": base64.b64encode(data).decode()}],
    }
    request = SendRequest.from_payload(payload)
    assert request.total_messages == 1
    assert request.images[0] == ImagePayload("结果.png", data, "PNG")

    with pytest.raises(SendError) as error:
        SendRequest.from_payload(dict(payload, at_users=["张三"]))
    assert error.value.status == "invalid_request"

    with pytest.raises(SendError) as error:
        SendRequest.from_payload(dict(payload, images=[{"filename": "x.png", "data_base64": "not-base64"}]))
    assert error.value.status == "invalid_request"


def test_request_fingerprint_includes_image_bytes(tmp_path):
    FakeDriver.calls = []
    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=FakeDriver,
    )
    base = dict(PAYLOAD, images=[{
        "filename": "result.png",
        "data_base64": base64.b64encode(make_png("red")).decode(),
    }])
    assert service.send_group_message(base)["status"] == "submitted"
    changed = dict(base, images=[{
        "filename": "result.png",
        "data_base64": base64.b64encode(make_png("blue")).decode(),
    }])
    assert service.send_group_message(changed)["status"] == "invalid_request"
    assert len(FakeDriver.calls) == 1


def test_text_then_each_image_is_a_separate_message():
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    events = []
    driver.auto = type("Auto", (), {"SetGlobalSearchTimeout": lambda self, value: None})()
    driver._select_group = lambda name: events.append(("group", name))
    driver._reset_draft = lambda: events.append(("reset",))
    driver._mention = lambda name: events.append(("mention", name))
    driver._paste_body = lambda value: events.append(("text", value))
    driver._paste_image = lambda image: events.append(("image", image.filename))
    driver._send = lambda: events.append(("send",))
    request = SendRequest(
        "测试群", ("张三",), "对比结果", "req-1",
        (ImagePayload("a.png", make_png(), "PNG"), ImagePayload("b.png", make_png(), "PNG")),
    )
    assert driver.send(request)["status"] == "submitted"
    assert events == [
        ("group", "测试群"), ("reset",), ("mention", "张三"),
        ("text", "对比结果"), ("send",),
        ("image", "a.png"), ("send",), ("image", "b.png"), ("send",),
    ]


def test_image_is_pasted_as_dib(monkeypatch):
    clipboard = type("Clipboard", (), {})()
    clipboard.CF_DIB = 8
    clipboard.OpenClipboard = lambda: None
    clipboard.EmptyClipboard = lambda: None
    clipboard.CloseClipboard = lambda: None
    captured = []
    clipboard.SetClipboardData = lambda kind, data: captured.append((kind, data))
    monkeypatch.setitem(sys.modules, "win32clipboard", clipboard)
    keys = []
    driver = WeChatUIDriver.__new__(WeChatUIDriver)
    driver._perform = lambda operation: operation()
    driver.auto = type("Auto", (), {"SendKeys": lambda self, value: keys.append(value)})()
    driver._paste_image(ImagePayload("a.png", make_png(), "PNG"))
    assert captured[0][0] == 8
    assert int.from_bytes(captured[0][1][:4], "little") == 40
    assert keys == ["{Ctrl}v"]


def test_partial_send_reports_completed_message_count(tmp_path):
    class PartialDriver(FakeDriver):
        def send(self, request):
            self.sent_count = 1
            raise RuntimeError("第二张图片粘贴失败")

    payload = dict(PAYLOAD, images=[{
        "filename": "result.png",
        "data_base64": base64.b64encode(make_png()).decode(),
    }])
    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=PartialDriver,
    )
    result = service.send_group_message(payload)
    assert result["status"] == "partial_send"
    assert result["sent_messages"] == 1
    assert result["total_messages"] == 2


def test_http_page_and_json_api():
    class FakeService:
        config = SenderConfig()
        received = None

        def send_group_message(self, payload):
            self.received = payload
            return {"request_id": payload["request_id"], "ok": True, "status": "submitted", "message": "已点击发送"}

    service = FakeService()
    server = create_server("127.0.0.1", 0, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/") as response:
            assert "消息派发台" in response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/health") as response:
            assert json.load(response)["version"] == "微信 4.1.15.56"
        request = urllib.request.Request(
            base + "/api/send",
            data=json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["status"] == "submitted"
        assert service.received == PAYLOAD
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_json_image_reaches_python_send_function(tmp_path):
    FakeDriver.calls = []
    service = SenderService(
        SenderConfig(state_db_path=tmp_path / "state.sqlite3"),
        driver_factory=FakeDriver,
    )
    server = create_server("127.0.0.1", 0, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = dict(PAYLOAD, images=[{
            "filename": "result.png",
            "data_base64": base64.b64encode(make_png()).decode(),
        }])
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/send",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result["status"] == "submitted"
        assert result["sent_messages"] == 2
        assert len(FakeDriver.calls[0].images) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
