# Demo local

A demo usa Postgres, MongoDB e Redis em containers locais; não acessa AWS, Firebase ou banco externo. Mongo é inicializado com a regra fixture `high_water_consumption`, que é didática e não deve ser usada em produção. Redis local grava RDB snapshots após ao menos uma alteração em 60 segundos, mantém AOF habilitado e persiste no volume Docker. Os dados são sintéticos e não são backup de produção.

## Inicialização e bootstrap

```bash
make up
make demo-dry-run
make demo
```

Repita `make demo`: sem dado novo, não deve produzir nova versão nem `added`.

A fixture tem três owners: IDs 1 e 2 pertencem à fazenda elegível; ID 3 pertence à fazenda controle. Para chamar o endpoint, o `database_id` assinado no token deve ser igual ao `user_id` consultado. Não há Keycloak local nem token de desenvolvimento fixo; a suíte `pytest tests/test_api.py` testa autenticação com chaves de teste e JWKS simulado. Token de produção só serve se também tiver a audience `ms-ouros-audience-engine` e corresponder à identidade da fixture.

## Entrada e resolução

Execute em sequência, depois do bootstrap:

```bash
make demo-activate
make demo-activate
make demo-resolve
```

A primeira chamada grava uma medição de setembro na fazenda controle e publica um snapshot em que o user 3 entra; a repetição não acrescenta linha de origem nem gera nova versão. A resolução adiciona outubro e usa um relógio QA explícito em novembro; a fazenda controle fica abaixo do limite de resolução e o user 3 sai do membership. O delta não é persistido nem entregue como notificação.

## Falhas testáveis

- Sem bearer, endpoint retorna `401`.
- Se um snapshot Redis estiver ausente, a resolução o reconstrói da base de origem sob advisory lock; se Redis ou PostgreSQL estiver indisponível, falha fechada com `503`.
- Desligar Redis torna a API não pronta e impede membership; não há fallback S3.

Os snapshots locais ficam no volume Redis; não há console web. Consulte as referências SQL descritas no README. `docker compose down -v` apaga o stack e os dados persistentes para iniciar uma demo limpa.
