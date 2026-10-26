# Deckoy proxy mode: remove the one firewall rule added by proxy-firewall-on.ps1.
# Run from an ADMINISTRATOR PowerShell:  powershell -ExecutionPolicy Bypass -File .\proxy-firewall-off.ps1
# Only the rule named "Deckoy: block StreamDeck.exe -> dock" is removed; no other rule is touched.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$AlsoDeckPad     # also remove a leftover rule from the DeckPad app, if you ever ran it
)
$ErrorActionPreference = 'Stop'
$ruleName = 'Deckoy: block StreamDeck.exe ' + [char]0x2192 + ' dock'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $WhatIfPreference) { throw 'Run this from an Administrator PowerShell (it changes Windows Firewall).' }

$rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $rule) { Write-Host "No rule named '$ruleName' exists; nothing to remove." }
elseif ($PSCmdlet.ShouldProcess($ruleName, 'Remove-NetFirewallRule')) {
    $rule | Remove-NetFirewallRule
    Write-Host 'Rule removed. The Elgato app can reach the dock directly again (quit Deckoy first).'
}

if ($AlsoDeckPad) {
    $oldName = 'DeckPad: block StreamDeck.exe ' + [char]0x2192 + ' dock'
    $old = Get-NetFirewallRule -DisplayName $oldName -ErrorAction SilentlyContinue
    if (-not $old) { Write-Host "No rule named '$oldName' exists; nothing to remove." }
    elseif ($PSCmdlet.ShouldProcess($oldName, 'Remove-NetFirewallRule')) {
        $old | Remove-NetFirewallRule
        Write-Host 'Leftover DeckPad rule removed.'
    }
}
