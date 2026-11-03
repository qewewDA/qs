"""Render the offline Stellaris assistant portal as one self-contained HTML page.

The renderer is deliberately pure: it accepts a payload dictionary and returns
HTML text. It performs no file, network, process or game access and uses only
the Python standard library.
"""

from __future__ import annotations

import json
from typing import Any


__all__ = ["render_portal"]

_DEFAULT_TITLE = "群星 · 离线指挥台"
_DEFAULT_CONFIG_FILE = "mcp-connection.json"
_DEFAULT_GUIDE_FILE = "先看这里.md"
_DEFAULT_PROMPT_FILE = "给Agent的一次性提示词.txt"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _prompt(item: Any, index: int) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    fallback = f"prompt_{index + 1}"
    name = _text(item.get("name"), fallback).strip() or fallback
    title = _text(item.get("title"), name).strip() or name
    return {
        "name": name,
        "title": title,
        "summary": _text(item.get("summary")),
        "text": _text(item.get("text")),
        "filename": _text(item.get("filename"), _DEFAULT_PROMPT_FILE).strip() or _DEFAULT_PROMPT_FILE,
    }


def _normalise(payload: dict) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    raw_prompts = source.get("prompts")
    prompts: list[dict[str, str]] = []
    if isinstance(raw_prompts, list):
        for index, item in enumerate(raw_prompts):
            prompt = _prompt(item, index)
            if prompt is not None:
                prompts.append(prompt)
    return {
        "title": _text(source.get("title"), _DEFAULT_TITLE).strip() or _DEFAULT_TITLE,
        "server_name": _text(source.get("server_name")),
        "campaign_dir": _text(source.get("campaign_dir")),
        "config_file": _text(source.get("config_file"), _DEFAULT_CONFIG_FILE).strip() or _DEFAULT_CONFIG_FILE,
        "config_text": _text(source.get("config_text")),
        "guide_file": _text(source.get("guide_file"), _DEFAULT_GUIDE_FILE).strip() or _DEFAULT_GUIDE_FILE,
        "prompts": prompts,
    }


