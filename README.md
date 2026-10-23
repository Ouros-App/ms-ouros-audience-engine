# Ouros Audience Engine

Serviço interno Python para calcular audiências determinísticas de `FARM_OWNER`. As definições das audiências são lidas do MongoDB; o SQL fornece os dados de origem, e os membros são serializados como Roaring Bitmaps versionados no Redis, que é o armazenamento principal. PostgreSQL guarda catálogo e referências. Se o Redis perder os snapshots, a primeira resolução os reconstrói da base de origem. O serviço não gera textos de dicas, não chama Firebase e não altera repositórios ou bancos externos.

## Estado e limites

- Implementados neste repositório: API FastAPI interna com JWT Keycloak (RS256, issuer, audience e identidade/role FARM_OWNER), definições validadas no MongoDB, leitura PostgreSQL read-only, cálculo de membership, snapshots versionados no Redis e metadados/CAS.
- A regra `high_water_consumption` é uma fixture de QA, não uma regra aprovada para produtores reais. Seu conjunto de dados e esquema são sintéticos; valide a semântica e o schema reais antes de configurar qualquer ambiente externo.
- Não implementados aqui: integração `GET /tips/me` e autorização JWT no Spring, histórico/dispatcher de transições, persistência de notificações/FCM, mudanças no banco versionado, Android, scheduler gerenciado e retenção/GC dos snapshots Redis.
- Não declarar o serviço pronto para produção. Não configure credenciais reais no Compose e não execute as SQL de demo em banco que não seja o stack local descartável.

## Requisitos

- Python `>=3.11,<3.15`
- Docker Engine com `docker compose`
- `make` para os comandos de demonstração

## Execução e demonstração local

O Compose inicializa Postgres, MongoDB, Redis, schema/roles locais, fixtures e API. MongoDB local recebe apenas uma regra de demo; Redis local grava RDB snapshots após pelo menos uma alteração em 60 segundos, mantém AOF habilitado e armazena ambos no volume Docker. Esses volumes preservam dados em reinicializações comuns, não substituem backup; fixtures não são produção. Os usuários técnicos PostgreSQL têm privilégios separados: `audience_source` lê somente tabelas de origem; `audience_metadata` lê o catálogo/vínculos e atualiza apenas referências de audiência.

```bash
make up
make demo-dry-run
make demo
```

`make demo` publica o snapshot inicial de duas fazendas e três produtores: os usuários 1 e 2 ficam elegíveis; o usuário 3 não. Bootstrap não grava histórico de transição nem envia push. Rodar `make demo` outra vez sem nova medição não cria uma nova versão quando conteúdo e versão de regra permanecem iguais.

Para demonstrar entrada e resolução com dados locais:

```bash
make demo-activate
make demo-activate  # repetição idempotente; não cria uma nova medição
make demo-resolve
```

`make demo-activate` registra medição sintética fechada em setembro de 2026; o usuário 3 entra no membership. `make demo-resolve` insere medição sintética de outubro e executa com data QA `2026-11-01T12:00:00Z`; o usuário 3 sai por avaliação válida. Nenhuma transição é persistida ou enviada. A data de avaliação simulada é recusada em `ENVIRONMENT=production`. Estes comandos dependem da sequência: snapshot inicial, ativação, depois resolução.

A API local fica em `http://localhost:8000`. Verifique prontidão e resolução:

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
```
Antes de usar este exemplo, configure a audience `ms-ouros-audience-engine` no client emissor do Keycloak e use um token cujo claim `database_id` seja `1`, igual ao `user_id` enviado:

```bash
curl -fsS http://localhost:8000/internal/v1/tips/resolve \
  -H "Authorization: Bearer $KEYCLOAK_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1,"user_role":"FARM_OWNER","request_id":"d97a8b61-9c9a-460e-a676-86ab2fa0ae53"}'
