"""
Gestor de Tarefas — Kanban + Calendário (PostgreSQL)
====================================================

Plano de ação rápido:
  - O quê (título) / Quem (@responsáveis) / Quando (início e prazo) / Descrição
  - Kanban, Calendário e Lista
  - Status: Iniciado, Em andamento, Realizado
  - Atraso automático pelo prazo + alertas
  - Registro de abertura por responsável
  - Prorrogação preservando o prazo programado original
  - Admin vê tudo e gerencia usuários; usuário vê só o que criou ou onde foi marcado

Execução:
    pip install -r requirements.txt
    streamlit run app.py

Login inicial: admin / admin123
"""

from __future__ import annotations

import calendar as calmod
import hashlib
import inspect
import os
import secrets
from datetime import date, datetime, timedelta

import streamlit as st

from db import (ErroBanco, config_db, consultar, consultar_um,
                executar, init_db, inserir)

import streamlit.components.v1 as components

# Componente próprio: arrastar entre colunas + duplo clique para abrir.
_PASTA_QUADRO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quadro")
_quadro_componente = components.declare_component("quadro_tarefas", path=_PASTA_QUADRO)


def quadro_kanban(colunas: list[dict], tema: dict, key: str = "quadro"):
    """Devolve {"acao": "mover"|"abrir", "id": int, ...} ou None."""
    return _quadro_componente(colunas=colunas, tema=tema, default=None, key=key)

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

STATUS_LIST = ["Backlog", "Iniciado", "Em andamento", "Realizado"]

# Paleta derivada da logo Avanseg (verdes) — status distinguíveis entre si.
MARCA_ESCURO = "#00833F"
MARCA = "#00A651"
MARCA_CLARO = "#8CC63F"

CORES = {
    "Backlog": "#8a94a6",
    "Iniciado": "#8CC63F",
    "Em andamento": "#E8A33D",
    "Realizado": "#00A651",
    "Atrasado": "#E5484D",
}

TEMAS = {
    "escuro": {"fundo": "#101419", "painel": "#191f26", "item": "#212932",
               "item2": "#2a333e", "linha": "#2c3742", "ink": "#f2f6f4",
               "texto": "#c3cdd3", "muted": "#8b98a2", "sombra": "rgba(0,0,0,.35)"},
    "claro":  {"fundo": "#f4f7f5", "painel": "#ffffff", "item": "#ffffff",
               "item2": "#eef5ef", "linha": "#dde5e0", "ink": "#12211a",
               "texto": "#3d4a44", "muted": "#68786f", "sombra": "rgba(16,40,28,.08)"},
}

DIAS_SEMANA = ["DOM", "SEG", "TER", "QUA", "QUI", "SEX", "SÁB"]
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]


# --------------------------------------------------------------------------- #
# Compatibilidade entre versões do Streamlit
# --------------------------------------------------------------------------- #

def _versao_streamlit() -> tuple[int, int]:
    try:
        return tuple(int(p) for p in st.__version__.split(".")[:2])
    except Exception:
        return (0, 0)


# width="stretch" só existe a partir da 1.49. Antes disso `width` era em pixels,
# então usar a assinatura como critério não basta — o que vale é a versão.
_MODERNO = _versao_streamlit() >= (1, 49)


def _kw(fn, novo: dict, antigo: dict) -> dict:
    """Escolhe o argumento suportado pela versão instalada do Streamlit."""
    try:
        params = inspect.signature(fn).parameters
        preferido = novo if _MODERNO else antigo
        alternativa = antigo if _MODERNO else novo
        if not preferido or any(k in params for k in preferido):
            return preferido
        if any(k in params for k in alternativa):
            return alternativa
    except (TypeError, ValueError):
        pass
    return antigo


LARG_BTN = _kw(st.button, {"width": "stretch"}, {"use_container_width": True})
LARG_FSB = _kw(st.form_submit_button, {"width": "stretch"}, {"use_container_width": True})
LARG_DF = _kw(st.dataframe, {"width": "stretch"}, {"use_container_width": True})
FMT_DATA = _kw(st.date_input, {"format": "DD/MM/YYYY"}, {})


def caixa():
    try:
        return st.container(border=True)
    except TypeError:
        return st.container()


def rerun() -> None:
    (getattr(st, "rerun", None) or st.experimental_rerun)()


# --------------------------------------------------------------------------- #
# Estilo (tema claro, não depende de config.toml)
# --------------------------------------------------------------------------- #

