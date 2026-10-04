// paper-index annotator: runs inside every doc frame (live page, reader view, PDF viewer) on <name>.localhost.
// Paints highlights with the CSS Custom Highlight API (the page's DOM is never touched) and talks to the app over
// postMessage. It runs before the page's own scripts and signs messages with a token they can't read.
(() => {
  if (window.top === window || window.__paperIndex) return;
  window.__paperIndex = true;
  const params = new URL(document.currentScript.src).searchParams, VIEW = params.get('view'), REAL_ORIGIN = params.get('origin');
  const TOKEN = crypto.randomUUID(), APP = window.parent, style = document.createElement('style');
  const SKIP = 'svg,canvas,script,style,noscript,template,button,select,textarea,input';  // graphs and controls aren't text
  const ranges = new Map();  // annotation id -> [Range], one per text node
  let origin = '*', anns = [], cats = [], def = null, tracked = null, selection = null, heads = [], pdfHeads = [], sent = {};
  const send = msg => APP.postMessage({ pi: 1, token: TOKEN, ...msg }, origin);
  const sendIfChanged = msg => { const k = JSON.stringify(msg); if (sent[msg.type] !== k) { sent[msg.type] = k; send(msg); } };
  send({ type: 'hello' });
  try { navigator.serviceWorker.register = () => Promise.reject(new Error('disabled')); } catch {}

  // ---- text model: offsets count highlightable characters within a root (the page, or one PDF page) ----
  const roots = () => VIEW === 'pdf' ? [...document.querySelectorAll('.textLayer.ready')] : [document.querySelector('[data-pi-root]') || document.body];
  const rootFor = a => VIEW === 'pdf' ? document.querySelector(`.textLayer.ready[data-page="${a.page}"]`) : roots()[0];
  const rootOf = n => { const el = n?.nodeType === 1 ? n : n?.parentElement; return el && roots().find(r => r.contains(el)); };
  function textOf(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: n => n.parentElement?.closest(SKIP) ? 2 : 1 });
    const nodes = [];
    for (let n; (n = w.nextNode());) nodes.push(n);
    return { nodes, text: nodes.map(n => n.data).join('') };
  }
  function offsetOf({ nodes }, node, off) {
    const point = new Range();
    point.setStart(node, off);
    let pos = 0;
    for (const n of nodes) {
      if (n === node) return pos + off;
      if (point.comparePoint(n, 0) > 0) break;  // this text starts after the point
      pos += n.data.length;
    }
    return pos;
  }
  function rangesFor({ nodes }, start, end) {
    const out = [];
    for (let i = 0, pos = 0; i < nodes.length && pos < end; pos += nodes[i++].data.length) {
      const n = nodes[i], s = Math.max(start, pos) - pos, e = Math.min(end, pos + n.data.length) - pos;
      if (s < e && n.data.slice(s, e).trim()) { const r = new Range(); r.setStart(n, s); r.setEnd(n, e); out.push(r); }
    }
    return out;
  }
  function locate(text, a) {  // exact offsets if the text is unchanged, else the nearest copy of the quote
    if (a.view === VIEW && text.slice(a.start, a.end) === a.quote) return [a.start, a.end];
    const words = a.quote.trim().split(/\s+/).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!words[0] || words.length > 400) return null;
    const near = [...text.matchAll(new RegExp(words.join('\\s*'), 'g'))].sort((x, y) => Math.abs(x.index - a.start) - Math.abs(y.index - a.start))[0];
    return near && [near.index, near.index + near[0].length];
  }
  const rectOf = rs => {
    const bs = (rs || []).map(r => r.getBoundingClientRect()).filter(b => b.width || b.height);
    const pick = (k, f) => f(...bs.map(b => b[k]));
    return bs.length ? { left: pick('left', Math.min), right: pick('right', Math.max), top: pick('top', Math.min), bottom: pick('bottom', Math.max) } : null;
  };

  // ---- painting ----
  function repaint() {
    if (!window.Highlight) return;
    const rgba = (hex, a) => `rgba(${[1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16))},${a})`;
    style.textContent = cats.map(c => `::highlight(pi-c-${c.id}){background-color:${rgba(c.color, .45)}}`).join('') +
      '::highlight(pi-note){text-decoration:underline dotted 2px rgba(0,0,0,.55)}::highlight(pi-resolved){background-color:rgba(128,128,128,.16)}' +
      '::highlight(pi-flash){background-color:rgba(47,91,211,.4)}svg,canvas{user-select:none}';
    if (!style.isConnected) (document.head || document.documentElement).append(style);
    const groups = Object.fromEntries([...cats.map(c => 'c-' + c.id), 'note', 'resolved'].map(k => [k, new Highlight()]));
    const texts = new Map(), orphans = [];
    ranges.clear();
    for (const a of anns) {
      const root = rootFor(a);
      if (!root) continue;  // PDF page not rendered yet
      if (!texts.has(root)) texts.set(root, textOf(root));
      const t = texts.get(root), at = locate(t.text, a), rs = at ? rangesFor(t, ...at) : [];
      if (!rs.length) { orphans.push(a.id); continue; }
      ranges.set(a.id, rs);
      const group = a.resolved ? groups.resolved : groups['c-' + a.color] || groups['c-' + def];
      rs.forEach(r => { group.add(r); if (a.comment && !a.resolved) groups.note.add(r); });
    }
    for (const k of CSS.highlights.keys()) if (k.startsWith('pi-') && k !== 'pi-flash') CSS.highlights.delete(k);
    for (const [k, h] of Object.entries(groups)) CSS.highlights.set('pi-' + k, h);
    sendLayout(orphans);
  }

  // ---- side panel: outline, highlight order, what's on screen ----
  const headEl = h => h.el || document.querySelector(`.page[data-page="${h.page}"]`);
  function sendLayout(orphans) {
    heads = VIEW === 'pdf' ? pdfHeads : [...roots()[0].querySelectorAll('h1,h2,h3,h4')]
      .filter(el => el.textContent.trim() && el.getClientRects().length && !el.closest('nav,footer,[aria-hidden="true"]'))
      .slice(0, 300).map(el => ({ el, level: +el.tagName[1], text: el.textContent.trim().replace(/\s+/g, ' ').slice(0, 140) }));
    const pos = a => VIEW !== 'pdf' && ranges.get(a.id)?.[0];
    const order = [...anns].sort((x, y) => pos(x) && pos(y) ? pos(x).compareBoundaryPoints(Range.START_TO_START, pos(y)) : (x.page || 0) - (y.page || 0) || x.start - y.start);
    const section = a => heads.findLastIndex(h => VIEW === 'pdf' ? h.page <= a.page : pos(a) && pos(a).comparePoint(h.el, 0) < 0);
    sendIfChanged({ type: 'layout', order: order.map(a => a.id), orphans, sections: Object.fromEntries(anns.map(a => [a.id, section(a)])),
                    headings: heads.map(({ level, text }) => ({ level, text })) });
    onScroll();
  }
  function onScroll() {
    const heading = heads.findLastIndex(h => headEl(h)?.getBoundingClientRect().top <= innerHeight * 0.3);
    const visible = [...ranges].filter(([, rs]) => { const b = rectOf(rs); return b && b.bottom > 0 && b.top < innerHeight; }).map(([id]) => id);
    sendIfChanged({ type: 'scrolled', heading, visible });
  }
  let frame = 0;
  addEventListener('scroll', () => {
    const rect = rectOf(tracked === 'sel' ? selection && [selection] : ranges.get(tracked));
    if (rect) send({ type: 'rect', rect });
    frame ||= requestAnimationFrame(() => { frame = 0; onScroll(); });
  }, { capture: true, passive: true });

  // ---- PDF: pages render as they come near; the outline lists the pages ----
  async function renderPdf() {
    pdfjsLib.GlobalWorkerOptions.workerSrc = document.querySelector('script[src*="pdf.min.js"]').src.replace('pdf.min', 'pdf.worker.min');
    const pdf = await pdfjsLib.getDocument('/__pi/file').promise, box = document.getElementById('pdf');
    const io = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && e.target.render()), { root: document, rootMargin: '1500px 0px' });
    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i), vp = page.getViewport({ scale: Math.min(box.clientWidth - 24, 980) / page.getViewport({ scale: 1 }).width });
      const el = Object.assign(box.appendChild(document.createElement('div')), { className: 'page' });
      el.style.cssText = `width:${vp.width}px;height:${vp.height}px;--scale-factor:${vp.scale}`;
      el.dataset.page = i;
      el.render = () => el.task ||= (async () => {
        const dpr = devicePixelRatio || 1, canvas = document.createElement('canvas'), layer = document.createElement('div');
        Object.assign(canvas, { width: vp.width * dpr, height: vp.height * dpr });
        Object.assign(layer, { className: 'textLayer' }).dataset.page = i;
        el.append(canvas, layer);
        await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp, transform: [dpr, 0, 0, dpr, 0, 0] }).promise;
        await pdfjsLib.renderTextLayer({ textContentSource: await page.getTextContent(), container: layer, viewport: vp, textDivs: [] }).promise;
        layer.classList.add('ready');
        repaint();
      })();
      io.observe(el);
    }
    pdfHeads = Array.from({ length: pdf.numPages }, (_, i) => ({ level: 1, text: `Page ${i + 1}`, page: i + 1 }));
    repaint();
  }

  // ---- input: selections, highlight clicks, links, shortcuts ----
  const typing = t => t.closest?.('input,textarea,select,[contenteditable=""],[contenteditable="true"]');
  document.addEventListener('mouseup', () => setTimeout(() => {
    const sel = getSelection(), r = sel?.rangeCount && sel.getRangeAt(0);
    const root = r && !r.collapsed && (rootOf(r.startContainer) || rootOf(r.endContainer));
    if (!root) return;
    const t = textOf(root);  // a selection is clipped to one root (one PDF page)
    const from = root.contains(r.startContainer) ? offsetOf(t, r.startContainer, r.startOffset) : 0;
    const raw = t.text.slice(from, root.contains(r.endContainer) ? offsetOf(t, r.endContainer, r.endOffset) : t.text.length);
    const quote = raw.trim(), start = from + raw.length - raw.trimStart().length;
    if (quote.length < 2) return;
    selection = r.cloneRange();
    send({ type: 'select', start, end: start + quote.length, quote, view: VIEW, page: VIEW === 'pdf' ? +root.dataset.page : null, rect: rectOf([r]) });
  }), true);
  document.addEventListener('mousedown', () => send({ type: 'down' }), true);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') send({ type: 'down' });
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !typing(e.target)) { e.preventDefault(); send({ type: 'search' }); }
  }, true);
  document.addEventListener('paste', e => {  // only real pastes: page scripts can't fake their way into opening links
    const t = e.clipboardData.getData('text').trim();
    if (e.isTrusted && /^https?:\/\/\S+$/.test(t) && !typing(e.target)) { e.preventDefault(); send({ type: 'open', url: t }); }
  }, true);
  addEventListener('click', e => {
    if (!getSelection().isCollapsed) return;
    const hit = (b, pad = 1) => e.clientX >= b.left - pad && e.clientX <= b.right + pad && e.clientY >= b.top - pad && e.clientY <= b.bottom + pad;
    const id = [...ranges].reverse().find(([, rs]) => rs.some(r => [...r.getClientRects()].some(b => hit(b))))?.[0];
    if (id !== undefined) {
      e.preventDefault(); e.stopPropagation();
      tracked = id;
      return send({ type: 'click-ann', id, rect: rectOf(ranges.get(id)) });
    }
    const a = e.target.closest?.('a[href]');
    let u; try { u = a && new URL(a.href, document.baseURI); } catch {}
    if (!u || !/^https?:$/.test(u.protocol) || e.defaultPrevented || !e.isTrusted) return;
    e.preventDefault(); e.stopPropagation();
    const page = h => h.split('#')[0];
    if (u.hash && [page(document.baseURI), page(location.href)].includes(page(u.href)))  // in-page anchor
      return document.getElementById(decodeURIComponent(u.hash.slice(1)))?.scrollIntoView();
    if (u.origin === location.origin && REAL_ORIGIN) u = new URL(u.pathname + u.search + u.hash, REAL_ORIGIN);
    send({ type: 'link', href: u.href, newTab: e.metaKey || e.ctrlKey || e.shiftKey });
  }, true);

  // ---- commands from the app ----
  const commands = {
    anns: m => { ({ anns, cats, def } = m); repaint(); },
    track: m => { tracked = m.id; },
    'clear-selection': () => { getSelection().removeAllRanges(); selection = null; },
    goto: m => headEl(heads[m.heading])?.scrollIntoView({ block: 'start' }),
    focus: async m => {
      const a = anns.find(x => x.id === m.id);
      if (VIEW === 'pdf' && a?.page) await document.querySelector(`.page[data-page="${a.page}"]`)?.render();
      const rs = ranges.get(m.id);
      if (!rs) return send({ type: 'missing', id: m.id });
      rs[0].startContainer.parentElement.scrollIntoView({ block: 'center' });
      CSS.highlights.set('pi-flash', new Highlight(...rs));
      setTimeout(() => CSS.highlights.delete('pi-flash'), 1400);
      tracked = m.id;
      setTimeout(() => send({ type: 'click-ann', id: m.id, rect: rectOf(rs) }), 60);
    },
  };
  addEventListener('message', e => {
    if (e.source !== APP || !e.data?.pi) return;
    origin = e.origin;
    commands[e.data.type]?.(e.data);
  });

  let queued = 0;  // pages that render late or keep changing: re-anchor at most every 600ms
  const observer = new MutationObserver(ms => { if (!queued && !ms.every(m => m.target === style)) queued = setTimeout(() => { queued = 0; repaint(); }, 600); });
  function ready() {
    if (VIEW === 'pdf') renderPdf(); else observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    send({ type: 'ready', highlights: !!window.Highlight });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
  addEventListener('load', repaint);
})();
