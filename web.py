"""
MDS AI Bot — Web UI + Embeddable Widget API (Flask).
- Full chat UI at /
- Embeddable widget JS at /widget.js
- API at /api/ask (auth required)
- API at /api/suggestions (auth required)
- API at /api/digests (auth required)
- API at /api/auth/* (login flow)
"""

import os
import re
import time
import json
import functools
from typing import Optional
import requests
from flask import Flask, render_template_string, request, jsonify, make_response
from flask_cors import CORS
from query import ask, summarize_source, track_search, get_popular_searches, extract_topics
import auth as auth_module
import email_sender
import videos as videos_module
import transcripts as transcripts_module
import mux_webhook as mux_webhook_module

VERSION = "1.5.0"

# Airtable constants — base shared with mds-digest-web project.
AIRTABLE_BASE_ID = "appT9TVZWhv7io4CN"
AIRTABLE_DIGESTS_TABLE = "Summaries"
AIRTABLE_MEMBERS_TABLE = "Members"
# Devices table holds APNs tokens per signed-in iOS user. Created 2026-05-06
# for build (27) push notifications. Schema documented in apns.py / devices.py.
AIRTABLE_DEVICES_TABLE = "iOS Devices"


# ============================================================
# Push-name -> full-name enrichment
# ============================================================
# Digest tl_dr / summary text comes from n8n's Claude pipeline, which only
# saw WhatsApp push names ("Brandon", "Jonathan") — so the prose ends up
# saying "Brandon's affiliate program" instead of "Brandon Himmel's affiliate
# program." We post-process the text on the read side using the AT Members
# table to substitute first names with full names where the match is
# unambiguous.
#
# Ambiguity rule: a first name only enriches if exactly ONE member in the
# table has that first name with a last name. "Brian" with five Brians stays
# as "Brian." Names without a space (push name = name in AT) are skipped.

_members_index_cache: dict[str, str] = {}
_members_index_cache_at: float = 0.0
_MEMBERS_INDEX_TTL_S = 3600.0  # 1 hour


