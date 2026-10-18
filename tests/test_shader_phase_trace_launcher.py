"""Verify shader-phase trace launcher flags without starting the game."""
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT.parent.parent / "launch_tokyojungle.ps1"
BACKEND = ROOT.parent / "ps3recomp" / "libs/video/rsx_d3d12_backend.c"


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def no_run_metadata(shader_phase_trace):
    powershell = (
        shutil.which("pwsh.exe")
        or shutil.which("powershell.exe")
        or shutil.which("pwsh")
        or shutil.which("powershell")
    )
    assert powershell, "Windows PowerShell is required for the no-Run launcher check"
    args = "-ShaderPhaseTrace" if shader_phase_trace else ""
    command = f"& '{LAUNCHER}' {args} | ConvertTo-Json -Compress -Depth 5"
    env = os.environ.copy()
    # Seed stale inherited diagnostics. The launcher must clear them, then add
    # only the explicitly requested shader-phase trace for the ON case.
    for name in (
        "TJ_FP_BLOB_TRACE",
        "TJ_PERF_TRACE",
        "TJ_GCM2D_PERF",
        "TJ_FRAME_PROF",
        "TJ_FIFO_PERF",
    ):
        env[name] = "1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"launcher failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_shader_phase_trace_launcher_no_run_and_diagnostic_only():
    off = no_run_metadata(False)
    on = no_run_metadata(True)

    assert off["LaunchAuthorized"] is False and on["LaunchAuthorized"] is False
    assert off["ProcessId"] is None and on["ProcessId"] is None
    assert off["ShaderPhaseTrace"] is False
    assert off["ShaderPhaseTraceEnabled"] is False
    assert on["ShaderPhaseTrace"] is True
    assert on["ShaderPhaseTraceEnabled"] is True
    assert off["FpBlobTraceEnabled"] is False
    assert on["FpBlobTraceEnabled"] is False

    assert off["FrameDeliveryTrace"] is False
    assert off["PresentWorkTrace"] is False
    for key in (
        "FrameDeliveryTrace",
        "PresentWorkTrace",
        "PresentLoopTraceEnabled",
        "FlipTimelineEnabled",
        "FpsTraceEnabled",
    ):
        assert on[key] is True

    # Trace opt-in changes only its own metadata and the expected shared
    # presentation-trace outputs; all launch, save, and candidate flags match.
    expected_changes = {
        "ShaderPhaseTrace",
        "ShaderPhaseTraceEnabled",
        "FrameDeliveryTrace",
        "PresentWorkTrace",
        "PresentLoopTraceEnabled",
        "FlipTimelineEnabled",
        "FpsTraceEnabled",
    }
    assert set(off) == set(on)
    changed = {key for key in off if off[key] != on[key]}
    assert changed == expected_changes
    assert off["SaveRoot"] == on["SaveRoot"]
    assert off["ExecutableSha256"] == on["ExecutableSha256"]

    for metadata in (off, on):
        assert metadata["PerformanceCapture"] is False
        assert metadata["FrameProfileSampleMs"] == 0
        assert metadata["Gcm2dPerfEnabled"] is False
        assert metadata["PrefixGpuCostEnabled"] is False
        assert metadata["FifoPerfEnabled"] is False
        assert metadata["PrefixCostEnabled"] is False
        assert metadata["AutomatedInput"] is False

    backend = BACKEND.read_text(encoding="utf-8")
    assert backend.count('getenv("TJ_PERF_TRACE")') == 2
    assert 'getenv("TJ_PERF_TRACE")' in function(
        backend, "static int tj_perf_trace_enabled(void)\n"
    )
    assert 'getenv("TJ_PERF_TRACE")' in function(
        backend, "static int tj_fps_trace_enabled(void)\n"
    )
    for gate in (
        "static int tj_prefix_cost_enabled(void)\n",
        "static int tj_gcm2d_backend_perf_enabled(void)\n",
        "static int d3d12_prefix_ownership_enabled(void)\n",
        "static int d3d12_texture_label_batch_enabled(void)\n",
        "static int d3d12_prefix_prepare_enabled(void)\n",
    ):
        assert "TJ_PERF_TRACE" not in function(backend, gate)


if __name__ == "__main__":
    test_shader_phase_trace_launcher_no_run_and_diagnostic_only()
    print("Shader phase launcher: no-Run metadata checks passed")

