# -*- coding: utf-8 -*-
"""
翻译后端(单元格级): 提炼自 claude-engine 01_extract + 03_rebuild, 去报价单专属硬编码、通用化。
翻译必须单元格/行级(译文契约 key = 整格文本), 不能 span 级。
extract_cells(doc) -> units(含聚合 zh + rect + redact + 对齐元数据)
rebuild_translate(doc, units, trans) -> redaction 真删源文 + 译文自适应字号回填
"""
import fitz, os


# base-14 回填字体(helv/tiro)缺智能标点/中文标点字形 -> 渲染成 '?'。回填前归一成 ASCII。
# 只动确会渲染失败的标点; 保留 ° ² ³ · µ 等 Latin-1 可渲染字符不碰。
_PUNCT_MAP = {
    "—": "-", "–": "-", "―": "-", "‒": "-", "−": "-",
    "…": "...",
    "“": '"', "”": '"', "„": '"', "‟": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "　": " ", " ": " ",
    "，": ",", "、": ",", "。": ".", "：": ":", "；": ";",
    "（": "(", "）": ")", "！": "!", "？": "?",
}
_PUNCT_TBL = str.maketrans(_PUNCT_MAP)

def _ascii_punct(s):
    return s.translate(_PUNCT_TBL) if s else s


def _has_cjk(t):
    return any('一' <= c <= '鿿' or c in '，。：；（）、' for c in t)

def _cov(a, lo, hi):
    return lo - 1.0 <= a <= hi + 1.0

def _order_spans(spans):
    items = sorted(spans, key=lambda e: e[0][1])
    lines = []
    for e in items:
        ey0, ey1 = e[0][1], e[0][3]
        for ln in lines:
            ly0 = min(x[0][1] for x in ln); ly1 = max(x[0][3] for x in ln)
            ov = min(ly1, ey1) - max(ly0, ey0)
            if ov > 0.4 * min(ly1 - ly0, ey1 - ey0):
                ln.append(e); break
        else:
            lines.append([e])
    lines.sort(key=lambda ln: min(x[0][1] for x in ln))
    out = []
    for ln in lines:
        ln.sort(key=lambda e: e[0][0]); out += ln
    return out

def _union(spans):
    return [round(min(e[0][0] for e in spans), 1), round(min(e[0][1] for e in spans), 1),
            round(max(e[0][2] for e in spans), 1), round(max(e[0][3] for e in spans), 1)]

def _mk(spans, page, kind, rect):
    o = _order_spans(spans)
    txt = "".join(e[1] for e in o)
    c = max(spans, key=lambda e: len(e[1]))
    col = c[3]
    return {"page": page, "kind": kind, "rect": rect, "redact": _union(spans),
            "size": round(max(e[2] for e in spans), 1),
            "color": [round((col >> 16 & 255)/255, 3), round((col >> 8 & 255)/255, 3), round((col & 255)/255, 3)],
            "bold": any((e[4] & 16) or ("Bold" in e[5]) for e in spans),
            "zh": txt}


