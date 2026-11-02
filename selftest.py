# -*- coding: utf-8 -*-
"""重构等价性验证：新模块 vs 上游单文件，产物逐条比对。"""
import os
import sys
import zipfile
import importlib.util
import tempfile
import shutil
import multiprocessing

PROJ = os.path.dirname(os.path.abspath(__file__))
ORIG = os.environ.get("PDF2CBZ_ORIG", os.path.join(PROJ, "upstream_v1.4.1.py"))
sys.path.insert(0, PROJ)


def load_upstream():
    """载入上游单文件版，用于等价性比对。找不到就返回 None（跳过该组用例）。"""
    if not ORIG or not os.path.exists(ORIG):
        return None
    import types
    if "tkinter" not in sys.modules:
        # 精简版 Python 可能没 tkinter，而上游文件顶层就 import 它：塞桩绕开
        try:
            import tkinter  # noqa: F401
        except ModuleNotFoundError:
            for _n in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
                sys.modules[_n] = types.ModuleType(_n)
            sys.modules["tkinter"].Tk = object
            sys.modules["tkinter"].Toplevel = object
            for _n in ("ttk", "filedialog", "messagebox"):
                setattr(sys.modules["tkinter"], _n, sys.modules["tkinter." + _n])
    spec = importlib.util.spec_from_file_location("orig_pdf2cbz", ORIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

import pymupdf
import archive
import batch
import extractor
import rename


def make_pix(color, fmt, w=80, h=80):
    px = pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(color) * (w * h), 0)
    return px.tobytes(fmt)


def make_pdf(path):
    """3 页：p1=jpegA+pngB, p2=jpegA(重复,测内容级去重), p3=pngC。"""
    a = make_pix((255, 0, 0), "jpg")
    b = make_pix((0, 128, 0), "png")
    c = make_pix((0, 0, 255), "png")
    doc = pymupdf.open()
    for streams in ([a, b], [a], [c]):
        page = doc.new_page(width=400, height=400)
        for i, s in enumerate(streams):
            page.insert_image(pymupdf.Rect(20 + i * 100, 20, 180 + i * 100, 180), stream=s)
    doc.save(path)
    doc.close()
    return a, b, c


def zip_fingerprint(path):
    """(文件名, 大小, CRC) 列表 —— 忽略 zip 内嵌时间戳。"""
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.file_size, i.CRC) for i in z.infolist()]


