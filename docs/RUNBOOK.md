# Runbook

## Local

```bash
make up
make demo-dry-run
make demo
curl -fsS http://localhost:8000/health/ready
make down
```

Recriar fixtures limpas é destrutivo para dados locais: `docker compose down -v`, depois `make up`. Não executar esse comando em contexto que contenha dados a preservar.

## Job de produção (contrato, não provisionado)

Scheduler externo invoca `ouros-audience-engine run-once --publish` em processo separado a cada 30 minutos UTC. A CLI obtém um advisory lock de sessão no Postgres; processos concorrentes aguardam até 60 segundos e falham sem publicar se não conseguirem o lock. `--dry-run` não publica objetos/metadados, embora possa popular o cache read-through. A API administrativa não está habilitada.

Exemplo de crontab para uma instalação que já tenha secrets e serviços configurados:

```cron
*/30 * * * * /opt/ouros-audience-engine/venv/bin/ouros-audience-engine run-once --publish
```

O processo imprime JSON por audiência; erro é escrito em stderr e resulta em falha de processo. Supervisione exit code/log do scheduler. Não há cron instalado pelo Compose, job-health persistente, métricas Prometheus nem alertas neste repo.

## Incidentes

- `401` na resolução: confirmar bearer enviado pelo cliente Spring e secret sincronizado; nunca ativar rota anônima.
- `503` da resolução: conferir readiness, banco de metadados, Redis, acesso S3/objeto e SHA-256. Não retornar tip vazia como workaround.
- Job falha antes do CAS: ponteiro anterior continua publicado; objetos imutáveis que já foram enviados podem ficar órfãos. Não apagar objetos manualmente sem provar que não estão referenciados.
- Erro CAS (`ConcurrentPublishError`): verificar jobs concorrentes; reexecutar depois que houver uma única execução. O processo não repete/recalcula automaticamente.
- Resultado `UNCHANGED_UNKNOWN`: nenhuma fazenda teve avaliação válida; snapshot anterior foi preservado. Investigar freshness/dados; não forçar bitmap vazio.
- `STALE` no contrato: watermark mais antigo que o limite da configuração; cliente deve distinguir isso de resolução.

## Transições e entrega

Transições são artefatos imutáveis no S3, mas não há dispatcher/replay de ponta a ponta nem receiver idempotente. O cursor `dispatched_through_version` é reservado e fica parado. Não avance manualmente e não configure FCM até completar ADR-0002 e integração Spring.
