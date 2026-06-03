@echo off
:: TimeTracker MSI builder
::
:: Usage:
::   build_msi.bat COMPANY_TOKEN [API_BASE]
::
:: Examples:
::   build_msi.bat mytoken123
::   build_msi.bat mytoken123 https://api.example.com
::
:: Output: installer\windows\output\TimeTracker.msi

if "%~1"=="" (
    echo.
    echo  Usage:   build_msi.bat COMPANY_TOKEN [API_BASE]
    echo  Example: build_msi.bat mytoken123 https://api.example.com
    echo.
    exit /b 1
)

set TOKEN=%~1
set APIBASE=%~2
if "%APIBASE%"=="" set APIBASE=https://api.yourtimetracker.com

echo.
echo  Building TimeTracker MSI...
echo    Token   : %TOKEN:~0,8%...
echo    API base: %APIBASE%
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0build_msi.ps1" ^
    -CompanyToken "%TOKEN%" ^
    -ApiBase "%APIBASE%"

exit /b %ERRORLEVEL%
