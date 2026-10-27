"""Input contract for prepared text and image messages."""

import base64
import binascii
from io import BytesIO
from dataclasses import dataclass
from typing import Any, Mapping


class SendError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


MAX_IMAGES = 5
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP", "BMP"}


@dataclass(frozen=True)
class ImagePayload:
    filename: str
    data: bytes
    format: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ImagePayload":
        if not isinstance(payload, Mapping):
            raise SendError("invalid_request", "images 的每一项必须是对象")
        filename = payload.get("filename")
        encoded = payload.get("data_base64")
        if not isinstance(filename, str) or not filename.strip():
            raise SendError("invalid_request", "图片 filename 必须是非空字符串")
        if not isinstance(encoded, str) or not encoded:
            raise SendError("invalid_request", "图片 data_base64 必须是非空字符串")
        if encoded.startswith("data:"):
            if "," not in encoded:
                raise SendError("invalid_request", "图片 data URL 格式错误")
            encoded = encoded.split(",", 1)[1]
        if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 8:
            raise SendError("invalid_request", f"单张图片不能超过 {MAX_IMAGE_BYTES // 1048576} MB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SendError("invalid_request", "图片不是有效的 base64 数据") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise SendError("invalid_request", f"单张图片不能超过 {MAX_IMAGE_BYTES // 1048576} MB")
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                if image.format not in SUPPORTED_FORMATS:
                    raise SendError("invalid_request", "仅支持 PNG、JPEG、WEBP、BMP 图片")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise SendError("invalid_request", "图片像素尺寸过大")
                image_format = image.format
                image.load()
        except SendError:
            raise
        except ImportError as exc:
            raise SendError("automation_unavailable", "图片发送需要安装 Pillow") from exc
        except Exception as exc:
            raise SendError("invalid_request", "图片文件无法解码") from exc
        return cls(filename=filename.strip(), data=data, format=image_format)


@dataclass(frozen=True)
class SendRequest:
    group_name: str
    at_users: tuple[str, ...]
    content: str
    request_id: str | None = None
    images: tuple[ImagePayload, ...] = ()

    @property
    def total_messages(self) -> int:
        return int(bool(self.content.strip())) + len(self.images)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SendRequest":
        if not isinstance(payload, Mapping):
            raise SendError("invalid_request", "payload 必须是 JSON 对象解析后的字典")

        group_name = payload.get("group_name")
        content = payload.get("content", "")
        at_users = payload.get("at_users", [])
        images = payload.get("images", [])
        request_id = payload.get("request_id")

        if not isinstance(group_name, str) or not group_name.strip():
            raise SendError("invalid_request", "group_name 必须是非空字符串")
        if not isinstance(content, str):
            raise SendError("invalid_request", "content 必须是字符串")
        if not isinstance(at_users, list):
            raise SendError("invalid_request", "at_users 必须是字符串列表")
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise SendError("invalid_request", f"images 必须是列表，最多 {MAX_IMAGES} 张")
        if any(not isinstance(user, str) or not user.strip() for user in at_users):
            raise SendError("invalid_request", "at_users 不能包含空名称")
        clean_users = tuple(user.strip() for user in at_users)
        if len(set(clean_users)) != len(clean_users):
            raise SendError("invalid_request", "at_users 不能包含重复名称")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise SendError("invalid_request", "request_id 必须是非空字符串")

        parsed_images = tuple(ImagePayload.from_payload(image) for image in images)
        if not content.strip() and not parsed_images:
            raise SendError("invalid_request", "content 和 images 至少提供一项")
        if clean_users and not content.strip():
            raise SendError("invalid_request", "需要 @ 成员时必须提供文本正文")

        return cls(
            group_name=group_name.strip(),
            at_users=clean_users,
            content=content,
            request_id=request_id.strip() if request_id else None,
            images=parsed_images,
        )
