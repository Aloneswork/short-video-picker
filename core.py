from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from io import BytesIO
from html import unescape
import json
import os
import re
import subprocess
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from app_logging import configure_logging


LOGGER = configure_logging("core")


APP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
MEDIA_EXT_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp4|mov|m4v|webm|jpg|jpeg|png|webp|heic)"
    r"(?:\?[^\s\"'<>]*)?",
    re.I,
)


@dataclass
class MediaItem:
    source_url: str
    page_url: str
    media_url: str
    kind: str
    title: str = ""
    suggested_name: str = ""
    note: str = ""
    selected: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    # A locally produced video first-frame preview, or the image URL itself.
    preview_url: str = ""
    # Explains why a visually duplicate resource is not selected by default.
    relationship_note: str = ""
    # UI-only download state; accepted on later batch-save calls.
    save_state: str = ""
    # Alternate public renditions for the same user-visible video. The selected
    # rendition is copied into ``media_url`` before downloading.
    quality_options: list[dict[str, Any]] = field(default_factory=list)
    quality_label: str = ""


@dataclass
class ParseResult:
    source_url: str
    page_url: str
    title: str
    items: list[MediaItem]
    status: str = "ok"
    error: str = ""
    debug: list[str] = field(default_factory=list)
    error_code: str = ""
    error_hint: str = ""
    retryable: bool = False
    # Optional front-end classification for special public work types such as
    # Douyin's time-limited Story/“日常”. It does not affect save semantics.
    content_type: str = ""
    content_label: str = ""
    # Public author nickname, when the source page exposes it anonymously.
    # Kept at the end so older positional construction remains compatible.
    author: str = ""


