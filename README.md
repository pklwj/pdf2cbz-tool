# PDF / MOBI / EPUB 转 CBZ 漫画打包工具

把图片型（扫描）PDF、Kindle 的 MOBI / AZW / AZW3 漫画书、以及 EPUB 电子书，一键转成 CBZ 漫画包，
Kindle / Kobo / Calibre / 各类漫画阅读器可直接打开。
带 tkinter 图形界面，可打包成免安装的单文件 exe。

**免安装版**：[下载 PDF2CBZ.exe](https://github.com/pklwj/pdf2cbz-tool/releases/latest/download/PDF2CBZ.exe)
（Windows x64，约 48 MB）。未做代码签名，首次运行可能被 SmartScreen 拦，点「更多信息 → 仍要运行」；
onefile 每次启动都要解包，慢 2~3 秒属正常。

## 特性

- **无损、保原格式**：PDF 走 `page.get_images()` + `doc.extract_image()` 直接取内嵌图片的原始编码字节；
  MOBI 直接搬记录字节。两条路径都不重编码、不重采样
- **三层去重**：PDF 是 xref 级（同一图片对象被多页复用）+ 内容 MD5 级；
  MOBI 是内容 MD5 级（有些书把同一张图存两遍）；EPUB 按资源路径（同一文件被多页引用只取一次）
- **压缩方式按格式逐张选**：jpg/png/webp/jpx 等已压缩格式 `ZIP_STORED` 原样存；bmp/tiff 等裸位图 `ZIP_DEFLATED`，避免 CBZ 比原文件还大
- **原子写入**：先写 `.part`，成功后 `os.replace` 换名，中断不会留下半截能打开的 CBZ
- **多进程并行**：MuPDF 不是线程安全的，所以走多进程（每进程独立开文件），任务按文件分派
- **单文件也能吃多核**：PDF 页数 ≥ 40 时自动按页切段并行，完成后合并（见下方实测）；
  MOBI / EPUB 的图是成品字节直拷，切片只会多一次合并，所以不切
- **转换前先报图片编码**：只读索引扫一遍，提示这次是「字节直拷，快」还是「解压重编码，慢」，不用等转完才知道
- **可中途停止**：真中止而不是卡住 —— terminate 后 0.14 s 收尾、子进程归零、临时文件当场清掉
- **批量重命名**：卷号自动提取（`Vol.12` / `v03` / `第7卷` / `12巻` / `#09`），两步改名避免链式覆盖，冲突整体拦截

## MOBI 这条路怎么走

MOBI 是 PalmDB 容器：78 字节文件头 + 记录表，记录 0 装 PalmDOC 头与 MOBI 头，图片原样占满整条记录。
容器解析交给 [pymobi](https://pypi.org/project/pymobi/)，但它的 `unpackMobi()` 只导出「正文引用到的图 + 封面」，
漫画书里没被 HTML 引用的页会整批漏掉，`imageExt()` 还会在 3 字节的短记录上抛 struct.error。
所以 `mobi.py` 只借它的字段表，图片自己按魔数逐条记录嗅探，另外处理两个实测坑：

- **尾部 NUL 填充**：三卷样本 200 张 JPEG 里 148 张的 EOI 之后还挂着 0~2 个 `0x00`（kindlegen 补齐用），
  必须裁掉；否则部分阅读器报错、CBZ 也白白变大。只裁 NUL，图像字节一个不动
- **缩略图不是正文页**：EXTH 202 指向封面缩略图（样本里是 153×240），按规范丢弃；
  留 3 张以上才丢，免得「只有封面 + 缩略图」的书被清空
- **DRM 一律拒**：PalmDOC 头 `encryptionType != 0` 直接报错。本工具只搬字节，不解密

字段偏移的来源：MOBI 头从未官方公开，`pymobi` 的字段表与
[MobileRead Wiki](https://wiki.mobileread.com/wiki/MOBI) 一致，并用三卷真实漫画书逐项核对过。

## EPUB 这条路怎么走

EPUB 也是个 ZIP，结构三层：`META-INF/container.xml` 指明 OPF，OPF 的 `<manifest>` 列资源、
`<spine>` 定阅读顺序，每页 XHTML 里的 `<img>` 指向真正的图片。

取图顺序 = **spine 页序优先，再补 manifest 里没被任何页引用的图**（封面、版权图常常只在 manifest 里挂着）。
这样漫画书拿到的是翻页顺序，而不是文件名顺序。

三个细节：

- **按资源路径去重**，不按字节。同一文件被多页引用时 href 相同，天然只取一次；不同路径但字节相同的
  情况极少，那时保两页更安全（按字节去重会少一页）
- **DRM 判定**：只拒「`META-INF/encryption.xml` 里点名加密了某张图」的书。字体混淆（Adobe / IDPF）
  也会写进 encryption.xml，那是常态，不算 DRM
- **XHTML 不严格时回退正则**：部分书商的 XHTML 不是合法 XML，ElementTree 解析失败就退回抓 `src` / `xlink:href`
- **href 里的 `?查询串` 和 `#锚点` 都会剥掉**。只剥锚点漏掉查询串时，那一页的图会掉到「补漏」队列末尾——
  实测能把页序 `[1,2]` 变成 `[2,1]`，对漫画是硬伤
- **container.xml 的 `full-path` 大小写不符或写成反斜杠时也认**，兜底按「最浅的 .opf」选，
  避免挑到书里夹带的模板 OPF（实测会变成「0 张图」）
- **DRM 判定两侧都做归一化**（大小写/反斜杠/前导斜杠/属性名 `URI` 或 `uri`）：同一本书既然容忍 href
  大小写，加密判定就必须一视同仁，否则会出现「图取得到、加密查不出」，把密文当图片写进 CBZ

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

打包用的 Python **必须带 tkinter**（精简版运行时不自带）。打完务必跑一次
`dist\PDF2CBZ.exe --selftest`：冻结环境最容易漏的是 `pymobi` 这类函数内按需导入的模块，
自检会真的转一本 PDF / EPUB / MOBI，漏了会直接 FAIL 而不是等用户撞上。

程序依赖 PyMuPDF、pymobi 与 tkinter。前两者由 `requirements.txt` 安装；tkinter 随官方 Python 安装包提供，
精简版 Python（部分内置运行时）不自带，缺失时 `import tkinter` 会报错。
pymobi 缺失时 MOBI 功能报「pip install pymobi」，PDF 功能不受影响（导入是按需的）。

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | GUI 与唯一启动入口；线程间通过队列传事件（Tk 非线程安全） |
| `extractor.py` | 图片提取总入口：只按扩展名分派 PDF / MOBI / EPUB，不含任何提取实现（27 行） |
| `pdf_images.py` | PDF 内嵌图片提取：xref + MD5 双层去重，流式产出 |
| `mobi.py` | MOBI / AZW / AZW3 内嵌图片提取：pymobi 解析容器 + 逐记录嗅探 |
| `epub.py` | EPUB 内嵌图片提取：标准库解 ZIP，按 OPF spine 阅读顺序取图 |
| `probe.py` | 源文件体检：页数/张数/编码分布/能否切段，任务层与界面层只问它 |
| `archive.py` | CBZ 打包：压缩方式选择、原子写入、页段合并、换名重试 |
| `batch.py` | 批量任务：单文件 worker、页段 worker、目录扫描、残留清理（不认识具体格式） |
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

MOBI / EPUB 没有解码开销（记录/条目里就是成品图）。三卷 MOBI（32.2 / 34.8 / 27.8 MB，
200 / 194 / 194 张）走完整流水线（3 进程并行 + 打包）：

| 输入 | 产物 | 张数 | 耗时 |
|---|---|---|---|
| 卷01.mobi | 32.1 MB CBZ | 200 | 2.6 s（三卷合计） |
| 卷02.mobi | 34.7 MB CBZ | 194 | 同上 |
| 卷03.mobi | 27.7 MB CBZ | 194 | 同上 |

产物逐条 CRC 通过；200 张里抽 3 张（含 GIF、末页封面）与源记录做了**逐字节比对，200/200 一致**。

真实漫画 EPUB（《晚安，布布》卷 01，83.8 MB、224 页）：探测 224 张、实际产出 224 张、
CBZ 85.6 MB（单位同工具显示，按 1024 计），耗时 **0.44 s**，条目与源 ZIP 逐字节比对 **224/224 一致**，首图即封面。

## 与上游 v1.4.1 的差异

本仓库是单文件版 `pdf2cbz_gui.py` 的模块化重构，`upstream_v1.4.1.py` 原件留档用于对照。
36 个函数/方法里 **21 个函数体逐字未改**，只换了所在模块；逐一对比见 [CHANGES.md](CHANGES.md)，可重新生成：

```bash
python gen_changes.py
```

主要改动：

- `.part` 临时名带 pid，修同名任务并发撞 Windows 文件锁（`WinError 32 / 5` 双方全败）
- `os.replace` 遇占用退避重试，修并发换同一目标名、杀软或索引器占用刚写好的文件
- 单文件按页切段并行（4 倍提速），跨段内容去重后重新编号，产物与单进程一致
- 重命名执行段从对话框抽出为 `rename.execute_renames()`，可单测
- GUI 改用队列传递事件；修正选项行控件在 780 宽下被挤出可视区
- 新增 MOBI / AZW / AZW3（`mobi.py`）与 EPUB（`epub.py`）输入，PDF 路径行为不变
- 新增 `probe.py` 统一「源文件体检」，任务层不再认识具体格式
- 内部接口改名以便同时服务两种源：`scan_pdfs→scan_inputs`、`convert_pdf_to_cbz→convert_file`、
  `ConvertJob/RangeJob.pdf_path→src_path`。行为不变，但外部调用方需跟随（上游单文件版没有这些名字）
- 新增测试与差异报告（上游没有）

单文件原版的提交保留在 `pre-split` 分支。

## 测试

```bash
python selftest.py     # 95 项：提取/打包/分段等价/并发/重命名/清理/MOBI+EPUB 分支
python e2e_gui.py      # 4 条 GUI 端到端：完整转换、中途停止、单文件分段、MOBI 输入
PDF2CBZ.exe --selftest # 冻结环境自检：PDF 转换 + 分段合并 + EPUB + MOBI 各跑一遍
                       # 结果写 %TEMP%\pdf2cbz_selftest.txt，退出码 0 = 通过
```

`selftest.py` 里有一项会拿 `upstream_v1.4.1.py` 跑同一输入，比对产物 `(文件名, 大小, CRC)` 是否逐条相等。
MOBI 用例用 `make_mobi()` 手搓的最小书覆盖分支（DRM / 无图 / 重复图 / 缩略图 / 尾部填充 / 合体书），
真实文件的结构由上面那三卷漫画书端到端验证。缺 pymobi 时 MOBI 用例记 **SKIP**，不算通过——
避免「没装依赖」被当成「跑过了」。

## 已知限制

- **PDF 只支持图片型**。纯文字或纯矢量 PDF 会报「未提取到任何图片」，不做栅格化兜底
- **MOBI 不处理 DRM**。加密书直接拒绝；Topaz（`.tpz`）是另一套格式，不支持
- **AZW3 缺真实样本验证**：三卷实测样本都是 MOBI6+KF8 合体书；纯 KF8（`.azw3`）只有合成样本覆盖，
  按同一套「逐记录嗅探」逻辑处理，但**未用真实文件验证**
- **`.prc` 只认带 MOBI 头的**：纯 PalmDOC（无 MOBI 头）会明确报错，不会当成电子书硬解
- **EPUB 不重排**：按 spine 顺序输出，不识别「从右往左」之类的阅读方向标记
- **EPUB 的 `.kepub.epub`** 同样按 `.epub` 处理，不做 Kobo 特有处理
- **MOBI 只取图片记录**。书里未被引用的图片也会一并取出（漫画书这是优点），不会按阅读顺序重排
- **保原格式的兼容性代价**：若源图是 JPX / JBIG2 / BMP / TIFF，会原样进包，部分阅读器不认
- **CBZ 可能比源文件大**：FlateDecode 的 PDF 转成 PNG 后通常大 20%~30%（上例 275 MB → 357 MB）
- 一页多图（分栏 / 分层）时按资源字典顺序输出，不保证视觉 z-order

## 许可

本仓库代码 MIT，见 [LICENSE](LICENSE)。

MOBI 路径使用 [pymobi](https://pypi.org/project/pymobi/)（GPLv3）解析容器。它是独立的第三方依赖，
不装则 MOBI 功能不可用、PDF 功能照常。若要分发含 MOBI 功能的 exe，注意 pymobi 的 GPLv3 约束。
