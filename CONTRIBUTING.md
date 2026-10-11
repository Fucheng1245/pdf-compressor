# 参与贡献

欢迎提交 Bug、兼容性反馈和改进 PR。开发使用 Python 3.10+；Finder 和原生进度窗需要 macOS。

## 开发环境

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python '.[test]'
.venv/bin/python -m pytest -q
```

在 macOS 编译原生进度窗：

```bash
clang -O2 -fobjc-arc -fblocks -framework Cocoa \
  src/pdfcompressor/native/Progress.m -o /tmp/PDFCompressorProgress
```

重新运行 `scripts/install.py` 可更新本机安装。`--home <测试目录> --workflows-only --python <解释器路径>` 可生成隔离测试工作流，不修改当前用户 Finder 偏好。

## 提交前

- 为改变的行为提供有意义的测试，运行现有回归测试。
- 图片解码、滤镜或遮罩改动应检查透明度、共享遮罩、文字和页面渲染。
- 提速应保持三档参数，给出相同输入上的重复测量，以及输出或像素一致性检查。
- 使用程序生成的匿名 PDF 作为测试样本；私人 PDF、日志、绝对个人路径和测量产物不提交。
- 新增依赖或修改 pypdf 内部 API 时说明原因，并更新相关文档。

PR 应说明问题、最终行为和验证结果。菜单兼容性反馈请附 macOS 版本；目前实际验收环境是 macOS 14.6，其他版本的 CI 不等于 Finder GUI 验收。
