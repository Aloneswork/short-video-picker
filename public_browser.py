"""Temporary, unauthenticated browser renderer for public Douyin share pages.

The process creates an ephemeral browser profile, reads only media nodes already
rendered on the public page, and is terminated by the caller after parsing.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from app_config import MAX_BATCH_WORKS

try:
    import websocket
except ImportError:  # Report the missing CDP transport through the normal runtime checks.
    websocket = None

from core import (
    LIVE_VIDEO_KEYS,
    MediaItem,
    ParseResult,
    balanced_object,
    extract_aweme_author,
    extract_video_quality_options,
    first_video_media_url,
    is_aweme_image_post,
    make_parse_error,
    suggest_name,
)
from preview_cache import cache_preview


# Preview generation may process several resources together. Keep total
# first-frame extraction bounded so the Mac stays responsive.
PREVIEW_SEMAPHORE = threading.Semaphore(4)


# Deliberately do not fall back to Google Chrome. The parser must never take
# over the user's daily Chrome process or profile.
BROWSERS = ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",)
BROWSER_START_TIMEOUT = 40
DOUYIN_PAGE_TIMEOUT = 40
DOUYIN_CHALLENGE_RELOAD_AFTER = 16
DOUYIN_PROFILE_TIMEOUT = 50
DOUYIN_SHARE_TIMEOUT = 18
DOUYIN_SHARE_RETRY_DELAY = 1.0
DOUYIN_MOBILE_SHARE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

# Edge 151 on macOS can copy the OS Microsoft account into a brand-new user
# data directory even when the active target is a Guest window.  Guest mode
# keeps that account out of the page session, but the copied identifiers still
# land in ``Local State``.  Disable Edge's macOS implicit-sign-in and PRT SSO
# features as well, so the throw-away renderer never imports that identity in
# the first place.  These are Edge feature names present in the installed
# browser, not access to the user's normal browser profile.
EDGE_DISABLED_FEATURES = (
    "msEdgeMacImplicitSignin",
    "msImplicitSignin",
    "msImplicitSignInNetworkRetry",
    "msProfileSignIn",
    "msCombinedSyncSignIn",
    "msAutoToggleAADPrtSSOForNonAADProfile",
    "msAutoToggleMSAPrtSSOForNonMSAProfile",
    "msAllowMSAPrtSSOForNonMSAProfile",
)


@dataclass
class ProfileResult:
    source_url: str
    page_url: str
    title: str = ""
    work_urls: list[str] = field(default_factory=list)
    debug: list[str] = field(default_factory=list)
    error_code: str = ""
    work_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)


def _browser_user_agent(browser: str) -> str:
    """Keep the advertised Chromium major aligned with the installed Edge."""
    version = "131.0.0.0"
    try:
        info_path = Path(browser).parent.parent / "Info.plist"
        with info_path.open("rb") as info_file:
            installed = str(plistlib.load(info_file).get("CFBundleShortVersionString") or "")
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", installed):
            version = installed
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CDP:
    def __init__(self, websocket_url: str) -> None:
        if websocket is None:
            raise RuntimeError("旧版浏览器连接组件不可用。")
        self.socket = websocket.create_connection(websocket_url, timeout=3)
        self.counter = 0
        self.events: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 8,
    ) -> dict[str, Any]:
        self.counter += 1
        call_id = self.counter
        self.socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                message = json.loads(self.socket.recv())
            except websocket.WebSocketTimeoutException:
                break
            if message.get("id") != call_id:
                if message.get("method"):
                    self.events.append(message)
                    if len(self.events) > 2000:
                        del self.events[: len(self.events) - 2000]
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "浏览器请求失败"))
            return message.get("result", {})
        raise TimeoutError(f"浏览器请求超时：{method}")

    def take_events(self) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    def close(self) -> None:
        self.socket.close()


class PublicBrowser:
    def __init__(self) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.profile = Path(tempfile.mkdtemp(prefix="short-video-picker-public-"))
        self.process: subprocess.Popen[bytes] | None = None
        self._page_websocket_url = ""
        self._page_target_id = ""

    def _request_json(self, path: str, method: str = "GET", timeout: float = 1) -> Any:
        request = Request(f"{self.base}{path}", method=method)
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def start(self) -> None:
        browser = next((path for path in BROWSERS if os.path.exists(path)), "")
        if not browser:
            raise RuntimeError("未找到独立的 Edge 解析组件。")
        self.process = subprocess.Popen(
            [
                browser,
                f"--remote-debugging-port={self.port}",
                f"--remote-allow-origins=http://127.0.0.1:{self.port}",
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--headless",
                "--guest",
                "--no-startup-window",
                "--disable-sync",
                "--disable-background-mode",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--use-mock-keychain",
                "--disable-gpu",
                "--disable-extensions",
                f"--disable-features={','.join(EDGE_DISABLED_FEATURES)}",
                # Avoid announcing the isolated renderer as HeadlessChrome to
                # public pages. This does not use or modify Google Chrome.
                f"--user-agent={_browser_user_agent(browser)}",
                "--window-size=1440,1000",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + BROWSER_START_TIMEOUT
        last_error = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"公开解析浏览器提前退出（状态 {self.process.returncode}）。")
            try:
                if self._request_json("/json/version").get("webSocketDebuggerUrl"):
                    return
            except OSError as exc:
                last_error = str(exc)
            time.sleep(0.25)
        detail = f"：{last_error}" if last_error else ""
        raise RuntimeError(f"公开解析浏览器启动超时{detail}")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._page_websocket_url = ""
        self._page_target_id = ""
        # Edge's Rosetta helper processes can briefly recreate the user-data
        # directory after the main process has already exited.  Do not stop at
        # the first successful rmtree: require the path to stay absent for a
        # short grace period, otherwise termination can leave an empty profile
        # directory behind.
        cleanup_deadline = time.monotonic() + 5
        absent_since: float | None = None
        while time.monotonic() < cleanup_deadline:
            shutil.rmtree(self.profile, ignore_errors=True)
            if self.profile.exists():
                absent_since = None
            elif absent_since is None:
                absent_since = time.monotonic()
            elif time.monotonic() - absent_since >= 0.5:
                break
            time.sleep(0.1)

    def _page_client(self) -> _CDP:
        """Open a fresh page target while retaining this batch's cookies."""
        # An unavailable work page can redirect itself to a recommendation after
        # rendering its error. Reusing that target lets the old redirect race the
        # next Page.navigate call. A fresh target isolates each work while the
        # shared browser/profile still preserves public-page verification state.
        if self._page_target_id:
            try:
                request = Request(f"{self.base}/json/close/{quote(self._page_target_id, safe='')}")
                with urlopen(request, timeout=2) as response:
                    response.read()
            except Exception:
                pass
            self._page_target_id = ""
            self._page_websocket_url = ""
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                target = self._request_json(
                    f"/json/new?{quote('about:blank', safe=':/?=&')}",
                    method="PUT",
                    timeout=4,
                )
                self._page_target_id = str(target.get("id") or "")
                self._page_websocket_url = target["webSocketDebuggerUrl"]
                return _CDP(self._page_websocket_url)
            except Exception as exc:  # Guest startup may expose CDP before the page socket is ready.
                last_error = exc
                if self._page_target_id:
                    try:
                        request = Request(
                            f"{self.base}/json/close/{quote(self._page_target_id, safe='')}"
                        )
                        with urlopen(request, timeout=2) as response:
                            response.read()
                    except Exception:
                        pass
                self._page_target_id = ""
                self._page_websocket_url = ""
                if attempt < 3:
                    time.sleep(0.5)
        raise RuntimeError(f"临时 Guest 页面连接失败：{last_error}")

    def parse_douyin_profile(
        self,
        source_url: str,
        page_url: str = "",
        *,
        limit: int = MAX_BATCH_WORKS,
    ) -> ProfileResult:
        """Read the first public works shown on a Douyin user profile."""
        resolved_page = page_url or source_url
        navigation_url = _canonical_douyin_profile_url(resolved_page)
        debug = ["已使用未登录的公开页面解析助手读取用户主页。"]
        if navigation_url != resolved_page:
            debug.append("已移除主页分享追踪参数，使用规范地址读取。")
        try:
            client = self._page_client()
        except Exception as exc:  # noqa: BLE001
            return ProfileResult(
                source_url,
                resolved_page,
                debug=debug + [f"异常详情：{exc}"],
                error_code="BROWSER-CONNECT",
            )

        expression = """
            JSON.stringify({
              title: document.querySelector('meta[property="og:title"]')?.content || document.title || '',
              url: location.href,
              gridLinks: Array.from(document.querySelectorAll(
                '[data-e2e="user-post-list"] a[href], [data-e2e="user-post-item"] a[href], ' +
                '[class*="user-post"] a[href], #user-post-list a[href]'
              )).map(x => x.href || '').filter(x => /\\/(?:video|note|slides)\\/\\d+/.test(x)),
              allLinks: Array.from(document.querySelectorAll('a[href]'))
                .map(x => x.href || '')
                .filter(x => /\\/(?:video|note|slides)\\/\\d+/.test(x)),
              ssrWorks: (() => {
                const out = [];
                const seen = new Set();
                function firstAddr(node) {
                  if (!node || typeof node !== 'object') return null;
                  const list = node.url_list || node.urlList;
                  if (Array.isArray(list)) {
                    const url = list.find(u => typeof u === 'string' && u.startsWith('http'));
                    return url || null;
                  }
                  return null;
                }
                function keep(aweme) {
                  if (!aweme || typeof aweme !== 'object') return;
                  const id = aweme.aweme_id || aweme.awemeId || '';
                  if (!id || seen.has(id)) return;
                  seen.add(id);
                  const share = aweme.share_info || aweme.shareInfo || {};
                  const video = aweme.video && typeof aweme.video === 'object' ? aweme.video : null;
                  out.push({
                    aweme_id: id,
                    desc: aweme.desc || share.share_desc || '',
                    share_url: share.share_url || share.shareUrl || '',
                    video_url: video ? firstAddr(video.play_addr || video.playAddr) : null,
                    video_cover: video
                      ? firstAddr(video.cover || video.origin_cover || video.originCover || video.dynamic_cover)
                      : null,
                    image_count: Array.isArray(aweme.images) ? aweme.images.length : 0,
                    nickname: ((aweme.author || aweme.author_info || {}).nickname || ''),
                  });
                }
                function walk(node) {
                  if (!node || typeof node !== 'object') return;
                  if (Array.isArray(node)) { node.forEach(walk); return; }
                  for (const key of ['aweme_list', 'awemeList', 'post_list', 'postList']) {
                    const list = node[key];
                    if (Array.isArray(list)) list.forEach(keep);
                  }
                  for (const key in node) { try { walk(node[key]); } catch (_) {} }
                }
                for (const root of [window._ROUTER_DATA, window.__INITIAL_STATE__]) walk(root);
                try { walk(JSON.parse(decodeURIComponent(window.RENDER_DATA || ''))); } catch (_) {}
                return out;
              })(),
              bodyLength: (document.body?.innerText || '').length,
              unavailable: ['用户不存在', '该用户不存在', '账号已注销', '主页不存在', '暂无作品']
                .find(x => (document.body?.innerText || '').includes(x)) || '',
              challenge: (document.body?.innerText || '').trim().length < 20 &&
                Array.from(document.scripts).some(x => (x.textContent || '').includes('_$jsvmprt')),
              profileResponses: (() => {
                const values = window.__shortVideoPickerProfileResponses;
                return Array.isArray(values) ? values.splice(0, values.length) : [];
              })()
            })
        """
        page: dict[str, Any] = {}
        work_urls: list[str] = []
        seen_urls: set[str] = set()
        challenge_seen = False
        reloaded = False
        stalled_rounds = 0
        last_count = 0
        pending_profile_responses: dict[str, str] = {}
        work_payloads: dict[str, dict[str, Any]] = {}
        started = time.monotonic()
        try:
            client.call("Page.enable")
            client.call("Network.enable")
            capture_script = r"""
                (() => {
                  const key = '__shortVideoPickerProfileResponses';
                  const store = [];
                  Object.defineProperty(window, key, {value: store, configurable: false});
                  const keep = text => {
                    if (typeof text !== 'string' || !text) return;
                    store.push(text);
                    if (store.length > 8) store.shift();
                  };
                  const wanted = url =>
                    String(url || '').includes('/aweme/v1/web/aweme/post/');
                  const originalFetch = window.fetch;
                  if (typeof originalFetch === 'function') {
                    window.fetch = async function(...args) {
                      const response = await originalFetch.apply(this, args);
                      try {
                        const url = response.url || args[0];
                        if (wanted(url)) {
                          response.clone().text().then(keep).catch(() => {});
                        }
                      } catch (_) {}
                      return response;
                    };
                  }
                  const originalOpen = XMLHttpRequest.prototype.open;
                  const originalSend = XMLHttpRequest.prototype.send;
                  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this.__svpProfileUrl = String(url || '');
                    return originalOpen.call(this, method, url, ...rest);
                  };
                  XMLHttpRequest.prototype.send = function(...args) {
                    if (wanted(this.__svpProfileUrl)) {
                      this.addEventListener('load', () => {
                        try {
                          const value = typeof this.response === 'string'
                            ? this.response
                            : JSON.stringify(this.response);
                          keep(value);
                        } catch (_) {}
                      }, {once: true});
                    }
                    return originalSend.apply(this, args);
                  };
                })();
            """
            try:
                client.call(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": capture_script},
                    timeout=8,
                )
            except (RuntimeError, TimeoutError):
                debug.append("主页列表响应内存读取不可用，已保留页面网格回退。")
            try:
                client.call("Page.navigate", {"url": navigation_url}, timeout=20)
            except TimeoutError:
                debug.append("主页导航响应较慢，已继续等待实际页面内容。")
            time.sleep(3)
            while time.monotonic() - started < DOUYIN_PROFILE_TIMEOUT:
                result = client.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout=15,
                )
                raw = result.get("result", {}).get("value", "{}")
                page = json.loads(raw) if isinstance(raw, str) else raw
                challenge_seen = challenge_seen or bool(page.get("challenge"))
                for raw_payload in page.get("profileResponses") or []:
                    try:
                        payload = json.loads(raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    _merge_profile_payload(payload, work_payloads)
                _collect_profile_response_payloads(
                    client,
                    pending_profile_responses,
                    work_payloads,
                )
                # Server-rendered profile data belongs to the opened account and
                # survives the security check that often blocks the aweme/post
                # XHR. It is a first-class source, not a DOM fallback.
                _merge_ssr_works(page.get("ssrWorks") or [], work_payloads)
                # The public profile endpoint returns aweme_list in newest-first
                # order. Prefer those canonical IDs, then the visible anchors of
                # the account's own work grid. Page-wide anchors include
                # recommended videos from other accounts and must never be
                # treated as this profile's works.
                for url in work_payloads:
                    if url not in seen_urls:
                        seen_urls.add(url)
                        work_urls.append(url)
                        if len(work_urls) >= limit:
                            break
                for url in page.get("gridLinks") or []:
                    canonical = _canonical_douyin_work_url(str(url))
                    if canonical and canonical not in seen_urls:
                        seen_urls.add(canonical)
                        work_urls.append(canonical)
                        if len(work_urls) >= limit:
                            break
                if len(work_urls) >= limit or page.get("unavailable"):
                    break
                if len(work_urls) == last_count:
                    stalled_rounds += 1
                else:
                    stalled_rounds = 0
                    last_count = len(work_urls)
                # Four stable reads after content appears means the public grid
                # has no more immediately accessible works. Do not wait out the
                # full timeout for a small account.
                if work_urls and stalled_rounds >= 4:
                    break
                if not reloaded and time.monotonic() - started >= DOUYIN_CHALLENGE_RELOAD_AFTER:
                    try:
                        client.call("Page.reload", {"ignoreCache": True}, timeout=20)
                    except TimeoutError:
                        debug.append("主页刷新响应较慢，已继续检查公开作品。")
                    reloaded = True
                    if challenge_seen:
                        debug.append("用户主页仍在安全校验，已自动刷新并继续等待。")
                else:
                    client.call(
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "window.scrollTo(0, Math.max(document.body.scrollHeight,"
                                "document.documentElement.scrollHeight)); true"
                            ),
                            "returnByValue": True,
                        },
                        timeout=8,
                    )
                time.sleep(1)
            # DOM anchors can reach the requested limit before the profile XHR
            # finishes loading. Drain a few final CDP event rounds so the
            # already-requested aweme_list body is not lost at that boundary.
            for _ in range(3):
                if not work_urls or len(work_payloads) >= len(work_urls):
                    break
                time.sleep(0.25)
                client.call(
                    "Runtime.evaluate",
                    {"expression": "true", "returnByValue": True},
                    timeout=8,
                )
                _collect_profile_response_payloads(
                    client,
                    pending_profile_responses,
                    work_payloads,
                )
        except Exception as exc:  # noqa: BLE001
            return ProfileResult(
                source_url,
                resolved_page,
                title=_profile_title(page.get("title", "")),
                work_urls=work_urls[:limit],
                debug=debug + [f"异常详情：{exc}"],
                error_code="BROWSER-READ",
            )
        finally:
            client.close()

        current_url = str(page.get("url") or resolved_page)
        title = _profile_title(page.get("title", ""))
        page_matches = _profile_key(current_url) == _profile_key(navigation_url)
        if work_urls and page_matches:
            if challenge_seen:
                debug.append("用户主页公开安全校验已完成。")
            payload_count = sum(1 for url in work_urls[:limit] if url in work_payloads)
            if payload_count:
                debug.append(
                    f"已从主页接口/服务端数据读取 {payload_count} 个作品的公开媒体数据。"
                )
            debug.append(f"主页公开网格已找到 {len(work_urls[:limit])} 个作品。")
            selected_payloads = {
                url: work_payloads[url]
                for url in work_urls[:limit]
                if url in work_payloads
            }
            return ProfileResult(
                source_url,
                current_url,
                title,
                work_urls[:limit],
                debug,
                work_payloads=selected_payloads,
            )
        diagnostic = (
            f"主页诊断：URL匹配={int(page_matches)}，不可用提示={page.get('unavailable') or '无'}，"
            f"公开作品链接={len(work_urls)}（作品网格={len(page.get('gridLinks') or [])}，"
            f"页面全局链接={len(page.get('allLinks') or [])}，"
            f"接口/服务端数据={len(work_payloads)}）。"
        )
        if page.get("unavailable"):
            code = "DY-PROFILE-UNAVAILABLE"
        elif challenge_seen:
            code = "DY-SECURITY-CHECK"
        elif not page_matches:
            code = "DY-PROFILE-PAGE-MISMATCH"
        else:
            code = "DY-PROFILE-NO-WORKS"
        return ProfileResult(
            source_url,
            current_url,
            title,
            work_urls[:limit],
            debug + [diagnostic],
            code,
            {
                url: work_payloads[url]
                for url in work_urls[:limit]
                if url in work_payloads
            },
        )

    def parse_douyin(self, source_url: str, page_url: str = "") -> ParseResult:
        debug = ["已使用未登录的公开页面解析助手。"]
        expected_id_match = re.search(r"/(?:note|video|slides)/(\d+)", page_url or source_url)
        expected_id = expected_id_match.group(1) if expected_id_match else ""
        expects_video = _is_video_page_url(page_url or source_url)
        navigation_url = _canonical_douyin_url(page_url or source_url)
        if navigation_url != (page_url or source_url):
            debug.append("已移除分享追踪参数，使用作品规范地址读取。")
        try:
            client = self._page_client()
        except Exception as exc:  # noqa: BLE001 - surfaced as a local parsing message.
            return make_parse_error(source_url, page_url or source_url, "BROWSER-CONNECT", debug=debug, detail=str(exc))
        try:
            client.call("Page.enable")
            client.call("Network.enable")
            try:
                client.call("Page.navigate", {"url": navigation_url}, timeout=20)
            except TimeoutError:
                # Chromium can continue and complete navigation even when the
                # command acknowledgement is delayed by a busy public page.
                debug.append("页面导航响应较慢，已继续等待实际页面内容。")
            # Let navigation commit before inspecting the reusable target. An
            # immediate query could otherwise observe the previous work.
            time.sleep(3)
            expected_js = json.dumps(expected_id)
            expression = """
                JSON.stringify({
                  title: document.title,
                  url: location.href,
                  images: Array.from(document.images).map(x => x.currentSrc || x.src).filter(Boolean),
                  videos: Array.from(document.querySelectorAll('video, video source')).map(x => x.currentSrc || x.src).filter(Boolean),
                  network: performance.getEntriesByType('resource').map(x => x.name)
                    .filter(x => x.includes('douyinvod.com')),
                  embedded: Array.from(document.scripts)
                    .map(x => x.textContent || '')
                    .filter(x => x.includes(__EXPECTED_ID__) && (x.includes('awemeId') || x.includes('aweme_id'))),
                  readyState: document.readyState,
                  bodyLength: (document.body?.innerText || '').length,
                  unavailable: ['你要观看的图文不存在', '你要观看的视频不存在', '作品不存在', '作品已删除', '暂无权限观看']
                    .find(x => (document.body?.innerText || '').includes(x)) || '',
                  challenge: (document.body?.innerText || '').trim().length < 20 &&
                    Array.from(document.scripts).some(x => (x.textContent || '').includes('_$jsvmprt'))
                })
            """.replace("__EXPECTED_ID__", expected_js)
            page: dict[str, Any] = {}
            challenge_seen = False
            reloaded = False
            started = time.monotonic()
            deadline = started + DOUYIN_PAGE_TIMEOUT
            evaluate_timeouts = 0
            while time.monotonic() < deadline:
                try:
                    result = client.call(
                        "Runtime.evaluate",
                        {"expression": expression, "returnByValue": True},
                        timeout=15,
                    )
                except TimeoutError:
                    evaluate_timeouts += 1
                    if evaluate_timeouts == 1:
                        debug.append("页面脚本响应较慢，已继续等待。")
                    time.sleep(0.5)
                    continue
                raw = result.get("result", {}).get("value", "{}")
                page = json.loads(raw) if isinstance(raw, str) else raw
                embedded_detail = _extract_pace_detail(page.get("embedded", []), expected_id)
                embedded_mentions_target = any(expected_id in chunk for chunk in page.get("embedded", [])) if expected_id else False
                page_matches = not expected_id or expected_id in str(page.get("url", "")) or embedded_mentions_target
                network_videos = [
                    url for url in page.get("network", [])
                    if _is_detail_video(url) and _media_matches_item(url, expected_id)
                ]
                visible_images = any(_is_detail_image(url) for url in page.get("images", []))
                visible_videos = any(
                    _is_detail_video(url) and _media_matches_item(url, expected_id)
                    for url in page.get("videos", [])
                ) or bool(network_videos)
                # A /video/ page paints its poster before the player requests
                # the MP4. Do not stop on that poster and misclassify it as a
                # standalone image resource.
                dom_media = page_matches and (visible_videos if expects_video else (visible_images or visible_videos))
                challenge_seen = challenge_seen or bool(page.get("challenge"))
                if page.get("unavailable") or embedded_detail or dom_media:
                    break
                if not reloaded and time.monotonic() - started >= DOUYIN_CHALLENGE_RELOAD_AFTER:
                    try:
                        client.call("Page.reload", {"ignoreCache": True}, timeout=20)
                    except TimeoutError:
                        debug.append("页面刷新响应较慢，已继续检查页面内容。")
                    reloaded = True
                    if challenge_seen:
                        debug.append("公开页面仍在安全校验，已自动刷新并继续等待。")
                    else:
                        debug.append("页面尚未返回目标作品，已自动刷新并继续等待。")
                time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            self._page_websocket_url = ""
            self._page_target_id = ""
            return make_parse_error(source_url, page_url or source_url, "BROWSER-READ", debug=debug, detail=str(exc))
        finally:
            client.close()

        resolved_page = page_url or source_url
        embedded_detail = _extract_pace_detail(page.get("embedded", []), expected_id)
        title = str((embedded_detail or {}).get("desc") or page.get("title", "")).replace(" - 抖音", "").strip()
        author = extract_aweme_author(embedded_detail or {})
        image_urls = [url for url in page.get("images", []) if _is_detail_image(url)]
        dom_video_urls = [
            url for url in (page.get("videos") or [])
            if _is_detail_video(url) and _media_matches_item(url, expected_id)
        ]
        network_video_urls = [
            url for url in (page.get("network") or [])
            if _is_detail_video(url) and _media_matches_item(url, expected_id)
        ]
        # The player may request the same work repeatedly with different short-
        # lived signatures or bitrates. DOM playback URLs are authoritative;
        # otherwise keep only the best network rendition for this visible work.
        # A single player may expose <video> plus several <source> renditions.
        # They are alternate encodes of the same work, not separate resources.
        video_urls = _best_network_video(dom_video_urls) or _best_network_video(network_video_urls)
        items: list[MediaItem] = []
        if embedded_detail:
            items.extend(_items_from_pace_detail(source_url, resolved_page, title, embedded_detail))
            if expects_video:
                items = [item for item in items if item.kind == "video"]
        # Embedded detail is authoritative and already contains every resource
        # for this work. DOM nodes often repeat its Live covers and playback
        # URLs, so consult them only when embedded detail is unavailable.
        if not items:
            fallback_urls = video_urls if expects_video else _unique(image_urls + video_urls)
            poster_url = image_urls[0] if expects_video and image_urls else ""
            for url in fallback_urls:
                kind = "image" if url in image_urls else "video"
                items.append(
                    MediaItem(
                        source_url=source_url,
                        page_url=resolved_page,
                        media_url=url,
                        kind=kind,
                        title=title,
                        suggested_name=suggest_name(
                            title, url, kind, len(items) + 1, source_url=resolved_page or source_url
                        ),
                        preview_url=url if kind == "image" else poster_url,
                    )
                )
        if not items:
            current_url = str(page.get("url") or "")
            page_matches = not expected_id or expected_id in current_url or any(
                expected_id in chunk for chunk in page.get("embedded", [])
            )
            diagnostic = (
                f"页面诊断：URL匹配={int(page_matches)}，不可用提示={page.get('unavailable') or '无'}，"
                f"图片候选={len(image_urls)}，视频节点={len(dom_video_urls)}，"
                f"网络视频={len(network_video_urls)}，ID相关脚本={len(page.get('embedded', []))}，"
                f"文档状态={page.get('readyState') or '未知'}。"
            )
            if page.get("unavailable"):
                code = "DY-WORK-UNAVAILABLE"
                reason = f"抖音页面提示：{page['unavailable']}。"
            elif challenge_seen:
                code = "DY-SECURITY-CHECK"
                reason = "公开页安全校验在等待时限内未完成。"
            elif not page_matches:
                code = "DY-PAGE-MISMATCH"
                reason = f"页面最终地址未指向目标作品：{current_url or '未知'}。"
            elif not embedded_detail:
                code = "DY-PAGE-SHELL"
                reason = "目标页面已完成打开，但抖音没有向当前匿名会话下发作品详情数据。"
            else:
                code = "DY-NO-MEDIA"
                reason = "已读取目标作品详情数据，但其中没有可保存的图片或视频流。"
            result = make_parse_error(
                source_url,
                resolved_page,
                code,
                title=title,
                status="empty",
                debug=debug + [diagnostic, reason],
            )
            result.author = author
            return result
        if challenge_seen:
            debug.append("公开页安全校验已完成。")
        debug.append(f"已找到当前作品资源：{len(items)} 个。")
        is_story = _is_story_detail(embedded_detail or {})
        return ParseResult(
            source_url,
            resolved_page,
            title,
            items,
            debug=debug,
            content_type="story" if is_story else "",
            content_label="限时日常" if is_story else "",
            author=author,
        )


