# -*- coding: utf-8 -*-
"""GUI 端到端：真窗口 + 真 mainloop，跑完整转换与中途停止两条路径。

用法：python e2e_gui.py     （会短暂弹出程序窗口，正常现象）
"""
import multiprocessing
import os
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)

import pymupdf

import main


def make_pdf(path, color, pages=2):
    px = pymupdf.Pixmap(pymupdf.csRGB, 60, 60, bytes(color) * (60 * 60), 0)
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=300, height=300)
        page.insert_image(pymupdf.Rect(10, 10, 200, 200), stream=px.tobytes("png"))
    doc.save(path)
    doc.close()


def prepare(names, pages=2):
    """建输入 PDF + 输出目录，返回 (app, outdir, pdfs)。"""
    tmp = tempfile.mkdtemp(prefix="gui_e2e_")
    indir = os.path.join(tmp, "in")
    outdir = os.path.join(tmp, "out")
    os.makedirs(indir)
    os.makedirs(outdir)
    pdfs = []
    for i, n in enumerate(names):
        p = os.path.join(indir, n + ".pdf")
        make_pdf(p, (40 + i * 50, 90, 160), pages)
        pdfs.append(p)
    app = main.App()
    app.files = [[p, ""] for p in pdfs]
    for p in pdfs:
        app.tree.insert("", "end", values=(os.path.basename(p), p, ""))
    app.out_dir.set(outdir)
    return app, outdir, pdfs


def collect(app, outdir):
    """采集界面状态 + 溢出检查（控件右边超出所在行即为被挤掉）。"""
    from tkinter import ttk
    rows = [app.btn_start.master]
    for f in app.winfo_children():
        if isinstance(f, ttk.LabelFrame) and f.cget("text").startswith("2."):
            rows.extend(f.winfo_children())
    overflow = []
    for r in rows:
        rw = r.winfo_width()
        for c in r.winfo_children():
            right = c.winfo_x() + c.winfo_width()
            if right > rw:
                label = c.cget("text") if "text" in c.keys() else c.winfo_class()
                overflow.append(f"{label}({right}>{rw})")
    return {
        "overflow": overflow,
        "log": app.log.get("1.0", "end"),
        "statuses": [app.tree.item(i, "values")[2] for i in app.tree.get_children()],
        "files": sorted(os.listdir(outdir)),
        "running": app.running,
        "title": app.title(),
        "progress": (float(app.progress["value"]), float(app.progress["maximum"])),
        "buttons": (str(app.btn_start["state"]), str(app.btn_cancel["state"])),
        "workers": app.workers.get(),
        "queued": app._events.qsize(),
        "selected": len(app.tree.selection()),
    }


def scenario_convert():
    """完整跑完 2 个文件。"""
    app, outdir, _ = prepare(["Vol 01", "Vol 02"])
    out = {}
    app.after(0, app.start)
    app.after(100, app._select_all)          # 等价于按 Ctrl+A

    def poll():
        if app.running:
            app.after(50, poll)
        else:
            out.update(collect(app, outdir))
            app.destroy()

    app.after(120, poll)
    app.after(60000, lambda: app.destroy() if app.running else None)   # 硬超时兜底
    app.mainloop()
    return out


def scenario_split():
    """单文件多页：走页段并行，验证能合并、能收尾。"""
    app, outdir, _ = prepare(["Solo"], pages=60)
    out = {}
    app.after(0, app.start)

    def poll():
        if app.running:
            app.after(50, poll)
        else:
            out.update(collect(app, outdir))
            app.destroy()

    app.after(120, poll)
    app.after(120000, lambda: app.destroy() if app.running else None)
    app.mainloop()
    return out


def scenario_cancel():
    """开跑后立刻点「停止」，确认能收尾、按钮能复位。"""
    app, outdir, _ = prepare(["C 01", "C 02", "C 03", "C 04"], pages=4)
    main.messagebox.askyesno = lambda *a, **k: True   # 跳过确认框
    out = {}
    app.after(0, app.start)
    app.after(20, app.cancel)

    def poll():
        if app.running:
            app.after(50, poll)
        else:
            out.update(collect(app, outdir))
            app.destroy()

    app.after(60, poll)
    app.after(60000, lambda: app.destroy() if app.running else None)
    app.mainloop()
    return out


def report(name, conds):
    bad = [k for k, v in conds.items() if not v]
    print(("PASS  " if not bad else "FAIL  ") + name + ("" if not bad else "  <- " + ", ".join(bad)))
    return not bad


def main_():
    ok = True

    c = scenario_convert()
    ok &= report("完整转换", {
        "产物两个 CBZ": c["files"][:2] == ["Vol 01.cbz", "Vol 02.cbz"],
        "有日志文件": "pdf2cbz.log" in c["files"],
        "状态回写": c["statuses"] == ["完成", "完成"],
        "running 复位": c["running"] is False,
        "按钮复位": c["buttons"] == ("normal", "disabled"),
        "标题复位": c["title"] == main._APP_TITLE,
        "进度满格": c["progress"] == (2.0, 2.0),
        "并行度=物理核估算": c["workers"] == main._default_workers(),
        "事件队列已排空": c["queued"] == 0,
        "Ctrl+A 全选": c["selected"] == 2,
        "无控件溢出": c["overflow"] == [],
        "日志含结束摘要": ("运行结束" in c["log"]) and ("成功 2" in c["log"]),
        "无异常栈": "Traceback" not in c["log"],
    })

    k = scenario_cancel()
    ok &= report("中途停止", {
        "running 复位": k["running"] is False,
        "按钮复位": k["buttons"] == ("normal", "disabled"),
        "标题复位": k["title"] == main._APP_TITLE,
        "事件队列已排空": k["queued"] == 0,
        "日志有收尾": ("已停止" in k["log"]) or ("运行结束" in k["log"]),
        "无异常栈": "Traceback" not in k["log"],
    })

    s = scenario_split()
    ok &= report("单文件分段并行", {
        "产物一个 CBZ": "Solo.cbz" in s["files"],
        "日志显示按页切段": "按页切" in s["log"] and "段并行" in s["log"],
        "日志显示合并": "合并中" in s["log"],
        "状态回写": s["statuses"] == ["完成"],
        "进度按段计满": s["progress"][0] == s["progress"][1] and s["progress"][1] > 1,
        "无 .part 残留": not any(x.endswith(".part") for x in s["files"]),
        "无异常栈": "Traceback" not in s["log"],
    })

    print("\nE2E", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main_())
