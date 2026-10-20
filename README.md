# Ouros Audience Engine

Serviço interno Python para calcular audiências determinísticas de `FARM_OWNER`. Os membros são serializados como Roaring Bitmaps imutáveis em S3; PostgreSQL guarda somente catálogo e referências; Redis funciona como cache read-through. O serviço não gera textos de dicas, não chama Firebase e não altera repositórios ou bancos externos.

## Estado e limites

- Implementados neste repositório: FastAPI interna autenticada, leitura PostgreSQL read-only, regra configurável de consumo de água, publicação versionada S3, cache Redis, metadados/CAS, manifests de snapshot e transição, CLI single-run e fixtures locais.
- A regra `high_water_consumption` é uma fixture de QA, não uma regra aprovada para produtores reais. Seu conjunto de dados e esquema são sintéticos; valide a semântica e o schema reais antes de configurar qualquer ambiente externo.
- Não implementados aqui: integração `GET /tips/me` e autorização JWT no Spring, consumidor idempotente de eventos, persistência de notificações/FCM, mudanças no banco versionado, Android, scheduler gerenciado, retenção/GC S3 e infraestrutura de produção.
- Não declarar o serviço pronto para produção. Não configure credenciais reais no Compose e não execute as SQL de demo em banco que não seja o stack local descartável.

## Requisitos

- Python 3.12+
- Docker Engine com `docker compose`
- `make` para os comandos de demonstração

## Execução e demonstração local

O Compose inicializa Postgres, Redis, MinIO, schema/roles locais, fixtures e API. Os usuários técnicos têm privilégios separados: `audience_source` lê somente tabelas de origem; `audience_metadata` lê o catálogo/vínculos e atualiza apenas referências de audiência. As senhas são valores descartáveis de demonstração.

```bash
make up
make demo-dry-run
make demo
```

`make demo` publica o snapshot inicial de duas fazendas e três produtores: os usuários 1 e 2 ficam elegíveis; o usuário 3 não. Bootstrap não cria transição de ativação nem envia push. Rodar `make demo` outra vez sem nova medição não cria uma nova versão quando conteúdo e versão de regra permanecem iguais.

Para demonstrar entrada e resolução com dados locais:

```bash
make demo-activate
make demo-activate  # repetição idempotente; não cria uma nova medição
make demo-resolve
```

`make demo-activate` registra medição sintética fechada em setembro de 2026; o usuário 3 entra. `make demo-resolve` insere medição sintética de outubro e executa com data QA `2026-11-01T12:00:00Z`; o usuário 3 sai por avaliação válida. A data de avaliação simulada é recusada em `ENVIRONMENT=production`. Estes comandos dependem da sequência: snapshot inicial, ativação, depois resolução.

A API local fica em `http://localhost:8000`; MinIO Console em `http://localhost:9001` (`minioadmin` / `minioadmin`). Verifique prontidão e resolução:

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/internal/v1/tips/resolve \
  -H 'Authorization: Bearer local-only-change-me' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1,"user_role":"FARM_OWNER","request_id":"d97a8b61-9c9a-460e-a676-86ab2fa0ae53"}'
```

Sem o token, a resolução retorna `401`. O endpoint retorna somente tip IDs, versão agregada e estado de frescor; os textos permanecem no catálogo de dicas. Falha ao carregar snapshot ou verificar checksum retorna `503`, nunca uma resposta vazia que pareça resolução.

Inspecione ponteiro e cursor sem consultar membros persistidos:

```bash
docker compose exec postgres psql -U audience_owner -d audience -c \
  'SELECT audience_key, snapshot_version, s3_object_key, source_watermark, dispatched_through_version FROM audience_snapshot_refs'
docker compose exec postgres psql -U audience_owner -d audience -c \
  'SELECT tip_id, audience_key, enabled FROM tip_audience_refs ORDER BY tip_id'
```

Os objetos versionados e manifests podem ser inspecionados pelo Console MinIO. Para desligar os serviços preservando os volumes: `make down`. Para apagar todos os dados locais e recriar fixtures, use explicitamente `docker compose down -v`.

## Execução da CLI

A CLI exige selecionar explicitamente um modo. `--dry-run` executa leitura e avaliação sem publicar snapshot/transições; pode preencher o cache read-through. `--publish` grava mudanças e é a forma de invocação por scheduler externo.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
ouros-audience-engine run-once --dry-run
ouros-audience-engine run-once --publish
```

A instalação local requer serviços disponíveis e URLs/credenciais em `.env`. Compose injeta sua própria configuração. Para simular períodos fechados em QA, a CLI aceita `--as-of 2026-11-01T12:00:00Z`; exige timestamp ISO-8601 com fuso e não é permitido em produção. A execução agendada deve chamar a CLI como processo separado, sem agendador embutido em workers ASGI. O contrato operacional inicial é `*/30 * * * *` UTC, configurado no scheduler do ambiente, com um único job por ambiente.

## Desenvolvimento

```bash
pip install -e '.[dev]'
make test
make lint
make typecheck
```

O CI verifica pytest, Ruff e mypy. Para testar o stack de integração local, `make up` seguido dos comandos de demo exercita Postgres, Redis, MinIO, publicação, resolução e transições.

## Configuração

`config/audiences.example.yaml` é o modelo conservador com audiências desativadas. Compose seleciona `config/audiences.demo.yaml`, que habilita somente a fixture local. Produção deve fornecer sua própria configuração revisada. Todas as chaves de audiência, limiares, freshness e políticas são validadas; YAML não aceita SQL arbitrário.

Variáveis disponíveis estão em `.env.example`. Valores e senhas ali são exemplos locais, não segredos reutilizáveis. Em produção use secret manager, TLS para PostgreSQL/Redis/S3/HTTP e bucket privado.

## Contratos e decisões

- [Arquitetura e limites](docs/ARCHITECTURE.md)
- [Contratos de dados e regra de QA](docs/DATA_CONTRACTS.md)
- [Runbook operacional](docs/RUNBOOK.md)
- [Demonstração local](docs/DEMO.md)
- [Segurança](docs/SECURITY.md)
- [Integração futura com Spring](docs/SPRING_INTEGRATION.md)
- [ADR-0001 — formato, S3 e cache](docs/adr/0001-s3-roaring-redis.md)
- [ADR-0002 — transições e entrega](docs/adr/0002-notification-delivery.md)

A migration `migrations/001_audience_metadata.sql` contém apenas metadados e deve ser revisada/aplicada pelo DBA no processo apropriado; a harness não a executou em banco externo. `docker/postgres/001_source_schema.sql` é fixture para Compose, não declaração do schema de produção. Integrações cross-repo requerem PRs e autorização próprios.
