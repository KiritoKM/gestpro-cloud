<#
  Revisa el stack y fuerza el reinicio de los servicios que no tienen todas sus replicas.
  Caso tipico: se reinicio Docker Desktop. Los contenedores terminan "limpio" (codigo 0) y,
  con restart_policy on-failure, Swarm no los vuelve a lanzar (solo relanza los que FALLAN).
  Uso:   .\scripts\revivir.ps1
#>
param([string]$Stack = "gestpro")
$incompletos = docker stack services $Stack --format '{{.Name}} {{.Replicas}}' |
    Where-Object { $_ -match '^(\S+) (\d+)/(\d+)' -and $Matches[2] -ne $Matches[3] } |
    ForEach-Object { ($_ -split ' ')[0] }
if (-not $incompletos) { Write-Host "Todo el stack esta completo." -ForegroundColor Green; docker stack services $Stack; exit 0 }
foreach ($s in $incompletos) {
    Write-Host "Reiniciando $s ..." -ForegroundColor Yellow
    docker service update --quiet --force $s | Out-Null
}
$limite = (Get-Date).AddSeconds(180)
do {
    Start-Sleep 5
    $pend = docker stack services $Stack --format '{{.Replicas}}' | Where-Object { $_ -match '(\d+)/(\d+)' -and $Matches[1] -ne $Matches[2] }
} while ($pend -and (Get-Date) -lt $limite)
docker stack services $Stack
