from datetime import datetime
import io
import zipfile
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import streamlit as st

from aba_conversao import renderizar_aba_conversao
from aba_indicadores import renderizar_aba_indicadores


st.set_page_config(
    page_title="MVP Varejo Inteligente — Estoque & Compras",
    page_icon="🛒",
    layout="wide",
)


def _ler_csv_inteligente(arquivo) -> pd.DataFrame:
    """Lê arquivos CSV gerados pelo Excel brasileiro (cp1252/latin1) ou UTF-8, detectando separador automaticamente."""
    raw_bytes = arquivo.getvalue()
    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin1"]
    separadores = [";", ",", "\t"]

    melhor_df = None
    for enc in encodings:
        try:
            texto = raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

        linhas = [l.strip() for l in texto.splitlines() if l.strip()]
        if not linhas:
            continue

        # Detecta qual separador aparece mais na primeira linha útil
        primeira_linha = linhas[0]
        sep_detectado = max(separadores, key=lambda s: primeira_linha.count(s))

        try:
            df_teste = pd.read_csv(
                io.StringIO(texto),
                sep=sep_detectado,
                engine="python",
                skip_blank_lines=True,
            )
            df_teste.columns = [str(c).strip() for c in df_teste.columns]
            if len(df_teste.columns) >= 2:
                melhor_df = df_teste
                break
        except Exception:
            continue

    if melhor_df is None:
        raise ValueError("Não foi possível identificar as colunas do CSV. Verifique se o arquivo não está corrompido.")

    # Mapeamento automático de sinônimos comuns em planilhas de supermercado
    mapa_sinonimos = {}
    for col in melhor_df.columns:
        c_low = col.lower().strip()
        if c_low in {"sku", "codigo", "código", "cod", "cód", "ean", "gtin", "cod_produto", "codigo_produto", "id"}:
            if "SKU" not in melhor_df.columns and "SKU" not in mapa_sinonimos.values():
                mapa_sinonimos[col] = "SKU"
        elif c_low in {"qtd inicial", "qtd_inicial", "quantidade", "qtd", "estoque", "estoque inicial", "estoque_inicial", "saldo", "saldo inicial", "saldo_inicial"}:
            if "Qtd Inicial" not in melhor_df.columns and "Qtd Inicial" not in mapa_sinonimos.values():
                mapa_sinonimos[col] = "Qtd Inicial"
        elif c_low in {"descrição", "descricao", "produto", "nome", "item", "nome_produto"}:
            if "Descrição" not in melhor_df.columns and "Descrição" not in mapa_sinonimos.values():
                mapa_sinonimos[col] = "Descrição"
        elif c_low in {"categoria", "grupo", "secao", "seção", "departamento"}:
            if "Categoria" not in melhor_df.columns and "Categoria" not in mapa_sinonimos.values():
                mapa_sinonimos[col] = "Categoria"
        elif c_low in {"marca", "fabricante", "fornecedor"}:
            if "Marca" not in melhor_df.columns and "Marca" not in mapa_sinonimos.values():
                mapa_sinonimos[col] = "Marca"

    if mapa_sinonimos:
        melhor_df = melhor_df.rename(columns=mapa_sinonimos)

    return melhor_df


def _extrair_itens_xml(conteudo_xml: bytes, tipo_forcado: str | None = None) -> list[dict]:
    """Extrai itens de NF-e (modelo 55) ou NFC-e (modelo 65) a partir do conteúdo XML."""
    registros = []
    try:
        root = ET.fromstring(conteudo_xml)
    except ET.ParseError:
        return registros

    # Remove namespaces para facilitar a busca de tags
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    inf_nfe = root.find(".//infNFe")
    if inf_nfe is None:
        return registros

    ide = inf_nfe.find("ide")
    tp_nf = ide.findtext("tpNF", default="1") if ide is not None else "1"
    mod = ide.findtext("mod", default="55") if ide is not None else "55"
    dh_emi = ide.findtext("dhEmi") or ide.findtext("dEmi") or ""
    data_emissao = dh_emi[:10] if len(dh_emi) >= 10 else ""

    if tipo_forcado:
        tipo_mov = tipo_forcado
    else:
        if mod == "65":
            tipo_mov = "Saída"
        else:
            tipo_mov = "Entrada" if tp_nf == "0" else "Saída"

    for det in inf_nfe.findall("det"):
        prod = det.find("prod")
        if prod is None:
            continue

        c_ean = (prod.findtext("cEAN") or "").strip()
        c_prod = (prod.findtext("cProd") or "").strip()
        sku = c_ean if (c_ean and c_ean.upper() != "SEM GTIN" and c_ean != "0") else c_prod
        if not sku:
            continue

        x_prod = (prod.findtext("xProd") or "Produto sem descrição").strip()
        u_com = (prod.findtext("uCom") or "UN").strip().upper()
        q_com = float(prod.findtext("qCom") or 0.0)
        v_un_com = float(prod.findtext("vUnCom") or 0.0)
        v_prod = float(prod.findtext("vProd") or (q_com * v_un_com))

        registros.append(
            {
                "SKU": sku,
                "Codigo_Interno_XML": c_prod,
                "EAN_XML": c_ean,
                "Descrição": x_prod,
                "Unidade": u_com,
                "Tipo": tipo_mov,
                "Data_Emissao": data_emissao,
                "Quantidade": q_com,
                "Valor_Unitario": v_un_com,
                "Valor_Total": v_prod,
            }
        )

    return registros


