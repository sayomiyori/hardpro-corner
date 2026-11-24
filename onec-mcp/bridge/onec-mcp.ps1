# onec-bridge: HTTP-мост к 1С через V83.COMConnector.
# Живёт на Windows, где зарегистрирован COMConnector и есть сеть до кластера 1С.
# Слушает только 127.0.0.1; наружу выставляется SSH-туннелем на hardpro-llm.
#
# Запуск: powershell -NoProfile -ExecutionPolicy Bypass -File bridge.ps1
# Проверка: curl http://127.0.0.1:8774/health

param([string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'))

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$script:cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$script:connection = $null
$script:connector = $null
$script:connectedAt = $null
$script:startedAt = Get-Date
$script:AuditPath = $script:cfg.audit

if ($script:AuditPath) {
    $dir = Split-Path -Parent $script:AuditPath
    if ($dir) { [IO.Directory]::CreateDirectory($dir) | Out-Null }
}

function Write-Audit([string]$Op, $Args, [bool]$Ok, $Millis, [string]$Err) {
    if (-not $script:AuditPath) { return }
    try {
        $entry = @{ ts = (Get-Date).ToString('o'); op = $Op; ok = $Ok; ms = $Millis; args = $Args; error = $Err }
        Add-Content -LiteralPath $script:AuditPath -Value ($entry | ConvertTo-Json -Depth 10 -Compress) -Encoding UTF8
    } catch { }
}

# ---------- COM helpers ----------

function Get-ComProperty($Object, [string]$Name) {
    return , $Object.GetType().InvokeMember($Name, [Reflection.BindingFlags]::GetProperty, $null, $Object, $null)
}
function Set-ComProperty($Object, [string]$Name, $Value) {
    $Object.GetType().InvokeMember($Name, [Reflection.BindingFlags]::SetProperty, $null, $Object, @($Value)) | Out-Null
}
function Invoke-ComMethod($Object, [string]$Name, [object[]]$Arguments = @()) {
    return , $Object.GetType().InvokeMember($Name, [Reflection.BindingFlags]::InvokeMethod, $null, $Object, $Arguments)
}
function Test-ComProperty($Object, [string]$Name) {
    try { $null = Get-ComProperty $Object $Name; return $true } catch { return $false }
}
function Get-PropAny($Object, [string[]]$Names) {
    foreach ($name in $Names) {
        if (Test-ComProperty $Object $name) { return Get-ComProperty $Object $name }
    }
    throw ('нет свойства: ' + ($Names -join '/'))
}
function Get-UuidText($Reference) {
    try {
        $uuid = Invoke-ComMethod $Reference 'УникальныйИдентификатор'
        return $script:connection.String($uuid).Trim('{', '}')
    } catch { return $null }
}

function Close-Base {
    try {
        if ($null -ne $script:connection) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($script:connection) | Out-Null }
        if ($null -ne $script:connector) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($script:connector) | Out-Null }
    } catch { }
    $script:connection = $null
    $script:connector = $null
    $script:connectedAt = $null
    [GC]::Collect()
}

function Connect-Base {
    if ($null -ne $script:connection) { return }
    $credential = Import-Clixml -LiteralPath $script:cfg.credentials
    if ($credential -isnot [Management.Automation.PSCredential]) { throw 'битый файл учётных данных' }
    $password = $credential.GetNetworkCredential().Password
    $user = $credential.UserName
    $q = { param($v) '"' + ([string]$v).Replace('"', '""') + '"' }
    $cs = $script:cfg.database + 'Usr=' + (& $q $user) + ';Pwd=' + (& $q $password) + ';'
    try {
        $script:connector = New-Object -ComObject V83.COMConnector
        $script:connection = $script:connector.Connect($cs)
        if ($null -eq $script:connection) { throw '1С вернула пустое соединение' }
        $script:connectedAt = Get-Date
    } catch {
        $hresult = $_.Exception.HResult
        Close-Base
        # Текст COM-ошибки может содержать строку подключения — наружу отдаём только HRESULT
        throw ('подключение к 1С не удалось, HRESULT 0x{0:X8}; сеть, лицензия, логин или техработы' -f $hresult)
    } finally {
        $password = $null; $cs = $null; $credential = $null
    }
}

