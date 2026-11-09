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

  TODA comprobacion se hace ANTES de registrar nada. Una tarea mal configurada no falla una
  vez: falla cada minuto, en silencio, dentro de un log que nadie mira.

.PARAMETER SolanaWallet
  Wallet de Solana a copiar EN VIVO (se puede repetir). Lee los swaps de Helius en cada ciclo.
  Es lo que la mayoria quiere: el bot sigue a alguien de verdad, ahora.

.PARAMETER Signals
  CSV estatico de senales (timestamp,trader_id,symbol,side,amount_usd). Alternativa a
  -SolanaWallet, util para repetir una ejecucion concreta. Un CSV no se actualiza solo: con
  esto el bot releera para siempre el mismo fichero.

.PARAMETER Config
  YAML de configuracion. Por defecto config\paper.yaml.

.PARAMETER AlIniciarElEquipo
  Arranca al encender el equipo en vez de al iniciar sesion. Mas robusto (sobrevive a un
  reinicio sin que nadie entre), pero Windows pedira la contrasena de la cuenta para guardarla.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\instalar-servicio.ps1 -SolanaWallet 8KWe...SCM

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\instalar-servicio.ps1 -Signals ejemplos\senales_solana.csv
#>

[CmdletBinding()]
param(
    [string[]]$SolanaWallet = @(),
    [string]$Signals,
    [string]$Config = "config\paper.yaml",
    [string]$NombreTarea = "AishaBotTrading",
    [switch]$AlIniciarElEquipo
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

# Lee una clave del .env. El bot usa el mismo criterio (adapters\env.py): el fichero es la
# fuente, el entorno manda por encima. Aqui solo se comprueba que EXISTE algo, nunca se
# imprime el valor.
function Get-ValorEnv([string]$nombre) {
    if (-not (Test-Path ".env")) { return "" }
    foreach ($linea in (Get-Content ".env" -Encoding UTF8)) {
        $l = $linea.Trim()
        if ($l -eq "" -or $l.StartsWith("#")) { continue }
        if ($l.StartsWith("export ")) { $l = $l.Substring(7).TrimStart() }
        $i = $l.IndexOf("=")
        if ($i -lt 1) { continue }
        if ($l.Substring(0, $i).Trim() -eq $nombre) {
            return $l.Substring($i + 1).Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

# --- comprobaciones antes de registrar nada ---

$errores = @()

# Exactamente una fuente de senales. Dos cursores sobre la misma base de datos y nadie sabria
# de donde salio cada posicion; ninguna y el bot arrancaria para no hacer nada.
$usaWallets = $SolanaWallet.Count -gt 0
$usaCsv     = -not [string]::IsNullOrWhiteSpace($Signals)
if ($usaWallets -and $usaCsv) {
    $errores += "-SolanaWallet y -Signals son alternativas: usa una, no las dos"
}
if (-not $usaWallets -and -not $usaCsv) {
    $errores += "falta la fuente de senales: -SolanaWallet <wallet> (en vivo) o -Signals <csv>"
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $errores += "no existe .venv\Scripts\python.exe (crea el entorno virtual primero)"
}
if (-not (Test-Path $Config)) { $errores += "no existe el fichero de configuracion: $Config" }
if ($usaCsv -and -not (Test-Path $Signals)) { $errores += "no existe el CSV de senales: $Signals" }
if (-not (Test-Path ".env")) { $errores += "no existe .env (copia config\.env.example)" }

# Una wallet mal escrita hace que el bot salga con codigo 2 en cada arranque, cada minuto,
# para siempre. Se valida aqui con el mismo criterio que validate_wallet en Python.
foreach ($w in $SolanaWallet) {
    if ($w -notmatch '^[1-9A-HJ-NP-Za-km-z]{32,44}$') {
        $errores += "wallet de Solana no valida: '$w' (se esperan 32-44 caracteres base58)"
    }
}

# Copiar en vivo es pedirle swaps a Helius: sin clave no hay nada que leer.
if ($usaWallets -and [string]::IsNullOrWhiteSpace((Get-ValorEnv "HELIUS_API_KEY"))) {
    $errores += "falta HELIUS_API_KEY en .env, y sin ella no se pueden leer las wallets en vivo"
}

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

# --- avisos que NO impiden instalar ---
# El bot esta disenado para operar aunque no haya chat: quedarse sin operar por no poder
# avisar seria dejar de hacer lo importante por no poder hacer lo accesorio. Pero sin aviso
# aqui, el bot arranca mudo y no te enteras hasta que buscas por que no responde.
$avisos = @()
if ([string]::IsNullOrWhiteSpace((Get-ValorEnv "TELEGRAM_BOT_TOKEN"))) {
    $avisos += "sin TELEGRAM_BOT_TOKEN: el bot operara MUDO (ni avisos ni control por chat)"
} elseif ([string]::IsNullOrWhiteSpace((Get-ValorEnv "TELEGRAM_ALLOWED_CHAT_IDS")) -and
          [string]::IsNullOrWhiteSpace((Get-ValorEnv "TELEGRAM_CHAT_ID"))) {
    $avisos += "hay token pero nadie autorizado: rellena TELEGRAM_ALLOWED_CHAT_IDS " +
               "(sacalo con 'python -m cli telegram-chat-id')"
}
if ([string]::IsNullOrWhiteSpace((Get-ValorEnv "LLM_API_KEY"))) {
    $avisos += "sin LLM_API_KEY: el chat entendera comandos exactos (/status, /pausa...) pero no frases libres"
}

if ($avisos.Count -gt 0) {
    Write-Host ""
    Write-Host "Se instala, pero ojo:" -ForegroundColor Yellow
    $avisos | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}

if (-not (Test-Path "logs")) { New-Item -ItemType Directory "logs" | Out-Null }

# --- la tarea ---

$configAbs = (Resolve-Path $Config).Path
$argumentos = "run --config `"$configAbs`" --root `"$raiz`""
if ($usaWallets) {
    foreach ($w in $SolanaWallet) { $argumentos += " --solana-wallet `"$w`"" }
} else {
    $argumentos += " --signals `"$((Resolve-Path $Signals).Path)`""
}

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
Write-Host "  fuente:    $(if ($usaWallets) { "EN VIVO, $($SolanaWallet.Count) wallet(s) via Helius" } else { "CSV estatico: $Signals" })"
Write-Host "  registro:  $raiz\logs\bot.log"
Write-Host ""
Write-Host "Arrancarlo ahora sin reiniciar:   Start-ScheduledTask -TaskName $NombreTarea"
Write-Host "Pararlo:                          Stop-ScheduledTask  -TaskName $NombreTarea"
Write-Host "Ver si esta vivo:                 Get-ScheduledTask   -TaskName $NombreTarea"
Write-Host "Quitarlo:                         scripts\desinstalar-servicio.ps1"
Write-Host ""
Write-Host "Parada de emergencia sin tocar nada de esto: crea un fichero vacio llamado STOP"
Write-Host "en $raiz  (o manda /stop al bot por Telegram)."