def _processar_arquivos_upload(arquivos, tipo_forcado: str | None = None) -> pd.DataFrame:
    """Lê múltiplos arquivos .XML ou .ZIP contendo XMLs e retorna DataFrame consolidado de movimentos."""
    todos_itens = []
    for arq in arquivos:
        nome = arq.name.lower()
        dados = arq.read()
        if nome.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(dados)) as zf:
                    for item_zip in zf.namelist():
                        if item_zip.lower().endswith(".xml") and not item_zip.startswith("__MACOSX"):
                            todos_itens.extend(_extrair_itens_xml(zf.read(item_zip), tipo_forcado))
            except zipfile.BadZipFile:
                st.warning(f"Arquivo ZIP inválido ignorado: {arq.name}")
        elif nome.endswith(".xml"):
            todos_itens.extend(_extrair_itens_xml(dados, tipo_forcado))

    if not todos_itens:
        return pd.DataFrame()
    return pd.DataFrame(todos_itens)


def _consolidar_base(df_movs: pd.DataFrame, df_partida: pd.DataFrame | None) -> pd.DataFrame:
    """Cruza o ponto de partida (estoque inicial ou ciclo anterior) com as entradas e saídas dos XMLs."""
    base_inicial = pd.DataFrame(columns=["SKU", "Descrição", "Categoria", "Marca", "Qtd Inicial", "Fator_Conversao"])
    if df_partida is not None and not df_partida.empty:
        base_inicial = df_partida.copy()
        base_inicial["SKU"] = base_inicial["SKU"].astype(str).str.strip()
        if "Qtd Inicial" not in base_inicial.columns:
            base_inicial["Qtd Inicial"] = 0.0
        base_inicial["Qtd Inicial"] = pd.to_numeric(base_inicial["Qtd Inicial"], errors="coerce").fillna(0.0)
        if "Fator_Conversao" not in base_inicial.columns:
            base_inicial["Fator_Conversao"] = 1.0

    if df_movs.empty:
        df_res = base_inicial.copy()
        df_res["Total_Entradas"] = 0.0
        df_res["Total_Saidas"] = 0.0
        df_res["Valor_Entradas"] = 0.0
        df_res["Valor_Saidas"] = 0.0
        df_res["Custo_Unitario"] = 0.0
        df_res["Preco_Venda"] = 0.0
        df_res["Saldo_Calculado"] = df_res["Qtd Inicial"]
        return df_res

    df_m = df_movs.copy()
    df_m["SKU"] = df_m["SKU"].astype(str).str.strip()

    entradas = (
        df_m[df_m["Tipo"] == "Entrada"]
        .groupby("SKU", as_index=False)
        .agg(
            Total_Entradas=("Quantidade", "sum"),
            Valor_Entradas=("Valor_Total", "sum"),
            Desc_Ent=("Descrição", "first"),
        )
    )
    entradas["Custo_Unitario"] = np.where(
        entradas["Total_Entradas"] > 0,
        entradas["Valor_Entradas"] / entradas["Total_Entradas"],
        0.0,
    )

    saidas = (
        df_m[df_m["Tipo"] == "Saída"]
        .groupby("SKU", as_index=False)
        .agg(
            Total_Saidas=("Quantidade", "sum"),
            Valor_Saidas=("Valor_Total", "sum"),
            Ultima_Saida=("Data_Emissao", "max"),
            Desc_Sai=("Descrição", "first"),
        )
    )
    saidas["Preco_Venda"] = np.where(
        saidas["Total_Saidas"] > 0,
        saidas["Valor_Saidas"] / saidas["Total_Saidas"],
        0.0,
    )

    skus_todos = pd.DataFrame(
        {
            "SKU": pd.unique(
                pd.concat(
                    [
                        base_inicial["SKU"] if not base_inicial.empty else pd.Series(dtype=str),
                        entradas["SKU"],
                        saidas["SKU"],
                    ],
                    ignore_index=True,
                )
            )
        }
    )

    df_res = skus_todos.merge(base_inicial, on="SKU", how="left")
    df_res = df_res.merge(entradas, on="SKU", how="left")
    df_res = df_res.merge(saidas, on="SKU", how="left")

    if "Descrição" not in df_res.columns:
        df_res["Descrição"] = np.nan
    df_res["Descrição"] = (
        df_res["Descrição"]
        .fillna(df_res["Desc_Sai"])
        .fillna(df_res["Desc_Ent"])
        .fillna("Produto sem descrição")
    )
    df_res.drop(columns=["Desc_Sai", "Desc_Ent"], inplace=True, errors="ignore")

    if "Categoria" not in df_res.columns:
        df_res["Categoria"] = "Geral"
    else:
        df_res["Categoria"] = df_res["Categoria"].fillna("Geral")

    if "Marca" not in df_res.columns:
        df_res["Marca"] = "Sem Marca"
    else:
        df_res["Marca"] = df_res["Marca"].fillna("Sem Marca")

    if "Fator_Conversao" not in df_res.columns:
        df_res["Fator_Conversao"] = 1.0
    else:
        df_res["Fator_Conversao"] = pd.to_numeric(df_res["Fator_Conversao"], errors="coerce").fillna(1.0)

    for col_num in ["Qtd Inicial", "Total_Entradas", "Total_Saidas", "Valor_Entradas", "Valor_Saidas", "Custo_Unitario", "Preco_Venda"]:
        df_res[col_num] = pd.to_numeric(df_res[col_num], errors="coerce").fillna(0.0)

    df_res["Saldo_Calculado"] = df_res["Qtd Inicial"] + df_res["Total_Entradas"] - df_res["Total_Saidas"]
    return df_res