def parse_douyin_share_fallback(source_url: str, page_url: str = "") -> ParseResult | None:
    """Read the same work from Douyin's official public mobile share page.

    The desktop work page occasionally returns only a hydrated application
    shell to a fresh anonymous browser: the target URL is correct, but no work
    detail request or media request is ever made.  The mobile public share page
    independently embeds the server-rendered ``item_list`` for that same public
    work, so it is a safe fallback that needs no login, copied browser profile,
    private API signature, or third-party parsing service.
    """
    resolved_page = page_url or source_url
    # A short/share link can resolve to a generic landing route even though
    # the original official share URL still contains the exact work ID. Check
    # both addresses instead of discarding that usable source identity.
    match = None
    for candidate in (resolved_page, source_url):
        candidate_canonical = _canonical_douyin_url(candidate)
        candidate_match = re.search(
            r"/(video|note|slides)/(\d+)",
            urlparse(candidate_canonical).path,
        )
        if candidate_match:
            match = candidate_match
            break
    if not match:
        return None
    page_kind, expected_id = match.groups()
    # Current mobile share routing calls image posts "note". Keep the original
    # route as a second compatibility option for a future slides-specific page.
    share_kinds = ["video"] if page_kind == "video" else ["note", page_kind]
    share_kinds = list(dict.fromkeys(share_kinds))
    source_parts = urlparse(source_url)
    source_is_official_share = (
        (source_parts.hostname or "").lower() in {"iesdouyin.com", "www.iesdouyin.com"}
        and bool(re.search(r"/share/(?:video|note|slides)/\d+", source_parts.path))
    )
    attempts_per_route = 2 if source_is_official_share else 1
    for share_kind in share_kinds:
        share_url = f"https://www.iesdouyin.com/share/{share_kind}/{expected_id}/"
        for attempt in range(attempts_per_route):
            if attempt:
                # Retry only a missing/transient public response. Successful
                # batch items pay no delay, while a brief rate limit no longer
                # falls straight through to the heavier Edge renderer.
                time.sleep(DOUYIN_SHARE_RETRY_DELAY)
            request = Request(
                share_url,
                headers={
                    "User-Agent": DOUYIN_MOBILE_SHARE_UA,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://www.douyin.com/",
                },
            )
            try:
                with urlopen(request, timeout=DOUYIN_SHARE_TIMEOUT) as response:
                    html = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
            except (OSError, TimeoutError, ValueError):
                continue
            marker = "window._ROUTER_DATA"
            marker_index = html.find(marker)
            if marker_index < 0:
                continue
            start = html.find("{", marker_index)
            blob = balanced_object(html, start) if start >= 0 else ""
            if not blob:
                continue
            try:
                router_data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            detail = _find_aweme_detail(router_data, expected_id)
            if not detail:
                filter_entry = _share_filter_entry(router_data, expected_id)
                filter_reason = _share_filter_reason(router_data, expected_id)
                if filter_reason:
                    if "story_25" in filter_reason:
                        # This is an internal web-share filter rule, not evidence
                        # of a 25-hour expiry. The same public work can remain
                        # playable inside a logged-in Douyin app while every
                        # official anonymous web entry says “请尝试在抖音内观看”.
                        code = "DY-STORY-WEB-FILTERED"
                    elif any(marker in filter_reason for marker in ("expired", "expire", "timeout", "ttl")):
                        code = "DY-STORY-EXPIRED"
                    elif any(
                        marker in filter_reason
                        for marker in ("friend", "private", "close", "permission", "not_public", "visible")
                    ):
                        code = "DY-STORY-NOT-PUBLIC"
                    elif "story" in filter_reason:
                        code = "DY-STORY-UNAVAILABLE"
                    elif any(
                        marker in filter_reason
                        for marker in (
                            "self_see",
                            "only_self",
                            "private",
                            "permission",
                            "friend_see",
                            "part_see",
                            "not_public",
                            "visible_self",
                        )
                    ):
                        code = "DY-WORK-NOT-PUBLIC"
                    else:
                        # An exact filter-list entry is authoritative even when
                        # Douyin introduces a new reason token. Returning a
                        # work-level unavailable error is more accurate than
                        # opening Edge twice and calling it an empty page shell.
                        code = "DY-WORK-UNAVAILABLE"
                    diagnostics = [f"抖音官方公开分享页过滤原因：{filter_reason}。"]
                    if filter_entry:
                        official_message = "；".join(
                            str(filter_entry.get(key) or "").strip()
                            for key in ("notice", "detail_msg", "detailMsg")
                            if str(filter_entry.get(key) or "").strip()
                        )
                        if official_message:
                            diagnostics.append(f"抖音公开页提示：{official_message}。")
                    result = make_parse_error(
                        source_url,
                        resolved_page,
                        code,
                        status="empty",
                        debug=diagnostics,
                    )
                    if code.startswith("DY-STORY-"):
                        result.content_type = "story"
                        result.content_label = "限时日常"
                    return result
                continue
            title = str(detail.get("desc") or "").strip()
            items = _items_from_pace_detail(source_url, resolved_page, title, detail)
            if page_kind == "video":
                items = [item for item in items if item.kind == "video"]
            if not items:
                continue
            for item in items:
                item.headers = {
                    "User-Agent": DOUYIN_MOBILE_SHARE_UA,
                    "Referer": share_url,
                    "Accept": "*/*",
                }
            is_story = _is_story_detail(detail)
            return ParseResult(
                source_url,
                resolved_page,
                title,
                items,
                debug=["已从抖音官方公开分享页读取作品数据，完成无登录兜底解析。"],
                content_type="story" if is_story else "",
                content_label="限时日常" if is_story else "",
                author=extract_aweme_author(detail),
            )
    return None


