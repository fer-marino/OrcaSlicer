# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "AI Assistant"
# description = "Chat with an external, OpenAI-compatible LLM (OpenAI, Anthropic via proxy, Ollama, vLLM, ...) for print-settings suggestions on the current project."
# author = "OrcaSlicer"
# version = "0.1.0"
# ///
"""AI Assistant -- a chat window that asks an external LLM to review the current
project and suggest OrcaSlicer settings changes.

Everything runs in-process: context comes straight from `orca.host` (read-only),
the HTTP call goes out from this plugin's own Python code, and the UI is a plain
`orca.host.ui.create_window()` window (non-modal), same as the Inspector plugin's
main window -- this build has no docked-panel API, only floating windows. There
is no separate server or middleware -- the "middleware" is just this file.

  page   --orca.postMessage({command:'chat', text})-->          plugin.on_message()
  page   --orca.postMessage({command:'save_settings', ...})-->      "
  page   --orca.postMessage({command:'apply'})-->                   "
  page   --orca.postMessage({command:'export'})-->                  "
  plugin --panel.post({command:'settings'|'message'|'suggestions'|'busy'|'applied'|'exported', ...})--> page

"Apply suggestions" calls `orca.host.preset_bundle().apply_config({key: value, ...})`,
which requires the PluginHostPresets.cpp write bindings (see PluginBindingUtils.hpp's
run_on_ui_blocking). It shows its own native Yes/No confirmation before touching
anything -- this is a plain pybind11 call, not a CPython audit event, so it is
not gated by PluginAuditManager the way filesystem/network access is. It marks
the affected presets dirty but does not save them. Declining, an unknown key, or
a value that fails to parse raises, and nothing is applied.

On an OrcaSlicer build without that binding (hasattr check), "Export .ini instead"
writes a plain `key = value` file into this plugin's storage folder that the user
imports via the existing File > Import Config flow.

The LLM call happens on a worker thread (on_message runs on the UI thread) and
results come back via panel.post(), same pattern as the Inspector plugin's
progress demos.
"""
import json
import math
import os
import threading
import urllib.error
import urllib.request

import orca

DEFAULT_SETTINGS = {
    "endpoint": "http://localhost:11434/v1/chat/completions",  # Ollama's OpenAI-compatible route
    "api_key": "",
    "model": "llama3.1",
}

# Config keys worth surfacing even when the user doesn't ask about them by name.
# Not exhaustive -- just enough for the model to reason about the common tradeoffs
# (strength vs. speed vs. surface quality) without shipping the entire config table.
HIGHLIGHT_KEYS = [
    "printer_model", "nozzle_diameter", "printable_height",
    "layer_height", "initial_layer_print_height",
    "wall_loops", "top_shell_layers", "bottom_shell_layers",
    "sparse_infill_density", "sparse_infill_pattern",
    "enable_support", "support_type", "support_threshold_angle", "raft_layers",
    "enable_prime_tower", "seam_position",
    "filament_type", "filament_colour",
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "bed_temperature", "hot_plate_temp_initial_layer",
    "fan_min_speed", "fan_max_speed", "overhang_fan_speed",
    "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "bridge_speed",
    "brim_type", "brim_width",
]

SYSTEM_PROMPT = """You are an OrcaSlicer print-settings assistant embedded in the slicer itself.
You receive a JSON "context" describing the current project (model geometry, active \
printer/filament/process presets, and a set of highlighted config values) plus a user request.

Reply with ONLY a single JSON object, no prose outside it, matching this shape:
{
  "analysis": "one or two sentences on the tradeoff you're making",
  "suggestions": [{"key": "<exact OrcaSlicer config key>", "value": "<new value as a string>", "reason": "<why>"}],
  "warnings": ["<anything the user should double check, e.g. an assumption you had to make>"]
}
Only use keys that appear in context.known_keys -- never invent a key name. If you are not
confident a change is needed, return an empty "suggestions" list rather than guessing.

If the configured filaments look like placeholders or leftovers rather than a deliberate choice
for this model (e.g. filament types/colors that don't match what the model appears to need, or an
AMS-sized filament list on a project that doesn't call for it), say so in "warnings" and mention
the sync button (the ↻ icon next to the gear icon) that pulls the real filament list from the
connected printer's AMS -- don't guess at what the "right" filaments would be instead.
"""


