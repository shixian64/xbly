"""Flash-photo transport helpers used by the Web BFF.

The Android client uploads flash photos directly to the app's OSS bucket and
asks ``getAliyunSignature`` to sign the OSS canonical request.  The BFF keeps
that signing response server-side: browsers submit only the image and receive
the final public resource URL.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.policy import default as EMAIL_POLICY
from email.utils import formatdate
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse


OSS_BUCKET = "newecs"
OSS_ENDPOINT = "oss-cn-shanghai.aliyuncs.com"
OSS_PUBLIC_ORIGIN = "https://oss.banghua.xin"

# Mirrors the limits enforced by the Android chat path: GIF is capped at 10
# MiB; other images use TUIKit's 29,360,128-byte image limit.  Multipart
# overhead is bounded separately so an upload cannot bypass the file limit by
# adding arbitrary form fields.
MAX_FLASH_GIF_BYTES = 10_485_760
MAX_FLASH_IMAGE_BYTES = 29_360_128
MAX_FLASH_MULTIPART_BYTES = MAX_FLASH_IMAGE_BYTES + 64 * 1024


class FlashPhotoError(ValueError):
    """A safe, user-facing flash-photo validation/transport error."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class UploadPart:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class ImageInfo:
    extension: str
    content_type: str
    size: int


_BOUNDARY_RE = re.compile(
    r'(?:^|;)\s*boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))',
    re.IGNORECASE,
)
_OSS_AUTH_RE = re.compile(
    r"^OSS\s+[A-Za-z0-9._-]{3,128}:[A-Za-z0-9+/=_-]{8,256}$"
)
_KNOWN_OSS_HOSTS = {
    "oss.banghua.xin",
    f"{OSS_BUCKET}.{OSS_ENDPOINT}",
    OSS_ENDPOINT,
}


def parse_multipart(content_type: str, body: bytes) -> Tuple[Dict[str, str], UploadPart]:
    """Parse one bounded browser ``FormData`` payload without ``cgi``.

    Only a single part named ``file`` is accepted.  Text fields are deliberately
    tiny because the route needs only a peer id and a few compatibility aliases.
    """

    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise FlashPhotoError("闪图上传必须使用 multipart/form-data", status=415)
    if len(body) > MAX_FLASH_MULTIPART_BYTES:
        raise FlashPhotoError("闪图文件过大", status=413)

    match = _BOUNDARY_RE.search(str(content_type or ""))
    boundary_text = (match.group(1) or match.group(2)) if match else ""
    try:
        boundary = boundary_text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FlashPhotoError("multipart boundary 无效") from exc
    if not boundary or len(boundary) > 70 or any(byte < 33 or byte > 126 for byte in boundary):
        raise FlashPhotoError("multipart boundary 无效")

    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        raise FlashPhotoError("multipart 数据格式无效")
    sections = body.split(delimiter)
    if len(sections) < 3 or not sections[-1].startswith(b"--"):
        raise FlashPhotoError("multipart 数据不完整")

    fields: Dict[str, str] = {}
    upload: Optional[UploadPart] = None
    part_count = 0
    for section in sections[1:-1]:
        part_count += 1
        if part_count > 8:
            raise FlashPhotoError("multipart 字段过多")
        if not section.startswith(b"\r\n") or not section.endswith(b"\r\n"):
            raise FlashPhotoError("multipart 分段格式无效")
        section = section[2:-2]
        header_blob, separator, payload = section.partition(b"\r\n\r\n")
        if not separator or len(header_blob) > 16 * 1024:
            raise FlashPhotoError("multipart 分段头无效")
        if b"\x00" in header_blob:
            raise FlashPhotoError("multipart 分段头无效")
        try:
            headers = BytesHeaderParser(policy=EMAIL_POLICY).parsebytes(header_blob + b"\r\n")
        except Exception as exc:
            raise FlashPhotoError("multipart 分段头无效") from exc
        if headers.get_content_disposition() != "form-data":
            raise FlashPhotoError("multipart 分段类型无效")
        transfer_encoding = str(headers.get("Content-Transfer-Encoding") or "").strip().lower()
        if transfer_encoding not in {"", "binary", "8bit"}:
            raise FlashPhotoError("不支持的 multipart 传输编码")
        name = str(headers.get_param("name", header="content-disposition") or "").strip()
        filename = headers.get_filename()
        if not name:
            raise FlashPhotoError("multipart 字段缺少名称")

        if filename is not None:
            if name != "file" or upload is not None:
                raise FlashPhotoError("闪图上传只允许一个 file 字段")
            safe_name = str(filename).replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not safe_name or len(safe_name) > 255 or any(ord(ch) < 32 for ch in safe_name):
                raise FlashPhotoError("文件名无效")
            if len(payload) > MAX_FLASH_IMAGE_BYTES:
                raise FlashPhotoError("闪图文件过大", status=413)
            upload = UploadPart(
                filename=safe_name,
                content_type=str(headers.get_content_type() or "application/octet-stream").lower(),
                data=payload,
            )
            continue

        if name in fields:
            raise FlashPhotoError(f"multipart 字段 {name} 重复")
        if len(payload) > 1024:
            raise FlashPhotoError(f"multipart 字段 {name} 过长")
        try:
            fields[name] = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise FlashPhotoError(f"multipart 字段 {name} 不是 UTF-8") from exc

    if upload is None:
        raise FlashPhotoError("请选择闪图图片")
    return fields, upload


