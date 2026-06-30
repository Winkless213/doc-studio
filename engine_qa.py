# -*- coding: utf-8 -*-
"""产出自检/质检(老板 6-25 要的"自检关", 让工具自己查、人不当找 bug 的人)。
两层:
  规则版 build_qa(result, mode): 汇总引擎已算的指标 -> PASS/WARN/FAIL, 零额外成本/额度。
  视觉版 vision_qa(pdf, brand_hint): qwen-vl 逐页"看一眼"是否可发客户, 抓规则查不到的视觉问题(opt-in, 用视觉额度)。
质检关注: 翻译完整(无中文残留)/版面(无文字溢出)/品牌清除(无残留=对外硬指标)/正文残留图(可能烤入品牌)。
"""
import os, json, base64, fitz

# 视觉质检模型: 多模态(能"看页面+描述残留")。qwen3.6-plus 实测能准确读出图内文字。
# 2026-06-29: 原 qwen-vl-plus 及 qwen3-vl-* 系列免费额度全部用尽(6/27 到期, FreeTierOnly 403);
# 换 qwen3.6-plus —— 它本身就是多模态(能收图)且免费额度充足(7 月才过期)。要更快/更省改 qwen3.6-flash(同样有视觉+额度)。
# ⚠️ 关思考(enable_thinking=False): dated -plus 默认开 thinking, 复杂页会从 ~2s 飙到 ~170s(同翻译路径规律)——见下方调用。
# 视觉额度与翻译/MCP 额度分开计、按模型独立。哪个空了挪到还有额度的多模态模型即可。
# 换模型免改代码: 设环境变量 DOC_QA_VL_MODEL=<新模型> 再重启 doc 服务即可(默认走下方常量)。换模型完整流程见
# project_doc_translation_debrand_tool memory「视觉质检模型额度用尽→换模型 SOP」。
QA_VL_MODEL = os.environ.get("DOC_QA_VL_MODEL", "qwen3.6-plus")


def build_qa(result, mode):
    """规则版自检: 引擎 result 里已算的指标 -> 质检项 + 总判定。
    verdict: pass(可发) / warn(可发但建议核) / fail(别发, 品牌泄露)。"""
    checks = []

    def add(key, ok, level, n):
        checks.append({"key": key, "ok": bool(ok), "level": level, "n": int(n)})

    if mode in ("translate", "both"):
        cjk = result.get("cjk_residual", 0)
        add("translation", cjk == 0, "warn", cjk)        # 中文残留字数
        ov = result.get("overflow", 0)
        add("overflow", ov == 0, "warn", ov)             # 文字溢出处数
    if mode in ("debrand", "both"):
        brand = result.get("brand_residual", result.get("residual", 0))
        add("brand", brand == 0, "fail", brand)          # 品牌词残留(对外硬指标, 残留=别发)
        img = result.get("images", {}) or {}
        add("body_images", img.get("kept_body", 0) == 0, "warn", img.get("kept_body", 0))  # 正文残留图

    has_fail = any(not c["ok"] and c["level"] == "fail" for c in checks)
    has_warn = any(not c["ok"] and c["level"] == "warn" for c in checks)
    verdict = "fail" if has_fail else ("warn" if has_warn else "pass")
    logo = (result.get("images", {}) or {}).get("removed", 0)
    return {"verdict": verdict, "checks": checks, "logo_removed": logo}


def qa_header(qa, vision=None):
    """把质检结果序列化成 ASCII 响应头串(头不能放中文, 数字+码; 前端按码渲染中英标签)。
    形如 verdict=warn;translation=0;overflow=0;brand=0;body_images=2;logo=1[;vision=ok|issues:N]"""
    parts = ["verdict=" + qa["verdict"]]
    for c in qa["checks"]:
        parts.append("%s=%d" % (c["key"], c["n"]))
    parts.append("logo=%d" % qa.get("logo_removed", 0))
    if vision is not None:
        parts.append("vision=" + ("ok" if vision.get("clean") else "issues:%d" % len(vision.get("pages_with_issues", []))))
    return ";".join(parts)


def vision_qa(pdf_path, brand_hint="", dpi=110, max_pages=12):
    """视觉版自检(opt-in): qwen-vl 逐页看产出是否可发客户。抓规则查不到的视觉问题
    (漏网 logo/烤入品牌、错位、空框、残留中文)。返回 {clean, pages_with_issues:[{page, issues}]}。
    复用 engine_review 的 VL 模型常量与端点; 用视觉额度(与翻译/MCP 额度分开)。"""
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        return {"clean": None, "error": "no DASHSCOPE_API_KEY", "pages_with_issues": [], "brand_text": []}
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    timeout=90.0, max_retries=0)
    doc = fitz.open(pdf_path)
    hint = ("，需删除的供应商品牌线索包含: " + brand_hint) if brand_hint else ""
    issues = []
    brand_text = []
    for pno in range(min(doc.page_count, max_pages)):
        png = doc[pno].get_pixmap(dpi=dpi).tobytes("png")
        b64 = base64.b64encode(png).decode()
        prompt = ("这是要发给海外客户的供应商文档的一页(已做翻译+去品牌处理)。请检查它是否可以直接发客户" + hint +
                  "。重点看: ①有没有残留的供应商 logo/公司名/电话/网址/标语口号 ②有没有没译干净的中文 "
                  "③有没有版面问题(文字重叠/溢出框/突兀空白框/文字压住表格线)。"
                  '只返回 JSON: {"clean": true/false, "issues": ["简短中文问题描述", ...], '
                  '"brand_text": ["页面上残留的供应商品牌/标语原文(照抄, 供一键去除用)", ...]}。'
                  "干净就 issues 与 brand_text 都空数组; brand_text 只放确属供应商品牌/标语的原文, 别放正常产品内容。")
        try:
            resp = client.chat.completions.create(
                model=QA_VL_MODEL, temperature=0,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
                extra_body={"enable_thinking": False})   # 关思考: 防复杂页推理链拖到分钟级(同翻译路径)
            txt = resp.choices[0].message.content.strip()
            obj = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
            if not obj.get("clean", True) and obj.get("issues"):
                issues.append({"page": pno + 1, "issues": obj["issues"][:5]})
            for bt in (obj.get("brand_text") or []):
                if isinstance(bt, str) and bt.strip() and bt.strip() not in brand_text:
                    brand_text.append(bt.strip())
        except Exception as e:
            issues.append({"page": pno + 1, "issues": ["视觉复检该页失败: " + str(e)[:50]]})
    return {"clean": len(issues) == 0, "pages_with_issues": issues, "brand_text": brand_text}
