# Arquitetura e limites

## Fluxo neste repositório

1. API carrega definições de audiência do MongoDB ao iniciar; CRUD administrativo recarrega a configuração somente no processo que atende à chamada. Outros processos/replicas precisam reiniciar, e mudanças diretas no Mongo exigem reiniciar todos os processos. A CLI carrega definições frescas a cada execução. Ambos usam PostgreSQL de origem/metadados e Redis.
2. `SourcePostgresRepository` usa `default_transaction_read_only=on`, consulta fazendas/proprietários e agrega leituras por mês fechado.
3. `WaterConsumptionRule` compara o período mais recente com a mediana dos três meses consecutivos anteriores, aplica histerese/freshness e retorna ACTIVE, RESOLVED ou UNKNOWN.
4. `AudiencePublisher` expande cada fazenda aos seus `farm_owners`, serializa `pyroaring.BitMap`, grava bitmap e manifest versionados no Redis e publica o ponteiro PostgreSQL via CAS.
5. FastAPI recebe `user_id` e `FARM_OWNER` apenas de cliente interno autenticado, carrega os vínculos de dicas e verifica membership no Redis. Se o snapshot estiver ausente, reconstrói de forma síncrona sob o mesmo advisory lock do scheduler. O texto da dica fica fora deste serviço.

## Persistência

PostgreSQL guarda `audience_snapshot_refs` (ponteiro do snapshot, digest, versão da regra e timestamps) e `tip_audience_refs`; não armazena IDs de membros. MongoDB guarda apenas os documentos de regra em `ouros_audience.audience_definitions`; não contém usuários nem membership.

Redis é o armazenamento principal dos snapshots. Bitmap e manifest usam chaves versionadas sem TTL. Um reset do Redis apaga os snapshots; a API os reconstrói usando a definição Mongo e a base PostgreSQL de origem como bootstrap novo.

Bitmap e manifest são gravados antes do CAS dos metadados. Um CAS malsucedido pode deixar chaves Redis sem referência; histórico de transições e coleta de lixo não fazem parte do MVP.

## Identidade e regra fixture

`pyroaring.BitMap` usa IDs positivos de 32 bits. O limite aplicado (2.147.483.647) corresponde ao tipo `SERIAL` do schema local fornecido, não a uma confirmação do domínio de IDs de produção. A aplicação rejeita valores fora do limite e nunca trunca.

`high_water_consumption` é exclusivamente regra de QA. A fixture usa medições fechadas mensais; cada `end_hydrometer - start_hydrometer` é somado no mês. O schema observado não fornece intervalos de leitura nem completude suficiente para normalizar durações, então essa agregação não é uma regra produtiva aprovada. Adoção externa depende de validar schema, unidade, periodicidade, correções retroativas e disponibilidade real.

## Processos e limites externos

A API não agenda jobs. O scheduler externo executa `ouros-audience-engine run-once --publish` a cada 30 minutos UTC. Rebuilds disparados pela API usam o mesmo advisory lock PostgreSQL e verificam o Redis novamente após adquiri-lo.

Histórico de transições, replay de eventos e entrega via FCM ficam fora deste MVP. A saída do job pode informar deltas em memória, mas nenhum artefato de transição é armazenado; a coluna legada `dispatched_through_version` não é usada.

Ver [ADR-0001](adr/0001-s3-roaring-redis.md) e [ADR-0002](adr/0002-notification-delivery.md) para as decisões de storage e entrega.
