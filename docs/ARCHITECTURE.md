# Arquitetura e limites

## Fluxo neste repositório

1. CLI de processo único lê YAML e abre adaptadores PostgreSQL, S3 e Redis.
2. `SourcePostgresRepository` usa conexão `default_transaction_read_only=on`, consulta farms/owners e agrega leituras pelo mês fechado.
3. `WaterConsumptionRule` compara o período mais recente com a mediana dos três meses consecutivos anteriores, aplica histerese/freshness e retorna ACTIVE, RESOLVED ou UNKNOWN.
4. `AudiencePublisher` expande cada fazenda aos IDs de seus `farm_owners`, preserva membros em avaliação UNKNOWN, serializa `pyroaring.BitMap`, escreve objetos e manifests com chaves imutáveis e publica o ponteiro via CAS.
5. FastAPI recebe `user_id` e `FARM_OWNER` somente de um cliente interno autenticado, carrega tip links e verifica membership no bitmap versionado. Texto de dica não sai deste serviço.
6. `BitmapReader` consulta Redis; em miss/invalidez, valida bytes buscados no S3 e tenta preencher cache. Erro de snapshot propaga como `503` na API.

## Persistência

- PostgreSQL guarda `audience_snapshot_refs` (um ponteiro e cursor agregado por audiência) e `tip_audience_refs` (vínculos tip→audience). Não grava lista, array ou linha por membro.
- S3 guarda bitmap, manifest do snapshot, bitmaps de transição e manifest de transição; chaves contêm audiência, versão e digest.
- Redis contém bytes do Roaring com chave versionada e TTL. Redis não é fonte de verdade nem fila durável.
- PostgreSQL e S3 não têm transação distribuída: objetos imutáveis são gravados primeiro; CAS do ponteiro ocorre por último. Conflito pode deixar objeto órfão, sem substituir o ponteiro publicado.

## Identidade e regra fixture

`pyroaring.BitMap` usa IDs positivos de 32 bits. O limite aplicado (2.147.483.647) corresponde ao tipo `SERIAL` do schema local fornecido, não a uma confirmação do domínio de IDs de produção. A aplicação rejeita valores fora do limite e nunca trunca.

`high_water_consumption` é exclusivamente regra de QA. A fixture usa medições fechadas mensais; cada `end_hydrometer - start_hydrometer` é somado no mês. O schema observado não fornece intervalos de leitura nem completude suficiente para normalizar durações, então essa agregação não é uma regra produtiva aprovada. Adoção externa depende de validar schema, unidade, periodicidade, correções retroativas e disponibilidade real.

## Processos e limites externos

A API não agenda jobs. O scheduler externo executa `ouros-audience-engine run-once --publish` a cada 30 minutos UTC. A CLI mantém um advisory lock de sessão no Postgres por até 60 segundos enquanto aguarda, serializando os jobs entre processos; o lock é liberado ao concluir ou fechar a conexão. CAS continua sendo a última proteção do ponteiro. Chamadores que contornem a CLI não adquirem esse lock.

Os manifests atuais preservam deltas `added`, `removed-resolved` e `removed-access-revoked`. Não há dispatcher implementado, consumidor Spring nem envio FCM. O cursor `dispatched_through_version` não avança automaticamente; não trate seus deltas como notificação entregue. Isso é uma fronteira deliberada enquanto não houver contrato de ingestão idempotente aprovado.

Ver [ADR-0001](adr/0001-s3-roaring-redis.md) e [ADR-0002](adr/0002-notification-delivery.md) para decisões e limitações.
