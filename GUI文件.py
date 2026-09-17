# -*- coding: utf-8 -*-
"""
HTML 批量翻译 - GUI
用法：直接 Run 本文件
语言：两个输入框，自己填「源语言」「目标语言」
      例如：源=俄语  目标=简体中文
            源=英语  目标=日语
"""

import os
import json
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import translate_html as core

CONFIG_FILE = "gui_config.json"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HTML 批量翻译 - DeepSeek")
        self.geometry("980x640")
        self.minsize(880, 560)

        self.cfg = load_config()
        self.msg_q = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.files = []
        self.translating = False

        self._build_ui()
        self._load_cfg_into_ui()
        self.after(100, self._poll_queue)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = dict(padx=6, pady=4)

        top = ttk.LabelFrame(self, text="配置")
        top.pack(fill="x", **pad)

        # API Key
        ttk.Label(top, text="API Key:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_key = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_key, show="*", width=60).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=6, pady=4)

        # 源语言 + 目标语言（两个独立输入框）
        ttk.Label(top, text="源语言:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.var_src = tk.StringVar(value="俄语")
        ttk.Entry(top, textvariable=self.var_src, width=20).grid(
            row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(top, text="目标语言:").grid(row=1, column=2, sticky="e", padx=6, pady=4)
        self.var_dst = tk.StringVar(value="简体中文")
        ttk.Entry(top, textvariable=self.var_dst, width=20).grid(
            row=1, column=3, sticky="w", padx=6, pady=4)

        # 输入目录
        ttk.Label(top, text="输入文件夹:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.var_in = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_in, width=60).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=6, pady=4)
        ttk.Button(top, text="选择…", command=self._pick_in).grid(row=2, column=3, padx=6)

        # 输出目录
        ttk.Label(top, text="输出文件夹:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.var_out = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_out, width=60).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=6, pady=4)
        ttk.Button(top, text="选择…", command=self._pick_out).grid(row=3, column=3, padx=6)

        # 跳过未改动
        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="跳过已翻译且未改动的文件（省钱）",
                        variable=self.var_skip).grid(row=4, column=1, sticky="w", padx=6, pady=2)

        top.columnconfigure(1, weight=1)

        # --- 中部 ---
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, **pad)

        left = ttk.LabelFrame(mid, text="待翻译文件（双击单独翻译）")
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))

        self.lst = tk.Listbox(left, selectmode="extended")
        self.lst.pack(side="left", fill="both", expand=True)
        sb1 = ttk.Scrollbar(left, orient="vertical", command=self.lst.yview)
        sb1.pack(side="right", fill="y")
        self.lst.config(yscrollcommand=sb1.set)
        self.lst.bind("<Double-Button-1>", self._on_double_click)

        right = ttk.LabelFrame(mid, text="进度 / 日志")
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))

        self.var_prog = tk.DoubleVar(value=0.0)
        ttk.Progressbar(right, variable=self.var_prog, maximum=100).pack(
            fill="x", padx=6, pady=(8, 2))
        self.var_prog_txt = tk.StringVar(value="就绪")
        ttk.Label(right, textvariable=self.var_prog_txt).pack(anchor="w", padx=8)

        self.txt = tk.Text(right, height=18, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, padx=6, pady=6)
        sb2 = ttk.Scrollbar(self.txt, orient="vertical", command=self.txt.yview)
        sb2.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb2.set)

        # --- 底部 ---
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)

        ttk.Button(bottom, text="刷新列表", command=self._refresh_list).pack(side="left", padx=6, pady=6)
        self.btn_all = ttk.Button(bottom, text="全部翻译", command=self._translate_all)
        self.btn_all.pack(side="left", padx=6, pady=6)
        self.btn_sel = ttk.Button(bottom, text="翻译选中", command=self._translate_selected)
        self.btn_sel.pack(side="left", padx=6, pady=6)
        self.btn_stop = ttk.Button(bottom, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="right", padx=6, pady=6)

    # ---------------- 配置 ----------------
    def _load_cfg_into_ui(self):
        self.var_key.set(self.cfg.get("api_key", ""))
        self.var_in.set(self.cfg.get("input", ""))
        self.var_out.set(self.cfg.get("output", ""))
        self.var_src.set(self.cfg.get("src_lang", "俄语"))
        self.var_dst.set(self.cfg.get("dst_lang", "简体中文"))
        self.var_skip.set(bool(self.cfg.get("skip_existing", True)))
        if self.var_in.get():
            self._refresh_list()

    def _save_cfg(self):
        cfg = {
            "api_key": self.var_key.get().strip(),
            "input": self.var_in.get().strip(),
            "output": self.var_out.get().strip(),
            "src_lang": self.var_src.get().strip(),
            "dst_lang": self.var_dst.get().strip(),
            "skip_existing": bool(self.var_skip.get()),
        }
        save_config(cfg)
        self.cfg = cfg

    # ---------------- 选择文件夹 ----------------
    def _pick_in(self):
        d = filedialog.askdirectory(title="选择输入文件夹")
        if d:
            self.var_in.set(d)
            if not self.var_out.get():
                self.var_out.set(os.path.join(os.path.dirname(d), "output"))
            self._refresh_list()

    def _pick_out(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.var_out.set(d)

    # ---------------- 文件列表 ----------------
    def _refresh_list(self):
        d = self.var_in.get().strip()
        self.lst.delete(0, "end")
        self.files = []
        if not d or not os.path.isdir(d):
            self._log("输入文件夹无效")
            return
        names = sorted(
            f for f in os.listdir(d)
            if f.lower().endswith((".html", ".htm"))
        )
        for n in names:
            self.files.append(os.path.join(d, n))
            self.lst.insert("end", n)
        self._log(f"共发现 {len(self.files)} 个 HTML 文件")

    def _on_double_click(self, event):
        if self.translating:
            return
        sel = self.lst.curselection()
        if not sel:
            return
        idx = sel[0]
        name = self.lst.get(idx)
        if not messagebox.askyesno("确认", f"单独翻译：\n{name}\n\n确定吗？"):
            return
        self._start([self.files[idx]])

    # ---------------- 翻译入口 ----------------
    def _translate_all(self):
        if not self.files:
            messagebox.showwarning("提示", "没有可翻译的文件，先刷新列表")
            return
        self._start(None)

    def _translate_selected(self):
        if self.translating:
            return
        sel = self.lst.curselection()
        if not sel:
            messagebox.showwarning("提示", "请先在左侧选中文件")
            return
        files = [self.files[i] for i in sel]
        self._start(files)

    def _start(self, only_files):
        if self.translating:
            messagebox.showinfo("提示", "正在翻译中，请先停止")
            return

        api_key = self.var_key.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请填写 API Key")
            return
        if any(ord(c) > 127 for c in api_key):
            messagebox.showerror("错误", "API Key 里含非 ASCII 字符（中文/全角），请检查！")
            return

        src = self.var_src.get().strip()
        dst = self.var_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("提示", "请填写源语言和目标语言")
            return
        if src == dst:
            messagebox.showwarning("提示", "源语言和目标语言不能一样")
            return

        in_dir = self.var_in.get().strip()
        out_dir = self.var_out.get().strip()
        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showwarning("提示", "输入文件夹无效")
            return
        if not out_dir:
            messagebox.showwarning("提示", "请选择输出文件夹")
            return

        os.makedirs(out_dir, exist_ok=True)
        self._save_cfg()

        self.stop_flag.clear()
        self.translating = True
        self.btn_all.config(state="disabled")
        self.btn_sel.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.var_prog.set(0)
        self.var_prog_txt.set("开始…")
        self._clear_log()
        self._log(f"源语言：{src}    目标语言：{dst}")

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(in_dir, out_dir, api_key, only_files, src, dst),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(self, in_dir, out_dir, api_key, only_files, src, dst):
        try:
            core.run_translation(
                input_dir=in_dir,
                output_dir=out_dir,
                api_key=api_key,
                on_progress=self._on_progress,
                stop_flag=self.stop_flag,
                only_files=only_files,
                src_lang=src,
                dst_lang=dst,
                skip_existing=bool(self.var_skip.get()),
            )
            self.msg_q.put(("done", None))
        except Exception as e:
            import traceback
            self.msg_q.put(("error", f"{e}\n\n{traceback.format_exc()}"))

    def _on_progress(self, *args):
        if len(args) == 1:
            self.msg_q.put(("log", str(args[0])))
        elif len(args) >= 3:
            cur, total, name = args[0], args[1], args[2]
            self.msg_q.put(("prog", (cur, total, name)))
        else:
            self.msg_q.put(("log", " ".join(str(a) for a in args)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "prog":
                    cur, total, name = payload
                    pct = 0 if not total else (cur / total) * 100
                    self.var_prog.set(pct)
                    self.var_prog_txt.set(f"[{cur}/{total}] {name}")
                elif kind == "done":
                    self._finish("全部完成 ✔")
                elif kind == "error":
                    self._log("错误：" + payload)
                    self._finish("出错了 ✘")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _stop(self):
        if self.translating:
            self.stop_flag.set()
            self._log("已发送停止信号，等待当前批次结束…")
            self.btn_stop.config(state="disabled")

    def _finish(self, msg):
        self.translating = False
        self.btn_all.config(state="normal")
        self.btn_sel.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.var_prog_txt.set(msg)
        self._log(msg)
        if msg.startswith("全部完成"):
            self.var_prog.set(100)

    def _log(self, s):
        self.txt.config(state="normal")
        self.txt.insert("end", str(s) + "\n")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _clear_log(self):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")


if __name__ == "__main__":
    App().mainloop()
