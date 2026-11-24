import streamlit as st
from aba_coleta import renderizar_aba_coleta
from aba_entradas_saidas import renderizar_aba_xml
from aba_indicadores import renderizar_aba_indicadores

st.set_page_config(
    page_title="MVP Varejo — Auditor de Estoque Inteligente",
    page_icon="📦",
    layout="wide",
)

# Inicializa o estado global da sessão
estados_iniciais = {
    "itens_coletados": {},
    "codigo_atual": None,
    "dados_api": {},
    "aviso_duplicado": None,
    "modo_foto_inventario": False,
    "qtd_calculada_ia": 1.0,
    "scanner_ativo": True,
    "scanner_iteracao": 0,
    "ultimo_codigo_scanner": None,
    "df_xml_consolidado": None,
    "df_movs_detalhadas": None,
    "ponto_partida": None,
}
for chave, valor in estados_iniciais.items():
    if chave not in st.session_state:
        st.session_state[chave] = valor

# Cabeçalho principal
st.title("📦 MVP Varejo — Inteligência e Auditoria de Estoque")
st.caption(
    "Monitore apenas os SKUs que realmente importam: colete a contagem física (ou use seu CSV), "
    "cruce com os XMLs (NF-e 55 e NFC-e 65) e acompanhe a evolução por ciclos."
)

# Barra lateral com status do fluxo em tempo real
with st.sidebar:
    st.header("🧭 Status do Fluxo")
    qtd_skus = len(st.session_state.get("itens_coletados", {}))
    tem_consolidado = (
        st.session_state.get("df_xml_consolidado") is not None
        and not st.session_state["df_xml_consolidado"].empty
    )
    ponto_partida = st.session_state.get("ponto_partida")

    if qtd_skus > 0:
        st.success(f"**1. Coleta:** {qtd_skus} SKU(s) na sessão")
    else:
        st.info("**1. Coleta:** Aguardando itens (ou CSV na Etapa 2)")

    if tem_consolidado:
        st.success("**2. XMLs:** Consolidado gerado")
    else:
        st.info("**2. XMLs:** Aguardando processamento")

    if ponto_partida is not None:
        data_ref = ponto_partida.get("data_registro", "")
        st.success(f"**3. Ciclo Ativo:** Ponto de Partida salvo ({data_ref})")
    else:
        st.info("**3. Ciclo Ativo:** Nenhum Ponto de Partida salvo")

    st.divider()
    st.caption("💡 **Dica:** Na Etapa 3 você pode salvar o resultado atual como Ponto de Partida para o próximo ciclo.")

# Divide as etapas em abas
aba1, aba2, aba3 = st.tabs([
    "Etapa 1: Coleta Física de Estoque",
    "Etapa 2: Entradas e Saídas (XML)",
    "Etapa 3: Indicadores e Simulação",
])

with aba1:
    renderizar_aba_coleta()

with aba2:
    renderizar_aba_xml()

with aba3:
    renderizar_aba_indicadores()