# Stable, user-visible codes for failures that can occur across different
# platforms and parser implementations. Keep the message short; error_hint is
# the next action shown beneath it in the desktop UI.
PARSE_ERROR_CATALOG: dict[str, tuple[str, str, bool]] = {
    "INPUT-NO-LINK": (
        "没有识别到可解析的分享链接。",
        "请先粘贴至少一个完整的 http 或 https 链接。",
        False,
    ),
    "PARSE-CANCELLED": (
        "已终止解析。",
        "该链接尚未完成解析，可稍后单独重试。",
        True,
    ),
    "PARSE-BUSY": (
        "已有解析任务正在进行。",
        "请等待当前任务结束或先终止当前任务，再重试该链接。",
        True,
    ),
    "NET-FETCH": (
        "分享链接暂时无法访问。",
        "请检查网络后重试；若原链接能在浏览器中打开，请反馈此错误码。",
        True,
    ),
    "BROWSER-NOT-FOUND": (
        "未找到本地解析所需的 Microsoft Edge。",
        "请安装或恢复 Microsoft Edge 后重新打开程序。",
        False,
    ),
    "BROWSER-START": (
        "本地解析浏览器启动失败。",
        "请关闭残留的 Edge 后重试；若持续出现，请反馈此错误码。",
        True,
    ),
    "BROWSER-CONNECT": (
        "解析器无法连接本地解析浏览器。",
        "请重试一次；若持续出现，请重新打开程序。",
        True,
    ),
    "BROWSER-READ": (
        "公开页面读取过程中断。",
        "请重试该链接；若持续出现，请反馈此错误码。",
        True,
    ),
    "DY-SECURITY-CHECK": (
        "抖音公开页面仍在安全校验，等待和刷新后仍未返回作品资源。",
        "请稍后再试，避免连续快速解析大量链接。",
        True,
    ),
    "DY-WORK-UNAVAILABLE": (
        "抖音公开页显示该作品不存在或当前不可观看。",
        "请在抖音中打开原链接确认；作品可能已删除、转为私密或仅登录后可见。",
        False,
    ),
    "DY-WORK-NOT-PUBLIC": (
        "抖音官方公开分享页明确拒绝匿名访问该作品。",
        "页面返回仅作者可见或作品权限受限；未登录程序不会绕过权限，请在抖音中确认作品公开范围。",
        False,
    ),
    "DY-PAGE-MISMATCH": (
        "公开页面跳转到了其他作品，已停止提取以避免保存错误素材。",
        "请重试一次；若仍出现，请在抖音中确认原链接是否已失效。",
        True,
    ),
    "DY-PAGE-SHELL": (
        "作品页面已打开，但抖音没有向当前匿名会话返回作品详情。",
        "程序会自动尝试官方公开分享页和新会话；若仍失败，请稍后再试并避免连续快速解析。",
        True,
    ),
    "DY-NO-MEDIA": (
        "已读取作品详情，但其中没有返回可保存的媒体地址。",
        "程序已尝试公开分享页兜底；若仍失败，请反馈错误码和原链接以更新解析规则。",
        True,
    ),
    "DY-STORY-EXPIRED": (
        "抖音公开页面明确显示该限时日常已过期。",
        "请让作者将日常转为长期公开作品，或在新的日常仍处于展示期时解析。",
        False,
    ),
    "DY-STORY-WEB-FILTERED": (
        "该日常可以在抖音 App 内公开查看，但抖音没有向未登录网页返回媒体。",
        "这是 App 与网页的公开数据范围不同，并非关注权限问题；程序不会读取手机登录状态或绕过平台渠道限制。",
        False,
    ),
    "DY-STORY-NOT-PUBLIC": (
        "该限时日常没有对未登录用户公开展示。",
        "仅好友、密友或私密可见的日常不会被绕过；请让作者调整可见范围后再试。",
        False,
    ),
    "DY-STORY-UNAVAILABLE": (
        "抖音公开分享页识别到限时日常，但当前网页没有返回可保存媒体。",
        "作品可能仅在 App 内下发，也可能刚发布尚未完成网页同步；请稍后重试一次。",
        True,
    ),
    "DY-PROFILE-UNAVAILABLE": (
        "抖音公开页显示该用户主页不存在或当前不可访问。",
        "请在抖音中打开主页链接确认；账号可能已注销、设为私密或仅登录后可见。",
        False,
    ),
    "DY-PROFILE-PAGE-MISMATCH": (
        "公开页面没有停留在目标用户主页，已停止提取以避免解析错误账号。",
        "请重试一次；若仍出现，请确认主页链接是否完整有效。",
        True,
    ),
    "DY-PROFILE-NO-WORKS": (
        "用户主页已打开，但没有找到可公开访问的作品。",
        "该用户可能尚未发布作品、作品均为私密，或主页暂时未完成加载。",
        True,
    ),
    "PARSER-CRASH": (
        "解析助手未能正常完成。",
        "请重试一次；若持续出现，请重新打开程序并反馈此错误码。",
        True,
    ),
    "PARSER-TIMEOUT": (
        "本次解析超过等待时限。",
        "请减少单批链接数量后重试。",
        True,
    ),
    "PARSER-BAD-OUTPUT": (
        "解析助手返回了无法识别的结果。",
        "请重新打开程序后重试；若持续出现，请反馈此错误码。",
        True,
    ),
    "PARSE-UNEXPECTED": (
        "解析过程中出现未分类异常。",
        "请重试一次；若持续出现，请反馈错误码和原链接。",
        True,
    ),
}