def storage_path(filename):
    return os.path.join(orca.host.plugin.storage(), filename)


SETTINGS_FILENAME = "settings.json"  # not "config.json": PluginAuditManager denies any path
                                      # containing "conf" outright, even inside a plugin's own
                                      # storage folder (see its default_denied_path_keywords()).


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(storage_path(SETTINGS_FILENAME), "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return settings


def save_settings(settings):
    with open(storage_path(SETTINGS_FILENAME), "w", encoding="utf-8") as f:
        json.dump(settings, f)


def config_highlights(cfg):
    rows = {}
    for key in HIGHLIGHT_KEYS:
        value = cfg(key)
        if value is not None:
            rows[key] = str(value)
    return rows


def build_context(user_text):
    model = orca.host.model()
    bundle = orca.host.preset_bundle()
    cfg = bundle.full_config_value
    objects = model.objects()

    bbox = model.bounding_box()
    printer = bundle.current_printer_preset()
    process = bundle.current_process_preset()

    return {
        "request": user_text,
        "model": {
            "object_count": len(objects),
            "printable_height": cfg("printable_height"),
            "bbox_size": list(bbox.size) if bbox.defined else None,
            "manifold": all(
                vol.is_manifold()
                for obj in objects
                for vol in (obj.volume(i) for i in range(obj.volume_count()))
            ) if objects else True,
            "objects": [
                {
                    "name": obj.name or "(unnamed)",
                    "volumes": obj.volume_count(),
                    "instances": obj.instance_count(),
                    "overrides": {k: str(obj.config_value(k)) for k in obj.config_keys()},
                }
                for obj in objects[:20]
            ],
        },
        "setup": {
            "printer": printer.name,
            "process": process.name,
            "filaments": [p.name for p in bundle.current_filament_presets() if p],
        },
        "highlights": config_highlights(cfg),
        "known_keys": sorted(bundle.full_config_keys()),
    }


def _post_json(endpoint, body_obj, settings, timeout):
    body = json.dumps(body_obj).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def call_llm(settings, context):
    # Local models (especially "thinking" ones that emit a reasoning_content chain before
    # the final answer) can take much longer than a hosted API on a real analytical prompt
    # with full project context, vs. the near-instant reply to small talk like "hi". 300s
    # wasn't enough in practice on local hardware; 600s gives real analytical prompts room
    # without waiting forever on a truly stuck request.
    payload = _post_json(settings["endpoint"], {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }, settings, timeout=600)
    message = payload["choices"][0]["message"]
    # Not a streaming call, so there's no live token-by-token progress -- but the full
    # response already carries the model's reasoning trace (if any) and token counts,
    # both otherwise discarded. Surfacing them after the fact costs nothing extra.
    return {
        "content": message["content"],
        "reasoning": message.get("reasoning_content") or "",
        "usage": payload.get("usage") or {},
    }


RELEVANT_KEYS_TOP_K = 60


def _cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def call_embeddings(settings, texts):
    # OpenAI-compatible convention: the embeddings route sits next to chat/completions
    # under the same /v1 prefix. Only meaningful for servers that actually implement it --
    # callers must treat a failure here as "no embeddings available", never fatal.
    endpoint = settings["endpoint"].rsplit("/chat/completions", 1)[0] + "/embeddings"
    payload = _post_json(endpoint, {"model": settings["model"], "input": texts}, settings, timeout=30)
    return [item["embedding"] for item in payload["data"]]


def relevant_keys(settings, user_text, known_keys, cache):
    # Trims the full (several-hundred-key) config key list down to the ones semantically
    # closest to the request, so the prompt -- and the local model's job of reading it --
    # stays a fraction of the size. `cache` is the plugin window's own dict, keyed by the
    # raw key string (which doesn't change during a session), so only the query itself needs
    # embedding on repeat requests.
    #
    # Best-effort: any failure (server has no /v1/embeddings, wrong model, network hiccup)
    # falls back to the untrimmed list rather than blocking the actual chat request on it.
    if len(known_keys) <= RELEVANT_KEYS_TOP_K:
        return known_keys
    try:
        missing = [key for key in known_keys if key not in cache]
        if missing:
            for key, vector in zip(missing, call_embeddings(settings, missing)):
                cache[key] = vector
        query_vector = call_embeddings(settings, [user_text])[0]
        ranked = sorted(known_keys, key=lambda key: _cosine_similarity(cache[key], query_vector), reverse=True)
        return ranked[:RELEVANT_KEYS_TOP_K]
    except Exception:
        return known_keys


def parse_reply(raw_text, known_keys):
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"analysis": raw_text, "suggestions": [], "warnings": ["Model did not return valid JSON."]}

    if not isinstance(parsed, dict):
        return {"analysis": raw_text, "suggestions": [], "warnings": ["Model returned JSON that wasn't an object."]}

    suggestions, warnings = [], list(parsed.get("warnings", []))
    for item in parsed.get("suggestions", []):
        if not isinstance(item, dict):
            warnings.append(f"malformed suggestion {item!r} ignored")
            continue
        key = item.get("key", "")
        if key in known_keys:
            suggestions.append(item)
        else:
            warnings.append(f"unknown key {key!r} ignored")
    return {"analysis": parsed.get("analysis", ""), "suggestions": suggestions, "warnings": warnings}


