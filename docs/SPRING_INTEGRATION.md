# Integração futura com Spring

Esta nota é contrato de integração proposto, não implementação nem confirmação do schema/segurança atuais do Spring. Requer PRs autorizados separados no Spring e repositório versionado do banco.

## Resolução para `GET /tips/me`

1. Spring autentica JWT e deriva `user_id` e role do principal verificado; não aceita esses valores do Android como parâmetro.
2. Para role `FARM_OWNER`, Spring chama `POST /internal/v1/tips/resolve` uma vez por requisição, com TLS/rede interna e service bearer.
3. Enviar `{ "user_id": <id do principal>, "user_role": "FARM_OWNER", "request_id": "<UUID>" }`.
4. Em `503` ou `source_status: STALE`, não converter falha/incompletude em lista vazia nem exibir como resolução; definir política de UI/retry explicitamente.
5. Consultar textos de tips com uma query parametrizada em lote somente para `tip_ids` recebidos. Deduplicar/ordenar resultado. Não copiar texto de dica para o engine.
6. Rever `GET /tips/{id}` com a mesma autorização; revisar compatibilidade `GET /tips` e `farms_tips` sem mudar sua semântica silenciosamente.
7. Verificar limites de cache/performance e contrato de role, `request_id`, stale e falhas em testes de integração.

Resposta atual do Engine inclui `user_id`, `tip_ids`, `evaluated_at`, `snapshot_info` agregado e `source_status` (`CURRENT`/`STALE`). `request_id` ainda não é ecoado na resposta.

## Ativações/notificações

O serviço atual somente grava deltas bitmap/manifests no S3; não há endpoint/eventos por membro. Antes de conectar, acordar endpoint interno batch do Spring que:

- valida role e vínculo sem aceitar payload não autenticado;
- deduplica persistentemente por `event_id` antes de gravar notification;
- aceita retry idempotente e responde apenas depois do commit durável;
- dispara FCM após commit/outbox, sem prometer exactly-once externo;
- distingue ativação, resolução e revogação de acesso; não envia push para resolução/revogação por padrão.

Dica ligada a mais de uma audiência usa OR. Uma saída em uma audiência só resolve dica se o usuário não estiver em nenhuma outra audiência ligada/habilitada. O cálculo precisa comparar os estados anterior/novo agregados, não encaminhar deltas individuais diretamente. Bootstrap e mudança de regra não fazem push em massa por padrão.

`GET /tips/me`, filtro de `GET /tips/{id}`, alterações `event_id` em notifications, FCM, Android e migration de produção estão fora deste repo e não podem ser declarados concluídos.