def make_parse_error(
    source_url: str,
    page_url: str,
    code: str,
    *,
    title: str = "",
    status: str = "error",
    debug: list[str] | None = None,
    detail: str = "",
) -> ParseResult:
    message, hint, retryable = PARSE_ERROR_CATALOG.get(code, PARSE_ERROR_CATALOG["PARSE-UNEXPECTED"])
    diagnostics = list(debug or [])
    if detail:
        diagnostics.append(f"异常详情：{detail}")
    return ParseResult(
        source_url=source_url,
        page_url=page_url or source_url,
        title=title,
        items=[],
        status=status,
        error=message,
        debug=diagnostics,
        error_code=code,
        error_hint=hint,
        retryable=retryable,
    )


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def extract_links(text: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for raw in URL_RE.findall(text):
        link = raw.strip().rstrip(").,;，。")
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def fetch_url(url: str, timeout: int = 18, method: str = "GET") -> tuple[str, bytes, dict[str, str], list[str]]:
    opener = build_opener(NoRedirectHandler)
    current_url = url
    trace: list[str] = []
    last_page: tuple[str, bytes, dict[str, str]] | None = None
    for _ in range(8):
        trace.append(redact_debug_url(current_url))
        request = make_request(current_url, method)
        try:
            with opener.open(request, timeout=timeout) as response:
                final_url = response.geturl()
                data = response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
                return final_url, data, headers, trace
        except HTTPError as exc:
            headers = {k.lower(): v for k, v in exc.headers.items()}
            body = exc.read()
            if 300 <= exc.code < 400 and headers.get("location"):
                current_url = urljoin(current_url, headers["location"])
                continue
            if body and "text/" in headers.get("content-type", ""):
                last_page = (current_url, body, headers)
            if last_page:
                page_url, data, page_headers = last_page
                trace.append(f"HTTP {exc.code}: {redact_debug_url(current_url)}")
                return page_url, data, page_headers, trace
            raise
    raise URLError("Too many redirects")


def make_request(url: str, method: str = "GET") -> Request:
    request = Request(
        url,
        method=method,
        headers={
            "User-Agent": APP_UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    return request


def redact_debug_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="…" if parsed.query else "", fragment=""))


def parse_share_url(source_url: str, use_browser_session: bool = False) -> ParseResult:
    try:
        page_url, body, headers, trace = fetch_url(source_url)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return make_parse_error(source_url, source_url, "NET-FETCH", detail=str(exc))

    content_type = headers.get("content-type", "")
    if "video/" in content_type or "image/" in content_type:
        kind = "video" if "video/" in content_type else "image"
        item = MediaItem(
            source_url=source_url,
            page_url=page_url,
            media_url=page_url,
            kind=kind,
            suggested_name=suggest_name("", page_url, kind, source_url=source_url),
        )
        return ParseResult(source_url, page_url, "", [item], debug=trace)

    html = decode_body(body, content_type)
    title = extract_title(html)
    media_urls = discover_media_urls(html)
    douyin_id = extract_douyin_item_id(" ".join([source_url, page_url, html[:4000]]))
    douyin_debug: list[str] = []
    items: list[MediaItem] = []
    seen: set[str] = set()
    for idx, media_url in enumerate(media_urls, start=1):
        normalized = normalize_media_url(media_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        kind = infer_kind(normalized)
        items.append(
            MediaItem(
                source_url=source_url,
                page_url=page_url,
                media_url=normalized,
                kind=kind,
                title=title,
                suggested_name=suggest_name(title, normalized, kind, idx, source_url=page_url or source_url),
            )
        )

    if not items:
        error = "No direct media URL found in the public page response."
        if douyin_id:
            error = (
                "公开页面没有直接返回作品资源。请使用本地解析助手处理该链接。"
            )
        return ParseResult(
            source_url,
            page_url,
            title,
            [],
            status="empty",
            error=error,
            debug=trace + douyin_debug,
        )
    return ParseResult(source_url, page_url, title, items, debug=trace)


def extract_douyin_item_id(text: str) -> str:
    patterns = [
        r"/(?:note|video|slides)/(\d{10,24})",
        r"[?&](?:aweme_id|item_id|item_ids|gids)=\[?(\d{10,24})",
        r"\b(\d{18,20})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def build_media_items_from_aweme_json(
    source_url: str, page_url: str, data: Any, title: str, expected_item_id: str = ""
) -> list[MediaItem]:
    urls = extract_aweme_media_urls(data, expected_item_id)
    if not urls and not expected_item_id:
        urls = walk_json_for_media(data)
    quality_options = extract_aweme_video_quality_options(data, expected_item_id)
    if quality_options and urls:
        # A normal Douyin video is one user-visible resource. Prefer the
        # highest advertised rendition while keeping every choice as metadata
        # for the desktop selector. Image/Live posts deliberately return no
        # top-level quality options here.
        current_video = next((url for url in urls if infer_kind(url) == "video"), "")
        if current_video:
            urls = [str(quality_options[0]["url"]) if url == current_video else url for url in urls]
    output: list[MediaItem] = []
    for index, media_url in enumerate(unique_urls(urls), start=1):
        kind = infer_kind(media_url)
        item = MediaItem(
                source_url=source_url,
                page_url=page_url,
                media_url=media_url,
                kind=kind,
                title=title,
                suggested_name=suggest_name(title, media_url, kind, index, source_url=page_url or source_url),
            )
        if kind == "video" and quality_options and not any(value.quality_options for value in output):
            item.quality_options = quality_options
            item.quality_label = str(quality_options[0]["label"])
        output.append(item)
    return output


def extract_aweme_author(value: Any) -> str:
    """Return the public nickname attached to one Douyin work payload."""
    if not isinstance(value, dict):
        return ""
    author = value.get("author") or value.get("authorInfo") or value.get("author_info")
    if not isinstance(author, dict):
        return ""
    for key in ("nickname", "nickName", "name", "displayName", "display_name"):
        nickname = str(author.get(key) or "").strip()
        if nickname:
            return nickname[:120]
    return ""


VIDEO_ADDRESS_KEYS = (
    "play_addr",
    "playAddr",
    "play_addr_h264",
    "playAddrH264",
    "play_addr_h265",
    "playAddrH265",
    "play_addr_265",
    "playAddr265",
    "play_addr_bytevc1",
    "playAddrBytevc1",
    "play_addr_lowbr",
    "playAddrLowbr",
    "download_addr",
    "downloadAddr",
    "url_list",
    "urlList",
)
VIDEO_BITRATE_KEYS = ("bit_rate", "bitRate")
LIVE_VIDEO_KEYS = ("video", "live_photo", "livePhoto")


def _dedupe_public_urls(values: list[str]) -> list[str]:
    """De-duplicate without decoding or otherwise changing signed CDN URLs."""
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def urls_from_media_address(value: Any) -> list[str]:
    """Read public URLs from snake/camel-case Douyin address containers."""
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, list):
        output: list[str] = []
        for child in value:
            output.extend(urls_from_media_address(child))
        return output
    if not isinstance(value, dict):
        return []

    output = []
    for key in ("src", "url", "mainUrl", "main_url"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            output.append(candidate)
    for key in ("url_list", "urlList", "download_url_list", "downloadUrlList"):
        output.extend(urls_from_media_address(value.get(key)))
    return output


def extract_video_media_urls(value: Any) -> list[str]:
    """Return playback URLs, falling back to rendition lists when necessary.

    One address usually contains several CDN aliases for the same stream.  The
    caller decides whether it needs only the first usable alias or the full
    list; alternate bitrates are consulted only when no direct address exists.
    """
    if isinstance(value, list):
        output: list[str] = []
        for child in value:
            output.extend(extract_video_media_urls(child))
        return _dedupe_public_urls(output)
    if not isinstance(value, dict):
        return urls_from_media_address(value)

    direct: list[str] = []
    for key in VIDEO_ADDRESS_KEYS:
        direct.extend(urls_from_media_address(value.get(key)))
    if direct:
        return _dedupe_public_urls(direct)

    renditions: list[str] = []
    for key in VIDEO_BITRATE_KEYS:
        entries = value.get(key)
        if not isinstance(entries, (dict, list)):
            continue
        renditions.extend(extract_video_media_urls(entries))
    return _dedupe_public_urls(renditions)


def extract_video_quality_options(value: Any) -> list[dict[str, Any]]:
    """Return unique bitrate renditions ordered from highest to lowest quality."""
    if not isinstance(value, dict):
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in VIDEO_BITRATE_KEYS:
        entries = value.get(key)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            urls = extract_video_media_urls(entry)
            if not urls:
                continue
            url = urls[0]
            if url in seen:
                continue
            seen.add(url)
            raw_bitrate = entry.get("bit_rate", entry.get("bitRate", entry.get("bitrate", 0)))
            try:
                bitrate = int(raw_bitrate or 0)
            except (TypeError, ValueError):
                bitrate = 0
            gear_name = str(
                entry.get("gear_name")
                or entry.get("gearName")
                or entry.get("quality_type")
                or entry.get("qualityType")
                or ""
            ).strip()
            label = gear_name.replace("_", " ") if gear_name else ""
            if not label and bitrate:
                label = f"{bitrate / 1_000_000:.1f} Mbps" if bitrate >= 1_000_000 else f"{bitrate // 1000} Kbps"
            output.append({"label": label or f"清晰度 {len(output) + 1}", "url": url, "bitrate": bitrate})
    output.sort(key=lambda option: int(option.get("bitrate") or 0), reverse=True)
    return output


def extract_aweme_video_quality_options(data: Any, expected_item_id: str = "") -> list[dict[str, Any]]:
    """Return normal-video renditions from the matching public work node."""
    for node in walk_json_nodes(data):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("aweme_id", node.get("awemeId", "")))
        if expected_item_id and node_id != expected_item_id:
            continue
        video = node.get("video")
        if not isinstance(video, dict):
            continue
        top_video_url = first_video_media_url(video)
        if not top_video_url or is_aweme_image_post(node, top_video_url):
            continue
        options = extract_video_quality_options(video)
        if options:
            return options
    return []


def first_video_media_url(value: Any) -> str:
    urls = extract_video_media_urls(value)
    return urls[0] if urls else ""


def is_aweme_image_post(detail: dict[str, Any], top_video_url: str = "") -> bool:
    """Distinguish an image/Live set from a normal video with cover nodes."""
    images = detail.get("images")
    if not isinstance(images, list) or not images:
        return False
    aweme_type = detail.get("awemeType", detail.get("aweme_type"))
    return (
        not top_video_url
        or bool(detail.get("isSlides") or detail.get("is_slides"))
        or (aweme_type is not None and str(aweme_type) != "0")
        or ".mp3" in unquote(top_video_url).lower()
    )


def extract_aweme_media_urls(data: Any, expected_item_id: str = "") -> list[str]:
    """Extract the user-visible media items from exact Douyin work nodes."""
    output: list[str] = []
    for node in walk_json_nodes(data):
        if not isinstance(node, dict):
            continue
        if not any(key in node for key in ("aweme_id", "awemeId", "images", *LIVE_VIDEO_KEYS)):
            continue
        if expected_item_id and str(node.get("aweme_id", node.get("awemeId", ""))) != expected_item_id:
            continue

        images = [image for image in (node.get("images") or []) if isinstance(image, dict)]
        top_video_url = first_video_media_url(node.get("video"))
        image_post = is_aweme_image_post(node, top_video_url)
        if images and (image_post or not top_video_url):
            for image in images:
                live_url = ""
                for video_key in LIVE_VIDEO_KEYS:
                    live_url = first_video_media_url(image.get(video_key))
                    if live_url:
                        break
                if live_url:
                    output.append(live_url)
                else:
                    output.extend(urls_from_address(image))

        # Image/Live posts also expose a top-level aggregate player (often the
        # background MP3). It is not another saveable item. Normal videos keep
        # exactly one authoritative player even if an image-shaped cover node
        # repeats the same motion stream.
        if top_video_url and not image_post:
            output.append(top_video_url)
        for video_key in LIVE_VIDEO_KEYS[1:]:
            live_url = first_video_media_url(node.get(video_key))
            if live_url:
                output.append(live_url)
    return unique_urls(output)


def walk_json_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json_nodes(child)


def urls_from_address(value: Any) -> list[str]:
    """Backward-compatible name for image/public address extraction."""
    return urls_from_media_address(value)


def decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract_title(html: str) -> str:
    candidates = [
        r"<title[^>]*>(.*?)</title>",
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in candidates:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            text = re.sub(r"\s+", " ", unescape(match.group(1))).strip()
            if text:
                return text[:90]
    return ""


def discover_media_urls(html: str) -> list[str]:
    urls: list[str] = []
    urls.extend(extract_media_from_json_blobs(html))
    urls.extend(MEDIA_EXT_RE.findall(unescape(html)))
    urls.extend(extract_meta_media(html))
    return unique_urls(urls)


def extract_meta_media(html: str) -> list[str]:
    urls: list[str] = []
    for attr in ("og:video", "og:video:url", "og:image", "twitter:image"):
        pattern = (
            r'<meta[^>]+(?:property|name)=["\']'
            + re.escape(attr)
            + r'["\'][^>]+content=["\']([^"\']+)["\']'
        )
        urls.extend(re.findall(pattern, html, re.I))
    return urls


def extract_media_from_json_blobs(html: str) -> list[str]:
    urls: list[str] = []
    script_re = re.compile(r"<script[^>]*>(.*?)</script>", re.I | re.S)
    for script in script_re.findall(html):
        script = unescape(script).strip()
        if not script or "http" not in script:
            continue
        urls.extend(extract_urls_from_text(script))
        for json_text in likely_json_objects(script):
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                continue
            urls.extend(walk_json_for_media(data))
    return urls


def likely_json_objects(text: str) -> list[str]:
    blobs: list[str] = []
    for marker in ("window.__INITIAL_STATE__=", "window._ROUTER_DATA=", "__NEXT_DATA__"):
        index = text.find(marker)
        if index == -1:
            continue
        start = text.find("{", index)
        if start == -1:
            continue
        blob = balanced_object(text, start)
        if blob:
            blobs.append(blob)
    if text.startswith("{") and text.endswith("}"):
        blobs.append(text)
    return blobs


def balanced_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : pos + 1]
    return ""


def walk_json_for_media(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = str(key).lower()
            if isinstance(child, str) and ("url" in key_lower or "uri" in key_lower):
                urls.extend(extract_urls_from_text(child))
            else:
                urls.extend(walk_json_for_media(child))
    elif isinstance(value, list):
        for child in value:
            urls.extend(walk_json_for_media(child))
    elif isinstance(value, str):
        urls.extend(extract_urls_from_text(value))
    return urls


def extract_urls_from_text(text: str) -> list[str]:
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    text = unescape(text)
    return URL_RE.findall(text)


def normalize_media_url(url: str) -> str:
    value = unquote(url).strip().rstrip("\\").rstrip(".,;")
    if value.startswith("//"):
        value = "https:" + value
    return value


def unique_urls(urls: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = normalize_media_url(url)
        if not is_probable_media_url(normalized):
            continue
        key = normalized.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def is_probable_media_url(url: str) -> bool:
    lower = url.lower()
    if any(ext in lower for ext in (".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return True
    media_tokens = ("video", "play", "image", "cover", "origin", "tos-", "douyinpic", "douyinvod")
    return lower.startswith("http") and any(token in lower for token in media_tokens)


def infer_kind(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return "image"
    if path.endswith((".mp4", ".mov", ".m4v", ".webm")):
        return "video"
    if "image" in url.lower() or "cover" in url.lower() or "pic" in url.lower():
        return "image"
    return "video"


def suggest_name(
    title: str,
    media_url: str,
    kind: str,
    index: int = 1,
    *,
    source_url: str = "",
) -> str:
    # The program deliberately normalizes the two user-facing formats.  Image
    # sources can be WEBP/PNG on the CDN, but are converted to JPEG on save.
    ext = ".mp4" if kind == "video" else ".jpg"
    base = slugify(title)
    item_id = extract_douyin_item_id(f"{source_url} {media_url}")
    is_douyin = "douyin" in f"{source_url} {media_url}".lower()
    # Titles are not unique: many unrelated Douyin works use the same caption.
    # Include the stable work ID even when a title exists so a filename collision
    # is not mistaken for an already-downloaded media file.
    if base and item_id and is_douyin:
        base = f"{base}_{item_id}"
    if not base:
        if is_douyin:
            label = "抖音视频" if kind == "video" else "抖音图片"
        else:
            label = "视频资源" if kind == "video" else "图片资源"
        if item_id:
            base = f"{label}_{item_id}"
        else:
            fingerprint = hashlib.sha256(media_url.encode("utf-8", errors="ignore")).hexdigest()[:10]
            base = f"{label}_{fingerprint}"
    return f"{base}_{index:02d}{ext.split('?')[0]}"


def slugify(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text[:80].strip("._")


def download_item_with_details(item: MediaItem, folder: str) -> dict[str, Any]:
    """Download once and classify duplicates by file content, not filename.

    A pre-existing target name is only an exact duplicate when the fully saved
    bytes have the same SHA-256. Different content is kept under a stable
    work-ID/content-digest filename and remains a newly saved resource.
    """
    os.makedirs(folder, exist_ok=True)
    filename = _safe_download_filename(item)
    path = os.path.join(folder, filename)
    temp_path = ""
    try:
        suffix = ".mp4" if item.kind == "video" else ".jpg"
        with tempfile.NamedTemporaryFile(
            prefix=".short-video-picker-",
            suffix=suffix,
            dir=folder,
            delete=False,
        ) as temp:
            temp_path = temp.name
        os.unlink(temp_path)
        _download_item_to_path(item, temp_path)
        incoming_hash = _sha256_file(temp_path)

        if os.path.exists(path):
            if _sha256_file(path) == incoming_hash:
                os.unlink(temp_path)
                temp_path = ""
                return {
                    "path": path,
                    "status": "existing",
                    "reason": "内容 SHA-256 与磁盘文件一致",
                    "matched_path": path,
                    "renamed_due_to_collision": False,
                    "sha256": incoming_hash,
                }
            path = _collision_path(folder, filename, item, incoming_hash)
            if os.path.exists(path):
                if _sha256_file(path) == incoming_hash:
                    os.unlink(temp_path)
                    temp_path = ""
                    return {
                        "path": path,
                        "status": "existing",
                        "reason": "内容 SHA-256 与同名冲突后的磁盘文件一致",
                        "matched_path": path,
                        "renamed_due_to_collision": False,
                        "sha256": incoming_hash,
                    }
                collision_stem, collision_ext = os.path.splitext(path)
                path = f"{collision_stem}_{incoming_hash[:12]}{collision_ext}"
                if os.path.exists(path) and _sha256_file(path) == incoming_hash:
                    os.unlink(temp_path)
                    temp_path = ""
                    return {
                        "path": path,
                        "status": "existing",
                        "reason": "内容 SHA-256 与摘要命名的磁盘文件一致",
                        "matched_path": path,
                        "renamed_due_to_collision": False,
                        "sha256": incoming_hash,
                    }
            os.replace(temp_path, path)
            temp_path = ""
            return {
                "path": path,
                "status": "saved",
                "reason": "文件名相同但内容不同，已自动改名保存",
                "matched_path": "",
                "renamed_due_to_collision": True,
                "sha256": incoming_hash,
            }

        os.replace(temp_path, path)
        temp_path = ""
        return {
            "path": path,
            "status": "saved",
            "reason": "新文件",
            "matched_path": "",
            "renamed_due_to_collision": False,
            "sha256": incoming_hash,
        }
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def download_item_with_status(item: MediaItem, folder: str) -> tuple[str, bool]:
    """Compatibility wrapper returning `(path, created)`."""
    result = download_item_with_details(item, folder)
    return str(result["path"]), result["status"] == "saved"


def _download_item_to_path(item: MediaItem, path: str) -> None:
    headers = {
        "User-Agent": APP_UA,
        "Referer": item.page_url,
        "Accept": "*/*",
    }
    for key in ("User-Agent", "Referer", "Accept"):
        value = str(item.headers.get(key) or "").strip()
        if value:
            headers[key] = value
    if item.kind == "image":
        request = Request(item.media_url, headers=headers)
        with urlopen(request, timeout=45) as response:
            # CDN images may be WEBP, AVIF or PNG.  Save a real JPEG so the
            # visible filename and file contents always agree.
            _save_image_as_jpeg(response.read(), path, item.media_url)
        return

    resume_key = hashlib.sha256(item.media_url.encode("utf-8", errors="ignore")).hexdigest()[:20]
    partial_path = os.path.join(os.path.dirname(path), f".short-video-picker-resume-{resume_key}.part")
    resume_from = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    request = Request(item.media_url, headers=headers)
    try:
        with urlopen(request, timeout=45) as response:
            status_value = getattr(response, "status", None)
            if status_value is None and hasattr(response, "getcode"):
                status_value = response.getcode()
            status = int(status_value or 200)
            append = bool(resume_from and status == 206)
            with open(partial_path, "ab" if append else "wb") as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    output.write(chunk)
        os.replace(partial_path, path)
    except Exception:
        LOGGER.exception("video_download_interrupted url=%s partial=%s", item.media_url, partial_path)
        raise


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _collision_path(folder: str, filename: str, item: MediaItem, digest: str) -> str:
    stem, ext = os.path.splitext(filename)
    item_id = extract_douyin_item_id(f"{item.page_url} {item.source_url} {item.media_url}")
    if item_id and item_id not in stem:
        index_match = re.search(r"(_\d{2})$", stem)
        if index_match:
            stem = f"{stem[:index_match.start()]}_{item_id}{index_match.group(1)}"
        else:
            stem = f"{stem}_{item_id}"
    else:
        stem = f"{stem}_{digest[:12]}"
    return os.path.join(folder, f"{stem}{ext}")


def _safe_download_filename(item: MediaItem) -> str:
    """Return a non-empty, path-safe filename with the canonical media suffix."""
    ext = ".mp4" if item.kind == "video" else ".jpg"
    candidate = os.path.basename(str(item.suggested_name or "").strip())
    raw_stem = os.path.splitext(candidate)[0]
    stem = "" if candidate.lower() == ext else slugify(raw_stem)
    item_id = extract_douyin_item_id(f"{item.page_url} {item.source_url} {item.media_url}")
    if item_id and item_id in raw_stem and item_id not in stem:
        index_match = re.search(r"(_\d{2})$", raw_stem)
        index_suffix = index_match.group(1) if index_match else ""
        title_part = raw_stem.split(f"_{item_id}", 1)[0]
        max_title = max(12, 80 - len(item_id) - len(index_suffix) - 1)
        title_part = slugify(title_part)[:max_title].rstrip("._")
        stem = f"{title_part}_{item_id}{index_suffix}" if title_part else f"{item_id}{index_suffix}"
    if not stem:
        return suggest_name(
            item.title,
            item.media_url,
            item.kind,
            source_url=f"{item.page_url} {item.source_url}",
        )
    return f"{stem}{ext}"


def _save_image_as_jpeg(data: bytes, path: str, source_url: str) -> None:
    """Save image bytes as JPEG, including on Python installs without Pillow.

    `sips` is shipped with macOS, so it is a reliable fallback for the desktop
    app when Finder launches a different Python runtime from the developer's.
    """
    try:
        from PIL import Image

        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        image.save(path, format="JPEG", quality=95, subsampling=0, optimize=True)
        return
    except Exception as pillow_error:  # noqa: BLE001 - use macOS fallback below.
        suffix = os.path.splitext(urlparse(source_url).path)[1] or ".image"
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                temp.write(data)
                temp_path = temp.name
            completed = subprocess.run(
                ["/usr/bin/sips", "-s", "format", "jpeg", temp_path, "--out", path],
                text=True,
                capture_output=True,
                timeout=30,
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip() or str(pillow_error)
                raise RuntimeError(f"图片转换为 JPG 失败：{message}")
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
