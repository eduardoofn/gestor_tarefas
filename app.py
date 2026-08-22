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

Primeiro acesso: com o banco vazio, o app pede a criação do administrador
na própria tela. A partir daí, usuários são apenas linhas na tabela usuarios.
"""

from __future__ import annotations

import calendar as calmod
import base64
import contextlib
import hashlib
import hmac
import html
import inspect
import os
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from db import (ErroBanco, banco_vazio, binario, config_db, consultar,
                consultar_um, cursor, executar, init_db, inserir)

import streamlit.components.v1 as components

# Componente próprio: arrastar entre colunas + duplo clique para abrir.
_PASTA_QUADRO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quadro")
_quadro_componente = components.declare_component("quadro_tarefas", path=_PASTA_QUADRO)


_PASTA_CALENDARIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendario")
_calendario_componente = components.declare_component("calendario_tarefas",
                                                      path=_PASTA_CALENDARIO)


def quadro_kanban(colunas: list[dict], tema: dict, key: str = "quadro"):
    """Devolve {"acao": "mover"|"abrir"|"novo", ...} ou None."""
    return _quadro_componente(colunas=colunas, tema=tema, default=None, key=key)


def calendario_mes(dados: dict, key: str = "calendario"):
    """Devolve {"acao": "abrir"|"novo"|"mes", ...} ou None."""
    return _calendario_componente(**dados, default=None, key=key)


# Sino: toca o alerta sonoro e (opcionalmente) devolve um tique periódico,
# que é o que faz o Streamlit reexecutar sozinho e buscar novas notificações.
_PASTA_SINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sino")
_sino_componente = components.declare_component("sino_alerta", path=_PASTA_SINO)
_CAMINHO_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LOGOAVANSEG.png")


def sino_alerta(tocar: int, intervalo: int = 0, key: str = "sino"):
    """`tocar` muda → toca o som. `intervalo` > 0 → tique de atualização."""
    return _sino_componente(tocar=tocar, intervalo=intervalo, default=None, key=key)


def mostrar_logo(largura: int = 130) -> None:
    """Mostra a logo centralizada, mantendo o topo compacto."""
    with open(_CAMINHO_LOGO, "rb") as arquivo:
        imagem = base64.b64encode(arquivo.read()).decode("ascii")
    st.markdown(
        f"<div class='logo-centro'><img src='data:image/png;base64,{imagem}' "
        f"width='{largura}' alt='Avanseg'></div>",
        unsafe_allow_html=True)

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
    "Aguardando": "#7c3aed",
}

# Anexos vão para o BYTEA do Postgres (o disco do Streamlit Cloud é volátil).
MAX_ANEXO_MB = 10
MAX_CARTOES_COLUNA = 40       # teto de desenho por coluna; o contador mostra o total
# Altura da tabela da Lista: o padrão do Streamlit trava em ~10 linhas.
ALTURA_MAX_TABELA = 620

# Carimbo visível no menu da conta. Serve para responder de olho na tela a
# pergunta "os arquivos novos entraram mesmo?" sem abrir editor nenhum.
VERSAO = "v13 · 20/08/2026"
# Cada tique é uma reexecução inteira do app. Aumente se o quadro crescer
# muito; desligue de vez pelo menu da conta.
SEG_ATUALIZACAO = 90          # tique do sino nas telas de leitura

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

# `format` no date_input NÃO é questão de versão nova ou velha: existe desde a
# 1.30. Passando pelo _kw, na 1.41 ele caía no ramo "antigo" (dicionário vazio)
# e o campo voltava para o padrão americano aaaa/mm/dd. Aqui a pergunta certa é
# só uma: a assinatura aceita `format`?
def _aceita(fn, arg: str) -> bool:
    try:
        return arg in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


FMT_DATA = {"format": "DD/MM/YYYY"} if _aceita(st.date_input, "format") else {}

# `vertical_alignment` só existe da 1.36 em diante. Sem ele, a linha da Lista
# ainda funciona — o texto fica colado no topo da célula, só isso.
ALINHA_COL = ({"vertical_alignment": "center"}
              if _aceita(st.columns, "vertical_alignment") else {})

# Seleção de linha no st.dataframe existe da 1.35 em diante. Onde não existe,
# a Lista cai no seletor de tarefa — mesma barra de ações nos dois casos.
DF_SELECAO = _aceita(st.dataframe, "on_select") and _aceita(st.dataframe, "selection_mode")


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
.logo-centro {{ text-align: center; line-height: 0; margin: 0 0 6px 0; }}

/* O Streamlit renderiza popovers e menus em uma camada própria. No tema claro,
   fixe o contraste também nessa camada para não herdar o visual escuro. */
body:has(.stApp) div[data-baseweb="popover"],
body:has(.stApp) div[data-baseweb="menu"],
body:has(.stApp) ul[data-baseweb="menu"] {{
    background: {p['painel']} !important; color: {p['ink']} !important;
}}
body:has(.stApp) div[data-baseweb="popover"] button,
body:has(.stApp) div[data-baseweb="menu"] button,
body:has(.stApp) ul[data-baseweb="menu"] button {{
    background: {p['item']} !important; color: {p['ink']} !important;
    border-color: {p['linha']} !important;
}}
body:has(.stApp) div[data-baseweb="popover"] button *,
body:has(.stApp) div[data-baseweb="menu"] button *,
body:has(.stApp) ul[data-baseweb="menu"] button * {{
    color: {p['ink']} !important; fill: {p['ink']} !important;
}}
body:has(.stApp) div[data-baseweb="popover"] li,
body:has(.stApp) div[data-baseweb="menu"] li,
body:has(.stApp) ul[data-baseweb="menu"] li {{
    background: {p['painel']} !important; color: {p['ink']} !important;
}}
body:has(.stApp) div[data-baseweb="popover"] li:hover,
body:has(.stApp) div[data-baseweb="menu"] li:hover,
body:has(.stApp) ul[data-baseweb="menu"] li:hover {{
    background: {p['item2']} !important; color: {p['ink']} !important;
}}

/* topo do Streamlit: sem barra, sem borda, sem sobra de espaço */
header[data-testid="stHeader"], div[data-testid="stDecoration"],
div[data-testid="stStatusWidget"] {{ display: none !important; }}
.block-container, [data-testid="stAppViewBlockContainer"] {{
    padding-top: .8rem !important; padding-bottom: .6rem !important; }}
[data-testid="stVerticalBlock"] {{ gap: .55rem; }}
div[data-testid="stExpander"] details {{ border-color: var(--linha) !important;
                                         background: var(--painel) !important; }}

/* Todo o visual dos componentes nativos vem daqui — o app não depende de
   .streamlit/config.toml, então funciona igual no local e no Streamlit Cloud. */
.stApp, .stApp .stMarkdown p, .stApp .stMarkdown li {{ color: var(--ink); }}
[data-testid="stWidgetLabel"] p, .stApp label p, .stApp label {{
    color: var(--texto) !important; font-weight: 600; }}

/* ---- campos: o Streamlit pinta o INVÓLUCRO, o input em si é transparente ---- */
.stApp .stTextInput > div > div, .stApp .stTextArea > div > div,
.stApp .stDateInput > div > div, .stApp .stNumberInput > div > div,
.stApp .stSelectbox > div > div, .stApp .stMultiSelect > div > div,
.stApp [data-testid="stTextInputRootElement"],
.stApp [data-testid="stWidgetLabel"] + div > div,
.stApp div[data-baseweb="input"], .stApp div[data-baseweb="base-input"],
.stApp div[data-baseweb="textarea"], .stApp div[data-baseweb="select"] > div {{
    background-color: {p['item']} !important;
    border-color: {p['linha']} !important;
    color: {p['ink']} !important;
}}
.stApp input, .stApp textarea, .stApp div[data-baseweb="select"] div {{
    background-color: transparent !important;
    color: {p['ink']} !important;
    -webkit-text-fill-color: {p['ink']} !important;
    caret-color: {MARCA};
}}
.stApp input::placeholder, .stApp textarea::placeholder {{
    color: {p['muted']} !important; -webkit-text-fill-color: {p['muted']} !important; }}
/* preenchimento automático do Chrome sobrescreve o fundo — isto impede */
.stApp input:-webkit-autofill, .stApp input:-webkit-autofill:focus {{
    -webkit-box-shadow: 0 0 0 60px {p['item']} inset !important;
    -webkit-text-fill-color: {p['ink']} !important; }}
.stApp .stTextInput > div > div:focus-within, .stApp .stTextArea > div > div:focus-within,
.stApp div[data-baseweb="input"]:focus-within {{ border-color: {MARCA} !important; }}
/* aviso "Press Enter to submit form" */
.stApp [data-testid="InputInstructions"] {{ color: {p['muted']} !important; }}
/* olhinho da senha e ícones dos campos */
.stApp [data-testid="stTextInput"] button, .stApp [data-baseweb="input"] svg {{
    color: {p['muted']} !important; fill: {p['muted']} !important; }}

/* botões */
.stApp .stButton button, .stApp .stFormSubmitButton button,
div[data-testid="stPopover"] button {{
    background: var(--item) !important; color: var(--ink) !important;
    border: 1px solid var(--linha) !important; font-size: 12.5px !important;
    padding: 2px 10px !important; min-height: 30px !important;
    white-space: nowrap !important; }}
.stApp .stButton button *, .stApp .stFormSubmitButton button *,
div[data-testid="stPopover"] button * {{ color: inherit !important; }}
.stApp .stButton button svg, .stApp .stFormSubmitButton button svg,
div[data-testid="stPopover"] button svg {{ fill: currentColor !important; }}
/* Espaçamento geral mais curto: o Streamlit separa cada bloco vertical com
   1rem, e na tela da tarefa — descrição, conclusão, anexos, gerenciar,
   histórico — isso somava uma rolagem inteira de ar. */
[data-testid="stVerticalBlock"] {{ gap: .42rem !important; }}
[data-testid="stHorizontalBlock"] {{ gap: .5rem !important; }}
.stApp .stMarkdown p {{ margin-bottom: .18rem !important; }}
.stApp hr {{ margin: .35rem 0 !important; }}
.stApp h5 {{ margin: .1rem 0 .25rem 0 !important; font-size: 14px !important; }}
[data-testid="stExpander"] details {{ margin-bottom: .1rem !important; }}
[data-testid="stExpander"] summary {{ padding: .25rem .6rem !important;
    font-size: 12.5px !important; }}
.stApp .stTextArea textarea, .stApp .stTextInput input {{ font-size: 13px !important; }}
.stApp [data-testid="stWidgetLabel"] p {{ font-size: 12px !important;
    margin-bottom: .1rem !important; }}
/* Nada de mexer no padding do container por data-testid: o seletor muda de
   nome entre versões do Streamlit (foi o que já nos mordeu no quadro). O
   aperto vem do gap acima, que é propriedade estável. */

/* Tira de alertas: uma linha, sem o padding gordo dos banners nativos. */
.tira-alertas {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0 8px 0; }}
.alerta-chip {{ font-size: 11.5px; font-weight: 700; padding: 2px 9px;
                border-radius: 999px; border: 1px solid; line-height: 1.5;
                white-space: nowrap; }}

/* Sino enxuto: mais notificação visível na mesma altura. */
div[data-testid="stPopover"] .stButton button {{
    min-height: 22px !important; padding: 0 5px !important;
    font-size: 12px !important; line-height: 1 !important; }}
div[data-testid="stPopover"] .stButton button p {{ margin: 0 !important; }}
.notif-txt {{ font-size: 12px; line-height: 1.35; }}
.notif-hora {{ font-size: 10px; opacity: .6; }}
.notif-linha {{ border-top: 1px solid var(--linha); margin: 5px 0 4px 0; }}

/* Sem isto, botão em coluna estreita quebra a palavra ("Canc / elar"). */
.stApp .stButton button p, .stApp .stFormSubmitButton button p {{
    white-space: nowrap !important; overflow: hidden; text-overflow: ellipsis; }}
.stApp .stButton button:hover, div[data-testid="stPopover"] button:hover {{
    border-color: var(--marca) !important; color: var(--marca) !important; }}
.stApp button[kind="primary"], .stApp button[kind="primaryFormSubmit"],
.stApp [data-testid="stBaseButton-primary"],
.stApp [data-testid="stBaseButton-primaryFormSubmit"],
.stApp [data-testid="baseButton-primary"],
.stApp [data-testid="baseButton-primaryFormSubmit"] {{
    background: {MARCA} !important; color: #ffffff !important;
    border-color: {MARCA} !important; }}
.stApp button[kind="primary"]:hover, .stApp [data-testid="stBaseButton-primary"]:hover {{
    background: {MARCA_ESCURO} !important; border-color: {MARCA_ESCURO} !important;
    color: #ffffff !important; }}

/* painéis flutuantes (menu, filtros, adicionar) e listas de opções */
div[data-baseweb="popover"] > div, div[data-baseweb="menu"], ul[data-baseweb="menu"] {{
    background: var(--painel) !important; color: var(--ink) !important;
    border: 1px solid var(--linha) !important; }}
div[data-baseweb="popover"] li, div[data-baseweb="menu"] li,
div[data-baseweb="popover"] li *, div[data-baseweb="menu"] li * {{
    color: var(--ink) !important; }}
span[data-baseweb="tag"] {{ background: var(--marca) !important; color: #fff !important; }}

.stApp [data-testid="stCaptionContainer"] p {{ color: var(--muted) !important; }}
.stApp [data-testid="stExpander"] summary p {{ color: var(--texto) !important; }}

/* ---------- cabeçalho ---------- */
.eyebrow {{ font-size: 11px; font-weight: 800; letter-spacing: 2.4px;
            text-transform: uppercase; color: var(--marca); margin-bottom: 2px; }}
.titulo-app {{ font-size: 18px; font-weight: 800; color: var(--ink);
               letter-spacing: -.3px; margin: 4px 0 0 0; }}
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

/* ---------- barra de ação da Lista ---------- */
.lista-sel {{ font-size: 13.5px; font-weight: 700; color: var(--ink);
              line-height: 1.35; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }}

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


def esc(texto: str | None) -> str:
    """Texto de usuário indo para dentro de HTML nosso: aspas e sinais de
    maior/menor viram entidades, senão um título com < > quebra a célula."""
    return html.escape(texto or "", quote=True)


def tags(nomes: str | None) -> str:
    if not nomes:
        return "<span class='tag'>sem responsável</span>"
    return "".join(f"<span class='tag'>@{esc(n.strip())}</span>" for n in nomes.split(","))


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #

# O relógio do processo é do servidor: no Streamlit Cloud, UTC. Sem fixar o
# fuso, das 21h em diante o app já virou o dia — e a tarefa de amanhã aparece
# como "vence hoje", a de hoje como "atrasada há 1 dia".
FUSO = ZoneInfo(os.getenv("TZ_APP", "America/Fortaleza"))


def agora() -> datetime:
    """Horário local, sem tzinfo: as colunas do banco são TIMESTAMP sem fuso e
    misturar datetime consciente com ingênuo estoura na subtração."""
    return datetime.now(FUSO).replace(tzinfo=None)


def hoje() -> date:
    return agora().date()


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
    if agora() - linha["ultimo_acesso"] > timedelta(hours=HORAS_INATIVIDADE):
        executar("DELETE FROM sessoes WHERE token = %s", (token,))
        return None
    executar("UPDATE sessoes SET ultimo_acesso = %s WHERE token = %s", (agora(), token))
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


def aguardando_aprovacao(t: dict) -> bool:
    """Concluída pelo responsável, mas ainda sem o aceite de quem criou."""
    return t["status"] == "Realizado" and t.get("aprovacao") == "Pendente"


def tamanho_legivel(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_} B"
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.0f} KB".replace(".", ",")
    return f"{bytes_ / (1024 * 1024):.1f} MB".replace(".", ",")


def situacao_prazo(t: dict) -> tuple[str, str]:
    if aguardando_aprovacao(t):
        return "Aguardando aprovação", CORES["Aguardando"]
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
    # compare_digest em vez de ==: a comparação normal para no primeiro byte
    # diferente, e o tempo da resposta entrega quanto do hash foi acertado.
    if u and hmac.compare_digest(gerar_hash(senha, u["salt"]), u["senha_hash"]):
        return u
    return None


def trocar_senha(usuario_id: int, nova: str) -> None:
    """Troca a senha e derruba as sessões abertas daquele usuário — senão o
    token antigo continua valendo, e trocar a senha não expulsa ninguém."""
    salt = secrets.token_hex(16)
    with cursor(commit=True) as cur:
        cur.execute("UPDATE usuarios SET senha_hash = %s, salt = %s WHERE id = %s",
                    (gerar_hash(nova, salt), salt, usuario_id))
        cur.execute("DELETE FROM sessoes WHERE usuario_id = %s", (usuario_id,))


# Freio simples contra tentativa em série. Vive na sessão do navegador, então
# não é barreira contra um script determinado — é o que dá para fazer sem
# tabela nova, e já corta a digitação de senha no chute.
TENTATIVAS_LIMITE = 5
ESPERA_SEGUNDOS = 30


def login_bloqueado() -> int:
    """Segundos que ainda faltam para poder tentar de novo (0 = liberado)."""
    ate = st.session_state.get("login_travado_ate")
    if not ate:
        return 0
    faltam = int((ate - agora()).total_seconds())
    if faltam <= 0:
        st.session_state.login_travado_ate = None
        st.session_state.login_falhas = 0
        return 0
    return faltam


def registrar_falha_login() -> None:
    falhas = st.session_state.get("login_falhas", 0) + 1
    st.session_state.login_falhas = falhas
    if falhas >= TENTATIVAS_LIMITE:
        st.session_state.login_travado_ate = agora() + timedelta(seconds=ESPERA_SEGUNDOS)


def registrar(tarefa_id: int, usuario_id: int | None, acao: str, detalhe: str = "",
              cur=None) -> None:
    """Com `cur`, o registro entra na MESMA transação de quem chamou — é assim
    que a tarefa e sua trilha de auditoria nascem ou falham juntas."""
    sql = ("INSERT INTO historico (tarefa_id, usuario_id, acao, detalhe) "
           "VALUES (%s, %s, %s, %s)")
    params = (tarefa_id, usuario_id, acao, detalhe)
    cur.execute(sql, params) if cur else executar(sql, params)


def nome_usuario(uid: int) -> str:
    u = consultar_um("SELECT nome FROM usuarios WHERE id = %s", (uid,))
    return u["nome"] if u else "?"


# ----- notificações --------------------------------------------------------- #

def eco(texto: str, tarefa_id: int | None = None, cur=None) -> None:
    """Retorno das SUAS próprias ações — na tela, no som e no sino.

    Notificação normal é para os outros: quem age não recebe aviso do próprio
    ato, senão o sino viraria eco puro. Só que o registro some junto, e o menu
    fica vazio mesmo você tendo criado e movido coisas. Então a ação própria
    entra no sino já marcada como lida: aparece no histórico do menu, não conta
    no contador de não lidas e não dispara bipe duas vezes.
    """
    st.session_state.aviso = (texto, "ok")
    if tarefa_id:
        usuario = st.session_state.get("usuario") or {}
        if usuario.get("id"):
            sql = ("""INSERT INTO notificacoes (usuario_id, tarefa_id, tipo, texto, lida)
                      VALUES (%s, %s, 'minha', %s, TRUE)""")
            params = (usuario["id"], tarefa_id, texto)
            cur.execute(sql, params) if cur else executar(sql, params)
    if st.session_state.get("som_ativo", True) and st.session_state.get("eco_som", True):
        st.session_state.tocar_som = st.session_state.get("tocar_som", 0) + 1


def notificar(destinos, tarefa_id: int, tipo: str, texto: str, exceto: int | None = None,
              cur=None) -> None:
    """Grava uma notificação por destinatário, sem notificar quem causou o evento."""
    sql = ("INSERT INTO notificacoes (usuario_id, tarefa_id, tipo, texto) "
           "VALUES (%s, %s, %s, %s)")
    for uid in {u for u in destinos if u and u != exceto}:
        params = (uid, tarefa_id, tipo, texto)
        cur.execute(sql, params) if cur else executar(sql, params)


def envolvidos(tid: int, criador_id: int | None = None) -> set[int]:
    """Criador + responsáveis: quem deve saber do que acontece na tarefa."""
    ids = set(responsaveis_ids(tid))
    if criador_id:
        ids.add(criador_id)
    else:
        linha = consultar_um("SELECT criador_id FROM tarefas WHERE id = %s", (tid,))
        if linha:
            ids.add(linha["criador_id"])
    return ids


def notificacoes_de(user: dict, limite: int = 15) -> list[dict]:
    return consultar("""SELECT n.*, t.titulo FROM notificacoes n
                        LEFT JOIN tarefas t ON t.id = n.tarefa_id
                        WHERE n.usuario_id = %s ORDER BY n.id DESC LIMIT %s""",
                     (user["id"], limite))


def painel_usuario(user: dict) -> dict:
    """Contador, feed do sino e as duas filas de pendência — UMA ida ao banco.

    Eram quatro consultas em sequência a cada clique. Num banco local isso é
    ruído; num banco remoto cada ida custa a latência da rede, e quatro idas
    empilhadas viram a demora que aparece ao trocar de tela. Aqui o Postgres
    monta tudo de uma vez e devolve numa linha só.
    """
    if user["papel"] == "admin":
        filtro_prorrog = "SELECT COUNT(*) FROM prorrogacoes WHERE situacao = 'Pendente'"
        filtro_conclu = ("""SELECT COUNT(*) FROM tarefas
                             WHERE status = 'Realizado' AND aprovacao = 'Pendente'
                               AND criador_id = %s""")
        params = tuple([user["id"]] * 5)
    else:
        filtro_prorrog = ("""SELECT COUNT(*) FROM prorrogacoes p
                               JOIN tarefas t ON t.id = p.tarefa_id
                              WHERE p.situacao = 'Pendente' AND t.criador_id = %s
                                AND p.solicitante_id <> %s""")
        filtro_conclu = ("""SELECT COUNT(*) FROM tarefas
                             WHERE status = 'Realizado' AND aprovacao = 'Pendente'
                               AND criador_id = %s""")
        params = tuple([user["id"]] * 7)

    linha = consultar_um(f"""
        SELECT (SELECT ROW_TO_JSON(x) FROM (SELECT * FROM usuarios
                 WHERE id = %s AND ativo) x)                    AS usuario,
               (SELECT COUNT(*) FROM notificacoes
                 WHERE usuario_id = %s AND NOT lida)            AS nao_lidas,
               (SELECT COALESCE(MAX(id), 0) FROM notificacoes
                 WHERE usuario_id = %s AND NOT lida)            AS maior,
               ({filtro_prorrog})                               AS prorrogacoes,
               ({filtro_conclu})                                AS conclusoes,
               -- 12 cabem agora que cada aviso é uma linha; vêm na mesma
               -- consulta, então mostrar mais não custa ida extra ao banco.
               -- A data sai formatada pelo próprio Postgres: dentro do JSON
               -- ela viraria texto ISO, e aí o fmt_hora do Python quebraria
               -- ao tentar strftime numa string.
               (SELECT COALESCE(JSON_AGG(f), '[]'::json) FROM (
                    SELECT n.id, n.texto, n.lida, n.tarefa_id,
                           TO_CHAR(n.criado_em, 'DD/MM/YYYY HH24:MI') AS quando
                      FROM notificacoes n
                     WHERE n.usuario_id = %s ORDER BY n.id DESC LIMIT 12
               ) f)                                             AS feed
    """, params)
    return linha or {"usuario": None, "nao_lidas": 0, "maior": 0,
                     "prorrogacoes": 0, "conclusoes": 0, "feed": []}


def resumo_notificacoes(user: dict) -> dict:
    """Contagem e maior id em UMA consulta — isto roda a cada reexecução."""
    linha = consultar_um("""SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS maior
                              FROM notificacoes WHERE usuario_id = %s AND NOT lida""",
                         (user["id"],))
    return linha or {"n": 0, "maior": 0}


def marcar_lidas(user: dict, notif_id: int | None = None) -> None:
    if notif_id:
        executar("UPDATE notificacoes SET lida = TRUE WHERE id = %s AND usuario_id = %s",
                 (notif_id, user["id"]))
    else:
        executar("UPDATE notificacoes SET lida = TRUE WHERE usuario_id = %s AND NOT lida",
                 (user["id"],))


def descartar_notificacao(user: dict, notif_id: int) -> None:
    """Fechar é apagar mesmo: notificação lida não serve de histórico —
    quem quer a trilha completa tem o histórico da tarefa."""
    executar("DELETE FROM notificacoes WHERE id = %s AND usuario_id = %s",
             (notif_id, user["id"]))


def limpar_notificacoes(user: dict) -> None:
    executar("DELETE FROM notificacoes WHERE usuario_id = %s", (user["id"],))


def nomes_usuarios(ids) -> dict[int, str]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    linhas = consultar("SELECT id, nome FROM usuarios WHERE id = ANY(%s) ORDER BY nome",
                       (list(ids),))
    return {l["id"]: l["nome"] for l in linhas}


def usuarios_ativos() -> list[dict]:
    """Memorizado dentro da reexecução: várias telas pedem a mesma lista, e o
    session_state é zerado no começo de cada passada do main()."""
    memo = st.session_state.setdefault("_memo", {})
    if "usuarios_ativos" not in memo:
        memo["usuarios_ativos"] = consultar(
            "SELECT id, nome FROM usuarios WHERE ativo ORDER BY nome")
    return memo["usuarios_ativos"]


def criar_tarefa(titulo, descricao, criador_id, data_inicio, prazo, responsaveis,
                 status: str = "Iniciado", concluido_em=None) -> int:
    if status == "Realizado" and concluido_em is None:
        concluido_em = agora()
    # Nascer já em Realizado é decisão de quem cria — não precisa se aprovar.
    aprovacao = "Aprovada" if status == "Realizado" else None
    # Um SELECT para todos os nomes, não um por responsável (fora da transação:
    # é leitura, e prender a conexão mais tempo não ajuda em nada).
    nomes = ", ".join(nomes_usuarios(responsaveis).values())
    quem_criou = nome_usuario(criador_id)
    detalhe = f"Responsáveis: {nomes} | Prazo: {fmt_data(prazo)} | Coluna: {status}"
    if concluido_em:
        detalhe += f" | Concluída em {fmt_hora(concluido_em)}"

    # Tudo numa transação só. Antes eram N commits: uma queda no meio deixava
    # tarefa sem responsável — invisível para quem não é admin, e sem trilha.
    with cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO tarefas (titulo, descricao, criador_id, data_inicio,
                                    prazo_original, prazo_atual, status, concluido_em,
                                    aprovacao, aprovado_por, aprovado_em, conclusao_em)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (titulo.strip(), descricao.strip(), criador_id, data_inicio, prazo, prazo,
             status, concluido_em, aprovacao,
             criador_id if aprovacao else None, concluido_em, concluido_em))
        tid = cur.fetchone()["id"]
        for uid in responsaveis:
            cur.execute("INSERT INTO tarefa_responsaveis (tarefa_id, usuario_id) "
                        "VALUES (%s, %s)", (tid, uid))
        registrar(tid, criador_id, "Tarefa criada", detalhe, cur=cur)
        notificar(responsaveis, tid, "marcado",
                  f"@{quem_criou} marcou você em “{titulo.strip()}” — "
                  f"prazo {fmt_data(prazo)}", exceto=criador_id, cur=cur)
        marcados = [u for u in responsaveis if u != criador_id]
        eco(f"Tarefa #{tid} criada" + (f" e atribuída a {nomes}." if marcados else "."),
            tid, cur=cur)
    return tid


SQL_TAREFA_BASE = """
SELECT t.*,
       u.nome AS criador,
       r.responsaveis,
       COALESCE(r.qtd_resp, 0)    AS qtd_resp,
       COALESCE(r.qtd_abertos, 0) AS qtd_abertos,
       COALESCE(p.prorrog_pendentes, 0) AS prorrog_pendentes,
       COALESCE(a.qtd_anexos, 0)  AS qtd_anexos,
       uc.nome AS concluiu_nome,
       ua.nome AS aprovou_nome{sou_resp}
  FROM tarefas t
  JOIN usuarios u ON u.id = t.criador_id
  LEFT JOIN usuarios uc ON uc.id = t.conclusao_por
  LEFT JOIN usuarios ua ON ua.id = t.aprovado_por
  LEFT JOIN (
        SELECT tr.tarefa_id,
               STRING_AGG(u2.nome, ', ' ORDER BY u2.nome) AS responsaveis,
               COUNT(*)                                   AS qtd_resp,
               COUNT(tr.aberto_em)                        AS qtd_abertos
          FROM tarefa_responsaveis tr
          JOIN usuarios u2 ON u2.id = tr.usuario_id
         GROUP BY tr.tarefa_id
       ) r ON r.tarefa_id = t.id
  LEFT JOIN (
        SELECT tarefa_id, COUNT(*) AS prorrog_pendentes
          FROM prorrogacoes WHERE situacao = 'Pendente' GROUP BY tarefa_id
       ) p ON p.tarefa_id = t.id
  LEFT JOIN (
        SELECT tarefa_id, COUNT(*) AS qtd_anexos FROM anexos GROUP BY tarefa_id
       ) a ON a.tarefa_id = t.id