def _members_first_name_index() -> dict[str, str]:
    """Return {first_name_lower: full_name} for members where exactly one
    person has that first name AND the AT name field has a space (so we can
    extract the last-name component). Cached for 1 hour."""
    global _members_index_cache, _members_index_cache_at
    now = time.time()
    if _members_index_cache and (now - _members_index_cache_at) < _MEMBERS_INDEX_TTL_S:
        return _members_index_cache

    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        return {}

    members: list[dict] = []
    offset: Optional[str] = None
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_MEMBERS_TABLE}"
    headers = {"Authorization": f"Bearer {pat}"}
    while True:
        params = {"pageSize": 100, "fields[]": "name"}
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(url, headers=headers, params=params, timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            break
        body = r.json()
        members.extend(body.get("records", []))
        offset = body.get("offset")
        if not offset:
            break

    # Bucket by first-name (lowercased), discard ambiguous keys.
    by_first: dict[str, set[str]] = {}
    for rec in members:
        name = ((rec.get("fields", {}) or {}).get("name") or "").strip()
        if not name or " " not in name:
            continue
        first = name.split()[0].strip()
        if len(first) < 2:
            continue
        by_first.setdefault(first.lower(), set()).add(name)

    index: dict[str, str] = {}
    for first, fulls in by_first.items():
        if len(fulls) == 1:
            index[first] = next(iter(fulls))

    _members_index_cache = index
    _members_index_cache_at = now
    return index


def _format_links_shared(text: str) -> str:
    """Make the n8n-generated `links_shared` field readable.

    n8n's Claude pipeline outputs entries in the format
        Title1 -- URL1Title2 -- URL2Title3 -- URL3
    with no whitespace or newline between each URL and the start of the
    next title. iOS renders this as a single wall of text where the URLs
    accidentally absorb the leading words of the next title (Andy's
    feedback: "shared links are messy, impossible to read").

    We detect each URL and inject a paragraph break right after it. The
    boundary is "URL → CapitalLetter+lowercase" or "URL → whitespace".
    URLs with mixed-case paths can fool this heuristic but the data here
    is overwhelmingly x.com / lowercase-path URLs, so it's reliable in
    practice.
    """
    if not text or not text.strip():
        return ""
    pattern = re.compile(r'(https?://[^\s]+?)(?=[A-Z][a-z]|\s|$)')
    out = pattern.sub(r'\1\n\n', text)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


def _enrich_full_names(text: str) -> str:
    """Replace standalone first names in the given text with their full
    names (Members table lookup, unambiguous matches only). Preserves
    possessive 's. Skips occurrences already followed by the last name
    so 'Brandon Himmel' stays as-is rather than becoming 'Brandon Himmel
    Himmel'.
    """
    if not text:
        return text
    index = _members_first_name_index()
    if not index:
        return text
    out = text
    for first_lower, full in index.items():
        # Last-name first word, used in negative lookahead to skip cases
        # where the full name is already spelled out.
        try:
            last_first_word = full.split(maxsplit=1)[1].split()[0]
        except IndexError:
            continue
        # Match the first name as a whole word, NOT immediately followed
        # by whitespace + the last-name word. Case-insensitive on the
        # match but we want the substitution to use the canonical casing
        # from the Members table.
        pattern = re.compile(
            rf"\b{re.escape(first_lower)}\b(?!\s+{re.escape(last_first_word)})",
            flags=re.IGNORECASE,
        )
        # Substitute with the full name, preserving any character that
        # might follow (e.g. apostrophe-s for possessive). Just replace
        # the matched first name itself.
        out = pattern.sub(full, out)
    return out

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


# ============================================================
# Manual WhatsApp re-ingestion endpoint (admin-only).
#
# Earlier we ran this at module import in a background thread, which appears
# to have caused 502s on the live service (likely SQLite contention between
# the gunicorn worker and the ingest thread, or memory pressure). Now it's
# a manual admin-triggered route — safer and explicit.
# ============================================================

def _trigger_whatsapp_ingest(force: bool = False) -> dict:
    from query import get_vectorstore
    from ingest import ingest_whatsapp
    vs = get_vectorstore()
    collection = vs._collection
    if not force:
        existing = collection.get(where={"type": "whatsapp"}, limit=1, include=[])
        if existing and existing.get("ids"):
            return {
                "skipped": True,
                "reason": "WhatsApp chunks already in index. Pass ?force=1 to override.",
            }
    count = ingest_whatsapp()
    return {"ingested": count}


def _trigger_video_ingest(force: bool = False) -> dict:
    """Pull video transcripts from Postgres and add them to ChromaDB.
    Mirrors `_trigger_whatsapp_ingest`. force=True bypasses the
    "already-ingested" short-circuit (use after re-transcription)."""
    from query import get_vectorstore
    from ingest import ingest_videos
    vs = get_vectorstore()
    collection = vs._collection
    if not force:
        existing = collection.get(where={"type": "video"}, limit=1, include=[])
        if existing and existing.get("ids"):
            return {
                "skipped": True,
                "reason": "Video chunks already in index. Pass ?force=1 to override.",
            }
    count = ingest_videos(force=force)
    return {"ingested": count}


# ============================================================
# Auth middleware
# ============================================================

def require_auth(view):
    """Decorator: require a valid Bearer token. Sets request.user_email."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "") or ""
        token = ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        email = auth_module.verify_token(token)
        if not email:
            return jsonify({"error": "Invalid or expired token"}), 401
        request.user_email = email
        return view(*args, **kwargs)
    return wrapper

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

  window._mdsWidgetSummarize = async function(name) {
    sendBtn.disabled = true;
    addMsg('Summarize the session: ' + name, 'user');
    addTyping();
    try {
      var res = await fetch(API_URL + '/api/summarize-source', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({source: name})
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
        <div style="text-align:center;padding:12px 0 8px;color:#a1a1aa;font-size:11px;">v{{ version }}</div>
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

        async function summarizeSource(name) {
            sendBtn.disabled = true;
            addMessage('Summarize the session: ' + name, 'user');
            addTyping();
            try {
                const res = await fetch('/api/summarize-source', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ source: name }),
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

        landingInput.focus();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, version=VERSION)


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
@require_auth
def api_ask():
    data = request.get_json()
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    # Track the search query
    track_search(question)

    # Wrap ask() so any uncaught exception logs a full traceback to the
    # Render runtime log AND returns a structured 500 to the client. Without
    # this, an exception bubbles up to Flask's default handler and we get an
    # opaque "server returned 500" on the client with no breadcrumb.
    #
    # The `error` field is the user-visible message iOS pulls into the chat
    # error banner. We pack `<ExceptionType>: <message>` here so a single
    # failure reproduces with full diagnostic value on the client side —
    # otherwise a generic "ask_failed" leaks zero signal to the user (and
    # to me when Andy reports the bug).
    try:
        result = ask(question)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[api_ask] question={question!r}\n{tb}", flush=True)
        msg = str(e) or "(no message)"
        return jsonify({
            "error": f"{type(e).__name__}: {msg}",
            "type": type(e).__name__,
            "message": msg,
            "where": "ask",
        }), 500

    return jsonify({
        "answer": result["answer"],
        "sources": result["sources"],
        "confidence": result["confidence"],
        "chunks_used": result["chunks_used"],
    })


@app.route("/api/summarize-source", methods=["POST"])
@require_auth
def api_summarize_source():
    """Summarize all content from a specific source/speaker by metadata lookup."""
    data = request.get_json()
    source_name = data.get("source", "").strip()

    if not source_name:
        return jsonify({"error": "No source name provided"}), 400

    result = summarize_source(source_name)
    return jsonify({
        "answer": result["answer"],
        "sources": result["sources"],
        "confidence": result["confidence"],
        "chunks_used": result["chunks_used"],
    })


@app.route("/api/suggestions")
@require_auth
def api_suggestions():
    """Return topic suggestions and popular searches."""
    topics = extract_topics()
    popular = get_popular_searches(limit=6)
    return jsonify({
        "topics": topics,
        "popular": popular,
    })


@app.route("/api/digests")
@require_auth
def api_digests():
    """List WhatsApp digests from Airtable Summaries table.

    Query params (all optional):
      - limit: page size, max 100, default 50
      - offset: Airtable pagination cursor (returned as next_offset)
      - period: 'daily' or 'weekly' to filter
      - chat: filter by exact chat_name
      - show_empty: '1'/'true' to include msg_count=0 records (hidden by default)
    """
    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        return jsonify({"error": "AIRTABLE_PAT not configured on the server."}), 500

    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 100))
    except ValueError:
        limit = 50
    offset = request.args.get("offset")
    period = request.args.get("period")
    chat_name = request.args.get("chat")
    show_empty = request.args.get("show_empty", "").lower() in ("1", "true", "yes")

    # Build Airtable query
    params = {
        "pageSize": limit,
        "sort[0][field]": "date",
        "sort[0][direction]": "desc",
    }
    if offset:
        params["offset"] = offset

    filters = []
    if period:
        # Escape single quotes in user input for Airtable formula
        safe_period = period.replace("'", "\\'")
        filters.append(f"{{period_type}}='{safe_period}'")
    if chat_name:
        safe_chat = chat_name.replace("'", "\\'")
        filters.append(f"{{chat_name}}='{safe_chat}'")
    if not show_empty:
        filters.append("{msg_count}>0")
    if filters:
        params["filterByFormula"] = (
            "AND(" + ",".join(filters) + ")" if len(filters) > 1 else filters[0]
        )

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_DIGESTS_TABLE}"
    headers = {"Authorization": f"Bearer {pat}"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Airtable fetch failed: {e}"}), 502

    data = resp.json()
    digests = []
    for record in data.get("records", []):
        f = record.get("fields", {})
        topics_raw = f.get("topics", "") or ""
        members_raw = f.get("notable_members", "") or ""
        # Enrich tl_dr + summary by replacing first names with full names
        # from the Members table (unambiguous matches only). Notable members
        # too — those often come back as bare first names from Claude.
        digests.append({
            "id": record["id"],
            "date": f.get("date"),
            "chat_id": f.get("chat_id"),
            "chat_name": f.get("chat_name"),
            "period_type": f.get("period_type"),
            "tl_dr": _enrich_full_names(f.get("tl_dr") or ""),
            "summary": _enrich_full_names(f.get("summary_text") or ""),
            "topics": [t.strip() for t in topics_raw.split(",") if t.strip()],
            "notable_members": [
                _enrich_full_names(m.strip())
                for m in members_raw.split(",") if m.strip()
            ],
            "links_shared": _format_links_shared(f.get("links_shared", "") or ""),
            "msg_count": f.get("msg_count", 0) or 0,
            "participant_count": f.get("participant_count", 0) or 0,
        })

    return jsonify({
        "digests": digests,
        "next_offset": data.get("offset"),
    })


# ----- /api/today -----------------------------------------------------------
# Single synthesized "what happened across MDS today" TL;DR. Aggregates today's
# WhatsApp digests across all channels and asks Claude to produce one paragraph
# the iOS home screen can show at a glance. Cached in-process for 1h so we
# don't re-bill Claude on every app open.

# Cache: { date_iso (str): {"tldr": str, "channels": [...], "generated_at": float} }
_today_cache: dict = {}
_today_cache_ttl_s = 3600.0  # 1 hour


def _today_iso_utc() -> str:
    """Return today's date in YYYY-MM-DD (UTC) — matches Airtable digest dates."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fetch_digests_for_date(date_iso: str) -> list[dict]:
    """Pull every msg_count>0 digest from Airtable for a given YYYY-MM-DD.

    The `date` field in Airtable is stored as a Date type (not a string),
    so a plain `{date}='YYYY-MM-DD'` filter never matches. IS_SAME compares
    by calendar day in the base's timezone — the right primitive here.
    """
    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        return []
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_DIGESTS_TABLE}"
    params = {
        "pageSize": 100,
        "filterByFormula": f"AND(IS_SAME({{date}}, '{date_iso}', 'day'),{{msg_count}}>0)",
        "sort[0][field]": "msg_count",
        "sort[0][direction]": "desc",
    }
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {pat}"},
                         params=params, timeout=20)
        r.raise_for_status()
        return r.json().get("records", [])
    except requests.RequestException:
        return []


