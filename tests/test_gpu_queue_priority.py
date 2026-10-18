"""Exercise the production opt-in D3D12 direct-queue priority helper."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / "ps3recomp" / "libs" / "video" / "rsx_d3d12_backend.c"


def function(source, signature):
    start = source.find(signature)
    while start >= 0:
        brace = source.find("{", start)
        semi = source.find(";", start)
        if brace >= 0 and (semi < 0 or brace < semi):
            depth, end = 1, brace + 1
            while depth:
                depth += (source[end] == "{") - (source[end] == "}")
                end += 1
            return source[start:end]
        start = source.find(signature, start + len(signature))
    raise ValueError(f"definition not found: {signature}")


def make_source(backend):
    enabled = function(backend, "static int d3d12_gpu_queue_high_priority_enabled(")
    priority_name = function(backend, "static const char* d3d12_command_queue_priority_name(")
    log = function(backend, "static void d3d12_log_command_queue_priority(")
    create = function(backend, "static HRESULT d3d12_create_command_queue_for_device(")
    init = function(backend, "static int init_d3d12(")
    assert 'getenv("TJ_GPU_QUEUE_HIGH_PRIORITY")' in enabled
    assert 'strcmp(value, "1") == 0' in enabled
    assert "D3D12_COMMAND_QUEUE_PRIORITY_GLOBAL_REALTIME" not in create
    assert "hr != E_INVALIDARG && hr != E_NOTIMPL" in create
    assert create.index("(*out_queue)->lpVtbl->Release(*out_queue)") < create.index(
        "hr != E_INVALIDARG && hr != E_NOTIMPL"
    )
    assert "D3D12_FEATURE_COMMAND_QUEUE_PRIORITY" in create
    assert "SUCCEEDED(*priority_query_hr)" in create
    request = init.index("d3d12_gpu_queue_high_priority_enabled()")
    create_call = init.index("d3d12_create_command_queue_for_device(", request)
    log_call = init.index("d3d12_log_command_queue_priority(", create_call)
    assert request < create_call < log_call
    assert "D3D12_COMMAND_QUEUE_PRIORITY_GLOBAL_REALTIME" not in init
    assert "SetProcessPriority" not in init
    return r'''
#define COBJMACROS
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const IID fixture_queue_iid = {0};
#define IID_ID3D12CommandQueue fixture_queue_iid

static unsigned checks, failures;
static unsigned feature_calls, create_calls, release_calls;
static unsigned release_count_seen_on_second_create;
static HRESULT feature_result;
static BOOL feature_supported;
static HRESULT create_results[2];
static int create_partial[2];
static D3D12_COMMAND_QUEUE_PRIORITY create_priorities[2];
static D3D12_COMMAND_LIST_TYPE feature_type;
static D3D12_COMMAND_QUEUE_PRIORITY feature_priority;

static ID3D12DeviceVtbl device_vtbl;
static ID3D12Device fixture_device = { &device_vtbl };
static ID3D12CommandQueueVtbl queue_vtbl;
static ID3D12CommandQueue fixture_queue = { &queue_vtbl };

static void check(int ok, const char* label) {
    ++checks;
    if (!ok) { ++failures; printf("FAIL %s\n", label); }
}
static ULONG STDMETHODCALLTYPE mock_queue_release(ID3D12CommandQueue* self) {
    (void)self; ++release_calls; return 1;
}
static HRESULT STDMETHODCALLTYPE mock_feature_support(ID3D12Device* self,
    D3D12_FEATURE feature, void* data, UINT size) {
    (void)self; ++feature_calls;
    check(feature == D3D12_FEATURE_COMMAND_QUEUE_PRIORITY,
          "feature query requests command queue priority");
    check(size == sizeof(D3D12_FEATURE_DATA_COMMAND_QUEUE_PRIORITY),
          "feature query uses exact structure size");
    if (data && size == sizeof(D3D12_FEATURE_DATA_COMMAND_QUEUE_PRIORITY)) {
        D3D12_FEATURE_DATA_COMMAND_QUEUE_PRIORITY* p =
            (D3D12_FEATURE_DATA_COMMAND_QUEUE_PRIORITY*)data;
        feature_type = p->CommandListType;
        feature_priority = p->Priority;
        p->PriorityForTypeIsSupported = feature_supported;
    }
    return feature_result;
}
static HRESULT STDMETHODCALLTYPE mock_create_queue(ID3D12Device* self,
    const D3D12_COMMAND_QUEUE_DESC* desc, REFIID iid, void** out) {
    (void)self; (void)iid;
    unsigned index = create_calls++;
    if (index < 2) {
        create_priorities[index] = desc->Priority;
        if (index == 1) release_count_seen_on_second_create = release_calls;
        if (create_partial[index] || SUCCEEDED(create_results[index]))
            *out = &fixture_queue;
        else
            *out = NULL;
        check(desc->Type == D3D12_COMMAND_LIST_TYPE_DIRECT,
              "created queue remains DIRECT");
        return create_results[index];
    }
    *out = NULL;
    return E_FAIL;
}

CONFIG_HELPER
PRIORITY_NAME_HELPER
LOG_HELPER
CREATE_HELPER

static void configure_case(const char* name, int* expected_request,
    unsigned* expected_queries, unsigned* expected_creates, unsigned* expected_releases,
    D3D12_COMMAND_QUEUE_PRIORITY* expected_first,
    D3D12_COMMAND_QUEUE_PRIORITY* expected_second,
    int* expected_selected, int* expected_supported, int* expected_fallback,
    HRESULT* expected_query_hr, HRESULT* expected_create_hr) {
    const char* env = "";
    feature_result = S_OK; feature_supported = TRUE;
    create_results[0] = S_OK; create_results[1] = S_OK;
    create_partial[0] = create_partial[1] = 0;
    *expected_queries = 0; *expected_creates = 1; *expected_releases = 0;
    *expected_first = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    *expected_second = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    *expected_selected = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    *expected_supported = 0; *expected_fallback = 0;
    *expected_query_hr = S_FALSE; *expected_create_hr = S_OK;

    if (!strcmp(name, "off-unset")) { env = ""; *expected_request = 0; }
    else if (!strcmp(name, "off-zero")) { env = "0"; *expected_request = 0; }
    else if (!strcmp(name, "off-near")) { env = "01"; *expected_request = 0; }
    else if (!strcmp(name, "off-word")) { env = "true"; *expected_request = 0; }
    else if (!strcmp(name, "unsupported")) {
        env = "1"; *expected_request = 1; *expected_queries = 1;
        feature_supported = FALSE; *expected_query_hr = S_OK; *expected_fallback = 1;
    } else if (!strcmp(name, "query-failed")) {
        env = "1"; *expected_request = 1; *expected_queries = 1;
        feature_result = E_FAIL; *expected_query_hr = E_FAIL; *expected_fallback = 1;
    } else if (!strcmp(name, "high")) {
        env = "1"; *expected_request = 1; *expected_queries = 1;
        *expected_first = D3D12_COMMAND_QUEUE_PRIORITY_HIGH;
        *expected_selected = D3D12_COMMAND_QUEUE_PRIORITY_HIGH;
        *expected_supported = 1; *expected_query_hr = S_OK;
    } else if (!strcmp(name, "fallback-invalid") || !strcmp(name, "fallback-notimpl")) {
        env = "1"; *expected_request = 1; *expected_queries = 1;
        *expected_creates = 2; *expected_releases = 1; *expected_fallback = 1;
        *expected_first = D3D12_COMMAND_QUEUE_PRIORITY_HIGH;
        *expected_second = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
        *expected_query_hr = S_OK; *expected_supported = 1;
        create_results[0] = !strcmp(name, "fallback-invalid") ? E_INVALIDARG : E_NOTIMPL;
        create_partial[0] = 1;
    } else if (!strcmp(name, "oom") || !strcmp(name, "device-removed") ||
               !strcmp(name, "other-error")) {
        env = "1"; *expected_request = 1; *expected_queries = 1;
        *expected_first = D3D12_COMMAND_QUEUE_PRIORITY_HIGH;
        *expected_selected = -1; *expected_supported = 1; *expected_query_hr = S_OK;
        *expected_create_hr = !strcmp(name, "oom") ? E_OUTOFMEMORY :
            (!strcmp(name, "device-removed") ? DXGI_ERROR_DEVICE_REMOVED : E_FAIL);
        create_results[0] = *expected_create_hr;
    } else if (!strcmp(name, "normal-oom")) {
        env = ""; *expected_request = 0; *expected_selected = -1;
        *expected_create_hr = E_OUTOFMEMORY; create_results[0] = E_OUTOFMEMORY;
    } else {
        printf("FAIL unknown case %s\n", name); ++failures; return;
    }
    (void)_putenv_s("TJ_GPU_QUEUE_HIGH_PRIORITY", env);
}

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    int expected_request = 0, expected_selected = -1, expected_supported = 0;
    int expected_fallback = 0;
    unsigned expected_queries = 0, expected_creates = 0, expected_releases = 0;
    D3D12_COMMAND_QUEUE_PRIORITY expected_first = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    D3D12_COMMAND_QUEUE_PRIORITY expected_second = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    HRESULT expected_query_hr = S_FALSE, expected_create_hr = S_OK;
    configure_case(argv[1], &expected_request, &expected_queries, &expected_creates,
                   &expected_releases, &expected_first, &expected_second,
                   &expected_selected, &expected_supported, &expected_fallback,
                   &expected_query_hr, &expected_create_hr);
    device_vtbl.CheckFeatureSupport = mock_feature_support;
    device_vtbl.CreateCommandQueue = mock_create_queue;
    queue_vtbl.Release = mock_queue_release;

    const int requested = d3d12_gpu_queue_high_priority_enabled();
    ID3D12CommandQueue* out = NULL;
    int selected = -99, high_supported = -99, fallback = -99;
    HRESULT query_hr = E_POINTER;
    HRESULT hr = d3d12_create_command_queue_for_device(&fixture_device, &out, requested,
        &selected, &query_hr, &high_supported, &fallback);
    d3d12_log_command_queue_priority(requested, selected, query_hr,
                                     high_supported, fallback, hr);

    check(requested == expected_request, "exact environment request value");
    check(feature_calls == expected_queries, "feature query count");
    check(create_calls == expected_creates, "queue creation retry count");
    check(release_calls == expected_releases, "partial queue release count");
    check(selected == expected_selected, "reported selected priority");
    check(high_supported == expected_supported, "reported HIGH support");
    check(fallback == expected_fallback, "reported fallback state");
    check(query_hr == expected_query_hr, "reported feature-query HRESULT");
    check(hr == expected_create_hr, "original create HRESULT handling");
    if (create_calls > 0)
        check(create_priorities[0] == expected_first, "first create priority");
    if (create_calls > 1) {
        check(create_priorities[1] == expected_second, "retry priority is NORMAL");
        check(release_count_seen_on_second_create == 1,
              "partial HIGH queue released before NORMAL retry");
    }
    if (expected_queries) {
        check(feature_type == D3D12_COMMAND_LIST_TYPE_DIRECT,
              "support query targets DIRECT queues");
        check(feature_priority == D3D12_COMMAND_QUEUE_PRIORITY_HIGH,
              "support query asks for HIGH");
    }
    check(out == (SUCCEEDED(hr) ? &fixture_queue : NULL),
          "returned queue matches final HRESULT");
    printf("CASE %s checks=%u failures=%u query=%u creates=%u releases=%u "
           "selected=%d supported=%d fallback=%d hr=0x%08lX\n", argv[1],
           checks, failures, feature_calls, create_calls, release_calls, selected,
           high_supported, fallback, (unsigned long)hr);
    return failures ? 1 : 0;
}
'''.replace("CONFIG_HELPER", enabled).replace("PRIORITY_NAME_HELPER", priority_name) \
      .replace("LOG_HELPER", log).replace("CREATE_HELPER", create)


def main():
    backend = BACKEND.read_text(encoding="utf-8")
    source = make_source(backend)
    clang = shutil.which("clang") or r"C:\Program Files\LLVM\bin\clang.exe"
    cases = ("off-unset", "off-zero", "off-near", "off-word", "unsupported",
             "query-failed", "high", "fallback-invalid", "fallback-notimpl",
             "oom", "device-removed", "other-error", "normal-oom")
    with tempfile.TemporaryDirectory(prefix="gpu-queue-priority-") as tmp:
        c_path = Path(tmp) / "queue_fixture.c"
        exe = Path(tmp) / "queue_fixture.exe"
        c_path.write_text(source, encoding="utf-8")
        subprocess.run([clang, "-std=c11", "-O2", "-D_CRT_SECURE_NO_WARNINGS",
                        str(c_path), "-o", str(exe)], check=True)
        outputs = []
        for case in cases:
            result = subprocess.run([str(exe), case], text=True, capture_output=True,
                                    check=False, timeout=10)
            if result.returncode:
                raise AssertionError(f"{case} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            outputs.append((case, result.stdout, result.stderr))
        logs = "\n".join(stdout + stderr for _, stdout, stderr in outputs)
        assert logs.count("[D3D12-QUEUE-PRIORITY]") == len(cases), logs
        assert "selected=HIGH(100)" in logs, logs
        assert "selected=NORMAL(0)" in logs, logs
        assert "fallback=1" in logs, logs
        print("RESULT cases=13 failures=0")
        for case, stdout, _ in outputs:
            print(stdout.rstrip())


if __name__ == "__main__":
    main()
