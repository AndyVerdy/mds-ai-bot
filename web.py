"""
MDS AI Bot — Web UI + Embeddable Widget API (Flask).
- Full chat UI at /
- Embeddable widget JS at /widget.js
- API at /api/ask (CORS-enabled for embedding on any site)
- API at /api/suggestions (topics + popular searches)
"""

import os
from flask import Flask, render_template_string, request, jsonify, make_response
from flask_cors import CORS
from query import ask, track_search, get_popular_searches, extract_topics

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ============================================================
# Embeddable widget JS — drop <script src="...widget.js"> on any page
# ============================================================
WIDGET_JS = """
(function() {
  var API_URL = '{{API_URL}}';

  var style = document.createElement('style');
  style.textContent = `
    #mds-widget-toggle {
      position: fixed; bottom: 24px; right: 24px; z-index: 99999;
      width: 48px; height: 48px; border-radius: 50%;
      background: #18181b; border: none; cursor: pointer;
      box-shadow: 0 2px 12px rgba(0,0,0,0.15);
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.15s;
    }
    #mds-widget-toggle:hover { transform: scale(1.06); }
    #mds-widget-toggle svg { width: 22px; height: 22px; fill: white; }
    #mds-widget-panel {
      position: fixed; bottom: 84px; right: 24px; z-index: 99999;
      width: 380px; max-height: 540px; border-radius: 0.75rem;
      background: #fff; border: 1px solid #e4e4e7;
      box-shadow: 0 4px 24px rgba(0,0,0,0.12);
      display: none; flex-direction: column; overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #mds-widget-panel.open { display: flex; }
    #mds-widget-header {
      padding: 12px 14px; border-bottom: 1px solid #e4e4e7;
      display: flex; align-items: center; justify-content: space-between;
    }
    #mds-widget-header h3 {
      margin: 0; font-size: 13px; font-weight: 600; color: #09090b;
    }
    #mds-widget-header .badge {
      font-size: 10px; padding: 1px 6px; background: #f4f4f5;
      color: #71717a; border: 1px solid #e4e4e7; border-radius: 999px;
    }
    #mds-widget-close {
      background: none; border: none; color: #a1a1aa; cursor: pointer;
      font-size: 18px; padding: 0 4px; line-height: 1;
    }
    #mds-widget-close:hover { color: #09090b; }
    #mds-widget-messages {
      flex: 1; overflow-y: auto; padding: 12px; display: flex;
      flex-direction: column; gap: 10px; min-height: 200px; max-height: 380px;
    }
    .mds-msg {
      font-size: 13px; line-height: 1.55; max-width: 90%; word-wrap: break-word;
    }
    .mds-msg.user {
      padding: 8px 12px; background: #18181b; color: #fafafa;
      border-radius: 0.5rem; border-bottom-right-radius: 0.15rem;
      align-self: flex-end;
    }
    .mds-msg.bot {
      align-self: flex-start; color: #09090b;
    }
    .mds-msg.bot ul, .mds-msg.bot ol { margin: 3px 0 3px 14px; }
    .mds-msg.bot p { margin-bottom: 5px; }
    .mds-msg.bot p:last-child { margin-bottom: 0; }
    .mds-msg.bot strong { font-weight: 600; }
    .mds-src-card {
      margin-top: 6px; padding: 8px 10px; border: 1px solid #e4e4e7;
      border-radius: 0.375rem; background: #fafafa; font-size: 11px;
    }
    .mds-src-card .src-label { color: #a1a1aa; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; font-size: 10px; }
    .mds-src-item { display: flex; gap: 6px; align-items: baseline; padding: 2px 0; flex-wrap: wrap; }
    .mds-src-item .speaker { font-weight: 500; color: #18181b; }
    .mds-src-item .meta { color: #a1a1aa; font-size: 10px; }
    .mds-src-item .video-link {
      display: inline-flex; align-items: center; gap: 3px;
      font-size: 9px; font-weight: 500; color: #2563eb;
      text-decoration: none; padding: 1px 6px;
      border: 1px solid #bfdbfe; border-radius: 999px;
      background: #eff6ff;
    }
    .mds-src-item .video-link:hover { background: #dbeafe; color: #1d4ed8; }
    .mds-src-item .video-link svg { width: 9px; height: 9px; }
    .mds-src-item .speaker-link {
      font-weight: 500; color: #18181b; cursor: pointer;
      border-bottom: 1px dashed #d4d4d8;
    }
    .mds-src-item .speaker-link:hover { color: #2563eb; border-bottom-color: #2563eb; }
    .mds-disclaimer {
      display: flex; align-items: flex-start; gap: 4px;
      padding: 6px 8px; background: #fef2f2; border: 1px solid #fecaca;
      border-radius: 0.25rem; font-size: 10px; color: #991b1b; line-height: 1.4;
    }
    .mds-disclaimer .disc-icon { flex-shrink: 0; }
    .mds-conf-bar { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
    .mds-conf-track { width: 60px; height: 4px; background: #e4e4e7; border-radius: 2px; overflow: hidden; }
    .mds-conf-fill { height: 100%; border-radius: 2px; }
    .mds-conf-label { font-size: 10px; font-weight: 500; }
    .mds-typing { display: inline-flex; gap: 3px; padding: 4px 0; }
    .mds-typing span {
      width: 5px; height: 5px; background: #d4d4d8; border-radius: 50%;
      animation: mds-bounce 1.4s ease-in-out infinite;
    }
    .mds-typing span:nth-child(2) { animation-delay: 0.2s; }
    .mds-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes mds-bounce {
      0%,60%,100% { transform: translateY(0); }
      30% { transform: translateY(-3px); }
    }
    #mds-widget-input-area {
      padding: 10px 12px; border-top: 1px solid #e4e4e7;
      display: flex; gap: 6px;
    }
    #mds-widget-input {
      flex: 1; padding: 7px 10px; background: #fff;
      border: 1px solid #e4e4e7; border-radius: 0.375rem;
      color: #09090b; font-size: 13px; outline: none;
    }
    #mds-widget-input:focus { border-color: #a1a1aa; }
    #mds-widget-input::placeholder { color: #a1a1aa; }
    #mds-widget-send {
      padding: 7px 12px; background: #18181b; border: none;
      border-radius: 0.375rem; color: #fafafa; font-size: 13px;
      font-weight: 500; cursor: pointer;
    }
    #mds-widget-send:hover { background: #27272a; }
    #mds-widget-send:disabled { opacity: 0.4; cursor: not-allowed; }
    .mds-welcome {
      text-align: center; padding: 20px 12px; color: #71717a; font-size: 13px;
    }
    .mds-welcome h4 { color: #09090b; margin-bottom: 4px; font-size: 14px; font-weight: 600; }
    .mds-topics { display: flex; flex-wrap: wrap; gap: 5px; justify-content: center; margin-top: 10px; }
    .mds-topics button {
      background: #fff; border: 1px solid #e4e4e7; color: #52525b;
      padding: 5px 9px; border-radius: 999px; cursor: pointer; font-size: 11px;
    }
    .mds-topics button:hover { border-color: #a1a1aa; color: #09090b; }
    @media (max-width: 480px) {
      #mds-widget-panel { width: calc(100vw - 24px); right: 12px; bottom: 72px; }
    }
  `;
  document.head.appendChild(style);

  var panel = document.createElement('div');
  panel.id = 'mds-widget-panel';
  panel.innerHTML = `
    <div id="mds-widget-header">
      <div style="display:flex;align-items:center;gap:6px">
        <h3>MDS Knowledge Search</h3>
        <span class="badge">AI</span>
      </div>
      <button id="mds-widget-close">&times;</button>
    </div>
    <div id="mds-widget-messages">
      <div class="mds-welcome" id="mds-welcome">
        <h4>Search MDS Knowledge Base</h4>
        <p>Ask about talks, sessions &amp; presentations</p>
        <div class="mds-topics" id="mds-topics-container"></div>
      </div>
    </div>
    <div id="mds-widget-input-area">
      <input id="mds-widget-input" type="text" placeholder="Ask a question...">
      <button id="mds-widget-send">Search</button>
    </div>
  `;
  document.body.appendChild(panel);

  var toggle = document.createElement('button');
  toggle.id = 'mds-widget-toggle';
  toggle.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/><path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/></svg>';
  document.body.appendChild(toggle);

  // Load topic suggestions dynamically
  fetch(API_URL + '/api/suggestions').then(r=>r.json()).then(function(data) {
    var container = document.getElementById('mds-topics-container');
    if (!container) return;
    var items = (data.topics || []).slice(0, 6);
    items.forEach(function(t) {
      var btn = document.createElement('button');
      btn.textContent = t;
      btn.onclick = function() { input.value = 'Summarize the key insights and advice from MDS sessions about ' + t; send(); };
      container.appendChild(btn);
    });
  }).catch(function(){});

  var messages = document.getElementById('mds-widget-messages');
  var input = document.getElementById('mds-widget-input');
  var sendBtn = document.getElementById('mds-widget-send');
  var isOpen = false;

  window._mdsWidgetSummarize = function(name) {
    input.value = 'Summarize the full conversation and key takeaways from the MDS session: ' + name;
    send();
  };

  toggle.onclick = function() {
    isOpen = !isOpen;
    panel.classList.toggle('open', isOpen);
    if (isOpen) input.focus();
  };

  document.getElementById('mds-widget-close').onclick = function() {
    isOpen = false;
    panel.classList.remove('open');
  };

  function confColor(c) {
    if (c > 0.6) return '#22c55e';
    if (c > 0.35) return '#eab308';
    return '#ef4444';
  }
  function confLabel(c) {
    if (c > 0.6) return 'High relevance';
    if (c > 0.35) return 'Moderate relevance';
    if (c > 0.2) return 'Low relevance';
    return 'Weak match';
  }

  function addMsg(text, type, extra) {
    var w = document.getElementById('mds-welcome');
    if (w) {
      w.remove();
      var disc = document.createElement('div');
      disc.className = 'mds-disclaimer';
      disc.innerHTML = '<span class="disc-icon">⚠️</span><span>AI-generated summaries from MDS sessions. May be inaccurate. Not professional advice.</span>';
      messages.appendChild(disc);
    }
    var div = document.createElement('div');
    div.className = 'mds-msg ' + type;
    if (type === 'bot') {
      var html = text
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')
        .replace(/\\*(.+?)\\*/g,'<em>$1</em>')
        .replace(/`(.+?)`/g,'<code>$1</code>')
        .replace(/^[\\-\\*] (.+)$/gm,'<li>$1</li>')
        .replace(/^(\\d+)\\. (.+)$/gm,'<li>$2</li>');
      html = html.replace(/((<li>.*<\\/li>\\n?)+)/g,'<ul>$1</ul>');
      html = html.split('\\n\\n').map(function(p){
        p=p.trim(); if(!p)return '';
        if(p.startsWith('<'))return p;
        return '<p>'+p+'</p>';
      }).join('');
      div.innerHTML = html;

      if (extra && extra.sources && extra.sources.length > 0) {
        var c = extra.confidence||0;
        var color = confColor(c);
        var card = document.createElement('div');
        card.className = 'mds-src-card';
        var srcHtml = '<div class="src-label">Sources</div>';
        extra.sources.forEach(function(s) {
          var parts = [];
          if (s.event) parts.push(s.event);
          if (s.date) parts.push(s.date);
          var vLink = s.video_url ? '<a class="video-link" href="'+s.video_url+'" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>Watch</a>' : '';
          var safeName = (s.speaker||'Unknown').replace(/"/g, '&quot;');
          srcHtml += '<div class="mds-src-item"><span class="speaker-link" data-source="'+safeName+'" onclick="window._mdsWidgetSummarize(this.dataset.source)">'+(s.speaker||'Unknown')+'</span>'+(parts.length?'<span class="meta">'+parts.join(' · ')+'</span>':'')+vLink+'</div>';
        });
        srcHtml += '<div class="mds-conf-bar"><div class="mds-conf-track"><div class="mds-conf-fill" style="width:'+Math.round(c*100)+'%;background:'+color+'"></div></div><span class="mds-conf-label" style="color:'+color+'">'+confLabel(c)+'</span></div>';
        card.innerHTML = srcHtml;
        div.appendChild(card);
      }
    } else {
      div.textContent = text;
    }
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function addTyping() {
    var div = document.createElement('div');
    div.className = 'mds-msg bot';
    div.id = 'mds-typing';
    div.innerHTML = '<div class="mds-typing"><span></span><span></span><span></span></div>';
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function removeTyping() {
    var t = document.getElementById('mds-typing');
    if (t) t.remove();
  }

  async function send() {
    var q = input.value.trim();
    if (!q) return;
    input.value = '';
    sendBtn.disabled = true;
    addMsg(q, 'user');
    addTyping();
    try {
      var res = await fetch(API_URL + '/api/ask', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({question: q})
      });
      var data = await res.json();
      removeTyping();
      if (data.error) { addMsg('Error: ' + data.error, 'bot'); }
      else { addMsg(data.answer, 'bot', {confidence: data.confidence, sources: data.sources}); }
    } catch(e) {
      removeTyping();
      addMsg('Could not connect.', 'bot');
    }
    sendBtn.disabled = false;
    input.focus();
  }

  sendBtn.onclick = send;
  input.onkeydown = function(e) { if(e.key==='Enter') send(); };
})();
"""

