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

`migrations/003_tip_observations.sql` é aditiva. Em um volume Compose já existente, aplique-a sem recriar o volume:

```bash
docker compose exec -T postgres psql -U audience_owner -d audience -v ON_ERROR_STOP=1 < migrations/003_tip_observations.sql
```

O catálogo de regras usa IDs de dicas do banco atual de produção e não corresponde aos dois IDs sintéticos da fixture local. Depois de aplicar as migrations `001`–`003` pelo processo aprovado e implantar este código, execute `ouros-audience-engine seed-tip-audiences` uma vez no ambiente conectado ao banco com as 32 dicas. Ele cria as 32 regras de observação e vínculos desabilitados, mais `daily_hydrometer_audit` habilitada e vinculada à dica 121. Não sobrescreve definições diferentes e não publica snapshots.

`daily_hydrometer_audit` inclui uma fazenda quando não há leitura em `water_registries` nas últimas 24 horas (ou nenhuma leitura); uma leitura recente resolve a condição. `registration_date` é tratado como UTC. As outras 32 regras exigem observações completas via endpoint admin e ficam desabilitadas; não envie valores inventados. Após o seed, a API carrega e publica a definição no próximo ciclo, sem reinício. Use `run-once --dry-run` para validação manual; `run-once --publish` é somente uma execução manual extraordinária.

Em produção, configure no Discloud `ENVIRONMENT=production`, `INFISICAL_ENV=prod`, `INFISICAL_PROJECT_ID`, `INFISICAL_PATH=/ms-ouros-audience-engine` e um `INFISICAL_TOKEN` read-only escopado a `prod:/ms-ouros-audience-engine`; `INFISICAL_HOST` usa `https://app.infisical.com` por padrão. `ENVIRONMENT` é configuração do deploy, nunca um secret do Infisical. O bootstrap do Infisical é obrigatório em produção, valida o path e exige que os quatro secrets abaixo estejam presentes na resposta antes de aplicar qualquer valor ao processo; configuração parcial ou falha de acesso interrompe o startup.

Mantenha nesse path `MONGODB_URI`, `SOURCE_DATABASE_URL`, `METADATA_DATABASE_URL` e `REDIS_URL`. A role Mongo deve ser customizada e restrita a `find`, `insert`, `update` e `remove` na coleção `ouros_audience.audience_definitions`. `audience_metadata` precisa de SELECT em `farms` e SELECT/INSERT/UPDATE em `farm_tip_observations`, concedidos pela migration 003; não precisa de escrita nas tabelas de origem. Aplique separadamente o índice único de `key`; a aplicação não cria índices e a credencial de serviço não precisa de `createIndex`. Não reutilize a URI `atlasAdmin` usada para provisionar o banco.

Redis local executa RDB (`save 60 1`) e mantém AOF habilitado; os arquivos ficam no volume `redis_data`. RDB é periódico e pode omitir alterações recentes se a persistência AOF também falhar. Não remova o volume. Em produção, habilite RDB/backups no provedor em armazenamento durável e confirme retenção e política `noeviction`.

## Publicação periódica em produção

O lifespan da API inicia uma publicação imediatamente e repete o ciclo 30 minutos após a conclusão do anterior. Cada ciclo recarrega as definições do Mongo, atualiza a configuração em memória e publica somente as audiências habilitadas, usando dados do PostgreSQL e salvando snapshots no Redis sob o advisory lock compartilhado. Após `seed-tip-audiences`, a definição é carregada e publicada no próximo ciclo; reinício não é necessário.

`ouros-audience-engine run-once --dry-run` verifica manualmente sem publicar; `run-once --publish` força uma execução manual. Ambos usam o mesmo lock. Falhas de leitura do Mongo ou do lock são registradas e tentadas novamente no ciclo seguinte; falhas por audiência são registradas sem impedir as demais. O scheduler roda em cada processo da API; o comando atual do Discloud inicia um processo, mas escalar workers/instâncias causará ciclos redundantes serializados pelo lock. Não há estado persistente de sucesso, métricas ou alertas.

`/health/ready` verifica dependências da API, mas não confirma que o último ciclo do scheduler publicou snapshots com sucesso.

## Incidentes

- `401` na resolução: confirmar token vigente e correspondência de issuer, JWKS e audience `ms-ouros-audience-engine`; `403` indica role, escopo da fazenda ou `user_id` divergente.
- `503` da resolução: conferir readiness, PostgreSQL de metadados/origem, conexão Redis e digest/manifest do snapshot.
- Job falha antes do CAS: o ponteiro anterior continua publicado; chaves Redis imutáveis já gravadas podem ficar sem referência. Não as apague manualmente sem confirmar que não estão apontadas pelo PostgreSQL.
- Erro CAS (`ConcurrentPublishError`): verificar jobs concorrentes; reexecutar depois que houver uma única execução. O processo não repete/recalcula automaticamente.
- Resultado `UNCHANGED_UNKNOWN`: nenhuma fazenda teve avaliação válida; o bitmap anterior é preservado somente para owners ainda vinculados, proprietários removidos são revogados e o watermark fica `NULL`. Investigar freshness/dados; não forçar bitmap vazio.
- `STALE` no contrato: watermark nulo ou mais antigo que o limite da configuração; cliente deve distinguir isso de resolução.
- Redis indisponível: a resolução falha fechada com `503`; não há fallback S3. Quando a conexão volta mas um snapshot está ausente, a primeira resolução o reconstrói.

## Reset e rebuild Redis

API e scheduler serializam rebuilds pelo advisory lock PostgreSQL. Após reset do Redis, o rebuild usa a fonte PostgreSQL como bootstrap novo, sem bitmap anterior; fazendas UNKNOWN podem perder membership até nova medição válida. Se nenhuma fazenda tiver avaliação válida, o rebuild falha e a API continua retornando `503`, em vez de membership vazia.

Não há histórico de transições nem dispatcher neste MVP. A coluna legada `dispatched_through_version` permanece sem uso e não deve ser avançada manualmente.
