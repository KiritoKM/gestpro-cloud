<#
  Demo en vivo de alta disponibilidad y tolerancia a fallos de GestPro Cloud.
  Cada paso explica que se va a hacer, lo ejecuta y espera ENTER para seguir.

  Uso:   .\scripts\demo_alta_disponibilidad.ps1             (interactivo, para la sustentacion)
         .\scripts\demo_alta_disponibilidad.ps1 -NoPause    (corre todo seguido, para ensayar)
         .\scripts\demo_alta_disponibilidad.ps1 -Pasos 1,3,4   (solo esos pasos; los de la sustentacion)
  Tener abiertos: http://localhost:8404/stats (HAProxy) y http://localhost:3000 (Grafana).
#>
param([switch]$NoPause, [int[]]$Pasos = @(0, 1, 2, 3, 4, 5), [string]$Stack = "gestpro")

function Paso([string]$Titulo, [string]$Explicacion) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host " $Titulo" -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host $Explicacion -ForegroundColor Gray
}
function Pausa([string]$Texto = "ENTER para continuar") {
    if (-not $NoPause) { Read-Host "`n>> $Texto" | Out-Null }
}
function Cmd([string]$Comando) {
    Write-Host "`n  PS> $Comando" -ForegroundColor Yellow
    Invoke-Expression $Comando
}
function Replicas([string]$Servicio) {
    docker service ls --filter "name=${Stack}_$Servicio" --format '{{.Replicas}}'
}
function Esperar-Replicas([string]$Servicio, [string]$Esperado, [int]$Segundos = 90) {
    $limite = (Get-Date).AddSeconds($Segundos)
    while ((Replicas $Servicio) -ne $Esperado -and (Get-Date) -lt $limite) { Start-Sleep 2 }
    Write-Host "  $Servicio -> $(Replicas $Servicio) replicas" -ForegroundColor Green
}
function Probar-Gateway([int]$Veces = 6) {
    # Peticiones sin token: HAProxy responde 401 rapido; con X-Replica se ve el balanceo en /api/... autenticado.
    1..$Veces | ForEach-Object {
        try { $r = Invoke-WebRequest -UseBasicParsing "http://localhost:8080/" -TimeoutSec 3; "  GET / -> $($r.StatusCode)" }
        catch { "  GET / -> ERROR $($_.Exception.Message)" }
        Start-Sleep -Milliseconds 300
    }
}