def inspect_image(data: bytes) -> ImageInfo:
    """Identify an image from magic bytes; filename/MIME claims are not trusted."""

    size = len(data)
    if size <= 0:
        raise FlashPhotoError("闪图文件为空")
    if size > MAX_FLASH_IMAGE_BYTES:
        raise FlashPhotoError("闪图文件过大", status=413)

    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        extension, content_type = "jpg", "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        extension, content_type = "png", "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        extension, content_type = "gif", "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        extension, content_type = "webp", "image/webp"
    else:
        # SVG is intentionally rejected: it is executable markup when served
        # with a permissive content type and is not used by the Android picker.
        raise FlashPhotoError("仅支持 JPEG、PNG、GIF 或 WebP 图片")

    if extension == "gif" and size > MAX_FLASH_GIF_BYTES:
        raise FlashPhotoError("GIF 闪图不能超过 10MB", status=413)
    return ImageInfo(extension=extension, content_type=content_type, size=size)


def validate_peer(value: Any) -> str:
    peer = str(value or "").strip()
    if not peer or len(peer) > 64 or any(ord(ch) < 33 for ch in peer):
        raise FlashPhotoError("聊天对象 UID 无效")
    return peer


def validate_unique_id(value: Any) -> str:
    unique_id = str(value or "").strip()
    if not unique_id or len(unique_id) > 512 or any(ord(ch) < 33 for ch in unique_id):
        raise FlashPhotoError("闪图 uniqueid 无效")
    return unique_id


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def _oss_authorization(result: Any) -> str:
    candidates = []
    data = getattr(result, "data", None)
    raw = getattr(result, "raw", None)
    if isinstance(data, str):
        candidates.append(data)
    for item in _walk_dicts(data):
        for key in ("authorization", "Authorization", "signature", "sign", "message"):
            if isinstance(item.get(key), str):
                candidates.append(item[key])
    if isinstance(raw, str):
        candidates.append(raw)
    for candidate in candidates:
        value = candidate.strip().strip("\ufeff")
        if "\r" not in value and "\n" not in value and _OSS_AUTH_RE.fullmatch(value):
            return value
    raise FlashPhotoError("OSS 上传授权失败", status=502)


def _object_url(object_key: str) -> str:
    return f"{OSS_PUBLIC_ORIGIN}/{quote(object_key, safe='/._-~')}"


def _object_key(extension: str) -> str:
    month = time.strftime("%Y%m")
    nonce = secrets.randbelow(1_000_000)
    stamp = int(time.time() * 1000)
    return f"images/{month}/{stamp + nonce}.{extension}"


