# Integração futura com Spring

Esta nota é contrato de integração proposto, não implementação nem confirmação do schema/segurança atuais do Spring. Requer PRs autorizados separados no Spring e repositório versionado do banco.

## Resolução para `GET /tips/me`

1. Spring autentica JWT e deriva `user_id` e role do principal verificado; não aceita esses valores do Android como parâmetro.
2. Para role `FARM_OWNER`, Spring chama `POST /internal/v1/tips/resolve` uma vez por requisição, em TLS/rede interna, encaminhando o access token Keycloak original validado.
3. Enviar `{ "user_id": <database_id do principal>, "user_role": "FARM_OWNER", "request_id": "<UUID>" }`; o Engine valida o JWT, audience e role e exige igualdade entre `user_id` e o claim assinado `database_id`. O client de emissão deve incluir a audience `ms-ouros-audience-engine`.
4. Em `503` ou `source_status: STALE`, não converter falha/incompletude em lista vazia nem exibir como resolução; definir política de UI/retry explicitamente.
5. Consultar textos de tips com uma query parametrizada em lote somente para `tip_ids` recebidos. Deduplicar/ordenar resultado. Não copiar texto de dica para o engine.
6. Rever `GET /tips/{id}` com a mesma autorização; revisar compatibilidade `GET /tips` e `farms_tips` sem mudar sua semântica silenciosamente.
7. Verificar limites de cache/performance e contrato de role, `request_id`, stale e falhas em testes de integração.

Resposta atual do Engine inclui `user_id`, `tip_ids`, `evaluated_at`, `snapshot_info` agregado e `source_status` (`CURRENT`/`STALE`). `request_id` ainda não é ecoado na resposta.

## Ativações/notificações

O contrato implementado pelo helper é `POST /internal/v1/notifications/batch` com `X-Internal-Service-Key`, em lotes de até 500 notificações. Cada item inclui `event_id`, `user_id`, `user_role`, `title`, `message`, `type`, `id_tip`, `data_payload` e `send_push`. `farm_owners.id` corresponde ao ID de `FARM_OWNER` no Spring: `UserPrincipal.create(FarmOwner)` usa `farmOwner.getId()` e a autorização carrega a fazenda por esse ID.

O helper mantém baseline agregado OR, ciclos pendentes e IDs de membros apenas no Redis; dispatcher recria eventos estáveis e valida contagens antes do ack. `TIP_ACTIVATED` usa push; `TIP_RESOLVED` é in-app; remoções de acesso são suprimidas. Títulos/mensagens neutros: `Nova Dica Técnica!` / `Uma nova dica técnica está disponível para você.` e `Dica resolvida` / `Uma dica técnica foi resolvida para você.`.

Scheduler e CLI `run-once --publish` preparam todos os snapshots habilitados e persistem refs imutáveis e memberships candidatos no ciclo Redis antes de qualquer CAS. Após reinício ou CAS parcial, retomam os mesmos refs/candidatos; não recalculam membership a partir de snapshots mistos. Só calculam deltas e enviam eventos ao Spring após confirmar todos os refs. O baseline só avança depois da resposta completa de cada lote (`total_created + total_skipped == total_received`). Falhas ou chave ausente mantêm ciclo e outbox pendentes para retry. Não há membership no PostgreSQL. Configure `SPRING_API_URL` (padrão `https://ms-spring-api.discloud.app`) e `SPRING_INTERNAL_SERVICE_KEY`. Reset Redis apaga baseline, candidatos e outbox; sem baseline existente, o próximo ciclo inicializa silenciosamente.

## API de gerenciamento de audiências

Todas as rotas ficam sob `/internal/v1`. As rotas administrativas exigem access token válido do Keycloak com `realm_access.roles` contendo `admin`; tokens de proprietário de fazenda não podem gerenciar audiências.

- `GET /users/{user_id}/audiences`: proprietário consulta apenas o próprio `database_id`; admin consulta qualquer ID positivo.
- `GET /admin/audiences?search=...`, `GET /admin/audiences/{key}`, `POST /admin/audiences`, `PUT /admin/audiences/{key}` e `DELETE /admin/audiences/{key}` oferecem busca e CRUD das definições; todas exigem role administrativa Keycloak. A validação aceita apenas métricas já implementadas pelo Engine.
- `GET /admin/tips?search=...&limit=...&offset=...` pesquisa IDs e textos de dicas existentes.
- `PUT /admin/audiences/{key}/tips/{tip_id}` habilita o vínculo com uma dica; `DELETE` nessa rota remove o vínculo. A resolução só retorna a dica quando o snapshot da audiência contém o usuário.

Alterar qualquer campo da definição além de `version` exige uma nova versão. A versão nova invalida o snapshot anterior, que o Engine recalcula na próxima consulta de participação. Ao excluir uma audiência, o Engine remove as chaves bitmap/manifest do Redis e as referências do PostgreSQL antes de remover a definição do MongoDB. Esses bancos não compartilham transação; uma falha durante a exclusão pode exigir reparo administrativo.

Após POST/PUT/DELETE da definição, somente o processo API que atendeu a requisição recarrega sua configuração. Em instalações com vários workers/réplicas, os demais processos e as alterações diretas no Mongo são refletidos no próximo ciclo de 30 minutos, sem reinicialização. `run-once` lê as definições atuais a cada execução.

O role de metadados do banco precisa receber os grants de `migrations/002_audience_admin_api.sql` antes de essas rotas pesquisarem dicas ou alterarem vínculos. A credencial `MONGODB_URI` do serviço precisa de `find`, `insert`, `update` e `remove` restritos à coleção de definições; provisionar o índice único de `key` separadamente.

As dicas de produção inspecionadas não têm predicados de regra aprovados para todos os temas, e o SQL não expõe os campos necessários para várias delas. Não crie nem habilite audiências de produção apenas com base no texto da dica; valide a semântica de participação com o responsável pelo produto.
