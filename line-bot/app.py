import os
import re
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

app = Flask(__name__)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

# group_id -> 最新統計表文字
state: dict[str, str] = {}


def is_stats_table(text: str) -> bool:
    return "目標" in text and "累積：" in text and "尚差：" in text


def fmt(n: float) -> str:
    """3.0 → '3'，3.2 → '3.2'"""
    n = round(n, 1)
    return str(int(n)) if n == int(n) else str(n)


def add_medical_note(text: str, name: str, amount: float) -> tuple[str | None, str | None]:
    lines = text.split("\n")
    result = []
    found = False

    for line in lines:
        if re.match(rf"^{re.escape(name)}[：:]", line):
            found = True
            result.append(line + f"（體檢件：{fmt(amount)}C）")
        else:
            result.append(line)

    if not found:
        return None, f"找不到「{name}」，請確認名字是否正確"

    return "\n".join(result), None


def remove_medical_note(text: str, name: str, amount: float) -> tuple[str | None, str | None]:
    lines = text.split("\n")
    result = []
    found = False

    for line in lines:
        if re.match(rf"^{re.escape(name)}[：:]", line):
            found = True
            note = f"（體檢件：{fmt(amount)}C）"
            if note not in line:
                return None, f"找不到「{name}」的體檢件 {fmt(amount)}C 備註"
            result.append(line.replace(note, ""))
        else:
            result.append(line)

    if not found:
        return None, f"找不到「{name}」，請確認名字是否正確"

    return "\n".join(result), None


def update_table(text: str, name: str, amount: float) -> tuple[str | None, str | None]:
    lines = text.split("\n")
    result = []
    found = False

    for line in lines:
        if re.match(rf"^{re.escape(name)}[：:]", line):
            found = True
            m = re.match(rf"^({re.escape(name)}[：:])([\d.]+)([Cc]?)(.*)", line)
            if m:
                new_val = round(float(m.group(2)) + amount, 1)
                suffix = m.group(3) or "c"
                result.append(f"{m.group(1)}{fmt(new_val)}{suffix}{m.group(4)}")
            else:
                # 空白欄位（尚未填數字）
                sep = "：" if "：" in line else ":"
                result.append(f"{name}{sep}{fmt(amount)}c")

        elif re.match(r"^累積[：:]", line):
            m = re.match(r"^(累積[：:])([\d.]+)(.*)", line)
            if m:
                new_val = round(float(m.group(2)) + amount, 1)
                result.append(f"{m.group(1)}{fmt(new_val)}{m.group(3)}")
            else:
                result.append(line)

        elif re.match(r"^尚差[：:]", line):
            m = re.match(r"^(尚差[：:])([\d.]+)(.*)", line)
            if m:
                new_val = round(float(m.group(2)) - amount, 1)
                result.append(f"{m.group(1)}{fmt(new_val)}{m.group(3)}")
            else:
                result.append(line)

        else:
            result.append(line)

    if not found:
        return None, f"找不到「{name}」，請確認名字是否正確"

    return "\n".join(result), None


@app.route("/")
def health():
    return "OK"


@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    text = event.message.text.strip()
    src = event.source
    gid = getattr(src, "group_id", None) or getattr(src, "room_id", None)
    if not gid:
        return

    # 1. 有人貼統計表 → 記錄起來
    if is_stats_table(text):
        state[gid] = text
        send(event.reply_token, "✅ 統計表已記錄，Bot 待命中")
        return

    # 2a. 有人發「XXX收...數字C 體檢件」→ 只加備註，不動數字
    m = re.match(r"^(.+?)\s*收\s*.+?([\d.]+)\s*[Cc].*體檢件", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            send(event.reply_token, "⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = add_medical_note(state[gid], name, amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        state[gid] = new_text
        send(event.reply_token, new_text)
        return

    # 2b. 有人發「XXX收...數字C」→ 加
    m = re.match(r"^(.+?)\s*收\s*.+?([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            send(event.reply_token, "⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = update_table(state[gid], name, amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        state[gid] = new_text
        send(event.reply_token, new_text)
        return

    # 3. 有人發「XXX退數字C」→ 扣
    m = re.match(r"^(.+?)\s*退\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            send(event.reply_token, "⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = update_table(state[gid], name, -amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        state[gid] = new_text
        send(event.reply_token, new_text)
        return

    # 4. 「XXX體檢取消數字C」→ 刪備註，業績不動
    m = re.match(r"^(.+?)\s*體檢取消\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            send(event.reply_token, "⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = remove_medical_note(state[gid], name, amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        state[gid] = new_text
        send(event.reply_token, new_text)
        return

    # 5. 「XXX體檢通過數字C」→ 刪備註 + 加入業績
    m = re.match(r"^(.+?)\s*體檢通過\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            send(event.reply_token, "⚠️ 尚未初始化，請先貼統計表")
            return
        text1, err = remove_medical_note(state[gid], name, amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        new_text, err = update_table(text1, name, amount)
        if err:
            send(event.reply_token, f"⚠️ {err}")
            return
        state[gid] = new_text
        send(event.reply_token, new_text)


def send(reply_token: str, text: str):
    with ApiClient(configuration) as api:
        MessagingApi(api).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
