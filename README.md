# PDF 转 CBZ 漫画打包工具

一个带图形界面的 Windows 小工具：把漫画 PDF 一键转换为 CBZ 漫画包（Kindle、Kobo、Calibre 及各类漫画阅读器可直接打开）。

## 功能

- **PDF → CBZ**：无损提取 PDF 内嵌图片（保持原始格式 JPEG/PNG，不重编码），自动去重（xref 级 + 内容哈希级），打包为 CBZ（ZIP 不压缩，包内字节与源图完全一致）
- **多进程并行**：多个 PDF 同时转换，并行任务数可选（1/2/4/6/8，默认 6，匹配 6 物理核）。PyMuPDF 官方不支持多线程，因此采用多进程（每个进程独立打开 PDF），实测 4 路→8 路提速 1.70x
- **多文件 / 整个文件夹**：可一次添加多个 PDF，也可选择整个文件夹批量转换（可选递归扫描子文件夹）
- **批量重命名**：将生成的 CBZ 统一重命名为 `名称 v卷号.cbz` 格式，卷号自动从原文件名提取（支持 `Vol.01`、`_02`、`v03`、`第12卷` 等格式）或按顺序编号，实时预览、冲突检查
- **自动清理**：转换后自动删除中间图片，只保留 CBZ

## 使用

双击 `PDF2CBZ.exe`：

1. **添加 PDF**：点击「添加 PDF…」多选文件，或「添加文件夹…」选择整个目录（勾选「含子文件夹」可递归）
2. **选项**：确认输出目录；设置「并行任务数」；可选「保留中间图片」「覆盖已存在 CBZ」
3. **开始转换**：点击「开始转换」，日志区实时显示每卷进度与结果
4. **批量重命名**：转换完成后点击「批量重命名 CBZ…」，输入名称前缀，预览确认后执行

## 从源码运行 / 构建

依赖：`pymupdf`（Python 3.10+）

```bash
pip install pymupdf
python pdf2cbz_gui.py
```

打包为单文件 exe：

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name PDF2CBZ pdf2cbz_gui.py
```

产物位于 `dist/PDF2CBZ.exe`。

> 注意：程序使用多进程，打包时已包含 `multiprocessing.freeze_support()`（PyInstaller windowed 模式下多进程必需）。

## 说明

- 转换逻辑使用 `page.get_images()` + `doc.extract_image()`，直接取出 PDF 内的原始编码字节，**无损且保持原格式**；跨页复用的图片对象与内容重复的图片均只保留一份
- CBZ 使用 ZIP_STORED（不压缩）模式——图片本身已是压缩格式，再压缩无收益且更慢
- 并行采用多进程而非多线程：PyMuPDF 底层 MuPDF 不保证线程安全，每个进程独立打开文件是官方推荐方式

## 许可证

[MIT](LICENSE)
