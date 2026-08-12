"""用户头像图片识别：只接受按文件魔数确认的位图格式。

请求里声明的 Content-Type 完全由客户端控制，不能作为安全依据。头像会以同源
URL 返回给浏览器，若把 SVG 或 HTML 当作图片返回即等同于同源脚本注入，因此这里
按文件头识别真实格式，并用识别结果覆盖响应的 Content-Type。
"""

from __future__ import annotations

AVATAR_MEDIA_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp", "image/gif")


def sniff_avatar_media_type(content: bytes) -> str | None:
    """返回内容真实的图片媒体类型；无法识别为白名单位图时返回 None。"""

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None