def montar_css(tema: str) -> str:
    p = TEMAS[tema]
    return f"""
<style>
:root {{
    --ink: {p['ink']}; --texto: {p['texto']}; --muted: {p['muted']};
    --linha: {p['linha']}; --painel: {p['painel']}; --fundo: {p['fundo']};
    --item: {p['item']}; --marca: {MARCA}; --marca-clara: {MARCA_CLARO};
}}

.stApp {{ background: var(--fundo); }}

/* topo do Streamlit: sem barra, sem borda, sem sobra de espaço */
header[data-testid="stHeader"] {{ background: transparent; height: 0; min-height: 0;
                                  border: none; box-shadow: none; }}
div[data-testid="stDecoration"] {{ display: none; }}
div[data-testid="stToolbar"] {{ right: 8px; }}
.block-container {{ padding-top: 1.2rem !important; }}
div[data-testid="stExpander"] details {{ border-color: var(--linha) !important;
                                         background: var(--painel) !important; }}

/* legibilidade dos componentes nativos, principalmente no tema claro */
.stApp, .stApp .stMarkdown p, .stApp .stMarkdown li {{ color: var(--ink); }}
[data-testid="stWidgetLabel"] p, .stApp label p, .stApp label {{
    color: var(--texto) !important; font-weight: 600; }}
.stApp input, .stApp textarea {{ color: var(--ink) !important; }}
div[data-baseweb="input"], div[data-baseweb="textarea"], div[data-baseweb="select"] > div {{
    background: var(--item) !important; border-color: var(--linha) !important; }}
div[data-baseweb="popover"] div[data-baseweb="menu"] {{ background: var(--painel) !important; }}
.stApp [data-testid="stCaptionContainer"] p {{ color: var(--muted) !important; }}

/* ---------- cabeçalho ---------- */
.eyebrow {{ font-size: 11px; font-weight: 800; letter-spacing: 2.4px;
            text-transform: uppercase; color: var(--marca); margin-bottom: 2px; }}
.titulo-app {{ font-size: 27px; font-weight: 800; color: var(--ink);
               letter-spacing: -.6px; margin: 0 0 4px 0; }}
.quem-sou {{ font-size: 12.5px; color: var(--texto); text-align: right; line-height: 1.45; }}
.quem-sou b {{ color: var(--ink); font-size: 13.5px; }}
.progresso {{ font-size: 11px; font-weight: 800; letter-spacing: 1.2px;
              text-transform: uppercase; color: var(--muted);
              text-align: right; margin-bottom: 6px; }}
.barra {{ height: 6px; border-radius: 999px; background: var(--item); overflow: hidden; }}
.barra > i {{ display: block; height: 100%; background: var(--marca); border-radius: 999px; }}
.btn-sair button {{ padding: 2px 12px !important; font-size: 12px !important;
                    min-height: 0 !important; height: 30px; }}

/* ---------- cartão (Lista e modo sem arrastar) ---------- */
.card {{ background: var(--painel); border: 1px solid var(--linha);
         border-left: 3px solid var(--acc, var(--linha)); border-radius: 9px;
         padding: 9px 12px; margin-bottom: 4px; box-shadow: 0 1px 3px {p['sombra']}; }}
.card-titulo {{ font-size: 14px; font-weight: 700; line-height: 1.35;
                color: var(--ink); margin: 6px 0 4px 0; }}
.rodape {{ display: flex; justify-content: space-between; gap: 10px; margin-top: 8px;
           padding-top: 6px; border-top: 1px solid var(--linha);
           font-size: 11.5px; color: var(--texto); }}
.col-header {{ display: flex; align-items: center; gap: 9px; font-size: 12px;
               font-weight: 800; letter-spacing: 1px; text-transform: uppercase;
               color: var(--texto); background: var(--painel);
               border-top: 3px solid var(--acc, var(--linha));
               padding: 10px 13px; border-radius: 10px; margin-bottom: 10px; }}
.count {{ margin-left: auto; padding: 1px 9px; border-radius: 7px; font-weight: 700;
          background: var(--item); border: 1px solid var(--linha); color: var(--texto); }}

/* ---------- etiquetas ---------- */
.badge {{ display: inline-block; padding: 2px 9px; border-radius: 6px; font-size: 10px;
          font-weight: 800; letter-spacing: .6px; text-transform: uppercase; }}
.tag {{ display: inline-block; padding: 2px 9px; margin: 3px 5px 0 0; border-radius: 20px;
        font-size: 11px; font-weight: 600; color: var(--texto);
        background: var(--item); border: 1px solid var(--linha); }}
.dot {{ width: 9px; height: 9px; border-radius: 999px; display: inline-block; }}

/* ---------- geral ---------- */
.meta {{ font-size: 11.5px; color: var(--muted); margin: 2px 0; }}
.dia-num {{ font-size: 12px; font-weight: 800; color: var(--texto); }}
.dia-fora {{ opacity: .3; }}
.dia-hoje {{ background: var(--marca); color: #fff; border-radius: 7px; padding: 1px 7px; }}
.riscado {{ text-decoration: line-through; color: var(--muted); }}
.vazio {{ text-align: center; color: var(--muted); font-size: 12.5px; padding: 14px 0;
          border: 1px dashed var(--linha); border-radius: 9px; }}
</style>
"""


def badge(texto: str, cor: str) -> str:
    return (f"<span class='badge' style='color:{cor};background:{cor}1a;"
            f"border:1px solid {cor}33'>{texto}</span>")


def tags(nomes: str | None) -> str:
    if not nomes:
        return "<span class='tag'>sem responsável</span>"
    return "".join(f"<span class='tag'>@{n.strip()}</span>" for n in nomes.split(","))


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #

def hoje() -> date:
    return date.today()


# ----- parâmetros da URL (compatível com versões antigas) -------------------- #

def qp_ler(chave: str) -> str | None:
    try:
        return st.query_params.get(chave)
    except Exception:
        return (st.experimental_get_query_params().get(chave) or [None])[0]


def qp_gravar(chave: str, valor: str) -> None:
    try:
        st.query_params[chave] = valor
    except Exception:
        st.experimental_set_query_params(**{chave: valor})


def qp_limpar() -> None:
    try:
        st.query_params.clear()
    except Exception:
        st.experimental_set_query_params()


# ----- sessão que sobrevive ao F5 ------------------------------------------- #

HORAS_INATIVIDADE = 8


def abrir_sessao(usuario_id: int) -> str:
    token = secrets.token_urlsafe(24)
    executar("INSERT INTO sessoes (token, usuario_id) VALUES (%s, %s)", (token, usuario_id))
    return token


def sessao_valida(token: str) -> dict | None:
    """Devolve o usuário se o token existir e não estiver parado há muito tempo."""
    linha = consultar_um("""SELECT s.usuario_id, s.ultimo_acesso, u.*
                              FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id
                             WHERE s.token = %s AND u.ativo""", (token,))
    if not linha:
        return None
    if datetime.now() - linha["ultimo_acesso"] > timedelta(hours=HORAS_INATIVIDADE):
        executar("DELETE FROM sessoes WHERE token = %s", (token,))
        return None
    executar("UPDATE sessoes SET ultimo_acesso = NOW() WHERE token = %s", (token,))
    return linha


def encerrar_sessao(token: str | None) -> None:
    if token:
        executar("DELETE FROM sessoes WHERE token = %s", (token,))


def fmt_data(valor: date | None) -> str:
    return valor.strftime("%d/%m/%Y") if valor else "—"


def fmt_hora(valor: datetime | None) -> str:
    return valor.strftime("%d/%m/%Y %H:%M") if valor else "—"


def gerar_hash(senha: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), salt.encode(), 120_000).hex()


def eh_atrasada(t: dict) -> bool:
    return t["status"] != "Realizado" and t["prazo_atual"] < hoje()


def situacao_prazo(t: dict) -> tuple[str, str]:
    if t["status"] == "Realizado":
        return "Concluída", CORES["Realizado"]
    dias = (t["prazo_atual"] - hoje()).days
    if dias < 0:
        return f"Atrasada há {abs(dias)} dia(s)", CORES["Atrasado"]
    if dias == 0:
        return "Vence hoje", CORES["Em andamento"]
    if dias <= 2:
        return f"Vence em {dias} dia(s)", CORES["Em andamento"]
    return f"Faltam {dias} dias", "#64748b"


# --------------------------------------------------------------------------- #
# Regras de negócio
# --------------------------------------------------------------------------- #

