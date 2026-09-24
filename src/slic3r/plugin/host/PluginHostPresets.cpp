#include "PluginHostBindings.hpp"
#include "slic3r/plugin/PluginBindingUtils.hpp"
#include "slic3r/plugin/PluginAuditManager.hpp"
#include "slic3r/plugin/PluginManager.hpp"

#include <libslic3r/Preset.hpp>
#include <libslic3r/PresetBundle.hpp>
#include <libslic3r/PrintConfig.hpp>

#include <slic3r/GUI/GUI_App.hpp>
#include <slic3r/GUI/I18N.hpp>
#include <slic3r/GUI/MainFrame.hpp>

#include <pybind11/stl.h>

#include <wx/msgdlg.h>
#include <wx/string.h>

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace Slic3r {
namespace {

py::list current_filament_presets(PresetBundle& bundle)
{
    py::list presets;
    for (const std::string& preset_name : bundle.filament_presets) {
        Preset* preset = bundle.filaments.find_preset(preset_name);
        if (preset == nullptr)
            presets.append(py::none());
        else
            presets.append(py::cast(preset, py::return_value_policy::reference));
    }
    return presets;
}

PresetCollection& printer_presets(PresetBundle& bundle)
{
    return static_cast<PresetCollection&>(bundle.printers);
}

// Apply a {config_key: value_string} diff to the live print/filament/printer
// presets and refresh the GUI to match, exactly like importing a config file
// does (MainFrame::load_config fans the diff out to whichever tabs recognize
// each key, marks them dirty, and reloads the Plater).
//
// This is a plain pybind11 call, not a recognized CPython audit event, so it
// never reaches PluginAuditManager's audit_hook -- there is no filesystem path
// or network target for it to key permission on. Mutating live print settings
// needs its own gate, so this shows the same Yes/No prompt style the audit
// manager uses for fs/network permissions before touching anything.
//
// Values are extracted from the Python dict, and the calling plugin's identity
// resolved, on the calling thread -- run_on_ui_blocking's callable must not
// touch Python objects, since it may run with the GIL not held (see
// PluginBindingUtils.hpp). Note current_plugin() is thread_local and only set
// while the host is calling into the plugin directly: a plugin that calls this
// from its own background thread (e.g. threading.Thread) will show up as "A
// plugin" below rather than by name.
std::vector<std::string> apply_config(const py::dict& values)
{
    std::vector<std::pair<std::string, std::string>> pending;
    pending.reserve(values.size());
    for (auto item : values)
        pending.emplace_back(py::str(item.first).cast<std::string>(), py::str(item.second).cast<std::string>());

    std::string plugin_name = "A plugin";
    const std::string plugin_key = PluginAuditManager::instance().current_plugin();
    if (!plugin_key.empty()) {
        PluginDescriptor descriptor;
        if (PluginManager::instance().try_get_plugin_descriptor(plugin_key, descriptor) && !descriptor.name.empty())
            plugin_name = descriptor.name;
    }

    return run_on_ui_blocking([pending = std::move(pending), plugin_name]() -> std::vector<std::string> {
        if (pending.empty())
            return {};

        wxString change_list;
        for (const auto& [key, value] : pending)
            change_list += wxString::FromUTF8(key.c_str()) + " = " + wxString::FromUTF8(value.c_str()) + "\n";

        wxMessageDialog dialog(nullptr,
                               wxString::Format(_L("Plugin \"%s\" wants to change the following setting(s):\n\n%s"),
                                                 wxString::FromUTF8(plugin_name.c_str()), change_list),
                               _L("Plugin settings change"), wxYES_NO | wxICON_WARNING);
        if (dialog.ShowModal() != wxID_YES)
            throw std::runtime_error("apply_config: user declined the requested settings change");

        DynamicPrintConfig config;
        std::vector<std::string> applied;
        for (const auto& [key, value] : pending) {
            try {
                config.set_deserialize_strict(key, value);
            } catch (const std::exception& ex) {
                throw std::runtime_error("apply_config: failed to set '" + key + "' = '" + value + "': " + ex.what());
            }
            applied.push_back(key);
        }
        GUI::wxGetApp().mainframe->load_config(config);
        return applied;
    });
}

} // namespace

void host_bindings::register_presets(py::module_& host)
{
    py::enum_<Preset::Type>(host, "PresetType")
        .value("Invalid", Preset::TYPE_INVALID)
        .value("Print", Preset::TYPE_PRINT)
        .value("SlaPrint", Preset::TYPE_SLA_PRINT)
        .value("Filament", Preset::TYPE_FILAMENT)
        .value("SlaMaterial", Preset::TYPE_SLA_MATERIAL)
        .value("Printer", Preset::TYPE_PRINTER)
        .value("PhysicalPrinter", Preset::TYPE_PHYSICAL_PRINTER)
        .value("Plate", Preset::TYPE_PLATE)
        .value("Model", Preset::TYPE_MODEL);

    py::class_<Preset, std::unique_ptr<Preset, py::nodelete>>(host, "Preset")
        .def_readonly("type", &Preset::type)
        .def_readonly("name", &Preset::name)
        .def_readonly("alias", &Preset::alias)
        .def_readonly("file", &Preset::file)
        .def_readonly("is_default", &Preset::is_default)
        .def_readonly("is_external", &Preset::is_external)
        .def_readonly("is_system", &Preset::is_system)
        .def_readonly("is_visible", &Preset::is_visible)
        .def_readonly("is_dirty", &Preset::is_dirty)
        .def_readonly("is_compatible", &Preset::is_compatible)
        .def_readonly("is_project_embedded", &Preset::is_project_embedded)
        .def_readonly("bundle_id", &Preset::bundle_id)
        .def("is_user", &Preset::is_user)
        .def("is_from_bundle", &Preset::is_from_bundle)
        .def("label", &Preset::label, py::arg("no_alias") = false)
        .def("config_keys", [](const Preset& preset) { return preset.config.keys(); })
        .def("config_value", [](const Preset& preset, const std::string& key) {
            return config_value_or_none(preset.config, key);
        });

    py::class_<PresetCollection, std::unique_ptr<PresetCollection, py::nodelete>>(host, "PresetCollection")
        .def("size", &PresetCollection::size)
        .def("get_selected_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_selected_preset();
        }, py::return_value_policy::reference_internal)
        .def("selected_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_selected_preset();
        }, py::return_value_policy::reference_internal)
        .def("get_selected_preset_name", &PresetCollection::get_selected_preset_name)
        .def("selected_preset_name", &PresetCollection::get_selected_preset_name)
        .def("get_edited_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("edited_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("preset", [](PresetCollection& collection, size_t index) -> Preset& {
            if (index >= collection.size())
                throw py::index_error("preset index out of range");
            return collection.preset(index);
        }, py::return_value_policy::reference_internal)
        .def("find_preset", [](PresetCollection& collection, const std::string& name) -> Preset* {
            return collection.find_preset(name);
        }, py::return_value_policy::reference_internal)
        .def("preset_names", [](const PresetCollection& collection) {
            std::vector<std::string> names;
            names.reserve(collection.get_presets().size());
            for (const Preset& preset : collection.get_presets())
                names.push_back(preset.name);
            return names;
        });

    py::class_<PresetBundle, std::unique_ptr<PresetBundle, py::nodelete>>(host, "PresetBundle")
        .def_property_readonly("prints", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.prints;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("printers", &printer_presets, py::return_value_policy::reference_internal)
        .def_property_readonly("filaments", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.filaments;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("sla_prints", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.sla_prints;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("sla_materials", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.sla_materials;
        }, py::return_value_policy::reference_internal)
        .def("current_process_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.prints.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_print_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.prints.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_printer_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.printers.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_filament_preset_names", [](PresetBundle& bundle) {
            return bundle.filament_presets;
        })
        .def("current_filament_presets", &current_filament_presets)
        .def("full_config_keys", [](const PresetBundle& bundle) {
            return bundle.full_config().keys();
        })
        .def("full_config_value", [](const PresetBundle& bundle, const std::string& key) {
            return config_value_or_none(bundle.full_config(), key);
        })
        .def(
            "apply_config",
            [](PresetBundle&, const py::dict& values) { return apply_config(values); },
            py::arg("values"),
            "Apply a {config_key: value_string} diff to the active print/filament/printer "
            "presets and refresh the GUI, the same way loading a config file does. Prompts the "
            "user with a Yes/No summary of the change first; declining raises. Values are "
            "parsed the same way a config file's key=value lines are; an unknown key or a value "
            "that fails to parse raises and nothing is applied. Runs on the UI thread "
            "(marshaled automatically) and marks the affected presets dirty -- it does not save "
            "them.");
}

} // namespace Slic3r
