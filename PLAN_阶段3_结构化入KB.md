# 阶段三(A): 报价单结构化 → Big KB 货源库

> 2026-06-24 核实 Big KB 真实入库结构后定。write-plan, 过目再 execute。

## 已核实的 Big KB 结构(真读代码, 非凭记忆)
Big KB 有**两套**结构, 别搞混:
1. **搜货输入 item**(`sourcing_intake._build_order_plan` 产出, 各入口统一):
   `{order_no, customer, demand品名, spec规格, qty数量, unit单位, keyword?}`
   → ERP询盘/docx/多文件全映射成它 → enrich(补keyword/hint/歧义) → plan → 搜货。
2. **搜货输出 candidate**(`models/sourcing.py` SourcingCandidate, 入库表):
   `{platform, title, price_cny, price_usd, sales, score, comment, link}`
   = 从1688搜回来的候选, **不是供应商报价**。

## ⭐ 方向修正(2026-06-24 老板反问 + 调研, 推翻"接KB搜货")
老板反问点醒: 报价单是**采购流程产物**(源头ERP询盘→KB搜货→供应商发报价单), 已经是报价结果,
**不需要再喂KB搜货**(我之前定位错)。正确归宿 = **ERP 的 offer(业务流第4步"采购员上传采购方案")**。
offer 提交后 ERP 现有 `sinkFromOffer` 自动沉淀货源库 `biz_product_supplier` —— 这才"惠及真流程"。
⚠️ 分清: ERP货源库 biz_product_supplier(offer自动沉淀, ERP内部) ≠ Big KB(搜1688市场情报, 独立)。
**报价单基本不接 Big KB**(它不是搜货输入)。

## 粒度调研结论(老板担心"逐行匹配正确率", 真查了)
- **逐行自动匹配订单行 = 行业难题**: 无统一料号时供应商描述vs订单品名差异大("elbow pipe"vs
  "right angle pipe"), 需供应商专属规范化, 不保准。"95-99%"是**从PDF抽行项**准确率(已做到),
  **非匹配到订单行**准确率(无料号低得多)。
- **整单对一行 = header-level, 行业稳妥主流**, 缺料号贸易场景不丢专业。
- **最佳 = 整单对一行 + 43行明细全留(BOM) + 采购员人工选订单行**: 把"匹配"从算法猜→人一键选
  (100%对, 符合人工闸理念, 同 Big KB 询盘联动交互)。明细全留=信息不比逐行拆少。
- 来源: Settle(PO line matching)/Width.ai(无SKU描述差异+LLM匹配)/Lido(抽取准确率)/Oracle(header copy)。

## offer 字段映射(报价单 → biz_offer, 已核实真实结构)
| 报价单 | biz_offer | 备注 |
|---|---|---|
| 表头供应商名 | supplierInfo(文本, 不强制建档) | 入ERP**存**供应商(对内), 与对客去品牌**删**相反=同份两用途 |
| 出厂总价 | price | 万元口径, 换算/确认留人工 |
| 1套 | quantity + quantityUnit | 采购员可改 |
| 43行明细 | bom(附明细) | 一行不丢, offer支撑材料 |
| (人工选) | orderId + orderItemId | 采购员选"对应哪个订单行", 避开匹配难题 |

## ⚠️ 审计修正(2026-06-24, 真数据验出)
**不能复用 translate 的 `extract_cells`** —— 它带 `has_cjk` 过滤(翻译不翻数字, 对的),
但报价单 p1 有 **164 个纯数字单元(价格13.32/199.80、数量、型号11011)被它漏掉**。
结构化入 KB 时**价格/数量是核心**, 复用 extract_cells 会让这些字段全空(happy path 也"跑通"
但 JSON 没价格 = feedback_verify_edge_cases 的坑)。
→ 阶段三**另起 `extract_grid_rows`(抓全量单元格含数字, 按行列归位)**,
  **不动 extract_cells / translate / debrand 已验路径**。

## 方案: 引擎加第四入口 `extract_quote_items`
新抽取 `extract_grid_rows` 抓全量单元格(含数字) → 按行(y)聚合、按列(x)归位 →
吐 Big KB 搜货 item。**这是第三产物, 与翻译/去品牌路径独立**。

### 字段映射(报价单表格列 → Big KB item)
| 报价单列 | Big KB item 字段 | 备注 |
|---|---|---|
| 产品名称 | demand | 搜货主词 |
| 型号规格 | spec | 规格(报价单独有型号, 比询盘全) |
| 数量 | qty | |
| 单位(数量单位) | unit | |
| 单价/价格 | (扩展)unit_price | item 现无此字段 → 加可选, 不破坏现有 |
| 功率/kw | spec 追加 | 并进规格 |
| 序号 | order_no | 行号 |
| 供应商名 | **丢弃** | 去品牌红线, 不入库 |

### 产出形式(MVP, 不直接写库)
先**只吐 JSON**(`items.json` = Big KB 搜货 item 数组), 不碰 Big KB 数据库 / 不跨进程写。
理由(规约16+11): ①跨项目直接写库风险高(schema/事务/并发) ②老板要人工核价闸
③Big KB 入库口是搜货流程的, 报价单是"待搜/待录"不是"已搜候选", 强塞 candidate 表语义错。
→ MVP = 引擎出标准 item JSON, **能被 Big KB 现有 `build_plan` 直接吃**(去搜货)或人工核后录。

## 表格行重组(技术核心 + 风险)
184 单元现在是"按单元格"散的, 要**按行(y 坐标)聚合 + 按列(x)归位**才成行项。
报价单有合并单元格(规格描述跨多列)、多层表头 → 行重组是这阶段唯一硬骨头。
**execute 第一步先验**: 能不能从 184 单元稳定还原出"一行一产品"的结构(拿真报价单验,
对照人看到的行数/产品数)。还原不稳就别往下。

## 分阶段
1. ✅ 行重组验证: 真报价单全量单元 → 网格还原"一行一产品", 序号1-43连续, 价格/型号都在。
2. ✅ 字段映射 → `engine_structify.quote_to_items` 出 items(Big KB 格式), 43行项缺价/量/名各0。
3. ✅ 结构对齐 Big KB item(order_no/customer/demand/spec/qty/unit + 扩展unit_price), 可直接喂 build_plan。
4. (未来)直连入库 / ERP 货源库 —— 需老板定写库口 + 核价闸, 不在 MVP。

## ✅ 完成(2026-06-24)
`engine_structify.py`: `python engine_structify.py --in 报价单.pdf --out items.json`。
**审计揪出并修的真坑**: ①不能复用 translate 的 extract_cells(has_cjk 漏 164 个数字单元含价格)
→ 另起全量抽取 ②按列序 g(0..6) 取值遇合并单元格/变长规格会错位(钢板仓行数量/价全错)
→ 改 **按表头 x 区间归字段**(_header_xmap 读"产品名称/单价"等表头定位, 不写死列序, 通用不同报价单)
③标题"15—900T"序号15骗过过滤 → x区间法天然排除。结果 43 行项, 价/量/名零缺失。

## 不做(规约2/16)
- 不直接写 Big KB / ERP 数据库(MVP 只出 JSON)
- 不碰已验的 translate / debrand / both 路径
- 不加价格换算/汇率/去重(那是 Big KB/ERP 侧的事)
