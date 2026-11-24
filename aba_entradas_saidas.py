from datetime import datetime
import io
import unicodedata
import xml.etree.ElementTree as ET
import pandas as pd
import streamlit as st


def _strip_ns(tag: str) -> str:
    """Remove o namespace XML da tag (ex: {http://portalfiscal.inf.br}infNFe -> infNFe)."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_child(elem, tag_name: str):
    """Busca o primeiro filho direto correspondente ao nome da tag sem namespace."""
    if elem is None:
        return None
    for child in elem:
        if _strip_ns(child.tag) == tag_name:
            return child
    return None


def _get_text(elem, tag_name: str, default: str = "") -> str:
    """Retorna o texto de uma subtag direta ou valor padrão."""
    child = _find_child(elem, tag_name)
    if child is not None and child.text:
        return child.text.strip()
    return default


def _normalizar_texto_coluna(nome: str) -> str:
    """Remove acentos, espaços extras e converte para minúsculas para localizar colunas com segurança."""
    texto = unicodedata.normalize("NFKD", str(nome)).encode("ASCII", "ignore").decode("ASCII")
    return texto.strip().lower()


def _normalizar_codigo(valor: str) -> str:
    """Normaliza código SKU/EAN (remove espaços e zeros à esquerda opcionais)."""
    if valor is None:
        return ""
    texto = str(valor).strip().upper()
    if not texto or texto == "SEM GTIN":
        return ""
    return texto.lstrip("0") or texto


def _converter_numero(serie: pd.Series) -> pd.Series:
    """Converte strings numéricas em formato brasileiro (1.250,50) ou americano (1250.50) para float."""
    def _parse(val):
        if pd.isna(val):
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if not s:
            return 0.0
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return 0.0

    return serie.apply(_parse)


def _padronizar_colunas_base(df: pd.DataFrame, origem: str) -> tuple[pd.DataFrame, str]:
    """
    Padroniza a coluna de identificação como 'SKU', garante 'Descrição', 'Categoria',
    'Marca' e 'Fator_Conversao' (padrão 1.0) e define a coluna de quantidade inicial.
    O cruzamento continua sendo feito exclusivamente pela chave 'SKU'.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    mapa_norm = {_normalizar_texto_coluna(c): c for c in df.columns}

    # 1. Localiza a coluna de identificação (SKU / EAN)
    if "SKU" not in df.columns:
        candidatos_sku = ["sku", "ean", "codigo", "sku / ean", "cod", "cprod", "cean"]
        col_sku = next((mapa_norm[k] for k in candidatos_sku if k in mapa_norm), None)
        if col_sku is None and len(df.columns) > 0:
            col_sku = df.columns[0]
        if col_sku:
            df = df.rename(columns={col_sku: "SKU"})

    df["SKU"] = df["SKU"].astype(str).str.strip()

    # 2. Garante Descrição, Categoria e Marca vindas do CSV ou da coleta
    candidatos_desc = ["descricao", "produto", "nome", "item", "xprod"]
    col_desc = next((mapa_norm[k] for k in candidatos_desc if k in mapa_norm), None)
    if col_desc and col_desc != "Descrição":
        df = df.rename(columns={col_desc: "Descrição"})
    if "Descrição" not in df.columns:
        df["Descrição"] = "Item sem descrição"
    df["Descrição"] = (
        df["Descrição"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace({"": "Item sem descrição", "nan": "Item sem descrição", "None": "Item sem descrição"})
    )

    candidatos_cat = ["categoria", "categoria / setor", "setor", "departamento", "grupo", "linha"]
    col_cat = next((mapa_norm[k] for k in candidatos_cat if k in mapa_norm), None)
    if col_cat and col_cat != "Categoria":
        df = df.rename(columns={col_cat: "Categoria"})
    if "Categoria" not in df.columns:
        df["Categoria"] = "Geral"
    df["Categoria"] = (
        df["Categoria"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace({"": "Geral", "nan": "Geral", "None": "Geral"})
    )

    candidatos_marca = ["marca", "fabricante", "brand", "fornecedor"]
    col_marca = next((mapa_norm[k] for k in candidatos_marca if k in mapa_norm), None)
    if col_marca and col_marca != "Marca":
        df = df.rename(columns={col_marca: "Marca"})
    if "Marca" not in df.columns:
        df["Marca"] = "Sem Marca"
    df["Marca"] = (
        df["Marca"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace({"": "Sem Marca", "nan": "Sem Marca", "None": "Sem Marca"})
    )

    # 3. Garante a coluna Fator_Conversao (Caixa -> Unidade)
    candidatos_fator = [
        "fator_conversao",
        "fator conversao",
        "fator",
        "embalagem",
        "qtd_emb",
        "qtd emb",
        "multiplicador",
        "mult",
    ]
    col_fator = next((mapa_norm[k] for k in candidatos_fator if k in mapa_norm), None)
    if col_fator and col_fator != "Fator_Conversao":
        df = df.rename(columns={col_fator: "Fator_Conversao"})

    if "Fator_Conversao" in df.columns:
        fatores = _converter_numero(df["Fator_Conversao"])
        df["Fator_Conversao"] = fatores.where(fatores > 0, 1.0)
    else:
        df["Fator_Conversao"] = 1.0

    # 4. Tratamento específico por origem
    if origem == "csv":
        candidatos_inicial = [
            "qtd inicial",
            "qtd_inicial",
            "quantidade inicial",
            "qtd fisica",
            "quantidade",
            "qtd",
            "saldo",
            "saldo inicial",
            "estoque",
            "estoque inicial",
        ]
        col_origem_qtd = next((mapa_norm[k] for k in candidatos_inicial if k in mapa_norm), None)

        if col_origem_qtd:
            valores_qtd = _converter_numero(df[col_origem_qtd])
        else:
            valores_qtd = pd.Series([0.0] * len(df), index=df.index)

        if "Qtd Inicial" in df.columns:
            df["Qtd Inicial"] = valores_qtd
        df["Qtd Física"] = valores_qtd
        return df, "Qtd Física"

    # Origem pela sessão atual ('sessao') ou Ponto de Partida ('ponto_partida')
    nome_alvo_qtd = "Qtd Inicial"
    if nome_alvo_qtd not in df.columns:
        candidatos_qtd = [
            "qtd fisica",
            "qtd inicial",
            "qtd_inicial",
            "quantidade inicial",
            "quantidade",
            "qtd",
            "saldo",
            "estoque",
        ]
        col_qtd = next((mapa_norm[k] for k in candidatos_qtd if k in mapa_norm), None)
        if col_qtd:
            df = df.rename(columns={col_qtd: nome_alvo_qtd})
        else:
            df[nome_alvo_qtd] = 0.0

    df[nome_alvo_qtd] = _converter_numero(df[nome_alvo_qtd])
    return df, nome_alvo_qtd


def _extrair_chaves_item(prod_elem) -> list[str]:
    """
    Coleta todos os códigos possíveis do item no XML (<cProd>, <cEAN>, <cEANTrib>)
    para garantir o cruzamento exclusivamente pelo SKU/EAN cadastrado.
    """
    candidatos = []
    for tag in ("cProd", "cEAN", "cEANTrib"):
        bruto = _get_text(prod_elem, tag)
        if bruto and bruto.upper() != "SEM GTIN":
            candidatos.append(bruto)
            norm = _normalizar_codigo(bruto)
            if norm and norm not in candidatos:
                candidatos.append(norm)
    return candidatos


def processar_arquivo_xml(arquivo_xml, codigos_monitorados_map: dict, cnpj_loja: str = ""):
    """
    Lê um arquivo XML de NF-e (modelo 55) ou NFC-e (modelo 65) e extrai
    as movimentações, datas de emissão e valores totais dos itens presentes na lista monitorada.
    """
    movimentacoes = []
    try:
        arquivo_xml.seek(0)
        conteudo = arquivo_xml.read()
        root = ET.fromstring(conteudo)
    except Exception as e:
        return [], f"Erro ao ler XML '{arquivo_xml.name}': {e}"

    inf_nfe = None
    for elem in root.iter():
        if _strip_ns(elem.tag) == "infNFe":
            inf_nfe = elem
            break

    if inf_nfe is None:
        return [], None

    ide = _find_child(inf_nfe, "ide")
    emit = _find_child(inf_nfe, "emit")
    dest = _find_child(inf_nfe, "dest")

    modelo = _get_text(ide, "mod")  # '55' = NF-e, '65' = NFC-e
    tp_nf = _get_text(ide, "tpNF")  # '0' = Entrada, '1' = Saída (na visão do emitente)
    fin_nfe = _get_text(ide, "finNFe")  # '1' = Normal, '4' = Devolução

    dh_emi_bruta = _get_text(ide, "dhEmi") or _get_text(ide, "dEmi")
    data_emissao = dh_emi_bruta[:10] if len(dh_emi_bruta) >= 10 else ""

    cnpj_emit = "".join(filter(str.isdigit, _get_text(emit, "CNPJ")))
    cnpj_dest = "".join(filter(str.isdigit, _get_text(dest, "CNPJ")))
    cnpj_loja_limpo = "".join(filter(str.isdigit, cnpj_loja))

    if cnpj_loja_limpo:
        if cnpj_dest == cnpj_loja_limpo and cnpj_emit != cnpj_loja_limpo:
            tipo_mov = "Entrada"
        elif cnpj_emit == cnpj_loja_limpo:
            tipo_mov = "Entrada" if tp_nf == "0" or fin_nfe == "4" else "Saída"
        else:
            tipo_mov = "Entrada" if (modelo == "55" and tp_nf == "1") else "Saída"
    else:
        if modelo == "65":
            tipo_mov = "Entrada" if fin_nfe == "4" else "Saída"
        else:
            tipo_mov = "Entrada"

    for elem in inf_nfe:
        if _strip_ns(elem.tag) != "det":
            continue

        prod = _find_child(elem, "prod")
        if prod is None:
            continue

        chave_original_encontrada = None
        for candidato in _extrair_chaves_item(prod):
            if candidato in codigos_monitorados_map:
                chave_original_encontrada = codigos_monitorados_map[candidato]
                break

        if not chave_original_encontrada:
            continue

        try:
            qtd = float(_get_text(prod, "qCom", "0").replace(",", "."))
        except ValueError:
            qtd = 0.0

        try:
            valor_total = float(_get_text(prod, "vProd", "0").replace(",", "."))
        except ValueError:
            valor_total = 0.0

        movimentacoes.append(
            {
                "SKU": chave_original_encontrada,
                "Tipo": tipo_mov,
                "Data_Emissao": data_emissao,
                "Quantidade": qtd,
                "Valor_Total": valor_total,
            }
        )

    return movimentacoes, None


def renderizar_aba_xml():
    st.header("📄 Processamento de XMLs (NF-e 55 e NFC-e 65)")
    st.caption(
        "Cruza a base de itens coletados (por SKU ou EAN) com os XMLs de entrada e saída para consolidar o saldo."
    )

    st.subheader("1. Base de SKUs Monitorados")
    opcoes_fonte = [
        "Usar itens coletados na sessão atual",
        "Enviar planilha CSV (coleta ou ciclo anterior)",
    ]
    if "ponto_partida" in st.session_state and st.session_state["ponto_partida"] is not None:
        opcoes_fonte.insert(0, "Continuar do Ponto de Partida salvo (Próximo Ciclo)")

    fonte_base = st.radio(
        "Origem da lista de produtos:",
        options=opcoes_fonte,
        horizontal=True,
    )

    df_base = None
    origem_tipo = "sessao"
    col_qtd_inicial = "Qtd Inicial"

    if fonte_base == "Continuar do Ponto de Partida salvo (Próximo Ciclo)":
        origem_tipo = "ponto_partida"
        snap = st.session_state["ponto_partida"]
        df_base, col_qtd_inicial = _padronizar_colunas_base(
            snap["df_base_proximo_ciclo"], origem="ponto_partida"
        )
        st.success(
            f"Ponto de Partida de **{snap['data_registro']}** carregado com **{len(df_base)}** SKU(s). "
            "O saldo final anterior assumiu o lugar de **Qtd Inicial**."
        )
    elif fonte_base == "Usar itens coletados na sessão atual":
        origem_tipo = "sessao"
        itens_sessao = st.session_state.get("itens_coletados", {})
        if itens_sessao:
            df_base, col_qtd_inicial = _padronizar_colunas_base(
                pd.DataFrame(list(itens_sessao.values())), origem="sessao"
            )
            st.success(f"Base ativa carregada com **{len(df_base)}** SKU(s) da sessão.")
        else:
            st.warning(
                "Nenhum item coletado na sessão atual. Colete itens na primeira aba ou envie um arquivo CSV."
            )
    else:
        origem_tipo = "csv"
        arquivo_csv = st.file_uploader(
            "Selecione o arquivo .CSV (da coleta ou 'base_proximo_ciclo')",
            type=["csv"],
            key="upload_csv_base_xml",
        )
        if arquivo_csv is not None:
            try:
                arquivo_csv.seek(0)
                conteudo_bytes = arquivo_csv.read()
                texto_csv = conteudo_bytes.decode("utf-8-sig")
                separador = ";" if ";" in texto_csv.splitlines()[0] else ","
                df_lido = pd.read_csv(io.StringIO(texto_csv), sep=separador, dtype=str)
                df_base, col_qtd_inicial = _padronizar_colunas_base(df_lido, origem="csv")

                # Se for um arquivo exportado como Ponto de Partida, restaura a referência de comparação
                if "Ref_Faturamento" in df_lido.columns and len(df_lido) > 0:
                    primeira = df_lido.iloc[0]
                    st.session_state["ponto_partida"] = {
                        "data_registro": str(primeira.get("Ref_Data", "Ciclo Anterior")),
                        "faturamento_total": float(str(primeira.get("Ref_Faturamento", 0)).replace(",", ".") or 0),
                        "valor_estoque_total": float(str(primeira.get("Ref_Capital", 0)).replace(",", ".") or 0),
                        "qtd_ruptura": int(float(str(primeira.get("Ref_Ruptura_Qtd", 0)).replace(",", ".") or 0)),
                        "perda_ruptura_total": float(str(primeira.get("Ref_Perda_Ruptura", 0)).replace(",", ".") or 0),
                        "df_base_proximo_ciclo": df_base.copy(),
                        "df_indicadores_ref": df_lido.copy(),
                    }
                    st.info("📌 Arquivo de **Ponto de Partida** detectado! Os indicadores anteriores serão usados para comparação.")

                st.success(f"CSV carregado com {len(df_base)} SKU(s).")
            except Exception as e:
                st.error(f"Erro ao ler o arquivo CSV: {e}")

    if df_base is None or df_base.empty:
        return

    with st.expander("Visualizar / Editar SKUs, Categoria, Marca, Saldo Inicial e Fator de Conversão", expanded=False):
        st.caption(
            "O cruzamento com os XMLs é feito sempre pelo **SKU**. Aqui você pode ajustar **Categoria**, "
            "**Marca**, **Fator_Conversao** ou corrigir pontualmente o saldo inicial."
        )
        colunas_editaveis = {col_qtd_inicial, "Fator_Conversao", "Categoria", "Marca"}
        colunas_bloqueadas = [c for c in df_base.columns if c not in colunas_editaveis]
        df_base = st.data_editor(
            df_base,
            disabled=colunas_bloqueadas,
            column_config={
                "Categoria": st.column_config.TextColumn("Categoria"),
                "Marca": st.column_config.TextColumn("Marca"),
                "Fator_Conversao": st.column_config.NumberColumn(
                    "Fator Conv. (Cx -> Un)",
                    min_value=0.01,
                    step=1.0,
                    format="%.2f",
                ),
                col_qtd_inicial: st.column_config.NumberColumn(
                    col_qtd_inicial,
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                ),
            },
            use_container_width=True,
            hide_index=True,
            key=f"editor_base_xml_{origem_tipo}",
        )

    st.subheader("2. Upload dos Arquivos XML (NF-e e NFC-e)")
    cnpj_loja = st.text_input(
        "CNPJ da Loja (opcional — ajuda a distinguir compras de vendas em NF-e modelo 55)",
        placeholder="Ex: 11.111.111/0001-91 (deixe vazio para considerar NF-e 55 = Entrada e NFC-e 65 = Saída)",
    )
    arquivos_xml = st.file_uploader(
        "Selecione um ou mais arquivos .XML",
        type=["xml"],
        accept_multiple_files=True,
        key="upload_multiplos_xml",
    )

    if not arquivos_xml:
        return

    if st.button("⚙️ Processar XMLs e Gerar Consolidado", type="primary", use_container_width=True):
        df_base, col_qtd_inicial = _padronizar_colunas_base(df_base, origem=origem_tipo)
        codigos_map = {}
        for sku_val in df_base["SKU"].astype(str):
            sku_limpo = sku_val.strip()
            codigos_map[sku_limpo] = sku_limpo
            norm = _normalizar_codigo(sku_limpo)
            if norm:
                codigos_map[norm] = sku_limpo

        todas_movs = []
        avisos = []
        with st.spinner(f"Processando {len(arquivos_xml)} arquivo(s) XML..."):
            for arq in arquivos_xml:
                movs, erro = processar_arquivo_xml(arq, codigos_map, cnpj_loja)
                if erro:
                    avisos.append(erro)
                todas_movs.extend(movs)

        for aviso in avisos:
            st.warning(aviso)

        df_movs = pd.DataFrame(todas_movs)
        df_resultado = df_base.copy()

        # Enriquecimento das movimentações detalhadas com os atributos do SKU
        if not df_movs.empty:
            mapa_meta = (
                df_resultado.drop_duplicates(subset=["SKU"])
                .set_index("SKU")[["Descrição", "Categoria", "Marca"]]
                .to_dict("index")
            )
            df_movs["Descrição"] = df_movs["SKU"].map(lambda s: mapa_meta.get(s, {}).get("Descrição", ""))
            df_movs["Categoria"] = df_movs["SKU"].map(lambda s: mapa_meta.get(s, {}).get("Categoria", "Geral"))
            df_movs["Marca"] = df_movs["SKU"].map(lambda s: mapa_meta.get(s, {}).get("Marca", "Sem Marca"))

            # Aplica o Fator de Conversão (Caixa -> Unidade) nas movimentações de Entrada
            mapa_fator = dict(zip(df_resultado["SKU"], df_resultado["Fator_Conversao"]))
            df_movs["Fator_Aplicado"] = df_movs.apply(
                lambda r: mapa_fator.get(r["SKU"], 1.0) if r["Tipo"] == "Entrada" else 1.0,
                axis=1,
            )
            df_movs["Quantidade_Original_XML"] = df_movs["Quantidade"]
            df_movs["Quantidade"] = df_movs["Quantidade"] * df_movs["Fator_Aplicado"]

        st.session_state["df_movs_detalhadas"] = df_movs

        # Remove colunas calculadas anteriormente para evitar duplicidade ao reprocessar
        colunas_antigas = (
            "Total_Entradas",
            "Total_Saidas",
            "Valor_Entradas",
            "Valor_Saidas",
            "Ultima_Entrada",
            "Ultima_Saida",
            "Saldo_Calculado",
        )
        for col_antiga in colunas_antigas:
            if col_antiga in df_resultado.columns:
                df_resultado = df_resultado.drop(columns=[col_antiga])

        # Agrupamento e cruzamento feitos exclusivamente por SKU
        if not df_movs.empty:
            df_entradas = df_movs[df_movs["Tipo"] == "Entrada"]
            df_saidas = df_movs[df_movs["Tipo"] == "Saída"]
            entradas = df_entradas.groupby("SKU", as_index=False).agg(
                Total_Entradas=("Quantidade", "sum"),
                Valor_Entradas=("Valor_Total", "sum"),
                Ultima_Entrada=("Data_Emissao", "max"),
            )
            saidas = df_saidas.groupby("SKU", as_index=False).agg(
                Total_Saidas=("Quantidade", "sum"),
                Valor_Saidas=("Valor_Total", "sum"),
                Ultima_Saida=("Data_Emissao", "max"),
            )
            df_resultado = df_resultado.merge(entradas, on="SKU", how="left")
            df_resultado = df_resultado.merge(saidas, on="SKU", how="left")
        else:
            df_resultado["Total_Entradas"] = 0.0
            df_resultado["Total_Saidas"] = 0.0
            df_resultado["Valor_Entradas"] = 0.0
            df_resultado["Valor_Saidas"] = 0.0
            df_resultado["Ultima_Entrada"] = ""
            df_resultado["Ultima_Saida"] = ""
            st.warning(
                "Nenhuma movimentação encontrada nos XMLs enviados para os SKUs cadastrados. "
                "Verifique se o código bipado na coleta corresponde ao <cProd> ou <cEAN> da nota."
            )

        # Garante tipagem numérica e preenche valores nulos
        colunas_numericas = [
            col_qtd_inicial,
            "Fator_Conversao",
            "Total_Entradas",
            "Total_Saidas",
            "Valor_Entradas",
            "Valor_Saidas",
        ]
        for col_num in colunas_numericas:
            df_resultado[col_num] = _converter_numero(df_resultado[col_num].fillna(0.0))

        df_resultado["Ultima_Entrada"] = df_resultado["Ultima_Entrada"].fillna("")
        df_resultado["Ultima_Saida"] = df_resultado["Ultima_Saida"].fillna("")

        # Saldo_Calculado já considera Total_Entradas multiplicado pelo Fator_Conversao
        df_resultado["Saldo_Calculado"] = (
            df_resultado[col_qtd_inicial] + df_resultado["Total_Entradas"] - df_resultado["Total_Saidas"]
        )
        st.session_state["df_xml_consolidado"] = df_resultado

    if "df_xml_consolidado" in st.session_state and st.session_state["df_xml_consolidado"] is not None:
        df_res = st.session_state["df_xml_consolidado"]
        st.subheader("3. Consolidado de Entradas e Saídas por SKU")
        st.table(df_res)
        data_hoje = datetime.now().strftime("%Y-%m-%d_%H-%M")
        csv_consolidado = df_res.to_csv(index=False, sep=";").encode("utf-8-sig")
        st.download_button(
            label="📥 Baixar Consolidado de Estoque (.CSV)",
            data=csv_consolidado,
            file_name=f"estoque_consolidado_xml_{data_hoje}.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )
