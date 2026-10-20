# Demo local

A demo usa somente Postgres, Redis e MinIO de containers locais; não acessa AWS, Firebase ou banco externo. As medições/fazendas/produtores e dicas são inventados. `config/audiences.demo.yaml` habilita a audiência de fixture e não deve ser promovido para produção.

## Inicialização e bootstrap

```bash
make up
make demo-dry-run
make demo
```

A primeira publicação deve mostrar versão `1`, dois membros, zero eventos de bootstrap e watermark de setembro de 2026. A fixture tem três owners: IDs 1 e 2 pertencem à fazenda elegível; ID 3 pertence à fazenda controle. O endpoint autenticado deve devolver `tip_ids: [1]` para user 1/2, e `tip_ids: []` para user 3. A dica 2 permanece desabilitada no vínculo.

```bash
curl -fsS http://localhost:8000/internal/v1/tips/resolve \
  -H 'Authorization: Bearer local-only-change-me' -H 'Content-Type: application/json' \
  -d '{"user_id":1,"user_role":"FARM_OWNER","request_id":"d97a8b61-9c9a-460e-a676-86ab2fa0ae53"}'
curl -fsS http://localhost:8000/internal/v1/tips/resolve \
  -H 'Authorization: Bearer local-only-change-me' -H 'Content-Type: application/json' \
  -d '{"user_id":3,"user_role":"FARM_OWNER","request_id":"57df2bb6-1ee5-4d89-b64c-a947cf659191"}'
```

Repita `make demo`: sem dado novo, não deve produzir nova versão nem `added`.

## Entrada e resolução

Execute em sequência, depois do bootstrap:

```bash
make demo-activate
make demo-activate
make demo-resolve
```

A primeira chamada grava uma medição de setembro na fazenda controle e publica uma transição com user 3 em `added`; a repetição não acrescenta linha de origem e não gera nova ativação. Resolução adiciona outubro e usa um relógio QA explícito em novembro; a fazenda controle tem valor abaixo do limite de resolução e user 3 sai em transição `resolved`. Reconsulte endpoint após cada etapa.

A demo gera manifests e bitmap de transição; não existe consumidor que envie eventos Spring e o cursor não avança. “Ativação” nesta demonstração significa delta persistido no S3, não notificação entregue.

## Falhas testáveis

- Sem bearer, endpoint retorna `401`.
- Se S3/objeto ou checksum não estiver disponível e o Redis não tiver cópia, resolução falha fechada com `503`.
- Desligar Redis não deve comprometer a leitura quando S3 está saudável; conferir se o reader consegue efetuar fallback.

Para conferir objetos/versionamento, use o Console MinIO em `http://localhost:9001` (`minioadmin` / `minioadmin`) e consulta SQL somente às referências descrita no README. Use `docker compose down -v` para apagar o stack e iniciar uma demo limpa.
