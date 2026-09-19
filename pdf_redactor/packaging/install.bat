@echo off
REM ---------------------------------------------------------------------------
REM Double-click this to install LocalRedact.
REM
REM It exists because running install.ps1 directly is awkward: Windows blocks
REM .ps1 files by default, and "Run with PowerShell" closes the window the
REM instant it finishes, so you never see what happened. This wrapper bypasses
REM the policy for this one invocation only (it changes no machine setting) and
REM keeps the window open at the end.
REM
REM Any arguments are passed straight through, so these all work:
REM     install.bat -DryRun
REM     install.bat -ContextMenu
REM     install.bat -Uninstall
REM     install.bat -Source "%USERPROFILE%\Downloads\LocalRedact-windows-exe.zip"
REM ---------------------------------------------------------------------------
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
    echo Install did not complete ^(exit code %RC%^).
    echo See docs\INSTALL_WINDOWS.md for the manual steps.
    echo.
)
pause
endlocal
exit /b %RC%
