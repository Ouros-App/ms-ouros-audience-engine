# ADR-0002: histórico de transições e entrega

- Status: histórico de transições removido do MVP; entrega continua bloqueada por contrato externo.
- Data: 2026-10-19.

## Contexto

Um delta pode ser perdido se depender somente do processo job ou de cache Redis. O handoff exige eventos reprocessáveis, evento idempotente no receptor e nenhum push FCM direto do Python. A API Spring e seu schema de notifications não foram alterados por esta entrega.

## Decisão implementada

O MVP não persiste bitmaps de `added`, `removed-resolved` ou `removed-access-revoked`, nem manifests de transição. O publisher pode calcular contagens em memória para o resultado do job, mas elas não são replayáveis. A coluna PostgreSQL `dispatched_through_version` permanece sem uso no schema já implantado.

## Limite explícito

Não existe dispatcher, expansão para evento por tip/user, event ID estável, receiver, política de retry/batch ou avanço de cursor neste repo. Não há artefatos de transição para replay e nenhuma chamada FCM é feita. Não se deve afirmar que ativação foi notificada.

## Próxima decisão necessária

Antes de implementar entrega, obter contrato autorizado do Spring para ingestão batch de `TIP_ACTIVATED`/`TIP_RESOLVED`, dedupe durável por `event_id`, semântica OR agregada quando uma dica tem múltiplas audiências, limites de lote e confirmação após commit. Como este MVP não guarda deltas, adicionar uma fonte durável de histórico/outbox independente dos snapshots Redis antes de prometer replay. Avançar cursor só depois de confirmação durável; retries devem reproduzir os mesmos IDs de evento. FCM deve ocorrer after-commit/outbox no Spring.

## Consequências

O MVP não permite investigar ou reproduzir deltas entre snapshots e não completa a entrega de notificações. Não configure envio até completar um contrato idempotente com o Spring.
