@echo off
rem Runs the weather paper trader (python -m wxbrackets paper --every 60) for the
rem "Tradeorama\Weather bot" scheduled task, which scripts\bots.ps1 sets up to start at boot.
rem Output is appended to logs\weather-bot.log. If the bot exits or crashes, it starts
rem again after 2 minutes. Stop it with: scripts\bots.ps1 stop
setlocal
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "LOG=%ROOT%\logs\weather-bot.log"
rem UTF-8 output: bracket labels use a less-or-equal sign the default Windows code page lacks.
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
cd /d "%ROOT%\weather" || exit /b 1

:loop
call :rotate
>>"%LOG%" echo ==== %date% %time% starting: python -m wxbrackets paper --every 60
"%ROOT%\weather\.venv\Scripts\python.exe" -m wxbrackets paper --every 60 >>"%LOG%" 2>&1
>>"%LOG%" echo ==== %date% %time% exited with code %errorlevel%; starting again in 2 minutes
rem ping as a sleep: timeout.exe fails when there's no console, as in a scheduled task
ping -n 121 127.0.0.1 >nul
goto loop

:rotate
rem Keep the log under about 5 MB: the previous one is kept as weather-bot.log.1
if not exist "%LOG%" exit /b 0
for %%F in ("%LOG%") do if %%~zF GTR 5000000 move /y "%LOG%" "%LOG%.1" >nul
exit /b 0
