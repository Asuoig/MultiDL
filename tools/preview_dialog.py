"""临时：预览对话框布局（截图用）。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.manager import Manager
from gui.app import App, AddDialog, OptionsDialog

which = sys.argv[1] if len(sys.argv) > 1 else "add"
tmp = os.path.join(ROOT, "test", "_dlg")
mgr = Manager(os.path.join(tmp, "data"), os.path.join(tmp, "dl"))
app = App(mgr, None)


def open_dlg():
    if which == "add":
        AddDialog(app, mgr.settings())
    else:
        OptionsDialog(app, mgr.settings())


app.after(500, open_dlg)
app.mainloop()
