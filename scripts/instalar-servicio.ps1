<#
.SYNOPSIS
  Registra el bot como tarea programada de Windows: arranca solo y se reinicia si se cae.

.DESCRIPTION
  Crea una tarea que arranca al iniciar sesion, sin ventana, y vuelve a levantar el bot si
  termina por cualquier motivo. Toda la salida va a logs\bot.log.

  Se usa el Programador de tareas y no un servicio de Windows a proposito: un servicio de
  verdad necesita envolver el proceso con software externo (NSSM y similares) o guardar la
  contrasena de la cuenta. La tarea programada viene con Windows, no pide contrasena si se
  dispara al iniciar sesion, y reinicia sola.

  NO activa el modo real. El bot se niega a operar con dinero de verdad sin confirmacion
  interactiva en terminal, y esta tarea no puede darla.

.PARAMETER Signals
  CSV de senales que copiar (timestamp,trader_id,symbol,side,amount_usd).

.PARAMETER Config
  YAML de configuracion. Por defecto config\config.example.yaml.

.PARAMETER AlIniciarElEquipo
  Arranca al encender el equipo en vez de al iniciar sesion. Mas robusto (sobrevive a un
  reinicio sin que nadie entre), pero Windows pedira la contrasena de la cuenta para guardarla.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\instalar-servicio.ps1 -Signals ejemplos\senales_solana.csv
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Signals,
    [string]$Config = "config\config.example.yaml",
    [string]$NombreTarea = "AishaBotTrading",
    [switch]$AlIniciarElEquipo
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

# --- comprobaciones antes de registrar nada ---
# Una tarea que apunta a ficheros que no existen falla en silencio cada minuto.

$errores = @()
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $errores += "no existe .venv\Scripts\python.exe (crea el entorno virtual primero)"
}
if (-not (Test-Path $Config))  { $errores += "no existe el fichero de configuracion: $Config" }
if (-not (Test-Path $Signals)) { $errores += "no existe el CSV de senales: $Signals" }
if (-not (Test-Path ".env"))   { $errores += "no existe .env (copia config\.env.example)" }

if ($errores.Count -gt 0) {
    Write-Host "No se instala nada. Falta esto:" -ForegroundColor Red
    $errores | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

# El modo se lee del YAML: si dice live, el bot no arrancaria y la tarea quedaria en bucle.
if ((Get-Content $Config -Raw) -match '(?m)^\s*mode:\s*live\b') {
    Write-Host "El YAML dice 'mode: live'. La ejecucion real exige confirmacion en terminal," -ForegroundColor Red
    Write-Host "asi que una tarea desatendida no puede usarlo. Usa 'mode: paper'." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "logs")) { New-Item -ItemType Directory "logs" | Out-Null }

# --- la tarea ---

$signalsAbs = (Resolve-Path $Signals).Path
$configAbs  = (Resolve-Path $Config).Path
$argumentos = "run --config `"$configAbs`" --root `"$raiz`" --signals `"$signalsAbs`""

$accion = New-ScheduledTaskAction `
    -Execute "$raiz\scripts\arrancar-bot.cmd" `
    -Argument $argumentos `
    -WorkingDirectory $raiz

if ($AlIniciarElEquipo) {
    $disparador = New-ScheduledTaskTrigger -AtStartup
    $principal  = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Password -RunLevel Limited
} else {
    $disparador = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal  = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
}

# RestartCount/RestartInterval: si el bot muere, vuelve. ExecutionTimeLimit 0: no lo mates por
# llevar mucho tiempo vivo, que es justo lo que se espera de el.
$opciones = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue) {
    Write-Host "Ya existia una tarea '$NombreTarea'; se reemplaza." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NombreTarea `
    -Action $accion `
    -Trigger $disparador `
    -Principal $principal `
    -Settings $opciones `
    -Description "Bot de copy-trading en modo simulado, con control por Telegram." | Out-Null

Write-Host ""
Write-Host "Instalado." -ForegroundColor Green
Write-Host "  arranca:   $(if ($AlIniciarElEquipo) { 'al encender el equipo' } else { 'al iniciar sesion' })"
Write-Host "  registro:  $raiz\logs\bot.log"
Write-Host ""
Write-Host "Arrancarlo ahora sin reiniciar:   Start-ScheduledTask -TaskName $NombreTarea"
Write-Host "Pararlo:                          Stop-ScheduledTask  -TaskName $NombreTarea"
Write-Host "Ver si esta vivo:                 Get-ScheduledTask   -TaskName $NombreTarea"
Write-Host "Quitarlo:                         scripts\desinstalar-servicio.ps1"
Write-Host ""
Write-Host "Parada de emergencia sin tocar nada de esto: crea un fichero vacio llamado STOP"
Write-Host "en $raiz  (o manda /stop al bot por Telegram)."