function Convert-Cell($Value) {
    if ($null -eq $Value -or $Value -is [DBNull]) { return $null }
    if ($Value -is [bool] -or $Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) { return $Value }
    if ($Value -is [datetime]) { return $Value.ToString('o') }
    if ($Value -is [string]) { return $Value }
    if ([Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        $presentation = $script:connection.String($Value)
        $uuid = Get-UuidText $Value
        if ($uuid) { return @{ ref = $uuid; presentation = $presentation } }
        return @{ value = $presentation }
    }
    return $script:connection.String($Value)
}

# ---------- метаданные ----------

$script:Categories = @(
    @{ key = 'Catalog'; meta = @('Справочники', 'Catalogs'); managers = @('Справочники', 'Catalogs'); full = 'Справочник'; write = $true }
    @{ key = 'Document'; meta = @('Документы', 'Documents'); managers = @('Документы', 'Documents'); full = 'Документ'; write = $true }
    @{ key = 'InformationRegister'; meta = @('РегистрыСведений', 'InformationRegisters'); managers = @('РегистрыСведений', 'InformationRegisters'); full = 'РегистрСведений'; write = $false }
    @{ key = 'AccumulationRegister'; meta = @('РегистрыНакопления', 'AccumulationRegisters'); managers = @('РегистрыНакопления', 'AccumulationRegisters'); full = 'РегистрНакопления'; write = $false }
    @{ key = 'Constant'; meta = @('Константы', 'Constants'); managers = @('Константы', 'Constants'); full = 'Константа'; write = $false }
    @{ key = 'Enum'; meta = @('Перечисления', 'Enums'); managers = @('Перечисления', 'Enums'); full = 'Перечисление'; write = $false }
    @{ key = 'ChartOfCharacteristicTypes'; meta = @('ПланыВидовХарактеристик', 'ChartsOfCharacteristicTypes'); managers = @('ПланыВидовХарактеристик', 'ChartsOfCharacteristicTypes'); full = 'ПланВидовХарактеристик'; write = $false }
    @{ key = 'ChartOfAccounts'; meta = @('ПланыСчетов', 'ChartsOfAccounts'); managers = @('ПланыСчетов', 'ChartsOfAccounts'); full = 'ПланСчетов'; write = $false }
    @{ key = 'BusinessProcess'; meta = @('БизнесПроцессы', 'BusinessProcesses'); managers = @('БизнесПроцессы', 'BusinessProcesses'); full = 'БизнесПроцесс'; write = $false }
    @{ key = 'Task'; meta = @('Задачи', 'Tasks'); managers = @('Задачи', 'Tasks'); full = 'Задача'; write = $false }
    @{ key = 'ExchangePlan'; meta = @('ПланыОбмена', 'ExchangePlans'); managers = @('ПланыОбмена', 'ExchangePlans'); full = 'ПланОбмена'; write = $false }
)

function Get-Category([string]$Key) {
    foreach ($c in $script:Categories) { if ($c.key -eq $Key) { return $c } }
    return $null
}
function Get-MetadataCollection($Category) {
    return Get-PropAny (Get-ComProperty $script:connection 'Metadata') $Category.meta
}
function Get-Managers($Category) {
    return Get-PropAny $script:connection $Category.managers
}

function Resolve-Object([string]$Name) {
    $raw = ([string]$Name).Trim()
    if (-not $raw) { throw 'не задан объект' }

    $category = $null
    $short = $raw

    if ($raw -match '^([A-Za-z]+)_(.+)$') {
        $category = Get-Category $Matches[1]
        if ($null -eq $category) { throw "неизвестный префикс объекта: $($Matches[1])" }
        $short = $Matches[2]
    } elseif ($raw -match '^([^\.]+)\.(.+)$') {
        $plural = $Matches[1]
        foreach ($c in $script:Categories) { if ($c.full -eq $plural -or $c.meta -contains $plural) { $category = $c } }
        if ($null -eq $category) { throw "неизвестный вид объекта: $plural" }
        $short = $Matches[2]
    }

    if ($null -ne $category) {
        $collection = Get-MetadataCollection $category
        for ($i = 0; $i -lt (Invoke-ComMethod $collection 'Count'); $i++) {
            $md = Invoke-ComMethod $collection 'Get' @($i)
            if ((Get-ComProperty $md 'Имя') -eq $short) {
                return @{ category = $category; name = $short; md = $md; full = ($category.full + '.' + $short) }
            }
        }
        throw "объект $raw не найден"
    }

    $found = @()
    foreach ($c in $script:Categories) {
        try { $collection = Get-MetadataCollection $c } catch { continue }
        for ($i = 0; $i -lt (Invoke-ComMethod $collection 'Count'); $i++) {
            $md = Invoke-ComMethod $collection 'Get' @($i)
            if ((Get-ComProperty $md 'Имя') -eq $raw) {
                $found += @{ category = $c; name = $raw; md = $md; full = ($c.full + '.' + $raw) }
            }
        }
    }
    if ($found.Count -eq 1) { return $found[0] }
    if ($found.Count -eq 0) { throw "объект $raw не найден; имя вида Catalog_Номенклатура или Справочник.Номенклатура" }
    throw ('объект ' + $raw + ' есть в нескольких видах: ' + (($found | ForEach-Object { $_.full }) -join ', '))
}

function Assert-Allowed([string]$FullName) {
    foreach ($pattern in @($script:cfg.deny)) {
        if ($pattern -and ($FullName -like $pattern)) { throw "объект $FullName запрещён в deny" }
    }
    $allow = @($script:cfg.allow)
    if ($allow.Count -gt 0) {
        foreach ($pattern in $allow) { if ($pattern -and ($FullName -like $pattern)) { return } }
        throw "объект $FullName не входит в allow ($($allow -join ', '))"
    }
}

function Find-InCollection($Collection, [string]$Name) {
    for ($i = 0; $i -lt (Invoke-ComMethod $Collection 'Count'); $i++) {
        $item = Invoke-ComMethod $Collection 'Get' @($i)
        if ((Get-ComProperty $item 'Имя') -eq $Name) { return $item }
    }
    return $null
}

function Get-Attribute($Md, [string]$Name) {
    # Свои реквизиты, затем стандартные (Наименование, Код, Дата, Номер, Проведен)
    $found = Find-InCollection (Get-ComProperty $Md 'Реквизиты') $Name
    if ($null -ne $found) { return $found }
    try { return Find-InCollection (Get-ComProperty $Md 'СтандартныеРеквизиты') $Name } catch { return $null }
}

function Get-RefMetadata($Attr) {
    try {
        $types = Get-ComProperty $Attr 'Тип'
        if ((Invoke-ComMethod $types 'Count') -ne 1) { return $null }
        $meta = Get-ComProperty $script:connection 'Metadata'
        $found = Invoke-ComMethod $meta 'НайтиПоТипу' @((Invoke-ComMethod $types 'Get' @(0)))
        return $found
    } catch { return $null }
}

function Get-ManagerFor($Target) {
    # Менеджер через NewObject: у коллекции менеджеров в COM нет Найти
    return $script:connection.NewObject($Target.category.full + 'Менеджер.' + $Target.name)
}

function Set-PrimitiveValue($Object, [string]$name, $Raw) {
    if ($null -eq $Raw) { Set-ComProperty $Object $name $null; return }
    $candidates = @()
    if ($Raw -is [bool]) { $candidates += , $Raw }
    elseif ($Raw -is [int] -or $Raw -is [long] -or $Raw -is [double] -or $Raw -is [decimal]) { $candidates += , [double]$Raw }
    elseif ($Raw -is [string]) {
        $candidates += , $Raw
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParse($Raw, [ref]$parsed)) { $candidates += , $parsed }
    } else { $candidates += , $Raw }
    $lastError = $null
    foreach ($candidate in $candidates) {
        try { Set-ComProperty $Object $name $candidate; return } catch { $lastError = $_.Exception.Message }
    }
    throw "поле $name не приняло значение '$Raw': $lastError"
}

