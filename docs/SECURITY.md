# Segurança

## Controles implementados

- API de resolução exige bearer service token configurado e comparação em tempo constante; token ausente em configuração resulta em `503`, chamada sem token/inválida em `401`.
- Modelo Pydantic rejeita campos extras, papéis diferentes de `FARM_OWNER` e IDs fora do domínio suportado.
- A conexão à origem PostgreSQL abre transação read-only; o schema local concede ao `audience_source` somente SELECT em tabelas operacionais.
- Conta `audience_metadata` tem SELECT em catálogo/vínculos e SELECT/INSERT/UPDATE apenas em referências de snapshot; não recebe grants de escrita na origem.
- Não há membership de usuário no PostgreSQL, texto de dica em snapshot, lista de membros em log ou chamada FCM.
- Compose mantém MinIO privado; os valores e contas são exclusivos para ambiente local descartável.

## Requisitos antes de qualquer ambiente real

1. Confirmar autorização/rede privada e TLS; rotacionar service token por secret manager. O bearer compartilhado não substitui TLS nem identidade de serviço mais forte.
2. Criar roles separadas no banco real, validar grants mínimos contra schema/migration revisados por DBA; não executar scripts `docker/postgres` fora do Compose.
3. Usar bucket privado com criptografia, políticas IAM limitadas a prefixo, versioning/lifecycle/retenção aprovados; `S3Store` deve receber apenas conta de serviço, nunca público.
4. Configurar TLS/autenticação Redis e PostgreSQL; MinIO de Compose não é endpoint de produção.
5. Colocar API atrás de rede interna/gateway TLS, rate limit, timeouts e auditoria de chamada. Endpoints `/docs`/OpenAPI não devem ser expostos publicamente.
6. Validar schema, range real de IDs, semântica de registros e limiares com proprietários dos dados e produto.
7. Projetar e testar dispatcher + receptor Spring com deduplicação durável por `event_id` antes de avançar cursor ou enviar notificações. FCM real permanece fora do serviço.
8. Definir retenção e exclusão de snapshots/eventos/logs segundo LGPD e processo aprovado; não há GC neste repositório.

Não use o serviço como rota pública de consulta de IDs. Spring deve obter `user_id` do principal JWT autenticado e chamar a API interna; o cliente móvel jamais escolhe o ID consultado.
