# Build LocalRedact.exe. Run from the project root:  .\packaging\build_exe.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path "run_app.py")) {
    throw "Run this from the folder that contains run_app.py"
}

Write-Host "[1/5] Virtual environment" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\Activate.ps1

Write-Host "[2/5] Dependencies" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install PyMuPDF==1.28.2 pyinstaller==6.16.0
# Offline OCR (optional) - pick one:
# python -m pip install pytesseract==0.3.13 Pillow==11.3.0
# python -m pip install rapidocr-onnxruntime==1.4.4 Pillow==11.3.0

Write-Host "[3/5] Tests" -ForegroundColor Cyan
python tests\test_localredact.py
if ($LASTEXITCODE -ne 0) { throw "Tests failed - refusing to build" }

Write-Host "[4/5] PyInstaller" -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller packaging\localredact.spec --noconfirm

Write-Host "[5/5] Built dist\LocalRedact.exe" -ForegroundColor Green
