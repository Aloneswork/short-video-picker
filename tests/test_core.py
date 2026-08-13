import unittest
import hashlib
import os
import plistlib
import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import (
    build_media_items_from_aweme_json,
    discover_media_urls,
    extract_aweme_author,
    extract_aweme_media_urls,
    extract_douyin_item_id,
    extract_video_quality_options,
    extract_video_media_urls,
    extract_links,
    infer_kind,
    MediaItem,
    make_parse_error,
    download_item_with_status,
    download_item_with_details,
    suggest_name,
)
from public_browser import (
    EDGE_DISABLED_FEATURES,
    PublicBrowser,
    _best_network_video,
    _browser_user_agent,
    _canonical_douyin_url,
    _extract_pace_detail,
    _is_detail_image,
    _is_detail_video,
    _is_story_detail,
    _media_matches_item,
    _share_filter_entry,
    _share_filter_reason,
    _is_video_page_url,
    _ffmpeg_path,
    _items_from_pace_detail,
    parse_douyin_share_fallback,
)
from core import ParseResult
from app_logging import redact_text, runtime_diagnostics
import desktop
import parser_assistant
import preview_cache
from preview_cache import PreviewServer, cache_preview
from desktop import APP_BUILD, APP_VERSION, HISTORY_LIMIT, PickerApi, _prepare_close


