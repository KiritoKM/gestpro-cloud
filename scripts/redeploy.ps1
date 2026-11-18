<#
  Reconstruye y actualiza SOLO algunos servicios del stack (rolling update de esos servicios).
  Uso:   .\scripts\redeploy.ps1 billing-factus grafana
#>
[CmdletBinding(PositionalBinding = $false)]
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]]$Services,
      [string]$Stack = "gestpro")
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$env:TAG = Get-Date -Format "yyyyMMdd-HHmmss"
# mr-coordinator y mr-worker comparten la imagen gestpro/mapreduce (se construye con mr-coordinator)
$aConstruir = @($Services | ForEach-Object { if ($_ -eq "mr-worker") { "mr-coordinator" } else { $_ } } | Select-Object -Unique)
docker compose -f docker-compose.yml build --quiet @aConstruir
if ($LASTEXITCODE -ne 0) { Write-Error "Fallo la construccion"; exit 1 }
foreach ($s in $Services) {
    $imagen = if ($s -like "mr-*") { "gestpro/mapreduce:$env:TAG" } else { "gestpro/${s}:$env:TAG" }
    Write-Host "Actualizando ${Stack}_$s -> $imagen"
    docker service update --quiet --image $imagen "${Stack}_$s" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "Fallo la actualizacion de $s"; exit 1 }
}
docker stack services $Stack
