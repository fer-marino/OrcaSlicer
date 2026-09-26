#include "PluginHost.hpp"
#include "PluginHostBindings.hpp"
#include "PluginHostUi.hpp"
#include <slic3r/plugin/PluginAuditManager.hpp>
#include <slic3r/plugin/PluginManager.hpp>

#include <pybind11/stl.h>

#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

namespace Slic3r {

namespace {

// Re-opens a captured (plugin_key, capability_name) pair as a ScopedPluginAuditContext, usable
// as a Python context manager. Backs the threading.Thread propagation installed by
// PythonInterpreter (see install_thread_audit_propagation there): a thread a plugin spawns via
// threading.Thread runs on its own OS thread, which starts with an empty thread_local audit
// identity, so without this the audit hook and identity-reading host APIs (e.g.
// PresetBundle.apply_config) would treat code running on it as not coming from any plugin at
// all. Internal -- the leading underscore says this isn't part of the plugin-facing API.
class AuditScope
{
public:
    AuditScope(std::string plugin_key, std::string capability_name)
        : m_plugin_key(std::move(plugin_key)), m_capability_name(std::move(capability_name))
    {}

    void enter() { m_scope.emplace(m_plugin_key, m_capability_name); }
    void exit(const pybind11::object&, const pybind11::object&, const pybind11::object&) { m_scope.reset(); }

private:
    std::string                             m_plugin_key;
    std::string                             m_capability_name;
    std::optional<ScopedPluginAuditContext> m_scope;
};

} // namespace

namespace host_bindings {
void register_plugin(pybind11::module_& host)
{
    auto plugin_host = host.def_submodule("plugin", "Plugin host API");

    plugin_host.def(
        "storage",
        []() -> std::string {
            const std::string plugin_key = PluginAuditManager::instance().current_plugin();
            if (plugin_key.empty())
                throw std::runtime_error("plugin.storage() must be called from a plugin callback");

            return PluginManager::instance().get_storage_dir(plugin_key);
        },
        "Return the installed folder of the current plugin.");

    pybind11::class_<AuditScope>(plugin_host, "_AuditScope",
                                 "Internal: re-opens a captured plugin audit identity as a context "
                                 "manager. Not part of the plugin-facing API.")
        .def(pybind11::init<std::string, std::string>())
        .def("__enter__", &AuditScope::enter)
        .def("__exit__", &AuditScope::exit);

    plugin_host.def(
        "_capture_audit_identity",
        []() -> std::pair<std::string, std::string> {
            return {PluginAuditManager::instance().current_plugin(), PluginAuditManager::instance().current_capability()};
        },
        "Internal: the calling thread's current (plugin_key, capability_name), or (\"\", \"\") "
        "outside a plugin callback. Not part of the plugin-facing API.");
}
} // namespace host_bindings

void PluginHost::RegisterBindings(pybind11::module_& module)
{
    auto host = module.def_submodule("host", "Host application API");

    // Value types first so the docstring signatures of later registrars
    // resolve to the bound Python names.
    host_bindings::register_geometry(host);
    host_bindings::register_mesh(host);
    host_bindings::register_presets(host);
    host_bindings::register_model(host);
    host_bindings::register_app(host);
    host_bindings::register_plugin(host);

    // UI: native dialogs and interactive HTML windows for plugins.
    PluginHostUi::RegisterBindings(host);

    // Slicing print-graph data model (Print, Layer, Surface, ...).
    host_bindings::register_slicing(host);
}

} // namespace Slic3r
