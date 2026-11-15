param(
    [Parameter(Mandatory=$true)][string]$KonomiTVRoot,
    [Parameter(Mandatory=$true)][string]$LanAddress,
    [Parameter(Mandatory=$true)][string]$RemoteSubnets
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in an Administrator PowerShell. Nothing was changed.'
}
$address = [Net.IPAddress]::Parse($LanAddress)
if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'An IPv4 LAN address is required.'
}
if (-not (Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $LanAddress)) {
    throw 'The specified LAN address is not assigned to this PC.'
}
$program = (Resolve-Path -LiteralPath (Join-Path $KonomiTVRoot 'server/thirdparty/Akebi/akebi-https-server.exe')).Path
$name = 'XA401-KonomiTV-LAN-HTTPS'
if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
    throw 'This rule already exists. Inspect it before changing or replacing it.'
}
New-NetFirewallRule -Name $name -DisplayName 'XA401 KonomiTV HTTPS (owned LAN)' `
    -Group 'XA401 KonomiTV' -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $LanAddress -LocalPort 7000 -RemoteAddress ($RemoteSubnets -split ',') `
    -Program $program -Profile Public,Private -Enabled True | Out-Null
Write-Output 'Allowed KonomiTV HTTPS port 7000 from the specified LAN subnets only.'
