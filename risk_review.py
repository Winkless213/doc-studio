# -*- coding: utf-8 -*-
"""Risk clue review for customer-facing PDFs.

The engine already removes the obvious supplier name/logo/contact signals. This
module adds a human-review layer for the less obvious traces that should not be
auto-deleted without confirmation.
"""
import base64
import json
import os
import re
from collections import defaultdict

import fitz

from debrand import is_contact_line


MUST_PATTERNS = [
    ("supplier_slogan", re.compile(r"\b(mission|vision)\s*[:：].{0,80}(customer|value|client)", re.I),
     "页脚/标语可能暴露供应商"),
    ("qr_channel", re.compile(r"\b(qr\s*code|scan\s+to|wechat|whatsapp|alibaba|made-in-china)\b", re.I),
     "销售渠道或二维码线索"),
]

TRACE_TERMS = [
    "Harbin", "Wafangdian", "Luoyang", "Zhejiang Sanwei", "Sanwei",
    "Changzhou", "Briggs", "Koop", "Loncin", "Lutian", "Honda",
    "domestic brand", "imported brand", "brand-name", "hard-tooth-surface",
]

KEEP_RE = re.compile(r"^[A-Z]{1,4}-?\d{2,}[A-Z0-9.\-]*$", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _safe_term(s):
    t = _norm(s)
    if len(t) < 3:
        return ""
    if t in {"□□", "[]", "N/A"}:
        return ""
    if KEEP_RE.match(t):
        return ""
    return t


def decode_vision_payload(payload):
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except Exception:
        pass
    try:
        return json.loads(base64.b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def _line_items(page):
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line.get("spans", [])).strip()
            if not text:
                continue
            yield {
                "text": text,
                "bbox": list(line["bbox"]),
            }


def _candidate(page_no, text, bbox, level, kind, source, reason, auto=False):
    clean = _norm(text)
    return {
        "id": "",
        "page": page_no,
        "text": clean,
        "level": level,            # must | review
        "kind": kind,
        "source": source,          # rule | vision | user
        "reason": reason,
        "bbox": [round(float(x), 2) for x in bbox],
        "defaultAction": "delete" if auto else "review",
        "selected": bool(auto),
    }


def scan_pdf(pdf_path, brand_tokens=None, vision_payload=None):
    """Return risk candidates. Must-delete candidates are pre-selected; review
    candidates are visible but left unchecked for the user.
    """
    brand_tokens = [_safe_term(t) for t in (brand_tokens or [])]
    brand_tokens = [t for t in brand_tokens if t]
    vision = decode_vision_payload(vision_payload)
    vision_terms = [_safe_term(t) for t in (vision.get("brand_text") or [])]
    vision_terms = [t for t in vision_terms if t]

    candidates = []
    doc = fitz.open(pdf_path)
    for pno, page in enumerate(doc, start=1):
        for item in _line_items(page):
            text = item["text"]
            low = text.lower()
            bbox = item["bbox"]

            if is_contact_line(text):
                candidates.append(_candidate(pno, text, bbox, "must", "contact", "rule", "联系方式/网址/邮箱", True))
                continue

            if any(t.lower() in low for t in brand_tokens):
                candidates.append(_candidate(pno, text, bbox, "must", "brand", "rule", "命中供应商品牌词", True))
                continue

            matched_must = next(((kind, reason) for kind, rx, reason in MUST_PATTERNS if rx.search(text)), None)
            if matched_must:
                kind, reason = matched_must
                candidates.append(_candidate(pno, text, bbox, "must", kind, "rule", reason, True))
                continue

            vt = next((t for t in vision_terms if t.lower() in low), None)
            if vt:
                candidates.append(_candidate(pno, text, bbox, "must", "vision_text", "vision", f"视觉复检提示: {vt}", True))
                continue

            trace = next((t for t in TRACE_TERMS if t.lower() in low), None)
            if trace:
                candidates.append(_candidate(pno, text, bbox, "review", "trace", "rule", f"可追溯线索: {trace}", False))

    doc.close()

    # Add vision terms that do not appear in text extraction, so the user can see
    # why visual QA blocked the green light.
    seen_texts = {c["text"].lower() for c in candidates}
    for term in vision_terms:
        if term.lower() not in seen_texts:
            must_from_vision = any(rx.search(term) for _, rx, _ in MUST_PATTERNS)
            candidates.append({
                "id": "",
                "page": None,
                "text": term,
                "level": "must" if must_from_vision else "review",
                "kind": "vision_text",
                "source": "vision",
                "reason": "视觉复检提示，但文本层未定位到",
                "bbox": None,
                "defaultAction": "delete" if must_from_vision else "review",
                "selected": bool(must_from_vision),
            })

    # Deduplicate by page/text/kind.
    uniq = []
    keys = set()
    for c in candidates:
        key = (c.get("page"), c["text"].lower(), c["kind"])
        if key in keys:
            continue
        keys.add(key)
        c["id"] = "risk-%03d" % (len(uniq) + 1)
        uniq.append(c)
    summary = {
        "must": sum(1 for c in uniq if c["level"] == "must"),
        "review": sum(1 for c in uniq if c["level"] == "review"),
        "selected": sum(1 for c in uniq if c.get("selected")),
    }
    return {"summary": summary, "candidates": uniq}


def apply_deletions(in_pdf, out_pdf, selections):
    """Redact selected candidate lines by page+bbox when available, otherwise by
    exact line text. This keeps execution deterministic after human confirmation.
    """
    selections = selections or []
    by_page = defaultdict(list)
    free_terms = []
    for s in selections:
        text = _safe_term(s.get("text", ""))
        if not text:
            continue
        if s.get("page") and s.get("bbox"):
            by_page[int(s["page"])].append((text, fitz.Rect(s["bbox"])))
        else:
            free_terms.append(text)

    doc = fitz.open(in_pdf)
    removed = []
    for pno, page in enumerate(doc, start=1):
        page_targets = by_page.get(pno, [])
        line_targets = []
        if free_terms:
            for item in _line_items(page):
                line_low = item["text"].lower()
                if any(t.lower() in line_low for t in free_terms) or _free_term_semantic_hit(line_low, free_terms):
                    line_targets.append((_norm(item["text"]), fitz.Rect(item["bbox"])))
        for text, rect in page_targets + line_targets:
            page.add_redact_annot(rect, fill=None)
            removed.append({"page": pno, "text": text})
        if page_targets or line_targets:
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
            except TypeError:
                page.apply_redactions()
    doc.save(out_pdf, garbage=4, deflate=True)
    doc.close()
    return {"removed": removed, "removedCount": len(removed)}


def _free_term_semantic_hit(line_low, terms):
    """Vision often returns normalized slogans while the PDF text contains a
    slightly different wording. Treat mission/customer-value slogans as the same
    risk family and remove the whole footer line after user confirmation.
    """
    if not ("mission" in line_low and ("customer" in line_low or "value" in line_low)):
        return False
    return any("mission" in t.lower() and ("customer" in t.lower() or "value" in t.lower()) for t in terms)


def write_review_artifacts(pdf_path, out_dir, pages=None, max_head=2):
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    selected = set(range(1, min(max_head, doc.page_count) + 1))
    for p in pages or []:
        try:
            if 1 <= int(p) <= doc.page_count:
                selected.add(int(p))
        except Exception:
            pass
    paths = []
    for pno in sorted(selected):
        out = os.path.join(out_dir, f"p{pno:02d}.png")
        doc[pno - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(out)
        paths.append(out)
    doc.close()
    return paths
