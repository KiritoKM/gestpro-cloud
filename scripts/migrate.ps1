<#
  Migra GestPro.db (monolito) a las bases PostgreSQL de los microservicios.
  Corre un contenedor temporal conectado a la red overlay del stack.
  Uso:   .\scripts\migrate.ps1                      (usa ..\GestPro.db)
         .\scripts\migrate.ps1 -Db ..\dist\GestPro.db
#>
param([string]$Db = "..\GestPro.db", [string]$Stack = "gestpro")
$root = Split-Path $PSScriptRoot -Parent
if (-not [IO.Path]::IsPathRooted($Db)) { $Db = Join-Path $root $Db }
$dbPath = (Resolve-Path $Db).Path
$password = (Get-Content (Join-Path $root "secrets\db_password.txt") -Raw).Trim()
# Reutilizamos la imagen de pos-sales: ya trae Python y el driver de PostgreSQL
$image = (docker service inspect "${Stack}_pos-sales" --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}').Split('@')[0]

docker run --rm --network "${Stack}_net" -e "DB_PASSWORD=$password" `
    -v "${dbPath}:/legacy/GestPro.db:ro" -v "${root}\scripts:/scripts:ro" `
    $image python /scripts/migrate_sqlite.py /legacy/GestPro.db
if ($LASTEXITCODE -ne 0) { Write-Error "Fallo la migracion"; exit 1 }
