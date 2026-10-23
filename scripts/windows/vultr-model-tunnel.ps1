param([ValidateSet('Run', 'Status', 'Stop')][string]$Action = 'Status')

# No passwords, private-key contents, global proxies or remote commands.
$ErrorActionPreference = 'Stop'
$tunnelHome = Join-Path $env:USERPROFILE '.tradingagents\tunnel'
$configPath = Join-Path $tunnelHome 'vultr-config.json'
$statePath = Join-Path $tunnelHome 'state.json'
$stopPath = Join-Path $tunnelHome 'stop.flag'
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($settings.SshUser -ne 'ta-tunnel' -or $settings.HostName -notmatch '^[a-zA-Z0-9.-]+$' -or
    [int]$settings.LocalPort -lt 1024 -or [int]$settings.LocalPort -gt 65535 -or
    -not (Test-Path -LiteralPath $settings.IdentityFile -PathType Leaf)) {
    throw 'Invalid dedicated tunnel configuration'
}

if ($Action -eq 'Status') {
    $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $settings.LocalPort -State Listen -ErrorAction SilentlyContinue)
    if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath }
    Write-Output ('LocalListener=' + ($listeners.Count -gt 0))
    exit 0
}

if ($Action -eq 'Stop') {
    New-Item -ItemType File -Path $stopPath -Force | Out-Null
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $child = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$state.SshPid) -ErrorAction SilentlyContinue
        # Avoid terminating an unrelated process if a stale PID was recycled.
        if ($child -and $child.Name -eq 'ssh.exe' -and $child.ParentProcessId -eq $state.WorkerPid -and
            $child.CommandLine.Contains('ta-tunnel@' + $settings.HostName) -and
            $child.CommandLine.Contains($settings.IdentityFile)) {
            Stop-Process -Id $child.ProcessId
        }
    }
    Write-Output 'Tunnel stop requested; model requests remain configured to fail closed.'
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\TradingAgentsVultrModelTunnel')
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath }
    while (-not (Test-Path -LiteralPath $stopPath)) {
        # Refuse to take over a port occupied by another service.
        $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $settings.LocalPort -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -gt 0) { throw 'Tunnel port already occupied; existing process left untouched' }
        $sshArguments = @('-N', '-D', ('127.0.0.1:' + $settings.LocalPort),
            '-i', ('"' + $settings.IdentityFile + '"'),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-p', [string]$settings.SshPort, ($settings.SshUser + '@' + $settings.HostName))
        $child = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArguments -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $tunnelHome 'ssh.stderr.log') `
            -RedirectStandardOutput (Join-Path $tunnelHome 'ssh.stdout.log')
        @{ WorkerPid = $PID; SshPid = $child.Id; LocalPort = $settings.LocalPort;
           StartedAt = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $statePath
        $child.WaitForExit()
        if (-not (Test-Path -LiteralPath $stopPath)) { Start-Sleep -Seconds 5 }
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
