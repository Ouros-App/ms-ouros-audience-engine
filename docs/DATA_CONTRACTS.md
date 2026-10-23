# Contratos de dados

## Schema

`docker/postgres/001_source_schema.sql` reproduz um schema mínimo e sintético (`farms`, `farm_owners`, `water_registries`, `tips`) para Compose. Não é migration nem prova de compatibilidade com produção. O único schema consultado é explicitado em `SourcePostgresRepository`; nomes/tipos e vínculo usuário-fazenda precisam ser confirmados contra a versão vigente antes de integração.

A migration `migrations/001_audience_metadata.sql` cria apenas `audience_snapshot_refs` e `tip_audience_refs`. Não contém `user_id`, `farm_id`, membership ou dados binários. `tip_audience_refs.enabled` controla exposição; a definição Mongo habilitada e o vínculo SQL habilitado são ambos necessários.

## Definições de audiência no MongoDB

Cada documento de `ouros_audience.audience_definitions` representa um `WaterAudienceConfig`; `_id` é gerado pelo Mongo e ignorado pelo loader. `key` é único. O restante do documento segue os campos e limites tipados no modelo Pydantic: `version`, `enabled`, `subject`, `member_id_space`, `source`, `metric`, `period`, `baseline`, `activation_pct`, `resolution_pct`, `min_baseline_samples`, `max_observation_age_days`, `unknown_policy` e `first_run_push`.

O loader rejeita documentos inválidos ou com campos extras; falha de validação impede a inicialização em vez de aplicar defaults silenciosos. Mongo guarda apenas definições, nunca IDs de usuários ou resultados do filtro. A API carrega definições ao iniciar; alterações por CRUD administrativo atualizam somente o processo que atende à requisição. Outras instâncias/processos precisam reiniciar para refletir a mudança. Alterações diretas no Mongo exigem reiniciar todos os processos da API. `run-once` carrega definições frescas ao iniciar cada execução.

## Regra QA `high_water_consumption`

- Sujeito: fazenda; membros: IDs de `farm_owners` atualmente vinculados.
- Período: último mês completo antes de `evaluated_at`.
- Consumo do mês: soma não negativa de `end_hydrometer - start_hydrometer` para as linhas do mês.
- Baseline: mediana dos três meses imediatamente anteriores e consecutivos.
- Ativa uma fazenda nova quando excede baseline em pelo menos 25%; resolve fazenda já ativa quando fica abaixo de 15% acima do baseline. Entre limites, estado permanece ativo conforme histerese da implementação.
- Leitura mais antiga que 45 dias, ausência do último mês completo, baseline insuficiente/não consecutivo ou dados inválidos resulta UNKNOWN; membership anterior é preservado durante publicação normal. Cada fazenda é avaliada pelo próprio último período completo; watermark de outra fazenda não bloqueia medição atrasada. Repetir o mesmo mês reavalia a regra determinística. UNKNOWN nunca é RESOLVED.
- Primeira publicação e rebuild após reset do Redis gravam somente a composição, sem histórico de transição. No rebuild, o bitmap anterior não existe; farms UNKNOWN podem deixar de aparecer até haver dados válidos.

Esta regra é didática e não aprovada por negócio/agronomia. O schema fixture só tem datas de cadastro e leituras inicial/final; não prova duração/completude de medições. Não use esse cálculo sobre dados reais sem contrato de medição validado.

## Bitmap/manifests

- Codec: `pyroaring.BitMap`, `pyroaring-portable`, versão de formato `1`; IDs positivos até `2^31-1`.
- SHA-256 é calculado sobre bytes serializados exatos. O reader valida manifest, digest, formato e bitmap antes de verificar membership.
- Manifest de snapshot registra ID-space, versão da regra, watermark, quantidade e chaves do bitmap. Não há histórico de transições persistido.
- Eventos Spring não são materializados nem enviados neste repo; nenhum manifest contém transições.
- Nenhum manifest contém nomes, email, tokens ou texto de tip.

## API de resolução

`POST /internal/v1/tips/resolve` exige um access token Keycloak RS256 com `iss`, `exp`, `iat`, `sub` e audience `ms-ouros-audience-engine`; o token precisa conter `account_type=farm_owner`, role `farm_owner`, `database_id` e `farm_id` positivos. O `user_id` do JSON deve corresponder ao `database_id` assinado. O restante do JSON é estrito: `{user_id, user_role: "FARM_OWNER", request_id}`. IDs inválidos, papéis diferentes e campos extras são rejeitados. Erro de assinatura/JWKS é `401`/`503`; identidade sem permissão é `403`.

Os textos de dicas pertencem à integração Spring documentada em [SPRING_INTEGRATION.md](SPRING_INTEGRATION.md); a API valida independentemente o access token Keycloak.

## Endpoints administrativos

As rotas `/internal/v1/admin/...` exigem access token Keycloak válido com role `admin`. O CRUD em `/admin/audiences` cria, busca, lista, edita e exclui definições Mongo; `/admin/tips` busca dicas, e as rotas `/admin/audiences/{key}/tips/{tip_id}` gerenciam vínculos PostgreSQL. A resolução de tips recebe somente a identidade do usuário, nunca uma definição de audiência enviada pelo cliente.

Após POST/PUT/DELETE de definição, o processo API que atendeu a chamada recarrega sua configuração. Em deployments com múltiplos workers ou réplicas, os demais processos permanecem com a configuração anterior até reiniciarem. Alterações feitas diretamente no Mongo não atualizam nenhum processo em execução. Cada execução `run-once` lê a configuração atual do Mongo.
