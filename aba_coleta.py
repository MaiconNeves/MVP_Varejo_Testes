from datetime import datetime
from google import genai
from google.genai import errors
from PIL import Image
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_qrcode_scanner import qrcode_scanner
from api_produtos import buscar_dados_mestres


def desligar_cameras_android():
    """Força o Chrome no Android a liberar a câmera física utilizada pelo scanner."""
    components.html(
        """
        <script>
        function pararTracks(doc) {
            if (!doc) return;
            const videos = doc.querySelectorAll('video');
            videos.forEach(v => {
                if (v.srcObject) {
                    v.srcObject.getTracks().forEach(track => track.stop());
                    v.srcObject = null;
                }
            });
        }
        try {
            pararTracks(document);
            if (window.parent && window.parent.document) {
                pararTracks(window.parent.document);
                const iframes = window.parent.document.querySelectorAll('iframe');
                iframes.forEach(iframe => {
                    try {
                        pararTracks(iframe.contentDocument || iframe.contentWindow.document);
                    } catch (e) {}
                });
            }
        } catch (err) {}
        </script>
        """,
        height=0,
        width=0,
    )


def contar_itens_nas_fotos(lista_fotos, nome_produto):
    """Envia as fotos redimensionadas para o Gemini e soma as unidades identificadas."""
    api_key = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        st.error("Chave GEMINI_API_KEY não encontrada nos Secrets do Streamlit.")
        return None

    client = genai.Client(api_key=api_key)
    total_contado = 0

    modelos_disponiveis = [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-4-hydrogen",
    ]

    prompt = (
        f"Você é um auditor de inventário de estoque. Analise esta imagem e conte "
        f"exatamente quantas unidades do produto '{nome_produto}' estão visíveis. "
        f"Conte apenas itens inteiros e claramente identificáveis deste produto. "
        f"Responda ESTRITAMENTE com o número inteiro (exemplo: 14). "
        f"Se não houver nenhuma unidade, responda 0."
    )

    for idx, foto_bytes in enumerate(lista_fotos, start=1):
        try:
            foto_bytes.seek(0)
            imagem = Image.open(foto_bytes).convert("RGB")
            imagem.thumbnail((1600, 1600))
        except Exception as e:
            st.error(f"Não foi possível processar a imagem {idx}: {e}")
            return None

        resposta = None
        ultimo_erro_api = None

        for modelo in modelos_disponiveis:
            try:
                resposta = client.models.generate_content(
                    model=modelo,
                    contents=[imagem, prompt],
                )
                break
            except errors.ClientError as e:
                ultimo_erro_api = e
                if getattr(e, "code", None) in (400, 403):
                    break
                continue
            except Exception as e:
                ultimo_erro_api = e
                continue

        if resposta is None:
            codigo_status = getattr(ultimo_erro_api, "code", "N/A")
            mensagem_api = getattr(ultimo_erro_api, "message", str(ultimo_erro_api))
            st.error(
                f"Falha na API do Gemini (Código {codigo_status}): {mensagem_api}\n\n"
                "Verifique nos Secrets do Streamlit se a GEMINI_API_KEY foi copiada sem espaços "
                "e se a API está ativa no Google AI Studio."
            )
            return None

        texto_resp = (resposta.text or "").strip()
        digitos = "".join(filter(str.isdigit, texto_resp))
        total_contado += int(digitos) if digitos else 0

    return total_contado


def inicializar_estados():
    """Garante que todas as variáveis de sessão existam antes de rodar a tela."""
    estados_padrao = {
        "itens_coletados": {},
        "codigo_atual": None,
        "aviso_duplicado": None,
        "dados_api": {},
        "modo_foto_inventario": False,
        "qtd_calculada_ia": 1.0,
        "scanner_ativo": True,
        "scanner_iteracao": 0,
        "ultimo_codigo_scanner": None,
    }
    for chave, valor in estados_padrao.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor


