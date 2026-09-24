# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "AI Assistant"
# description = "Chat with an external, OpenAI-compatible LLM (OpenAI, Anthropic via proxy, Ollama, vLLM, ...) for print-settings suggestions on the current project."
# author = "OrcaSlicer"
# version = "0.1.0"
# ///
"""AI Assistant -- a chat panel that asks an external LLM to review the current
project and suggest OrcaSlicer settings changes.

Everything runs in-process: context comes straight from `orca.host` (read-only),
the HTTP call goes out from this plugin's own Python code, and the panel is an
`orca.host.ui` dock panel, same as the Dock Panel Demo. There is no separate
server or middleware -- the "middleware" is just this file.

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
"""


def storage_path(filename):
    return os.path.join(orca.host.plugin.storage(), filename)


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(storage_path("config.json"), "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return settings


def save_settings(settings):
    with open(storage_path("config.json"), "w", encoding="utf-8") as f:
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


def call_llm(settings, context):
    body = json.dumps({
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"

    request = urllib.request.Request(settings["endpoint"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


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
  #gear { margin-left: auto; background: transparent; color: var(--orca-fg);
          border-color: var(--orca-border); }
  #settings { display: none; margin-top: 8px; gap: 6px; flex-direction: column; }
  #settings input { width: 100%; box-sizing: border-box; }
  #settings.open { display: flex; }
  #log { flex: 1; overflow-y: auto; padding: 10px; }
  .msg { margin-bottom: 10px; white-space: pre-wrap; }
  .msg.user { color: var(--orca-fg); }
  .msg.assistant { color: var(--orca-fg); }
  .msg.error { color: #d9534f; }
  .card { border: 1px solid var(--orca-border); border-radius: 6px; padding: 8px 10px; margin: 6px 0; }
  .card .k { font-family: monospace; }
  .card .reason { color: var(--orca-muted); font-size: 12px; }
  footer { flex: none; display: flex; gap: 6px; padding: 8px 10px; border-top: 1px solid var(--orca-border); }
  footer input { flex: 1; }
  .warn { color: #b8860b; font-size: 12px; }
  .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
  .actions button.quiet { background: transparent; color: var(--orca-fg); border-color: var(--orca-border); }
</style>

<header>
  <div class="row">
    <b>AI Assistant</b>
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
<footer>
  <input id="input" placeholder="Ask about this project's settings...">
  <button id="send">Send</button>
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
  function renderSuggestions(data) {
    var log = document.getElementById("log");
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
      document.getElementById("send").disabled = !!message.value;
      document.getElementById("apply").disabled = !!message.value;
      document.getElementById("export").disabled = !!message.value;
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
  document.getElementById("send").addEventListener("click", function () {
    var input = document.getElementById("input");
    var text = input.value.trim();
    if (!text) return;
    addMessage("user", text);
    orca.postMessage({ command: "chat", text: text });
    input.value = "";
  });
  document.getElementById("input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") document.getElementById("send").click();
  });
  document.getElementById("apply").addEventListener("click", function () {
    orca.postMessage({ command: "apply" });
  });
  document.getElementById("export").addEventListener("click", function () {
    orca.postMessage({ command: "export" });
  });

  orca.postMessage({ command: "get_settings" });
})();
"""


class AiAssistant(orca.script.ScriptPluginCapabilityBase):
    panel = None
    settings = None

    def get_name(self):
        return "AI Assistant"

    def execute(self):
        self.settings = load_settings()
        if self.panel is not None and self.panel.is_open():
            self.panel.show()
            return orca.ExecutionResult.success("AI Assistant is already open.")
        self.panel = orca.host.ui.create_dock_panel(
            html=PAGE,
            title="AI Assistant",
            width=360,
            height=560,
            on_message=self.on_message,
            on_close=self.on_close,
            dock="right",
        )
        return orca.ExecutionResult.success("AI Assistant opened.")

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
            threading.Thread(target=self.run_chat, args=(message.get("text", ""),), daemon=True).start()
        elif command == "apply":
            threading.Thread(target=self.run_apply, daemon=True).start()
        elif command == "export":
            threading.Thread(target=self.run_export, daemon=True).start()

    def run_chat(self, text):
        self.panel.post({"command": "busy", "value": True})
        try:
            context = build_context(text)
            raw = call_llm(self.settings, context)
            reply = parse_reply(raw, set(context["known_keys"]))
            self._last_suggestions = reply["suggestions"]
            self.panel.post({"command": "suggestions", **reply})
        except urllib.error.URLError as exc:
            self.panel.post({"command": "message", "role": "error", "text": f"Request failed: {exc}"})
        except Exception as exc:
            self.panel.post({"command": "message", "role": "error", "text": f"Error: {exc}"})
        finally:
            self.panel.post({"command": "busy", "value": False})

    def run_apply(self):
        suggestions = getattr(self, "_last_suggestions", [])
        if not suggestions:
            return
        self.panel.post({"command": "busy", "value": True})
        try:
            bundle = orca.host.preset_bundle()
            if not hasattr(bundle, "apply_config"):
                self.panel.post({"command": "message", "role": "error",
                                 "text": "This OrcaSlicer build has no write API yet -- use Export instead."})
                return
            try:
                applied = bundle.apply_config({item["key"]: item["value"] for item in suggestions})
                self.panel.post({"command": "applied", "keys": applied})
            except RuntimeError as exc:
                self.panel.post({"command": "message", "role": "error", "text": str(exc)})
        finally:
            self.panel.post({"command": "busy", "value": False})

    def run_export(self):
        suggestions = getattr(self, "_last_suggestions", [])
        self.panel.post({"command": "busy", "value": True})
        try:
            path = storage_path("suggested_settings.ini")
            with open(path, "w", encoding="utf-8") as f:
                f.write("; generated by AI Assistant -- File > Import Config to apply\n")
                for item in suggestions:
                    f.write(f"{item['key']} = {item['value']}\n")
            self.panel.post({"command": "exported", "path": path})
        finally:
            self.panel.post({"command": "busy", "value": False})

    def on_close(self):
        self.panel = None


@orca.plugin
class AiAssistantPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(AiAssistant)