# --------------------------------------------------------------------------------
if ($Pasos -contains 0) {
    Paso "0. Estado del cluster" "Swarm orquesta el stack 'gestpro': cada servicio declara cuantas replicas quiere.
    Swarm compara el estado deseado con el real y lo corrige solo (reconciliacion)."
    Cmd "docker node ls"
    Cmd "docker stack services $Stack"
    Pausa

}
# --------------------------------------------------------------------------------
if ($Pasos -contains 1) {
    Paso "1. Autorecuperacion: matamos una replica de pos-sales" "Eliminamos a la fuerza un contenedor de pos-sales (3 replicas).
    - HAProxy lo saca del balanceo por health check (ver /stats: la replica pasa a DOWN).
    - Swarm detecta que hay 2/3 y crea una replica nueva (restart_policy: on-failure).
    - Los usuarios no lo notan: las otras 2 replicas siguen atendiendo."
    Pausa "ENTER para matar la replica"
    $victima = docker ps -q -f "name=${Stack}_pos-sales" | Select-Object -First 1
    Cmd "docker rm -f $victima"
    Probar-Gateway 4
    Cmd "docker service ps ${Stack}_pos-sales --format 'table {{.Name}}\t{{.CurrentState}}\t{{.Error}}' | Select-Object -First 6"
    Esperar-Replicas "pos-sales" "3/3"
    Pausa

}
# --------------------------------------------------------------------------------
if ($Pasos -contains 2) {
    Paso "2. Escalado horizontal" "Subimos pos-sales a 5 replicas. HAProxy las descubre solo por el DNS de Swarm
    (server-template + tasks.pos-sales) y Prometheus empieza a medirlas (Grafana: Replicas activas)."
    Cmd "docker service scale ${Stack}_pos-sales=5"
    Start-Sleep 8
    Write-Host "  En la app, cada respuesta trae la cabecera X-Replica: se ven 5 replicas distintas." -ForegroundColor Gray
    Pausa "ENTER para volver a 3 replicas"
    Cmd "docker service scale ${Stack}_pos-sales=3"
    Pausa

}
# --------------------------------------------------------------------------------
if ($Pasos -contains 3) {
    Paso "3. Consistencia eventual: clients-credits caido" "Apagamos clients-credits (0 replicas) y hacemos una VENTA A CREDITO en la app.
    - La venta se registra igual: pos-sales no depende de clients-credits (comunicacion asincrona).
    - El evento sale.created queda PENDIENTE en el stream de Redis para el grupo 'clients'.
    - Al volver el servicio, procesa el evento y la deuda del cliente se actualiza sola."
    Cmd "docker service scale ${Stack}_clients-credits=0"
    $redis = docker ps -q -f "name=${Stack}_redis" | Select-Object -First 1
    Write-Host "`n  >> Haz ahora la venta a credito en http://localhost:8080 (Punto de venta)." -ForegroundColor Magenta
    Pausa "ENTER cuando la venta este hecha"
    Write-Host "  Eventos pendientes por grupo (lag = eventos que el grupo aun no ha leido):" -ForegroundColor Gray
    docker exec $redis redis-cli XINFO GROUPS gestpro:events | Out-String -Stream |
        Select-String -Pattern '^(name|lag)$' -Context 0, 1 | ForEach-Object { $_.Context.PostContext } |
        ForEach-Object -Begin { $i = 0; $par = @() } -Process { $par += $_; if (++$i % 2 -eq 0) { "    grupo=$($par[0])  lag=$($par[1])"; $par = @() } }
    Pausa "ENTER para volver a encender clients-credits"
    Cmd "docker service scale ${Stack}_clients-credits=2"
    Write-Host "  En la app: la linea de tiempo de la venta marca 'clients-credits sumo la deuda'." -ForegroundColor Gray
    Pausa

}
# --------------------------------------------------------------------------------
if ($Pasos -contains 4) {
    Paso "4. Actualizacion fallida -> rollback automatico" "Desplegamos una 'version nueva' de pos-sales que falla su healthcheck (SIMULAR_FALLO=1).
    - update_config: una replica a la vez, start-first, failure_action: rollback.
    - La version nueva nunca queda sana -> Swarm vuelve sola a la version anterior.
    - Mientras tanto la API sigue respondiendo (las replicas viejas nunca se apagaron)."
    Pausa "ENTER para desplegar la version rota"
    Cmd "docker service update --detach --env-add SIMULAR_FALLO=1 ${Stack}_pos-sales | Out-Null"
    $t = Get-Date; $ultimo = ""
    do {
        Start-Sleep 2
        $st = docker service inspect "${Stack}_pos-sales" --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}'
        if ($st -ne $ultimo) { Write-Host ("  {0,4:N0} s  {1}" -f ((Get-Date) - $t).TotalSeconds, $st) -ForegroundColor Green; $ultimo = $st }
    } while ($st -notin @('rollback_completed', 'rollback_paused', 'completed', 'paused') -and ((Get-Date) - $t).TotalSeconds -lt 180)
    Cmd "docker service inspect ${Stack}_pos-sales --format 'Resultado: {{.UpdateStatus.Message}}'"
    Pausa

}
# --------------------------------------------------------------------------------
if ($Pasos -contains 5) {
    Paso "5. MapReduce: un worker muere a mitad del job" "Lanza el job desde la app (Reportes -> Ejecutar MapReduce) y, mientras corre, matamos un worker.
    - Las tareas se toman con BLMOVE (cola confiable) y cada worker renueva un 'lease' en Redis.
    - El lease del worker muerto expira y el coordinador devuelve su particion a la cola.
    - El job termina con el mismo resultado (la app muestra 'particiones reintentadas')."
    Write-Host "`n  >> Lanza ahora el job MapReduce en la app." -ForegroundColor Magenta
    Pausa "ENTER para matar un worker (hazlo mientras el job esta en curso)"
    $worker = docker ps -q -f "name=${Stack}_mr-worker" | Select-Object -First 1
    Cmd "docker kill $worker"
    Esperar-Replicas "mr-worker" "3/3"

}
Write-Host "`nDemo de alta disponibilidad terminada." -ForegroundColor Cyan
