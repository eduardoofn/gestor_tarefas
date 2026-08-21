"""
db.py — camada de acesso ao PostgreSQL.

Por que um módulo separado?
---------------------------
O Streamlit reexecuta o script principal inteiro a cada interação, então uma
variável global em app.py seria recriada toda hora (e vazaria conexões).
Módulos importados, ao contrário, ficam no sys.modules e são carregados uma
única vez por processo — então o pool aqui embaixo nasce uma vez e vive
enquanto o app viver, sem depender de @st.cache_resource.

Isso também evita que o pool seja resolvido dezenas de vezes por página.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

ErroBanco = psycopg2.Error

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_iniciado = False


# --------------------------------------------------------------------------- #
# Configuração e pool
# --------------------------------------------------------------------------- #

def config_db() -> dict:
    """Lê a conexão de .streamlit/secrets.toml ou das variáveis PG*."""
    cfg = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "tarefas"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", ""),
    }
    try:
        import streamlit as st
        if "postgres" in st.secrets:
            s = st.secrets["postgres"]
            for chave in cfg:
                if chave in s:
                    cfg[chave] = s[chave]
            cfg["port"] = int(cfg["port"])
    except Exception:
        pass
    return cfg


# O servidor guarda TIMESTAMP sem fuso, então o que importa é o fuso da SESSÃO:
# é ele que decide em que horário o NOW() (e os DEFAULT NOW() do schema) cai ao
# ser gravado. Sem isto, um container em UTC — o caso do Streamlit Cloud — grava
# três horas à frente e o app vira o dia às 21h.
FUSO_BANCO = os.getenv("TZ_APP", "America/Fortaleza")

# Expressão que devolve o horário local mesmo com a sessão em UTC. É o que
# entra no lugar de NOW() em todo o schema: assim o DEFAULT de `criado_em` não
# depende de nenhuma configuração de sessão — que, atrás de um pooler em modo
# transação, não sobrevive de uma consulta para a outra.
AGORA_SQL = f"(NOW() AT TIME ZONE '{FUSO_BANCO}')"


def com_fuso(sql: str) -> str:
    """Troca NOW() pela expressão com fuso. Aplicada uma única vez, no start."""
    return sql.replace("NOW()", AGORA_SQL)


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Cria o pool. Tenta fixar o fuso pelo parâmetro de inicialização e, se o
    servidor recusar, segue sem ele.

    Por que o plano B: atrás de um pooler em modo transação (pgBouncer, e o
    Supavisor do Supabase na porta 6543) o parâmetro `options` pode ser
    rejeitado no handshake. O app não depende disso — os horários que ele
    grava já vêm com o fuso embutido, no Python e no `AT TIME ZONE` do
    schema. O `options` é só um reforço para quem abre o psql na mão.
    """
    global _pool
    if _pool is None:
        cfg = config_db()
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                1, 10, **cfg, options=f"-c timezone={FUSO_BANCO}")
        except psycopg2.Error:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, **cfg)
    return _pool


# Conexão recém-usada está viva — testar de novo custaria uma ida e volta ao
# servidor em CADA consulta, e com o banco em outra região isso dobra a
# latência da tela. Só quem passou deste tempo parada é conferida.
SEGUNDOS_SEM_CHECAR = 60
_visto: dict[int, float] = {}


def _viva(conn) -> bool:
    """Postgres gerenciado derruba conexão ociosa e o pool devolve o cadáver
    assim mesmo — o sintoma é um erro na primeira interação da manhã."""
    if conn.closed:
        return False
    if time.monotonic() - _visto.get(id(conn), 0) < SEGUNDOS_SEM_CHECAR:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except psycopg2.Error:
        return False


def _conexao():
    """Tira uma conexão do pool, descartando as mortas. Duas tentativas: se a
    segunda também vier morta, o problema não é conexão ociosa e o erro deve
    subir para a tela."""
    pool = get_pool()
    for _ in range(2):
        conn = pool.getconn()
        if _viva(conn):
            conn.rollback()          # limpa transação abortada de um erro anterior
            _visto[id(conn)] = time.monotonic()
            return conn
        _visto.pop(id(conn), None)
        pool.putconn(conn, close=True)
    return pool.getconn()


@contextmanager
def cursor(commit: bool = False):
    pool = get_pool()
    conn = _conexao()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit() if commit else conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        pool.putconn(conn)


# --------------------------------------------------------------------------- #
# Operações
# --------------------------------------------------------------------------- #

