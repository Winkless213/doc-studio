# -*- coding: utf-8 -*-
"""
通用文档去品牌引擎 (catalog / 报价单 / PDF)
机制 = 删除"盖在干净背景上的覆盖层":
  - logo 图: 自动检测(小图+跨页重复)→ page.delete_image → 露出底图(像素完美, 不用 LaMa)
  - 品牌文字/联系信息: 无填充 redaction(fill=None)真删, 露出底图(无白框)
  - 句中品牌名: 删词后回填, 不留窟窿
覆盖性靠"品牌词由调用方给(每本目录不同)+ 自动检测", 不写死任何一本目录。
一次性大水印/异形 logo → self-check 列出, 走人工确认 / 视觉模型(后续接 Qwen-VL)。

用法:
  python debrand.py --in <pdf> --out <pdf> --brand ACME ACME [--no-contact]
"""
import fitz, os, re, json, argparse
from collections import defaultdict

CORP_SUFFIX = ["group", "co.", "co ", "co,", "ltd", "inc", "corp", "company",
               "limited", "gmbh", "集团", "有限公司", "公司"]
# ASCII 联系词用词边界(避免 "tel" 命中 "intelligent"); CJK 用子串
# 只匹配真正的联系信息词; 不含社媒平台名(平台标签要保留, 账号靠 URL/续行清扫抓)
CONTACT_ASCII = re.compile(
    r"\b(address|headquarters|tel|phone|fax|mobile|hotline|e-?mail|website)\b", re.I)
# 社媒/电商平台名(用于识别"Find Us/联系我们"这类供应商销售渠道页 -> 整页删候选)
SOCIAL_PLATFORMS = ["twitter", "facebook", "instagram", "instagrma", "youtube",
                    "linkedin", "tiktok", "whatsapp", "alibaba", "aliexpress",
                    "amazon", "made-in-china", "wechat", "find us", "follow us",
                    "contact us", "scan", "微信", "关注我们", "联系我们"]
