@echo off
REM ---------------------------------------------------------------------------
REM Build LocalRedact.exe on Windows.
REM Run this from the project root (the folder containing run_app.py):
REM     packaging\build_exe.bat
REM ---------------------------------------------------------------------------
setlocal

if not exist run_app.py (
    echo ERROR: run this from the folder that contains run_app.py
    echo    cd path\to\pdf_redactor
    echo    packaging\build_exe.bat
    exit /b 1
)

echo [1/5] Creating virtual environment .venv
if not exist .venv (
    python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat || exit /b 1

echo [2/5] Installing dependencies
python -m pip install --upgrade pip || exit /b 1
python -m pip install PyMuPDF==1.28.2 pyinstaller==6.16.0 || exit /b 1

REM Uncomment ONE of these for offline OCR support in the .exe:
REM python -m pip install pytesseract==0.3.13 Pillow==11.3.0
REM python -m pip install rapidocr-onnxruntime==1.4.4 Pillow==11.3.0

echo [3/5] Running the test suite
python tests\test_localredact.py || (
    echo ERROR: tests failed, refusing to build
    exit /b 1
)

echo [4/5] Building the executable
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
pyinstaller packaging\localredact.spec --noconfirm || exit /b 1

echo [5/5] Done
echo.
echo   dist\LocalRedact.exe
echo.
echo Copy that single file anywhere. It needs no installer and no Python.
endlocal
