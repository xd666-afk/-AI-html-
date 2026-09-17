# -*- coding: utf-8 -*-
"""
HTML 批量翻译 - GUI 前端
依赖同目录的 translate_html.py（真实接口，位置参数）
"""

import os
import sys
import json
import shutil
import threading
import queue
import time
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import translate_html as th


# ---------- 路径 ----------
def _get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _get_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "gui_config.json")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

# ---------- 颜色 ----------
COLOR_DOING_BG = "#D6E9FF"
COLOR_DOING_FG = "#0B4C9E"
COLOR_DONE_FG = "#888888"
COLOR_FAIL_FG = "#C0392B"

# ---------- 语言（显示名 -> code） ----------
LANG_DISPLAY = [
    ("自动检测", "auto"),
    ("简体中文", "zh-CN"),
    ("英语", "en"),
    ("俄语", "ru"),
    ("日语", "ja"),
    ("韩语", "ko"),
    ("法语", "fr"),
    ("德语", "de"),
    ("西班牙语", "es"),
    ("葡萄牙语", "pt"),
    ("意大利语", "it"),
    ("阿拉伯语", "ar"),
    ("泰语", "th"),
]

DISPLAY_TO_CODE = {d: c for d, c in LANG_DISPLAY}
CODE_TO_DISPLAY = {c: d for d, c in LANG_DISPLAY}

