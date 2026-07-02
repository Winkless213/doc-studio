/* 文档处理工具 桌面版 前端逻辑(无构建, 原生 JS)。
   流程: 上传 → /detect-type 自动判别 → 矢量(/both等) 或 图片向导(/analyze-catalog → 框选擦除 → /apply-catalog)。 */
'use strict'

let file = null
let docType = null          // 'vector' | 'image'
let catPages = []           // [{page,type,typeLabel,suggestedDelete,suggestErase,why,brandBoxes,thumb,del,eraseBoxes}]
let eraseTarget = null      // 当前编辑擦除的页对象
let currentReview = null

const $ = id => document.getElementById(id)
function toast(msg, kind) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast show ' + (kind || '')
  clearTimeout(t._t); t._t = setTimeout(() => { t.className = 'toast' }, 2600)
}

/* ---------- 上传 ---------- */
const drop = $('drop'), fileInput = $('file')
drop.onclick = () => fileInput.click()
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over') }
drop.ondragleave = () => drop.classList.remove('over')
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]) }
fileInput.onchange = () => { if (fileInput.files[0]) onFile(fileInput.files[0]) }

function onFile(f) {
  if (!/\.pdf$/i.test(f.name)) { toast('仅支持 PDF', 'err'); return }
  file = f
  $('fname').textContent = '已选择：' + f.name
  $('fname').classList.remove('hide')
  resetAll()
  detect()
}

function resetAll() {
  docType = null; catPages = []
  currentReview = null
  $('vectorCard').classList.add('hide')
  $('catCard').classList.add('hide')
  $('catReview').classList.add('hide')
  $('catStart').classList.remove('hide')
  $('vectorResult').innerHTML = '处理结果与质检会显示在这里。'
}

/* ---------- 判别 ---------- */
async function detect() {
  const bar = $('detectBar')
  bar.innerHTML = '<div class="detect busy"><span class="spin" style="border-color:#888;border-top-color:transparent"></span> 正在判别文档类型…</div>'
  const fd = new FormData(); fd.append('file', file)
  try {
    const r = await fetch('/detect-type', { method: 'POST', body: fd })
    const j = await r.json()
    setType(j.type === 'image' ? 'image' : 'vector')
  } catch (e) {
    setType('vector'); toast('判别失败，默认按矢量处理，可手动切换', 'err')
  }
}

function setType(tp) {
  docType = tp
  const bar = $('detectBar')
  const other = tp === 'image' ? 'vector' : 'image'
  const otherLabel = tp === 'image' ? '翻译 / 去品牌（矢量文档）' : '图片型目录清洗'
  const cur = tp === 'image' ? '检测为：图片型目录（品牌烤进图片）' : '检测为：矢量文档（报价单 / 矢量目录）'
  bar.innerHTML = `<div class="detect ${tp}">
    <span>${tp === 'image' ? '🖼' : '📄'}</span><span>${cur}</span>
    <span class="switch">判断错了？<a onclick="setType('${other}')">切换为${otherLabel}</a></span></div>`
  if (tp === 'vector') { $('vectorCard').classList.remove('hide'); $('catCard').classList.add('hide') }
  else { $('catCard').classList.remove('hide'); $('vectorCard').classList.add('hide'); catPages = []; $('catReview').classList.add('hide'); $('catStart').classList.remove('hide') }
}
window.setType = setType

/* ---------- 矢量处理 ---------- */
const SUFFIX = { both: '_clean', translate: '_translated', debrand: '_debranded' }
const CHECK = { translation: ['翻译完整', 'warn'], overflow: ['文字无溢出', 'warn'], brand: ['供应商品牌已清除', 'fail'], body_images: ['正文图片（可能含烤入品牌）', 'warn'] }

