# 🗂️ Gestor de Tarefas — Kanban + Calendário (PostgreSQL)

Plano de ação rápido: **o quê** (título), **quem** (@responsáveis), **quando** (início e prazo) e **descrição** com histórico completo.

---

## 1. Preparar o banco

```sql
-- no psql ou pgAdmin
CREATE DATABASE tarefas;
```

O app cria as tabelas sozinho no primeiro start. Se preferir criar na mão, rode o `schema.sql`.

## 2. Configurar a conexão

**Opção A — variáveis de ambiente** (Windows / PowerShell):

```powershell
$env:PGHOST="localhost"; $env:PGPORT="5432"
$env:PGDATABASE="tarefas"; $env:PGUSER="postgres"; $env:PGPASSWORD="sua_senha"
```

**Opção B — arquivo `.streamlit/secrets.toml`** (tem prioridade): copie o `secrets.toml.exemplo`, renomeie e preencha.

## 3. Rodar

```bash
pip install -r requirements.txt
streamlit run app.py
```

> **Primeiro acesso:** `admin` / `admin123` — troque a senha em **Minha conta**.

Se a conexão falhar, o app mostra o erro do PostgreSQL e o host/porta/base que tentou usar, em vez de quebrar.

---

## Regras implementadas

| Requisito | Como funciona |
|---|---|
| **Navegação** | Menu recolhido (☰) no topo: Kanban · Calendário · Lista · Nova tarefa · Aprovações · Usuários · Minha conta |
| **Visualizações** | Kanban com arrastar-e-soltar entre colunas, Calendário mensal (tarefa cai no dia do prazo) e Lista/tabela |
| **Criar e editar** | Botão "+ Adicionar" no topo de cada coluna do Kanban, ou a aba "Nova tarefa" para o formulário completo. No detalhe, criador ou admin edita ou exclui |
| **Status** | Backlog → Iniciado → Em andamento → Realizado (com `CHECK` no banco) |
| **Conclusão** | Finalizar **exige uma descrição** (pode ser só "OK"). A tarefa vai para Realizado como **Pendente** e só fecha de fato quando o criador aceita. Recusa devolve para Em andamento com a observação |
| **Anexos** | Vários arquivos por tarefa, até 10 MB cada, guardados no próprio Postgres (`BYTEA`) |
| **Duplicar** | No detalhe: copia título, responsáveis, descrição e (opcional) anexos para uma tarefa nova em Iniciado |
| **Notificações** | Sino no topo com contador, `toast` na tela e bipe sintetizado no navegador. Dispara ao marcar alguém, mover, anexar, finalizar, aprovar/recusar e pedir prorrogação |
| **Atraso** | Automático: `prazo_atual < hoje` e status ≠ Realizado. Card fica vermelho e o topo mostra alerta |
| **Alertas** | Banner de atrasadas, de "vencem hoje", de conclusões a aprovar e de prorrogações aguardando decisão |
| **Abrir** | O responsável clica em **Abrir** → grava data/hora. O card mostra `👁️ 2/3 abriram` e o detalhe mostra quem abriu e quando |
| **Prorrogação** | Responsável pede novo prazo + justificativa → criador/admin aprova ou recusa. `prazo_original` **nunca** é alterado |
| **Visibilidade** | Admin vê tudo. Usuário comum só vê tarefas **que criou** ou **em que foi marcado (@)** — filtro no `WHERE` do SQL, não na tela |
| **Usuários** | Só o admin cadastra, ativa/desativa e redefine senhas |
| **Histórico** | Trilha de auditoria de tudo + comentários livres |

---

## Modelo de dados

| Tabela | Papel |
|---|---|
| `usuarios` | login, perfil (`admin`/`usuario`), senha em PBKDF2-SHA256 com salt individual |
| `tarefas` | título, descrição, criador, `data_inicio`, `prazo_original`, `prazo_atual`, status + o bloco da conclusão (`conclusao_texto`, `conclusao_por`, `conclusao_em`, `aprovacao`, `aprovado_por`, `aprovado_em`, `aprovacao_obs`) |
| `tarefa_responsaveis` | vínculo @ + `aberto_em` (quem abriu e quando) |
| `prorrogacoes` | prazo anterior, prazo solicitado, justificativa, situação, quem decidiu |
| `anexos` | arquivo em `BYTEA` + nome, tipo MIME, tamanho e quem enviou |
| `notificacoes` | destinatário, tarefa, tipo, texto e se já foi lida |
| `historico` | auditoria e comentários |

