// Web Chat — Sofía 2.0 · Maple Collège
// Request/response simple. Streaming SSE puede agregarse en una iteración futura.

const SESSION_ID = window.SOFIA_SESSION_ID || "";
const $messages = document.getElementById("messages");
const $form = document.getElementById("chat-form");
const $input = document.getElementById("input");
const $sendBtn = document.getElementById("send-btn");
const $typing = document.getElementById("typing");
const $meta = document.getElementById("meta");

function escapeHTML(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function formatBubble(text) {
  // Markdown ligero para mensajes del assistant. Bug fix B.3 (reunión Maple
  // 19-may): Sofía genera **texto** estándar pero el frontend solo manejaba
  // *texto* WhatsApp — quedaban asteriscos visibles. Ahora ambos funcionan.
  //
  // Orden importa: **bold** ANTES que *italic* para que la regex de italic
  // (un solo asterisco) no rompa el match de bold.
  const escaped = escapeHTML(text);
  return escaped
    // [texto](url) → hipervínculo clickeable (FIX 2: link de Maps amigable)
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    )
    // **negrita** Markdown estándar (lo que Sofía genera)
    .replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>")
    // *italic* / _italic_ (lookbehind/lookahead para no chocar con palabras)
    .replace(/(?<![*\w])\*([^*\n]+?)\*(?!\w)/g, "<em>$1</em>")
    .replace(/(?<!\w)_([^_\n]+?)_(?!\w)/g, "<em>$1</em>")
    // Saltos de línea
    .replace(/\n/g, "<br/>");
}

function appendBubble(text, role) {
  const div = document.createElement("div");
  div.className = `bubble bubble--${role}`;
  div.innerHTML = formatBubble(text);
  $messages.appendChild(div);
  scrollToBottom();
}

function appendError(text) {
  const div = document.createElement("div");
  div.className = "bubble bubble--error";
  div.textContent = text;
  $messages.appendChild(div);
  scrollToBottom();
}

function scrollToBottom() {
  $messages.scrollTop = $messages.scrollHeight;
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

function setTyping(on) {
  $typing.hidden = !on;
  if (on) scrollToBottom();
}

function updateMeta(turn) {
  if (!turn) {
    $meta.textContent = "";
    return;
  }
  const parts = [
    `t${turn.turn_number}`,
    turn.intent ? `intent=${turn.intent}` : null,
    `${turn.tokens_input}→${turn.tokens_output} tk`,
    turn.tokens_cached ? `cache=${turn.tokens_cached}` : null,
    `$${turn.cost_usd.toFixed(5)}`,
    `${turn.latency_ms}ms`,
  ].filter(Boolean);
  $meta.textContent = parts.join("  ·  ");
}

async function sendMessage(content) {
  setTyping(true);
  $sendBtn.disabled = true;
  $input.disabled = true;

  try {
    const resp = await fetch("/webhook/web", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ content }),
    });

    if (!resp.ok) {
      const errText = await resp.text();
      appendError(`Error ${resp.status}: ${errText.slice(0, 200)}`);
      return;
    }

    const data = await resp.json();
    appendBubble(data.response, "bot");
    updateMeta(data);
  } catch (err) {
    appendError(`Error de red: ${err.message || err}`);
  } finally {
    setTyping(false);
    $sendBtn.disabled = false;
    $input.disabled = false;
    $input.focus();
  }
}

$form.addEventListener("submit", (e) => {
  e.preventDefault();
  const content = $input.value.trim();
  if (!content) return;
  appendBubble(content, "user");
  $input.value = "";
  sendMessage(content);
});

// Focus inicial
$input.focus();