$('runVector').onclick = async () => {
  if (!file) return
  const mode = document.querySelector('input[name=mode]:checked').value
  const vision = $('vision').checked
  const btn = $('runVector'); btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 处理中' + (vision ? '（含视觉复检，较慢）' : '（翻译较慢，请稍候）')
  $('vectorResult').innerHTML = '处理中…'
  const fd = new FormData()
  fd.append('file', file)
  if (mode !== 'translate') fd.append('brand', $('brand').value || '')
  if (vision) fd.append('vision', 'true')
  const url = mode === 'translate' ? '/translate' : (mode === 'debrand' ? '/debrand' : '/both')
  try {
    const r = await fetch(url, { method: 'POST', body: fd })
    if (!r.ok) { let m = '处理失败'; try { m = (await r.json()).detail || m } catch (e) {} toast(m, 'err'); $('vectorResult').innerHTML = m; return }
    const blob = await r.blob()
    const base = file.name.replace(/\.pdf$/i, '')
    renderVectorResult(r.headers, blob, base + (SUFFIX[mode] || '_processed') + '.pdf')
  } catch (e) {
    toast('处理失败', 'err'); $('vectorResult').innerHTML = '处理失败'
  } finally {
    btn.disabled = false; btn.textContent = '开始处理'
  }
}

function parseQA(h) { const m = {}; (h || '').split(';').forEach(p => { const i = p.indexOf('='); if (i > 0) m[p.slice(0, i)] = p.slice(i + 1) }); return m }
function decodeVision(b64) { try { return JSON.parse(decodeURIComponent(escape(atob(b64)))) } catch (e) { return { issues: [], brand_text: [] } } }
function decodeReview(b64) { try { return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0)))) } catch (e) { return null } }
function esc(s) { return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) }

function renderVectorResult(headers, blob, fname) {
  const map = parseQA(headers.get('x-doc-qa'))
  const vis = headers.get('x-doc-qa-vision') ? decodeVision(headers.get('x-doc-qa-vision')) : { issues: [], brand_text: [] }
  currentReview = headers.get('x-doc-review') ? decodeReview(headers.get('x-doc-review')) : null
  const verdict = map.verdict || 'pass'
  const needsReview = currentReview && currentReview.summary && (currentReview.summary.must || currentReview.summary.review)
  const vWord = needsReview ? '需人工确认：发现供应商线索候选' : { pass: '可直接发客户', warn: '可发，建议先核对下方标注项', fail: '不建议发：品牌未清干净' }[verdict]
  let html = `<div class="verdict ${needsReview ? 'warn' : verdict}">${needsReview ? '⚠' : (verdict === 'pass' ? '✓' : (verdict === 'fail' ? '✕' : '⚠'))} ${vWord}</div>`
  Object.keys(CHECK).forEach(k => { if (map[k] !== undefined) { const n = +map[k]; const lv = n === 0 ? 'ok' : CHECK[k][1]; html += `<div class="chk ${lv}">${n === 0 ? '✓' : (lv === 'fail' ? '✕' : '⚠')} ${CHECK[k][0]}${n > 0 ? ' (' + n + ')' : ''}</div>` } })
  if (+map.logo > 0) html += `<div class="hint">已自动删除 ${map.logo} 个供应商 logo。</div>`
  if (vis.issues && vis.issues.length) { html += '<div class="label" style="margin-top:10px">AI 视觉复检</div>'; vis.issues.forEach(p => { html += `<div class="chk bad">第${p.page}页：${(p.issues || []).join('；')}</div>` }) }
  if (currentReview && currentReview.candidates && currentReview.candidates.length) html += renderRiskReview(currentReview)
  const url = URL.createObjectURL(blob)
  html += `<div style="margin-top:14px;display:flex;gap:10px;align-items:center">
    <a class="btn" href="${url}" download="${fname}">⬇ 导出 PDF</a>
    <span class="hint">核对质检结果无误后下载。</span></div>`
  $('vectorResult').innerHTML = html
}

