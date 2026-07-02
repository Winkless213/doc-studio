# -*- coding: utf-8 -*-
"""图片型产品目录清洗 CLI 入口(这条线没服务化, 要人工判断环)。
流程: analyze(VL分类+框品牌) -> [人核 proposal] -> unify_header_boxes(统一页眉框) ->
       fill_failed(兜底VL失败页) -> decisions -> apply(删页+盖页眉+复审图)。
用法: 改下面 SRC/OUT/BRAND 跑; proposal.json 可人工编辑后重跑 apply。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8")
from engine_review import (analyze, unify_header_boxes, fill_failed_with_common_box,
                           proposal_to_decisions, apply)

SRC = r"input\catalog.pdf"
OUT = r"output\catalog_debranded.pdf"
REVIEW = r"review\catalog"
BRAND = "ACME, ACME Group"
REANALYZE = False   # True=重跑VL分析; False=复用已有 proposal.json(省VL调用)

if REANALYZE:
    res = analyze(SRC, REVIEW, brand_hint=BRAND)
    prop = res["proposal"]
else:
    prop = json.load(open(REVIEW + r"\proposal.json", encoding="utf-8"))

prop, union = unify_header_boxes(prop, REVIEW)   # 统一页眉框治单页VL框飘
fill_failed_with_common_box(prop)                # VL失败页兜底
print("统一页眉框:", union)
decisions = proposal_to_decisions(prop)
rep = apply(SRC, OUT, decisions, REVIEW)
print("apply:", json.dumps(rep, ensure_ascii=False))
print("成品:", OUT)
