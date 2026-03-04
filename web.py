"""
MDS AI Bot — Web UI (Flask).
Simple chat interface for querying the MDS knowledge base.
"""

import os
from flask import Flask, render_template_string, request, jsonify
from query import ask

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MDS Knowledge Assistant</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        header {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border-bottom: 1px solid #334155;
            padding: 1rem 2rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        header h1 {
            font-size: 1.25rem;
            font-weight: 600;
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        header .badge {
            font-size: 0.7rem;
            padding: 0.2rem 0.5rem;
            background: #1e3a5f;
            color: #60a5fa;
            border-radius: 999px;
            font-weight: 500;
        }
        .chat-container {
            flex: 1;
            max-width: 800px;
            width: 100%;
            margin: 0 auto;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
            overflow-y: auto;
        }
        .message {
            padding: 1rem 1.25rem;
            border-radius: 12px;
            max-width: 90%;
            line-height: 1.6;
            font-size: 0.95rem;
        }
        .message.user {
            background: #1e3a5f;
            align-self: flex-end;
            border-bottom-right-radius: 4px;
        }
        .message.bot {
            background: #1e293b;
            border: 1px solid #334155;
            align-self: flex-start;
            border-bottom-left-radius: 4px;
        }
        .message.bot .confidence {
            margin-top: 0.75rem;
            padding-top: 0.5rem;
            border-top: 1px solid #334155;
            font-size: 0.8rem;
            color: #94a3b8;
        }
        .message.bot .confidence .high { color: #4ade80; }
        .message.bot .confidence .medium { color: #facc15; }
        .message.bot .confidence .low { color: #f87171; }
        .message.bot .sources {
            margin-top: 0.5rem;
            font-size: 0.8rem;
            color: #64748b;
        }
        .message.bot .sources span {
            display: inline-block;
            background: #0f172a;
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            margin: 0.15rem 0.15rem 0 0;
            font-size: 0.75rem;
        }
        .message.bot pre {
            background: #0f172a;
            padding: 0.75rem;
            border-radius: 6px;
            overflow-x: auto;
            margin: 0.5rem 0;
        }
        .message.bot ul, .message.bot ol {
            margin: 0.5rem 0 0.5rem 1.25rem;
        }
        .message.bot p { margin-bottom: 0.5rem; }
        .message.bot p:last-child { margin-bottom: 0; }
        .input-area {
            background: #1e293b;
            border-top: 1px solid #334155;
            padding: 1rem 2rem;
        }
        .input-wrapper {
            max-width: 800px;
            margin: 0 auto;
            display: flex;
            gap: 0.75rem;
        }
        .input-wrapper input {
            flex: 1;
            padding: 0.75rem 1rem;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            color: #e2e8f0;
            font-size: 0.95rem;
            outline: none;
            transition: border-color 0.2s;
        }
        .input-wrapper input:focus {
            border-color: #60a5fa;
        }
        .input-wrapper input::placeholder { color: #475569; }
        .input-wrapper button {
            padding: 0.75rem 1.5rem;
            background: linear-gradient(135deg, #3b82f6, #6366f1);
            border: none;
            border-radius: 8px;
            color: white;
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: opacity 0.2s;
        }
        .input-wrapper button:hover { opacity: 0.9; }
        .input-wrapper button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .typing {
            display: inline-flex;
            gap: 4px;
            padding: 0.5rem 0;
        }
        .typing span {
            width: 8px; height: 8px;
            background: #475569;
            border-radius: 50%;
            animation: bounce 1.4s ease-in-out infinite;
        }
        .typing span:nth-child(2) { animation-delay: 0.2s; }
        .typing span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce {
            0%, 60%, 100% { transform: translateY(0); }
            30% { transform: translateY(-6px); }
        }
        .welcome {
            text-align: center;
            padding: 3rem 1rem;
            color: #64748b;
        }
        .welcome h2 {
            font-size: 1.5rem;
            color: #94a3b8;
            margin-bottom: 0.5rem;
        }
        .welcome p { margin-bottom: 0.25rem; }
        .welcome .examples {
            margin-top: 1.5rem;
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            justify-content: center;
        }
        .welcome .examples button {
            background: #1e293b;
            border: 1px solid #334155;
            color: #94a3b8;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        .welcome .examples button:hover {
            border-color: #60a5fa;
            color: #e2e8f0;
        }
    </style>
</head>
<body>
    <header>
        <h1>MDS Knowledge Assistant</h1>
        <span class="badge">Powered by Claude</span>
    </header>

    <div class="chat-container" id="chat">
        <div class="welcome" id="welcome">
            <h2>Ask anything about MDS content</h2>
            <p>I search through video transcripts and presentations to find answers.</p>
            <div class="examples">
                <button onclick="askExample(this)">What did Josh Hadley talk about?</button>
                <button onclick="askExample(this)">What strategies were discussed for Amazon sellers?</button>
                <button onclick="askExample(this)">Who spoke about exit planning?</button>
                <button onclick="askExample(this)">What is the 2020-20 rule?</button>
            </div>
        </div>
    </div>

    <div class="input-area">
        <div class="input-wrapper">
            <input type="text" id="questionInput" placeholder="Ask a question about MDS content..."
                   onkeydown="if(event.key==='Enter')sendQuestion()">
            <button id="sendBtn" onclick="sendQuestion()">Ask</button>
        </div>
    </div>

    <script>
        const chat = document.getElementById('chat');
        const input = document.getElementById('questionInput');
        const sendBtn = document.getElementById('sendBtn');
        const welcome = document.getElementById('welcome');

        function askExample(btn) {
            input.value = btn.textContent;
            sendQuestion();
        }

        function addMessage(text, type, extra) {
            if (welcome) welcome.remove();

            const div = document.createElement('div');
            div.className = `message ${type}`;

            if (type === 'bot') {
                // Convert basic markdown to HTML
                let html = text
                    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                    .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
                    .replace(/\\*(.+?)\\*/g, '<em>$1</em>')
                    .replace(/`(.+?)`/g, '<code>$1</code>')
                    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
                    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
                    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
                    .replace(/^[\\-\\*] (.+)$/gm, '<li>$1</li>')
                    .replace(/^(\\d+)\\. (.+)$/gm, '<li>$2</li>');

                // Wrap consecutive <li> in <ul>
                html = html.replace(/((<li>.*<\\/li>\\n?)+)/g, '<ul>$1</ul>');
                // Paragraphs
                html = html.split('\\n\\n').map(p => {
                    p = p.trim();
                    if (!p) return '';
                    if (p.startsWith('<h') || p.startsWith('<ul') || p.startsWith('<ol') || p.startsWith('<pre'))
                        return p;
                    return `<p>${p}</p>`;
                }).join('');

                div.innerHTML = html;

                if (extra) {
                    const conf = extra.confidence || 0;
                    const confClass = conf > 0.6 ? 'high' : conf > 0.3 ? 'medium' : 'low';
                    const confDiv = document.createElement('div');
                    confDiv.className = 'confidence';
                    confDiv.innerHTML = `Confidence: <span class="${confClass}">${Math.round(conf * 100)}%</span> &middot; ${extra.chunks_used} chunks used`;

                    if (extra.sources && extra.sources.length > 0) {
                        const uniqueSources = [...new Set(extra.sources.map(s => {
                            const name = s.source ? s.source.split('/').pop() : 'Unknown';
                            return name;
                        }))];
                        const srcDiv = document.createElement('div');
                        srcDiv.className = 'sources';
                        srcDiv.innerHTML = 'Sources: ' + uniqueSources.map(s => `<span>${s}</span>`).join('');
                        confDiv.appendChild(srcDiv);
                    }
                    div.appendChild(confDiv);
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

        async function sendQuestion() {
            const question = input.value.trim();
            if (!question) return;

            input.value = '';
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

                if (data.error) {
                    addMessage('Error: ' + data.error, 'bot');
                } else {
                    addMessage(data.answer, 'bot', {
                        confidence: data.confidence,
                        chunks_used: data.chunks_used,
                        sources: data.sources,
                    });
                }
            } catch (err) {
                removeTyping();
                addMessage('Failed to connect to the server. Is it running?', 'bot');
            }

            sendBtn.disabled = false;
            input.focus();
        }

        input.focus();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json()
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "No question provided"}), 400

    result = ask(question)
    return jsonify({
        "answer": result["answer"],
        "sources": result["sources"],
        "confidence": result["confidence"],
        "chunks_used": result["chunks_used"],
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
