# -*- coding: utf-8 -*-
"""
阶段三: 报价单 -> 结构化行项 -> Big KB 搜货 item JSON。
独立于 translate/debrand 已验路径。抓全量单元格(含数字, 不像 extract_cells 过滤 CJK),
按表格网格还原"一行一产品", 映射成 Big KB 标准 item。MVP 只出 JSON, 不写库。

Big KB item 标准结构(对齐 sourcing_intake._build_order_plan):
  {order_no, customer, demand(品名), spec(规格), qty(数量), unit(单位), unit_price?(扩展)}
"""
import fitz, re, json, argparse


def _cluster(vals, tol=3):
    vals = sorted(set(round(v, 1) for v in vals))
    out = []
    for v in vals:
        if out and v - out[-1][-1] <= tol: out[-1].append(v)
        else: out.append([v])
    return [sum(g) / len(g) for g in out]


def _grid(page):
    vlines, hlines = [], []
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
    return _cluster([v[0] for v in vlines]), _cluster([h[0] for h in hlines])


_NUM = re.compile(r"^\d+$")
_PRICE = re.compile(r"^\d+\.\d+$")   # 单价是小数(0.09/13.32), 纯整数多是数量/型号号

def _header_xmap(page):
    """读表头行(含'产品名称'/'单价'等)定位各字段的 x 区间, 不写死列序 -> 通用不同报价单。"""
    fields = {"产品名称": "demand", "型号规格": "spec", "数量单位": "qty_unit",
              "单价": "unit_price", "序号": "order_no"}
    found = {}  # field -> (x0, x1)
    for b in page.get_text("dict")["blocks"]:
        if b.get("type", 0) != 0: continue
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                t = s["text"].strip()
                if t in fields and fields[t] not in found:
                    found[fields[t]] = (s["bbox"][0], s["bbox"][2])
    return found


def quote_to_items(doc):
    """按表头 x 区间归字段(非列序, 抗合并单元格/变长规格)。只取产品行(序号纯数字)。
    供应商信息不在表格行内 = 天然不入库(去品牌红线)。"""
    items = []
    xmap = None
    for page in doc:
        hm = _header_xmap(page)
        if hm.get("order_no") and hm.get("unit_price"):
            xmap = hm                      # 本页有完整表头 -> 更新; 续页无表头沿用上页
        if not xmap:
            continue
        # 字段 x 中心(用表头格中心匹配同列数据)
        centers = {f: (x0 + x1) / 2 for f, (x0, x1) in xmap.items()}
        order_c = centers["order_no"]; price_c = centers["unit_price"]
        demand_c = centers.get("demand"); spec_c = centers.get("spec"); qty_c = centers.get("qty_unit")

        _, ys = _grid(page)
        # 按行(y)聚合所有 span, 每 span 记 (x_center, text)
        rowmap = {}
        for b in page.get_text("dict")["blocks"]:
            if b.get("type", 0) != 0: continue
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    t = s["text"].strip()
                    if not t: continue
                    cy = (s["bbox"][1] + s["bbox"][3]) / 2
                    cx = (s["bbox"][0] + s["bbox"][2]) / 2
                    r = next((i for i in range(len(ys) - 1) if ys[i] - 0.5 <= cy <= ys[i + 1] + 0.5), -1)
                    if r < 0: continue
                    rowmap.setdefault(r, []).append((cx, t, s["bbox"][0]))

        def near(spans, center, tol=18):
            """取 x 中心落在 center±tol 的 span 文本(按 x0 排序拼接)。"""
            hit = [(x0, t) for cx, t, x0 in spans if abs(cx - center) <= tol]
            return "".join(t for _, t in sorted(hit))

        for r in sorted(rowmap.keys()):
            spans = rowmap[r]
            order = near(spans, order_c, tol=12)
            if not _NUM.match(order):       # 只取产品行(序号=纯数字)
                continue
            qty_unit = near(spans, qty_c, tol=14) if qty_c else ""
            m = re.match(r"^(\d+)\s*(\D*)$", qty_unit)
            qnum, qunit = (m.group(1), m.group(2).strip()) if m else (qty_unit, "")
            # 单价: price 列附近的小数
            price_cell = [t for cx, t, _ in spans if abs(cx - price_c) <= 16 and _PRICE.match(t)]
            items.append({
                "order_no": order, "customer": "",
                "demand": near(spans, demand_c, tol=28) if demand_c else "",
                "spec": near(spans, spec_c, tol=28) if spec_c else "",
                "qty": qnum, "unit": qunit,
                "unit_price": price_cell[0] if price_cell else "",
            })
    return items


