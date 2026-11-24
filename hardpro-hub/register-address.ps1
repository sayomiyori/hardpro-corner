param(
  [string]$Address = "hardpro.lan",
  [string]$Ip,
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
$aliases = @($Address, "hub." + $Address, "agent." + $Address, "chat." + $Address)
$marker = "# hardpro-hub"

$current = Get-Content $hosts -Raw
if ($Remove) {
  $lines = Get-Content $hosts | Where-Object { $_ -notmatch [regex]::Escape($marker) -and $_ -notmatch "hardpro\.lan" }
  Set-Content -Path $hosts -Value $lines -Encoding ASCII
  Write-Host "запись удалена из $hosts"
  return
}

if (-not $Ip) { throw 'Укажите адрес локального шлюза через -Ip.' }

if ($current -match [regex]::Escape($marker)) {
  Write-Host "уже зарегистрировано"
} else {
  try {
    Add-Content -Path $hosts -Value "`n$Ip`t$($aliases -join ' ')`t$marker" -Encoding ASCII
  } catch {
    throw "нужны права администратора: запусти этот скрипт от имени администратора"
  }
  Write-Host "добавлено: $Ip -> $($aliases -join ', ')"
}

foreach ($name in $aliases) {
  $p = Test-Connection -ComputerName $Ip -Count 1 -Quiet
  Write-Host ("  {0,-22} {1}" -f $name, $(if ($p) { "хост отвечает" } else { "нет пинга" }))
}
Write-Host ""
Write-Host "вход:"
Write-Host "  https://$Address`:8443/   доска моделей и замеры"
Write-Host "  https://$Address`:8444/   агент-разработка"
Write-Host "  https://$Address`:8775/   Open WebUI"
Write-Host "сертификат самоподписанный: принять один раз в браузере"
