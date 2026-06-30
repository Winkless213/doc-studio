# -*- coding: utf-8 -*-
"""
统一文档处理引擎: EXTRACT -> PLAN -> REBUILD。翻译 / 去品牌 / (结构化) 三合一。
机制 = 原版面"挖字换字": 定位文字单元 -> redaction 真删 -> 按动作回填。
每个文字单元四选一动作: translate(译文) / delete(删) / scrub(中性词) / keep(保留)。

阶段一(本版): 框架立起来, debrand+translate+keep 跑通, 复现现有两套不退化。
复用 debrand.py 已验证的函数(logo检测/页面分类/品牌判定), 不重写。
"""
import fitz, re, json, argparse, os, sys
from collections import defaultdict
from debrand import (detect_logo_xrefs, classify_pages, is_company_name,
                     strip_brand, is_contact_line)
from engine_translate import _ascii_punct


def _brand_hit(tokens, text):
    """品牌词命中(忽略空格): 中文设计常把页脚标语拆成带空格单字('创 造 客 户 价 值'),
    去空格后再 substring 匹配 → 品牌词'创造客户价值'也能命中那种带空格的标语。
    token 本身无空格则与原来逐字匹配等价(零回归)。"""
    if not tokens or not text:
        return False
    nt = re.sub(r"\s+", "", text).lower()
    return any(re.sub(r"\s+", "", t).lower() in nt for t in tokens if t and t.strip())


def missed_logo_candidates(doc, kill, min_pages=3, max_area=0.06, edge=0.15):
    """漏网 logo 候选: 小图(area<max_area)、出现<min_pages页、且落在页眉/页脚带(top/bottom edge)。
    自动层(跨>=min_pages才删)与 oneoff大图报告(area>0.05)之间的洞 = 单页小 logo。
    位置约束(页眉脚带)滤掉目录正文里的产品缩略图/图标, 只留真正像供应商标识的。"""
    cand = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        H = page.rect.height
        page_area = page.rect.width * H
        for im in page.get_image_info(xrefs=True):
            xref = im.get("xref", 0)
            bb = im.get("bbox")
            if not xref or xref in kill or not bb:
                continue
            r = fitz.Rect(bb)
            area = (r.width * r.height) / page_area
            cy = (r.y0 + r.y1) / 2
            in_band = cy < H * edge or cy > H * (1 - edge)     # 仅页眉/页脚带
            if area < max_area and in_band:
                c = cand.setdefault(xref, {"pages": [], "area": round(area, 4)})
                if (pno + 1) not in c["pages"]:
                    c["pages"].append(pno + 1)
    return {x: v for x, v in cand.items() if len(v["pages"]) < min_pages}


def _debrand_images(doc, remove_xref=None):
    """去 logo 图片 + 出报告(老板"两者结合": 自动删 + 列出删了/还留哪些供人工核)。
    删三类: ①自动检测(同 xref 跨>=3页) ②页眉/页脚带小图(漏网单页 logo, missed_logo_candidates) ③显式 remove_xref。
    返回 (kill 集, report)。report.kept_body = 正文区残留图数(可能是烤进产品照的品牌, 需走图片向导)——这是诚实边界。"""
    logos, _ = detect_logo_xrefs(doc)
    explicit = set(remove_xref or [])
    kill = set(logos) | explicit
    band = missed_logo_candidates(doc, kill)            # 页眉/页脚带小图 = 漏网单页 logo
    kill |= set(band.keys())
    for pno in range(doc.page_count):
        for im in doc[pno].get_images(full=True):
            if im[0] in kill:
                try: doc[pno].delete_image(im[0])
                except Exception: pass
    removed = ([{"xref": x, "why": "cross-page"} for x in sorted(logos)]
               + [{"xref": x, "why": "header/footer", "pages": band[x]["pages"]} for x in sorted(band)]
               + [{"xref": x, "why": "explicit"} for x in sorted(explicit) if x not in logos])
    kept = []
    for pno in range(doc.page_count):
        H = doc[pno].rect.height or 1
        for im in doc[pno].get_image_info(xrefs=True):
            x = im.get("xref"); bb = im.get("bbox")
            if not x or x in kill or not bb: continue
            r = fitz.Rect(bb); cy = (r.y0 + r.y1) / 2
            zone = "header" if cy < H * 0.15 else ("footer" if cy > H * 0.85 else "body")
            kept.append({"xref": x, "page": pno + 1, "zone": zone})
    report = {"removed": len(removed), "removed_detail": removed,
              "kept_total": len(kept), "kept_body": sum(1 for k in kept if k["zone"] == "body"),
              "kept_detail": kept}
    return kill, report


