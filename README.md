# DocStudio — 文档处理工具(翻译 + 去品牌)

外贸文档本地处理工具:中英互译报价单 / 产品目录,并自动去除 PDF 中的品牌信息(logo、公司名、联系方式)。个人作品展示版,已移除业务方 / 客户信息与密钥。

## 功能
- **中英翻译**:PDF 报价单 / 目录原地翻译,保留版面;术语库 + 翻译记忆保证术语一致。
- **自动去品牌**:检测并移除 logo、公司名、联系方式。矢量 / 版面层直接删覆盖层还原干净底图;图片型(品牌烤进照片)用 LaMa 图像修复。
- **质检 + 人工终审**:残留检测、歧义提问、终审确认后导出。
- **两种形态**:桌面 App(pywebview)+ FastAPI 服务。

## 技术
Python · PyMuPDF(版面 / 文本 / 图层)· 视觉和文本大模型(DashScope / Qwen-VL)· LaMa 图像修复 · FastAPI · pywebview

核心思路:**原地挖字换字、不重画版面**;品牌多为干净底图上的覆盖层,删图层即还原。

## 运行
    pip install -r requirements.txt
    export DASHSCOPE_API_KEY="your-key"   # 需要大模型识别能力时配置
    python server.py          # FastAPI 服务
    # 或 python desktop_app.py   # 桌面版

## 主要文件
- engine_translate.py / debrand.py / engine_inpaint.py / engine_review.py — 翻译 / 去品牌 / 修复 / 质检引擎
- server.py — FastAPI 服务 · desktop_app.py — 桌面应用 · glossary.json — 术语库
