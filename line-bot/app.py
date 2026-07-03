import os
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, ImageMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError
from poster import generate_poster

app = Flask(__name__)

# Bot A：業績統計群組
handler_a       = WebhookHandler(os.environ["LINE_CHANNEL_SECRET_A"])
configuration_a = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN_A"])

# Bot B：業績王公告群組
handler_b       = WebhookHandler(os.environ["LINE_CHANNEL_SECRET_B"])
configuration_b = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN_B"])
GROUP_ID_B      = os.environ.get("LINE_GROUP_ID_B", "")

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

# group_id -> 最新統計表文字
state: dict[str, str] = {}


def is_stats_table(text: str) -> bool:
    return "目標" in text and "累積：" in text and "尚差：" in text


def fmt(n: float) -> str:
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


def send_reply(configuration: Configuration, reply_token: str, text: str):
    with ApiClient(configuration) as api:
        MessagingApi(api).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


@app.route("/")
def health():
    return "OK"


@app.route("/static/posters/<filename>")
def serve_poster(filename):
    return send_from_directory("static/posters", filename)


# ── Bot A：業績統計 ──────────────────────────────────────────────

@app.route("/callback_a", methods=["POST"])
def callback_a():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler_a.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler_a.add(MessageEvent, message=TextMessageContent)
def on_message_a(event):
    text = event.message.text.strip()
    src = event.source
    gid = getattr(src, "group_id", None) or getattr(src, "room_id", None)
    if not gid:
        return

    def reply(msg):
        send_reply(configuration_a, event.reply_token, msg)

    if text == "群組ID":
        reply(f"Group ID: {gid}")
        return

    if is_stats_table(text):
        state[gid] = text
        reply("✅ 統計表已記錄，Bot 待命中")
        return

    m = re.match(r"^(.+?)\s*收\s*.+?([\d.]+)\s*[Cc].*體檢件", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            reply("⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = add_medical_note(state[gid], name, amount)
        if err:
            reply(f"⚠️ {err}")
            return
        state[gid] = new_text
        reply(new_text)
        return

    m = re.match(r"^(.+?)\s*收\s*.+?([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            reply("⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = update_table(state[gid], name, amount)
        if err:
            reply(f"⚠️ {err}")
            return
        state[gid] = new_text
        reply(new_text)
        return

    m = re.match(r"^(.+?)\s*退\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            reply("⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = update_table(state[gid], name, -amount)
        if err:
            reply(f"⚠️ {err}")
            return
        state[gid] = new_text
        reply(new_text)
        return

    m = re.match(r"^(.+?)\s*體檢取消\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            reply("⚠️ 尚未初始化，請先貼統計表")
            return
        new_text, err = remove_medical_note(state[gid], name, amount)
        if err:
            reply(f"⚠️ {err}")
            return
        state[gid] = new_text
        reply(new_text)
        return

    m = re.match(r"^(.+?)\s*體檢通過\s*([\d.]+)\s*[Cc]\s*$", text)
    if m:
        name, amount = m.group(1).strip(), float(m.group(2))
        if gid not in state:
            reply("⚠️ 尚未初始化，請先貼統計表")
            return
        text1, err = remove_medical_note(state[gid], name, amount)
        if err:
            reply(f"⚠️ {err}")
            return
        new_text, err = update_table(text1, name, amount)
        if err:
            reply(f"⚠️ {err}")
            return
        state[gid] = new_text
        reply(new_text)


# ── Bot B：業績王公告 ──────────────────────────────────────────────

@app.route("/callback_b", methods=["POST"])
def callback_b():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler_b.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler_b.add(MessageEvent, message=TextMessageContent)
def on_message_b(event):
    text = event.message.text.strip()
    src = event.source
    gid = getattr(src, "group_id", None) or getattr(src, "room_id", None)
    if not gid:
        return

    def reply(msg):
        send_reply(configuration_b, event.reply_token, msg)

    if text == "群組ID":
        reply(f"Group ID: {gid}")
        return

    m = re.match(r"業績王\s+(\S+)\s+(\S+)(?:\s+(\d{4}\.\d{2}\.\d{2}))?", text)
    if not m:
        return

    name     = m.group(1)
    title    = m.group(2)
    date_str = m.group(3) or datetime.now().strftime("%Y.%m.%d")
    try:
        d            = datetime.strptime(date_str, "%Y.%m.%d")
        date_display = f"{d.month}/{d.day}"
        next_day     = f"{(d + timedelta(days=1)).month}/{(d + timedelta(days=1)).day}"
    except Exception:
        date_display, next_day = date_str, "明天"

    poster_path = generate_poster(name, title, date_str)
    if poster_path is None:
        reply(f"❌ 找不到 {name} 的照片\n請將照片放在 photos/{name}.jpg")
        return

    filename  = os.path.basename(poster_path)
    image_url = f"{BASE_URL}/static/posters/{filename}"
    announcement = (
        f"恭喜{date_display}業績王\n"
        f"「{name} {title}」\n"
        f"請 {name} 於{next_day}晨會上台分享唷～\n"
        f"🇫🇷迎戰北法🇫🇷\n"
        f"Go!!Go!!Go🎉🎉🎉"
    )
    target = GROUP_ID_B or gid
    with ApiClient(configuration_b) as api:
        MessagingApi(api).push_message(
            PushMessageRequest(
                to=target,
                messages=[
                    TextMessage(text=announcement),
                    ImageMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url,
                    ),
                ],
            )
        )
    reply(f"✅ 已發送 {name} {title} 的業績王公告！")


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
