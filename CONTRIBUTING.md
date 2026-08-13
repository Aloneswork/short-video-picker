# 贡献指南

感谢你愿意帮助改进“视频资源整理”。小到错别字、复现信息和解析失败样本，大到代码修复，都很有价值。

## 从哪里开始

- 使用问题或可复现的缺陷：提交 [Bug report](https://github.com/Aloneswork/short-video-picker/issues/new?template=bug_report.yml)。
- 新功能或交互改进：提交 [Feature request](https://github.com/Aloneswork/short-video-picker/issues/new?template=feature_request.yml)。
- 尚未成形的想法、经验和一般问题：使用 [Discussions](https://github.com/Aloneswork/short-video-picker/discussions)。
- 安全漏洞：不要创建公开 Issue，请按 [SECURITY.md](SECURITY.md) 私下报告。

提交公开内容前，请移除 Cookie、访问令牌、个人路径、真实姓名、联系方式和不适合公开的分享链接。不要上传无权公开的媒体文件。

## 本地开发

要求 macOS 12+、Python 3.13 和 Node.js 22（仅用于 JavaScript 语法检查）：

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
python -m py_compile app.py app_config.py app_logging.py core.py desktop.py \
  parser_assistant.py preview_cache.py public_browser.py
sed -n '484,1353p' index.html | node --check -
python scripts/sync_release_metadata.py --check
./scripts/check_source_clean.sh
```

开发运行：

```sh
python desktop.py
```

构建自包含应用：

```sh
PYTHON_BIN="$PWD/.venv/bin/python" ./build_macos_app.sh
```

## Pull Request

1. 从 `main` 创建分支。
2. 每个 PR 聚焦一个可审阅的目标，并说明原因和用户影响。
3. 为行为变化补充或更新测试。
4. 确认上述检查通过；涉及界面时附上不含私人数据的截图。
5. 不提交 `vendor/`、`.app`、ZIP、日志、缓存、诊断包或真实用户数据。

提交贡献即表示你有权提交相关内容，并同意按项目的 [MIT License](LICENSE) 发布。
