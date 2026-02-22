param(
  [Parameter(Mandatory = $true)][string]$SnapshotDir
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (!(Test-Path $SnapshotDir)) { throw "Snapshot directory not found: $SnapshotDir" }
if (!(Test-Path (Join-Path $SnapshotDir "db.sqlite3"))) { throw "Snapshot missing db.sqlite3" }

Copy-Item (Join-Path $SnapshotDir "db.sqlite3") (Join-Path $root "db.sqlite3") -Force
if (Test-Path (Join-Path $SnapshotDir "advisor.db")) {
  Copy-Item (Join-Path $SnapshotDir "advisor.db") (Join-Path $root "import_old/database/advisor.db") -Force
}

Write-Output "RESTORE_OK $SnapshotDir"
