# 视频资源整理 v2.8.3（Build 29）

发布日期：2026-08-13

## 本版修复

- **修复打包版启动白屏**：PyInstaller 应用包内符号链接导致 WebKit 无法加载页面，现已在加载前解析到真实文件。
- 解析失败时的来源诊断（页面地址、数据来源数量）直接显示在错误提示中，便于反馈定位。
- 更新仓库界面截图。

## 本版验证

- Python 完整回归测试：72 项通过。
- 构建产物不依赖 Anaconda；ZIP 完整性与 SHA-256 校验通过；codesign 验证通过。
- 实际打开 `.app` 验证主界面正常渲染。

## 发行说明

- 自包含应用：内置 Python 运行时，不需要用户安装 Python。
- 仅支持 Apple Silicon（arm64）；最低 macOS 12；Windows 版本暂未提供。
- ad-hoc 临时签名，尚未 Apple 公证；其他 Mac 首次打开若提示未识别开发者，请右键选择“打开”。
- 校验方法：`shasum -a 256 视频资源整理-v2.8.3-macOS-arm64.zip`，与 Release 附带的 `SHA256SUMS.txt` 比对。