def executar(sql: str, params: tuple = ()) -> None:
    with cursor(commit=True) as cur:
        cur.execute(sql, params)


def inserir(sql: str, params: tuple = ()) -> int:
    """O SQL precisa terminar com RETURNING id."""
    with cursor(commit=True) as cur:
        cur.execute(sql, params)
        return cur.fetchone()["id"]


def consultar(sql: str, params: tuple = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def consultar_um(sql: str, params: tuple = ()) -> dict | None:
    linhas = consultar(sql, params)
    return linhas[0] if linhas else None


def binario(dados: bytes):
    """Empacota bytes para a coluna BYTEA (anexos)."""
    return psycopg2.Binary(dados)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

DDL = [
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        id         SERIAL PRIMARY KEY,
        nome       TEXT        NOT NULL,
        usuario    TEXT        NOT NULL UNIQUE,
        email      TEXT,
        senha_hash TEXT        NOT NULL,
        salt       TEXT        NOT NULL,
        papel      TEXT        NOT NULL DEFAULT 'usuario'
                   CHECK (papel IN ('admin', 'usuario')),
        ativo      BOOLEAN     NOT NULL DEFAULT TRUE,
        criado_em  TIMESTAMP   NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tarefas (
        id             SERIAL PRIMARY KEY,
        titulo         TEXT      NOT NULL,
        descricao      TEXT,
        criador_id     INTEGER   NOT NULL REFERENCES usuarios(id),
        data_inicio    DATE      NOT NULL,
        prazo_original DATE      NOT NULL,
        prazo_atual    DATE      NOT NULL,
        status         TEXT      NOT NULL DEFAULT 'Iniciado'
                       CHECK (status IN ('Backlog', 'Iniciado', 'Em andamento', 'Realizado')),
        criado_em      TIMESTAMP NOT NULL DEFAULT NOW(),
        concluido_em   TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tarefa_responsaveis (
        tarefa_id  INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
        usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
        aberto_em  TIMESTAMP,
        PRIMARY KEY (tarefa_id, usuario_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prorrogacoes (
        id               SERIAL PRIMARY KEY,
        tarefa_id        INTEGER   NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
        solicitante_id   INTEGER   NOT NULL REFERENCES usuarios(id),
        prazo_anterior   DATE      NOT NULL,
        prazo_solicitado DATE      NOT NULL,
        justificativa    TEXT,
        situacao         TEXT      NOT NULL DEFAULT 'Pendente'
                         CHECK (situacao IN ('Pendente', 'Aprovada', 'Recusada')),
        decidido_por     INTEGER   REFERENCES usuarios(id),
        decidido_em      TIMESTAMP,
        criado_em        TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS historico (
        id         SERIAL PRIMARY KEY,
        tarefa_id  INTEGER   NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
        usuario_id INTEGER   REFERENCES usuarios(id),
        acao       TEXT      NOT NULL,
        detalhe    TEXT,
        criado_em  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_tarefas_prazo ON tarefas (prazo_atual)",
    "CREATE INDEX IF NOT EXISTS ix_tarefas_status ON tarefas (status)",
    "CREATE INDEX IF NOT EXISTS ix_resp_usuario ON tarefa_responsaveis (usuario_id)",
    "CREATE INDEX IF NOT EXISTS ix_hist_tarefa ON historico (tarefa_id)",
    """
    CREATE TABLE IF NOT EXISTS sessoes (
        token         TEXT      PRIMARY KEY,
        usuario_id    INTEGER   NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        criado_em     TIMESTAMP NOT NULL DEFAULT NOW(),
        ultimo_acesso TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_prorrog_situacao ON prorrogacoes (situacao)",
    "CREATE INDEX IF NOT EXISTS ix_sessoes_usuario ON sessoes (usuario_id)",
    """
    CREATE TABLE IF NOT EXISTS anexos (
        id         SERIAL PRIMARY KEY,
        tarefa_id  INTEGER   NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
        usuario_id INTEGER   REFERENCES usuarios(id),
        nome       TEXT      NOT NULL,
        tipo       TEXT,
        tamanho    INTEGER   NOT NULL,
        conteudo   BYTEA     NOT NULL,
        criado_em  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notificacoes (
        id         SERIAL PRIMARY KEY,
        usuario_id INTEGER   NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        tarefa_id  INTEGER   REFERENCES tarefas(id) ON DELETE CASCADE,
        tipo       TEXT      NOT NULL,
        texto      TEXT      NOT NULL,
        lida       BOOLEAN   NOT NULL DEFAULT FALSE,
        criado_em  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_anexos_tarefa ON anexos (tarefa_id)",
    "CREATE INDEX IF NOT EXISTS ix_notif_usuario ON notificacoes (usuario_id, lida, id)",
    "CREATE INDEX IF NOT EXISTS ix_tarefas_criador ON tarefas (criador_id)",
    # Sustenta os contadores dos banners, que rodam a cada reexecução da tela.
    "CREATE INDEX IF NOT EXISTS ix_tarefas_aprovacao ON tarefas (status, aprovacao)",
]

# Migrações aplicadas a bancos que já existiam antes das colunas novas.
# ADD COLUMN IF NOT EXISTS é idempotente, então rodar toda vez não custa nada.
MIGRACOES = [
    # --- trava de conclusão: só fecha depois do aceite de quem criou ---------
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS conclusao_texto TEXT",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS conclusao_por INTEGER REFERENCES usuarios(id)",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS conclusao_em TIMESTAMP",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS aprovacao TEXT",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS aprovado_por INTEGER REFERENCES usuarios(id)",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS aprovado_em TIMESTAMP",
    "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS aprovacao_obs TEXT",

    # Tarefas que já estavam em Realizado antes da trava entram como aprovadas,
    # senão o quadro antigo apareceria inteiro "aguardando aprovação".
    """UPDATE tarefas SET aprovacao = 'Aprovada', aprovado_em = COALESCE(concluido_em, NOW())
        WHERE status = 'Realizado' AND aprovacao IS NULL""",
]


# Restrições que vivem na tabela `tarefas`. Elas NÃO entram no bloco de cima:
# um par DROP + ADD CONSTRAINT valida a regra contra a tabela inteira e segura
# um ACCESS EXCLUSIVE enquanto isso. Rodando a cada subida do app, isso trava a
# tabela toda vez — barato com 50 tarefas, um susto com 50 mil. Aqui a
# restrição só é criada quando realmente não existe.
RESTRICOES = {
    "tarefas_status_check":
        """ALTER TABLE tarefas ADD CONSTRAINT tarefas_status_check
           CHECK (status IN ('Backlog', 'Iniciado', 'Em andamento', 'Realizado'))""",
    "tarefas_aprovacao_check":
        """ALTER TABLE tarefas ADD CONSTRAINT tarefas_aprovacao_check
           CHECK (aprovacao IS NULL OR aprovacao IN ('Pendente', 'Aprovada', 'Recusada'))""",
}

# Faxina de dados que só crescem. Roda uma vez por processo, não por clique.
HORAS_SESSAO = 8
DIAS_NOTIFICACAO = 30

LIMPEZA = [
    f"""DELETE FROM sessoes
         WHERE ultimo_acesso < {AGORA_SQL} - INTERVAL '{HORAS_SESSAO} hours'""",
    f"""DELETE FROM notificacoes
         WHERE lida AND criado_em < {AGORA_SQL} - INTERVAL '{DIAS_NOTIFICACAO} days'""",
]

# Bancos criados antes desta versão têm DEFAULT NOW() (UTC no servidor da
# Supabase). Reapontar o DEFAULT é instantâneo: não reescreve linha nenhuma.
COLUNAS_COM_DEFAULT = [
    ("usuarios", "criado_em"), ("tarefas", "criado_em"),
    ("prorrogacoes", "criado_em"), ("historico", "criado_em"),
    ("sessoes", "criado_em"), ("sessoes", "ultimo_acesso"),
    ("anexos", "criado_em"), ("notificacoes", "criado_em"),
]


def restricao_existe(nome: str) -> bool:
    return consultar_um("SELECT 1 AS x FROM pg_constraint WHERE conname = %s",
                        (nome,)) is not None


def init_db() -> None:
    """Cria o schema uma única vez por processo."""
    global _iniciado
    if _iniciado:
        return
    for ddl in DDL:
        executar(com_fuso(ddl))
    for mig in MIGRACOES:
        executar(com_fuso(mig))
    for tabela, coluna in COLUNAS_COM_DEFAULT:
        executar(f"ALTER TABLE {tabela} ALTER COLUMN {coluna} "
                 f"SET DEFAULT {AGORA_SQL}")
    for nome, sql in RESTRICOES.items():
        if not restricao_existe(nome):
            executar(sql)
    for sql in LIMPEZA:
        executar(sql)
    _iniciado = True


def banco_vazio() -> bool:
    """True quando ainda não existe nenhum usuário cadastrado."""
    return consultar_um("SELECT id FROM usuarios LIMIT 1") is None