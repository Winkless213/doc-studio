# -*- coding: utf-8 -*-
"""
桌面版 API(本地无鉴权, 直连引擎)。复用 server.py 的处理函数, 不走 ERP RuoYi 鉴权。
端点:
  GET  /health                 就绪探针
  GET  /app                    桌面 UI 页(static/desktop.html)
  POST /detect-type            判别 矢量 vs 图片型(纯算法)
  POST /both /translate /debrand   矢量文档处理(翻译/去品牌, 返回 PDF + 质检头)
  POST /analyze-catalog        图片型逐页分析(缩略图 + VL 分类 + 品牌框)
  POST /apply-catalog          图片型一体导出(删页 + LaMa 擦除)
桌面壳 desktop_app.py 在本地起本服务 + pywebview 开窗口。
"""
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 直接复用 server.py 里建好的引擎端点(同进程同函数), 避免重复实现。
# server.py 的 app 端点不带鉴权(鉴权在 Java 那层), 桌面版正好直接用。
import server as S

app = FastAPI(title="Doc Tool Desktop", version="1.0")

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

# 复用 server.py 的处理端点(把它的路由挂过来)
app.add_api_route("/health", S.health, methods=["GET"])
app.add_api_route("/detect-type", S.detect_type, methods=["POST"])
app.add_api_route("/both", S.both, methods=["POST"])
app.add_api_route("/translate", S.translate, methods=["POST"])
app.add_api_route("/debrand", S.debrand, methods=["POST"])
app.add_api_route("/risk-review/apply", S.apply_risk_review, methods=["POST"])
app.add_api_route("/analyze-catalog", S.analyze_catalog, methods=["POST"])
app.add_api_route("/apply-catalog", S.apply_catalog, methods=["POST"])


@app.get("/app")
def desktop_ui():
    return FileResponse(os.path.join(_STATIC, "desktop.html"))


if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
