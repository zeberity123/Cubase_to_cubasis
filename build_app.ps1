$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'build\pyinstaller-cache'
    python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name CubaseSongExporter --distpath dist --workpath build\pyinstaller --specpath build `
        --exclude-module matplotlib --exclude-module pandas --exclude-module scipy `
        --exclude-module IPython --exclude-module pytest song_exporter_app.py
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
    Copy-Item -LiteralPath README.md -Destination dist\README.md -Force
    python bundle_notices.py
    if ($LASTEXITCODE -ne 0) { throw 'Copying runtime notices failed.' }
    Write-Host 'Built dist\CubaseSongExporter.exe'
} finally {
    Pop-Location
}