def _fetch_latest_nonempty_digests() -> tuple[list[dict], str]:
    """Return the most-recent calendar day's set of msg_count>0 digests and
    its date string. Fallback when today (and possibly the past few days)
    don't have any data — covers weekends, holidays, batch lag, etc.

    Returns ([], "") if the table is entirely empty (catastrophic case).
    """
    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        return ([], "")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_DIGESTS_TABLE}"
    params = {
        "pageSize": 50,
        "filterByFormula": "{msg_count}>0",
        "sort[0][field]": "date",
        "sort[0][direction]": "desc",
    }
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {pat}"},
                         params=params, timeout=20)
        r.raise_for_status()
        records = r.json().get("records", [])
    except requests.RequestException:
        return ([], "")
    if not records:
        return ([], "")
    latest_date = (records[0].get("fields", {}) or {}).get("date") or ""
    same_day = [
        rec for rec in records
        if (rec.get("fields", {}) or {}).get("date") == latest_date
    ]
    # Sort that day's records by msg_count desc to match the today flow.
    same_day.sort(key=lambda r: (r.get("fields", {}) or {}).get("msg_count") or 0,
                  reverse=True)
    return (same_day, latest_date)


def _synthesize_today_tldr(date_iso: str, records: list[dict]) -> str:
    """Ask Claude for a single 2-3 sentence cross-channel summary of today."""
    from langchain_anthropic import ChatAnthropic
    import config as cfg

    bullets = []
    for rec in records:
        f = rec.get("fields", {}) or {}
        chat = f.get("chat_name") or "Unknown"
        tl_dr = (f.get("tl_dr") or "").strip()
        if tl_dr:
            # Enrich first names BEFORE feeding to Claude so the synthesized
            # cross-channel TL;DR uses full names natively.
            bullets.append(f"- {chat}: {_enrich_full_names(tl_dr)}")
    if not bullets:
        return ""

    body = "\n".join(bullets)
    prompt = (
        "Below are today's per-channel WhatsApp summaries from the Million "
        "Dollar Sellers community. Synthesize them into a SINGLE paragraph "
        "(2-3 sentences, max 60 words) that captures what's happening across "
        "MDS today — operator-confident, calm, editorial voice. Don't list "
        "channels. Don't say 'Today across MDS' or 'In summary'. Lead with "
        "the most consequential thread.\n\n"
        f"DATE: {date_iso}\n\n{body}"
    )
    try:
        llm = ChatAnthropic(
            model=cfg.LLM_MODEL,
            temperature=0.2,
            anthropic_api_key=cfg.ANTHROPIC_API_KEY,
        )
        resp = llm.invoke(prompt)
        return (resp.content or "").strip()
    except Exception:
        # Fallback: just return the longest tl_dr as a passable summary.
        return max((b.split(": ", 1)[-1] for b in bullets), key=len, default="")