class CoreTests(unittest.TestCase):
    def test_macos_build_is_self_contained_and_release_aware(self):
        root = Path(__file__).resolve().parents[1]
        build_script = (Path(__file__).resolve().parents[1] / "build_macos_app.sh").read_text(encoding="utf-8")
        spec = (root / "short_video_picker.spec").read_text(encoding="utf-8")
        self.assertFalse((root / "macos" / "launcher").exists())
        self.assertIn("-m PyInstaller", build_script)
        self.assertIn('--runtime-probe "$PROBE_JSON"', build_script)
        self.assertIn('--loopback-probe "$LOOPBACK_JSON"', build_script)
        self.assertIn("--parser-assistant", build_script)
        self.assertIn("SIGN_IDENTITY", build_script)
        self.assertIn("NOTARY_PROFILE", build_script)
        self.assertIn('shasum -a 256 "$STAGING_ARCHIVE"', build_script)
        self.assertIn('mv "$STAGING_CHECKSUM" "$CHECKSUM"', build_script)
        self.assertIn('json.loads((ROOT / "version.json")', spec)
        self.assertIn('"CFBundleVersion": str(VERSION["build"])', spec)
        self.assertIn('(str(ROOT / "LICENSE"), ".")', spec)
        self.assertIn('(str(ROOT / "THIRD_PARTY_NOTICES.md"), ".")', spec)
        self.assertIn('(str(RELEASE_LICENSES), "THIRD_PARTY_LICENSES")', spec)
        self.assertIn('公开发行包缺少项目许可证或第三方许可声明', build_script)
        self.assertIn('collect_release_licenses.py', build_script)
        self.assertIn('USE_LOCAL_VENDOR="${USE_LOCAL_VENDOR:-0}"', build_script)
        self.assertIn('"readline"', spec)
        self.assertIn('LC_ALL=C /usr/bin/grep -F -q', build_script)
        self.assertIn('STAGING_ARCHIVE="$STAGING_ROOT/release.zip"', build_script)
        license_script = (root / "scripts" / "collect_release_licenses.py").read_text(encoding="utf-8")
        self.assertIn('公开发行构建禁止使用 Conda/Anaconda 运行时', license_script)
        sync_script = (root / "scripts" / "sync_release_metadata.py").read_text(encoding="utf-8")
        self.assertIn('CONFIG = json.loads((ROOT / "version.json")', sync_script)

    def test_release_version_is_consistent(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "index.html").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        version = json.loads((root / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(APP_VERSION, version["version"])
        self.assertEqual(APP_BUILD, version["build"])
        self.assertIn("id=\"runtime-version\"", html)
        self.assertIn(f"v{APP_VERSION}（Build {APP_BUILD}）", readme)

    def test_source_tree_has_no_legacy_release_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        ignored_parts = {".git", ".venv", ".venv-public.nosync", "venv", "vendor", "dist.nosync", ".build-staging.nosync"}
        forbidden = (
            ".playwright-cli",
            ".claude",
            ".workbuddy",
            "index.backup-20260721-092302.html",
            "index.backup-claude-095634.html",
            "assets/backup-old-icons",
        )
        self.assertEqual([path for path in forbidden if (root / path).exists()], [])
        self.assertEqual([path for path in root.rglob("*.dSYM") if ignored_parts.isdisjoint(path.parts)], [])
        self.assertEqual([path for path in root.rglob(".DS_Store") if ignored_parts.isdisjoint(path.parts)], [])

    def test_frontend_uses_standard_thumbnail_ratios_and_marks_existing_media(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn(".media-grid { display:grid;", html)
        self.assertIn("grid-auto-flow:dense; align-items:start;", html)
        self.assertIn(".media.landscape .thumb-frame { aspect-ratio:8 / 5; }", html)
        self.assertIn(".thumb-frame { position:relative; display:grid; place-items:center; aspect-ratio:3 / 4;", html)
        self.assertIn(".thumb-main { position:absolute; z-index:1; inset:0; width:100%; height:100%; object-fit:contain; object-position:center center;", html)
        self.assertIn(".thumb-blur { position:absolute;", html)
        self.assertIn("object-fit:cover; object-position:center center; filter:blur(", html)
        self.assertIn("border:2px solid var(--line)", html)
        self.assertIn("transform:translateY(-2px)", html)
        self.assertIn("existing?' is-existing':''", html)
        self.assertIn(".media.is-existing.checked", html)
        self.assertIn("item.media_url===result.media_url", html)
        self.assertIn('id="link-count"', html)
        self.assertIn("function updateLinkCount()", html)
        self.assertIn(".source-title-text .story-chip", html)
        self.assertIn("result.content_type==='story'", html)
        self.assertIn('id="cancel-parse"', html)
        self.assertIn("api().parse_status(jobId)", html)
        self.assertIn("saved||existing ? ''", html)
        self.assertNotIn("doneText = saved ? '已存' : '保存'", html)
        self.assertNotIn(">已存<", html)
        self.assertIn("async function retryOne(button)", html)
        self.assertIn("state.results[group]=retryResult(previous,status)", html)
        self.assertIn("oldSection.replaceWith(buildSourceSection(state.results[group],group))", html)
        self.assertIn("window.scrollTo(0,scrollTop)", html)
        self.assertIn("retryOne(retry)", html)
        self.assertNotIn("请将该链接重新粘贴到输入框后再次解析", html)
        self.assertIn('id="runtime-version"', html)
        self.assertIn("initial.app_path", html)
        self.assertIn(".save-bar { position:fixed; left:0; right:0; bottom:0;", html)
        self.assertIn("border-radius:0; background:var(--panel); box-shadow:none;", html)
        self.assertNotIn(".save-bar { position:sticky;", html)
        self.assertIn("calc(var(--save-bar-height) + 24px)", html)
        self.assertIn("new ResizeObserver(syncSaveBarHeight).observe(bar)", html)
        self.assertIn('id="duplicate-details"', html)
        self.assertIn("内容完全一致的重复项（SHA-256）", html)
        self.assertIn('id="failure-details"', html)
        self.assertIn("`已处理 ${completed}/${total}`", html)
        self.assertIn("if (saved) pieces.push(`新保存 ${saved} 项`)", html)
        self.assertIn("if (existing) pieces.push(`内容重复 ${existing} 项`)", html)
        self.assertIn("if (failed) pieces.push(`失败 ${failed} 项`)", html)
        self.assertNotIn("pieces.push(`新保存 ${saved} 项`,`内容重复 ${existing} 项`)", html)
        self.assertIn("node.title=text", html)
        self.assertIn('id="history-button"', html)
        self.assertIn('id="history-popover"', html)
        self.assertIn('id="history-search"', html)
        self.assertIn('data-history-filter="success"', html)
        self.assertIn('data-history-filter="failed"', html)
        self.assertIn("bridge.get_parse_history", html)
        self.assertIn("function syncResultPresence", html)
        self.assertIn("bridge.set_result_presence(hasResults)", html)
        self.assertIn("syncResultPresence();", html)

    def test_public_author_is_extracted_and_serialised(self):
        payload = {"author": {"nickname": "测试作者"}}
        self.assertEqual(extract_aweme_author(payload), "测试作者")
        result = ParseResult(
            "source",
            "page",
            "文案",
            [],
            author=extract_aweme_author(payload),
        )
        self.assertEqual(parser_assistant.serialise(result)["author"], "测试作者")

    def test_extract_links(self):
        text = "复制链接 https://v.douyin.com/abc123/ 还有 https://example.com/a.mp4。"
        self.assertEqual(
            extract_links(text),
            ["https://v.douyin.com/abc123/", "https://example.com/a.mp4"],
        )

    def test_discover_media_urls_from_html(self):
        html = """
        <html><head>
        <meta property="og:video" content="https://cdn.example.com/a.mp4">
        <script>window.__INITIAL_STATE__={"image_url":"https:\\/\\/cdn.example.com\\/b.webp"}</script>
        </head></html>
        """
        urls = discover_media_urls(html)
        self.assertIn("https://cdn.example.com/a.mp4", urls)
        self.assertIn("https://cdn.example.com/b.webp", urls)

    def test_infer_kind(self):
        self.assertEqual(infer_kind("https://cdn.example.com/a.mp4"), "video")
        self.assertEqual(infer_kind("https://cdn.example.com/a.jpg"), "image")

    def test_suggest_name(self):
        name = suggest_name("a/b c", "https://cdn.example.com/x", "video", 2)
        self.assertEqual(name, "a_b_c_02.mp4")

    def test_suggest_name_normalizes_user_facing_formats(self):
        self.assertEqual(
            suggest_name("图集", "https://cdn.example.com/source.webp?x=1", "image", 1),
            "图集_01.jpg",
        )
        self.assertEqual(
            suggest_name("图集", "https://cdn.example.com/play?format=webm", "video", 2),
            "图集_02.mp4",
        )

    def test_titleless_douyin_video_uses_work_id_filename(self):
        page = "https://www.douyin.com/video/7663078622605666481"
        self.assertEqual(
            suggest_name("", "https://v3.douyinvod.com/play?mime_type=video_mp4", "video", source_url=page),
            "抖音视频_7663078622605666481_01.mp4",
        )

    def test_titled_douyin_filename_also_uses_work_id(self):
        page = "https://www.douyin.com/video/7663078622605666481"
        self.assertEqual(
            suggest_name("相同标题", "https://v3.douyinvod.com/play", "video", source_url=page),
            "相同标题_7663078622605666481_01.mp4",
        )

    def test_download_repairs_empty_or_extension_only_filename(self):
        page = "https://www.douyin.com/video/7663078622605666481"
        expected = "抖音视频_7663078622605666481_01.mp4"
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"already here"

        with tempfile.TemporaryDirectory() as folder, patch("core.urlopen", return_value=FakeResponse()):
            existing = os.path.join(folder, expected)
            with open(existing, "wb") as output:
                output.write(b"already here")
            item = MediaItem(
                page,
                page,
                "https://v3.douyinvod.com/play?mime_type=video_mp4",
                "video",
                suggested_name=".mp4",
            )
            path, created = download_item_with_status(item, folder)
        self.assertFalse(created)
        self.assertEqual(os.path.basename(path), expected)

    def test_existing_canonical_filename_is_not_downloaded_again(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"already here"

        with tempfile.TemporaryDirectory() as folder, patch("core.urlopen", return_value=FakeResponse()):
            filename = "同一资源_01.mp4"
            with open(os.path.join(folder, filename), "wb") as output:
                output.write(b"already here")
            item = MediaItem("source", "page", "https://invalid.example/video.mp4", "video", suggested_name=filename)
            path, created = download_item_with_status(item, folder)
            self.assertFalse(created)
            self.assertEqual(os.path.basename(path), filename)

    def test_exact_duplicate_reports_hash_match_and_matched_path(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"identical bytes"

        with tempfile.TemporaryDirectory() as folder, patch("core.urlopen", return_value=FakeResponse()):
            filename = "真正重复_01.mp4"
            existing = os.path.join(folder, filename)
            Path(existing).write_bytes(b"identical bytes")
            item = MediaItem("source", "page", "https://cdn.example.com/video", "video", suggested_name=filename)
            result = download_item_with_details(item, folder)

        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["matched_path"], existing)
        self.assertIn("SHA-256", result["reason"])

    def test_same_filename_different_content_is_saved_not_reported_duplicate(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"new media bytes"

        with tempfile.TemporaryDirectory() as folder, patch("core.urlopen", return_value=FakeResponse()):
            original = os.path.join(folder, "相同标题_01.mp4")
            Path(original).write_bytes(b"unrelated old bytes")
            item = MediaItem(
                "https://www.douyin.com/video/7663078622605666481",
                "https://www.douyin.com/video/7663078622605666481",
                "https://v3.douyinvod.com/new-video",
                "video",
                suggested_name="相同标题_01.mp4",
            )
            result = download_item_with_details(item, folder)

        self.assertEqual(result["status"], "saved")
        self.assertTrue(result["renamed_due_to_collision"])
        self.assertIn("7663078622605666481", os.path.basename(result["path"]))
        self.assertIn("内容不同", result["reason"])

    def test_download_uses_media_specific_public_share_headers(self):
        captured = {}

        class FakeResponse:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                self.reads += 1
                return b"video" if self.reads == 1 else b""

        def fake_urlopen(request, timeout=0):
            captured["user_agent"] = request.get_header("User-agent")
            captured["referer"] = request.get_header("Referer")
            captured["accept"] = request.get_header("Accept")
            captured["timeout"] = timeout
            return FakeResponse()

        with tempfile.TemporaryDirectory() as folder, patch("core.urlopen", side_effect=fake_urlopen):
            item = MediaItem(
                "source",
                "https://www.douyin.com/video/123",
                "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=example",
                "video",
                suggested_name="兜底视频.mp4",
                headers={"User-Agent": "Mobile Share UA", "Referer": "https://www.iesdouyin.com/share/video/123/"},
            )
            path, created = download_item_with_status(item, folder)
            saved_data = Path(path).read_bytes()

        self.assertTrue(created)
        self.assertEqual(saved_data, b"video")
        self.assertEqual(captured["user_agent"], "Mobile Share UA")
        self.assertEqual(captured["referer"], "https://www.iesdouyin.com/share/video/123/")
        self.assertEqual(captured["timeout"], 45)

    def test_video_download_resumes_a_stable_partial_with_range(self):
        captured = {}

        class PartialResponse:
            status = 206

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"tail"

        def fake_urlopen(request, timeout=0):
            captured["range"] = request.get_header("Range")
            captured["timeout"] = timeout
            return PartialResponse()

        media_url = "https://cdn.example.com/resumable.mp4?token=private"
        resume_key = hashlib.sha256(media_url.encode()).hexdigest()[:20]
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / f".short-video-picker-resume-{resume_key}.part"
            partial.write_bytes(b"head-")
            item = MediaItem("source", "page", media_url, "video", suggested_name="断点.mp4")
            with patch("core.urlopen", side_effect=fake_urlopen):
                result = download_item_with_details(item, folder)
            saved = Path(result["path"]).read_bytes()

        self.assertEqual(captured["range"], "bytes=5-")
        self.assertEqual(captured["timeout"], 45)
        self.assertEqual(saved, b"head-tail")

    def test_video_download_restarts_when_server_ignores_range(self):
        class FullResponse:
            status = 200

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _size=-1):
                if getattr(self, "done", False): return b""
                self.done = True
                return b"complete"

        media_url = "https://cdn.example.com/full-restart.mp4"
        resume_key = hashlib.sha256(media_url.encode()).hexdigest()[:20]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / f".short-video-picker-resume-{resume_key}.part").write_bytes(b"stale")
            item = MediaItem("source", "page", media_url, "video", suggested_name="重下.mp4")
            with patch("core.urlopen", return_value=FullResponse()):
                result = download_item_with_details(item, folder)
            saved = Path(result["path"]).read_bytes()

        self.assertEqual(saved, b"complete")

    def test_extract_douyin_item_id(self):
        text = "https://www.douyin.com/note/7644905499757940014?previous_page=app_code_link"
        self.assertEqual(extract_douyin_item_id(text), "7644905499757940014")

    def test_build_items_from_douyin_slides_payload(self):
        payload = {
            "aweme_detail": {
                "aweme_id": "123",
                "aweme_type": 2,
                "images": [
                    {"url_list": ["https://p3.douyinpic.com/image-a.webp"]},
                    {"download_url_list": ["https://p3.douyinpic.com/image-b.webp"]},
                ],
                "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/aggregate.mp4"]}},
            }
        }
        items = build_media_items_from_aweme_json("https://v.douyin.com/a/", "https://www.douyin.com/note/123", payload, "测试")
        self.assertEqual([item.kind for item in items], ["image", "image"])
        self.assertNotIn("aggregate.mp4", [item.media_url for item in items])

    def test_aweme_extractor_keeps_every_live_item_from_mixed_public_fields(self):
        payload = {
            "aweme_detail": {
                "aweme_id": "123",
                "aweme_type": 2,
                "images": [
                    {
                        "url_list": ["https://p3.douyinpic.com/static.webp"],
                    },
                    {
                        "url_list": ["https://p3.douyinpic.com/live-a.webp"],
                        "live_photo": {
                            "download_addr": {"url_list": ["https://v3.douyinvod.com/live-a.mp4"]},
                        },
                    },
                    {
                        "urlList": ["https://p3.douyinpic.com/live-b.webp"],
                        "livePhoto": {
                            "bitRate": [
                                {
                                    "playAddrH264": {
                                        "urlList": ["https://v3.douyinvod.com/live-b.mp4"],
                                    }
                                }
                            ]
                        },
                    },
                ],
                "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/aggregate.mp4"]}},
            }
        }

        urls = extract_aweme_media_urls(payload, expected_item_id="123")

        self.assertEqual(
            urls,
            [
                "https://p3.douyinpic.com/static.webp",
                "https://v3.douyinvod.com/live-a.mp4",
                "https://v3.douyinvod.com/live-b.mp4",
            ],
        )

    def test_video_address_extractor_uses_bitrate_only_as_fallback(self):
        direct = "https://v3.douyinvod.com/direct.mp4?signature=a%2Fb%3D"
        alternate = "https://v3.douyinvod.com/alternate.mp4"
        value = {
            "downloadAddr": {"urlList": [direct]},
            "bit_rate": [{"play_addr_h264": {"url_list": [alternate]}}],
        }
        self.assertEqual(extract_video_media_urls(value), [direct])

        bitrate_only = {"bitRate": [{"playAddrH264": {"urlList": [alternate]}}]}
        self.assertEqual(extract_video_media_urls(bitrate_only), [alternate])

    def test_video_quality_options_are_deduplicated_and_highest_first(self):
        high = "https://v3.douyinvod.com/high.mp4"
        low = "https://v3.douyinvod.com/low.mp4"
        value = {
            "bitRate": [
                {"bitRate": 800_000, "gearName": "normal_720", "playAddr": {"urlList": [low]}},
                {"bitRate": 2_400_000, "gearName": "high_1080", "playAddr": {"urlList": [high]}},
                {"bitRate": 2_400_000, "gearName": "duplicate", "playAddr": {"urlList": [high]}},
            ]
        }

        options = extract_video_quality_options(value)

        self.assertEqual([option["url"] for option in options], [high, low])
        self.assertEqual([option["bitrate"] for option in options], [2_400_000, 800_000])
        self.assertEqual(options[0]["label"], "high 1080")

    def test_aweme_builder_exposes_normal_video_quality_choices(self):
        high = "https://v3.douyinvod.com/high.mp4"
        low = "https://v3.douyinvod.com/low.mp4"
        payload = {
            "aweme_detail": {
                "aweme_id": "1234567890123456789",
                "aweme_type": 0,
                "video": {
                    "play_addr": {"url_list": [low]},
                    "bit_rate": [
                        {"bit_rate": 700_000, "gear_name": "720p", "play_addr": {"url_list": [low]}},
                        {"bit_rate": 2_000_000, "gear_name": "1080p", "play_addr": {"url_list": [high]}},
                    ],
                },
            }
        }

        items = build_media_items_from_aweme_json(
            "source", "https://www.douyin.com/video/1234567890123456789", payload, "清晰度",
            expected_item_id="1234567890123456789",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].media_url, high)
        self.assertEqual([option["url"] for option in items[0].quality_options], [high, low])
        self.assertEqual(items[0].quality_label, "1080p")

    def test_expected_item_id_excludes_unrelated_aweme(self):
        payload = {
            "item_list": [
                {"aweme_id": "wanted", "images": [{"url_list": ["https://p3.douyinpic.com/wanted.webp"]}]},
                {"aweme_id": "other", "images": [{"url_list": ["https://p3.douyinpic.com/other.webp"]}]},
            ]
        }
        items = build_media_items_from_aweme_json("source", "page", payload, "测试", expected_item_id="wanted")
        self.assertEqual(len(items), 1)
        self.assertIn("wanted.webp", items[0].media_url)

    def test_public_page_filters_only_current_detail_resources(self):
        self.assertTrue(_is_detail_image("https://p3.douyinpic.com/a.webp?s=PackSourceEnum_AWEME_DETAIL"))
        self.assertFalse(_is_detail_image("https://p3.douyinpic.com/a.webp?s=PackSourceEnum_WEBPC_RELATED_AWEME"))
        self.assertTrue(_is_detail_video("https://v3.douyinvod.com/a/?mime_type=video_mp4"))
        self.assertFalse(_is_detail_video("https://example.com/a.mp4?mime_type=video_mp4"))
        self.assertTrue(_is_detail_video("https://v95-web-sz.douyinvod.com/video/tos/media-video-avc1/?mime_type=video_mp4"))
        self.assertFalse(_is_detail_video("https://v95-web-sz.douyinvod.com/video/tos/media-audio-und-mp4a/?mime_type=video_mp4"))
        self.assertTrue(_is_video_page_url("https://www.douyin.com/video/7664281366611111537?x=1"))
        self.assertFalse(_is_video_page_url("https://www.douyin.com/note/7664436367111774190?x=1"))

    def test_canonical_douyin_url_removes_share_tracking_query(self):
        source = "https://www.douyin.com/video/7663078622605666481?previous_page=app_code_link"
        self.assertEqual(
            _canonical_douyin_url(source),
            "https://www.douyin.com/video/7663078622605666481",
        )
        self.assertEqual(_canonical_douyin_url("https://example.com/video/1?x=1"), "https://example.com/video/1?x=1")

    def test_network_fallback_keeps_best_video_rendition(self):
        base = "https://v95-web-sz.douyinvod.com/video/tos/media-video-avc1/"
        selected = _best_network_video([
            f"{base}?mime_type=video_mp4&br=430&signature=one",
            f"{base}?mime_type=video_mp4&br=1200&signature=two",
        ])
        self.assertEqual(len(selected), 1)
        self.assertIn("br=1200", selected[0])

    def test_media_url_item_id_excludes_related_video(self):
        target = "7663078622605666481"
        self.assertTrue(_media_matches_item(f"https://v3.douyinvod.com/x?mime_type=video_mp4&__vid={target}", target))
        self.assertFalse(_media_matches_item("https://v3.douyinvod.com/x?mime_type=video_mp4&__vid=999", target))

    def test_error_catalog_serialises_stable_code_and_hint(self):
        result = make_parse_error("source", "page", "DY-WORK-UNAVAILABLE", status="empty")
        payload = parser_assistant.serialise(result)
        self.assertEqual(payload["error_code"], "DY-WORK-UNAVAILABLE")
        self.assertFalse(payload["retryable"])
        self.assertIn("抖音", payload["error"])
        self.assertTrue(payload["error_hint"])

    def test_public_browser_classifies_unavailable_work(self):
        class FakeClient:
            def call(self, method, params=None, **kwargs):
                if method == "Runtime.evaluate":
                    page = {
                        "title": "",
                        "url": "https://www.douyin.com/note/123",
                        "images": [],
                        "videos": [],
                        "network": [],
                        "embedded": ['awemeId":"123","aweme":null'],
                        "unavailable": "你要观看的图文不存在",
                        "challenge": False,
                    }
                    return {"result": {"value": __import__("json").dumps(page)}}
                return {}

            def close(self):
                pass

        with patch("public_browser._free_port", return_value=54322):
            browser = PublicBrowser()
        browser._page_client = lambda: FakeClient()  # type: ignore[method-assign]
        try:
            with patch("public_browser.time.sleep", return_value=None):
                result = browser.parse_douyin("source", "https://www.douyin.com/note/123")
        finally:
            browser.close()
        self.assertEqual(result.error_code, "DY-WORK-UNAVAILABLE")
        self.assertFalse(result.retryable)

    def test_public_browser_keeps_target_network_video(self):
        target = "7663078622605666481"

        class FakeClient:
            def call(self, method, params=None, **kwargs):
                if method == "Runtime.evaluate":
                    page = {
                        "title": "测试视频 - 抖音",
                        "url": f"https://www.douyin.com/video/{target}",
                        "images": [],
                        "videos": [],
                        "network": [f"https://v3.douyinvod.com/x?mime_type=video_mp4&br=800&__vid={target}"],
                        "embedded": [],
                        "unavailable": "",
                        "challenge": False,
                    }
                    return {"result": {"value": __import__("json").dumps(page)}}
                return {}

            def close(self):
                pass

        with patch("public_browser._free_port", return_value=54323):
            browser = PublicBrowser()
        browser._page_client = lambda: FakeClient()  # type: ignore[method-assign]
        try:
            with patch("public_browser.time.sleep", return_value=None):
                result = browser.parse_douyin("source", f"https://www.douyin.com/video/{target}")
        finally:
            browser.close()
        self.assertEqual(result.error_code, "")
        self.assertEqual(len(result.items), 1)
        self.assertIn(target, result.items[0].media_url)

    def test_public_browser_classifies_completed_empty_shell_separately(self):
        target = "7663078622605666481"

        class FakeClient:
            def call(self, method, params=None, **kwargs):
                if method == "Runtime.evaluate":
                    page = {
                        "title": "",
                        "url": f"https://www.douyin.com/video/{target}",
                        "images": [],
                        "videos": [],
                        "network": [],
                        "embedded": [f'pathname:/video/{target}; awemeId'],
                        "readyState": "complete",
                        "bodyLength": 500,
                        "unavailable": "",
                        "challenge": False,
                    }
                    return {"result": {"value": __import__("json").dumps(page)}}
                return {}

            def close(self):
                pass

        with patch("public_browser._free_port", return_value=54327):
            browser = PublicBrowser()
        browser._page_client = lambda: FakeClient()  # type: ignore[method-assign]
        try:
            with (
                patch("public_browser.time.sleep", return_value=None),
                patch("public_browser.time.monotonic", side_effect=[0, 1, 2, 100]),
            ):
                result = browser.parse_douyin("source", f"https://www.douyin.com/video/{target}")
        finally:
            browser.close()
        self.assertEqual(result.error_code, "DY-PAGE-SHELL")
        self.assertIn("文档状态=complete", result.debug[-2])

    def test_official_share_fallback_reads_exact_video_router_data(self):
        target = "7666677674693244646"
        router_data = {
            "loaderData": {
                "video_page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": target,
                                "desc": "公开分享视频",
                                "video": {
                                    "play_addr": {
                                        "url_list": [
                                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=example"
                                        ]
                                    },
                                    "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
                                },
                                "images": None,
                            }
                        ]
                    }
                }
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.title, "公开分享视频")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].kind, "video")
        self.assertIn("playwm", result.items[0].media_url)
        self.assertIn("cover.jpeg", result.items[0].preview_url)
        self.assertIn("iesdouyin.com/share/video", result.items[0].headers["Referer"])

    def test_official_share_fallback_retries_and_keeps_original_share_identity(self):
        target = "7661096859192068602"
        router_data = {
            "videoInfoRes": {
                "item_list": [
                    {
                        "aweme_id": target,
                        "desc": "重试成功",
                        "video": {
                            "play_addr": {
                                "url_list": ["https://v3.douyinvod.com/retry-video"]
                            }
                        },
                    }
                ]
            }
        }
        good_html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __init__(self, html):
                self.html = html

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return self.html.encode()

        source = f"https://www.iesdouyin.com/share/video/{target}/?from_ssr=1"
        generic_redirect = f"https://www.douyin.com/jingxuan?modal_id={target}"
        with (
            patch(
                "public_browser.urlopen",
                side_effect=[FakeResponse("<html></html>"), FakeResponse(good_html)],
            ) as urlopen,
            patch("public_browser.time.sleep") as sleep,
        ):
            result = parse_douyin_share_fallback(source, generic_redirect)

        self.assertIsNotNone(result)
        self.assertEqual(result.title, "重试成功")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_official_share_fallback_keeps_one_image_per_slide_and_ignores_music(self):
        target = "7644905499757940014"
        router_data = {
            "videoInfoRes": {
                "item_list": [
                    {
                        "aweme_id": target,
                        "desc": "公开图集",
                        "video": {
                            "play_addr": {
                                "url_list": ["https://music.example.com/background.mp3"]
                            }
                        },
                        "images": [
                            {"url_list": ["https://p3.douyinpic.com/one.webp"]},
                            {"url_list": ["https://p3.douyinpic.com/two.webp"]},
                        ],
                    }
                ]
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/note/{target}",
                f"https://www.douyin.com/note/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual([item.kind for item in result.items], ["image", "image"])
        self.assertNotIn("background.mp3", " ".join(item.media_url for item in result.items))

    def test_official_share_fallback_marks_public_story(self):
        target = "7668692124564635529"
        router_data = {
            "videoInfoRes": {
                "item_list": [
                    {
                        "aweme_id": target,
                        "desc": "公开限时日常",
                        "is_story": 1,
                        "story_ttl": 86400,
                        "video": {
                            "play_addr": {
                                "url_list": [
                                    "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=story"
                                ]
                            },
                            "cover": {"url_list": ["https://p3.douyinpic.com/story.jpeg"]},
                        },
                    }
                ]
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.content_type, "story")
        self.assertEqual(result.content_label, "限时日常")
        self.assertEqual(len(result.items), 1)
        serialised = parser_assistant.serialise(result)
        self.assertEqual(serialised["content_type"], "story")
        self.assertEqual(serialised["content_label"], "限时日常")

    def test_official_share_fallback_classifies_app_only_story_filter(self):
        target = "7668692124564635529"
        router_data = {
            "videoInfoRes": {
                "filter_list": [
                    {"aweme_id": target, "filter_reason": "story_25_filter"}
                ],
                "item_list": [],
                "status_code": 0,
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, "DY-STORY-WEB-FILTERED")
        self.assertEqual(result.content_type, "story")
        self.assertFalse(result.retryable)
        self.assertEqual(_share_filter_reason(router_data, target), "story_25_filter")

    def test_official_share_fallback_requires_explicit_expiry_signal(self):
        target = "7668692124564635529"
        router_data = {
            "videoInfoRes": {
                "filter_list": [
                    {"aweme_id": target, "filter_reason": "story_ttl_expired"}
                ],
                "item_list": [],
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, "DY-STORY-EXPIRED")

    def test_official_share_fallback_classifies_friend_only_story(self):
        target = "7668692124564635529"
        router_data = {
            "videoInfoRes": {
                "filter_list": [
                    {"aweme_id": target, "filter_reason": "story_close_friend_filter"}
                ],
                "item_list": [],
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, "DY-STORY-NOT-PUBLIC")
        self.assertFalse(result.retryable)

    def test_official_share_fallback_classifies_self_only_normal_work(self):
        target = "7667146943013071205"
        router_data = {
            "videoInfoRes": {
                "filter_list": [
                    {
                        "aweme_id": target,
                        "filter_reason": "status_self_see",
                        "notice": "抱歉，作品不见了",
                        "detail_msg": "因作品权限或已被删除，无法观看",
                    }
                ],
                "item_list": [],
            }
        }
        html = f"<script>window._ROUTER_DATA = {__import__('json').dumps(router_data)}</script>"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return html.encode()

        with patch("public_browser.urlopen", return_value=FakeResponse()):
            result = parse_douyin_share_fallback(
                f"https://www.douyin.com/video/{target}",
                f"https://www.douyin.com/video/{target}",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, "DY-WORK-NOT-PUBLIC")
        self.assertFalse(result.retryable)
        self.assertEqual(_share_filter_reason(router_data, target), "status_self_see")
        self.assertEqual(_share_filter_entry(router_data, target)["notice"], "抱歉，作品不见了")
        self.assertIn("权限或已被删除", result.debug[-1])

    def test_story_flags_do_not_mark_normal_video(self):
        self.assertTrue(_is_story_detail({"is_24_story": True}))
        self.assertTrue(_is_story_detail({"story_ttl": 3600}))
        self.assertFalse(_is_story_detail({"is_story": 0, "story_ttl": 0, "aweme_type": 0}))

    def test_native_clipboard_bridge_returns_pbpaste_text(self):
        completed = unittest.mock.Mock(returncode=0, stdout="分享链接 https://v.douyin.com/example/")
        with patch("desktop.subprocess.run", return_value=completed) as run:
            result = PickerApi().clipboard_text()
        self.assertTrue(result["ok"])
        self.assertIn("v.douyin.com", result["text"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbpaste"])

    def test_empty_input_returns_stable_error_code(self):
        result = PickerApi().start_parse("没有链接")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INPUT-NO-LINK")

    def test_parse_history_is_searchable_filterable_and_keeps_configured_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            api = PickerApi(Path(folder) / "parse_history.sqlite3")
            results = []
            for index in range(HISTORY_LIMIT + 5):
                status = index % 3
                results.append(
                    {
                        "source_url": f"https://www.douyin.com/video/{7000000000000000000 + index}",
                        "input_url": f"https://v.douyin.com/input-{index}/",
                        "title": f"第 {index} 条猫咪文案",
                        "author": f"作者 {index}",
                        "items": [{"kind": "video"}] if status == 0 else [],
                        "error_code": "" if status == 0 else ("PARSE-CANCELLED" if status == 1 else "DY-NO-MEDIA"),
                    }
                )
            api._record_parse_history(results, parsed_at="2026-08-07T12:00:00+08:00")

            all_records = api.get_parse_history()
            self.assertTrue(all_records["ok"])
            self.assertEqual(all_records["total"], HISTORY_LIMIT)
            self.assertEqual(len(all_records["records"]), HISTORY_LIMIT)
            self.assertEqual(all_records["records"][0]["title"], f"第 {HISTORY_LIMIT + 4} 条猫咪文案")
            self.assertEqual(all_records["records"][-1]["title"], "第 5 条猫咪文案")

            searched = api.get_parse_history("input-99/", "all")
            self.assertEqual([row["author"] for row in searched["records"]], ["作者 99"])
            self.assertEqual(searched["records"][0]["input_url"], "https://v.douyin.com/input-99/")

            successful = api.get_parse_history("", "success")
            self.assertTrue(successful["records"])
            self.assertTrue(all(row["result_status"] == "success" for row in successful["records"]))
            failed = api.get_parse_history("", "failed")
            self.assertTrue(failed["records"])
            self.assertTrue(all(row["result_status"] in {"failed", "cancelled"} for row in failed["records"]))

    def test_parse_history_exports_csv_and_txt_with_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            api = PickerApi(root / "parse_history.sqlite3")
            api._record_parse_history(
                [
                    {
                        "source_url": "https://www.douyin.com/video/7111111111111111111",
                        "input_url": "https://v.douyin.com/example/",
                        "title": "导出文案",
                        "author": "导出作者",
                        "items": [{"kind": "video"}],
                    }
                ],
                parsed_at="2026-08-12T12:00:00+08:00",
            )
            window = unittest.mock.Mock()
            csv_path = root / "history.csv"
            txt_path = root / "history.txt"
            with patch.object(desktop.webview, "windows", [window]):
                window.create_file_dialog.return_value = [str(csv_path)]
                csv_result = api.export_parse_history("csv")
                window.create_file_dialog.return_value = [str(txt_path)]
                txt_result = api.export_parse_history("txt")

            self.assertTrue(csv_result["ok"])
            self.assertTrue(txt_result["ok"])
            csv_text = csv_path.read_text(encoding="utf-8-sig")
            txt_text = txt_path.read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("解析时间,输入链接,来源链接"))
            self.assertTrue(txt_text.startswith("解析时间\t输入链接\t来源链接"))
            self.assertIn("导出文案", csv_text)
            self.assertIn("导出作者", txt_text)

    def test_native_close_confirmation_tracks_only_visible_parse_results(self):
        with tempfile.TemporaryDirectory() as folder:
            api = PickerApi(Path(folder) / "parse_history.sqlite3")
            window = unittest.mock.Mock(confirm_close=False)
            _prepare_close(window, api)
            self.assertFalse(window.confirm_close)

            api.set_result_presence(True)
            _prepare_close(window, api)
            self.assertTrue(window.confirm_close)

            api.set_result_presence(False)
            _prepare_close(window, api)
            self.assertFalse(window.confirm_close)

    def test_parse_api_rejects_a_second_concurrent_job(self):
        api = PickerApi()
        api._parse_jobs["already-running"] = {"done": False}
        result = api.start_parse("https://www.douyin.com/video/7111111111111111111")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PARSE-BUSY")
        self.assertEqual(list(api._parse_jobs), ["already-running"])

    def test_initial_state_exposes_running_build_and_path(self):
        initial = PickerApi().get_initial_state()
        self.assertEqual(initial["version"], APP_VERSION)
        self.assertEqual(initial["build"], APP_BUILD)
        self.assertTrue(initial["app_path"])
        with (Path(__file__).resolve().parents[1] / "macos" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.assertEqual(info["CFBundleShortVersionString"], "0.0.0")
        self.assertEqual(info["CFBundleVersion"], "0")

    def test_media_bridge_rejects_unknown_fields_and_unsafe_urls(self):
        with self.assertRaisesRegex(ValueError, "不支持的字段"):
            PickerApi._validated_media_value(
                {"source_url": "s", "page_url": "p", "media_url": "https://cdn/x", "kind": "video", "shell": True}
            )
        with self.assertRaisesRegex(ValueError, "http 或 https"):
            PickerApi._validated_media_value(
                {"source_url": "s", "page_url": "p", "media_url": "file:///etc/passwd", "kind": "video"}
            )
        with self.assertRaisesRegex(ValueError, "视频或图片"):
            PickerApi._validated_media_value(
                {"source_url": "s", "page_url": "p", "media_url": "https://cdn/x", "kind": "document"}
            )

    def test_media_bridge_whitelists_headers_and_quality_options(self):
        value = PickerApi._validated_media_value(
            {
                "source_url": "source",
                "page_url": "page",
                "media_url": "https://cdn.example.com/high.mp4",
                "kind": "video",
                "suggested_name": "../安全.mp4",
                "headers": {"User-Agent": "UA", "Cookie": "private", "Accept": "*/*"},
                "quality_options": [
                    {"label": "高清", "url": "https://cdn.example.com/high.mp4", "bitrate": "2400000", "extra": "drop"},
                    {"label": "本地", "url": "file:///tmp/a.mp4", "bitrate": 1},
                ],
            }
        )
        self.assertEqual(value["suggested_name"], "安全.mp4")
        self.assertEqual(value["headers"], {"User-Agent": "UA", "Accept": "*/*"})
        self.assertEqual(
            value["quality_options"],
            [{"label": "高清", "url": "https://cdn.example.com/high.mp4", "bitrate": 2_400_000}],
        )

    def test_preview_cache_uses_tokenized_loopback_url(self):
        class FakeHttpServer:
            def __init__(self, address, handler):
                self.server_address = (address[0], 43210)
                self.handler = handler

            def serve_forever(self): return None
            def shutdown(self): return None
            def server_close(self): return None

        with tempfile.TemporaryDirectory() as folder:
            cache_root = Path(folder)
            with (
                patch.object(preview_cache, "PREVIEW_CACHE_DIR", cache_root),
                patch.object(preview_cache, "ThreadingHTTPServer", FakeHttpServer),
            ):
                reference = cache_preview(b"preview-bytes", ".jpg")
                self.assertTrue(reference.startswith("preview-cache://"))
                self.assertNotIn("base64", reference)
                server = PreviewServer()
                server.start()
                try:
                    resolved = server.resolve(reference)
                    self.assertTrue(resolved.startswith("http://127.0.0.1:43210/"))
                    self.assertIn(server.token, resolved)
                    self.assertEqual(next(cache_root.iterdir()).read_bytes(), b"preview-bytes")
                    self.assertEqual(server.resolve("preview-cache://../secret"), "")
                finally:
                    server.close()

    def test_log_redaction_removes_queries_and_unrelated_paths(self):
        redacted = redact_text(
            "GET https://v3.douyinvod.com/path/private.mp4?signature=secret "
            "https://www.douyin.com/video/7663078622605666481?token=secret"
        )
        self.assertNotIn("signature", redacted)
        self.assertNotIn("token", redacted)
        self.assertNotIn("private.mp4", redacted)
        self.assertIn("/work/7663078622605666481", redacted)

    def test_runtime_diagnostics_has_actionable_component_results(self):
        result = runtime_diagnostics()
        self.assertEqual(result["version"], APP_VERSION)
        self.assertEqual(result["build"], APP_BUILD)
        self.assertEqual(
            {entry["name"] for entry in result["checks"]},
            {"内置 Python", "Pillow", "macOS WebView", "Microsoft Edge", "ffmpeg"},
        )
        self.assertTrue(all({"ok", "level", "detail", "guidance"} <= set(entry) for entry in result["checks"]))

    def test_frozen_parser_uses_same_embedded_executable(self):
        api = PickerApi()
        job_id = "frozen-command"
        api._parse_jobs[job_id] = {"done": False, "cancel_requested": False, "results": [], "completed": 0}
        process = unittest.mock.Mock()
        process.stdin = unittest.mock.Mock()
        process.stdout = iter(
            [json.dumps({"event": "complete", "result": {"ok": False, "message": "done", "results": []}})]
        )
        process.stderr = unittest.mock.Mock()
        process.stderr.read.return_value = ""
        process.wait.return_value = 0
        with (
            patch.object(desktop.sys, "frozen", True, create=True),
            patch("desktop.subprocess.Popen", return_value=process) as popen,
            patch.object(api, "_record_parse_job_history"),
        ):
            api._run_parse_job(job_id, ["https://www.douyin.com/video/7111111111111111111"])
        self.assertEqual(popen.call_args.args[0], [sys.executable, "--parser-assistant", "--stream"])

    def test_cancelled_parse_keeps_completed_results_and_marks_pending_links(self):
        api = PickerApi()
        job_id = "cancel-test"
        links = [
            "https://www.douyin.com/video/7111111111111111111",
            "https://www.douyin.com/video/7222222222222222222",
        ]
        api._parse_jobs[job_id] = {
            "completed": 1,
            "results": [{"source_url": links[0], "items": [{"kind": "video"}]}],
        }
        api._finish_cancelled_parse(job_id, links)
        status = api.parse_status(job_id)
        self.assertTrue(status["cancelled"])
        self.assertTrue(status["done"])
        self.assertEqual(len(status["results"]), 2)
        self.assertEqual(status["results"][1]["error_code"], "PARSE-CANCELLED")
        self.assertIn("完成 1/2", status["message"])

    def test_desktop_shutdown_terminates_active_parser_groups(self):
        api = PickerApi()
        process = unittest.mock.Mock()
        api._parse_processes["active"] = process
        api._parse_jobs["active"] = {"done": False, "cancel_requested": False}
        with patch.object(api, "_signal_parse_process") as signal_process:
            api.shutdown()
        self.assertTrue(api._parse_jobs["active"]["cancel_requested"])
        signal_process.assert_called_once_with(process, __import__("signal").SIGTERM)

    def test_parser_termination_installs_cleanup_signals(self):
        with patch("parser_assistant.signal.signal") as install:
            parser_assistant._install_termination_cleanup()
        installed = [call.args[0] for call in install.call_args_list]
        self.assertEqual(installed, [__import__("signal").SIGTERM, __import__("signal").SIGINT])

    def test_parser_cleans_candidate_when_terminated_during_browser_start(self):
        instances = []

        class TerminatedBrowser:
            def __init__(self):
                self.closed = False
                instances.append(self)

            def start(self):
                raise SystemExit(143)

            def close(self):
                self.closed = True

        with (
            patch("parser_assistant.PublicBrowser", TerminatedBrowser),
            patch("parser_assistant.parse_douyin_share_fallback", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                parser_assistant.parse_payload(
                    {"links": ["https://www.douyin.com/video/7667777065190242683"]}
                )

        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed)

    def test_browser_user_agent_uses_installed_version(self):
        with (
            patch("public_browser.Path.open", unittest.mock.mock_open(read_data=b"not-a-plist")),
            patch("public_browser.plistlib.load", return_value={"CFBundleShortVersionString": "150.0.4078.83"}),
        ):
            user_agent = _browser_user_agent("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        self.assertIn("Chrome/150.0.4078.83", user_agent)

    def test_pace_payload_extracts_images_and_live_video_without_aggregate_duplicate(self):
        import json

        detail = {
            "awemeId": "123",
            "desc": "测试图集",
            "isSlides": True,
            "images": [
                {
                    "urlList": ["https://p3.douyinpic.com/a.webp", "https://p3.douyinpic.com/a.jpeg"],
                    "video": {"playAddr": [{"src": "https://v3.douyinvod.com/live-a"}]},
                },
                {"urlList": ["https://p3.douyinpic.com/static.webp", "https://p3.douyinpic.com/static.jpeg"]},
            ],
            "video": {"playAddr": [{"src": "https://v3.douyinvod.com/aggregate"}]},
        }
        flight = ["$", "component", None, {"aweme": {"detail": detail}}]
        chunk = f"self.__pace_f.push([1,{json.dumps('7:' + json.dumps(flight))}])"
        parsed = _extract_pace_detail([chunk], "123")
        self.assertEqual(parsed, detail)
        items = _items_from_pace_detail("source", "page", "测试图集", parsed)
        self.assertEqual([item.kind for item in items], ["video", "image"])
        self.assertTrue(items[0].selected)
        self.assertIn("Live", items[0].relationship_note)
        self.assertTrue(items[0].preview_url.endswith("a.jpeg"))
        self.assertTrue(items[1].media_url.endswith("static.jpeg"))
        self.assertNotIn("a.jpeg", [item.media_url for item in items])

    def test_pace_payload_keeps_multiple_live_videos_from_mixed_fields(self):
        detail = {
            "awemeId": "124",
            "isSlides": True,
            "images": [
                {
                    "urlList": ["https://p3.douyinpic.com/live-a.jpeg"],
                    "video": {"playAddr": [{"src": "https://v3.douyinvod.com/live-a.mp4"}]},
                },
                {
                    "url_list": ["https://p3.douyinpic.com/live-b.jpeg"],
                    "live_photo": {
                        "download_addr": {"url_list": ["https://v3.douyinvod.com/live-b.mp4"]},
                    },
                },
                {
                    "urlList": ["https://p3.douyinpic.com/live-c.jpeg"],
                    "livePhoto": {
                        "bitRate": [
                            {
                                "playAddrH264": {
                                    "urlList": ["https://v3.douyinvod.com/live-c.mp4"],
                                }
                            }
                        ]
                    },
                },
            ],
            "video": {"playAddr": [{"src": "https://v3.douyinvod.com/aggregate.mp4"}]},
        }

        items = _items_from_pace_detail("source", "page", "多项 Live 图", detail)

        self.assertEqual(
            [item.media_url for item in items],
            [
                "https://v3.douyinvod.com/live-a.mp4",
                "https://v3.douyinvod.com/live-b.mp4",
                "https://v3.douyinvod.com/live-c.mp4",
            ],
        )
        self.assertEqual(
            [item.preview_url for item in items],
            [
                "https://p3.douyinpic.com/live-a.jpeg",
                "https://p3.douyinpic.com/live-b.jpeg",
                "https://p3.douyinpic.com/live-c.jpeg",
            ],
        )

    def test_slides_page_uses_embedded_detail_instead_of_single_dom_fallback(self):
        import json

        item_id = "7672340039637230035"
        detail = {
            "awemeId": item_id,
            "isSlides": True,
            "images": [
                {"urlList": ["https://p3.douyinpic.com/one.jpeg"]},
                {"urlList": ["https://p3.douyinpic.com/two.jpeg"]},
            ],
            "video": {"playAddr": [{"src": "https://v3.douyinvod.com/aggregate.mp4"}]},
        }
        flight = ["$", "component", None, {"aweme": {"detail": detail}}]
        chunk = f"self.__pace_f.push([1,{json.dumps('7:' + json.dumps(flight))}])"
        page = {
            "title": "多项图集 - 抖音",
            "url": f"https://www.douyin.com/slides/{item_id}",
            "images": [],
            "videos": [],
            "network": [],
            "embedded": [chunk],
            "readyState": "complete",
            "bodyLength": 100,
            "unavailable": "",
            "challenge": False,
        }

        class FakeClient:
            def call(self, method, *_args, **_kwargs):
                if method == "Runtime.evaluate":
                    return {"result": {"value": json.dumps(page)}}
                return {}

            def close(self):
                return None

        # Bypass __init__: this is an in-memory parser test and must not bind a
        # real CDP port or create a browser profile.
        browser = PublicBrowser.__new__(PublicBrowser)
        with (
            patch.object(browser, "_page_client", return_value=FakeClient()),
            patch("public_browser.time.sleep", return_value=None),
        ):
            result = browser.parse_douyin(
                "https://v.douyin.com/example/",
                f"https://www.douyin.com/slides/{item_id}",
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [item.media_url for item in result.items],
            [
                "https://p3.douyinpic.com/one.jpeg",
                "https://p3.douyinpic.com/two.jpeg",
            ],
        )

    def test_normal_video_keeps_one_player_and_uses_public_cover(self):
        detail = {
            "awemeId": "456",
            "desc": "普通视频",
            "images": [
                {
                    "urlList": ["https://p3.douyinpic.com/repeated-cover.jpeg"],
                    "video": {"playAddr": [{"src": "https://v3.douyinvod.com/repeated-motion"}]},
                }
            ],
            "video": {
                "playAddr": [{"src": "https://v3.douyinvod.com/main-video"}],
                "originCover": {"urlList": ["https://p3.douyinpic.com/main-cover.jpeg"]},
            },
        }
        items = _items_from_pace_detail("source", "page", "普通视频", detail)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].media_url, "https://v3.douyinvod.com/main-video")
        self.assertEqual(items[0].preview_url, "https://p3.douyinpic.com/main-cover.jpeg")

    def test_normal_video_exposes_quality_choices_and_defaults_to_highest(self):
        high = "https://v3.douyinvod.com/high-1080.mp4"
        low = "https://v3.douyinvod.com/normal-720.mp4"
        detail = {
            "awemeId": "789",
            "video": {
                "playAddr": [{"src": low}],
                "bitRate": [
                    {"bitRate": 900_000, "gearName": "720p", "playAddr": {"urlList": [low]}},
                    {"bitRate": 2_500_000, "gearName": "1080p", "playAddr": {"urlList": [high]}},
                ],
            },
        }

        items = _items_from_pace_detail("source", "page", "有码率", detail)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].media_url, high)
        self.assertEqual([option["url"] for option in items[0].quality_options], [high, low])
        self.assertEqual(items[0].quality_label, "1080p")

    def test_finder_launch_can_find_homebrew_ffmpeg(self):
        with (
            patch("public_browser.shutil.which", return_value=None),
            patch("public_browser.os.path.isfile", side_effect=lambda path: path == "/opt/homebrew/bin/ffmpeg"),
            patch("public_browser.os.access", return_value=True),
        ):
            self.assertEqual(_ffmpeg_path(), "/opt/homebrew/bin/ffmpeg")

    def test_batch_reuses_one_browser_and_preserves_link_order(self):
        instances = []

        class FakeBrowser:
            def __init__(self):
                self.started = 0
                self.closed = 0
                self.links = []
                instances.append(self)

            def start(self):
                self.started += 1

            def close(self):
                self.closed += 1

            def parse_douyin(self, source_url, page_url=""):
                self.links.append(source_url)
                item_id = extract_douyin_item_id(source_url)
                return ParseResult(
                    source_url,
                    page_url,
                    item_id,
                    [MediaItem(source_url, page_url, f"https://v3.douyinvod.com/{item_id}", "video")],
                )

        links = [f"https://www.douyin.com/video/{item_id}" for item_id in ("111111111111111111", "222222222222222222", "333333333333333333")]
        with (
            patch.object(parser_assistant, "PublicBrowser", FakeBrowser),
            patch.object(parser_assistant, "fetch_url") as fetch_url,
            patch.object(parser_assistant, "parse_douyin_share_fallback", return_value=None),
            patch.object(parser_assistant, "enrich_previews_and_duplicates", return_value=None),
        ):
            progress_events = []
            result = parser_assistant.parse_payload({"links": links}, progress_events.append)

        self.assertTrue(result["ok"])
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].started, 1)
        self.assertEqual(instances[0].closed, 1)
        self.assertEqual(instances[0].links, links)
        self.assertEqual([entry["source_url"] for entry in result["results"]], links)
        self.assertEqual([event["completed"] for event in progress_events], [1, 2, 3])
        self.assertEqual([len(event["results"]) for event in progress_events], [1, 1, 1])
        fetch_url.assert_not_called()

    def test_official_iesdouyin_share_link_skips_redundant_resolution(self):
        target = "7670557704947897331"
        source = f"https://www.iesdouyin.com/share/video/{target}/?from_ssr=1"
        fallback = ParseResult(
            source,
            source,
            "官方分享页",
            [MediaItem(source, source, "https://v3.douyinvod.com/direct.mp4", "video")],
        )

        self.assertTrue(parser_assistant._is_direct_douyin_work_url(source))
        with (
            patch.object(parser_assistant, "fetch_url") as fetch_url,
            patch.object(parser_assistant, "parse_douyin_share_fallback", return_value=fallback),
            patch.object(parser_assistant, "PublicBrowser", side_effect=AssertionError("不应启动 Edge")),
            patch.object(parser_assistant, "enrich_previews_and_duplicates", return_value=None),
        ):
            result = parser_assistant.parse_payload({"links": [source]})

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"][0]["items"]), 1)
        fetch_url.assert_not_called()

    def test_direct_work_uses_official_share_fallback_before_fresh_browser(self):
        work_url = "https://www.douyin.com/video/7111111111111111111"
        instances = []

        class FakeBrowser:
            def __init__(self):
                instances.append(self)

            def start(self):
                pass

            def close(self):
                pass

            def parse_douyin(self, source_url, page_url=""):
                return make_parse_error(source_url, page_url, "DY-PAGE-SHELL", status="empty")

        fallback = ParseResult(
            work_url,
            work_url,
            "兜底成功",
            [MediaItem(work_url, work_url, "https://cdn.example.com/fallback.mp4", "video")],
            debug=["公开分享页"],
        )
        with (
            patch.object(parser_assistant, "PublicBrowser", FakeBrowser),
            patch.object(parser_assistant, "parse_douyin_share_fallback", return_value=fallback),
            patch.object(parser_assistant, "enrich_previews_and_duplicates", return_value=None),
        ):
            result = parser_assistant.parse_payload({"links": [work_url]})

        self.assertTrue(result["ok"])
        self.assertEqual(len(instances), 0)
        self.assertEqual(result["results"][0]["title"], "兜底成功")
        self.assertIn("公开分享页", result["results"][0]["debug"][0])

    def test_edge_start_failure_still_uses_public_story_fallback(self):
        work_url = "https://www.douyin.com/video/7668692124564635529"

        class FakeBrowser:
            def start(self):
                raise RuntimeError("公开解析浏览器启动超时")

            def close(self):
                pass

        fallback = ParseResult(
            work_url,
            work_url,
            "公开限时日常",
            [MediaItem(work_url, work_url, "https://cdn.example.com/story.mp4", "video")],
            debug=["公开分享页"],
            content_type="story",
            content_label="限时日常",
        )
        with (
            patch.object(parser_assistant, "PublicBrowser", FakeBrowser),
            patch.object(parser_assistant, "parse_douyin_share_fallback", side_effect=[None, fallback]),
            patch.object(parser_assistant, "enrich_previews_and_duplicates", return_value=None),
        ):
            result = parser_assistant.parse_payload({"links": [work_url]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["content_type"], "story")
        self.assertIn("Edge", result["results"][0]["debug"][0])

    def test_browser_start_tolerates_transient_debug_endpoint_failures(self):
        with patch("public_browser._free_port", return_value=54321):
            browser = PublicBrowser()
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.pid = 54322
        attempts = [OSError("503"), OSError("503"), {"webSocketDebuggerUrl": "ws://ready"}]

        with (
            patch("public_browser.os.path.exists", return_value=True),
            patch("public_browser.subprocess.Popen", return_value=process) as popen,
            patch("public_browser.time.sleep", return_value=None),
            patch.object(browser, "_request_json", side_effect=attempts) as request_json,
        ):
            browser.start()

        self.assertEqual(request_json.call_count, 3)
        command = popen.call_args.args[0]
        for option in (
            "--headless",
            "--guest",
            "--no-startup-window",
            "--disable-sync",
            "--disable-background-mode",
            "--use-mock-keychain",
        ):
            self.assertIn(option, command)
        self.assertNotIn("--headless=new", command)
        disabled_features = next(
            option for option in command if option.startswith("--disable-features=")
        )
        for feature in EDGE_DISABLED_FEATURES:
            self.assertIn(feature, disabled_features)
        self.assertEqual(command[-1], "about:blank")
        browser.close()
        self.assertFalse(browser.profile.exists())


if __name__ == "__main__":
    unittest.main()


