<#
  Deja GestPro Cloud limpio para la demo:
    1. elimina el stack y sus volumenes (bases de datos, Redis, Prometheus, Grafana)
    2. lo despliega de nuevo
    3. migra GestPro.db del monolito
    4. carga sedes, catalogo (Excel) y clientes de demo
  Uso:   .\scripts\reset_demo.ps1 -AdminPassword <clave del admin en el monolito>
         .\scripts\reset_demo.ps1 -AdminPassword admin123      (sin la BD del monolito)
#>
param([Parameter(Mandatory = $true)][string]$AdminPassword,
      [string]$Database = "..\dist\GestPro.db", [string]$Stack = "gestpro")
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "1/4 Eliminando el stack '$Stack' y sus volumenes..."
docker stack rm $Stack | Out-Null
$limite = (Get-Date).AddSeconds(120)
# esperar a que Swarm elimine TODOS los contenedores (tambien los detenidos) que usan los volumenes
while ((docker ps -aq -f "label=com.docker.stack.namespace=$Stack") -and (Get-Date) -lt $limite) { Start-Sleep 3 }
while ((docker network ls -q -f "name=${Stack}_net") -and (Get-Date) -lt $limite) { Start-Sleep 3 }
docker volume ls -q -f "label=com.docker.stack.namespace=$Stack" | ForEach-Object { docker volume rm $_ | Out-Null }
$quedan = docker volume ls -q -f "label=com.docker.stack.namespace=$Stack"
if ($quedan) { Write-Error "No se pudieron borrar los volumenes: $quedan"; exit 1 }

Write-Host "2/4 Desplegando..."
& "$PSScriptRoot\deploy.ps1" -Stack $Stack | Out-Null
$inicio = Get-Date; $revivido = $false
do {
    Start-Sleep 5
    $pendientes = docker stack services $Stack --format '{{.Replicas}}' | Where-Object { $_ -match '(\d+)/(\d+)' -and $Matches[1] -ne $Matches[2] }
    # Si una replica salio con codigo 0 mientras esperaba su BD, on-failure no la relanza: se fuerza una vez
    if ($pendientes -and -not $revivido -and ((Get-Date) - $inicio).TotalSeconds -gt 90) {
        Write-Host "   Hay replicas incompletas: forzando su reinicio..."
        & "$PSScriptRoot\revivir.ps1" -Stack $Stack | Out-Null; $revivido = $true
    }
} while ($pendientes -and ((Get-Date) - $inicio).TotalSeconds -lt 300)
if ($pendientes) { Write-Warning "Aun hay servicios incompletos; revisa 'docker stack services $Stack'" }
Start-Sleep 10   # tiempo para que HAProxy marque las replicas como UP

if (Test-Path $Database) {
    Write-Host "3/4 Migrando $Database ..."
    & "$PSScriptRoot\migrate.ps1" -Db $Database -Stack $Stack
} else {
    Write-Host "3/4 No se encontro $Database : se omite la migracion (usuario inicial admin / admin123)"
}

Write-Host "4/4 Cargando datos de demo..."
python (Join-Path $PSScriptRoot "cargar_demo.py") $AdminPassword
docker stack services $Stack
