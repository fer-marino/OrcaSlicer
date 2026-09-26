# AI Assistant plugin

`sandboxes/orca_ai_assistant_plugin_any.py` is a `Script` capability plugin: a chat window
that sends the current project's context to an external OpenAI-compatible LLM endpoint and
turns the reply into print-setting suggestions the user can apply or export. Everything runs
in-process -- the HTTP call is the plugin's own Python code, there is no separate server.

## Setup

1. File > Plugins > Install from file, pick `orca_ai_assistant_plugin_any.py`.
2. Run it (the Plugins dialog's run icon, or Speed Dial -> "AI Assistant").
3. Click the gear icon and set:
   - **Chat completions endpoint**: any OpenAI-compatible `/v1/chat/completions` URL
     (Ollama, vLLM, llama.cpp server, a local model host such as Unsloth Studio's API server).
   - **Model name**.
   - **API key** (optional, sent as `Authorization: Bearer ...` when set).

## Features

- **Chat**: free-form questions about the current project's settings, plus quick-start
  preset chips for common asks (speed / strength / surface quality / material / support).
- **Suggestions**: rendered as `key = value` cards with a reason. **Apply** writes them via
  `PresetBundle.apply_config()` (marks the affected presets dirty; does not save them) and
  shows its own confirmation dialog first. **Export** writes a plain `.ini` into the plugin's
  storage folder for `File > Import Config`, for OrcaSlicer builds without the write API
  (checked via `hasattr`).
- **Sync filaments** (the &#8635; icon): pulls the real filament list from a connected
  printer's AMS via `orca.host.plater().sync_ams_filaments()`. Requires an actual
  paired/connected device, not just a selected printer profile -- with nothing connected it
  quietly does nothing, matching the sidebar's own sync button.
- **Progress feedback**: an animated bar and a "Thinking... Ns" counter while a request is in
  flight. Once a reply arrives, a collapsed "Show thinking" section reveals the model's
  `reasoning_content` (if the server returned one), and a small caption shows prompt/completion
  token counts -- both are whatever the final response carried, not live streaming.
- **Cancel**: frees the UI immediately and marks the in-flight request stale so its result is
  dropped on arrival. It cannot abort the underlying socket read (Python's `urllib` has no
  clean way to interrupt a blocking call from another thread), so the request may still run to
  completion or timeout in the background.
- **Relevant-key retrieval**: the full config key list (`known_keys`, several hundred entries)
  is trimmed to the ~60 keys most semantically similar to the request before it's sent, via
  the server's `/v1/embeddings` route (corpus embeddings are cached per window; only the query
  needs embedding on repeat requests). Falls back to the untrimmed list if that route doesn't
  exist, gives a wrong shape, or the call otherwise fails -- this is a latency optimization,
  never a hard requirement.

## Known limitations

- Local "thinking" models can take several minutes to answer a real analytical prompt with
  full project context (vs. near-instant small talk); the request timeout is 600s.
- Embedding-based key trimming needs `/v1/embeddings` on the same base URL as the chat
  endpoint; servers without it just see the full key list (slower, not broken).
- One request at a time per window: chat/apply/export/sync all share a single busy flag.

## Gotchas for plugin authors

- **Don't name a plugin's own file `config.json`** (or anything containing "conf"). Every
  audited filesystem path is denied outright if the file/directory name contains `"conf"`, even
  inside the plugin's own storage folder (`PluginAuditManager::default_denied_path_keywords()`,
  see `PluginAuditManager.cpp`) -- this plugin's settings file is `settings.json` instead.
- **A `create_window()` capability shouldn't clear its own panel handle in `on_close()`.**
  `execute()` re-runs on the same capability instance across repeated launches, and closing the
  previous window before opening a new one is asynchronous: a stale `on_close` for the *old*
  window can fire after `self.panel` has already been reassigned to the *new* one. Query
  `self.panel.is_open()` (a live check) instead of tracking open/closed state in Python.

## Plugin-system changes this plugin exercised

### Audit identity across `threading.Thread`

`PluginAuditManager`'s current-plugin/current-capability context is `thread_local`, set only
while the host calls directly into a plugin capability's callback. A callback that spawns a
`threading.Thread` for its own long-running work (this plugin does, for every LLM call) runs
that thread on a fresh OS thread whose `thread_local` starts empty. Without help, the CPython
audit hook's "no plugin context" early-out silently waives every fs/network/process permission
check for code running there, and host APIs that read the calling plugin's identity (e.g.
`PresetBundle.apply_config`'s confirmation dialog) fall back to an anonymous "A plugin".

`PythonInterpreter::install_thread_audit_propagation()` (`PythonInterpreter.cpp`) closes this
gap by monkey-patching `threading.Thread.start`/`.run` once, process-wide, at interpreter init:
`start()` captures the calling thread's `(plugin_key, capability_name)` via the new
`orca.host.plugin._capture_audit_identity()` binding, and `run()` re-opens it as a
`ScopedPluginAuditContext` (via the `orca.host.plugin._AuditScope` wrapper, in `PluginHost.cpp`)
for the thread's lifetime. Both are internal, leading-underscore names, not part of the
plugin-facing API.

### DevTools for plugin webviews

`WebViewHostDialog::create_webview()` now calls
`EnableAccessToDevTools(app_config->get_bool("developer_mode"))`, the same opt-in gate
`PrinterWebView.cpp` already used. With Developer Mode on in Preferences, a plugin author can
right-click -> Inspect a `create_window()` page the same way the printer web UI can be
inspected.

### AMS filament sync binding

`orca.host.plater().sync_ams_filaments()` (`PluginHostApp.cpp`) wraps the existing
`Sidebar::sync_ams_list()` -- the same action as the sidebar's own sync button (the
"Synchronize Filament List from AMS" native command). Safe to call unconditionally: with no
printer connected it no-ops, and with a connected-but-empty AMS it shows the app's own "not
connected" prompt; neither path raises.
