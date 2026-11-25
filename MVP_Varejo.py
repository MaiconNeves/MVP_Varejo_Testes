import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
import pandas as pd
import streamlit as st

from aba_conversao import renderizar_aba_conversao
from aba_indicadores import renderizar_aba_indicadores


st.set_page_config(
    page_title="Varejo Inteligente | Copiloto de Compras e Estoque",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _inicializar_estado():
    """Garante que todas as chaves necessárias existam no st.session_state."""
    defaults = {
        "ponto_partida": None,
        "df_xml_consolidado": pd.DataFrame(),
        "df_movs_detalhadas": pd.DataFrame(),
        "chaves_xml_lidas": set(),
    }
    for chave, valor in defaults.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor
def _limpar_texto_coluna(nome: str) -> str:
    """Remove acentos, espaços extras e deixa tudo em minúsculo para comparação."""
    texto = str(nome).strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("utf-8")
    for ch in ["-", "/", ".", "(", ")", ":"]:
        texto = texto.replace(ch, " ")
    return "_".join(texto.split())
def _carregar_planilha_universal(arquivo) -> pd.DataFrame:
    """Lê planilhas em qualquer formato (.xlsx, .xls, .ods, .csv, .txt)."""
    nome = arquivo.name.lower()
    dados = arquivo.read()

    if nome.endswith((".xlsx", ".xls", ".ods")):
        return pd.read_excel(io.BytesIO(dados))

    # Tenta ler CSV/TXT com diferentes encodings comuns no Brasil
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            texto = dados.decode(enc)
            return pd.read_csv(io.StringIO(texto), sep=None, engine="python")
        except Exception:
            continue

    raise ValueError("Não foi possível decodificar o arquivo. Salve como .xlsx ou .csv (UTF-8).")


def _normalizar_colunas_planilha(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Renomeia automaticamente colunas com nomes variados para o padrão do sistema:
    SKU, Descrição, Qtd Inicial, Categoria, Marca, Fator_Conversao, Ref_Data.
    """
    df = df_raw.copy()

    # Dicionário de sinônimos aceitos (já normalizados sem acento/minúsculo)
    mapa_sinonimos = {
        "SKU": {
            "sku", "codigo", "cod", "cod_produto", "codigo_produto", "cprod",
            "ean", "gtin", "cean", "cod_barras", "codigo_de_barras", "id_produto", "item_id"
        },
        "Descrição": {
            "descricao", "produto", "nome", "nome_produto", "xprod", "item",
            "mercadoria", "desc", "descricao_produto", "titulo"
        },
        "Qtd Inicial": {
            "qtd_inicial", "qtde_inicial", "quantidade_inicial", "saldo_inicial",
            "estoque_inicial", "estoque", "saldo", "qtd", "qtde", "quantidade",
            "qtd_fisica", "qtde_fisica", "estoque_atual", "saldo_atual", "fisico", "contagem"
        },
        "Categoria": {
            "categoria", "depto", "departamento", "secao", "grupo", "familia", "linha"
        },
        "Marca": {
            "marca", "fabricante", "fornecedor", "brand"
        },
        "Fator_Conversao": {
            "fator_conversao", "fator", "conversao", "qtd_cx", "un_cx", "embalagem", "multiplo"
        },
        "Ref_Data": {
            "ref_data", "data_ref", "data_corte", "data", "data_inventario", "data_contagem"
        },
    }

    renomear = {}
    colunas_ja_mapeadas = set()

    for col_original in df.columns:
        col_norm = _limpar_texto_coluna(col_original)
        for destino, sinonimos in mapa_sinonimos.items():
            if destino not in colunas_ja_mapeadas and col_norm in sinonimos:
                renomear[col_original] = destino
                colunas_ja_mapeadas.add(destino)
                break

    df = df.rename(columns=renomear)

    # Limpa valores numéricos na Qtd Inicial (ex: "1.200,50" -> 1200.50)
    if "Qtd Inicial" in df.columns:
        if df["Qtd Inicial"].dtype == object:
            df["Qtd Inicial"] = (
                df["Qtd Inicial"]
                .astype(str)
                .str.replace(".", "", regex=False)
                .str.replace(",", ".", regex=False)
            )
        df["Qtd Inicial"] = pd.to_numeric(df["Qtd Inicial"], errors="coerce").fillna(0.0)

    if "SKU" in df.columns:
        df["SKU"] = df["SKU"].astype(str).str.strip()
        # Remove linhas vazias ou 'nan'
        df = df[df["SKU"].notna() & (df["SKU"] != "") & (df["SKU"].str.lower() != "nan")]

    return df

def _ler_xml_nfe_nfce(conteudo_bytes: bytes, nome_arquivo: str) -> tuple[str | None, list[dict]]:
    """
    Faz o parsing básico de um arquivo XML de NF-e (mod 55) ou NFC-e (mod 65).
    Retorna (chave_acesso, lista_de_itens_movimentados).
    """
    try:
        root = ET.fromstring(conteudo_bytes)
    except ET.ParseError:
        return None, []

    # Remove namespaces para facilitar a busca das tags
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    inf_nfe = root.find(".//infNFe")
    if inf_nfe is None:
        return None, []

    chave_acesso = inf_nfe.attrib.get("Id", "").replace("NFe", "").strip()
    if not chave_acesso:
        chave_acesso = nome_arquivo

    ide = inf_nfe.find("ide")
    tp_nf = ide.findtext("tpNF", default="1") if ide is not None else "1"
    mod = ide.findtext("mod", default="55") if ide is not None else "55"
    dh_emi = ide.findtext("dhEmi") or ide.findtext("dEmi") or ""
    data_emissao = dh_emi[:10] if len(dh_emi) >= 10 else datetime.now().strftime("%Y-%m-%d")

    # Regra padrão: NFC-e (mod 65) é sempre Saída (venda no caixa).
    # NF-e (mod 55): tpNF == "0" é Entrada; se vier de fornecedor (tpNF == "1" emitida por terceiro),
    # o usuário também pode forçar o tipo na tela de upload.
    tipo_mov = "Saída" if (mod == "65" or tp_nf == "1") else "Entrada"

    itens = []
    for det in inf_nfe.findall("det"):
        prod = det.find("prod")
        if prod is None:
            continue

        c_ean = (prod.findtext("cEAN") or "").strip()
        c_prod = (prod.findtext("cProd") or "").strip()
        # Prioriza EAN válido como SKU unificado para cruzar entrada e saída
        sku = c_ean if (c_ean and c_ean.upper() != "SEM GTIN" and len(c_ean) >= 8) else c_prod
        if not sku:
            continue

        descricao = (prod.findtext("xProd") or "Produto sem descrição").strip()
        u_com = (prod.findtext("uCom") or "UN").strip().upper()
        q_com = float(prod.findtext("qCom") or 0.0)
        v_un_com = float(prod.findtext("vUnCom") or 0.0)
        v_prod = float(prod.findtext("vProd") or 0.0)

        itens.append(
            {
                "Chave_NFe": chave_acesso,
                "Modelo": mod,
                "Tipo": tipo_mov,
                "Data_Emissao": data_emissao,
                "Data": data_emissao,
                "SKU": sku,
                "Codigo_Fornecedor": c_prod,
                "EAN": c_ean,
                "Descrição": descricao,
                "Unidade": u_com,
                "Quantidade": q_com,
                "Valor_Unitario": v_un_com,
                "Valor_Total": v_prod,
            }
        )

    return chave_acesso, itens


def _extrair_arquivos_xml(arquivos_enviados) -> list[tuple[str, bytes]]:
    """Extrai arquivos .xml individuais ou de dentro de pacotes .zip."""
    xmls = []
    for arq in arquivos_enviados:
        nome = arq.name.lower()
        dados = arq.read()
        if nome.endswith(".xml"):
            xmls.append((arq.name, dados))
        elif nome.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(dados)) as zf:
                for item_zip in zf.namelist():
                    if item_zip.lower().endswith(".xml") and not item_zip.startswith("__MACOSX"):
                        xmls.append((item_zip, zf.read(item_zip)))
    return xmls


def _renderizar_etapa_1_ponto_partida():
    st.header("1. Ponto de Partida (Estoque Inicial ou Ciclo Anterior)")
    st.caption(
        "Informe o saldo inicial da loja (contagem inicial) ou suba a planilha de fechamento do ciclo anterior "
        "para dar continuidade ao histórico."
    )

    opcao = st.radio(
        "Como deseja iniciar este ciclo?",
        options=[
            "Começar do zero (Saldo inicial zerado ou planilha de contagem)",
            "Continuar do ciclo anterior (Subir planilha Base Próximo Ciclo)",
        ],
        horizontal=True,
    )

    if opcao.startswith("Continuar"):
        arq_ciclo = st.file_uploader(
            "Suba o arquivo CSV gerado no fechamento anterior (base_proximo_ciclo_*.csv):",
            type=["csv"],
            key="up_ciclo_anterior",
        )
        if arq_ciclo is not None:
            try:
                df_ref = pd.read_csv(arq_ciclo, sep=None, engine="python")
                if "SKU" in df_ref.columns and "Qtd Inicial" in df_ref.columns:
                    df_ref["SKU"] = df_ref["SKU"].astype(str).str.strip()
                    st.session_state["ponto_partida"] = {
                        "origem": "ciclo_anterior",
                        "data_corte": str(df_ref["Ref_Data"].iloc[0]) if "Ref_Data" in df_ref.columns else "",
                        "df_base_proximo_ciclo": df_ref,
                    }
                    st.success(f"Ciclo anterior carregado com {len(df_ref)} SKUs! Avance para a Etapa 2.")
                    st.dataframe(df_ref.head(15), use_container_width=True, hide_index=True)
                else:
                    st.error("O arquivo precisa conter as colunas 'SKU' e 'Qtd Inicial'.")
            except Exception as e:
                st.error(f"Erro ao ler arquivo de ciclo: {e}")
    else:
        col1, col2 = st.columns(2)
        arq_ini = col1.file_uploader(
            "Opcional: Planilha de Contagem Inicial (.CSV com colunas SKU, Descrição, Qtd Inicial, Categoria, Marca):",
            type=["csv"],
            key="up_estoque_inicial",
        )
        # Modelo para download rápido
        modelo_df = pd.DataFrame(
            [
                {"SKU": "7891000100103", "Descrição": "LEITE INTEGRAL 1L", "Categoria": "Laticínios", "Marca": "Marca A", "Qtd Inicial": 48},
                {"SKU": "7891991010856", "Descrição": "CERVEJA LATA 350ML", "Categoria": "Bebidas", "Marca": "Marca B", "Qtd Inicial": 120},
            ]
        )
        col2.download_button(
            label="📥 Baixar Modelo de Planilha Inicial (.CSV)",
            data=modelo_df.to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name="modelo_estoque_inicial.csv",
            mime="text/csv",
        )

        if arq_ini is not None:
            try:
                df_ini = pd.read_csv(arq_ini, sep=None, engine="python")
                if "SKU" in df_ini.columns and "Qtd Inicial" in df_ini.columns:
                    df_ini["SKU"] = df_ini["SKU"].astype(str).str.strip()
                    st.session_state["ponto_partida"] = {
                        "origem": "contagem_inicial",
                        "df_base_proximo_ciclo": df_ini,
                    }
                    st.success(f"Estoque inicial carregado com {len(df_ini)} produtos!")
                else:
                    st.error("A planilha precisa ter pelo menos as colunas 'SKU' e 'Qtd Inicial'.")
            except Exception as e:
                st.error(f"Erro ao ler planilha inicial: {e}")

def _renderizar_etapa_2_xmls():
    st.header("2. Processamento de XMLs (Entradas e Saídas)")
    st.caption(
        "Suba os arquivos XML individuais ou pastas compactadas (.ZIP) das notas de compra (NF-e) e vendas no caixa (NFC-e)."
    )

    c_ent, c_sai = st.columns(2)
    files_entrada = c_ent.file_uploader(
        "📦 XMLs / ZIPs de ENTRADA (Compras de Fornecedores):",
        type=["xml", "zip"],
        accept_multiple_files=True,
        key="up_xml_entradas",
    )
    files_saida = c_sai.file_uploader(
        "🧾 XMLs / ZIPs de SAÍDA (Vendas / NFC-e / NF-e):",
        type=["xml", "zip"],
        accept_multiple_files=True,
        key="up_xml_saidas",
    )

    if st.button("🚀 Processar XMLs e Consolidar Estoque", type="primary", use_container_width=True):
        if not files_entrada and not files_saida:
            st.warning("Envie pelo menos um arquivo XML ou ZIP de entrada ou saída.")
            return

        with st.spinner("Lendo notas fiscais e consolidando movimentações..."):
            todos_itens = []
            chaves_vistas = set()

            for arq_nome, arq_bytes in _extrair_arquivos_xml(files_entrada or []):
                chave, itens = _ler_xml_nfe_nfce(arq_bytes, arq_nome)
                if chave and chave not in chaves_vistas:
                    chaves_vistas.add(chave)
                    for it in itens:
                        it["Tipo"] = "Entrada"
                        todos_itens.append(it)

            for arq_nome, arq_bytes in _extrair_arquivos_xml(files_saida or []):
                chave, itens = _ler_xml_nfe_nfce(arq_bytes, arq_nome)
                if chave and chave not in chaves_vistas:
                    chaves_vistas.add(chave)
                    for it in itens:
                        it["Tipo"] = "Saída"
                        todos_itens.append(it)

            if not todos_itens:
                st.error("Nenhum item válido encontrado nos XMLs enviados.")
                return

            df_movs = pd.DataFrame(todos_itens)

            # Consolida por SKU
            entradas = df_movs[df_movs["Tipo"] == "Entrada"].groupby("SKU", as_index=False).agg(
                Total_Entradas=("Quantidade", "sum"),
                Valor_Entradas=("Valor_Total", "sum"),
                Custo_Unitario=("Valor_Unitario", "mean"),
            )
            saidas = df_movs[df_movs["Tipo"] == "Saída"].groupby("SKU", as_index=False).agg(
                Total_Saidas=("Quantidade", "sum"),
                Valor_Saidas=("Valor_Total", "sum"),
                Preco_Venda=("Valor_Unitario", "mean"),
                Ultima_Saida=("Data_Emissao", "max"),
            )
            cadastro = df_movs.groupby("SKU", as_index=False).agg(
                Descrição=("Descrição", "first"),
            )

            # 0. Garante que não haja itens duplicados na mesma nota fiscal
            if "Item" in df_movs.columns and "Chave_NFe" in df_movs.columns:
                df_movs = df_movs.drop_duplicates(subset=["Chave_NFe", "Item"], keep="first")

            # Padroniza SKU antes de qualquer agrupamento
            df_movs["SKU"] = df_movs["SKU"].astype(str).str.strip()

            # 1. Consolida por SKU (1 linha única por SKU)
            entradas = df_movs[df_movs["Tipo"] == "Entrada"].groupby("SKU", as_index=False).agg(
                Total_Entradas=("Quantidade", "sum"),
                Valor_Entradas=("Valor_Total", "sum"),
                Custo_Unitario=("Valor_Unitario", "mean"),
            )
            saidas = df_movs[df_movs["Tipo"] == "Saída"].groupby("SKU", as_index=False).agg(
                Total_Saidas=("Quantidade", "sum"),
                Valor_Saidas=("Valor_Total", "sum"),
                Preco_Venda=("Valor_Unitario", "mean"),
                Ultima_Saida=("Data_Emissao", "max"),
            )
            cadastro = df_movs.groupby("SKU", as_index=False).agg(
                Descrição=("Descrição", "first"),
            )

            df_cons = cadastro.merge(entradas, on="SKU", how="left").merge(saidas, on="SKU", how="left")
            for col_num in ["Total_Entradas", "Valor_Entradas", "Custo_Unitario", "Total_Saidas", "Valor_Saidas",
                            "Preco_Venda"]:
                df_cons[col_num] = df_cons[col_num].fillna(0.0)

            df_cons["Categoria"] = "Geral"
            df_cons["Marca"] = "Sem Marca"
            df_cons["Qtd Inicial"] = 0.0
            df_cons["Fator_Conversao"] = 1.0

            # 2. Aplica ponto de partida se existir (SEM duplicar SKUs!)
            snap = st.session_state.get("ponto_partida")
            if snap and "df_base_proximo_ciclo" in snap:
                df_ref = snap["df_base_proximo_ciclo"].copy()
                df_ref["SKU"] = df_ref["SKU"].astype(str).str.strip()

                # PULO DO GATO 1: Consolida df_ref por SKU para impedir que o merge duplique produtos!
                agg_ref = {}
                if "Qtd Inicial" in df_ref.columns:
                    agg_ref["Qtd Inicial"] = "sum"
                if "Categoria" in df_ref.columns:
                    agg_ref["Categoria"] = "first"
                if "Marca" in df_ref.columns:
                    agg_ref["Marca"] = "first"
                if "Fator_Conversao" in df_ref.columns:
                    agg_ref["Fator_Conversao"] = "first"
                if "Descrição" in df_ref.columns:
                    agg_ref["Descrição"] = "first"

                df_ref_unico = df_ref.groupby("SKU", as_index=False).agg(agg_ref)

                cols_merge = [c for c in ["SKU", "Qtd Inicial", "Categoria", "Marca", "Fator_Conversao"] if
                              c in df_ref_unico.columns]
                df_cons = df_cons.drop(columns=[c for c in cols_merge if c != "SKU" and c in df_cons.columns])

                # PULO DO GATO 2: how="right" traz APENAS os produtos que você escolheu no Ponto de Partida!
                # (Se quiser trazer todos os produtos dos XMLs mesmo os que não estão na planilha, troque "right" por "outer")
                df_cons = df_cons.merge(df_ref_unico[cols_merge], on="SKU", how="right")

                # Se algum produto escolhido não teve movimento no XML, puxa a descrição da planilha inicial
                if "Descrição_x" in df_cons.columns and "Descrição_y" in df_cons.columns:
                    df_cons["Descrição"] = df_cons["Descrição_x"].fillna(df_cons["Descrição_y"])
                    df_cons = df_cons.drop(columns=["Descrição_x", "Descrição_y"])
                df_cons["Descrição"] = df_cons["Descrição"].fillna("Item do Saldo Inicial")

                for col_num in ["Qtd Inicial", "Total_Entradas", "Valor_Entradas", "Custo_Unitario", "Total_Saidas",
                                "Valor_Saidas", "Preco_Venda"]:
                    if col_num in df_cons.columns:
                        df_cons[col_num] = df_cons[col_num].fillna(0.0)
                df_cons["Categoria"] = df_cons["Categoria"].fillna("Geral")
                df_cons["Marca"] = df_cons["Marca"].fillna("Sem Marca")
                df_cons["Fator_Conversao"] = df_cons["Fator_Conversao"].fillna(1.0)

                # Filtra também o df_movs para o Kardex e os demais painéis respeitarem apenas os SKUs escolhidos
                df_movs = df_movs[df_movs["SKU"].isin(df_ref_unico["SKU"])].copy()

            # 3. Trava final de segurança: garante 1 única linha por SKU e calcula o saldo exato
            df_cons = df_cons.drop_duplicates(subset=["SKU"], keep="first").reset_index(drop=True)
            df_cons["Saldo_Calculado"] = df_cons["Qtd Inicial"] + df_cons["Total_Entradas"] - df_cons["Total_Saidas"]

            # Limpa backups antigos de conversão ao carregar novos XMLs
            for k_bak in ["_Backup_Movs_Original", "_Backup_Consolidado_Original"]:
                if k_bak in st.session_state:
                    del st.session_state[k_bak]

            st.session_state["df_xml_consolidado"] = df_cons
            st.session_state["df_movs_detalhadas"] = df_movs
            st.session_state["chaves_xml_lidas"] = chaves_vistas

            st.success(
                f"Processamento concluído! {len(chaves_vistas)} notas fiscais lidas e {len(df_cons)} SKUs consolidados."
            )

    if not st.session_state["df_xml_consolidado"].empty:
        df_exib = st.session_state["df_xml_consolidado"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Notas Fiscais Lidas", len(st.session_state["chaves_xml_lidas"]))
        m2.metric("SKUs Consolidados", len(df_exib))
        m3.metric("Total Unidades Vendidas", f"{df_exib['Total_Saidas'].sum():,.0f}".replace(",", "."))
        st.dataframe(df_exib.head(20), use_container_width=True, hide_index=True)


def main():
    _inicializar_estado()

    with st.sidebar:
        st.title("🛒 Varejo Inteligente")
        st.caption("Copiloto de Compras e Prevenção de Ruptura")
        st.divider()
        loja_nome = st.text_input("Loja / Unidade em Análise:", value="Loja 01 - Matriz")
        st.info(
            "**Fluxo Recomendado:**\n"
            "1. Defina o Ponto de Partida\n"
            "2. Processe os XMLs\n"
            "3. Ajuste Caixas → Unidades\n"
            "4. Analise Compras e Rupturas"
        )

    st.title(f"Gestão Inteligente de Estoque — {loja_nome}")

    tab1, tab2, tab_conv, tab3 = st.tabs(
        [
            "1. Ponto de Partida",
            "2. Processar XMLs",
            "3. De-Para (Conversão CX→UN)",
            "4. Indicadores e Kardex",
        ]
    )

    with tab1:
        _renderizar_etapa_1_ponto_partida()

    with tab2:
        _renderizar_etapa_2_xmls()

    with tab_conv:
        renderizar_aba_conversao()

    with tab3:
        renderizar_aba_indicadores()


if __name__ == "__main__":
    main()