def criar_usuario(nome: str, usuario: str, email: str, senha: str, papel: str) -> int:
    salt = secrets.token_hex(16)
    return inserir(
        """INSERT INTO usuarios (nome, usuario, email, senha_hash, salt, papel)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (nome.strip(), usuario.strip().lower(), email.strip(),
         gerar_hash(senha, salt), salt, papel))


def autenticar(usuario: str, senha: str) -> dict | None:
    u = consultar_um("SELECT * FROM usuarios WHERE usuario = %s AND ativo",
                     (usuario.strip().lower(),))
    if u and gerar_hash(senha, u["salt"]) == u["senha_hash"]:
        return u
    return None


def registrar(tarefa_id: int, usuario_id: int | None, acao: str, detalhe: str = "") -> None:
    executar("INSERT INTO historico (tarefa_id, usuario_id, acao, detalhe) "
             "VALUES (%s, %s, %s, %s)", (tarefa_id, usuario_id, acao, detalhe))


def nome_usuario(uid: int) -> str:
    u = consultar_um("SELECT nome FROM usuarios WHERE id = %s", (uid,))
    return u["nome"] if u else "?"


def usuarios_ativos() -> list[dict]:
    return consultar("SELECT id, nome FROM usuarios WHERE ativo ORDER BY nome")


def criar_tarefa(titulo, descricao, criador_id, data_inicio, prazo, responsaveis,
                 status: str = "Iniciado", concluido_em=None) -> int:
    if status == "Realizado" and concluido_em is None:
        concluido_em = datetime.now()
    tid = inserir(
        """INSERT INTO tarefas (titulo, descricao, criador_id, data_inicio,
                                prazo_original, prazo_atual, status, concluido_em)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (titulo.strip(), descricao.strip(), criador_id, data_inicio, prazo, prazo,
         status, concluido_em))
    for uid in responsaveis:
        executar("INSERT INTO tarefa_responsaveis (tarefa_id, usuario_id) VALUES (%s, %s)",
                 (tid, uid))
    nomes = ", ".join(nome_usuario(u) for u in responsaveis)
    detalhe = f"Responsáveis: {nomes} | Prazo: {fmt_data(prazo)} | Coluna: {status}"
    if concluido_em:
        detalhe += f" | Concluída em {fmt_hora(concluido_em)}"
    registrar(tid, criador_id, "Tarefa criada", detalhe)
    return tid


SQL_TAREFA_BASE = """
SELECT t.*,
       u.nome AS criador,
       (SELECT STRING_AGG(u2.nome, ', ' ORDER BY u2.nome)
          FROM tarefa_responsaveis tr JOIN usuarios u2 ON u2.id = tr.usuario_id
         WHERE tr.tarefa_id = t.id) AS responsaveis,
       (SELECT COUNT(*) FROM tarefa_responsaveis tr WHERE tr.tarefa_id = t.id) AS qtd_resp,
       (SELECT COUNT(*) FROM tarefa_responsaveis tr
         WHERE tr.tarefa_id = t.id AND tr.aberto_em IS NOT NULL) AS qtd_abertos,
       (SELECT COUNT(*) FROM prorrogacoes p
         WHERE p.tarefa_id = t.id AND p.situacao = 'Pendente') AS prorrog_pendentes
  FROM tarefas t
  JOIN usuarios u ON u.id = t.criador_id
"""


def listar_tarefas(user: dict) -> list[dict]:
    """Admin vê tudo; usuário vê o que criou ou onde foi marcado (@)."""
    if user["papel"] == "admin":
        return consultar(SQL_TAREFA_BASE + " ORDER BY t.prazo_atual, t.id")
    return consultar(SQL_TAREFA_BASE + """
        WHERE t.criador_id = %s
           OR EXISTS (SELECT 1 FROM tarefa_responsaveis tr
                       WHERE tr.tarefa_id = t.id AND tr.usuario_id = %s)
        ORDER BY t.prazo_atual, t.id""", (user["id"], user["id"]))


def obter_tarefa(tid: int, user: dict) -> dict | None:
    for t in listar_tarefas(user):
        if t["id"] == tid:
            return t
    return None


def responsaveis_ids(tid: int) -> list[int]:
    return [r["usuario_id"] for r in
            consultar("SELECT usuario_id FROM tarefa_responsaveis WHERE tarefa_id = %s", (tid,))]


def eh_responsavel(tid: int, uid: int) -> bool:
    return bool(consultar_um("SELECT 1 AS x FROM tarefa_responsaveis "
                             "WHERE tarefa_id = %s AND usuario_id = %s", (tid, uid)))


def pode_gerenciar(t: dict, user: dict) -> bool:
    return (user["papel"] == "admin" or t["criador_id"] == user["id"]
            or eh_responsavel(t["id"], user["id"]))


def pode_editar(t: dict, user: dict) -> bool:
    """Editar a tarefa em si e excluir: só criador ou admin."""
    return user["papel"] == "admin" or t["criador_id"] == user["id"]


def registrar_abertura(tid: int, user: dict) -> None:
    linha = consultar_um("SELECT aberto_em FROM tarefa_responsaveis "
                         "WHERE tarefa_id = %s AND usuario_id = %s", (tid, user["id"]))
    if linha and linha["aberto_em"] is None:
        executar("UPDATE tarefa_responsaveis SET aberto_em = NOW() "
                 "WHERE tarefa_id = %s AND usuario_id = %s", (tid, user["id"]))
        registrar(tid, user["id"], "Tarefa aberta", "Confirmou leitura da atividade")


def alterar_status(t: dict, novo: str, user: dict) -> None:
    if novo == t["status"]:
        return
    executar("UPDATE tarefas SET status = %s, concluido_em = %s WHERE id = %s",
             (novo, datetime.now() if novo == "Realizado" else None, t["id"]))
    registrar(t["id"], user["id"], "Status alterado", f"{t['status']} → {novo}")


def atualizar_tarefa(t: dict, titulo, descricao, inicio, novos_resp, user) -> None:
    mudancas = []
    if titulo.strip() != t["titulo"]:
        mudancas.append(f"Título: {t['titulo']} → {titulo.strip()}")
    if inicio != t["data_inicio"]:
        mudancas.append(f"Início: {fmt_data(t['data_inicio'])} → {fmt_data(inicio)}")
    executar("UPDATE tarefas SET titulo = %s, descricao = %s, data_inicio = %s WHERE id = %s",
             (titulo.strip(), descricao.strip(), inicio, t["id"]))

    atuais = set(responsaveis_ids(t["id"]))
    novos = set(novos_resp)
    for uid in novos - atuais:
        executar("INSERT INTO tarefa_responsaveis (tarefa_id, usuario_id) VALUES (%s, %s)",
                 (t["id"], uid))
        mudancas.append(f"Incluído @{nome_usuario(uid)}")
    for uid in atuais - novos:
        executar("DELETE FROM tarefa_responsaveis WHERE tarefa_id = %s AND usuario_id = %s",
                 (t["id"], uid))
        mudancas.append(f"Removido @{nome_usuario(uid)}")

    if mudancas or descricao.strip() != (t["descricao"] or ""):
        registrar(t["id"], user["id"], "Tarefa editada",
                  " | ".join(mudancas) or "Descrição atualizada")


def excluir_tarefa(tid: int) -> None:
    executar("DELETE FROM tarefas WHERE id = %s", (tid,))


