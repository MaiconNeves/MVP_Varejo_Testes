from datetime import datetime
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

            # Regra Ouro:
            # 1) Dia com Estoque Disponível: (Saldo_Abertura + Entradas > 0) OU (Saidas > 0)
            # 2) Dia Zerado / Ruptura: (Saldo_Abertura + Entradas <= 0) E (Saidas == 0)
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

        # Cálculo de dias consecutivos recentes COM estoque e SEM venda
        dias_sem_mov_consecutivos = 0
        for saldo_disp_d, saidas_d, fechamento_d in reversed(historico_dias_sku):
            if saidas_d > 0:
                break
            if saldo_disp_d > 0 or fechamento_d > 0:
                dias_sem_mov_consecutivos += 1
            else:
                # Se não tinha estoque no dia, não conta como dia parado com estoque
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
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Calcula os indicadores de estoque com base na reconstrução histórica diária (Kardex)
    aplicando a Regra Ouro para disponibilidade e ruptura, além de Margem Bruta e Líquida.
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

    col_inicial = "Qtd Inicial" if "Qtd Inicial" in df.columns else "Qtd Física"
    if col_inicial not in df.columns:
        df[col_inicial] = 0.0

    # 1. Reconstrução do Kardex Diário (Regra Ouro)
    df_resumo_kardex, df_kardex_completo = _reconstruir_kardex_diario(
        df_consolidado=df,
        df_movs=df_movs,
        dias_periodo=dias_periodo,
        col_inicial=col_inicial,
    )
    df = df.merge(df_resumo_kardex, on="SKU", how="left")

    # Garante que itens sem saldo atual tenham 0 dias sem mov com estoque
    df["Dias_Sem_Mov_Estoque"] = np.where(
        df["Saldo_Calculado"] > 0,
        df["Dias_Sem_Mov_Estoque"].fillna(0).astype(int),
        0,
    )

    # 2. Regras personalizadas de Cobertura Mínima e Alvo por Categoria
    regras_cat = regras_categoria or {}
    df["Cob_Min_Aplicada"] = df["Categoria"].map(
        lambda c: int(regras_cat.get(c, {}).get("cob_min", cobertura_minima))
    )
    df["Cob_Alvo_Aplicada"] = df["Categoria"].map(
        lambda c: int(regras_cat.get(c, {}).get("cob_alvo", cobertura_alvo))
    )

    data_ref = pd.Timestamp.now().normalize()
    if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
        datas_validas = pd.to_datetime(df_movs["Data_Emissao"], errors="coerce").dropna()
        if not datas_validas.empty:
            data_ref = datas_validas.max()

    # 3. Demanda Média Diária (DMD Real descontando dias zerados sem saída vs. DMD Bruta)
    dias_disp_lote = df["Dias_Com_Estoque"].clip(lower=1)
    df["DMD_Real_Hist"] = (df["Total_Saidas"] / dias_disp_lote).round(2)
    df["DMD_Bruta_Lote"] = (df["Total_Saidas"] / max(dias_periodo, 1)).round(2)

    # Histórico acumulado entre ciclos (Ponto de Partida)
    hist_acum = historico_acumulado or {}
    saidas_prev = df["SKU"].map(lambda s: float(hist_acum.get(s, {}).get("saidas_acum", 0.0)))
    dias_disp_prev = df["SKU"].map(lambda s: float(hist_acum.get(s, {}).get("dias_disp_acum", 0.0)))
    df["Saidas_Acumuladas"] = (saidas_prev + df["Total_Saidas"]).round(2)
    df["Dias_Disp_Acumulados"] = (dias_disp_prev + df["Dias_Com_Estoque"]).round(0)
    df["DMD_Real_Acumulada"] = np.where(
    df["Dias_Disp_Acumulados"] > 0,
    (df["Saidas_Acumuladas"] / df["Dias_Disp_Acumulados"].clip(lower=1)).round(2),
    df["DMD_Real_Hist"],
    )
    if usar_dmd_acumulada and len(hist_acum) > 0:
     df["DMD"] = df["DMD_Real_Acumulada"]
    else:
     df["DMD"] = df["DMD_Real_Hist"]
    # 4. Estoque Médio Diário Real e Giro de Estoque
    df["Estoque_Medio"] = df["Estoque_Medio_Diario"]
    df["Giro_Estoque"] = np.where(
    df["Estoque_Medio"] > 0,
    (df["Total_Saidas"] / df["Estoque_Medio"]).round(2),
    0.0,
    )
    # 5. Cobertura Futura Real (em dias) e Data Estimada de Ruptura
    df["Cobertura_Dias"] = np.where(
        df["DMD"] > 0,
        (df["Saldo_Calculado"].clip(lower=0) / df["DMD"]).round(1),
        np.where(df["Saldo_Calculado"] > 0, 999.0, 0.0),
    )
    df["Dias_Sem_Giro"] = _calcular_dias_sem_giro(df, data_ref)
    
    def _estimar_data_ruptura(row_r):
        if row_r["Saldo_Calculado"] <= 0:
            return "Em Ruptura Hoje"
        if row_r["DMD"] <= 0:
            return "Sem Demanda"
        dias_rest = int(np.floor(row_r["Cobertura_Dias"]))
        return (data_ref + pd.Timedelta(days=dias_rest)).strftime("%d/%m/%Y")
        
    df["Previsao_Ruptura"] = df.apply(_estimar_data_ruptura, axis=1)
    
    # 6. Preços, Custos, Valor em Estoque e Margens (Bruta e Líquida)
    df["Preco_Medio_Venda"] = np.where(
        df["Total_Saidas"] > 0,
        (df["Valor_Saidas"] / df["Total_Saidas"]).round(2),
        0.0,
    )
    df["Custo_Medio_Compra"] = np.where(
        df["Total_Entradas"] > 0,
        (df["Valor_Entradas"] / df["Total_Entradas"]).round(2),
        0.0,
    )
    valor_unitario_ref = np.where(
        df["Custo_Medio_Compra"] > 0,
        df["Custo_Medio_Compra"],
        df["Preco_Medio_Venda"],
    )
    df["Valor_Estoque_Atual"] = (df["Saldo_Calculado"].clip(lower=0) * valor_unitario_ref).round(2)
    
    df["Tem_Custo_XML"] = df["Custo_Medio_Compra"] > 0
    df["CMV_Vendido"] = np.where(
        df["Tem_Custo_XML"],
        (df["Total_Saidas"] * df["Custo_Medio_Compra"]).round(2),
        0.0,
    )
    df["Lucro_Bruto"] = np.where(
        df["Tem_Custo_XML"] & (df["Valor_Saidas"] > 0),
        (df["Valor_Saidas"] - df["CMV_Vendido"]).round(2),
        0.0,
    )
    df["Margem_Bruta_Pct"] = np.where(
        df["Tem_Custo_XML"] & (df["Valor_Saidas"] > 0),
        ((df["Lucro_Bruto"] / df["Valor_Saidas"]) * 100).round(2),
        np.nan,
    )
    fator_deducoes = float(pct_deducoes_despesas) / 100.0
    df["Deducoes_Despesas_Est"] = (df["Valor_Saidas"] * fator_deducoes).round(2)
    df["Lucro_Liquido_Est"] = np.where(
        df["Tem_Custo_XML"] & (df["Valor_Saidas"] > 0),
        (df["Lucro_Bruto"] - df["Deducoes_Despesas_Est"]).round(2),
        0.0,
    )
    df["Margem_Liquida_Pct"] = np.where(
        df["Tem_Custo_XML"] & (df["Valor_Saidas"] > 0),
        ((df["Lucro_Liquido_Est"] / df["Valor_Saidas"]) * 100).round(2),
        np.nan,
    )
    # 7. Ruptura Histórica (Passado - Regra Ouro) e Impacto Financeiro Perdido
    df["Unidades_Perdidas_Hist"] = np.where(
        (df["Dias_Ruptura_Hist"] > 0) & (df["DMD_Real_Hist"] > 0),
        np.ceil(df["DMD_Real_Hist"] * df["Dias_Ruptura_Hist"]),
        0.0,
    )
    df["Perda_Financeira_Hist"] = (
        df["Unidades_Perdidas_Hist"] * df["Preco_Medio_Venda"]
    ).round(2)
    
    margem_unit_bruta = np.where(
        df["Tem_Custo_XML"],
        (df["Preco_Medio_Venda"] - df["Custo_Medio_Compra"]).clip(lower=0.0),
        0.0,
    )
    df["Lucro_Bruto_Perdido_Hist"] = (
        df["Unidades_Perdidas_Hist"] * margem_unit_bruta
    ).round(2)
    
    em_ruptura_hoje = df["Saldo_Calculado"] <= 0
    df["Perda_Diaria_Futura"] = np.where(
        em_ruptura_hoje & (df["DMD"] > 0),
        (df["DMD"] * df["Preco_Medio_Venda"]).round(2),
        0.0,
    )
    
    # 8. Curva ABC Potencial (Faturamento Realizado + Venda Perdida Histórica)
    df["Receita_Potencial"] = (df["Valor_Saidas"] + df["Perda_Financeira_Hist"]).round(2)
    usar_financeiro = df["Receita_Potencial"].sum() > 0
    criterio_abc = "Receita_Potencial" if usar_financeiro else "Total_Saidas"
    
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
    
    # 9. Diagnóstico Operacional (Presente + Passado)
    df["Status_Estoque"] = np.select(
        [
            df["Saldo_Calculado"] <= 0,
            df["Cobertura_Dias"] < df["Cob_Min_Aplicada"],
            (df["Cobertura_Dias"] > dias_excesso) & (df["Cobertura_Dias"] < 999.0),
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
    
    # 10. Sugestão de Compra baseada na DMD Real
    necessidade_bruta = (df["DMD"] * df["Cob_Alvo_Aplicada"]) - df["Saldo_Calculado"]
    df["Sugestao_Compra"] = np.where(
        necessidade_bruta > 0,
        np.ceil(necessidade_bruta),
        0.0,
    )
    
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
    
def _agrupar_por_dimensao(df_ind: pd.DataFrame, dimensao: str, pct_deducoes_despesas: float) -> pd.DataFrame:
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
    fator_ded = float(pct_deducoes_despesas) / 100.0
    agrupado["Lucro_Liquido_Est_RS"] = np.where(
        agrupado["CMV_RS"] > 0,
        (agrupado["Lucro_Bruto_RS"] - (agrupado["Faturamento_RS"] * fator_ded)).round(2),
        0.0,
    )
    agrupado["Margem_Liquida_Pct"] = np.where(
        (agrupado["Faturamento_RS"] > 0) & (agrupado["CMV_RS"] > 0),
        ((agrupado["Lucro_Liquido_Est_RS"] / agrupado["Faturamento_RS"]) * 100).round(2),
        np.nan,
    )
    return agrupado
    
def renderizar_aba_indicadores():
    st.header("📊 Indicadores Históricos, Kardex Diário e Rentabilidade")
    st.caption(
        "Reconstrução dia a dia (Regra Ouro) para medir disponibilidade real, rupturas passadas, margem bruta/líquida e reposição."
    )
    if (
        "df_xml_consolidado" not in st.session_state
        or st.session_state["df_xml_consolidado"] is None
        or st.session_state["df_xml_consolidado"].empty
    ):
        st.info(
            "Nenhum dado consolidado encontrado. Processe os arquivos XML na Etapa 2 primeiro."
        )
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
                historico_acumulado[sku_k] = {
                    "saidas_acum": float(getattr(r, "Saidas_Acum_Ref", 0.0) or 0.0),
                    "dias_disp_acum": float(getattr(r, "Dias_Disp_Acum_Ref", 0.0) or 0.0),
                }

    # 1. Parâmetros de Simulação
    with st.container(border=True):
        st.subheader("Parâmetros da Simulação")
        p1, p2, p3, p4, p5 = st.columns(5)
        dias_periodo = p1.number_input(
            "Janela analisada (dias)",
            min_value=1,
            max_value=365,
            value=30,
            help="Quantidade de dias corridos da janela analisada.",
        )
        cobertura_minima = p2.number_input(
            "Cob. mínima padrão (dias)",
            min_value=1,
            max_value=90,
            value=7,
            help="Valor padrão de alerta caso a categoria não tenha regra própria.",
        )
        cobertura_alvo = p3.number_input(
            "Cob. alvo padrão (dias)",
            min_value=1,
            max_value=180,
            value=15,
            help="Valor padrão de compra caso a categoria não tenha regra própria.",
        )
        dias_excesso = p4.number_input(
            "Limite excesso (dias)",
            min_value=15,
            max_value=365,
            value=45,
            help="Acima deste limite o estoque é considerado excessivo.",
        )
        pct_deducoes_despesas = p5.number_input(
            "Impostos + Despesas (%)",
            min_value=0.0,
            max_value=95.0,
            value=15.0,
            step=0.5,
            help="Percentual estimado de impostos sobre venda, taxas de cartão e despesas operacionais para cálculo da Margem Líquida.",
        )

        usar_dmd_acumulada = True
        if len(historico_acumulado) > 0:
            usar_dmd_acumulada = st.checkbox(
                "Usar DMD Real Acumulada (histórico somado dos ciclos) para Cobertura Futura e Sugestão de Compra",
                value=True,
                help="Recomendado ao importar lotes curtos/irregulares (ex: 8d -> 4d -> 5d).",
            )
        # Configuração de Cobertura Mínima e Cobertura Alvo por Categoria
        categorias_disponiveis = sorted(
            df_consolidado["Categoria"].fillna("Geral").astype(str).str.strip().replace("", "Geral").unique().tolist()
            if "Categoria" in df_consolidado.columns
            else ["Geral"]
        )
        regras_categoria = {}
        with st.expander("⚙️ Configurar Cobertura Mínima e Cobertura Alvo por Categoria", expanded=False):
            st.caption(
                "Personalize os dias de Cobertura Mínima (alerta) e Cobertura Alvo (compra) por categoria. "
                "Categorias não alteradas seguem os valores padrão acima."
            )
            df_cfg_cat = pd.DataFrame(
                {
                    "Categoria": categorias_disponiveis,
                    "Cobertura Mínima / Alerta (dias)": [int(cobertura_minima)] * len(categorias_disponiveis),
                    "Cobertura Alvo Compra (dias)": [int(cobertura_alvo)] * len(categorias_disponiveis),
                }
            )
            df_cfg_editado = st.data_editor(
                df_cfg_cat,
                disabled=["Categoria"],
                column_config={
                    "Categoria": st.column_config.TextColumn("Categoria"),
                    "Cobertura Mínima / Alerta (dias)": st.column_config.NumberColumn(
                        "Cob. Mínima / Alerta (dias)",
                        min_value=1,
                        max_value=180,
                        step=1,
                    ),
                    "Cobertura Alvo Compra (dias)": st.column_config.NumberColumn(
                        "Cob. Alvo Compra (dias)",
                        min_value=1,
                        max_value=365,
                        step=1,
                    ),
                },
                use_container_width=True,
                hide_index=True,
                key="editor_cobertura_categoria",
            )
            for r_cat in df_cfg_editado.itertuples(index=False):
                regras_categoria[str(r_cat[0])] = {
                    "cob_min": int(r_cat[1]),
                    "cob_alvo": int(r_cat[2]),
                }

    df_ind, df_kardex_completo = calcular_indicadores_estoque(
        df_consolidado=df_consolidado,
        df_movs=df_movs,
        dias_periodo=int(dias_periodo),
        cobertura_minima=int(cobertura_minima),
        cobertura_alvo=int(cobertura_alvo),
        dias_excesso=int(dias_excesso),
        regras_categoria=regras_categoria,
        historico_acumulado=historico_acumulado,
        usar_dmd_acumulada=usar_dmd_acumulada,
        pct_deducoes_despesas=float(pct_deducoes_despesas),
    )

    faturamento_total = float(df_ind["Valor_Saidas"].sum())
    fat_medio_diario = faturamento_total / max(int(dias_periodo), 1)
    valor_estoque_total = float(df_ind["Valor_Estoque_Atual"].sum())
    qtd_ruptura_hoje = int((df_ind["Saldo_Calculado"] <= 0).sum())
    qtd_ruptura_hist = int((df_ind["Dias_Ruptura_Hist"] > 0).sum())
    perda_ruptura_hist_total = float(df_ind["Perda_Financeira_Hist"].sum())
    lucro_bruto_perdido_total = float(df_ind["Lucro_Bruto_Perdido_Hist"].sum())
    disponibilidade_media = float(df_ind["Disponibilidade_Pct"].mean()) if not df_ind.empty else 100.0
    total_sugerido = int(df_ind["Sugestao_Compra"].sum())

    # Cálculo consolidado de Margem Bruta e Líquida
    df_com_custo = df_ind[df_ind["Tem_Custo_XML"] & (df_ind["Valor_Saidas"] > 0)]
    fat_base_margem = float(df_com_custo["Valor_Saidas"].sum())
    lucro_bruto_total = float(df_com_custo["Lucro_Bruto"].sum())
    lucro_liquido_total = float(df_com_custo["Lucro_Liquido_Est"].sum())
    margem_bruta_geral = (lucro_bruto_total / fat_base_margem * 100) if fat_base_margem > 0 else np.nan
    margem_liquida_geral = (lucro_liquido_total / fat_base_margem * 100) if fat_base_margem > 0 else np.nan

    # 2. Cards de Resumo Executivo
    st.subheader("Resumo Executivo (Histórico + Projeção)")
    if tem_comparacao:
        dias_p1 = int(snap.get("dias_periodo", 0) or 0)
        info_p1 = f" ({dias_p1}d)" if dias_p1 > 0 else ""
        st.caption(
            f"📈 Comparando lote atual ({int(dias_periodo)}d) contra o Ponto de Partida salvo em {snap['data_registro']}{info_p1}."
        )

    m1, m2, m3, m4 = st.columns(4)
    m5, m6, m7, m8 = st.columns(4)
    delta_fat_dia = None
    delta_cap = None
    delta_rup_qtd = None
    delta_rup_val = None

    if tem_comparacao:
        fat_dia_p1 = float(snap.get("fat_medio_diario", 0.0) or 0.0)
        if fat_dia_p1 == 0.0 and int(snap.get("dias_periodo", 0) or 0) > 0:
            fat_dia_p1 = float(snap.get("faturamento_total", 0.0)) / int(snap["dias_periodo"])
        if fat_dia_p1 > 0:
            diff_fat_dia = fat_medio_diario - fat_dia_p1
            delta_fat_dia = f"{_formatar_brl(diff_fat_dia)}/dia vs. P1"
        diff_cap = valor_estoque_total - float(snap.get("valor_estoque_total", 0.0))
        diff_rup_qtd = qtd_ruptura_hoje - int(snap.get("qtd_ruptura", 0))
        diff_rup_val = perda_ruptura_hist_total - float(snap.get("perda_ruptura_total", 0.0))
        delta_cap = f"{_formatar_brl(diff_cap)} vs. P1"
        delta_rup_qtd = f"{diff_rup_qtd:+d} SKU(s) vs. P1"
        delta_rup_val = f"{_formatar_brl(diff_rup_val)} vs. P1"

    m1.metric(
        "Faturamento do Lote",
        _formatar_brl(faturamento_total),
        delta=f"Média: {_formatar_brl(fat_medio_diario)}/dia",
        delta_color="off",
    )
    m2.metric(
        "Faturamento Médio Diário",
        f"{_formatar_brl(fat_medio_diario)}/dia",
        delta=delta_fat_dia,
        delta_color="normal",
    )
    m3.metric(
        "Margem Bruta (XML)",
        f"{margem_bruta_geral:.1f}%" if pd.notna(margem_bruta_geral) else "Sem custo XML",
        delta=f"Lucro Bruto: {_formatar_brl(lucro_bruto_total)}" if pd.notna(margem_bruta_geral) else None,
        delta_color="normal",
    )
    m4.metric(
        "Margem Líquida Est.",
        f"{margem_liquida_geral:.1f}%" if pd.notna(margem_liquida_geral) else "Sem custo XML",
        delta=f"Lucro Líq.: {_formatar_brl(lucro_liquido_total)}" if pd.notna(margem_liquida_geral) else None,
        delta_color="normal",
    )
    m5.metric(
        "Disponibilidade na Gôndola",
        f"{disponibilidade_media:.1f}%",
        delta=f"{qtd_ruptura_hist} SKU(s) faltaram no período",
        delta_color="inverse" if qtd_ruptura_hist > 0 else "off",
    )
    m6.metric(
        "Venda Perdida (Passado)",
        _formatar_brl(perda_ruptura_hist_total),
        delta=delta_rup_val if tem_comparacao else (f"Margem Perdida: {_formatar_brl(lucro_bruto_perdido_total)}" if lucro_bruto_perdido_total > 0 else None),
        delta_color="inverse",
        help="Estimativa de vendas deixadas na mesa nos dias em que a prateleira ficou vazia sem nenhuma saída (Regra Ouro).",
    )
    m7.metric(
        "Ruptura Hoje (Zerados)",
        f"{qtd_ruptura_hoje} SKU(s)",
        delta=delta_rup_qtd,
        delta_color="inverse",
    )
    m8.metric(
        "Capital Estoque / Compra",
        _formatar_brl(valor_estoque_total),
        delta=delta_cap if tem_comparacao else f"Sugestão compra: {total_sugerido} un.",
        delta_color="off",
    )
    # 3. Controle de Ciclos: Salvar Ponto de Partida
    with st.expander("📌 Ponto de Partida (Salvar Snapshot para Comparar Próximos Ciclos)", expanded=False):
        st.write(
            "Salve o estado atual como Ponto de Partida (Ponto 1). Na próxima simulação, "
            "o saldo final de hoje será usado como estoque inicial e a DMD Real será acumulada."
        )
        c_snap1, c_snap2 = st.columns(2)
        data_agora = datetime.now().strftime("%d/%m/%Y %H:%M")
        df_next_cycle = _gerar_base_proximo_ciclo(
            df_ind=df_ind,
            data_registro=data_agora,
            faturamento_total=faturamento_total,
            fat_medio_diario=fat_medio_diario,
            dias_periodo=int(dias_periodo),
            valor_estoque_total=valor_estoque_total,
            qtd_ruptura=qtd_ruptura_hoje,
            perda_ruptura_total=perda_ruptura_hist_total,
        )
        if c_snap1.button("💾 Salvar Estado Atual como Ponto de Partida", type="secondary", use_container_width=True):
            st.session_state["ponto_partida"] = {
                "data_registro": data_agora,
                "dias_periodo": int(dias_periodo),
                "faturamento_total": faturamento_total,
                "fat_medio_diario": fat_medio_diario,
                "valor_estoque_total": valor_estoque_total,
                "qtd_ruptura": qtd_ruptura_hoje,
                "perda_ruptura_total": perda_ruptura_hist_total,
                "df_base_proximo_ciclo": df_next_cycle,
                "df_indicadores_ref": df_ind.copy(),
            }
            st.success(
                f"✅ Ponto de Partida salvo ({data_agora})! Volte na Etapa 2 e selecione "
                "'Continuar do Ponto de Partida salvo' para processar os próximos XMLs."
            )
            st.rerun()

        csv_proximo_ciclo = df_next_cycle.to_csv(index=False, sep=";").encode("utf-8-sig")
        data_arq = datetime.now().strftime("%Y-%m-%d_%H-%M")
        c_snap2.download_button(
            label="📤 Baixar Base p/ Próximo Ciclo (.CSV)",
            data=csv_proximo_ciclo,
            file_name=f"base_proximo_ciclo_{data_arq}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    # 4. Painel de Auditoria de Ruptura (Passada + Risco Futuro)
    df_criticos = df_ind[
        (df_ind["Dias_Ruptura_Hist"] > 0)
        | (df_ind["Status_Estoque"].isin(["🔴 Ruptura Hoje", "🟡 Atenção (Baixo)"]))
    ].sort_values(by=["Perda_Financeira_Hist", "Valor_Saidas"], ascending=False)

    if not df_criticos.empty:
        with st.container(border=True):
            st.subheader(f"🚨 Auditoria de Rupturas Passadas e Risco Futuro ({len(df_criticos)} item(ns))")
            st.caption(
                "Inclui itens que sofreram falta no meio do período (mesmo já reabastecidos) e itens com risco iminente de ruptura."
            )
            cols_ruptura = [
                c
                for c in [
                    "SKU",
                    "Descrição",
                    "Categoria",
                    "Marca",
                    "Curva_ABC",
                    "Status_Estoque",
                    "Disponibilidade_Pct",
                    "Dias_Ruptura_Hist",
                    "Episodios_Ruptura",
                    "DMD_Real_Hist",
                    "Perda_Financeira_Hist",
                    "Lucro_Bruto_Perdido_Hist",
                    "Saldo_Calculado",
                    "Previsao_Ruptura",
                    "Sugestao_Compra",
                ]
                if c in df_criticos.columns
            ]
            st.dataframe(
                df_criticos[cols_ruptura].rename(
                    columns={
                        "Curva_ABC": "Curva",
                        "Status_Estoque": "Situação Hoje",
                        "Disponibilidade_Pct": "Disponib. (%)",
                        "Dias_Ruptura_Hist": "Dias Zerado (Passado)",
                        "Episodios_Ruptura": "Vezes Zerado",
                        "DMD_Real_Hist": "DMD Real",
                        "Perda_Financeira_Hist": "Venda Perdida Passado (R$)",
                        "Lucro_Bruto_Perdido_Hist": "Lucro Bruto Perdido (R$)",
                        "Saldo_Calculado": "Saldo Hoje",
                        "Previsao_Ruptura": "Previsão Ruptura",
                        "Sugestao_Compra": "Comprar Agora (un.)",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
            csv_compra_urgente = df_criticos[cols_ruptura].to_csv(index=False, sep=";").encode("utf-8-sig")
            st.download_button(
                label="🛒 Baixar Auditoria de Ruptura e Compra Urgente (.CSV)",
                data=csv_compra_urgente,
                file_name=f"auditoria_ruptura_compra_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # 5. Sub-abas de Análise Detalhada
    tab_sku, tab_kardex, tab_agrupado, tab_evolucao = st.tabs(
        [
            "📦 Detalhamento por SKU",
            "🔍 Inspetor de Extrato Diário (Kardex)",
            "🏷️ Consolidado por Categoria e Marca",
            "📈 Evolução Diária Geral",
        ]
    )

    # --- SUB-ABA 1: DETALHAMENTO POR SKU ---
    with tab_sku:
        f1, f2, f3, f4 = st.columns(4)
        filtro_curva = f1.multiselect(
            "Curva ABC (Potencial):",
            options=["A", "B", "C"],
            default=["A", "B", "C"],
        )
        filtro_status = f2.multiselect(
            "Status Hoje:",
            options=sorted(df_ind["Status_Estoque"].unique().tolist()),
            default=sorted(df_ind["Status_Estoque"].unique().tolist()),
        )
        opcoes_cat = sorted(df_ind["Categoria"].unique().tolist())
        filtro_categoria = f3.multiselect(
            "Categoria:",
            options=opcoes_cat,
            default=opcoes_cat,
        )
        opcoes_marca = sorted(df_ind["Marca"].unique().tolist())
        filtro_marca = f4.multiselect(
            "Marca:",
            options=opcoes_marca,
            default=opcoes_marca,
        )

        # Novo filtro: Qtde Dias Sem Mov (com estoque disponível e sem vendas)
        opcoes_dias_sem_mov = ["Todos", 1, 5, 7, 10]
        if hasattr(st, "segmented_control"):
            filtro_dias_sem_mov = st.segmented_control(
                "Qtde Dias Sem Mov (Estoque > 0 sem vendas):",
                options=opcoes_dias_sem_mov,
                default="Todos",
                format_func=lambda x: "Todos os itens" if x == "Todos" else f"≥ {x} dia(s)",
                key="filtro_dias_sem_mov_sku",
            )
        else:
            filtro_dias_sem_mov = st.selectbox(
                "Qtde Dias Sem Mov (Estoque > 0 sem vendas):",
                options=opcoes_dias_sem_mov,
                index=0,
                format_func=lambda x: "Todos os itens" if x == "Todos" else f"≥ {x} dia(s)",
                key="filtro_dias_sem_mov_sku",
            )

        df_filtrado = df_ind[
            df_ind["Curva_ABC"].isin(filtro_curva)
            & df_ind["Status_Estoque"].isin(filtro_status)
            & df_ind["Categoria"].isin(filtro_categoria)
            & df_ind["Marca"].isin(filtro_marca)
        ].copy()

        # Aplica o filtro de dias sem movimento quando uma opção numérica estiver selecionada
        if filtro_dias_sem_mov is not None and filtro_dias_sem_mov != "Todos":
            min_dias_parado = int(filtro_dias_sem_mov)
            df_filtrado = df_filtrado[
                (df_filtrado["Saldo_Calculado"] > 0)
                & (df_filtrado["Dias_Sem_Mov_Estoque"] >= min_dias_parado)
            ].sort_values(by=["Dias_Sem_Mov_Estoque", "Valor_Estoque_Atual"], ascending=[False, False])
            capital_parado_filtro = float(df_filtrado["Valor_Estoque_Atual"].sum())
            st.info(
                f"🛑 Estoque Parado (≥ {min_dias_parado} dia(s) sem venda): "
                f"{len(df_filtrado)} SKU(s) com saldo positivo somando {_formatar_brl(capital_parado_filtro)} em estoque."
            )

        if tem_comparacao and "df_base_proximo_ciclo" in snap:
            df_ref = snap["df_base_proximo_ciclo"]
            if "SKU" in df_ref.columns and "Saldo_Anterior_Ref" in df_ref.columns:
                mapa_saldo_ant = dict(
                    zip(
                        df_ref["SKU"].astype(str),
                        pd.to_numeric(df_ref["Saldo_Anterior_Ref"], errors="coerce").fillna(0.0),
                    )
                )
                df_filtrado["Var_Saldo"] = df_filtrado["SKU"].astype(str).map(mapa_saldo_ant)
                df_filtrado["Var_Saldo"] = (df_filtrado["Saldo_Calculado"] - df_filtrado["Var_Saldo"]).round(2)

        df_exibicao = df_filtrado.copy()
        df_exibicao["Cobertura_Exibicao"] = np.where(
            df_exibicao["Cobertura_Dias"] >= 999.0,
            "Sem demanda",
            df_exibicao["Cobertura_Dias"].astype(str) + " d",
        )
        df_exibicao["Margem_Bruta_Exib"] = df_exibicao["Margem_Bruta_Pct"].apply(
            lambda x: f"{x:.1f}%" if pd.notna(x) else "Sem custo"
        )
        df_exibicao["Margem_Liq_Exib"] = df_exibicao["Margem_Liquida_Pct"].apply(
            lambda x: f"{x:.1f}%" if pd.notna(x) else "Sem custo"
        )
        colunas_ordenadas = [
            "SKU",
            "Descrição",
            "Categoria",
            "Marca",
            "Curva_ABC",
            "Status_Estoque",
            "Dias_Sem_Mov_Estoque",
            "Disponibilidade_Pct",
            "Dias_Ruptura_Hist",
            "Perda_Financeira_Hist",
            "Saldo_Calculado",
            "Valor_Estoque_Atual",
            "Var_Saldo",
            "Total_Saidas",
            "Valor_Saidas",
            "Lucro_Bruto",
            "Margem_Bruta_Exib",
            "Lucro_Liquido_Est",
            "Margem_Liq_Exib",
            "DMD_Real_Hist",
            "DMD_Real_Acumulada",
            "DMD_Bruta_Lote",
            "Cob_Min_Aplicada",
            "Cob_Alvo_Aplicada",
            "Cobertura_Exibicao",
            "Previsao_Ruptura",
            "Giro_Estoque",
            "Sugestao_Compra",
        ]
        colunas_presentes = [c for c in colunas_ordenadas if c in df_exibicao.columns]
        st.dataframe(
            df_exibicao[colunas_presentes].rename(
                columns={
                    "Curva_ABC": "Curva ABC",
                    "Status_Estoque": "Status Hoje",
                    "Dias_Sem_Mov_Estoque": "Dias s/ Mov (c/ Estoque)",
                    "Disponibilidade_Pct": "Disponib. (%)",
                    "Dias_Ruptura_Hist": "Dias Zerado (Passado)",
                    "Perda_Financeira_Hist": "Venda Perdida Hist. (R$)",
                    "Saldo_Calculado": "Saldo Hoje",
                    "Valor_Estoque_Atual": "Valor Estoque (R$)",
                    "Var_Saldo": "Var. Saldo (vs P1)",
                    "Total_Saidas": "Saídas (Qtd)",
                    "Valor_Saidas": "Faturamento (R$)",
                    "Lucro_Bruto": "Lucro Bruto (R$)",
                    "Margem_Bruta_Exib": "Margem Bruta",
                    "Lucro_Liquido_Est": "Lucro Líq. Est. (R$)",
                    "Margem_Liq_Exib": "Margem Líq. Est.",
                    "DMD_Real_Hist": "DMD Real",
                    "DMD_Real_Acumulada": "DMD Real Acum.",
                    "DMD_Bruta_Lote": "DMD Bruta",
                    "Cob_Min_Aplicada": "Cob. Mín. (d)",
                    "Cob_Alvo_Aplicada": "Cob. Alvo (d)",
                    "Cobertura_Exibicao": "Cobertura Futura",
                    "Previsao_Ruptura": "Previsão Ruptura",
                    "Giro_Estoque": "Giro Real",
                    "Sugestao_Compra": "Sugestão Compra",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
        data_hoje = datetime.now().strftime("%Y-%m-%d_%H-%M")
        csv_indicadores = df_filtrado.to_csv(index=False, sep=";").encode("utf-8-sig")
        st.download_button(
            label="📥 Baixar Relatório Completo de Indicadores por SKU (.CSV)",
            data=csv_indicadores,
            file_name=f"indicadores_estoque_historico_{data_hoje}.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )

    # --- SUB-ABA 2: INSPETOR DE EXTRATO DIÁRIO (KARDEX) ---
    with tab_kardex:
        st.subheader("Inspetor de Extrato Diário (Kardex por SKU)")
        st.caption(
            "Auditoria dia a dia pela Regra Ouro: o dia em que a última unidade é vendida conta como disponível; "
            "apenas dias seguintes com prateleira vazia e sem saída são marcados como ruptura."
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
                "Selecione o produto para inspecionar o histórico diário:",
                options=opcoes_sku_kardex,
                key="select_sku_kardex",
            )
            sku_selecionado = mapa_label_sku[sku_escolhido_label]

            row_sku = df_ind[df_ind["SKU"] == sku_selecionado].iloc[0]
            df_k_sku = df_kardex_completo[df_kardex_completo["SKU"] == sku_selecionado].copy()

            # --- NOVO: Conta quantas vezes o SKU apareceu nos XMLs por dia ---
            df_k_sku["Aparicoes_XML"] = 0
            if not df_movs.empty and "SKU" in df_movs.columns and "Data" in df_movs.columns:
                df_mov_sku = df_movs[df_movs["SKU"].astype(str).str.strip() == str(sku_selecionado).strip()].copy()
                if not df_mov_sku.empty:
                    # Padroniza o formato da data para cruzar com df_k_sku["Data"]
                    df_mov_sku["_Data_Str"] = pd.to_datetime(df_mov_sku["Data"]).dt.strftime("%Y-%m-%d")
                    df_k_sku["_Data_Str"] = pd.to_datetime(df_k_sku["Data"]).dt.strftime("%Y-%m-%d")
                    contagem_xml = df_mov_sku.groupby("_Data_Str").size().to_dict()
                    df_k_sku["Aparicoes_XML"] = df_k_sku["_Data_Str"].map(contagem_xml).fillna(0).astype(int)
                    df_k_sku.drop(columns=["_Data_Str"], inplace=True)

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Dias com Estoque", f"{int(row_sku['Dias_Com_Estoque'])} / {int(row_sku['Dias_Janela_Kardex'])} d")
            k2.metric("Dias Zerado (Passado)", f"{int(row_sku['Dias_Ruptura_Hist'])} d")
            k3.metric("DMD Real (Dias Disp.)", f"{row_sku['DMD_Real_Hist']:.2f} un/d")
            k4.metric("Venda Perdida Hist.", _formatar_brl(float(row_sku["Perda_Financeira_Hist"])))
            k5.metric(
                "Margem Bruta / Líq.",
                f"{row_sku['Margem_Bruta_Pct']:.1f}% / {row_sku['Margem_Liquida_Pct']:.1f}%"
                if pd.notna(row_sku["Margem_Bruta_Pct"])
                else "Sem custo XML",
            )
            st.line_chart(
                df_k_sku.set_index("Data")["Saldo_Fechamento"],
                use_container_width=True,
            )

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
                        "Aparicoes_XML": "Aparições XML (Linhas)",
                        "Saldo_Abertura": "Saldo Abertura",
                        "Entradas": "Entradas (+)",
                        "Saldo_Disponivel": "Saldo Disponível",
                        "Saidas": "Saídas (-)",
                        "Saldo_Fechamento": "Saldo Fechamento",
                        "Situacao_Dia": "Situação no Dia (Regra Ouro)",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
            csv_kardex_sku = df_k_sku.to_csv(index=False, sep=";").encode("utf-8-sig")
            st.download_button(
                label=f"📥 Baixar Extrato Diário do SKU {sku_selecionado} (.CSV)",
                data=csv_kardex_sku,
                file_name=f"kardex_diario_{sku_selecionado}_{datetime.now().strftime('%Y-%m-%d')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # --- SUB-ABA 3: CONSOLIDADO POR CATEGORIA E MARCA ---
    with tab_agrupado:
        st.subheader("Consolidado por Categoria e Marca")
        visao_grupo = st.radio(
            "Agrupar resultados por:",
            options=["Categoria", "Marca"],
            horizontal=True,
            key="radio_agrupamento_cat_marca",
        )
        df_grupo = _agrupar_por_dimensao(df_ind, visao_grupo, float(pct_deducoes_despesas))
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
                delta=f"Compra sugerida total: {int(df_grupo['Sugestao_Compra_Un'].sum())} un.",
                delta_color="off",
            )
            st.markdown(f"Faturamento Realizado vs. Venda Perdida Histórica por {visao_grupo} (R$)")
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
                "Lucro_Bruto_Perdido_RS",
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
                        "Lucro_Bruto_Perdido_RS": "Lucro Bruto Perdido (R$)",
                        "Capital_Estoque_RS": "Capital Estoque (R$)",
                        "Giro_Medio": "Giro Médio",
                        "Sugestao_Compra_Un": "Sugestão Compra (un.)",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
            csv_grupo = df_grupo.to_csv(index=False, sep=";").encode("utf-8-sig")

            st.download_button(
                label=f"📥 Baixar Consolidado por {visao_grupo} (.CSV)",
                data=csv_grupo,
                file_name=f"consolidado_{visao_grupo.lower()}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # --- SUB-ABA 4: EVOLUÇÃO DIÁRIA GERAL ---
    with tab_evolucao:
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
            st.subheader("Evolução Diária de SKUs em Ruptura (Prateleira Vazia sem Saída)")
            ruptura_por_dia = (
                    df_kardex_completo.assign(
                        Em_Ruptura=lambda d: np.where(d["Situacao_Dia"] == "🔴 Zerado / Ruptura", 1, 0)
                    )
                    .groupby("Data", as_index=False)["Em_Ruptura"]
                    .sum()
                    .sort_values("Data")
                )
            st.line_chart(ruptura_por_dia.set_index("Data")["Em_Ruptura"])