def _find_aweme_detail(value: Any, expected_id: str) -> dict[str, Any] | None:
    """Return the exact work node from either camelCase or snake_case JSON."""
    if isinstance(value, dict):
        item_id = str(value.get("awemeId") or value.get("aweme_id") or "")
        if item_id == expected_id and (isinstance(value.get("video"), dict) or isinstance(value.get("images"), list)):
            return value
        detail = value.get("detail")
        if isinstance(detail, dict):
            item_id = str(detail.get("awemeId") or detail.get("aweme_id") or "")
            if item_id == expected_id and (isinstance(detail.get("video"), dict) or isinstance(detail.get("images"), list)):
                return detail
        for child in value.values():
            found = _find_aweme_detail(child, expected_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_aweme_detail(child, expected_id)
            if found:
                return found
    return None


def _share_filter_entry(value: Any, expected_id: str) -> dict[str, Any] | None:
    """Return the official share-page filter entry for this exact work."""
    if isinstance(value, dict):
        filters = value.get("filter_list") or value.get("filterList")
        if isinstance(filters, list):
            for entry in filters:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("aweme_id") or entry.get("awemeId") or "")
                if item_id == expected_id:
                    return entry
        for child in value.values():
            entry = _share_filter_entry(child, expected_id)
            if entry:
                return entry
    elif isinstance(value, list):
        for child in value:
            entry = _share_filter_entry(child, expected_id)
            if entry:
                return entry
    return None


