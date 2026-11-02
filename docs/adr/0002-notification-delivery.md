# ADR-0002: histórico de transições e entrega

- Status: implementado com ciclo prepare/stage/CAS/resume e entrega Spring batch.
- Data: 2026-10-19.

## Contexto

Eventos de ativação/resolução devem sobreviver a falhas HTTP, sem persistir IDs de membros em PostgreSQL nem chamar FCM diretamente do Engine. O Spring usa `FarmOwner.id` como principal `FARM_OWNER`, correspondente a `public.farm_owners.id` no Engine. Os contratos autorizados definem o endpoint batch, a chave interna e mensagens neutras.

## Decisão

O helper mantém baseline, membership agregado OR por dica e ciclo pendente exclusivamente no Redis. Só um bootstrap sem baseline silencía a população inicial; mudanças de assinatura ainda geram o delta real. Revogações por perda de vínculo com um proprietário são suprimidas. O dispatcher recria eventos com IDs estáveis, limita lotes a 500 e só confirma o ciclo após `total_created + total_skipped == total_received` em cada lote. `TIP_ACTIVATED` usa push e `TIP_RESOLVED` é in-app.

O scheduler e a CLI compartilham um ciclo em duas fases: os candidatos de snapshot e seus refs imutáveis são preparados e persistidos no Redis com memberships antes do primeiro CAS. Cada retry retoma os refs serializados; não reavalia candidatos após CAS parcial. O dispatcher só roda depois de confirmar todos os refs; os deltas são calculados contra o baseline e o baseline avança apenas após ack válido de todos os lotes.

## Consequências

O Redis guarda candidatos, membership, baseline e outbox; reset apaga todo esse estado e snapshots, risco aceito que força novo bootstrap silencioso. Nenhum ID de membro é escrito no PostgreSQL. Configure `SPRING_INTERNAL_SERVICE_KEY` apenas no ambiente aprovado; sua ausência mantém o ciclo candidato e outbox pendentes sem confirmar baseline.