CONTACT_CJK = ["地址", "厂址", "电话", "传真", "手机", "邮箱", "网址", "微信"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE   = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(com|net|cn|org|cc)\b", re.I)

# ===== 页面分类信号(通用英文工业目录词, 非某一本专属) =====
# 产品页: 产品类别词 / 规格参数词 / 型号样式
PRODUCT_CAT = ["excavator", "loader", "grader", "roller", "bulldozer", "dozer",
               "forklift", "drill", "crane", "truck", "pump", "pile driver", "piledriver",
               "auxiliary", "attachment", "spare part", "core product", "solution",
               "series", "machine", "equipment", "modification", "tractor"]
SPEC_WORDS = ["operating weight", "rated power", "rated", "power", "weight", "capacity",
              "engine", "bucket", "dimension", "speed", "torque", "drilling depth",
              "kw", "kg", "rpm", "excavation"]
# 企业宣传"强信号"= 多词章节短语, 产品页正文绝不会出现 -> 全文匹配且优先于产品判定
# (解决"发展历程页正文提到 excavator 被误判产品": 标题字号常被大数字/年份抢占, 故认短语不认标题字号)
PROMO_STRONG = ["development history", "sales network", "corporate culture", "manufacturing system",
                "r & d strength", "r&d strength", "research institute", "global megaproject",
                "megaproject showcase", "global exhibition", "after-sales", "after sales",
                "service system", "strength of the group", "corporate vision", "corporate mission",
                "company profile", "core competitiveness", "brand story"]
# 企业宣传"弱信号"= 单词, 只在标题判(避免正文偶现误伤)
PROMO_TITLE = ["overview", "milestone", "about us", "honor", "showcase", "history",
               "network", "culture", "corporate", "exhibition"]
# 资质页(留则注意图内可能烤名字): 证书/认证/荣誉/专利
CREDENTIAL_W = ["certificate", "certification", "quality certification", "patent",
                "iso", "honor", "honour", "award", "supplier assess"]
MODEL_RE = re.compile(r"\b[A-Z]{1,4}-?\d{2,}[A-Z0-9.\-]*\b")  # 型号样式 HW1300/HWL90/HWC-2.6T


def detect_logo_xrefs(doc, min_pages=3, max_area=0.06):
    """通用 logo 检测: 同一 xref 跨 >=min_pages 页且中位面积 < max_area。"""
    W, H = doc[0].rect.width, doc[0].rect.height
    page_area = W * H
    placements = defaultdict(list)
    for pno in range(doc.page_count):
        for im in doc[pno].get_image_info(xrefs=True):
            xref = im.get("xref", 0)
            bb = im.get("bbox")
            if not xref or not bb:
                continue
            r = fitz.Rect(bb)
            placements[xref].append((pno, (r.width * r.height) / page_area))
    logos = {}
    for xref, pl in placements.items():
        pages = sorted(set(p for p, _ in pl))
        areas = sorted(a for _, a in pl)
        med = areas[len(areas) // 2]
        if len(pages) >= min_pages and med < max_area:
            logos[xref] = {"pages": len(pages), "med_area": round(med, 4)}
    return logos, placements


def is_company_name(text, tokens):
    """该 span 是否本身就是公司名(标题/页脚): 含品牌词 + 公司后缀 + 词数少。"""
    low = text.lower()
    if not any(t.lower() in low for t in tokens):
        return False
    if not any(s in low for s in CORP_SUFFIX):
        return False
    return len(text.split()) <= 7


def strip_brand(text, tokens):
    """从句子中删掉品牌词并整理空格/标点(用于句中替换, 不留窟窿)。"""
    out = text
    for t in sorted(tokens, key=len, reverse=True):
        out = re.sub(re.escape(t), "", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.，。、])", r"\1", out)
    out = re.sub(r"^[\s,;，；]+", "", out)
    return out.strip()


def is_contact_line(text):
    if EMAIL_RE.search(text) or URL_RE.search(text):
        return True
    if CONTACT_ASCII.search(text):
        return True
    return any(k in text for k in CONTACT_CJK)


def page_title(page):
    """页标题 = 最大字号文本(主标题/页眉), 用于按章节词判企业宣传页。"""
    spans = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type", 0) != 0:
            continue
        for ln in blk.get("lines", []):
            for s in ln["spans"]:
                if s["text"].strip():
                    spans.append((s["size"], s["text"].strip()))
    if not spans:
        return ""
    big = max(s[0] for s in spans)
    # 取最大字号一档(含并列), 拼起来当标题
    return " ".join(t for sz, t in spans if sz >= big - 0.5).lower()


def classify_pages(doc):
    """通用页面分类器: 产品/企业宣传/资质/联系/空页 + 建议(keep/drop/review)。
    只提示, 删除仍走人工(--drop-page)或 --auto-drop 确认。信号是通用工业目录词, 不写死某本。"""
    plan = []
    for pno in range(doc.page_count):
        page = doc[pno]
        txt = page.get_text("text")
        low = txt.lower()
        compact = re.sub(r"\s+", "", txt)
        title = page_title(page)

        social = sum(1 for p in SOCIAL_PLATFORMS if p in low)
        models = len({m for m in MODEL_RE.findall(txt)})
        spec = sum(1 for w in SPEC_WORDS if w in low)
        prodcat = sum(1 for w in PRODUCT_CAT if w in low)
        promo_strong = [w for w in PROMO_STRONG if w in low]   # 全文(强)
        promo = sum(1 for w in PROMO_TITLE if w in title)      # 只看标题(弱)
        cred = sum(1 for w in CREDENTIAL_W if w in low)

        is_product = (models >= 3 and spec >= 1) or prodcat >= 2 or (prodcat >= 1 and spec >= 2)

        if pno == 0:
            t, s, why = "cover", "keep", "封面"
        elif social >= 3:
            t, s, why = "contact", "drop", f"社媒/联系目录(命中{social}平台)"
        elif len(compact) < 40:
            t, s, why = "empty", "drop", f"近空白页({len(compact)}字符)"
        elif promo_strong:                                     # 章节短语 -> 优先于产品(正文偶有产品词不算数)
            t, s, why = "promo", "drop", f"企业宣传页(命中章节短语: {promo_strong[0]})"
        elif is_product:
            t, s, why = "product", "keep", f"产品页(型号{models}/规格{spec}/类别{prodcat})"
        elif promo >= 1:
            t, s, why = "promo", "drop", "企业宣传页(标题命中章节词)"
        elif cred >= 1:
            t, s, why = "credential", "review", "资质页(证书/荣誉; 留则注意图内可能烤公司名)"
        else:
            t, s, why = "unknown", "review", "无法判定, 人工看"
        plan.append({"p": pno + 1, "type": t, "suggest": s, "why": why})
    return plan


def debrand(in_pdf, out_pdf, tokens, remove_contact=True, review_dir=None,
            extra_xrefs=None, drop_pages=None, scrub_terms=None, auto_drop=False):
    if review_dir:
        os.makedirs(review_dir, exist_ok=True)
    doc = fitz.open(in_pdf)
    report = {"logo_xrefs": {}, "removed_text": [], "replaced_text": [],
              "contact_removed": [], "residual": [], "oneoff_images": [],
              "page_plan": [], "dropped_pages": [], "scrubbed": []}

    # 1) 检测 + 删除 logo 图(自动) + 视觉/人工层指定的一次性对象(extra_xrefs)
    logos, placements = detect_logo_xrefs(doc)
    report["logo_xrefs"] = logos
    extra = set(extra_xrefs or [])
    kill_img = set(logos) | extra
    for pno in range(doc.page_count):
        page = doc[pno]
        for im in page.get_images(full=True):
            if im[0] in kill_img:
                try:
                    page.delete_image(im[0])
                except Exception as e:
                    print(f"  delete_image p{pno+1} x{im[0]} FAIL {e}")

    # 2) 文字层: 品牌词 + 联系信息(块级 + 续行块清扫)
    reinsert = []  # (pno, bbox, text, size, color, bold)
    for pno in range(doc.page_count):
        page = doc[pno]
        blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type", 0) == 0]

        # 2a) 块级联系信息检测: 块内任一行含联系信号 -> 整块删
        contact_idx = set()
        if remove_contact:
            for i, blk in enumerate(blocks):
                btext = "".join(s["text"] for ln in blk.get("lines", []) for s in ln["spans"])
                if btext.strip() and is_contact_line(btext):
                    contact_idx.add(i)
            # 2b) 续行块清扫: 紧贴已删块正下方 + x 重叠 + 短碎片(PDF 拆行的 URL/账号尾巴)
            for i, blk in enumerate(blocks):
                if i in contact_idx:
                    continue
                bt = "".join(s["text"] for ln in blk.get("lines", []) for s in ln["spans"]).strip()
                if len(bt) > 28:           # 续行碎片都很短, 长块不误伤正文
                    continue
                bx0, by0, bx1, by1 = blk["bbox"]
                for j in contact_idx.copy():
                    cx0, cy0, cx1, cy1 = blocks[j]["bbox"]
                    x_ov = min(bx1, cx1) - max(bx0, cx0)
                    if x_ov > 0.3 * (bx1 - bx0) and 0 <= (by0 - cy1) < 8:
                        contact_idx.add(i)
                        break
            for i in contact_idx:
                page.add_redact_annot(fitz.Rect(blocks[i]["bbox"]), fill=None)
                bt = "".join(s["text"] for ln in blocks[i].get("lines", []) for s in ln["spans"])
                report["contact_removed"].append({"p": pno + 1, "t": bt[:80]})

        # 2b2) 识别线索脱敏: 行含指定关键词(项目名/地名等去logo也能反查) -> 删整行
        scrub_lines = []
        if scrub_terms:
            for i, blk in enumerate(blocks):
                if i in contact_idx:
                    continue
                for ln in blk.get("lines", []):
                    ltxt = "".join(s["text"] for s in ln["spans"])
                    low = ltxt.lower()
                    if any(term.lower() in low for term in scrub_terms):
                        page.add_redact_annot(fitz.Rect(ln["bbox"]), fill=None)
                        scrub_lines.append(fitz.Rect(ln["bbox"]))
                        report["scrubbed"].append({"p": pno + 1, "t": ltxt[:80]})

        # 2c) 非联系块内的品牌词: 公司名整删 / 句中删词回填
        for i, blk in enumerate(blocks):
            if i in contact_idx:
                continue
            for ln in blk.get("lines", []):
                lr = fitz.Rect(ln["bbox"])
                if any(lr.intersects(sb) for sb in scrub_lines):   # 已被脱敏删的行不再回填品牌替换
                    continue
                for s in ln["spans"]:
                    t = s["text"]
                    if not any(tok.lower() in t.lower() for tok in tokens):
                        continue
                    r = fitz.Rect(s["bbox"])
                    # 公司名 或 单 token 账号/标识(无空格, 如 acme_china / @acme_group) -> 整删
                    if is_company_name(t, tokens) or (" " not in t.strip()):
                        page.add_redact_annot(r, fill=None)
                        report["removed_text"].append({"p": pno + 1, "t": t})
                    else:
                        new = strip_brand(t, tokens)
                        page.add_redact_annot(r, fill=None)
                        if new:
                            bold = bool((s["flags"] & 16) or "Bold" in s["font"])
                            col = s["color"]
                            rgb = ((col >> 16 & 255) / 255, (col >> 8 & 255) / 255, (col & 255) / 255)
                            reinsert.append((pno, s["bbox"], new, s["size"], rgb, bold))
                        report["replaced_text"].append({"p": pno + 1, "from": t, "to": new})
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
        except TypeError:
            page.apply_redactions()

    # 3) 句中替换文字回填
    for pno, bbox, text, size, rgb, bold in reinsert:
        page = doc[pno]
        x0, y0, x1, y1 = bbox
        fk = "hebo" if bold else "helv"
        rect = fitz.Rect(x0, y0 - 0.5, max(x1, x0 + 4) + 30, y1 + 1)  # 右侧留余量防溢出
        fs = min(size, 9.6)
        while fs >= 2.5:
            if page.insert_textbox(rect, text, fontsize=fs, fontname=fk, color=rgb, align=0) >= 0:
                break
            fs -= 0.2

    # 4) 页面分类(去品牌后, 删页前; 页码=老板在终版看到的页码)
    plan = classify_pages(doc)
    report["page_plan"] = plan

    # 5) 删页(1-based -> 0-based; --drop-page 手动 + --auto-drop 删所有建议drop的页)
    pages_to_drop = set(drop_pages or [])
    if auto_drop:
        pages_to_drop |= {pp["p"] for pp in plan if pp["suggest"] == "drop"}
    if pages_to_drop:
        n = doc.page_count
        valid = sorted({p for p in pages_to_drop if 1 <= p <= n})
        for i in sorted((p - 1 for p in valid), reverse=True):
            doc.delete_page(i)
        report["dropped_pages"] = valid

    doc.save(out_pdf, garbage=4, deflate=True)

    # 4) 自检: 残留品牌词 + 渲染 + 一次性大图(人工确认候选)
    chk = fitz.open(out_pdf)
    W, H = chk[0].rect.width, chk[0].rect.height
    page_area = W * H
    for pno in range(chk.page_count):
        txt = chk[pno].get_text("text")
        for tok in tokens:
            if tok.lower() in txt.lower():
                report["residual"].append({"p": pno + 1, "tok": tok})
        # 一次性大图(非已删 logo) on 任意页: 可能是水印/hero, 列给人工
        for im in chk[pno].get_image_info(xrefs=True):
            xref = im.get("xref", 0)
            bb = im.get("bbox")
            if not xref or not bb:
                continue
            a = (fitz.Rect(bb).width * fitz.Rect(bb).height) / page_area
            if 0.05 < a < 0.92:
                report["oneoff_images"].append({"p": pno + 1, "xref": xref, "area": round(a, 3)})
        if review_dir:
            chk[pno].get_pixmap(matrix=fitz.Matrix(1.4, 1.4)).save(
                os.path.join(review_dir, f"p{pno+1:02d}.png"))
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--brand", nargs="+", required=True, help="品牌词(每本目录不同, 由你给)")
    ap.add_argument("--no-contact", action="store_true")
    ap.add_argument("--remove-xref", nargs="*", type=int, default=[],
                    help="视觉/人工层认定的一次性图对象 xref(如封面水印), 自动检测漏掉的")
    ap.add_argument("--drop-page", nargs="*", type=int, default=[],
                    help="整页删除的页码(1-based; 手动指定, 可与 --auto-drop 叠加)")
    ap.add_argument("--auto-drop", action="store_true",
                    help="自动删除分类器判为 drop 的页(企业宣传/联系/空页); keep/review 页保留")
    ap.add_argument("--scrub", nargs="*", default=[],
                    help="识别线索关键词(项目名/地名等去logo也能反查): 行含任一词则删整行")
    ap.add_argument("--scrub-file", default=None,
                    help="JSON 文件, 内含关键词数组(每本目录一份脱敏词表, 可复用)")
    ap.add_argument("--review", default=None)
    a = ap.parse_args()
    scrub = list(a.scrub)
    if a.scrub_file:
        with open(a.scrub_file, encoding="utf-8") as f:
            scrub += json.load(f)
    rep = debrand(a.inp, a.out, a.brand, remove_contact=not a.no_contact,
                  review_dir=a.review, extra_xrefs=a.remove_xref, drop_pages=a.drop_page,
                  scrub_terms=scrub, auto_drop=a.auto_drop)
    print(f"logo xrefs deleted: {sorted(rep['logo_xrefs'])}")
    print(f"company-name removed: {len(rep['removed_text'])} | sentence-replaced: {len(rep['replaced_text'])} | contact lines: {len(rep['contact_removed'])}")
    print(f"RESIDUAL brand tokens: {len(rep['residual'])} -> {rep['residual']}")
    print(f"SCRUBBED 识别线索行: {len(rep['scrubbed'])}")
    print("\n===== 页面分类建议(keep=留 / drop=建议删 / review=人工定) =====")
    icon = {"keep": "✅留", "drop": "❌删", "review": "⚠️ 看"}
    for pp in rep["page_plan"]:
        print(f"  p{pp['p']:>2}  [{icon.get(pp['suggest'], pp['suggest'])}] {pp['type']:<10} {pp['why']}")
    keep = [pp["p"] for pp in rep["page_plan"] if pp["suggest"] == "keep"]
    drop = [pp["p"] for pp in rep["page_plan"] if pp["suggest"] == "drop"]
    rev = [pp["p"] for pp in rep["page_plan"] if pp["suggest"] == "review"]
    print(f"\n  建议留 {len(keep)} 页: {keep}")
    print(f"  建议删 {len(drop)} 页: {drop}")
    print(f"  待定(人工) {len(rev)} 页: {rev}")
    print(f"  实际删除: {rep['dropped_pages']}")
