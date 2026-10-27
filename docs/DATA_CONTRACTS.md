# Contratos de dados

## Schema

`docker/postgres/001_source_schema.sql` reproduz um schema mínimo e sintético (`farms`, `farm_owners`, `water_registries`, `tips`) para Compose. Não é migration nem prova de compatibilidade com produção. O único schema consultado é explicitado em `SourcePostgresRepository`; nomes/tipos e vínculo usuário-fazenda precisam ser confirmados contra a versão vigente antes de integração.

`migrations/001_audience_metadata.sql` cria `audience_snapshot_refs` e `tip_audience_refs`; `migrations/003_tip_observations.sql` cria `farm_tip_observations`, uma única medição completa por `(farm_id, tip_id)`, sem IDs de membros. `tip_audience_refs.enabled` controla exposição; a definição Mongo e o vínculo SQL precisam estar habilitados.

## Definições de audiência no MongoDB

Cada documento de `ouros_audience.audience_definitions` representa um `WaterAudienceConfig` ou `TipAudienceConfig`; `_id` é gerado pelo Mongo e ignorado pelo loader. `key` é único. Regras tipadas definem as condições e nunca armazenam usuários ou resultados do filtro.

O loader rejeita documentos inválidos ou com campos extras; falha de validação impede a inicialização em vez de aplicar defaults silenciosos. Mongo guarda apenas definições, nunca IDs de usuários ou resultados do filtro. A API carrega definições ao iniciar e o scheduler recarrega o Mongo a cada ciclo de 30 minutos. CRUD administrativo atualiza o processo que atende à chamada sob o mesmo lock da publicação; os demais processos e alterações diretas no Mongo são refletidos no ciclo seguinte, sem reinicialização. `run-once` carrega definições frescas ao iniciar cada execução.

## Regras das 32 dicas e observações POC

`default_tip_audiences()` contém 32 regras candidatas associadas aos IDs atuais de `public.tips`. `seed-tip-audiences` cria regras e vínculos dessas dicas desabilitados e, separadamente, cria/habilita `daily_hydrometer_audit` para a dica 121. É idempotente, reconcilia o estado `enabled` dos vínculos e recusa sobrescrever definições diferentes ou vínculos de dicas diferentes; não publica snapshots.

Um `TipAudienceConfig` tem `tip_id`, janela de freshness e `any_of`: cada ramo é AND de comparações; os ramos são OR. Comparações numéricas e booleanas são finitas e estritamente tipadas. Os campos definidos pela regra são o contrato exato de `signals`.

Para registrar uma medição, um administrador Keycloak envia:

```http
PUT /internal/v1/admin/farms/{farm_id}/tips/{tip_id}/observation
Content-Type: application/json

{
  "observed_at": "2026-10-27T12:00:00Z",
  "signals": {
    "nipple_setup_out_of_age_target": false,
    "nipple_flow_ml_min": 75
  }
}
```

`signals` precisa conter exatamente todos os campos da regra, com tipos compatíveis. O PUT substitui o snapshot atual da combinação fazenda/dica; não há histórico de medições. O publisher calcula membership por fazenda e o expande aos `farm_owners`. Medição ausente, futura, incompleta ou expirada resulta UNKNOWN e preserva membership anterior; no bootstrap sem nenhuma fazenda válida, não publica bitmap vazio.

Os dados de produção consultados atualmente não contêm a maioria das medições operacionais das 32 dicas. Alguns sinais são flags derivadas (por exemplo, comparação de custos ou adequação por idade) porque o guia não fornece todos os limiares numéricos. O endpoint apenas recebe valores; não inventa sensores nem calcula essas flags. Até um coletor ou operador admin enviar medições válidas, essas audiências permanecem sem membership calculável. As regras e limiares são POC e precisam de validação agronômica.

## Regra derivada: auditoria diária do hidrômetro

`daily_hydrometer_audit` usa `public.water_registries.registration_date`, sem inventar sinais: uma fazenda entra se não tiver leitura ou se a mais recente tiver pelo menos 24 horas; leitura mais nova resolve a condição. Uma data futura ou fazenda ausente na consulta resulta UNKNOWN. Membros são os `farm_owners` da fazenda; o vínculo SQL habilitado é somente com `tips.id = 121`.

`registration_date` não tem fuso no schema; o engine interpreta o valor como UTC, seguindo o acesso existente à tabela. O watermark do snapshot é o horário UTC da consulta de origem, e sua validade para a API é de um dia. Esta regra cobre a auditoria diária do hidrômetro, não comprova fluxo estático noturno, pH ou qualidade da água.


## Regra QA `high_water_consumption`

- Sujeito: fazenda; membros: IDs de `farm_owners` atualmente vinculados.
- Período: último mês completo antes de `evaluated_at`.
- Consumo do mês: soma não negativa de `end_hydrometer - start_hydrometer` para as linhas do mês.
- Baseline: mediana dos três meses imediatamente anteriores e consecutivos.
- Ativa uma fazenda nova quando excede baseline em pelo menos 25%; resolve fazenda já ativa quando fica abaixo de 15% acima do baseline. Entre limites, estado permanece ativo conforme histerese da implementação.
- Leitura mais antiga que 45 dias, ausência do último mês completo, baseline insuficiente/não consecutivo ou dados inválidos resulta UNKNOWN; membership anterior é preservado somente para owners ainda vinculados, e proprietários removidos são revogados mesmo quando todos os dados são UNKNOWN. Se qualquer fazenda com owners tiver avaliação UNKNOWN, o watermark do snapshot fica NULL; caso contrário, é o mais antigo entre as avaliações válidas. Cada fazenda é avaliada pelo próprio último período completo; watermark de outra fazenda não bloqueia medição atrasada. Repetir o mesmo mês reavalia a regra determinística. UNKNOWN nunca é RESOLVED.
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

As rotas `/internal/v1/admin/...` exigem access token Keycloak válido com role `admin`. O CRUD em `/admin/audiences` cria, busca, lista, edita e exclui definições Mongo; `/admin/tips` busca dicas, `/admin/audiences/{key}/tips/{tip_id}` gerencia vínculos PostgreSQL, e `/admin/farms/{farm_id}/tips/{tip_id}/observation` substitui a medição atual de uma fazenda/dica. A resolução de tips recebe somente a identidade do usuário, nunca uma definição de audiência enviada pelo cliente.

Após POST/PUT/DELETE de definição, o processo API que atendeu a chamada recarrega sua configuração sob o lock compartilhado com o scheduler. Os outros processos e alterações diretas no Mongo aparecem no próximo ciclo periódico, sem reinício. Cada execução `run-once` também lê a configuração atual do Mongo.
