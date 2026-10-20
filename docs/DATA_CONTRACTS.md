# Contratos de dados

## Schema

`docker/postgres/001_source_schema.sql` reproduz um schema mínimo e sintético (`farms`, `farm_owners`, `water_registries`, `tips`) para Compose. Não é migration nem prova de compatibilidade com produção. O único schema consultado é explicitado em `SourcePostgresRepository`; nomes/tipos e vínculo usuário-fazenda precisam ser confirmados contra a versão vigente antes de integração.

A migration `migrations/001_audience_metadata.sql` cria apenas `audience_snapshot_refs` e `tip_audience_refs`. Não contém `user_id`, `farm_id`, membership ou dados binários. `tip_audience_refs.enabled` controla exposição; config de audiência habilitada e link SQL habilitado são ambos necessários.

## Regra QA `high_water_consumption`

- Sujeito: fazenda; membros: IDs de `farm_owners` atualmente vinculados.
- Período: último mês completo antes de `evaluated_at`.
- Consumo do mês: soma não negativa de `end_hydrometer - start_hydrometer` para as linhas do mês.
- Baseline: mediana dos três meses imediatamente anteriores e consecutivos.
- Ativa uma fazenda nova quando excede baseline em pelo menos 25%; resolve fazenda já ativa quando fica abaixo de 15% acima do baseline. Entre limites, estado permanece ativo conforme histerese da implementação.
- Leitura mais antiga que 45 dias, ausência, baseline insuficiente/não consecutivo, watermark sem avanço ou dados inválidos resulta UNKNOWN; membership anterior é preservado. UNKNOWN nunca é RESOLVED.
- Primeira versão publica a composição, mas os bitmaps de entrada/saída do bootstrap ficam vazios.

Esta regra é didática e não aprovada por negócio/agronomia. O schema fixture só tem datas de cadastro e leituras inicial/final; não prova duração/completude de medições. Não use esse cálculo sobre dados reais sem contrato de medição validado.

## Bitmap/manifests

- Codec: `pyroaring.BitMap`, `pyroaring-portable`, versão de formato `1`; IDs positivos até `2^31-1`.
- SHA-256 é calculado sobre bytes serializados exatos. Reader valida digest e bitmap antes de membership.
- Manifest de snapshot registra ID-space, versão da regra, watermark, quantidade, bootstrap, chaves/digests das transições.
- Manifest de transição distingue saída por resolução e revogação de acesso. Os eventos Spring não são materializados nem enviados neste repo.
- Nenhum manifest contém nomes, email, tokens ou texto de tip.

## API de resolução

`POST /internal/v1/tips/resolve` exige `Authorization: Bearer` e JSON estrito `{user_id, user_role: "FARM_OWNER", request_id}`. IDs negativos/zero/fora de faixa, papéis não permitidos e campos extras são rejeitados. A resposta possui somente `tip_ids` ordenados sem duplicata, timestamp, versões de audiência e `source_status`. Um erro de vínculo, snapshot, S3 ou checksum é `503`, não lista vazia.

Os textos de dicas e autenticação JWT pertencem à integração Spring documentada em [SPRING_INTEGRATION.md](SPRING_INTEGRATION.md).
