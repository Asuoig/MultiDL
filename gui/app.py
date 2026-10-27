"""MultiDL 桌面界面（tkinter / ttk，纯标准库）。"""
from __future__ import annotations

import os
import sys
import time
import queue
import webbrowser
import subprocess
import tkinter as tk
from collections import deque
from tkinter import ttk, filedialog, messagebox

APP = "MultiDL"
APP_TITLE = "MultiDL 下载器"

CONN_CHOICES = ["自动", "2", "4", "8", "12", "16", "24", "32"]


def _conn_to_label(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "自动"
    return "自动" if v <= 0 else str(v)


def _label_to_conn(s):
    s = str(s).strip()
    return 0 if s in ("自动", "", "auto") else int(s)

STATE_TXT = {
    "queued": "排队中", "probing": "探测中", "running": "下载中",
    "paused": "已暂停", "done": "已完成", "error": "出错", "canceled": "已取消",
}
STATE_TAG = {
    "running": "run", "probing": "run", "queued": "wait", "paused": "wait",
    "done": "ok", "error": "err", "canceled": "err",
}

# 网络模式（界面文案 ↔ 内部值）
NET_LABELS = {"auto": "自动", "direct": "直连", "proxy": "代理"}
NET_MODES = {v: k for k, v in NET_LABELS.items()}

BAR_FULL = "\u25a0"      # ■ 实心方块
BAR_EMPTY = "\u25a1"     # □ 空心方块（浅色轨道）


def human(n, suffix=""):
    if not n:
        return "-"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            txt = ("%d %s" % (n, u)) if u == "B" else ("%.2f %s" % (n, u))
            return txt + suffix
        n /= 1024.0
    return "-"


def human_speed(n):
    return human(n, "/s") if n else "-"


def eta_text(remain, speed):
    if not speed or speed <= 0:
        return "-"
    s = int(remain / speed)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s // 3600} 时 {(s % 3600) // 60} 分"


def bar_text(pct, width=10):
    if pct is None:
        return ""
    pct = max(0.0, min(100.0, pct))
    k = int(round(pct / 100.0 * width))
    return BAR_FULL * k + BAR_EMPTY * (width - k)


# ---- 速度可视化 ----
# 实心块 ▁▂▃▄▅▆▇█ 对比度最高（实心 vs 空白），小字号下比盲文点阵看得清；
# 点数控制在 16 个以内，避免一排大黑块的笨重感。
SPARK_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"   # ▁▂▃▄▅▆▇█
SPARK_WIDTH = 16


def sparkline(hist, peak=None, width=SPARK_WIDTH):
    """把速度历史画成柱状曲线。

    高度以「窗口内平均速度」为基准（不是峰值）：
    - 速度稳定时曲线落在中间档，不会顶成一排满格黑块；
    - 掉速会明显掉到底部，提速会顶到顶部，变化一眼可见；
    - 样本不足 3 个时退回用 peak / 首个样本兜底。
    """
    if not hist:
        return ""
    data = list(hist)[-width:]
    if len(data) < width:                      # 右侧对齐，左边补空
        data = [None] * (width - len(data)) + data

    vals = [v for v in data if v]
    if len(vals) >= 3:
        base = sum(vals) / len(vals)
    elif peak:
        base = peak
    elif vals:
        base = vals[0]
    else:
        return ""
    if base <= 0:
        return ""

    n = len(SPARK_CHARS) - 1
    out = []
    for v in data:
        if not v:
            out.append(" ")
            continue
        # 灵敏度：比平均慢 25% → 最低档；等于平均 → 中间档；快 25% → 最高档。
        # 区间收窄是为了让 ±20% 左右的日常波动也看得见（区间太宽会显得一条直线）。
        k = int(round((v / base - 0.75) / 0.5 * n))
        out.append(SPARK_CHARS[max(0, min(n, k))])
    return "".join(out)


class Dialog(tk.Toplevel):
    def __init__(self, master, title, w=520, h=300):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.result = None
        self.configure(bg="#f7f8fa")
        self.geometry(f"{w}x{h}")
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def buttons(self, ok_text="确定"):
        f = ttk.Frame(self)
        f.pack(side="bottom", fill="x", padx=14, pady=12)
        ttk.Button(f, text="取消", command=self.destroy, width=10).pack(side="right")
        b = ttk.Button(f, text=ok_text, command=self.on_ok, width=10)
        b.pack(side="right", padx=(0, 8))
        b.focus_set()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.on_ok())

    def on_ok(self):
        self.destroy()