def resetar_para_proximo_item():
    """Limpa o item atual e reativa o leitor para a próxima bipagem."""
    st.session_state.codigo_atual = None
    st.session_state.qtd_calculada_ia = 1.0
    st.session_state.modo_foto_inventario = False
    st.session_state.scanner_ativo = True
    st.session_state.scanner_iteracao += 1


def renderizar_aba_coleta():
    inicializar_estados()

    # 1. Escolha do método de entrada
    modo_entrada = st.radio(
        "Como deseja informar o código do produto?",
        options=["Computador (Digitar SKU / EAN)", "Celular (Câmera)"],
        horizontal=True,
    )

    codigo_lido = None

    # 2. Captura do código conforme a escolha
    if modo_entrada == "Celular (Câmera)":
        if st.session_state.scanner_ativo and not st.session_state.codigo_atual:
            st.caption("Aponte a câmera para o código de barras ou QR Code.")
            leitura_bruta = qrcode_scanner(
                key=f"scanner_mvp_{st.session_state.scanner_iteracao}"
            )
            if leitura_bruta and leitura_bruta != st.session_state.ultimo_codigo_scanner:
                st.session_state.ultimo_codigo_scanner = leitura_bruta
                codigo_lido = leitura_bruta
                st.session_state.scanner_ativo = False
                st.session_state.scanner_iteracao += 1
                desligar_cameras_android()
        else:
            desligar_cameras_android()
            if st.button("🔄 Ler Outro Código de Barras", use_container_width=True):
                st.session_state.aviso_duplicado = None
                st.session_state.ultimo_codigo_scanner = None
                resetar_para_proximo_item()
                st.rerun()
    else:
        desligar_cameras_android()
        with st.form("form_busca_manual", clear_on_submit=True):
            c_input, c_btn = st.columns([3, 1])
            sku_digitado = c_input.text_input(
                "Digite o SKU ou Código de Barras (EAN)",
                placeholder="Ex: 7894900011517",
                label_visibility="collapsed",
            )
            buscar = c_btn.form_submit_button("Buscar Código", use_container_width=True)

            if buscar and sku_digitado.strip():
                codigo_lido = sku_digitado.strip()

    # 3. Processamento unificado do código
    if codigo_lido:
        ean_bipado = "".join(filter(str.isdigit, str(codigo_lido))) or str(codigo_lido)

        if ean_bipado in st.session_state.itens_coletados:
            item_ex = st.session_state.itens_coletados[ean_bipado]
            st.session_state.aviso_duplicado = (
                f"Código **{ean_bipado}** ({item_ex['Descrição']}) já foi cadastrado"
                f" com saldo **{item_ex['Qtd Física']}**!"
            )
            st.session_state.codigo_atual = None
            st.rerun()

        elif ean_bipado != st.session_state.codigo_atual:
            st.session_state.aviso_duplicado = None
            st.session_state.codigo_atual = ean_bipado
            st.session_state.modo_foto_inventario = False
            st.session_state.qtd_calculada_ia = 1.0
            with st.spinner("Buscando dados mestres..."):
                st.session_state.dados_api = buscar_dados_mestres(ean_bipado)
            st.rerun()

    # 4. Alerta caso o código já exista na lista
    if st.session_state.aviso_duplicado:
        st.error(st.session_state.aviso_duplicado)

    # 5. Formulário de confirmação do novo item + Painel de captura via câmera nativa
    if st.session_state.codigo_atual:
        st.info(f"**Novo Código Selecionado:** {st.session_state.codigo_atual}")
        info = st.session_state.dados_api
        desc_padrao = info.get("descricao", "")
        cat_padrao = info.get("categoria", "") or "Geral"
        marca_padrao = info.get("marca", "")

        if st.button(
            "📸 Gerar Inventário do Item (Contar por Fotos)",
            use_container_width=True,
        ):
            st.session_state.modo_foto_inventario = not st.session_state.modo_foto_inventario
            if st.session_state.modo_foto_inventario:
                desligar_cameras_android()

        if st.session_state.modo_foto_inventario:
            desligar_cameras_android()
            with st.container(border=True):
                st.markdown("#### Contagem Visual do Estoque")
                st.caption(
                    "Toque abaixo para abrir a câmera ou selecionar fotos da galeria."
                )

                fotos_selecionadas = st.file_uploader(
                    "📷 Abrir Câmera ou Galeria",
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=True,
                    key=f"uploader_{st.session_state.codigo_atual}",
                )

                if fotos_selecionadas:
                    qtd_fotos = len(fotos_selecionadas)
                    st.success(f"📎 **{qtd_fotos} foto(s) recebida(s)!**")

                    cols_prev = st.columns(min(qtd_fotos, 3))
                    for idx, f_img in enumerate(fotos_selecionadas[:3]):
                        cols_prev[idx].image(f_img, use_container_width=True)

                    if st.button(
                        "✅ Contar Itens nas Fotos Agora",
                        type="primary",
                        use_container_width=True,
                    ):
                        with st.spinner(f"Analisando {qtd_fotos} foto(s) com Gemini..."):
                            nome_busca = (
                                desc_padrao
                                if desc_padrao
                                else st.session_state.codigo_atual
                            )
                            total = contar_itens_nas_fotos(
                                fotos_selecionadas, nome_busca
                            )
                            if total is not None:
                                st.session_state.qtd_calculada_ia = float(total)
                                st.session_state.modo_foto_inventario = False
                                st.success(f"Contagem concluída: **{total}** unidades!")
                                st.rerun()

        # Formulário principal (editável antes de salvar)
        with st.form("form_estoque", clear_on_submit=True):
            descricao = st.text_input("Descrição", value=desc_padrao)
            c1, c2, c3, c4 = st.columns(4)
            categoria = c1.text_input("Categoria / Setor", value=cat_padrao)
            marca = c2.text_input("Marca", value=marca_padrao)
            quantidade = c3.number_input(
                "Qtd Atual na Prateleira",
                min_value=0.0,
                value=st.session_state.qtd_calculada_ia,
                step=1.0,
            )
            fator_conv = c4.number_input(
                "Fator Conv. (Cx -> Un)",
                min_value=1.0,
                value=1.0,
                step=1.0,
                help="Se a nota fiscal de compra vem em caixa com 12 unidades e você vende por unidade, informe 12.",
            )

            b1, b2 = st.columns(2)
            if b1.form_submit_button("Salvar Item", use_container_width=True):
                st.session_state.itens_coletados[st.session_state.codigo_atual] = {
                    "SKU": st.session_state.codigo_atual,
                    "Descrição": descricao.strip() if descricao.strip() else "Item sem descrição",
                    "Categoria": categoria.strip() if categoria.strip() else "Geral",
                    "Marca": marca.strip() if marca.strip() else "Sem Marca",
                    "Qtd Física": quantidade,
                    "Fator_Conversao": fator_conv,
                }
                st.session_state.ultimo_codigo_scanner = None
                resetar_para_proximo_item()
                st.rerun()

            if b2.form_submit_button("Cancelar", use_container_width=True):
                st.session_state.ultimo_codigo_scanner = None
                resetar_para_proximo_item()
                st.rerun()

    # 6. Tabela dos itens já salvos + Exportação CSV
    if st.session_state.itens_coletados:
        st.subheader(f"Itens Monitorados ({len(st.session_state.itens_coletados)})")
        df_coletados = pd.DataFrame(list(st.session_state.itens_coletados.values()))
        st.dataframe(df_coletados, use_container_width=True, hide_index=True)

        csv_bytes = df_coletados.to_csv(index=False, sep=";").encode("utf-8-sig")
        data_hoje = datetime.now().strftime("%Y-%m-%d_%H-%M")
        nome_arquivo = f"inventario_coleta_{data_hoje}.csv"

        st.download_button(
            label="📥 Baixar Planilha da Coleta (.CSV)",
            data=csv_bytes,
            file_name=nome_arquivo,
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )
