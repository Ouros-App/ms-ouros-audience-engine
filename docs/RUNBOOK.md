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

Mongo local usa `ouros_audience.audience_definitions` no volume `mongo_data`; o init script cria somente uma regra sintética para demo, nunca para produção. Use as rotas administrativas da API para gerenciar definições. Cada gravação recarrega apenas o processo que atendeu à requisição; reinicie os demais workers/réplicas. Alterações diretas no Mongo exigem reinício de todos os processos da API. A CLI carrega as definições ao iniciar cada execução.

Antes do deploy, disponibilize `MONGODB_URI` em `/ms-ouros-audience-engine` no Infisical, com uma role customizada restrita a `find`, `insert`, `update` e `remove` na coleção `ouros_audience.audience_definitions`. Aplique separadamente o índice único de `key`; a aplicação não cria índices e a credencial de serviço não precisa de `createIndex`. Não reutilize a URI `atlasAdmin` usada para provisionar o banco.

Redis local executa RDB (`save 60 1`) e mantém AOF habilitado; os arquivos ficam no volume `redis_data`. RDB é periódico e pode omitir alterações recentes se a persistência AOF também falhar. Não remova o volume. Em produção, habilite RDB/backups no provedor em armazenamento durável e confirme retenção e política `noeviction`.

## Job de produção (contrato, não provisionado)

Scheduler externo invoca `ouros-audience-engine run-once --publish` em processo separado a cada 30 minutos UTC. A CLI obtém advisory lock de sessão no Postgres; processos concorrentes aguardam até 60 segundos. `--dry-run` não grava snapshots/metadados; `--publish` grava bitmap/manifest no Redis e atualiza o ponteiro. A API também reconstrói snapshots ausentes, sob o mesmo lock.

Exemplo de crontab para uma instalação que já tenha secrets e serviços configurados:

```cron
*/30 * * * * /opt/ouros-audience-engine/venv/bin/ouros-audience-engine run-once --publish
```

O processo imprime JSON por audiência; falhas individuais são incluídas como `FAILED`, registradas em stderr sem a mensagem da exceção e não interrompem as audiências seguintes. Depois de processar todas, qualquer falha retorna exit code `1`. Supervisione exit code/log do scheduler. Não há cron instalado pelo Compose, job-health persistente, métricas Prometheus nem alertas neste repo.

## Incidentes

- `401` na resolução: confirmar token vigente e correspondência de issuer, JWKS e audience `ms-ouros-audience-engine`; `403` indica role, escopo da fazenda ou `user_id` divergente.
- `503` da resolução: conferir readiness, PostgreSQL de metadados/origem, conexão Redis e digest/manifest do snapshot.
- Job falha antes do CAS: o ponteiro anterior continua publicado; chaves Redis imutáveis já gravadas podem ficar sem referência. Não as apague manualmente sem confirmar que não estão apontadas pelo PostgreSQL.
- Erro CAS (`ConcurrentPublishError`): verificar jobs concorrentes; reexecutar depois que houver uma única execução. O processo não repete/recalcula automaticamente.
- Resultado `UNCHANGED_UNKNOWN`: nenhuma fazenda teve avaliação válida; snapshot anterior foi preservado. Investigar freshness/dados; não forçar bitmap vazio.
- `STALE` no contrato: watermark mais antigo que o limite da configuração; cliente deve distinguir isso de resolução.
- Redis indisponível: a resolução falha fechada com `503`; não há fallback S3. Quando a conexão volta mas um snapshot está ausente, a primeira resolução o reconstrói.

## Reset e rebuild Redis

API e scheduler serializam rebuilds pelo advisory lock PostgreSQL. Após reset do Redis, o rebuild usa a fonte PostgreSQL como bootstrap novo, sem bitmap anterior; fazendas UNKNOWN podem perder membership até nova medição válida. Se nenhuma fazenda tiver avaliação válida, o rebuild falha e a API continua retornando `503`, em vez de membership vazia.

Não há histórico de transições nem dispatcher neste MVP. A coluna legada `dispatched_through_version` permanece sem uso e não deve ser avançada manualmente.