def main():
    with st.sidebar:
        st.title("🛒 Varejo Inteligente")
        st.caption("Copiloto de Compras & Prevenção de Ruptura")
        st.divider()
        nome_loja = st.text_input("Loja / Unidade em Análise:", value="Loja 01 - Matriz")
        st.info("Fluxo rápido:\n1. Ponto de Partida\n2. XMLs (Entradas/Saídas)\n3. Conversão CX→UN\n4. Lista de Compra & Kardex")

    st.title(f"Gestão Inteligente de Estoque — {nome_loja}")

    tab1, tab2, tab_conv, tab3 = st.tabs(
        [
            "1. Ponto de Partida",
            "2. Processar XMLs",
            "3. De-Para (Conversão CX→UN)",
            "4. Indicadores e Kardex",
        ]
    )

    # --- ABA 1: PONTO DE PARTIDA ---
    with tab1:
        st.header("1. Ponto de Partida (Estoque Inicial ou Ciclo Anterior)")
        st.caption(
            "Informe o saldo inicial da loja (contagem inicial) ou suba a planilha de fechamento do ciclo anterior para dar continuidade ao histórico."
        )

        modo_inicio = st.radio(
            "Como deseja iniciar este ciclo?",
            options=[
                "Começar do zero (Saldo inicial zerado ou planilha de contagem)",
                "Continuar do ciclo anterior (Subir planilha Base Próximo Ciclo)",
            ],
            horizontal=True,
        )

        if modo_inicio.startswith("Começar"):
            c_up, c_mod = st.columns([2, 1])
            arq_inicial = c_up.file_uploader(
                "Opcional: Planilha de Contagem Inicial (.CSV com colunas SKU, Descrição, Qtd Inicial, Categoria, Marca):",
                type=["csv"],
                key="up_inicial",
            )
            modelo_csv = pd.DataFrame(
                {
                    "SKU": ["7891000100103", "7894900011517"],
                    "Descrição": ["Leite Integral 1L", "Refrigerante Cola 2L"],
                    "Categoria": ["Laticínios", "Bebidas"],
                    "Marca": ["Marca A", "Marca B"],
                    "Qtd Inicial": [48, 36],
                }
            ).to_csv(index=False, sep=";").encode("utf-8-sig")
            c_mod.download_button(
                "📥 Baixar Modelo de Planilha Inicial (.CSV)",
                data=modelo_csv,
                file_name="modelo_estoque_inicial.csv",
                mime="text/csv",
                use_container_width=True,
            )

            if arq_inicial is not None:
                try:
                    df_ini = _ler_csv_inteligente(arq_inicial)
                    if "SKU" in df_ini.columns and "Qtd Inicial" in df_ini.columns:
                        st.session_state["ponto_partida"] = {"df_base_proximo_ciclo": df_ini}
                        st.success(f"Planilha inicial carregada com {len(df_ini)} produtos!")
                        st.dataframe(df_ini.head(15), use_container_width=True, hide_index=True)
                    else:
                        st.error(
                            f"A planilha precisa ter pelo menos as colunas 'SKU' e 'Qtd Inicial'. "
                            f"Colunas encontradas no seu arquivo: {list(df_ini.columns)}"
                        )
                except Exception as e:
                    st.error(f"Erro ao ler planilha inicial: {e}")
            else:
                st.session_state.setdefault("ponto_partida", None)
        else:
            arq_ciclo = st.file_uploader(
                "Suba o arquivo 'base_proximo_ciclo_XXXX.csv' gerado no fechamento anterior:",
                type=["csv"],
                key="up_ciclo_anterior",
            )
            if arq_ciclo is not None:
                try:
                    df_ref = _ler_csv_inteligente(arq_ciclo)
                    if "SKU" in df_ref.columns and "Qtd Inicial" in df_ref.columns:
                        st.session_state["ponto_partida"] = {"df_base_proximo_ciclo": df_ref}
                        data_ref = df_ref["Ref_Data"].iloc[0] if "Ref_Data" in df_ref.columns and not df_ref.empty else "Anterior"
                        st.success(f"Ciclo anterior ({data_ref}) carregado com {len(df_ref)} SKUs e histórico acumulado!")
                        st.dataframe(df_ref.head(15), use_container_width=True, hide_index=True)
                    else:
                        st.error(
                            f"Arquivo inválido. Colunas encontradas: {list(df_ref.columns)}. "
                            "Certifique-se de subir o CSV gerado pelo botão 'Base Próximo Ciclo'."
                        )
                except Exception as e:
                    st.error(f"Erro ao ler arquivo do ciclo anterior: {e}")

    # --- ABA 2: PROCESSAR XMLs ---
    with tab2:
        st.header("2. Importar XMLs de Entradas (Compras) e Saídas (Vendas)")
        st.caption(
            "Suba arquivos .XML individuais ou pastas compactadas em .ZIP exportadas pelo sistema de caixa ou enviadas pelo contador."
        )

        col_e, col_s = st.columns(2)
        with col_e:
            st.subheader("📦 XMLs de Entradas (NF-e Fornecedores)")
            files_ent = st.file_uploader(
                "Arraste XMLs ou ZIPs de compras:",
                type=["xml", "zip"],
                accept_multiple_files=True,
                key="up_xml_entradas",
            )
        with col_s:
            st.subheader("🧾 XMLs de Saídas (NFC-e / NF-e Vendas)")
            files_sai = st.file_uploader(
                "Arraste XMLs ou ZIPs de vendas:",
                type=["xml", "zip"],
                accept_multiple_files=True,
                key="up_xml_saidas",
            )

        if st.button("🚀 Processar XMLs e Cruzar Estoque", type="primary", use_container_width=True):
            if not files_ent and not files_sai:
                st.warning("Selecione pelo menos um arquivo XML ou ZIP de entradas ou saídas.")
            else:
                with st.spinner("Lendo notas fiscais e consolidando movimentações..."):
                    df_ent = _processar_arquivos_upload(files_ent, tipo_forcado="Entrada") if files_ent else pd.DataFrame()
                    df_sai = _processar_arquivos_upload(files_sai, tipo_forcado="Saída") if files_sai else pd.DataFrame()
                    df_movs = pd.concat([df_ent, df_sai], ignore_index=True)

                    snap = st.session_state.get("ponto_partida")
                    df_partida = snap["df_base_proximo_ciclo"] if (snap and "df_base_proximo_ciclo" in snap) else None

                    df_consolidado = _consolidar_base(df_movs, df_partida)

                    # Limpa backups antigos de conversão ao processar novo lote
                    st.session_state.pop("_Backup_Movs_Original", None)
                    st.session_state.pop("_Backup_Consolidado_Original", None)

                    st.session_state["df_movs_detalhadas"] = df_movs
                    st.session_state["df_xml_consolidado"] = df_consolidado

                st.success(
                    f"Processamento concluído! {len(df_consolidado)} produtos consolidados a partir de {len(df_movs)} linhas de notas fiscais."
                )
                st.info("👉 Próximo passo: Verifique a aba **3. De-Para (Conversão CX→UN)** caso haja compras em caixa/fardo, ou vá direto para **4. Indicadores e Kardex**.")

        if "df_xml_consolidado" in st.session_state and not st.session_state["df_xml_consolidado"].empty:
            st.subheader("Prévia dos Produtos Consolidados")
            st.dataframe(st.session_state["df_xml_consolidado"].head(20), use_container_width=True, hide_index=True)

    # --- ABA 3: CONVERSÃO CX -> UN ---
    with tab_conv:
        renderizar_aba_conversao()

    # --- ABA 4: INDICADORES E KARDEX ---
    with tab3:
        renderizar_aba_indicadores()


if __name__ == "__main__":
    main()
