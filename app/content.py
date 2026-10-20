import json


def parse_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def message_text(raw: dict) -> str:
    if raw.get("deleted"):
        return "消息已撤回"
    body = parse_json((raw.get("body") or {}).get("content", ""))
    if not isinstance(body, dict):
        return str(body)
    if isinstance(body.get("text"), str):
        return body["text"]
    if raw.get("msg_type") == "post":
        post = body
        if "content" not in post:
            post = next((x for x in body.values() if isinstance(x, dict) and "content" in x), {})
        lines = [post.get("title", "")]
        for row in post.get("content", []):
            if isinstance(row, list):
                lines.append(
                    "".join(
                        str(
                            node.get("text")
                            or node.get("user_name")
                            or ("[图片]" if node.get("tag") == "img" else "")
                        )
                        for node in row
                        if isinstance(node, dict)
                    )
                )
        return "\n".join(line for line in lines if line)
    labels = {
        "image": "图片",
        "file": "文件",
        "audio": "语音",
        "media": "视频",
        "sticker": "表情",
        "interactive": "卡片",
    }
    label = labels.get(raw.get("msg_type"), raw.get("msg_type", "消息"))
    return f"[{label}] " + str(body.get("file_name", ""))
