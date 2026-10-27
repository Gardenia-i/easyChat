"""Read-only UI Automation tree capture for adapting a WeChat client version.

Run it from a normal interactive PowerShell window. During the countdown,
bring WeChat to the foreground and leave the target UI state visible.
"""

import argparse
import datetime as dt
import re
import time
from pathlib import Path


WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}
VISIBLE_LABELS = {
    "微信", "Weixin", "聊天", "Chats", "通讯录", "通訊錄", "Contacts",
    "搜索", "搜尋", "Search", "发送", "傳送", "Send", "消息", "Message",
    "群聊", "Group Chat", "所有人", "查看更多消息", "以下为新消息",
}


def masked_name(value: str, target: str = "", member: str = "", reveal=False) -> str:
    value = value or ""
    if not value:
        return ""
    if reveal or value in VISIBLE_LABELS:
        return value.replace("\n", "\\n")[:160]
    for original, marker in ((target, "<TARGET>"), (member, "<MEMBER>")):
        if original and value == original:
            return marker
        if original and value.startswith(original):
            suffix = value[len(original):]
            if re.fullmatch(r"[\s（）()0-9人]*", suffix):
                return marker + suffix
        if original and original in value:
            return f"<contains {marker}; chars={len(value)}>"
    return f"<hidden; chars={len(value)}>"


def property_or(control, property_name, fallback=""):
    try:
        return getattr(control, property_name) or fallback
    except Exception:
        return fallback


def process_name(pid: int) -> str:
    if not pid:
        return "unknown"
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return f"pid:{pid}"


def top_level_roots(auto, focused):
    """Include separate WeChat popups as well as the focused main window."""
    roots = []
    try:
        desktop_children = auto.GetRootControl().GetChildren()
    except Exception:
        desktop_children = []
    for control in desktop_children:
        pid = property_or(control, "ProcessId", 0)
        if process_name(pid).lower() in WECHAT_PROCESS_NAMES:
            roots.append(control)
    if roots:
        return roots

    # UIA can expose the focused control even when desktop enumeration fails.
    root = focused
    for _ in range(20):
        try:
            parent = root.GetParentControl()
        except Exception:
            break
        if parent is None or property_or(parent, "ClassName") == "#32769":
            break
        root = parent
    return [root]


def capture_tree(auto, roots, *, depth_limit, max_nodes, target, member, reveal):
    lines = []
    inspected = 0
    truncated = False
    for root_index, root in enumerate(roots, 1):
        lines.append(f"\n=== WECHAT WINDOW {root_index} ===")
        stack = [(root, 0)]
        while stack:
            if inspected >= max_nodes:
                truncated = True
                break
            control, depth = stack.pop()
            inspected += 1
            control_type = str(property_or(control, "ControlTypeName", "?"))
            class_name = str(property_or(control, "ClassName", "?"))
            automation_id = str(property_or(control, "AutomationId", ""))
            name = masked_name(
                property_or(control, "Name", ""), target, member, reveal
            )
            pid = property_or(control, "ProcessId", 0)
            indent = "  " * min(depth, 30)
            lines.append(
                f"{indent}[{depth:02}] {control_type} "
                f"class={class_name!r} id={automation_id!r} "
                f"name={name!r} pid={pid}"
            )
            if depth >= depth_limit:
                continue
            try:
                children = control.GetChildren()
            except Exception as exc:
                lines.append(f"{indent}  <children error: {type(exc).__name__}>")
                continue
            stack.extend((child, depth + 1) for child in reversed(children))
        if truncated:
            break
    return lines, inspected, truncated


def main():
    parser = argparse.ArgumentParser(description="只读采集微信控件树")
    parser.add_argument("--label", choices=("main", "search", "mention"), required=True)
    parser.add_argument("--delay", type=int, default=10, help="倒计时秒数，期间切到微信")
    parser.add_argument("--depth", type=int, default=24)
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument("--target", default="", help="目标群名，输出中替换为 <TARGET>")
    parser.add_argument("--member", default="", help="@ 成员名，输出中替换为 <MEMBER>")
    parser.add_argument("--include-names", action="store_true", help="输出完整名称；分享前请自行检查隐私")
    parser.add_argument("--output", type=Path, help="指定输出 txt 路径")
    args = parser.parse_args()
    if args.delay < 0 or args.depth < 0 or args.max_nodes < 1:
        parser.error("delay、depth 和 max-nodes 必须是非负数，max-nodes 至少为 1")

    try:
        import uiautomation as auto
    except ImportError as exc:
        parser.error("缺少 uiautomation，请先安装 requirements-sender.txt")

    print(f"请在 {args.delay} 秒内切到微信并准备好 {args.label} 界面。脚本只读取，不会点击或发送。", flush=True)
    time.sleep(args.delay)
    auto.SetGlobalSearchTimeout(1.0)
    focused = auto.GetFocusedControl()
    pid = property_or(focused, "ProcessId", 0)
    active_process = process_name(pid)
    if active_process.lower() not in WECHAT_PROCESS_NAMES:
        parser.error(
            f"倒计时结束时前台是 {active_process}，不是微信。请重新运行并切到微信。"
        )

    roots = top_level_roots(auto, focused)
    lines, count, truncated = capture_tree(
        auto, roots,
        depth_limit=args.depth,
        max_nodes=args.max_nodes,
        target=args.target,
        member=args.member,
        reveal=args.include_names,
    )
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or Path("diagnostics") / f"wechat_4_1_15_56_{args.label}_{timestamp}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"captured_at={dt.datetime.now().isoformat(timespec='seconds')}",
        f"label={args.label}",
        f"foreground_process={active_process}",
        f"foreground_class={property_or(focused, 'ClassName', '<empty>')}",
        f"window_count={len(roots)} nodes={count} truncated={truncated}",
        f"names={'full' if args.include_names else 'masked'}",
    ]
    output.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"已保存：{output.resolve()}（{count} 个控件，truncated={truncated}）")
    if count <= 10:
        print(
            "控件树未展开（仅采到窗口外壳）。请保持微信主窗口可见，运行 "
            "python -X utf8 vendor/wechatauto_replica/wake_uia.py "
            "--cache diagnostics/gate_cache.json，成功后重跑 --label main。"
        )


if __name__ == "__main__":
    main()