def _remove_empty_top_box(doc):
    """去品牌后顶部若剩一个空的带框区域(原 logo/公司名框被删空 -> 突兀空框) -> 白底盖掉(连边框)。
    判据: 最顶端两条横线之间的带, 带内【无文字】= 被删空的品牌框(标题/数据行有文字, 天然不误伤)。
    只处理上 40%、带高>=6pt 的带, 保留下一行顶边框。返回处理的页数。"""
    n = 0
    for page in doc:
        W, H = page.rect.width, page.rect.height
        hys = set()
        for dr in page.get_drawings():
            for it in dr["items"]:
                if it[0] == "l" and abs(it[1].y - it[2].y) < 0.7:
                    hys.add(round(min(it[1].y, it[2].y), 1))
                elif it[0] == "re":
                    hys.add(round(it[1].y0, 1)); hys.add(round(it[1].y1, 1))
        hys = sorted(y for y in hys if y < H * 0.4)
        if len(hys) < 2:
            continue
        y_top = hys[0]
        y_next = next((y for y in hys[1:] if y - y_top >= 6), None)   # 跳过紧贴的双线
        if y_next is None:
            continue
        if page.get_text("text", clip=fitz.Rect(0, y_top, W, y_next)).strip():
            continue                                                 # 带内有文字 = 不是空框, 不动
        page.draw_rect(fitz.Rect(0, max(0, y_top - 2), W, y_next - 0.5), color=None, fill=(1, 1, 1))
        n += 1
    return n


def is_real_table(page):
    """真表格判据(地基验出): 横竖线都多且数量均衡 -> 排除目录的地图/时间轴插图线。"""
    v = h = 0
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 0.7: v += 1
                elif abs(p1.y - p2.y) < 0.7: h += 1
            elif it[0] == "re":
                v += 2; h += 2
    return v >= 20 and h >= 20 and min(v, h) / max(v, h) > 0.4


# ---------- EXTRACT: 全量文字单元(不限语言) + 元数据 ----------
def extract(doc):
    units = []
    for pno in range(doc.page_count):
        page = doc[pno]
        tbl = is_real_table(page)
        for b in page.get_text("dict")["blocks"]:
            if b.get("type", 0) != 0:
                continue
            for ln in b.get("lines", []):
                line_txt = "".join(s["text"] for s in ln["spans"])
                for s in ln["spans"]:
                    if not s["text"].strip():
                        continue
                    units.append({
                        "page": pno, "text": s["text"], "line": line_txt,
                        "bbox": list(s["bbox"]), "size": s["size"],
                        "bold": bool((s["flags"] & 16) or "Bold" in s["font"]),
                        "color": s["color"], "table": tbl, "act": "keep", "new": None,
                    })
    return units


# ---------- PLAN: 每单元定动作 ----------
KEEP_RE = re.compile(r"^[\d\W]*$")            # 纯数字/标点 -> 保留(不翻译)
MODEL_LIKE = re.compile(r"^[A-Z]{1,4}-?\d", re.I)  # 型号样式 -> 保留

