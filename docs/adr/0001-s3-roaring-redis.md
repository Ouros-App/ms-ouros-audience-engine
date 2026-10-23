# ADR-0001: Roaring portable no Redis como armazenamento principal

- Status: aceito para o MVP; contrato de persistência/eviction do Redis de produção ainda precisa ser confirmado.
- Data: 2026-10-19.

## Contexto

A audiência contém IDs estáveis de `FARM_OWNER`, pode ser grande, e não deve ser materializada como relação produtor×dica em SQL. O serviço precisa responder membership interno sem transferir a lista de membros.

## Decisão

Usar `pyroaring.BitMap` serializado no formato portable, suportando IDs positivos de 32 bits (domínio de `SERIAL` do schema fixture). Validar o limite antes de codificar/usar; sem coerção/truncamento. Cada snapshot é armazenado em chaves Redis versionadas e imutáveis, sem TTL; o manifest validado identifica codec, versão, audience, versão da regra, ID-space, watermark, quantidade e bitmap.

Postgres mantém ponteiro, digest, watermark, timestamps e versão, além da tabela pequena de vínculos tip→audience. As colunas `s3_object_key` e `s3_manifest_key` são mantidas pelo contrato de schema existente, mas armazenam chaves Redis. A API lê do Redis e reconstrói snapshots ausentes da base de origem sob o advisory lock compartilhado com o scheduler.

## Consequências

- Redis é a fonte primária dos snapshots; sua indisponibilidade falha explicitamente. Snapshot ausente aciona rebuild; Redis resetado não tem fallback durável.
- Rebuild após reset é fresh bootstrap: a histerese e a preservação de membership UNKNOWN são reiniciadas; fazendas na faixa de 15–25% podem mudar de membership. Esse comportamento foi aceito para o MVP.
- Bitmap e manifest são gravados antes do CAS PostgreSQL; falha/conflito pode deixar chaves Redis sem referência. Não há GC nem histórico de transições.
- PostgreSQL `SERIAL` é apenas limite demonstrativo: validar domínio real e schema antes de operar em dados reais. Se IDs excederem 32 bits, migrar para `BitMap64` com formato/versionamento distinto e testes de interoperabilidade antes de publicação.
- `pyroaring` portable foi escolhido por compressão/interoperabilidade do formato, mas nenhum consumidor Java foi implementado/testado.
