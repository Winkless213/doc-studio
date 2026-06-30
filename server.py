# -*- coding: utf-8 -*-
"""
DocStudio 文档处理服务(独立 Python 小服务, co-locate 同台 ECS 同 Big KB)。
两条线统一载体:
  - 对内: /parse-quote 供应商报价单 PDF -> 采购系统报价草稿(采购员录入用)
  - 对外: /debrand(仅去品牌) /translate(仅翻译) /both(翻译+去品牌, 主用途, 发客户)
其他内部系统通过 HTTP 调本服务。引擎逻辑复用 engine_structify / engine。
"""
import fitz, os, shutil, tempfile, base64
from datetime import date
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from engine_structify import quote_to_offer, quote_to_items
from engine import run as engine_run
from engine_translate import extract_cells
from engine_qa import build_qa, qa_header, vision_qa
import engine_review
import json

app = FastAPI(title="DocStudio Document Processing Service", version="0.1")

# 翻译模型 = plus 档"按到期自动接力"队列(先到期先用完, 免费额度不浪费; 当前模型过期或某批调用失败→自动切链中下一个)。
# 额度=每模型100万免费token·独立计算·各有到期日(2026-06-24 控制台 img 为准)。搜货 MCP 另吃 3.7max 系列, 互不抢。
# 要调整: 改 TRANSLATE_CHAIN(模型, 到期日)即可; 到期日由控制台免费额度有效期决定。
TRANSLATE_CHAIN = [
    ("qwen3.6-plus-2026-04-02", date(2026, 7, 2)),    # 7/02 最早到期, 先用掉
    ("qwen3.5-plus-2026-04-20", date(2026, 7, 23)),   # 7/02 后接力(3.5 太拉但到期早, 暂列; 不想用可删此行)
    ("qwen3.7-plus-2026-05-26", date(2026, 9, 1)),    # 最后接力(3.7 满额度, plus 质量好)
]

def translate_models(today=None):
    """从今天起未过到期日的 plus 档模型链(按到期先后); 全过期则兜底用最后一个。"""
    today = today or date.today()
    chain = [m for m, exp in TRANSLATE_CHAIN if today <= exp]
    return chain or [TRANSLATE_CHAIN[-1][0]]

TRANSLATE_MODEL = translate_models()[0]   # 当前主模型(按今天日期自动选)


def _fix_form_utf8(s):
    """修 multipart Form 文本字段的中文乱码: Starlette 按 latin-1 解 multipart 字段, 客户端原始
    字节(浏览器=UTF-8 / Windows curl=GBK)被当 latin-1 → 乱码。
    先 latin-1 编回原始字节, 再按 UTF-8 优先、GBK 兜底解; 含中文则采纳。已是正常中文的串
    (含非 latin-1 字符)encode 会抛 → 原样返回(零误伤)。"""
    if not s:
        return s
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s                                 # 已含真中文等 → 不是乱码, 原样返回
    for enc in ("utf-8", "gbk"):
        try:
            dec = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if any("一" <= c <= "鿿" for c in dec):   # 解出中文 = 命中
            return dec
    return s


def _qa_headers(rep, mode, out_pdf=None, brand="", vision=False):
    """自检质检关 -> 响应头供前端展示(老板要的"工具自己查")。
    X-Doc-QA = ASCII 质检码(verdict + 各项数字, 头不放中文; 前端按码渲染标签)。
    vision=True 时跑 qwen-vl 逐页视觉复检, 问题明细 base64 进 X-Doc-QA-Vision(避免头放中文)。"""
    qa = build_qa(rep or {}, mode)
    vis = vision_qa(out_pdf, brand) if (vision and out_pdf) else None
    hdr = {"X-Doc-QA": qa_header(qa, vis),
           "Access-Control-Expose-Headers": "X-Doc-QA, X-Doc-QA-Vision"}
    if vis and (vis.get("pages_with_issues") or vis.get("brand_text")):
        payload = {"issues": vis.get("pages_with_issues", []), "brand_text": vis.get("brand_text", [])}
        hdr["X-Doc-QA-Vision"] = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode()).decode()
    return hdr