def solicitar_prorrogacao(t: dict, novo_prazo: date, justificativa: str, user: dict) -> None:
    executar("""INSERT INTO prorrogacoes (tarefa_id, solicitante_id, prazo_anterior,
                                          prazo_solicitado, justificativa, situacao)
                VALUES (%s, %s, %s, %s, %s, 'Pendente')""",
             (t["id"], user["id"], t["prazo_atual"], novo_prazo, justificativa.strip()))
    registrar(t["id"], user["id"], "Prorrogação solicitada",
              f"{fmt_data(t['prazo_atual'])} → {fmt_data(novo_prazo)} | {justificativa.strip()}")


def decidir_prorrogacao(pedido: dict, aprovar: bool, user: dict) -> None:
    situacao = "Aprovada" if aprovar else "Recusada"
    executar("UPDATE prorrogacoes SET situacao = %s, decidido_por = %s, decidido_em = NOW() "
             "WHERE id = %s", (situacao, user["id"], pedido["id"]))
    if aprovar:
        # prazo_original NUNCA muda — fica o registro da data programada inicial.
        executar("UPDATE tarefas SET prazo_atual = %s WHERE id = %s",
                 (pedido["prazo_solicitado"], pedido["tarefa_id"]))
    registrar(pedido["tarefa_id"], user["id"], f"Prorrogação {situacao.lower()}",
              f"{fmt_data(pedido['prazo_anterior'])} → {fmt_data(pedido['prazo_solicitado'])}")


def pedidos_para_decidir(user: dict) -> list[dict]:
    todos = consultar("""
        SELECT p.*, t.titulo, t.criador_id, u.nome AS solicitante
          FROM prorrogacoes p
          JOIN tarefas t ON t.id = p.tarefa_id
          JOIN usuarios u ON u.id = p.solicitante_id
         WHERE p.situacao = 'Pendente' ORDER BY p.id""")
    if user["papel"] == "admin":
        return todos
    return [p for p in todos
            if p["criador_id"] == user["id"] and p["solicitante_id"] != user["id"]]


# --------------------------------------------------------------------------- #
# Componentes visuais
# --------------------------------------------------------------------------- #

def abrir_tarefa(tid: int, user: dict) -> None:
    registrar_abertura(tid, user)
    st.session_state.tarefa_sel = tid
    rerun()


def card_tarefa(t: dict, user: dict, chave: str) -> None:
    atrasada = eh_atrasada(t)
    rotulo, cor = situacao_prazo(t)
    acento = CORES["Atrasado"] if atrasada else CORES[t["status"]]
    selo = (badge("Atrasada", CORES["Atrasado"]) if atrasada
            else badge(t["status"], CORES[t["status"]]))
    if t["prorrog_pendentes"]:
        selo += " " + badge("Prorrogação", "#7c3aed")
    st.markdown(
        f"<div class='card' style='--acc:{acento}'>{selo}"
        f"<div class='card-titulo'>{t['titulo']}</div>"
        f"<div>{tags(t['responsaveis'])}</div>"
        f"<div class='rodape'>"
        f"<span>{fmt_data(t['data_inicio'])} → {fmt_data(t['prazo_atual'])}</span>"
        f"<span style='color:{cor};font-weight:800'>{rotulo}</span></div>"
        f"<div class='meta'>{t['qtd_abertos']} de {t['qtd_resp']} abriram</div></div>",
        unsafe_allow_html=True)
    if st.button("Abrir", key=f"abrir_{chave}_{t['id']}", **LARG_BTN):
        abrir_tarefa(t["id"], user)


def form_novo_card(status: str, user: dict) -> None:
    """Formulário compacto para criar a tarefa já dentro da coluna."""
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    with st.expander(f"+  Adicionar em {status}"):
        with st.form(f"add_{status}", clear_on_submit=True):
            titulo = st.text_input("Atividade", key=f"add_tit_{status}",
                                   placeholder="O quê precisa ser feito?")
            resp = st.multiselect("Responsáveis (@)", list(mapa), key=f"add_resp_{status}")
            prazo = st.date_input("Prazo", value=hoje() + timedelta(days=7),
                                  key=f"add_prazo_{status}", **FMT_DATA)
            if st.form_submit_button("Criar", type="primary", **LARG_FSB):
                if not titulo.strip():
                    st.error("Informe a atividade.")
                elif not resp:
                    st.error("Marque ao menos um responsável.")
                else:
                    criar_tarefa(titulo, "", user["id"], hoje(), prazo,
                                 [mapa[n] for n in resp], status)
                    rerun()


def cabecalho(user: dict, tarefas: list[dict]) -> None:
    esq, meio, dir_ = st.columns([3, 1.4, 0.7])
    with esq:
        st.markdown("<div class='eyebrow'>Plano de ação</div>"
                    "<div class='titulo-app'>Gestor de Tarefas</div>", unsafe_allow_html=True)
    with meio:
        papel = "Administrador" if user["papel"] == "admin" else "Usuário"
        st.markdown(f"<div class='quem-sou'><b>{user['nome']}</b><br>"
                    f"@{user['usuario']} · {papel}</div>", unsafe_allow_html=True)
    with dir_:
        st.markdown("<div class='btn-sair'>", unsafe_allow_html=True)
        if st.button("Sair", key="sair"):
            encerrar_sessao(st.session_state.get("token"))
            st.session_state.clear()
            qp_limpar()
            rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    total = len(tarefas)
    feitas = len([t for t in tarefas if t["status"] == "Realizado"])
    pct = int(feitas / total * 100) if total else 0
    pontos = " ".join(
        f"<span class='dot' style='background:{CORES[s]}'></span> "
        f"<span class='meta' style='display:inline;margin-right:16px'>"
        f"{len([t for t in tarefas if t['status'] == s])} {s.lower()}</span>"
        for s in STATUS_LIST)
    e2, d2 = st.columns([3, 1.2])
    e2.markdown(f"<div style='padding-top:4px'>{pontos}</div>", unsafe_allow_html=True)
    d2.markdown(f"<div class='progresso'>{feitas} de {total} concluídas</div>"
                f"<div class='barra'><i style='width:{pct}%'></i></div>", unsafe_allow_html=True)
    st.write("")


def alertas(tarefas: list[dict], user: dict) -> None:
    atrasadas = [t for t in tarefas if eh_atrasada(t)]
    minhas = [t for t in atrasadas if eh_responsavel(t["id"], user["id"])]
    vencem_hoje = [t for t in tarefas
                   if t["status"] != "Realizado" and t["prazo_atual"] == hoje()]
    if atrasadas:
        alvo = minhas or atrasadas
        titulos = ", ".join(t["titulo"] for t in alvo[:3])
        extra = f" (+{len(alvo) - 3})" if len(alvo) > 3 else ""
        st.error(f"**{len(atrasadas)} tarefa(s) em atraso.** {titulos}{extra}")
    if vencem_hoje:
        st.warning(f"**{len(vencem_hoje)} tarefa(s) vencem hoje.**")
    pendentes = pedidos_para_decidir(user)
    if pendentes:
        st.info(f"**{len(pendentes)} pedido(s) de prorrogação** aguardando sua decisão.")