@app.route("/api/today")
@require_auth
def api_today():
    """Single cross-channel TL;DR for today + per-channel digest links.

    Response:
        {
          "date": "2026-05-06",
          "tldr": "Three threads dominated…",
          "channels": [
            {"chat_name": "MDS AI & Automations", "digest_id": "rec…",
             "msg_count": 49, "tl_dr": "Cross-LLM code review…"},
            …
          ],
          "generated_at": "2026-05-06T16:32:11Z",
          "fallback_date": null  // or "2026-05-05" if today had no digests
        }
    """
    import time
    from datetime import datetime, timezone

    today = _today_iso_utc()

    # Serve from cache if fresh.
    cached = _today_cache.get(today)
    if cached and (time.time() - cached["generated_at"]) < _today_cache_ttl_s:
        return jsonify(cached["payload"])

    # Pull today's digests; if today is empty, fall back to whatever the
    # most-recent non-empty date is. Earlier this only fell back one day,
    # which broke the home-screen Brief on weekends and any time the batch
    # ran a few days behind (Andy hit this on 2026-05-07: latest data was
    # 05-05, fallback to 05-06 returned empty, brief showed "no data").
    records = _fetch_digests_for_date(today)
    fallback_date: Optional[str] = None
    actual_date = today
    if not records:
        records, latest_date = _fetch_latest_nonempty_digests()
        if records and latest_date:
            actual_date = latest_date
            fallback_date = latest_date

    # Synthesize.
    tldr = _synthesize_today_tldr(actual_date, records) if records else ""

    channels = []
    for rec in records:
        f = rec.get("fields", {}) or {}
        channels.append({
            "chat_name": f.get("chat_name"),
            "digest_id": rec["id"],
            "msg_count": int(f.get("msg_count") or 0),
            "tl_dr": _enrich_full_names(f.get("tl_dr") or ""),
        })

    # Enrich the synthesized cross-channel TLDR too — Claude's input bullets
    # were already enriched (see _synthesize_today_tldr above) but a second
    # pass catches anything Claude paraphrased back into a bare first name.
    tldr = _enrich_full_names(tldr)

    payload = {
        "date": actual_date,
        "tldr": tldr,
        "channels": channels,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fallback_date": fallback_date,
    }
    _today_cache[today] = {"generated_at": time.time(), "payload": payload}
    return jsonify(payload)


# ============================================================
# Device tokens (APNs)
# ============================================================
# The iOS app posts its APNs hex device token here on every successful
# registerForRemoteNotifications. Tokens can rotate (uninstall, restore from
# backup, re-install) so we upsert on (token), not (email).
#
# Airtable schema for "iOS Devices" table — create manually or via the meta
# API. Required fields:
#   token              Single line text (primary)
#   email              Single line text
#   platform           Single select: ios | android | web
#   bundle_id          Single line text
#   app_version        Single line text
#   app_build          Single line text
#   enabled            Checkbox (default true)
#   last_seen          Date with time, ISO8601
#   live_activity_token  Long text (optional, for Build 28)
#   live_activity_id   Single line text (optional, for Build 28)
#   last_error_status  Number (optional, 0-599)
#   last_error_reason  Single line text (optional)


def _airtable_headers() -> dict:
    pat = os.getenv("AIRTABLE_PAT") or ""
    return {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}


def _airtable_devices_url() -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_DEVICES_TABLE}"


