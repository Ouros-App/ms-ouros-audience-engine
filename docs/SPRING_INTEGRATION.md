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

O MVP atual guarda somente snapshots de membership no Redis; não persiste deltas nem emite eventos por membro. Antes de conectar notificações, acordar endpoint batch do Spring e adicionar uma fonte durável de histórico/outbox que sobreviva ao reset do Redis. O endpoint deve:

- valida role e vínculo sem aceitar payload não autenticado;
- deduplica persistentemente por `event_id` antes de gravar notification;
- aceita retry idempotente e responde apenas depois do commit durável;
- dispara FCM após commit/outbox, sem prometer exactly-once externo;
- distingue ativação, resolução e revogação de acesso; não envia push para resolução/revogação por padrão.

Dica ligada a mais de uma audiência usa OR. Uma saída em uma audiência só resolve a dica se o usuário não estiver em nenhuma outra audiência ligada/habilitada. O cálculo precisa comparar estados anterior/novo agregados a partir de histórico durável; os snapshots Redis atuais não permitem reconstruir deltas perdidos após reset. Bootstrap e mudança de regra não fazem push em massa por padrão.

`GET /tips/me`, filtro de `GET /tips/{id}`, alterações `event_id` em notifications, FCM, Android e migration de produção estão fora deste repo e não podem ser declarados concluídos.

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