function Set-AttributeValue($Object, $Attr, $Raw) {
    $name = Get-ComProperty $Attr 'Имя'
    if ($null -eq $Raw) { Set-ComProperty $Object $name $null; return }

    $refMd = Get-RefMetadata $Attr
    if ($null -ne $refMd) {
        $target = Resolve-Object ([string](Invoke-ComMethod $refMd 'ПолноеИмя'))
        $manager = Get-ManagerFor $target
        $uuidText = if ($Raw -is [string]) { $Raw } else { [string]$Raw.ref }
        if (-not $uuidText -or $uuidText -match '^0{8}-0{4}-0{4}-0{4}-0{12}$') {
            Set-ComProperty $Object $name (Invoke-ComMethod $manager 'ПустаяСсылка')
            return
        }
        $uuid = $script:connection.NewObject('УникальныйИдентификатор', $uuidText)
        Set-ComProperty $Object $name (Invoke-ComMethod $manager 'ПолучитьСсылку' @($uuid))
        return
    }

    Set-PrimitiveValue $Object $name $Raw
}

function Set-Fields($Object, $Md, $Data) {
    $applied = @()
    if ($null -eq $Data) { return $applied }
    foreach ($p in @($Data.PSObject.Properties)) {
        $attr = Get-Attribute $Md $p.Name
        if ($null -eq $attr) {
            # Стандартные реквизиты в метаданных COM не всегда видны, но на объекте есть
            if (-not (Test-ComProperty $Object $p.Name)) {
                throw "у объекта $(Get-ComProperty $Md 'Имя') нет поля $($p.Name)"
            }
            Set-PrimitiveValue $Object $p.Name $p.Value
            $applied += $p.Name
            continue
        }
        Set-AttributeValue $Object $attr $p.Value
        $applied += $p.Name
    }
    return $applied
}

function Read-Object($Md, $Object) {
    $fields = [ordered]@{}
    $attrs = Get-ComProperty $Md 'Реквизиты'
    for ($i = 0; $i -lt (Invoke-ComMethod $attrs 'Count'); $i++) {
        $attr = Invoke-ComMethod $attrs 'Get' @($i)
        $name = Get-ComProperty $attr 'Имя'
        try { $fields[$name] = Convert-Cell (Get-ComProperty $Object $name) } catch { }
    }
    foreach ($stdName in @('Наименование', 'Код', 'Артикул', 'Дата', 'Номер', 'Проведен', 'ПометкаУдаления')) {
        if ($fields.Contains($stdName)) { continue }
        try { if (Test-ComProperty $Object $stdName) { $fields[$stdName] = Convert-Cell (Get-ComProperty $Object $stdName) } } catch { }
    }
    $tables = [ordered]@{}
    $tab = Get-ComProperty $Md 'ТабличныеЧасти'
    for ($i = 0; $i -lt (Invoke-ComMethod $tab 'Count'); $i++) {
        $t = Invoke-ComMethod $tab 'Get' @($i)
        $tables[(Get-ComProperty $t 'Имя')] = (Invoke-ComMethod (Get-ComProperty $Object (Get-ComProperty $t 'Имя')) 'Count')
    }
    return @{ fields = $fields; tabularSections = $tables }
}

