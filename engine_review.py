# -*- coding: utf-8 -*-
"""
图片型 PDF 去品牌"审查向导"(image-type catalog debrand wizard)。
适用: 纯图片页 PDF(扫描件/设计导出, 无文本层, 品牌烤进像素), 现有文本去品牌(engine.py)无能为力者。

原则(老板 2026-06-24 定): 人只做判断(看/删/批准), 执行全自动, 绝不要人工 PS/换图。
  - 看(分类+定位品牌): qwen-vl 视觉模型(独立闲置额度) —— 替代"人逐张看"
  - 判断(删哪些/盖哪些): 工具出"待你定清单", 人逐条点 = 护城河
  - 执行(删页/盖页眉白底): Python, 零模型成本, 零 PS
复杂背景封面(烤字、盖白会丑)默认"删页"处理; LaMa 像素擦除暂不做(要装才上)。

流程: analyze(出清单+缩略图) -> 人确认/改 decisions -> apply(删页+盖带, 出成品+复审图)。
"""
import fitz, os, json, base64

# 视觉模型(逐页分类+框品牌)。原 qwen-vl-max 免费额度 6/27 过期(403 FreeTierOnly)→ 换 qwen3.6-plus
# (多模态、有额度;分类任务够用。grounding 框可能偏弱,但删页只用 type/suggest 不依赖框)。
# env DOC_QA_VL_MODEL 可覆盖(与 engine_qa 同一个开关,换一处即两边一起换)。换模型流程见 find_vl_model.py。
VL_MODEL = os.environ.get("DOC_QA_VL_MODEL", "qwen3.6-plus")
_DASH = "https://dashscope.aliyuncs.com/compatible-mode/v1"
THUMB_DPI = 110               # 缩略图渲染 dpi; VL 返回的 px 框按此换算回 pt(px*72/dpi)


def render_thumbs(doc, out_dir, dpi=THUMB_DPI):
    """每页渲染缩略图(给人看 / 给 VL 看)。"""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i in range(doc.page_count):
        p = os.path.join(out_dir, f"p{i+1:02d}.png")
        doc[i].get_pixmap(dpi=dpi).save(p)
        paths.append(p)
    return paths


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


_CLS_PROMPT = """你在审查一本产品目录的某一页(图片),目的是去除"供应商可追溯信息"后转给经销商。
只返回 JSON(不要解释):
{"type":"cover|product|promo|contact|cert|other",
 "suggest":"keep|delete|clean",
 "brand_boxes":[[x0,y0,x1,y1], ...],
 "why":"一句中文"}
- type: cover=封面; product=产品(型号/规格/产品图,要留); promo=企业宣传(简介/发展史/工厂/文化/客户案例); contact=联系方式/销售渠道/二维码; cert=证书荣誉(图内常烤公司名)。
- brand_boxes: 框出页面上所有"供应商可追溯信息"(公司名/品牌logo/电话/网址/二维码/地址)的像素坐标[左上x0,y0,右下x1,y1],左上角(0,0)。**只框品牌, 绝不框产品标题/型号/规格/参数/产品图**。无则给 []。
- suggest 规则: 宣传/联系/封面页=delete(整页删,比盖白干净); 产品页且品牌只是少量角标/页眉logo(盖掉不伤内容)=clean(回填白底盖掉 brand_boxes); 产品页若品牌压在产品图/彩色背景上(盖白会留丑块或毁图)=keep 并在 why 注明"需人工确认/LaMa"; 无品牌产品页=keep。"""


def classify_pages_vl(thumb_paths, brand_hint=""):
    """用 qwen-vl 逐页分类+框出品牌(px坐标), 返回 proposal 列表。VL 调不动则全部退回 unknown(交人工)。"""
    from openai import OpenAI
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        return [{"page": i + 1, "type": "unknown", "suggest": "review", "brand_boxes": [],
                 "why": "无 DASHSCOPE_API_KEY, 交人工看缩略图"} for i in range(len(thumb_paths))]
    client = OpenAI(api_key=key, base_url=_DASH)
    hint = f"\n已知品牌词(供参考): {brand_hint}" if brand_hint else ""
    out = []
    for i, tp in enumerate(thumb_paths):
        try:
            resp = client.chat.completions.create(
                model=VL_MODEL, temperature=0,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _CLS_PROMPT + hint},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(tp)}"}},
                ]}],
                extra_body={"enable_thinking": False})   # 关思考: 防逐页推理链拖到分钟级
            txt = resp.choices[0].message.content.strip()
            d = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
            d["page"] = i + 1
            d.setdefault("brand_boxes", [])
            out.append(d)
        except Exception as e:
            out.append({"page": i + 1, "type": "unknown", "suggest": "review", "brand_boxes": [],
                        "why": f"VL 失败({type(e).__name__}), 交人工"})
    return out


def analyze(in_pdf, review_dir, brand_hint=""):
    """出"待你定清单": 渲染缩略图 + VL 分类。返回 proposal(每页一条, 含建议动作)。"""
    doc = fitz.open(in_pdf)
    thumbs = render_thumbs(doc, review_dir)
    n = doc.page_count
    doc.close()
    proposal = classify_pages_vl(thumbs, brand_hint)
    with open(os.path.join(review_dir, "proposal.json"), "w", encoding="utf-8") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=1)
    return {"pages": n, "thumbs": review_dir, "proposal": proposal}


