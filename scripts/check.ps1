$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements first; see README.' }
& $pythonPath (Join-Path $PSScriptRoot 'check.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
