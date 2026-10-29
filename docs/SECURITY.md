# Segurança

## Controles implementados

- API de resolução valida JWT Keycloak por JWKS e RS256, exigindo `iss`, `aud`, `exp`, `iat` e `sub`; somente identidade `farm_owner` com `database_id`/`farm_id` assinados pode consultar. O `user_id` enviado deve ser igual ao `database_id` do token.
- Modelo Pydantic rejeita campos extras, papéis diferentes de `FARM_OWNER` e IDs fora do domínio suportado.
- A conexão à origem PostgreSQL abre transação read-only; o schema local concede ao `audience_source` somente SELECT em tabelas operacionais.
- Conta `audience_metadata` tem SELECT em catálogo/vínculos e SELECT/INSERT/UPDATE apenas em referências de snapshot; não recebe grants de escrita na origem.
- A resolução não aceita definições de audiência enviadas por clientes e Mongo nunca armazena membership. Rotas administrativas permitem criar, alterar e remover definições com token Keycloak e role `admin`; o `MONGODB_URI` único do serviço lê e grava apenas `ouros_audience.audience_definitions`.
- As rotas `/internal/v1` usam limite distribuído no Redis de 60 requisições por `request.client.host` a cada 60 segundos; exceder o limite retorna `429` com `Retry-After`, e falha do Redis retorna `503`. Em produção no Discloud, configure `FORWARDED_ALLOW_IPS` somente com os IPs de origem confiáveis para que o Uvicorn normalize o IP real; não use `*`. Sem isso, clientes podem compartilhar a quota. `/health/live` e `/health/ready` ficam fora do limite.
- As portas dos serviços Compose são publicadas apenas em `127.0.0.1`; Postgres, Mongo, Redis usam fixtures locais e não substituem serviços de produção.

## Requisitos antes de qualquer ambiente real

1. Confirmar rede privada e TLS até o serviço; manter o issuer/JWKS Keycloak confiável e a audience `ms-ouros-audience-engine` configurada no client de emissão.
2. Criar roles PostgreSQL separadas com grants mínimos. Para Mongo, use role customizada limitada a `find`, `insert`, `update` e `remove` na coleção `ouros_audience.audience_definitions`; provisionar o índice único de `key` fora da credencial da aplicação. Não reutilize `atlasAdmin` no serviço.
3. Configurar Redis de produção com TLS/autenticação, capacidade suficiente e política `noeviction`; habilitar persistência RDB em armazenamento durável e backups/retention do provedor. RDB é periódico, não garante perda zero; a aplicação reconstrói snapshots ausentes, mas um bootstrap novo pode alterar membership em fazendas UNKNOWN ou na faixa de histerese.
4. Configurar TLS/autenticação para PostgreSQL, MongoDB e Redis. O Redis local grava RDB a cada 60 segundos após ao menos uma alteração, mantém AOF habilitado e usa volume Docker; isso não é backup de produção.
5. Manter a API atrás de rede interna/gateway TLS, com rate limit de borda, timeouts e auditoria de chamada. O limitador da aplicação não mitiga DDoS volumétrico antes do tráfego chegar ao processo. Endpoints `/docs`/OpenAPI não devem ser expostos publicamente.
6. Validar schema, range real de IDs, semântica de registros e limiares com proprietários dos dados e produto.
7. Notificações ficam fora do MVP. Para implementá-las, definir contrato idempotente com Spring e persistir histórico/outbox que sobreviva ao reset Redis; FCM deve permanecer after-commit/outbox no Spring.
8. Definir retenção e exclusão de snapshots Redis conforme LGPD. Não há coleta de versões antigas nem histórico de transições neste MVP.

Não use o serviço como rota pública de consulta de IDs. Spring deve obter `user_id` do principal JWT autenticado e chamar a API interna; o cliente móvel jamais escolhe o ID consultado.
