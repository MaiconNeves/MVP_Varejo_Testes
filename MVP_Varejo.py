import io
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
import pandas as pd
import streamlit as st

from aba_conversao import renderizar_aba_conversao
from aba_indicadores import renderizar_aba_indicadores
from aba_whatsapp_db import renderizar_aba_fechamento

try:
    from aba_coleta import renderizar_aba_coleta
    TEM_ABA_COLETA = True
except Exception:
    TEM_ABA_COLETA = False

try:
    from aba_entradas_saidas import renderizar_aba_entradas_saidas
    TEM_ABA_ENTRADAS_SAIDAS = True
except Exception:
    TEM_ABA_ENTRADAS_SAIDAS = False


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


def _padronizar_ean(valor) -> str:
    """Normaliza EAN/GTIN: trata notação científica do Excel, remove '.0' e padroniza GTIN-14 para EAN-13."""
    if pd.isna(valor):
        return ""
    s = str(valor).strip().upper()
    if s in ("", "NAN", "NONE", "SEM GTIN", "0"):
        return ""
    if "E+" in s or "E-" in s:
        try:
            s = f"{float(s.replace(',', '.')):.0f}"
        except Exception:
            pass
    if s.endswith(".0"):
        s = s[:-2]
    apenas_digitos = "".join(ch for ch in s if ch.isdigit())
    if len(apenas_digitos) == 14 and apenas_digitos.startswith("0"):
        apenas_digitos = apenas_digitos[1:]
    return apenas_digitos if len(apenas_digitos) >= 8 else ""


def _carregar_planilha_universal(arquivo) -> pd.DataFrame:
    """Lê planilhas em qualquer formato (.xlsx, .xls, .ods, .csv, .txt) blindado para Excel BR."""
    nome = arquivo.name.lower()
    dados = arquivo.read()

    if nome.endswith((".xlsx", ".xls", ".ods")):
        return pd.read_excel(io.BytesIO(dados), dtype=str)

    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            texto = dados.decode(enc)
            linhas = texto.splitlines()
            primeira_linha = linhas[0] if linhas else ""
            if ";" in primeira_linha:
                sep = ";"
            elif "\t" in primeira_linha:
                sep = "\t"
            else:
                sep = ","
            return pd.read_csv(io.StringIO(texto), sep=sep, dtype=str)
        except Exception:
            continue

    raise ValueError("Não foi possível decodificar o arquivo. Salve como .xlsx ou .csv.")


