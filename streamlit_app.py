import streamlit as st
import re
import io
import requests
import urllib.parse
import zipfile
import unicodedata
import time
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple
from pypdf import PdfReader

# ==========================================
# CONFIGURAÇÃO DA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Radar Inteligente — DOEMS & BG CBMMS",
    page_icon="🔍",
    layout="centered"
)

# ==========================================
# CHAVE MESTRA (MODO DE TESTE - BOLETINS)
# ==========================================
ATIVAR_MODO_TESTE = True

# ==========================================
# CONFIGURAÇÕES DA API — BOLETINS (CBMMS)
# ==========================================
BASE_URL = "https://sistemas.bombeiros.ms.gov.br"
LOGIN_URL = f"{BASE_URL}/ws-auth/fazer-login"
BUSCA_BG_URL = f"{BASE_URL}/ws-boletim-geral/publicacao"
DOWNLOAD_BG_URL = f"{BASE_URL}/ws-alfresco/arquivo/"
REQUEST_TIMEOUT = (15, None)

# ==========================================
# ESTADO DA SESSÃO
# ==========================================
def inicializar_estado() -> None:
    defaults = {
        "busca_concluida": False,
        "bgs_encontrados": [],
        "doems_encontrados": [],
        "nome_pesquisado": "",
        "modo_pesquisa": "DOEMS",
        "mensagem_status": "",
        "modo_teste_ativo": False,
        "falhas_processamento": [],
        "tempo_pesquisa_segundos": None,
        "tempo_processamento_segundos": None,
    }
    for chave, valor in defaults.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor

inicializar_estado()

# ==========================================
# REGEX / PADRÕES — BOLETINS
# ==========================================
MESES_REGEX = (
    r"JANEIRO|FEVEREIRO|MAR[ÇC]O|ABRIL|MAIO|JUNHO|"
    r"JULHO|AGOSTO|SETEMBRO|OUTUBRO|NOVEMBRO|DEZEMBRO"
)
RE_NOTA = re.compile(r"^\s*NOTA\s+N\.\s*(\d+)\s*$", re.IGNORECASE)
RE_FOOTER = re.compile(
    r"BOLETIM GERAL N\.\s+\d+.*?P[ÁA]GINA\s+\d+\s*/\s*\d+", re.IGNORECASE
)
RE_ATTACHMENT = re.compile(r".+\.(pdf|docx?|xlsx?|jpg|jpeg|png)$", re.IGNORECASE)
RE_MILITAR_LISTA = re.compile(
    r"^(?:\dº\s*)?(?:CEL|TEN\s*CEL|TC|MAJ|CAP|ASP|CAD|1º TEN|2º TEN|ST|1º SGT|2º SGT|3º SGT|CB|SD)\s+BM\b",
    re.IGNORECASE,
)
RE_CABECALHOS_FIXOS = [
    re.compile(r"^ESTADO DE MATO GROSSO DO SUL$", re.IGNORECASE),
    re.compile(r"^SECRETARIA DE ESTADO", re.IGNORECASE),
    re.compile(r"^CORPO DE BOMBEIROS MILITAR", re.IGNORECASE),
    re.compile(r"^BOLETIM GERAL$", re.IGNORECASE),
    re.compile(r"^ANO\s+\d{4}\s+N\.\s+\d+", re.IGNORECASE),
    re.compile(r"^COMANDANTE-GERAL:", re.IGNORECASE),
    re.compile(r"^CHEFE DO ESTADO MAIOR GERAL:", re.IGNORECASE),
]
RE_LINHAS_LIXO = [
    re.compile(r"^P[ÁA]GINA\s+\d+\s*/\s*\d+$", re.IGNORECASE),
    re.compile(r"^\d{1,2}\s+DE\s+\w+\s+DE\s+\d{4}$", re.IGNORECASE),
    re.compile(r"^BOLETIM GERAL N\.\s+\d+$", re.IGNORECASE),
]

# ==========================================
# FUNÇÕES AUXILIARES GERAIS
# ==========================================
def formatar_cpf(cpf_bruto: str) -> str:
    cpf_limpo = re.sub(r"\D", "", cpf_bruto or "")
    cpf_limpo = cpf_limpo.zfill(11)
    if len(cpf_limpo) == 11:
        return f"{cpf_limpo[:3]}.{cpf_limpo[3:6]}.{cpf_limpo[6:9]}-{cpf_limpo[9:]}"
    return cpf_bruto


