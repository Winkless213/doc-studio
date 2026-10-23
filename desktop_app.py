# -*- coding: utf-8 -*-
"""
文档处理工具 — 本地桌面版(双击即开)。
一个原生窗口里跑完整文档处理: 自动判别矢量/图片型 → 翻译/去品牌 / 图片向导(删页+擦厂标 LaMa) → 导出。
不依赖 ERP、不连 prod、不起 mysql/java。重依赖(torch/LaMa/opencv)装在本目录 .venv。

启动: .venv\\Scripts\\python.exe desktop_app.py   (或双击 启动文档工具.cmd)
原理: 后台线程起 FastAPI(server.py 同款引擎) → pywebview 开原生窗口加载本地 UI(static/desktop.html)。
"""
import os
import sys
import threading
import socket
import time

# PyInstaller 的窗口模式（console=False）下 sys.stdout/stderr 为 None，
# uvicorn 初始化日志时会调用 sys.stdout.isatty() 直接崩溃；这里补一个空流。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 让桌面 UI 走"无鉴权"的本地端点(server.py 的 ERP 端点带 RuoYi 鉴权; 桌面版直连引擎, 见 desktop_api.py)
os.environ.setdefault("DOC_DESKTOP", "1")

# LaMa/torch 模型缓存指到 D 盘(C 盘空间紧; 默认会下到 C:\Users\...\.cache\torch)。
_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(os.path.splitdrive(_HERE)[0] or "D:", os.sep, "torch-cache")
os.environ.setdefault("TORCH_HOME", _CACHE)
os.environ.setdefault("HF_HOME", os.path.join(os.path.splitdrive(_HERE)[0] or "D:", os.sep, "hf-cache"))


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run_server(port):
    import uvicorn
    from desktop_api import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _wait_ready(port, timeout=30):
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    port = _free_port()
    threading.Thread(target=_run_server, args=(port,), daemon=True).start()
    _wait_ready(port)
    import webview
    webview.create_window("DocStudio 文档处理工具", f"http://127.0.0.1:{port}/app",
                          width=1180, height=820, min_size=(900, 640))
    webview.start()


def _self_test() -> int:
    """安装后自检：起本地服务、请求桌面页、确认静态资源随包，不开窗口。"""
    import pathlib
    import tempfile
    import traceback
    import urllib.request

    log_path = pathlib.Path(tempfile.gettempdir()) / "DocStudio-self-test.log"
    report, server_errors = [], []

    def serve():
        try:
            _run_server(port)
        except Exception:
            server_errors.append(traceback.format_exc())

    try:
        port = _free_port()
        threading.Thread(target=serve, daemon=True).start()
        ready = _wait_ready(port)
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/app", timeout=10).read()
        static_ok = pathlib.Path(_HERE, "static", "desktop.js").exists()
        ok = ready and static_ok and b"<html" in body.lower() and len(body) > 2000
        report += [f"server_ready={ready}", f"app_page_bytes={len(body)}", f"static_js={static_ok}",
                   "OK" if ok else "FAILED: 桌面页或静态资源未正常返回"]
        code = 0 if ok else 3
    except Exception:
        report.append(traceback.format_exc())
        code = 3
    report += server_errors
    log_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    main()