# --------------------------------------------------------------------------- #
# Telas
# --------------------------------------------------------------------------- #

def tela_login() -> None:
    st.markdown("<br>", unsafe_allow_html=True)
    _, meio, _ = st.columns([1, 1.2, 1])
    with meio:
        st.markdown("<div class='eyebrow'>Plano de ação</div>"
                    "<div class='titulo-app'>Gestor de Tarefas</div>"
                    "<div class='meta'>Kanban e Calendário para acompanhar quem faz o quê, e até quando.</div><br>",
                    unsafe_allow_html=True)
        with st.form("login"):
            usuario = st.text_input("Usuário", key="log_user")
            senha = st.text_input("Senha", type="password", key="log_senha")
            if st.form_submit_button("Entrar", type="primary", **LARG_FSB):
                u = autenticar(usuario, senha)
                if u:
                    st.session_state.usuario = u
                    st.session_state.token = abrir_sessao(u["id"])
                    qp_gravar("s", st.session_state.token)
                    rerun()
                else:
                    st.error("Usuário ou senha inválidos.")
        st.info("Primeiro acesso: **admin / admin123**")


def montar_colunas(tarefas: list[dict]) -> list[dict]:
    colunas = []
    for status in STATUS_LIST:
        cards = []
        for t in [x for x in tarefas if x["status"] == status]:
            atrasada = eh_atrasada(t)
            situacao, cor_prazo = situacao_prazo(t)
            acento = CORES["Atrasado"] if atrasada else CORES[status]
            resp = t["responsaveis"] or "sem responsável"
            cards.append({
                "id": t["id"],
                "titulo": f"#{t['id']} &nbsp;{t['titulo']}",
                "selo": "Atrasada" if atrasada else status,
                "acento": acento,
                "pessoas": " ".join(f"@{n.strip()}" for n in resp.split(",")),
                "periodo": f"{fmt_data(t['data_inicio'])} → {fmt_data(t['prazo_atual'])}",
                "situacao": situacao,
                "cor_prazo": cor_prazo,
                "prorrogacao": bool(t["prorrog_pendentes"]),
            })
        colunas.append({"nome": status, "cor": CORES[status], "cards": cards})
    return colunas


def barra_adicionar(user: dict) -> None:
    """Um botão '+' discreto por coluna; os campos abrem ao clicar."""
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    for col, status in zip(st.columns(len(STATUS_LIST), gap="small"), STATUS_LIST):
        with col:
            abridor = (st.popover(f"＋  {status}", **LARG_BTN)
                       if hasattr(st, "popover") else st.expander(f"＋  {status}"))
            with abridor:
                with st.form(f"add_{status}", clear_on_submit=True):
                    titulo = st.text_input("Atividade", key=f"add_tit_{status}",
                                           placeholder="O quê precisa ser feito?")
                    resp = st.multiselect("Responsáveis (@)", list(mapa),
                                          key=f"add_resp_{status}")
                    prazo = st.date_input("Prazo", value=hoje() + timedelta(days=7),
                                          key=f"add_prazo_{status}", **FMT_DATA)
                    fim = None
                    if status == "Realizado":
                        fim = st.date_input("Data de conclusão", value=hoje(),
                                            key=f"add_fim_{status}", **FMT_DATA)
                    if st.form_submit_button("Criar", type="primary", **LARG_FSB):
                        if not titulo.strip():
                            st.error("Informe a atividade.")
                        elif not resp:
                            st.error("Marque ao menos um responsável.")
                        else:
                            criar_tarefa(titulo, "", user["id"], hoje(), prazo,
                                         [mapa[n] for n in resp], status,
                                         datetime.combine(fim, datetime.min.time()) if fim else None)
                            rerun()


def aba_kanban(tarefas: list[dict], user: dict) -> None:
    barra_adicionar(user)

    tema = TEMAS[st.session_state.tema]
    evento = quadro_kanban(montar_colunas(tarefas),
                           {**tema, "marca": MARCA}, key="quadro_kanban")

    if evento and evento.get("n") != st.session_state.get("ultimo_evento"):
        st.session_state.ultimo_evento = evento["n"]
        alvo = next((t for t in tarefas if t["id"] == evento["id"]), None)
        if alvo:
            if evento["acao"] == "mover" and evento["para"] != alvo["status"]:
                alterar_status(alvo, evento["para"], user)
                rerun()
            elif evento["acao"] == "abrir":
                abrir_tarefa(alvo["id"], user)


def aba_calendario(tarefas: list[dict], user: dict) -> None:
    st.session_state.setdefault("cal_ano", hoje().year)
    st.session_state.setdefault("cal_mes", hoje().month)

    nav = st.columns([1, 1, 5, 1])
    if nav[0].button("◀", key="cal_ant", **LARG_BTN):
        st.session_state.cal_mes -= 1
        if st.session_state.cal_mes == 0:
            st.session_state.cal_mes, st.session_state.cal_ano = 12, st.session_state.cal_ano - 1
        rerun()
    if nav[1].button("▶", key="cal_prox", **LARG_BTN):
        st.session_state.cal_mes += 1
        if st.session_state.cal_mes == 13:
            st.session_state.cal_mes, st.session_state.cal_ano = 1, st.session_state.cal_ano + 1
        rerun()
    ano, mes = st.session_state.cal_ano, st.session_state.cal_mes
    nav[2].markdown(f"<div style='font-size:19px;font-weight:700;padding-top:4px'>"
                    f"{MESES[mes - 1]} de {ano}</div>", unsafe_allow_html=True)
    if nav[3].button("Hoje", key="cal_hoje", **LARG_BTN):
        st.session_state.cal_ano, st.session_state.cal_mes = hoje().year, hoje().month
        rerun()

    st.caption("Cada tarefa aparece no dia do seu prazo.")

    por_dia: dict[date, list[dict]] = {}
    for t in tarefas:
        por_dia.setdefault(t["prazo_atual"], []).append(t)

    for col, dia in zip(st.columns(7), DIAS_SEMANA):
        col.markdown(f"<div class='meta' style='text-align:center;font-weight:700'>{dia}</div>",
                     unsafe_allow_html=True)

    for semana in calmod.Calendar(firstweekday=6).monthdatescalendar(ano, mes):
        for col, dia in zip(st.columns(7, gap="small"), semana):
            with col:
                with caixa():
                    classe = "dia-num" if dia.month == mes else "dia-num dia-fora"
                    numero = f"<span class='dia-hoje'>{dia.day}</span>" if dia == hoje() else str(dia.day)
                    st.markdown(f"<div class='{classe}'>{numero}</div>", unsafe_allow_html=True)
                    itens = por_dia.get(dia, [])
                    for t in itens[:3]:
                        cor = CORES["Atrasado"] if eh_atrasada(t) else CORES[t["status"]]
                        st.markdown(f"<span class='dot' style='background:{cor}'></span>",
                                    unsafe_allow_html=True)
                        rotulo = t["titulo"][:16] + ("…" if len(t["titulo"]) > 16 else "")
                        if st.button(rotulo, key=f"cal_{dia}_{t['id']}", **LARG_BTN):
                            abrir_tarefa(t["id"], user)
                    if len(itens) > 3:
                        st.markdown(f"<div class='meta'>+{len(itens) - 3} tarefa(s)</div>",
                                    unsafe_allow_html=True)