def _signed_oss_request(
    im_api: Any,
    method: str,
    object_key: str,
    *,
    data: Optional[bytes] = None,
    content_type: str = "",
    opener: Any = None,
) -> Tuple[int, Dict[str, str]]:
    date = formatdate(usegmt=True)
    content_md5 = ""
    headers = {"Date": date}
    if data is not None:
        content_md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
        headers.update(
            {
                "Content-Type": content_type,
                "Content-MD5": content_md5,
                "Content-Length": str(len(data)),
            }
        )
    canonical = (
        f"{method}\n{content_md5}\n{content_type}\n{date}\n"
        f"/{OSS_BUCKET}/{object_key}"
    )
    signature_result = im_api.aliyun_signature(canonical)
    if not getattr(signature_result, "ok", False):
        raise FlashPhotoError("OSS 上传授权失败", status=502)
    headers["Authorization"] = _oss_authorization(signature_result)

    target = f"https://{OSS_BUCKET}.{OSS_ENDPOINT}/{quote(object_key, safe='/._-~')}"
    req = urllib_request.Request(target, data=data, headers=headers, method=method)
    open_url = opener or urllib_request.urlopen
    try:
        with open_url(req, timeout=45) as response:
            status_value = getattr(response, "status", None)
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            response_headers = {str(k): str(v) for k, v in response.headers.items()}
            # Consume at most a tiny response. PUT/DELETE normally return no
            # body; this also ensures the connection can be reused/closed.
            response.read(4096)
    except urllib_error.HTTPError as exc:
        try:
            exc.read(4096)
        except Exception:
            pass
        raise FlashPhotoError("OSS 上传失败", status=502) from exc
    except Exception as exc:
        raise FlashPhotoError("OSS 上传失败", status=502) from exc
    if status < 200 or status >= 300:
        raise FlashPhotoError("OSS 上传失败", status=502)
    return status, response_headers


def upload_image(im_api: Any, data: bytes, *, opener: Any = None) -> Dict[str, Any]:
    info = inspect_image(data)
    object_key = _object_key(info.extension)
    _status, headers = _signed_oss_request(
        im_api,
        "PUT",
        object_key,
        data=data,
        content_type=info.content_type,
        opener=opener,
    )
    return {
        "path": object_key,
        "url": _object_url(object_key),
        "content_type": info.content_type,
        "size": info.size,
        "etag": str(headers.get("ETag") or headers.get("Etag") or "").strip('"'),
    }


def delete_image(im_api: Any, object_key: str, *, opener: Any = None) -> bool:
    """Best-effort cleanup for an upload whose business send was rejected."""

    try:
        _signed_oss_request(im_api, "DELETE", object_key, opener=opener)
        return True
    except Exception:
        return False


def _safe_oss_path(value: Any) -> str:
    raw = str(value or "").strip().strip('"')
    if not raw or len(raw) > 2048 or any(ord(ch) < 32 for ch in raw):
        return ""
    if raw.startswith("//"):
        return ""
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _KNOWN_OSS_HOSTS:
            return ""
        path = parsed.path
        if (parsed.hostname or "").lower() == OSS_ENDPOINT:
            # Path-style OSS URLs may include /newecs/ before the object key.
            prefix = f"/{OSS_BUCKET}/"
            if path.startswith(prefix):
                path = path[len(prefix) - 1 :]
        raw = path
    raw = unquote(raw).replace("\\", "/").lstrip("/")
    if not raw or raw.endswith("/") or len(raw.encode("utf-8")) > 1023:
        return ""
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    if parts[0] != "images" or not re.search(
        r"\.(?:jpe?g|png|gif|webp)$", parts[-1], re.IGNORECASE
    ):
        return ""
    return "/".join(parts)


def result_photo(result: Any) -> Dict[str, str]:
    """Normalize both active and legacy GetflashphotoTencent response shapes."""

    data = getattr(result, "data", None)
    candidates = []
    if isinstance(data, str):
        candidates.append(data)
    for item in _walk_dicts(data):
        for key in ("photourl", "photo_url", "url", "path", "message"):
            if item.get(key) not in (None, ""):
                candidates.append(item[key])
    for candidate in candidates:
        path = _safe_oss_path(candidate)
        if path:
            return {"path": path, "url": _object_url(path)}
    return {"path": "", "url": ""}


def result_value(result: Any, *keys: str) -> str:
    data = getattr(result, "data", None)
    for item in _walk_dicts(data):
        for key in keys:
            if item.get(key) not in (None, ""):
                return str(item[key])
    return ""
