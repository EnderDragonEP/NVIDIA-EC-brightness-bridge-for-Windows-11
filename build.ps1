param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectDirectory

try {
    & $Python -c "import win32com.client, win32gui, PyInstaller"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing build dependencies..."
        & $Python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
    }

    & $Python -m py_compile nvidia_ec_brightness_bridge.py
    if ($LASTEXITCODE -ne 0) {
        throw "Python syntax validation failed."
    }

    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed."
    }

    & $Python -m PyInstaller --noconfirm --clean NvidiaEcBrightnessBridge.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    Write-Host ""
    Write-Host "Build complete:"
    Write-Host (Join-Path $projectDirectory "dist\NvidiaEcBrightnessBridge.exe")
}
finally {
    Pop-Location
}