Índices em `tarefas(prazo_atual)`, `tarefas(status)`, `tarefa_responsaveis(usuario_id)`, `historico(tarefa_id)`, `prorrogacoes(situacao)`, `anexos(tarefa_id)` e `notificacoes(usuario_id, lida, id)`.

> As colunas da conclusão entram por `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` no start, então bancos antigos sobem sem intervenção. Tarefas que já estavam em Realizado são marcadas como `Aprovada` na migração — senão o quadro antigo apareceria inteiro "aguardando aprovação".

---

## Notas técnicas

- **Estrutura:** `app.py` (interface e regras) + `db.py` (acesso ao PostgreSQL).
- **Pool de conexões** (`ThreadedConnectionPool`, 1–10) vive como variável de módulo em `db.py`. O Streamlit reexecuta o script principal a cada interação, mas módulos importados ficam no `sys.modules` — então o pool nasce uma vez por processo, sem `@st.cache_resource` e sem vazar conexões.
- **Compatibilidade de versão:** `width="stretch"` só existe a partir da 1.49 — antes disso `width` era em pixels. O app decide pela versão instalada (`st.__version__`), não pela assinatura da função, e cai para `use_container_width` nas versões anteriores. Testado nas 1.41.1 e 1.61.1.
- **Temas claro e escuro:** paleta derivada da logo (verdes). A troca fica no menu recolhido e vale para a sessão. O `config.toml` define o tema base dos campos nativos do Streamlit — deixe-o em `dark` se o uso principal for o escuro.
- **Sessão:** ao entrar, o app grava um token na tabela `sessoes` e o coloca na URL (`?s=…`). Atualizar a página não desloga; a sessão só cai em Sair ou após 8 horas sem uso.
- **Navegação:** menu recolhido (☰) no lugar das abas.
- **Quadro:** componente próprio em `quadro/index.html` (HTML + JS puro, sem build). Arrastar entre colunas e **duplo clique** para abrir o detalhe. Fala com o Python pelo protocolo de componentes do Streamlit (`componentReady`, `setComponentValue`, `setFrameHeight`), então não há dependência externa. Os cartões são `div` com classe própria — estilizar containers do Streamlit por `data-testid` não funciona entre versões (na 1.41 o mesmo seletor envolve todo bloco vertical, não só os com borda). Soltar em **Realizado** não move: abre o formulário da conclusão e o cartão volta no redesenho.
- **Sino:** `sino/index.html`, componente sem interface (altura 0). Sintetiza o bipe pela Web Audio API — nada de arquivo de áudio para hospedar — e devolve um tique a cada 60 s, que é o que faz o Streamlit reexecutar e buscar notificações novas. O tique só roda em Kanban/Calendário/Lista: um rerun no meio da digitação de um formulário seria um estorvo. Dá para desligar som e atualização automática no menu da conta.
- **Áudio e o navegador:** som só toca depois de alguma interação do usuário na página. O componente tenta destravar no primeiro clique, então o primeiro alerta de uma sessão recém-aberta pode sair mudo — o aviso visual aparece sempre.
- **Anexos:** ficam no Postgres, não em disco. O sistema de arquivos do Streamlit Cloud é volátil e sumiria a cada redeploy. O `BYTEA` só é lido quando alguém pede aquele arquivo específico, para não carregar todos os blobs a cada rerun.
- **Datas** trafegam como `date`/`datetime` nativos (colunas `DATE`/`TIMESTAMP`), sem conversão de string.

## Próximos passos possíveis

- Notificação por e-mail/WhatsApp ao marcar alguém e ao vencer o prazo
- Dashboard de produtividade (no prazo × atrasadas por pessoa)
- Subtarefas / checklist
- Prévia de imagens e PDFs anexados direto na tela do detalhe