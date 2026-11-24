from datetime import datetime
import numpy as np
import pandas as pd
import streamlit as st


def _detectar_suspeitos_conversao(df_consolidado: pd.DataFrame, df_movs: pd.DataFrame) -> pd.DataFrame:
    """
    Identifica SKUs que provavelmente entraram em caixa/fardo/pacote e saíram em unidade,
    comparando a unidade de medida da entrada e a relação entre custo de entrada e preço de venda.
    """
    df = df_consolidado.copy()
    df["SKU"] = df["SKU"].astype(str).str.strip()

    if "Fator_Conversao" not in df.columns:
        df["Fator_Conversao"] = 1.0
    else:
        df["Fator_Conversao"] = pd.to_numeric(df["Fator_Conversao"], errors="coerce").fillna(1.0)
        df.loc[df["Fator_Conversao"] <= 0, "Fator_Conversao"] = 1.0

    # Tenta capturar a unidade de entrada mais comum nas movimentações (ex: CX, FD, UN)
    mapa_un_entrada = {}
    if df_movs is not None and not df_movs.empty and "Tipo" in df_movs.columns:
        entradas = df_movs[df_movs["Tipo"] == "Entrada"].copy()
        col_un = next((c for c in ["Unidade", "uCom", "UN"] if c in entradas.columns), None)
        if col_un and not entradas.empty:
            entradas["SKU"] = entradas["SKU"].astype(str).str.strip()
            mapa_un_entrada = (
                entradas.groupby("SKU")[col_un]
                .agg(lambda s: s.dropna().astype(str).str.upper().iloc[0] if not s.dropna().empty else "UN")
                .to_dict()
            )

    df["Unidade_Entrada_XML"] = df["SKU"].map(mapa_un_entrada).fillna("-")

    # Identifica colunas de custo e preço de venda se existirem
    col_custo = next((c for c in ["Custo_Unitario", "Valor_Unit_Entrada", "Custo_Medio"] if c in df.columns), None)
    col_preco = next((c for c in ["Preco_Venda", "Valor_Unit_Saida", "Preco_Medio"] if c in df.columns), None)

    custo_val = pd.to_numeric(df[col_custo], errors="coerce").fillna(0.0) if col_custo else pd.Series(0.0, index=df.index)
    preco_val = pd.to_numeric(df[col_preco], errors="coerce").fillna(0.0) if col_preco else pd.Series(0.0, index=df.index)

    # Regra de alerta: Unidade típica de embalagem fechada OU custo maior que 1.5x o preço de venda
    unidades_embalagem = {"CX", "CXA", "FD", "FARDO", "PCT", "PACOTE", "DP", "DISPLAY", "DZ", "SC", "SACO"}
    alerta_un = df["Unidade_Entrada_XML"].isin(unidades_embalagem)
    alerta_preco = (custo_val > 0) & (preco_val > 0) & (custo_val > (preco_val * 1.5))

    df["Alerta_Conversao"] = np.where(
        df["Fator_Conversao"] > 1.0,
        "✅ Ajustado",
        np.where(alerta_un | alerta_preco, "⚠️ Revisar (Possível Caixa/Fardo)", "🟢 OK"),
    )

    # Sugestão automática de fator quando custo > preço (ex: custo 60 / preço 5 = ~12)
    fator_sugerido = np.where(
        (alerta_preco) & (df["Fator_Conversao"] == 1.0),
        np.maximum(1.0, np.round(custo_val / np.where(preco_val > 0, preco_val, 1.0))),
        df["Fator_Conversao"],
    )
    df["Fator_Sugerido"] = fator_sugerido.astype(float)
    df["_Custo_Base"] = custo_val
    df["_Preco_Base"] = preco_val

    return df


