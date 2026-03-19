"""
Gerador de Audiobook com Voz Humanizada (PT-BR)
================================================
Dependências: pip install edge-tts pdfplumber

SEM ffmpeg, SEM pydub.
- Anuncia o número da página antes de cada uma
- Suporta PDFs de 1 ou 2 colunas automaticamente

Uso:
  python audiobook_generator.py arquivo.pdf
  python audiobook_generator.py arquivo.pdf --saida meu_livro.mp3
  python audiobook_generator.py arquivo.pdf --voz pt-BR-AntonioNeural
  python audiobook_generator.py arquivo.pdf --sem-pagina
"""

import edge_tts
import asyncio
import pdfplumber
import re
import os
import sys
import argparse
from pathlib import Path

# ──────────────────────────────────────────────
# CONFIGURAÇÕES
# ──────────────────────────────────────────────
VOZ_PADRAO = "pt-BR-FranciscaNeural"
CHUNK_SIZE  = 3000


# ──────────────────────────────────────────────
# EXTRAÇÃO DO PDF — anuncia página, suporta 2 colunas
# ──────────────────────────────────────────────
def extrair_texto_pagina(page, num_pagina: int, anunciar_pagina: bool, offset_pagina: int) -> str:
    """
    Extrai o texto da página com detecção automática de 1 ou 2 colunas.
    Prefixa o conteúdo com "Página N." para orientar o ouvinte.
    """
    words = page.extract_words()
    if not words:
        return ""

    # Detecta 2 colunas pelo histograma horizontal de palavras
    largura = page.width
    faixas  = [0] * 10
    for w in words:
        faixa = min(int((w["x0"] / largura) * 10), 9)
        faixas[faixa] += 1

    total          = sum(faixas)
    meio_esq       = sum(faixas[3:5])
    lados          = sum(faixas[0:3]) + sum(faixas[5:8])
    proporcao_meio = meio_esq / total if total else 1

    if proporcao_meio < 0.08 and lados > total * 0.5:
        meio      = largura / 2
        texto_esq = page.within_bbox((0,    0, meio,    page.height)).extract_text() or ""
        texto_dir = page.within_bbox((meio, 0, largura, page.height)).extract_text() or ""
        texto     = texto_esq + "\n\n" + texto_dir
    else:
        texto = page.extract_text() or ""

    if not texto.strip():
        return ""

    # Prefixa com anúncio de página
    prefixo = f"Página {num_pagina + offset_pagina - 1}.\n" if anunciar_pagina else ""
    return prefixo + texto


def extrair_texto_pdf(pdf_path: str, anunciar_pagina: bool, offset_pagina: int = 0) -> str:
    partes = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"📖 PDF carregado: {total} página(s)")

        for i, page in enumerate(pdf.pages, 1):
            try:
                texto = extrair_texto_pagina(page, i, anunciar_pagina, offset_pagina)
                if texto.strip():
                    partes.append(texto)
                else:
                    print(f"   ⚠️  Página {i}/{total} sem texto extraível — ignorada")
            except Exception as e:
                print(f"   ❌ Erro na página {i}: {e} — ignorada")

    return "\n\n".join(partes)


# ──────────────────────────────────────────────
# LIMPEZA DO TEXTO
# ──────────────────────────────────────────────
def limpar_texto(texto: str) -> str:
    # Une palavras hifenizadas quebradas no final da linha
    texto = re.sub(r"-\s*\n\s*", "", texto)

    # Remove rodapés/cabeçalhos numéricos soltos
    texto = re.sub(r"(?m)^[-–—]?\s*\d+\s*[-–—]?\s*$", "", texto)

    # Preserva "Página N." — não remove essa linha
    # Remove outras ocorrências de "Página N" sem ponto (rodapé do PDF)
    texto = re.sub(r"(?im)^página\s+\d+\s*$", "", texto)

    # Parágrafo → pausa natural
    texto = re.sub(r"\n{2,}", ". ", texto)

    # Quebra de linha simples → espaço (evita pausas artificiais no TTS)
    texto = re.sub(r"\n", " ", texto)

    # Espaços duplicados
    texto = re.sub(r"[ \t]{2,}", " ", texto)

    # Caracteres não imprimíveis
    texto = re.sub(r"[^\x20-\x7E\xA0-\xFF]", " ", texto)

    return texto.strip()