```

Sem bearer Keycloak válido, a resolução retorna `401`; tokens sem role `farm_owner` ou cuja identidade não corresponde ao `user_id` enviado retornam `403`. A resposta contém somente tip IDs, versão agregada e estado de frescor; os textos permanecem no catálogo. Snapshot Redis ausente aciona rebuild síncrono; falha de Redis, da origem ou da validação retorna `503`, nunca uma resposta vazia.

Inspecione o ponteiro e as chaves Redis sem consultar IDs de membros:

```bash
docker compose exec postgres psql -U audience_owner -d audience -c \
  'SELECT audience_key, snapshot_version, s3_object_key, s3_manifest_key, bitmap_sha256, source_watermark FROM audience_snapshot_refs'
docker compose exec redis redis-cli --scan --pattern 'ouros:audience:bitmap:*'
docker compose exec postgres psql -U audience_owner -d audience -c \
  'SELECT tip_id, audience_key, enabled FROM tip_audience_refs ORDER BY tip_id'
```

`s3_object_key` e `s3_manifest_key` são nomes legados do schema; seus valores contêm chaves Redis. `docker compose down -v` apaga os volumes e os snapshots locais. Após um reset do Redis, a primeira resolução reconstrói a audiência como bootstrap novo; a histerese e a preservação de membership UNKNOWN não sobrevivem ao reset.

## Execução da CLI

`--dry-run` avalia sem gravar snapshots ou metadados; `--publish` publica o bitmap e o manifest no Redis e atualiza o ponteiro PostgreSQL. A CLI é a forma de invocação pelo scheduler externo.

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

O CI roda pytest com cobertura mínima de 80%, Ruff e mypy; valida o Compose e faz build da imagem. CodeQL roda como análise separada. Para testar o stack local, `make up` seguido dos comandos de demo exercita Postgres, Redis, publicação, resolução e rebuild após perda de snapshot.

## Configuração

Cada documento em `ouros_audience.audience_definitions` define uma audiência; o modelo Pydantic valida os campos e `key` tem índice único. Mongo guarda regras, nunca usuários ou membership. A API carrega definições ao iniciar; alterações via rotas administrativas atualizam apenas o processo que atendeu à requisição. Reinicie os demais processos/replicas após CRUD por API; alterações diretas no Mongo exigem reiniciar todos os processos da API. A CLI recarrega definições a cada execução. O banco de produção foi criado vazio: não promova a fixture QA como regra real.

Variáveis disponíveis estão em `.env.example`. Valores e senhas ali são exemplos locais, não segredos reutilizáveis. Em produção use secret manager e TLS para PostgreSQL, MongoDB, Redis e HTTP; configure Redis com `noeviction`, persistência RDB em armazenamento durável e backups/retention do provedor. RDB é periódico e, sozinho, pode perder alterações desde o último snapshot.

## Contratos e decisões

- [Arquitetura e limites](docs/ARCHITECTURE.md)
- [Contratos de dados e regra de QA](docs/DATA_CONTRACTS.md)
- [Runbook operacional](docs/RUNBOOK.md)
- [Demonstração local](docs/DEMO.md)
- [Segurança](docs/SECURITY.md)
- [Integração futura com Spring](docs/SPRING_INTEGRATION.md)
- [ADR-0001 — formato e armazenamento Redis](docs/adr/0001-s3-roaring-redis.md)
- [ADR-0002 — transições e entrega](docs/adr/0002-notification-delivery.md)

A migration `migrations/001_audience_metadata.sql` contém apenas metadados e deve ser revisada/aplicada pelo DBA no processo apropriado; a harness não a executou em banco externo. Os nomes `s3_object_key` e `s3_manifest_key` foram mantidos para compatibilidade com o schema implantado, mas agora armazenam chaves Redis. `docker/postgres/001_source_schema.sql` é fixture para Compose, não declaração do schema de produção. Integrações cross-repo requerem PRs e autorização próprios.