def run():
    tmp = tempfile.mkdtemp(prefix="p2cbz_test_")
    fails = []
    tally = {"pass": 0, "fail": 0}

    def check(name, cond, detail=""):
        print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + str(detail)))
        tally["pass" if cond else "fail"] += 1
        if not cond:
            fails.append(name)

    pdf = os.path.join(tmp, "Vol 01.pdf")
    a, b, c = make_pdf(pdf)

    # --- 1. 提取：jpegA 在 p1/p2 出现两次，png 两张，共 3 张唯一 ---
    got = list(extractor.iter_unique_images(pdf))
    check("extractor 去重后 3 张", len(got) == 3, len(got))
    check("extractor 保原格式(jpg/png)",
          [e for e, _ in got] == ["jpeg", "png", "png"], [e for e, _ in got])
    check("extractor 字节与源图一致", got[0][1] == a, "jpeg 不一致")

    # --- 2. 打包：压缩方式按格式选 ---
    d_new = os.path.join(tmp, "new")
    os.makedirs(d_new)
    job = batch.ConvertJob(pdf_path=pdf, out_dir=d_new, keep_images=False, overwrite=False)
    p, ok, msg = batch.convert_pdf_to_cbz(job)
    check("batch 单文件转换成功", ok, msg)
    cbz_new = os.path.join(d_new, "Vol 01.cbz")
    check("CBZ 落盘", os.path.exists(cbz_new))
    check("无 .part 残留", not any(n.endswith(".part") for n in os.listdir(d_new)),
          os.listdir(d_new))
    with zipfile.ZipFile(cbz_new) as z:
        types = {i.filename: i.compress_type for i in z.infolist()}
    check("jpeg 用 STORED", types.get("img_001.jpeg") == zipfile.ZIP_STORED, types)
    check("png 用 STORED", types.get("img_002.png") == zipfile.ZIP_STORED, types)
    check("bmp/tiff 走 DEFLATE", archive.zip_compress_type("bmp") == zipfile.ZIP_DEFLATED
          and archive.zip_compress_type("TIFF") == zipfile.ZIP_DEFLATED)
    check("未知格式走 DEFLATE", archive.zip_compress_type("weird") == zipfile.ZIP_DEFLATED)

    # --- 2b. 分段并行：切两段再合并，必须与单进程产物一模一样 ---
    d_split = os.path.join(tmp, "split")
    os.makedirs(d_split)
    p1 = os.path.join(d_split, "Vol 01.cbz.p00000.part")
    p2 = os.path.join(d_split, "Vol 01.cbz.p00001.part")
    n1, _ = archive.write_part(pdf, p1, 0, 2)
    n2, _ = archive.write_part(pdf, p2, 2, 3)
    cbz_split = os.path.join(d_split, "Vol 01.cbz")
    n_merged = archive.merge_parts([p1, p2], cbz_split)
    check("分段写盘张数 2+1", (n1, n2) == (2, 1), (n1, n2))
    check("合并后 3 张", n_merged == 3, n_merged)
    check("分段产物与单进程等价(名/大小/CRC)",
          zip_fingerprint(cbz_split) == zip_fingerprint(cbz_new))

    q1 = os.path.join(d_split, "Vol 01.cbz.p00000.part")
    q2 = os.path.join(d_split, "Vol 01.cbz.p00001.part")
    archive.write_part(pdf, q1, 0, 2)
    archive.write_part(pdf, q2, 2, 3)
    _p, ok_m, msg_m = batch.merge_ranges(pdf, d_split, "Vol 01", [q1, q2], False)
    check("merge_ranges 消息含张数", ok_m and "3 张唯一图片" in msg_m, msg_m)
    check("页段文件用完即删", not os.path.exists(q1) and not os.path.exists(q2))

    check("count_pages", batch.count_pages(pdf) == 3, batch.count_pages(pdf))
    rjob = batch.RangeJob(pdf, os.path.join(d_split, "x.cbz.p00000.part"), 0, 1)
    tag, _part, ok_r, _m, n_r, label_r, el_r = batch.run_job(rjob)
    check("run_job 分派到页段", tag == "range" and ok_r and n_r == 2, (tag, ok_r, n_r))
    check("页段回报页区间与耗时", label_r == "第 1–1 页" and el_r >= 0, (label_r, el_r))
    os.remove(_part)

    # 页进度回调：每页一次，带总页数
    seen = []
    list(extractor.iter_unique_images(pdf, on_page=lambda p, t: seen.append((p, t))))
    check("on_page 每页回调一次", seen == [(0, 3), (1, 3), (2, 3)], seen)
    check("ConvertJob 四参调用仍可用",
          batch.ConvertJob("a", "b", False, False).progress is None)

    # --- 3. 与上游单文件等价（PDF2CBZ_ORIG 指向上游 pdf2cbz_gui.py） ---
    orig = load_upstream()
    if orig is None:
        print("SKIP  与上游单文件等价性比对（设 PDF2CBZ_ORIG=<上游 pdf2cbz_gui.py> 后可跑）")
    else:
        d_old = os.path.join(tmp, "old")
        os.makedirs(d_old)
        p2, ok2, msg2 = orig.convert_pdf_to_cbz(
            orig.ConvertJob(pdf_path=pdf, out_dir=d_old, keep_images=False, overwrite=False))
        check("上游转换成功", ok2, msg2)
        check("产物逐条等价(名/大小/CRC)",
              zip_fingerprint(cbz_new) == zip_fingerprint(os.path.join(d_old, "Vol 01.cbz")))
        check("返回消息等价", msg == msg2, f"{msg!r} != {msg2!r}")

    # --- 4. keep_images 分支 ---
    d_keep = os.path.join(tmp, "keep")
    os.makedirs(d_keep)
    job_k = batch.ConvertJob(pdf_path=pdf, out_dir=d_keep, keep_images=True, overwrite=False)
    pk, okk, msgk = batch.convert_pdf_to_cbz(job_k)
    imgdir = os.path.join(d_keep, "Vol 01_images")
    check("keep_images 目录生成", okk and os.path.isdir(imgdir), msgk)
    check("keep_images 图片数 3",
          len(os.listdir(imgdir)) == 3 if os.path.isdir(imgdir) else False,
          os.listdir(imgdir) if os.path.isdir(imgdir) else "无目录")
    check("keep_images 无 .tmp_imgs 残留",
          not any(n.startswith(".tmp_imgs") for n in os.listdir(d_keep)),
          os.listdir(d_keep))

    # --- 5. 覆盖保护 / 原子性 ---
    p3, ok3, msg3 = batch.convert_pdf_to_cbz(job)
    check("已存在时默认跳过", (not ok3) and "跳过" in msg3, msg3)
    job_ov = job._replace(overwrite=True)
    p4, ok4, msg4 = batch.convert_pdf_to_cbz(job_ov)
    check("勾选覆盖后重做", ok4, msg4)

    # --- 6. 纯矢量 PDF 报错路径 ---
    vec = os.path.join(tmp, "vector.pdf")
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "no images here")
    doc.save(vec)
    doc.close()
    dv = os.path.join(tmp, "vec")
    os.makedirs(dv)
    pv, okv, msgv = batch.convert_pdf_to_cbz(
        batch.ConvertJob(vec, dv, False, False))
    check("无图 PDF 报错且不留 .part",
          (not okv) and "未提取到任何图片" in msgv and os.listdir(dv) == [],
          (msgv, os.listdir(dv)))

    # --- 7. 重命名逻辑 ---
    cases = {"I's v01.cbz": 1, "Vol.12.cbz": 12, "Vol 03 x.cbz": 3, "第7卷.cbz": 7,
             "#09.cbz": 9, "12巻.cbz": 12, "plain name.cbz": None, "2024 yearly.pdf": None}
    bad = {k: (rename.extract_volume(k), v) for k, v in cases.items()
           if rename.extract_volume(k) != v}
    check("extract_volume 8 组用例", not bad, bad)

    nat = ["v1.cbz", "v2.cbz", "v10.cbz"]
    check("自然排序 1<2<10", sorted(nat, key=rename.nat_key) == nat,
          sorted(nat, key=rename.nat_key))

    dr = os.path.join(tmp, "ren")
    os.makedirs(dr)
    for n in ["A.cbz", "B.cbz", "C.cbz"]:
        open(os.path.join(dr, n), "wb").close()
    plan = rename.plan_renames(dr, "Manga", "seq")
    check("计划格式 'Manga v01.cbz'", plan[0][1] == "Manga v01.cbz", plan)
    check("顺序模式无冲突", rename.find_conflicts(plan, dr) == [])
    dup = [("A.cbz", "X.cbz"), ("B.cbz", "X.cbz")]
    check("重名冲突被捕获", len(rename.find_conflicts(dup, dr)) == 1,
          rename.find_conflicts(dup, dr))

    # --- 7b. 重命名执行（原先埋在 GUI 里点不到，抽到 execute_renames 后可测） ---
    de = os.path.join(tmp, "exec")
    os.makedirs(de)
    for n in ["a.cbz", "b.cbz", "c.cbz"]:
        open(os.path.join(de, n), "wb").close()
    ok_n, errs_n = rename.execute_renames(de, [("a.cbz", "A1.cbz"), ("b.cbz", "B1.cbz")])
    check("execute_renames 批量成功", (ok_n, errs_n) == (2, []), (ok_n, errs_n))
    check("execute_renames 落盘正确",
          sorted(os.listdir(de)) == ["A1.cbz", "B1.cbz", "c.cbz"], os.listdir(de))

    # 链式：A1→c.cbz 的同时 c.cbz→C1.cbz，目标名正好是另一个源名
    ok_c, errs_c = rename.execute_renames(de, [("A1.cbz", "c.cbz"), ("c.cbz", "C1.cbz")])
    check("链式改名不互相覆盖",
          (ok_c, errs_c) == (2, []) and sorted(os.listdir(de)) == ["B1.cbz", "C1.cbz", "c.cbz"],
          (ok_c, errs_c, os.listdir(de)))

    # 单文件失败：目标被预置文件占用，其余照常完成，失败项回滚原名
    de2 = os.path.join(tmp, "exec2")
    os.makedirs(de2)
    for n in ["p.cbz", "q.cbz", "r.cbz", "taken.cbz"]:
        open(os.path.join(de2, n), "wb").close()
    ok_m, errs_m = rename.execute_renames(
        de2, [("p.cbz", "P1.cbz"), ("q.cbz", "taken.cbz"), ("r.cbz", "R1.cbz")])
    check("单文件失败不影响其余", ok_m == 2 and len(errs_m) == 1, (ok_m, errs_m))
    check("失败项回滚为原名且无 .tmp 残留",
          os.path.exists(os.path.join(de2, "q.cbz"))
          and not any(n.startswith(".__rn_") for n in os.listdir(de2)),
          os.listdir(de2))

    # --- 7c. 换名重试：被占用时有限次后抛错，不会无限等 ---
    lk = os.path.join(tmp, "lock")
    os.makedirs(lk)
    srcf = os.path.join(lk, "a.part")
    dstf = os.path.join(lk, "b.cbz")
    open(srcf, "wb").close()
    open(dstf, "wb").close()
    held = open(dstf, "rb")      # Windows 下持有句柄即可阻塞换名
    try:
        raised = False
        try:
            archive.replace_with_retry(srcf, dstf, attempts=2, base_delay=0.01)
        except PermissionError:
            raised = True
        check("换名被占用时重试后抛错（不死等）", raised or os.name != "nt", "未抛错")
    finally:
        held.close()

    # --- 8. 多进程真实跑一遍（pickle + spawn 导入） ---
    pm = os.path.join(tmp, "pool")
    os.makedirs(pm)
    pdf2 = os.path.join(tmp, "Vol 02.pdf")
    make_pdf(pdf2)
    jobs = [batch.ConvertJob(pdf, pm, False, False),
            batch.ConvertJob(pdf2, pm, False, False)]
    with multiprocessing.Pool(2) as pool:
        res = list(pool.imap_unordered(batch.convert_pdf_to_cbz, jobs))
    check("多进程 worker 可 pickle 且跑通", all(r[1] for r in res), res)
    check("并行产出两个 CBZ",
          sorted(n for n in os.listdir(pm) if n.endswith(".cbz")) == ["Vol 01.cbz", "Vol 02.cbz"],
          os.listdir(pm))

    # --- 8b. 同名不同目录并发：.part 带 pid 后不再互相撞锁 ---
    ca = os.path.join(tmp, "a")
    cb = os.path.join(tmp, "b")
    shared = os.path.join(tmp, "shared")
    for d in (ca, cb, shared):
        os.makedirs(d)
    shutil.copy(pdf, os.path.join(ca, "Same.pdf"))
    shutil.copy(pdf2, os.path.join(cb, "Same.pdf"))
    cjobs = [batch.ConvertJob(os.path.join(ca, "Same.pdf"), shared, False, True),
             batch.ConvertJob(os.path.join(cb, "Same.pdf"), shared, False, True)]
    with multiprocessing.Pool(2) as pool:
        cres = list(pool.imap_unordered(batch.convert_pdf_to_cbz, cjobs))
    check("同名输出并发不再双方全败", all(ok for _, ok, _ in cres),
          [m.splitlines()[0] for _, ok, m in cres if not ok])
    check("并发后输出目录只剩一个 CBZ（后者覆盖）",
          sorted(os.listdir(shared)) == ["Same.cbz"], os.listdir(shared))

    # --- 8c. 输出重名预检（UI 在 start() 里用它提前提醒） ---
    col = batch.find_output_collisions(
        [os.path.join(ca, "Same.pdf"), os.path.join(cb, "Same.pdf"), pdf])
    check("输出重名检测出 1 组", len(col) == 1 and col[0][0] == "Same.cbz"
          and len(col[0][1]) == 2, col)
    check("无重名时返回空", batch.find_output_collisions([pdf, pdf2]) == [])

    # --- 9. 清理残留 ---
    for n in [".tmp_x", ".__rn_0_1.tmp", "bad.cbz.part", "Vol 09.cbz.999.part", "keep.cbz"]:
        open(os.path.join(pm, n), "wb").close()
    batch.cleanup_stale_tmp(pm)
    left = sorted(os.listdir(pm))
    check("残留清理只删中间产物",
          left == sorted(["Vol 01.cbz", "Vol 02.cbz", "keep.cbz"]), left)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n==== 结果：%d 项通过，%d 项失败 ====" % (tally["pass"], tally["fail"]))
    if fails:
        print("失败项：", fails)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
