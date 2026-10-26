# Builds dist\Deckoy\Deckoy.exe (PyInstaller onedir, windowed) and dist\Deckoy-1.0.0-win64.zip.
# Usage:  .\build.ps1            (runs the tests first)
#         .\build.ps1 -SkipTests
param([switch]$SkipTests)
# Native tools (pytest, PyInstaller) write progress to stderr, which 'Stop' would turn into a failure in Windows PowerShell 5.1:
# every native call is checked through $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
$version = (Select-String -Path deckoy\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$zip = "dist\Deckoy-$version-win64.zip"

if (-not $SkipTests) {
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; not building.' }
}

python tools\make_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist\Deckoy) { Remove-Item dist\Deckoy -Recurse -Force }

python -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name Deckoy `
    --icon assets\deckoy.ico `
    --collect-submodules zeroconf `
    --exclude-module tkinter `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml `
    run_deckoy.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

# Freshly written files are sometimes briefly locked by antivirus scanners: retry the zip a few times.
for ($i = 1; $i -le 6; $i++) {
    try {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path dist\Deckoy -DestinationPath $zip -ErrorAction Stop
        break
    } catch {
        if ($i -eq 6) { throw }
        Start-Sleep -Seconds 3
    }
}
if (-not $SkipTests) {
    # The built exe must really quit: run the frozen-exe quit tests against it.
    $env:DECKOY_TEST_FROZEN = '1'
    python -m pytest -q tests\test_quit_process.py -k frozen
    $frozenExit = $LASTEXITCODE
    Remove-Item Env:\DECKOY_TEST_FROZEN
    if ($frozenExit -ne 0) { throw 'The built Deckoy.exe failed its quit tests.' }
}
$exe = Get-Item dist\Deckoy\Deckoy.exe
$mb = [math]::Round((Get-ChildItem dist\Deckoy -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Built $($exe.FullName) (folder $mb MB) and $zip"