function renderRiskReview(review) {
  const must = review.summary?.must || 0
  const pending = review.summary?.review || 0
  let html = `<div class="riskbox">
    <div class="riskhead"><span>风险线索审查</span><small>必删 ${must} · 待审 ${pending}</small></div>
    <div class="hint">勾选后会二次清洗。待审项默认保留，避免误删型号、产品配置或正常卖点。</div>`
  review.candidates.forEach(c => {
    const checked = c.selected ? 'checked' : ''
    const tag = c.level === 'must' ? '必删' : '待审'
    html += `<label class="riskrow ${c.level}">
      <input type="checkbox" data-risk="${esc(c.id)}" ${checked}>
      <span class="riskmeta"><b>${tag}</b>${c.page ? `第${c.page}页` : '未定位'} · ${esc(c.reason)}</span>
      <span class="risktext">${esc(c.text)}</span>
    </label>`
  })
  html += `<div class="riskactions">
    <button class="btn warn sm" onclick="applyRiskReview()">生成二次清洗版</button>
    <span class="hint">规则 QA 与视觉 QA 不一致时，以人工确认后的二次版为准。</span>
  </div></div>`
  return html
}

async function applyRiskReview() {
  if (!currentReview || !currentReview.id) return
  const selected = currentReview.candidates.filter(c => {
    const el = document.querySelector(`[data-risk="${CSS.escape(c.id)}"]`)
    return el && el.checked
  })
  if (!selected.length) { toast('没有勾选要删除的线索', 'err'); return }
  const btn = event && event.target ? event.target : null
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 二次清洗中' }
  const fd = new FormData()
  fd.append('review_id', currentReview.id)
  fd.append('selections', JSON.stringify(selected))
  try {
    const r = await fetch('/risk-review/apply', { method: 'POST', body: fd })
    if (!r.ok) { let m = '二次清洗失败'; try { m = (await r.json()).detail || m } catch (e) {} toast(m, 'err'); return }
    const blob = await r.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = (file ? file.name.replace(/\.pdf$/i, '') : 'document') + '_reviewed_clean.pdf'
    a.click()
    toast('已生成二次清洗版 PDF', 'ok')
    currentReview = r.headers.get('x-doc-review') ? decodeReview(r.headers.get('x-doc-review')) : currentReview
  } catch (e) {
    toast('二次清洗失败', 'err')
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '生成二次清洗版' }
  }
}
window.applyRiskReview = applyRiskReview

/* ---------- 图片向导 ---------- */
$('analyzeBtn').onclick = async () => {
  if (!file) return
  const btn = $('analyzeBtn'); btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 分析中（逐页看图，较慢）'
  const fd = new FormData(); fd.append('file', file); fd.append('brand', $('brand').value || '')
  try {
    const r = await fetch('/analyze-catalog', { method: 'POST', body: fd })
    const j = await r.json()
    if (!j.items || !j.items.length) { toast('没有可处理的页面', 'err'); return }
    catPages = j.items.map(it => ({ ...it, del: !!it.suggestedDelete, riskBoxes: (it.brandBoxes || []).slice(), eraseBoxes: [] }))
    $('catStart').classList.add('hide'); $('catReview').classList.remove('hide')
    renderCat()
  } catch (e) { toast('分析失败', 'err') } finally { btn.disabled = false; btn.textContent = '分析（逐页）' }
}

$('reChoose').onclick = () => { catPages = []; $('catReview').classList.add('hide'); $('catStart').classList.remove('hide') }