def extract_cells(doc):
    """单元格/段落/行级聚合(只抓含源语言CJK的单元; 数字/型号/英文自动保留=不入units)。"""
    UNITS = []
    for pno, page in enumerate(doc):
        start = len(UNITS)
        vlines = []; hlines = []
        for d in page.get_drawings():
            for it in d["items"]:
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.x - p2.x) < 0.7: vlines.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
                    elif abs(p1.y - p2.y) < 0.7: hlines.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif it[0] == "re":
                    r = it[1]
                    vlines += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
                    hlines += [(r.y0, r.x0, r.x1), (r.y1, r.x0, r.x1)]
        W, H = page.rect.width, page.rect.height
        t_top = min((h[0] for h in hlines), default=H)
        t_bot = max((h[0] for h in hlines), default=0)

        def cell_of(cx, cy):
            L = [v[0] for v in vlines if v[0] <= cx + .5 and _cov(cy, v[1], v[2])]
            R = [v[0] for v in vlines if v[0] >= cx - .5 and _cov(cy, v[1], v[2])]
            T = [h[0] for h in hlines if h[0] <= cy + .5 and _cov(cx, h[1], h[2])]
            B = [h[0] for h in hlines if h[0] >= cy - .5 and _cov(cx, h[1], h[2])]
            return (round(max(L) if L else 0, 1), round(max(T) if T else 0, 1),
                    round(min(R) if R else W, 1), round(min(B) if B else H, 1))

        spans = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type", 0) != 0: continue
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    if s["text"].strip():
                        spans.append((s["bbox"], s["text"], s["size"], s["color"], s["flags"], s["font"]))

        inside = [s for s in spans if t_top - 1 <= (s[0][1] + s[0][3]) / 2 <= t_bot + 1]
        outside = [s for s in spans if not (t_top - 1 <= (s[0][1] + s[0][3]) / 2 <= t_bot + 1)]

        if outside:
            o = _order_spans(outside); cur = []; lastc = None; groups = []
            for e in o:
                cy = (e[0][1] + e[0][3]) / 2
                if lastc is None or abs(cy - lastc) <= 6: cur.append(e)
                else: groups.append(cur); cur = [e]
                lastc = cy
            if cur: groups.append(cur)
            for g in groups:
                if any(_has_cjk(e[1]) for e in g):
                    UNITS.append(_mk(g, pno, "line", _union(g)))

        cells = {}
        for s in inside:
            sig = cell_of((s[0][0] + s[0][2]) / 2, (s[0][1] + s[0][3]) / 2)
            cells.setdefault(sig, []).append(s)
        sigs = list(cells.keys())
        para = sorted([sg for sg in sigs if (sg[2] - sg[0]) > 120 and any(_has_cjk(e[1]) for e in cells[sg])],
                      key=lambda sg: (sg[2] - sg[0]) * (sg[3] - sg[1]), reverse=True)
        absorbed = set()
        for psg in para:
            if psg in absorbed: continue
            grp = list(cells[psg])
            for sg in sigs:
                if sg is psg or sg in absorbed: continue
                ccx = (sg[0] + sg[2]) / 2; ccy = (sg[1] + sg[3]) / 2
                if psg[0] - .5 <= ccx <= psg[2] + .5 and psg[1] - .5 <= ccy <= psg[3] + .5:
                    grp += cells[sg]; absorbed.add(sg)
            absorbed.add(psg)
            UNITS.append(_mk(grp, pno, "para", [psg[0], psg[1], psg[2], psg[3]]))
        for sg in sigs:
            if sg in absorbed: continue
            if any(_has_cjk(e[1]) for e in cells[sg]):
                UNITS.append(_mk(cells[sg], pno, "cell", [sg[0], sg[1], sg[2], sg[3]]))

        # 吸收浮字: 含中文的单元, 把落在其 rect 内的所有 span(数字/型号等杂散块)并进来,
        # 整格当一个翻译单元 -> 译文含数字、整格清除, 不再有浮字被译文盖住(治叠字根因)。
        # 简单格 rect 内无杂散 span => _mk 结果不变, 不影响既有单元(改完 diff 校验)。
        for i in range(start, len(UNITS)):
            u = UNITS[i]
            if not _has_cjk(u["zh"]):
                continue
            rx0, ry0, rx1, ry1 = u["rect"]
            grp = [s for s in spans
                   if rx0 - 0.5 <= (s[0][0] + s[0][2]) / 2 <= rx1 + 0.5
                   and ry0 - 0.5 <= (s[0][1] + s[0][3]) / 2 <= ry1 + 0.5]
            if grp:
                UNITS[i] = _mk(grp, pno, u["kind"], u["rect"])
    return UNITS


# 译文回填字体: 嵌入文档原生字体, 让译文与文档保留的数字/型号字体统一(治"字体不统一")。
# 探测文档主西文字体名 -> 映射系统字体文件; 匹配不到回退 Times New Roman; 系统无则 base-14。
_FONT_DIR = r"C:\Windows\Fonts"
_FONT_RULES = [
    (("times", "roman", "serif", "宋", "song", "simsun"), ("times.ttf", "timesbd.ttf")),
    (("arial", "helvetica", "sans", "黑", "hei"), ("arial.ttf", "arialbd.ttf")),
    (("calibri",), ("calibri.ttf", "calibrib.ttf")),
]

def _font_name(ff):
    return "f_" + os.path.splitext(os.path.basename(ff))[0]

def _resolve_doc_font(doc):
    """探测文档主西文字体, 返回(regular_ttf, bold_ttf)路径让译文与文档统一; 回退Times New Roman; 无则(None,None)=base-14。"""
    from collections import Counter
    cnt = Counter()
    for pno in range(min(doc.page_count, 3)):
        for b in doc[pno].get_text("dict")["blocks"]:
            if b.get("type", 0) != 0: continue
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    if any(ch.isascii() and ch.isalpha() for ch in s["text"]):
                        cnt[s["font"].lower()] += len(s["text"].strip())
    main = cnt.most_common(1)[0][0] if cnt else ""
    files = next((fs for keys, fs in _FONT_RULES if any(k in main for k in keys)), ("times.ttf", "timesbd.ttf"))
    reg = os.path.join(_FONT_DIR, files[0]); bd = os.path.join(_FONT_DIR, files[1])
    return (reg, bd) if (os.path.exists(reg) and os.path.exists(bd)) else (None, None)