PAGE = r"""
<style>
  body { margin: 0; padding: 0; display: flex; flex-direction: column; height: 100vh;
         font-size: 13px; }
  header { flex: none; padding: 8px 10px; border-bottom: 1px solid var(--orca-border); }
  header .row { display: flex; gap: 6px; align-items: center; }
  #sync-filaments { margin-left: auto; background: transparent; color: var(--orca-fg);
          border-color: var(--orca-border); }
  #gear { background: transparent; color: var(--orca-fg);
          border-color: var(--orca-border); }
  #settings { display: none; margin-top: 8px; gap: 6px; flex-direction: column; }
  #settings input { width: 100%; box-sizing: border-box; }
  #settings.open { display: flex; }
  #log { flex: 1; overflow-y: auto; padding: 10px; }
  .msg { margin-bottom: 10px; white-space: pre-wrap; }
  .msg.user { color: var(--orca-fg); }
  .msg.assistant { color: var(--orca-fg); }
  .msg.error { color: #d9534f; }
  .msg.thinking { color: var(--orca-muted); font-style: italic; }
  details.thoughts { margin-bottom: 10px; }
  details.thoughts summary { cursor: pointer; color: var(--orca-muted); font-size: 12px; }
  details.thoughts .thoughts-body { white-space: pre-wrap; color: var(--orca-muted); font-size: 12px;
    border-left: 2px solid var(--orca-border); padding-left: 8px; margin-top: 6px; }
  .usage { color: var(--orca-muted); font-size: 11px; margin: -4px 0 10px; }
  .card { border: 1px solid var(--orca-border); border-radius: 6px; padding: 8px 10px; margin: 6px 0; }
  .card .k { font-family: monospace; }
  .card .reason { color: var(--orca-muted); font-size: 12px; }
  footer { flex: none; display: flex; gap: 6px; padding: 8px 10px; border-top: 1px solid var(--orca-border); }
  footer input { flex: 1; }
  .warn { color: #b8860b; font-size: 12px; }
  .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
  button.quiet { background: transparent; color: var(--orca-fg); border-color: var(--orca-border); }
  #presets { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 10px 10px; flex: none; }
  #presets .chip { background: transparent; color: var(--orca-fg); border-color: var(--orca-border); font-size: 11px; padding: 4px 10px; }
  #progress-bar { display: none; flex: none; height: 3px; margin: 0 10px 8px; background: var(--orca-border);
                  border-radius: 2px; overflow: hidden; }
  #progress-bar.active { display: block; }
  #progress-bar .fill { width: 30%; height: 100%; background: var(--orca-accent); border-radius: 2px;
                         animation: orca-ai-progress-slide 1.1s ease-in-out infinite; }
  @keyframes orca-ai-progress-slide {
    0% { margin-left: -30%; }
    100% { margin-left: 100%; }
  }
</style>

<header>
  <div class="row">
    <b>AI Assistant</b>
    <button id="sync-filaments" title="Sync filaments from the connected printer's AMS">&#8635;</button>
    <button id="gear" title="Settings">&#9881;</button>
  </div>
  <div id="settings">
    <input id="endpoint" placeholder="Chat completions endpoint (OpenAI-compatible)">
    <input id="model" placeholder="Model name">
    <input id="api_key" placeholder="API key (optional)" type="password">
    <button id="save">Save</button>
  </div>
</header>
<div id="log"></div>
<div class="actions" id="suggestion-actions" style="display:none">
  <button id="apply">Apply suggestions</button>
  <button id="export" class="quiet">Export .ini instead</button>
</div>
<div id="presets">
  <button class="chip" data-q="What print settings should I check for this model?">General check</button>
  <button class="chip" data-q="Optimize this for print speed.">Faster printing</button>
  <button class="chip" data-q="Optimize this for part strength.">Stronger part</button>
  <button class="chip" data-q="Optimize this for surface quality.">Better surface</button>
  <button class="chip" data-q="Optimize this to use less material.">Less material</button>
  <button class="chip" data-q="This model has overhangs -- what support settings should I use?">Overhangs/support</button>
</div>
<div id="progress-bar"><div class="fill"></div></div>
<footer>
  <input id="input" placeholder="Ask about this project's settings...">
  <button id="send">Send</button>
  <button id="cancel" class="quiet" style="display:none">Cancel</button>
</footer>

<script>
(function () {
  var lastSuggestions = [];

  function esc(s) {
    var span = document.createElement("span");
    span.textContent = s == null ? "" : String(s);
    return span.innerHTML;
  }
  function addMessage(role, text) {
    var div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    document.getElementById("log").appendChild(div);
    div.scrollIntoView();
  }
  // "Thinking... Ns" status row shown while a request is in flight (busy=true) --
  // pure elapsed-time feedback, not real token counts: call_llm() isn't a streaming
  // request, so there's no token-by-token progress to report.
  var thinkingEl = null;
  var thinkingTimer = null;
  var thinkingStart = 0;
  function showThinking() {
    hideThinking();
    thinkingEl = document.createElement("div");
    thinkingEl.className = "msg thinking";
    thinkingEl.textContent = "Thinking… 0s";
    document.getElementById("log").appendChild(thinkingEl);
    thinkingEl.scrollIntoView();
    document.getElementById("progress-bar").classList.add("active");
    thinkingStart = Date.now();
    thinkingTimer = setInterval(function () {
      var secs = Math.floor((Date.now() - thinkingStart) / 1000);
      thinkingEl.textContent = "Thinking… " + secs + "s";
    }, 250);
  }
  function hideThinking() {
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
    document.getElementById("progress-bar").classList.remove("active");
  }

  function renderSuggestions(data) {
    var log = document.getElementById("log");
    if (data.reasoning) {
      var details = document.createElement("details");
      details.className = "thoughts";
      var summary = document.createElement("summary");
      summary.textContent = "Show thinking";
      details.appendChild(summary);
      var body = document.createElement("div");
      body.className = "thoughts-body";
      body.textContent = data.reasoning;
      details.appendChild(body);
      log.appendChild(details);
    }
    if (data.analysis) addMessage("assistant", data.analysis);
    data.suggestions.forEach(function (s) {
      var div = document.createElement("div");
      div.className = "card";
      div.innerHTML = "<span class=\"k\">" + esc(s.key) + "</span> = " + esc(s.value) +
        "<div class=\"reason\">" + esc(s.reason || "") + "</div>";
      log.appendChild(div);
    });
    (data.warnings || []).forEach(function (w) {
      var div = document.createElement("div");
      div.className = "warn";
      div.textContent = "⚠ " + w;
      log.appendChild(div);
    });
    var usage = data.usage || {};
    if (usage.prompt_tokens != null || usage.completion_tokens != null) {
      var parts = [];
      if (usage.prompt_tokens != null) parts.push(usage.prompt_tokens + " prompt");
      if (usage.completion_tokens != null) parts.push(usage.completion_tokens + " completion");
      if (usage.total_tokens != null) parts.push(usage.total_tokens + " total");
      var usageEl = document.createElement("div");
      usageEl.className = "usage";
      usageEl.textContent = parts.join(" · ") + " tokens";
      log.appendChild(usageEl);
    }
    log.scrollTop = log.scrollHeight;
    lastSuggestions = data.suggestions;
    document.getElementById("suggestion-actions").style.display = lastSuggestions.length ? "flex" : "none";
  }

  orca.onMessage(function (message) {
    if (!message) return;
    if (message.command === "settings") {
      document.getElementById("endpoint").value = message.endpoint || "";
      document.getElementById("model").value = message.model || "";
      document.getElementById("api_key").value = message.api_key_set ? "********" : "";
    } else if (message.command === "message") {
      addMessage(message.role, message.text);
    } else if (message.command === "suggestions") {
      renderSuggestions(message);
    } else if (message.command === "busy") {
      var isBusy = !!message.value;
      document.getElementById("send").disabled = isBusy;
      document.getElementById("apply").disabled = isBusy;
      document.getElementById("export").disabled = isBusy;
      document.getElementById("input").disabled = isBusy;
      document.getElementById("sync-filaments").disabled = isBusy;
      document.querySelectorAll("#presets .chip").forEach(function (chip) { chip.disabled = isBusy; });
      document.getElementById("cancel").style.display = isBusy ? "" : "none";
      if (isBusy) showThinking(); else hideThinking();
    } else if (message.command === "applied") {
      addMessage("assistant", "Applied: " + message.keys.join(", ") +
        ". The affected presets are now marked modified -- save them if you want to keep the change.");
      document.getElementById("suggestion-actions").style.display = "none";
    } else if (message.command === "exported") {
      addMessage("assistant", "Wrote " + message.path + " -- import it via File > Import Config.");
    }
  });

  document.getElementById("gear").addEventListener("click", function () {
    document.getElementById("settings").classList.toggle("open");
  });
  document.getElementById("sync-filaments").addEventListener("click", function () {
    orca.postMessage({ command: "sync_filaments" });
  });
  document.getElementById("save").addEventListener("click", function () {
    var key = document.getElementById("api_key").value;
    orca.postMessage({
      command: "save_settings",
      endpoint: document.getElementById("endpoint").value,
      model: document.getElementById("model").value,
      api_key: key === "********" ? null : key,
    });
    document.getElementById("settings").classList.remove("open");
  });
  function sendChat(text) {
    text = text.trim();
    if (!text) return;
    document.getElementById("presets").style.display = "none";
    addMessage("user", text);
    orca.postMessage({ command: "chat", text: text });
  }
  document.getElementById("send").addEventListener("click", function () {
    var input = document.getElementById("input");
    sendChat(input.value);
    input.value = "";
  });
  document.getElementById("input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") document.getElementById("send").click();
  });
  document.querySelectorAll("#presets .chip").forEach(function (chip) {
    chip.addEventListener("click", function () { sendChat(chip.dataset.q); });
  });
  document.getElementById("apply").addEventListener("click", function () {
    orca.postMessage({ command: "apply" });
  });
  document.getElementById("export").addEventListener("click", function () {
    orca.postMessage({ command: "export" });
  });
  document.getElementById("cancel").addEventListener("click", function () {
    orca.postMessage({ command: "cancel" });
  });

  orca.postMessage({ command: "get_settings" });
})();
</script>
"""


