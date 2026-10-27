# Arquitetura e limites

## Fluxo neste repositório

1. API carrega definições de audiência do MongoDB ao iniciar; CRUD administrativo recarrega a configuração somente no processo que atende à chamada. Outros processos/replicas precisam reiniciar, e mudanças diretas no Mongo exigem reiniciar todos os processos. A CLI carrega definições frescas a cada execução. Ambos usam PostgreSQL de origem/metadados e Redis.
2. `SourcePostgresRepository` usa `default_transaction_read_only=on` e lê fazendas, proprietários, última leitura de hidrômetro e registros mensais de água. `MetadataPostgresRepository` lê observações tipadas por fazenda/dica.
3. `DailyWaterMeterAuditRule` recomenda a dica 121 quando não há leitura nas últimas 24 horas; `TipObservationRule` avalia sinais POC; `WaterConsumptionRule` permanece QA, sem contrato de periodicidade validado.
4. `AudiencePublisher` expande fazendas ativas aos seus `farm_owners`, serializa `pyroaring.BitMap`, grava bitmap e manifest versionados no Redis e publica o ponteiro PostgreSQL via CAS.
5. FastAPI recebe `user_id` e `FARM_OWNER` apenas de cliente interno autenticado, carrega os vínculos de dicas e verifica membership no Redis. Se o snapshot estiver ausente, reconstrói de forma síncrona sob o mesmo advisory lock do scheduler. O texto da dica fica fora deste serviço.

## Persistência

PostgreSQL guarda `audience_snapshot_refs`, `tip_audience_refs` e o snapshot atual de sinais em `farm_tip_observations`; não armazena IDs de membros. MongoDB guarda apenas as definições em `ouros_audience.audience_definitions`; não contém usuários nem membership.

Redis é o armazenamento principal dos snapshots. Bitmap e manifest usam chaves versionadas sem TTL. Um reset do Redis apaga os snapshots; a API os reconstrói usando a definição Mongo e a base PostgreSQL de origem como bootstrap novo.

Bitmap e manifest são gravados antes do CAS dos metadados. Um CAS malsucedido pode deixar chaves Redis sem referência; histórico de transições e coleta de lixo não fazem parte do MVP.

## Observações de regras POC

O endpoint de observações é restrito a administradores Keycloak e substitui o conjunto atual de sinais por `(farm_id, tip_id)`. A API valida os campos/tipos exigidos pela definição Mongo; a tabela guarda somente a última observação, não um histórico. Valores ausentes, futuros ou expirados viram UNKNOWN; o publisher preserva membership anterior apenas para owners ainda vinculados, revoga os removidos mesmo se todas as fazendas estiverem UNKNOWN e publica watermark nulo quando qualquer fazenda com owners estiver UNKNOWN. Bootstrap sem avaliação válida falha fechado.

As 32 regras de observação POC e seus vínculos são criados desabilitados. `daily_hydrometer_audit` é a exceção habilitada: usa apenas a data da leitura mais recente, vincula a dica 121 e tem watermark da consulta de origem. As outras dicas precisam de sinais válidos; não preencha valores ausentes.

## Identidade e regra fixture

`pyroaring.BitMap` usa IDs positivos de 32 bits. O limite aplicado (2.147.483.647) corresponde ao tipo `SERIAL` do schema local fornecido, não a uma confirmação do domínio de IDs de produção. A aplicação rejeita valores fora do limite e nunca trunca.

`high_water_consumption` é exclusivamente regra de QA. A fixture usa medições fechadas mensais; cada `end_hydrometer - start_hydrometer` é somado no mês. O schema observado não fornece intervalos de leitura nem completude suficiente para normalizar durações, então essa agregação não é uma regra produtiva aprovada. Adoção externa depende de validar schema, unidade, periodicidade, correções retroativas e disponibilidade real.

## Processos e limites externos

Scheduler externo executa `ouros-audience-engine run-once --publish` a cada 30 minutos UTC. `seed-tip-audiences` cria as 32 regras de observação desabilitadas e `daily_hydrometer_audit`; nenhum seed publica snapshots. Rebuilds disparados pela API usam o mesmo advisory lock PostgreSQL e verificam o Redis novamente após adquiri-lo.

Histórico de transições, replay de eventos e entrega via FCM ficam fora deste MVP. A saída do job pode informar deltas em memória, mas nenhum artefato de transição é armazenado; a coluna legada `dispatched_through_version` não é usada.

Ver [ADR-0001](adr/0001-s3-roaring-redis.md) e [ADR-0002](adr/0002-notification-delivery.md) para as decisões de storage e entrega.