_tmp = fitz.open(); _tp = _tmp.new_page(width=620, height=900)
def _fit_fs(text, fk, rect, start, align=0, fontfile=None):
    r = fitz.Rect(*rect); fs = min(start, 9.6)
    while fs >= 2.5:
        lo = _tp.insert_textbox(r, text, fontsize=fs, fontname=fk, fontfile=fontfile, align=align)
        if lo >= 0: return round(fs, 1), lo
        fs -= 0.2
    return 2.5, max(_tp.insert_textbox(r, text, fontsize=2.5, fontname=fk, fontfile=fontfile, align=align), 0.0)

def _place(page, rect, text, fk, col, align, start, vcenter=True, clear=None, fontfile=None):
    x0, y0, x1, y1 = rect
    fs, lo = _fit_fs(text, fk, rect, start, align, fontfile)
    top = y0 + (lo / 2 if vcenter else 0)
    box = fitz.Rect(x0, top, x1, y1)
    # 回填前铺白底, 盖掉原表单填空横线/杂线(治"横线划穿译文"); clear 给整格内部范围(差边框), 不给则只铺文字框
    page.draw_rect(fitz.Rect(*clear) if clear else box, color=None, fill=(1, 1, 1))
    return page.insert_textbox(box, text, fontsize=fs, fontname=fk, fontfile=fontfile, color=col, align=align)


def rebuild_translate(doc, units, trans=None):
    """按 unit['act'] 处理: translate=删源文回填译文 / delete=删不回填 / keep=不动。
    兼容纯翻译调用(无 act): 有译文=translate, 无=keep。"""
    trans = trans or {}
    reg_ff, bold_ff = _resolve_doc_font(doc)          # 探测文档原生字体, 译文嵌入同字体(字体统一)
    for u in units:
        if "act" not in u:
            u["act"] = "translate" if u["zh"] in trans else "keep"
            u["new"] = trans.get(u["zh"]) if u["act"] == "translate" else None
    from collections import defaultdict
    byp = defaultdict(list)
    for u in units: byp[u["page"]].append(u)
    KIMG = getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0)
    KART = getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0)
    RTXT = getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 2)
    bad = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        for u in byp[pno]:
            if u["act"] not in ("translate", "delete"): continue
            ux = u["redact"]
            rr = fitz.Rect(ux[0] - 0.3, ux[1] - 0.3, ux[2] + 0.3, ux[3] + 0.3)
            try: page.add_redact_annot(rr, fill=None)
            except Exception: page.add_redact_annot(rr, fill=(1, 1, 1))
        try: page.apply_redactions(images=KIMG, graphics=KART, text=RTXT)
        except TypeError: page.apply_redactions()

        for u in byp[pno]:
            if u["act"] != "translate": continue
            en = u.get("new")
            if not en: continue
            c, ux = u["rect"], u["redact"]; col = tuple(u["color"])
            ff = bold_ff if u["bold"] else reg_ff             # 嵌入文档原生字体(译文与文档统一); 无则 base-14
            fk = _font_name(ff) if ff else ("tibo" if u["bold"] else "tiro")
            en = _ascii_punct(en.replace("|||", "\n"))        # 多段分隔->换行 + 标点归一(base-14 缺智能标点字形)
            short_cell = u["kind"] == "cell" and len(u["zh"]) <= 8
            align = 1 if short_cell else 0                   # 短cell(表头/单位)居中, 其余左
            ins = 1.2; x0, y0c, x1, y1c = c
            # cell 和 para 都是表格单元格 -> 白底缩在格内(差边框)、文字按格框排+垂直居中, 不会盖表格线/不越界。
            # (para 之前误同"自由文字行 line"走 ux 锚定: 原文 bbox 顶可能在 cell 顶线之上, 导致白底盖掉顶线+
            #  文字顶到线+底部留空。para 是表格单元不是自由行, 归 cell 分支治本——见 jt/img_64 老板抓的。)
            if u["kind"] in ("cell", "para"):
                clear = (x0 + 0.7, y0c + 0.7, x1 - 0.7, y1c - 0.7)   # 整格内部铺白(差边框0.7), 盖满填空横线含右端线头
                ov = _place(page, (x0 + ins, y0c + ins, x1 - ins, y1c - ins), en, fk, col, align, min(u["size"], 9), vcenter=True, clear=clear, fontfile=ff)
            else:                                            # 仅 line(表格外自由文字, 如页眉公司名): ux 锚定不盖左侧 logo
                top = ux[1] - 0.6
                left = ux[0]            # 从原文左缘起排, 不从整格左缘(否则译出的公司名会盖住左侧 logo)
                clear = (left, top, x1 - 0.7, y1c - 0.3)            # 文字带铺白(从原文左缘起, 不碰左侧 logo)
                ov = _place(page, (left, top, x1 - ins, y1c + 1), en, fk, col, align, min(u["size"], 9.5), vcenter=False, clear=clear, fontfile=ff)
            if ov < -0.3: bad += 1
    return {"cells": len(units),
            "translated": sum(1 for u in units if u["act"] == "translate"),
            "deleted": sum(1 for u in units if u["act"] == "delete"),
            "overflow": bad}
