<#
.SYNOPSIS
  Quita la tarea programada del bot. No borra ni la base de datos ni los registros.
#>
[CmdletBinding()]
param([string]$NombreTarea = "AishaBotTrading")

$ErrorActionPreference = "Stop"

if (-not (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue)) {
    Write-Host "No hay ninguna tarea '$NombreTarea'. Nada que quitar."
    exit 0
}

Stop-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false
Write-Host "Tarea '$NombreTarea' eliminada." -ForegroundColor Green
Write-Host "La base de datos y logs\bot.log siguen donde estaban."
