from types import SimpleNamespace as B

from app.agent import _is_user_text, _trim


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": "[Context]"}, {"type": "text", "text": text}]}


def assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def tool_result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}


def test_is_user_text_distinguishes_tool_results():
    assert _is_user_text(user("hi")) and _is_user_text({"role": "user", "content": "hi"})
    assert not _is_user_text(tool_result()) and not _is_user_text(assistant(B(type="text", text="x")))


def test_trim_cuts_at_user_turn_and_strips_thinking_from_retained_tail():
    history = [
        user("one"), assistant(B(type="thinking", thinking="", signature="s1"), B(type="text", text="a")),
        user("two"), assistant(B(type="thinking", thinking="", signature="s2"), B(type="tool_use", id="t1", name="x", input={})),
        tool_result(), assistant(B(type="text", text="b")),
        user("three"), assistant(B(type="thinking", thinking="", signature="s3"), B(type="text", text="c")),
    ]
    trimmed = _trim(history, 6)  # window starts inside the tool round; the cut must move forward to a user turn
    assert [m["role"] for m in trimmed] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert trimmed[0]["content"][1]["text"] == "two"  # cut landed on a user turn, not inside the tool round
    assert all(getattr(b, "type", "") != "thinking" for m in trimmed if m["role"] == "assistant" for b in m["content"])
    assert trimmed[1]["content"][0].type == "tool_use"  # tool_use kept, pair intact


def test_trim_leaves_short_history_untouched():
    history = [user("one"), assistant(B(type="thinking", thinking="", signature="s1"), B(type="text", text="a"))]
    assert _trim(history, 40) is history


def test_trim_keeps_last_exchange_when_window_has_no_user_turn():
    history = [
        user("one"), assistant(B(type="thinking", thinking="", signature="s1"), B(type="tool_use", id="t1", name="x", input={})),
        tool_result(), assistant(B(type="tool_use", id="t2", name="x", input={})), tool_result(), assistant(B(type="text", text="done")),
    ]
    trimmed = _trim(history, 3)
    assert len(trimmed) == 6 and trimmed[0]["content"][1]["text"] == "one"
    assert trimmed[1]["content"][0].type == "tool_use"