def aba_lista(tarefas: list[dict], user: dict) -> None:
    if not tarefas:
        st.info("Nenhuma tarefa encontrada com os filtros atuais.")
        return
    st.dataframe(
        [{"ID": t["id"], "Atividade": t["titulo"], "Responsáveis": t["responsaveis"] or "—",
          "Início": fmt_data(t["data_inicio"]), "Prazo": fmt_data(t["prazo_atual"]),
          "Prazo original": fmt_data(t["prazo_original"]),
          "Status": "Atrasada" if eh_atrasada(t) else t["status"],
          "Situação": situacao_prazo(t)[0],
          "Abriram": f"{t['qtd_abertos']}/{t['qtd_resp']}", "Criador": t["criador"]}
         for t in tarefas], hide_index=True, **LARG_DF)

    titulos = {t["id"]: t["titulo"] for t in tarefas}
    c1, c2 = st.columns([3, 1])
    escolha = c1.selectbox("Abrir tarefa", options=[0] + list(titulos), key="lista_sel",
                           format_func=lambda i: "Selecione…" if i == 0 else f"#{i} — {titulos[i]}")
    c2.markdown("<br>", unsafe_allow_html=True)
    if escolha and c2.button("Abrir", key="lista_abrir", type="primary", **LARG_BTN):
        abrir_tarefa(escolha, user)


def aba_nova_tarefa(user: dict) -> None:
    st.markdown("#### Nova tarefa")
    st.caption("O quê, quem e quando. Use o Backlog para o que ainda não começou.")
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    with st.form("nova_tarefa", clear_on_submit=True):
        titulo = st.text_input("O quê — atividade (título)", key="nt_titulo",
                               placeholder="Ex.: Revisar fechamento do inventário")
        descricao = st.text_area("Descrição — detalhes da atividade", height=110, key="nt_desc",
                                 placeholder="Contexto, entregável esperado, links…")
        marcados = st.multiselect("Quem — marque os envolvidos (@)", list(mapa), key="nt_resp",
                                  placeholder="Escolha uma ou mais pessoas")
        c1, c2, c3 = st.columns(3)
        inicio = c1.date_input("Data de início", value=hoje(), key="nt_ini", **FMT_DATA)
        prazo = c2.date_input("Prazo", value=hoje() + timedelta(days=7), key="nt_prazo", **FMT_DATA)
        coluna = c3.selectbox("Começa em qual coluna", STATUS_LIST, index=1, key="nt_status")
        if st.form_submit_button("Criar tarefa", type="primary", **LARG_FSB):
            if not titulo.strip():
                st.error("Informe o título da atividade.")
            elif not marcados:
                st.error("Marque ao menos uma pessoa responsável.")
            elif prazo < inicio:
                st.error("O prazo não pode ser anterior à data de início.")
            else:
                tid = criar_tarefa(titulo, descricao, user["id"], inicio, prazo,
                                   [mapa[n] for n in marcados], coluna)
                st.success(f"Tarefa #{tid} criada e atribuída a {', '.join(marcados)}.")


def aba_prorrogacoes(user: dict) -> None:
    st.markdown("#### Aguardando sua decisão")
    pendentes = pedidos_para_decidir(user)
    if not pendentes:
        st.caption("Nenhum pedido pendente.")
    for p in pendentes:
        with caixa():
            st.markdown(f"<div class='card-titulo'>{p['titulo']}</div>"
                        f"<div class='meta'>Solicitado por @{p['solicitante']}</div>"
                        f"<div class='meta'>Prazo atual {fmt_data(p['prazo_anterior'])} → "
                        f"solicitado <b>{fmt_data(p['prazo_solicitado'])}</b></div>"
                        f"<div class='meta'>Justificativa: {p['justificativa'] or '—'}</div>",
                        unsafe_allow_html=True)
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("Aprovar", key=f"ap_{p['id']}", type="primary", **LARG_BTN):
                decidir_prorrogacao(p, True, user)
                rerun()
            if c2.button("Recusar", key=f"rc_{p['id']}", **LARG_BTN):
                decidir_prorrogacao(p, False, user)
                rerun()

    st.markdown("#### Meus pedidos")
    meus = consultar("""SELECT p.*, t.titulo FROM prorrogacoes p
                        JOIN tarefas t ON t.id = p.tarefa_id
                        WHERE p.solicitante_id = %s ORDER BY p.id DESC""", (user["id"],))
    if not meus:
        st.caption("Você ainda não solicitou prorrogações.")
    for p in meus:
        cor = {"Pendente": "#ea580c", "Aprovada": CORES["Realizado"],
               "Recusada": CORES["Atrasado"]}[p["situacao"]]
        st.markdown(f"{badge(p['situacao'], cor)} &nbsp; <b>{p['titulo']}</b> "
                    f"<span class='meta'>{fmt_data(p['prazo_anterior'])} → "
                    f"{fmt_data(p['prazo_solicitado'])}</span>", unsafe_allow_html=True)


