@echo off
rem LibraMind launcher. Double-click to run the library app on this computer
rem and the school network. Secrets (SESSION_SECRET, DATABASE_URL) are read
rem from settings.env in the project folder, which is never put in git.
title LibraMind - close this window to stop it
cd /d "%~dp0.."
if not exist settings.env goto :nosecret
findstr /b /c:"SESSION_SECRET=" settings.env >nul || goto :nosecret
set "AUTH_MODE=password"
set "SHARED_PASSWORD=yes"
echo.
echo  LibraMind is starting...
echo.
echo  On this computer:         http://127.0.0.1:8000
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq 'Dhcp' -or $_.PrefixOrigin -eq 'Manual' } | Where-Object { $_.IPAddress -notlike '169.254.*' }).IPAddress"`) do echo  On the school network:    http://%%i:8000
echo.
echo  Keep this window open while people use the app. Close it to stop.
echo.
start "" cmd /c "timeout /t 15 >nul & start http://127.0.0.1:8000/"
.venv\Scripts\python.exe -m library_agent serve --host 0.0.0.0
pause
exit /b

:nosecret
echo.
echo  settings.env is missing SESSION_SECRET, so the app will not start.
echo  Add a line like this to settings.env in the project folder:
echo.
echo    SESSION_SECRET=a-long-random-value
echo.
echo  Make one with:  .venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(24))"
echo.
pause
exit /b 1
