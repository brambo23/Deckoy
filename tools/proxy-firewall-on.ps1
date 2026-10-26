# Deckoy proxy mode: block the Elgato Stream Deck app (StreamDeck.exe) from reaching the REAL dock.
#
# Adds exactly ONE Windows Firewall rule (outbound, block), filtered by program path AND remote address:
#       "Deckoy: block StreamDeck.exe -> dock"
# Deckoy.exe is a different program, so it can still reach the dock. Nothing else is changed.
# Undo: run proxy-firewall-off.ps1 (removes that one rule by name), or delete the rule in "Windows Defender Firewall
# with Advanced Security" -> Outbound Rules.
#
# This is a security setting, so Deckoy never runs it for you. Read it, then run it from an ADMINISTRATOR PowerShell:
#       powershell -ExecutionPolicy Bypass -File .\proxy-firewall-on.ps1 -DockIp <your dock IP>           (asks before changing anything)
#       powershell -ExecutionPolicy Bypass -File .\proxy-firewall-on.ps1 -DockIp <your dock IP> -WhatIf   (shows what it would do)
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true, HelpMessage = 'The IPv4 address of your Stream Deck Network Dock (see your router or the Elgato app device info), e.g. 192.168.1.50')]
    [string]$DockIp,
    [string]$ProgramPath = 'C:\Program Files\Elgato\StreamDeck\StreamDeck.exe',
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$ruleName = 'Deckoy: block StreamDeck.exe ' + [char]0x2192 + ' dock'

if ($DockIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "DockIp '$DockIp' is not an IPv4 address." }
if (-not (Test-Path $ProgramPath)) { throw "Program not found: $ProgramPath (pass -ProgramPath if Stream Deck is installed elsewhere)." }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference) { throw 'Run this from an Administrator PowerShell (it changes Windows Firewall).' }

Write-Host "Rule name : $ruleName"
Write-Host "Direction : Outbound   Action: Block   Protocol: Any"
Write-Host "Program   : $ProgramPath"
Write-Host "Remote IP : $DockIp"

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) { Write-Host 'The rule already exists; nothing to do.'; return }

if (-not $Yes -and -not $WhatIfPreference) {
    $answer = Read-Host 'Create this rule? (y/N)'
    if ($answer -notmatch '^[yY]') { Write-Host 'Cancelled.'; return }
}
if ($PSCmdlet.ShouldProcess($ruleName, 'New-NetFirewallRule')) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Outbound -Action Block -Program $ProgramPath `
        -RemoteAddress $DockIp -Protocol Any -Profile Any `
        -Description 'Created by Deckoy proxy mode: the Elgato app must reach the dock only through Deckoy. Remove with proxy-firewall-off.ps1.' | Out-Null
    Write-Host 'Rule created. Restart the Elgato Stream Deck app so it reconnects through Deckoy.'
}
