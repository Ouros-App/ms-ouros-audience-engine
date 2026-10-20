# ADR-0002: transições duráveis antes de qualquer entrega

- Status: desenho parcial; dispatcher e integração de entrega bloqueados por contrato externo.
- Data: 2026-10-19.

## Contexto

Um delta pode ser perdido se depender somente do processo job ou de cache Redis. O handoff exige eventos reprocessáveis, evento idempotente no receptor e nenhum push FCM direto do Python. A API Spring e seu schema de notifications não foram alterados por esta entrega.

## Decisão implementada

Ao publicar uma versão que muda, o publisher grava bitmaps imutáveis `added`, `removed-resolved`, `removed-access-revoked`, manifest de transição e manifest de snapshot antes do CAS do ponteiro PostgreSQL. Bootstrap e mudança de versão de regra são tratados como bootstrap e não contêm delta de push. O banco reserva um `dispatched_through_version` agregado por audiência.

## Limite explícito

Não existe dispatcher, expansão para evento por tip/user, event ID estável, receiver, política de retry/batch ou avanço de cursor neste repo. Deltas S3 não significam entrega durável. Não se deve chamar `advance_dispatch_cursor` nem afirmar que ativação foi notificada. Nenhuma chamada FCM é feita.

## Próxima decisão necessária

Antes de implementar entrega, obter contrato autorizado do Spring para ingestão batch de `TIP_ACTIVATED`/`TIP_RESOLVED`, dedupe durável por `event_id`, semântica OR agregada quando uma dica tem múltiplas audiências, limites de lote e confirmação após commit. O dispatcher deve percorrer versões sequenciais desde cursor, ler/validar manifests e bitmaps antigos/novos, comparar membership agregado por tip e somente então enviar o lote. Avançar cursor depois de confirmação durável; retries devem reproduzir os mesmos IDs de evento. FCM deve ocorrer after-commit/outbox no Spring. Até lá, flags e integração de envio permanecem ausentes/desligadas.

## Consequências

A publicação mantém a capacidade de investigar deltas, mas não completa a aceitação de replay/entrega do handoff. A lacuna é bloqueadora para notificação e precisa de teste cross-service; não é mascarada por sink mock ou avanço do cursor local.
