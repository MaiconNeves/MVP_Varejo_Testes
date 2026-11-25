import os
import sqlite3
import urllib.parse
from datetime import datetime
import pandas as pd
import requests
import streamlit as st

try:
    import psycopg2
    TEM_POSTGRES = True
except ImportError:
    TEM_POSTGRES = False

DB_LOCAL_PATH = "varejo_inteligente_multiloja.db"


def _get_database_url() -> str | None:
    """Busca a URL do Supabase nos Secrets do Streamlit ou variáveis de ambiente."""
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return os.environ.get("DATABASE_URL")


def _conectar():
    """Retorna (conexao, tipo_banco) onde tipo_banco é 'postgres' ou 'sqlite'."""
    db_url = _get_database_url()
    if not db_url:
        st.warning("⚠️ Debug: DATABASE_URL não foi encontrada nos Secrets.")
    elif not TEM_POSTGRES:
        st.warning("⚠️ Debug: O pacote psycopg2 não foi carregado (TEM_POSTGRES = False).")
    else:
        try:
            conn = psycopg2.connect(db_url)
            return conn, "postgres"
        except Exception as e:
            st.error(f"❌ Erro ao conectar no Supabase: {e}")

    return sqlite3.connect(DB_LOCAL_PATH), "sqlite"


def inicializar_banco():
    """Cria as tabelas Multi-Tenant (separadas por loja_id) no Supabase ou SQLite."""
    conn, tipo = _conectar()
    cursor = conn.cursor()

    if tipo == "postgres":
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS lojas (
            loja_id TEXT PRIMARY KEY,
            nome_fantasia TEXT NOT NULL,
            responsavel TEXT,
            telefone_whatsapp TEXT NOT NULL,
            perfil TEXT DEFAULT 'Pequeno',
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS snapshots_estoque (
            id SERIAL PRIMARY KEY,
            loja_id TEXT NOT NULL REFERENCES lojas(loja_id),
            data_processamento TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sku TEXT NOT NULL,
            descricao TEXT,
            categoria TEXT,
            curva_abc TEXT,
            saldo_atual DOUBLE PRECISION,
            venda_dia DOUBLE PRECISION,
            sugestao_compra DOUBLE PRECISION,
            situacao TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_alertas (
            id SERIAL PRIMARY KEY,
            loja_id TEXT NOT NULL REFERENCES lojas(loja_id),
            telefone_destino TEXT NOT NULL,
            tipo_alerta TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            canal TEXT DEFAULT 'WhatsApp Link/API',
            enviado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS lojas (
            loja_id TEXT PRIMARY KEY,
            nome_fantasia TEXT NOT NULL,
            responsavel TEXT,
            telefone_whatsapp TEXT NOT NULL,
            perfil TEXT DEFAULT 'Pequeno',
            ativo INTEGER DEFAULT 1,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS snapshots_estoque (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loja_id TEXT NOT NULL,
            data_processamento DATETIME DEFAULT CURRENT_TIMESTAMP,
            sku TEXT NOT NULL,
            descricao TEXT,
            categoria TEXT,
            curva_abc TEXT,
            saldo_atual REAL,
            venda_dia REAL,
            sugestao_compra REAL,
            situacao TEXT
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loja_id TEXT NOT NULL,
            telefone_destino TEXT NOT NULL,
            tipo_alerta TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            canal TEXT DEFAULT 'WhatsApp Link/API',
            enviado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

    conn.commit()
    conn.close()


def listar_lojas_ativas() -> pd.DataFrame:
    """Retorna todas as lojas ativas cadastradas no sistema."""
    conn, _ = _conectar()
    try:
        df = pd.read_sql_query(
            "SELECT loja_id, nome_fantasia, responsavel, telefone_whatsapp, perfil FROM lojas ORDER BY nome_fantasia",
            conn,
        )
    except Exception:
        df = pd.DataFrame(columns=["loja_id", "nome_fantasia", "responsavel", "telefone_whatsapp", "perfil"])
    finally:
        conn.close()
    return df


def salvar_loja(loja_id: str, nome_fantasia: str, responsavel: str, telefone: str, perfil: str):
    """Cadastra ou atualiza um cliente (loja_id único) no banco permanente."""
    conn, tipo = _conectar()
    cursor = conn.cursor()
    ph = "%s" if tipo == "postgres" else "?"

    cursor.execute(f"""
    INSERT INTO lojas (loja_id, nome_fantasia, responsavel, telefone_whatsapp, perfil, atualizado_em)
    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, CURRENT_TIMESTAMP)
    ON CONFLICT(loja_id) DO UPDATE SET
        nome_fantasia = excluded.nome_fantasia,
        responsavel = excluded.responsavel,
        telefone_whatsapp = excluded.telefone_whatsapp,
        perfil = excluded.perfil,
        atualizado_em = CURRENT_TIMESTAMP
    """, (loja_id.strip().upper(), nome_fantasia.strip(), responsavel.strip(), telefone.strip(), perfil))

    conn.commit()
    conn.close()


def salvar_snapshot_indicadores(loja_id: str, df_ind: pd.DataFrame):
    """Grava o fechamento atual dos SKUs isolado pelo loja_id do cliente."""
    if df_ind is None or df_ind.empty:
        return

    conn, tipo = _conectar()
    cursor = conn.cursor()
    ph = "%s" if tipo == "postgres" else "?"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    id_limpo = loja_id.strip().upper()

    registros = []
    for _, row in df_ind.iterrows():
        saldo = float(row.get("Saldo_Calculado", 0.0))
        dmd = float(row.get("DMD", 0.0))
        cob_alvo = float(row.get("Cob_Alvo_Aplicada", 15.0))
        sugestao = max(0.0, round((dmd * cob_alvo) - saldo, 0)) if saldo < (dmd * cob_alvo) else 0.0
        situacao = "Ruptura Hoje" if saldo <= 0 else ("Estoque Baixo" if float(row.get("Cobertura_Dias", 99)) < 7 else "Normal")

        registros.append((
            id_limpo,
            agora,
            str(row.get("SKU", "")),
            str(row.get("Descrição", "")),
            str(row.get("Categoria", "Geral")),
            str(row.get("Curva_ABC", "C")),
            saldo,
            dmd,
            sugestao,
            situacao,
        ))

    cursor.executemany(f"""
    INSERT INTO snapshots_estoque
    (loja_id, data_processamento, sku, descricao, categoria, curva_abc, saldo_atual, venda_dia, sugestao_compra, situacao)
    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
    """, registros)

    conn.commit()
    conn.close()


def registrar_envio_alerta(loja_id: str, telefone: str, tipo_alerta: str, mensagem: str, canal: str):
    """Registra o envio vinculado exclusivamente ao loja_id do cliente."""
    conn, tipo = _conectar()
    cursor = conn.cursor()
    ph = "%s" if tipo == "postgres" else "?"

    cursor.execute(f"""
    INSERT INTO historico_alertas (loja_id, telefone_destino, tipo_alerta, mensagem, canal)
    VALUES ({ph}, {ph}, {ph}, {ph}, {ph})
    """, (loja_id.strip().upper(), telefone, tipo_alerta, mensagem, canal))

    conn.commit()
    conn.close()


def montar_mensagem_whatsapp(nome_loja: str, responsavel: str, df_ind: pd.DataFrame, tipo_msg: str) -> str:
    """Gera mensagens curtas e objetivas personalizadas para o cliente selecionado."""
    saudacao = f"Olá *{responsavel}*!" if responsavel else "Olá!"
    df = df_ind.copy()

    # Blindagem caso alguma coluna calculada ainda não exista no DataFrame
    for col in ["Saldo_Calculado", "DMD", "Total_Saidas"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    if "Curva_ABC" not in df.columns:
        df["Curva_ABC"] = "A"

    if tipo_msg == "🚨 Ruptura Crítica (Curva A)":
        criticos = df[df["Saldo_Calculado"] <= 0].sort_values(by=["Curva_ABC", "DMD"], ascending=[True, False]).head(6)
        if criticos.empty:
            return f"✅ *{nome_loja.upper()}* — Ótima notícia! Nenhum item da sua Curva A está em ruptura hoje. 🚀"

        linhas = [
            f"🚨 *ALERTA DE RUPTURA — {nome_loja.upper()}*",
            f"{saudacao} Estes itens campeões de venda estão *ZERADOS / EM RUPTURA* agora:\n"
        ]
        for _, r in criticos.iterrows():
            dmd = float(r["DMD"])
            sug = int(max(1, round(dmd * 15 - float(r["Saldo_Calculado"]))))
            linhas.append(f"🛑 *{r.get('Descrição', r.get('SKU', 'Produto'))}* _(Curva {r.get('Curva_ABC', 'A')})_")
            linhas.append(f"   • Venda média: {dmd:.1f}/dia | Sugestão compra: *{sug} un*")
        linhas.append("\n💡 _Priorize a reposição desses itens para não perder venda hoje!_")
        return "\n".join(linhas)

    else:
        total_skus = len(df)
        em_ruptura = int((df["Saldo_Calculado"] <= 0).sum())
        parados = int(((df["Saldo_Calculado"] > 0) & (df["Total_Saidas"] == 0)).sum())
        top_venda = df.sort_values(by="Total_Saidas", ascending=False).head(3)

        linhas = [
            f"📊 *RESUMO DIÁRIO — {nome_loja.upper()}*",
            f"{saudacao} Confira o raio-X rápido do seu estoque hoje:\n",
            f"• SKUs monitorados: *{total_skus}*",
            f"• 🛑 Em Ruptura (Comprar urgente): *{em_ruptura} itens*",
            f"• ⚠️ Parados (Conferir gôndola): *{parados} itens*\n",
            "🏆 *Mais vendidos do período:*"
        ]
        for _, r in top_venda.iterrows():
            linhas.append(f"   - {r['Descrição']} ({int(r['Total_Saidas'])} un)")
        linhas.append("\n📲 _Responda *DETALHES* se quiser receber a lista completa de compras._")
        return "\n".join(linhas)


def disparar_evolution_api(url_base: str, instancia: str, api_key: str, telefone: str, mensagem: str) -> tuple[bool, str]:
    """Envia mensagem automaticamente via Evolution API."""
    numero_limpo = "".join(filter(str.isdigit, telefone))
    if not numero_limpo.startswith("55"):
        numero_limpo = "55" + numero_limpo

    endpoint = f"{url_base.rstrip('/')}/message/sendText/{instancia.strip()}"
    headers = {"apikey": api_key.strip(), "Content-Type": "application/json"}
    payload = {"number": numero_limpo, "text": mensagem}

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return True, "Mensagem enviada automaticamente via API!"
        return False, f"Erro HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, f"Falha na conexão com API: {e}"


def renderizar_aba_whatsapp_db(loja_nome_padrao: str = "Loja 01 - Matriz"):
    inicializar_banco()
    _, tipo_banco = _conectar()

    st.header("📲 Central Multi-Clientes (WhatsApp & Banco na Nuvem)")
    if tipo_banco == "postgres":
        st.success("🟢 Conectado ao **Banco Multi-Tenant Permanente (Supabase / PostgreSQL)**.")
    else:
        st.warning("🟡 Modo local (SQLite). Configure `DATABASE_URL` nos Secrets para persistência permanente na nuvem.")

    # --- SELETOR DE CLIENTE CADASTRADO OU NOVO ---
    df_lojas = listar_lojas_ativas()
    opcoes_clientes = ["➕ Cadastrar Nova Loja / Cliente"]
    mapa_lojas = {}

    for _, row_l in df_lojas.iterrows():
        label = f"{row_l['loja_id']} — {row_l['nome_fantasia']}"
        opcoes_clientes.append(label)
        mapa_lojas[label] = row_l

    cliente_sel = st.selectbox("🏢 Selecione o Cliente / Mercado:", opcoes_clientes)

    if cliente_sel in mapa_lojas:
        dados_cli = mapa_lojas[cliente_sel]
        id_padrao = str(dados_cli["loja_id"])
        nome_padrao = str(dados_cli["nome_fantasia"])
        resp_padrao = str(dados_cli["responsavel"] or "")
        tel_padrao = str(dados_cli["telefone_whatsapp"] or "")
        perf_padrao = str(dados_cli["perfil"] or "Pequeno")
    else:
        proximo_num = len(df_lojas) + 1
        id_padrao = f"MERC_{proximo_num:03d}"
        nome_padrao = loja_nome_padrao
        resp_padrao = ""
        tel_padrao = ""
        perf_padrao = "Pequeno"

    st.subheader("1. Cadastro e Gravação Isolada por Cliente")
    c0, c1, c2, c3, c4 = st.columns([1.3, 2.2, 1.8, 1.8, 1.3])
    loja_id = c0.text_input("ID / CNPJ Único:", value=id_padrao, key="wpp_loja_id")
    nome_loja = c1.text_input("Nome do Mercado:", value=nome_padrao, key="wpp_loja")
    responsavel = c2.text_input("Dono / Comprador:", value=resp_padrao, placeholder="Ex: Seu José", key="wpp_resp")
    telefone = c3.text_input("WhatsApp (com DDD):", value=tel_padrao, placeholder="15999999999", key="wpp_tel")
    perfil = c4.selectbox("Perfil:", ["Pequeno", "Médio"], index=0 if perf_padrao == "Pequeno" else 1, key="wpp_perfil")

    df_cons = st.session_state.get("df_xml_consolidado", pd.DataFrame())

    col_b1, col_b2 = st.columns(2)
    if col_b1.button("💾 Salvar Cadastro do Cliente", use_container_width=True):
        if not (loja_id.strip() and nome_loja.strip() and telefone.strip()):
            st.error("Preencha ID/CNPJ, Nome do Mercado e WhatsApp com DDD.")
        else:
            salvar_loja(loja_id, nome_loja, responsavel, telefone, perfil)
            st.success(f"✅ Cliente **{loja_id} — {nome_loja}** salvo no banco!")
            st.rerun()

    if col_b2.button("📊 Salvar Snapshot de Estoque deste Cliente", type="primary", use_container_width=True):
        if df_cons.empty:
            st.warning("Processe os XMLs na Etapa 2 antes de gravar o snapshot.")
        elif not (loja_id.strip() and telefone.strip()):
            st.error("Salve os dados do cliente (ID e WhatsApp) antes de gravar o snapshot.")
        else:
            salvar_loja(loja_id, nome_loja, responsavel, telefone, perfil)
            salvar_snapshot_indicadores(loja_id, df_cons)
            st.success(f"✅ Snapshot de **{len(df_cons)} SKUs** gravado exclusivamente para **{loja_id} — {nome_loja}**!")

    if df_cons.empty:
        st.info("⚠️ Processe os XMLs na **Etapa 2** para liberar a geração e o envio das mensagens.")
        return

    st.divider()

    # --- BLOCO 2: GERADOR E DISPARO PERSONALIZADO ---
    st.subheader(f"2. Disparo de Alerta — {nome_loja}")
    tipo_alerta = st.radio(
        "Escolha o tipo de alerta:",
        options=[
            "🚨 Ruptura Crítica (Curva A)",
            "📦 Estoque Parado / Gôndola",
            "📊 Resumo Executivo do Dia",
        ],
        horizontal=True,
    )

    df_fonte = st.session_state.get("df_indicadores_calculados", df_cons)
    texto_gerado = montar_mensagem_whatsapp(nome_loja, responsavel, df_fonte, tipo_alerta)

    col_msg, col_envio = st.columns([1.4, 1])

    with col_msg:
        mensagem_final = st.text_area(
            "Pré-visualização da Mensagem (editável):",
            value=texto_gerado,
            height=240,
        )

    with col_envio:
        st.markdown("### 🚀 Enviar Agora")
        num_limpo = "".join(filter(str.isdigit, telefone))
        if num_limpo:
            if not num_limpo.startswith("55"):
                num_limpo = "55" + num_limpo
            texto_url = urllib.parse.quote(mensagem_final)
            link_wa = f"https://wa.me/{num_limpo}?text={texto_url}"

            st.link_button(
                "📲 Abrir e Enviar no WhatsApp Agora",
                url=link_wa,
                type="primary",
                use_container_width=True,
            )
            if st.button("✅ Registrar Envio no Histórico", use_container_width=True):
                registrar_envio_alerta(loja_id, num_limpo, tipo_alerta, mensagem_final, "WhatsApp Web/App")
                st.success("Alerta registrado no histórico deste cliente!")
        else:
            st.warning("Informe o **WhatsApp com DDD** acima para liberar o envio.")

        with st.expander("⚙️ Envio Automático em Segundo Plano (Evolution API)"):
            api_url = st.text_input("URL da API:", placeholder="https://sua-api.com")
            api_inst = st.text_input("Instância:", placeholder="varejo_bot")
            api_key = st.text_input("API Key / Token:", type="password")
            if st.button("Disparar via API Automática", use_container_width=True):
                if not (api_url and api_inst and api_key and num_limpo):
                    st.error("Preencha URL, Instância, Token e WhatsApp do cliente.")
                else:
                    ok, retorno = disparar_evolution_api(api_url, api_inst, api_key, num_limpo, mensagem_final)
                    if ok:
                        registrar_envio_alerta(loja_id, num_limpo, tipo_alerta, mensagem_final, "Evolution API")
                        st.success(retorno)
                    else:
                        st.error(retorno)

    st.divider()

    # --- BLOCO 3: HISTÓRICO FILTRADO APENAS DO CLIENTE SELECIONADO ---
    st.subheader(f"3. Histórico de Alertas — {loja_id} ({nome_loja})")
    conn, tipo_b = _conectar()
    ph = "%s" if tipo_b == "postgres" else "?"
    try:
        df_hist = pd.read_sql_query(
            f'SELECT enviado_em AS "Data/Hora", loja_id AS "ID Loja", telefone_destino AS "WhatsApp", tipo_alerta AS "Tipo", canal AS "Canal" FROM historico_alertas WHERE loja_id = {ph} ORDER BY id DESC LIMIT 20',
            conn,
            params=(loja_id.strip().upper(),),
        )
    except Exception:
        df_hist = pd.DataFrame()
    finally:
        conn.close()

    if df_hist.empty:
        st.caption(f"Nenhum alerta registrado para **{loja_id}** ainda.")
    else:
        st.dataframe(df_hist, use_container_width=True, hide_index=True)