def _airtable_find_device(token: str) -> Optional[dict]:
    """Look up an existing record by APNs token. Returns the AT record or None."""
    safe_token = token.replace("'", "\\'")
    params = {
        "filterByFormula": f"{{token}}='{safe_token}'",
        "maxRecords": 1,
    }
    try:
        r = requests.get(_airtable_devices_url(), headers=_airtable_headers(),
                         params=params, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return None
    records = r.json().get("records", [])
    return records[0] if records else None


def _airtable_list_enabled_devices(platform: str = "ios") -> list[dict]:
    """List all enabled device records for fan-out."""
    devices: list[dict] = []
    offset: Optional[str] = None
    while True:
        params = {
            "pageSize": 100,
            "filterByFormula": f"AND({{enabled}}=1,{{platform}}='{platform}')",
        }
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(_airtable_devices_url(), headers=_airtable_headers(),
                             params=params, timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            break
        body = r.json()
        devices.extend(body.get("records", []))
        offset = body.get("offset")
        if not offset:
            break
    return devices


# ============================================================
# Text-to-Speech proxy (ElevenLabs)
# ============================================================
# The iOS Listen button reads digest text aloud. AVSpeechSynthesizer's default
# voice quality is poor; ElevenLabs Turbo v2.5 produces near-natural narration
# at ~$0.30/1k chars (Andy's starter tier covers 40k chars/mo, ~80-130 reads).
#
# We proxy on the server so:
#   - The xi-api-key never ships in the iOS binary.
#   - We can cache (text_hash, voice_id) → mp3 bytes so re-Listening the same
#     digest in the same hour is free.
#   - Auth gating prevents drive-by abuse of the quota.

_tts_cache: dict = {}            # {(sha1, voice_id): (timestamp, mp3_bytes)}
_tts_cache_ttl_s = 3600.0        # 1 hour
_tts_max_chars = 1500            # ~$0.45/call ceiling


def _tts_cache_get(key: tuple) -> Optional[bytes]:
    import time
    entry = _tts_cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if (time.time() - ts) > _tts_cache_ttl_s:
        _tts_cache.pop(key, None)
        return None
    return data


def _tts_cache_set(key: tuple, data: bytes) -> None:
    import time
    # Trim if growing unbounded (rough cap).
    if len(_tts_cache) > 100:
        _tts_cache.clear()
    _tts_cache[key] = (time.time(), data)


def _clean_markdown_for_tts(text: str) -> str:
    """Strip markdown syntax so ElevenLabs doesn't read it aloud.

    Removes the syntax characters (asterisks, underscores, hashes, backticks,
    list markers, link brackets) but preserves the underlying words and
    natural punctuation. `--` and `---` em-dash sequences become ", " so the
    voice gets a natural pause instead of saying "dash dash".
    """
    import re
    if not text:
        return text
    # Markdown links: [label](url) -> label
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Bold (handle before italics): **x** and __x__ -> x
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
    text = re.sub(r'__([^_\n]+)__', r'\1', text)
    # Italics: *x* and _x_ -> x (lookarounds avoid list markers + word_internal_underscores)
    text = re.sub(r'(?<![\w*])\*([^\s*][^*\n]*?)\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<![\w_])_([^\s_][^_\n]*?)_(?!\w)', r'\1', text)
    # Inline code: `x` -> x
    text = re.sub(r'`+([^`\n]+)`+', r'\1', text)
    # Headers: leading #+ at line start
    text = re.sub(r'(?m)^\s*#{1,6}\s+', '', text)
    # List markers at line start
    text = re.sub(r'(?m)^\s*[-*+]\s+', '', text)
    text = re.sub(r'(?m)^\s*\d+\.\s+', '', text)
    # Em-dash sequences: -- / --- -> ", " for natural pause
    text = re.sub(r' ?-{2,} ?', ', ', text)
    # FINAL-PASS NUKE: any markdown emphasis / decorator characters that
    # somehow survived the structured patterns above. Triple asterisks
    # (***x***), unmatched/asymmetric asterisks, leftover underscores at
    # word boundaries, tildes from ~~strikethrough~~, residual hashes.
    # Safe for TTS — these characters never need to be voiced.
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'(?<!\w)_+|_+(?!\w)', '', text)
    text = re.sub(r'~+', '', text)
    text = re.sub(r'(?m)^#+\s*', '', text)
    # Collapse runs of spaces/tabs introduced by removals; preserve newlines
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return text.strip()


@app.route("/api/tts", methods=["POST"])
@require_auth
def api_tts():
    """Synthesize the given text via ElevenLabs and return audio/mpeg.

    Body: {"text": "...", "voice_id": "<optional>"}
    Returns: audio/mpeg (raw MP3 bytes) on 200, JSON error otherwise.
    """
    import hashlib
    from flask import Response

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return jsonify({"error": "TTS not configured"}), 500
    default_voice = (os.getenv("ELEVENLABS_VOICE_ID") or "JBFqnCBsd6RMkjVDRZzb").strip()
    model_id = (os.getenv("ELEVENLABS_MODEL_ID") or "eleven_turbo_v2_5").strip()

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    voice_id = (data.get("voice_id") or default_voice).strip()
    if not text:
        return jsonify({"error": "Missing text"}), 400
    # Strip markdown before caching so two requests with/without syntax hit
    # the same cache entry, and ElevenLabs never sees the literal characters.
    text = _clean_markdown_for_tts(text)
    if not text:
        return jsonify({"error": "Empty text after cleanup"}), 400
    if len(text) > _tts_max_chars:
        text = text[:_tts_max_chars]

    # Cache key version: bump to invalidate stale entries when the cleanup
    # logic changes (e.g. v1 cached MP3s with literal markdown chars before
    # the strip; v2 expects cleaned text).
    cache_key = ("v2", hashlib.sha1(text.encode("utf-8")).hexdigest(), voice_id)
    # Debug header: first 80 chars of cleaned text so the iOS client (or curl)
    # can verify what was actually sent to ElevenLabs without listening to MP3.
    cleaned_sample = text[:80].replace("\n", " ")
    cached = _tts_cache_get(cache_key)
    if cached:
        return Response(cached, mimetype="audio/mpeg",
                        headers={"X-MDS-TTS-Cache": "hit",
                                 "X-MDS-TTS-Cleaned-Sample": cleaned_sample,
                                 "X-MDS-TTS-Cleaned-Length": str(len(text)),
                                 "Cache-Control": "private, max-age=3600"})

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as e:
        return jsonify({"error": f"TTS upstream error: {e}"}), 502
    if r.status_code != 200:
        # Pass the upstream reason through so iOS can fall back gracefully.
        try:
            err = r.json().get("detail", {}).get("message") or r.text[:200]
        except Exception:
            err = r.text[:200]
        return jsonify({"error": f"ElevenLabs {r.status_code}: {err}"}), 502

    audio = r.content
    _tts_cache_set(cache_key, audio)
    return Response(audio, mimetype="audio/mpeg",
                    headers={"X-MDS-TTS-Cache": "miss",
                             "X-MDS-TTS-Cleaned-Sample": cleaned_sample,
                             "X-MDS-TTS-Cleaned-Length": str(len(text)),
                             "Cache-Control": "private, max-age=3600"})


@app.route("/api/devices", methods=["POST"])
@require_auth
def api_devices_register():
    """Register or refresh the caller's APNs device token.

    Idempotent on `token` — if the same hex token already exists, we update
    its email + last_seen + app version. If it's new, we create.

    On token rotation iOS will call this with a new hex string, so over time
    the same email may have multiple active tokens (one per device install).
    That's correct: a user with phone + iPad gets two tokens, two pushes.
    """
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token or len(token) < 32 or not all(c in "0123456789abcdefABCDEF" for c in token):
        return jsonify({"error": "Missing or malformed device token"}), 400
    platform = (data.get("platform") or "ios").strip()
    bundle_id = (data.get("bundle_id") or "com.mds.knowledgebase").strip()
    app_version = (data.get("app_version") or "").strip()
    app_build = (data.get("app_build") or "").strip()
    email = getattr(request, "user_email", "") or ""

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    existing = _airtable_find_device(token)
    fields = {
        "token": token,
        "email": email,
        "platform": platform,
        "bundle_id": bundle_id,
        "app_version": app_version,
        "app_build": app_build,
        "enabled": True,
        "last_seen": now_iso,
    }
    try:
        if existing:
            patch_body = {"fields": fields}
            r = requests.patch(
                f"{_airtable_devices_url()}/{existing['id']}",
                headers=_airtable_headers(),
                data=json.dumps(patch_body),
                timeout=15,
            )
        else:
            post_body = {"fields": fields, "typecast": True}
            r = requests.post(
                _airtable_devices_url(),
                headers=_airtable_headers(),
                data=json.dumps(post_body),
                timeout=15,
            )
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not save device: {e}"}), 502
    return jsonify({"ok": True})


@app.route("/api/devices/live-activity", methods=["POST"])
@require_auth
def api_devices_live_activity():
    """Store the per-Activity push token iOS hands out via Activity.pushTokenUpdates.

    A Live Activity push token is DIFFERENT from a regular APNs device token —
    it's scoped to one specific running activity and is what we target with
    apns-push-type=liveactivity payloads.

    Body: {activity_id, live_activity_token, date}.
    Stored alongside the most recent regular device record for the calling
    user (we just look up by email + take the most recent ios row). If
    there are multiple recent rows we update the one with the matching
    bundle_id; in practice one device = one row.
    """
    data = request.get_json(silent=True) or {}
    la_token = (data.get("live_activity_token") or "").strip()
    activity_id = (data.get("activity_id") or "").strip()
    date_iso = (data.get("date") or "").strip()
    email = getattr(request, "user_email", "") or ""
    if not la_token or not activity_id:
        return jsonify({"error": "Missing live_activity_token or activity_id"}), 400

    # Find the most-recent enabled iOS device for this email.
    safe_email = email.replace("'", "\\'")
    params = {
        "filterByFormula": f"AND({{email}}='{safe_email}',{{platform}}='ios',{{enabled}}=1)",
        "sort[0][field]": "last_seen",
        "sort[0][direction]": "desc",
        "maxRecords": 1,
    }
    try:
        r = requests.get(_airtable_devices_url(), headers=_airtable_headers(),
                         params=params, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not look up device: {e}"}), 502
    records = r.json().get("records", [])
    if not records:
        return jsonify({"error": "No active iOS device for this user"}), 404

    rec = records[0]
    fields = {
        "live_activity_token": la_token,
        "live_activity_id": activity_id,
    }
    if date_iso:
        # Stash the date into a "last_seen"-adjacent free-form note? Simpler:
        # we don't need to track the date here — the activity id encodes it
        # implicitly per-day on the iOS side. Skip.
        pass
    try:
        requests.patch(
            f"{_airtable_devices_url()}/{rec['id']}",
            headers=_airtable_headers(),
            data=json.dumps({"fields": fields}),
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not save Live Activity token: {e}"}), 502
    return jsonify({"ok": True})


@app.route("/api/devices", methods=["DELETE"])
@require_auth
def api_devices_unregister():
    """Disable the caller's device. Pass `?token=<hex>` to target a specific
    device, or omit to disable every device for this email (sign-out flow).
    Soft delete (set enabled=false) so we keep history.
    """
    email = getattr(request, "user_email", "") or ""
    token = (request.args.get("token") or "").strip()
    if token:
        rec = _airtable_find_device(token)
        if not rec:
            return jsonify({"ok": True, "found": 0})
        try:
            requests.patch(
                f"{_airtable_devices_url()}/{rec['id']}",
                headers=_airtable_headers(),
                data=json.dumps({"fields": {"enabled": False}}),
                timeout=10,
            ).raise_for_status()
        except requests.RequestException as e:
            return jsonify({"error": f"Could not update device: {e}"}), 502
        return jsonify({"ok": True, "found": 1})

    # No token: disable all devices for this user.
    safe_email = email.replace("'", "\\'")
    params = {"filterByFormula": f"{{email}}='{safe_email}'", "pageSize": 100}
    try:
        r = requests.get(_airtable_devices_url(), headers=_airtable_headers(),
                         params=params, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not list devices: {e}"}), 502
    records = r.json().get("records", [])
    for rec in records:
        try:
            requests.patch(
                f"{_airtable_devices_url()}/{rec['id']}",
                headers=_airtable_headers(),
                data=json.dumps({"fields": {"enabled": False}}),
                timeout=10,
            )
        except requests.RequestException:
            continue
    return jsonify({"ok": True, "found": len(records)})


# ============================================================
# Admin push fan-out
# ============================================================
# Called by n8n (or Andy curling) AFTER the morning WA digest batch finishes
# writing to Airtable. We pull today's TL;DR and per-channel counts, then
# send a single APNs push to every enabled iOS device.
#
# Auth: X-Admin-Secret header equal to the ADMIN_PUSH_SECRET Render env var.
# Token-based auth (the user-session JWT) was rejected because n8n would
# need to refresh it; a fixed shared secret is simpler and scoped to this one
# endpoint.

def _require_admin_push_secret() -> Optional[tuple]:
    """Returns a (jsonify, status) error tuple if not authorized, else None."""
    expected = (os.getenv("ADMIN_PUSH_SECRET") or "").strip()
    if not expected:
        return jsonify({"error": "ADMIN_PUSH_SECRET not configured"}), 500
    provided = (request.headers.get("X-Admin-Secret") or "").strip()
    if not provided or provided != expected:
        return jsonify({"error": "Forbidden"}), 403
    return None


def _build_today_push_payload(date_iso: str, channels: list[dict],
                              tldr: str) -> dict:
    """Wrap the Today summary in an APNs alert payload.

    The body is the synthesized cross-channel TL;DR. Subtitle gives a quick
    "X new digests · Y chats" breakdown so the lock-screen notification has
    real density without opening the app.
    """
    n_chats = len(channels)
    n_msgs = sum(int(c.get("msg_count") or 0) for c in channels)
    title = "Morning digests are ready"
    if n_chats == 0:
        subtitle = ""
    elif n_chats == 1:
        subtitle = f"1 chat · {n_msgs} messages"
    else:
        subtitle = f"{n_chats} chats · {n_msgs} messages"
    body = (tldr or "").strip()
    if not body:
        body = "Open the app to read what happened across MDS today."
    elif len(body) > 240:
        body = body[:237] + "…"
    return {
        "aps": {
            "alert": {
                "title": title,
                "subtitle": subtitle,
                "body": body,
            },
            "sound": "default",
            "badge": n_chats,
            "thread-id": "mds-today",
            "interruption-level": "active",
        },
        # Custom keys for any iOS-side handling. The Live Activity start
        # was removed in build (38) — the morning-digest Live Activity was
        # redundant with the push notification (one-shot event, not an
        # ongoing process) and cluttered the Dynamic Island with no clear
        # purpose. Push handles this case fine on its own.
        "today_date": date_iso,
        "n_channels": n_chats,
        "n_messages": n_msgs,
    }


@app.route("/api/admin/push/today", methods=["POST"])
def api_admin_push_today():
    """Fan out today's digest TL;DR to every enabled iOS device.

    Headers:
        X-Admin-Secret: must equal ADMIN_PUSH_SECRET env var.

    Optional body (JSON):
        {"dry_run": true}  → don't actually send, just count.

    Response:
        {
          "date": "2026-05-06",
          "n_channels": 8,
          "n_devices": 4,
          "sent": 4,
          "failed": 0,
          "errors": []
        }
    """
    err = _require_admin_push_secret()
    if err is not None:
        return err

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run"))

    # Reuse the same logic /api/today uses, but force a refresh of today's
    # records (don't trust the 1h cache — n8n calls this right after writing).
    today = _today_iso_utc()
    records = _fetch_digests_for_date(today)
    fallback_date = None
    if not records:
        from datetime import datetime, timedelta, timezone
        y = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        records = _fetch_digests_for_date(y)
        fallback_date = y if records else None

    tldr = _synthesize_today_tldr(fallback_date or today, records) if records else ""
    channels = []
    for rec in records:
        f = rec.get("fields", {}) or {}
        channels.append({
            "chat_name": f.get("chat_name"),
            "msg_count": int(f.get("msg_count") or 0),
            "tl_dr": f.get("tl_dr") or "",
        })

    payload = _build_today_push_payload(fallback_date or today, channels, tldr)

    devices = _airtable_list_enabled_devices(platform="ios")

    summary = {
        "date": fallback_date or today,
        "n_channels": len(channels),
        "n_devices": len(devices),
        "sent": 0,
        "failed": 0,
        "errors": [],
        "dry_run": dry_run,
    }
    if dry_run or not devices:
        return jsonify(summary)

    # Lazy-import APNs client so the module is fine to load when env vars
    # aren't set yet.
    try:
        from apns import get_apns_client, APNsError
        client = get_apns_client()
    except Exception as e:
        return jsonify({"error": f"APNs not configured: {e}"}), 500

    for rec in devices:
        f = rec.get("fields", {}) or {}
        token = (f.get("token") or "").strip()
        if not token:
            continue
        try:
            client.send(
                device_token=token,
                payload=payload,
                push_type="alert",
                priority=10,
                collapse_id=f"mds-today-{summary['date']}",
            )
            summary["sent"] += 1
        except APNsError as e:
            summary["failed"] += 1
            summary["errors"].append({
                "token_prefix": token[:8],
                "status": e.status,
                "reason": e.reason,
            })
            # Auto-disable on terminal errors per Apple guidance.
            if e.status in (400, 410) and e.reason in (
                "BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"
            ):
                try:
                    requests.patch(
                        f"{_airtable_devices_url()}/{rec['id']}",
                        headers=_airtable_headers(),
                        data=json.dumps({"fields": {
                            "enabled": False,
                            "last_error_status": e.status,
                            "last_error_reason": e.reason,
                        }}),
                        timeout=10,
                    )
                except requests.RequestException:
                    pass
        except Exception as e:
            summary["failed"] += 1
            summary["errors"].append({
                "token_prefix": token[:8],
                "status": 0,
                "reason": str(e)[:200],
            })
    return jsonify(summary)


# ============================================================
# Auth routes
# ============================================================

@app.route("/api/auth/request-code", methods=["POST"])
def api_auth_request_code():
    """Send a 6-digit login code to the user's email.

    Gated by source-base AT Database Status (Current Member, New Member, or
    Pending Group Entrance). Other statuses get 403.

    Apple App Store reviewer bypass: when the email matches the configured
    REVIEWER_EMAIL env var, this endpoint returns 200 immediately WITHOUT
    sending an email. The reviewer enters REVIEWER_FIXED_CODE in the verify
    step. Tell Apple the credentials in TestFlight reviewer notes.
    """
    import os
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not auth_module.is_valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    if not auth_module.is_member_email(email):
        return jsonify({
            "error": "We can't find an active MDS membership for that email. "
                     "Sign in is for current and new MDS members only."
        }), 403

    # Reviewer path: don't bother with Resend, the reviewer knows the fixed code.
    reviewer = (os.getenv("REVIEWER_EMAIL") or "").strip().lower()
    if reviewer and email.lower() == reviewer:
        return jsonify({"ok": True})

    code = auth_module.generate_code()
    auth_module.store_code(email, code)
    sent = email_sender.send_login_code(email, code)
    if not sent:
        return jsonify({"error": "Could not send the email. Try again."}), 502
    return jsonify({"ok": True})


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    """Verify the 6-digit code and issue a session token."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    code = (data.get("code") or "").strip()
    if not auth_module.is_valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if not code:
        return jsonify({"error": "Please enter the 6-digit code."}), 400
    if not auth_module.consume_code(email, code):
        return jsonify({"error": "That code is invalid or expired. Request a new one."}), 401
    try:
        session = auth_module.issue_token(email)
    except Exception as e:
        return jsonify({"error": f"Could not create session: {e}"}), 500
    return jsonify(session)


@app.route("/api/auth/me")
@require_auth
def api_auth_me():
    """Return the email tied to the current session."""
    return jsonify({"email": getattr(request, "user_email", None)})


@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def api_auth_logout():
    """Invalidate the current token."""
    header = request.headers.get("Authorization", "") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    auth_module.revoke_token(token)
    return jsonify({"ok": True})


@app.route("/api/admin/reingest-wa", methods=["POST"])
@require_auth
def api_admin_reingest_wa():
    """Manually trigger WhatsApp ingestion. Admin-only via ADMIN_EMAILS."""
    import os
    admin_emails = {a.strip().lower() for a in (os.getenv("ADMIN_EMAILS","") or "").split(",") if a.strip()}
    user = (getattr(request, "user_email", "") or "").lower()
    if user not in admin_emails:
        return jsonify({"error": "admin only"}), 403
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        result = _trigger_whatsapp_ingest(force=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/reingest-videos", methods=["POST"])
@require_auth
def api_admin_reingest_videos():
    """Manually trigger video transcript ingestion. Admin-only via ADMIN_EMAILS.
    Pass ?force=1 to re-add even if video chunks are already in the index."""
    import os
    admin_emails = {a.strip().lower() for a in (os.getenv("ADMIN_EMAILS","") or "").split(",") if a.strip()}
    user = (getattr(request, "user_email", "") or "").lower()
    if user not in admin_emails:
        return jsonify({"error": "admin only"}), 403
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        result = _trigger_video_ingest(force=force)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


# ============================================================
# MDS Video Platform routes — feature-flagged off by default.
# Routes only register when ENABLE_VIDEO_PLATFORM=true is set in the env,
# so the existing OTP/RAG/digest routes stay untouched on production until
# we flip the flag on Render. See videos.py for the M1 implementation.
# ============================================================
if videos_module.is_enabled():
    videos_module.register_video_routes(app, require_auth)

    # Mux webhook — fires for video.upload.asset_created /
    # video.asset.ready / etc. when admin uploads via the M2 admin app.
    # Verifies HMAC signature against MUX_WEBHOOK_SECRET (fail-open if
    # the secret env var isn't set, for initial setup before Andy
    # generates the signing key in the Mux dashboard).
    @app.route("/api/webhooks/mux", methods=["POST"])
    def mux_webhook():
        raw = request.get_data(cache=True)
        sig = request.headers.get("Mux-Signature", "")
        if not mux_webhook_module.verify_signature(raw, sig):
            return jsonify({"error": "Invalid signature"}), 401
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        body, code = mux_webhook_module.handle_webhook(payload)
        return jsonify(body), code

    # AssemblyAI completion webhook — no `require_auth` (server-to-server,
    # gated by the X-MDS-Webhook-Secret shared secret instead).
    @app.route("/api/webhooks/assemblyai", methods=["POST"])
    def assemblyai_webhook():
        secret_value = request.headers.get("X-MDS-Webhook-Secret", "")
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        body, code = transcripts_module.handle_webhook(payload, secret_value)
        return jsonify(body), code

    # Admin trigger to (re)submit a video for transcription. Useful for
    # the M3 backfill of pre-existing test videos and for retry after
    # a failed transcription. Requires user auth + the caller must be a
    # super_admin in their org. M5+ admin UI will replace this manual hook.
    @app.route("/api/admin/videos/<video_id>/transcribe", methods=["POST"])
    @require_auth
    def admin_submit_transcription(video_id: str):
        try:
            result = transcripts_module.submit_transcription(video_id)
            return jsonify({
                "ok": True,
                "transcript_id": result.get("id"),
                "status": result.get("status"),
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Server-to-server transcribe trigger, gated by a shared secret rather
    # than a user session. Used by the mds-video-admin M5 retry button so
    # it never has to spoof Andy's session token to reach this code path
    # (see feedback_dont_signin_as_andy.md). Same body shape as the
    # user-auth route above; just a different auth surface.
    @app.route("/api/internal/videos/<video_id>/transcribe", methods=["POST"])
    def internal_submit_transcription(video_id: str):
        expected = os.getenv("MDS_ADMIN_INTERNAL_SECRET", "")
        received = request.headers.get("X-MDS-Admin-Secret", "")
        if not expected or expected != received:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            result = transcripts_module.submit_transcription(video_id)
            return jsonify({
                "ok": True,
                "transcript_id": result.get("id"),
                "status": result.get("status"),
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