def plan(units, tokens=None, mode="debrand", remove_contact=True, translations=None):
    do_deb = mode in ("debrand", "both")
    do_tr = mode in ("translate", "both")
    tokens = tokens or []
    translations = translations or {}
    for u in units:
        t = u["text"]
        # 去品牌动作(优先): 联系信息 / 品牌词
        if do_deb and remove_contact and is_contact_line(u["line"]):
            u["act"] = "delete"; continue
        if do_deb and any(tok.lower() in t.lower() for tok in tokens):
            if is_company_name(t, tokens) or " " not in t.strip():
                u["act"] = "delete"
            else:
                u["act"] = "scrub"; u["new"] = strip_brand(t, tokens)
            continue
        # 翻译动作: 数字/型号/纯标点保留, 其余查译文回填(缺译文则保持keep, 留待Qwen补)
        if do_tr and not KEEP_RE.match(t.strip()) and not MODEL_LIKE.match(t.strip()):
            tr = translations.get(t.strip())
            if tr:
                u["act"] = "translate"; u["new"] = tr
    return units


# ---------- REBUILD: redaction 真删 + 按动作回填 + 删图 + 删页 ----------
def rebuild(doc, units, kill_xrefs=None):
    kill = set(kill_xrefs or [])
    for pno in range(doc.page_count):
        for im in doc[pno].get_images(full=True):
            if im[0] in kill:
                try: doc[pno].delete_image(im[0])
                except Exception: pass

    byp = defaultdict(list)
    for u in units:
        byp[u["page"]].append(u)
    reinsert = []
    for pno in range(doc.page_count):
        page = doc[pno]
        for u in byp[pno]:
            if u["act"] in ("delete", "scrub", "translate"):
                page.add_redact_annot(fitz.Rect(u["bbox"]), fill=None)
                if u["act"] in ("scrub", "translate") and u.get("new"):
                    reinsert.append(u)
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
        except TypeError:
            page.apply_redactions()

    for u in reinsert:
        page = doc[u["page"]]
        x0, y0, x1, y1 = u["bbox"]
        fk = "hebo" if u["bold"] else "helv"
        col = u["color"]; rgb = ((col >> 16 & 255)/255, (col >> 8 & 255)/255, (col & 255)/255)
        rect = fitz.Rect(x0, y0 - 0.5, x1 + 30, y1 + 1)
        new = _ascii_punct(u["new"])
        fs = min(u["size"], 9.6)
        while fs >= 2.5:
            if page.insert_textbox(rect, new, fontsize=fs, fontname=fk, color=rgb) >= 0:
                break
            fs -= 0.2

def drop_doc_pages(doc, drop_pages):
    if not drop_pages:
        return
    n = doc.page_count
    for i in sorted({p - 1 for p in drop_pages if 1 <= p <= n}, reverse=True):
        doc.delete_page(i)


def _run_translate(doc, out_pdf, translations, drop_pages, auto_drop, review_dir):
    """翻译模式: 单元格级抽取 + 译文回填(走 engine_translate 后端)。"""
    from engine_translate import extract_cells, rebuild_translate
    units = extract_cells(doc)
    tr = rebuild_translate(doc, units, translations)
    page_plan = classify_pages(doc)
    dp = set(drop_pages or [])
    if auto_drop:
        dp |= {pp["p"] for pp in page_plan if pp["suggest"] == "drop"}
    drop_doc_pages(doc, sorted(dp))
    doc.save(out_pdf, garbage=4, deflate=True)
    chk = fitz.open(out_pdf)
    cjk = sum(1 for pno in range(chk.page_count) for ch in chk[pno].get_text("text") if '一' <= ch <= '鿿')
    if review_dir:
        os.makedirs(review_dir, exist_ok=True)
        for pno in range(chk.page_count):
            chk[pno].get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).save(os.path.join(review_dir, f"p{pno+1:02d}.png"))
    return {"mode": "translate", **tr, "cjk_residual": cjk, "out_pages": chk.page_count}


