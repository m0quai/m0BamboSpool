<#
.SYNOPSIS
  Creates the m0BamboSpool filament extra fields in Spoolman.

.DESCRIPTION
  Spoolman manages extra-field definitions in its Settings UI. This script
  reads the current definition, preserves existing fields, and submits the
  merged definition through the settings endpoint used by the installation.
  It is safe to run repeatedly.
#>
[CmdletBinding()]
param(
  [string]$SpoolmanBaseUrl = "http://localhost:7912",
  [string]$SpoolmanContainer = "spoolman",
  [string]$DatabasePath,
  [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$api = "$($SpoolmanBaseUrl.TrimEnd('/'))/api/v1"
$settingsUrl = "$api/setting/"
$fieldUrl = "$api/setting/extra_fields_filament"

if (-not $DatabasePath) {
  try {
    $mounts = docker inspect $SpoolmanContainer --format '{{json .Mounts}}' | ConvertFrom-Json
    $dataMount = $mounts | Where-Object { $_.Destination -match '/home/app/.local/share/spoolman|/home/app/data' } | Select-Object -First 1
    if ($dataMount -and $dataMount.Type -eq 'bind') {
      $DatabasePath = Join-Path $dataMount.Source 'spoolman.db'
    }
  } catch {
    throw "Spoolman-Container '$SpoolmanContainer' konnte nicht inspiziert werden: $($_.Exception.Message)"
  }
}
if ($DatabasePath -and -not (Test-Path -LiteralPath $DatabasePath)) {
  throw "Spoolman-Datenbank nicht gefunden: $DatabasePath"
}
if ($DatabasePath) {
  $backup = "$DatabasePath.before-filament-fields-$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
  Copy-Item -LiteralPath $DatabasePath -Destination $backup -Force
  Write-Host "Datenbank erkannt und gesichert: $DatabasePath"
}

$required = @(
  [ordered]@{ name = "Type"; order = 0; unit = $null; field_type = "choice"; default_value = '"Basic"'; choices = @("AERO","CF","GF","FR","Basic","HF","Translucent","Aero","Dynamic","Galaxy","Glow","Impact","Lite","Marble","Matte","Metal","Silk","Silk+","Sparkle","Tough","Tough+","Wood","Support for ABS","Support for PA PET","Support for PLA","Support for PLA-PETG","G","W","85A","90A","95A","95A HF","for AMS"); multi_choice = $false; key = "type"; entity_type = "filament" },
  [ordered]@{ name = "Nozzle Temperature"; order = 0; unit = "°C"; field_type = "integer_range"; default_value = "[190,230]"; choices = $null; multi_choice = $null; key = "nozzle_temperature"; entity_type = "filament" },
  [ordered]@{ name = "Filament ID"; order = 0; unit = $null; field_type = "text"; default_value = '""'; choices = $null; multi_choice = $null; key = "filament_id"; entity_type = "filament" },
  [ordered]@{ name = "Setting ID"; order = 0; unit = $null; field_type = "text"; default_value = '""'; choices = $null; multi_choice = $null; key = "setting_id"; entity_type = "filament" },
  [ordered]@{ name = "PA-Profil Index"; order = 0; unit = $null; field_type = "integer"; default_value = '-1'; choices = $null; multi_choice = $null; key = "cali_idx"; entity_type = "filament" }
)

if ($WhatIf) {
  $required | ConvertTo-Json -Depth 10
  return
}

$py = @'
import base64, json, sqlite3, sys
db=sys.argv[1]
required=json.loads(base64.b64decode(sys.argv[2]).decode())
con=sqlite3.connect(db)
tables=con.execute("select name from sqlite_master where type='table'").fetchall()
done=False
for (table,) in tables:
    cols=[r[1] for r in con.execute(f"pragma table_info({table})")]
    if 'key' not in cols or 'value' not in cols: continue
    row=con.execute(f"select key,value from {table} where key='extra_fields_filament'").fetchone()
    if not row: continue
    fields=[x for x in json.loads(row[1]) if isinstance(x,dict) and x.get('key') != 'pa_filament_id']
    keys={x.get('key') for x in fields if isinstance(x,dict)}
    fields.extend(x for x in required if x['key'] not in keys)
    con.execute(f"update {table} set value=? where key='extra_fields_filament'",(json.dumps(fields,ensure_ascii=False),))
    con.commit(); print('Filament-Extra-Felder geprüft/aktualisiert:', ', '.join(x['key'] for x in fields)); done=True; break
if not done: raise SystemExit('Spoolman-Settings-Tabelle oder extra_fields_filament nicht gefunden')
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($py))
$requiredJson = $required | ConvertTo-Json -Depth 10 -Compress
$requiredEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($requiredJson))
$containerDb = '/home/app/.local/share/spoolman/spoolman.db'
docker exec $SpoolmanContainer python -c "import base64;exec(base64.b64decode('$encoded').decode())" $containerDb $requiredEncoded
if ($LASTEXITCODE -ne 0) { throw "Die direkte Spoolman-Datenbankaktualisierung ist fehlgeschlagen." }

docker restart $SpoolmanContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Der Spoolman-Container konnte nicht neu gestartet werden." }
$state = docker inspect $SpoolmanContainer --format '{{.State.Status}}'
if ($state -ne 'running') { throw "Spoolman läuft nach dem Neustart nicht (Status: $state)." }
Write-Host "Spoolman neu gestartet; Status: $state"