# ============================================================
# Full-page chat UI
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MDS Knowledge Search</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
            background: #ffffff;
            color: #09090b;
            min-height: 100vh;
        }

        /* === HEADER (hidden on landing, shown in chat) === */
        header {
            border-bottom: 1px solid #e4e4e7;
            padding: 0.875rem 1.5rem;
            display: none;
            align-items: center;
            gap: 0.625rem;
        }
        header h1 { font-size: 0.95rem; font-weight: 600; letter-spacing: -0.01em; cursor: pointer; }
        header .badge {
            font-size: 0.65rem; padding: 0.125rem 0.5rem;
            background: #f4f4f5; color: #71717a; border: 1px solid #e4e4e7;
            border-radius: 999px; font-weight: 500;
        }
        body.chat-mode header { display: flex; }

        /* === LANDING PAGE — centered search === */
        .landing {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 2rem;
            text-align: center;
        }
        body.chat-mode .landing { display: none; }

        .landing h1 {
            font-size: 1.75rem;
            font-weight: 600;
            letter-spacing: -0.03em;
            margin-bottom: 0.375rem;
        }
        .landing .subtitle {
            color: #71717a;
            font-size: 0.925rem;
            margin-bottom: 2rem;
        }
        .landing .search-box {
            width: 100%;
            max-width: 560px;
            display: flex;
            gap: 0.5rem;
            margin-bottom: 2rem;
        }
        .landing .search-box input {
            flex: 1;
            padding: 0.75rem 1rem;
            border: 1px solid #e4e4e7;
            border-radius: 0.5rem;
            font-size: 0.95rem;
            outline: none;
            transition: border-color 0.15s;
        }
        .landing .search-box input:focus { border-color: #a1a1aa; }
        .landing .search-box input::placeholder { color: #a1a1aa; }
        .landing .search-box button {
            padding: 0.75rem 1.5rem;
            background: #18181b;
            border: none;
            border-radius: 0.5rem;
            color: #fafafa;
            font-size: 0.925rem;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.15s;
        }
        .landing .search-box button:hover { background: #27272a; }

        .suggestions-section {
            max-width: 560px;
            width: 100%;
        }
        .suggestions-label {
            font-size: 0.7rem;
            color: #a1a1aa;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 500;
            margin-bottom: 0.625rem;
        }
        .topic-pills {
            display: flex;
            flex-wrap: wrap;
            gap: 0.375rem;
            justify-content: center;
            margin-bottom: 1.5rem;
        }
        .topic-pill {
            background: #fff;
            border: 1px solid #e4e4e7;
            color: #52525b;
            padding: 0.375rem 0.875rem;
            border-radius: 999px;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.15s;
        }
        .topic-pill:hover {
            border-color: #a1a1aa;
            color: #09090b;
            background: #fafafa;
        }
        .loading-topics {
            color: #d4d4d8;
            font-size: 0.8rem;
            padding: 0.5rem;
        }
        .disclaimer {
            display: flex;
            align-items: flex-start;
            gap: 0.5rem;
            max-width: 560px;
            padding: 0.625rem 0.875rem;
            background: #fef2f2;
            border: 1px solid #fecaca;
            border-radius: 0.5rem;
            font-size: 0.75rem;
            color: #991b1b;
            line-height: 1.5;
            margin-top: 1.5rem;
            text-align: left;
        }
        .disclaimer .disc-icon { flex-shrink: 0; font-size: 0.875rem; }

        /* === CHAT MODE === */
        .chat-container {
            display: none;
            flex: 1; max-width: 720px; width: 100%;
            margin: 0 auto; padding: 1.5rem;
            flex-direction: column; gap: 1.25rem;
            overflow-y: auto;
        }
        body.chat-mode .chat-container { display: flex; }

        .message { max-width: 90%; line-height: 1.65; font-size: 0.9rem; }
        .message.user {
            align-self: flex-end; background: #18181b; color: #fafafa;
            padding: 0.625rem 0.875rem; border-radius: 0.75rem;
            border-bottom-right-radius: 0.25rem;
        }
        .message.bot { align-self: flex-start; padding: 0; }
        .message.bot .answer-text { line-height: 1.7; }
        .message.bot ul, .message.bot ol { margin: 0.375rem 0 0.375rem 1.25rem; }
        .message.bot li { margin-bottom: 0.25rem; }
        .message.bot p { margin-bottom: 0.5rem; }
        .message.bot p:last-child { margin-bottom: 0; }
        .message.bot code {
            background: #f4f4f5; padding: 0.125rem 0.375rem;
            border-radius: 0.25rem; font-size: 0.825rem;
        }
        .message.bot strong { font-weight: 600; }

        .source-card {
            margin-top: 0.875rem; padding: 0.75rem;
            border: 1px solid #e4e4e7; border-radius: 0.5rem;
            background: #fafafa;
        }
        .source-card .source-header {
            display: flex; align-items: center; gap: 0.375rem;
            margin-bottom: 0.5rem;
        }
        .source-card .source-header svg {
            width: 14px; height: 14px; color: #71717a; flex-shrink: 0;
        }
        .source-card .source-header span {
            font-size: 0.75rem; font-weight: 500; color: #71717a;
            text-transform: uppercase; letter-spacing: 0.05em;
        }
        .source-item {
            display: flex; align-items: baseline; gap: 0.5rem;
            padding: 0.25rem 0; font-size: 0.8rem; flex-wrap: wrap;
        }
        .source-item .speaker { font-weight: 500; color: #18181b; }
        .source-item .meta { color: #a1a1aa; font-size: 0.75rem; }
        .source-item .video-link {
            display: inline-flex; align-items: center; gap: 0.25rem;
            font-size: 0.7rem; font-weight: 500; color: #2563eb;
            text-decoration: none; padding: 0.125rem 0.5rem;
            border: 1px solid #bfdbfe; border-radius: 999px;
            background: #eff6ff; transition: all 0.15s;
        }
        .source-item .video-link:hover {
            background: #dbeafe; border-color: #93c5fd; color: #1d4ed8;
        }
        .source-item .video-link svg {
            width: 11px; height: 11px; flex-shrink: 0;
        }
        .source-item .speaker-link {
            font-weight: 500; color: #18181b; cursor: pointer;
            border-bottom: 1px dashed #d4d4d8;
            transition: all 0.15s;
        }
        .source-item .speaker-link:hover {
            color: #2563eb; border-bottom-color: #2563eb;
        }

        .chat-disclaimer {
            display: flex; align-items: flex-start; gap: 0.5rem;
            padding: 0.625rem 0.875rem;
            background: #fef2f2; border: 1px solid #fecaca;
            border-radius: 0.5rem; font-size: 0.75rem;
            color: #991b1b; line-height: 1.5;
        }
        .chat-disclaimer .disc-icon { flex-shrink: 0; font-size: 0.875rem; }

        /* === COLORFUL RELEVANCE BAR === */
        .confidence-bar {
            margin-top: 0.625rem; display: flex; align-items: center; gap: 0.5rem;
        }
        .confidence-bar .bar-track {
            flex: 0 0 80px; height: 6px; background: #f4f4f5;
            border-radius: 3px; overflow: hidden;
        }
        .confidence-bar .bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
        .confidence-bar .label {
            font-size: 0.725rem; font-weight: 500; white-space: nowrap;
        }

        /* === INPUT AREA (hidden on landing, shown in chat) === */
        .input-area {
            display: none;
            border-top: 1px solid #e4e4e7;
            padding: 0.875rem 1.5rem; background: #fff;
        }
        body.chat-mode .input-area { display: block; }
        .input-wrapper {
            max-width: 720px; margin: 0 auto;
            display: flex; gap: 0.5rem;
        }
        .input-wrapper input {
            flex: 1; padding: 0.5rem 0.75rem;
            border: 1px solid #e4e4e7; border-radius: 0.375rem;
            font-size: 0.875rem; outline: none;
            transition: border-color 0.15s;
            background: #fff; color: #09090b;
        }
        .input-wrapper input:focus { border-color: #a1a1aa; }
        .input-wrapper input::placeholder { color: #a1a1aa; }
        .input-wrapper button {
            padding: 0.5rem 1rem; background: #18181b;
            border: none; border-radius: 0.375rem;
            color: #fafafa; font-size: 0.875rem; font-weight: 500;
            cursor: pointer; transition: background 0.15s;
        }
        .input-wrapper button:hover { background: #27272a; }
        .input-wrapper button:disabled { opacity: 0.4; cursor: not-allowed; }

        .typing { display: inline-flex; gap: 4px; padding: 0.5rem 0; }
        .typing span {
            width: 6px; height: 6px; background: #d4d4d8;
            border-radius: 50%; animation: bounce 1.4s ease-in-out infinite;
        }
        .typing span:nth-child(2) { animation-delay: 0.2s; }
        .typing span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce {
            0%, 60%, 100% { transform: translateY(0); }
            30% { transform: translateY(-4px); }
        }

        @media (max-width: 640px) {
            .landing { padding: 1.5rem; }
            .landing h1 { font-size: 1.375rem; }
            .chat-container { padding: 1rem; }
            .input-area { padding: 0.75rem 1rem; }
        }
    </style>
</head>
<body>
    <header>
        <h1 onclick="resetToLanding()">MDS Knowledge Search</h1>
        <span class="badge">AI</span>
    </header>

    <!-- LANDING: centered search -->
    <div class="landing" id="landing">
        <h1>MDS Knowledge Search</h1>
        <p class="subtitle">Search mastermind sessions, talks & presentations</p>
        <div class="search-box">
            <input type="text" id="landingInput" placeholder="Ask anything about MDS content..."
                   onkeydown="if(event.key==='Enter')searchFromLanding()">
            <button onclick="searchFromLanding()">Search</button>
        </div>
        <div class="suggestions-section">
            <div id="topicsArea">
                <p class="suggestions-label">Topics</p>
                <div class="topic-pills" id="topicPills">
                    <span class="loading-topics">Loading topics...</span>
                </div>
            </div>
            <div id="popularArea" style="display:none">
                <p class="suggestions-label">Popular searches</p>
                <div class="topic-pills" id="popularPills"></div>
            </div>
        </div>
        <div class="disclaimer"><span class="disc-icon">⚠️</span><span>This tool provides AI-generated summaries from recorded MDS sessions. Responses may be incomplete or inaccurate. This is not professional, legal, or financial advice. Always verify information and use your own judgment.</span></div>
    </div>

    <!-- CHAT: appears after first search -->
    <div class="chat-container" id="chat"></div>

    <div class="input-area">
        <div class="input-wrapper">
            <input type="text" id="questionInput" placeholder="Ask a follow-up question..."
                   onkeydown="if(event.key==='Enter')sendQuestion()">
            <button id="sendBtn" onclick="sendQuestion()">Search</button>
        </div>
    </div>

    <script>
        const chat = document.getElementById('chat');
        const landingInput = document.getElementById('landingInput');
        const chatInput = document.getElementById('questionInput');
        const sendBtn = document.getElementById('sendBtn');
        let inChatMode = false;

        // Load suggestions on page load
        fetch('/api/suggestions')
            .then(r => r.json())
            .then(data => {
                const topicPills = document.getElementById('topicPills');
                topicPills.innerHTML = '';
                (data.topics || []).forEach(t => {
                    const btn = document.createElement('button');
                    btn.className = 'topic-pill';
                    btn.textContent = t;
                    btn.onclick = () => { landingInput.value = 'Summarize the key insights and advice from MDS sessions about ' + t; searchFromLanding(); };
                    topicPills.appendChild(btn);
                });
                if ((data.topics || []).length === 0) {
                    topicPills.innerHTML = '<span class="loading-topics">No topics yet</span>';
                }

                const popular = data.popular || [];
                if (popular.length > 0) {
                    document.getElementById('popularArea').style.display = '';
                    const popularPills = document.getElementById('popularPills');
                    popular.forEach(q => {
                        const btn = document.createElement('button');
                        btn.className = 'topic-pill';
                        btn.textContent = q;
                        btn.onclick = () => { landingInput.value = q; searchFromLanding(); };
                        popularPills.appendChild(btn);
                    });
                }
            })
            .catch(() => {
                document.getElementById('topicPills').innerHTML = '';
            });

        function switchToChatMode() {
            if (inChatMode) return;
            inChatMode = true;
            document.body.classList.add('chat-mode');
            const disc = document.createElement('div');
            disc.className = 'chat-disclaimer';
            disc.innerHTML = '<span class="disc-icon">⚠️</span><span>This tool provides AI-generated summaries from recorded MDS sessions. Responses may be incomplete or inaccurate. This is not professional, legal, or financial advice. Always verify information and use your own judgment.</span>';
            chat.appendChild(disc);
            chatInput.focus();
        }

        function resetToLanding() {
            inChatMode = false;
            document.body.classList.remove('chat-mode');
            chat.innerHTML = '';
            landingInput.value = '';
            landingInput.focus();
        }

        function searchFromLanding() {
            const q = landingInput.value.trim();
            if (!q) return;
            switchToChatMode();
            chatInput.value = '';
            doSearch(q);
        }

        function confColor(c) {
            if (c > 0.6) return '#22c55e';
            if (c > 0.35) return '#eab308';
            return '#ef4444';
        }

        function confLabel(c) {
            if (c > 0.6) return 'High relevance';
            if (c > 0.35) return 'Moderate relevance';
            if (c > 0.2) return 'Low relevance';
            return 'Weak match';
        }

        function formatSourceItem(src) {
            const speaker = src.speaker || 'Unknown';
            const parts = [];
            if (src.event) parts.push(src.event);
            if (src.date) parts.push(src.date);
            if (src.topic) parts.push(src.topic);
            const meta = parts.length > 0 ? parts.join(' &middot; ') : '';
            const videoLink = src.video_url
                ? `<a class="video-link" href="${src.video_url}" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>Watch</a>`
                : '';
            const safeName = speaker.replace(/"/g, '&quot;');
            return `<div class="source-item"><span class="speaker-link" data-source="${safeName}" onclick="summarizeSource(this.dataset.source)">${speaker}</span>${meta ? `<span class="meta">${meta}</span>` : ''}${videoLink}</div>`;
        }

        function addMessage(text, type, extra) {
            const div = document.createElement('div');
            div.className = `message ${type}`;

            if (type === 'bot') {
                let html = text
                    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                    .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
                    .replace(/\\*(.+?)\\*/g, '<em>$1</em>')
                    .replace(/`(.+?)`/g, '<code>$1</code>')
                    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
                    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
                    .replace(/^[\\-\\*] (.+)$/gm, '<li>$1</li>')
                    .replace(/^(\\d+)\\. (.+)$/gm, '<li>$2</li>');
                html = html.replace(/((<li>.*<\\/li>\\n?)+)/g, '<ul>$1</ul>');
                html = html.split('\\n\\n').map(p => {
                    p = p.trim(); if (!p) return '';
                    if (p.startsWith('<h') || p.startsWith('<ul') || p.startsWith('<ol')) return p;
                    return `<p>${p}</p>`;
                }).join('');

                div.innerHTML = `<div class="answer-text">${html}</div>`;

                if (extra) {
                    const conf = extra.confidence || 0;
                    const color = confColor(conf);
                    const pct = Math.round(conf * 100);

                    if (extra.sources && extra.sources.length > 0) {
                        const srcCard = document.createElement('div');
                        srcCard.className = 'source-card';
                        srcCard.innerHTML = `
                            <div class="source-header">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                                <span>Sources</span>
                            </div>
                            ${extra.sources.map(formatSourceItem).join('')}
                            <div class="confidence-bar">
                                <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
                                <span class="label" style="color:${color}">${confLabel(conf)}</span>
                            </div>
                        `;
                        div.appendChild(srcCard);
                    }
                }
            } else {
                div.textContent = text;
            }
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
            return div;
        }

        function addTyping() {
            const div = document.createElement('div');
            div.className = 'message bot';
            div.id = 'typing';
            div.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
        }

        function removeTyping() {
            const t = document.getElementById('typing');
            if (t) t.remove();
        }

        async function doSearch(question) {
            sendBtn.disabled = true;
            addMessage(question, 'user');
            addTyping();
            try {
                const res = await fetch('/api/ask', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question }),
                });
                const data = await res.json();
                removeTyping();
                if (data.error) { addMessage('Error: ' + data.error, 'bot'); }
                else { addMessage(data.answer, 'bot', { confidence: data.confidence, sources: data.sources }); }
            } catch (err) {
                removeTyping();
                addMessage('Failed to connect to the server.', 'bot');
            }
            sendBtn.disabled = false;
            chatInput.focus();
        }

        function sendQuestion() {
            const question = chatInput.value.trim();
            if (!question) return;
            chatInput.value = '';
            doSearch(question);
        }

        function summarizeSource(name) {
            const query = 'Summarize the full conversation and key takeaways from the MDS session: ' + name;
            chatInput.value = '';
            doSearch(query);
        }

        landingInput.focus();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/widget.js")
def widget_js():
    """Serve the embeddable widget JavaScript."""
    api_url = request.host_url.rstrip("/")
    js = WIDGET_JS.replace("{{API_URL}}", api_url)
    resp = make_response(js)
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json()
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    # Track the search query
    track_search(question)

    result = ask(question)
    return jsonify({
        "answer": result["answer"],
        "sources": result["sources"],
        "confidence": result["confidence"],
        "chunks_used": result["chunks_used"],
    })


@app.route("/api/suggestions")
def api_suggestions():
    """Return topic suggestions and popular searches."""
    topics = extract_topics()
    popular = get_popular_searches(limit=6)
    return jsonify({
        "topics": topics,
        "popular": popular,
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
