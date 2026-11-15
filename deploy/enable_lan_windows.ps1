param(
    [string]$KonomiTVRoot,
    [string]$ProgramPath,
    [ValidateRange(1,65535)][int]$Port = 7000,
    [Parameter(Mandatory=$true)][string]$LanAddress,
    [Parameter(Mandatory=$true)][string]$RemoteSubnets,
    [switch]$InspectOnly
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $InspectOnly -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
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
$conflicts = @()
foreach ($block in (Get-NetFirewallRule -Enabled True -Direction Inbound -Action Block)) {
    $application = $block | Get-NetFirewallApplicationFilter
    if ($application.Program -ine $program -or $block.Name -notlike 'TCP Query User{*') { continue }
    $filter = $block | Get-NetFirewallPortFilter
    if ($filter.Protocol -notin @('TCP', '6')) { continue }
    if (@($filter.LocalPort).Count -eq 1 -and $filter.LocalPort -eq 'Any') {
        $conflicts += [pscustomobject]@{Name=$block.Name; Program=$application.Program;
            Profile=[string]$block.Profile; OriginalLocalPort=@($filter.LocalPort)}
    }
}
$remaining = @()
if ($Port -gt 1) { $remaining += "1-$($Port-1)" }
if ($Port -lt 65535) { $remaining += "$($Port+1)-65535" }
if ($InspectOnly) {
    [pscustomobject]@{Program=$program; Port=$Port; LocalAddress=$LanAddress;
        RemoteSubnets=$RemoteSubnets; ConflictingAutomaticTCPBlockRules=$conflicts;
        RemainingBlockedTCPPorts=$remaining;
        ChangesMade=$false} | ConvertTo-Json -Depth 5
    return
}
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
if ($conflicts.Count -gt 0) {
    $auditDir = Join-Path $env:LOCALAPPDATA 'XA401/firewall-backups'
    New-Item -ItemType Directory -Path $auditDir -Force | Out-Null
    $auditPath = Join-Path $auditDir ((Get-Date -Format 'yyyyMMddTHHmmssfff') + '.json')
    $conflicts | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $auditPath -Encoding UTF8
    foreach ($entry in $conflicts) {
        Get-NetFirewallRule -Name $entry.Name | Get-NetFirewallPortFilter |
            Set-NetFirewallPortFilter -LocalPort $remaining | Out-Null
    }
    Write-Output "Excluded only TCP port $Port from $($conflicts.Count) matching automatic block rule(s); all other ports remain blocked."
    Write-Output "Previous port scope recorded in $auditPath"
}
Write-Output "Allowed KonomiTV HTTPS port $Port from the specified LAN subnets only."