function Get-ObjectByRef($Target, [string]$Ref) {
    if (-not $Ref) { throw 'не задан ref' }
    $manager = Get-ManagerFor $Target
    $uuid = $script:connection.NewObject('УникальныйИдентификатор', $Ref)
    $refValue = Invoke-ComMethod $manager 'ПолучитьСсылку' @($uuid)
    $object = Invoke-ComMethod $refValue 'ПолучитьОбъект'
    if ($null -eq $object) { throw "объект $($Target.full) с ref $Ref не найден" }
    return @{ ref = $refValue; object = $object }
}

function Invoke-Any($Object, [string[]]$Names, [object[]]$Arguments = @()) {
    $last = $null
    foreach ($n in $Names) {
        try { return , (Invoke-ComMethod $Object $n $Arguments) } catch { $last = $_ }
    }
    throw $last
}

function Get-SystemEnum([string]$EnumName, [string]$ValueName) {
    $enum = Get-PropAny $script:connection @($EnumName, 'WriteMode')
    return Get-ComProperty $enum $ValueName
}

# ---------- операции ----------

function Invoke-Op([string]$Op, $A) {
    Connect-Base
    $mode = [string]$script:cfg.mode
    if ($mode -eq 'readonly' -and $Op -in @('create', 'update', 'post', 'unpost', 'mark_delete', 'unmark_delete')) {
        throw 'мост в режиме readonly: запись запрещена'
    }
    if ($mode -ne 'admin' -and $Op -eq 'delete') { throw 'удаление требует mode=admin' }

    switch ($Op) {
        'health' {
            $meta = Get-ComProperty $script:connection 'Metadata'
            $connectedAt = if ($script:connectedAt) { $script:connectedAt.ToString('o') } else { $null }
            # Проверка, что системное перечисление режимов записи доступно из COM
            $postReady = $false
            try { $null = Get-SystemEnum 'РежимЗаписиДокумента' 'Проведение'; $postReady = $true } catch { }
            return @{
                ok = $true; mode = $mode; database = $script:cfg.database
                configuration = (Get-ComProperty $meta 'Name'); version = (Get-ComProperty $meta 'Version')
                connectedAt = $connectedAt; uptimeSec = [int](((Get-Date) - $script:startedAt).TotalSeconds)
                postingReady = $postReady
            }
        }
        'categories' {
            return @{ ok = $true; categories = @($script:Categories | ForEach-Object { @{ key = $_.key; full = $_.full; writable = $_.write } }) }
        }
        'meta' {
            $category = Get-Category ([string]$A.category)
            if ($null -eq $category) { throw ('category: ' + (($script:Categories | ForEach-Object { $_.key }) -join ', ')) }
            $collection = Get-MetadataCollection $category
            $items = @()
            for ($i = 0; $i -lt (Invoke-ComMethod $collection 'Count'); $i++) {
                $md = Invoke-ComMethod $collection 'Get' @($i)
                $items += @{ name = (Get-ComProperty $md 'Имя'); full = ($category.full + '.' + (Get-ComProperty $md 'Имя')) }
            }
            return @{ ok = $true; category = $category.key; count = $items.Count; objects = $items }
        }
        'describe' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $attrs = @()
            $collection = Get-ComProperty $target.md 'Реквизиты'
            for ($i = 0; $i -lt (Invoke-ComMethod $collection 'Count'); $i++) {
                $attr = Invoke-ComMethod $collection 'Get' @($i)
                $item = @{ name = (Get-ComProperty $attr 'Имя'); synonym = [string](Get-ComProperty $attr 'Синоним') }
                $refMd = Get-RefMetadata $attr
                if ($null -ne $refMd) { $item.refType = [string](Invoke-ComMethod $refMd 'ПолноеИмя') }
                $attrs += $item
            }
            try {
                $std = Get-ComProperty $target.md 'СтандартныеРеквизиты'
                for ($i = 0; $i -lt (Invoke-ComMethod $std 'Count'); $i++) {
                    $attr = Invoke-ComMethod $std 'Get' @($i)
                    $attrs += @{ name = (Get-ComProperty $attr 'Имя'); synonym = [string](Get-ComProperty $attr 'Синоним'); standard = $true }
                }
            } catch { }
            $tables = @()
            $tab = Get-ComProperty $target.md 'ТабличныеЧасти'
            for ($i = 0; $i -lt (Invoke-ComMethod $tab 'Count'); $i++) {
                $t = Invoke-ComMethod $tab 'Get' @($i)
                $tAttrs = @()
                $tR = Get-ComProperty $t 'Реквизиты'
                for ($j = 0; $j -lt (Invoke-ComMethod $tR 'Count'); $j++) {
                    $tAttrs += Get-ComProperty (Invoke-ComMethod $tR 'Get' @($j)) 'Имя'
                }
                $tables += @{ name = (Get-ComProperty $t 'Имя'); attributes = $tAttrs }
            }
            return @{ ok = $true; object = $target.full; writable = [bool]$target.category.write; attributes = $attrs; tabularSections = $tables }
        }
        'query' {
            $text = [string]$A.query
            if ($text -notmatch '(?is)^\s*(SELECT|ВЫБРАТЬ)\b') { throw 'принимается только ВЫБРАТЬ/SELECT' }
            if ($text -match ';' -or $text -match '(?i)\b(INTO|ПОМЕСТИТЬ|УНИЧТОЖИТЬ|DROP)\b') {
                throw 'батчи и временные таблицы запрещены'
            }
            $limit = 200
            if ($null -ne $A.max_rows) { $limit = [int]$A.max_rows }
            if ($limit -lt 1 -or $limit -gt 2000) { throw 'max_rows 1..2000' }

            $query = $script:connection.NewObject('Query')
            Set-ComProperty $query 'Text' $text
            if ($null -ne $A.parameters) {
                foreach ($p in $A.parameters.PSObject.Properties) {
                    if ($p.Value -is [array]) { throw 'параметры запроса — только скаляры' }
                    Invoke-ComMethod $query 'УстановитьПараметр' @($p.Name, $p.Value) | Out-Null
                }
            }
            $result = Invoke-ComMethod $query 'Выполнить'
            $columns = Get-ComProperty $result 'Колонки'
            $names = @()
            for ($i = 0; $i -lt (Invoke-ComMethod $columns 'Count'); $i++) {
                $names += Get-ComProperty (Invoke-ComMethod $columns 'Get' @($i)) 'Имя'
            }
            $selection = Invoke-ComMethod $result 'Выбрать'
            $rows = @()
            while ($rows.Count -lt $limit -and (Invoke-ComMethod $selection 'Следующий')) {
                $row = [ordered]@{}
                for ($i = 0; $i -lt $names.Count; $i++) {
                    $row[$names[$i]] = Convert-Cell (Invoke-ComMethod $selection 'Получить' @($i))
                }
                $rows += $row
            }
            $truncated = $false
            if ($rows.Count -eq $limit) { $truncated = [bool](Invoke-ComMethod $selection 'Следующий') }
            return @{ ok = $true; columns = $names; rows = $rows; count = $rows.Count; truncated = $truncated }
        }
        'get' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $found = Get-ObjectByRef $target ([string]$A.ref)
            $data = Read-Object $target.md $found.object
            return @{
                ok = $true; object = $target.full; ref = (Get-UuidText $found.ref)
                fields = $data.fields; tabularSections = $data.tabularSections
            }
        }
        'create' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            if (-not $target.category.write) { throw "$($target.full): запись через мост только для справочников и документов" }
            $createMethod = if ($target.category.key -eq 'Document') { 'СоздатьДокумент' } else { 'СоздатьЭлемент' }
            $object = Invoke-ComMethod (Get-ManagerFor $target) $createMethod
            $applied = Set-Fields $object $target.md $A.data
            if ($A.dryRun) { return @{ ok = $true; object = $target.full; dryRun = $true; applied = $applied } }
            Invoke-ComMethod $object 'Записать' | Out-Null
            $ref = Get-ComProperty $object 'Ссылка'
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $ref); applied = $applied }
        }
        'update' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            if (-not $target.category.write) { throw "$($target.full): запись через мост только для справочников и документов" }
            $found = Get-ObjectByRef $target ([string]$A.ref)
            $applied = Set-Fields $found.object $target.md $A.data
            if ($A.dryRun) { return @{ ok = $true; object = $target.full; dryRun = $true; applied = $applied } }
            Invoke-ComMethod $found.object 'Записать' | Out-Null
            $back = Read-Object $target.md $found.object
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); applied = $applied; fields = $back.fields }
        }
        'post' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            if ($target.category.key -ne 'Document') { throw 'проведение только для документов' }
            $found = Get-ObjectByRef $target ([string]$A.ref)
            Invoke-ComMethod $found.object 'Записать' @((Get-SystemEnum 'РежимЗаписиДокумента' 'Проведение')) | Out-Null
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); posted = $true }
        }
        'unpost' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            if ($target.category.key -ne 'Document') { throw 'отмена проведения только для документов' }
            $found = Get-ObjectByRef $target ([string]$A.ref)
            Invoke-ComMethod $found.object 'Записать' @((Get-SystemEnum 'РежимЗаписиДокумента' 'ОтменаПроведения')) | Out-Null
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); posted = $false }
        }
        'mark_delete' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $found = Get-ObjectByRef $target ([string]$A.ref)
            Invoke-ComMethod $found.object 'УстановитьПометкуУдаления' @($true) | Out-Null
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); deletionMark = $true }
        }
        'unmark_delete' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $found = Get-ObjectByRef $target ([string]$A.ref)
            Invoke-ComMethod $found.object 'УстановитьПометкуУдаления' @($false) | Out-Null
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); deletionMark = $false }
        }
        'delete' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $found = Get-ObjectByRef $target ([string]$A.ref)
            Invoke-ComMethod $found.object 'Удалить' | Out-Null
            return @{ ok = $true; object = $target.full; ref = (Get-UuidText $found.ref); deleted = $true }
        }
        'check_fill' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            if ($target.category.key -ne 'Document') { throw 'проверка заполнения только для документов' }
            $found = Get-ObjectByRef $target ([string]$A.ref)
            $result = Invoke-Any $found.object @('ПроверитьЗаполнение') @()
            while ($result -is [array] -and $result.Count -eq 1) { $result = $result[0] }
            $items = @()
            $note = $null
            try {
                $checks = Get-ComProperty $result 'ПроверкаЭлементов'
                for ($i = 0; $i -lt [int](Invoke-ComMethod $checks 'Количество'); $i++) {
                    $el = Invoke-Any $checks @('Получить', 'Get') @($i)
                    $item = @{}
                    foreach ($pair in @(@('description', 'Описание'), @('attribute', 'ПроверяемыйРеквизит'), @('checkType', 'Тип'), @('ref', 'Ссылка'))) {
                        try {
                            $value = Get-ComProperty $el $pair[1]
                            if ($pair[0] -eq 'ref') { $item[$pair[0]] = Convert-Cell $value }
                            else { $item[$pair[0]] = [string]$value }
                        } catch { }
                    }
                    $items += $item
                }
            } catch {
                # Не у всех конфигураций ПроверитьЗаполнение возвращает структуру
                if ($result -is [bool]) {
                    return @{ ok = $true; object = $target.full; ref = [string]$A.ref; fillCheckPassed = [bool]$result; problems = @(); count = 0 }
                }
                $note = 'результат без ПроверкаЭлементов: ' + ([string]$result)
            }
            return @{ ok = $true; object = $target.full; ref = [string]$A.ref; problems = $items; count = $items.Count; note = $note }
        }
        'search' {
            $needle = ([string]$A.query).Trim()
            if (-not $needle) { throw 'нужна подстрока query' }
            $limit = 50
            if ($null -ne $A.limit) { $limit = [int]$A.limit }
            if ($limit -lt 1 -or $limit -gt 200) { throw 'limit 1..200' }
            $hits = @()
            $low = $needle.ToLower()
            foreach ($c in $script:Categories) {
                try { $collection = Get-MetadataCollection $c } catch { continue }
                for ($i = 0; $i -lt (Invoke-ComMethod $collection 'Count'); $i++) {
                    if ($hits.Count -ge $limit) { break }
                    $md = Invoke-Any $collection @('Get', 'Получить') @($i)
                    $name = Get-ComProperty $md 'Имя'
                    if ($name.ToLower().Contains($low)) {
                        $hits += @{ name = $name; full = ($c.full + '.' + $name); category = $c.key }
                    }
                }
                if ($hits.Count -ge $limit) { break }
            }
            return @{ ok = $true; query = $needle; count = $hits.Count; objects = $hits }
        }
        'tabular' {
            $target = Resolve-Object ([string]$A.object)
            Assert-Allowed $target.full
            $tableName = [string]$A.table
            $tab = Get-ComProperty $target.md 'ТабличныеЧасти'
            $table = Find-InCollection $tab $tableName
            if ($null -eq $table) {
                $names = @()
                for ($i = 0; $i -lt (Invoke-ComMethod $tab 'Count'); $i++) {
                    $names += Get-ComProperty (Invoke-Any $tab @('Get', 'Получить') @($i)) 'Имя'
                }
                throw ("нет табличной части $tableName; есть: " + ($names -join ', '))
            }
            $found = Get-ObjectByRef $target ([string]$A.ref)
            $rowsColl = Get-ComProperty $found.object $tableName
            $limit = 200
            if ($null -ne $A.limit) { $limit = [int]$A.limit }
            if ($limit -lt 1 -or $limit -gt 2000) { throw 'limit 1..2000' }
            $attrNames = @()
            $tAttrs = Get-ComProperty $table 'Реквизиты'
            for ($i = 0; $i -lt (Invoke-ComMethod $tAttrs 'Count'); $i++) {
                $attrNames += Get-ComProperty (Invoke-Any $tAttrs @('Get', 'Получить') @($i)) 'Имя'
            }
            $rows = @()
            $total = [int](Invoke-ComMethod $rowsColl 'Count')
            for ($i = 0; $i -lt [Math]::Min($limit, $total); $i++) {
                $rowObj = Invoke-Any $rowsColl @('Получить', 'Get') @($i)
                $row = [ordered]@{}
                foreach ($n in $attrNames) {
                    try { $row[$n] = Convert-Cell (Get-ComProperty $rowObj $n) } catch { }
                }
                $rows += $row
            }
            return @{
                ok = $true; object = $target.full; table = $tableName
                total = $total; returned = $rows.Count; columns = $attrNames; rows = $rows
            }
        }
        default { throw "неизвестная операция: $Op" }
    }
}


