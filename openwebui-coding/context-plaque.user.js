// ==UserScript==
// @name         Open WebUI — Context Plaque
// @namespace    bench.openwebui
// @version      2.2.0
// @description  Slim context-window meter pinned above the message box. Reads real prompt/completion counts from the API, the on-screen stats line, or the DOM.
// @match        http://192.168.88.202:8775/*
// @match        *://*/*
// @include      /^https?:\/\/[^\/]*hardpro-llm[^\/]*\//
// @run-at       document-start
// @grant        GM_info
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const CFG = {
    fallbackWindow: 32768,   // used when the model window can't be detected
    pollMs: 1200,
    warnPct: 70,
    dangerPct: 90,
    debug: true              // console lines with [plaque] prefix
  };

  // Substring -> context window. First match wins. Edit freely.
  const WINDOWS = {
    'gpt-5': 400000, 'gpt-4.1': 1047576, 'gpt-4o': 128000, 'gpt-4-turbo': 128000,
    'o3': 200000, 'o4-mini': 200000, 'claude': 200000,
    'gemini': 1048576, 'deepseek': 131072, 'glm': 200000, 'kimi': 262144,
    'qwen3-coder': 262144, 'qwen2.5-coder': 32768, 'qwen3': 131072, 'qwen': 32768,
    'huihui': 32768, 'abliterated': 32768,
    'llama3.3': 131072, 'llama3.2': 131072, 'llama3.1': 131072,
    'codestral': 32768, 'devstral': 131072, 'mistral': 32768,
    'phi4': 16384, 'gemma3': 131072
  };

  const log = function () {
    if (!CFG.debug) return;
    const a = ['[plaque]'].concat([].slice.call(arguments));
    console.log.apply(console, a);
  };

  const CSS = `
    :host { all: initial; display: block; }
    .wrap {
      display: flex; align-items: center; gap: 10px;
      padding: 0 2px 6px; font: 500 11px/1.2 ui-sans-serif, system-ui, sans-serif;
      color: #8b8b8f; user-select: none; pointer-events: none;
    }
    .num { font-variant-numeric: tabular-nums; letter-spacing: .02em; white-space: nowrap; }
    .num b { color: #d4d4d8; font-weight: 600; }
    .track {
      flex: 1 1 auto; height: 4px; border-radius: 999px;
      background: rgba(255,255,255,.09); overflow: hidden; position: relative;
    }
    .fill {
      height: 100%; width: 0%; border-radius: 999px;
      background: #6b7280; transition: width .25s ease, background .25s ease;
    }
    .fill.warn { background: #d97706; }
    .fill.danger { background: #dc2626; }
    .pct { min-width: 34px; text-align: right; }
    @media (prefers-color-scheme: light) {
      .num b { color: #18181b; } .wrap { color: #71717a; }
      .track { background: rgba(0,0,0,.1); }
    }
  `;

  // ---------- numbers ----------

  function tokenEstimate(text) {
    return Math.max(1, Math.round((text || '').length / 3.6));
  }

  function fmt(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

  function windowFor(model) {
    const m = (model || '').toLowerCase();
    for (const key in WINDOWS) if (m.includes(key)) return WINDOWS[key];
    return null;
  }

  function resolveWindow(model, hintTokens) {
    return windowFor(model) || (hintTokens ? Math.round(hintTokens / 0.6) : CFG.fallbackWindow);
  }

  // ---------- page plumbing ----------

  function basePath() {
    const m = location.pathname.match(/^(.*?)\/c\/[^/?#]+/);
    return m ? m[1] : '';
  }

  function apiUrl(path) {
    return basePath() + path;
  }

  function chatId() {
    const m = location.pathname.match(/\/c\/([^/?#]+)/);
    return m ? m[1] : null;
  }

  function authHeaders() {
    const out = { 'Content-Type': 'application/json' };
    try {
      const tok = localStorage.getItem('token');
      if (tok) out.Authorization = 'Bearer ' + tok;
    } catch (e) { /* private mode */ }
    return out;
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // The fork under HARD-PRO does not ship #chat-input, so try everything.
  const INPUT_SELECTORS = [
    '#chat-input',
    'textarea[placeholder]',
    'textarea',
    '[contenteditable="true"]',
    '[placeholder*="Отправить"]',
    '[placeholder*="Send"]',
    '[placeholder*="Сообщение"]',
    '[placeholder*="message"]',
    'form textarea'
  ];

  function findInput() {
    for (let i = 0; i < INPUT_SELECTORS.length; i++) {
      const els = document.querySelectorAll(INPUT_SELECTORS[i]);
      for (let j = els.length - 1; j >= 0; j--) {
        if (isVisible(els[j])) return els[j];
      }
    }
    return null;
  }

  function findAnchor() {
    const input = findInput();
    if (!input) return null;
    let node = input;
    for (let i = 0; i < 8 && node && node !== document.body; i++) {
      const r = node.getBoundingClientRect();
      if (r.width > 420 && node.querySelector('button')) return node;
      node = node.parentElement;
    }
    return input.parentElement;
  }

  function modelName() {
    try {
      const sel = document.querySelector('select#model');
      if (sel && sel.value) return sel.value;
      const btn = document.querySelector('#model-selector-0-button, [aria-label="Model"]');
      if (btn) return btn.innerText.trim();
      const all = document.querySelectorAll('select');
      for (let i = all.length - 1; i >= 0; i--) {
        if (all[i].value && /[/:\w-]{4,}/.test(all[i].value)) return all[i].value;
      }
    } catch (e) { /* not mounted */ }
    return '';
  }

  // The HARD-PRO build prints "prompt 8016 · completion 178 · total 8194".
  // Take the newest one on screen when the API is not available.
  function numbersFromUI() {
    const text = document.body.innerText || '';
    const re = /total\s*[:\s]\s*(\d+)/gi;
    let m, last = null;
    while ((m = re.exec(text)) !== null) last = m;
    if (!last) return null;
    const tail = text.slice(Math.max(0, last.index - 120), last.index + 40);
    const p = /prompt\s*[:\s]\s*(\d+)/i.exec(tail);
    const c = /completion\s*[:\s]\s*(\d+)/i.exec(tail);
    return {
      prompt: p ? parseInt(p[1], 10) : parseInt(last[1], 10),
      completion: c ? parseInt(c[1], 10) : 0,
      total: parseInt(last[1], 10)
    };
  }

  async function readUsage() {
    const ta = findInput();
    const typed = ta ? (ta.value || ta.innerText || '') : '';
    const typedTokens = typed ? tokenEstimate(typed) : 0;
    const model = modelName();
    const id = chatId();

    if (id) {
      try {
        const r = await fetch(apiUrl('/api/v1/chats/' + id), { headers: authHeaders() });
        if (r.ok) {
          const j = await r.json();
          const cu = (j && j.context_usage) || (j && j.chat && j.chat.context_usage);
          if (cu && (cu.tokens || cu.estimated_tokens)) {
            return {
              used: (cu.estimated_tokens || cu.tokens) + typedTokens,
              window: resolveWindow(model, cu.threshold),
              source: 'api'
            };
          }
          log('api ok but no context_usage; falling back');
        } else {
          log('api status', r.status);
        }
      } catch (e) {
        log('api failed', e.message);
      }
    }

    const ui = numbersFromUI();
    if (ui) {
      return { used: ui.prompt + typedTokens, window: resolveWindow(model, 0), source: 'ui' };
    }

    let chars = 0;
    document.querySelectorAll('[data-message-author-role], .prose, .markdown').forEach(function (el) {
      chars += (el.innerText || '').length;
    });
    return { used: Math.round(chars / 3.6) + typedTokens, window: resolveWindow(model, 0), source: 'dom' };
  }

  // ---------- widget ----------

  const host = document.createElement('div');
  host.id = 'bench-context-plaque';
  host.style.cssText = 'display:block;position:relative;z-index:20;pointer-events:none;margin:0 auto 4px;width:100%;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML =
    '<style>' + CSS + '</style>' +
    '<div class="wrap">' +
    '  <span class="num" id="num"><b>0</b> / 0</span>' +
    '  <div class="track"><div class="fill" id="fill"></div></div>' +
    '  <span class="num pct" id="pct">0%</span>' +
    '</div>';

  const elNum = root.getElementById('num');
  const elPct = root.getElementById('pct');
  const elFill = root.getElementById('fill');

  let mountedOn = null;

  function mount() {
    const anchor = findAnchor();
    if (!anchor || !anchor.parentElement) {
      if (CFG.debug) log('no anchor; input found =', !!findInput());
      return false;
    }
    if (mountedOn === anchor && host.isConnected) return true;
    anchor.parentElement.insertBefore(host, anchor);
    mountedOn = anchor;
    log('mounted above', anchor.tagName.toLowerCase(), anchor.className.slice(0, 40));
    return true;
  }

  function render(u) {
    const pct = u.window ? Math.min(999, Math.round((u.used / u.window) * 100)) : 0;
    elNum.innerHTML = '<b>' + fmt(u.used) + '</b> / ' + fmt(u.window);
    elPct.textContent = pct + '%';
    elFill.style.width = Math.min(100, pct) + '%';
    elFill.className = 'fill' + (pct >= CFG.dangerPct ? ' danger' : pct >= CFG.warnPct ? ' warn' : '');
    host.title = {
      api: 'Open WebUI API: ' + u.used + ' of ' + u.window + ' tokens',
      ui: 'Read from the on-screen stats line: ' + u.used + ' of ' + u.window,
      dom: 'Estimated from rendered text (no counter found)'
    }[u.source];
  }

  let busy = false;
  let alive = true;

  async function tick() {
    if (busy || !alive) return;
    busy = true;
    try {
      if (mount()) render(await readUsage());
    } catch (e) {
      log('tick error', e.message);
    } finally {
      busy = false;
    }
  }

  async function looksLikeOpenWebUI() {
    try {
      const r = await fetch(apiUrl('/api/config'), { headers: authHeaders() });
      if (!r.ok) return false;
      const j = await r.json();
      return !!(j && (j.version !== undefined || j.features !== undefined));
    } catch (e) {
      return false;
    }
  }

  let timer = null;

  function onBody(fn) {
    if (document.body) return fn();
    const o = new MutationObserver(function () {
      if (document.body) { o.disconnect(); fn(); }
    });
    o.observe(document.documentElement, { childList: true });
  }

  onBody(function () {
    log('loaded on', location.origin, 'path', location.pathname);

    let pending = null;
    new MutationObserver(function () {
      if (pending) return;
      pending = setTimeout(function () { pending = null; tick(); }, 400);
    }).observe(document.body, { childList: true, subtree: true });

    timer = setInterval(tick, CFG.pollMs);

    (async function gate() {
      for (let i = 0; i < 40 && alive; i++) {
        if (findInput()) { log('input found at attempt', i); tick(); return; }
        if (i === 20 && !(await looksLikeOpenWebUI())) {
          alive = false;
          clearInterval(timer);
          log('not Open WebUI, shutting down');
          return;
        }
        await new Promise(function (r) { setTimeout(r, 500); });
      }
      log('gave up: no input after 20s');
    })();

    tick();
  });

  try {
    window.__benchPlaque = { tick: tick, read: readUsage, mount: mount, host: host };
  } catch (e) { /* sandboxed global */ }
})();
