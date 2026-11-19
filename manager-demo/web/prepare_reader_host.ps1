$ErrorActionPreference='Stop'
$dir = Join-Path $PSScriptRoot 'private'
if (!(Test-Path -LiteralPath $dir)){New-Item -ItemType Directory -Path $dir | Out-Null}
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$sids=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544','S-1-5-19')
foreach($sid in $sids) {
    $rights=if($sid -eq 'S-1-5-19'){'ReadAndExecute'}else{'FullControl'}
    $id=[Security.Principal.SecurityIdentifier]::new($sid)
    $rule=[Security.AccessControl.FileSystemAccessRule]::new($id,$rights,'ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $dir -AclObject $acl
$python = Join-Path $PSScriptRoot 'venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python)) {
    python -m venv (Join-Path $PSScriptRoot 'venv')
    if($LASTEXITCODE){throw 'Не удалось создать окружение'}
}
& $python -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: pywin32 cryptography
if($LASTEXITCODE){throw 'Не удалось подготовить окружение'}
& $python (Join-Path $PSScriptRoot 'prepare_reader_windows.py') csr $dir
if($LASTEXITCODE){throw 'Не удалось подготовить запрос на сертификат'}
Write-Output (Join-Path $dir 'server.csr')
