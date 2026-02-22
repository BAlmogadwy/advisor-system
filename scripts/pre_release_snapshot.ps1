param(
  [string]$Tag = "pre_release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$dest = Join-Path $root ("runtime/release_snapshots/{0}_{1}" -f $Tag, $ts)
New-Item -ItemType Directory -Force -Path $dest | Out-Null

Copy-Item (Join-Path $root "db.sqlite3") (Join-Path $dest "db.sqlite3") -Force
if (Test-Path (Join-Path $root "import_old/database/advisor.db")) {
  Copy-Item (Join-Path $root "import_old/database/advisor.db") (Join-Path $dest "advisor.db") -Force
}

$manifest = @{
  tag = $Tag
  created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
  source = $root
  files = Get-ChildItem $dest | Select-Object Name, Length, LastWriteTimeUtc
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $dest "manifest.json") -Encoding UTF8

Write-Output "SNAPSHOT_OK $dest"