# ──────────────────────────────────────────────
# DIVISÃO EM CHUNKS (sempre em pontuação)
# ──────────────────────────────────────────────
def dividir_em_chunks(texto: str, tamanho: int = CHUNK_SIZE) -> list[str]:
    chunks = []
    while len(texto) > tamanho:
        corte = tamanho
        for sep in [". ", "! ", "? ", " "]:
            pos = texto.rfind(sep, 0, tamanho)
            if pos != -1:
                corte = pos + len(sep)
                break
        chunks.append(texto[:corte].strip())
        texto = texto[corte:]
    if texto.strip():
        chunks.append(texto.strip())
    return chunks


# ──────────────────────────────────────────────
# SÍNTESE VIA STREAMING
# ──────────────────────────────────────────────
async def sintetizar_para_arquivo(chunks: list[str], mp3_saida: str, voz: str):
    total = len(chunks)
    with open(mp3_saida, "wb") as arquivo_saida:
        for i, chunk in enumerate(chunks, 1):
            print(f"   🔊 Chunk {i}/{total} ({len(chunk)} chars)...")
            for tentativa in range(1, 4):
                try:
                    communicate = edge_tts.Communicate(chunk, voz)
                    async for bloco in communicate.stream():
                        if bloco["type"] == "audio":
                            arquivo_saida.write(bloco["data"])
                    break
                except Exception as e:
                    if tentativa < 3:
                        print(f"      ⚠️  Tentativa {tentativa} falhou: {e}. Retentando...")
                        await asyncio.sleep(2)
                    else:
                        print(f"      ❌ Chunk {i} falhou: {e}")


# ──────────────────────────────────────────────
# FLUXO PRINCIPAL
# ──────────────────────────────────────────────
async def gerar_audiobook(
    pdf_path: str,
    mp3_saida: str = "audiobook_completo.mp3",
    voz: str = VOZ_PADRAO,
    offset_pagina: int = 1,
    anunciar_pagina: bool = True,
):
    print("\n📄 Extraindo texto do PDF...")
    texto_bruto = extrair_texto_pdf(pdf_path, anunciar_pagina, offset_pagina)
    texto = limpar_texto(texto_bruto)

    if not texto:
        print("❌ Nenhum texto extraível. O PDF pode ser baseado em imagens.")
        print("   Dica: use 'ocrmypdf' para converter antes.")
        return

    print(f"✅ Texto extraído: {len(texto):,} caracteres")

    chunks = dividir_em_chunks(texto)
    print(f"\n🔧 Dividido em {len(chunks)} chunk(s)")
    print(f"🎙️  Voz: {voz}")
    print(f"📢 Anúncio de página: {'sim' if anunciar_pagina else 'não'}")
    print(f"💾 Saída: {mp3_saida}\n")

    await sintetizar_para_arquivo(chunks, mp3_saida, voz,)

    if os.path.exists(mp3_saida) and os.path.getsize(mp3_saida) > 0:
        tamanho_mb = os.path.getsize(mp3_saida) / (1024 * 1024)
        print(f"\n✅ Audiobook salvo: {mp3_saida} ({tamanho_mb:.1f} MB)")
    else:
        print("\n❌ Arquivo vazio ou não criado. Verifique os erros acima.")


# ──────────────────────────────────────────────
# PONTO DE ENTRADA
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Converte PDF em audiobook MP3 com anúncio de página"
    )
    parser.add_argument("pdf", help="Caminho para o arquivo PDF")
    parser.add_argument("--saida", default="audiobook_completo.mp3", help="Nome do MP3 de saída")
    parser.add_argument("--offset", type=int, default=1, help="Número da página para começar (padrão: 1)")
    parser.add_argument(
        "--voz",
        default=VOZ_PADRAO,
        help=(
            "Voz PT-BR:\n"
            "  pt-BR-FranciscaNeural  feminina, expressiva (padrão)\n"
            "  pt-BR-AntonioNeural    masculina\n"
            "  pt-BR-AnaNeural        feminina neutra"
        ),
    )
    parser.add_argument(
        "--sem-pagina",
        action="store_true",
        help="Desativa o anúncio de número de página",
    )

    args = parser.parse_args()

    if not Path(args.pdf).exists():
        print(f"❌ Arquivo não encontrado: {args.pdf}")
        sys.exit(1)

    asyncio.run(gerar_audiobook(
        args.pdf,
        args.saida,
        args.voz,
        args.offset,
        anunciar_pagina=not args.sem_pagina,
    ))