<#
Читатель 1С на Hoster: держит открытый COM-сеанс и обратный SSH-туннель к SpaceWeb.
Запускается по расписанию (каждые две минуты) и при загрузке сервера.

    powershell -NoProfile -ExecutionPolicy Bypass -File reader_watchdog.ps1
#>
[CmdletBinding()]
param(
    [string] $Root = 'E:\Work\Projects\Codex\hardpro-manager-reader',
    [int] $Port = 18771,
    [string] $TunnelTarget = 'hp-onec-hoster@195.208.123.14'
)

$ErrorActionPreference = 'Stop'
$log = Join-Path $Root 'watchdog.log'

function Write-Log([string] $Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Write-Verbose $line
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
}

function Test-Listener([int] $LocalPort) {
    [bool] (Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue)
}

function Get-ReaderPython([string] $Dir) {
    $candidates = @(
        (Join-Path $Dir '.venv\Scripts\python.exe'),
        (Join-Path $Dir 'venv\Scripts\python.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
    $found = $candidates | Select-Object -First 1
    if ($found) { return $found }

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
        $found = @($parent.ExecutablePath, $proc.ExecutablePath) |
            Where-Object { $_ -like '*\python.exe' } | Select-Object -First 1
    }
    return $found
}

function Start-Reader([string] $Dir) {
    $python = Get-ReaderPython $Dir
    if (-not $python) { throw "Не найден python читателя в $Dir" }
    Start-Process -FilePath $python -ArgumentList '-B', 'onec_reader_windows.py', 'reader.json' `
        -WorkingDirectory $Dir -WindowStyle Hidden
    Write-Log "запущен читатель: $python"
}

function Get-TunnelProcess {
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$TunnelTarget*" -and $_.CommandLine -like "*18771*" }
}

function Start-Tunnel([string] $Dir) {
    $key = Join-Path $Dir 'ssh-spaceweb\id_ed25519'
    $known = Join-Path $Dir 'ssh-spaceweb\known_hosts'
    if (-not (Test-Path -LiteralPath $key) -or -not (Test-Path -LiteralPath $known)) {
        throw 'Нет ключа или known_hosts для обратного туннеля'
    }
    $forward = "127.0.0.1:${Port}:127.0.0.1:${Port}"
    $arguments = @(
        '-F', 'NUL', '-NT',
        '-i', $key,
        '-o', "UserKnownHostsFile=$known",
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'HostKeyAlgorithms=ssh-ed25519',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-R', $forward,
        $TunnelTarget
    )
    Start-Process -FilePath 'ssh.exe' -ArgumentList $arguments -WindowStyle Hidden
    Write-Log "запущен обратный туннель: $forward"
}

if (-not (Test-Path -LiteralPath $Root)) { throw "Нет каталога читателя: $Root" }

if (-not (Test-Listener $Port)) {
    Write-Log "порт $Port не слушает, читатель перезапускается"
    Start-Reader $Root
    Start-Sleep -Seconds 5
    if (-not (Test-Listener $Port)) { Write-Log "читатель не поднялся за 5 секунд" }
}

if (-not (Get-TunnelProcess)) {
    Write-Log 'обратный туннель не найден, запуск'
    Start-Tunnel $Root
}

# Ротация журнала: держим последние 400 строк.
if (Test-Path -LiteralPath $log) {
    $tail = Get-Content -LiteralPath $log -Tail 400 -Encoding utf8
    Set-Content -LiteralPath $log -Value $tail -Encoding utf8
}
