# ADR-0001: Roaring portable em S3, Redis como cache

- Status: aceito para fixture local; aprovação de produção pendente.
- Data: 2026-10-19.

## Contexto

A audiência contém IDs estáveis de `FARM_OWNER`, pode ser grande, e não deve ser materializada como relação produtor×dica em SQL. O serviço precisa responder membership interno sem transferir a lista de membros.

## Decisão

Usar `pyroaring.BitMap` serializado no formato portable, suportando IDs positivos de 32 bits (domínio de `SERIAL` do schema fixture). Validar o limite antes de codificar/usar; sem coerção/truncamento. Cada versão tem objetos imutáveis em S3 e SHA-256 dos bytes. O manifest JSON validado identifica codec, versão, audience, versão da regra, ID-space, watermark, bootstrap, quantidade e transições. Redis armazena os mesmos bytes como string binária sob chave versionada com TTL e é preenchido por read-through.

Postgres só mantém ponteiro, digest, watermark, timestamps, versão e cursor agregado, além da tabela pequena de vínculos tip→audience. API sempre carrega bitmap via Redis/S3 e responde tip IDs; não retorna membro de terceiros nem texto editorial.

## Consequências

- `GETBIT` de Redis não se aplica; membership usa decode/contains do Roaring.
- Redis pode ser descartado sem perder snapshots. Falha de S3 sem cache válido falha explicitamente.
- S3 recebe uploads antes de CAS no banco; falha/conflict pode deixar objetos não referenciados. Ainda não há GC.
- PostgreSQL `SERIAL` é apenas limite demonstrativo: validar domínio real e schema antes de operar em dados reais. Se IDs excederem 32 bits, migrar para `BitMap64` com formato/versionamento distinto e testes de interoperabilidade antes de publicação.
- `pyroaring` portable foi escolhido por compressão/interoperabilidade do formato, mas nenhum consumidor Java foi implementado/testado.