def _script_json(value: dict[str, Any]) -> str:
    """Serialise data so it cannot terminate the surrounding script element."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>群星 · 离线指挥台</title>
<style>
:root {
  --bg: #040b16;
  --bg-2: #071426;
  --panel: rgba(9, 25, 46, 0.82);
  --panel-2: #0d2340;
  --line: #23466b;
  --line-bright: #3f6f9f;
  --text: #e8f1ff;
  --muted: #9fb4cc;
  --accent: #5cc8ff;
  --gold: #ffcc66;
  --shadow: 0 14px 36px rgba(0, 0, 0, 0.28);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  min-height: 100vh;
  color: var(--text);
  background:
    radial-gradient(circle at 8% 4%, rgba(40, 110, 180, 0.18), transparent 28%),
    radial-gradient(circle at 92% 0%, rgba(30, 80, 140, 0.16), transparent 30%),
    linear-gradient(180deg, #030912, var(--bg-2) 54%, #030912);
  background-attachment: fixed;
  font-family: system-ui, -apple-system, "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  line-height: 1.6;
}
button, textarea { font: inherit; }
a { color: var(--accent); }
.skip {
  position: absolute;
  left: -9999px;
  top: 12px;
  z-index: 10;
  padding: 8px 12px;
  background: var(--panel-2);
  border: 1px solid var(--gold);
  border-radius: 6px;
  color: var(--text);
}
.skip:focus { left: 12px; }
.top, main, footer { width: min(980px, 100%); margin: 0 auto; }
.top { padding: 34px 22px 10px; }
.brand { display: flex; align-items: center; gap: 12px; }
.mark {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  border: 1px solid var(--accent);
  transform: rotate(45deg);
  box-shadow: 0 0 14px rgba(92, 200, 255, 0.45);
}
h1 {
  margin: 0;
  font-size: clamp(1.45rem, 3.2vw, 2.1rem);
  font-weight: 650;
  letter-spacing: 0.045em;
}
.lead { margin: 16px 0 0; color: #d8ecff; font-size: clamp(1rem, 2vw, 1.12rem); }
.notice {
  margin: 9px 0 0;
  padding-left: 11px;
  border-left: 2px solid var(--gold);
  color: var(--muted);
  font-size: 0.92rem;
}
main { padding: 16px 22px 44px; }
section {
  margin-top: 22px;
  padding: 20px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
}
section.access { border-color: #2d587f; }
.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px 16px;
  flex-wrap: wrap;
}
h2 { margin: 0 0 12px; font-size: 1.06rem; letter-spacing: 0.035em; }
.preview-title { margin-top: 20px; }
.hint { color: var(--muted); font-size: 0.86rem; }
.field { display: flex; flex-direction: column; gap: 6px; margin-top: 16px; }
.field > span { color: #cfe6ff; font-size: 0.9rem; }
textarea {
  width: 100%;
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  background: #071426;
  resize: vertical;
}
textarea:focus-visible, button:focus-visible, a:focus-visible, summary:focus-visible {
  outline: 2px solid var(--gold);
  outline-offset: 2px;
}
.preview { min-height: 300px; line-height: 1.55; font-size: 0.94rem; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
button {
  padding: 10px 16px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  background: #122c4c;
  cursor: pointer;
}
button.primary {
  min-width: 210px;
  padding: 13px 22px;
  border-color: #3ba7e0;
  background: linear-gradient(180deg, #1d6fa8, #14517e);
  font-size: 1.02rem;
  font-weight: 700;
  box-shadow: 0 6px 18px rgba(29, 111, 168, 0.28);
}
button:disabled { opacity: 0.45; cursor: not-allowed; }
.link { font-size: 0.9rem; text-decoration: none; }
.status { min-height: 1.4em; margin: 10px 0 0; color: var(--gold); font-size: 0.92rem; }
.examples { margin: 10px 0 0; padding-left: 22px; color: #cfe6ff; }
.examples li { margin: 7px 0; }
.examples li::marker { color: var(--gold); }
.config {
  margin-top: 22px;
  padding: 0 18px 18px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
}
.config summary { padding: 14px 0; cursor: pointer; font-weight: 650; }
.config-body { display: grid; gap: 10px; }
.config-text { font-family: ui-monospace, Consolas, "Courier New", monospace; font-size: 0.82rem; }
.guide { margin: 16px 0 0; }
.empty { margin: 0; color: var(--muted); }
footer { padding: 0 22px 30px; color: var(--muted); font-size: 0.82rem; }
@media (hover: hover) and (pointer: fine) {
  button:hover:not(:disabled) { border-color: var(--accent); }
  .link:hover { text-decoration: underline; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<a class="skip" href="#main">跳到主要内容</a>
<header class="top">
  <div class="brand"><span class="mark" aria-hidden="true"></span><h1 id="page-title">群星 · 离线指挥台</h1></div>
  <p class="lead">首次在客户端接入 MCP，向同一个 Agent 发一次提示词；之后直接中文指挥。新对话没有保留上下文时再发一次。</p>
</header>
<main id="main">
  <section class="access" aria-labelledby="access-heading">
    <div class="section-head"><h2 id="access-heading">一次性接入提示词</h2><span class="hint" id="prompt-summary"></span></div>
    <p class="hint">复制一次发到同一个 Agent 对话；之后直接说中文命令。</p>
    <div class="actions">
      <button type="button" id="copy-prompt" class="primary">复制一次性接入提示词</button>
      <button type="button" id="download-prompt">下载提示词 txt</button>
      <a id="prompt-file-link" class="link" href="#" target="_blank" rel="noopener">打开同目录提示词文本</a>
    </div>
    <p id="status" class="status" role="status" aria-live="polite"></p>
    <label class="field" for="playstyle"><span>打法 / 偏好（可选）</span><textarea id="playstyle" rows="3" placeholder="例如：种田优先，矿物预算 800，不主动开战。"></textarea></label>
    <p class="hint" id="storage-note">打法 / 偏好只保存在此浏览器；清空浏览器数据会丢失。</p>
    <h2 class="preview-title">预览</h2>
    <p class="hint">纯文本；打法 / 偏好会附加在提示词末尾，作为这次对话的持续偏好。</p>
    <textarea id="preview" class="preview" readonly rows="16" aria-label="一次性接入提示词预览"></textarea>

  </section>
  <section aria-labelledby="examples-heading">
    <h2 id="examples-heading">之后可以直接这样说</h2>
    <p class="hint">这些只是对话示例，不会自动执行；同一个对话里直接发中文即可。</p>
    <ul class="examples">
      <li>看看现在帝国的情况，先别推进时间。</li>
      <li>在地球补一个研究实验室，预算 600 矿物。</li>
      <li>让第 3 舰队到目标星系待命，到了告诉我。</li>
      <li>保存检查点，然后退出。</li>
    </ul>
  </section>
  <details id="config-details" class="config">
    <summary>MCP 连接配置（首次由客户端接入）</summary>
    <div class="config-body">
      <p class="hint" id="server-line"></p>
      <p class="hint" id="campaign-line" hidden></p>
      <label class="field" for="config-text"><span id="config-file-label">mcp-connection.json</span><textarea id="config-text" class="config-text" readonly rows="10" spellcheck="false"></textarea></label>
      <div class="actions"><button type="button" id="copy-config">复制 MCP 配置</button></div>
    </div>
  </details>
  <p class="guide"><a id="guide-link" href="先看这里.md" target="_blank" rel="noopener">打开同目录使用说明</a></p>
</main>
<footer>离线提示词入口，游戏操作由已连接的 MCP 执行。</footer>
<script type="application/json" id="portal-data">__PORTAL_DATA__</script>
<script>
(function () {
  "use strict";

  var data;
  try {
    data = JSON.parse(document.getElementById("portal-data").textContent || "{}");
  } catch (error) {
    data = {};
  }

  function safeName(value, fallback) {
    var name = String(value == null ? "" : value);
    name = name.split(/[\\/]/).pop() || "";
    name = name.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "").trim();
    if (!name || name === "." || name === "..") {
      return fallback;
    }
    return name;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  var prompts = Array.isArray(data.prompts) ? data.prompts : [];
  var prompt = prompts.length ? prompts[0] : null;
  var serverName = typeof data.server_name === "string" ? data.server_name : "";
  var campaignDir = typeof data.campaign_dir === "string" ? data.campaign_dir : "";
  var configText = typeof data.config_text === "string" ? data.config_text : "";
  var configFile = safeName(data.config_file, "mcp-connection.json");
  var guideFile = safeName(data.guide_file, "先看这里.md");
  var title = typeof data.title === "string" && data.title.trim() ? data.title.trim() : "群星 · 离线指挥台";
  var STORAGE_KEY = "stellaris-agent-playstyle-v1";

  document.title = title;
  byId("page-title").textContent = title;

  var playstyle = byId("playstyle");
  var preview = byId("preview");
  var copyPrompt = byId("copy-prompt");
  var downloadPrompt = byId("download-prompt");
  var promptFileLink = byId("prompt-file-link");
  var status = byId("status");
  var storageNote = byId("storage-note");
  var configDetails = byId("config-details");
  var configTextArea = byId("config-text");
  var copyConfig = byId("copy-config");

  byId("config-file-label").textContent = configFile;
  byId("server-line").textContent = serverName ? "服务器：" + serverName : "服务器：未指定";
  if (campaignDir) {
    var campaignLine = byId("campaign-line");
    campaignLine.hidden = false;
    campaignLine.textContent = "战役目录：" + campaignDir;
  }
  configTextArea.value = configText;
  copyConfig.disabled = !configText.trim();
  byId("guide-link").textContent = "打开同目录使用说明：" + guideFile;
  byId("guide-link").href = guideFile;

  if (prompt) {
    byId("prompt-summary").textContent = [prompt.title, prompt.summary].filter(Boolean).join(" · ");
    promptFileLink.hidden = false;
    promptFileLink.textContent = "打开同目录提示词文本：" + safeName(prompt.filename, "给Agent的一次性提示词.txt");
    promptFileLink.href = safeName(prompt.filename, "给Agent的一次性提示词.txt");
  } else {
    byId("prompt-summary").textContent = "";
    promptFileLink.hidden = true;
    copyPrompt.disabled = true;
    downloadPrompt.disabled = true;
    status.textContent = "没有可用的接入提示词。请检查导出参数。";
  }

  try {
    var savedPlaystyle = window.localStorage.getItem(STORAGE_KEY);
    if (savedPlaystyle) {
      playstyle.value = savedPlaystyle;
    }
  } catch (error) {
    storageNote.textContent = "当前浏览器不允许本地保存；打法 / 偏好只在本页有效。";
  }

  function appendPlaystyle() {
    var style = playstyle.value.trim();
    if (!style) {
      return "";
    }
    return "\n\n---\n【我的持续偏好（仅此对话，之后直接下命令时也请遵守）】\n" + style;
  }

  function updatePreview() {
    if (!prompt) {
      preview.value = "";
      return;
    }
    preview.value = String(prompt.text || "") + appendPlaystyle();
    copyPrompt.disabled = !preview.value.trim();
    downloadPrompt.disabled = !preview.value.trim();
  }

  function setStatus(message) {
    status.textContent = message;
  }

  function legacyCopy(text, selectElement) {
    if (selectElement && typeof selectElement.select === "function") {
      try {
        selectElement.focus();
        selectElement.select();
      } catch (error) {
        // Selection is best effort; copy may still work.
      }
    }
    try {
      return document.execCommand("copy");
    } catch (error) {
      return false;
    }
  }

  async function copyText(text, successMessage, failureMessage, selectElement) {
    if (!text || !text.trim()) {
      setStatus("没有可复制的内容。");
      return false;
    }
    var copied = false;
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (error) {
        copied = false;
      }
    }
    if (!copied) {
      copied = legacyCopy(text, selectElement);
    }
    if (copied) {
      setStatus(successMessage);
      return true;
    }
    if (selectElement && typeof selectElement.select === "function") {
      try {
        selectElement.focus();
        selectElement.select();
      } catch (error) {
        // The failure message still tells the user what to do.
      }
    }
    setStatus(failureMessage);
    return false;
  }

  function flashButton(button, label) {
    if (!button.dataset.originalLabel) {
      button.dataset.originalLabel = button.textContent;
    }
    button.textContent = label;
    window.setTimeout(function () {
      button.textContent = button.dataset.originalLabel;
    }, 1200);
  }

  playstyle.addEventListener("input", function () {
    try {
      window.localStorage.setItem(STORAGE_KEY, playstyle.value);
    } catch (error) {
      storageNote.textContent = "当前浏览器不允许本地保存；打法 / 偏好只在本页有效。";
    }
    updatePreview();
  });

  copyPrompt.addEventListener("click", function () {
    copyText(
      preview.value,
      "已复制一次性接入提示词。",
      "复制失败，已选中提示词，请按 Ctrl+C 手动复制。",
      preview
    ).then(function (copied) {
      if (copied) {
        flashButton(copyPrompt, "已复制");
      }
    });
  });

  copyConfig.addEventListener("click", function () {
    configDetails.open = true;
    copyText(
      configTextArea.value,
      "已复制 MCP 配置。",
      "复制失败，已选中配置文本，请按 Ctrl+C 手动复制。",
      configTextArea
    ).then(function (copied) {
      if (copied) {
        flashButton(copyConfig, "已复制");
      }
    });
  });

  downloadPrompt.addEventListener("click", function () {
    if (!prompt) {
      setStatus("没有可下载的提示词。");
      return;
    }
    var filename = safeName(prompt.filename, "给Agent的一次性提示词.txt");
    try {
      var blob = new Blob([preview.value], { type: "text/plain;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 0);
      setStatus("已生成下载：" + filename);
    } catch (error) {
      setStatus("下载失败，请手动复制预览文本。");
    }
  });

  updatePreview();
})();
</script>
</body>
</html>
"""


def render_portal(payload: dict) -> str:
    """Return a complete offline HTML page for the supplied portal payload.

    The result embeds all data as escaped JSON and renders text through
    textContent/value in the browser. The function does not perform I/O.
    """
    data = _normalise(payload)
    return _HTML_TEMPLATE.replace("__PORTAL_DATA__", _script_json(data))