def _share_filter_reason(value: Any, expected_id: str) -> str:
    """Return the normalized official filter reason for this exact work."""
    entry = _share_filter_entry(value, expected_id)
    if entry:
        return str(entry.get("filter_reason") or entry.get("filterReason") or "").strip().lower()
    return ""


def _is_story_detail(detail: dict[str, Any]) -> bool:
    """Recognise a currently public Douyin Story/“日常” work payload."""
    for key in ("is_story", "isStory", "is_24_story", "is24Story"):
        value = detail.get(key)
        if value is True or str(value).lower() in {"1", "true"}:
            return True
    for key in ("story_ttl", "storyTtl", "story_source_type", "storySourceType"):
        value = detail.get(key)
        try:
            if value is not None and int(value) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return any(detail.get(key) for key in ("story_data", "storyData", "story_metadata", "storyMetadata"))


def _extract_pace_detail(chunks: list[str], expected_id: str) -> dict[str, Any] | None:
    """Decode the public React-flight payload and return this work's detail."""

    for raw in chunks:
        match = re.search(r"self\.__pace_f\.push\(\[1,(.*)\]\)\s*$", raw, re.DOTALL)
        if not match:
            continue
        try:
            decoded = json.loads(match.group(1))
            payload = decoded.split(":", 1)[1]
            found = _find_aweme_detail(json.loads(payload), expected_id)
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if found:
            return found
    return None