def _span_brand_scrub(doc, tokens, remove_contact=True):
    """span 级品牌/联系删除(覆盖英文品牌, 补单元格级 has_cjk 漏的)。复用 debrand 逻辑。"""
    tokens = tokens or []
    reinsert = []; n = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type", 0) == 0]
        contact_idx = set()
        if remove_contact:
            for i, blk in enumerate(blocks):
                bt = "".join(s["text"] for ln in blk.get("lines", []) for s in ln["spans"])
                if bt.strip() and is_contact_line(bt):
                    contact_idx.add(i)
            for i in contact_idx:
                page.add_redact_annot(fitz.Rect(blocks[i]["bbox"]), fill=None); n += 1
        for i, blk in enumerate(blocks):
            if i in contact_idx:
                continue
            for ln in blk.get("lines", []):
                for s in ln["spans"]:
                    t = s["text"]
                    if not any(tok.lower() in t.lower() for tok in tokens):
                        continue
                    r = fitz.Rect(s["bbox"]); n += 1
                    if is_company_name(t, tokens) or " " not in t.strip():
                        page.add_redact_annot(r, fill=None)
                    else:
                        new = strip_brand(t, tokens)
                        page.add_redact_annot(r, fill=None)
                        if new:
                            bold = bool((s["flags"] & 16) or "Bold" in s["font"])
                            col = s["color"]; rgb = ((col >> 16 & 255)/255, (col >> 8 & 255)/255, (col & 255)/255)
                            reinsert.append((pno, s["bbox"], new, s["size"], rgb, bold))
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
        except TypeError:
            page.apply_redactions()
    for pno, bbox, text, size, rgb, bold in reinsert:
        page = doc[pno]; x0, y0, x1, y1 = bbox
        fk = "hebo" if bold else "helv"
        rect = fitz.Rect(x0, y0 - 0.5, x1 + 30, y1 + 1)
        text = _ascii_punct(text)
        fs = min(size, 9.6)
        while fs >= 2.5:
            if page.insert_textbox(rect, text, fontsize=fs, fontname=fk, color=rgb) >= 0:
                break
            fs -= 0.2
    return n


def _run_both(doc, out_pdf, tokens, translations, remove_xref, drop_pages, auto_drop, review_dir):
    """叠加: 单元格级抽取, 每单元三选一 —— 品牌/联系→删, 中文→译, 其余→留。"""
    from engine_translate import extract_cells, rebuild_translate
    tokens = tokens or []; translations = translations or {}
    kill, img_report = _debrand_images(doc, remove_xref)   # 删 logo(跨页+页眉脚带小图+显式) + 出报告
    units = extract_cells(doc)
    for u in units:
        zh = u["zh"]
        if is_contact_line(zh) or _brand_hit(tokens, zh):   # 品牌词命中忽略空格(治带空格的页脚标语)
            u["act"] = "delete"; u["new"] = None
        elif zh in translations:
            u["act"] = "translate"; u["new"] = translations[zh]
        else:
            u["act"] = "keep"; u["new"] = None
    tr = rebuild_translate(doc, units)
    span_n = _span_brand_scrub(doc, tokens)        # span 级补删英文品牌(both 局限修复)
    _remove_empty_top_box(doc)                     # 去品牌后顶部空品牌框白盖掉(连边线), 去突兀空框
    page_plan = classify_pages(doc)
    dp = set(drop_pages or [])
    if auto_drop:
        dp |= {pp["p"] for pp in page_plan if pp["suggest"] == "drop"}
    drop_doc_pages(doc, sorted(dp))
    doc.save(out_pdf, garbage=4, deflate=True)
    chk = fitz.open(out_pdf)
    cjk = sum(1 for pno in range(chk.page_count) for ch in chk[pno].get_text("text") if '一' <= ch <= '鿿')
    brand = sum(1 for pno in range(chk.page_count) for t in tokens if t.lower() in chk[pno].get_text("text").lower())
    if review_dir:
        os.makedirs(review_dir, exist_ok=True)
        for pno in range(chk.page_count):
            chk[pno].get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).save(os.path.join(review_dir, f"p{pno+1:02d}.png"))
    return {"mode": "both", **tr, "span_scrubbed": span_n, "cjk_residual": cjk, "brand_residual": brand, "out_pages": chk.page_count, "images": img_report}


