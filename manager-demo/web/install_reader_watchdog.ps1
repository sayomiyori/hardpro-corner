<#
Ставит сторожа читателя 1С в планировщик Hoster: запуск при загрузке и каждые две минуты.
Нужно выполнить один раз в PowerShell от администратора на самом Hoster:

    powershell -NoProfile -ExecutionPolicy Bypass -File install_reader_watchdog.ps1

Задача работает от SYSTEM, поэтому читатель и туннель поднимаются до входа пользователя
в RDP. После отключения питания это единственное, что нужно серверу: BIOS с настройкой
«Restore AC Power Loss = Power On» включает его сам, задача поднимает канал к 1С.
#>
[CmdletBinding()]
param(
    [string] $Root = 'E:\Work\Projects\Codex\hardpro-manager-reader',
    [string] $TaskName = 'HARD-PRO 1C reader watchdog'
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Запустите PowerShell от администратора'
}

$watchdog = Join-Path $Root 'reader_watchdog.ps1'
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Нет файла сторожа: $watchdog (сначала скопируйте reader_watchdog.ps1)"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`"" -WorkingDirectory $Root
$trigger = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration ([TimeSpan]::MaxValue)
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'Держит читатель 1С и обратный SSH-туннель' -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime, LastTaskResult

$listening = Get-NetTCPConnection -State Listen -LocalPort 18771 -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host 'Читатель 1С слушает 127.0.0.1:18771'
} else {
    Write-Warning 'Порт 18771 не слушает: смотрите watchdog.log и права HP_Manager_Read'
}
