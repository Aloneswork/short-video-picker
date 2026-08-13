"""One-shot child process for local, unauthenticated public-page parsing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import re
import signal
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from app_config import MAX_BATCH_LINKS, MAX_BATCH_WORKS
from app_logging import configure_logging


LOGGER = configure_logging("parser")

# The assistant is launched as a fresh Python process, so it must explicitly
# load the app-bundled dependencies rather than relying on the desktop shell.
BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from core import (
    ParseResult,
    build_media_items_from_aweme_json,
    extract_aweme_author,
    extract_douyin_item_id,
    fetch_url,
    make_parse_error,
    parse_share_url,
)
from public_browser import (
    PublicBrowser,
    _is_douyin_profile_url,
    _is_story_detail,
    enrich_previews_and_duplicates,
    parse_douyin_share_fallback,
)


RETRY_WITH_FRESH_BROWSER = {
    "BROWSER-READ",
    "DY-SECURITY-CHECK",
    "DY-PAGE-MISMATCH",
    "DY-PAGE-SHELL",
    "DY-NO-MEDIA",
    "DY-PROFILE-PAGE-MISMATCH",
    "DY-PROFILE-NO-WORKS",
}
PROFILE_WORK_FINAL_RETRY = {
    "DY-SECURITY-CHECK",
    "DY-PAGE-MISMATCH",
    "DY-PAGE-SHELL",
    "DY-NO-MEDIA",
}
PUBLIC_SHARE_FALLBACK = {
    "BROWSER-READ",
    "DY-SECURITY-CHECK",
    "DY-PAGE-MISMATCH",
    "DY-PAGE-SHELL",
    "DY-NO-MEDIA",
}


def _install_termination_cleanup() -> None:
    """Turn termination into an exception so parse_payload's finally runs."""
    def terminate(signum, _frame) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)


def _is_direct_douyin_work_url(link: str) -> bool:
    """Full work URLs can go straight to Edge without a redundant HTML GET."""
    parsed = urlparse(link)
    hostname = (parsed.hostname or "").lower()
    if not extract_douyin_item_id(link):
        return False
    if hostname in {"douyin.com", "www.douyin.com"}:
        return any(marker in parsed.path for marker in ("/video/", "/note/", "/slides/"))
    # iesdouyin.com/share/... is already Douyin's official server-rendered
    # work page. Resolving it first performs the same public request twice,
    # which needlessly increases security-check risk in a batch.
    if hostname in {"iesdouyin.com", "www.iesdouyin.com"}:
        return bool(re.search(r"/share/(?:video|note|slides)/\d+", parsed.path))
    return False


def _is_direct_douyin_profile_url(link: str) -> bool:
    parsed = urlparse(link)
    return (
        parsed.hostname in {"douyin.com", "www.douyin.com"}
        and parsed.path.startswith("/user/")
        and _is_douyin_profile_url(link)
    )


def serialise(result, origin: dict | None = None):
    payload = {
        "source_url": result.source_url,
        "page_url": result.page_url,
        "title": result.title,
        "status": result.status,
        "error": result.error,
        "error_code": result.error_code,
        "error_hint": result.error_hint,
        "retryable": result.retryable,
        "content_type": result.content_type,
        "content_label": result.content_label,
        "author": result.author,
        "debug": result.debug,
        "items": [asdict(item) for item in result.items],
    }
    if origin:
        payload.update(origin)
    return payload


def _first_address_url(value) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("url_list", "urlList", "download_url_list", "downloadUrlList"):
        urls = value.get(key)
        if isinstance(urls, list):
            url = next(
                (str(candidate) for candidate in urls if str(candidate).startswith(("http://", "https://"))),
                "",
            )
            if url:
                return url
    return ""