"""


SQL_SOU_RESP = """,
       EXISTS (SELECT 1 FROM tarefa_responsaveis tr3
                WHERE tr3.tarefa_id = t.id AND tr3.usuario_id = %s) AS sou_responsavel"""


def sql_tarefas() -> str:
    return SQL_TAREFA_BASE.format(sou_resp=SQL_SOU_RESP)


def listar_tarefas(user: dict) -> list[dict]:
    """Admin vê tudo; usuário vê o que criou ou onde foi marcado (@).

    `sou_responsavel` vem junto de propósito: os alertas precisavam disso e
    faziam uma consulta por tarefa atrasada — com o quadro cheio, era a maior
    fonte de lentidão da tela."""
    sql = sql_tarefas()
    if user["papel"] == "admin":
        return consultar(sql + " ORDER BY t.prazo_atual, t.id", (user["id"],))
    return consultar(sql + """
        WHERE t.criador_id = %s
           OR EXISTS (SELECT 1 FROM tarefa_responsaveis tr2
                       WHERE tr2.tarefa_id = t.id AND tr2.usuario_id = %s)
        ORDER BY t.prazo_atual, t.id""", (user["id"], user["id"], user["id"]))


def obter_tarefa(tid: int, user: dict) -> dict | None:
    """Busca só a tarefa pedida. Antes isto listava o quadro inteiro e
    filtrava em Python — caro numa tela que reexecuta a cada clique."""
    sql = sql_tarefas() + " WHERE t.id = %s"
    if user["papel"] != "admin":
        sql += """ AND (t.criador_id = %s
                    OR EXISTS (SELECT 1 FROM tarefa_responsaveis tr2
                                WHERE tr2.tarefa_id = t.id AND tr2.usuario_id = %s))"""
        return consultar_um(sql, (user["id"], tid, user["id"], user["id"]))
    return consultar_um(sql, (user["id"], tid))


def responsaveis_ids(tid: int) -> list[int]:
    return [r["usuario_id"] for r in
            consultar("SELECT usuario_id FROM tarefa_responsaveis WHERE tarefa_id = %s", (tid,))]


def eh_responsavel(tid: int, uid: int) -> bool:
    return bool(consultar_um("SELECT 1 AS x FROM tarefa_responsaveis "
                             "WHERE tarefa_id = %s AND usuario_id = %s", (tid, uid)))


def pode_gerenciar(t: dict, user: dict) -> bool:
    if user["papel"] == "admin" or t["criador_id"] == user["id"]:
        return True
    if "sou_responsavel" in t:          # já veio na consulta, não pergunte de novo
        return bool(t["sou_responsavel"])
    return eh_responsavel(t["id"], user["id"])


def pode_editar(t: dict, user: dict) -> bool:
    """Editar a tarefa em si e excluir: só criador ou admin."""
    return user["papel"] == "admin" or t["criador_id"] == user["id"]


def pode_aprovar(t: dict, user: dict) -> bool:
    """Aceitar ou recusar a conclusão: SÓ quem pediu a tarefa.

    Admin não entra nesta: ver tudo e administrar usuários é uma coisa,
    decidir se a entrega de outra pessoa serve é outra — quem sabe o que
    pediu é quem pediu. Efeito colateral a conhecer: se o criador for
    desativado, as conclusões dele ficam sem quem aceite; nesse caso, passe
    a tarefa para outro criador editando-a, ou reative o usuário.
    """
    return t["criador_id"] == user["id"]


def registrar_abertura(tid: int, user: dict) -> None:
    linha = consultar_um("SELECT aberto_em FROM tarefa_responsaveis "
                         "WHERE tarefa_id = %s AND usuario_id = %s", (tid, user["id"]))
    if linha and linha["aberto_em"] is None:
        executar("UPDATE tarefa_responsaveis SET aberto_em = %s "
                 "WHERE tarefa_id = %s AND usuario_id = %s",
                 (agora(), tid, user["id"]))
        registrar(tid, user["id"], "Tarefa aberta", "Confirmou leitura da atividade")


def alterar_status(t: dict, novo: str, user: dict) -> bool:
    """Move entre colunas. Ir para Realizado NÃO passa por aqui — exige a
    descrição de conclusão e o aceite do solicitante (ver enviar_conclusao)."""
    if novo == t["status"]:
        return False
    if novo == "Realizado":
        return False
    # Saindo de Realizado, o ciclo de aprovação recomeça do zero.
    executar("""UPDATE tarefas SET status = %s, concluido_em = NULL,
                       aprovacao = NULL, aprovado_por = NULL, aprovado_em = NULL
                 WHERE id = %s""", (novo, t["id"]))
    registrar(t["id"], user["id"], "Status alterado", f"{t['status']} → {novo}")
    notificar(envolvidos(t["id"], t["criador_id"]), t["id"], "movida",
              f"@{user['nome']} moveu “{t['titulo']}” de {t['status']} para {novo}",
              exceto=user["id"])
    eco(f"“{t['titulo']}” movida para {novo}.", t["id"])
    return True


# ----- conclusão com aceite do solicitante ---------------------------------- #

def enviar_conclusao(t: dict, texto: str, user: dict) -> bool:
    """Finalizar NUNCA fecha a tarefa. Sempre para em 'aguardando aprovação'.

    Antes havia um atalho: se quem finalizava era o próprio criador, aprovava
    sozinho — a análise já teria sido feita por quem tinha de fazer. Na prática
    o atalho engolia a etapa justamente no caso mais comum, que é o criador
    tocando as próprias tarefas, e a trava parecia não existir. Regra única
    agora: toda conclusão passa pela análise de quem pediu, sem exceção — só
    que, quando é o próprio criador, o botão de aceitar já aparece ali do lado.

    Devolve sempre False (nada é aprovado neste passo); a assinatura fica para
    não quebrar quem chama.
    """
    momento = agora()
    executar("""UPDATE tarefas
                   SET status = 'Realizado', conclusao_texto = %s, conclusao_por = %s,
                       conclusao_em = %s, aprovacao = 'Pendente', aprovado_por = NULL,
                       aprovado_em = NULL, concluido_em = NULL, aprovacao_obs = NULL
                 WHERE id = %s""",
             (texto.strip(), user["id"], momento, t["id"]))
    registrar(t["id"], user["id"], "Conclusão enviada", texto.strip())
    eco(f"“{t['titulo']}” finalizada — aguardando a análise de @{t['criador']}.", t["id"])
    notificar([t["criador_id"]], t["id"], "aprovar",
              f"@{user['nome']} finalizou “{t['titulo']}” e aguarda sua aprovação",
              exceto=user["id"])
    return False


def decidir_conclusao(t: dict, aprovar: bool, observacao: str, user: dict) -> None:
    momento = agora()
    if aprovar:
        executar("""UPDATE tarefas SET aprovacao = 'Aprovada', aprovado_por = %s,
                           aprovado_em = %s, concluido_em = COALESCE(conclusao_em, %s),
                           aprovacao_obs = %s
                     WHERE id = %s""",
                 (user["id"], momento, momento, observacao.strip() or None, t["id"]))
        registrar(t["id"], user["id"], "Conclusão aprovada", observacao.strip())
        eco(f"Conclusão de “{t['titulo']}” aceita.", t["id"])
        notificar(envolvidos(t["id"], t["criador_id"]), t["id"], "aprovada",
                  f"@{user['nome']} aceitou a conclusão de “{t['titulo']}”",
                  exceto=user["id"])
    else:
        # Recusou: volta a andar, sem apagar o texto da tentativa anterior.
        executar("""UPDATE tarefas SET status = 'Em andamento', aprovacao = 'Recusada',
                           aprovado_por = %s, aprovado_em = %s, concluido_em = NULL,
                           aprovacao_obs = %s
                     WHERE id = %s""",
                 (user["id"], momento, observacao.strip(), t["id"]))
        registrar(t["id"], user["id"], "Conclusão recusada", observacao.strip())
        eco(f"Conclusão de “{t['titulo']}” recusada — volta para Em andamento.", t["id"])
        notificar(envolvidos(t["id"], t["criador_id"]), t["id"], "recusada",
                  f"@{user['nome']} recusou a conclusão de “{t['titulo']}”: "
                  f"{observacao.strip()}", exceto=user["id"])


def conclusoes_para_aprovar(user: dict) -> list[dict]:
    """Tarefas finalizadas que dependem da análise deste usuário."""
    base = SQL_TAREFA_BASE.format(sou_resp="")
    return consultar(base + """
        WHERE t.status = 'Realizado' AND t.aprovacao = 'Pendente'
          AND t.criador_id = %s
        ORDER BY t.conclusao_em DESC""", (user["id"],))


# ----- anexos --------------------------------------------------------------- #

def anexos_da_tarefa(tid: int, com_conteudo: bool = False) -> list[dict]:
    colunas = "a.id, a.nome, a.tipo, a.tamanho, a.criado_em, a.usuario_id, u.nome AS autor"
    if com_conteudo:
        colunas += ", a.conteudo"
    return consultar(f"""SELECT {colunas} FROM anexos a
                         LEFT JOIN usuarios u ON u.id = a.usuario_id
                         WHERE a.tarefa_id = %s ORDER BY a.id""", (tid,))


def salvar_anexo(tid: int, arquivo, user: dict) -> None:
    dados = arquivo.getvalue()
    linha = consultar_um("SELECT titulo, criador_id FROM tarefas WHERE id = %s", (tid,))
    destinos = envolvidos(tid, linha["criador_id"]) if linha else set()
    with cursor(commit=True) as cur:
        cur.execute("""INSERT INTO anexos (tarefa_id, usuario_id, nome, tipo,
                                           tamanho, conteudo)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (tid, user["id"], arquivo.name, getattr(arquivo, "type", None),
                     len(dados), binario(dados)))
        registrar(tid, user["id"], "Anexo adicionado",
                  f"{arquivo.name} ({tamanho_legivel(len(dados))})", cur=cur)
        if linha:
            notificar(destinos, tid, "anexo",
                      f"@{user['nome']} anexou {arquivo.name} em “{linha['titulo']}”",
                      exceto=user["id"], cur=cur)


