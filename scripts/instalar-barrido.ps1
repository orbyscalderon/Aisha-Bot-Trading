<#
.SYNOPSIS
  Registra un barrido periodico de wallets de Solana, para acumular historial.

.DESCRIPTION
  Un solo barrido mira los ultimos ~300 intercambios de cada pool, que en los activos son
  minutos. Quien opera dos veces al dia no aparece casi nunca; quien opera sin parar aparece
  siempre. Es decir: un barrido suelto SELECCIONA BOTS por construccion, y eso no se arregla
  afinando umbrales.

  Lo que si lo arregla es repetir el barrido durante dias contra el mismo historial. "Visto
  en 8 barridos de 4 dias" no se puede fingir operando rapido: hay que seguir ahi manana.

  Esta tarea es de SOLO LECTURA. No opera, no toca la base de datos del bot y no gasta
  peticiones de Helius: GeckoTerminal es gratis y sin clave. Es independiente de la tarea del
  bot, asi que se puede tener esta sin el bot instalado, que es justo lo que hace falta
  mientras aun no hay a quien copiar.

.PARAMETER CadaHoras
  Cada cuantas horas se repite. Por defecto 4: seis barridos al dia bastan para separar a
  quien persiste de quien pasaba por ahi, sin castigar la API.

.PARAMETER Historial
  CSV que acumula. Por defecto datos\barridos.csv.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\instalar-barrido.ps1
#>

[CmdletBinding()]
param(
    [int]$CadaHoras = 4,
    [string]$Historial = "datos\barridos.csv",
    [string]$Config = "config\paper.yaml",
    [string]$NombreTarea = "AishaBarridoWallets",
    [int]$MaxPools = 12,
    [string]$MinOperacion = "300"
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

if ($CadaHoras -lt 1 -or $CadaHoras -gt 24) {
    Write-Host "-CadaHoras tiene que estar entre 1 y 24." -ForegroundColor Red
    exit 1
}

$errores = @()
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $errores += "no existe .venv\Scripts\python.exe (crea el entorno virtual primero)"
}
if (-not (Test-Path $Config)) { $errores += "no existe el fichero de configuracion: $Config" }
if ($errores.Count -gt 0) {
    Write-Host "No se instala nada. Falta esto:" -ForegroundColor Red
    $errores | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

if (-not (Test-Path "logs")) { New-Item -ItemType Directory "logs" | Out-Null }
$carpetaHist = Split-Path -Parent $Historial
if ($carpetaHist -and -not (Test-Path $carpetaHist)) {
    New-Item -ItemType Directory $carpetaHist | Out-Null
}

$argumentos = "discover-solana-wallets --config `"$Config`" --historial `"$Historial`" " +
              "--max-pools $MaxPools --min-operacion $MinOperacion"

$accion = New-ScheduledTaskAction `
    -Execute "$raiz\scripts\arrancar-bot.cmd" `
    -Argument $argumentos `
    -WorkingDirectory $raiz

# Empieza dentro de un minuto y se repite indefinidamente. StartWhenAvailable recupera el
# barrido perdido si el equipo estaba apagado: perder barridos sesga el historial hacia las
# horas en que el equipo esta encendido.
$disparador = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours $CadaHoras)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# Un barrido tarda un par de minutos; si uno se atasca, que lo mate a la media hora y siga el
# siguiente. IgnoreNew evita dos barridos a la vez escribiendo el mismo CSV.
$opciones = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
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
    -Description "Barrido de wallets de Solana (solo lectura) para acumular historial." | Out-Null

Write-Host ""
Write-Host "Barrido instalado." -ForegroundColor Green
Write-Host "  cada:      $CadaHoras horas"
Write-Host "  historial: $raiz\$Historial"
Write-Host "  registro:  $raiz\logs\bot.log"
Write-Host ""
Write-Host "Dentro de unos dias, mira quien persiste:"
Write-Host "  .venv\Scripts\python.exe -m cli discover-solana-wallets --historial $Historial ``"
Write-Host "      --min-barridos 8 --out candidatas.txt"
Write-Host ""
Write-Host "Y pasa esas por la criba antes de copiar a nadie:"
Write-Host "  .venv\Scripts\python.exe -m cli screen-solana-wallets -w <direccion> --since <fecha> --sol-quotes <csv>"
Write-Host ""
Write-Host "Quitarlo:  Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:`$false"