def _profile_video_result(work_url: str, payload: dict | None):
    """Use video data already returned to the public profile page."""
    if not isinstance(payload, dict):
        return None
    # Image posts and Live-photo sets need their per-image relationship data;
    # retain the existing detail-page parser for those richer content types.
    if isinstance(payload.get("images"), list) and payload.get("images"):
        return None
    item_id = extract_douyin_item_id(work_url)
    share_info = payload.get("share_info") or {}
    share_desc = share_info.get("share_desc") if isinstance(share_info, dict) else ""
    title = str(
        payload.get("desc")
        or share_desc
        or ""
    ).strip()
    items = build_media_items_from_aweme_json(
        work_url,
        work_url,
        payload,
        title,
        expected_item_id=item_id,
    )
    videos = [item for item in items if item.kind == "video"]
    if not videos:
        return None
    item = videos[0]
    video = payload.get("video") or {}
    if isinstance(video, dict):
        for key in ("cover", "origin_cover", "originCover", "dynamic_cover", "dynamicCover"):
            preview_url = _first_address_url(video.get(key))
            if preview_url:
                item.preview_url = preview_url
                break
    is_story = _is_story_detail(payload)
    return ParseResult(
        work_url,
        work_url,
        title,
        [item],
        debug=["已复用主页公开作品列表中的媒体数据，跳过重复打开该视频页面。"],
        content_type="story" if is_story else "",
        content_label="限时日常" if is_story else "",
        author=extract_aweme_author(payload),
    )