def excluir_anexo(anexo_id: int, tid: int, user: dict) -> None:
    linha = consultar_um("SELECT nome FROM anexos WHERE id = %s", (anexo_id,))
    executar("DELETE FROM anexos WHERE id = %s", (anexo_id,))
    if linha:
        registrar(tid, user["id"], "Anexo removido", linha["nome"])


# ----- duplicar ------------------------------------------------------------- #

def duplicar_tarefa(t: dict, titulo: str, inicio: date, prazo: date,
                    responsaveis: list[int], user: dict,
                    copiar_descricao: bool = True, copiar_anexos: bool = False) -> int:
    novo_id = criar_tarefa(titulo, t["descricao"] or "" if copiar_descricao else "",
                           user["id"], inicio, prazo, responsaveis, "Iniciado")
    with cursor(commit=True) as cur:
        if copiar_anexos:
            # Ou a cópia leva todos os anexos, ou não leva nenhum: meia cópia
            # silenciosa é pior do que um erro na tela.
            for a in anexos_da_tarefa(t["id"], com_conteudo=True):
                cur.execute(
                    """INSERT INTO anexos (tarefa_id, usuario_id, nome, tipo,
                                           tamanho, conteudo)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (novo_id, user["id"], a["nome"], a["tipo"], a["tamanho"],
                     binario(bytes(a["conteudo"]))))
        registrar(novo_id, user["id"], "Cópia de tarefa",
                  f"Duplicada a partir de #{t['id']} — {t['titulo']}", cur=cur)
        registrar(t["id"], user["id"], "Tarefa duplicada",
                  f"Gerou a tarefa #{novo_id}", cur=cur)
    return novo_id


def atualizar_tarefa(t: dict, titulo, descricao, inicio, novos_resp, user) -> None:
    mudancas = []
    if titulo.strip() != t["titulo"]:
        mudancas.append(f"Título: {t['titulo']} → {titulo.strip()}")
    if inicio != t["data_inicio"]:
        mudancas.append(f"Início: {fmt_data(t['data_inicio'])} → {fmt_data(inicio)}")

    atuais = set(responsaveis_ids(t["id"]))
    novos = set(novos_resp)
    entram, saem = novos - atuais, atuais - novos
    # Nomes resolvidos antes: dentro da transação só ficam as escritas.
    nomes = nomes_usuarios(entram | saem)
    mudancas += [f"Incluído @{nomes.get(uid, '?')}" for uid in entram]
    mudancas += [f"Removido @{nomes.get(uid, '?')}" for uid in saem]
    houve = bool(mudancas) or descricao.strip() != (t["descricao"] or "")
    resumo = " | ".join(mudancas) or "Descrição atualizada"
    # Leitura resolvida ANTES de abrir a transação: consulta feita lá dentro
    # sairia por outra conexão do pool e não enxergaria o que ainda não foi
    # confirmado — além de segurar duas conexões à toa.
    avisar = envolvidos(t["id"], t["criador_id"]) | novos if houve else set()

    with cursor(commit=True) as cur:
        cur.execute("UPDATE tarefas SET titulo = %s, descricao = %s, data_inicio = %s "
                    "WHERE id = %s",
                    (titulo.strip(), descricao.strip(), inicio, t["id"]))
        for uid in entram:
            cur.execute("INSERT INTO tarefa_responsaveis (tarefa_id, usuario_id) "
                        "VALUES (%s, %s)", (t["id"], uid))
            notificar([uid], t["id"], "marcado",
                      f"@{user['nome']} marcou você em “{titulo.strip()}” — "
                      f"prazo {fmt_data(t['prazo_atual'])}", exceto=user["id"], cur=cur)
        for uid in saem:
            cur.execute("DELETE FROM tarefa_responsaveis "
                        "WHERE tarefa_id = %s AND usuario_id = %s", (t["id"], uid))
        if houve:
            registrar(t["id"], user["id"], "Tarefa editada", resumo, cur=cur)
            # Dizer O QUE mudou, não só "salvo": é o que confirma que pegou.
            eco(f"Tarefa #{t['id']} atualizada — {resumo}", t["id"], cur=cur)
            notificar(avisar, t["id"], "editada",
                      f"@{user['nome']} alterou “{titulo.strip()}”: {resumo}",
                      exceto=user["id"], cur=cur)

    if not houve:
        st.session_state.aviso = ("Nada mudou — nenhum campo foi alterado.", "alerta")


def excluir_tarefa(tid: int) -> None:
    executar("DELETE FROM tarefas WHERE id = %s", (tid,))


def solicitar_prorrogacao(t: dict, novo_prazo: date, justificativa: str, user: dict) -> None:
    executar("""INSERT INTO prorrogacoes (tarefa_id, solicitante_id, prazo_anterior,
                                          prazo_solicitado, justificativa, situacao)
                VALUES (%s, %s, %s, %s, %s, 'Pendente')""",
             (t["id"], user["id"], t["prazo_atual"], novo_prazo, justificativa.strip()))
    registrar(t["id"], user["id"], "Prorrogação solicitada",
              f"{fmt_data(t['prazo_atual'])} → {fmt_data(novo_prazo)} | {justificativa.strip()}")
    notificar([t["criador_id"]], t["id"], "prorrogacao",
              f"@{user['nome']} pediu prorrogação em “{t['titulo']}” para "
              f"{fmt_data(novo_prazo)}", exceto=user["id"])


def decidir_prorrogacao(pedido: dict, aprovar: bool, user: dict) -> None:
    situacao = "Aprovada" if aprovar else "Recusada"
    # Decisão e prazo mudam juntos: aprovar e não mover a data (ou o contrário)
    # deixaria o pedido resolvido com a tarefa no prazo velho.
    with cursor(commit=True) as cur:
        cur.execute("UPDATE prorrogacoes SET situacao = %s, decidido_por = %s, "
                    "decidido_em = %s WHERE id = %s",
                    (situacao, user["id"], agora(), pedido["id"]))
        if aprovar:
            # prazo_original NUNCA muda — fica o registro da data programada inicial.
            cur.execute("UPDATE tarefas SET prazo_atual = %s WHERE id = %s",
                        (pedido["prazo_solicitado"], pedido["tarefa_id"]))
    registrar(pedido["tarefa_id"], user["id"], f"Prorrogação {situacao.lower()}",
              f"{fmt_data(pedido['prazo_anterior'])} → {fmt_data(pedido['prazo_solicitado'])}")
    notificar([pedido["solicitante_id"]], pedido["tarefa_id"], "prorrogacao",
              f"@{user['nome']} {situacao.lower()} sua prorrogação em "
              f"“{pedido['titulo']}”", exceto=user["id"])


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

def fechar_paineis(exceto: str | None = None) -> None:
    """Baixa as bandeirinhas que mantêm diálogos abertos.

    O X do diálogo do Streamlit fecha a janela no navegador, mas não avisa o
    Python: a bandeirinha continua ligada no session_state. Na reexecução
    seguinte — arrastar um cartão, por exemplo — o diálogo ressuscita, e é por
    isso que mover uma tarefa abria a tela de criar tarefa. Toda ação vinda do
    quadro ou do calendário passa por aqui antes de abrir o que for dela.
    """
    for bandeira in ("abrir_novo", "cal_dia", "concluir_id", "nova_em",
                     "nova_data", "dup_lista"):
        if bandeira != exceto:
            st.session_state[bandeira] = None if bandeira != "abrir_novo" else False


def abrir_tarefa(tid: int, user: dict) -> None:
    st.session_state.abrir_novo = False
    st.session_state.dup_lista = None
    registrar_abertura(tid, user)
    st.session_state.tarefa_sel = tid
    rerun()


def card_tarefa(t: dict, user: dict, chave: str) -> None:
    atrasada = eh_atrasada(t)
    rotulo, cor = situacao_prazo(t)
    acento = CORES["Atrasado"] if atrasada else CORES[t["status"]]
    selo = (badge("Atrasada", CORES["Atrasado"]) if atrasada
            else badge(t["status"], CORES[t["status"]]))
    if aguardando_aprovacao(t):
        acento = CORES["Aguardando"]
        selo = badge("Aguardando aprovação", CORES["Aguardando"])
    if t["prorrog_pendentes"]:
        selo += " " + badge("Prorrogação", "#7c3aed")
    if t.get("qtd_anexos"):
        selo += " " + badge(f"📎 {t['qtd_anexos']}", "#64748b")
    st.markdown(
        f"<div class='card' style='--acc:{acento}'>{selo}"
        f"<div class='card-titulo'>{esc(t['titulo'])}</div>"
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
            descricao = st.text_area("Descrição (opcional)", height=90,
                                     key=f"add_desc_card_{status}",
                                     placeholder="Contexto, entregável esperado, links…")
            resp = st.multiselect("Responsáveis (@)", list(mapa), key=f"add_resp_{status}")
            prazo = st.date_input("Prazo", value=hoje() + timedelta(days=7),
                                  key=f"add_prazo_{status}", **FMT_DATA)
            if st.form_submit_button("Criar", type="primary", **LARG_FSB):
                if not titulo.strip():
                    st.error("Informe a atividade.")
                elif not resp:
                    st.error("Marque ao menos um responsável.")
                else:
                    criar_tarefa(titulo, descricao, user["id"], hoje(), prazo,
                                 [mapa[n] for n in resp], status)
                    rerun()


def conteudo_nova_tarefa(user: dict) -> None:
    """Campos da criação rápida. Sem st.form: assim o campo de conclusão
    aparece no instante em que a etapa Realizado é escolhida."""
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    titulo = st.text_input("Atividade", key="qa_tit")
    descricao = st.text_area("Descrição (opcional)", height=90, key="qa_desc",
                             placeholder="Contexto, entregável esperado, links…")
    etapa = st.selectbox("Etapa", STATUS_LIST, index=1, key="qa_col")
    resp = st.multiselect("Responsáveis (@)", list(mapa), key="qa_resp")
    prazo = st.date_input("Prazo", value=hoje() + timedelta(days=7), key="qa_prazo", **FMT_DATA)

    fim_em = None
    if etapa == "Realizado":
        fim_em = st.date_input("Concluída em", value=hoje(), key="qa_fim", **FMT_DATA)

    c1, c2 = st.columns(2)
    if c1.button("Criar", type="primary", key="qa_criar", **LARG_BTN):
        if not titulo.strip():
            st.error("Informe a atividade.")
        elif not resp:
            st.error("Marque ao menos um responsável.")
        else:
            criar_tarefa(titulo, descricao, user["id"], hoje(), prazo,
                         [mapa[n] for n in resp], etapa,
                         datetime.combine(fim_em, datetime.min.time()) if fim_em else None)
            for chave in ("qa_tit", "qa_desc", "qa_resp"):
                st.session_state.pop(chave, None)
            st.session_state.abrir_novo = False
            rerun()
    if c2.button("Cancelar", key="qa_cancel", **LARG_BTN):
        st.session_state.abrir_novo = False
        rerun()


# O diálogo mantém-se aberto entre interações — é o que permite o campo condicional.
_dialogo_nova = (st.dialog("Nova tarefa")(conteudo_nova_tarefa)
                 if hasattr(st, "dialog") else None)


def iniciais(nome: str) -> str:
    partes = [p for p in nome.split() if p]
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()


def cabecalho(user: dict, paginas: list[str]) -> str:
    """Título curto à esquerda; navegação, novo, filtros e conta à direita."""
    atual = st.session_state.get("pagina", "Kanban")
    if atual not in paginas + ["Usuários", "Minha conta"]:
        atual = "Kanban"

    titulo, logo_c, menu_c, novo_c, filtro_c, sino_c, conta_c = st.columns(
        [5, 6, 1, 1, 1, 1, 1])

    with titulo:
        st.markdown("<div class='titulo-app'>Gestor de Tarefas</div>", unsafe_allow_html=True)

    with logo_c:
        mostrar_logo(largura=70)

    with menu_c:
        if hasattr(st, "popover"):
            with st.popover("☰", **LARG_BTN):
                for p in paginas:
                    if st.button(("• " if p == atual else "") + p, key=f"nav_{p}", **LARG_BTN):
                        st.session_state.pagina = p
                        st.session_state.abrir_novo = False
                        rerun()
        else:
            escolha = st.selectbox("Menu", paginas, index=paginas.index(atual),
                                   key="nav_sel", label_visibility="collapsed")
            if escolha != atual:
                st.session_state.pagina = escolha
                rerun()

    with novo_c:
        if _dialogo_nova:
            if st.button("＋", key="btn_novo", **LARG_BTN):
                st.session_state.abrir_novo = True
                rerun()
        else:
            with st.expander("＋"):
                conteudo_nova_tarefa(user)

    st.session_state.filtro_slot = filtro_c

    with sino_c:
        pendentes_n = st.session_state.get("notif_n", 0)
        rotulo = f"🔔 {pendentes_n}" if pendentes_n else "🔔"
        if hasattr(st, "popover"):
            with st.popover(rotulo, **LARG_BTN):
                itens = st.session_state.get("painel", {}).get("feed") or []
                if not itens:
                    st.caption("Nenhuma notificação por aqui.")
                # Texto e botões na MESMA linha, com um filete no lugar do
                # st.divider (que tem margem larga). Cada aviso passou de três
                # blocos empilhados para uma tira só — cabe o dobro na janela.
                for i, n in enumerate(itens):
                    ponto = "🟢 " if not n["lida"] else ""
                    quando = n.get("quando") or fmt_hora(n.get("criado_em"))
                    texto, abrir_c, fechar_c = st.columns([8.5, 1, 1])
                    texto.markdown(
                        f"<div class='notif-txt'>{ponto}{esc(n['texto'])}</div>"
                        f"<div class='notif-hora'>{quando}</div>",
                        unsafe_allow_html=True)
                    # Ícone em vez de palavra: o rótulo escrito mandava na
                    # largura do botão e comia a coluna do texto.
                    if n["tarefa_id"] and abrir_c.button("↗", key=f"nt_{n['id']}",
                                                         help="Abrir tarefa", **LARG_BTN):
                        marcar_lidas(user, n["id"])
                        abrir_tarefa(n["tarefa_id"], user)
                    if fechar_c.button("✕", key=f"nx_{n['id']}", help="Descartar",
                                       **LARG_BTN):
                        descartar_notificacao(user, n["id"])
                        rerun()
                    if i < len(itens) - 1:
                        st.markdown("<div class='notif-linha'></div>",
                                    unsafe_allow_html=True)

                ligado = st.session_state.get("som_ativo", True)
                b1, b2, b3, _ = st.columns([1, 1, 1, 3])
                if pendentes_n and b1.button("✓", key="notif_todas",
                                             help="Marcar todas como lidas", **LARG_BTN):
                    marcar_lidas(user)
                    rerun()
                if itens and b2.button("🗑", key="notif_limpar",
                                       help="Limpar tudo", **LARG_BTN):
                    limpar_notificacoes(user)
                    rerun()
                if b3.button("🔊" if ligado else "🔇", key="som_toggle",
                             help="Som ligado — clique para desligar" if ligado
                                  else "Som desligado — clique para ligar", **LARG_BTN):
                    st.session_state.som_ativo = not ligado
                    rerun()
        else:
            if pendentes_n:
                st.markdown(f"<div class='meta'>🔔 {pendentes_n}</div>", unsafe_allow_html=True)

    with conta_c:
        if hasattr(st, "popover"):
            with st.popover(iniciais(user["nome"]), **LARG_BTN):
                st.markdown(f"<b>{esc(user['nome'])}</b><br>"
                            f"<span class='meta'>@{esc(user['usuario'])} · "
                            f"{'Administrador' if user['papel'] == 'admin' else 'Usuário'}<br>"
                            f"Gestor de Tarefas {VERSAO}</span>",
                            unsafe_allow_html=True)
                st.divider()
                if user["papel"] == "admin" and st.button("Usuários", key="cta_usuarios", **LARG_BTN):
                    st.session_state.pagina = "Usuários"
                    st.session_state.abrir_novo = False
                    rerun()
                if st.button("Minha conta", key="cta_conta", **LARG_BTN):
                    st.session_state.pagina = "Minha conta"
                    st.session_state.abrir_novo = False
                    rerun()
                auto = st.session_state.get("auto_atualizar", True)
                if st.button(f"Atualização automática: {'ligada' if auto else 'desligada'}",
                             key="cta_auto", **LARG_BTN):
                    st.session_state.auto_atualizar = not auto
                    st.session_state.abrir_novo = False
                    rerun()
                claro = st.session_state.tema == "claro"
                if st.button("Tema escuro" if claro else "Tema claro",
                             key="cta_tema", **LARG_BTN):
                    st.session_state.tema = "escuro" if claro else "claro"
                    st.session_state.abrir_novo = False
                    rerun()
                st.divider()
                if st.button("Sair", key="sair", **LARG_BTN):
                    encerrar_sessao(st.session_state.get("token"))
                    st.session_state.clear()
                    qp_limpar()
                    rerun()
        else:
            if st.button("Sair", key="sair"):
                encerrar_sessao(st.session_state.get("token"))
                st.session_state.clear()
                qp_limpar()
                rerun()

    # Se a página mudou desde o último desenho, o diálogo não deve reaparecer.
    if st.session_state.get("pagina_anterior") != atual:
        st.session_state.pagina_anterior = atual
        st.session_state.abrir_novo = False

    if st.session_state.get("abrir_novo") and _dialogo_nova:
        _dialogo_nova(user)

    return atual


def contagens_pendentes(user: dict) -> dict:
    """Só os números para os banners — antes as duas filas eram carregadas
    inteiras (com todas as subconsultas) a cada reexecução."""
    if user["papel"] == "admin":
        prorrog = consultar_um("SELECT COUNT(*) AS n FROM prorrogacoes "
                               "WHERE situacao = 'Pendente'")
        conclu = consultar_um("""SELECT COUNT(*) AS n FROM tarefas
                                  WHERE status = 'Realizado' AND aprovacao = 'Pendente'
                                    AND criador_id = %s""", (user["id"],))
    else:
        prorrog = consultar_um("""SELECT COUNT(*) AS n FROM prorrogacoes p
                                    JOIN tarefas t ON t.id = p.tarefa_id
                                   WHERE p.situacao = 'Pendente' AND t.criador_id = %s
                                     AND p.solicitante_id <> %s""",
                               (user["id"], user["id"]))
        conclu = consultar_um("""SELECT COUNT(*) AS n FROM tarefas
                                  WHERE status = 'Realizado' AND aprovacao = 'Pendente'
                                    AND criador_id = %s""", (user["id"],))
    return {"prorrogacoes": prorrog["n"], "conclusoes": conclu["n"]}


def alertas(tarefas: list[dict], user: dict) -> None:
    """Uma linha discreta de etiquetas, no lugar de quatro faixas empilhadas.

    Os banners do Streamlit (error/warning/info) têm padding próprio e, com
    três ou quatro deles, empurravam o quadro para fora da primeira tela. O
    conteúdo é o mesmo — número e assunto —, só que numa tira que ocupa uma
    linha e mantém a cor como código: vermelho atraso, âmbar vence hoje,
    roxo esperando você.
    """
    atrasadas = [t for t in tarefas if eh_atrasada(t)]
    minhas = [t for t in atrasadas if t.get("sou_responsavel")]
    vencem_hoje = [t for t in tarefas
                   if t["status"] != "Realizado" and t["prazo_atual"] == hoje()]
    contas = st.session_state.get("painel") or contagens_pendentes(user)

    etiquetas = []
    if atrasadas:
        alvo = minhas or atrasadas
        assunto = alvo[0]["titulo"]
        if len(assunto) > 34:
            assunto = assunto[:33] + "…"
        resto = f" +{len(alvo) - 1}" if len(alvo) > 1 else ""
        etiquetas.append((CORES["Atrasado"],
                          f"{len(atrasadas)} em atraso · {esc(assunto)}{resto}"))
    if vencem_hoje:
        etiquetas.append((CORES["Em andamento"], f"{len(vencem_hoje)} vence(m) hoje"))
    if contas["conclusoes"]:
        etiquetas.append((CORES["Aguardando"],
                          f"{contas['conclusoes']} conclusão(ões) a aprovar"))
    if contas["prorrogacoes"]:
        etiquetas.append((CORES["Aguardando"],
                          f"{contas['prorrogacoes']} prorrogação(ões) a decidir"))
    if not etiquetas:
        return

    tira = "".join(
        f"<span class='alerta-chip' style='color:{cor};background:{cor}1c;"
        f"border-color:{cor}55'>{texto}</span>" for cor, texto in etiquetas)
    st.markdown(f"<div class='tira-alertas'>{tira}</div>", unsafe_allow_html=True)


def avisar_novidades(user: dict, painel: dict) -> None:
    """Compara o maior id de notificação não lida com o da última execução.
    Se subiu, mostra o toast e pede o bipe ao componente do sino."""
    topo = painel["maior"]
    st.session_state.notif_n = painel["nao_lidas"]
    visto = st.session_state.get("notif_visto")
    if visto is None:                       # primeira carga da sessão: sem barulho
        st.session_state.notif_visto = topo
        return
    if topo > visto:
        novas = consultar("""SELECT texto FROM notificacoes
                             WHERE usuario_id = %s AND NOT lida AND id > %s
                             ORDER BY id DESC LIMIT 3""", (user["id"], visto))
        for n in novas:
            try:
                st.toast(n["texto"], icon="🔔")
            except Exception:
                st.info(n["texto"])
        st.session_state.notif_visto = topo
        if st.session_state.get("som_ativo", True):
            st.session_state.tocar_som = st.session_state.get("tocar_som", 0) + 1


# --------------------------------------------------------------------------- #
# Telas
# --------------------------------------------------------------------------- #

def tela_primeiro_acesso() -> None:
    """Aparece só enquanto a tabela de usuários estiver vazia."""
    st.markdown("<br>", unsafe_allow_html=True)
    mostrar_logo()
    _, meio, _ = st.columns([1, 1.4, 1])
    with meio:
        st.markdown("<div class='eyebrow'>Primeiro acesso</div>"
                    "<div class='titulo-app'>Criar administrador</div>"
                    "<div class='meta'>Este será o único usuário com acesso total. "
                    "Depois, ele cadastra os demais.</div><br>", unsafe_allow_html=True)
        with st.form("primeiro_acesso"):
            nome = st.text_input("Nome completo", key="pa_nome")
            login = st.text_input("Usuário (login)", key="pa_login")
            email = st.text_input("E-mail", key="pa_email")
            senha = st.text_input("Senha", type="password", key="pa_senha")
            conf = st.text_input("Confirmar senha", type="password", key="pa_conf")
            if st.form_submit_button("Criar administrador", type="primary", **LARG_FSB):
                if not (nome.strip() and login.strip()):
                    st.error("Preencha nome e usuário.")
                elif len(senha) < 6:
                    st.error("A senha precisa ter ao menos 6 caracteres.")
                elif senha != conf:
                    st.error("A confirmação não confere.")
                else:
                    criar_usuario(nome, login, email, senha, "admin")
                    st.success("Administrador criado. Faça login para continuar.")
                    rerun()


def tela_login() -> None:
    st.markdown("<br>", unsafe_allow_html=True)
    mostrar_logo()
    _, meio, _ = st.columns([1, 1.2, 1])
    with meio:
        st.markdown("<div class='eyebrow'>Plano de ação</div>"
                    "<div class='titulo-app'>Gestor de Tarefas</div><br>",
                    unsafe_allow_html=True)
        with st.form("login"):
            usuario = st.text_input("Usuário", key="log_user")
            senha = st.text_input("Senha", type="password", key="log_senha")
            if st.form_submit_button("Entrar", type="primary", **LARG_FSB):
                espera = login_bloqueado()
                if espera:
                    st.error(f"Muitas tentativas seguidas. Tente de novo em "
                             f"{espera} segundo(s).")
                else:
                    u = autenticar(usuario, senha)
                    if u:
                        st.session_state.login_falhas = 0
                        st.session_state.usuario = u
                        st.session_state.token = abrir_sessao(u["id"])
                        qp_gravar("s", st.session_state.token)
                        rerun()
                    else:
                        registrar_falha_login()
                        st.error("Usuário ou senha inválidos.")


def montar_colunas(tarefas: list[dict]) -> list[dict]:
    """Monta o quadro com um teto de cartões por coluna.

    O peso não estava mais no banco e sim no navegador: cada cartão vira DOM,
    e uma coluna com centenas deles trava a rolagem e o arrastar. O contador
    do topo continua mostrando o total real — o que é cortado é só o desenho,
    e a coluna avisa quantos ficaram de fora. Ordem é por prazo, então o que
    some é sempre o menos urgente."""
    colunas = []
    for status in STATUS_LIST:
        cards = []
        do_status = [x for x in tarefas if x["status"] == status]
        for t in do_status[:MAX_CARTOES_COLUNA]:
            atrasada = eh_atrasada(t)
            situacao, cor_prazo = situacao_prazo(t)
            acento = CORES["Atrasado"] if atrasada else CORES[status]
            resp = t["responsaveis"] or "sem responsável"
            nomes = [n.strip().split(" ")[0] for n in resp.split(",")]
            pessoas = "@" + nomes[0] + (f" +{len(nomes) - 1}" if len(nomes) > 1 else "")
            if aguardando_aprovacao(t):
                acento = CORES["Aguardando"]
            cards.append({
                "id": t["id"],
                "titulo": t["titulo"],
                "atrasada": atrasada,
                "acento": acento,
                "pessoas": pessoas if t["responsaveis"] else "sem responsável",
                "prazo": t["prazo_atual"].strftime("%d/%m"),
                "cor_prazo": cor_prazo,
                "prorrogacao": bool(t["prorrog_pendentes"]),
                "aguardando": aguardando_aprovacao(t),
                "cor_aguardando": CORES["Aguardando"],
                "anexos": t.get("qtd_anexos") or 0,
            })
        colunas.append({"nome": status, "cor": CORES[status], "cards": cards,
                        "total": len(do_status),
                        "ocultos": max(len(do_status) - MAX_CARTOES_COLUNA, 0)})
    return colunas


def form_nova_tarefa_coluna(status: str, user: dict, prazo_sugerido=None) -> None:
    """Formulário que aparece quando o + da coluna (ou do dia) é clicado."""
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    # `status` é reatribuído logo abaixo quando o + vem do calendário; as chaves
    # dos campos ficam presas ao valor de entrada para não trocarem de nome no
    # meio do formulário (widget com chave nova perde o que já foi digitado).
    base = status
    with caixa():
        titulo_form = (f"Nova tarefa com prazo em {fmt_data(prazo_sugerido)}"
                       if prazo_sugerido else f"Nova tarefa em {status}")
        st.markdown(f"<b>{titulo_form}</b>", unsafe_allow_html=True)
        with st.form(f"add_{base}", clear_on_submit=True):
            c1, c2, c3 = st.columns([3, 2, 1.4])
            titulo = c1.text_input("Atividade", key=f"add_tit_{base}")
            resp = c2.multiselect("Responsáveis (@)", list(mapa), key=f"add_resp_{base}")
            prazo = c3.date_input("Prazo", value=prazo_sugerido or hoje() + timedelta(days=7),
                                  key=f"add_prazo_{base}", **FMT_DATA)
            if prazo_sugerido:
                status = c2.selectbox("Coluna", STATUS_LIST, index=1, key="add_col_cal")
            descricao = st.text_area("Descrição (opcional)", height=90,
                                     key=f"add_desc_{base}",
                                     placeholder="Contexto, entregável esperado, links…")
            fim_em = None
            if status == "Realizado":
                fim_em = c3.date_input("Concluída em", value=hoje(),
                                       key=f"add_fim_{base}", **FMT_DATA)
            b1, b2, _ = st.columns([1, 1, 4])
            criar = b1.form_submit_button("Criar", type="primary", **LARG_FSB)
            cancelar = b2.form_submit_button("Cancelar", **LARG_FSB)
            if cancelar:
                st.session_state.nova_em = None
                st.session_state.nova_data = None
                rerun()
            if criar:
                if not titulo.strip():
                    st.error("Informe a atividade.")
                elif not resp:
                    st.error("Marque ao menos um responsável.")
                else:
                    criar_tarefa(
                        titulo, descricao, user["id"], hoje(), prazo,
                        [mapa[n] for n in resp], status,
                        datetime.combine(fim_em, datetime.min.time()) if fim_em else None)
                    st.session_state.nova_em = None
                    st.session_state.nova_data = None
                    rerun()


def form_conclusao(t: dict, user: dict, chave: str = "det", em_dialogo: bool = False) -> None:
    """Finalizar exige uma descrição — nem que seja um 'OK'. Depois disso a
    tarefa fica aguardando o aceite de quem criou.

    Dentro do diálogo, o título e a moldura vêm da própria janela: repetir
    "Finalizar tarefa" e desenhar caixa dentro de caixa só encolhia o espaço
    útil — foi o que espremeu o botão Cancelar até quebrar a palavra no meio.
    """
    sou_o_solicitante = user["id"] == t["criador_id"]
    moldura = contextlib.nullcontext() if em_dialogo else caixa()
    with moldura:
        if not em_dialogo:
            st.markdown("##### Finalizar tarefa")
        st.markdown(f"<div class='card-titulo' style='margin-top:0'>{esc(t['titulo'])}</div>",
                    unsafe_allow_html=True)
        st.markdown(
            "<div class='meta'>A tarefa fica em <b>aguardando aprovação</b>. Como você é "
            "quem pediu, o botão de aceitar aparece logo em seguida.</div>"
            if sou_o_solicitante else
            f"<div class='meta'>@{esc(t['criador'])} é notificado e decide se aceita. Até lá a "
            f"tarefa fica em <b>aguardando aprovação</b>.</div>",
            unsafe_allow_html=True)
        if t.get("aprovacao") == "Recusada" and t.get("aprovacao_obs"):
            st.warning(f"Recusada antes: {t['aprovacao_obs']}")

        with st.form(f"concluir_{chave}_{t['id']}", clear_on_submit=True):
            texto = st.text_area("O que foi entregue?", height=110,
                                 key=f"cc_txt_{chave}_{t['id']}",
                                 placeholder="Pode ser só “OK”.")
            b1, b2 = st.columns(2)          # metade a metade: rótulo não quebra
            enviar = b1.form_submit_button("Finalizar", type="primary", **LARG_FSB)
            cancelar = b2.form_submit_button("Cancelar", **LARG_FSB)
            if cancelar:
                st.session_state.concluir_id = None
                rerun()                    # dentro do diálogo, isto o fecha
            if enviar:
                if not texto.strip():
                    st.error("Escreva ao menos uma palavra — vale “OK”.")
                else:
                    enviar_conclusao(t, texto, user)
                    st.session_state.concluir_id = None
                    if sou_o_solicitante:
                        # Quem pediu já cai na tarefa para aceitar de imediato.
                        st.session_state.tarefa_sel = t["id"]
                    rerun()


def _conteudo_dialogo_conclusao(t: dict, user: dict) -> None:
    form_conclusao(t, user, chave="kanban", em_dialogo=True)


_dialogo_conclusao = (st.dialog("Finalizar tarefa")(_conteudo_dialogo_conclusao)
                      if hasattr(st, "dialog") else None)


def caixa_aprovacao(t: dict, user: dict) -> None:
    """Quem criou analisa a conclusão: aceita e fecha, ou recusa e devolve."""
    with caixa():
        st.markdown("##### Analisar conclusão")
        st.markdown(f"<div class='meta'>Finalizada por @{esc(t.get('concluiu_nome') or '—')} "
                    f"em {fmt_hora(t.get('conclusao_em'))}</div>", unsafe_allow_html=True)
        st.write(t.get("conclusao_texto") or "_Sem descrição._")
        obs = st.text_area("Observação (obrigatória para recusar)", height=70,
                           key=f"apr_obs_{t['id']}")
        c1, c2 = st.columns(2)
        if c1.button("Aceitar conclusão", type="primary", key=f"apr_ok_{t['id']}", **LARG_BTN):
            decidir_conclusao(t, True, obs, user)
            rerun()
        if c2.button("Recusar", key=f"apr_no_{t['id']}", **LARG_BTN):
            if not obs.strip():
                st.error("Diga o que falta para a tarefa poder voltar ao responsável.")
            else:
                decidir_conclusao(t, False, obs, user)
                rerun()


def form_duplicar(t: dict, user: dict) -> None:
    mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
    atuais = [n for n, i in mapa.items() if i in responsaveis_ids(t["id"])]
    dias = max((t["prazo_original"] - t["data_inicio"]).days, 0)
    with st.form(f"duplicar_{t['id']}"):
        novo_tit = st.text_input("Título da cópia", value=f"{t['titulo']} (cópia)",
                                 key=f"dup_tit_{t['id']}")
        novos = st.multiselect("Responsáveis (@)", list(mapa), default=atuais,
                               key=f"dup_resp_{t['id']}")
        c1, c2 = st.columns(2)
        inicio = c1.date_input("Data de início", value=hoje(),
                               key=f"dup_ini_{t['id']}", **FMT_DATA)
        prazo = c2.date_input("Prazo", value=hoje() + timedelta(days=dias or 7),
                              key=f"dup_prazo_{t['id']}", **FMT_DATA)
        c3, c4 = st.columns(2)
        copiar_desc = c3.checkbox("Copiar descrição", value=True, key=f"dup_desc_{t['id']}")
        copiar_anx = c4.checkbox(f"Copiar anexos ({t.get('qtd_anexos') or 0})",
                                 value=False, key=f"dup_anx_{t['id']}",
                                 disabled=not t.get("qtd_anexos"))
        if st.form_submit_button("Duplicar", type="primary", **LARG_FSB):
            if not novo_tit.strip():
                st.error("Informe o título da cópia.")
            elif not novos:
                st.error("Marque ao menos um responsável.")
            elif prazo < inicio:
                st.error("O prazo não pode ser anterior à data de início.")
            else:
                novo_id = duplicar_tarefa(t, novo_tit, inicio, prazo,
                                          [mapa[n] for n in novos], user,
                                          copiar_desc, copiar_anx)
                st.session_state.aviso = f"Tarefa #{novo_id} criada como cópia."
                st.session_state.dup_lista = None
                st.session_state.tarefa_sel = novo_id
                rerun()


def _conteudo_dialogo_duplicar(t: dict, user: dict) -> None:
    st.markdown(f"<div class='meta'>Cópia de #{t['id']} — {esc(t['titulo'])}</div>",
                unsafe_allow_html=True)
    form_duplicar(t, user)
    if st.button("Cancelar", key=f"dupx_{t['id']}", **LARG_BTN):
        st.session_state.dup_lista = None
        rerun()


_dialogo_duplicar = (st.dialog("Duplicar tarefa")(_conteudo_dialogo_duplicar)
                     if hasattr(st, "dialog") else None)


def aba_kanban(tarefas: list[dict], user: dict) -> None:
    # Arrastar para Realizado não conclui direto: abre o formulário da conclusão.
    # Em diálogo, quando a versão do Streamlit tem: embaixo do quadro ele
    # passava despercebido e parecia que o cartão só tinha voltado sozinho.
    if st.session_state.get("concluir_id"):
        alvo = next((t for t in tarefas if t["id"] == st.session_state.concluir_id), None)
        if alvo and _dialogo_conclusao:
            _dialogo_conclusao(alvo, user)
        elif alvo:
            form_conclusao(alvo, user, chave="kanban")
        else:
            st.session_state.concluir_id = None

    tema = TEMAS[st.session_state.tema]
    evento = quadro_kanban(montar_colunas(tarefas),
                           {**tema, "marca": MARCA}, key="quadro_kanban")

    if evento and evento.get("n") != st.session_state.get("ultimo_evento"):
        st.session_state.ultimo_evento = evento["n"]
        fechar_paineis()          # nada de diálogo velho reabrindo por cima
        alvo = next((t for t in tarefas if t["id"] == evento.get("id")), None)
        if alvo:
            if evento["acao"] == "mover" and evento["para"] != alvo["status"]:
                if evento["para"] == "Realizado":
                    if alvo.get("sou_responsavel") or pode_aprovar(alvo, user):
                        st.session_state.concluir_id = alvo["id"]
                    else:
                        st.session_state.aviso = ("Só um responsável pode finalizar "
                                                  "esta tarefa.", "alerta")
                    rerun()
                elif alterar_status(alvo, evento["para"], user):
                    rerun()
            elif evento["acao"] == "abrir":
                abrir_tarefa(alvo["id"], user)


def montar_calendario(tarefas: list[dict], ano: int, mes: int) -> dict:
    por_prazo: dict[date, list[dict]] = {}
    inicios: dict[date, int] = {}
    for t in tarefas:
        por_prazo.setdefault(t["prazo_atual"], []).append(t)
        inicios[t["data_inicio"]] = inicios.get(t["data_inicio"], 0) + 1

    dias = []
    for semana in calmod.Calendar(firstweekday=6).monthdatescalendar(ano, mes):
        for dia in semana:
            itens = []
            for t in por_prazo.get(dia, []):
                if aguardando_aprovacao(t):
                    cor = CORES["Aguardando"]
                elif eh_atrasada(t):
                    cor = CORES["Atrasado"]
                else:
                    cor = CORES[t["status"]]
                itens.append({
                    "id": t["id"],
                    "titulo": t["titulo"],
                    "completo": f"#{t['id']} {t['titulo']} — {t['responsaveis'] or 'sem responsável'}"
                                f" ({t['status']})",
                    "cor": cor,
                })
            dias.append({
                "data": dia.isoformat(),
                "numero": dia.day,
                "fora": dia.month != mes,
                "hoje": dia == hoje(),
                "itens": itens,
                "inicios": inicios.get(dia, 0) if dia.month == mes else 0,
            })

    return {
        "titulo": f"{MESES[mes - 1]} de {ano}",
        "semana": DIAS_SEMANA,
        "dias": dias,
        "legenda": [{"nome": s, "cor": CORES[s]} for s in STATUS_LIST]
                   + [{"nome": "Atrasada", "cor": CORES["Atrasado"]},
                      {"nome": "Aguardando aprovação", "cor": CORES["Aguardando"]}],
        "tema": {**TEMAS[st.session_state.tema], "marca": MARCA},
    }


def conteudo_dia(dia: date, tarefas: list[dict], user: dict) -> None:
    """Tudo o que vence naquele dia, sem o corte de três chips da grade."""
    do_dia = [t for t in tarefas if t["prazo_atual"] == dia]
    comeca = [t for t in tarefas if t["data_inicio"] == dia]
    st.markdown(f"<div class='titulo-app'>{dia.day} de {MESES[dia.month - 1]} de {dia.year}"
                f"</div><div class='meta'>{len(do_dia)} com prazo neste dia · "
                f"{len(comeca)} iniciando</div>", unsafe_allow_html=True)

    if not do_dia and not comeca:
        st.caption("Nada programado para este dia.")

    for t in do_dia:
        rotulo, cor = situacao_prazo(t)
        selo = (badge("Atrasada", CORES["Atrasado"]) if eh_atrasada(t)
                else badge(t["status"], CORES[t["status"]]))
        if aguardando_aprovacao(t):
            selo = badge("Aguardando aprovação", CORES["Aguardando"])
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"{selo}<div class='card-titulo'>#{t['id']} — {esc(t['titulo'])}</div>"
                    f"<div class='meta'>{tags(t['responsaveis'])}</div>"
                    f"<div class='meta' style='color:{cor}'>{rotulo}</div>",
                    unsafe_allow_html=True)
        if c2.button("Abrir", key=f"dia_ab_{t['id']}", **LARG_BTN):
            st.session_state.cal_dia = None
            abrir_tarefa(t["id"], user)
        st.divider()

    if comeca:
        st.markdown("<div class='meta'><b>Começam neste dia</b></div>", unsafe_allow_html=True)
        for t in comeca:
            st.markdown(f"<div class='meta'>#{t['id']} — {esc(t['titulo'])} "
                        f"(prazo {fmt_data(t['prazo_atual'])})</div>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    if c1.button("Nova tarefa neste dia", type="primary", key="dia_nova", **LARG_BTN):
        st.session_state.cal_dia = None
        st.session_state.nova_em = "Iniciado"
        st.session_state.nova_data = dia
        rerun()
    if c2.button("Fechar", key="dia_fechar", **LARG_BTN):
        st.session_state.cal_dia = None
        rerun()


_dialogo_dia = (st.dialog("Programação do dia")(conteudo_dia)
                if hasattr(st, "dialog") else None)


def aba_calendario(tarefas: list[dict], user: dict) -> None:
    st.session_state.setdefault("cal_ano", hoje().year)
    st.session_state.setdefault("cal_mes", hoje().month)

    if st.session_state.get("nova_em"):
        form_nova_tarefa_coluna(st.session_state.nova_em, user,
                                st.session_state.get("nova_data"))

    if st.session_state.get("cal_dia"):
        if _dialogo_dia:
            _dialogo_dia(st.session_state.cal_dia, tarefas, user)
        else:
            with caixa():
                conteudo_dia(st.session_state.cal_dia, tarefas, user)

    dados = montar_calendario(tarefas, st.session_state.cal_ano, st.session_state.cal_mes)
    evento = calendario_mes(dados, key="calendario")

    if evento and evento.get("n") != st.session_state.get("ultimo_evento"):
        st.session_state.ultimo_evento = evento["n"]
        fechar_paineis()
        if evento["acao"] == "mes":
            delta = evento["delta"]
            if delta == 0:
                st.session_state.cal_ano, st.session_state.cal_mes = hoje().year, hoje().month
            else:
                mes = st.session_state.cal_mes + delta
                ano = st.session_state.cal_ano
                if mes == 0:
                    mes, ano = 12, ano - 1
                elif mes == 13:
                    mes, ano = 1, ano + 1
                st.session_state.cal_mes, st.session_state.cal_ano = mes, ano
            rerun()
        elif evento["acao"] == "abrir":
            abrir_tarefa(evento["id"], user)
        elif evento["acao"] == "dia":
            st.session_state.cal_dia = date.fromisoformat(evento["data"])
            rerun()
        elif evento["acao"] == "novo":
            st.session_state.nova_em = "Iniciado"
            st.session_state.nova_data = date.fromisoformat(evento["data"])
            rerun()


def altura_tabela(n_linhas: int) -> int:
    """Altura em pixels para a tabela mostrar `n_linhas` sem rolagem interna.

    O padrão do Streamlit trava em ~10 linhas e obriga a rolar dentro de uma
    caixinha. Aqui a tabela cresce com o resultado até o teto — passar disso
    empurraria a barra de ações para fora da tela."""
    return min(max(n_linhas, 3) * 35 + 40, ALTURA_MAX_TABELA)


def linhas_lista(tarefas: list[dict]) -> list[dict]:
    """As colunas da tabela, na ordem em que aparecem."""
    return [{"#": t["id"],
             "Atividade": t["titulo"],
             "Responsáveis": t["responsaveis"] or "—",
             "Início": fmt_data(t["data_inicio"]),
             "Prazo": fmt_data(t["prazo_atual"]),
             "Prazo original": fmt_data(t["prazo_original"]),
             "Status": "Atrasada" if eh_atrasada(t) else t["status"],
             "Aprovação": t.get("aprovacao") or "—",
             "Situação": situacao_prazo(t)[0],
             "Anexos": t.get("qtd_anexos") or 0,
             "Abriram": f"{t['qtd_abertos']}/{t['qtd_resp']}",
             "Criador": t["criador"]}
            for t in tarefas]


def barra_acoes_lista(t: dict, user: dict) -> None:
    """O que fazer com a tarefa selecionada: abrir ou duplicar."""
    with caixa():
        c1, c2, c3 = st.columns([6, 1.3, 1.3], **ALINHA_COL)
        rotulo, cor = situacao_prazo(t)
        c1.markdown(
            f"<div class='lista-sel' title=\"{esc(t['titulo'])}\">#{t['id']} — "
            f"{esc(t['titulo'])}</div>"
            f"<div class='meta'>{esc(t['responsaveis'] or 'sem responsável')} · "
            f"prazo {fmt_data(t['prazo_atual'])} · "
            f"<span style='color:{cor};font-weight:700'>{rotulo}</span></div>",
            unsafe_allow_html=True)
        if c2.button("Abrir", key=f"lst_ab_{t['id']}", type="primary", **LARG_BTN):
            abrir_tarefa(t["id"], user)
        if pode_gerenciar(t, user):
            if c3.button("Duplicar", key=f"lst_dup_{t['id']}", **LARG_BTN):
                st.session_state.dup_lista = t["id"]
                rerun()


def aba_lista(tarefas: list[dict], user: dict) -> None:
    """Tabela completa do Streamlit — com ordenação, busca e download nativos.

    A tarefa é escolhida na própria tabela (clique na linha) quando a versão
    instalada suporta seleção; nas anteriores, pelo seletor logo abaixo. Feita
    a escolha, a barra de ações traz Abrir e Duplicar."""
    if not tarefas:
        st.info("Nenhuma tarefa encontrada com os filtros atuais.")
        return

    # Pedido de cópia: em diálogo quando a versão tem, senão numa caixa no
    # topo, que é o único lugar onde não passa despercebido.
    alvo_dup = next((t for t in tarefas
                     if t["id"] == st.session_state.get("dup_lista")), None)
    if st.session_state.get("dup_lista") and not alvo_dup:
        st.session_state.dup_lista = None
    elif alvo_dup and _dialogo_duplicar:
        _dialogo_duplicar(alvo_dup, user)
    elif alvo_dup:
        with caixa():
            st.markdown("##### Duplicar tarefa")
            st.markdown(f"<div class='meta'>Cópia de #{alvo_dup['id']} — "
                        f"{esc(alvo_dup['titulo'])}</div>", unsafe_allow_html=True)
            form_duplicar(alvo_dup, user)
            if st.button("Cancelar", key="dup_lista_cancel", **LARG_BTN):
                st.session_state.dup_lista = None
                rerun()

    dados = linhas_lista(tarefas)
    escolhida = None

    if DF_SELECAO:
        evento = st.dataframe(dados, hide_index=True, key="lista_df",
                              on_select="rerun", selection_mode="single-row",
                              height=altura_tabela(len(dados)), **LARG_DF)
        # O retorno mudou de forma entre versões (objeto com .selection num
        # lado, dicionário no outro) — os dois caminhos levam ao mesmo índice.
        selecao = getattr(evento, "selection", None)
        if selecao is None and isinstance(evento, dict):
            selecao = evento.get("selection")
        linhas = getattr(selecao, "rows", None)
        if linhas is None and isinstance(selecao, dict):
            linhas = selecao.get("rows")
        linhas = linhas or []
        if linhas and linhas[0] < len(tarefas):
            escolhida = tarefas[linhas[0]]
    else:
        st.dataframe(dados, hide_index=True,
                     height=altura_tabela(len(dados)), **LARG_DF)

    if escolhida:
        barra_acoes_lista(escolhida, user)
    elif DF_SELECAO:
        st.markdown(f"<div class='meta'>{len(tarefas)} tarefa(s) · clique numa linha "
                    "para abrir ou duplicar</div>", unsafe_allow_html=True)
    else:
        # Versões anteriores à seleção na tabela: o seletor faz o mesmo papel.
        titulos = {t["id"]: t["titulo"] for t in tarefas}
        alvo = st.selectbox("Tarefa", options=[0] + list(titulos), key="lista_sel",
                            format_func=lambda i: ("Selecione…" if i == 0
                                                   else f"#{i} — {titulos[i]}"))
        if alvo:
            barra_acoes_lista(next(t for t in tarefas if t["id"] == alvo), user)


def aba_nova_tarefa(user: dict) -> None:
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
    st.markdown("#### Conclusões aguardando sua aprovação")
    conclusoes = conclusoes_para_aprovar(user)
    if not conclusoes:
        st.caption("Nenhuma conclusão aguardando análise.")
    for t in conclusoes:
        with caixa():
            st.markdown(f"<div class='card-titulo'>#{t['id']} — {esc(t['titulo'])}</div>"
                        f"<div class='meta'>Finalizada por @{esc(t['concluiu_nome'] or '—')} em "
                        f"{fmt_hora(t['conclusao_em'])} · prazo {fmt_data(t['prazo_atual'])}</div>",
                        unsafe_allow_html=True)
            st.write(t["conclusao_texto"] or "_Sem descrição._")
            obs = st.text_area("Observação (obrigatória para recusar)", height=68,
                               key=f"pa_obs_{t['id']}")
            c1, c2, c3 = st.columns([1, 1, 3])
            if c1.button("Aceitar", key=f"pa_ok_{t['id']}", type="primary", **LARG_BTN):
                decidir_conclusao(t, True, obs, user)
                rerun()
            if c2.button("Recusar", key=f"pa_no_{t['id']}", **LARG_BTN):
                if not obs.strip():
                    st.error("Informe o que falta para devolver a tarefa.")
                else:
                    decidir_conclusao(t, False, obs, user)
                    rerun()
            if c3.button("Abrir tarefa", key=f"pa_open_{t['id']}", **LARG_BTN):
                abrir_tarefa(t["id"], user)

    st.markdown("#### Prorrogações aguardando sua decisão")
    pendentes = pedidos_para_decidir(user)
    if not pendentes:
        st.caption("Nenhum pedido pendente.")
    for p in pendentes:
        with caixa():
            st.markdown(f"<div class='card-titulo'>{esc(p['titulo'])}</div>"
                        f"<div class='meta'>Solicitado por @{esc(p['solicitante'])}</div>"
                        f"<div class='meta'>Prazo atual {fmt_data(p['prazo_anterior'])} → "
                        f"solicitado <b>{fmt_data(p['prazo_solicitado'])}</b></div>"
                        f"<div class='meta'>Justificativa: {esc(p['justificativa'] or '—')}</div>",
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
        st.markdown(f"{badge(p['situacao'], cor)} &nbsp; <b>{esc(p['titulo'])}</b> "
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
            trocar_senha(alvo["id"], nova)
            st.success(f"Senha de {alvo['nome']} redefinida — as sessões abertas "
                       "dessa pessoa foram encerradas.")


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
                # Derruba as outras sessões e abre uma nova para quem está aqui:
                # trocar a senha na máquina do escritório deve deslogar o resto.
                trocar_senha(user["id"], nova)
                st.session_state.token = abrir_sessao(user["id"])
                qp_gravar("s", st.session_state.token)
                st.success("Senha alterada. As demais sessões foram encerradas.")


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
                + (badge("Aguardando aprovação", CORES["Aguardando"]) + " &nbsp; "
                   if aguardando_aprovacao(t) else "")
                + badge(t["status"], CORES[t["status"]])
                + f"<div class='titulo-app' style='margin-top:8px'>{esc(t['titulo'])}</div>"
                + f"<div style='color:{cor};font-weight:700;margin-bottom:6px'>{rotulo}</div>",
                unsafe_allow_html=True)

    esq, dir_ = st.columns([2, 1])

    with esq:
        st.markdown("##### Descrição")
        st.write(t["descricao"] or "_Sem descrição._")

        # ---- conclusão: enviar, analisar ou apenas mostrar o que ficou ------
        if aguardando_aprovacao(t):
            if pode_aprovar(t, user):
                caixa_aprovacao(t, user)
            else:
                with caixa():
                    st.markdown("##### Conclusão enviada")
                    st.markdown(f"<div class='meta'>Aguardando o aceite de "
                                f"@{esc(t['criador'])} · "
                                f"enviada em {fmt_hora(t['conclusao_em'])}</div>",
                                unsafe_allow_html=True)
                    st.write(t["conclusao_texto"] or "_Sem descrição._")
        elif t["status"] == "Realizado" and t.get("aprovacao") == "Aprovada":
            with caixa():
                st.markdown("##### Conclusão")
                st.write(t["conclusao_texto"] or "_Sem descrição._")
                st.markdown(f"<div class='meta'>Aceita por @{esc(t.get('aprovou_nome') or '—')} "
                            f"em {fmt_hora(t.get('aprovado_em'))}</div>",
                            unsafe_allow_html=True)
        elif pode_gerenciar(t, user):
            if t.get("aprovacao") == "Recusada" and t.get("aprovacao_obs"):
                st.warning(f"Conclusão recusada por @{t.get('aprovou_nome') or '—'}: "
                           f"{t['aprovacao_obs']}")
            if st.session_state.get("concluir_id") == t["id"]:
                form_conclusao(t, user)
            elif st.button("✔ Finalizar tarefa", key=f"det_concluir_{t['id']}",
                           type="primary", **LARG_BTN):
                st.session_state.concluir_id = t["id"]
                rerun()

        # ---- anexos ----------------------------------------------------------
        # Recolhido por padrão: o bloco aberto empurrava descrição e histórico
        # para fora da tela. O total já aparece no próprio rótulo, então dá
        # para saber que há anexo sem precisar abrir.
        n_anexos = t.get("qtd_anexos") or 0
        with st.expander(f"📎 Anexos ({n_anexos})"):
            # Consulta só quando existe algo — tarefa sem anexo não gasta ida ao banco.
            lista_anexos = anexos_da_tarefa(t["id"]) if n_anexos else []
            for a in lista_anexos:
                c1, c2, c3 = st.columns([5, 1.3, 1])
                c1.markdown(f"**{esc(a['nome'])}**  \n<span class='meta'>"
                            f"{tamanho_legivel(a['tamanho'])} · "
                            f"@{esc(a['autor'] or 'sistema')} · "
                            f"{fmt_hora(a['criado_em'])}</span>", unsafe_allow_html=True)
                # O BYTEA só é lido quando alguém pede aquele arquivo — carregar
                # todos os anexos a cada rerun encheria a memória à toa.
                if st.session_state.get("baixar_anexo") == a["id"]:
                    bruto = consultar_um("SELECT conteudo FROM anexos WHERE id = %s",
                                         (a["id"],))
                    c2.download_button("Salvar", data=bytes(bruto["conteudo"]),
                                       file_name=a["nome"],
                                       mime=a["tipo"] or "application/octet-stream",
                                       key=f"dl_{a['id']}", **LARG_BTN)
                elif c2.button("Baixar", key=f"prep_{a['id']}", **LARG_BTN):
                    st.session_state.baixar_anexo = a["id"]
                    rerun()
                if (pode_editar(t, user) or a["usuario_id"] == user["id"]) and \
                   c3.button("Excluir", key=f"delanx_{a['id']}", **LARG_BTN):
                    excluir_anexo(a["id"], t["id"], user)
                    rerun()
            if not lista_anexos:
                st.caption("Nenhum arquivo anexado.")

            if pode_gerenciar(t, user):
                arquivos = st.file_uploader(
                    f"Anexar (até {MAX_ANEXO_MB} MB cada)", accept_multiple_files=True,
                    label_visibility="collapsed", key=f"upl_{t['id']}_{n_anexos}")
                if arquivos and st.button("Enviar anexos", key=f"btn_anexar_{t['id']}",
                                          type="primary", **LARG_BTN):
                    grandes = [a.name for a in arquivos
                               if len(a.getvalue()) > MAX_ANEXO_MB * 1024 * 1024]
                    if grandes:
                        st.error(f"Acima do limite: {', '.join(grandes)}")
                    else:
                        for arq in arquivos:
                            salvar_anexo(t["id"], arq, user)
                        rerun()

        if pode_gerenciar(t, user):
            with st.expander("Duplicar tarefa"):
                form_duplicar(t, user)

        if pode_editar(t, user):
            with st.expander("Editar tarefa"):
                mapa = {u["nome"]: u["id"] for u in usuarios_ativos()}
                atuais = [n for n, i in mapa.items() if i in responsaveis_ids(t["id"])]
                with st.form(f"editar_{t['id']}"):
                    novo_tit = st.text_input("Título", value=t["titulo"],
                                             key=f"ed_tit_{t['id']}")
                    nova_desc = st.text_area("Descrição", value=t["descricao"] or "",
                                             height=110, key=f"ed_desc_{t['id']}")
                    novos = st.multiselect("Responsáveis (@)", list(mapa), default=atuais,
                                           key=f"ed_resp_{t['id']}")
                    novo_ini = st.date_input("Data de início", value=t["data_inicio"],
                                             key=f"ed_ini_{t['id']}", **FMT_DATA)
                    if st.form_submit_button("Salvar alterações", type="primary", **LARG_FSB):
                        if not novo_tit.strip():
                            st.error("O título não pode ficar vazio.")
                        elif not novos:
                            st.error("A tarefa precisa de ao menos um responsável.")
                        else:
                            atualizar_tarefa(t, novo_tit, nova_desc, novo_ini,
                                             [mapa[n] for n in novos], user)
                            rerun()

            with st.expander("Excluir tarefa"):
                st.warning("A exclusão apaga responsáveis, prorrogações e histórico da tarefa.")
                if st.checkbox("Confirmo a exclusão", key=f"conf_del_{t['id']}") and \
                   st.button("Excluir definitivamente", key=f"btn_del_{t['id']}",
                             **LARG_BTN):
                    excluir_tarefa(t["id"])
                    st.session_state.tarefa_sel = None
                    rerun()

        # Trilha longa não precisa vir inteira para a tela: os 30 últimos
        # respondem a pergunta "o que aconteceu agora", e o total fica no rótulo.
        eventos = consultar("""SELECT h.*, u.nome FROM historico h
                               LEFT JOIN usuarios u ON u.id = h.usuario_id
                               WHERE h.tarefa_id = %s ORDER BY h.id DESC LIMIT 30""", (tid,))
        ultimo = eventos[0] if eventos else None
        if ultimo:
            st.markdown(f"<div class='meta'>Última movimentação: "
                        f"<b>{esc(ultimo['acao'])}</b> — {esc(ultimo['nome'] or 'Sistema')} · "
                        f"{fmt_hora(ultimo['criado_em'])}</div>",
                        unsafe_allow_html=True)
        rotulo_hist = (f"Histórico (últimos {len(eventos)})" if len(eventos) == 30
                       else f"Histórico ({len(eventos)} registro(s))")
        with st.expander(rotulo_hist):
            for e in eventos:
                st.markdown(f"**{esc(e['acao'])}** — {esc(e['nome'] or 'Sistema')} · "
                            f"{fmt_hora(e['criado_em'])}  \n"
                            f"<span class='meta'>{esc(e['detalhe'] or '')}</span>",
                            unsafe_allow_html=True)

        with st.form(f"comentario_{tid}", clear_on_submit=True):
            texto = st.text_area("Adicionar comentário ao histórico", height=80,
                                 key=f"det_coment_{tid}")
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
            st.markdown(f"<div class='meta'>Criada por @{esc(t['criador'])}</div>",
                        unsafe_allow_html=True)
            for r in consultar("""SELECT u.nome, tr.aberto_em FROM tarefa_responsaveis tr
                                  JOIN usuarios u ON u.id = tr.usuario_id
                                  WHERE tr.tarefa_id = %s ORDER BY u.nome""", (tid,)):
                if r["aberto_em"]:
                    st.markdown(f"**@{esc(r['nome'])}**  \n"
                                f"<span class='meta'>abriu em {fmt_hora(r['aberto_em'])}</span>",
                                unsafe_allow_html=True)
                else:
                    st.markdown(f"**@{esc(r['nome'])}**  \n"
                                f"<span class='meta'>ainda não abriu</span>",
                                unsafe_allow_html=True)

        if pode_gerenciar(t, user):
            with caixa():
                st.markdown("##### Gerenciar")
                # Realizado não é escolha de selectbox: passa pela conclusão.
                opcoes = [s for s in STATUS_LIST if s != "Realizado"]
                atual = t["status"] if t["status"] in opcoes else opcoes[0]
                novo = st.selectbox("Status", opcoes, index=opcoes.index(atual),
                                    key=f"det_status_{t['id']}")
                if st.button("Salvar status", type="primary",
                             key=f"det_salvar_{t['id']}", **LARG_BTN):
                    if alterar_status(t, novo, user):
                        rerun()
                    else:
                        st.caption("Nada a alterar.")
                st.caption("Para marcar como Realizado, use “Finalizar tarefa”.")

                pendente = consultar_um("SELECT * FROM prorrogacoes WHERE tarefa_id = %s "
                                        "AND situacao = 'Pendente'", (tid,))
                if pendente:
                    st.info(f"Pedido pendente para {fmt_data(pendente['prazo_solicitado'])}.")
                else:
                    with st.expander("Pedir prorrogação"):
                        with st.form(f"prorrogar_{t['id']}"):
                            novo_prazo = st.date_input(
                                "Novo prazo", value=t["prazo_atual"] + timedelta(days=7),
                                key=f"pr_prazo_{t['id']}", **FMT_DATA)
                            just = st.text_area("Justificativa", height=80,
                                                key=f"pr_just_{t['id']}")
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

def mostrar_aviso() -> None:
    """Mensagem de uma execução só — some no próximo clique."""
    aviso = st.session_state.pop("aviso", None)
    if not aviso:
        return
    texto, tipo = aviso if isinstance(aviso, tuple) else (aviso, "ok")
    (st.warning if tipo == "alerta" else st.success)(texto)


def rodape_sino(ativo: bool) -> None:
    """Mantém o componente do sino montado: ele toca o bipe e, quando `ativo`,
    devolve um tique periódico que faz o Streamlit reexecutar e buscar o que
    chegou de novo."""
    intervalo = SEG_ATUALIZACAO if (ativo and st.session_state.get("auto_atualizar", True)) else 0
    resposta = sino_alerta(st.session_state.get("tocar_som", 0), intervalo, key="sino")
    if resposta and resposta.get("tique") and \
       resposta["tique"] != st.session_state.get("ultimo_tique"):
        st.session_state.ultimo_tique = resposta["tique"]
        rerun()


def aplicar_filtros(tarefas: list[dict], pagina: str) -> list[dict]:
    """Filtros dentro de um painel suspenso, ao lado do menu."""
    if pagina not in ("Kanban", "Calendário", "Lista"):
        return tarefas

    with st.session_state.filtro_slot:
        abridor = (st.popover("⚲", **LARG_BTN)
                   if hasattr(st, "popover") else st.expander("Filtros"))
        with abridor:
            status_sel = st.multiselect("Status", STATUS_LIST, default=STATUS_LIST,
                                        key="f_status")
            so_atrasadas = st.checkbox("Somente atrasadas", key="f_atrasadas")
            busca = st.text_input("Buscar no título", key="f_busca")

    saida = [t for t in tarefas if t["status"] in status_sel]
    if so_atrasadas:
        saida = [t for t in saida if eh_atrasada(t)]
    if busca.strip():
        termo = busca.strip().lower()
        saida = [t for t in saida if termo in t["titulo"].lower()]
    return saida


def main() -> None:
    st.set_page_config(page_title="Gestor de Tarefas", page_icon="🗂️",
                       layout="wide", initial_sidebar_state="collapsed")
    st.session_state.setdefault("tema", "escuro")
    st.session_state.setdefault("pagina", "Kanban")
    st.session_state["_memo"] = {}      # memória curta: vale só esta passada
    st.markdown(montar_css(st.session_state.tema), unsafe_allow_html=True)

    try:
        init_db()
    except ErroBanco as erro:
        cfg = config_db()
        st.error("**Não foi possível conectar ao PostgreSQL.**")
        st.code(str(erro).strip())
        st.caption(f"Tentando {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']} — "
                   "confira .streamlit/secrets.toml ou as variáveis PGHOST/PGPORT/"
                   "PGDATABASE/PGUSER/PGPASSWORD.")
        st.stop()

    # Retoma a sessão pelo token da URL (sobrevive ao F5).
    if banco_vazio():
        tela_primeiro_acesso()
        return

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

    painel = painel_usuario(st.session_state.usuario)
    user = painel.get("usuario")
    if not user:
        encerrar_sessao(st.session_state.get("token"))
        st.session_state.clear()
        qp_limpar()
        rerun()
    st.session_state.usuario = user
    st.session_state.setdefault("tarefa_sel", None)
    st.session_state.setdefault("concluir_id", None)

    st.session_state.painel = painel       # a mesma consulta serve a tela inteira
    avisar_novidades(user, painel)

    if st.session_state.tarefa_sel:
        st.session_state.abrir_novo = False
        mostrar_aviso()
        tela_detalhe(st.session_state.tarefa_sel, user)
        rodape_sino(ativo=False)
        return

    paginas = ["Kanban", "Calendário", "Lista", "Nova tarefa", "Aprovações"]

    # O cabeçalho vem primeiro para sabermos a página: telas como Usuários,
    # Minha conta ou Nova tarefa não mostram tarefa nenhuma, e carregar o
    # quadro inteiro para elas era ida ao banco jogada fora.
    pagina = cabecalho(user, paginas)
    telas_com_quadro = ("Kanban", "Calendário", "Lista")

    if pagina in telas_com_quadro:
        tarefas = listar_tarefas(user)
        filtradas = aplicar_filtros(tarefas, pagina)
        mostrar_aviso()
        alertas(tarefas, user)
    else:
        tarefas = filtradas = []
        mostrar_aviso()
        alertas([], user)

    if pagina == "Kanban":
        aba_kanban(filtradas, user)
    elif pagina == "Calendário":
        aba_calendario(filtradas, user)
    elif pagina == "Lista":
        aba_lista(filtradas, user)
    elif pagina == "Nova tarefa":
        aba_nova_tarefa(user)
    elif pagina == "Aprovações":
        aba_prorrogacoes(user)
    elif pagina == "Usuários":
        aba_usuarios(user)
    elif pagina == "Minha conta":
        aba_conta(user)

    # Atualização automática só nas telas de leitura: em formulário, um rerun
    # no meio da digitação seria um estorvo.
    rodape_sino(ativo=pagina in ("Kanban", "Calendário", "Lista"))


if __name__ == "__main__":
    main()