# ---------- MCP surface ----------

$script:Instructions = @(
    'База 1С через COMConnector на Windows; связь идёт туннелем со стороны Windows.'
    'Порядок: onec_status -> onec_metadata -> onec_describe -> onec_query.'
    'Поля не угадывай: сначала onec_describe, потом пиши.'
    'Запросы — язык запросов 1С: ВЫБРАТЬ ... ИЗ Справочник.Номенклатура КАК Т.'
    'Ссылки в data: {"ref":"<guid>"} или guid строкой; даты ISO 2024-01-31T00:00:00.'
    'Проведение: onec_post_document, отмена: onec_unpost_document.'
) -join ' '

function New-Schema([hashtable]$Props, [string[]]$Required) {
    $schema = @{ type = 'object'; properties = $Props }
    if ($Required -and $Required.Count -gt 0) { $schema.required = $Required }
    return $schema
}

$script:Tools = @(
    @{ name = 'onec_status'; description = 'Связь с базой 1С, конфигурация, версия, режим моста. Первый вызов при любом сомнении.'; inputSchema = New-Schema @{} @() }
    @{ name = 'onec_metadata'; description = 'Список объектов 1С по категории. Категории: Catalog, Document, InformationRegister, AccumulationRegister, Constant, Enum, ChartOfCharacteristicTypes, ChartOfAccounts, BusinessProcess, Task, ExchangePlan.'; inputSchema = New-Schema @{ category = @{ type = 'string'; description = 'Catalog, Document и т.д.' } } @('category') }
    @{ name = 'onec_describe'; description = 'Поля объекта с типами ссылок и списком табличных частей. Вызывай перед записью.'; inputSchema = New-Schema @{ object = @{ type = 'string'; description = 'Catalog_Номенклатура или Справочник.Номенклатура' } } @('object') }
    @{ name = 'onec_query'; description = "Язык запросов 1С, только ВЫБРАТЬ. Пример: ВЫБРАТЬ ПЕРВЫЕ 50 Ссылка, Наименование, Код ИЗ Справочник.Номенклатура КАК Т ГДЕ НЕ Т.ПометкаУдаления. Параметры: &Имя -> parameters.Имя."; inputSchema = New-Schema @{ query = @{ type = 'string' }; max_rows = @{ type = 'number'; description = 'по умолчанию 200, максимум 2000' }; parameters = @{ type = 'object'; description = 'Скалярные параметры запроса' } } @('query') }
    @{ name = 'onec_get'; description = 'Все поля объекта по ref (guid).'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' } } @('object', 'ref') }
    @{ name = 'onec_create'; description = 'Создать элемент справочника или документ. data — поля объекта, служебные (Ссылка, Номер) не передавай.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; data = @{ type = 'object' }; dryRun = @{ type = 'boolean'; description = 'проверить поля без записи' } } @('object', 'data') }
    @{ name = 'onec_update'; description = 'Изменить поля объекта; остальные не трогаются. Возвращает поля после записи.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' }; data = @{ type = 'object' }; dryRun = @{ type = 'boolean'; description = 'проверить поля без записи' } } @('object', 'ref', 'data') }
    @{ name = 'onec_post_document'; description = 'Провести документ.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' } } @('object', 'ref') }
    @{ name = 'onec_unpost_document'; description = 'Отменить проведение документа.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' } } @('object', 'ref') }
    @{ name = 'onec_mark_delete'; description = 'Поставить или снять пометку удаления.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' }; mark = @{ type = 'boolean'; description = 'true — пометить, false — снять' } } @('object', 'ref', 'mark') }
    @{ name = 'onec_delete'; description = 'Непосредственное удаление. Только при mode=admin.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' } } @('object', 'ref') }
    @{ name = 'onec_check_fill'; description = 'Проверить готовность документа к проведению без записи (ПроверитьЗаполнение): список проблем.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' } } @('object', 'ref') }
    @{ name = 'onec_search'; description = 'Поиск объектов метаданных по подстроке во всех категориях. Первый шаг, когда имя объекта неизвестно.'; inputSchema = New-Schema @{ query = @{ type = 'string'; description = 'Подстрока имени, например Товар или Номенклатура' }; limit = @{ type = 'number'; description = 'по умолчанию 50, максимум 200' } } @('query') }
    @{ name = 'onec_tabular'; description = 'Строки табличной части объекта (Товары, Услуги и т.д.): колонки, количество, до 200 строк.'; inputSchema = New-Schema @{ object = @{ type = 'string' }; ref = @{ type = 'string' }; table = @{ type = 'string'; description = 'Имя табличной части из onec_describe' }; limit = @{ type = 'number' } } @('object', 'ref', 'table') }
)