def parse_payload(
    payload: dict,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    links = payload.get("links", [])
    truncated = len(links) > MAX_BATCH_LINKS
    if truncated:
        links = links[:MAX_BATCH_LINKS]

    browser: PublicBrowser | None = None
    preview_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-pipeline")
    preview_jobs = []
    parsed_entries: list[tuple[object, dict]] = []
    expanded_truncated = False
    profile_inputs = 0
    profile_works = 0

    def report_progress(input_index: int, link: str, entry_start: int) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "completed": input_index,
                "total": len(links),
                "current": link,
                "results": [
                    serialise(result, origin)
                    for result, origin in parsed_entries[entry_start:]
                ],
            }
        )

    def ensure_browser() -> PublicBrowser:
        nonlocal browser
        if browser is None:
            candidate = PublicBrowser()
            try:
                candidate.start()
            # SIGTERM/SIGINT are converted to SystemExit so parse_payload's
            # outer finally can run. They may arrive while start() is still
            # booting, before ``browser = candidate``. Catch BaseException
            # only for resource cleanup, then immediately re-raise it.
            except BaseException:
                candidate.close()
                raise
            browser = candidate
        return browser

    def reset_browser() -> PublicBrowser:
        nonlocal browser
        if browser is not None:
            browser.close()
            browser = None
        return ensure_browser()

    def resolve_link(link: str) -> str:
        if _is_direct_douyin_work_url(link) or _is_direct_douyin_profile_url(link):
            return link
        try:
            page_url, _body, _headers, _trace = fetch_url(link)
            return page_url
        except OSError:
            return link

    def queue_preview(result) -> None:
        if not result.items:
            return
        future = preview_pool.submit(
            enrich_previews_and_duplicates,
            result.items,
            result.debug,
        )
        preview_jobs.append((result, future))

    def parse_work(link: str, page_url: str, *, profile_origin: bool = False):
        nonlocal browser
        try:
            if extract_douyin_item_id(f"{link} {page_url}"):
                # Prefer Douyin's official, server-rendered public share route.
                # Recent works may be absent from a long-lived desktop page
                # session even though this anonymous route already exposes the
                # exact item. Edge remains the fallback for unsupported pages.
                public_result = parse_douyin_share_fallback(link, page_url)
                if public_result is not None:
                    queue_preview(public_result)
                    return public_result
                result = ensure_browser().parse_douyin(link, page_url)
                if result.error_code in PUBLIC_SHARE_FALLBACK:
                    first_result = result
                    fallback = parse_douyin_share_fallback(link, page_url)
                    if fallback is not None:
                        result = fallback
                        result.debug.insert(
                            0,
                            f"作品主页面返回 {first_result.error_code}，已自动改用官方公开分享页兜底。",
                        )
                if result.error_code in RETRY_WITH_FRESH_BROWSER:
                    first_code = result.error_code
                    result = reset_browser().parse_douyin(link, page_url)
                    result.debug.insert(0, f"首次解析返回 {first_code}，已使用全新临时会话自动重试一次。")
                    if result.error_code in PUBLIC_SHARE_FALLBACK:
                        second_result = result
                        fallback = parse_douyin_share_fallback(link, page_url)
                        if fallback is not None:
                            result = fallback
                            result.debug.insert(
                                0,
                                f"新会话仍返回 {second_result.error_code}，官方公开分享页复查成功。",
                            )
                # A long profile batch can very occasionally miss one otherwise
                # public work even after the fresh-session retry. Recheck only
                # that failed profile work once in the already-open new session;
                # this adds no browser concurrency and does not slow successful
                # items or ordinary pasted work links.
                if profile_origin and result.error_code in PROFILE_WORK_FINAL_RETRY:
                    second_code = result.error_code
                    result = ensure_browser().parse_douyin(link, page_url)
                    result.debug.insert(
                        0,
                        f"主页作品重试仍返回 {second_code}，已在同一临时会话中完成最后一次复查。",
                    )
                queue_preview(result)
                # A confirmed unavailable page can leave the anonymous profile
                # in a stalled/loading state. Do not let that state contaminate
                # the next valid work in the batch. Successful works still reuse
                # one session, preserving the anti-challenge reliability fix.
                if result.error_code == "DY-WORK-UNAVAILABLE":
                    browser.close()
                    browser = None
            else:
                result = parse_share_url(link, use_browser_session=False)
            return result
        except Exception as exc:  # noqa: BLE001 - one bad link must not block a batch.
            text = str(exc)
            if extract_douyin_item_id(f"{link} {page_url}"):
                fallback = parse_douyin_share_fallback(link, page_url)
                if fallback is not None:
                    fallback.debug.insert(
                        0,
                        "本地 Edge 解析器未能启动或连接，已自动改用抖音官方公开分享页。",
                    )
                    queue_preview(fallback)
                    return fallback
            if "未找到独立的 Edge" in text:
                code = "BROWSER-NOT-FOUND"
            elif "浏览器启动" in text or "浏览器提前退出" in text:
                code = "BROWSER-START"
            else:
                code = "PARSE-UNEXPECTED"
            return make_parse_error(link, page_url, code, detail=text)

    def parse_profile(link: str, page_url: str):
        try:
            profile = ensure_browser().parse_douyin_profile(link, page_url, limit=MAX_BATCH_WORKS)
            if profile.error_code in RETRY_WITH_FRESH_BROWSER:
                first_code = profile.error_code
                profile = reset_browser().parse_douyin_profile(link, page_url, limit=MAX_BATCH_WORKS)
                profile.debug.insert(0, f"首次读取主页返回 {first_code}，已使用全新临时会话自动重试一次。")
            return profile
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "未找到独立的 Edge" in text:
                code = "BROWSER-NOT-FOUND"
            elif "浏览器启动" in text or "浏览器提前退出" in text:
                code = "BROWSER-START"
            else:
                code = "PARSE-UNEXPECTED"
            return make_parse_error(link, page_url, code, detail=text)

    # Douyin's public page now commonly presents a short-lived browser check to
    # every fresh profile. Reuse one isolated Edge session for the whole batch
    # so its public-page cookies survive between links, and navigate serially
    # rather than opening three simultaneous anonymous profiles that are more
    # likely to remain on the empty verification shell.
    try:
        work_slots = 0
        for input_index, link in enumerate(links, start=1):
            if work_slots >= MAX_BATCH_WORKS:
                expanded_truncated = True
                break
            entry_start = len(parsed_entries)
            page_url = resolve_link(link)
            if _is_douyin_profile_url(page_url):
                profile_inputs += 1
                profile = parse_profile(link, page_url)
                if hasattr(profile, "work_urls"):
                    profile_url = profile.page_url or page_url
                    profile_title = profile.title or "抖音用户主页"
                    if profile.error_code:
                        error = make_parse_error(
                            link,
                            profile_url,
                            profile.error_code,
                            title=profile_title,
                            status="empty",
                            debug=profile.debug,
                        )
                        # 让匿名渠道限制的原因直接可见，便于反馈定位。
                        diagnostic_line = next(
                            (line for line in (profile.debug or []) if line.startswith("主页诊断：")),
                            "",
                        )
                        if diagnostic_line:
                            error.error_hint = f"{error.error_hint}（{diagnostic_line}）"
                        error.author = profile_title
                        parsed_entries.append(
                            (
                                error,
                                {
                                    "origin_type": "profile",
                                    "input_url": link,
                                    "profile_url": profile_url,
                                    "profile_title": profile_title,
                                    "profile_index": 0,
                                    "profile_count": 0,
                                },
                            )
                        )
                        work_slots += 1
                        report_progress(input_index, link, entry_start)
                        continue
                    remaining = MAX_BATCH_WORKS - work_slots
                    work_urls = profile.work_urls[:remaining]
                    if len(profile.work_urls) > remaining:
                        expanded_truncated = True
                    profile_count = len(work_urls)
                    profile_works += profile_count
                    profile_payloads = getattr(profile, "work_payloads", {})
                    for profile_index, work_url in enumerate(work_urls, start=1):
                        result = _profile_video_result(
                            work_url,
                            profile_payloads.get(work_url),
                        )
                        if result is None:
                            result = parse_work(work_url, work_url, profile_origin=True)
                        else:
                            queue_preview(result)
                        if not result.author:
                            result.author = profile_title
                        result.debug.insert(
                            0,
                            f"该作品来自用户主页“{profile_title}”（{profile_index}/{profile_count}）。",
                        )
                        parsed_entries.append(
                            (
                                result,
                                {
                                    "origin_type": "profile",
                                    "input_url": link,
                                    "profile_url": profile_url,
                                    "profile_title": profile_title,
                                    "profile_index": profile_index,
                                    "profile_count": profile_count,
                                },
                            )
                        )
                    work_slots += profile_count
                    report_progress(input_index, link, entry_start)
                    continue
                # parse_profile converted an unexpected setup failure directly
                # into the standard ParseResult error shape.
                parsed_entries.append(
                    (
                        profile,
                        {
                            "origin_type": "profile",
                            "input_url": link,
                            "profile_url": page_url,
                            "profile_title": "抖音用户主页",
                            "profile_index": 0,
                            "profile_count": 0,
                        },
                    )
                )
                work_slots += 1
            else:
                parsed_entries.append((parse_work(link, page_url), {}))
                work_slots += 1
            report_progress(input_index, link, entry_start)
    finally:
        if browser is not None:
            browser.close()
        preview_pool.shutdown(wait=True)
    for result, future in preview_jobs:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - previews are optional.
            result.debug.append(f"缩略图后台生成失败：{exc}")
    results = [serialise(result, origin) for result, origin in parsed_entries]
    total = sum(len(result["items"]) for result in results)
    errors = sum(1 for result in results if not result["items"])
    message = f"已处理 {len(links)} 个输入链接，解析 {len(results)} 个作品，发现 {total} 个资源"
    if profile_inputs:
        message += f"，其中 {profile_works} 个作品来自 {profile_inputs} 个用户主页"
    if truncated:
        message += f"，输入链接最多 {MAX_BATCH_LINKS} 条，剩余链接未执行"
    if expanded_truncated:
        message += f"，单批作品最多 {MAX_BATCH_WORKS} 个，超出部分未执行"
    if errors:
        message += f"，{errors} 个链接需要处理"
    return {"ok": total > 0, "message": message, "results": results}


def main() -> int:
    _install_termination_cleanup()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if "--stream" in sys.argv[1:]:
            def emit_progress(event: dict[str, Any]) -> None:
                print(json.dumps({"event": "progress", **event}, ensure_ascii=False), flush=True)

            result = parse_payload(payload, progress_callback=emit_progress)
            print(json.dumps({"event": "complete", "result": result}, ensure_ascii=False), flush=True)
        else:
            print(json.dumps(parse_payload(payload), ensure_ascii=False))
    except BaseException:
        LOGGER.exception("parser_fatal")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
