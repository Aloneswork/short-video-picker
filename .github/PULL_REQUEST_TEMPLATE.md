## 改动内容

<!-- 简要说明改了什么以及为什么。 -->

## 用户影响

<!-- 说明行为、兼容性、隐私或发布产物是否变化。 -->

## 验证

- [ ] `python -m pytest -q -p no:cacheprovider`
- [ ] Python 与 JavaScript 语法检查
- [ ] `python scripts/sync_release_metadata.py --check`
- [ ] `./scripts/check_source_clean.sh`
- [ ] 涉及界面时已附不含私人数据的截图

## 安全与数据

- [ ] 未提交 Cookie、令牌、个人路径、日志、诊断包或真实用户媒体
- [ ] 新增依赖或网络行为已在说明中列出