def _normalizar_colunas_planilha(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Renomeia automaticamente colunas com nomes variados para o padrão do sistema:
    SKU (EAN), Descrição, Qtd Inicial, Categoria, Marca, Fator_Conversao, Ref_Data.
    """
    df = df_raw.copy()

    mapa_sinonimos = {
        "SKU": {
            "ean", "gtin", "cean", "cod_barras", "codigo_de_barras", "codigo_barras",
            "sku", "codigo", "cod", "cod_produto", "codigo_produto", "cprod", "id_produto", "item_id"
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
        ean_pad = df["SKU"].apply(_padronizar_ean)
        raw_pad = df["SKU"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        df["SKU"] = ean_pad.where(ean_pad != "", raw_pad)
        df = df[df["SKU"].notna() & (df["SKU"] != "") & (df["SKU"].str.lower() != "nan")]

    return df


def _ler_xml_nfe_nfce(conteudo_bytes: bytes, nome_arquivo: str) -> tuple[str | None, list[dict]]:
    """Faz o parsing de XML de NF-e (mod 55), NFC-e (mod 65) e CF-e SAT (<infCFe>)."""
    try:
        root = ET.fromstring(conteudo_bytes)
    except ET.ParseError:
        return None, []

    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[-1]

    # Suporte para CF-e SAT (muito usado em vendas/saídas no varejo)
    inf_cfe = root.find(".//infCFe")
    if inf_cfe is not None:
        chave_acesso = inf_cfe.attrib.get("Id", "").replace("CFe", "").strip() or nome_arquivo
        ide = inf_cfe.find("ide")
        d_emi = ide.findtext("dEmi", default="") if ide is not None else ""
        if len(d_emi) == 8 and d_emi.isdigit():
            data_emissao = f"{d_emi[:4]}-{d_emi[4:6]}-{d_emi[6:8]}"
        else:
            data_emissao = datetime.now().strftime("%Y-%m-%d")

        itens = []
        for idx_det, det in enumerate(inf_cfe.findall("det"), start=1):
            prod = det.find("prod")
            if prod is None:
                continue

            num_item = det.attrib.get("nItem", str(idx_det))
            c_ean = _padronizar_ean(prod.findtext("cEAN"))
            c_prod = (prod.findtext("cProd") or "").strip()
            sku = c_ean if c_ean else c_prod
            if not sku:
                continue

            descricao = (prod.findtext("xProd") or "Produto sem descrição").strip()
            u_com = (prod.findtext("uCom") or "UN").strip().upper()
            q_com = float(prod.findtext("qCom") or 0.0)
            v_un_com = float(prod.findtext("vUnCom") or 0.0)
            v_prod = float(prod.findtext("vProd") or (q_com * v_un_com))

            itens.append(
                {
                    "Chave_NFe": chave_acesso,
                    "Item": num_item,
                    "Modelo": "59",
                    "Tipo": "Saída",
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

    # Suporte padrão NF-e (mod 55) e NFC-e (mod 65)
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

    tipo_mov = "Saída" if (mod == "65" or tp_nf == "1") else "Entrada"

    itens = []
    for idx_det, det in enumerate(inf_nfe.findall("det"), start=1):
        prod = det.find("prod")
        if prod is None:
            continue

        num_item = det.attrib.get("nItem", str(idx_det))
        c_ean = _padronizar_ean(prod.findtext("cEAN"))
        c_ean_trib = _padronizar_ean(prod.findtext("cEANTrib"))
        c_prod = (prod.findtext("cProd") or "").strip()

        ean_valido = c_ean or c_ean_trib
        sku = ean_valido if ean_valido else c_prod
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
                "Item": num_item,
                "Modelo": mod,
                "Tipo": tipo_mov,
                "Data_Emissao": data_emissao,
                "Data": data_emissao,
                "SKU": sku,
                "Codigo_Fornecedor": c_prod,
                "EAN": ean_valido,
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


def _processar_upload_planilha(arquivo_enviado, origem_label: str, key_prefix: str):
    """Lê, normaliza por EAN e permite mapeamento manual caso o cliente envie colunas com nomes diferentes."""
    try:
        df_bruto = _carregar_planilha_universal(arquivo_enviado)
        df_norm = _normalizar_colunas_planilha(df_bruto)

        if "SKU" not in df_norm.columns or "Qtd Inicial" not in df_norm.columns:
            st.warning(
                "Algumas colunas vieram com nomes diferentes do padrão. "
                "Selecione abaixo quais colunas correspondem ao **EAN / Código de Barras** e ao **Saldo Inicial**:"
            )
            colunas_originais = list(df_bruto.columns)
            c_m1, c_m2, c_m3 = st.columns(3)
            col_sku = c_m1.selectbox("Coluna de EAN / Código de Barras:", colunas_originais, key=f"{key_prefix}_sku")
            col_qtd = c_m2.selectbox("Coluna de Qtd Inicial / Saldo:", colunas_originais, key=f"{key_prefix}_qtd")
            col_desc = c_m3.selectbox(
                "Coluna de Descrição (opcional):",
                ["(Nenhuma)"] + colunas_originais,
                key=f"{key_prefix}_desc",
            )

            if st.button("✅ Confirmar Mapeamento de Colunas", key=f"{key_prefix}_btn"):
                mapa_manual = {col_sku: "SKU", col_qtd: "Qtd Inicial"}
                if col_desc != "(Nenhuma)":
                    mapa_manual[col_desc] = "Descrição"
                df_norm = _normalizar_colunas_planilha(df_bruto.rename(columns=mapa_manual))
            else:
                st.dataframe(df_bruto.head(5), use_container_width=True, hide_index=True)
                return

        if "SKU" in df_norm.columns and "Qtd Inicial" in df_norm.columns:
            st.session_state["ponto_partida"] = {
                "origem": origem_label,
                "data_corte": str(df_norm["Ref_Data"].iloc[0]) if "Ref_Data" in df_norm.columns and len(df_norm) > 0 else "",
                "df_base_proximo_ciclo": df_norm,
            }
            st.success(f"Planilha carregada com sucesso ({len(df_norm)} EANs reconhecidos)! Avance para a Etapa 2.")
            st.dataframe(df_norm.head(15), use_container_width=True, hide_index=True)

    except Exception as e:
        st.error(f"Erro ao ler a planilha: {e}")


def _renderizar_etapa_1_ponto_partida():
    st.header("1. Ponto de Partida (Estoque Inicial ou Ciclo Contínuo)")
    st.caption(
        "Escolha iniciar por uma planilha de contagem inicial (por EAN) ou continuar a partir do arquivo exportado no ciclo anterior."
    )

    opcao = st.radio(
        "Como deseja iniciar este ciclo?",
        options=[
            "📄 Subir Planilha de Contagem Inicial (.XLSX, .CSV, .ODS)",
            "📂 Continuar do Ciclo Anterior via Arquivo (.CSV ou .XLSX)",
        ],
        horizontal=True,
    )

    if opcao.startswith("📂 Continuar"):
        arq_ciclo = st.file_uploader(
            "Suba o arquivo do fechamento anterior (.CSV, .XLSX, .XLS ou .ODS):",
            type=["csv", "xlsx", "xls", "ods"],
            key="up_ciclo_anterior",
        )
        if arq_ciclo is not None:
            _processar_upload_planilha(arq_ciclo, "ciclo_anterior", "ciclo")
    else:
        col1, col2 = st.columns(2)
        arq_ini = col1.file_uploader(
            "Opcional: Planilha de Contagem Inicial (.XLSX, .XLS, .CSV ou .ODS):",
            type=["csv", "xlsx", "xls", "ods"],
            key="up_estoque_inicial",
        )
        modelo_df = pd.DataFrame(
            [
                {"EAN": "7891000100103", "Descrição": "LEITE INTEGRAL 1L", "Categoria": "Laticínios", "Marca": "Marca A", "Qtd Inicial": 48},
                {"EAN": "7891991010856", "Descrição": "CERVEJA LATA 350ML", "Categoria": "Bebidas", "Marca": "Marca B", "Qtd Inicial": 120},
            ]
        )
        col2.download_button(
            label="📥 Baixar Modelo de Planilha Inicial (.CSV)",
            data=modelo_df.to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name="modelo_estoque_inicial.csv",
            mime="text/csv",
        )

        if arq_ini is not None:
            _processar_upload_planilha(arq_ini, "contagem_inicial", "inicial")


def _gravar_ajuste_manual(sku_alvo: str, desc_alvo: str, motivo_aj: str, qtd_aj: float, data_aj):
    """Efetiva o lançamento do ajuste manual nas movimentações e no consolidado."""
    eh_entrada = motivo_aj.endswith("(+)")
    tipo_mov_aj = "Entrada" if eh_entrada else "Saída"
    data_str = data_aj.strftime("%Y-%m-%d")

    nova_mov = pd.DataFrame([{
        "Chave_NFe": f"AJUSTE_{datetime.now().strftime('%H%M%S')}",
        "Item": "1",
        "Modelo": "AJ",
        "Tipo": tipo_mov_aj,
        "Data_Emissao": data_str,
        "Data": data_str,
        "SKU": sku_alvo,
        "Codigo_Fornecedor": sku_alvo,
        "EAN": sku_alvo,
        "Descrição": desc_alvo,
        "Unidade": "UN",
        "Quantidade": float(qtd_aj),
        "Valor_Unitario": 0.0,
        "Valor_Total": 0.0,
    }])

    df_movs_atual = st.session_state.get("df_movs_detalhadas", pd.DataFrame())
    st.session_state["df_movs_detalhadas"] = pd.concat([df_movs_atual, nova_mov], ignore_index=True)

    df_cons_atual = st.session_state["df_xml_consolidado"].copy()
    mask = df_cons_atual["SKU"] == sku_alvo
    if eh_entrada:
        df_cons_atual.loc[mask, "Total_Entradas"] += float(qtd_aj)
    else:
        df_cons_atual.loc[mask, "Total_Saidas"] += float(qtd_aj)

    df_cons_atual["Saldo_Calculado"] = (
        df_cons_atual["Qtd Inicial"]
        + df_cons_atual["Total_Entradas"]
        - df_cons_atual["Total_Saidas"]
    )
    st.session_state["df_xml_consolidado"] = df_cons_atual
    st.session_state["msg_sucesso_ajuste"] = (
        f"Ajuste aplicado! **{motivo_aj}** de **{qtd_aj:g} un** registrado para "
        f"**{desc_alvo}** em {data_aj.strftime('%d/%m/%Y')}."
    )


@st.dialog("Confirmar Lançamento")
def _modal_confirmar_ajuste(sku_alvo: str, desc_alvo: str, motivo_aj: str, qtd_aj: float, data_aj):
    """Exibe a caixa de confirmação ao pressionar Enter na quantidade ou clicar no botão."""
    st.markdown("### Deseja realmente lançar esse ajuste manual?")
    st.info(
        f"**Produto:** {sku_alvo} — {desc_alvo}\n\n"
        f"**Motivo:** {motivo_aj}\n\n"
        f"**Quantidade:** {qtd_aj:g} un\n\n"
        f"**Data:** {data_aj.strftime('%d/%m/%Y')}"
    )
    col_sim, col_nao = st.columns(2)
    if col_sim.button("✅ Sim, confirmar", type="primary", use_container_width=True):
        _gravar_ajuste_manual(sku_alvo, desc_alvo, motivo_aj, qtd_aj, data_aj)
        st.rerun()
    if col_nao.button("❌ Cancelar", use_container_width=True):
        st.rerun()


def _renderizar_etapa_2_xmls():
    st.header("2. Processamento de XMLs (Cruzamento por EAN)")
    st.caption(
        "Suba os arquivos XML individuais ou pastas compactadas (.ZIP) das notas de compra (NF-e) e vendas no caixa (NFC-e / SAT)."
    )

    c_ent, c_sai = st.columns(2)
    files_entrada = c_ent.file_uploader(
        "📦 XMLs / ZIPs de ENTRADA (Compras de Fornecedores):",
        type=["xml", "zip"],
        accept_multiple_files=True,
        key="up_xml_entradas",
    )
    files_saida = c_sai.file_uploader(
        "🧾 XMLs / ZIPs de SAÍDA (Vendas / NFC-e / SAT):",
        type=["xml", "zip"],
        accept_multiple_files=True,
        key="up_xml_saidas",
    )

    if st.button("🚀 Processar XMLs e Consolidar Estoque por EAN", type="primary", use_container_width=True):
        if not files_entrada and not files_saida:
            st.warning("Envie pelo menos um arquivo XML ou ZIP de entrada ou saída.")
            return

        with st.spinner("Lendo notas fiscais e consolidando movimentações por EAN..."):
            todos_itens = []
            chaves_vistas = set()

            for arq_nome, arq_bytes in _extrair_arquivos_xml(files_entrada or []):
                chave, itens = _ler_xml_nfe_nfce(arq_bytes, arq_nome)
                chave_grupo = (chave, "Entrada")
                if chave and chave_grupo not in chaves_vistas:
                    chaves_vistas.add(chave_grupo)
                    for it in itens:
                        it["Tipo"] = "Entrada"
                        todos_itens.append(it)

            for arq_nome, arq_bytes in _extrair_arquivos_xml(files_saida or []):
                chave, itens = _ler_xml_nfe_nfce(arq_bytes, arq_nome)
                chave_grupo = (chave, "Saída")
                if chave and chave_grupo not in chaves_vistas:
                    chaves_vistas.add(chave_grupo)
                    for it in itens:
                        it["Tipo"] = "Saída"
                        todos_itens.append(it)

            if not todos_itens:
                st.error("Nenhum item válido encontrado nos XMLs enviados.")
                return

            df_movs = pd.DataFrame(todos_itens)

            if "Item" in df_movs.columns and "Chave_NFe" in df_movs.columns:
                df_movs = df_movs.drop_duplicates(subset=["Chave_NFe", "Tipo", "Item"], keep="first")

            df_movs["SKU"] = df_movs["SKU"].astype(str).str.strip()

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
            for col_num in ["Total_Entradas", "Valor_Entradas", "Custo_Unitario", "Total_Saidas", "Valor_Saidas", "Preco_Venda"]:
                df_cons[col_num] = df_cons[col_num].fillna(0.0)

            df_cons["Categoria"] = "Geral"
            df_cons["Marca"] = "Sem Marca"
            df_cons["Qtd Inicial"] = 0.0
            df_cons["Fator_Conversao"] = 1.0

            snap = st.session_state.get("ponto_partida")
            if snap and "df_base_proximo_ciclo" in snap:
                df_ref = snap["df_base_proximo_ciclo"].copy()
                ean_ref = df_ref["SKU"].apply(_padronizar_ean)
                raw_ref = df_ref["SKU"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
                df_ref["SKU"] = ean_ref.where(ean_ref != "", raw_ref)

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

                cols_merge = [
                    c for c in ["SKU", "Qtd Inicial", "Categoria", "Marca", "Fator_Conversao", "Descrição"]
                    if c in df_ref_unico.columns
                ]
                df_cons = df_cons.drop(columns=[c for c in cols_merge if c not in ("SKU", "Descrição") and c in df_cons.columns])

                df_cons = df_cons.merge(df_ref_unico[cols_merge], on="SKU", how="outer")

                if "Descrição_x" in df_cons.columns and "Descrição_y" in df_cons.columns:
                    df_cons["Descrição"] = df_cons["Descrição_y"].fillna(df_cons["Descrição_x"])
                    df_cons = df_cons.drop(columns=["Descrição_x", "Descrição_y"])
                df_cons["Descrição"] = df_cons["Descrição"].fillna("Item do Saldo Inicial")

                for col_num in ["Qtd Inicial", "Total_Entradas", "Valor_Entradas", "Custo_Unitario", "Total_Saidas", "Valor_Saidas", "Preco_Venda"]:
                    if col_num in df_cons.columns:
                        df_cons[col_num] = df_cons[col_num].fillna(0.0)
                df_cons["Categoria"] = df_cons["Categoria"].fillna("Geral")
                df_cons["Marca"] = df_cons["Marca"].fillna("Sem Marca")
                df_cons["Fator_Conversao"] = df_cons["Fator_Conversao"].fillna(1.0)

            df_cons = df_cons.drop_duplicates(subset=["SKU"], keep="first").reset_index(drop=True)
            df_cons["Saldo_Calculado"] = df_cons["Qtd Inicial"] + df_cons["Total_Entradas"] - df_cons["Total_Saidas"]

            for k_bak in ["_Backup_Movs_Original", "_Backup_Consolidado_Original"]:
                if k_bak in st.session_state:
                    del st.session_state[k_bak]

            st.session_state["df_xml_consolidado"] = df_cons
            st.session_state["df_movs_detalhadas"] = df_movs
            st.session_state["chaves_xml_lidas"] = chaves_vistas

            qtd_itens_saida = len(df_movs[df_movs["Tipo"] == "Saída"])
            qtd_itens_entrada = len(df_movs[df_movs["Tipo"] == "Entrada"])
            st.success(
                f"Processamento concluído! {len(chaves_vistas)} notas lidas "
                f"({qtd_itens_entrada} linhas de entrada e {qtd_itens_saida} linhas de saída) — {len(df_cons)} EANs consolidados."
            )

    if not st.session_state["df_xml_consolidado"].empty:
        df_exib = st.session_state["df_xml_consolidado"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Notas Fiscais Lidas", len(st.session_state["chaves_xml_lidas"]))
        m2.metric("EANs Consolidados", len(df_exib))
        m3.metric("Total Entradas (UN)", f"{df_exib['Total_Entradas'].sum():,.0f}".replace(",", "."))
        m4.metric("Total Saídas (UN)", f"{df_exib['Total_Saidas'].sum():,.0f}".replace(",", "."))
        st.dataframe(df_exib.head(20), use_container_width=True, hide_index=True)

        st.divider()
        if "msg_sucesso_ajuste" in st.session_state:
            st.success(st.session_state.pop("msg_sucesso_ajuste"))

        with st.expander("🛠️ Lançar Ajuste Manual / Quebra / Vencimento (Sem Nota Fiscal)", expanded=True):
            st.caption(
                "Registre perdas (garrafa quebrada, produto vencido, consumo interno) "
                "ou acertos de contagem física. Digite a quantidade e pressione **Enter** para confirmar."
            )
            opcoes_sku = {
                f"{row['SKU']} — {row['Descrição']} (Saldo atual: {row['Saldo_Calculado']:.0f})": (row["SKU"], row["Descrição"])
                for _, row in df_exib.iterrows()
            }

            with st.form("form_ajuste_manual", border=False):
                c_aj1, c_aj2, c_aj3, c_aj4 = st.columns([3, 1.5, 1.5, 1.5])
                item_sel = c_aj1.selectbox("Produto / EAN:", list(opcoes_sku.keys()), key="aj_sku_sel")
                motivo_aj = c_aj2.selectbox(
                    "Motivo:",
                    ["Quebra / Avaria (-)", "Vencimento (-)", "Consumo Interno (-)", "Acerto Contagem (+)"],
                    key="aj_motivo",
                )
                qtd_aj = c_aj3.number_input("Quantidade:", min_value=0.01, value=1.0, step=1.0, key="aj_qtd")
                data_aj = c_aj4.date_input("Data da ocorrência:", value=datetime.now().date(), format="DD/MM/YYYY", key="aj_data")

                enviou_form = st.form_submit_button("✅ Aplicar Ajuste no Estoque", type="primary")

            if enviou_form:
                sku_alvo, desc_alvo = opcoes_sku[item_sel]
                _modal_confirmar_ajuste(sku_alvo, desc_alvo, motivo_aj, qtd_aj, data_aj)


def main():
    _inicializar_estado()

    with st.sidebar:
        st.title("🛒 Varejo Inteligente")
        st.caption("Copiloto de Compras e Prevenção de Ruptura (Modo Testes)")
        st.divider()
        loja_nome = st.text_input("Loja / Unidade em Análise:", value="Loja 01 - Matriz")
        st.info(
            "**Fluxo Recomendado:**\n"
            "1. Defina o Ponto de Partida (EAN)\n"
            "2. Processe os XMLs\n"
            "3. Ajuste Caixas → Unidades\n"
            "4. Analise Compras e Rupturas\n"
            "5. Exporte o Fechamento (.CSV)"
        )

    st.title(f"Gestão Inteligente de Estoque — {loja_nome}")

    nomes_abas = [
        "1. Ponto de Partida",
        "2. Processar XMLs",
        "3. De-Para (Conversão CX→UN)",
        "4. Indicadores e Kardex",
        "5. Fechamento e Resumo",
    ]
    if TEM_ABA_ENTRADAS_SAIDAS:
        nomes_abas.append("6. Entradas x Saídas")
    if TEM_ABA_COLETA:
        nomes_abas.append("7. Coleta Rápida")

    abas = st.tabs(nomes_abas)

    with abas[0]:
        _renderizar_etapa_1_ponto_partida()

    with abas[1]:
        _renderizar_etapa_2_xmls()

    with abas[2]:
        renderizar_aba_conversao()

    with abas[3]:
        renderizar_aba_indicadores()

    with abas[4]:
        renderizar_aba_fechamento(loja_nome)

    idx_extra = 5
    if TEM_ABA_ENTRADAS_SAIDAS:
        with abas[idx_extra]:
            renderizar_aba_entradas_saidas()
        idx_extra += 1

    if TEM_ABA_COLETA:
        with abas[idx_extra]:
            renderizar_aba_coleta()


if __name__ == "__main__":
    main()
