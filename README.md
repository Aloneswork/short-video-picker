# 短视频批量解析

用于公开抖音分享链接的本地 macOS 解析与下载工具。当前发行版：**v2.8.0（Build 26）**。

## 运行与分发

双击正式应用即可运行，不要求另一台 Mac 预装 Python：

```text
dist.nosync/视频资源整理.app
```

对外传输使用 `dist.nosync/视频资源整理-v2.8.0-macOS-arm64.zip`，SHA-256 记录在
`dist.nosync/SHA256SUMS.txt`。当前构建面向 Apple Silicon（arm64）和 macOS 12 及以上。

开发环境仍可运行 `python3 desktop.py`，但这不是分发方式。`start.command` 会优先打开正式应用，仅在没有应用包时回退到开发环境 Python。

## 主要功能

- 单批最多 30 个输入链接或主页作品；上限由 `version.json` 统一提供给 Python 和前端。
- 优先使用抖音未登录公开分享页；必要时使用隔离、无登录、无 Cookie 的 Microsoft Edge 临时会话兜底。
- 支持普通视频、图集、Live 图、`/slides/` 和匿名网页可见的“限时日常”，保留明确错误码和单条重新解析。
- 普通视频返回多个公开码率时可选择清晰度，默认选择可识别的最高码率。
- 视频下载使用稳定 `.part` 文件断点续传；服务端不支持 Range 时自动完整重下。批量下载失败项可直接重试。
- 图片保存为 JPG、视频保存为 MP4；同名文件用 SHA-256 判断内容重复，同名不同内容会安全改名。
- 缩略图保存在本机缓存并通过随机令牌的 `127.0.0.1` 地址展示，不再把数 MB base64 数据经子进程和 WebView 往返传输。缓存上限 128 MB，超过 7 天自动清理。
- 本机保留最近 1000 条解析记录，可搜索、筛选并导出 CSV 或 TXT。
- 顶栏“运行诊断”检查内置 Python、Pillow、WebView、Edge 和 ffmpeg，可导出脱敏诊断包。
- 桌面桥接只接受 `MediaItem` 白名单字段、`http/https` 媒体地址、视频/图片类型和有限请求头。

## 运行依赖与降级行为

正式 `.app` 已内置 Python、Pillow、pywebview 和所需 macOS 框架绑定。

- Microsoft Edge 是复杂作品页面的可选兜底。没有 Edge 时仍尝试官方公开分享页，但覆盖率可能下降。
- ffmpeg 仅用于“作品没有公开封面时”的视频首帧预览和部分图片指纹兜底。没有 ffmpeg 不影响媒体解析和保存，但这类预览可能为空。
- Pillow 已随应用打包，不再依赖 macOS `sips` 或用户自行安装。

应用界面由 pywebview 提供。pywebview 为本地 HTML 启动随机端口的回环服务，缩略图缓存也使用独立的随机令牌回环服务；二者都只绑定 `127.0.0.1`，不对局域网或公网开放。

## 日志、历史与缓存

- 日志：`~/Library/Logs/视频资源整理/app.log`，1 MB 轮转，保留 5 个备份；URL 查询参数和无关路径会脱敏。
- 历史：`~/Library/Application Support/视频资源整理/parse_history.sqlite3`。
- 缩略图：`~/Library/Caches/视频资源整理/previews`。

诊断包只包含运行环境摘要和脱敏日志，不包含 Cookie、浏览器配置或媒体文件。

## 构建、签名与公证

从 GitHub 干净检出后，使用 Python 3.13 创建独立环境：

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
PYTHON_BIN="$PWD/.venv/bin/python" ./build_macos_app.sh
```

`vendor/` 只是本机可选的依赖镜像，不会上传 GitHub；干净检出使用
`requirements-macos.txt` 中锁定的依赖。发行包和校验值通过 GitHub Releases 或其他二进制分发渠道交付，不提交到源码历史。

```sh
./build_macos_app.sh
```

构建使用 Python 3.13 与 PyInstaller，把解释器和运行依赖收进应用；也不会把 Windows/Android WebView 库、依赖测试、`vendor/bin/bottle.py`、缓存或日志放入发布包。`version.json` 是版本号、Build 和公共上限的单一来源；构建前由 `scripts/sync_release_metadata.py --check` 校验 README/RELEASE_NOTES，构建时写入应用 `Info.plist`，源码中的 `macos/Info.plist` 只保留模板值。

默认只做 ad-hoc 临时签名和严格完整性校验。当前机器没有可用 Developer ID，因此当前产物**未经过 Apple 公证**。如已配置证书与钥匙串公证配置，可使用：

```sh
SIGN_IDENTITY="Developer ID Application: ..." NOTARY_PROFILE="profile-name" ./build_macos_app.sh
```

脚本仅在 `notarytool --wait`、`stapler staple` 和 `stapler validate` 全部成功后才报告“已公证”。

## 安全与范围

工具只读取公开网页已经返回的资源，不登录、不导入 Cookie，也不绕过私密、好友、付费或已删除内容。请只保存你拥有、已获授权，或平台规则和当地法律允许保存的内容。

本版没有引入 Playwright，也没有抽象多平台适配器；现有 CDP 与抖音解析结构保持不变。

## 仓库授权

当前 GitHub 仓库为私有协作仓库，项目自身源码未授予开源许可，保留所有权利。直接依赖的许可信息见 `THIRD_PARTY_NOTICES.md`。