def _first_media_url(value: Any) -> str:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        for key in ("src", "url", "mainUrl"):
            url = value.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return url
        for key in ("urlList", "url_list"):
            url = _first_media_url(value.get(key))
            if url:
                return url
    if isinstance(value, list):
        preferred = [item for item in value if isinstance(item, str) and (".jpeg" in item or ".jpg" in item)]
        for item in preferred + value:
            url = _first_media_url(item)
            if url:
                return url
    return ""


def _items_from_pace_detail(
    source_url: str, page_url: str, title: str, detail: dict[str, Any]
) -> list[MediaItem]:
    resources: list[tuple[str, str, str, bool, str, list[dict[str, Any]]]] = []
    images = [image for image in (detail.get("images") or []) if isinstance(image, dict)]
    video = detail.get("video")
    top_video_url = ""
    top_video_preview = ""
    if isinstance(video, dict):
        top_video_url = first_video_media_url(video)
        top_video_preview = _video_cover_url(video, detail)

    # A slides post also exposes an aggregate player video. It duplicates the
    # per-image Live resources and is not a separate user-selectable item.
    # Conversely, for a normal video the top-level player is authoritative.
    # Some payloads also repeat its motion stream inside an image-shaped node;
    # treating that node as a Live photo produced the false second video.
    is_image_post = is_aweme_image_post(detail, top_video_url)
    if top_video_url and not is_image_post:
        options = extract_video_quality_options(video) if isinstance(video, dict) else []
        selected_url = str(options[0]["url"]) if options else top_video_url
        resources.append(("video", selected_url, top_video_preview, True, "", options))
    else:
        for image in images:
            image_url = _first_media_url(image.get("urlList") or image.get("url_list"))
            video_url = ""
            for video_key in LIVE_VIDEO_KEYS:
                video_url = first_video_media_url(image.get(video_key))
                if video_url:
                    break
            # The image in this same record is the authoritative poster for
            # its Live MP4. Keep it as preview metadata rather than exposing a
            # second saveable resource or relying on ffmpeg to find a frame.
            if video_url:
                resources.append(("video", video_url, image_url, True, "Live 图动态视频", []))
            elif image_url:
                resources.append(("image", image_url, image_url, True, "", []))

    items: list[MediaItem] = []
    for kind, url, preview_url, selected, relationship_note, quality_options in resources:
        if any(item.media_url == url for item in items):
            continue
        items.append(
            MediaItem(
                source_url=source_url,
                page_url=page_url,
                media_url=url,
                kind=kind,
                title=title,
                suggested_name=suggest_name(title, url, kind, len(items) + 1, source_url=page_url or source_url),
                preview_url=preview_url,
                selected=selected,
                relationship_note=relationship_note,
                quality_options=quality_options,
                quality_label=str(quality_options[0]["label"]) if quality_options else "",
            )
        )
    return items


