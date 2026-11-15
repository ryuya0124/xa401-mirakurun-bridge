param(
    [string]$KonomiTVRoot,
    [string]$ProgramPath,
    [ValidateRange(1,65535)][int]$Port = 7000,
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
$program = if ($ProgramPath) { (Resolve-Path -LiteralPath $ProgramPath).Path } else {
    (Resolve-Path -LiteralPath (Join-Path $KonomiTVRoot 'server/thirdparty/Akebi/akebi-https-server.exe')).Path
}
$name = 'XA401-KonomiTV-LAN-HTTPS'
$existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Group -ne 'XA401 KonomiTV') { throw 'Existing rule is not owned by this setup.' }
    Set-NetFirewallRule -Name $name -Direction Inbound -Action Allow -Protocol TCP `
        -LocalAddress $LanAddress -LocalPort $Port -RemoteAddress ($RemoteSubnets -split ',') `
        -Program $program -Profile Public,Private -Enabled True | Out-Null
} else {
    New-NetFirewallRule -Name $name -DisplayName 'XA401 KonomiTV HTTPS (owned LAN)' `
        -Group 'XA401 KonomiTV' -Direction Inbound -Action Allow -Protocol TCP `
        -LocalAddress $LanAddress -LocalPort $Port -RemoteAddress ($RemoteSubnets -split ',') `
        -Program $program -Profile Public,Private -Enabled True | Out-Null
}
Write-Output "Allowed KonomiTV HTTPS port $Port from the specified LAN subnets only."