@app.get("/health")
def health():
    return {"status": "ok", "service": "doc-processing"}


def _detect_doc_type(pdf_bytes):
    """纯算法判别 矢量 vs 图片型(零模型/零延迟): 算每页"最大单图覆盖页面的比例", 取全文档平均。
    报价单/矢量目录=几乎无图(只小 logo)平均~0%; 图片型目录(品牌烤进产品照/banner)平均高(示例41%/示例100%)。
    阈值 20%(实测两类天堑分明: 报价单0% vs 示例41%)。图片型=可能有烤入品牌→走向导; 矢量=矢量去品牌能搞定。"""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = doc.page_count or 1
    covs = []
    for p in doc:
        pa = (p.rect.width * p.rect.height) or 1
        mx = 0.0
        for im in p.get_images(full=True):
            try:
                for r in p.get_image_rects(im[0]):
                    mx = max(mx, (r.width * r.height) / pa)
            except Exception:
                pass
        covs.append(mx)
    avg = sum(covs) / n
    big = sum(1 for c in covs if c > 0.5)
    return {"type": "image" if avg > 0.20 else "vector",
            "pages": doc.page_count, "avgImageCoverage": round(avg, 3), "bigImagePages": big}


@app.post("/detect-type")
async def detect_type(file: UploadFile = File(...)):
    """判别上传 PDF 是 矢量文档(报价单/矢量目录) 还是 图片型目录(品牌烤进像素), 供前端自动分流。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        return _detect_doc_type(data)
    except Exception as e:
        raise HTTPException(500, f"判别失败: {e}")


@app.post("/parse-quote")
async def parse_quote(file: UploadFile = File(...), mode: str = "offer"):
    """对内线: 供应商报价单 PDF -> 结构化。
    mode=offer: 整单对一行 offer 草稿(供应商/总价/数量/单位 + BOM明细) —— 给 ERP 录 offer。
    mode=items: 逐行明细数组 —— 备用。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise HTTPException(400, f"无法打开 PDF: {e}")
    if mode == "items":
        return {"items": quote_to_items(doc)}
    return quote_to_offer(doc)


