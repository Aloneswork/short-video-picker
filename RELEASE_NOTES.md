# 视频资源整理 v2.8.0（Build 26）

发布日期：2026-08-12

## 本版结果

- macOS 应用改为 PyInstaller 自包含构建，内置 Python 3.13、Pillow、pywebview 与运行依赖，不再依赖目标机器的 Python。
- 修复 pytest 自己生成 `__pycache__` 导致套件自毁的问题；关闭 pytest cache，并把源码洁净度改为发布前独立门禁。
- 解析记录上限从 100 提升到 1000，可导出 CSV/TXT；前后端批量上限改由 `version.json` 单一配置提供。
- 增加多码率清晰度选择，默认最高码率；增加稳定 partial 文件断点续传和失败项重试。
- 缩略图从 base64 data URL 改为 128 MB/7 天的本地缓存和随机令牌回环 URL，降低子进程与 WebView 的 JSON 体积。
- 增加轮转脱敏日志、运行环境诊断和可导出的诊断包；明确 Edge、ffmpeg 缺失时的降级行为。
- 删除前端未使用的同步 `parse_links` 遗留接口，收紧桌面桥接字段、URL、资源类型、请求头、清晰度选项和目标目录校验。
- 删除无用的 Bottle CLI 副本；Bottle 库本身仍是 pywebview 本地服务的真实运行依赖。
- 版本、Build、公共上限由 `version.json` 统一管理；构建时写入应用 `Info.plist`。
- 保留原有 CDP 与抖音专用解析结构：本版不引入 Playwright，不抽象多平台适配器。

## 发行状态

- 完整 Python 回归测试 82 项通过；Python 编译、JavaScript 语法、版本同步和 plist 校验通过。
- 正式包的内置运行时、解析子进程、随机令牌 `127.0.0.1` 缩略图服务、ZIP 完整性和实际 GUI 启动均已烟测通过。
- 清理无关可选依赖后，arm64 应用约 36 MB，分发 ZIP 约 16 MB。
- 默认产物使用 ad-hoc 临时签名，并执行 `codesign --verify --deep --strict`。
- 当前构建机没有 Developer ID 身份，因此当前产物未做 Apple 公证；构建脚本已提供 Developer ID 和 `notarytool`/`stapler` 的真实入口。
- 发行验证项和最终测试数字以本次实际构建完成后的输出为准，不预先写入虚假结果。

正式应用位于 `dist.nosync/视频资源整理.app`，传输包为
`dist.nosync/视频资源整理-v2.8.0-macOS-arm64.zip`。
