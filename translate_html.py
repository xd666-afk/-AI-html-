# -*- coding: utf-8 -*-
"""
GUI文件.py —— DeepSeek API 批量翻译 HTML（tkinter 桌面版）
与 translate_html.py 必须放在同一目录下。

功能：
- 批量翻译整个 input 目录
- 左侧文件列表，双击某个文件可"只翻译这一个"（弹窗确认）
- 双进度条（文件级 + 片段级）
- 术语表、语言选择、缓存、批大小/并发数
"""

import os
import sys
import json
import shutil
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 保证可以从同目录导入核心模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import translate_html as th


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_config.json")

# 颜色 / 状态
COLOR_DOING_BG = "#D6E9FF"
COLOR_DOING_FG = "#0B4C9E"
COLOR_DONE_FG  = "#888888"
COLOR_FAIL_FG  = "#C0392B"
COLOR_SKIP_FG  = "#AAAAAA"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("HTML 批量翻译（DeepSeek）")
        self.root.geometry("1060x660")

        # 状态
        self.worker = None
        self.stop_flag = threading.Event()
        self.msg_q = queue.Queue()
        self.fname_to_idx = {}   # 文件名 -> Listbox 行号
        self.files = []          # 当前列表中的文件名（顺序）
        self.single_mode = False # 是否单文件模式
        self.single_target = None  # 单文件模式下正在翻的原文件名（用于显示）

        # 变量
        base = os.path.dirname(os.path.abspath(__file__))
        self.var_in  = tk.StringVar(value=os.path.join(base, "input"))
        self.var_out = tk.StringVar(value=os.path.join(base, "output"))
        self.var_cache = tk.StringVar(value=os.path.join(base, "cache"))
        self.var_api = tk.StringVar(value="")
        self.var_src = tk.StringVar(value="英语")
        self.var_dst = tk.StringVar(value="简体中文")
        self.var_glossary = tk.StringVar(value="")
        self.var_batch = tk.StringVar(value=str(th.DEFAULT_BATCH))
        self.var_workers = tk.StringVar(value=str(th.DEFAULT_WORKERS))
        self.var_file = tk.DoubleVar(value=0.0)
        self.var_frag = tk.DoubleVar(value=0.0)
        self.var_status = tk.StringVar(value="就绪")

        self._load_config()
        self._build_ui()
        self._refresh_file_list()
        self.root.after(120, self._poll_queue)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=8, pady=6)

        # 左：文件列表
        left = ttk.LabelFrame(main, text="待翻译文件 (input) —— 双击可单独翻译")
        left.pack(side="left", fill="both", expand=False, padx=(0, 8))

        header = ttk.Frame(left)
        header.pack(fill="x", padx=6, pady=(4, 2))
        self.lb_count = ttk.Label(header, text="(0)")
        self.lb_count.pack(side="left")
        ttk.Button(header, text="刷新", width=6, command=self._refresh_file_list).pack(side="right")

        lb_wrap = ttk.Frame(left)
        lb_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        sb = ttk.Scrollbar(lb_wrap, orient="vertical")
        self.lb = tk.Listbox(lb_wrap, width=36, height=30, activestyle="none",
                             yscrollcommand=sb.set)
        sb.config(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)

        # 双击 = 只翻这个文件
        self.lb.bind("<Double-Button-1>", self._on_double_click)

        # 右：配置 + 日志
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        # ----- 配置区 -----
        cfg = ttk.LabelFrame(right, text="配置")
        cfg.pack(fill="x")
        cfg.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(cfg, text="输入目录").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_in).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(cfg, text="选择", width=6,
                   command=lambda: self._choose_dir(self.var_in)).grid(row=r, column=2, **pad)

        r += 1
        ttk.Label(cfg, text="输出目录").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_out).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(cfg, text="选择", width=6,
                   command=lambda: self._choose_dir(self.var_out)).grid(row=r, column=2, **pad)

        r += 1
        ttk.Label(cfg, text="缓存目录").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_cache).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(cfg, text="选择", width=6,
                   command=lambda: self._choose_dir(self.var_cache)).grid(row=r, column=2, **pad)

        r += 1
        ttk.Label(cfg, text="API Key").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_api, show="*").grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(cfg, text="保存配置", width=8, command=self._save_config).grid(row=r, column=2, **pad)

        r += 1
        ttk.Label(cfg, text="源语言").grid(row=r, column=0, sticky="w", **pad)
        lang_values = list(th.LANGUAGES.keys())
        self.cb_src = ttk.Combobox(cfg, textvariable=self.var_src, values=lang_values,
                                   state="readonly", width=18)
        self.cb_src.grid(row=r, column=1, sticky="w", **pad)

        ttk.Label(cfg, text="目标语言").grid(row=r, column=2, sticky="w", **pad)
        self.cb_dst = ttk.Combobox(cfg, textvariable=self.var_dst, values=lang_values,
                                   state="readonly", width=18)
        self.cb_dst.grid(row=r, column=3, sticky="w", **pad)

        r += 1
        ttk.Label(cfg, text="术语表").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_glossary).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(cfg, text="选择", width=6,
                   command=self._choose_file).grid(row=r, column=2, **pad)

        r += 1
        ttk.Label(cfg, text="批大小").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_batch, width=8).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(cfg, text="并发数").grid(row=r, column=2, sticky="w", **pad)
        ttk.Entry(cfg, textvariable=self.var_workers, width=8).grid(row=r, column=3, sticky="w", **pad)

        # ----- 按钮 -----
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(6, 4))
        self.btn_start = ttk.Button(bar, text="开始翻译（全部）", command=self._start_all)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(bar, text="打开输出目录", command=self._open_output).pack(side="left", padx=4)

        # ----- 双进度条 -----
        prog = ttk.LabelFrame(right, text="进度")
        prog.pack(fill="x", pady=(0, 4))
        ttk.Label(prog, text="文件").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Progressbar(prog, variable=self.var_file, maximum=100.0).grid(
            row=0, column=1, sticky="ew", padx=6, pady=2)
        ttk.Label(prog, text="片段").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Progressbar(prog, variable=self.var_frag, maximum=100.0).grid(
            row=1, column=1, sticky="ew", padx=6, pady=2)
        prog.columnconfigure(1, weight=1)
        ttk.Label(prog, textvariable=self.var_status).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 4))

        # ----- 日志 -----
        logf = ttk.LabelFrame(right, text="日志")
        logf.pack(fill="both", expand=True)
        self.txt = tk.Text(logf, height=12, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self._log("就绪。填好 API Key，点【开始翻译（全部）】批量翻，")
        self._log("或在左侧列表【双击】某个文件，只翻这一个。")

    # ---------- 文件列表 ----------
    def _refresh_file_list(self):
        self.lb.delete(0, "end")
        self.fname_to_idx.clear()
        self.files = []

        d = self.var_in.get().strip()
        if not os.path.isdir(d):
            self.lb_count.config(text="(0) 目录不存在")
            return

        names = [n for n in os.listdir(d) if n.lower().endswith((".html", ".htm"))]
        names.sort(key=str.lower)

        for i, name in enumerate(names):
            self.lb.insert("end", name)
            self.fname_to_idx[name] = i
        self.files = names
        self.lb_count.config(text=f"({len(names)})")

    def _mark_file(self, fname, state):
        idx = self.fname_to_idx.get(os.path.basename(fname))
        if idx is None:
            return
        try:
            if state == "doing":
                self.lb.itemconfig(idx, background=COLOR_DOING_BG, foreground=COLOR_DOING_FG)
                self.lb.see(idx)
            elif state == "done":
                self.lb.itemconfig(idx, background="", foreground=COLOR_DONE_FG)
            elif state == "fail":
                self.lb.itemconfig(idx, background="", foreground=COLOR_FAIL_FG)
        except tk.TclError:
            pass

    # ---------- 交互 ----------
    def _on_double_click(self, event):
        """双击左侧文件 → 弹窗确认 → 只翻这个文件。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在翻译中，等它跑完或先点【停止】。")
            return

        sel = self.lb.curselection()
        if not sel:
            return
        idx = sel[0]
        fname = self.lb.get(idx)
        in_dir = self.var_in.get().strip()
        full_path = os.path.join(in_dir, fname)
        if not os.path.isfile(full_path):
            messagebox.showwarning("提示", "文件不存在，先点【刷新】。")
            return

        ok = messagebox.askyesno(
            "单文件翻译",
            f"只翻译这一个文件吗？\n\n"
            f"文件：{fname}\n"
            f"源语言：{self.var_src.get()}\n"
            f"目标语言：{self.var_dst.get()}\n\n"
            f"（其它文件不会被处理）"
        )
        if not ok:
            return

        self._start_single(fname)

    def _choose_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or os.getcwd())
        if d:
            var.set(d)
            if var is self.var_in:
                self._refresh_file_list()

    def _choose_file(self):
        f = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.var_glossary.get() or os.getcwd()),
            filetypes=[("文本", "*.txt"), ("所有文件", "*.*")])
        if f:
            self.var_glossary.set(f)

    def _open_output(self):
        d = self.var_out.get().strip()
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo("提示", "输出目录不存在。")

    # ---------- 配置持久化 ----------
    def _load_config(self):
        if not os.path.isfile(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.var_api.set(cfg.get("api_key", ""))
            self.var_in.set(cfg.get("input", self.var_in.get()))
            self.var_out.set(cfg.get("output", self.var_out.get()))
            self.var_cache.set(cfg.get("cache", self.var_cache.get()))
            self.var_src.set(cfg.get("src", self.var_src.get()))
            self.var_dst.set(cfg.get("dst", self.var_dst.get()))
            self.var_glossary.set(cfg.get("glossary", ""))
            self.var_batch.set(str(cfg.get("batch", self.var_batch.get())))
            self.var_workers.set(str(cfg.get("workers", self.var_workers.get())))
        except Exception as e:
            print("读取配置失败：", e)

    def _save_config(self):
        cfg = {
            "api_key": self.var_api.get().strip(),
            "input": self.var_in.get().strip(),
            "output": self.var_out.get().strip(),
            "cache": self.var_cache.get().strip(),
            "src": self.var_src.get(),
            "dst": self.var_dst.get(),
            "glossary": self.var_glossary.get().strip(),
            "batch": self.var_batch.get().strip(),
            "workers": self.var_workers.get().strip(),
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self._log("配置已保存到 gui_config.json")

    # ---------- 日志 ----------
    def _log(self, msg):
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ---------- 启动：全部 ----------
    def _start_all(self):
        if not self._precheck():
            return
        try:
            batch = int(self.var_batch.get())
            workers = int(self.var_workers.get())
        except ValueError:
            messagebox.showwarning("提示", "批大小/并发数必须是整数")
            return

        self.single_mode = False
        self.single_target = None
        self._refresh_file_list()
        self._begin_run()
        in_dir = self.var_in.get().strip()

        self.worker = threading.Thread(
            target=self._worker_batch,
            args=(in_dir, self.var_out.get().strip(), self.var_cache.get().strip(),
                  self.var_api.get().strip(), self.var_src.get(), self.var_dst.get(),
                  self.var_glossary.get().strip(), batch, workers),
            daemon=True)
        self.worker.start()

    # ---------- 启动：单个 ----------
    def _start_single(self, fname):
        if not self._precheck():
            return
        try:
            batch = int(self.var_batch.get())
            workers = int(self.var_workers.get())
        except ValueError:
            messagebox.showwarning("提示", "批大小/并发数必须是整数")
            return

        self.single_mode = True
        self.single_target = fname
        self._begin_run()
        # 单文件模式：只把这一行的颜色重置，其它保持
        self._mark_file(fname, "doing")

        in_dir = self.var_in.get().strip()
        out_dir = self.var_out.get().strip()
        cache_dir = self.var_cache.get().strip()

        self.worker = threading.Thread(
            target=self._worker_single,
            args=(in_dir, out_dir, cache_dir,
                  self.var_api.get().strip(), self.var_src.get(), self.var_dst.get(),
                  self.var_glossary.get().strip(), batch, workers, fname),
            daemon=True)
        self.worker.start()

    def _precheck(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已经在翻译中。")
            return False
        if not self.var_api.get().strip():
            messagebox.showwarning("提示", "请填写 API Key")
            return False
        if not os.path.isdir(self.var_in.get().strip()):
            messagebox.showwarning("提示", "输入目录不存在")
            return False
        # 语言对检查
        if self.var_src.get() == self.var_dst.get():
            r = messagebox.askyesno("提示", "源语言与目标语言相同，确定继续？")
            if not r:
                return False
        return True

    def _begin_run(self):
        self.stop_flag.clear()
        self.var_file.set(0.0)
        self.var_frag.set(0.0)
        self.var_status.set("运行中…")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        # 重置所有项颜色
        for i in range(self.lb.size()):
            self.lb.itemconfig(i, background="", foreground="")

    def _stop(self):
        self.stop_flag.set()
        self.var_status.set("正在停止…")
        self._log(">> 已发送停止信号，等待当前批次结束…")

    # ---------- 后台线程：批量 ----------
    def _worker_batch(self, in_dir, out_dir, cache_dir, api, src, dst, glossary, batch, workers):
        def on_file(done, total, name):
            self.msg_q.put(("file", (done, total, name)))

        def on_frag(done, total, name):
            self.msg_q.put(("frag", (done, total)))

        def log_cb(msg):
            self.msg_q.put(("log", msg))

        try:
            th.run_translation(
                input_dir=in_dir,
                output_dir=out_dir,
                cache_dir=cache_dir,
                api_key=api,
                src_lang=src,
                dst_lang=dst,
                glossary_path=glossary if glossary else None,
                batch_size=batch,
                workers=workers,
                on_file=on_file,
                on_fragment=on_frag,
                log_cb=log_cb,
                stop_flag=self.stop_flag,
            )
            self.msg_q.put(("done", None))
        except Exception as e:
            import traceback
            self.msg_q.put(("error", f"{e}\n{traceback.format_exc()}"))

    # ---------- 后台线程：单个 ----------
    def _worker_single(self, in_dir, out_dir, cache_dir, api, src, dst, glossary,
                       batch, workers, fname):
        """只翻一个文件：临时目录隔离，跑完把结果搬回输出目录。"""
        tmp_root = None
        try:
            tmp_root = tempfile.mkdtemp(prefix="single_html_")
            tmp_in = os.path.join(tmp_root, "input")
            tmp_out = os.path.join(tmp_root, "output")
            os.makedirs(tmp_in, exist_ok=True)
            os.makedirs(tmp_out, exist_ok=True)

            src_path = os.path.join(in_dir, fname)
            dst_in = os.path.join(tmp_in, fname)
            shutil.copy2(src_path, dst_in)

            def on_file(done, total, name):
                # 把自己映射回原文件名，让左侧列表能高亮
                self.msg_q.put(("file", (done, total, fname)))

            def on_frag(done, total, name):
                self.msg_q.put(("frag", (done, total)))

            def log_cb(msg):
                self.msg_q.put(("log", msg))

            self.msg_q.put(("log", f">> 单文件模式：{fname}"))

            th.run_translation(
                input_dir=tmp_in,
                output_dir=tmp_out,
                cache_dir=cache_dir,          # 复用同一个 cache，省钱
                api_key=api,
                src_lang=src,
                dst_lang=dst,
                glossary_path=glossary if glossary else None,
                batch_size=batch,
                workers=workers,
                on_file=on_file,
                on_fragment=on_frag,
                log_cb=log_cb,
                stop_flag=self.stop_flag,
            )

            # 把结果搬回原输出目录
            produced = os.path.join(tmp_out, fname)
            if not os.path.isfile(produced):
                # 有些实现输出文件名可能变过，兜底找一下
                candidates = [n for n in os.listdir(tmp_out)
                              if n.lower().endswith((".html", ".htm"))]
                if candidates:
                    produced = os.path.join(tmp_out, candidates[0])

            if os.path.isfile(produced):
                os.makedirs(out_dir, exist_ok=True)
                final_dst = os.path.join(out_dir, fname)
                shutil.copy2(produced, final_dst)
                self.msg_q.put(("log", f">> 已输出：{final_dst}"))
            else:
                self.msg_q.put(("error", "单文件翻译未生成输出文件。"))

            self.msg_q.put(("done_single", fname))
        except Exception as e:
            import traceback
            self.msg_q.put(("error", f"{e}\n{traceback.format_exc()}"))
        finally:
            if tmp_root and os.path.isdir(tmp_root):
                try:
                    shutil.rmtree(tmp_root, ignore_errors=True)
                except Exception:
                    pass

    # ---------- 队列轮询 ----------
    def _poll_queue(self):
        try:
            while True:
                kind, data = self.msg_q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "file":
                    done, total, name = data
                    if total > 0:
                        self.var_file.set(done * 100.0 / total)
                    self._mark_doing_by_progress(name)
                    self.var_status.set(f"文件 {done}/{total}  {os.path.basename(name)}")
                elif kind == "frag":
                    done, total = data
                    if total > 0:
                        self.var_frag.set(done * 100.0 / total)
                elif kind == "file_doing":
                    self._mark_file(data, "doing")
                elif kind == "file_done":
                    self._mark_file(data, "done")
                elif kind == "file_fail":
                    self._mark_file(data, "fail")
                elif kind == "done":
                    self.var_status.set("完成 ✅")
                    self.var_file.set(100.0)
                    self.var_frag.set(100.0)
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self._log("== 全部完成 ==")
                    messagebox.showinfo("完成", "批量翻译完成，去输出目录看看。")
                elif kind == "done_single":
                    fname = data
                    self.var_status.set(f"单文件完成 ✅  {fname}")
                    self.var_file.set(100.0)
                    self.var_frag.set(100.0)
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self._mark_file(fname, "done")
                    self._log(f"== 单文件完成：{fname} ==")
                    messagebox.showinfo("完成", f"已翻译：{fname}\n去输出目录看看。")
                elif kind == "error":
                    self.var_status.set("出错 ❌")
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    if self.single_mode and self.single_target:
                        self._mark_file(self.single_target, "fail")
                    self._log("!!! 出错：\n" + str(data))
                    messagebox.showerror("出错", str(data)[:1000])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _mark_doing_by_progress(self, current_name):
        base = os.path.basename(current_name)
        for i in range(self.lb.size()):
            try:
                txt = self.lb.get(i)
            except tk.TclError:
                continue
            if txt != base and self.lb.itemcget(i, "background") == COLOR_DOING_BG:
                self.lb.itemconfig(i, background="", foreground="")
        self._mark_file(base, "doing")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