@app.post("/debrand")
async def debrand(file: UploadFile = File(...), brand: str = Form(""), auto_drop: bool = Form(True), vision: bool = Form(False)):
    """对外线: 报价单/目录 PDF -> 去品牌 PDF(删供应商 logo/公司名/联系方式)。给客户转发用。
    brand: 逗号分隔品牌词(每本不同); auto_drop: 自动删企业宣传/联系/空页。返回处理后的 PDF。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    brand = _fix_form_utf8(brand)                                    # 修 multipart 中文乱码
    tokens = [t.strip() for t in brand.split(",") if t.strip()]
    tmp = tempfile.mkdtemp(prefix="debrand_")
    in_pdf = os.path.join(tmp, "in.pdf")
    out_pdf = os.path.join(tmp, "out.pdf")
    with open(in_pdf, "wb") as f:
        f.write(data)
    try:
        rep = engine_run(in_pdf, out_pdf, mode="debrand", tokens=tokens, auto_drop=auto_drop)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"去品牌失败: {e}")
    base = (file.filename or "document").rsplit(".", 1)[0]
    return FileResponse(out_pdf, media_type="application/pdf",
                        filename=f"{base}_debranded.pdf",
                        headers=_qa_headers(rep, "debrand", out_pdf, brand, vision),
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


_GLOSSARY = None
def _load_glossary():
    """术语库(键=中文, 值=锁定英文译法)。关思考后的质量护栏: 专业词靠这里锁定而非长思考。
    过滤分节注释键(_开头, 值为 ----)。缓存。文件 = 服务同目录 glossary.json(每翻一份可往里加确认过的词)。"""
    global _GLOSSARY
    if _GLOSSARY is None:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.json")
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            _GLOSSARY = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
        except Exception:
            _GLOSSARY = {}
    return _GLOSSARY


def _qwen_translate(texts, target="英文"):
    """端到端翻译那步: 调 Qwen(DashScope 兼容端点)批量翻译中文 -> {原文: 译文} 契约。
    缺译文的单元 engine 会保留原文(不崩)。数字/型号让模型原样保留。
    术语库: 精确命中整条直接用锁定译法(不调模型); 句中涉及的术语注入 prompt 作硬约束。"""
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise HTTPException(500, "未配置 DASHSCOPE_API_KEY，无法翻译")
    from openai import OpenAI
    # timeout 限单次调用上限(默认 600s 会让某模型卡住时整个请求/前端跟着挂); max_retries=0 因
    # 跨模型 relay 本身就是重试机制, 不再叠加 SDK 重试放大延迟。某模型超时→relay 自动切下一个。
    client = OpenAI(api_key=key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    timeout=90.0, max_retries=0)
    uniq = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
    models = translate_models()        # plus 档到期接力链(当前+后续未过期)
    glossary = _load_glossary()
    result = {}
    # 术语库锁定①: 精确命中整条 → 直接用锁定译法, 不调模型(100% 一致 + 省额度)
    pending = []
    for t in uniq:
        if t in glossary:
            result[t] = glossary[t]
        else:
            pending.append(t)
    BATCH = 40
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        # 术语库锁定②: 批内文本(子串)涉及的术语注入 prompt 作硬约束, 句中专业词也走锁定译法
        # (如"全钢装配式钢板仓"含"装配式"→bolted, 解决关思考后个别专业词的措辞)
        hits = {k: v for k, v in glossary.items() if any(k in s for s in batch)}
        gloss_block = ""
        if hits:
            gloss_block = ("术语对照表(出现在句中也必须严格采用此译法): "
                           + "; ".join(f"{k}={v}" for k, v in hits.items()) + "。\n")
        prompt = ("把下面 JSON 数组里的中文逐条翻译成" + target +
                  "，严格返回等长 JSON 数组(顺序对应)。数字/型号/规格代号原样保留，"
                  "贸易与机械术语用通用译法。只返回 JSON 数组本身，不要解释。\n" +
                  gloss_block +
                  json.dumps(batch, ensure_ascii=False))
        for model in models:           # plus 档到期接力链: 成功即停; 该模型失败(额度/错误)→切下一个
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    # 关长思考: 这些 plus 快照模型默认开 thinking, 复杂批触发长链推理→单批从 24s 飙到 177s,
                    # 多页目录会撞 10min 超时失败。翻译是知识/措辞任务非多步推理, 实测关思考质量 95%+ 一致
                    # (差异多为风格/复数), 个别专业词(装配式→bolted)交术语库锁定+人工终审, 比逐次思考更稳更省。
                    extra_body={"enable_thinking": False})
                txt = resp.choices[0].message.content.strip()
                arr = json.loads(txt[txt.index('['):txt.rindex(']') + 1])
                for src, tr in zip(batch, arr):
                    if isinstance(tr, str) and tr.strip():
                        result[src] = tr.strip()
                break                  # 该批成功, 不再试后续模型
            except Exception:
                continue               # 该模型失败, 试链中下一个
    return result


@app.post("/translate")
async def translate(file: UploadFile = File(...), target: str = Form("英文"), auto_drop: bool = Form(False), vision: bool = Form(False)):
    """对外线: 中文 PDF -> 翻译版 PDF(端到端: 抽中文 -> Qwen 翻 -> 原版面回填)。给客户发外文版用。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    target = _fix_form_utf8(target)                                  # 修 multipart 中文乱码
    tmp = tempfile.mkdtemp(prefix="translate_")
    in_pdf = os.path.join(tmp, "in.pdf")
    out_pdf = os.path.join(tmp, "out.pdf")
    with open(in_pdf, "wb") as f:
        f.write(data)
    try:
        doc = fitz.open(in_pdf)
        units = extract_cells(doc)
        doc.close()
        src = [u["zh"] for u in units if u.get("zh", "").strip()]
        translations = _qwen_translate(src, target) if src else {}
        rep = engine_run(in_pdf, out_pdf, mode="translate", translations=translations, auto_drop=auto_drop)
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"翻译失败: {e}")
    base = (file.filename or "document").rsplit(".", 1)[0]
    return FileResponse(out_pdf, media_type="application/pdf",
                        filename=f"{base}_translated.pdf",
                        headers=_qa_headers(rep, "translate", out_pdf, "", vision),
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@app.post("/both")
async def both(file: UploadFile = File(...), brand: str = Form(""), target: str = Form("英文"), auto_drop: bool = Form(True), vision: bool = Form(False)):
    """对外线主用途: 中文供应商文档 -> 翻译 + 去品牌 PDF(端到端: 抽中文 -> Qwen 翻 -> 原版面回填 +
    删品牌logo/公司名/联系方式 + 删企业宣传/联系/空页)。给客户发外文且不泄露供应商。
    brand: 逗号分隔品牌词(供应商名等, 每份不同); 留空则只删自动识别的 logo/联系行/宣传页。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    brand = _fix_form_utf8(brand); target = _fix_form_utf8(target)   # 修 multipart 中文乱码
    tokens = [t.strip() for t in brand.split(",") if t.strip()]
    tmp = tempfile.mkdtemp(prefix="both_")
    in_pdf = os.path.join(tmp, "in.pdf")
    out_pdf = os.path.join(tmp, "out.pdf")
    with open(in_pdf, "wb") as f:
        f.write(data)
    try:
        doc = fitz.open(in_pdf)
        units = extract_cells(doc)
        doc.close()
        src = [u["zh"] for u in units if u.get("zh", "").strip()]
        translations = _qwen_translate(src, target) if src else {}
        rep = engine_run(in_pdf, out_pdf, mode="both", tokens=tokens, translations=translations, auto_drop=auto_drop)
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"翻译+去品牌失败: {e}")
    base = (file.filename or "document").rsplit(".", 1)[0]
    return FileResponse(out_pdf, media_type="application/pdf",
                        filename=f"{base}_clean.pdf",
                        headers=_qa_headers(rep, "both", out_pdf, brand, vision),
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


# ============ 图片型目录清洗向导(老板 6-29 拍板) ============
# 图片型目录(品牌烤进像素, 矢量去品牌碰不了)走"缩略图向导": analyze 出每页缩略图+VL分类建议
# → 前端人工点选删哪些页 → apply 删页出干净 PDF。第一步=删页(可靠, 解决封面/宣传页/最后N页);
# 留页上小厂标的"框选盖白"= 第二步再做。

# 中文页类型标签(给前端展示, 后端只回英文 type 码; 前端也可自走 i18n)
_CATALOG_TYPE_CN = {"cover": "封面", "product": "产品", "promo": "宣传",
                    "contact": "联系", "cert": "资质", "other": "其他", "unknown": "待定"}


def _thumb_b64(png_path, shrink=1):
    """读 VL 用的缩略图 PNG, 缩小后转 base64(给前端网格展示, 减小响应体)。shrink=1 即边长减半。"""
    pix = fitz.Pixmap(png_path)
    if shrink:
        pix.shrink(shrink)
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()


@app.post("/analyze-catalog")
async def analyze_catalog(file: UploadFile = File(...), brand: str = Form("")):
    """图片型目录: 渲染每页缩略图 + VL 逐页分类(封面/产品/宣传/联系/资质), 返回供前端审查的清单。
    suggested_delete = VL 建议整页删(封面/宣传/联系)。前端展示缩略图 + 预选, 人最终点选。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    brand = _fix_form_utf8(brand)
    tmp = tempfile.mkdtemp(prefix="catalog_")
    in_pdf = os.path.join(tmp, "in.pdf")
    review = os.path.join(tmp, "review")
    with open(in_pdf, "wb") as f:
        f.write(data)
    try:
        res = engine_review.analyze(in_pdf, review, brand)        # 渲染缩略图 + VL 分类
        # VL 框是缩略图像素坐标(THUMB_DPI), 换成 0-1 比例给前端(可叠在任意尺寸缩略图上画框)
        thumb_pix = fitz.Pixmap(os.path.join(review, "p01.png"))
        tw, th = thumb_pix.width, thumb_pix.height
        items = []
        for p in res["proposal"]:
            n = p["page"]
            boxes = []
            for bx in p.get("brand_boxes", []):
                if len(bx) == 4 and tw and th:
                    boxes.append([round(bx[0] / tw, 4), round(bx[1] / th, 4),
                                  round(bx[2] / tw, 4), round(bx[3] / th, 4)])
            items.append({
                "page": n,
                "type": p.get("type", "unknown"),
                "typeLabel": _CATALOG_TYPE_CN.get(p.get("type", "unknown"), "待定"),
                "suggestedDelete": p.get("suggest") == "delete",   # VL 建议整页删
                "suggestErase": p.get("suggest") == "clean",       # VL 建议盖白/擦除(产品页角落 logo)
                "why": p.get("why", ""),
                "brandBoxes": boxes,                                # 0-1 比例, 供 Stage 2 产品页擦除预选+手动微调
                "thumb": _thumb_b64(os.path.join(review, f"p{n:02d}.png")),
            })
        return {"pages": res["pages"], "items": items}
    except Exception as e:
        raise HTTPException(500, f"分析失败: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/apply-catalog")
async def apply_catalog(file: UploadFile = File(...), delete_pages: str = Form("[]"),
                        erase: str = Form("{}")):
    """图片型目录一体导出: 删页 + 区域擦除(产品页 logo/厂标), 返回干净 PDF。
    delete_pages: 要删的页码 JSON 数组(1-based), 如 [1,2,15,16]。
    erase: {页码: [[x0,y0,x1,y1], ...]} 每页要擦的框(0-1 比例); 每框自动选 纯色填充 / LaMa 修复。
    顺序: 先擦除(在原页号上)再删页 → 擦除后的页若也在删除列表里不影响。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        dp = sorted({int(x) for x in json.loads(delete_pages)})
    except Exception:
        raise HTTPException(400, "delete_pages 需为页码 JSON 数组, 如 [1,3,16]")
    try:
        erase_map = {int(k): v for k, v in json.loads(erase).items()} if erase else {}
    except Exception:
        raise HTTPException(400, "erase 需为 {页码: [[x0,y0,x1,y1],...]} 的 JSON")
    tmp = tempfile.mkdtemp(prefix="catalog_apply_")
    in_pdf = os.path.join(tmp, "in.pdf")
    out_pdf = os.path.join(tmp, "out.pdf")
    with open(in_pdf, "wb") as f:
        f.write(data)
    doc = fitz.open(in_pdf)
    total = doc.page_count
    if dp and len(dp) >= total:
        doc.close(); shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(400, "不能删除全部页面")
    try:
        # 1) 逐页擦除(只对保留页做; 要删的页没必要擦)
        if erase_map:
            from engine_inpaint import inpaint_page, replace_page_with_image
            for pno1, boxes in erase_map.items():
                if not boxes or pno1 in dp or not (1 <= pno1 <= total):
                    continue
                page = doc[pno1 - 1]
                img = inpaint_page(page, boxes)
                replace_page_with_image(page, img)
        # 2) 删页(从后往前)
        for i in sorted([p - 1 for p in dp if 1 <= p <= total], reverse=True):
            doc.delete_page(i)
        doc.save(out_pdf, garbage=4, deflate=True)
        doc.close()
    except Exception as e:
        doc.close(); shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"处理失败: {e}")
    base = (file.filename or "document").rsplit(".", 1)[0]
    return FileResponse(out_pdf, media_type="application/pdf",
                        filename=f"{base}_cleaned.pdf",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))