function renderCat() {
  const del = catPages.filter(p => p.del).length
  const erase = catPages.filter(p => !p.del && p.eraseBoxes.length).length
  $('catSum').textContent = `共 ${catPages.length} 页，将删 ${del} 页、擦除 ${erase} 页、保留 ${catPages.length - del} 页`
  const grid = $('catGrid'); grid.innerHTML = ''
  catPages.forEach(p => {
    const div = document.createElement('div')
    div.className = 'pg' + (p.del ? ' del' : (p.eraseBoxes.length ? ' erase' : ''))
    const eraseBadge = (!p.del && p.eraseBoxes.length) ? `<span class="erasebadge">擦 ${p.eraseBoxes.length}</span>` : ''
    const riskBadge = (!p.del && p.riskBoxes && p.riskBoxes.length) ? `<span class="riskbadge">待审 ${p.riskBoxes.length}</span>` : ''
    div.innerHTML = `
      <div class="thumb">
        <img src="${p.thumb}">
        ${p.del ? '<div class="mask">🗑 已删除</div>' : ''}
      </div>
      <div class="meta">
        <span class="pn">第 ${p.page} 页</span>
        <span class="tag ${p.suggestedDelete ? 'del' : ''}">${p.typeLabel}</span>
        ${eraseBadge}
        ${riskBadge}
      </div>
      <div class="meta" style="padding-top:0">
        <label style="cursor:pointer"><input type="checkbox" ${p.del ? 'checked' : ''} data-del="${p.page}"> 删页</label>
        ${p.del ? '' : `<button class="btn ghost sm" data-erase="${p.page}">审查/擦品牌</button>`}
      </div>`
    grid.appendChild(div)
  })
  grid.querySelectorAll('[data-del]').forEach(cb => cb.onchange = () => { const p = catPages.find(x => x.page == cb.dataset.del); p.del = cb.checked; renderCat() })
  grid.querySelectorAll('[data-erase]').forEach(b => b.onclick = () => openErase(catPages.find(x => x.page == b.dataset.erase)))
}

function applyInstr() {
  const txt = $('instr').value.trim(); if (!txt) return
  const ps = parseInstruction(txt, catPages.length)
  if (!ps.length) { toast('没识别到要删的页码', 'err'); return }
  const set = new Set(ps)
  catPages.forEach(p => { if (set.has(p.page)) p.del = true })
  $('instr').value = ''; renderCat(); toast('已标记 ' + ps.length + ' 页删除', 'ok')
}
window.applyInstr = applyInstr

const CN = { 一: 1, 两: 2, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 }
function cnNum(s) { return CN[s] || parseInt(s, 10) || 0 }
function parseInstruction(text, total) {
  const set = new Set(); let t0 = (text || '').trim()
  let m = t0.match(/最后\s*([0-9一二两三四五六七八九十]+)\s*页/) || t0.match(/last\s+(\d+)/i)
  if (m) { const n = cnNum(m[1] || m[2]); for (let i = 0; i < n; i++) if (total - i >= 1) set.add(total - i); return [...set] }
  m = t0.match(/前\s*([0-9一二两三四五六七八九十]+)\s*页/) || t0.match(/first\s+(\d+)/i)
  if (m) { const n = cnNum(m[1] || m[2]); for (let i = 1; i <= n && i <= total; i++) set.add(i); return [...set] }
  t0 = t0.replace(/第|页/g, ' ')
  t0.split(/[,，;；\s]+/).forEach(tok => {
    tok = tok.trim(); if (!tok) return
    const r = tok.match(/^(\d+)\s*[-~～]\s*(\d+)$/)
    if (r) { let a = +r[1], b = +r[2]; if (a > b)[a, b] = [b, a]; for (let i = a; i <= b; i++) if (i >= 1 && i <= total) set.add(i) }
    else { const n = +tok; if (n >= 1 && n <= total) set.add(n) }
  })
  return [...set]
}

/* ---------- 擦除编辑器(canvas 框选, 坐标都用 0-1 比例) ---------- */
let canvas, ctx, img2, boxes, drawing, startX, startY

function openErase(p) {
  eraseTarget = p
  boxes = p.eraseBoxes.length ? p.eraseBoxes.map(b => b.slice()) : (p.riskBoxes || []).map(b => b.slice())
  $('eraseTitle').textContent = `擦除品牌 — 第 ${p.page} 页`
  $('eraseOverlay').classList.add('show')
  canvas = $('eraseCanvas'); ctx = canvas.getContext('2d')
  img2 = new Image()
  img2.onload = () => {
    const maxW = Math.min(900, window.innerWidth - 120), maxH = window.innerHeight - 220
    let w = img2.width, h = img2.height, scale = Math.min(maxW / w, maxH / h, 1)
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale)
    redraw()
  }
  img2.src = p.thumb
  bindCanvas()
}
window.openErase = openErase