DEFAULT_CONFIG = {
    "api_key": "",
    "input": "",
    "output": "",
    "src_lang": "auto",
    "dst_lang": "zh-CN",
    "skip_existing": True,
    "batch_size": th.DEFAULT_BATCH,
    "workers": th.DEFAULT_WORKERS,
    "glossary": "",
}


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("HTML 批量翻译（DeepSeek）")
        self.root.geometry("980x720")
        self.root.minsize(860, 600)

        # 运行状态
        self.worker = None
        self.stop_flag = threading.Event()
        self.msg_q = queue.Queue()
        self.running = False
        self.single_mode = False
        self.current_file_index = None
        self.file_rows = []
        self._list_start_time = 0.0

        # 进度条节流用
        self._last_frag_shown = -1
        self._last_batch_shown = -1
        self._cur_frag_file = None

        # 配置
        self.cfg = dict(DEFAULT_CONFIG)
        self._load_config()

        # 界面
        self._build_ui()

        # 队列轮询
        self.root.after(120, self._poll_queue)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------
    def _build_ui(self):
        # ===== 顶部：API Key =====
        top = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        top.pack(fill="x")

        ttk.Label(top, text="API Key：").pack(side="left")
        self.var_api_key = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.entry_api_key = ttk.Entry(top, textvariable=self.var_api_key, show="*")
        self.entry_api_key.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="显示", variable=self.var_show_key,
            command=self._toggle_key_show
        ).pack(side="left")

        # ===== 目录行（输入 / 输出） =====
        dirf = ttk.Frame(self.root, padding=(10, 6, 10, 0))
        dirf.pack(fill="x")
        dirf.columnconfigure(1, weight=1)

        ttk.Label(dirf, text="输入目录：").grid(row=0, column=0, sticky="w")
        self.var_input = tk.StringVar(value=self.cfg.get("input", ""))
        ttk.Entry(dirf, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(dirf, text="浏览…", command=self._choose_input).grid(row=0, column=2)

        ttk.Label(dirf, text="输出目录：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_output = tk.StringVar(value=self.cfg.get("output", ""))
        ttk.Entry(dirf, textvariable=self.var_output).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(dirf, text="浏览…", command=self._choose_output).grid(row=1, column=2, pady=(6, 0))

        ttk.Label(dirf, text="术语表：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_glossary = tk.StringVar(value=self.cfg.get("glossary", ""))
        ttk.Entry(dirf, textvariable=self.var_glossary).grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(dirf, text="浏览…", command=self._choose_glossary).grid(row=2, column=2, pady=(6, 0))

        # ===== 参数行 =====
        pf = ttk.Frame(self.root, padding=(10, 6, 10, 0))
        pf.pack(fill="x")

        ttk.Label(pf, text="源语言：").pack(side="left")
        self.var_src = tk.StringVar(value=CODE_TO_DISPLAY.get(self.cfg.get("src_lang", "auto"), "自动检测"))
        self.cmb_src = ttk.Combobox(
            pf, textvariable=self.var_src, state="readonly", width=10,
            values=[d for d, _ in LANG_DISPLAY]
        )
        self.cmb_src.pack(side="left", padx=(0, 12))

        ttk.Label(pf, text="目标语言：").pack(side="left")
        self.var_dst = tk.StringVar(value=CODE_TO_DISPLAY.get(self.cfg.get("dst_lang", "zh-CN"), "简体中文"))
        self.cmb_dst = ttk.Combobox(
            pf, textvariable=self.var_dst, state="readonly", width=10,
            values=[d for d, _ in LANG_DISPLAY]
        )
        self.cmb_dst.pack(side="left", padx=(0, 12))

        ttk.Label(pf, text="批量：").pack(side="left")
        self.var_batch = tk.IntVar(value=int(self.cfg.get("batch_size", th.DEFAULT_BATCH)))
        ttk.Spinbox(pf, from_=1, to=200, textvariable=self.var_batch, width=5).pack(side="left", padx=(0, 12))

        ttk.Label(pf, text="并发：").pack(side="left")
        self.var_workers = tk.IntVar(value=int(self.cfg.get("workers", th.DEFAULT_WORKERS)))
        ttk.Spinbox(pf, from_=1, to=64, textvariable=self.var_workers, width=5).pack(side="left", padx=(0, 12))

        self.var_skip = tk.BooleanVar(value=bool(self.cfg.get("skip_existing", True)))
        ttk.Checkbutton(pf, text="跳过已存在", variable=self.var_skip).pack(side="left", padx=(0, 12))

        # ===== 按钮行 =====
        bf = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        bf.pack(fill="x")

        self.btn_start = ttk.Button(bf, text="▶ 开始翻译", command=self._on_start)
        self.btn_start.pack(side="left")

        self.btn_stop = ttk.Button(bf, text="■ 停止", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.btn_clear = ttk.Button(bf, text="清空日志", command=self._clear_log)
        self.btn_clear.pack(side="left", padx=(8, 0))

        self.lbl_status = ttk.Label(bf, text="空闲")
        self.lbl_status.pack(side="right")

        # ===== 进度条 =====
        pgf = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        pgf.pack(fill="x")
        pgf.columnconfigure(1, weight=1)

        ttk.Label(pgf, text="文件：").grid(row=0, column=0, sticky="w")
        self.pb_file = ttk.Progressbar(pgf, mode="determinate", maximum=1000)
        self.pb_file.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        self.lbl_pb_file = ttk.Label(pgf, text="0/0", width=16)
        self.lbl_pb_file.grid(row=0, column=2, sticky="e")

        ttk.Label(pgf, text="片段：").grid(row=1, column=0, sticky="w")
        self.pb_frag = ttk.Progressbar(pgf, mode="determinate", maximum=1000)
        self.pb_frag.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        self.lbl_pb_frag = ttk.Label(pgf, text="0/0", width=16)
        self.lbl_pb_frag.grid(row=1, column=2, sticky="e")

        ttk.Label(pgf, text="批次：").grid(row=2, column=0, sticky="w")
        self.pb_batch = ttk.Progressbar(pgf, mode="determinate", maximum=1000)
        self.pb_batch.grid(row=2, column=1, sticky="ew", padx=6, pady=2)
        self.lbl_pb_batch = ttk.Label(pgf, text="0/0", width=16)
        self.lbl_pb_batch.grid(row=2, column=2, sticky="e")

        # ===== 主体：左文件列表 / 右日志 =====
        body = ttk.Frame(self.root, padding=(10, 8, 10, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 左：文件列表
        lf = ttk.LabelFrame(body, text="文件（双击单文件翻译）", padding=4)
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        self.lst = tk.Listbox(lf, width=42, activestyle="none")
        self.lst.grid(row=0, column=0, sticky="nsew")
        sb1 = ttk.Scrollbar(lf, orient="vertical", command=self.lst.yview)
        sb1.grid(row=0, column=1, sticky="ns")
        self.lst.config(yscrollcommand=sb1.set)
        self.lst.bind("<Double-Button-1>", self._on_double_click)

        # 右：日志
        rf = ttk.LabelFrame(body, text="日志", padding=4)
        rf.grid(row=0, column=1, sticky="nsew")
        rf.rowconfigure(0, weight=1)
        rf.columnconfigure(0, weight=1)

        self.txt = tk.Text(rf, wrap="word", state="disabled", height=10)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(rf, orient="vertical", command=self.txt.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.txt.config(yscrollcommand=sb2.set)
        self.txt.tag_config("err", foreground=COLOR_FAIL_FG)
        self.txt.tag_config("ok", foreground="#1E7A34")
        self.txt.tag_config("dim", foreground="#666666")

    # ============================================================
    # 配置
    # ============================================================
    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        self.cfg[k] = data[k]
        except Exception as e:
            print("读取配置失败:", e)

    def _save_config(self):
        data = {
            "api_key": self.var_api_key.get().strip(),
            "input": self.var_input.get().strip(),
            "output": self.var_output.get().strip(),
            "src_lang": DISPLAY_TO_CODE.get(self.var_src.get(), "auto"),
            "dst_lang": DISPLAY_TO_CODE.get(self.var_dst.get(), "zh-CN"),
            "skip_existing": bool(self.var_skip.get()),
            "batch_size": int(self.var_batch.get()),
            "workers": int(self.var_workers.get()),
            "glossary": self.var_glossary.get().strip(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"保存配置失败：{e}", "err")

    # ============================================================
    # 输入控件
    # ============================================================
    def _toggle_key_show(self):
        self.entry_api_key.config(show="" if self.var_show_key.get() else "*")

    def _choose_input(self):
        d = filedialog.askdirectory(title="选择输入目录")
        if d:
            self.var_input.set(d)

    def _choose_output(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_output.set(d)

    def _choose_glossary(self):
        p = filedialog.askopenfilename(
            title="选择术语表",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if p:
            self.var_glossary.set(p)

    # ============================================================
    # 日志 / 进度条
    # ============================================================
    def _log(self, msg, tag=None):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.txt.config(state="normal")
        self.txt.insert("end", line, tag or ())
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _clear_log(self):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")

    def _set_progress(self, bar, lbl, done, total):
        total = max(int(total or 0), 0)
        done = max(int(done or 0), 0)
        if total <= 0:
            bar["maximum"] = 1000
            bar["value"] = 0
            lbl.config(text="0/0")
            return
        if done > total:
            done = total
        pct = done * 100.0 / total
        bar["maximum"] = 1000
        bar["value"] = int(pct * 10)   # 0 ~ 1000
        lbl.config(text=f"{done}/{total} ({pct:.0f}%)")

    def _reset_progress(self):
        self._last_frag_shown = -1
        self._last_batch_shown = -1
        self._cur_frag_file = None
        for bar, lbl in ((self.pb_file, self.lbl_pb_file),
                         (self.pb_frag, self.lbl_pb_frag),
                         (self.pb_batch, self.lbl_pb_batch)):
            bar["maximum"] = 1000
            bar["value"] = 0
            lbl.config(text="0/0")

    # ============================================================
    # 文件列表
    # ============================================================
    def _scan_files(self, in_dir):
        out = []
        if not in_dir or not os.path.isdir(in_dir):
            return out
        for name in sorted(os.listdir(in_dir)):
            if name.lower().endswith((".html", ".htm")):
                p = os.path.join(in_dir, name)
                if os.path.isfile(p):
                    out.append(name)
        return out

    def _refresh_file_list(self):
        self.lst.delete(0, "end")
        self.file_rows = []
        names = self._scan_files(self.var_input.get().strip())
        for n in names:
            self.lst.insert("end", n)
            self.file_rows.append(n)
        self._list_start_time = time.time()

    def _find_row(self, fname):
        try:
            return self.file_rows.index(fname)
        except ValueError:
            return -1

    def _mark_row(self, idx, kind):
        if idx is None or idx < 0 or idx >= self.lst.size():
            return
        if kind == "doing":
            self.lst.itemconfig(idx, background=COLOR_DOING_BG, foreground=COLOR_DOING_FG)
        elif kind == "done":
            self.lst.itemconfig(idx, background="", foreground=COLOR_DONE_FG)
        elif kind == "fail":
            self.lst.itemconfig(idx, background="", foreground=COLOR_FAIL_FG)
        elif kind == "reset":
            self.lst.itemconfig(idx, background="", foreground="")

    # ============================================================
    # 队列轮询
    # ============================================================
    def _poll_queue(self):
        n = 0
        try:
            while n < 300:
                msg = self.msg_q.get_nowait()
                self._handle_msg(msg)
                n += 1
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg.get("kind")

        if kind == "log":
            self._log(msg.get("text", ""), msg.get("tag"))

        elif kind == "file":
            done = msg.get("done", 0)
            total = msg.get("total", 0)
            self._set_progress(self.pb_file, self.lbl_pb_file, done, total)
            self.lbl_status.config(text=f"文件 {done}/{total}")

        elif kind == "frag":
            done = msg.get("done", 0)
            total = msg.get("total", 0)
            name = msg.get("name", "")

            # 换文件了 → 重置片段进度条
            if name and name != self._cur_frag_file:
                self._cur_frag_file = name
                self._last_frag_shown = -1
                self.pb_frag["maximum"] = 1000
                self.pb_frag["value"] = 0
                self.lbl_pb_frag.config(text="0/0")

            if total <= 0:
                self.pb_frag["maximum"] = 1000
                self.pb_frag["value"] = 0
                self.lbl_pb_frag.config(text="0/0")
                return

            step = max(1, total // 100)
            if done >= total or done - self._last_frag_shown >= step:
                self._last_frag_shown = done
                pct = done * 100.0 / total
                self.pb_frag["maximum"] = 1000
                self.pb_frag["value"] = int(pct * 10)
                self.lbl_pb_frag.config(text=f"{done}/{total} ({pct:.0f}%)")

        elif kind == "batch":
            done = msg.get("done", 0)
            total = msg.get("total", 0)
            self._set_progress(self.pb_batch, self.lbl_pb_batch, done, total)

        elif kind == "file_doing":
            idx = self._find_row(msg.get("name", ""))
            self._mark_row(idx, "doing")
            if idx >= 0:
                self.lst.see(idx)

        elif kind == "file_done":
            idx = self._find_row(msg.get("name", ""))
            self._mark_row(idx, "done")

        elif kind == "file_fail":
            idx = self._find_row(msg.get("name", ""))
            self._mark_row(idx, "fail")

        elif kind == "done":
            self._on_finished(single=False)
            return

        elif kind == "done_single":
            self._on_finished(single=True)
            return

        elif kind == "error":
            self._log(msg.get("text", "未知错误"), "err")
            self._on_finished(single=msg.get("single", False))
            return

    # ============================================================
    # 启动 / 停止
    # ============================================================
    def _collect_params(self):
        api_key = self.var_api_key.get().strip()
        in_dir = self.var_input.get().strip()
        out_dir = self.var_output.get().strip()
        glossary = self.var_glossary.get().strip()

        if not api_key:
            messagebox.showwarning("提示", "请填写 API Key")
            return None
        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showwarning("提示", "输入目录无效")
            return None
        if not out_dir:
            messagebox.showwarning("提示", "请选择输出目录")
            return None

        src_code = DISPLAY_TO_CODE.get(self.var_src.get(), "auto")
        dst_code = DISPLAY_TO_CODE.get(self.var_dst.get(), "zh-CN")
        if dst_code == "auto":
            messagebox.showwarning("提示", "目标语言不能是“自动检测”")
            return None

        try:
            batch_size = int(self.var_batch.get())
            workers = int(self.var_workers.get())
        except Exception:
            messagebox.showwarning("提示", "批量 / 并发必须是整数")
            return None

        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)

        return {
            "api_key": api_key,
            "in_dir": in_dir,
            "out_dir": out_dir,
            "src_code": src_code,
            "dst_code": dst_code,
            "glossary": glossary if glossary and os.path.isfile(glossary) else None,
            "batch_size": batch_size,
            "workers": workers,
        }

    def _on_start(self):
        if self.running:
            return
        params = self._collect_params()
        if not params:
            return

        self._save_config()
        self._refresh_file_list()

        if not self.file_rows:
            messagebox.showinfo("提示", "输入目录里没有 .html / .htm 文件")
            return

        self.running = True
        self.single_mode = False
        self.stop_flag = threading.Event()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._reset_progress()
        self.lbl_status.config(text="准备中…")

        self.worker = threading.Thread(
            target=self._worker_batch, args=(params,), daemon=True
        )
        self.worker.start()

    def _on_double_click(self, _evt):
        if self.running:
            messagebox.showinfo("提示", "正在运行中，请先停止")
            return
        sel = self.lst.curselection()
        if not sel:
            return
        fname = self.file_rows[sel[0]]
        params = self._collect_params()
        if not params:
            return

        self._save_config()
        if self.lst.size() == 0:
            self._refresh_file_list()

        self.running = True
        self.single_mode = True
        self.stop_flag = threading.Event()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._reset_progress()
        self.lbl_status.config(text=f"单文件：{fname}")

        self.worker = threading.Thread(
            target=self._worker_single, args=(params, fname), daemon=True
        )
        self.worker.start()

    def _on_stop(self):
        if not self.running:
            return
        self.stop_flag.set()
        self.lbl_status.config(text="正在停止…")
        self._log("已请求停止，等待当前批次结束…", "dim")

    def _on_finished(self, single=False):
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="空闲" if not single else "空闲（单文件完成）")

    # ============================================================
    # 回调（在 worker 线程里执行，只往队列塞消息）
    # ============================================================
    def _cb_log(self, text):
        try:
            self.msg_q.put({"kind": "log", "text": str(text)})
        except Exception:
            pass

    def _cb_file(self, total, done, fname):
        self.msg_q.put({"kind": "file", "total": total, "done": done, "name": fname})

    def _cb_frag(self, total, done, fname):
        self.msg_q.put({"kind": "frag", "total": total, "done": done, "name": fname})

    def _cb_batch(self, done, total):
        self.msg_q.put({"kind": "batch", "total": total, "done": done})

    # ============================================================
    # worker：批量
    # ============================================================
    def _worker_batch(self, p):
        try:
            self._cb_log("开始批量翻译…")

            th.run_translation(
                p["in_dir"],
                p["out_dir"],
                CACHE_DIR,
                p["api_key"],
                p["src_code"],
                p["dst_code"],
                p["glossary"],
                p["batch_size"],
                p["workers"],
                on_file=self._cb_file,
                on_fragment=self._cb_frag,
                on_batch=self._cb_batch,
                log_cb=self._cb_log,
                stop_flag=self.stop_flag,
            )
            self.msg_q.put({"kind": "done", "single": False})
        except Exception as e:
            self.msg_q.put({
                "kind": "error",
                "text": f"运行出错：{e}\n{traceback.format_exc()}",
                "single": False,
            })

    # ============================================================
    # worker：单文件
    # ============================================================
    def _worker_single(self, p, fname):
        tmp_in = None
        try:
            self.msg_q.put({"kind": "file_doing", "name": fname})
            self.msg_q.put({"kind": "file", "total": 1, "done": 0, "name": fname})

            tmp_in = os.path.join(CACHE_DIR, "_single_in")
            if os.path.isdir(tmp_in):
                shutil.rmtree(tmp_in, ignore_errors=True)
            os.makedirs(tmp_in, exist_ok=True)

            src_path = os.path.join(p["in_dir"], fname)
            shutil.copy2(src_path, os.path.join(tmp_in, fname))

            th.run_translation(
                tmp_in,
                p["out_dir"],
                CACHE_DIR,
                p["api_key"],
                p["src_code"],
                p["dst_code"],
                p["glossary"],
                p["batch_size"],
                p["workers"],
                on_file=self._cb_file,
                on_fragment=self._cb_frag,
                on_batch=self._cb_batch,
                log_cb=self._cb_log,
                stop_flag=self.stop_flag,
            )

            self.msg_q.put({"kind": "file_done", "name": fname})
            self.msg_q.put({"kind": "file", "total": 1, "done": 1, "name": fname})
            self._cb_log(f"单文件完成：{fname}")
            self.msg_q.put({"kind": "done_single", "single": True})
        except Exception as e:
            self.msg_q.put({"kind": "file_fail", "name": fname})
            self.msg_q.put({
                "kind": "error",
                "text": f"单文件出错：{e}\n{traceback.format_exc()}",
                "single": True,
            })
        finally:
            if tmp_in and os.path.isdir(tmp_in):
                shutil.rmtree(tmp_in, ignore_errors=True)

    # ============================================================
    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("确认", "任务运行中，确定退出？"):
                return
            self.stop_flag.set()
        self._save_config()
        self.root.destroy()


# ============================================================
def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
