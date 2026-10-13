# Implantação do servidor compartilhado

Guia para subir o servidor MCP numa máquina da rede interna, com **uma base de
conhecimento comum a todos** e acesso individual por pessoa.

```
Claude Code / Claude Desktop (cada pessoa)
   │  HTTP + Authorization: Bearer <token da pessoa>
   ▼
proxy Caddy  :8011                    caddy/tokens.caddy (fora do git, chmod 600)
   │  request_header X-Proxy-Segredo       prova de que passou por AQUI
   │  um bloco `handle` por pessoa:
   │    header_up X-Db-Pessoa  "maria_silva"
   │    header_up X-Db-Perfis  "vendas,estoque"     (SET: o cliente não forja)
   │    header_up X-Db-Expira  "2026-12-31"
   ▼
mcp_server   127.0.0.1:8010 → 8000    FastMCP, streamable-http, sem sessão
   │  gate por CHAMADA → só SELECT/WITH → guardrail → banco
   ▼
auditoria/   quem consultou o quê (inclusive recusas)
perguntas/   o que perguntaram e o que a base entregou (fila de lacunas)

painel  127.0.0.1:8012                cria/rotaciona/renova/revoga tokens e recarrega o
                                      Caddy pelo socket de admin (sem derrubar ninguém)
```

## Pré-requisitos

- Linux com Docker e Docker Compose v2.24+ (o compose usa `env_file` opcional).
- Rota de rede da máquina até os bancos.
- Um **usuário só-leitura** em cada banco.

## 1. Configuração

```bash
cp .env.example .env
cp .env.admin.example .env.admin
cp caddy/tokens.caddy.example caddy/tokens.caddy
chmod 600 .env .env.admin caddy/tokens.caddy
```

No `.env`:

- os perfis (`PERFIS`, `db_<perfil>_*`) — ver a seção de modelos do arquivo;
- `MCP_PROXY_SEGREDO` — **obrigatório**: `openssl rand -hex 32`. Vazio, o servidor
  MCP nega toda requisição HTTP (403), de propósito;
- `MCP_ENDPOINT` — o endereço que as pessoas vão usar
  (`http://servidor.interno:8011/mcp`); só aparece nos comandos prontos do painel;
- `REDE_SUBNET` — troque se `172.30.99.0/24` colidir com alguma rede sua.

No `.env.admin`: `ADMIN_SENHA` (`openssl rand -hex 24`). Sem ela o painel funciona
só em `127.0.0.1` e não pede login.

O `caddy/tokens.caddy` copiado do exemplo não dá acesso a ninguém (o bloco de
exemplo vem comentado). Ele precisa **existir** antes de subir o proxy: bind-mount
de arquivo que não existe vira diretório.

## 2. Construir e testar antes de expor

```bash
docker compose build                       # COM_ORACLE=1 no .env para Oracle antigo
docker compose run --rm cli perfis
docker compose run --rm cli --perfil vendas ping
docker compose run --rm --entrypoint python cli check.py
docker compose run --rm --entrypoint python cli recuperar.py "faturamento do mês"
```

Dicionário de um perfil novo (escreve em `contexto/`, então monta o fonte):

```bash
docker compose run --rm -v "$PWD:/app" --user "$(id -u):$(id -g)" \
    --entrypoint python cli gerar_dicionario.py --perfil vendas
```

## 3. Subir

```bash
docker compose up -d mcp proxy admin
docker compose logs mcp | head          # "gate do proxy ATIVO" e o número de notas
```

Por padrão tudo publica só em `127.0.0.1`. Para abrir **o proxy** à rede interna,
`MCP_PROXY_BIND=0.0.0.0` no `.env` e `docker compose up -d proxy`. O servidor MCP
cru (porta 8010) nunca deve ser publicado fora do loopback.

O token trafega em HTTP puro na rede interna. Fora dela — ou se a rede não é de
confiança — ponha TLS: troque o `:8011` do `caddy/Caddyfile` por um nome de host e
remova o `auto_https off`, ou coloque um proxy TLS na frente.

## 4. Dar acesso a uma pessoa

Painel → **+ novo acesso** → nome e bancos → **Gerar token**. A tela mostra o token
uma vez, com o comando do Claude Code pronto:

```bash
claude mcp add --transport http motor-consulta http://servidor.interno:8011/mcp \
    --scope user --header "Authorization: Bearer <token>"
```

Para o Claude Desktop, a página **Como conectar** traz o bloco do
`claude_desktop_config.json` (via `mcp-remote`, que exige Node na máquina).

| Ação no painel | Efeito |
|---|---|
| bancos liberados | vale na chamada seguinte da pessoa, sem reconectar |
| renovar | estende o prazo (`TOKEN_PRAZO_DIAS`, 90) **sem** trocar o token |
| rotacionar | token novo; o antigo morre na hora; renova o prazo |
| testar | handshake MCP real pelo proxy com o token da pessoa |
| revogar | pede o rótulo digitado; o token para de valer na hora |

Toda escrita no arquivo de tokens é transacional: grava, recarrega o Caddy e, se
ele recusar a configuração, volta o conteúdo anterior. Ficam os 10 últimos backups
(`caddy/tokens.caddy.bak-*`) e o rastro de cada alteração em `auditoria-tokens/`.

## 5. Operação

**Atualizar a base de conhecimento** (nota nova, dicionário novo):
`git pull && docker compose restart mcp` — `NOTES/` e `contexto/` entram por volume,
mas o servidor monta o índice no boot.

**Atualizar o código:** `git pull && docker compose up -d --build mcp admin` — o
código é assado na imagem.

**Relatório de lacunas** (tela `/dossie` do painel): agende o `gerar_dossie.sh`,
por exemplo toda segunda às 6h30 no cron do host:

```cron
30 6 * * 1  cd /caminho/motor-consulta && ./gerar_dossie.sh
```

**Painel sempre de pé:** o serviço tem `restart: always` e healthcheck, mas o
Docker não reinicia container *unhealthy* nem ressuscita o que um `compose down`
removeu. Agende o `watchdog_admin.sh` a cada 2 minutos. Ele **não** reinicia o
painel em 503: nesse código o painel está no ar avisando que o proxy caiu.

**Retenção:** `auditoria/`, `perguntas/` e `dossie/` são um arquivo por dia ou por
relatório, com permissão 0600/0640; expurgo é `rm` dos antigos. Contêm SQL e
perguntas de negócio — trate como o `.env`.

## 6. Se algo não funciona

| Sintoma | Causa provável |
|---|---|
| 403 "acesso somente pelo proxy" | `MCP_PROXY_SEGREDO` diferente entre proxy e mcp: `docker compose up -d proxy mcp` |
| 401 no cliente | token errado, revogado ou colado com espaço/quebra de linha |
| "ACESSO VENCIDO" | prazo do acesso venceu: **renovar** no painel |
| "ACESSO NEGADO ao banco" | o banco não está liberado para a pessoa |
| painel não sobe com `ADMIN_BIND=0.0.0.0` | falta `ADMIN_SENHA` no `.env.admin` (é proposital) |
| painel sem bancos para liberar | `PERFIS` não está no `.env` da pasta do compose |
| `SSL is required but the server doesn't support it` | `db_<perfil>_ssl=1` num MySQL sem TLS |
| `DPY-3015` no Oracle | servidor exige modo thick: `COM_ORACLE=1` e `docker compose build` |
| nota nova não aparece | faltou `docker compose restart mcp` |