class AddDialog(Dialog):
    def __init__(self, master, defaults):
        super().__init__(master, "添加下载任务", 560, 278)
        pad = {"padx": 14, "pady": (10, 0), "anchor": "w"}

        ttk.Label(self, text="下载地址（每行一个，可批量）").pack(**pad)
        self.txt = tk.Text(self, height=4, wrap="none", font=("Consolas", 9),
                           relief="solid", bd=1, highlightthickness=0)
        self.txt.pack(fill="x", padx=14, pady=(3, 0))
        try:
            clip = master.clipboard_get()
            if clip.strip().startswith(("http://", "https://", "ftp://")):
                self.txt.insert("1.0", clip.strip().splitlines()[0])
        except Exception:
            pass

        row = ttk.Frame(self)
        row.pack(fill="x", padx=14, pady=(10, 0))
        ttk.Label(row, text="保存到").pack(side="left")
        self.dir = tk.StringVar(value=defaults["download_dir"])
        ttk.Entry(row, textvariable=self.dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="浏览", width=7,
                   command=self._pick).pack(side="left")

        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=14, pady=(8, 0))
        ttk.Label(row2, text="连接数").pack(side="left")
        self.conn = tk.StringVar(value=_conn_to_label(defaults["default_conn"]))
        ttk.Combobox(row2, values=CONN_CHOICES, textvariable=self.conn,
                     width=5, state="readonly").pack(side="left", padx=(6, 18))
        ttk.Label(row2, text="Referer（防盗链必填）").pack(side="left")
        self.ref = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ref).pack(side="left", fill="x",
                                                   expand=True, padx=6)

        self.autostart = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="加入后立即开始", variable=self.autostart).pack(
            padx=14, pady=(10, 0), anchor="w")

        self.buttons("开始下载")
        self.txt.focus_set()

    def _pick(self):
        d = filedialog.askdirectory(initialdir=self.dir.get(), parent=self)
        if d:
            self.dir.set(d)

    def on_ok(self):
        raw = self.txt.get("1.0", "end").strip()
        urls = [x.strip() for x in raw.splitlines() if x.strip()]
        if not urls:
            messagebox.showwarning("提示", "请填写下载地址", parent=self)
            return
        self.result = {
            "urls": urls,
            "dir": self.dir.get().strip(),
            "conn": _label_to_conn(self.conn.get()),
            "referer": self.ref.get().strip() or None,
            "autostart": bool(self.autostart.get()),
        }
        self.destroy()


