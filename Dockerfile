# Imagem do motor-consulta: CLI, servidor MCP e painel de acessos (um serviço
# por entrypoint no docker-compose.yml).
FROM python:3.12-slim

# Oracle Instant Client é OPCIONAL: o oracledb fala com a maioria dos servidores
# em modo thin, sem client library. Servidor Oracle antigo (verificador de senha
# 10g, erro DPY-3015) exige modo thick — construa com COM_ORACLE=1.
ARG COM_ORACLE=0
ARG IC_URL=https://download.oracle.com/otn_software/linux/instantclient/2113000/instantclient-basiclite-linux.x64-21.13.0.0.0dbru.zip

# libfbclient2: client library do Firebird, exigida pelo driver fdb.
# curl: o healthcheck do painel usa (a imagem slim não tem wget).
# Com COM_ORACLE=1: libaio + o Instant Client. O Instant Client procura
# libaio.so.1, e no Debian 13 o pacote entrega libaio.so.1t64 — daí o symlink.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfbclient2 curl \
    && if [ "$COM_ORACLE" = "1" ]; then \
         apt-get install -y --no-install-recommends libaio1t64 unzip \
         && ln -sf "$(find / -name 'libaio.so.1t64' 2>/dev/null | head -1)" /usr/lib/libaio.so.1 \
         && mkdir -p /opt/oracle \
         && curl -fsSL "$IC_URL" -o /tmp/ic.zip \
         && unzip -q /tmp/ic.zip -d /opt/oracle \
         && rm /tmp/ic.zip \
         && mv /opt/oracle/instantclient_* /opt/oracle/instantclient \
         && echo /opt/oracle/instantclient > /etc/ld.so.conf.d/oracle-instantclient.conf \
         && ldconfig; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# O conexao.py só entra em modo thick se este diretório existir de fato.
ENV ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Banco de exemplo (SQLite, dados fictícios): a imagem já sobe consultável.
RUN python exemplo/criar_banco.py

# A CLI é o ponto de entrada; os argumentos do "docker compose run" viram subcomandos.
ENTRYPOINT ["python", "consultar.py"]
CMD ["ping"]