def run(in_pdf, out_pdf, mode="debrand", tokens=None, remove_xref=None,
        drop_pages=None, auto_drop=False, translations=None, review_dir=None):
    doc = fitz.open(in_pdf)
    if mode == "translate":
        return _run_translate(doc, out_pdf, translations or {}, drop_pages, auto_drop, review_dir)
    if mode == "both":
        return _run_both(doc, out_pdf, tokens, translations or {}, remove_xref, drop_pages, auto_drop, review_dir)
    kill, img_report = _debrand_images(doc, remove_xref)   # 删 logo(跨页+页眉脚带小图+显式) + 出报告
    units = extract(doc)
    plan(units, tokens=tokens, mode=mode, translations=translations)
    rebuild(doc, units)                            # 图已在 _debrand_images 删, 这里只删字+回填
    _remove_empty_top_box(doc)                     # 去品牌后顶部空品牌框(删 logo/公司名留下的空框)白盖掉
    page_plan = classify_pages(doc)                # ⭐去品牌后再分类(空封底此时才现形)
    dp = set(drop_pages or [])
    if auto_drop:
        dp |= {pp["p"] for pp in page_plan if pp["suggest"] == "drop"}
    drop_doc_pages(doc, sorted(dp))
    doc.save(out_pdf, garbage=4, deflate=True)

    acts = defaultdict(int)
    for u in units: acts[u["act"]] += 1
    chk = fitz.open(out_pdf)
    residual = sum(1 for pno in range(chk.page_count) for tok in (tokens or [])
                   if tok.lower() in chk[pno].get_text("text").lower())
    if review_dir:
        os.makedirs(review_dir, exist_ok=True)
        for pno in range(chk.page_count):
            chk[pno].get_pixmap(matrix=fitz.Matrix(1.4, 1.4)).save(
                os.path.join(review_dir, f"p{pno+1:02d}.png"))
    return {"actions": dict(acts), "logos": sorted(kill), "dropped": sorted(dp),
            "out_pages": chk.page_count, "residual": residual, "images": img_report}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--mode", default="debrand", choices=["debrand", "translate", "both"])
    ap.add_argument("--brand", nargs="*", default=[])
    ap.add_argument("--remove-xref", nargs="*", type=int, default=[])
    ap.add_argument("--drop-page", nargs="*", type=int, default=[])
    ap.add_argument("--auto-drop", action="store_true")
    ap.add_argument("--translations", default=None, help="JSON {原文:译文}")
    ap.add_argument("--review", default=None)
    a = ap.parse_args()
    tr = None
    if a.translations:
        with open(a.translations, encoding="utf-8") as f:
            tr = json.load(f)
    rep = run(a.inp, a.out, mode=a.mode, tokens=a.brand, remove_xref=a.remove_xref,
              drop_pages=a.drop_page, auto_drop=a.auto_drop, translations=tr, review_dir=a.review)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    img = rep.get("images") or {}
    if img.get("removed"):
        print(f"\n✓ 已自动删除 {img['removed']} 个 logo(跨页/页眉脚带小图/显式):", file=sys.stderr)
        for r in img.get("removed_detail", []):
            extra = f" 页{r['pages']}" if r.get("pages") else ""
            print(f"   xref {r['xref']}  ({r['why']}{extra})", file=sys.stderr)
    kb = img.get("kept_body", 0)
    if kb:
        print(f"\n⚠ 正文区还有 {kb} 张图未动(产品照等)。如其中烤进了供应商品牌, 用图片向导(engine_review)单独处理:", file=sys.stderr)
        for k in img.get("kept_detail", []):
            if k["zone"] == "body":
                print(f"   xref {k['xref']}  页{k['page']}", file=sys.stderr)
