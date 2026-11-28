from datetime import datetime
import pandas as pd
import streamlit as st
from aba_indicadores import calcular_indicadores_estoque


def montar_resumo_executivo(nome_loja: str, responsavel: str, df_ind: pd.DataFrame, tipo_resumo: str) -> str:
    """Gera texto de resumo gerencial para revisão/cópia durante os testes."""
    saudacao = f"Olá {responsavel}!" if responsavel else "Olá!"
    df = df_ind.copy()

    for col in ["Saldo_Calculado", "DMD", "Total_Saidas", "Sugestao_Compra", "Cob_Alvo_Aplicada"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 15.0 if col == "Cob_Alvo_Aplicada" else 0.0

    if "Curva_ABC" not in df.columns:
        df["Curva_ABC"] = "C"

    if tipo_resumo == "🚨 Ruptura (Todos os Itens)":
        criticos = (
            df[df["Saldo_Calculado"] <= 0]
            .sort_values(by=["Curva_ABC", "DMD", "Total_Saidas"], ascending=[True, False, False])
            .head(20)
        )
        if criticos.empty:
            return f"✅ {nome_loja.upper()} — Nenhum item está em ruptura hoje."

        linhas = [
            f"🚨 ALERTA DE RUPTURA — {nome_loja.upper()}",
            f"{saudacao} Estes itens estão ZERADOS / EM RUPTURA agora:\n",
        ]
        for _, r in criticos.iterrows():
            dmd = float(r["DMD"])
            cob_alvo = float(r["Cob_Alvo_Aplicada"]) if float(r["Cob_Alvo_Aplicada"]) > 0 else 15.0
            sug_calc = float(r["Sugestao_Compra"])
            if sug_calc <= 0 and dmd > 0:
                sug_calc = max(1.0, round(dmd * cob_alvo - float(r["Saldo_Calculado"])))
            sug = int(round(sug_calc))
            curva = r.get("Curva_ABC", "C")

            linhas.append(f"• {r.get('Descrição', r.get('SKU', 'Produto'))} (Curva {curva})")
            linhas.append(f"  Venda média: {dmd:.2f}/dia | Sugestão compra: {sug} un")
        return "\n".join(linhas)

    elif tipo_resumo == "📦 Estoque Parado / Gôndola":
        parados = (
            df[(df["Saldo_Calculado"] > 0) & (df["Total_Saidas"] == 0)]
            .sort_values(by="Saldo_Calculado", ascending=False)
            .head(15)
        )
        if parados.empty:
            return f"✅ {nome_loja.upper()} — Nenhum item parado com saldo positivo encontrado."

        linhas = [
            f"📦 ITENS PARADOS — {nome_loja.upper()}",
            f"{saudacao} Itens com saldo em estoque mas sem vendas no período:\n",
        ]
        for _, r in parados.iterrows():
            linhas.append(f"• {r.get('Descrição', r.get('SKU', 'Produto'))} — Saldo: {r['Saldo_Calculado']:.0f} un")
        return "\n".join(linhas)

    else:
        total_skus = len(df)
        em_ruptura = int((df["Saldo_Calculado"] <= 0).sum())
        parados = int(((df["Saldo_Calculado"] > 0) & (df["Total_Saidas"] == 0)).sum())
        top_venda = df.sort_values(by="Total_Saidas", ascending=False).head(5)

        linhas = [
            f"📊 RESUMO EXECUTIVO — {nome_loja.upper()}",
            f"{saudacao} Confira o raio-X rápido do estoque:\n",
            f"• EANs monitorados: {total_skus}",
            f"• Em Ruptura (Comprar urgente): {em_ruptura} itens",
            f"• Parados (Conferir gôndola): {parados} itens\n",
            "🏆 Mais vendidos do período:",
        ]
        for _, r in top_venda.iterrows():
            linhas.append(f"  - {r.get('Descrição', r.get('SKU', ''))} ({int(r['Total_Saidas'])} un)")
        return "\n".join(linhas)


def _preparar_csv_proximo_ciclo(df_fonte: pd.DataFrame) -> bytes:
    """Formata o saldo atual como 'Qtd Inicial' indexado por EAN para a Etapa 1 do próximo ciclo."""
    df_export = pd.DataFrame()
    df_export["EAN"] = df_fonte["SKU"]
    df_export["Descrição"] = df_fonte.get("Descrição", "")
    df_export["Categoria"] = df_fonte.get("Categoria", "Geral")
    df_export["Marca"] = df_fonte.get("Marca", "Sem Marca")
    df_export["Fator_Conversao"] = df_fonte.get("Fator_Conversao", 1.0)
    df_export["Qtd Inicial"] = df_fonte.get("Saldo_Calculado", 0.0)
    df_export["Ref_Data"] = datetime.now().strftime("%Y-%m-%d")

    return df_export.to_csv(index=False, sep=";").encode("utf-8-sig")


def renderizar_aba_fechamento(loja_nome_padrao: str = "Loja 01 - Matriz"):
    st.header("📋 Fechamento de Ciclo e Resumo Gerencial (Modo Testes)")
    st.info("🧪 **Modo de Testes Ativo:** Banco de dados e disparos de WhatsApp desativados temporariamente.")

    c1, c2 = st.columns([2, 1.5])
    nome_loja = c1.text_input("Nome da Loja / Unidade:", value=loja_nome_padrao, key="teste_nome_loja")
    responsavel = c2.text_input("Responsável / Comprador (opcional):", placeholder="Ex: Seu José", key="teste_resp")

    df_cons = st.session_state.get("df_xml_consolidado", pd.DataFrame())
    if df_cons.empty:
        st.warning("⚠️ Processe os XMLs na **Etapa 2** para liberar a exportação do fechamento e o resumo.")
        return

    df_fonte = st.session_state.get("df_indicadores_calculados")
    if df_fonte is None or df_fonte.empty or "DMD" not in df_fonte.columns:
        df_movs = st.session_state.get("df_movs_detalhadas", pd.DataFrame())
        dias_calc = 30
        if df_movs is not None and not df_movs.empty and "Data_Emissao" in df_movs.columns:
            datas_validas = pd.to_datetime(df_movs["Data_Emissao"], errors="coerce").dropna()
            if not datas_validas.empty:
                diff_dias = int((datas_validas.max() - datas_validas.min()).days) + 1
                if 1 <= diff_dias <= 365:
                    dias_calc = diff_dias

        df_fonte, _ = calcular_indicadores_estoque(
            df_consolidado=df_cons,
            df_movs=df_movs,
            dias_periodo=dias_calc,
        )
        st.session_state["df_indicadores_calculados"] = df_fonte.copy()

    st.divider()
    st.subheader("1. Exportar Arquivo para Continuar Próximo Ciclo")
    st.caption(
        "Como o banco de dados está desligado nos testes, baixe este arquivo para usá-lo na "
        "**Etapa 1 (Continuar do Ciclo Anterior)** na próxima rodada."
    )

    data_hoje = datetime.now().strftime("%Y%m%d_%H%M")
    csv_bytes = _preparar_csv_proximo_ciclo(df_fonte)

    st.download_button(
        label="📥 Baixar Fechamento de Estoque (.CSV para Próximo Ciclo)",
        data=csv_bytes,
        file_name=f"fechamento_estoque_{data_hoje}.csv",
        mime="text/csv",
        type="primary",
    )

    st.divider()
    st.subheader("2. Pré-visualização do Resumo Gerencial")
    tipo_resumo = st.radio(
        "Selecione o modelo de resumo:",
        options=[
            "🚨 Ruptura (Todos os Itens)",
            "📦 Estoque Parado / Gôndola",
            "📊 Resumo Executivo do Dia",
        ],
        horizontal=True,
    )

    texto_gerado = montar_resumo_executivo(nome_loja, responsavel, df_fonte, tipo_resumo)
    st.text_area(
        "Texto gerado (apenas visualização local):",
        value=texto_gerado,
        height=240,
    )