class OptionsDialog(Dialog):
    def __init__(self, master, settings):
        super().__init__(master, "选项", 520, 272)
        pad = {"padx": 16, "pady": (12, 0)}

        ttk.Label(self, text="默认下载目录").pack(**pad, anchor="w")
        row = tk.Frame(self, bg="#f7f8fa")
        row.pack(fill="x", padx=16, pady=(3, 0))
        self.dir = tk.StringVar(value=settings["download_dir"])
        ttk.Entry(row, textvariable=self.dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览", width=7, command=self._pick).pack(side="left", padx=6)

        grid = tk.Frame(self, bg="#f7f8fa")
        grid.pack(fill="x", padx=16, pady=(14, 0))
        ttk.Label(grid, text="同时下载任务数").grid(row=0, column=0, sticky="w", pady=5)
        self.par = tk.IntVar(value=settings["max_parallel"])
        ttk.Spinbox(grid, from_=1, to=10, textvariable=self.par, width=6).grid(
            row=0, column=1, sticky="w", padx=10)

        ttk.Label(grid, text="每任务默认连接数").grid(row=1, column=0, sticky="w", pady=5)
        self.conn = tk.StringVar(value=_conn_to_label(settings["default_conn"]))
        ttk.Combobox(grid, values=CONN_CHOICES, textvariable=self.conn,
                     width=6, state="readonly").grid(row=1, column=1, sticky="w", padx=10)

        ttk.Label(grid, text="全局限速（KB/s，0=不限）").grid(row=2, column=0, sticky="w", pady=5)
        self.spd = tk.IntVar(value=int(settings["speed_limit"] // 1024))
        ttk.Spinbox(grid, from_=0, to=999999, increment=128,
                    textvariable=self.spd, width=10).grid(row=2, column=1, sticky="w", padx=10)

        self.clip = tk.BooleanVar(value=bool(settings.get("clipboard_watch", True)))
        ttk.Checkbutton(self, text="监视剪贴板：复制下载链接后询问是否下载",
                        variable=self.clip).pack(padx=16, pady=(14, 0), anchor="w")

        self.buttons("保存")
        self.bind("<Return>", lambda e: self.on_ok())

    def _pick(self):
        d = filedialog.askdirectory(initialdir=self.dir.get(), parent=self)
        if d:
            self.dir.set(d)

    def on_ok(self):
        self.result = {
            "download_dir": self.dir.get().strip(),
            "max_parallel": int(self.par.get() or 3),
            "default_conn": _label_to_conn(self.conn.get()),
            "speed_limit": int(self.spd.get() or 0) * 1024,
            "clipboard_watch": bool(self.clip.get()),
        }
        self.destroy()


class App(tk.Tk):
    def __init__(self, mgr, api_port=None):
        super().__init__()
        self.mgr = mgr
        self.api_port = api_port
        self._clip_last = ""
        self._sort = ("created", True)
        self._last_total_speed = 0.0
        self._speed_hist = {}                  # tid -> deque[float]（速度曲线数据）
        self._dl_prev = {}                     # tid -> (已下载字节, 时间戳)，用来算瞬时速度
        self._net_hint_at = 0.0
        self._tray = None

        self.title(APP_TITLE)
        self.geometry("1260x680")
        self.minsize(860, 480)
        self.configure(bg="#f7f8fa")
        self._init_style()
        self._build_menu()
        self._build_toolbar()
        self._build_list()
        self._build_status()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-n>", lambda e: self.add_task())
        self.bind("<Delete>", lambda e: self.remove_selected())
        self.bind("<F5>", lambda e: self.refresh())

        self.after(300, self._tick)
        self.after(900, self._clip_watch)
        self._set_icon()
        self._init_tray()

    # ---------------- 外观 ----------------
    def _init_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except Exception:
            pass
        self.option_add("*Font", "{Microsoft YaHei UI} 9")
        st.configure("Treeview", rowheight=24, fieldbackground="#ffffff",
                     background="#ffffff", borderwidth=0)
        st.configure("Treeview.Heading", padding=(6, 5))
        st.map("Treeview", background=[("selected", "#cfe3ff")],
               foreground=[("selected", "#0b1b33")])
        st.configure("Toolbar.TFrame", background="#f2f4f7")
        st.configure("Status.TFrame", background="#eef1f5")

    def _set_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ico = os.path.join(base, "assets", "icon.ico")
        try:
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="添加任务...\tCtrl+N", command=self.add_task)
        f.add_separator()
        f.add_command(label="开始选中", command=lambda: self.act("resume"))
        f.add_command(label="暂停选中", command=lambda: self.act("pause"))
        f.add_command(label="删除选中\tDel", command=self.remove_selected)
        f.add_separator()
        f.add_command(label="全部开始", command=self.mgr.start_all)
        f.add_command(label="全部暂停", command=self.mgr.pause_all)
        f.add_separator()
        f.add_command(label="退出", command=self.on_close)
        m.add_cascade(label="任务", menu=f)

        o = tk.Menu(m, tearoff=0)
        o.add_command(label="选项...", command=self.options)
        o.add_command(label="打开下载目录", command=self.open_dir)
        m.add_cascade(label="选项", menu=o)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="打开日志文件夹", command=self.open_logs)
        h.add_command(label="复制诊断信息（反馈用）", command=self.copy_diag)
        h.add_separator()
        h.add_command(label="关于", command=self.about)
        m.add_cascade(label="帮助", menu=h)
        self.config(menu=m)

    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame", padding=(8, 6))
        bar.pack(fill="x")
        for txt, cmd in (("添加任务", self.add_task), ("开始", lambda: self.act("resume")),
                         ("暂停", lambda: self.act("pause")),
                         ("删除", self.remove_selected), ("选项", self.options)):
            ttk.Button(bar, text=txt, command=cmd, width=10).pack(side="left", padx=2)

        self.quick = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.quick)
        e.pack(side="left", fill="x", expand=True, padx=(12, 6))
        e.bind("<Return>", lambda ev: self.quick_add())
        ttk.Button(bar, text="下载", command=self.quick_add, width=8).pack(side="left")

    def _build_list(self):
        wrap = ttk.Frame(self, padding=(8, 6))
        wrap.pack(fill="both", expand=True)

        cols = ("name", "size", "progress", "graph", "speed", "remain", "eta", "conn", "state", "url")
        heads = {"name": "文件名", "size": "大小", "progress": "进度", "graph": "速度曲线",
                 "speed": "速度", "remain": "剩余大小", "eta": "剩余时间", "conn": "连接",
                 "state": "状态", "url": "地址"}
        widths = {"name": 196, "size": 80, "progress": 152, "graph": 148, "speed": 84,
                  "remain": 84, "eta": 78, "conn": 48, "state": 72, "url": 220}

        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.tree.heading(c, text=heads[c], command=lambda cc=c: self.sort_by(cc))
            anchor = "w" if c in ("name", "url") else "center"
            self.tree.column(c, width=widths[c], anchor=anchor,
                             stretch=(c in ("name", "url")))
        self.tree.tag_configure("ok", foreground="#15803d")
        self.tree.tag_configure("err", foreground="#b91c1c")
        self.tree.tag_configure("run", foreground="#0b1b33")
        self.tree.tag_configure("wait", foreground="#6b7280")
        self.tree.tag_configure("odd", background="#fbfcfd")
        self.tree.tag_configure("even", background="#ffffff")

        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.placeholder = ttk.Label(
            wrap, text="把下载链接粘贴到上方输入框，或按「添加任务」\n"
                       "支持 HTTP / HTTPS / FTP / HLS(m3u8)",
            foreground="#9aa3b0", justify="center", background="#ffffff")
        self.placeholder.place(relx=0.5, rely=0.35, anchor="center")

        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Button-3>", self._ctx_menu)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="打开文件", command=self.open_selected)
        self.menu.add_command(label="打开所在文件夹", command=self.open_selected_dir)
        self.menu.add_separator()
        self.menu.add_command(label="开始/继续", command=lambda: self.act("resume"))
        self.menu.add_command(label="暂停", command=lambda: self.act("pause"))
        self.menu.add_command(label="复制下载地址", command=self.copy_url)
        self.menu.add_separator()
        self.menu.add_command(label="从列表移除", command=lambda: self.remove_selected(False))
        self.menu.add_command(label="删除任务和文件", command=lambda: self.remove_selected(True))

    def _build_status(self):
        s = ttk.Frame(self, style="Status.TFrame", padding=(10, 5))
        s.pack(fill="x", side="bottom")
        self.st_left = ttk.Label(s, text="就绪", background="#eef1f5")
        self.st_left.pack(side="left")
        self.st_right = ttk.Label(s, text="", background="#eef1f5")
        self.st_right.pack(side="right")
        if self.api_port:
            ttk.Label(s, text=f"浏览器扩展端口 {self.api_port}",
                      background="#eef1f5", foreground="#8a93a0").pack(side="right", padx=14)

        # ---- 网络模式选择：自动 / 直连 / 代理（默认自动，选择会持久化） ----
        ttk.Label(s, text="网络", background="#eef1f5").pack(side="left", padx=(16, 5))
        self.net_box = ttk.Combobox(s, values=list(NET_LABELS.values()),
                                    width=5, state="readonly")
        self.net_box.set(NET_LABELS.get(self.mgr.proxy_mode, "自动"))
        self.net_box.bind("<<ComboboxSelected>>", self._on_net_mode)
        self.net_box.pack(side="left")
        self.net_hint = ttk.Label(s, text="", background="#eef1f5", foreground="#8a93a0")
        self.net_hint.pack(side="left", padx=(8, 0))
        self._refresh_net_hint()

    # ---------------- 网络模式 ----------------
    def _on_net_mode(self, _ev=None):
        mode = NET_MODES.get(self.net_box.get(), "auto")
        try:
            self.mgr.set_proxy_mode(mode)
        except Exception:
            pass
        self._refresh_net_hint()

    def _refresh_net_hint(self):
        try:
            from engine.proxy import describe
            self.net_hint.config(text=describe())
        except Exception:
            self.net_hint.config(text="")

    # ---------------- 交互 ----------------
    def selected_ids(self):
        return list(self.tree.selection())

    def act(self, action):
        ids = self.selected_ids()
        if not ids:
            return
        fn = {"pause": self.mgr.pause, "resume": self.mgr.resume,
              "cancel": self.mgr.cancel}[action]
        for tid in ids:
            fn(tid)
        self.refresh()

    def remove_selected(self, with_file=None):
        ids = self.selected_ids()
        if not ids:
            return
        if with_file is None:
            with_file = messagebox.askyesno(
                "删除任务", f"同时删除 {len(ids)} 个已下载文件？\n选「否」只从列表移除。",
                parent=self)
        for tid in ids:
            self.mgr.remove(tid, with_file)
        self.refresh()

    def copy_url(self):
        ids = self.selected_ids()
        if ids:
            t = self.mgr.get(ids[0])
            if t:
                self.clipboard_clear()
                self.clipboard_append(t.url)

    def open_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        t = self.mgr.get(ids[0])
        if not t:
            return
        if t.state != "done" or not os.path.exists(t.target):
            messagebox.showinfo("提示", "该任务还没有下载完成。", parent=self)
            return
        try:
            os.startfile(t.target)
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self)

    def open_selected_dir(self):
        ids = self.selected_ids()
        if not ids:
            self.open_dir()
            return
        t = self.mgr.get(ids[0])
        if t:
            self._reveal(t.target)

    def open_dir(self):
        self._reveal(os.path.join(self.mgr.download_dir, "x"))

    def _reveal(self, path):
        try:
            if os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                d = os.path.dirname(path)
                if os.path.isdir(d):
                    os.startfile(d)
        except Exception:
            pass

    def _ctx_menu(self, ev):
        row = self.tree.identify_row(ev.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(ev.x_root, ev.y_root)

    def quick_add(self):
        url = self.quick.get().strip()
        if not url:
            self.add_task()
            return
        try:
            self.mgr.add(url)
            self.quick.set("")
            self.refresh()
        except Exception as e:
            messagebox.showerror("添加失败", str(e), parent=self)

    def add_task(self):
        dlg = AddDialog(self, self.mgr.settings())
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        if r["dir"] and os.path.abspath(r["dir"]) != self.mgr.download_dir:
            self.mgr.set_download_dir(r["dir"])
        for u in r["urls"]:
            try:
                self.mgr.add(u, referer=r["referer"], max_conn=r["conn"],
                             autostart=r["autostart"])
            except Exception as e:
                messagebox.showerror("添加失败", f"{u}\n{e}", parent=self)
        self.refresh()

    def options(self):
        dlg = OptionsDialog(self, self.mgr.settings())
        self.wait_window(dlg)
        if not dlg.result:
            return
        r = dlg.result
        self.mgr.set_download_dir(r["download_dir"])
        self.mgr.set_limits(max_parallel=r["max_parallel"],
                            default_conn=r["default_conn"],
                            speed_limit=r["speed_limit"])
        self.mgr.set_clipboard_watch(r["clipboard_watch"])
        self.refresh()

    def about(self):
        messagebox.showinfo(
            "关于",
            "MultiDL 1.0.13\n\n多线程分段下载器 · 独立实现\n"
            "· 自适应增连   · 断点续传   · 单文件定位写\n"
            "· 支持 HTTP/HTTPS/FTP/HLS\n\n"
            "纯 Python 标准库实现，无第三方依赖。",
            parent=self)

    def open_logs(self):
        import diag
        diag.open_log_folder()

    def copy_diag(self):
        import diag
        try:
            self.clipboard_clear()
            self.clipboard_append(diag.report_text())
            messagebox.showinfo(
                "已复制",
                "诊断信息（环境 + 最近日志）已复制到剪贴板。\n"
                "反馈问题时直接粘贴到 GitHub Issue 即可。",
                parent=self)
        except Exception as e:
            messagebox.showerror("复制失败", str(e), parent=self)

    def sort_by(self, col):
        key, rev = self._sort
        self._sort = (col, not rev if key == col else False)

    # ---------------- 剪贴板监视 ----------------
    def _clip_watch(self):
        if not getattr(self.mgr, "clipboard_watch", True):
            self.after(900, self._clip_watch)
            return
        try:
            txt = self.clipboard_get().strip()
        except Exception:
            txt = ""
        if (txt and txt != self._clip_last
                and txt.lower().startswith(("http://", "https://", "ftp://"))
                and len(txt) < 2048 and "\n" not in txt):
            self._clip_last = txt
            if txt not in [self.mgr.get(i).url for i in self.mgr.order if self.mgr.get(i)]:
                if messagebox.askyesno("发现下载链接",
                                       f"是否下载这个链接？\n\n{txt[:120]}", parent=self):
                    try:
                        self.mgr.add(txt)
                    except Exception:
                        pass
        self.after(900, self._clip_watch)

    # ---------------- 刷新 ----------------
    def _tick(self):
        try:
            self.refresh()
        except Exception:
            pass
        self.after(400, self._tick)

    def refresh(self):
        snap = self.mgr.snapshots()
        now = time.monotonic()
        key, rev = self._sort
        idx = {"name": "name", "size": "size", "progress": "percent",
               "graph": "speed", "speed": "speed", "remain": "size",
               "eta": "size", "conn": "connections",
               "state": "state", "url": "url"}
        k = idx.get(key, "created")
        snap.sort(key=lambda s: (s.get(k) is None, s.get(k)), reverse=rev)

        seen = set()
        total_speed = 0.0
        total_remain = 0
        total_size = 0
        total_done = 0
        for i, s in enumerate(snap):
            seen.add(s["id"])
            total_speed += s["speed"] or 0
            remain = max(0, (s["size"] or 0) - (s["downloaded"] or 0))
            if s["state"] in ("running", "probing", "queued", "paused"):
                total_remain += remain
            if s["size"]:
                total_size += s["size"]
            total_done += s["downloaded"] or 0

            # 速度曲线：用「两次刷新间的字节增量」算瞬时速度。
            # task.speed 是累计平均速度，会把波动抹平（曲线永远一条直线），
            # 而且刚启动时还没更新就是 0；增量法能真实反映每一次卡顿和冲高。
            hist = self._speed_hist.get(s["id"])
            if hist is None:
                hist = self._speed_hist[s["id"]] = deque(maxlen=SPARK_WIDTH)
            if s["state"] in ("running", "probing"):
                done_bytes = int(s["downloaded"] or 0)
                prev = self._dl_prev.get(s["id"])
                if prev is not None:
                    dt = now - prev[1]
                    inst = (done_bytes - prev[0]) / dt if dt > 0.05 else 0.0
                    hist.append(max(0.0, inst))
                self._dl_prev[s["id"]] = (done_bytes, now)

            pct = s["percent"]
            if s["size"]:
                remain_txt = human(remain)
            elif s["state"] in ("running", "probing", "paused"):
                remain_txt = "未知"      # 服务器未给总长度（chunked）
            else:
                remain_txt = "-"
            vals = (
                s["name"] or os.path.basename(s["target"] or ""),
                human(s["size"]) if s["size"] else "-",
                (bar_text(pct) + (" %5.1f%%" % pct if pct is not None else "")) if pct is not None
                else bar_text(None),
                sparkline(hist) if hist else "",
                human_speed(s["speed"]),
                remain_txt,
                eta_text(remain, s["speed"]) if s["state"] == "running" else "-",
                f'{s["connections"]}/{s["max_conn"]}' if s["max_conn"] else "自动",
                STATE_TXT.get(s["state"], s["state"]),
                s["url"],
            )
            tag = STATE_TAG.get(s["state"], "")
            tags = (tag, "odd" if i % 2 else "even")
            if self.tree.exists(s["id"]):
                self.tree.item(s["id"], values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=s["id"], values=vals, tags=tags)
        for iid in list(self.tree.get_children()):
            if iid not in seen:
                self.tree.delete(iid)
                self._speed_hist.pop(iid, None)
                self._dl_prev.pop(iid, None)

        if snap:
            self.placeholder.place_forget()
        else:
            self.placeholder.place(relx=0.5, rely=0.35, anchor="center")

        st = self.mgr.stats()
        self.st_left.config(
            text=f"共 {st['total']} 个任务   进行中 {st['active']}   等待 {st['waiting']}   "
                 f"已完成 {st['done']}")
        # 状态栏：总速度 + 总剩余 + 总体进度
        if total_size:
            overall = total_done / total_size * 100.0
            tail = f"总速度  {human_speed(total_speed)}    剩余  {human(total_remain)}    " \
                   f"总体 {overall:.1f}%"
        else:
            tail = f"总速度  {human_speed(total_speed)}    剩余  {human(total_remain)}"
        self.st_right.config(text=tail)
        self.title(f"{APP_TITLE}  —  {human_speed(total_speed)}" if total_speed
                   else APP_TITLE)
        # 系统代理可能被外部改动，隔几秒刷新一次提示
        if now - self._net_hint_at > 3:
            self._net_hint_at = now
            self._refresh_net_hint()
        if self._tray:
            try:
                self._tray.set_tooltip(f"MultiDL — {human_speed(total_speed)}" if total_speed
                                       else "MultiDL")
            except Exception:
                pass

    # ---------------- 系统托盘 ----------------
    def _init_tray(self):
        try:
            from tray import Tray
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            icon = os.path.join(base, "assets", "icon.ico")
            self._tray = Tray(icon, "MultiDL", menu=[
                ("显示主窗口", "show"),
                ("暂停全部", "pause_all"),
                ("继续全部", "start_all"),
                (None, None),
                ("退出", "quit"),
            ])
            self._tray.start()
            self.after(200, self._poll_tray)
        except Exception:
            self._tray = None

    def _poll_tray(self):
        if self._tray:
            try:
                while True:
                    self._handle_tray(self._tray.actions.get_nowait())
            except queue.Empty:
                pass
            except Exception:
                pass
        self.after(250, self._poll_tray)

    def _handle_tray(self, act):
        if act == "show":
            self.deiconify()
            self.lift()
            self.focus_force()
        elif act == "pause_all":
            self.mgr.pause_all()
        elif act == "start_all":
            self.mgr.start_all()
        elif act == "quit":
            self._really_quit()

    def on_close(self):
        # 有托盘 → 关闭窗口只是隐藏到托盘，程序继续在后台跑；真正退出用托盘菜单「退出」
        if self._tray:
            try:
                self.mgr.save_now()
            except Exception:
                pass
            self.withdraw()
            return
        self._really_quit()

    def _really_quit(self):
        try:
            self.mgr.save_now()
        except Exception:
            pass
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        self.destroy()
