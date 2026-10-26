# Phase 0 helper: capture the Elgato Stream Deck app <-> Network Dock traffic until the app drops the deck.
#
# What it does (all passive; it never sends anything to the dock and changes no settings):
#   * dumpcap ring-buffer capture of "host <dock> or udp port 5353" on the adapter that routes to the dock
#   * a 1 Hz ping log of the dock (separates "network gone" from "dock/app stopped talking")
#   * watches the Elgato app's own log for "SCAVENGER TIMED OUT" / "Device disconnected" (new lines only);
#     20 s after it appears it stops everything, merges the ring files and copies the Elgato logs next to them.
#
# BEFORE you run it: quit Deckoy and the Touch Portal SDDockBridge plugin. START the Elgato Stream Deck app yourself
# (the capture has to include its connection set-up), then use the deck normally until it drops.
# Run from a normal PowerShell window (run as Administrator only if dumpcap complains about permissions):
#     powershell -ExecutionPolicy Bypass -File .\capture_until_drop.ps1 -DockIp <your dock IP>
# Ctrl+C stops it early (files are still merged). Add -SelfTest to only check the adapter and log detection.
param(
    [Parameter(Mandatory = $true, HelpMessage = 'The IPv4 address of your Stream Deck Network Dock (see your router or the Elgato app device info), e.g. 192.168.1.50')]
    [string]$DockIp,
    [int]$MaxHours = 24,
    [int]$PostDropSeconds = 20,
    [int]$RingFiles = 24,
    [int]$RingMegabytes = 50,
    [string]$OutRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'captures'),
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
$wire = 'C:\Program Files\Wireshark'
$dumpcap = Join-Path $wire 'dumpcap.exe'
$mergecap = Join-Path $wire 'mergecap.exe'
$elgatoLog = Join-Path $env:APPDATA 'Elgato\StreamDeck\logs\StreamDeck.log'
$dropPattern = 'SCAVENGER TIMED OUT|Device disconnected|Dock disconnected'

function Get-DumpcapDevice([string]$ip) {
    $route = Find-NetRoute -RemoteIPAddress $ip | Where-Object { $_.IPAddress } | Select-Object -First 1
    if (-not $route) { throw "No route to $ip" }
    $ad = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex
    $dev = '\Device\NPF_' + $ad.InterfaceGuid
    $known = & $dumpcap -D | Out-String
    if ($known -notmatch [regex]::Escape($ad.InterfaceGuid)) { throw "dumpcap does not list adapter $($ad.Name) ($dev)" }
    [pscustomobject]@{ Device = $dev; Name = $ad.Name; LocalIp = $route.IPAddress }
}

# Returns the new matching lines since $state.Offset (and updates the state). Copes with log rotation/restart.
function Read-NewDropLines($path, $state, $pattern) {
    if (-not (Test-Path $path)) { return @() }
    $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
    try {
        if ($fs.Length -lt $state.Offset) { $state.Offset = 0 }   # file was replaced (app restarted)
        $null = $fs.Seek($state.Offset, 'Begin')
        $sr = New-Object System.IO.StreamReader($fs)
        $text = $sr.ReadToEnd()
        $state.Offset = $fs.Position
    } finally { $fs.Dispose() }
    @($text -split "`r?`n" | Where-Object { $_ -match $pattern })
}

if ($SelfTest) {
    $d = Get-DumpcapDevice $DockIp
    "adapter: $($d.Name)  local ip: $($d.LocalIp)  device: $($d.Device)"
    $tmp = Join-Path $env:TEMP 'deckoy_selftest.log'
    Set-Content $tmp "line one`n"
    $st = @{ Offset = (Get-Item $tmp).Length }
    Add-Content $tmp "2026 something ordinary`n2026 [X]: Upload Image  Ignore error: SCAVENGER TIMED OUT`n"
    $hit = Read-NewDropLines $tmp $st $dropPattern
    "detected new drop lines: $($hit.Count) (expected 1)"
    $none = Read-NewDropLines $tmp $st $dropPattern
    "second read finds nothing: $($none.Count -eq 0)"
    Remove-Item $tmp
    "elgato log present: $(Test-Path $elgatoLog)  mergecap present: $(Test-Path $mergecap)"
    return
}

$dev = Get-DumpcapDevice $DockIp
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $OutRoot $stamp
New-Item -ItemType Directory -Force $out | Out-Null
Write-Host "Capturing on '$($dev.Name)' ($($dev.LocalIp)) -> $out"
Write-Host "Make sure the Elgato Stream Deck app is (re)started NOW, after this capture began. Ctrl+C to stop early."

$pcapBase = Join-Path $out 'dock.pcapng'
$capArgs = @('-i', $dev.Device, '-f', "host $DockIp or udp port 5353", '-b', "filesize:$($RingMegabytes * 1024)",
          '-b', "files:$RingFiles", '-w', $pcapBase, '-q')
$cap = Start-Process -FilePath $dumpcap -ArgumentList $capArgs -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
if ($cap.HasExited) { throw "dumpcap exited immediately (exit code $($cap.ExitCode)). Try running PowerShell as Administrator." }

$pingJob = Start-Job -ArgumentList $DockIp, (Join-Path $out 'ping.log') -ScriptBlock {
    param($ip, $file)
    $p = New-Object System.Net.NetworkInformation.Ping
    while ($true) {
        try { $r = $p.Send($ip, 1000); $line = "{0:o} {1} {2}ms" -f (Get-Date), $r.Status, $r.RoundtripTime } catch { $line = "{0:o} Error {1}" -f (Get-Date), $_.Exception.Message }
        Add-Content -Path $file -Value $line
        Start-Sleep -Seconds 1
    }
}

$state = @{ Offset = 0 }
if (Test-Path $elgatoLog) { $state.Offset = (Get-Item $elgatoLog).Length }   # only react to NEW lines
$deadline = (Get-Date).AddHours($MaxHours)
$dropAt = $null
try {
    while ((Get-Date) -lt $deadline -and -not $cap.HasExited) {
        Start-Sleep -Seconds 2
        $hits = Read-NewDropLines $elgatoLog $state $dropPattern
        if ($hits.Count -gt 0) {
            $dropAt = Get-Date
            Write-Host "`nDROP DETECTED at $($dropAt.ToString('o')):"
            $hits | ForEach-Object { Write-Host "  $_" }
            Set-Content (Join-Path $out 'drop.txt') (@("detected_local_time=$($dropAt.ToString('o'))") + $hits)
            Write-Host "Capturing $PostDropSeconds more seconds..."
            Start-Sleep -Seconds $PostDropSeconds
            break
        }
    }
} finally {
    if (-not $cap.HasExited) { Stop-Process -Id $cap.Id -Force }
    Stop-Job $pingJob -ErrorAction SilentlyContinue; Remove-Job $pingJob -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    $parts = Get-ChildItem $out -Filter 'dock_*.pcapng' | Sort-Object Name
    if ($parts.Count -gt 0) {
        $merged = Join-Path $out 'dock_merged.pcapng'
        & $mergecap -w $merged ($parts | ForEach-Object FullName)
        Write-Host "Merged $($parts.Count) capture file(s) -> $merged"
    }
    foreach ($f in 'StreamDeck.log', 'StreamDeck.json') {
        $src = Join-Path (Split-Path $elgatoLog) $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $out "elgato_$f") -Force }
    }
    Write-Host "Done. Folder: $out"
    if ($dropAt) { Write-Host "Tell Claude: the folder above, and that the drop was at $($dropAt.ToString('HH:mm:ss'))." }
    else { Write-Host "No drop was detected (stopped early / timed out)." }
}