def _quote_supplier(doc):
    """报价单表头供应商名(含'公司'/'Co.'的最长串, 去联系电话尾巴)。
    入 ERP 货源要存供应商(对内), 与对客去品牌删供应商相反 —— 同份报价单两种用途。"""
    best = ""
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            if b.get("type", 0) != 0: continue
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if ("公司" in t or "Co." in t or "Ltd" in t) and len(t) > len(best):
                    best = t
    return re.split(r"联系电话|电话|报价单编号|Tel|联系", best)[0].strip()


def _quote_supplier_phone(doc):
    """供应商电话: 仅取'联系电话/电话/Tel'标签后的号码(明确标注才取, 不硬猜裸数字)。无标签返回空。"""
    for page in doc:
        m = re.search(r"(?:联系电话|联系方式|电话|Tel|TEL|Phone)\s*[:：]\s*([\d\-\s（）()]{7,22})", page.get_text())
        return re.sub(r"\s+", "", m.group(1)) if m else ""
    return ""


def _quote_supplier_address(doc):
    """供应商地址: 仅取'地址/Address'标签后的内容(明确标注才取)。没有就返回空 -> 前端留空+提示让人补, 不硬填。"""
    for page in doc:
        m = re.search(r"(?:地\s*址|Address|ADD)\s*[:：]\s*(.+)", page.get_text())
        return m.group(1).strip()[:80] if m else ""
    return ""


def _quote_total(doc):
    """出厂总价(找'出厂总价/总价'标签同行右侧的数)。报价单'价格/万元'口径。"""
    for page in doc:
        spans = [(s["text"].strip(), (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2)
                 for b in page.get_text("dict")["blocks"] if b.get("type", 0) == 0
                 for ln in b["lines"] for s in ln["spans"] if s["text"].strip()]
        for t, x, y in spans:
            if "出厂总价" in t or ("总价" in t and "单价" not in t):
                nums = [(xx, tt) for tt, xx, yy in spans if abs(yy - y) < 6 and xx > x and re.match(r"^[\d.,]+$", tt)]
                if nums:
                    return sorted(nums, key=lambda n: -float(n[1].replace(",", "")))[0][1]
    return ""


def quote_to_offer(doc):
    """整单对一行(header级): 报价单 -> 一个 ERP offer + 明细BOM。
    采购员人工选订单行后, 这个 offer 落 biz_offer(price/qty/unit/supplierInfo), 明细 BOM 随附。"""
    items = quote_to_items(doc)
    return {
        "supplierInfo": _quote_supplier(doc),   # offer.supplierInfo(文本, 不强制建档)
        "supplierPhone": _quote_supplier_phone(doc),     # 仅"电话:"标签后才取, 否则空(不硬填)
        "supplierAddress": _quote_supplier_address(doc), # 仅"地址:"标签后才取, 否则空(前端留空+提示让人补)
        "price": _quote_total(doc),             # offer.price(出厂总价, 万元口径; 入库换算/确认留人工)
        "priceCurrency": "CNY",
        "quantity": "1",                        # 整套=1套(采购员可改)
        "quantityUnit": "套",
        "bom": items,                           # 报价单全部明细行(一行不丢, 作 offer 支撑材料)
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True, help="输出 JSON")
    ap.add_argument("--offer", action="store_true", help="出整单 offer(供应商/总价/明细BOM), 否则出 item 数组")
    a = ap.parse_args()
    doc = fitz.open(a.inp)
    if a.offer:
        data = quote_to_offer(doc)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"offer -> {a.out}")
        print(f"  供应商: {data['supplierInfo']}")
        print(f"  总价: {data['price']} {data['priceCurrency']} (万元口径)  数量: {data['quantity']}{data['quantityUnit']}")
        print(f"  明细 BOM: {len(data['bom'])} 行")
    else:
        items = quote_to_items(doc)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"提取 {len(items)} 个产品行项 -> {a.out}")
        for it in items[:8]:
            print(f"  #{it['order_no']:>2} {it['demand']:<10} 型号={it['spec']:<14} {it['qty']}{it['unit']} 单价={it['unit_price']}")
