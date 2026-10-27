from inspect_wechat_ui import capture_tree, masked_name


def test_default_capture_masks_chat_names_but_preserves_selectors():
    assert masked_name("搜索") == "搜索"
    assert masked_name("rpa测试(20)", target="rpa测试") == "<TARGET>(20)"
    assert masked_name("Littlelu", member="Littlelu") == "<MEMBER>"
    assert masked_name("私人聊天内容") == "<hidden; chars=6>"


def test_tree_capture_only_reads_controls():
    class Control:
        def __init__(self, name, children=()):
            self.Name = name
            self.ClassName = "mmui::XTabBarItem"
            self.ControlTypeName = "ButtonControl"
            self.AutomationId = "chat_tab"
            self.ProcessId = 123
            self.children = children

        def GetChildren(self):
            return self.children

    root = Control("微信", [Control("搜索"), Control("私人聊天内容")])
    lines, count, truncated = capture_tree(
        None, [root], depth_limit=5, max_nodes=10,
        target="", member="", reveal=False,
    )
    text = "\n".join(lines)
    assert count == 3
    assert truncated is False
    assert "class='mmui::XTabBarItem'" in text
    assert "私人聊天内容" not in text
    assert "<hidden; chars=6>" in text
