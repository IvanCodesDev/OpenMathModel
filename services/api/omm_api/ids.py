from uuid import uuid4


def new_id(prefix: str) -> str:
    """生成带类型前缀的字符串 ID（契约 Id 格式：^[a-z]+_[A-Za-z0-9]{6,40}$）。"""
    return f"{prefix}_{uuid4().hex}"