def _video_cover_url(video: dict[str, Any], detail: dict[str, Any]) -> str:
    """Return the public poster attached to a normal video work."""
    keys = (
        "originCover", "origin_cover", "cover", "poster",
        "dynamicCover", "dynamic_cover", "animatedCover", "animated_cover",
    )
    for container in (video, detail):
        for key in keys:
            url = _first_media_url(container.get(key))
            if url:
                return url
    return ""


def _is_detail_image(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and "douyinpic.com" in parsed.netloc and "AWEME_DETAIL" in unquote(parsed.query)


def _is_video_page_url(url: str) -> bool:
    return bool(re.search(r"/video/\d+", urlparse(url).path))


def _is_douyin_profile_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return hostname.endswith("douyin.com") and bool(re.search(r"/(?:share/)?user/[^/]+", parsed.path))


def _profile_key(url: str) -> str:
    parsed = urlparse(url)
    match = re.search(r"/(?:share/)?user/([^/?#]+)", parsed.path)
    return unquote(match.group(1)) if match else ""


def _canonical_douyin_profile_url(url: str) -> str:
    parsed = urlparse(url)
    if not _is_douyin_profile_url(url):
        return url
    return parsed._replace(query="", fragment="").geturl()


def _canonical_douyin_work_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    match = re.search(r"/(video|note|slides)/(\d+)", parsed.path)
    if not hostname.endswith("douyin.com") or not match:
        return ""
    kind, item_id = match.groups()
    return f"https://www.douyin.com/{kind}/{item_id}"


def _profile_work_url(aweme: dict[str, Any]) -> str:
    """Build a canonical work URL from one public profile-list entry."""
    share_info = aweme.get("share_info") or aweme.get("shareInfo") or {}
    if isinstance(share_info, dict):
        for key in ("share_url", "shareUrl"):
            canonical = _canonical_douyin_work_url(str(share_info.get(key) or ""))
            if canonical:
                return canonical
    item_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
    if not re.fullmatch(r"\d{10,24}", item_id):
        return ""
    images = aweme.get("images")
    kind = "note" if isinstance(images, list) and images else "video"
    return f"https://www.douyin.com/{kind}/{item_id}"


def _profile_aweme_entries(value: Any) -> list[dict[str, Any]]:
    """Find and de-duplicate aweme_list entries in a profile response."""
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("aweme_list", "awemeList"):
                entries = node.get(key)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    item_id = str(entry.get("aweme_id") or entry.get("awemeId") or "")
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        output.append(entry)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return output


def _merge_profile_payload(
    payload: Any,
    work_payloads: dict[str, dict[str, Any]],
) -> None:
    for aweme in _profile_aweme_entries(payload):
        work_url = _profile_work_url(aweme)
        if work_url:
            work_payloads.setdefault(work_url, aweme)


def _merge_ssr_works(
    works: list[dict[str, Any]],
    work_payloads: dict[str, dict[str, Any]],
) -> None:
    """Merge the server-rendered work list of the opened profile page.

    The SSR list is rendered for the account whose page is open, so it remains
    reliable when the aweme/post XHR is blocked by the anonymous security
    check. Entries carry only the fields the front-end needs; richer detail
    pages are still parsed per work when media addresses are missing.
    """
    for work in works:
        if not isinstance(work, dict):
            continue
        item_id = str(work.get("aweme_id") or "")
        if not item_id:
            continue
        desc = str(work.get("desc") or "")
        video_url = str(work.get("video_url") or "")
        cover = str(work.get("video_cover") or "")
        aweme: dict[str, Any] = {
            "aweme_id": item_id,
            "desc": desc,
            "share_info": {
                "share_url": str(work.get("share_url") or ""),
                "share_desc": desc,
            },
            "author": {"nickname": str(work.get("nickname") or "")},
            "images": [],
        }
        if video_url:
            aweme["video"] = {
                "play_addr": {"url_list": [video_url]},
                "cover": {"url_list": [cover]} if cover else {},
            }
        _merge_profile_payload({"aweme_list": [aweme]}, work_payloads)


def _collect_profile_response_payloads(
    client: _CDP,
    pending: dict[str, str],
    work_payloads: dict[str, dict[str, Any]],
) -> None:
    """Read only JSON responses already requested by the visible profile page."""
    for event in client.take_events():
        if event.get("method") != "Network.responseReceived":
            continue
        params = event.get("params") or {}
        response = params.get("response") or {}
        url = str(response.get("url") or "")
        mime_type = str(response.get("mimeType") or "").lower()
        if not any(
            marker in url
            for marker in (
                "/aweme/v1/web/aweme/post/",
                "/web/api/v2/aweme/post/",
                "/douyin/user/posts",
            )
        ):
            continue
        if mime_type and "json" not in mime_type:
            continue
        request_id = str(params.get("requestId") or "")
        if request_id:
            pending.setdefault(request_id, url)

    for request_id in list(pending):
        try:
            response_body = client.call(
                "Network.getResponseBody",
                {"requestId": request_id},
                timeout=4,
            )
        except (RuntimeError, TimeoutError):
            # responseReceived can precede loadingFinished. Keep it pending and
            # try again on the next one-second profile polling round.
            continue
        raw_body = response_body.get("body") or ""
        if not raw_body:
            # Chromium may acknowledge getResponseBody just before the XHR
            # finishes. Keep this request pending for the next drain round.
            continue
        pending.pop(request_id, None)
        if response_body.get("base64Encoded"):
            try:
                raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
            except (ValueError, UnicodeError):
                continue
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            continue
        _merge_profile_payload(payload, work_payloads)


def _profile_title(value: Any) -> str:
    title = str(value or "").strip()
    title = re.sub(r"\s*[-_|]\s*抖音.*$", "", title).strip()
    title = re.sub(r"的(?:主页|抖音)$", "", title).strip()
    return title or "抖音用户主页"


def _canonical_douyin_url(url: str) -> str:
    """Remove share tracking parameters that can stall public-page hydration."""
    parsed = urlparse(url)
    if "douyin.com" not in parsed.netloc or not re.search(r"/(?:note|video|slides)/\d+", parsed.path):
        return url
    return parsed._replace(query="", fragment="").geturl()


def _is_detail_video(url: str) -> bool:
    parsed = urlparse(url)
    decoded_path = unquote(parsed.path).lower()
    decoded_query = unquote(parsed.query).lower()
    return (
        parsed.scheme in ("http", "https")
        and "douyinvod.com" in parsed.netloc
        and "video" in decoded_query
        and "media-audio" not in decoded_path
    )


def _media_matches_item(url: str, expected_id: str) -> bool:
    """Reject a related/recommended work when a media URL identifies its item."""
    if not expected_id:
        return True
    query = parse_qs(urlparse(url).query)
    identifiers = query.get("__vid", []) + query.get("aweme_id", []) + query.get("item_id", [])
    return not identifiers or expected_id in identifiers


def _best_network_video(urls: list[str]) -> list[str]:
    candidates = _unique(urls)
    if not candidates:
        return []

    def bitrate(url: str) -> int:
        try:
            return int((parse_qs(urlparse(url).query).get("br") or ["0"])[0])
        except (TypeError, ValueError):
            return 0

    return [max(candidates, key=bitrate)]


def _fetch_bytes(url: str, timeout: int = 14) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.douyin.com/",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _image_preview_and_hash(url: str) -> tuple[str, int | None]:
    """Download a public poster and embed a small stable copy for WebKit."""
    data = _fetch_bytes(url)
    try:
        frame_hash = _image_hash(data)
    except Exception:
        frame_hash = None

    preview_data = data
    mime = "image/jpeg"
    try:
        from PIL import Image

        image = Image.open(BytesIO(data)).convert("RGB")
        image.thumbnail((640, 720))
        output = BytesIO()
        image.save(output, format="JPEG", quality=82, optimize=True)
        preview_data = output.getvalue()
    except Exception:
        if data.startswith(b"\x89PNG"):
            mime = "image/png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        elif data[4:12] in (b"ftypavif", b"ftypavis"):
            mime = "image/avif"
    suffix = {"image/png": ".png", "image/webp": ".webp", "image/avif": ".avif"}.get(mime, ".jpg")
    preview = cache_preview(preview_data, suffix)
    return preview, frame_hash


def _image_hash(data: bytes) -> int:
    """Create a dHash-style fingerprint, with an ffmpeg fallback for Pillow."""
    try:
        from PIL import Image

        image = Image.open(BytesIO(data)).convert("L").resize((17, 16))
        pixels = list(image.getdata())
    except Exception:  # Finder may start a Python runtime without Pillow.
        pixels = _ffmpeg_grayscale_pixels(data)
    result = 0
    for row in range(16):
        offset = row * 17
        for column in range(16):
            result = (result << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    return result


def _ffmpeg_grayscale_pixels(data: bytes) -> list[int]:
    """Decode a still with ffmpeg when the optional Pillow package is absent."""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("缺少用于生成缩略图的本地组件。")
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".image", delete=False) as temp:
            temp.write(data)
            temp_path = temp.name
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", temp_path, "-frames:v", "1",
             "-vf", "scale=17:16,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
            capture_output=True,
            timeout=15,
        )
        if completed.returncode != 0 or len(completed.stdout) < 272:
            raise RuntimeError("无法读取图片用于重复识别。")
        return list(completed.stdout[:272])
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _video_preview_and_hash(url: str) -> tuple[str, int | None]:
    """Extract a lightweight first-frame JPEG for the UI and duplicate check."""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return "", None
    with PREVIEW_SEMAPHORE:
        completed = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-user_agent", "Mozilla/5.0",
                "-headers", "Referer: https://www.douyin.com/\r\n",
                "-ss", "0.08", "-i", url, "-frames:v", "1", "-vf", "scale=480:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True,
            timeout=20,
        )
    if completed.returncode != 0 or not completed.stdout:
        return "", None
    # The preview must remain usable even when optional duplicate detection
    # cannot run (for example, if Pillow is unavailable in Finder's Python).
    preview = cache_preview(completed.stdout, ".jpg")
    try:
        frame_hash = _image_hash(completed.stdout)
    except Exception:
        frame_hash = None
    return preview, frame_hash


