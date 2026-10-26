param(
    [string]$Python = "",
    [switch]$InstallDependencies,
    [switch]$SkipInstallers,
    [switch]$SkipVerification
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not $Python) {
    $candidates = @(
        $env:LANBRIDGE_PYTHON38,
        (Join-Path $env:LOCALAPPDATA "LanBridgeBuild\Python38Nuget2\tools\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python38\python.exe"),
        "C:\Python38\python.exe",
        (Get-Command python.exe -ErrorAction SilentlyContinue).Source,
        (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        # The Windows Store alias exists on disk even when Python is absent.
        & $candidate -c "import sys, struct; sys.exit(0 if sys.version_info[:3] == (3, 8, 10) and struct.calcsize('P') == 8 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $candidate
            break
        }
    }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "64-bit Python 3.8.10 was not found. Pass its python.exe path with -Python."
}

Write-Host "Python: $Python"
& $Python -c "import sys, struct; assert sys.version_info[:3] == (3, 8, 10) and struct.calcsize('P') == 8, '64-bit Python 3.8.10 is required'"
if ($LASTEXITCODE -ne 0) {
    throw "The build interpreter must be 64-bit Python 3.8.10. Python 3.9+ executables do not run on Windows 7."
}
if ($InstallDependencies) {
    $managed = & $Python -c "import sys; print('1' if 'uv-python' in sys.executable.lower() else '0')"
    $pipArgs = @("-m", "pip", "install")
    if ($managed -eq "1") { $pipArgs += "--break-system-packages" }
    $pipArgs += @("-r", (Join-Path $projectRoot "requirements-build.txt"))
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) { throw "Installing build dependencies failed." }
}

& $Python -c "import PIL, PyInstaller, sys; sys.exit(0 if PIL.__version__ == '10.4.0' and PyInstaller.__version__ == '5.13.2' else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies do not match requirements-build.txt. Re-run with -InstallDependencies."
}

& $Python -c "import tkinter; root = tkinter.Tk(); root.withdraw(); root.update(); root.destroy()"
if ($LASTEXITCODE -ne 0) { throw "The build interpreter must include a working Tcl/Tk runtime." }

# Discover the installer compiler before replacing any release artifacts.
if (-not $SkipInstallers) {
    $isccCandidates = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $iscc = $isccCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $iscc) {
        throw "Inno Setup ISCC.exe was not found. Use -SkipInstallers to build only portable EXEs."
    }
}

& $Python scripts\generate_icons.py --output assets
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

New-Item -ItemType Directory -Force -Path dist | Out-Null
New-Item -ItemType Directory -Force -Path build | Out-Null

# A successful build must never leave new portable EXEs beside stale installers.
$targetNames = @(
    "邻桥快传-发送端.exe",
    "邻桥快传-接收端.exe",
    "邻桥快传-发送端-安装包.exe",
    "邻桥快传-接收端-安装包.exe",
    "LanBridge-Sender-x64.exe",
    "LanBridge-Receiver-x64.exe",
    "LanBridge-Sender-Setup-x64.exe",
    "LanBridge-Receiver-Setup-x64.exe",
    "SHA256SUMS.txt"
)
foreach ($targetName in $targetNames) {
    $targetPath = Join-Path $projectRoot "dist\$targetName"
    if (Test-Path -LiteralPath $targetPath) {
        Remove-Item -LiteralPath $targetPath -Force
    }
}

function Build-Executable {
    param(
        [string]$Name,
        [string]$WorkPath
    )
    Write-Host "Building $Name ..."
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath dist `
        --workpath $WorkPath `
        (Join-Path $projectRoot "packaging\$Name.spec")
    if ($LASTEXITCODE -ne 0) { throw "$Name build failed." }
}

Build-Executable "LanBridge-Sender-x64" "build\sender"
Build-Executable "LanBridge-Receiver-x64" "build\receiver"

if (-not $SkipInstallers) {
    Write-Host "Building installers ..."
    & $iscc /Qp packaging\LanBridge-Sender.iss
    if ($LASTEXITCODE -ne 0) { throw "Sender installer build failed." }
    & $iscc /Qp packaging\LanBridge-Receiver.iss
    if ($LASTEXITCODE -ne 0) { throw "Receiver installer build failed." }
}

$expectedNames = if ($SkipInstallers) {
    @("邻桥快传-接收端.exe", "邻桥快传-发送端.exe")
} else {
    @(
        "邻桥快传-接收端-安装包.exe",
        "邻桥快传-接收端.exe",
        "邻桥快传-发送端-安装包.exe",
        "邻桥快传-发送端.exe"
    )
}
$checksums = foreach ($targetName in $expectedNames) {
    $targetPath = Join-Path $projectRoot "dist\$targetName"
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        throw "Missing build artifact: $targetName"
    }
    $hash = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $targetName"
}
$checksums | Set-Content -LiteralPath (Join-Path $projectRoot "dist\SHA256SUMS.txt") -Encoding UTF8

if (-not $SkipVerification) {
    $verifyArgs = @("scripts\verify_windows_artifacts.py", "--dist", "dist")
    if ($SkipInstallers) { $verifyArgs += "--skip-installers" }
    & $Python @verifyArgs
    if ($LASTEXITCODE -ne 0) { throw "Windows artifact verification failed." }
}

Write-Host "Build complete. EXEs and installers are in dist\."
