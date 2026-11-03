$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = 'D:\RAG knowledge\.venv\Scripts\python.exe'
$env:RAG_TEST_EXTRA_SITE_PACKAGES = 'E:\RAG quention\.venv\Lib\site-packages'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$tag = [guid]::NewGuid().ToString('N')
# Windows local asyncio sockets must be available. Workers deny external networking.
& $python scripts/run_rsi_offline.py --manifest var/repair-round/bundle/manifest.json --out "var/repair-round/repro-$tag"
$pairedExit = $LASTEXITCODE
# Original frozen evaluator intentionally remains FAIL for the broken parent/equal-length condition.
Write-Output "Original paired runner exit: $pairedExit (archived original run: 1)"
& $python extra_checks.py "engineering-repro-$tag"
if ($LASTEXITCODE -ne 0) { throw 'Independent engineering checks failed; preserve archive.' }
Write-Output "Outputs: var/repair-round/repro-$tag and engineering-repro-$tag"
