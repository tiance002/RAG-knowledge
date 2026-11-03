# Local engineering fixture. The only A/B executor is run_rsi_offline.py.
param(
    [string]$Python = 'D:\RAG knowledge\.venv\Scripts\python.exe',
    [string]$ExtraPackages = 'E:\RAG quention\.venv\Lib\site-packages',
    [string]$OriginRun = 'var/r1/r0-final',
    [string]$Output = ('var/r1/example-' + [guid]::NewGuid().ToString('N'))
)
$ErrorActionPreference = 'Stop'
function Invoke-Step([string[]]$Argv) {
    & $Python @Argv
    if ($LASTEXITCODE -ne 0) { throw "Step failed with exit code $LASTEXITCODE" }
}
Push-Location (Split-Path -Parent $PSScriptRoot)
$previousPackages = $env:RAG_TEST_EXTRA_SITE_PACKAGES
try {
    $env:RAG_TEST_EXTRA_SITE_PACKAGES = $ExtraPackages
    if (Test-Path -LiteralPath $Output) { throw "Output already exists: $Output" }
    $registry = 'var/r1/candidate-registry'
    $fixture = 'experiments/r1/engineering'
    $common = @('scripts/rsi_candidates.py', '--registry', $registry)
    $ids = (Invoke-Step ($common + @('register-r0', '--manifest', 'experiments/r0/synthetic.json',
        '--run', $OriginRun))) | ConvertFrom-Json
    $feedback = Get-Content -Raw -Encoding utf8 (Join-Path $OriginRun 'feedback.json') | ConvertFrom-Json
    $obs = @($feedback.observations | Where-Object { $_.qid -eq 'delivery' -and $_.category -eq 'context_length_change' })
    if ($obs.Count -ne 1) { throw 'Expected a single verified delivery observation' }
    $child = (Invoke-Step ($common + @('derive', '--parent', $ids.B, '--config', "$fixture/config.json",
        '--worker-patch', "$fixture/worker-patch.json", '--from-run', $OriginRun, '--observation-id', $obs[0].observation_id,
        '--observation', 'Frozen capture retained the row and header.', '--hypothesis', 'Engineering control: copying the selection preserves its order.',
        '--change', 'Return a copy of the selected indices.', '--impact', 'The captured evidence remains identical.',
        '--falsification', 'The ordered members or evidence bytes differ.'))) | ConvertFrom-Json
    $bundle = Join-Path $Output 'bundle'
    Invoke-Step ($common + @('materialize', '--parent', $ids.B, '--child', $child.candidate_id,
        '--out', $bundle, '--engineering-fixture', '--cases', "$fixture/cases.json", '--evaluation', "$fixture/expected.json"))
    Invoke-Step @('scripts/run_rsi_offline.py', '--manifest', (Join-Path $bundle 'manifest.json'),
        '--out', (Join-Path $Output 'run'))
} finally {
    $env:RAG_TEST_EXTRA_SITE_PACKAGES = $previousPackages
    Pop-Location
}