class AiAssistant(orca.script.ScriptPluginCapabilityBase):
    panel = None
    settings = None

    def get_name(self):
        return "AI Assistant"

    def execute(self):
        self.settings = load_settings()
        self._key_embedding_cache = {}
        self._cancel_token = 0
        self._busy = False
        # Capability objects are instantiated once per plugin load, so a second Run lands on
        # the same instance -- close any previous window first (create_window has no
        # show()/focus(), so bringing an existing window forward isn't possible; matches the
        # Inspector plugin's own execute() pattern).
        if self.panel is not None and self.panel.is_open():
            self.panel.close()
        self.panel = orca.host.ui.create_window(
            html=PAGE,
            title="AI Assistant",
            width=520,
            height=680,
            on_message=self.on_message,
            on_close=self.on_close,
        )
        return orca.ExecutionResult.success("AI Assistant opened.")

    # Runs `target(*args)` on a worker thread, guarded so only one such job can be in
    # flight at a time (chat/apply/export/sync_filaments all go through this). Handles the
    # busy:true/false round trip itself so each target function only needs its own logic.
    # Cancelling doesn't abort the underlying network call (urllib has no clean way to do
    # that from another thread) -- it just bumps the token so the stale result is dropped
    # and the UI is freed up immediately; the request may still finish in the background.
    def _run_in_background(self, target, *args):
        if self._busy:
            self.panel.post({"command": "message", "role": "error",
                             "text": "Still working on the previous request -- please wait or cancel it."})
            return
        self._busy = True
        token = self._cancel_token
        self.panel.post({"command": "busy", "value": True})

        def worker():
            try:
                target(token, *args)
            finally:
                if token == self._cancel_token:
                    self._busy = False
                    self.panel.post({"command": "busy", "value": False})

        threading.Thread(target=worker, daemon=True).start()

    def on_message(self, message):
        command = (message or {}).get("command")
        if command == "get_settings":
            self.panel.post({
                "command": "settings",
                "endpoint": self.settings["endpoint"],
                "model": self.settings["model"],
                "api_key_set": bool(self.settings.get("api_key")),
            })
        elif command == "save_settings":
            if message.get("endpoint"):
                self.settings["endpoint"] = message["endpoint"]
            if message.get("model"):
                self.settings["model"] = message["model"]
            if message.get("api_key") is not None:
                self.settings["api_key"] = message["api_key"]
            save_settings(self.settings)
        elif command == "chat":
            self._run_in_background(self.run_chat, message.get("text", ""))
        elif command == "apply":
            self._run_in_background(self.run_apply)
        elif command == "export":
            self._run_in_background(self.run_export)
        elif command == "sync_filaments":
            self._run_in_background(self.run_sync_filaments)
        elif command == "cancel":
            # Doesn't stop the underlying network call (see _run_in_background) -- just
            # frees the UI and marks whatever comes back from it as stale.
            self._cancel_token += 1
            self._busy = False
            self.panel.post({"command": "busy", "value": False})
            self.panel.post({"command": "message", "role": "assistant", "text": "Cancelled."})

    def run_sync_filaments(self, token):
        try:
            orca.host.plater().sync_ams_filaments()
            if token != self._cancel_token:
                return
            self.panel.post({"command": "message", "role": "assistant",
                             "text": "Requested a filament sync from the connected printer's AMS. "
                                     "If no printer is connected, OrcaSlicer will have shown its own "
                                     "\"not connected\" prompt."})
        except AttributeError:
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error",
                                 "text": "This OrcaSlicer build has no AMS sync API yet."})
        except Exception as exc:
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error", "text": f"Sync failed: {exc}"})

    def run_chat(self, token, text):
        try:
            context = build_context(text)
            context["known_keys"] = relevant_keys(self.settings, text, context["known_keys"],
                                                   self._key_embedding_cache)
            llm_result = call_llm(self.settings, context)
            reply = parse_reply(llm_result["content"], set(context["known_keys"]))
            reply["reasoning"] = llm_result["reasoning"]
            reply["usage"] = llm_result["usage"]
            if token != self._cancel_token:
                return
            self._last_suggestions = reply["suggestions"]
            self.panel.post({"command": "suggestions", **reply})
        except urllib.error.URLError as exc:
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error", "text": f"Request failed: {exc}"})
        except Exception as exc:
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error", "text": f"Error: {exc}"})

    def run_apply(self, token):
        suggestions = getattr(self, "_last_suggestions", [])
        if not suggestions:
            return
        bundle = orca.host.preset_bundle()
        if not hasattr(bundle, "apply_config"):
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error",
                                 "text": "This OrcaSlicer build has no write API yet -- use Export instead."})
            return
        try:
            applied = bundle.apply_config({item["key"]: item["value"] for item in suggestions})
            if token == self._cancel_token:
                self.panel.post({"command": "applied", "keys": applied})
        except RuntimeError as exc:
            if token == self._cancel_token:
                self.panel.post({"command": "message", "role": "error", "text": str(exc)})

    def run_export(self, token):
        suggestions = getattr(self, "_last_suggestions", [])
        path = storage_path("suggested_settings.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write("; generated by AI Assistant -- File > Import Config to apply\n")
            for item in suggestions:
                f.write(f"{item['key']} = {item['value']}\n")
        if token == self._cancel_token:
            self.panel.post({"command": "exported", "path": path})

    def on_close(self):
        # Deliberately doesn't touch self.panel: execute() already asks self.panel.is_open()
        # (a live query, not a cached flag) before deciding whether to close/reopen. Setting
        # self.panel = None here raced with execute() -- if a new window was already assigned
        # to self.panel by the time this stale callback (from the *previous* window's async
        # close) ran, it wiped out the new, valid handle, and every later self.panel.post(...)
        # in this run (on_message, run_chat, run_apply, run_export) raised AttributeError,
        # silently swallowed and logged by the host bridge instead of reaching the page.
        pass


@orca.plugin
class AiAssistantPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(AiAssistant)
