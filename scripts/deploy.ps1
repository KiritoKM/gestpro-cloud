<#
  Construye las imagenes y despliega GestPro Cloud en Docker Swarm.
  Uso:   .\scripts\deploy.ps1
  Cada despliegue usa una etiqueta nueva (TAG), asi Swarm hace un rolling update
  replica por replica y, si una version nueva falla su healthcheck, hace rollback.
#>
param([string]$Stack = "gestpro")
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function New-Secret([int]$Length) {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $Length | ForEach-Object { [char]$_ })
}

# 1. Secretos: se generan una sola vez y nunca se suben al repositorio
New-Item -ItemType Directory -Force secrets | Out-Null
foreach ($s in @(@{File = "db_password.txt"; Len = 24 }, @{File = "jwt_secret.txt"; Len = 48 },
                 @{File = "grafana_admin_password.txt"; Len = 16 })) {
    $path = Join-Path $root "secrets\$($s.File)"
    if (-not (Test-Path $path)) {
        [IO.File]::WriteAllText($path, (New-Secret $s.Len))
        Write-Host "Secreto creado: secrets\$($s.File)"
    }
}
# Credenciales de Factus: se extraen de la BD del monolito (antes estaban en texto plano)
if (-not (Test-Path (Join-Path $root "secrets\factus_credentials.json"))) {
    python (Join-Path $root "scripts\extraer_secreto_factus.py")
}
# Sin credenciales de Factus (repo recien clonado, sin el monolito): facturacion simulada
if (-not $env:FACTUS_MODE -and (Get-Content (Join-Path $root "secrets\factus_credentials.json") -Raw).Trim() -eq '{}') {
    $env:FACTUS_MODE = "simulado"
    Write-Host "Sin credenciales de Factus: billing-factus se despliega en modo simulado"
}

# 2. Swarm activo
if ((docker info --format '{{.Swarm.LocalNodeState}}') -ne 'active') { docker swarm init | Out-Null }

# 3. Construir imagenes (docker escribe el progreso en stderr: se evalua el codigo de salida)
$env:TAG = Get-Date -Format "yyyyMMdd-HHmmss"
Write-Host "Construyendo imagenes con TAG=$env:TAG ..."
docker compose -f docker-compose.yml build --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "Fallo la construccion de las imagenes"; exit 1 }

# 4. Desplegar el stack
docker stack deploy -c docker-compose.yml $Stack
if ($LASTEXITCODE -ne 0) { Write-Error "Fallo docker stack deploy"; exit 1 }
docker stack services $Stack