def aplicar_fator_conversao(
    df_consolidado: pd.DataFrame,
    df_movs: pd.DataFrame,
    mapa_fatores: dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Aplica o Fator_Conversao nas entradas do df_movs e recalcula o consolidado
    (Entradas, Saldo Final e Custo Unitário) para que o Kardex rode em unidades reais.
    """
    df_c = df_consolidado.copy()
    df_c["SKU"] = df_c["SKU"].astype(str).str.strip()

    # Guarda backup original na primeira vez para permitir reajustes sem multiplicar 2x
    if "_Backup_Movs_Original" not in st.session_state and df_movs is not None:
        st.session_state["_Backup_Movs_Original"] = df_movs.copy()
    if "_Backup_Consolidado_Original" not in st.session_state:
        st.session_state["_Backup_Consolidado_Original"] = df_c.copy()

    base_c = st.session_state["_Backup_Consolidado_Original"].copy()
    base_m = st.session_state.get("_Backup_Movs_Original", df_movs).copy() if df_movs is not None else pd.DataFrame()

    base_c["SKU"] = base_c["SKU"].astype(str).str.strip()
    base_c["Fator_Conversao"] = base_c["SKU"].map(mapa_fatores).fillna(1.0)
    base_c.loc[base_c["Fator_Conversao"] <= 0, "Fator_Conversao"] = 1.0

    # 1. Ajusta as movimentações de ENTRADA (multiplica Qtd pelo fator e divide Valor Unitário)
    if not base_m.empty and "SKU" in base_m.columns and "Tipo" in base_m.columns:
        base_m["SKU"] = base_m["SKU"].astype(str).str.strip()
        fator_series = base_m["SKU"].map(mapa_fatores).fillna(1.0)
        mask_entrada = base_m["Tipo"] == "Entrada"

        if "Quantidade" in base_m.columns:
            base_m.loc[mask_entrada, "Quantidade"] = (
                pd.to_numeric(base_m.loc[mask_entrada, "Quantidade"], errors="coerce").fillna(0.0)
                * fator_series[mask_entrada]
            )
        for col_u in ["Valor_Unitario", "Custo_Unitario"]:
            if col_u in base_m.columns:
                base_m.loc[mask_entrada, col_u] = (
                    pd.to_numeric(base_m.loc[mask_entrada, col_u], errors="coerce").fillna(0.0)
                    / fator_series[mask_entrada]
                )

    # 2. Ajusta colunas agregadas no Consolidado
    for col_ent in ["Total_Entradas", "Entradas"]:
        if col_ent in base_c.columns:
            base_c[col_ent] = pd.to_numeric(base_c[col_ent], errors="coerce").fillna(0.0) * base_c["Fator_Conversao"]

    for col_custo in ["Custo_Unitario", "Valor_Unit_Entrada", "Custo_Medio"]:
        if col_custo in base_c.columns:
            base_c[col_custo] = pd.to_numeric(base_c[col_custo], errors="coerce").fillna(0.0) / base_c["Fator_Conversao"]

    # Recalcula Saldo_Calculado se houver colunas de Inicial, Entradas e Saídas
    col_ini = next((c for c in ["Qtd Inicial", "Saldo_Inicial"] if c in base_c.columns), None)
    col_ent = next((c for c in ["Total_Entradas", "Entradas"] if c in base_c.columns), None)
    col_sai = next((c for c in ["Total_Saidas", "Saidas"] if c in base_c.columns), None)
    if col_ini and col_ent and col_sai and "Saldo_Calculado" in base_c.columns:
        base_c["Saldo_Calculado"] = (
            pd.to_numeric(base_c[col_ini], errors="coerce").fillna(0.0)
            + pd.to_numeric(base_c[col_ent], errors="coerce").fillna(0.0)
            - pd.to_numeric(base_c[col_sai], errors="coerce").fillna(0.0)
        )

    return base_c, base_m


def renderizar_aba_conversao():
    st.header("🔄 De-Para e Fator de Conversão (Caixa → Unidade)")
    st.caption(
        "Ajuste produtos que entram na nota do fornecedor em Caixa/Fardo/Pacote (ex: 1 CX) "
        "e saem no caixa em Unidade (ex: 12 UN). O sistema multiplica as entradas e corrige o custo unitário automaticamente."
    )

    if (
        "df_xml_consolidado" not in st.session_state
        or st.session_state["df_xml_consolidado"] is None
        or st.session_state["df_xml_consolidado"].empty
    ):
        st.info("Nenhum dado consolidado encontrado. Processe os arquivos XML na Etapa 2 primeiro.")
        return

    df_consolidado = st.session_state["df_xml_consolidado"]
    df_movs = st.session_state.get("df_movs_detalhadas", pd.DataFrame())

    # Sempre analisa a partir do backup original para exibir custos/unidades reais da nota
    base_analise = st.session_state.get("_Backup_Consolidado_Original", df_consolidado).copy()
    if "Fator_Conversao" in df_consolidado.columns:
        mapa_atual = dict(zip(df_consolidado["SKU"].astype(str).str.strip(), df_consolidado["Fator_Conversao"]))
        base_analise["Fator_Conversao"] = base_analise["SKU"].astype(str).str.strip().map(mapa_atual).fillna(1.0)

    df_analise = _detectar_suspeitos_conversao(base_analise, df_movs)

    # Métricas de alerta
    qtd_suspeitos = int((df_analise["Alerta_Conversao"].str.startswith("⚠️")).sum())
    qtd_ajustados = int((df_analise["Fator_Conversao"] > 1.0).sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Total de SKUs", len(df_analise))
    m2.metric("Suspeitos de Caixa/Fardo", qtd_suspeitos, delta="Custo > Preço ou Unid. CX/FD", delta_color="inverse" if qtd_suspeitos > 0 else "off")
    m3.metric("SKUs com Fator Aplicado", qtd_ajustados)

    # Filtros rápidos + Importação/Exportação de Tabela De-Para
    with st.expander("📥 Importar ou Baixar Tabela De-Para (.CSV) para reutilizar em outros meses", expanded=False):
        c_up, c_down = st.columns(2)
        arquivo_depara = c_up.file_uploader("Subir tabela De-Para salva anteriormente (.CSV)", type=["csv"], key="up_depara")
        if arquivo_depara is not None:
            try:
                df_imp = pd.read_csv(arquivo_depara, sep=None, engine="python")
                if {"SKU", "Fator_Conversao"}.issubset(df_imp.columns):
                    mapa_imp = dict(
                        zip(
                            df_imp["SKU"].astype(str).str.strip(),
                            pd.to_numeric(df_imp["Fator_Conversao"], errors="coerce").fillna(1.0),
                        )
                    )
                    df_analise["Fator_Conversao"] = df_analise["SKU"].map(mapa_imp).fillna(df_analise["Fator_Conversao"])
                    st.success("Tabela De-Para carregada! Clique em 'Aplicar Conversões' abaixo para confirmar.")
                else:
                    st.error("O CSV precisa ter as colunas 'SKU' e 'Fator_Conversao'.")
            except Exception as e:
                st.error(f"Erro ao ler CSV: {e}")

        cols_export = [c for c in ["SKU", "Descrição", "Categoria", "Unidade_Entrada_XML", "Fator_Conversao"] if c in df_analise.columns]
        csv_depara = df_analise[cols_export].to_csv(index=False, sep=";").encode("utf-8-sig")
        c_down.download_button(
            label="📤 Baixar Tabela De-Para Atual (.CSV)",
            data=csv_depara,
            file_name=f"tabela_depara_conversao_{datetime.now().strftime('%Y-%m-%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    f1, f2 = st.columns(2)
    busca = f1.text_input("Buscar por SKU ou Descrição:", placeholder="Ex: Cerveja, Leite, 789...")
    filtro_tipo = f2.selectbox(
        "Filtrar visualização:",
        options=["Todos os SKUs", "⚠️ Apenas Suspeitos (Revisar)", "✅ Apenas Já Ajustados (Fator > 1)"],
    )

    df_view = df_analise.copy()
    if busca.strip():
        termo = busca.strip().lower()
        mask_sku = df_view["SKU"].str.lower().str.contains(termo, na=False)
        mask_desc = df_view["Descrição"].astype(str).str.lower().str.contains(termo, na=False) if "Descrição" in df_view.columns else False
        df_view = df_view[mask_sku | mask_desc]

    if filtro_tipo.startswith("⚠️"):
        df_view = df_view[df_view["Alerta_Conversao"].str.startswith("⚠️")]
    elif filtro_tipo.startswith("✅"):
        df_view = df_view[df_view["Fator_Conversao"] > 1.0]

    # Ordena colocando suspeitos no topo
    df_view = df_view.sort_values(by=["Alerta_Conversao", "SKU"], ascending=[True, True]).reset_index(drop=True)

    # Calcula prévia do custo unitário ajustado
    df_view["Custo_Unit_Ajustado"] = np.where(
        df_view["_Custo_Base"] > 0,
        (df_view["_Custo_Base"] / df_view["Fator_Conversao"]).round(2),
        0.0,
    )

    cols_editor = [
        "Alerta_Conversao",
        "SKU",
        "Descrição",
        "Unidade_Entrada_XML",
        "_Custo_Base",
        "_Preco_Base",
        "Fator_Sugerido",
        "Fator_Conversao",
        "Custo_Unit_Ajustado",
    ]
    cols_editor = [c for c in cols_editor if c in df_view.columns]

    df_editado = st.data_editor(
        df_view[cols_editor],
        column_config={
            "Alerta_Conversao": st.column_config.TextColumn("Diagnóstico", disabled=True),
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Descrição": st.column_config.TextColumn("Produto", disabled=True, width="large"),
            "Unidade_Entrada_XML": st.column_config.TextColumn("Un. XML", disabled=True, width="small"),
            "_Custo_Base": st.column_config.NumberColumn("Custo Nota (R$)", format="R$ %.2f", disabled=True),
            "_Preco_Base": st.column_config.NumberColumn("Preço Venda (R$)", format="R$ %.2f", disabled=True),
            "Fator_Sugerido": st.column_config.NumberColumn("Sugestão", format="%.0f", disabled=True, width="small"),
            "Fator_Conversao": st.column_config.NumberColumn(
                "Fator (Qtd por Embalagem)",
                min_value=1.0,
                max_value=10000.0,
                step=1.0,
                format="%.2f",
                help="Informe quantas unidades vêm dentro de 1 embalagem da nota de entrada (ex: 12 para caixa com 12).",
            ),
            "Custo_Unit_Ajustado": st.column_config.NumberColumn("Custo Unit. Real (R$)", format="R$ %.2f", disabled=True),
        },
        use_container_width=True,
        hide_index=True,
        key="editor_fator_conversao",
    )

    col_btn1, col_btn2 = st.columns(2)
    if col_btn1.button("✅ Aplicar Fatores de Conversão nos Indicadores", type="primary", use_container_width=True):
        # Junta os valores atuais de todos os SKUs com as edições feitas na tela filtrada
        mapa_final = dict(zip(df_analise["SKU"], df_analise["Fator_Conversao"]))
        for row in df_editado.itertuples(index=False):
            mapa_final[str(row.SKU).strip()] = float(row.Fator_Conversao)

        novo_consolidado, novas_movs = aplicar_fator_conversao(df_consolidado, df_movs, mapa_final)
        st.session_state["df_xml_consolidado"] = novo_consolidado
        if not novas_movs.empty:
            st.session_state["df_movs_detalhadas"] = novas_movs

        st.success("Fatores aplicados! Os saldos de entrada, custos unitários e o Kardex já foram recalculados.")
        st.rerun()

    if col_btn2.button("✨ Aceitar Todas as Sugestões Automáticas", use_container_width=True):
        mapa_sugestoes = dict(zip(df_analise["SKU"], df_analise["Fator_Sugerido"]))
        novo_consolidado, novas_movs = aplicar_fator_conversao(df_consolidado, df_movs, mapa_sugestoes)
        st.session_state["df_xml_consolidado"] = novo_consolidado
        if not novas_movs.empty:
            st.session_state["df_movs_detalhadas"] = novas_movs

        st.success("Sugestões aplicadas com sucesso! Revise a tabela para conferir.")
        st.rerun()