function Invoke-ToolCall([string]$Name, $ToolArgs) {
    $map = @{
        'onec_status' = @{ op = 'health'; args = @{} }
        'onec_metadata' = @{ op = 'meta'; args = @{ category = $ToolArgs.category } }
        'onec_describe' = @{ op = 'describe'; args = @{ object = $ToolArgs.object } }
        'onec_query' = @{ op = 'query'; args = @{ query = $ToolArgs.query; max_rows = $ToolArgs.max_rows; parameters = $ToolArgs.parameters } }
        'onec_get' = @{ op = 'get'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_create' = @{ op = 'create'; args = @{ object = $ToolArgs.object; data = $ToolArgs.data; dryRun = $ToolArgs.dryRun } }
        'onec_update' = @{ op = 'update'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref; data = $ToolArgs.data; dryRun = $ToolArgs.dryRun } }
        'onec_post_document' = @{ op = 'post'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_unpost_document' = @{ op = 'unpost'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_mark_delete' = @{ op = $(if ($ToolArgs.mark -eq $false) { 'unmark_delete' } else { 'mark_delete' }); args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_delete' = @{ op = 'delete'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_check_fill' = @{ op = 'check_fill'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref } }
        'onec_search' = @{ op = 'search'; args = @{ query = $ToolArgs.query; limit = $ToolArgs.limit } }
        'onec_tabular' = @{ op = 'tabular'; args = @{ object = $ToolArgs.object; ref = $ToolArgs.ref; table = $ToolArgs.table; limit = $ToolArgs.limit } }
    }
    if (-not $map.ContainsKey($Name)) { throw "нет инструмента: $Name" }
    $call = $map[$Name]
    return Invoke-Op $call.op $call.args
}

# ---------- stdio ----------

$script:In = New-Object IO.StreamReader([Console]::OpenStandardInput(), (New-Object Text.UTF8Encoding($false)))
$script:Out = New-Object IO.StreamWriter([Console]::OpenStandardOutput(), (New-Object Text.UTF8Encoding($false)))
$script:Out.AutoFlush = $true
$script:Queue = New-Object 'System.Collections.Concurrent.BlockingCollection[string]'
$script:lastKeepAlive = Get-Date
$script:lastActivity = Get-Date
$script:lastConnectFail = $null

function Write-Line([string]$Text) {
    $script:Out.WriteLine($Text)
}

# Чтение stdin отдельным потоком: главный поток должен уметь простаивать
# и держать COM-сессию живой, пока клиент молчит.
$readerRunspace = [runspacefactory]::CreateRunspace()
$readerRunspace.Open()
$readerPs = [PowerShell]::Create()
$readerPs.Runspace = $readerRunspace
[void]$readerPs.AddScript({
    param($reader, $queue)
    while ($true) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { $queue.CompleteAdding(); break }
        $queue.Add($line)
    }
}).AddArgument($script:In).AddArgument($script:Queue)
[void]$readerPs.BeginInvoke()

