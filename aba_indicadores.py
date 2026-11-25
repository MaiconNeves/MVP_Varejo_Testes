import datetime
import numpy as np
import pandas as pd
import streamlit as st


def _formatar_brl(valor: float) -> str:
    """Formata um número float para moeda brasileira (R$ 1.234,56)."""
    sinal = "-" if valor < 0 else ""
    abs_val = abs(valor)
    return f"{sinal}R$ {abs_val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _calcular_dias_sem_giro(df: pd.DataFrame, data_referencia: pd.Timestamp) -> pd.Series:
    """Calcula quantos dias se passaram desde a última saída registrada até a data de referência."""
    if "Ultima_Saida" not in df.columns:
        return pd.Series([np.nan] * len(df), index=df.index)

    datas_saida = pd.to_datetime(df["Ultima_Saida"], errors="coerce")
    dias = (data_referencia - datas_saida).dt.days
    return dias.clip(lower=0)


def _estimar_data_ruptura(row_r: pd.Series) -> str:
    """Estima a data em que o estoque atual vai zerar com base na DMD Real."""
    if row_r["Saldo_Calculado"] <= 0:
        return "Em Ruptura Hoje"
    if row_r["DMD"] <= 0:
        return "Sem Demanda"
    dias_rest = int(np.floor(row_r["Cobertura_Dias"]))
    if dias_rest >= 365:
        return "+1 ano"
    data_prev = pd.Timestamp.now().normalize() + pd.Timedelta(days=dias_rest)
    return data_prev.strftime("%d/%m/%Y")