function redraw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img2, 0, 0, canvas.width, canvas.height)
  ctx.lineWidth = 2; ctx.strokeStyle = '#dc2626'; ctx.fillStyle = 'rgba(220,38,38,.22)'
  boxes.forEach(b => {
    const x = b[0] * canvas.width, y = b[1] * canvas.height, w = (b[2] - b[0]) * canvas.width, h = (b[3] - b[1]) * canvas.height
    ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h)
  })
  $('eraseInfo').textContent = '已框 ' + boxes.length + ' 个'
}

function bindCanvas() {
  canvas.onmousedown = e => { const r = canvas.getBoundingClientRect(); startX = (e.clientX - r.left) / canvas.width; startY = (e.clientY - r.top) / canvas.height; drawing = true }
  canvas.onmousemove = e => {
    if (!drawing) return
    const r = canvas.getBoundingClientRect(); const cx = (e.clientX - r.left) / canvas.width, cy = (e.clientY - r.top) / canvas.height
    redraw()
    ctx.strokeStyle = '#dc2626'; ctx.lineWidth = 2; ctx.fillStyle = 'rgba(220,38,38,.22)'
    const x = Math.min(startX, cx) * canvas.width, y = Math.min(startY, cy) * canvas.height
    const w = Math.abs(cx - startX) * canvas.width, h = Math.abs(cy - startY) * canvas.height
    ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h)
  }
  canvas.onmouseup = e => {
    if (!drawing) return; drawing = false
    const r = canvas.getBoundingClientRect(); const cx = (e.clientX - r.left) / canvas.width, cy = (e.clientY - r.top) / canvas.height
    const x0 = Math.min(startX, cx), y0 = Math.min(startY, cy), x1 = Math.max(startX, cx), y1 = Math.max(startY, cy)
    if (x1 - x0 > 0.01 && y1 - y0 > 0.01) boxes.push([+x0.toFixed(4), +y0.toFixed(4), +x1.toFixed(4), +y1.toFixed(4)])
    redraw()
  }
}
function clearBoxes() { boxes = []; redraw() }
function undoBox() { boxes.pop(); redraw() }
function closeErase() { $('eraseOverlay').classList.remove('show') }
function saveErase() { eraseTarget.eraseBoxes = boxes.map(b => b.slice()); closeErase(); renderCat(); toast('已保存擦除区域', 'ok') }
window.clearBoxes = clearBoxes; window.undoBox = undoBox; window.closeErase = closeErase; window.saveErase = saveErase

/* ---------- 一体导出 ---------- */
$('exportCat').onclick = async () => {
  const del = catPages.filter(p => p.del).map(p => p.page)
  if (del.length >= catPages.length) { toast('不能删除全部页面，至少保留一页', 'err'); return }
  const erase = {}
  catPages.forEach(p => { if (!p.del && p.eraseBoxes.length) erase[p.page] = p.eraseBoxes })
  const btn = $('exportCat'); btn.disabled = true
  btn.innerHTML = '<span class="spin"></span> ' + (Object.keys(erase).length ? '擦除+导出中（AI 修复较慢）' : '导出中')
  const fd = new FormData()
  fd.append('file', file); fd.append('delete_pages', JSON.stringify(del)); fd.append('erase', JSON.stringify(erase))
  try {
    const r = await fetch('/apply-catalog', { method: 'POST', body: fd })
    if (!r.ok) { let m = '处理失败'; try { m = (await r.json()).detail || m } catch (e) {} toast(m, 'err'); return }
    const blob = await r.blob()
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = file.name.replace(/\.pdf$/i, '') + '_cleaned.pdf'; a.click()
    toast('已导出干净版 PDF', 'ok')
  } catch (e) { toast('处理失败', 'err') } finally { btn.disabled = false; btn.textContent = '应用并导出' }
}