function Invoke-IdleTick {
    # Переподключение только когда клиент молчит: иначе initialize упрётся в 165 с
    if ($script:Queue.Count -gt 0) { return }
    # Соединение поднимается только по запросу клиента: иначе initialize
    # упрётся в 165-секундный коннект и сервер выпадет из агента
    if ($null -eq $script:connection) { return }
    if (((Get-Date) - $script:lastKeepAlive).TotalSeconds -lt 120) { return }
    try {
        $probe = $script:connection.NewObject('Query')
        Set-ComProperty $probe 'Text' 'ВЫБРАТЬ 1 КАК Один'
        Invoke-ComMethod $probe 'Выполнить' | Out-Null
        $script:lastKeepAlive = Get-Date
$script:lastActivity = Get-Date
$script:lastConnectFail = $null
    } catch {
        Close-Base
    }
}

[Console]::Error.WriteLine('onec-mcp (COM) стартует, режим ' + $script:cfg.mode)

# Прогрева на старте нет: initialize должен отвечать мгновенно, даже если база лежит.
# Первый инструментальный вызов поднимет сессию сам.

while (-not $script:Queue.IsCompleted) {
    $line = ''
    $got = $script:Queue.TryTake([ref]$line, 5000)
    if (-not $got) {
        if ($script:Queue.IsCompleted) { break }
        Invoke-IdleTick
        continue
    }
    $script:lastActivity = Get-Date
$script:lastConnectFail = $null
    $text = $line.Trim()
    if (-not $text) { continue }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $response = $null
    $toolName = ''
    try {
        $request = $text | ConvertFrom-Json
    } catch {
        Write-Line (@{ jsonrpc = '2.0'; id = $null; error = @{ code = -32700; message = 'не разобрал JSON' } } | ConvertTo-Json -Depth 5 -Compress)
        continue
    }
    if ($null -eq $request.id) { continue }

    try {
        switch ([string]$request.method) {
            'initialize' {
                $response = @{ jsonrpc = '2.0'; id = $request.id; result = @{
                    protocolVersion = '2025-06-18'
                    capabilities = @{ tools = @{ listChanged = $false } }
                    serverInfo = @{ name = 'onec-com'; version = '1.0.0' }
                    instructions = $script:Instructions
                } }
            }
            'ping' { $response = @{ jsonrpc = '2.0'; id = $request.id; result = @{} } }
            'tools/list' { $response = @{ jsonrpc = '2.0'; id = $request.id; result = @{ tools = $script:Tools } } }
            'tools/call' {
                $toolName = [string]$request.params.name
                try {
                    $value = Invoke-ToolCall $toolName $request.params.arguments
                    $payload = $value | ConvertTo-Json -Depth 25 -Compress
                    if ($payload.Length -gt 150000) { $payload = $payload.Substring(0, 150000) + '… обрезано: сузь ВЫБРАТЬ' }
                    $response = @{ jsonrpc = '2.0'; id = $request.id; result = @{ content = @(@{ type = 'text'; text = $payload }); isError = $false } }
                    $sw.Stop()
                    Write-Audit $toolName $request.params.arguments $true $sw.ElapsedMilliseconds ''
                } catch {
                    $message = $_.Exception.Message
                    $sw.Stop()
                    Write-Audit $toolName $request.params.arguments $false $sw.ElapsedMilliseconds $message
                    if ($message -match '0x80010108|0x800706BA|0x800706BE|RPC|сеанс был заверш|Соединение с сервером') { Close-Base }
                    $response = @{ jsonrpc = '2.0'; id = $request.id; result = @{ content = @(@{ type = 'text'; text = $message }); isError = $true } }
                }
            }
            default {
                $response = @{ jsonrpc = '2.0'; id = $request.id; error = @{ code = -32601; message = "метод не поддержан: $($request.method)" } }
            }
        }
    } catch {
        $response = @{ jsonrpc = '2.0'; id = $request.id; error = @{ code = -32603; message = $_.Exception.Message } }
    }
    Write-Line ($response | ConvertTo-Json -Depth 25 -Compress)
}

try { Close-Base } catch { }