def aba_usuarios(user: dict) -> None:
    st.markdown("#### Cadastrar novo usuário")
    with st.form("novo_usuario", clear_on_submit=True):
        c1, c2 = st.columns(2)
        nome = c1.text_input("Nome completo", key="nu_nome")
        login = c2.text_input("Usuário (login)", key="nu_login")
        c3, c4 = st.columns(2)
        email = c3.text_input("E-mail", key="nu_email")
        papel = c4.selectbox("Perfil", ["usuario", "admin"], key="nu_papel",
                             format_func=lambda p: "Usuário" if p == "usuario" else "Administrador")
        senha = st.text_input("Senha provisória", type="password", key="nu_senha")
        if st.form_submit_button("Cadastrar usuário", type="primary", **LARG_FSB):
            if not (nome.strip() and login.strip() and senha):
                st.error("Preencha nome, usuário e senha.")
            elif consultar_um("SELECT id FROM usuarios WHERE usuario = %s",
                              (login.strip().lower(),)):
                st.error("Já existe um usuário com esse login.")
            else:
                criar_usuario(nome, login, email, senha, papel)
                st.success(f"Usuário {login} criado.")

    st.markdown("#### Usuários cadastrados")
    usuarios = consultar("SELECT * FROM usuarios ORDER BY nome")
    st.dataframe(
        [{"ID": u["id"], "Nome": u["nome"], "Usuário": u["usuario"], "E-mail": u["email"] or "—",
          "Perfil": "Administrador" if u["papel"] == "admin" else "Usuário",
          "Ativo": "Sim" if u["ativo"] else "Não", "Criado em": fmt_hora(u["criado_em"])}
         for u in usuarios], hide_index=True, **LARG_DF)

    st.markdown("#### Manutenção")
    alvo = st.selectbox("Selecione o usuário", usuarios, key="usr_alvo",
                        format_func=lambda u: f"{u['nome']} ({u['usuario']})")
    c1, c2 = st.columns(2)
    with c1:
        acao = "Desativar acesso" if alvo["ativo"] else "Reativar acesso"
        if st.button(acao, key="usr_toggle", **LARG_BTN):
            if alvo["id"] == user["id"]:
                st.error("Você não pode desativar o próprio acesso.")
            else:
                executar("UPDATE usuarios SET ativo = NOT ativo WHERE id = %s", (alvo["id"],))
                rerun()
    with c2:
        nova = st.text_input("Nova senha", type="password", key="usr_senha")
        if st.button("Redefinir senha", key="usr_reset", **LARG_BTN) and nova:
            salt = secrets.token_hex(16)
            executar("UPDATE usuarios SET senha_hash = %s, salt = %s WHERE id = %s",
                     (gerar_hash(nova, salt), salt, alvo["id"]))
            st.success(f"Senha de {alvo['nome']} redefinida.")


def aba_conta(user: dict) -> None:
    st.markdown("#### Alterar minha senha")
    with st.form("trocar_senha"):
        atual = st.text_input("Senha atual", type="password", key="mc_atual")
        nova = st.text_input("Nova senha", type="password", key="mc_nova")
        conf = st.text_input("Confirmar nova senha", type="password", key="mc_conf")
        if st.form_submit_button("Alterar senha", type="primary", **LARG_FSB):
            if not autenticar(user["usuario"], atual):
                st.error("Senha atual incorreta.")
            elif len(nova) < 6:
                st.error("A nova senha precisa ter ao menos 6 caracteres.")
            elif nova != conf:
                st.error("A confirmação não confere.")
            else:
                salt = secrets.token_hex(16)
                executar("UPDATE usuarios SET senha_hash = %s, salt = %s WHERE id = %s",
                         (gerar_hash(nova, salt), salt, user["id"]))
                st.success("Senha alterada com sucesso.")


def tela_detalhe(tid: int, user: dict) -> None:
    t = obter_tarefa(tid, user)
    if not t:
        st.error("Tarefa não encontrada ou sem permissão de acesso.")
        if st.button("Voltar", key="volta_erro"):
            st.session_state.tarefa_sel = None
            rerun()
        return

    if st.button("← Voltar ao quadro", key="voltar"):
        st.session_state.tarefa_sel = None
        rerun()

    rotulo, cor = situacao_prazo(t)
    st.markdown((badge("Atrasada", CORES["Atrasado"]) + " &nbsp; " if eh_atrasada(t) else "")
                + badge(t["status"], CORES[t["status"]])
                + f"<div class='titulo-app' style='margin-top:8px'>{t['titulo']}</div>"
                + f"<div style='color:{cor};font-weight:700;margin-bottom:6px'>{rotulo}</div>",
                unsafe_allow_html=True)

    esq, dir_ = st.columns([2, 1])

    with esq:
        st.markdown("##### Descrição")
        st.write(t["descricao"] or "_Sem descrição._")

        if pode_editar(t, user):
            with st.expander("Editar tarefa"):
                mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
                atuais = [n for n, i in mapa.items() if i in responsaveis_ids(t["id"])]
                with st.form("editar"):
                    novo_tit = st.text_input("Título", value=t["titulo"], key="ed_tit")
                    nova_desc = st.text_area("Descrição", value=t["descricao"] or "", height=110, key="ed_desc")
                    novos = st.multiselect("Responsáveis (@)", list(mapa), default=atuais, key="ed_resp")
                    novo_ini = st.date_input("Data de início", value=t["data_inicio"], key="ed_ini", **FMT_DATA)
                    if st.form_submit_button("Salvar alterações", type="primary", **LARG_FSB):
                        if not novo_tit.strip():
                            st.error("O título não pode ficar vazio.")
                        elif not novos:
                            st.error("A tarefa precisa de ao menos um responsável.")
                        else:
                            atualizar_tarefa(t, novo_tit, nova_desc, novo_ini,
                                             [mapa[n] for n in novos], user)
                            rerun()
                st.caption("O prazo só muda por prorrogação, para preservar o histórico.")

            with st.expander("Excluir tarefa"):
                st.warning("A exclusão apaga responsáveis, prorrogações e histórico da tarefa.")
                if st.checkbox("Confirmo a exclusão", key="conf_del") and \
                   st.button("Excluir definitivamente", key="btn_del", **LARG_BTN):
                    excluir_tarefa(t["id"])
                    st.session_state.tarefa_sel = None
                    rerun()

        st.markdown("##### Histórico")
        for e in consultar("""SELECT h.*, u.nome FROM historico h
                              LEFT JOIN usuarios u ON u.id = h.usuario_id
                              WHERE h.tarefa_id = %s ORDER BY h.id DESC""", (tid,)):
            st.markdown(f"**{e['acao']}** — {e['nome'] or 'Sistema'} · {fmt_hora(e['criado_em'])}  \n"
                        f"<span class='meta'>{e['detalhe'] or ''}</span>", unsafe_allow_html=True)

        with st.form("comentario", clear_on_submit=True):
            texto = st.text_area("Adicionar comentário ao histórico", height=80, key="det_coment")
            if st.form_submit_button("Registrar comentário") and texto.strip():
                registrar(tid, user["id"], "Comentário", texto.strip())
                rerun()

    with dir_:
        with caixa():
            st.markdown("##### Quando")
            st.markdown(f"<div class='meta'>Início</div><b>{fmt_data(t['data_inicio'])}</b>",
                        unsafe_allow_html=True)
            if t["prazo_atual"] != t["prazo_original"]:
                st.markdown(f"<div class='meta' style='margin-top:8px'>Prazo</div>"
                            f"<b>{fmt_data(t['prazo_atual'])}</b><br>"
                            f"<span class='meta riscado'>Programado inicialmente: "
                            f"{fmt_data(t['prazo_original'])}</span>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div class='meta' style='margin-top:8px'>Prazo</div>"
                            f"<b>{fmt_data(t['prazo_atual'])}</b>", unsafe_allow_html=True)
            if t["concluido_em"]:
                st.markdown(f"<div class='meta' style='margin-top:8px'>Concluída em</div>"
                            f"<b>{fmt_hora(t['concluido_em'])}</b>", unsafe_allow_html=True)

        with caixa():
            st.markdown("##### Quem")
            st.markdown(f"<div class='meta'>Criada por @{t['criador']}</div>",
                        unsafe_allow_html=True)
            for r in consultar("""SELECT u.nome, tr.aberto_em FROM tarefa_responsaveis tr
                                  JOIN usuarios u ON u.id = tr.usuario_id
                                  WHERE tr.tarefa_id = %s ORDER BY u.nome""", (tid,)):
                if r["aberto_em"]:
                    st.markdown(f"**@{r['nome']}**  \n"
                                f"<span class='meta'>abriu em {fmt_hora(r['aberto_em'])}</span>",
                                unsafe_allow_html=True)
                else:
                    st.markdown(f"**@{r['nome']}**  \n"
                                f"<span class='meta'>ainda não abriu</span>",
                                unsafe_allow_html=True)

        if pode_gerenciar(t, user):
            with caixa():
                st.markdown("##### Gerenciar")
                novo = st.selectbox("Status", STATUS_LIST, index=STATUS_LIST.index(t["status"]),
                                    key="det_status")
                if st.button("Salvar status", type="primary", key="det_salvar", **LARG_BTN):
                    alterar_status(t, novo, user)
                    rerun()

                pendente = consultar_um("SELECT * FROM prorrogacoes WHERE tarefa_id = %s "
                                        "AND situacao = 'Pendente'", (tid,))
                if pendente:
                    st.info(f"Pedido pendente para {fmt_data(pendente['prazo_solicitado'])}.")
                else:
                    with st.expander("Pedir prorrogação"):
                        with st.form("prorrogar"):
                            novo_prazo = st.date_input(
                                "Novo prazo", value=t["prazo_atual"] + timedelta(days=7), key="pr_prazo", **FMT_DATA)
                            just = st.text_area("Justificativa", height=80, key="pr_just")
                            if st.form_submit_button("Solicitar", **LARG_FSB):
                                if novo_prazo <= t["prazo_atual"]:
                                    st.error("O novo prazo precisa ser posterior ao atual.")
                                elif not just.strip():
                                    st.error("Informe a justificativa.")
                                else:
                                    solicitar_prorrogacao(t, novo_prazo, just, user)
                                    rerun()