def _reconstruir_kardex_diario(
    df_consolidado: pd.DataFrame,
    df_movs: pd.DataFrame | None,
    dias_periodo: int,
    col_inicial: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Reconstrói o extrato diário (Kardex) de cada SKU dentro da janela analisada
    aplicando a Regra Ouro de disponibilidade e ruptura, além de calcular
    os dias consecutivos recentes com estoque disponível e sem venda.
    """
    n_dias = max(int(dias_periodo), 1)
    datas_mov = pd.Series(dtype="datetime64[ns]")

    if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
        datas_mov = pd.to_datetime(df_movs["Data_Emissao"], errors="coerce").dropna()

    if not datas_mov.empty:
        data_fim = datas_mov.max().normalize()
        data_inicio_mov = datas_mov.min().normalize()
        data_inicio_janela = data_fim - pd.Timedelta(days=n_dias - 1)
        data_inicio = min(data_inicio_mov, data_inicio_janela)
    else:
        data_fim = pd.Timestamp.now().normalize()
        data_inicio = data_fim - pd.Timedelta(days=n_dias - 1)

    eixo_datas = pd.date_range(start=data_inicio, end=data_fim, freq="D")
    datas_str = [d.strftime("%Y-%m-%d") for d in eixo_datas]
    total_dias_calendario = len(datas_str)

    mov_agrupado = {}
    if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
        df_m = df_movs.copy()
        df_m["SKU"] = df_m["SKU"].astype(str).str.strip()
        df_m["Data_Norm"] = pd.to_datetime(df_m["Data_Emissao"], errors="coerce").dt.strftime("%Y-%m-%d")
        df_m = df_m.dropna(subset=["Data_Norm"])
        piv = (
            df_m.groupby(["SKU", "Data_Norm", "Tipo"], as_index=False)["Quantidade"]
            .sum()
        )
        for row in piv.itertuples(index=False):
            chave = (row.SKU, row.Data_Norm)
            if chave not in mov_agrupado:
                mov_agrupado[chave] = {"Entrada": 0.0, "Saída": 0.0}
            if row.Tipo == "Entrada":
                mov_agrupado[chave]["Entrada"] += float(row.Quantidade)
            elif row.Tipo == "Saída":
                mov_agrupado[chave]["Saída"] += float(row.Quantidade)

    resumos_sku = []
    registros_kardex = []

    for row in df_consolidado.itertuples(index=False):
        sku = str(getattr(row, "SKU")).strip()
        desc = str(getattr(row, "Descrição", ""))
        cat = str(getattr(row, "Categoria", "Geral"))
        marca = str(getattr(row, "Marca", "Sem Marca"))

        idx_ini = df_consolidado.columns.get_loc(col_inicial)
        saldo_corrente = float(row[idx_ini]) if pd.notna(row[idx_ini]) else 0.0

        dias_com_estoque = 0
        dias_ruptura_hist = 0
        episodios_ruptura = 0
        saldos_diarios = []
        historico_dias_sku = []
        em_ruptura_anterior = False

        for d_str in datas_str:
            abertura = round(saldo_corrente, 4)
            mov_dia = mov_agrupado.get((sku, d_str), {"Entrada": 0.0, "Saída": 0.0})
            entradas_dia = round(mov_dia["Entrada"], 4)
            saidas_dia = round(mov_dia["Saída"], 4)

            saldo_disponivel_dia = round(abertura + entradas_dia, 4)
            fechamento = round(saldo_disponivel_dia - saidas_dia, 4)

            teve_estoque_disponivel = (saldo_disponivel_dia > 0) or (saidas_dia > 0)

            if teve_estoque_disponivel:
                dias_com_estoque += 1
                if fechamento <= 0 and saidas_dia > 0:
                    situacao_dia = "🟡 Zerou na Venda (Disponível)"
                elif saidas_dia == 0:
                    situacao_dia = "🟢 Estoque Parado (Sem Venda)"
                else:
                    situacao_dia = "🟢 Com Estoque"
                em_ruptura_anterior = False
            else:
                dias_ruptura_hist += 1
                situacao_dia = "🔴 Zerado / Ruptura"
                if not em_ruptura_anterior:
                    episodios_ruptura += 1
                    em_ruptura_anterior = True

            saldos_diarios.append(max(fechamento, 0.0))
            historico_dias_sku.append((saldo_disponivel_dia, saidas_dia, fechamento))

            registros_kardex.append(
                {
                    "SKU": sku,
                    "Descrição": desc,
                    "Categoria": cat,
                    "Marca": marca,
                    "Data": d_str,
                    "Saldo_Abertura": round(abertura, 2),
                    "Entradas": round(entradas_dia, 2),
                    "Saldo_Disponivel": round(saldo_disponivel_dia, 2),
                    "Saidas": round(saidas_dia, 2),
                    "Saldo_Fechamento": round(fechamento, 2),
                    "Situacao_Dia": situacao_dia,
                }
            )
            saldo_corrente = fechamento

        dias_sem_mov_consecutivos = 0
        for saldo_disp_d, saidas_d, fechamento_d in reversed(historico_dias_sku):
            if saidas_d > 0:
                break
            if saldo_disp_d > 0 or fechamento_d > 0:
                dias_sem_mov_consecutivos += 1
            else:
                break

        estoque_medio_diario = round(float(np.mean(saldos_diarios)), 2) if saldos_diarios else 0.0
        disponibilidade_pct = round((dias_com_estoque / max(total_dias_calendario, 1)) * 100, 1)

        resumos_sku.append(
            {
                "SKU": sku,
                "Dias_Janela_Kardex": total_dias_calendario,
                "Dias_Com_Estoque": int(dias_com_estoque),
                "Dias_Ruptura_Hist": int(dias_ruptura_hist),
                "Episodios_Ruptura": int(episodios_ruptura),
                "Disponibilidade_Pct": disponibilidade_pct,
                "Estoque_Medio_Diario": estoque_medio_diario,
                "Dias_Sem_Mov_Estoque": int(dias_sem_mov_consecutivos),
            }
        )

    df_resumo_kardex = pd.DataFrame(resumos_sku)
    df_kardex_completo = pd.DataFrame(registros_kardex)
    return df_resumo_kardex, df_kardex_completo


def calcular_indicadores_estoque(
    df_consolidado: pd.DataFrame,
    df_movs: pd.DataFrame | None = None,
    dias_periodo: int = 30,
    cobertura_minima: int = 7,
    cobertura_alvo: int = 15,
    dias_excesso: int = 45,
    regras_categoria: dict | None = None,
    historico_acumulado: dict | None = None,
    usar_dmd_acumulada: bool = True,
    pct_deducoes_despesas: float = 15.0,
    margem_alvo_padrao: float = 20.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Calcula os indicadores de estoque com base na reconstrução histórica diária (Kardex)
    aplicando a Regra Ouro para disponibilidade e ruptura, além de Margem Bruta e Líquida
    parametrizáveis por Categoria.
    """
    df = df_consolidado.copy()
    df["SKU"] = df["SKU"].astype(str).str.strip()

    if "Categoria" not in df.columns:
        df["Categoria"] = "Geral"
    else:
        df["Categoria"] = df["Categoria"].fillna("Geral").astype(str).str.strip().replace("", "Geral")

    if "Marca" not in df.columns:
        df["Marca"] = "Sem Marca"
    else:
        df["Marca"] = df["Marca"].fillna("Sem Marca").astype(str).str.strip().replace("", "Sem Marca")

    col_inicial = next((c for c in ["Qtd Inicial", "Saldo_Inicial", "Estoque_Inicial"] if c in df.columns), None)
    if col_inicial is None:
        df["Qtd Inicial"] = 0.0
        col_inicial = "Qtd Inicial"
    else:
        df[col_inicial] = pd.to_numeric(df[col_inicial], errors="coerce").fillna(0.0)

    col_entradas = next((c for c in ["Total_Entradas", "Entradas"] if c in df.columns), None)
    df["Total_Entradas"] = pd.to_numeric(df[col_entradas], errors="coerce").fillna(0.0) if col_entradas else 0.0

    col_saidas = next((c for c in ["Total_Saidas", "Saidas"] if c in df.columns), None)
    df["Total_Saidas"] = pd.to_numeric(df[col_saidas], errors="coerce").fillna(0.0) if col_saidas else 0.0

    if "Saldo_Calculado" not in df.columns:
        df["Saldo_Calculado"] = df[col_inicial] + df["Total_Entradas"] - df["Total_Saidas"]
    else:
        df["Saldo_Calculado"] = pd.to_numeric(df["Saldo_Calculado"], errors="coerce").fillna(0.0)

    col_custo = next((c for c in ["Custo_Unitario", "Valor_Unit_Entrada", "Custo_Medio"] if c in df.columns), None)
    df["Custo_Unitario"] = pd.to_numeric(df[col_custo], errors="coerce").fillna(0.0) if col_custo else 0.0

    col_preco = next((c for c in ["Preco_Venda", "Valor_Unit_Saida", "Preco_Medio"] if c in df.columns), None)
    df["Preco_Venda"] = pd.to_numeric(df[col_preco], errors="coerce").fillna(0.0) if col_preco else 0.0

    # 1. Reconstrução Diária (Kardex Regra Ouro)
    df_resumo_kardex, df_kardex_completo = _reconstruir_kardex_diario(
	df_consolidado=df,
	df_movs=df_movs,
	dias_periodo=dias_periodo,
	col_inicial=col_inicial,
	)
    df = df.merge(df_resumo_kardex, on="SKU", how="left")
    # 2. DMD Real (apenas nos dias em que houve estoque disponível)
    df["DMD_Real_Hist"] = np.where(
        df["Dias_Com_Estoque"] > 0,
        df["Total_Saidas"] / df["Dias_Com_Estoque"],
        0.0,
        )
    saidas_acum_list = []
    dias_disp_acum_list = []
    for row in df.itertuples(index=False):
        sku_k = str(getattr(row, "SKU")).strip()
        hist_item = (historico_acumulado or {}).get(sku_k, {})
        saidas_ant = float(hist_item.get("saidas_acum", 0.0))
        dias_ant = int(hist_item.get("dias_disp_acum", 0))
        saidas_acum_list.append(saidas_ant + float(getattr(row, "Total_Saidas")))
        dias_disp_acum_list.append(dias_ant + int(getattr(row, "Dias_Com_Estoque")))

    df["Saidas_Acumuladas"] = saidas_acum_list
    df["Dias_Disp_Acumulados"] = dias_disp_acum_list

    if usar_dmd_acumulada and historico_acumulado:
        df["DMD"] = np.where(
            df["Dias_Disp_Acumulados"] > 0,
            df["Saidas_Acumuladas"] / df["Dias_Disp_Acumulados"],
            df["DMD_Real_Hist"],
        )
        else:
            df["DMD"] = df["DMD_Real_Hist"]
        # 3. Regras de Cobertura, Excesso e Margens por Categoria
            regras = regras_categoria or {}
            df["Cob_Min_Aplicada"] = df["Categoria"].map(
            lambda c: int(regras.get(c, {}).get("min", cobertura_minima))
            )
            df["Cob_Alvo_Aplicada"] = df["Categoria"].map(
            lambda c: int(regras.get(c, {}).get("alvo", cobertura_alvo))
            )
            df["Dias_Excesso_Aplicado"] = df["Categoria"].map(
            lambda c: int(regras.get(c, {}).get("excesso", dias_excesso))
            )
            df["Deductions_Pct_Aplicada"] = df["Categoria"].map(
            lambda c: float(regras.get(c, {}).get("deducoes_pct", pct_deducoes_despesas))
            )
            df["Margem_Alvo_Pct_Aplicada"] = df["Categoria"].map(
            lambda c: float(regras.get(c, {}).get("margem_alvo_pct", margem_alvo_padrao))
            )
            # 4. Cobertura em Dias e Previsão de Ruptura
            df["Cobertura_Dias"] = np.where(
            df["DMD"] > 0,
            (df["Saldo_Calculado"].clip(lower=0.0) / df["DMD"]).round(1),
            999.0,
            )
            df["Previsao_Ruptura"] = df.apply(_estimar_data_ruptura, axis=1)
            # 5. Indicadores Financeiros (usando deduções e margem alvo da categoria)
            df["Valor_Saidas"] = (df["Total_Saidas"] * df["Preco_Venda"]).round(2)
            df["CMV_Vendido"] = (df["Total_Saidas"] * df["Custo_Unitario"]).round(2)
            df["Lucro_Bruto"] = np.where(
            df["CMV_Vendido"] > 0,
            (df["Valor_Saidas"] - df["CMV_Vendido"]).round(2),
            0.0,
            )
    df["Margem_Bruta_Pct"] = np.where(
        (df["Valor_Saidas"] > 0) & (df["CMV_Vendido"] > 0),
        ((df["Lucro_Bruto"] / df["Valor_Saidas"]) * 100).round(2),
        np.nan,
    )
    fator_ded_series = df["Deductions_Pct_Aplicada"] / 100.0
    df["Lucro_Liquido_Est"] = np.where(
        df["CMV_Vendido"] > 0,
        (df["Lucro_Bruto"] - (df["Valor_Saidas"] * fator_ded_series)).round(2),
        0.0,
    )
    df["Margem_Liquida_Pct"] = np.where(
        (df["Valor_Saidas"] > 0) & (df["CMV_Vendido"] > 0),
        ((df["Lucro_Liquido_Est"] / df["Valor_Saidas"]) * 100).round(2),
        np.nan,
    )
    df["Status_Margem"] = np.where(
        df["Margem_Liquida_Pct"].isna(),
        "⚪ Sem Custo",
    np.where(
        df["Margem_Liquida_Pct"] >= df["Margem_Alvo_Pct_Aplicada"],
        "✅ Na Meta",
        "⚠️ Abaixo da Meta",
    ),
    )
    df["Valor_Estoque_Atual"] = (df["Saldo_Calculado"].clip(lower=0.0) * df["Custo_Unitario"]).round(2)
    df["Venda_Perdida_Un"] = (df["Dias_Ruptura_Hist"] * df["DMD_Real_Hist"]).round(1)
    df["Perda_Financeira_Hist"] = (df["Venda_Perdida_Un"] * df["Preco_Venda"]).round(2)
    df["Lucro_Bruto_Perdido_Hist"] = np.where(
    df["Custo_Unitario"] > 0,
    (df["Venda_Perdida_Un"] * (df["Preco_Venda"] - df["Custo_Unitario"]).clip(lower=0.0)).round(2),
    0.0,
    )
    # 6. Giro de Estoque e Dias sem Giro
    df["Giro_Estoque"] = np.where(
        df["Estoque_Medio_Diario"] > 0,
        (df["Total_Saidas"] / df["Estoque_Medio_Diario"]).round(2),
        0.0,
    )
    data_ref_giro = (
        pd.to_datetime(df_movs["Data_Emissao"], errors="coerce").max()
        if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns
        else pd.Timestamp.now()
    )
    if pd.isna(data_ref_giro):
        data_ref_giro = pd.Timestamp.now()
    df["Dias_Sem_Giro"] = _calcular_dias_sem_giro(df, data_ref_giro)
    # 7. Curva ABC
    criterio_abc = "Valor_Saidas" if df["Valor_Saidas"].sum() > 0 else "Total_Saidas"
    df = df.sort_values(by=criterio_abc, ascending=False).reset_index(drop=True)
    total_geral = df[criterio_abc].sum()
    if total_geral > 0:
        df["Participacao_Pct"] = ((df[criterio_abc] / total_geral) * 100).round(2)
        df["Acumulado_Pct"] = df["Participacao_Pct"].cumsum().round(2)
        df["Curva_ABC"] = np.select(
            [df["Acumulado_Pct"] <= 80.0, df["Acumulado_Pct"] <= 95.0],
            ["A", "B"],
            default="C",
            )
    else:
        df["Participacao_Pct"] = 0.0
        df["Acumulado_Pct"] = 0.0
        df["Curva_ABC"] = "C"
    # 8. Diagnóstico Operacional (usando Cobertura Mínima e Dias Excesso por Categoria)
    df["Status_Estoque"] = np.select(
        [
        df["Saldo_Calculado"] <= 0,
        df["Cobertura_Dias"] < df["Cob_Min_Aplicada"],
        (df["Cobertura_Dias"] > df["Dias_Excesso_Aplicado"]) & (df["Cobertura_Dias"] < 999.0),
        df["Cobertura_Dias"] == 999.0,
        ],
        [
        "🔴 Ruptura Hoje",
        "🟡 Atenção (Baixo)",
        "🔵 Excesso",
        "⚪ Sem Saída no Período",
        ],
        default="🟢 Saudável",
    )
    # 9. Sugestão de Compra (usando Cobertura Alvo por Categoria)
    necessidade_bruta = (df["DMD"] * df["Cob_Alvo_Aplicada"]) - df["Saldo_Calculado"]
    df["Sugestao_Compra"] = np.where(
        necessidade_bruta > 0,
        np.ceil(necessidade_bruta),
        0.0,
    )
    df["Investimento_Compra_RS"] = (df["Sugestao_Compra"] * df["Custo_Unitario"]).round(2)
    return df, df_kardex_completo

def _gerar_base_proximo_ciclo(
    df_ind: pd.DataFrame,
    data_registro: str,
    faturamento_total: float,
    fat_medio_diario: float,
    dias_periodo: int,
    valor_estoque_total: float,
    qtd_ruptura: int,
    perda_ruptura_total: float,
) -> pd.DataFrame:
    """Monta a planilha estruturada para continuar a análise no próximo ciclo."""
    colunas_preservar = [c for c in ["SKU", "Descrição", "Categoria", "Marca", "Fator_Conversao"] if c in df_ind.columns]
    df_next = df_ind[colunas_preservar].copy()
    df_next["Qtd Inicial"] = df_ind["Saldo_Calculado"].clip(lower=0.0)
    df_next["Saldo_Anterior_Ref"] = df_ind["Saldo_Calculado"]
    df_next["Status_Anterior_Ref"] = df_ind["Status_Estoque"]
    df_next["Saidas_Acum_Ref"] = df_ind["Saidas_Acumuladas"]
    df_next["Dias_Disp_Acum_Ref"] = df_ind["Dias_Disp_Acumulados"]
    df_next["Ref_Data"] = data_registro
    df_next["Ref_Dias_Periodo"] = int(dias_periodo)
    df_next["Ref_Faturamento"] = round(faturamento_total, 2)
    df_next["Ref_Fat_Medio_Diario"] = round(fat_medio_diario, 2)
    df_next["Ref_Capital"] = round(valor_estoque_total, 2)
    df_next["Ref_Ruptura_Qtd"] = int(qtd_ruptura)
    df_next["Ref_Perda_Ruptura"] = round(perda_ruptura_total, 2)
    return df_next
def _agrupar_por_dimensao(df_ind: pd.DataFrame, dimensao: str) -> pd.DataFrame:
            """Consolida KPIs financeiros, de disponibilidade, ruptura histórica e margens por Categoria ou Marca."""
            agrupado = (
                df_ind.groupby(dimensao, as_index=False)
                .agg(
            Qtd_SKUs=("SKU", "count"),
            SKUs_Ruptura_Hoje=("Saldo_Calculado", lambda s: int((s <= 0).sum())),
            SKUs_Com_Ruptura_Hist=("Dias_Ruptura_Hist", lambda s: int((s > 0).sum())),
            Disponibilidade_Media_Pct=("Disponibilidade_Pct", "mean"),
            Dias_Zerado_Total=("Dias_Ruptura_Hist", "sum"),
            Total_Saidas_Un=("Total_Saidas", "sum"),
            Faturamento_RS=("Valor_Saidas", "sum"),
            CMV_RS=("CMV_Vendido", "sum"),
            Lucro_Bruto_RS=("Lucro_Bruto", "sum"),
            Lucro_Liquido_Est_RS=("Lucro_Liquido_Est", "sum"),
            Capital_Estoque_RS=("Valor_Estoque_Atual", "sum"),
            Venda_Perdida_Hist_RS=("Perda_Financeira_Hist", "sum"),
            Lucro_Bruto_Perdido_RS=("Lucro_Bruto_Perdido_Hist", "sum"),
            Giro_Medio=("Giro_Estoque", "mean"),
            Sugestao_Compra_Un=("Sugestao_Compra", "sum"),
            )
            .sort_values(by="Faturamento_RS", ascending=False)
            .reset_index(drop=True)
            )
            fat_total_grupo = agrupado["Faturamento_RS"].sum()
            agrupado["Participacao_Fat_Pct"] = np.where(
            fat_total_grupo > 0,
            ((agrupado["Faturamento_RS"] / fat_total_grupo) * 100).round(2),
            0.0,
            )
            agrupado["Disponibilidade_Media_Pct"] = agrupado["Disponibilidade_Media_Pct"].round(1)
            agrupado["Giro_Medio"] = agrupado["Giro_Medio"].round(2)
            agrupado["Margem_Bruta_Pct"] = np.where(
            (agrupado["Faturamento_RS"] > 0) & (agrupado["CMV_RS"] > 0),
            ((agrupado["Lucro_Bruto_RS"] / agrupado["Faturamento_RS"]) * 100).round(2),
            np.nan,
            )
            agrupado["Margem_Liquida_Pct"] = np.where(
            (agrupado["Faturamento_RS"] > 0) & (agrupado["CMV_RS"] > 0),
            ((agrupado["Lucro_Liquido_Est_RS"] / agrupado["Faturamento_RS"]) * 100).round(2),
            np.nan,
            )
            return agrupado
def renderizar_aba_indicadores():
    st.header("📊 Painel de Compras, Ruptura e Inteligência de Estoque")
    if (
            "df_xml_consolidado" not in st.session_state
            or st.session_state["df_xml_consolidado"] is None
            or st.session_state["df_xml_consolidado"].empty
        ):
        st.info("Nenhum dado consolidado encontrado. Processe os arquivos XML na Etapa 2 primeiro.")
        return
    df_consolidado = st.session_state["df_xml_consolidado"]
    df_movs = st.session_state.get("df_movs_detalhadas", pd.DataFrame())
    snap = st.session_state.get("ponto_partida")
    tem_comparacao = snap is not None
    historico_acumulado = {}
    if tem_comparacao and "df_base_proximo_ciclo" in snap:
        df_ref = snap["df_base_proximo_ciclo"]
        if "SKU" in df_ref.columns and "Saidas_Acum_Ref" in df_ref.columns:
            for r in df_ref.itertuples(index=False):
                sku_k = str(getattr(r, "SKU")).strip()
                val_saidas = getattr(r, "Saidas_Acum_Ref", 0.0)
                val_dias = getattr(r, "Dias_Disp_Acum_Ref", 0.0)
                historico_acumulado[sku_k] = {
                    "saidas_acum": float(val_saidas) if pd.notna(val_saidas) else 0.0,
                    "dias_disp_acum": float(val_dias) if pd.notna(val_dias) else 0.0,
                }

    dias_padrao = 30
    if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
        datas_validas = pd.to_datetime(df_movs["Data_Emissao"], errors="coerce").dropna()
        if not datas_validas.empty:
            dias_calc = int((datas_validas.max() - datas_validas.min()).days) + 1
            if 1 <= dias_calc <= 365:
                dias_padrao = dias_calc
    # Identifica categorias únicas presentes nos dados
    if "Categoria" in df_consolidado.columns:
        categorias_unicas = sorted(
    df_consolidado["Categoria"].fillna("Geral").astype(str).str.strip().replace("", "Geral").unique().tolist()
    )
    else:
        categorias_unicas = ["Geral"]
    with st.expander("⚙️ Ajustar Dias de Cobertura, Excesso e Margens (Global e por Categoria)", expanded=False):
        st.markdown("1. Parâmetros Padrão (Aplicados a todas as categorias se não houver regra específica)")
    p1, p2, p3, p4, p5, p6 = st.columns(6)
    dias_periodo = p1.number_input(
    "Janela analisada (dias)",
    min_value=1,
    max_value=365,
    value=dias_padrao,
    help="Quantidade de dias corridos da janela analisada.",
    )
    cobertura_minima = p2.number_input(
    "Alerta estoque baixo (dias)",
    min_value=1,
    max_value=90,
    value=7,
    help="Abaixo dessa cobertura em dias, o produto entra em alerta de reposição.",
    )
    cobertura_alvo = p3.number_input(
    "Comprar para quantos dias?",
    min_value=1,
    max_value=180,
    value=15,
    help="Meta de estoque em dias para calcular a sugestão de compra.",
    )
    dias_excesso = p4.number_input(
    "Excesso acima de (dias)",
    min_value=15,
    max_value=365,
    value=45,
    help="Produtos com estoque para mais dias que isso são marcados como excesso.",
    )
    pct_deducoes_despesas = p5.number_input(
    "Impostos + Despesas (%)",
    min_value=0.0,
    max_value=80.0,
    value=15.0,
    step=0.5,
    help="Percentual médio sobre o faturamento para estimar o lucro líquido.",
    )
    margem_alvo_padrao = p6.number_input(
    "Margem Líq. Meta (%)",
    min_value=0.0,
    max_value=90.0,
    value=20.0,
    step=0.5,
    help="Meta de margem líquida para sinalizar produtos abaixo da rentabilidade esperada.",
    )
    usar_dmd_acumulada = st.checkbox(
    "Combinar histórico de ciclos anteriores no cálculo da venda diária (DMD)",
    value=True,
    disabled=not bool(historico_acumulado),
    )
    st.divider()
    st.markdown("2. Personalizar Regras por Categoria (Opcional)")
    st.caption(
    "Edite diretamente na tabela abaixo para categorias com prazos de reposição, giro ou margens diferentes."
    )
    df_regras_base = pd.DataFrame(
    {
    "Categoria": categorias_unicas,
    "Alerta Baixo (dias)": [int(cobertura_minima)] * len(categorias_unicas),
    "Comprar para (dias)": [int(cobertura_alvo)] * len(categorias_unicas),
    "Excesso acima de (dias)": [int(dias_excesso)] * len(categorias_unicas),
    "Impostos + Despesas (%)": [float(pct_deducoes_despesas)] * len(categorias_unicas),
    "Margem Líq. Meta (%)": [float(margem_alvo_padrao)] * len(categorias_unicas),
    }
    )
    df_regras_editado = st.data_editor(
    df_regras_base,
    column_config={
    "Categoria": st.column_config.TextColumn("Categoria", disabled=True),
    "Alerta Baixo (dias)": st.column_config.NumberColumn(
    "Alerta Baixo (dias)", min_value=1, max_value=180, step=1
    ),
    "Comprar para (dias)": st.column_config.NumberColumn(
    "Comprar para (dias)", min_value=1, max_value=365, step=1
    ),
    "Excesso acima de (dias)": st.column_config.NumberColumn(
    "Excesso acima de (dias)", min_value=5, max_value=730, step=1
    ),
    "Impostos + Despesas (%)": st.column_config.NumberColumn(
    "Impostos + Despesas (%)", min_value=0.0, max_value=80.0, step=0.5, format="%.1f%%"
    ),
    "Margem Líq. Meta (%)": st.column_config.NumberColumn(
    "Margem Líq. Meta (%)", min_value=0.0, max_value=90.0, step=0.5, format="%.1f%%"
    ),
    },
    hide_index=True,
    use_container_width=True,
    key="editor_regras_categoria_indicadores",
    )
    regras_categoria_dict = {}
    for row_reg in df_regras_editado.itertuples(index=False):
        cat_nome = str(row_reg[0]).strip()
        regras_categoria_dict[cat_nome] = {
            "min": int(row_reg[1]),
            "alvo": int(row_reg[2]),
            "excesso": int(row_reg[3]),
            "deducoes_pct": float(row_reg[4]),
            "margem_alvo_pct": float(row_reg[5]),
        }
    df_ind, df_kardex_completo = calcular_indicadores_estoque(
    df_consolidado=df_consolidado,
    df_movs=df_movs,
    dias_periodo=int(dias_periodo),
    cobertura_minima=int(cobertura_minima),
    cobertura_alvo=int(cobertura_alvo),
    dias_excesso=int(dias_excesso),
    regras_categoria=regras_categoria_dict,
    historico_acumulado=historico_acumulado,
    usar_dmd_acumulada=usar_dmd_acumulada,
    pct_deducoes_despesas=float(pct_deducoes_despesas),
    margem_alvo_padrao=float(margem_alvo_padrao),
    )
    faturamento_total = float(df_ind["Valor_Saidas"].sum())
    perda_ruptura_total = float(df_ind["Perda_Financeira_Hist"].sum())
    capital_parado_excesso = float(
    df_ind[df_ind["Status_Estoque"].isin(["🔵 Excesso", "⚪ Sem Saída no Período"])]["Valor_Estoque_Atual"].sum()
    )
    valor_estoque_total = float(df_ind["Valor_Estoque_Atual"].sum())
    investimento_compra_total = float(df_ind["Investimento_Compra_RS"].sum())
    qtd_comprar_urgente = int((df_ind["Sugestao_Compra"] > 0).sum())
    qtd_ruptura = int((df_ind["Saldo_Calculado"] <= 0).sum())
    fat_medio_diario = faturamento_total / max(int(dias_periodo), 1)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
    "💰 Faturamento no Período",
    _formatar_brl(faturamento_total),
    delta=f"Média: {_formatar_brl(fat_medio_diario)}/dia",
    delta_color="off",
    )
    k2.metric(
    "🚨 Venda Perdida por Falta",
    _formatar_brl(perda_ruptura_total),
    delta=f"{qtd_ruptura} produtos zerados hoje",
    delta_color="inverse" if perda_ruptura_total > 0 else "off",
    )
    k3.metric(
    "📦 Dinheiro Parado (Excesso)",
    _formatar_brl(capital_parado_excesso),
    delta=f"De {_formatar_brl(valor_estoque_total)} em estoque",
    delta_color="off",
    )
    k4.metric(
    "🛒 Compra Sugerida Hoje",
    _formatar_brl(investimento_compra_total),
    delta=f"{qtd_comprar_urgente} produtos para repor",
    delta_color="off",
    )
    st.divider()
    # =========================================================================
    # SEPARAÇÃO EM 2 CAMADAS
    # =========================================================================
    camada_comprador, camada_auditoria = st.tabs([
    "🛒 1. Lista de Ação (O que Comprar e Queimar Hoje)",
    "🔍 2. Auditoria Técnica, Kardex e Categorias",
    ])
    # CAMADA 1: MODO COMPRADOR / DONO
    with camada_comprador:
        col_modo, col_curva, col_busca = st.columns([2, 1.2, 1.8])
    visao_acao = col_modo.radio(
    "O que você quer ver agora?",
    options=[
    "🔴 Lista de Compras (Repor Urgente)",
    "🔵 Queima de Estoque (Parados / Excesso)",
    "📋 Todos os Produtos (Resumo Simples)",
    ],
    horizontal=False,
    )
    filtro_curva = col_curva.multiselect(
    "Importância (Curva ABC):",
    options=["A", "B", "C"],
    default=["A", "B", "C"],
    help="A = Produtos que trazem 80% do faturamento | B = Intermediários | C = Baixo giro",
    )
    busca_simples = col_busca.text_input(
    "Buscar produto:",
    placeholder="Digite nome ou código...",
    key="busca_camada_comprador",
    )
    df_acao = df_ind[df_ind["Curva_ABC"].isin(filtro_curva)].copy()
    if busca_simples.strip():
        termo = busca_simples.strip().lower()
    df_acao = df_acao[
    df_acao["SKU"].str.lower().str.contains(termo, na=False)
    | df_acao["Descrição"].astype(str).str.lower().str.contains(termo, na=False)
    ]
    if visao_acao.startswith("🔴"):
        df_acao = df_acao[df_acao["Sugestao_Compra"] > 0].sort_values(
        by=["Curva_ABC", "Perda_Financeira_Hist", "Sugestao_Compra"],
        ascending=[True, False, False],
        )
        st.subheader(f"🛒 Pedido de Compra Sugerido ({len(df_acao)} itens)")
        st.caption(
        "Quantidades calculadas conforme os dias de cobertura definidos para cada categoria."
        )
    elif visao_acao.startswith("🔵"):
        df_acao = df_acao[
            df_acao["Status_Estoque"].isin(["🔵 Excesso", "⚪ Sem Saída no Período"])
            & (df_acao["Saldo_Calculado"] > 0)
            ].sort_values(by="Valor_Estoque_Atual", ascending=False)
        st.subheader(f"🏷️ Itens para Promoção / Queima de Estoque ({len(df_acao)} itens)")
        st.caption(
        "Produtos que ultrapassaram o limite de dias de excesso da sua categoria ou sem venda no período."
    )
    else:
        st.subheader(f"📋 Visão Geral dos Produtos ({len(df_acao)} itens)")
    if df_acao.empty:
        st.success("Nenhum produto enquadrado neste filtro no momento!")
    else:
        df_exib_comprador = df_acao.copy()
    df_exib_comprador["DMD_Exib"] = df_exib_comprador["DMD"].round(2)
    df_exib_comprador["Saldo_Exib"] = df_exib_comprador["Saldo_Calculado"].round(1)
    df_exib_comprador["Compra_Exib"] = df_exib_comprador["Sugestao_Compra"].astype(int)
    cols_simples = [
    "Status_Estoque",
    "Curva_ABC",
    "Categoria",
    "SKU",
    "Descrição",
    "Saldo_Exib",
    "DMD_Exib",
    "Previsao_Ruptura",
    "Compra_Exib",
    "Investimento_Compra_RS",
    ]
    st.dataframe(
    df_exib_comprador[cols_simples].rename(
    columns={
    "Status_Estoque": "Situação",
    "Curva_ABC": "Curva",
    "Categoria": "Categoria",
    "SKU": "Código",
    "Descrição": "Produto",
    "Saldo_Exib": "Estoque Atual",
    "DMD_Exib": "Venda / Dia",
    "Previsao_Ruptura": "Quando Acaba?",
    "Compra_Exib": "Sugestão Compra",
    "Investimento_Compra_RS": "Custo Est. Compra (R$)",
    }
    ),
    column_config={
    "Custo Est. Compra (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
    },
    use_container_width=True,
    hide_index=True,
    )
    b1, b2 = st.columns(2)
    csv_pedido = df_exib_comprador[cols_simples].to_csv(index=False, sep=";").encode("utf-8-sig")
    b1.download_button(
    label="📥 Baixar Lista Atual para Excel / WhatsApp (.CSV)",
    data=csv_pedido,
    file_name=f"lista_acao_estoque_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv",
    mime="text/csv",
    type="primary",
    use_container_width=True,
    )
    df_prox_ciclo = _gerar_base_proximo_ciclo(
    df_ind=df_ind,
    data_registro=datetime.datetime.now().strftime("%Y-%m-%d"),
    faturamento_total=faturamento_total,
    fat_medio_diario=fat_medio_diario,
    dias_periodo=int(dias_periodo),
    valor_estoque_total=valor_estoque_total,
    qtd_ruptura=qtd_ruptura,
    perda_ruptura_total=perda_ruptura_total,
    )
    csv_prox = df_prox_ciclo.to_csv(index=False, sep=";").encode("utf-8-sig")
    b2.download_button(
    label="💾 Baixar Fechamento para Continuar no Próximo Mês (.CSV)",
    data=csv_prox,
    file_name=f"fechamento_ciclo{datetime.datetime.now().strftime('%Y-%m-%d')}.csv",
    mime="text/csv",
    use_container_width=True,
    )
    # CAMADA 2: AUDITORIA TÉCNICA, KARDEX DIA A DIA E CATEGORIAS/MARCAS
    with camada_auditoria:
        sub_kardex, sub_grupo, sub_completo, sub_evolucao = st.tabs([
    "🔎 Extrato Diário por Produto (Kardex)",
    "🏷️ Por Categoria e Marca",
    "📑 Tabela Técnica Completa (Todas Colunas)",
    "📈 Gráficos de Evolução Diária",
    ])
    with sub_kardex:
        st.subheader("Auditoria Dia a Dia por Produto (Regra Ouro)")
    st.caption(
    "Mostra exatamente como o saldo evoluiu dia após dia. Dias com prateleira vazia e sem venda "
    "são isolados para não distorcer a média de vendas."
    )
    if df_kardex_completo.empty:
        st.info("Sem movimentações suficientes para montar o extrato diário.")
    else:
        opcoes_sku_kardex = (
    df_ind["SKU"].astype(str)
    + " — "
    + df_ind["Descrição"].astype(str)
    + " ("
    + df_ind["Categoria"].astype(str)
    + ")"
    ).tolist()
    mapa_label_sku = dict(zip(opcoes_sku_kardex, df_ind["SKU"].astype(str).tolist()))
    sku_escolhido_label = st.selectbox(
    "Selecione o produto para auditar:",
    options=opcoes_sku_kardex,
    key="select_sku_kardex",
    )
    sku_selecionado = mapa_label_sku[sku_escolhido_label]
    row_sku = df_ind[df_ind["SKU"] == sku_selecionado].iloc[0]
    df_k_sku = df_kardex_completo[df_kardex_completo["SKU"] == sku_selecionado].copy()
    df_k_sku["Aparicoes_XML"] = 0
    if not df_movs.empty and "SKU" in df_movs.columns and "Data_Emissao" in df_movs.columns:
        df_mov_sku = df_movs[df_movs["SKU"].astype(str).str.strip() == str(sku_selecionado).strip()].copy()
    if not df_mov_sku.empty:
        df_mov_sku["_Data_Str"] = pd.to_datetime(df_mov_sku["Data_Emissao"], errors="coerce").dt.strftime("%Y-%m-%d")
    df_k_sku["_Data_Str"] = pd.to_datetime(df_k_sku["Data"], errors="coerce").dt.strftime("%Y-%m-%d")
    contagem_xml = df_mov_sku.groupby("_Data_Str").size().to_dict()
    df_k_sku["Aparicoes_XML"] = df_k_sku["_Data_Str"].map(contagem_xml).fillna(0).astype(int)
    df_k_sku.drop(columns=["_Data_Str"], inplace=True)
    ka1, ka2, ka3, ka4, ka5 = st.columns(5)
    ka1.metric("Dias com Estoque", f"{int(row_sku['Dias_Com_Estoque'])} / {int(row_sku['Dias_Janela_Kardex'])} d")
    ka2.metric("Dias Zerado (Passado)", f"{int(row_sku['Dias_Ruptura_Hist'])} d")
    ka3.metric("DMD Real (Dias Disp.)", f"{row_sku['DMD_Real_Hist']:.2f} un/d")
    ka4.metric("Venda Perdida Hist.", _formatar_brl(float(row_sku["Perda_Financeira_Hist"])))
    ka5.metric(
    "Margem Bruta / Líq.",
    f"{row_sku['Margem_Bruta_Pct']:.1f}% / {row_sku['Margem_Liquida_Pct']:.1f}%"
    if pd.notna(row_sku["Margem_Bruta_Pct"])
    else "Sem custo XML",
    )
    st.line_chart(df_k_sku.set_index("Data")["Saldo_Fechamento"], use_container_width=True)
    st.dataframe(
    df_k_sku[
    [
    "Data",
    "Aparicoes_XML",
    "Saldo_Abertura",
    "Entradas",
    "Saldo_Disponivel",
    "Saidas",
    "Saldo_Fechamento",
    "Situacao_Dia",
    ]
    ].rename(
    columns={
    "Aparicoes_XML": "Notas/Cupons no Dia",
    "Saldo_Abertura": "Saldo Abertura",
    "Entradas": "Entradas (+)",
    "Saldo_Disponivel": "Saldo Disponível",
    "Saidas": "Saídas (-)",
    "Saldo_Fechamento": "Saldo Fechamento",
    "Situacao_Dia": "Situação no Dia",
    }
    ),
    use_container_width=True,
    hide_index=True,
    )
    with sub_grupo:
        st.subheader("Rentabilidade e Ruptura por Categoria ou Marca")
    visao_grupo = st.radio(
    "Agrupar resultados por:",
    options=["Categoria", "Marca"],
    horizontal=True,
    key="radio_agrupamento_cat_marca",
    )
    df_grupo = _agrupar_por_dimensao(df_ind, visao_grupo)
    if not df_grupo.empty:
        top_fat = df_grupo.iloc[0]
    top_perda = df_grupo.sort_values(by="Venda_Perdida_Hist_RS", ascending=False).iloc[0]
    g1, g2, g3 = st.columns(3)
    g1.metric(
    f"{visao_grupo} Líder em Faturamento",
    str(top_fat[visao_grupo]),
    delta=f"{_formatar_brl(float(top_fat['Faturamento_RS']))} ({top_fat['Participacao_Fat_Pct']:.1f}%)",
    delta_color="off",
    )
    g2.metric(
    f"Maior Venda Perdida ({visao_grupo})",
    str(top_perda[visao_grupo]),
    delta=f"-{_formatar_brl(float(top_perda['Venda_Perdida_Hist_RS']))} ({int(top_perda['Dias_Zerado_Total'])} dias zerado)",
    delta_color="inverse" if float(top_perda["Venda_Perdida_Hist_RS"]) > 0 else "off",
    )
    g3.metric(
    f"Total de {visao_grupo}s",
    f"{len(df_grupo)}",
    delta=f"Compra sugerida: {int(df_grupo['Sugestao_Compra_Un'].sum())} un.",
    delta_color="off",
    )
    chart_df = df_grupo.set_index(visao_grupo)[["Faturamento_RS", "Lucro_Bruto_RS", "Venda_Perdida_Hist_RS"]].rename(
    columns={
    "Faturamento_RS": "Faturamento Realizado (R$)",
    "Lucro_Bruto_RS": "Lucro Bruto (R$)",
    "Venda_Perdida_Hist_RS": "Venda Perdida Passado (R$)",
    }
    )
    st.bar_chart(chart_df, use_container_width=True)
    df_grupo_exib = df_grupo.copy()
    df_grupo_exib["Margem_Bruta_Exib"] = df_grupo_exib["Margem_Bruta_Pct"].apply(
    lambda x: f"{x:.1f}%" if pd.notna(x) else "Sem custo"
    )
    df_grupo_exib["Margem_Liq_Exib"] = df_grupo_exib["Margem_Liquida_Pct"].apply(
    lambda x: f"{x:.1f}%" if pd.notna(x) else "Sem custo"
    )
    cols_grupo_exib = [
    visao_grupo,
    "Qtd_SKUs",
    "Disponibilidade_Media_Pct",
    "Dias_Zerado_Total",
    "SKUs_Ruptura_Hoje",
    "Faturamento_RS",
    "Participacao_Fat_Pct",
    "Lucro_Bruto_RS",
    "Margem_Bruta_Exib",
    "Lucro_Liquido_Est_RS",
    "Margem_Liq_Exib",
    "Venda_Perdida_Hist_RS",
    "Capital_Estoque_RS",
    "Giro_Medio",
    "Sugestao_Compra_Un",
    ]
    st.dataframe(
    df_grupo_exib[cols_grupo_exib].rename(
    columns={
    "Qtd_SKUs": "SKUs",
    "Disponibilidade_Media_Pct": "Disponib. Média (%)",
    "Dias_Zerado_Total": "Dias Zerado (Soma)",
    "SKUs_Ruptura_Hoje": "Ruptura Hoje",
    "Faturamento_RS": "Faturamento (R$)",
    "Participacao_Fat_Pct": "% Receita",
    "Lucro_Bruto_RS": "Lucro Bruto (R$)",
    "Margem_Bruta_Exib": "Margem Bruta",
    "Lucro_Liquido_Est_RS": "Lucro Líq. Est. (R$)",
    "Margem_Liq_Exib": "Margem Líq. Est.",
    "Venda_Perdida_Hist_RS": "Venda Perdida Hist. (R$)",
    "Capital_Estoque_RS": "Capital Estoque (R$)",
    "Giro_Medio": "Giro Médio",
    "Sugestao_Compra_Un": "Sugestão Compra (un.)",
    }
    ),
    use_container_width=True,
    hide_index=True,
    )
    with sub_completo:
        st.subheader("Base Completa de Indicadores Técnicos")
    st.dataframe(df_ind, use_container_width=True, hide_index=True)
    csv_full = df_ind.to_csv(index=False, sep=";").encode("utf-8-sig")
    st.download_button(
    label="📥 Baixar Base Técnica Completa (.CSV)",
    data=csv_full,
    file_name=f"indicadores_completos_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv",
    mime="text/csv",
    use_container_width=True,
    )
    with sub_evolucao:
        if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
            df_saidas_dia = df_movs[
    (df_movs["Tipo"] == "Saída") & (df_movs["Data_Emissao"] != "")
    ].copy()
    if not df_saidas_dia.empty:
        st.subheader("Evolução Diária de Vendas (Unidades)")
    resumo_diario = (
    df_saidas_dia.groupby("Data_Emissao", as_index=False)["Quantidade"]
    .sum()
    .sort_values("Data_Emissao")
    )
    st.bar_chart(resumo_diario.set_index("Data_Emissao")["Quantidade"])
    if not df_kardex_completo.empty:
        st.subheader("Evolução Diária de Produtos em Ruptura (Prateleira Vazia)")
    ruptura_por_dia = (
    df_kardex_completo.assign(
    Em_Ruptura=lambda d: np.where(d["Situacao_Dia"] == "🔴 Zerado / Ruptura", 1, 0)
    )
    .groupby("Data", as_index=False)["Em_Ruptura"]
    .sum()
    .sort_values("Data")
    )
    st.line_chart(ruptura_por_dia.set_index("Data")["Em_Ruptura"])
