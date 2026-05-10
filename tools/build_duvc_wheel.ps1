#requires -Version 5.1
<#
.SYNOPSIS
    Build a delvewheel-bundled duvc-ctl wheel for the current Python ABI.

.DESCRIPTION
    Upstream duvc-ctl ships prebuilt wheels for cp38–cp312 only. capa pins
    Python >=3.13, so we build from source and bundle duvc-core.dll via
    delvewheel. The result lands in capa/vendor/ and is referenced from
    pyproject.toml's [tool.uv.sources] until upstream publishes cp313 wheels.

    Toolchain requirements:
      * Visual Studio Build Tools 2022 (C++ + Windows SDK)
      * CMake 3.20+
      * uv (Python launcher)
      * git

.PARAMETER Ref
    Git ref to build from. Defaults to "main". Pin to a tag (e.g. "v2.1.0")
    once upstream cuts a release that builds cleanly on Python 3.13.

.PARAMETER OutDir
    Destination directory relative to the repo root. Defaults to "vendor".

.EXAMPLE
    pwsh -File tools/build_duvc_wheel.ps1
    pwsh -File tools/build_duvc_wheel.ps1 -Ref v2.1.0
#>
param(
    [string]$Ref = "main",
    [string]$OutDir = "vendor"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$absOut = Join-Path $repoRoot $OutDir
$tmp = Join-Path $env:TEMP ("duvc_build_" + [guid]::NewGuid().Guid.Substring(0, 8))

New-Item -ItemType Directory -Path $tmp -Force | Out-Null
New-Item -ItemType Directory -Path $absOut -Force | Out-Null

try {
    Write-Host "==> Cloning duvc-ctl ($Ref) into $tmp\src"
    git clone --depth 1 --branch $Ref https://github.com/allanhanan/duvc-ctl.git "$tmp\src"

    Write-Host "==> Creating isolated 3.13 build env"
    uv venv --python 3.13 "$tmp\env" --quiet
    $py = "$tmp\env\Scripts\python.exe"
    uv pip install --python $py --quiet build delvewheel

    Write-Host "==> Building bare wheel from bindings/python"
    Push-Location "$tmp\src\bindings\python"
    try {
        & $py -m build --wheel --outdir "$tmp\wheelhouse"
        if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    $bareWheel = Get-ChildItem "$tmp\wheelhouse\duvc_ctl-*.whl" | Select-Object -First 1
    if ($null -eq $bareWheel) { throw "no wheel produced in $tmp\wheelhouse" }

    Write-Host "==> Unpacking wheel to expose bin\duvc-core.dll for delvewheel"
    # delvewheel searches the filesystem (not inside the wheel) for the DLLs
    # its .pyd imports. We unpack the wheel so its bundled bin\duvc-core.dll
    # is on disk and pass that dir via --add-path.
    Copy-Item $bareWheel.FullName "$tmp\wheel.zip" -Force
    Expand-Archive "$tmp\wheel.zip" -DestinationPath "$tmp\unpacked" -Force

    Write-Host "==> Running delvewheel repair (bundles duvc-core.dll + msvcp140.dll)"
    & $py -m delvewheel repair $bareWheel.FullName -w "$tmp\final" --add-path "$tmp\unpacked\bin"
    if ($LASTEXITCODE -ne 0) { throw "delvewheel repair failed (exit $LASTEXITCODE)" }

    $finalWheel = Get-ChildItem "$tmp\final\duvc_ctl-*.whl" | Select-Object -First 1
    if ($null -eq $finalWheel) { throw "no bundled wheel produced in $tmp\final" }

    # Replace any older duvc_ctl wheel in vendor/ so we don't accumulate stale
    # artifacts when bumping the upstream ref.
    Get-ChildItem $absOut -Filter "duvc_ctl-*.whl" | Remove-Item -Force
    Copy-Item $finalWheel.FullName $absOut -Force

    Write-Host ""
    Write-Host "==> Done"
    Write-Host "    Vendored: $absOut\$($finalWheel.Name)"
    Write-Host "    From ref: $Ref"
    Write-Host ""
    Write-Host "Next: bump 'duvc-ctl' under [tool.uv.sources] in pyproject.toml if the"
    Write-Host "filename changed, then run 'uv lock'."
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