def apply(in_pdf, out_pdf, decisions, review_dir=None, pad=4):
    """执行决策。decisions=[{page, action, brand_boxes(px,可选)}], action: keep|delete|clean。
    clean=按 brand_boxes(VL 像素坐标, px*72/THUMB_DPI 换回 pt)白底盖掉品牌; delete=物理删页。
    VL 框偏紧(实测漏 logo 下沿) -> 按比例放大(横0.25/纵0.45, 仍小于到正文的间距), 截到页内。
    复审图渲染到 review_dir/after。"""
    sc = 72.0 / THUMB_DPI
    doc = fitz.open(in_pdf)
    dmap = {d["page"]: d for d in decisions}
    covered = []; flagged = []
    for pno in range(doc.page_count):
        d = dmap.get(pno + 1)
        if not d or d.get("action") != "clean":
            continue
        page = doc[pno]; r = page.rect
        body = 0
        for bx in d.get("brand_boxes", []):
            if len(bx) != 4:
                continue
            x0, y0, x1, y1 = [v * sc for v in bx]
            # 只自动盖"页眉 logo 带"(顶部 7%) = 白底, 盖了无痕。产品标题在 8% 处(刚好排除), 不误裁。
            # 正文/产品图上的角标(y0 更低)不乱盖(易裁内容/留丑白块) -> 计数上报, 交人工或 LaMa。
            if y0 > 0.07 * r.height:
                body += 1
                continue
            ex = max(pad, 0.25 * (x1 - x0)); ey = max(pad, 0.45 * (y1 - y0))
            box = fitz.Rect(x0 - ex, y0 - ey, x1 + ex, y1 + ey) & r
            if not box.is_empty:
                page.draw_rect(box, color=(1, 1, 1), fill=(1, 1, 1))
        covered.append(pno + 1)
        if body:
            flagged.append({"page": pno + 1, "body_marks": body})
    drop = sorted([p - 1 for p, d in dmap.items() if d.get("action") == "delete"], reverse=True)
    for i in drop:
        if 0 <= i < doc.page_count:
            doc.delete_page(i)
    doc.save(out_pdf, garbage=4, deflate=True)
    out_pages = fitz.open(out_pdf).page_count
    if review_dir:
        after = os.path.join(review_dir, "after")
        os.makedirs(after, exist_ok=True)
        chk = fitz.open(out_pdf)
        for i in range(chk.page_count):
            chk[i].get_pixmap(dpi=THUMB_DPI).save(os.path.join(after, f"p{i+1:02d}.png"))
        chk.close()
    return {"out_pages": out_pages,
            "dropped": sorted(p for p, d in dmap.items() if d.get("action") == "delete"),
            "covered": covered, "flagged": flagged}


def fill_failed_with_common_box(proposal):
    """VL 失败页(suggest=review)兜底: 模板目录页眉位置一致, 从成功 clean 页学"通用页眉框"
    (取出现在顶部 1/4 区的框、按中位坐标)套上去, 改成 clean + flag 请人工复核。原地改 proposal。"""
    tops = []
    for p in proposal:
        if p.get("suggest") == "clean":
            for bx in p.get("brand_boxes", []):
                if len(bx) == 4 and bx[1] < 200:        # 顶部区(px, 缩略图顶 1/4)
                    tops.append(bx)
    if not tops:
        return proposal
    tops.sort(key=lambda b: b[1])
    med = sorted(tops, key=lambda b: b[0] * 1e4 + b[1])[len(tops) // 2]   # 取一个代表性顶部框
    for p in proposal:
        if p.get("suggest") == "review":
            p["suggest"] = "clean"
            p["brand_boxes"] = [med]
            p["why"] = (p.get("why", "") + " | 已套通用页眉框兜底,请复核").strip()
    return proposal


def unify_header_boxes(proposal, review_dir, edge=0.075):
    """模板目录页眉 logo 位置固定 -> 取所有 clean 页顶部带(y0<edge*页高)框的并集, 套到每个 clean 页页眉。
    治单页 VL grounding 框偏小/飘导致 logo 残留(实测某样例目录: 单页框 [25,18,137,60] 漏 logo 右端方块+GROUP)。
    并集框覆盖所有页 logo 最大范围, 模板一致性保证盖全; body 区(产品图角标)框保留不动(仍走 flagged 不盖)。
    需 review_dir 读缩略图页高(px)换算 edge 带。原地改 proposal, 返回 (proposal, 并集框 or None)。"""
    thumbs = [os.path.join(review_dir, f) for f in os.listdir(review_dir) if f.startswith("p") and f.endswith(".png")]
    if not thumbs:
        return proposal, None
    page_h = fitz.Pixmap(sorted(thumbs)[0]).height
    tops = [b for p in proposal if p.get("suggest") == "clean"
            for b in p.get("brand_boxes", []) if len(b) == 4 and b[1] < page_h * edge]
    if not tops:
        return proposal, None
    union = [min(b[0] for b in tops), min(b[1] for b in tops),
             max(b[2] for b in tops), max(b[3] for b in tops)]
    for p in proposal:
        if p.get("suggest") == "clean":
            body = [b for b in p.get("brand_boxes", []) if len(b) == 4 and b[1] >= page_h * edge]
            p["brand_boxes"] = [union] + body
    return proposal, union


def proposal_to_decisions(proposal):
    """proposal 的 suggest -> 默认 decisions(人不改就照此执行); clean 带上 brand_boxes。"""
    out = []
    for p in proposal:
        act = {"delete": "delete", "clean": "clean"}.get(p.get("suggest"), "keep")
        out.append({"page": p["page"], "action": act, "brand_boxes": p.get("brand_boxes", [])})
    return out
