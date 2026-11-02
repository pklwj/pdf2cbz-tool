# PDF 转 CBZ 漫画打包工具

把图片型（扫描）PDF 一键转成 CBZ 漫画包，Kindle / Kobo / Calibre / 各类漫画阅读器可直接打开。
带 tkinter 图形界面，可打包成免安装的单文件 exe。

## 特性

- **无损、保原格式**：`page.get_images()` + `doc.extract_image()` 直接取 PDF 内嵌图片的原始编码字节，不重编码、不重采样
- **双层去重**：xref 级（同一图片对象被多页复用）+ 内容 MD5 级（不同 xref 但字节相同）
- **压缩方式按格式逐张选**：jpg/png/webp/jpx 等已压缩格式 `ZIP_STORED` 原样存；bmp/tiff 等裸位图 `ZIP_DEFLATED`，避免 CBZ 比原 PDF 还大
- **原子写入**：先写 `.part`，成功后 `os.replace` 换名，中断不会留下半截能打开的 CBZ
- **多进程并行**：MuPDF 不是线程安全的，所以走多进程（每进程独立开 PDF），任务按文件分派
- **单文件也能吃多核**：页数 ≥ 40 时自动按页切段并行，完成后合并（见下方实测）
- **可中途停止**：正在处理的文件被中止，已完成的不受影响
- **批量重命名**：卷号自动提取（`Vol.12` / `v03` / `第7卷` / `12巻` / `#09`），两步改名避免链式覆盖，冲突整体拦截

## 快速开始

```bash
pip install -r requirements.txt
python main.py
```

打包成单文件 exe（与上游参数一致）：

```bash
pyinstaller --noconfirm --onefile --windowed --name PDF2CBZ main.py
# 产物：dist/PDF2CBZ.exe
```

程序依赖 PyMuPDF 与 tkinter。PyMuPDF 由 `requirements.txt` 安装；tkinter 随官方 Python 安装包提供，
精简版 Python（部分内置运行时）不自带，缺失时 `import tkinter` 会报错。

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | GUI 与唯一启动入口；线程间通过队列传事件（Tk 非线程安全） |
| `extractor.py` | PDF 图片无损提取，流式产出，双层去重 |
| `archive.py` | CBZ 打包：压缩方式选择、原子写入、页段合并、换名重试 |
| `batch.py` | 批量任务：单文件 worker、页段 worker、目录扫描、残留清理 |
| `rename.py` | 批量重命名纯逻辑：卷号识别、计划、冲突检测、执行 |

## 性能

同一台机器（12 逻辑核；并行度默认取 `cpu_count() // 2` 并向下取整到 1/2/4/6/8 档，本机为 6），
212 页 / 275 MB、全部 FlateDecode 的扫描件：

| 方式 | 耗时 | 产物 |
|---|---|---|
| 单进程逐个提取 | 130.5 s | 211 张，356.9 MB |
| 按页切 6 段并行 + 合并 | **34.0 s**（分段 33.1 + 合并 0.9） | 211 张，356.9 MB，逐条 CRC 一致 |

耗时几乎全在 PyMuPDF 把 FlateDecode 解压再编码成 PNG 上（约 562 ms/张）。
若 PDF 内是 JPEG（DCTDecode），`extract_image` 直接拷贝字节，实测 7.9 ms/张 —— 相差 71 倍，
所以慢不慢取决于源 PDF 的图像编码方式，而非体积。

## 与上游 v1.4.1 的差异

本仓库是单文件版 `pdf2cbz_gui.py` 的模块化重构，`upstream_v1.4.1.py` 原件留档用于对照。
36 个函数/方法里 **26 个函数体逐字未改**，只换了所在模块；逐一对比见 [CHANGES.md](CHANGES.md)，可重新生成：

```bash
python gen_changes.py
```

主要改动：

- `.part` 临时名带 pid，修同名任务并发撞 Windows 文件锁（`WinError 32 / 5` 双方全败）
- `os.replace` 遇占用退避重试，修并发换同一目标名、杀软或索引器占用刚写好的文件
- 单文件按页切段并行（4 倍提速），跨段内容去重后重新编号，产物与单进程一致
- 重命名执行段从对话框抽出为 `rename.execute_renames()`，可单测
- GUI 改用队列传递事件；修正选项行控件在 780 宽下被挤出可视区
- 新增测试与差异报告（上游没有）

单文件原版的提交保留在 `pre-split` 分支。

## 测试

```bash
python selftest.py     # 44 项：提取/打包/分段等价/并发/重命名/清理
python e2e_gui.py      # 3 条 GUI 端到端：完整转换、中途停止、单文件分段
PDF2CBZ.exe --selftest # 验证打包成 exe 后 PyMuPDF 与多进程仍可用（写 %TEMP%\pdf2cbz_selftest.txt）
```

`selftest.py` 里有一项会拿 `upstream_v1.4.1.py` 跑同一输入，比对产物 `(文件名, 大小, CRC)` 是否逐条相等。

## 已知限制

- **只支持图片型 PDF**。纯文字或纯矢量 PDF 会报「未提取到任何图片」，不做栅格化兜底
- **保原格式的兼容性代价**：若源图是 JPX / JBIG2 / BMP / TIFF，会原样进包，部分阅读器不认
- **CBZ 可能比 PDF 大**：FlateDecode 源转成 PNG 后通常大 20%~30%（上例 275 MB → 357 MB）
- 一页多图（分栏 / 分层）时按资源字典顺序输出，不保证视觉 z-order

## 许可

MIT，见 [LICENSE](LICENSE)。
