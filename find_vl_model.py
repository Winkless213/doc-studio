# -*- coding: utf-8 -*-
"""视觉质检模型额度用尽时, 一键找可用替代。
用途: engine_qa 的视觉复检报 403 FreeTierOnly(某视觉模型免费额度用尽)时, 跑这个脚本——
它列出账户所有"能看图"的多模态模型, 逐个拿真图片实测(既验能收图、又验有额度), 打出哪些 OK。
然后把 OK 的填进环境变量 DOC_QA_VL_MODEL(免改代码)或 engine_qa.py 的 QA_VL_MODEL, 重启 doc 服务即可。

    cd D:/doc-debrand && python find_vl_model.py

筛选规则: 候选 = ①名字含 vl/vision/omni(专用视觉) ②主对话档 -plus/-flash/-max/-turbo
(qwen3.x 这些"对话模型"其实多为多模态、能收图, 且免费额度往往比专用视觉档大得多——qwen3.6-plus 就是这么发现的,
光按 vl/vision/omni 筛会漏掉它)。排除 realtime/audio/tts/asr/embedding/rerank/coder/math(非看图或会异常)
和带日期的快照(只测滚动别名, 减少冗余探测)。拿写了 'Hello ACME Corp' 的小图问"图里写了什么",
能正确读出 = 可用(有额度+能收图); 报 FreeTier/exhausted = 额度用尽; 其它 = 不支持/异常。
omni 系列经 compat 端点需限定纯文本输出(脚本已自动加 modalities=['text'])。
"""
import os, re, base64, fitz
from openai import OpenAI

KEY = os.environ.get("DASHSCOPE_API_KEY")
if not KEY:
    raise SystemExit("未设置 DASHSCOPE_API_KEY")
client = OpenAI(api_key=KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

# 造一张带字小图当探针
_d = fitz.open(); _p = _d.new_page(width=300, height=120)
_p.insert_text((20, 60), "Hello ACME Corp", fontsize=20)
B64 = base64.b64encode(_p.get_pixmap(dpi=100).tobytes("png")).decode()


def probe(model):
    try:
        kw = {"modalities": ["text"]} if "omni" in model else {}
        r = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "图里写了什么英文? 只回原文。"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + B64}}]}],
            extra_body={"enable_thinking": False}, **kw)
        out = r.choices[0].message.content.strip()[:40]
        return ("OK" if "ACME" in out else "OK?(读图存疑)"), out
    except Exception as e:
        s = str(e)
        if "FreeTier" in s or "exhausted" in s:
            return "额度用尽", "FreeTierOnly"
        return "不可用", s[:80]


VISION_KEYS = ("vl", "vision", "omni")
CHAT_KEYS = ("-plus", "-flash", "-max", "-turbo")            # qwen3.x 主对话档多为多模态
EXCLUDE = ("realtime", "audio", "tts", "asr", "embedding", "rerank", "coder", "math")
DATED = re.compile(r"-20\d\d-\d\d-\d\d$")                    # 带日期快照, 只测滚动别名


def is_candidate(m):
    lo = m.lower()
    if any(x in lo for x in EXCLUDE) or DATED.search(lo):
        return False
    return any(k in lo for k in VISION_KEYS) or any(k in lo for k in CHAT_KEYS)


def main():
    ids = [m.id for m in client.models.list().data]
    cands = sorted(m for m in ids if is_candidate(m))
    print("能看图的候选模型 %d 个, 逐个实测(读 'Hello ACME Corp'):\n" % len(cands))
    ok = []
    for m in cands:
        status, detail = probe(m)
        print("  %-32s %-12s %s" % (m, status, detail))
        if status.startswith("OK"):
            ok.append(m)
    print("\n可用(有额度+能读图): %s" % (", ".join(ok) if ok else "无! 需到阿里云控制台为某视觉模型开通付费"))
    print("换法: 设 DOC_QA_VL_MODEL=<上面任一> 或改 engine_qa.py QA_VL_MODEL, 再重启 doc 服务(端口 8200)。")


if __name__ == "__main__":
    main()
