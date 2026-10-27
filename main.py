"""MultiDL 桌面版入口。"""
from __future__ import annotations

import os
import sys
import argparse
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 先装好日志与全局异常钩子，再做其余导入——这样连"导入期崩溃"也能写进日志。
import diag  # noqa: E402

diag.setup_logging()
diag.install()

from engine.manager import Manager        # noqa: E402
from gui.app import App                  # noqa: E402


def default_dirs():
    home = os.path.expanduser("~")
    base = os.environ.get("LOCALAPPDATA") or os.path.join(home, ".local", "share")
    return os.path.join(home, "Downloads", "MultiDL"), os.path.join(base, "MultiDL")


def main():
    log = diag.get_logger()
    dl_dir, data_dir = default_dirs()
    ap = argparse.ArgumentParser(description="MultiDL 多线程下载器")
    ap.add_argument("--dir", default=None, help="保存目录（默认读配置，其次 ~/Downloads/MultiDL）")
    ap.add_argument("--data", default=None)
    ap.add_argument("--conn", type=int, default=0, help="每任务连接数，0=自动（按文件大小）")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--speed", type=int, default=0, help="KB/s，0=不限")
    ap.add_argument("--port", type=int, default=8765, help="浏览器扩展端口，0=关闭")
    ap.add_argument("--tray", action="store_true", help="启动后驻留系统托盘，不弹出主窗口")
    ap.add_argument("--proxy", default=None,
                    help="代理 host:port（如 127.0.0.1:7890）；默认读 Windows 系统代理；none=强制直连")
    ap.add_argument("--diagnose", action="store_true", help="启动后直接打开网络瓶颈排查")
    ap.add_argument("url", nargs="?", default=None)
    a = ap.parse_args()
    log.info("启动参数: dir=%s data=%s conn=%s parallel=%s speed=%s port=%s proxy=%s url=%s",
             a.dir, a.data, a.conn, a.parallel, a.speed, a.port,
             diag.mask_proxy(a.proxy), diag.mask_url(a.url))

    mgr = Manager(a.data or data_dir, a.dir or dl_dir, max_parallel=a.parallel,
                  speed_limit=a.speed * 1024, default_conn=a.conn)
    if a.proxy is not None:        # 命令行显式指定 → 优先于配置里的网络模式
        from engine.proxy import set_override
        mgr._proxy_from_cli = True
        set_override(a.proxy)
    if a.dir:                      # 命令行显式指定 → 覆盖配置文件里的目录
        mgr.set_download_dir(a.dir)
    if a.conn != 0 or a.parallel != 3 or a.speed != 0:
        mgr.set_limits(max_parallel=a.parallel, default_conn=a.conn,
                       speed_limit=a.speed * 1024)

    api_port = None
    if a.port:
        try:
            from server.api import make_server
            srv = make_server(mgr, a.port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            api_port = a.port
            log.info("本地 API 已启动: 127.0.0.1:%s", a.port)
        except OSError as e:
            api_port = None          # 端口被占用（可能已有实例）→ 静默跳过
            log.warning("API 端口 %s 不可用（可能已有实例在跑）: %s", a.port, e)

    if a.url:
        try:
            mgr.add(a.url)
        except Exception:
            log.exception("命令行 URL 添加失败: %s", diag.mask_url(a.url))

    app = App(mgr, api_port)
    diag.install(app)                # 接管 tkinter 界面回调里的异常
    if a.diagnose:
        app.after(400, app.net_diag)
    if a.tray:
        app.withdraw()               # 只驻留托盘，不弹主窗口
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        diag.handle_exception(*sys.exc_info())
        sys.exit(1)
