param([switch]$SkipValidator)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $projectRoot
try {
    uv venv --python 3.11 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment' }
    uv pip install --python .venv\Scripts\python.exe -e '.[backend,mcp,test]'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies' }
    if (-not $SkipValidator) {
        npm.cmd install --prefix .tools/gltf-validator --no-audit --no-fund gltf-validator@2.0.0-dev.3.10
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install glTF Validator' }
    }
} finally {
    Pop-Location
}
