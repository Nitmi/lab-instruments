// Bounded PureThermal Media Foundation reader. Fixed GET controls; no UVC SET/FFC.
// Independent implementation using Windows SDK and GroupGets' documented XU layout.
#define NOMINMAX
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mferror.h>
#include <ks.h>
#include <ksproxy.h>
#include <ksmedia.h>
#include <vidcap.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
using Microsoft::WRL::ComPtr;
namespace fs = std::filesystem;
using Bytes = std::vector<uint8_t>;

static void need(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
static void check(HRESULT hr, const char* stage) {
    if (FAILED(hr)) { std::ostringstream s; s << stage << ":HRESULT:" << std::hex << static_cast<uint32_t>(hr); throw std::runtime_error(s.str()); }
}
static std::string json_quote(const std::wstring& text) {
    std::ostringstream s; s << '"';
    for (wchar_t ch : text) {
        if (ch >= 32 && ch < 127 && ch != L'"' && ch != L'\\') s << static_cast<char>(ch);
        else s << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(ch);
    }
    s << '"'; return s.str();
}
static std::string json_quote(const std::string& text) { return json_quote(std::wstring(text.begin(), text.end())); }
static void write_new(const fs::path& path, const Bytes& data) {
    HANDLE handle = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    need(handle != INVALID_HANDLE_VALUE, "create exclusive evidence file");
    DWORD written = 0;
    const BOOL ok = WriteFile(handle, data.data(), static_cast<DWORD>(data.size()), &written, nullptr);
    const BOOL flushed = FlushFileBuffers(handle);
    const BOOL closed = CloseHandle(handle);
    need(ok && flushed && closed && written == data.size(), "write evidence file");
}
static void write_text(const fs::path& path, const std::string& text) { write_new(path, Bytes(text.begin(), text.end())); }
static std::string hex(const Bytes& data) {
    std::ostringstream s; for (auto b : data) s << std::hex << std::setw(2) << std::setfill('0') << unsigned(b); return s.str();
}
struct Control { const char* name; const char* group; ULONG selector; ULONG size; };
static const std::array<Control, 14> controls = {{
    {"serial", "sys", 3, 8}, {"part", "oem", 8, 32}, {"software", "oem", 9, 8},
    {"agc", "agc", 1, 4}, {"radiometry", "rad", 5, 4}, {"tlinear", "rad2", 18, 4},
    {"resolution", "rad2", 19, 4}, {"auto_resolution", "rad2", 20, 4}, {"flux", "rad2", 17, 16},
    {"telemetry", "sys", 7, 4}, {"location", "sys", 8, 4}, {"ffc_status", "sys", 18, 4}, {"gain", "sys", 19, 4}, {"shutter", "sys", 15, 4}
}};
static GUID xu_guid(const std::string& group) {
    const std::string name = "pt1-lep-" + group + (group.size() == 3 ? "-0000" : "0000");
    need(name.size() == 16, "XU GUID length"); GUID result{}; std::memcpy(&result, name.data(), 16); return result;
}
struct Node { DWORD index; ComPtr<IKsControl> control; };
struct Reader {
    static constexpr DWORD video = static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM);
    ComPtr<IMFMediaSource> source;
    ComPtr<IMFActivate> selected;
    ComPtr<IMFSourceReader> reader;
    std::map<std::string, Node> nodes;
    bool com = false, mf = false;
    bool source_shutdown = true, activation_shutdown = true, mf_shutdown = true;
    void start() {
        check(CoInitializeEx(nullptr, COINIT_MULTITHREADED), "COM initialize"); com = true;
        check(MFStartup(MF_VERSION, MFSTARTUP_FULL), "MF startup"); mf = true;
    }
    std::vector<std::wstring> enumerate(const std::wstring& link) {
        ComPtr<IMFAttributes> attributes;
        check(MFCreateAttributes(&attributes, 1), "enumeration attributes");
        check(attributes->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID), "enumeration type");
        IMFActivate** devices = nullptr; UINT32 count = 0;
        check(MFEnumDeviceSources(attributes.Get(), &devices, &count), "enumerate camera interfaces");
        std::vector<std::wstring> links;
        try {
            need(count <= 128, "too many camera interfaces");
            for (UINT32 i = 0; i < count; ++i) {
                wchar_t* value = nullptr; UINT32 length = 0;
                check(devices[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK, &value, &length), "symbolic link");
                std::wstring current(value, length); CoTaskMemFree(value);
                std::wstring lower = current; std::transform(lower.begin(), lower.end(), lower.begin(), towlower);
                if (lower.find(L"\\\\?\\usb#vid_1e4e&pid_0100") == 0) links.push_back(current);
                if (!link.empty() && current == link) { need(!selected, "duplicate exact symbolic link"); selected = devices[i]; }
            }
        } catch (...) { for (UINT32 i = 0; i < count; ++i) devices[i]->Release(); CoTaskMemFree(devices); throw; }
        for (UINT32 i = 0; i < count; ++i) devices[i]->Release(); CoTaskMemFree(devices);
        return links;
    }
    void open() {
        need(selected != nullptr, "exact camera interface not found");
        check(selected->ActivateObject(IID_PPV_ARGS(&source)), "activate exact source");
        source_shutdown = activation_shutdown = false;
    }
    Node& node(const std::string& group) {
        auto found = nodes.find(group); if (found != nodes.end()) return found->second;
        ComPtr<IKsTopologyInfo> topology;
        check(source.As(&topology), "camera topology"); DWORD count = 0;
        check(topology->get_NumNodes(&count), "topology count"); need(count <= 64, "topology limit");
        for (DWORD i = 0; i < count; ++i) {
            GUID type{}; check(topology->get_NodeType(i, &type), "node type");
            if (type != KSNODETYPE_DEV_SPECIFIC) continue;
            ComPtr<IUnknown> unknown; ComPtr<IKsControl> ks;
            check(topology->CreateNodeInstance(i, IID_PPV_ARGS(&unknown)), "create topology node");
            check(unknown.As(&ks), "node control");
            KSP_NODE property{}; property.NodeId = i; property.Property.Set = xu_guid(group);
            property.Property.Id = KSPROPERTY_EXTENSION_UNIT_INFO;
            property.Property.Flags = KSPROPERTY_TYPE_GET | KSPROPERTY_TYPE_TOPOLOGY;
            ULONG bytes = 0;
            const HRESULT hr = ks->KsProperty(reinterpret_cast<PKSPROPERTY>(&property), sizeof(property), nullptr, 0, &bytes);
            if (hr == HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND)) continue;
            if (hr == HRESULT_FROM_WIN32(ERROR_MORE_DATA)) {
                need(bytes <= 4096, "extension descriptor size");
                return nodes.emplace(group, Node{i, ks}).first->second;
            }
            check(hr, "find extension unit");
        }
        throw std::runtime_error("required extension unit unavailable");
    }
    Bytes get(const Control& control) {
        auto& n = node(control.group);
        KSP_NODE property{}; property.NodeId = n.index; property.Property.Set = xu_guid(control.group);
        property.Property.Id = control.selector; property.Property.Flags = KSPROPERTY_TYPE_GET | KSPROPERTY_TYPE_TOPOLOGY;
        Bytes data(control.size); ULONG count = 0;
        check(n.control->KsProperty(reinterpret_cast<PKSPROPERTY>(&property), sizeof(property), data.data(), control.size, &count), control.name);
        need(count == control.size, "extension GET length differs"); return data;
    }
    void format() {
        ComPtr<IMFAttributes> attributes;
        check(MFCreateAttributes(&attributes, 2), "reader attributes");
        check(attributes->SetUINT32(MF_READWRITE_DISABLE_CONVERTERS, TRUE), "disable converters");
        check(attributes->SetUINT32(MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, FALSE), "disable processing");
        check(MFCreateSourceReaderFromMediaSource(source.Get(), attributes.Get(), &reader), "source reader");
        const GUID y16 = {0x20363159, 0, 0x10, {0x80,0,0,0xaa,0,0x38,0x9b,0x71}};
        ComPtr<IMFMediaType> chosen;
        for (DWORD i = 0; i < 256; ++i) {
            ComPtr<IMFMediaType> type;
            HRESULT hr = reader->GetNativeMediaType(video, i, &type);
            if (hr == MF_E_NO_MORE_TYPES) break;
            check(hr, "native media type"); GUID subtype{}; UINT32 w = 0, h = 0;
            check(type->GetGUID(MF_MT_SUBTYPE, &subtype), "media subtype");
            check(MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &w, &h), "media dimensions");
            if (subtype == y16 && w == 160 && h == 122 && !chosen) chosen = type;
        }
        need(chosen != nullptr, "native 160x122 Y16 unavailable");
        check(reader->SetCurrentMediaType(video, nullptr, chosen.Get()), "select native Y16");
        ComPtr<IMFMediaType> actual; GUID subtype{}; UINT32 w = 0, h = 0;
        check(reader->GetCurrentMediaType(video, &actual), "media readback");
        check(actual->GetGUID(MF_MT_SUBTYPE, &subtype), "subtype readback");
        check(MFGetAttributeSize(actual.Get(), MF_MT_FRAME_SIZE, &w, &h), "dimensions readback");
        need(subtype == y16 && w == 160 && h == 122, "media readback differs");
        UINT32 stride = 0;
        check(actual->GetUINT32(MF_MT_DEFAULT_STRIDE, &stride), "native Y16 stride");
        need(stride == 320, "only tightly packed top-down Y16 stride is supported");
    }
    Bytes sample(LONGLONG& timestamp) {
        DWORD stream = 0, flags = 0; ComPtr<IMFSample> sample;
        check(reader->ReadSample(video, 0, &stream, &flags, &timestamp, &sample), "read one buffer");
        need(flags == 0 && sample != nullptr, "null/end/changed/error/tick media sample");
        ComPtr<IMFMediaBuffer> buffer; check(sample->ConvertToContiguousBuffer(&buffer), "contiguous buffer");
        BYTE* data = nullptr; DWORD length = 0, maximum = 0;
        check(buffer->Lock(&data, &maximum, &length), "lock sample");
        Bytes result;
        try { need(length == 160 * 122 * 2 && maximum >= length, "sample byte length differs"); result.assign(data, data + length); }
        catch (...) { buffer->Unlock(); throw; }
        check(buffer->Unlock(), "unlock sample"); return result;
    }
    void close() {
        reader.Reset(); nodes.clear();
        if (source) { source_shutdown = SUCCEEDED(source->Shutdown()); source.Reset(); }
        if (selected) { activation_shutdown = SUCCEEDED(selected->ShutdownObject()); selected.Reset(); }
        if (mf) { mf_shutdown = SUCCEEDED(MFShutdown()); mf = false; }
        if (com) { CoUninitialize(); com = false; }
    }
    ~Reader() { close(); }
};
static Bytes synthetic_control(const Control& control) {
    Bytes raw(control.size, 0); uint32_t value = 0;
    const std::string name = control.name;
    if (name == "serial") return {1,2,3,4,5,6,7,8};
    if (name == "software") return {1,2,3,4,5,6,0,0};
    if (name == "part") { std::memcpy(raw.data(), "500-0771-01", 11); return raw; }
    if (name == "flux") { const uint16_t values[] = {8192,30000,8192,30000,8192,30000,0,30000}; std::memcpy(raw.data(), values, 16); return raw; }
    if (name == "radiometry" || name == "tlinear" || name == "resolution" || name == "telemetry" || name == "location") value = 1;
    std::memcpy(raw.data(), &value, 4); return raw;
}
static Bytes synthetic_sample(unsigned index, const std::wstring& fault) {
    std::vector<uint16_t> words(160 * 122, static_cast<uint16_t>(30000 + index * 10));
    auto* t = words.data() + 160 * 120; std::fill(t, t + 320, uint16_t(0));
    t[0] = 0x0900; t[1] = static_cast<uint16_t>(1000 + index * 120); t[3] = 48;
    t[20] = static_cast<uint16_t>(100 + index); t[24] = 30000; t[26] = 30000;
    t[30] = 500; t[208] = 1; t[209] = 1;
    if (fault == L"ffc") t[3] = 32;
    if (fault == L"agc") t[3] |= 1 << 12;
    Bytes result(words.size() * 2); std::memcpy(result.data(), words.data(), result.size()); return result;
}
int wmain(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--version") { std::cout << "lab-pt3-helper 0.4.0\n"; return 0; }
    if (argc != 8) { std::cerr << "mode action new-output-directory exact-link serial-hex software-hex buffers\n"; return 2; }
    const std::wstring mode = argv[1], action = argv[2], link = argv[4];
    const bool synthetic = mode == L"test_double";
    if ((!synthetic && mode != L"real") || (action != L"devices" && action != L"identify" && action != L"capture")) return 2;
    const fs::path output(argv[3]);
    std::wstring fault; wchar_t fault_buffer[64]{};
    if (synthetic && GetEnvironmentVariableW(L"LAB_PT3_TEST_FAULT", fault_buffer, 64) < 64) fault = fault_buffer;
    Reader reader; bool ok = false, capture_started = false;
    std::string error, frames = "["; std::vector<std::wstring> devices;
    try {
        const std::wstring count_text = argv[7]; need(count_text.size() == 1 && count_text[0] >= L'1' && count_text[0] <= L'8', "buffer bound");
        const unsigned count = static_cast<unsigned>(count_text[0] - L'0');
        need(fs::is_directory(output), "existing output directory required");
        if (synthetic) { need(link == L"SYNTHETIC-PT3" || action == L"devices", "test-double link"); devices = {L"SYNTHETIC-PT3"}; }
        else {
            if (action != L"devices") { std::wstring lower = link; std::transform(lower.begin(), lower.end(), lower.begin(), towlower); need(lower.find(L"\\\\?\\usb#vid_1e4e&pid_0100") == 0 && link.size() <= 512, "exact PureThermal interface required"); }
            reader.start(); devices = reader.enumerate(action == L"devices" ? L"" : link);
            if (action != L"devices") reader.open();
        }
        auto get = [&](const Control& control, const std::string& phase) {
            Bytes raw = synthetic ? synthetic_control(control) : reader.get(control);
            write_new(output / (phase + "-" + control.name + ".bin"), raw); return raw;
        };
        if (action != L"devices") {
            auto serial = get(controls[0], "identity"), part = get(controls[1], "identity"), sw = get(controls[2], "identity");
            need(std::string(part.begin(), std::find(part.begin(), part.end(), uint8_t(0))) == "500-0771-01", "Lepton part mismatch");
            if (action == L"capture") {
                const std::wstring serial_arg(argv[5]), sw_arg(argv[6]);
                const auto serial_hex = hex(serial), software_hex = hex(sw);
                need(std::wstring(serial_hex.begin(), serial_hex.end()) == serial_arg && std::wstring(software_hex.begin(), software_hex.end()) == sw_arg, "Lepton serial/software mismatch");
                if (!synthetic) reader.format();
                capture_started = true;
                LONGLONG stamp = 0;
                write_new(output / "warmup.y16", synthetic ? synthetic_sample(0, L"") : reader.sample(stamp));
                for (const auto& c : controls) {
                    auto value = get(c, "before");
                    const std::string name(c.name);
                    if (c.size == 4) {
                        uint32_t setting = 0; std::memcpy(&setting, value.data(), 4);
                        if (name == "radiometry" || name == "tlinear" || name == "telemetry" || name == "location") need(setting == 1, "radiometric mode not ready");
                        if (name == "agc" || name == "auto_resolution" || name == "ffc_status") need(setting == 0, "AGC/auto-resolution/FFC state not ready");
                        if (name == "resolution") need(setting <= 1, "resolution unsupported");
                    }
                }
                for (unsigned i = 0; i < count; ++i) {
                    std::ostringstream name; name << "frame-" << std::setw(4) << std::setfill('0') << i << ".y16";
                    auto raw = synthetic ? synthetic_sample(i + 1, fault) : reader.sample(stamp);
                    if (synthetic) stamp = static_cast<LONGLONG>(i + 1) * 1200000;
                    write_new(output / name.str(), raw);
                    if (i) frames += ",";
                    frames += "{\"name\":" + json_quote(name.str()) + ",\"timestamp_100ns\":" + std::to_string(stamp) + "}";
                }
                for (const auto& c : controls) get(c, "after");
            }
        }
        ok = true;
    } catch (const std::exception& e) { error = e.what(); }
    frames += "]"; reader.close();
    const bool cleaned = reader.source_shutdown && reader.activation_shutdown && reader.mf_shutdown;
    ok = ok && cleaned;
    std::ostringstream result;
    result << "{\"schema_version\":\"lab-instruments.pt3-native.v1\",\"ok\":" << (ok ? "true" : "false")
           << ",\"origin\":" << json_quote(synthetic ? "test_double" : "windows_media_foundation")
           << ",\"error\":" << json_quote(error) << ",\"capture_started\":" << (capture_started ? "true" : "false")
           << ",\"cleanup_complete\":" << (cleaned ? "true" : "false") << ",\"link\":" << json_quote(link)
           << ",\"frames\":" << frames << ",\"devices\":[";
    for (size_t i = 0; i < devices.size(); ++i) { if (i) result << ','; result << json_quote(devices[i]); }
    result << "]}\n";
    try { write_text(output / "native.json", result.str()); } catch (const std::exception& e) { std::cerr << e.what(); return 3; }
    return ok ? 0 : 3;
}