# --------------------------------------------------------------------------- #
# Aplicação
# --------------------------------------------------------------------------- #

def menu_navegacao(user: dict, tarefas: list[dict]) -> tuple[str, list[dict]]:
    """Menu recolhido e filtros lado a lado, ambos compactos."""
    paginas = ["Kanban", "Calendário", "Lista", "Nova tarefa", "Prorrogações"]
    if user["papel"] == "admin":
        paginas.append("Usuários")
    paginas.append("Minha conta")

    atual = st.session_state.get("pagina", "Kanban")
    if atual not in paginas:
        atual = "Kanban"

    menu_col, filtro_col, _ = st.columns([1.1, 1.1, 5])

    with menu_col:
        if hasattr(st, "popover"):
            with st.popover(f"☰  {atual}", **LARG_BTN):
                for p in paginas:
                    if st.button(p, key=f"nav_{p}", **LARG_BTN):
                        st.session_state.pagina = p
                        rerun()
                st.divider()
                claro = st.session_state.tema == "claro"
                if st.button("Tema escuro" if claro else "Tema claro",
                             key="nav_tema", **LARG_BTN):
                    st.session_state.tema = "escuro" if claro else "claro"
                    rerun()
        else:
            escolha = st.selectbox("Menu", paginas, index=paginas.index(atual),
                                   key="nav_sel", label_visibility="collapsed")
            if escolha != atual:
                st.session_state.pagina = escolha
                rerun()

    filtradas = tarefas
    with filtro_col:
        if atual in ("Kanban", "Calendário", "Lista"):
            abridor = (st.popover("⚲  Filtros", **LARG_BTN)
                       if hasattr(st, "popover") else st.expander("Filtros"))
            with abridor:
                status_sel = st.multiselect("Status", STATUS_LIST, default=STATUS_LIST,
                                            key="f_status")
                so_atrasadas = st.checkbox("Somente atrasadas", key="f_atrasadas")
                busca = st.text_input("Buscar no título", key="f_busca")
            filtradas = [t for t in tarefas if t["status"] in status_sel]
            if so_atrasadas:
                filtradas = [t for t in filtradas if eh_atrasada(t)]
            if busca.strip():
                termo = busca.strip().lower()
                filtradas = [t for t in filtradas if termo in t["titulo"].lower()]

    return atual, filtradas


def main() -> None:
    st.set_page_config(page_title="Gestor de Tarefas", page_icon="🗂️",
                       layout="wide", initial_sidebar_state="collapsed")
    st.session_state.setdefault("tema", "escuro")
    st.session_state.setdefault("pagina", "Kanban")
    st.markdown(montar_css(st.session_state.tema), unsafe_allow_html=True)

    try:
        init_db(lambda: criar_usuario("Administrador", "admin", "admin@local",
                                      "admin123", "admin"))
    except ErroBanco as erro:
        cfg = config_db()
        st.error("**Não foi possível conectar ao PostgreSQL.**")
        st.code(str(erro).strip())
        st.caption(f"Tentando {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']} — "
                   "confira .streamlit/secrets.toml ou as variáveis PGHOST/PGPORT/"
                   "PGDATABASE/PGUSER/PGPASSWORD.")
        st.stop()

    # Retoma a sessão pelo token da URL (sobrevive ao F5).
    if "usuario" not in st.session_state:
        token = qp_ler("s")
        if token:
            u = sessao_valida(token)
            if u:
                st.session_state.usuario = u
                st.session_state.token = token
            else:
                qp_limpar()

    if "usuario" not in st.session_state:
        tela_login()
        return

    user = consultar_um("SELECT * FROM usuarios WHERE id = %s AND ativo",
                        (st.session_state.usuario["id"],))
    if not user:
        encerrar_sessao(st.session_state.get("token"))
        st.session_state.clear()
        qp_limpar()
        rerun()
    st.session_state.usuario = user
    st.session_state.setdefault("tarefa_sel", None)

    if st.session_state.tarefa_sel:
        tela_detalhe(st.session_state.tarefa_sel, user)
        return

    tarefas = listar_tarefas(user)
    cabecalho(user, tarefas)
    pagina, filtradas = menu_navegacao(user, tarefas)
    alertas(tarefas, user)

    if pagina == "Kanban":
        aba_kanban(filtradas, user)
    elif pagina == "Calendário":
        aba_calendario(filtradas, user)
    elif pagina == "Lista":
        aba_lista(filtradas, user)
    elif pagina == "Nova tarefa":
        aba_nova_tarefa(user)
    elif pagina == "Prorrogações":
        aba_prorrogacoes(user)
    elif pagina == "Usuários":
        aba_usuarios(user)
    elif pagina == "Minha conta":
        aba_conta(user)


if __name__ == "__main__":
    main()