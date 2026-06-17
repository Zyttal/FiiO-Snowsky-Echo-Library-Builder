# Build a Windows .exe of echo-library-builder.
# Run from the project root in PowerShell with the pyenv-win venv active.
#
# Requires:
#   - pyinstaller in the active venv (installed at first use below)
#   - PowerShell 5+ (for Expand-Archive)
$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location ..
$Root = (Get-Location).Path
$Pkg = Join-Path $Root "packaging"
$FfmpegDir = Join-Path $Pkg "ffmpeg\windows"
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build"

Write-Host ">>> Cleaning previous build"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }

Write-Host ">>> Ensuring pyinstaller is installed"
pyenv exec python -m pip install --quiet pyinstaller

Write-Host ">>> Fetching static ffmpeg"
New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
$FfmpegExe = Join-Path $FfmpegDir "ffmpeg.exe"
if (-not (Test-Path $FfmpegExe)) {
    $tmp = New-TemporaryFile
    Remove-Item $tmp
    $tmpDir = "$tmp.d"
    New-Item -ItemType Directory $tmpDir | Out-Null
    $zip = Join-Path $tmpDir "ffmpeg.zip"
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmpDir
    $found = Get-ChildItem -Recurse -Path $tmpDir -Filter "ffmpeg.exe" | Select-Object -First 1
    Copy-Item $found.FullName $FfmpegExe
    Remove-Item -Recurse -Force $tmpDir
}

Write-Host ">>> Running PyInstaller"
pyenv exec pyinstaller --clean --noconfirm packaging\pyinstaller.spec

$ExeOut = Join-Path $Dist "echo-library-builder.exe"
if (-not (Test-Path $ExeOut)) {
    Write-Error "PyInstaller did not produce $ExeOut"
    exit 1
}

Write-Host ">>> Done: $ExeOut"