def formatar_duracao(segundos: Optional[float]) -> str:
    if segundos is None:
        return "-"
    if segundos < 60:
        return f"{segundos:.1f} s"
    minutos = int(segundos // 60)
    resto = segundos % 60
    return f"{minutos} min {resto:.1f} s"


def formatar_data_iso(data_iso: str) -> str:
    try:
        return datetime.strptime(data_iso.split("T")[0], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return data_iso


def data_para_ordenacao(data_str: str) -> datetime:
    """Converte strings de data variadas para datetime para ordenação."""
    formatos = ["%d/%m/%Y", "%Y-%m-%d", "%d DE %B DE %Y"]
    for fmt in formatos:
        try:
            return datetime.strptime(data_str.strip(), fmt)
        except Exception:
            pass
    # Tenta extrair por mês por extenso (ex: "17 DE MARÇO DE 2026")
    meses = {
        "JANEIRO": 1, "FEVEREIRO": 2, "MARÇO": 3, "MARCO": 3, "ABRIL": 4,
        "MAIO": 5, "JUNHO": 6, "JULHO": 7, "AGOSTO": 8,
        "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12,
    }
    m = re.search(r"(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", data_str.upper())
    if m:
        try:
            return datetime(int(m.group(3)), meses.get(m.group(2), 1), int(m.group(1)))
        except Exception:
            pass
    return datetime.min


# ==========================================
# FUNÇÕES — DOEMS
# ==========================================
def extrair_texto_pdf_doems(file_bytes: bytes):
    """Extrai todo o texto do PDF para busca no DOEMS."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        return text, len(reader.pages)
    except Exception as e:
        return None, str(e)


def processar_publicacao_doems(texto_completo: str, nome_busca: str) -> List[Dict]:
    """Lógica híbrida de extração do DOEMS: isola publicação e define se é Ato Direto ou Tabela."""
    nome_formatado = r"\s+".join(nome_busca.strip().split())
    regex_nome = re.compile(nome_formatado, re.IGNORECASE)
    regex_cabecalho = re.compile(
        r"(?i)(?:^|\n)\s*(PORTARIA|DECRETO|RESOLUÇÃO|EDITAL|ATO|EXTRATO|INSTRUÇÃO)[^\n]+"
    )
    cabecalhos = [(m.start(), m.group().strip()) for m in regex_cabecalho.finditer(texto_completo)]
    resultados = []

    for match_nome in regex_nome.finditer(texto_completo):
        pos_nome = match_nome.start()
        cabecalho_atual = None
        pos_inicio_ato = 0
        pos_fim_ato = len(texto_completo)

        for i, (pos_cab, texto_cab) in enumerate(cabecalhos):
            if pos_cab <= pos_nome:
                cabecalho_atual = texto_cab
                pos_inicio_ato = pos_cab
                if i + 1 < len(cabecalhos):
                    pos_fim_ato = cabecalhos[i + 1][0]
            else:
                break

        if not cabecalho_atual:
            continue

        ato_completo = texto_completo[pos_inicio_ato:pos_fim_ato].strip()

        if len(ato_completo) < 3500:
            resultados.append({
                "tipo": "direto",
                "cabecalho": cabecalho_atual,
                "texto_integral": ato_completo,
            })
        else:
            regex_acao = re.compile(
                r"(?i)(RESOLVE|RESOLVEM|DECIDE|DECRETA|TORNA PÚBLICO|CONVOCA|DESIGNAR|NOMEAR|EXONERAR|AUTORIZAR|CERTIFICA)[^\n]*?(?::|;|\n|$)"
            )
            match_acao = regex_acao.search(ato_completo)
            match_nome_ato = re.search(nome_formatado, ato_completo, re.IGNORECASE)
            pos_nome_no_ato = match_nome_ato.start() if match_nome_ato else len(ato_completo)

            if match_acao:
                inicio_pos_acao = match_acao.end()
                texto_pos_acao = ato_completo[inicio_pos_acao:pos_nome_no_ato]
                padrao_inicio_tabela = r"(?i)\n\s*(?:NOME|MATR[ÍI]CULA|ORDEM|ANEXO|\d+[\.\-]?\s+(?:CEL|TC|MAJ|CAP|TEN|ASP|CAD|AL|SUBTEN|SGT|CB|SD|BM|PM)|(?:CEL|TC|MAJ|CAP|TEN|ASP|CAD|AL|SUBTEN|SGT|CB|SD)\s+(?:BM|PM|QOBM|QABM)|\d{1,3}\s+-\s+[A-Z])"
                match_tabela = re.search(padrao_inicio_tabela, texto_pos_acao)
                acao_texto = texto_pos_acao[:match_tabela.start()].strip() if match_tabela else texto_pos_acao.strip()
                contexto = ato_completo[:inicio_pos_acao].strip() + "\n\n" + acao_texto
            else:
                contexto = ato_completo[:min(600, pos_nome_no_ato)].strip() + "\n[...]"

            linhas = ato_completo.split("\n")
            linha_idx = 0
            for idx, l in enumerate(linhas):
                if re.search(nome_formatado, l, re.IGNORECASE):
                    linha_idx = idx
                    break

            linha_bruta = linhas[linha_idx]
            if linha_idx + 1 < len(linhas):
                linha_bruta += " " + linhas[linha_idx + 1]

            match_nome_linha = re.search(nome_formatado, linha_bruta, re.IGNORECASE)
            if match_nome_linha:
                pos_fim_nome = match_nome_linha.end()
                texto_pos_nome = linha_bruta[pos_fim_nome:]
                match_matr = re.search(r"\d{2,3}\.?\d{3}-?\d{1,3}", texto_pos_nome)
                if match_matr:
                    linha_bruta = linha_bruta[: pos_fim_nome + match_matr.end()]
                else:
                    padrao_patente = r"(?i)(?:\d+[\.\-ºª]?\s+)?(?:CEL|TC|MAJ|CAP|TEN|ASP|CAD|AL|SUBTEN|SGT|CB|SD)\b"
                    match_prox = re.search(padrao_patente, texto_pos_nome)
                    if match_prox:
                        linha_bruta = linha_bruta[: pos_fim_nome + match_prox.start()]

                texto_pre_nome = linha_bruta[: match_nome_linha.start()]
                padrao_patente = r"(?i)(?:\d+[\.\-ºª]?\s+)?(?:CEL|TC|MAJ|CAP|TEN|ASP|CAD|AL|SUBTEN|SGT|CB|SD)\b"
                matches_patentes = list(re.finditer(padrao_patente, texto_pre_nome))
                if matches_patentes:
                    linha_bruta = linha_bruta[matches_patentes[-1].start():]

            resultados.append({
                "tipo": "tabela",
                "cabecalho": cabecalho_atual,
                "contexto": contexto.strip(),
                "linha_dados": linha_bruta.strip(),
            })

    # Desduplicação
    resultados_unicos = []
    chaves = set()
    for r in resultados:
        chave = r["cabecalho"] + (r.get("linha_dados", "") or r.get("texto_integral", ""))
        if chave not in chaves:
            chaves.add(chave)
            resultados_unicos.append(r)

    return resultados_unicos


# ==========================================
# FUNÇÕES — BOLETINS (CBMMS)
# ==========================================
def normalizar_unicode(texto: str) -> str:
    if not texto:
        return ""
    substituicoes = {
        "\u00A0": " ", "\u00AD": "", "\u200B": "", "\ufeff": "",
        "–": "-", "—": "-", "\u201C": '"', "\u201D": '"',
        "\u2018": "'", "\u2019": "'", "\ufffe": "",
    }
    for antigo, novo in substituicoes.items():
        texto = texto.replace(antigo, novo)
    mapa_letras = str.maketrans({"Ν": "N", "О": "O", "Ο": "O", "Τ": "T", "Α": "A", "М": "M", "С": "C", "Р": "P", "І": "I"})
    texto = texto.translate(mapa_letras)
    texto = re.sub(r"[ΝN][ΟO][ΤT][ΑA]\s+[NΝ]\.", "NOTA N.", texto, flags=re.IGNORECASE)
    return texto


def normalizar_para_match(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = normalizar_unicode(texto)
    texto = texto.lower()
    return re.sub(r"\s+", " ", texto).strip()


def nome_aparece_no_bloco(texto_bloco: str, nome_militar: str) -> bool:
    if not texto_bloco or not nome_militar:
        return False
    texto_match = normalizar_para_match(texto_bloco)
    nome_match = normalizar_para_match(nome_militar)
    if nome_match in texto_match:
        return True
    try:
        partes = [re.escape(p) for p in re.split(r"\s+", nome_match.strip()) if p]
        regex = r"\b" + r"\s+".join(partes) + r"\b"
        if re.search(regex, texto_match, flags=re.IGNORECASE):
            return True
    except re.error:
        pass
    tokens = [t for t in nome_match.split() if len(t) >= 3]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in texto_match)
    return (hits / len(tokens)) >= 0.75


def limpar_texto_para_exibicao(texto: str) -> str:
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def extrair_data_documento_pdf(pdf_bytes: bytes) -> str:
    try:
        leitor = PdfReader(io.BytesIO(pdf_bytes))
        if not leitor.pages:
            return "Data não identificada"
        texto = leitor.pages[0].extract_text() or ""
        texto = normalizar_unicode(texto).replace("\r", "\n")
        texto_flat = re.sub(r"[ \t]+", " ", texto)
        padroes = [
            rf"ANO\s+\d{{4}}\s+N\.\s+\d+\s+(\d{{1,2}}\s+DE\s+(?:{MESES_REGEX})\s+DE\s+\d{{4}})\s+\d+\s+P[ÁA]GINAS?",
            rf"\b(\d{{1,2}}\s+DE\s+(?:{MESES_REGEX})\s+DE\s+\d{{4}})\b",
            r"\b(\d{2}/\d{2}/\d{4})\b",
        ]
        for padrao in padroes:
            m = re.search(padrao, texto_flat, flags=re.IGNORECASE)
            if m:
                return m.group(1).upper()
        return "Data não identificada"
    except Exception:
        return "Data não identificada"


def linha_eh_titulo(linha: str) -> bool:
    linha = (linha or "").strip()
    if not linha or RE_NOTA.match(linha) or RE_ATTACHMENT.match(linha) or RE_MILITAR_LISTA.match(linha):
        return False
    if re.search(r"^ADM\.$|Aprovado por:|Militares Relacionados com a Nota|Responsável pelo ato", linha, re.IGNORECASE):
        return False
    if linha.endswith((".", ";", ":")) or len(linha) > 100:
        return False
    letras = [c for c in linha if c.isalpha()]
    if not letras:
        return False
    maiusculas = sum(1 for c in letras if c.isupper())
    proporcao = maiusculas / len(letras)
    return proporcao >= 0.65 or (proporcao >= 0.45 and len(linha.split()) <= 7)


def limpar_linhas_pagina(texto_pagina: str) -> List[str]:
    texto_pagina = (texto_pagina or "").replace("\r", "\n")
    linhas = []
    for linha in texto_pagina.splitlines():
        linha = normalizar_unicode(linha)
        linha = re.sub(r"[ \t]+", " ", linha).strip()
        if not linha or RE_FOOTER.search(linha):
            continue
        if any(p.search(linha) for p in RE_CABECALHOS_FIXOS):
            continue
        if any(p.search(linha) for p in RE_LINHAS_LIXO):
            continue
        linhas.append(linha)
    return linhas


def extrair_linhas_do_pdf(pdf_bytes: bytes) -> List[str]:
    leitor = PdfReader(io.BytesIO(pdf_bytes))
    linhas = []
    for pagina in leitor.pages:
        texto = pagina.extract_text() or ""
        linhas.extend(limpar_linhas_pagina(texto))
    return linhas


def extrair_contexto_pre_nota(linhas: List[str], idx_nota: int) -> Tuple[str, str, int]:
    j = idx_nota - 1
    assunto = []
    while j >= 0 and linha_eh_titulo(linhas[j]):
        assunto.insert(0, linhas[j])
        j -= 1
        if len(assunto) >= 3:
            break
    setor = []
    k = j
    while k >= 0 and len(setor) < 2:
        linha = linhas[k].strip()
        if not linha:
            k -= 1
            continue
        if RE_NOTA.match(linha) or RE_ATTACHMENT.match(linha) or RE_MILITAR_LISTA.match(linha):
            break
        if re.search(r"^ADM\.$|Aprovado por:|Militares Relacionados com a Nota|Responsável pelo ato", linha, re.IGNORECASE):
            break
        if len(linha) > 100 or linha.endswith((".", ";", ":")):
            break
        setor.insert(0, linha)
        k -= 1
    idx_inicio_bloco = idx_nota - len(assunto) if assunto else idx_nota
    return " ".join(setor).strip(), " ".join(assunto).strip(), idx_inicio_bloco


def aparar_bloco_ate_ultimo_militar(linhas_bloco: List[str]) -> List[str]:
    marcador = None
    for i, linha in enumerate(linhas_bloco):
        if re.search(r"^Militares Relacionados com a Nota$", linha, re.IGNORECASE):
            marcador = i
            break
    if marcador is None:
        return linhas_bloco
    ultimo = marcador
    for i in range(marcador + 1, len(linhas_bloco)):
        linha = linhas_bloco[i].strip()
        if not linha:
            continue
        if RE_ATTACHMENT.match(linha) or RE_NOTA.match(linha):
            break
        if re.search(r"^ADM\.$", linha, re.IGNORECASE) or RE_MILITAR_LISTA.match(linha):
            ultimo = i
            continue
        if linha_eh_titulo(linha):
            break
        if i > marcador + 1:
            break
    return linhas_bloco[: ultimo + 1]


def montar_blocos_de_notas(linhas_pdf: List[str]) -> List[Dict]:
    notas = []
    indices_notas = [i for i, linha in enumerate(linhas_pdf) if RE_NOTA.match(linha)]
    if not indices_notas:
        return notas
    metadados = []
    for idx_nota in indices_notas:
        setor, assunto, idx_inicio = extrair_contexto_pre_nota(linhas_pdf, idx_nota)
        match_num = RE_NOTA.match(linhas_pdf[idx_nota])
        numero = match_num.group(1) if match_num else "?"
        metadados.append({"idx_nota": idx_nota, "idx_inicio": idx_inicio, "numero": numero, "setor": setor, "cabecalho": assunto or "Sem assunto identificado"})
    for pos, meta in enumerate(metadados):
        inicio = meta["idx_inicio"]
        fim = metadados[pos + 1]["idx_inicio"] if pos + 1 < len(metadados) else len(linhas_pdf)
        linhas_bloco = aparar_bloco_ate_ultimo_militar(linhas_pdf[inicio:fim])
        texto_completo = "\n".join(linhas_bloco).strip()
        notas.append({"nota": meta["numero"], "setor": meta["setor"], "cabecalho": meta["cabecalho"], "texto_completo": limpar_texto_para_exibicao(texto_completo)})
    return notas


def extrair_notas_do_militar(pdf_bytes: bytes, nome_militar: str) -> List[Dict]:
    linhas_pdf = extrair_linhas_do_pdf(pdf_bytes)
    blocos = montar_blocos_de_notas(linhas_pdf)
    return [b for b in blocos if nome_aparece_no_bloco(b["texto_completo"], nome_militar)]


# ==========================================
# API — BOLETINS
# ==========================================
def autenticar(sessao: requests.Session, usuario: str, senha: str) -> None:
    resposta = sessao.post(LOGIN_URL, json={"login": usuario, "senha": senha}, timeout=REQUEST_TIMEOUT)
    if resposta.status_code != 200:
        raise ValueError("Falha no login. Verifique as suas credenciais.")
    token = None
    try:
        dados = resposta.json()
        if isinstance(dados, dict):
            token = dados.get("token")
        elif isinstance(dados, list) and dados:
            token = dados[0].get("token")
    except Exception:
        pass
    if not token:
        token = resposta.headers.get("token")
    if token:
        sessao.headers.update({"token": token})


def buscar_publicacoes_bg(sessao: requests.Session, nome_busca: str, data_inicial, data_final) -> List[Dict]:
    params = {
        "de": data_inicial.strftime("%Y-%m-%dT03:00:00.000Z"),
        "ate": data_final.strftime("%Y-%m-%dT03:00:00.000Z"),
        "tipo": "buscaExata",
        "conteudo": nome_busca,
    }
    resposta = sessao.get(BUSCA_BG_URL, params=params, timeout=REQUEST_TIMEOUT)
    if resposta.status_code != 200:
        raise ValueError("Erro ao pesquisar boletins. Verifique se o sistema está online.")
    dados = resposta.json()
    if isinstance(dados, list):
        return dados
    return dados.get("content", dados.get("data", []))


def baixar_pdf_bg(sessao: requests.Session, upload_id: str) -> bytes:
    resposta = sessao.get(f"{DOWNLOAD_BG_URL}{upload_id}", timeout=REQUEST_TIMEOUT)
    if resposta.status_code != 200:
        raise ValueError(f"Não foi possível baixar o PDF do upload {upload_id}.")
    return resposta.content


# ==========================================
# GERAÇÃO DO ZIP COM LOTES DE 30 (ORDENADO POR DATA)
# ==========================================
def gerar_zip_unificado(
    nome_busca: str,
    doems_resultados: List[Dict],
    bg_resultados: List[Dict],
    modo: str,
) -> bytes:
    """
    Agrupa todos os resultados (DOEMS e/ou BG) em uma lista unificada,
    ordena por data e fatia em lotes de até 30 publicações por arquivo TXT dentro do ZIP.
    """
    registros_unificados = []

    # Registros do DOEMS
    for ato in doems_resultados:
        registros_unificados.append({
            "fonte": "DOEMS",
            "data_str": ato.get("do_data", ""),
            "data_ord": data_para_ordenacao(ato.get("do_data", "")),
            "numero": ato.get("do_numero", ""),
            "ato": ato,
        })

    # Registros dos Boletins
    for bg in bg_resultados:
        data_doc = bg.get("data_documento", "")
        for nota in bg.get("resultados", []):
            registros_unificados.append({
                "fonte": "BG",
                "data_str": data_doc,
                "data_ord": data_para_ordenacao(data_doc),
                "numero": bg.get("numero_bg", ""),
                "nota": nota,
            })

    # Ordena por data crescente
    registros_unificados.sort(key=lambda x: x["data_ord"])

    TAMANHO_LOTE = 30
    lotes = [registros_unificados[i: i + TAMANHO_LOTE] for i in range(0, len(registros_unificados), TAMANHO_LOTE)]

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for idx_lote, lote in enumerate(lotes, 1):
            linhas = [
                f"RELATÓRIO DE MONITORAMENTO: {nome_busca.upper()} — PARTE {idx_lote}",
                "=" * 60,
                f"Gerado em: {datetime.now().strftime('%d/%m/%Y às %H:%M:%S')}",
                f"Fonte(s): {modo}",
                "=" * 60,
                "",
            ]

            for i, reg in enumerate(lote, 1):
                num_geral = (idx_lote - 1) * TAMANHO_LOTE + i

                if reg["fonte"] == "DOEMS":
                    ato = reg["ato"]
                    linhas.append(f"=== RESULTADO {num_geral} | FONTE: DOEMS | Edição {reg['numero']} ({reg['data_str']}) ===")
                    linhas.append(f"[CABEÇALHO]\n{ato['cabecalho']}\n")
                    if ato["tipo"] == "direto":
                        linhas.append(f"[TEXTO COMPLETO]\n{ato['texto_integral']}\n")
                    else:
                        linhas.append(f"[CONTEXTO]\n{ato['contexto']}\n")
                        linhas.append(f"[DADOS TABELA]\n{ato['linha_dados']}\n")
                else:
                    nota = reg["nota"]
                    linhas.append(f"=== RESULTADO {num_geral} | FONTE: BG CBMMS | BG N. {reg['numero']} ({reg['data_str']}) ===")
                    if nota.get("setor"):
                        linhas.append(f"[SETOR]\n{nota['setor']}\n")
                    linhas.append(f"[ASSUNTO]\n{nota.get('cabecalho', '')}\n")
                    linhas.append(f"[NOTA N. {nota['nota']}]\n{nota['texto_completo']}\n")

                linhas.append("")

            txt_conteudo = "\n".join(linhas).encode("utf-8")
            nome_arquivo = f"relatorio_{nome_busca.replace(' ', '_')}_parte_{idx_lote:02d}.txt"
            zip_file.writestr(nome_arquivo, txt_conteudo)

    return zip_buffer.getvalue()


# ==========================================
# INTERFACE PRINCIPAL
# ==========================================
st.title("🔍 Radar Inteligente — DOEMS & BG CBMMS")
st.markdown("Pesquise um nome no **Diário Oficial do MS** e/ou nos **Boletins Gerais do CBMMS** e exporte relatórios otimizados para IA.")
st.divider()

# --- SELEÇÃO DO MODO DE PESQUISA ---
st.subheader("1. Selecione a fonte de pesquisa")
modo_pesquisa = st.radio(
    "Onde deseja buscar?",
    options=["📰 Somente DOEMS", "🚒 Somente Boletins (BG)", "🔁 DOEMS + Boletins"],
    horizontal=True,
    label_visibility="collapsed",
)

buscar_doems = "DOEMS" in modo_pesquisa
buscar_bg = "Boletins" in modo_pesquisa or "DOEMS +" in modo_pesquisa

# Normaliza label para exibição no ZIP
if buscar_doems and buscar_bg:
    label_modo = "DOEMS + BG CBMMS"
elif buscar_doems:
    label_modo = "DOEMS"
else:
    label_modo = "BG CBMMS"

st.divider()

# --- PARÂMETROS COMUNS ---
st.subheader("2. Parâmetros da Pesquisa")
nome_busca = st.text_input("Nome completo para pesquisar:", placeholder="Ex: Geraldo Roberto Dias")

hoje = date.today()
try:
    oito_anos_atras = hoje.replace(year=hoje.year - 8)
except ValueError:
    oito_anos_atras = hoje.replace(year=hoje.year - 8, day=28)

data_limite_bg = date(2018, 7, 17)
data_inicial_default = data_limite_bg if buscar_bg else oito_anos_atras

col1, col2 = st.columns(2)
with col1:
    data_inicial = st.date_input("Data Inicial", value=data_inicial_default, format="DD/MM/YYYY",
                                  min_value=data_limite_bg if buscar_bg else None,
                                  help="Para Boletins: busca disponível a partir de 17/07/2018." if buscar_bg else None)
with col2:
    data_final = st.date_input("Data Final", value=hoje, format="DD/MM/YYYY")

if data_inicial > data_final:
    st.error("⚠️ A Data Inicial não pode ser posterior à Data Final.")
    datas_validas = False
else:
    datas_validas = True

# --- CREDENCIAIS (somente para Boletins) ---
usuario_final = None
senha_final = None

if buscar_bg:
    st.divider()
    st.subheader("3. Credenciais de Acesso — CBMMS")
    st.info("🛡️ As credenciais comunicam diretamente com os servidores do CBMMS. Nenhuma senha é salva nesta aplicação.")

    if ATIVAR_MODO_TESTE:
        with st.expander("🔑 Possui um Código de Convite?"):
            st.write("Insira o código para testar sem utilizar o seu login pessoal.")
            codigo_convite = st.text_input("Código de Convite", type="password", key="cod_convite")
            colA, colB = st.columns([1, 4])
            with colA:
                if st.button("Validar Código"):
                    try:
                        if codigo_convite == st.secrets["senhaAPP"]:
                            st.session_state.modo_teste_ativo = True
                            st.success("Acesso de teste ativado!")
                        else:
                            st.error("Código inválido.")
                    except Exception:
                        st.error("Erro ao validar o código nos Secrets.")
            with colB:
                if st.session_state.modo_teste_ativo:
                    if st.button("Sair do Modo de Teste"):
                        st.session_state.modo_teste_ativo = False
                        st.rerun()

    bloquear_campos = st.session_state.modo_teste_ativo
    if bloquear_campos:
        st.success("✅ Utilizando credenciais de teste. Não é necessário preencher o CPF.")
    else:
        st.caption("Insira seu CPF com ou sem pontuação.")

    usuario_input = st.text_input("Login (CPF)", disabled=bloquear_campos, key="cpf_input")
    senha_input = st.text_input("Senha", type="password", disabled=bloquear_campos, key="senha_input")

st.divider()

# --- BOTÃO DE BUSCA ---
btn_label = f"🔎 Buscar em {label_modo}"
btn_buscar = st.button(btn_label, type="primary", disabled=not datas_validas)

# ==========================================
# LÓGICA PRINCIPAL
# ==========================================
if btn_buscar:
    if not nome_busca:
        st.warning("Insira um nome válido para pesquisar.")
        st.stop()

    # Valida credenciais para Boletins
    if buscar_bg:
        if st.session_state.modo_teste_ativo:
            usuario_final = st.secrets["userteste"]
            senha_final = st.secrets["senhateste"]
        else:
            usuario_final = formatar_cpf(usuario_input)
            senha_final = senha_input
        if not usuario_final or not senha_final:
            st.warning("Preencha o CPF e a senha para buscar nos Boletins.")
            st.stop()

    # Reinicia estado
    st.session_state.busca_concluida = False
    st.session_state.bgs_encontrados = []
    st.session_state.doems_encontrados = []
    st.session_state.nome_pesquisado = nome_busca
    st.session_state.modo_pesquisa = label_modo
    st.session_state.falhas_processamento = []
    st.session_state.tempo_pesquisa_segundos = None
    st.session_state.tempo_processamento_segundos = None

    data_ini_str = data_inicial.strftime("%Y-%m-%d")
    data_fim_str = data_final.strftime("%Y-%m-%d")

    # ======= BUSCA DOEMS =======
    if buscar_doems:
        with st.status("🗞️ Consultando o Diário Oficial do MS (DOEMS)...", expanded=True) as status_doems:
            try:
                termo_url = urllib.parse.quote_plus(nome_busca.strip())
                api_url = (
                    f"https://www.diariooficial.ms.gov.br/api/diarios/busca-diarios"
                    f"?tipo=1&texto={termo_url}&dataFinal={data_fim_str}&dataInicial={data_ini_str}&registrosPorPagina=500"
                )
                response = requests.get(api_url, timeout=15)
                response.raise_for_status()
                dados_api = response.json()
            except Exception as e:
                status_doems.update(label="Erro ao conectar na API do DOEMS.", state="error", expanded=True)
                st.error(str(e))
                st.stop()

            paginas = dados_api.get("paginasDiario", [])
            total_registros = dados_api.get("totalDeRegistros", 0)

            if total_registros == 0 or not paginas:
                status_doems.update(label="Nenhum registro encontrado no DOEMS.", state="complete", expanded=False)
                st.info(f"'{nome_busca}' não encontrado no DOEMS no período selecionado.")
            else:
                arquivos_unicos = {}
                for item in paginas:
                    link = item["caminhoArquivo"]
                    if link not in arquivos_unicos:
                        arquivos_unicos[link] = {"numero": item["numero"], "data": item["dataPublicacao"], "descricao": item["descricao"]}

                st.write(f"📊 DOEMS: **{total_registros} ocorrências** em **{len(arquivos_unicos)} edições**.")
                progress_bar = st.progress(0)
                contador = 0
                total_arquivos = len(arquivos_unicos)
                relatorio_doems = []

                for link_pdf, metadados in arquivos_unicos.items():
                    contador += 1
                    progress_bar.progress(contador / total_arquivos, text=f"Processando {contador}/{total_arquivos}: Edição {metadados['numero']}")
                    try:
                        pdf_resp = requests.get(link_pdf, timeout=30)
                        if pdf_resp.status_code == 200:
                            texto_completo, _ = extrair_texto_pdf_doems(pdf_resp.content)
                            if texto_completo:
                                atos = processar_publicacao_doems(texto_completo, nome_busca)
                                for ato in atos:
                                    ato["do_numero"] = metadados["numero"]
                                    ato["do_data"] = formatar_data_iso(metadados["data"])
                                    ato["do_desc"] = metadados["descricao"]
                                    relatorio_doems.append(ato)
                    except Exception as e:
                        st.session_state.falhas_processamento.append(f"DOEMS Edição {metadados['numero']}: {e}")

                progress_bar.empty()
                st.session_state.doems_encontrados = relatorio_doems
                status_doems.update(label=f"DOEMS: {len(relatorio_doems)} atos extraídos!", state="complete", expanded=False)

    # ======= BUSCA BOLETINS =======
    if buscar_bg:
        with st.status("🚒 Consultando Boletins Gerais (CBMMS)...", expanded=True) as status_bg:
            sessao = requests.Session()
            sessao.headers.update({"Content-Type": "application/json"})
            try:
                login_exibido = "CONTA_DE_TESTE" if st.session_state.modo_teste_ativo else usuario_final
                st.write(f"🔐 Conectando ao CBMMS como: {login_exibido}")
                autenticar(sessao, usuario_final, senha_final)
                st.write("✅ Autenticação realizada com sucesso!")

                inicio_pesquisa = time.perf_counter()
                lista_pubs = buscar_publicacoes_bg(sessao, nome_busca, data_inicial, data_final)
                st.session_state.tempo_pesquisa_segundos = time.perf_counter() - inicio_pesquisa

                if not lista_pubs:
                    status_bg.update(label="Nenhum boletim encontrado.", state="complete", expanded=False)
                    st.info(f"'{nome_busca}' não encontrado nos Boletins no período selecionado.")
                else:
                    st.write(f"📥 {len(lista_pubs)} boletim(ns) retornados pela API.")
                    barra = st.progress(0)
                    texto_prog = st.empty()
                    bgs_com_resultados = []

                    inicio_proc = time.perf_counter()
                    for i, pub in enumerate(lista_pubs, start=1):
                        if not isinstance(pub, dict):
                            barra.progress(i / len(lista_pubs))
                            continue
                        upload_id = pub.get("upload", {}).get("id") if isinstance(pub.get("upload"), dict) else pub.get("uploadId")
                        num_bg = pub.get("numeroDaPublicacao", "S/N")
                        texto_prog.text(f"🔍 Processando BG N. {num_bg} ({i}/{len(lista_pubs)})...")

                        if not upload_id:
                            st.session_state.falhas_processamento.append(f"BG {num_bg}: sem upload_id.")
                            barra.progress(i / len(lista_pubs))
                            continue

                        try:
                            pdf_bytes = baixar_pdf_bg(sessao, upload_id)
                            data_doc = extrair_data_documento_pdf(pdf_bytes)
                            notas = extrair_notas_do_militar(pdf_bytes, nome_busca)
                            if notas:
                                bgs_com_resultados.append({"numero_bg": num_bg, "data_documento": data_doc, "resultados": notas})
                        except Exception as e:
                            st.session_state.falhas_processamento.append(f"BG {num_bg}: {e}")

                        barra.progress(i / len(lista_pubs))

                    st.session_state.tempo_processamento_segundos = time.perf_counter() - inicio_proc
                    barra.empty()
                    texto_prog.text("✅ Processamento concluído!")
                    st.session_state.bgs_encontrados = bgs_com_resultados
                    status_bg.update(label=f"Boletins: {sum(len(b['resultados']) for b in bgs_com_resultados)} nota(s) extraídas!", state="complete", expanded=False)

            except ValueError as e:
                status_bg.update(label="Erro no processo.", state="error", expanded=True)
                st.error(str(e))
            except requests.ConnectTimeout:
                status_bg.update(label="Tempo de conexão excedido.", state="error", expanded=True)
                st.error("O servidor demorou demais para responder.")
            except requests.ConnectionError as e:
                status_bg.update(label="Erro de conexão.", state="error", expanded=True)
                st.error(f"Erro de conexão: {e}")
            except Exception as e:
                status_bg.update(label="Erro interno.", state="error", expanded=True)
                st.error(f"Erro interno: {e}")

    st.session_state.busca_concluida = True

# ==========================================
# EXIBIÇÃO DOS RESULTADOS
# ==========================================
if st.session_state.busca_concluida:
    st.divider()

    doems_resultados = st.session_state.doems_encontrados
    bg_resultados = st.session_state.bgs_encontrados
    nome_pesquisado = st.session_state.nome_pesquisado

    total_doems = len(doems_resultados)
    total_bg = sum(len(b["resultados"]) for b in bg_resultados)
    total_geral = total_doems + total_bg

    if total_geral == 0:
        st.warning("Nenhum ato/nota foi extraído no período e fontes selecionadas.")
        if st.session_state.falhas_processamento:
            with st.expander("⚠️ Ocorrências durante o processamento"):
                for falha in st.session_state.falhas_processamento:
                    st.warning(falha)
        st.stop()

    # Métricas de tempo
    col_t1, col_t2, col_t3 = st.columns(3)
    with col_t1:
        st.metric("Atos DOEMS", total_doems)
    with col_t2:
        st.metric("Notas BG", total_bg)
    with col_t3:
        st.metric("Total", total_geral)

    if st.session_state.tempo_pesquisa_segundos is not None:
        st.caption(f"⏱️ Pesquisa: {formatar_duracao(st.session_state.tempo_pesquisa_segundos)} | Processamento: {formatar_duracao(st.session_state.tempo_processamento_segundos)}")

    st.success(f"✅ Extração finalizada! **{total_geral} resultado(s)** encontrado(s) para **{nome_pesquisado}**.")

    # Botão de Download ZIP
    zip_bytes = gerar_zip_unificado(
        nome_pesquisado,
        doems_resultados,
        bg_resultados,
        st.session_state.modo_pesquisa,
    )
    total_lotes = -(-total_geral // 30)  # ceiling division
    st.download_button(
        label=f"📥 Baixar Relatório Completo (.ZIP — {total_lotes} arquivo(s), ordenado por data)",
        data=zip_bytes,
        file_name=f"radar_{nome_pesquisado.replace(' ', '_')}.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )

    st.divider()

    # --- Resultados DOEMS ---
    if doems_resultados:
        st.subheader("📰 Resultados — Diário Oficial (DOEMS)")
        for ato in doems_resultados:
            titulo = f"📖 DOEMS nº {ato['do_numero']} ({ato['do_data']}) — {ato['cabecalho'][:50]}..."
            with st.expander(titulo, expanded=False):
                st.markdown(f"**Data de Publicação:** {ato['do_data']}")
                st.info(ato["cabecalho"])
                if ato["tipo"] == "direto":
                    st.markdown("**[TEXTO COMPLETO]**")
                    st.write(ato["texto_integral"])
                else:
                    st.markdown("**[CONTEXTO DA AÇÃO]**")
                    st.write(ato["contexto"])
                    st.markdown("**[DADOS DA TABELA]**")
                    st.code(ato["linha_dados"], language="text")

    # --- Resultados Boletins ---
    if bg_resultados:
        st.subheader("🚒 Resultados — Boletins Gerais (BG CBMMS)")
        for bg in bg_resultados:
            data_doc = bg.get("data_documento", "Data não identificada")
            st.markdown(f"**📄 BG N. {bg['numero_bg']} — {data_doc}**")
            for res in bg["resultados"]:
                titulo_exp = f"📌 NOTA N. {res['nota']}"
                if res.get("cabecalho"):
                    titulo_exp += f" — {res['cabecalho']}"
                with st.expander(titulo_exp, expanded=False):
                    st.markdown(f"**Data:** {data_doc}")
                    if res.get("setor"):
                        st.markdown(f"**Setor:** {res['setor']}")
                    if res.get("cabecalho"):
                        st.markdown(f"**Assunto:** {res['cabecalho']}")
                    st.code(res["texto_completo"], language=None)

    # --- Falhas ---
    if st.session_state.falhas_processamento:
        with st.expander("⚠️ Ocorrências durante o processamento"):
            for falha in st.session_state.falhas_processamento:
                st.warning(falha)
