@echo off
REM Arranca el bot sin terminal visible y deja todo en logs\bot.log.
REM Lo invoca la tarea programada que crea scripts\instalar-servicio.ps1.
REM Tambien se puede ejecutar a mano para probar que la linea de arranque es correcta.

setlocal
set "RAIZ=%~dp0.."
cd /d "%RAIZ%" || exit /b 1

if not exist "%RAIZ%\.venv\Scripts\python.exe" (
  echo [%date% %time%] NO existe .venv\Scripts\python.exe>> "%RAIZ%\logs\bot.log"
  exit /b 1
)
if not exist "%RAIZ%\logs" mkdir "%RAIZ%\logs"

echo.>> "%RAIZ%\logs\bot.log"
echo ========== arranque %date% %time% ==========>> "%RAIZ%\logs\bot.log"

REM 2>&1 manda tambien los errores al log: sin terminal, lo que no se escriba se pierde.
"%RAIZ%\.venv\Scripts\python.exe" -m cli %* >> "%RAIZ%\logs\bot.log" 2>&1
set CODIGO=%ERRORLEVEL%

echo [%date% %time%] el bot termino con codigo %CODIGO%>> "%RAIZ%\logs\bot.log"
exit /b %CODIGO%
