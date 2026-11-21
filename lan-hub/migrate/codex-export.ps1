param(
  [string]$CodexHome = "$env:USERPROFILE\.codex",
  [string]$ProjectsRoot = "D:\\Projects",
  [string]$SshHost = "llm-host",
  [string]$RemoteStaging = "/home/hp_local_llm/hardpro/incoming",
  [string]$OutFile = "$env:TEMP\hardpro-codex-export.tgz",
  [switch]$SkipSessions,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Write-Host "== HARD-PRO codex export =="
Write-Host "codex home : $CodexHome"
Write-Host "projects   : $ProjectsRoot"
Write-Host "remote     : ${SshHost}:$RemoteStaging"

if (-not (Test-Path $CodexHome)) { throw "нет каталога $CodexHome" }

$stage = Join-Path $env:TEMP "hardpro-export-$stamp"
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# --- manifest of what we carry ---
function Copy-Tree([string]$from, [string]$to, [string[]]$exclude = @()) {
  if (-not (Test-Path $from)) { return }
  New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
  $args = @($from, $to, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
            "/R:1", "/W:1", "/XD")
  if ($exclude.Count) { $args += $exclude }
  & robocopy @args | Out-Null
}

$codexDirs = @("sessions", "archived_sessions", "skills", "plugins", "rules", "plans",
               "automations", "memories_v2", "browser", "memories_1.sqlite",
               "memories_v2_1.sqlite", "state_5.sqlite", "thread_history_1.sqlite",
               "session_index.jsonl", "history.jsonl", "AGENTS.md", "config.toml",
               "models_cache.json", "version.json", "goals_1.sqlite")
if ($SkipSessions) {
  $codexDirs = $codexDirs | Where-Object { $_ -notin @("sessions", "archived_sessions",
    "thread_history_1.sqlite", "state_5.sqlite", "session_index.jsonl", "history.jsonl") }
}

$excludeNames = "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build",
                ".next", ".turbo", "target"

foreach ($item in $codexDirs) {
  $src = Join-Path $CodexHome $item
  if (-not (Test-Path $src)) { continue }
  $dst = Join-Path (Join-Path $stage "codex") $item
  if ((Get-Item $src).PSIsContainer) {
    Copy-Tree $src $dst $excludeNames
  } else {
    New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
    Copy-Item $src $dst -Force
  }
  Write-Host "  + codex/$item"
}

# --- project sources: git remotes and small trees, not bulk media ---
if (Test-Path $ProjectsRoot) {
  $projects = Get-ChildItem $ProjectsRoot -Directory | Where-Object { $_.Name -notin @("ChatGPT") }
  foreach ($p in $projects) {
    $dst = Join-Path (Join-Path $stage "projects") $p.Name
    if (Test-Path (Join-Path $p.FullName ".git")) {
      Push-Location $p.FullName
      try { $remote = (git remote get-url origin 2>$null) } catch { $remote = $null }
      Pop-Location
      if ($remote) {
        $meta = @{ name = $p.Name; remote = $remote; path = $p.FullName }
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        $meta | ConvertTo-Json | Set-Content (Join-Path $dst ".hardpro-remote.json") -Encoding UTF8
        Write-Host "  + projects/$($p.Name) (git: $remote)"
        continue
      }
    }
    $size = (Get-ChildItem $p.FullName -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object -Sum Length).Sum
    if ($size -gt 2GB) {
      New-Item -ItemType Directory -Force -Path $dst | Out-Null
      @{ name = $p.Name; path = $p.FullName; skipped = $true; bytes = $size } |
        ConvertTo-Json | Set-Content (Join-Path $dst ".hardpro-skipped.json") -Encoding UTF8
      Write-Host "  ~ projects/$($p.Name) пропущен (${size}B > 2GB), записан маркер"
      continue
    }
    Copy-Tree $p.FullName $dst $excludeNames
    Write-Host "  + projects/$($p.Name)"
  }
}

# --- pack ---
@{ created = (Get-Date).ToString("o"); host = $env:COMPUTERNAME; user = $env:USERNAME;
   codex_home = $CodexHome; projects_root = $ProjectsRoot; skip_sessions = [bool]$SkipSessions } |
  ConvertTo-Json | Set-Content (Join-Path $stage "manifest.json") -Encoding UTF8

if ($DryRun) { Write-Host "dry run: $stage"; return }

if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
Write-Host "packing -> $OutFile"
& tar -czf $OutFile -C $stage .
if ($LASTEXITCODE -ne 0) { throw "tar завершился с кодом $LASTEXITCODE" }

$bytes = (Get-Item $OutFile).Length
Write-Host ("archive: {0:N1} MB" -f ($bytes / 1MB))

Write-Host "uploading to $SshHost"
& ssh $SshHost "mkdir -p $RemoteStaging"
if ($LASTEXITCODE -ne 0) { throw "ssh $SshHost недоступен" }
& scp $OutFile "${SshHost}:$RemoteStaging/codex-export-$stamp.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp не удался" }

Write-Host "importing on server"
& ssh $SshHost "cd ~/hardpro && python3 bin/codex-import.py $RemoteStaging/codex-export-$stamp.tgz"
if ($LASTEXITCODE -ne 0) { throw "импорт на сервере упал" }

Remove-Item $stage -Recurse -Force
Write-Host "готово: $RemoteStaging/codex-export-$stamp.tgz"