def _ffmpeg_path() -> str:
    """Find ffmpeg even when Finder launches the app with a minimal PATH."""
    frozen_root = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else None
    if frozen_root is not None:
        bundled = frozen_root / "bin" / "ffmpeg"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _hash_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def enrich_previews_and_duplicates(items: list[MediaItem], debug: list[str]) -> None:
    """Cache usable previews locally and remove visual duplicates.

    Public CDN images are converted to compact cached files so WebKit does not
    depend on hotlink headers or an expiring signed URL. A network or
    decoder failure only skips this optional enhancement; parsing remains
    usable and the original URL stays available as a fallback.
    """
    if not items:
        return

    hashes: dict[int, int] = {}

    def enrich_one(index: int, item: MediaItem) -> tuple[int, str, int | None]:
        if item.kind == "image":
            preview, media_hash = _image_preview_and_hash(item.media_url)
            return index, preview, media_hash
        # Embedded detail already provides the exact poster for normal
        # videos and for each Live image. Prefer that stable, lightweight
        # URL. Local first-frame extraction is only a last resort.
        if item.preview_url:
            if item.preview_url.startswith(("http://", "https://")):
                preview, media_hash = _image_preview_and_hash(item.preview_url)
                return index, preview, media_hash
            return index, item.preview_url, None
        preview, frame_hash = _video_preview_and_hash(item.media_url)
        return index, preview, frame_hash

    # Fetch image fingerprints and video first frames concurrently. FFmpeg
    # calls remain globally limited by PREVIEW_SEMAPHORE above.
    workers = min(6, len(items))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-preview") as pool:
        futures = [pool.submit(enrich_one, index, item) for index, item in enumerate(items)]
        for future in futures:
            try:
                index, preview, media_hash = future.result()
            except Exception:
                continue
            if preview:
                items[index].preview_url = preview
            if media_hash is not None:
                hashes[index] = media_hash

    remove_indexes: set[int] = set()
    image_indexes = [index for index, item in enumerate(items) if item.kind == "image" and index in hashes]
    video_indexes = [index for index, item in enumerate(items) if item.kind == "video" and index in hashes]
    # Video / Live photo has priority over a matching static cover.
    for image_index in image_indexes:
        if items[image_index].relationship_note:
            continue
        for video_index in video_indexes:
            # The public image can be sharper, resized or slightly cropped
            # compared with the MP4 first frame. A 256-bit dHash distance
            # of 40 remains conservative while tolerating those changes.
            if _hash_distance(hashes[image_index], hashes[video_index]) <= 40:
                video = items[video_index]
                remove_indexes.add(image_index)
                if not video.relationship_note:
                    video.relationship_note = "与静态封面成对的动态资源"
                break

    # Public DOM nodes can expose a second playback URL for the same MP4.
    # Keep the first (embedded-detail URLs are added first) and remove the
    # near-identical later rendition from the user-facing result.
    for position, video_index in enumerate(video_indexes):
        for other_index in video_indexes[:position]:
            if _hash_distance(hashes[video_index], hashes[other_index]) <= 8:
                remove_indexes.add(video_index)
                break

    # A secondary image cover can also appear independently of a video.
    for position, image_index in enumerate(image_indexes):
        if items[image_index].relationship_note:
            continue
        for other_index in image_indexes[:position]:
            if _hash_distance(hashes[image_index], hashes[other_index]) <= 12:
                remove_indexes.add(image_index)
                break
    if remove_indexes:
        for index in sorted(remove_indexes, reverse=True):
            del items[index]
        debug.append(f"已过滤 {len(remove_indexes)} 个重复播放地址或封面项。")
