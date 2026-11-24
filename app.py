"""
Atômica Print Lab · Criar Bottons (Streamlit) v3.43.0

Guias (do centro para fora):
    Segurança  → textos / informações importantes devem ficar dentro dela
    Arte       → área visível do botton
    Dobra      → onde a matriz dobra o papel
    Corte      → limite externo do papel (sangria)

Para adicionar outros tamanhos de botton, basta incluir uma entrada em BUTTONS.

Executar:  streamlit run app.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, replace
from typing import Literal

import streamlit as st
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageOps

from reportlab.lib.colors import HexColor, black
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import branding as brand

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonSpec:
    """Tamanho nominal e medidas das guias, em milímetros."""

    size: float
    cut: float
    fold: float
    art: float
    safe: float
    shape: Literal["circle", "square"] = "circle"


@dataclass(frozen=True)
class Guide:
    key: str
    label: str
    attr: str  # atributo correspondente em ButtonSpec
    color: str  # cor da guia
    md_color: str  # cor equivalente no markdown do Streamlit
    style: str  # "dashed" | "dotted" | "solid"


# Medidas transcritas das referências enviadas. O preset de 44 mm foi
# atualizado para a referência mais recente (corte 56 / dobra 49 / arte 44 /
# segurança 40), evitando duas opções com o mesmo tamanho nominal.
# A ordenação é numérica, pelo tamanho nominal do botton.
BUTTONS: dict[str, ButtonSpec] = dict(sorted({
    "18 mm": ButtonSpec(size=18, cut=29, fold=24, art=18, safe=12),
    "26 mm": ButtonSpec(size=26, cut=37, fold=31, art=26, safe=24),
    "32 mm": ButtonSpec(size=32, cut=44, fold=41, art=32, safe=30),
    "33 mm": ButtonSpec(size=33, cut=44, fold=41, art=33, safe=30),
    "35 mm": ButtonSpec(size=35, cut=48, fold=41, art=35, safe=32),
    "37 mm": ButtonSpec(size=37, cut=46, fold=41, art=37, safe=34),
    "38 mm": ButtonSpec(size=38, cut=48, fold=44, art=38, safe=34),
    "44 mm": ButtonSpec(size=44, cut=54, fold=49, art=44, safe=40),
    "45 mm": ButtonSpec(size=45, cut=55, fold=51, art=45, safe=41),
    "55 mm": ButtonSpec(size=55, cut=69, fold=61, art=55, safe=49),
    "50 × 50 mm (quadrado)": ButtonSpec(
        size=50, cut=61, fold=58, art=50, safe=47, shape="square"
    ),
    "58 mm": ButtonSpec(size=58, cut=70, fold=64, art=58, safe=54),
    "65 mm": ButtonSpec(size=65, cut=75, fold=71, art=65, safe=61),
    "88 mm": ButtonSpec(size=88, cut=98, fold=94, art=88, safe=84),
}.items(), key=lambda item: item[1].size))


def _cut_diameter_widget_key(size_key: str) -> str:
    """Chave independente para preservar o corte personalizado de cada tamanho."""
    return f"custom_cut_diameter_mm::{size_key}"


def _clamp_cut_diameter(value, base_spec: ButtonSpec) -> float:
    """Limita o corte entre a guia da Dobra e 5 mm acima do corte padrão."""
    try:
        diameter = float(value)
    except (TypeError, ValueError):
        diameter = float(base_spec.cut)
    if not math.isfinite(diameter):
        diameter = float(base_spec.cut)
    diameter = max(float(base_spec.fold), min(float(base_spec.cut) + 5.0, diameter))
    return round(diameter, 2)


def _save_cut_diameter_for_size(size_key: str, widget_key: str) -> None:
    """Guarda o ajuste do corte sem alterar as demais guias ou perfis."""
    base_spec = BUTTONS.get(str(size_key))
    if base_spec is None:
        return
    diameter = _clamp_cut_diameter(st.session_state.get(widget_key), base_spec)
    st.session_state[widget_key] = diameter
    overrides = st.session_state.setdefault("_cut_diameter_overrides", {})
    overrides[str(size_key)] = diameter

# Ordem: da mais externa para a mais interna
GUIDES: tuple[Guide, ...] = (
    Guide("cut", "Corte", "cut", "#E5457B", "red", "dashed"),
    Guide("fold", "Dobra", "fold", "#FFFF00", "yellow", "dashed"),
    Guide("art", "Arte", "art", "#2F9BD8", "blue", "solid"),
    Guide("safe", "Segurança", "safe", "#00F919", "white", "dashed"),
)

PAPERS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "Carta (Letter)": (215.9, 279.4),
}

MAX_SOURCE_PX = 4000  # lado maior da imagem usada (evita PDFs gigantes)
PDF_MAX_DPI = 600

# Valores neutros do painel de correção; cada arte mantém seu próprio estado.
IMAGE_ADJUSTMENT_DEFAULTS = {
    "white_balance_temperature": 0,  # negativo = frio; positivo = quente
    "white_balance_tint": 0,          # negativo = verde; positivo = magenta
    "exposure": 0.0,                 # EV
    "contrast": 100,                 # percentual
    "saturation": 100,               # percentual
    "hue": 0,                        # graus
    "sharpness": 100,                # percentual
    "remove_background": False,
}
IMAGE_ADJUSTMENT_FIELDS = tuple(IMAGE_ADJUSTMENT_DEFAULTS)

# Ajustes em lote: operações independentes, definidas diretamente no painel.
# Os pares enquadramento e preenchimento são mutuamente exclusivos por natureza;
# as demais operações podem ser combinadas livremente.
BATCH_FRAMING_OPTIONS = {
    "cut": "Preencher até o corte",
    "art": "Encaixar na área da arte",
}
BATCH_FILL_OPTIONS = {
    "blur": "Aplicar desfoque",
    "color": "Aplicar cor sólida",
}
BATCH_BLUR_DEFAULT = 0.5
BATCH_BACKGROUND_DEFAULT = "#FFFFFF"


def _batch_deactivate_custom_background(state) -> None:
    """Desativa a imagem de fundo do lote e invalida o uploader anterior."""
    was_active = bool(state.get("batch_apply_custom_background", False))
    state["batch_apply_custom_background"] = False
    if was_active:
        state["batch_custom_background_upload_revision"] = int(
            state.get("batch_custom_background_upload_revision", 0) or 0
        ) + 1


def _apply_batch_priority_rules(state, active_key: str) -> None:
    """Resolve opções incompatíveis conforme a última escolha do usuário.

    Regras:
    - Preencher até o corte não admite desfoque, cor-base nem fundo personalizado.
    - Desfoque, cor sólida e fundo personalizado são alternativas entre si.
    - Qualquer preenchimento/fundo exige que a arte seja recortada pela guia
      Arte; o editor ativa esse recorte por arte, preservando enquadramentos livres.
    - Encaixar na área da arte pode coexistir com qualquer um dos três fundos.
    - Desmarcar os dois presets equivale ao enquadramento livre/manual.
    """
    active = bool(state.get(active_key, False))

    if active_key == "batch_apply_framing_cut" and active:
        state["batch_apply_framing_art"] = False
        state["batch_apply_blur"] = False
        state["batch_apply_solid_color"] = False
        _batch_deactivate_custom_background(state)
        return

    if active_key == "batch_apply_framing_art":
        if active:
            state["batch_apply_framing_cut"] = False
        # Desmarcar os dois presets representa enquadramento livre/manual.
        # Como na edição individual, isso NÃO desativa preenchimentos ativos.
        return

    if active_key == "batch_apply_blur" and active:
        state["batch_apply_solid_color"] = False
        _batch_deactivate_custom_background(state)
        state["batch_apply_framing_cut"] = False
        # A ausência de preset explícito equivale ao enquadramento livre. Cada
        # arte selecionada será migrada para a guia Arte preservando seu modo manual.
        return

    if active_key == "batch_apply_solid_color" and active:
        state["batch_apply_blur"] = False
        _batch_deactivate_custom_background(state)
        state["batch_apply_framing_cut"] = False
        return

    if active_key == "batch_apply_custom_background" and active:
        state["batch_apply_blur"] = False
        state["batch_apply_solid_color"] = False
        state["batch_apply_framing_cut"] = False
        return


def _normalise_batch_priority_state(state) -> None:
    """Migra combinações antigas para um conjunto coerente de opções."""
    if bool(state.get("batch_apply_framing_cut", False)):
        _apply_batch_priority_rules(state, "batch_apply_framing_cut")
    elif bool(state.get("batch_apply_custom_background", False)):
        _apply_batch_priority_rules(state, "batch_apply_custom_background")
    elif bool(state.get("batch_apply_blur", False)):
        _apply_batch_priority_rules(state, "batch_apply_blur")
    elif bool(state.get("batch_apply_solid_color", False)):
        _apply_batch_priority_rules(state, "batch_apply_solid_color")
    elif bool(state.get("batch_apply_framing_art", False)):
        _apply_batch_priority_rules(state, "batch_apply_framing_art")


def _batch_framing_cut_changed() -> None:
    _apply_batch_priority_rules(st.session_state, "batch_apply_framing_cut")


def _batch_framing_art_changed() -> None:
    _apply_batch_priority_rules(st.session_state, "batch_apply_framing_art")


def _batch_fill_blur_changed() -> None:
    _apply_batch_priority_rules(st.session_state, "batch_apply_blur")


def _batch_fill_color_changed() -> None:
    _apply_batch_priority_rules(st.session_state, "batch_apply_solid_color")


def _batch_custom_background_checkbox_changed() -> None:
    """Aplica prioridade do fundo personalizado ou limpa o uploader desativado."""
    _apply_batch_priority_rules(st.session_state, "batch_apply_custom_background")
    if not bool(st.session_state.get("batch_apply_custom_background", False)):
        # A revisão muda também quando a desativação veio de uma regra de
        # prioridade, garantindo que o próximo upload comece vazio.
        st.session_state["batch_custom_background_upload_revision"] = int(
            st.session_state.get("batch_custom_background_upload_revision", 0) or 0
        ) + 1

# ----------------------------------------------------------------------------
# Geometria
# ----------------------------------------------------------------------------


def image_box(img_w: int, img_h: int, cover_d: float, zoom: float, off_x: float, off_y: float):
    """
    Retângulo da imagem em mm, relativo ao centro do botton (eixo y para cima).
    Com zoom = 1 a imagem "cobre" exatamente o círculo de corte (cover).
    Retorna (x, y, largura, altura), onde (x, y) é o canto inferior esquerdo.
    """
    scale = cover_d / min(img_w, img_h) * zoom
    w, h = img_w * scale, img_h * scale
    return off_x - w / 2, off_y - h / 2, w, h


def fit_image_box(img_w: int, img_h: int, fit_d: float, zoom: float, off_x: float, off_y: float):
    """Retângulo em mm que contém a imagem inteira dentro da área indicada."""
    scale = fit_d / max(img_w, img_h) * zoom
    w, h = img_w * scale, img_h * scale
    return off_x - w / 2, off_y - h / 2, w, h


def covers_circle(box, diameter: float) -> bool:
    x, y, w, h = box
    r = diameter / 2
    return x <= -r + 1e-6 and y <= -r + 1e-6 and x + w >= r - 1e-6 and y + h >= r - 1e-6


def covers_shape(box, diameter: float) -> bool:
    """Verifica se a imagem cobre o quadrado/círculo delimitado pelo corte."""
    x, y, w, h = box
    r = diameter / 2
    return x <= -r + 1e-6 and y <= -r + 1e-6 and x + w >= r - 1e-6 and y + h >= r - 1e-6


def compute_layout(page_w: float, page_h: float, d: float, margin: float, gap: float, mode: str, reserved_bottom: float = 0.0):
    """
    Centros (x, y) em mm, com origem no canto SUPERIOR esquerdo da folha,
    ordenados da esquerda para a direita e de cima para baixo.
    mode: "grid" (alinhado) ou "hex" (colunas alternadas, mais compacto).
    """
    pitch = d + gap
    reserved_bottom = max(0.0, float(reserved_bottom))
    content_h = max(0.0, page_h - reserved_bottom)
    usable_w, usable_h = page_w - 2 * margin, content_h - 2 * margin
    if usable_w < d - 1e-9 or usable_h < d - 1e-9:
        return []

    row_pitch = pitch if mode == "grid" else pitch * math.sqrt(3) / 2
    n_rows = int((usable_h - d) / row_pitch + 1e-9) + 1

    pts: list[tuple[float, float]] = []
    for r in range(n_rows):
        shift = pitch / 2 if (mode == "hex" and r % 2) else 0.0
        room = usable_w - d - shift
        if room < -1e-9:
            continue
        for c in range(int(room / pitch + 1e-9) + 1):
            pts.append((d / 2 + shift + c * pitch, d / 2 + r * row_pitch))
    if not pts:
        return []

    # centraliza o conjunto na folha
    min_x, max_x = min(p[0] for p in pts) - d / 2, max(p[0] for p in pts) + d / 2
    min_y, max_y = min(p[1] for p in pts) - d / 2, max(p[1] for p in pts) + d / 2
    dx = (page_w - (max_x - min_x)) / 2 - min_x
    dy = (content_h - (max_y - min_y)) / 2 - min_y
    return [(x + dx, y + dy) for x, y in pts]


def recommend_single_layout(page_w: float, page_h: float, spec: ButtonSpec):
    """Escolhe margem/espaçamento confortáveis sem perder capacidade da folha."""
    best = None
    modes = ("grid", "hex") if spec.shape == "circle" else ("grid",)
    for margin in (4.0, 5.0, 6.0, 7.0, 8.0):
        for gap in (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0):
            for mode in modes:
                positions = compute_layout(page_w, page_h, spec.cut, margin, gap, mode)
                # Primeiro maximiza a quantidade; em empates prefere mais respiro.
                score = (len(positions), -abs(gap - 3.0), -abs(margin - 5.0), gap, margin, 1 if mode == "grid" else 0)
                if best is None or score > best[0]:
                    best = (score, margin, gap, mode, positions)
    if best is None:
        return 5.0, 3.0, "grid"

    # A mesma quantidade nunca deve exibir Hexagonal como padrão: quando
    # Grade empata ou supera a capacidade, a Grade é a escolha visual padrão.
    selected_margin, selected_gap = best[1], best[2]
    grid_count = len(compute_layout(page_w, page_h, spec.cut, selected_margin, selected_gap, "grid"))
    hex_count = (
        len(compute_layout(page_w, page_h, spec.cut, selected_margin, selected_gap, "hex"))
        if spec.shape == "circle" else 0
    )
    selected_mode = "hex" if hex_count > grid_count else "grid"
    return selected_margin, selected_gap, selected_mode


def _rect_intersects(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw - 1e-9 and ax + aw > bx + 1e-9 and ay < by + bh - 1e-9 and ay + ah > by + 1e-9


def _prune_free_rectangles(rectangles):
    """Remove retângulos livres contidos em outros, mantendo o MaxRects enxuto."""
    clean = [r for r in rectangles if r[2] > 1e-7 and r[3] > 1e-7]
    keep = [True] * len(clean)
    for i, a in enumerate(clean):
        if not keep[i]:
            continue
        ax, ay, aw, ah = a
        for j, b in enumerate(clean):
            if i == j or not keep[j]:
                continue
            bx, by, bw, bh = b
            if ax >= bx - 1e-9 and ay >= by - 1e-9 and ax + aw <= bx + bw + 1e-9 and ay + ah <= by + bh + 1e-9:
                keep[i] = False
                break
    return [r for r, ok in zip(clean, keep) if ok]


def _maxrects_pack(page_w, page_h, specs, order, margin, gap, heuristic="short"):
    """Empacota caixas de corte com MaxRects e diferentes critérios de encaixe.

    Cada item ocupa uma caixa (corte + folga). Essa abordagem é conservadora
    para círculos, mas garante que os limites de corte nunca se sobreponham e
    que a distância mínima pedida seja respeitada também entre formatos mistos.
    """
    left = margin - gap / 2
    top = margin - gap / 2
    usable_w = page_w - 2 * margin + gap
    usable_h = page_h - 2 * margin + gap
    if usable_w <= 0 or usable_h <= 0:
        return []

    free = [(left, top, usable_w, usable_h)]
    placed = []
    remaining_sides = [specs[i].cut + gap for i in order]
    suffix_min = [0.0] * (len(remaining_sides) + 1)
    for k in range(len(remaining_sides) - 1, -1, -1):
        suffix_min[k] = remaining_sides[k] if k == len(remaining_sides) - 1 else min(remaining_sides[k], suffix_min[k + 1])

    for order_pos, item_index in enumerate(order):
        side = remaining_sides[order_pos]
        best = None
        for free_index, rect in enumerate(free):
            fx, fy, fw, fh = rect
            if side > fw + 1e-9 or side > fh + 1e-9:
                continue
            dw, dh = fw - side, fh - side
            short_side, long_side = min(dw, dh), max(dw, dh)
            if heuristic == "area":
                score = (fw * fh - side * side, short_side, long_side, fy, fx)
            elif heuristic == "bottom":
                score = (fy + side, fx, short_side, long_side)
            elif heuristic == "long":
                score = (long_side, short_side, fy, fx)
            else:  # best short side fit
                score = (short_side, long_side, fy, fx)
            if best is None or score < best[0]:
                best = (score, free_index, (fx, fy, side, side))

        if best is None:
            # Se nenhum espaço livre comporta nem o menor dos itens restantes,
            # não há motivo para percorrer centenas de itens sem possibilidade.
            largest_free_square = max((min(r[2], r[3]) for r in free), default=0.0)
            next_min = suffix_min[order_pos + 1] if order_pos + 1 < len(order) else float("inf")
            if largest_free_square + 1e-9 < next_min:
                break
            continue

        used = best[2]
        ux, uy, uw, uh = used
        new_free = []
        for rect in free:
            if not _rect_intersects(rect, used):
                new_free.append(rect)
                continue
            fx, fy, fw, fh = rect
            if ux > fx + 1e-9:
                new_free.append((fx, fy, ux - fx, fh))
            if ux + uw < fx + fw - 1e-9:
                new_free.append((ux + uw, fy, fx + fw - (ux + uw), fh))
            if uy > fy + 1e-9:
                new_free.append((fx, fy, fw, uy - fy))
            if uy + uh < fy + fh - 1e-9:
                new_free.append((fx, uy + uh, fw, fy + fh - (uy + uh)))
        free = _prune_free_rectangles(new_free)
        placed.append((item_index, ux + side / 2, uy + side / 2))

    if placed:
        min_x = min(cx - specs[i].cut / 2 for i, cx, _ in placed)
        max_x = max(cx + specs[i].cut / 2 for i, cx, _ in placed)
        min_y = min(cy - specs[i].cut / 2 for i, _, cy in placed)
        max_y = max(cy + specs[i].cut / 2 for i, _, cy in placed)
        dx = (page_w - (max_x - min_x)) / 2 - min_x
        dy = (page_h - (max_y - min_y)) / 2 - min_y
        placed = [(i, cx + dx, cy + dy) for i, cx, cy in placed]
        placed.sort(key=lambda p: (p[2], p[1]))
    return placed


def _shelf_pack(page_w, page_h, specs, order, margin, gap, preserve_order=False):
    """Distribuição em fileiras: cada fileira combina tamanhos que se encaixam.

    Ao contrário de um encaixe puramente local, esta estratégia ocupa a largura
    da folha de forma organizada e centraliza cada fileira. Cada item é alocado
    dentro de uma caixa de lado corte+espaço, garantindo folga entre contornos.
    """
    left = margin - gap / 2
    top = margin - gap / 2
    usable_w = page_w - 2 * margin + gap
    usable_h = page_h - 2 * margin + gap
    if usable_w <= 0 or usable_h <= 0:
        return [], {"rows": 0, "fill": 0.0}

    rows = []
    total_height = 0.0
    for item_index in order:
        side = specs[item_index].cut + gap
        if side > usable_w + 1e-9 or side > usable_h + 1e-9:
            continue

        row_choices = []
        candidate_rows = [(len(rows) - 1, rows[-1])] if preserve_order and rows else list(enumerate(rows))
        for row_idx, row in candidate_rows:
            remaining = usable_w - row["used_w"]
            new_height = max(row["height"], side)
            height_delta = new_height - row["height"]
            if side <= remaining + 1e-9 and total_height + height_delta <= usable_h + 1e-9:
                # Best-fit: ocupa primeiro a fileira cujo espaço restante
                # depois deste item será menor, sem aumentar desnecessariamente
                # a altura total da composição.
                row_choices.append((remaining - side, height_delta, row_idx))

        if row_choices:
            _, height_delta, row_idx = min(row_choices)
            row = rows[row_idx]
            row["items"].append((item_index, side))
            row["used_w"] += side
            row["height"] += height_delta
            total_height += height_delta
        elif total_height + side <= usable_h + 1e-9:
            rows.append({"items": [(item_index, side)], "used_w": side, "height": side})
            total_height += side

    if not rows:
        return [], {"rows": 0, "fill": 0.0}

    # Fileiras são centralizadas individualmente e o bloco inteiro fica no
    # centro da área útil. Itens menores ficam centralizados na altura da linha.
    y_cursor = top + (usable_h - total_height) / 2
    placed = []
    row_fills = []
    for row in rows:
        x_cursor = left + (usable_w - row["used_w"]) / 2
        row_fills.append(row["used_w"] / usable_w)
        for item_index, side in row["items"]:
            cx = x_cursor + side / 2
            cy = y_cursor + row["height"] / 2
            placed.append((item_index, cx, cy))
            x_cursor += side
        y_cursor += row["height"]

    # Normaliza a origem para manter o centro geométrico da composição na folha.
    placed.sort(key=lambda p: (p[2], p[1]))
    return placed, {"rows": len(rows), "fill": sum(row_fills) / len(row_fills)}


def _mixed_order_variants(expanded, specs):
    """Gera ordens determinísticas diferentes para reduzir dependência da entrada."""
    desc = sorted(expanded, key=lambda i: (-specs[i].cut, i))
    desc_rev = sorted(expanded, key=lambda i: (-specs[i].cut, -i))
    asc = sorted(expanded, key=lambda i: (specs[i].cut, i))

    # Alterna peças grandes e pequenas; útil quando fileiras podem misturar tamanhos.
    alternating = []
    lo, hi = 0, len(desc) - 1
    while lo <= hi:
        alternating.append(desc[lo])
        lo += 1
        if lo <= hi:
            alternating.append(desc[hi])
            hi -= 1

    # Garante que o algoritmo experimente colocar ao menos uma unidade de
    # cada tipo antes de completar as quantidades restantes. Isso evita que
    # os tamanhos pequenos ocupem todo o espaço e eliminem um tamanho grande.
    quantities_by_type = {}
    for idx in expanded:
        quantities_by_type[idx] = quantities_by_type.get(idx, 0) + 1
    first_each = sorted(quantities_by_type, key=lambda i: (-specs[i].cut, i))
    balanced = list(first_each)
    for idx in sorted(expanded, key=lambda i: (-specs[i].cut, i)):
        if quantities_by_type[idx] > 1:
            balanced.extend([idx] * (quantities_by_type[idx] - 1))
            quantities_by_type[idx] = 1

    # Distribui os tipos por frequência, evitando que a ordem de cadastro
    # monopolize o início da folha.
    grouped = {}
    for idx in expanded:
        grouped.setdefault(idx, []).append(idx)
    type_order = sorted(grouped, key=lambda i: (-len(grouped[i]), -specs[i].cut, i))
    round_robin = []
    queues = {i: list(grouped[i]) for i in type_order}
    while any(queues.values()):
        for i in type_order:
            if queues[i]:
                round_robin.append(queues[i].pop())

    # A ordem de cadastro também é testada. Ela permite que o número do
    # botton corresponda à leitura visual da folha sempre que o encaixe permitir.
    variants = [list(expanded), balanced, desc, desc_rev, alternating, round_robin, asc]
    unique = []
    seen = set()
    for variant in variants:
        marker = tuple(variant)
        if marker not in seen:
            seen.add(marker)
            unique.append(variant)
    return unique


def _circle_hex_pack(page_w, page_h, specs, order, margin, gap, stagger_fraction=0.5):
    """Empacota círculos de diâmetros diferentes em fileiras hexagonais.

    A distância vertical entre fileiras é calculada par a par usando os raios
    reais. Isso evita sobreposição e respeita a folga mínima também quando os
    diâmetros variam bastante.
    """
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    if usable_w <= 0 or usable_h <= 0:
        return []

    placed = []
    cursor = 0
    row_index = 0
    while cursor < len(order):
        row_items = []
        local_x = []
        min_local = max_local = 0.0

        # Constrói uma fileira com a sequência atual, respeitando a largura útil.
        while cursor < len(order):
            item_index = order[cursor]
            radius = specs[item_index].cut / 2
            if not row_items:
                candidate_x = 0.0
                candidate_min = -radius
                candidate_max = radius
            else:
                prev_index = row_items[-1]
                prev_radius = specs[prev_index].cut / 2
                candidate_x = local_x[-1] + prev_radius + radius + gap
                candidate_min = min_local
                candidate_max = candidate_x + radius

            if candidate_max - candidate_min > usable_w + 1e-9:
                break
            row_items.append(item_index)
            local_x.append(candidate_x)
            min_local = min(candidate_min, candidate_x - radius)
            max_local = max(candidate_max, candidate_x + radius)
            cursor += 1

        # Um botton maior que a própria área útil não pode ser encaixado.
        if not row_items:
            cursor += 1
            continue

        pitches = [
            local_x[k + 1] - local_x[k]
            for k in range(len(local_x) - 1)
        ]
        fallback_pitch = specs[row_items[0]].cut + gap
        median_pitch = sorted(pitches)[len(pitches) // 2] if pitches else fallback_pitch
        stagger = median_pitch * stagger_fraction if row_index % 2 else 0.0

        row_center = (min_local + max_local) / 2
        desired_center = page_w / 2 + stagger
        shift = desired_center - row_center
        shift = max(margin - min_local, min(shift, page_w - margin - max_local))
        row_x = [x + shift for x in local_x]
        row_max_radius = max(specs[i].cut / 2 for i in row_items)
        row_y = margin + row_max_radius

        # Mantém a distância mínima em relação a TODOS os círculos anteriores,
        # não apenas à fileira imediatamente anterior.
        for item_index, x in zip(row_items, row_x):
            radius = specs[item_index].cut / 2
            for prev_index, prev_x, prev_y in placed:
                prev_radius = specs[prev_index].cut / 2
                separation = radius + prev_radius + gap
                dx = abs(x - prev_x)
                if dx < separation:
                    required_dy = math.sqrt(max(0.0, separation * separation - dx * dx))
                    row_y = max(row_y, prev_y + required_dy)

        if row_y + row_max_radius > page_h - margin + 1e-9:
            break

        placed.extend(
            (item_index, x, row_y)
            for item_index, x in zip(row_items, row_x)
        )
        row_index += 1

    if placed:
        min_x = min(x - specs[i].cut / 2 for i, x, _ in placed)
        max_x = max(x + specs[i].cut / 2 for i, x, _ in placed)
        min_y = min(y - specs[i].cut / 2 for i, _, y in placed)
        max_y = max(y + specs[i].cut / 2 for i, _, y in placed)
        dx = (page_w - (max_x - min_x)) / 2 - min_x
        dy = (page_h - (max_y - min_y)) / 2 - min_y
        placed = [(i, x + dx, y + dy) for i, x, y in placed]
        placed.sort(key=lambda item: (item[2], item[1]))
    return placed


def _sequence_inversions(candidate, expected_order):
    """Mede quanto a leitura visual da folha se afasta da ordem dos bottons."""
    expected_rank = {}
    for rank, item_index in enumerate(expected_order):
        expected_rank.setdefault(item_index, rank)
    visual = [
        item_index
        for item_index, _, _ in sorted(candidate, key=lambda item: (item[2], item[1]))
    ]
    ranks = [expected_rank.get(item_index, item_index) for item_index in visual]
    return sum(
        1 for i in range(len(ranks)) for j in range(i + 1, len(ranks))
        if ranks[i] > ranks[j]
    )


def _layout_bbox_area(candidate, specs):
    """Área da caixa ocupada pela composição; menor tende a ser mais compacta."""
    if not candidate:
        return float("inf")
    min_x = min(x - specs[i].cut / 2 for i, x, _ in candidate)
    max_x = max(x + specs[i].cut / 2 for i, x, _ in candidate)
    min_y = min(y - specs[i].cut / 2 for i, _, y in candidate)
    max_y = max(y + specs[i].cut / 2 for i, _, y in candidate)
    return (max_x - min_x) * (max_y - min_y)


def compute_mixed_layout(page_w, page_h, specs, quantities, margin, gap, mode="auto"):
    """Distribui bottons mistos por grade, hexágono ou melhor aproveitamento.

    Para círculos de tamanhos diferentes, o modo hexagonal usa a geometria real
    dos círculos. Quando existem bottons quadrados, a opção hexagonal é ignorada
    e a grade retangular preserva a distância entre todos os cortes.
    """
    expanded = [i for i, qty in enumerate(quantities) for _ in range(max(0, int(qty)))]
    if not expanded:
        return []

    all_circles = all(specs[i].shape == "circle" for i in expanded)
    unique_shapes = {(specs[i].cut, specs[i].shape) for i in expanded}

    if mode == "auto":
        grid = compute_mixed_layout(page_w, page_h, specs, quantities, margin, gap, mode="grid")
        if not all_circles:
            return grid
        hexagonal = compute_mixed_layout(page_w, page_h, specs, quantities, margin, gap, mode="hex")
        # Prioriza quantidade; em empate escolhe o arranjo que ocupa a menor área.
        if len(hexagonal) > len(grid):
            return hexagonal
        if len(hexagonal) < len(grid):
            return grid
        return hexagonal if _layout_bbox_area(hexagonal, specs) <= _layout_bbox_area(grid, specs) else grid

    if mode == "hex" and all_circles:
        if len(unique_shapes) == 1:
            cut, _ = next(iter(unique_shapes))
            positions = compute_layout(page_w, page_h, cut, margin, gap, "hex")
            result = [(item_index, pos[0], pos[1]) for pos, item_index in zip(positions, expanded)]
            return result

        best = []
        best_score = None
        for order in _mixed_order_variants(expanded, specs):
            candidate = _circle_hex_pack(page_w, page_h, specs, order, margin, gap)
            if not candidate:
                continue
            score = (
                len(candidate),
                -_sequence_inversions(candidate, expanded),
                -_layout_bbox_area(candidate, specs),
            )
            if best_score is None or score > best_score:
                best, best_score = candidate, score
        return best

    # Grade: para um único tamanho/formato usa a malha regular exata.
    if len(unique_shapes) == 1:
        cut, _ = next(iter(unique_shapes))
        positions = compute_layout(page_w, page_h, cut, margin, gap, "grid")
        return [(item_index, pos[0], pos[1]) for pos, item_index in zip(positions, expanded)]

    best = []
    best_score = None
    for order in _mixed_order_variants(expanded, specs):
        shelf, shelf_stats = _shelf_pack(
            page_w, page_h, specs, order, margin, gap,
            preserve_order=(order == expanded),
        )
        candidates = [(shelf, 1, shelf_stats.get("fill", 0.0))]
        for heuristic in ("short", "area", "bottom"):
            packed = _maxrects_pack(page_w, page_h, specs, order, margin, gap, heuristic)
            candidates.append((packed, 0, 0.0))

        for candidate, shelf_flag, row_fill in candidates:
            if not candidate:
                continue
            counts = [0] * len(specs)
            for item_index, _, _ in candidate:
                counts[item_index] += 1
            covered = sum(count > 0 for count in counts)
            completed = sum(
                1 for i, qty in enumerate(quantities)
                if int(qty) > 0 and counts[i] >= int(qty)
            )
            inversions = _sequence_inversions(candidate, expanded)
            score = (
                covered, len(candidate), completed, -inversions,
                -_layout_bbox_area(candidate, specs), shelf_flag, row_fill,
            )
            if best_score is None or score > best_score:
                best, best_score = candidate, score
    return best


def recommend_mixed_layout(page_w, page_h, specs, quantities):
    """Sugere margem, folga e disposição com o melhor aproveitamento."""
    all_circles = all(
        specs[i].shape == "circle"
        for i, qty in enumerate(quantities) if int(qty) > 0
    )
    modes = ("grid", "hex") if all_circles else ("grid",)
    best = None
    for margin in (4.0, 5.0, 6.0, 7.0, 8.0):
        for gap in (2.0, 3.0, 4.0, 5.0, 6.0):
            for mode in modes:
                positions = compute_mixed_layout(
                    page_w, page_h, specs, quantities, margin, gap, mode=mode
                )
                covered = len({i for i, _, _ in positions})
                counts = [sum(1 for i, _, _ in positions if i == j) for j in range(len(specs))]
                completed = sum(
                    1 for i, qty in enumerate(quantities)
                    if int(qty) > 0 and counts[i] >= int(qty)
                )
                score = (
                    covered,
                    len(positions),
                    completed,
                    -abs(gap - 3.0),
                    -abs(margin - 5.0),
                    -_layout_bbox_area(positions, specs),
                    1 if mode == "hex" else 0,
                    gap,
                    margin,
                )
                if best is None or score > best[0]:
                    best = (score, margin, gap, mode)
    return (best[1], best[2], best[3]) if best else (5.0, 3.0, "grid")


# ----------------------------------------------------------------------------
# Imagem
# ----------------------------------------------------------------------------


@st.cache_data(show_spinner=False, hash_funcs={bytes: lambda _value: 0})
def _load_image_cached(image_digest: str, data: bytes) -> Image.Image:
    """Decodifica uma imagem uma única vez por fingerprint SHA-256."""
    with Image.open(io.BytesIO(data)) as source:
        im = ImageOps.exif_transpose(source).convert("RGBA")
    longest = max(im.size)
    if longest > MAX_SOURCE_PX:
        k = MAX_SOURCE_PX / longest
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    return im


def load_image(data: bytes, bg_hex: str = "#FFFFFF", image_digest: str | None = None) -> Image.Image:
    """Abre a imagem original em qualidade de produção; bg_hex é mantido por compatibilidade."""
    digest = image_digest or hashlib.sha256(data).hexdigest()
    return _load_image_cached(digest, data)


@st.cache_data(show_spinner=False, max_entries=32, hash_funcs={bytes: lambda _value: 0})
def load_image_preview(image_digest: str, data: bytes, max_side: int = 1250) -> Image.Image:
    """Decodifica uma versão leve para tela, sem reduzir o arquivo original de produção."""
    limit = max(256, int(max_side))
    with Image.open(io.BytesIO(data)) as source:
        # JPEG permite decodificação em baixa resolução diretamente no decoder.
        try:
            source.draft("RGB", (limit, limit))
        except (AttributeError, OSError, ValueError):
            pass
        # Só copia/rotaciona quando há orientação EXIF a aplicar. Para a maioria
        # dos PNGs/JPEGs, reduz primeiro em seu modo nativo e converte depois.
        try:
            orientation = int(source.getexif().get(274, 1))
        except Exception:
            orientation = 1
        working = ImageOps.exif_transpose(source) if orientation not in (0, 1) else source
        working.thumbnail((limit, limit), Image.Resampling.LANCZOS, reducing_gap=3.0)
        preview = working.convert("RGBA")
    return preview


@st.cache_data(show_spinner=False, max_entries=256, hash_funcs={bytes: lambda _value: 0})
def get_image_dimensions(image_digest: str, data: bytes) -> tuple[int, int]:
    """Obtém dimensões efetivas para DPI sem decodificar a imagem inteira."""
    with Image.open(io.BytesIO(data)) as source:
        width, height = source.size
        try:
            orientation = int(source.getexif().get(274, 1))
        except Exception:
            orientation = 1
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    longest = max(width, height, 1)
    factor = min(1.0, MAX_SOURCE_PX / longest)
    return max(1, round(width * factor)), max(1, round(height * factor))


def _encode_pool_thumbnail(image_bytes: bytes, size: int = 96) -> str:
    """Cria a miniatura do Pool usando decodificação reduzida quando disponível."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        try:
            source.draft("RGB", (size * 3, size * 3))
        except (AttributeError, OSError, ValueError):
            pass
        try:
            orientation = int(source.getexif().get(274, 1))
        except Exception:
            orientation = 1
        working = ImageOps.exif_transpose(source) if orientation not in (0, 1) else source
        # A redução acontece antes da conversão RGBA para limitar uso de memória.
        working.thumbnail((size - 8, size - 8), Image.Resampling.BILINEAR, reducing_gap=3.0)
        thumb = working.convert("RGBA")
    tile = Image.new("RGBA", (size, size), "white")
    tile.alpha_composite(thumb, ((size - thumb.width) // 2, (size - thumb.height) // 2))
    buffer = io.BytesIO()
    tile.save(buffer, format="PNG", optimize=False, compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def prepare_uploaded_artwork(file_name: str, image_bytes: bytes, size: int = 96) -> tuple[str, str]:
    """Valida o upload e prepara digest/miniatura em uma única decodificação leve."""
    digest = hashlib.sha256(image_bytes).hexdigest()
    return digest, _encode_pool_thumbnail(image_bytes, size)


@st.cache_resource(show_spinner=False)
def _get_background_removal_session():
    """Carrega uma única sessão U²-Net por processo; o modelo é baixado no primeiro uso."""
    from rembg import new_session
    return new_session("u2net")


@st.cache_data(show_spinner=False, max_entries=16, hash_funcs={bytes: lambda _value: 0})
def _process_artwork_image_cached(
    source_digest: str, source_bytes: bytes, remove_background: bool,
    white_balance_temperature: int, white_balance_tint: int, exposure: float,
    contrast: int, saturation: int, hue: int, sharpness: int,
) -> bytes:
    """Aplica remoção de fundo e correções não destrutivas sempre sobre a origem."""
    neutral = (
        not remove_background
        and int(white_balance_temperature) == 0
        and int(white_balance_tint) == 0
        and abs(float(exposure)) < 1e-9
        and int(contrast) == 100
        and int(saturation) == 100
        and int(hue) == 0
        and int(sharpness) == 100
    )
    if neutral:
        return source_bytes

    with Image.open(io.BytesIO(source_bytes)) as opened:
        rgba = ImageOps.exif_transpose(opened).convert("RGBA")
    longest = max(rgba.size)
    if longest > MAX_SOURCE_PX:
        factor = MAX_SOURCE_PX / longest
        rgba = rgba.resize((max(1, round(rgba.width * factor)), max(1, round(rgba.height * factor))), Image.Resampling.LANCZOS)

    if remove_background:
        # Importação tardia: a aplicação continua abrindo mesmo antes da instalação
        # opcional do modelo. O iniciar.bat instala o backend CPU automaticamente.
        from rembg import remove
        result = remove(
            rgba, session=_get_background_removal_session(), decontaminate=True
        )
        if isinstance(result, bytes):
            with Image.open(io.BytesIO(result)) as opened_result:
                rgba = opened_result.convert("RGBA")
        else:
            rgba = result.convert("RGBA")

    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")

    # Balanço de branco por ganho de canais, mantendo os detalhes e o alfa.
    temperature = max(-100, min(100, int(white_balance_temperature))) / 100.0
    tint = max(-100, min(100, int(white_balance_tint))) / 100.0
    gains = (
        (1.0 + 0.28 * temperature) * (1.0 + 0.12 * tint),
        1.0 - 0.16 * tint,
        (1.0 - 0.28 * temperature) * (1.0 + 0.12 * tint),
    )
    if any(abs(gain - 1.0) > 1e-9 for gain in gains):
        channels = rgb.split()
        corrected = []
        for channel, gain in zip(channels, gains):
            lut = [max(0, min(255, round(value * gain))) for value in range(256)]
            corrected.append(channel.point(lut))
        rgb = Image.merge("RGB", tuple(corrected))

    # Iluminação/exposição em pontos EV, contraste e intensidade de cor.
    from PIL import ImageEnhance
    exposure_factor = 2.0 ** max(-2.0, min(2.0, float(exposure)))
    if abs(exposure_factor - 1.0) > 1e-9:
        rgb = ImageEnhance.Brightness(rgb).enhance(exposure_factor)
    contrast_factor = max(0.0, min(2.0, int(contrast) / 100.0))
    if abs(contrast_factor - 1.0) > 1e-9:
        rgb = ImageEnhance.Contrast(rgb).enhance(contrast_factor)
    saturation_factor = max(0.0, min(2.0, int(saturation) / 100.0))
    if abs(saturation_factor - 1.0) > 1e-9:
        rgb = ImageEnhance.Color(rgb).enhance(saturation_factor)

    hue_shift = max(-180, min(180, int(hue)))
    if hue_shift:
        hsv = rgb.convert("HSV")
        h, sat, val = hsv.split()
        shift = round(hue_shift * 255 / 360)
        h = h.point(lambda value: (value + shift) % 256)
        rgb = Image.merge("HSV", (h, sat, val)).convert("RGB")

    sharpness_factor = max(0.0, min(2.0, int(sharpness) / 100.0))
    if abs(sharpness_factor - 1.0) > 1e-9:
        rgb = ImageEnhance.Sharpness(rgb).enhance(sharpness_factor)

    corrected_rgba = Image.merge("RGBA", (*rgb.split(), alpha))
    buffer = io.BytesIO()
    corrected_rgba.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _sync_processed_artwork(item: dict, progress_callback=None) -> None:
    """Atualiza a arte a partir do original e sinaliza as etapas demoradas da IA."""
    original = item.get("original_image")
    if not isinstance(original, bytes) or not original:
        # Migração transparente de itens já existentes nesta compilação.
        original = item.get("image", b"")
        item["original_image"] = original
    if not original:
        return

    original_digest = item.get("original_image_digest")
    if not isinstance(original_digest, str) or len(original_digest) != 64:
        original_digest = hashlib.sha256(original).hexdigest()
        item["original_image_digest"] = original_digest

    edit = item.setdefault("edit", {})
    params = {key: edit.get(key, default) for key, default in IMAGE_ADJUSTMENT_DEFAULTS.items()}
    signature_source = repr((original_digest, tuple(sorted(params.items())))).encode("utf-8")
    signature = hashlib.sha256(signature_source).hexdigest()
    if item.get("_processed_image_signature") == signature:
        return

    use_ai = bool(params["remove_background"])
    batch_active = bool(st.session_state.get("_batch_processing_active", False))
    local_progress = None
    if use_ai and not batch_active and progress_callback is None:
        local_progress = st.progress(0, text="Preparando remoção de fundo por IA…")

    def _report(percent: int, message: str) -> None:
        percent = max(0, min(100, int(percent)))
        if progress_callback is not None:
            progress_callback(percent, message)
        elif local_progress is not None:
            local_progress.progress(percent, text=message)

    try:
        def _run_image_processing():
            return _process_artwork_image_cached(
                original_digest, original, bool(params["remove_background"]),
                int(params["white_balance_temperature"]), int(params["white_balance_tint"]),
                float(params["exposure"]), int(params["contrast"]), int(params["saturation"]),
                int(params["hue"]), int(params["sharpness"]),
            )

        if use_ai:
            _report(4, "Preparando imagem para remoção de fundo…")
            # O primeiro uso pode baixar/carregar o U²-Net; o spinner mantém o
            # usuário informado durante essa etapa, mesmo quando não há callback
            # de progresso interno disponível na biblioteca rembg.
            with st.spinner("Carregando o modelo de IA (U²-Net)…"):
                _get_background_removal_session()
            _report(16, "Modelo pronto · IA analisando a imagem…")
            with st.spinner("Removendo o fundo com IA. Esta etapa pode levar alguns instantes…"):
                processed = _run_image_processing()
            _report(92, "Finalizando transparência e composição…")
        elif batch_active:
            processed = _run_image_processing()
        else:
            with st.spinner("Aplicando correções à imagem…"):
                processed = _run_image_processing()

    except Exception as exc:
        # Mantém a origem intacta e sinaliza o problema no painel da arte em foco.
        item["image"] = original
        item["image_digest"] = original_digest
        item["thumbnail_data_url"] = _encode_pool_thumbnail(original, 96)
        item["image_processing_error"] = str(exc)
        item["_processed_image_signature"] = signature
        if use_ai:
            _report(100, "Não foi possível concluir a remoção de fundo.")
        return

    item["image"] = processed
    item["image_digest"] = hashlib.sha256(processed).hexdigest()
    item["thumbnail_data_url"] = _encode_pool_thumbnail(processed, 96)
    item["_processed_image_signature"] = signature
    item.pop("image_processing_error", None)
    if use_ai:
        _report(100, "Remoção de fundo concluída · 100%")


def _legacy_initial_contain_zoom(image_bytes: bytes) -> float:
    """Reconhece o antigo preset de entrada (contain) para migração única.

    Versões anteriores iniciavam imagens retangulares com o maior lado dentro
    do Corte. O padrão atual é Preencher até o Corte (cover, zoom=1).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            oriented = ImageOps.exif_transpose(source)
            width, height = oriented.size
        longest = max(int(width), int(height), 1)
        shortest = max(min(int(width), int(height)), 1)
        return max(0.05, min(1.0, shortest / longest))
    except Exception:
        return 1.0


# ----------------------------------------------------------------------------
# Pré-visualização (PIL)
# ----------------------------------------------------------------------------


def _draw_shape(draw: ImageDraw.ImageDraw, shape: str, bbox, *, outline=None, fill=None, width=1):
    if shape == "square":
        draw.rectangle(bbox, outline=outline, fill=fill, width=width)
    else:
        draw.ellipse(bbox, outline=outline, fill=fill, width=width)


def _shape_mask(shape: str, size: tuple[int, int], diameter_px: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    c_x, c_y = size[0] / 2, size[1] / 2
    r = diameter_px / 2
    _draw_shape(
        ImageDraw.Draw(mask), shape,
        [c_x - r, c_y - r, c_x + r, c_y + r], fill=255,
    )
    return mask


def _paste_box(layer: Image.Image, img: Image.Image, box, mm_px: float, center: float):
    """Redimensiona e posiciona uma imagem em RGB ou RGBA, respeitando o canal alfa."""
    x, y, w, h = box
    resized = img.resize((max(1, round(w * mm_px)), max(1, round(h * mm_px))), Image.LANCZOS)
    if resized.mode != "RGBA":
        resized = resized.convert("RGBA")
    dest_x = round(center + x * mm_px)
    dest_y = round(center - (y + h) * mm_px)
    # Recorta a parte visível antes de compor: fotos ampliadas frequentemente
    # ultrapassam a área quadrada de corte e precisam ser aparadas sem erro.
    left = max(0, dest_x)
    top = max(0, dest_y)
    right = min(layer.width, dest_x + resized.width)
    bottom = min(layer.height, dest_y + resized.height)
    if right <= left or bottom <= top:
        return
    crop = resized.crop((left - dest_x, top - dest_y, right - dest_x, bottom - dest_y))
    if layer.mode == "RGBA":
        layer.alpha_composite(crop, (left, top))
    else:
        layer.paste(crop.convert("RGB"), (left, top), crop.getchannel("A"))


def _make_backdrop_source(source: Image.Image, bg: str) -> Image.Image:
    """Cria uma fonte opaca para o fundo: recorta o conteúdo visível do PNG e compõe transparências sobre bg."""
    rgba = source.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox is not None:
        rgba = rgba.crop(alpha_bbox)
    # Um PNG totalmente transparente não tem conteúdo para desfocar; a cor-base
    # continua sendo um fundo válido e previsível.
    rgb = ImageColor.getrgb(bg)
    opaque = Image.new("RGBA", rgba.size if rgba.size[0] and rgba.size[1] else (1, 1), (*rgb, 255))
    if alpha_bbox is not None:
        opaque.alpha_composite(rgba)
    return opaque


def render_artwork(
    spec: ButtonSpec,
    img: Image.Image | None,
    *,
    ppm: float,
    zoom: float = 1.0,
    off_x: float = 0.0,
    off_y: float = 0.0,
    bg: str = "#FFFFFF",
    fit_mode: str = "cut",
    blur_fill: bool = True,
    blur_mm: float = 0.5,
    background_mode: str | None = None,
    background_image: Image.Image | None = None,
) -> Image.Image:
    """Gera a arte sem guias; um fundo personalizado pode preencher todo o corte."""
    pixels = max(1, round(spec.cut * ppm))
    center = pixels / 2
    background_mode = background_mode or ("blur" if fit_mode == "art" and blur_fill else "color")
    if background_mode not in {"color", "blur"}:
        background_mode = "color"

    base = Image.new("RGBA", (pixels, pixels), (*ImageColor.getrgb(bg), 255))
    if background_image is not None:
        # A imagem de fundo cobre o quadrado externo da guia de corte. O recorte
        # circular/quadrado é aplicado ao final, preservando a sangria visual.
        backdrop_source = background_image.convert("RGBA")
        backdrop = ImageOps.fit(
            backdrop_source, (pixels, pixels), method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        base.alpha_composite(backdrop)
    if img is not None:
        source = img.convert("RGBA")

        if background_image is None and background_mode == "blur":
            # O backdrop é criado separadamente da arte nítida: ele sempre cobre
            # TODO o corte, centralizado, mesmo quando a arte é movida ou reduzida.
            # Para PNG transparente, o conteúdo visível é recortado antes de
            # ampliar, evitando desfocar apenas uma pequena borda de transparência.
            backdrop_source = _make_backdrop_source(source, bg)
            backdrop = Image.new("RGB", (pixels, pixels), bg)
            _paste_box(
                backdrop,
                backdrop_source,
                image_box(backdrop_source.width, backdrop_source.height, spec.cut, 1.0, 0.0, 0.0),
                ppm,
                center,
            )
            blurred = backdrop.filter(
                ImageFilter.GaussianBlur(radius=max(0.0, float(blur_mm) * ppm))
            )
            base = Image.new("RGBA", blurred.size, (0, 0, 0, 255))
            base.alpha_composite(blurred.convert("RGBA"))

        # A arte principal usa exatamente o zoom e os deslocamentos definidos
        # no editor. O fundo desfocado permanece fixo e preenche qualquer espaço
        # que a arte não ocupar dentro da guia de corte.
        # O preset selecionado continua controlando a escala da arte principal.
        # Com fundo personalizado, apenas a máscara de visibilidade fica limitada
        # à guia Arte, deixando o anel Arte–Corte livre para a borda decorativa.
        effective_fit_mode = fit_mode if fit_mode in {"cut", "art"} else "cut"
        if effective_fit_mode == "art":
            foreground = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
            _paste_box(
                foreground,
                source,
                fit_image_box(source.width, source.height, spec.art, zoom, off_x, off_y),
                ppm,
                center,
            )
        else:
            foreground = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
            _paste_box(
                foreground,
                source,
                image_box(source.width, source.height, spec.cut, zoom, off_x, off_y),
                ppm,
                center,
            )
        if effective_fit_mode == "art" or background_image is not None:
            visible_diameter = spec.art if (effective_fit_mode == "art" or background_image is not None) else spec.cut
            visible_mask = _shape_mask(spec.shape, (pixels, pixels), visible_diameter * ppm)
            foreground.putalpha(ImageChops.multiply(foreground.getchannel("A"), visible_mask))

        composed = base.convert("RGBA")
        composed.alpha_composite(foreground)
        base = composed.convert("RGB")
    else:
        base = base.convert("RGB")

    cut_mask = _shape_mask(spec.shape, (pixels, pixels), spec.cut * ppm)
    outside = Image.new("RGB", (pixels, pixels), bg)
    outside.paste(base, (0, 0), cut_mask)
    return outside


def _pil_square_guide(draw, c: float, r: float, color: str, style: str, lw: float, mm_px: float):
    """Desenha tracejado/pontilhado em um perímetro quadrado."""
    width = max(1, round(lw))
    side = 2 * r
    perimeter = 4 * side
    dot_r = max(0.6, lw * 0.6)

    def point(distance):
        d = distance % perimeter
        if d < side:
            return c - r + d, c - r
        if d < 2 * side:
            return c + r, c - r + (d - side)
        if d < 3 * side:
            return c + r - (d - 2 * side), c + r
        return c - r, c + r - (d - 3 * side)

    if style == "solid":
        draw.rectangle([c - r, c - r, c + r, c + r], outline=color, width=width)
        return

    step = (2.2 * mm_px + 1.4 * mm_px) if style == "dashed" else (1.6 * mm_px)
    count = max(1, round(perimeter / step))
    if style == "dotted":
        for i in range(count):
            x, y = point(i * perimeter / count)
            draw.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=color)
        return

    dash = 2.2 * mm_px
    gap = 1.4 * mm_px
    pos = 0.0
    while pos < perimeter:
        end = min(pos + dash, perimeter)
        cursor = pos
        while cursor < end:
            next_corner = (math.floor(cursor / side) + 1) * side
            segment_end = min(end, next_corner)
            x1, y1 = point(cursor)
            x2, y2 = point(segment_end)
            draw.line((x1, y1, x2, y2), fill=color, width=width)
            cursor = segment_end
        pos += dash + gap


def _pil_guide(draw: ImageDraw.ImageDraw, c: float, r: float, color: str, style: str,
               lw: float, mm_px: float, shape: str = "circle"):
    if shape == "square":
        _pil_square_guide(draw, c, r, color, style, lw, mm_px)
        return
    w = max(1, round(lw))
    bbox = [c - r, c - r, c + r, c + r]
    if style == "solid":
        draw.ellipse(bbox, outline=color, width=w)
    elif style == "dashed":
        dash, gap = 2.2 * mm_px, 1.4 * mm_px
        n = max(1, round(2 * math.pi * r / (dash + gap)))
        step = 360 / n
        for i in range(n):
            draw.arc(bbox, i * step, i * step + step * dash / (dash + gap), fill=color, width=w)
    else:  # dotted
        n = max(1, round(2 * math.pi * r / (1.6 * mm_px)))
        dot = max(0.6, lw * 0.6)
        for i in range(n):
            a = 2 * math.pi * i / n
            x, y = c + r * math.cos(a), c + r * math.sin(a)
            draw.ellipse([x - dot, y - dot, x + dot, y + dot], fill=color)


def render_button(
    spec: ButtonSpec,
    img: Image.Image | None,
    *,
    ppm: float,
    zoom: float = 1.0,
    off_x: float = 0.0,
    off_y: float = 0.0,
    bg: str = "#FFFFFF",
    guides: tuple[str, ...] = tuple(g.key for g in GUIDES),
    pad_mm: float = 3.0,
    line_mm: float = 0.3,
    fit_mode: str = "cut",
    blur_fill: bool = True,
    blur_mm: float = 0.5,
    background_mode: str | None = None,
    background_image: Image.Image | None = None,
    ss: int = 2,
) -> Image.Image:
    """Renderiza o botton com guias; ppm = pixels por mm."""
    mm_px = ppm * ss
    size = round((spec.cut + 2 * pad_mm) * mm_px)
    center = size / 2
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    artwork = render_artwork(
        spec, img, ppm=mm_px, zoom=zoom, off_x=off_x, off_y=off_y, bg=bg,
        fit_mode=fit_mode, blur_fill=blur_fill, blur_mm=blur_mm,
        background_mode=background_mode, background_image=background_image,
    ).convert("RGBA")
    cut_mask = _shape_mask(spec.shape, artwork.size, spec.cut * mm_px)
    artwork.putalpha(cut_mask)
    out.alpha_composite(artwork, (round((size - artwork.width) / 2), round((size - artwork.height) / 2)))

    glayer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glayer)
    for g in GUIDES:
        if g.key in guides:
            _pil_guide(
                gdraw, center, getattr(spec, g.attr) / 2 * mm_px,
                g.color, g.style, line_mm * mm_px, mm_px, spec.shape,
            )
    out = Image.alpha_composite(out, glayer)
    return out.resize((round(size / ss), round(size / ss)), Image.LANCZOS)


SHEET_PREVIEW_WIDTH_PX = 760


def sheet_preview_pixels_per_mm(page_w: float, page_h: float) -> float:
    """Mantém a largura visual clássica do canvas em 760 px.

    A escala física exata fica disponível no visualizador separado em tela cheia;
    a prévia de trabalho prioriza a composição centralizada que ocupava melhor
    a área da interface nas versões anteriores.
    """
    return SHEET_PREVIEW_WIDTH_PX / max(float(page_w), 1.0)


def layout_settings_profile_key(
    paper: str, page_w: float, page_h: float, size_key: str, shape: str
) -> tuple:
    """Chave das recomendações sem distinguir a rotação da mesma folha."""
    short_edge, long_edge = sorted((float(page_w), float(page_h)))
    return (str(paper), short_edge, long_edge, str(size_key), str(shape))


def render_sheet_preview(
    spec, img, positions, page_w, page_h, *, zoom, off_x, off_y, bg, guides,
    fit_mode="cut", blur_fill=True, blur_mm=0.5, background_mode=None,
    background_image=None,
) -> Image.Image:
    ppm = sheet_preview_pixels_per_mm(page_w, page_h)
    sheet = Image.new("RGB", (round(page_w * ppm), round(page_h * ppm)), "white")
    ImageDraw.Draw(sheet).rectangle([0, 0, sheet.width - 1, sheet.height - 1], outline="#C9CED6")
    if not positions:
        return sheet
    btn = render_button(
        spec, img, ppm=ppm, zoom=zoom, off_x=off_x, off_y=off_y, bg=bg, guides=guides,
        pad_mm=0.5, line_mm=0.25, fit_mode=fit_mode,
        blur_fill=blur_fill, blur_mm=blur_mm, background_mode=background_mode,
        background_image=background_image,
    )
    for x, y in positions:
        sheet.paste(btn, (round(x * ppm - btn.width / 2), round(y * ppm - btn.height / 2)), btn)
    return sheet


def render_mixed_sheet_preview(
    items, placements, page_w, page_h, *, zoom=1.0, off_x=0.0, off_y=0.0,
    bg="#FFFFFF", guides=tuple(g.key for g in GUIDES), fit_mode="cut",
    blur_fill=True, blur_mm=0.5, background_mode=None, background_image=None,
    footer_margin_mm=5.0,
) -> Image.Image:
    """Prévia de uma folha com arte e escala física visual estável por orientação."""
    ppm = sheet_preview_pixels_per_mm(page_w, page_h)
    sheet = Image.new("RGB", (round(page_w * ppm), round(page_h * ppm)), "white")
    ImageDraw.Draw(sheet).rectangle([0, 0, sheet.width - 1, sheet.height - 1], outline="#C9CED6")
    for item_index, cx, cy in placements:
        item = items[item_index]
        button = render_button(
            item["spec"], item["img"], ppm=ppm,
            zoom=item.get("zoom", zoom),
            off_x=item.get("off_x", off_x),
            off_y=item.get("off_y", off_y),
            bg=item.get("bg", bg), guides=guides, pad_mm=0.5, line_mm=0.25,
            fit_mode=item.get("fit_mode", fit_mode),
            blur_fill=item.get("blur_fill", blur_fill),
            blur_mm=item.get("blur_mm", blur_mm),
            background_mode=item.get("background_mode", background_mode),
            background_image=item.get("background_image", background_image),
        )
        sheet.paste(
            button,
            (round(cx * ppm - button.width / 2), round(cy * ppm - button.height / 2)),
            button,
        )
    _draw_sheet_brand_footer_pil(sheet, ppm, footer_margin_mm)
    return sheet


# ----------------------------------------------------------------------------
# Projeto reeditável e identificação da folha
# ----------------------------------------------------------------------------

PROJECT_ARCHIVE_NAME = "atomica_print_lab_project.atlp"  # compatibilidade com PDFs de projeto antigos
PROJECT_MANIFEST_NAME = "manifest.json"
PROJECT_SCHEMA_VERSION = 1
PROJECT_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
PROJECT_MAX_UPLOAD_BYTES = 190 * 1024 * 1024
PROJECT_MAX_ITEMS = 5000
SHEET_FOOTER_MIN_MARGIN_MM = 4.5


def _json_safe(value):
    """Converte parâmetros do projeto para JSON sem serializar conteúdo binário."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        raise TypeError("O projeto contém dados binários fora da seção de arquivos." )
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _project_asset_path(archive, data: bytes, cache: dict[str, str]) -> str:
    """Grava cada ativo uma única vez, indexado pelo SHA-256 do conteúdo."""
    digest = hashlib.sha256(data).hexdigest()
    existing = cache.get(digest)
    if existing:
        return existing
    path = f"assets/{digest}.bin"
    # Fotos PNG/JPEG/WebP já são comprimidas. ZIP_STORED evita gastar CPU
    # recomprimindo-as e torna a gravação do .atp mais rápida.
    archive.writestr(path, data, compress_type=zipfile.ZIP_STORED)
    cache[digest] = path
    return path


def build_atomica_project_archive(artwork_items, settings) -> bytes:
    """Gera um .atp independente com Pool, originais, fundos e parâmetros."""
    items = list(artwork_items or [])
    if not items:
        raise ValueError("Adicione pelo menos uma arte ao Pool antes de salvar o projeto.")
    if len(items) > PROJECT_MAX_ITEMS:
        raise ValueError(f"O projeto excede o limite de {PROJECT_MAX_ITEMS} artes.")

    manifest = {
        "format": "atomica-print-lab-project",
        "container_format": "atp",
        "schema_version": PROJECT_SCHEMA_VERSION,
        "app_version": "3.43.0",
        "created_by": "Atômica Print Lab",
        "settings": _json_safe(settings),
        "items": [],
    }
    buffer = io.BytesIO()
    asset_cache: dict[str, str] = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for index, item in enumerate(items):
            item_id = int(item.get("id", index + 1))
            image_bytes = item.get("image") or item.get("original_image")
            original_bytes = item.get("original_image") or image_bytes
            if not isinstance(image_bytes, bytes) or not image_bytes:
                raise ValueError(f"A arte {index + 1} não possui uma imagem válida para salvar.")
            if not isinstance(original_bytes, bytes) or not original_bytes:
                raise ValueError(f"A imagem original da arte {index + 1} não está disponível.")

            image_path = _project_asset_path(archive, image_bytes, asset_cache)
            original_path = _project_asset_path(archive, original_bytes, asset_cache)
            edit = dict(item.get("edit") or {})
            background_bytes = edit.pop("background_image_bytes", None)
            if background_bytes is not None and not isinstance(background_bytes, bytes):
                raise ValueError(f"O fundo personalizado da arte {index + 1} possui formato inválido.")
            background_path = (
                _project_asset_path(archive, background_bytes, asset_cache)
                if background_bytes else None
            )
            manifest["items"].append({
                "id": item_id,
                "name": str(item.get("name") or f"Arte {index + 1}"),
                "qty": max(1, min(2000, int(item.get("qty", 1) or 1))),
                "image_path": image_path,
                "original_image_path": original_path,
                "image_digest": hashlib.sha256(image_bytes).hexdigest(),
                "original_image_digest": hashlib.sha256(original_bytes).hexdigest(),
                "background_image_path": background_path,
                "edit": _json_safe(edit),
            })
        archive.writestr(
            PROJECT_MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=1,
        )

    payload = buffer.getvalue()
    if len(payload) > PROJECT_MAX_UPLOAD_BYTES:
        raise ValueError(
            "O projeto ficou maior que 190 MB e ultrapassa o limite de reenvio do aplicativo. "
            "Remova artes que não fazem parte deste trabalho ou salve-as em projetos separados."
        )
    return payload


def atomica_project_export_signature(artwork_items, settings) -> str:
    """Assinatura leve para não oferecer um .atp antigo após alterações no projeto."""
    summary = []
    for item in artwork_items or []:
        edit = dict(item.get("edit") or {})
        background_bytes = edit.pop("background_image_bytes", None)
        summary.append({
            "id": int(item.get("id", 0)),
            "name": str(item.get("name") or ""),
            "qty": int(item.get("qty", 1) or 1),
            "image_digest": str(item.get("image_digest") or hashlib.sha256(item.get("image") or b"").hexdigest()),
            "original_image_digest": str(item.get("original_image_digest") or hashlib.sha256(item.get("original_image") or b"").hexdigest()),
            "background_digest": hashlib.sha256(background_bytes).hexdigest() if background_bytes else "",
            "edit": _json_safe(edit),
        })
    payload = {"settings": _json_safe(settings), "items": summary}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def attach_atomica_project(pdf_bytes: bytes, project_archive: bytes) -> bytes:
    """Mantido apenas para compatibilidade com fluxos internos/arquivos antigos."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.add_attachment(PROJECT_ARCHIVE_NAME, project_archive)
    metadata = dict(reader.metadata or {})
    metadata.update({"/Creator": "Atômica Print Lab", "/Producer": "Atômica Print Lab · PDF reeditável"})
    writer.add_metadata(metadata)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def restore_atomica_project_from_archive(project_bytes: bytes) -> dict:
    """Valida e reconstrói o conteúdo de um pacote .atp ou .atlp."""
    if not isinstance(project_bytes, bytes) or not project_bytes:
        raise ValueError("O arquivo de projeto está vazio.")
    if len(project_bytes) > PROJECT_MAX_UPLOAD_BYTES:
        raise ValueError("O arquivo de projeto ultrapassa o limite de 190 MB do aplicativo.")
    try:
        with zipfile.ZipFile(io.BytesIO(project_bytes), "r") as archive:
            infos = archive.infolist()
            if len(infos) > (PROJECT_MAX_ITEMS * 3 + 2):
                raise ValueError("O projeto contém um número excessivo de arquivos internos.")
            names = {info.filename for info in infos}
            if PROJECT_MANIFEST_NAME not in names:
                raise ValueError("Este arquivo não contém um manifesto de projeto do Atômica Print Lab.")
            manifest_info = archive.getinfo(PROJECT_MANIFEST_NAME)
            if manifest_info.file_size > 10 * 1024 * 1024:
                raise ValueError("O manifesto do projeto excede o limite permitido.")
            manifest = json.loads(archive.read(PROJECT_MANIFEST_NAME).decode("utf-8"))
            if manifest.get("format") != "atomica-print-lab-project":
                raise ValueError("Este arquivo não é um projeto do Atômica Print Lab.")
            schema_version = int(manifest.get("schema_version", 0))
            if schema_version != PROJECT_SCHEMA_VERSION:
                raise ValueError("A versão dos dados deste projeto não é compatível com esta versão do aplicativo.")

            records = manifest.get("items", [])
            if not isinstance(records, list) or not records or len(records) > PROJECT_MAX_ITEMS:
                raise ValueError("O projeto não contém uma lista válida de artes.")

            restored, total_bytes, blob_cache = [], 0, {}
            for record in records:
                blobs = {}
                for key in ("image_path", "original_image_path", "background_image_path"):
                    path = record.get(key)
                    if not path:
                        continue
                    # Os caminhos são nomes internos do ZIP, nunca caminhos do sistema.
                    if (
                        not isinstance(path, str) or path not in names or path.startswith("/")
                        or "\\" in path or ".." in path.split("/")
                    ):
                        raise ValueError("O projeto contém um arquivo de arte ausente ou inválido.")
                    if path not in blob_cache:
                        info = archive.getinfo(path)
                        if info.file_size < 1:
                            raise ValueError("O projeto contém uma imagem vazia.")
                        total_bytes += int(info.file_size)
                        if total_bytes > PROJECT_MAX_UNCOMPRESSED_BYTES:
                            raise ValueError("O projeto excede o limite de 500 MB para restauração.")
                        blob_cache[path] = archive.read(path)
                    blobs[path] = blob_cache[path]

                image_path = record.get("image_path")
                original_path = record.get("original_image_path")
                if not image_path or not original_path or image_path not in blobs or original_path not in blobs:
                    raise ValueError("O projeto não contém a imagem atual e a original de uma das artes.")
                image_bytes = blobs[image_path]
                original_bytes = blobs[original_path]
                image_digest = hashlib.sha256(image_bytes).hexdigest()
                original_digest = hashlib.sha256(original_bytes).hexdigest()
                expected_image_digest = str(record.get("image_digest") or image_digest)
                expected_original_digest = str(record.get("original_image_digest") or original_digest)
                if expected_image_digest != image_digest or expected_original_digest != original_digest:
                    raise ValueError("Uma das imagens do projeto está corrompida ou foi alterada.")

                edit = dict(record.get("edit") or {})
                bg_path = record.get("background_image_path")
                edit["background_image_bytes"] = blobs.get(bg_path) if bg_path else None
                restored.append({
                    "id": int(record.get("id", len(restored) + 1)),
                    "name": str(record.get("name") or f"Arte {len(restored) + 1}"),
                    "image": image_bytes,
                    "image_digest": image_digest,
                    "original_image": original_bytes,
                    "original_image_digest": original_digest,
                    "qty": max(1, min(2000, int(record.get("qty", 1) or 1))),
                    "edit": edit,
                })
            if not restored:
                raise ValueError("O arquivo não contém artes recuperáveis no projeto.")
            return {"settings": manifest.get("settings") or {}, "items": restored}
    except ValueError:
        raise
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
        raise ValueError("Não foi possível ler o arquivo .atp. O arquivo pode estar incompleto ou corrompido.") from exc


def restore_atomica_project_from_atp(project_bytes: bytes) -> dict:
    """Reconhece o formato .atp e delega a validação do pacote interno."""
    if not isinstance(project_bytes, bytes) or not project_bytes.startswith(b"PK"):
        raise ValueError("O arquivo selecionado não parece ser um projeto .atp válido.")
    return restore_atomica_project_from_archive(project_bytes)


def restore_atomica_project_from_pdf(pdf_bytes: bytes) -> dict:
    """Abre projetos embutidos em PDFs antigos, mantendo compatibilidade retroativa."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    candidates = []
    for filename, payloads in reader.attachments.items():
        if str(filename).lower().endswith((".atlp", ".atp", ".zip")):
            candidates.extend(payloads if isinstance(payloads, list) else [payloads])
    for payload in candidates:
        try:
            return restore_atomica_project_from_archive(payload)
        except ValueError:
            continue
    raise ValueError(
        "Este PDF não contém um projeto editável do Atômica Print Lab. "
        "PDFs comuns ou versões antigas sem dados internos não permitem recuperar o Pool e os ajustes originais."
    )


def _apply_pending_atomica_project_restore() -> None:
    pending = st.session_state.pop("_pending_atomica_project_restore", None)
    if not isinstance(pending, dict):
        return
    settings, items = pending.get("settings") or {}, pending.get("items") or []
    size_key = str(settings.get("size_key") or "44 mm")
    if size_key not in BUTTONS:
        size_key = "44 mm"
    base_spec = BUTTONS[size_key]
    shape = str(settings.get("shape") or base_spec.shape)
    if shape != base_spec.shape:
        size_key = next((key for key, value in BUTTONS.items() if value.shape == shape), size_key)
        base_spec = BUTTONS[size_key]
    cut = _clamp_cut_diameter(settings.get("cut_diameter", base_spec.cut), base_spec)
    paper = str(settings.get("paper_size") or "A4")
    if paper not in [*PAPERS, "Personalizado"]:
        paper = "A4"
    orientation = str(settings.get("page_orientation") or "Retrato")
    if orientation not in {"Retrato", "Paisagem"}:
        orientation = "Retrato"
    custom_w = float(settings.get("custom_page_w", 210.0) or 210.0)
    custom_h = float(settings.get("custom_page_h", 297.0) or 297.0)
    raw_w, raw_h = (custom_w, custom_h) if paper == "Personalizado" else PAPERS[paper]
    page_w, page_h = (min(raw_w, raw_h), max(raw_w, raw_h)) if orientation == "Retrato" else (max(raw_w, raw_h), min(raw_w, raw_h))
    layout = str(settings.get("layout_label") or "Grade")
    if layout.startswith("Hexagonal") and shape != "circle":
        layout = "Grade"
    old_ids = {int(item.get("id", -1)) for item in st.session_state.get("artwork_items", []) if isinstance(item, dict)}
    new_ids = {int(item.get("id", -1)) for item in items if isinstance(item, dict)}
    for item_id in old_ids | new_ids:
        prefixes = (f"artwork_{item_id}_", f"artwork_color_picker_{item_id}_")
        exact_keys = {
            f"remove_custom_background_{item_id}", f"reset_image_adjustments_{item_id}",
            f"pool_select_{item_id}", f"pool_batch_select_{item_id}", f"pool_qty_input_{item_id}",
            f"pool_plus_{item_id}", f"pool_minus_{item_id}", f"pool_up_{item_id}",
            f"pool_down_{item_id}", f"pool_remove_{item_id}",
        }
        for key in list(st.session_state.keys()):
            if key in exact_keys or any(str(key).startswith(prefix) for prefix in prefixes):
                st.session_state.pop(key, None)
    st.session_state.pop("artwork_pool_ui", None)
    st.session_state["format_label"] = "Quadrado" if shape == "square" else "Redondo"
    st.session_state["single_size"] = size_key
    st.session_state["paper_size"] = paper
    st.session_state["page_orientation"] = orientation
    st.session_state["custom_page_w"] = custom_w
    st.session_state["custom_page_h"] = custom_h
    st.session_state["margin_mm"] = float(settings.get("margin_mm", 5.0))
    st.session_state["gap_mm"] = float(settings.get("gap_mm", 3.0))
    st.session_state["layout_label"] = layout
    overrides = st.session_state.setdefault("_cut_diameter_overrides", {})
    saved_overrides = settings.get("cut_diameter_overrides", {})
    if isinstance(saved_overrides, dict):
        for saved_key, saved_value in saved_overrides.items():
            if saved_key in BUTTONS:
                overrides[str(saved_key)] = _clamp_cut_diameter(saved_value, BUTTONS[str(saved_key)])
    overrides[size_key] = cut
    st.session_state[_cut_diameter_widget_key(size_key)] = cut
    guides = set(settings.get("guides") or ["cut"])
    for guide in GUIDES:
        st.session_state[f"guide_single_{guide.key}"] = guide.key in guides
    for item in items:
        item.setdefault("thumbnail_data_url", thumbnail_data_url(item["image_digest"], item["image"]))
        item.setdefault("original_image", item["image"])
        item.setdefault("original_image_digest", item["image_digest"])
    st.session_state["artwork_items"] = items
    st.session_state["artwork_next_id"] = max((int(item["id"]) for item in items), default=0) + 1
    selected_id = settings.get("selected_id")
    valid_ids = {int(item["id"]) for item in items}
    st.session_state["artwork_selected_id"] = int(selected_id) if selected_id is not None and int(selected_id) in valid_ids else int(items[0]["id"])
    st.session_state["artwork_batch_selected_ids"] = []
    st.session_state["artwork_upload_revision"] = int(st.session_state.get("artwork_upload_revision", 0)) + 1
    st.session_state["_artwork_upload_processed_signatures"] = []
    st.session_state["_layout_profile_schema"] = 2
    profile_key = layout_settings_profile_key(paper, page_w, page_h, size_key, shape)
    st.session_state["_layout_profile_key"] = profile_key
    profiles = st.session_state.setdefault("_layout_profiles", {})
    profiles[profile_key] = {"margin_mm": st.session_state["margin_mm"], "gap_mm": st.session_state["gap_mm"], "layout_label": layout}
    st.session_state["_atomica_project_restored_notice"] = True


def _sheet_brand_pdf_font_name() -> str:
    """Registra Gochi Hand no ReportLab para incorporá-la ao PDF final."""
    try:
        font_path = brand.sheet_font_path()
        if font_path:
            font_name = "AtomicaGochiHand"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
            return font_name
    except Exception:
        pass
    return "Helvetica"


def _draw_sheet_brand_footer_pil(
    sheet: Image.Image, ppm: float, margin_mm: float = 5.0
) -> None:
    """Desenha a marca dentro da margem já existente, sem reservar área da folha."""
    if float(margin_mm) < SHEET_FOOTER_MIN_MARGIN_MM:
        return
    from PIL import ImageFont
    font_size = max(7, round(2.0 * ppm))
    try:
        # Usa a mesma Gochi Hand da identidade visual. Se ainda não estiver
        # no pacote, branding.py a obtém do repositório oficial do Google Fonts.
        gochi_path = brand.sheet_font_path()
        font = ImageFont.truetype(str(gochi_path), font_size) if gochi_path else ImageFont.truetype("DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    ImageDraw.Draw(sheet).text(
        (sheet.width / 2, sheet.height - 2.7 * ppm),
        "Atômica Print Lab", fill="#626873", font=font, anchor="mm"
    )


# ----------------------------------------------------------------------------
# PDF (ReportLab)
# ----------------------------------------------------------------------------


def configure_pdf_print_size(c: canvas.Canvas) -> None:
    """Solicita ao leitor de PDF imprimir a página sem ajustar ou reduzir.

    A geometria do documento continua definida em milímetros por pagesize.
    Estas preferências ajudam leitores compatíveis a respeitar a escala 1:1;
    a opção manual do usuário ou do driver da impressora ainda pode prevalecer.
    """
    c.setViewerPreference("PrintScaling", "None")
    c.setViewerPreference("PrintArea", "MediaBox")
    c.setViewerPreference("PrintClip", "MediaBox")


def draw_guides(c: canvas.Canvas, cx: float, cy: float, spec: ButtonSpec, enabled, line_pt: float = 0.35):
    """Desenha guias vetoriais; o corte sai preto e contínuo no PDF."""
    for g in GUIDES:
        if g.key not in enabled:
            continue
        c.saveState()
        c.setLineWidth(line_pt)
        if g.key == "cut":
            c.setStrokeColor(black)
            c.setDash()  # linha de corte preta e contínua na exportação
        else:
            c.setStrokeColor(HexColor(g.color))
            if g.style == "dashed":
                c.setDash(2.2 * mm, 1.4 * mm)
            elif g.style == "dotted":
                c.setLineWidth(max(line_pt * 2, 0.8))
                c.setLineCap(1)
                c.setDash(0.01, 1.6 * mm)
        radius = getattr(spec, g.attr) / 2 * mm
        if spec.shape == "square":
            c.rect(cx - radius, cy - radius, 2 * radius, 2 * radius, stroke=1, fill=0)
        else:
            c.circle(cx, cy, radius, stroke=1, fill=0)
        c.restoreState()


def build_pdf(
    spec, img, positions, page_w, page_h, qty, *, zoom, off_x, off_y, bg, guides,
    fit_mode="cut", blur_fill=True, blur_mm=0.5, background_mode=None,
    background_image=None,
) -> bytes:
    # A composição é rasterizada em alta resolução; as linhas permanecem vetoriais.
    artwork = render_artwork(
        spec, img, ppm=PDF_MAX_DPI / 25.4, zoom=zoom, off_x=off_x, off_y=off_y,
        bg=bg, fit_mode=fit_mode, blur_fill=blur_fill, blur_mm=blur_mm,
        background_mode=background_mode, background_image=background_image,
    )
    jpeg = io.BytesIO()
    artwork.save(jpeg, "JPEG", quality=95, subsampling=0)
    jpeg.seek(0)
    reader = ImageReader(jpeg)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w * mm, page_h * mm))
    configure_pdf_print_size(c)
    shape_title = "quadrado" if spec.shape == "square" else "redondo"
    c.setTitle(f"Folha de bottons {shape_title} {spec.size:g} mm")
    c.setCreator("Gerador de folha de bottons")

    # Um único "form" com a arte recortada + guias; é reutilizado em todos os bottons
    r = spec.cut / 2 * mm
    c.beginForm("botton", -r - 2 * mm, -r - 2 * mm, r + 2 * mm, r + 2 * mm)
    c.saveState()
    clip = c.beginPath()
    if spec.shape == "square":
        clip.rect(-r, -r, 2 * r, 2 * r)
    else:
        clip.circle(0, 0, r)
    c.clipPath(clip, stroke=0, fill=0)
    c.drawImage(reader, -r, -r, 2 * r, 2 * r)
    c.restoreState()
    draw_guides(c, 0, 0, spec, guides)
    c.endForm()

    per_page = len(positions)
    remaining = qty
    while remaining > 0:
        for px, py in positions[:remaining]:
            c.saveState()
            c.translate(px * mm, (page_h - py) * mm)
            c.doForm("botton")
            c.restoreState()
        remaining -= per_page
        c.showPage()
    c.save()
    return buf.getvalue()


def build_mixed_pdf(
    items, placements, page_w, page_h, *, zoom, off_x, off_y, bg, guides,
    fit_mode="cut", blur_fill=True, blur_mm=0.5, background_mode=None,
) -> bytes:
    """Gera uma única folha PDF com bottons mistos, cada um com sua própria arte."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w * mm, page_h * mm))
    configure_pdf_print_size(c)
    c.setTitle("Folha personalizada de bottons")
    c.setCreator("Atômica Print Lab · Criar Bottons")

    for index, item in enumerate(items):
        spec, img = item["spec"], item["img"]
        artwork = render_artwork(
            spec, img, ppm=PDF_MAX_DPI / 25.4,
            zoom=item.get("zoom", zoom),
            off_x=item.get("off_x", off_x),
            off_y=item.get("off_y", off_y),
            bg=bg,
            fit_mode=item.get("fit_mode", fit_mode),
            blur_fill=item.get("blur_fill", blur_fill),
            blur_mm=item.get("blur_mm", blur_mm),
            background_mode=item.get("background_mode", background_mode),
            background_image=item.get("background_image", background_image),
        )
        jpeg = io.BytesIO()
        artwork.save(jpeg, "JPEG", quality=95, subsampling=0)
        jpeg.seek(0)
        reader = ImageReader(jpeg)

        radius = spec.cut / 2 * mm
        form_name = f"mixed_button_{index}"
        c.beginForm(form_name, -radius - 2 * mm, -radius - 2 * mm, radius + 2 * mm, radius + 2 * mm)
        c.saveState()
        clip = c.beginPath()
        if spec.shape == "square":
            clip.rect(-radius, -radius, 2 * radius, 2 * radius)
        else:
            clip.circle(0, 0, radius)
        c.clipPath(clip, stroke=0, fill=0)
        c.drawImage(reader, -radius, -radius, 2 * radius, 2 * radius)
        c.restoreState()
        draw_guides(c, 0, 0, spec, guides)
        c.endForm()

    for item_index, px, py in placements:
        c.saveState()
        c.translate(px * mm, (page_h - py) * mm)
        c.doForm(f"mixed_button_{item_index}")
        c.restoreState()
    c.save()
    return buf.getvalue()



def build_art_batch_pdf(
    items, positions, page_w, page_h, total_qty, *, bg, guides, project_archive=None,
    page_margin_mm=5.0,
) -> bytes:
    """Gera um PDF de tamanho único, repetindo cada arte conforme sua quantidade."""
    if not items or not positions or total_qty <= 0:
        return b""

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w * mm, page_h * mm))
    configure_pdf_print_size(c)
    spec = items[0]["spec"]
    shape_title = "quadrado" if spec.shape == "square" else "redondo"
    c.setTitle(f"Folha de bottons {shape_title} {spec.size:g} mm")
    c.setCreator("Atômica Print Lab · Criar Bottons")
    footer_font_name = _sheet_brand_pdf_font_name()

    image_buffers = []
    expanded_order = []
    for index, item in enumerate(items):
        artwork = render_artwork(
            item["spec"], item["img"], ppm=PDF_MAX_DPI / 25.4,
            zoom=item["zoom"], off_x=item["off_x"], off_y=item["off_y"],
            bg=item.get("bg", bg), fit_mode=item["fit_mode"],
            blur_fill=item.get("blur_fill", True), blur_mm=item.get("blur_mm", 0.5),
            background_mode=item.get("background_mode", "color"),
            background_image=item.get("background_image"),
        )
        jpeg = io.BytesIO()
        artwork.save(jpeg, "JPEG", quality=95, subsampling=0)
        jpeg.seek(0)
        image_buffers.append(jpeg)  # mantém os buffers vivos durante a criação do PDF
        reader = ImageReader(jpeg)

        radius = item["spec"].cut / 2 * mm
        form_name = f"art_button_{index}"
        c.beginForm(form_name, -radius - 2 * mm, -radius - 2 * mm, radius + 2 * mm, radius + 2 * mm)
        c.saveState()
        clip = c.beginPath()
        if item["spec"].shape == "square":
            clip.rect(-radius, -radius, 2 * radius, 2 * radius)
        else:
            clip.circle(0, 0, radius)
        c.clipPath(clip, stroke=0, fill=0)
        c.drawImage(reader, -radius, -radius, 2 * radius, 2 * radius)
        c.restoreState()
        draw_guides(c, 0, 0, item["spec"], guides)
        c.endForm()
        expanded_order.extend([index] * max(0, int(item.get("qty", 1))))

    per_page = len(positions)
    page_count = math.ceil(min(total_qty, len(expanded_order)) / per_page)
    for page_index in range(page_count):
        start = page_index * per_page
        page_art_indexes = expanded_order[start:start + per_page]
        for item_index, (px, py) in zip(page_art_indexes, positions):
            c.saveState()
            c.translate(px * mm, (page_h - py) * mm)
            c.doForm(f"art_button_{item_index}")
            c.restoreState()
        # A identificação fica dentro da margem existente. Não altera o tamanho
        # da página nem reduz a área usada para posicionar os bottons.
        if float(page_margin_mm) >= SHEET_FOOTER_MIN_MARGIN_MM:
            c.saveState()
            c.setFont(footer_font_name, 6)
            c.setFillColor(HexColor("#626873"))
            c.drawCentredString((page_w * mm) / 2, 2.0 * mm, "Atômica Print Lab")
            c.restoreState()
        if page_index < page_count - 1:
            c.showPage()

    c.save()
    pdf_bytes = buf.getvalue()
    return attach_atomica_project(pdf_bytes, project_archive) if project_archive else pdf_bytes


@st.cache_data(show_spinner=False)
def make_art_thumbnail(image_bytes: bytes, size: int = 92) -> Image.Image:
    """Cria miniatura quadrada para o pool, sem exibir o nome do arquivo."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        thumb = ImageOps.exif_transpose(source).convert("RGBA")
        thumb.thumbnail((size - 8, size - 8), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size), "white")
    tile.alpha_composite(thumb, ((size - thumb.width) // 2, (size - thumb.height) // 2))
    return tile.convert("RGB")


@st.cache_data(show_spinner=False, max_entries=512, hash_funcs={bytes: lambda _value: 0})
def thumbnail_data_url(image_digest: str, image_bytes: bytes) -> str:
    """Miniatura leve para itens de sessões antigas que ainda não têm thumbnail salvo."""
    return _encode_pool_thumbnail(image_bytes, size=96)


@st.cache_data(show_spinner=False, max_entries=256, hash_funcs={bytes: lambda _value: 0})
def editor_image_data_url(image_digest: str, image_bytes: bytes) -> str:
    """Imagem leve em WebP para o canvas; o original fica reservado à exportação."""
    # O canvas raramente ocupa mais de ~900 px na interface. Uma fonte de
    # 1000 px mantém nitidez suficiente e diminui o tempo de decodificação/
    # transferência ao alternar entre artes pela primeira vez.
    preview = load_image_preview(image_digest, image_bytes, 1000)
    buffer = io.BytesIO()
    # method=0 prioriza velocidade; a exportação de produção continua usando
    # o original em alta resolução, sem qualquer redução.
    preview.save(buffer, format="WEBP", quality=88, method=0)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/webp;base64,{encoded}"


@st.cache_data(show_spinner=False, max_entries=256, hash_funcs={bytes: lambda _value: 0})
def editor_backdrop_data_url(image_digest: str, image_bytes: bytes, bg_hex: str = "#FFFFFF") -> str:
    """Fonte opaca leve para o modo desfocado, codificada somente quando necessária."""
    source = load_image_preview(image_digest, image_bytes, 1000)
    backdrop = _make_backdrop_source(source, bg_hex)
    buffer = io.BytesIO()
    backdrop.save(buffer, format="WEBP", quality=86, method=0)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/webp;base64,{encoded}"


def image_editor_token(
    image_bytes: bytes, spec: ButtonSpec, item_id: int | None = None,
    image_digest: str | None = None,
) -> str:
    """Token exclusivo da arte/perfil; reutiliza o digest já calculado no upload."""
    signature = (
        f"{item_id if item_id is not None else 'legacy'}|{spec.shape}|"
        f"{spec.size}|{spec.cut}|{spec.fold}|{spec.art}|{spec.safe}"
    ).encode("utf-8")
    digest = image_digest or hashlib.sha256(image_bytes).hexdigest()
    return hashlib.sha256(digest.encode("ascii") + signature).hexdigest()


def _component_value(value, name: str):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _store_editor_transform(
    transform, token: str, prefix: str, *, update_widget_flags: bool = False
) -> bool:
    """Persiste o último gesto e sua referência geométrica exata por ID.

    A referência vem do canvas que emitiu o evento, nunca do tamanho do
    selectbox durante o rerun. Isso evita interpretar um arraste feito em 18 mm
    como se tivesse sido feito no tamanho de destino.
    """
    if not isinstance(transform, dict) or transform.get("token") != token:
        return False
    try:
        zoom = float(transform.get("zoom"))
        off_x = float(transform.get("off_x"))
        off_y = float(transform.get("off_y"))
        revision = int(transform.get("revision", st.session_state.get(f"{prefix}_transform_revision", 0)))
        current_seq = int(st.session_state.get(f"{prefix}_event_seq", 0))
        raw_seq = transform.get("event_seq")
        if raw_seq is None:
            current_values = (
                float(st.session_state.get(f"{prefix}_zoom", 1.0)),
                float(st.session_state.get(f"{prefix}_offset_x", 0.0)),
                float(st.session_state.get(f"{prefix}_offset_y", 0.0)),
            )
            if all(abs(a - b) < 1e-8 for a, b in zip((zoom, off_x, off_y), current_values)):
                return False
            event_seq = current_seq + 1
        else:
            event_seq = int(raw_seq)
    except (TypeError, ValueError):
        return False

    current_revision = int(st.session_state.get(f"{prefix}_transform_revision", 0))
    if revision != current_revision or event_seq <= current_seq:
        return False
    if not all(math.isfinite(v) for v in (zoom, off_x, off_y)):
        return False

    zoom = max(0.01, min(20.0, zoom))
    off_x = max(-5000.0, min(5000.0, off_x))
    off_y = max(-5000.0, min(5000.0, off_y))
    st.session_state[f"{prefix}_zoom"] = zoom
    st.session_state[f"{prefix}_offset_x"] = off_x
    st.session_state[f"{prefix}_offset_y"] = off_y
    st.session_state[f"{prefix}_event_seq"] = event_seq

    try:
        item_id = int(str(prefix).removeprefix("artwork_"))
    except (TypeError, ValueError):
        item_id = None
    artwork = _find_artwork_item(item_id) if item_id is not None else None
    if artwork is not None:
        saved = artwork.setdefault("edit", {})
        saved["zoom"] = zoom
        saved["offset_x"] = off_x
        saved["offset_y"] = off_y
        saved["transform_revision"] = revision
        saved["event_seq"] = event_seq

        raw_profile = transform.get("profile")
        profile = None
        if isinstance(raw_profile, dict):
            try:
                candidate = {
                    "size": float(raw_profile.get("size", 0.0) or 0.0),
                    "cut": float(raw_profile["cut"]),
                    "fold": float(raw_profile["fold"]),
                    "art": float(raw_profile["art"]),
                    "safe": float(raw_profile["safe"]),
                    "shape": str(raw_profile.get("shape", "circle")),
                }
                if (
                    candidate["shape"] in {"circle", "square"}
                    and all(math.isfinite(candidate[k]) and candidate[k] > 0
                            for k in ("cut", "fold", "art", "safe"))
                ):
                    profile = candidate
            except (KeyError, TypeError, ValueError):
                profile = None

        if profile is not None:
            saved["scale_reference_spec"] = profile
            saved["scale_reference_diameter"] = profile["safe"]

        raw_fit_mode = str(transform.get("fit_mode", saved.get("fit_basis_mode", "cut"))).lower()
        if raw_fit_mode in {"cut", "art"}:
            saved["fit_basis_mode"] = raw_fit_mode
            if update_widget_flags:
                st.session_state[f"{prefix}_fit_basis_mode"] = raw_fit_mode

        # O canvas envia a normalização já calculada usando as guias que estavam
        # realmente visíveis no instante do gesto. Preferimos esse valor para
        # não depender da ordem dos callbacks do Streamlit.
        raw_relative = transform.get("safety_relative")
        relative = None
        if isinstance(raw_relative, dict):
            try:
                candidate_relative = {
                    "zoom": float(raw_relative["zoom"]),
                    "offset_x": float(raw_relative["offset_x"]),
                    "offset_y": float(raw_relative["offset_y"]),
                }
                if all(math.isfinite(v) for v in candidate_relative.values()):
                    relative = candidate_relative
            except (KeyError, TypeError, ValueError):
                relative = None
        if relative is not None:
            saved["safety_relative_zoom"] = relative["zoom"]
            saved["safety_relative_offset_x"] = relative["offset_x"]
            saved["safety_relative_offset_y"] = relative["offset_y"]
            saved["safety_transform_schema"] = 1
        elif profile is not None:
            _safety_relative_transform(saved, profile)

        if bool(transform.get("manual_edit", False)):
            # Qualquer movimento/redimensionamento transforma o preset inicial
            # em composição manual. Isso deve acontecer mesmo se o callback do
            # componente não for chamado; o registro da arte é a fonte da verdade.
            saved["fit_cut_checked"] = False
            saved["fit_art_checked"] = False
            saved["manual_transform_active"] = True
            if update_widget_flags:
                st.session_state[f"{prefix}_fit_cut_checked"] = False
                st.session_state[f"{prefix}_fit_art_checked"] = False
    return True


def apply_image_editor_state(result, token: str, prefix: str) -> None:
    """Copia o estado do canvas e mantém a transformação no registro da arte."""
    if _store_editor_transform(_component_value(result, "transform"), token, prefix):
        try:
            item_id = int(str(prefix).removeprefix("artwork_"))
        except (TypeError, ValueError):
            return
        item = _find_artwork_item(item_id)
        if item is not None:
            persist_artwork_edit_state(item, prefix)


def _find_artwork_item(item_id: int):
    try:
        target_id = int(item_id)
    except (TypeError, ValueError):
        return None
    return next(
        (entry for entry in st.session_state.get("artwork_items", [])
         if int(entry.get("id", -1)) == target_id),
        None,
    )


def _persist_artwork_fields(item_id: int, fields: tuple[str, ...]) -> None:
    """Persiste somente os campos alterados no registro da arte correspondente."""
    item = _find_artwork_item(item_id)
    if item is None:
        return
    prefix = f"artwork_{int(item_id)}"
    saved = item.setdefault("edit", {})
    for suffix in fields:
        key = f"{prefix}_{suffix}"
        if key in st.session_state:
            saved[suffix] = st.session_state[key]
    if set(fields).intersection({"zoom", "offset_x", "offset_y", "fit_basis_mode", "fit_cut_checked", "fit_art_checked"}):
        _safety_relative_transform(saved, saved.get("scale_reference_spec"))


def _persist_artwork_transform_for_item(item_id: int) -> None:
    """Fallback de troca: confirma somente transformação, nunca os controles de fundo."""
    _persist_artwork_fields(
        int(item_id),
        ("fit_basis_mode", "fit_cut_checked", "fit_art_checked", "zoom", "offset_x",
         "offset_y", "transform_revision", "event_seq"),
    )


def make_artwork_settings_callback(item_id: int, widget_suffix: str):
    """Salva apenas o campo acionado, evitando copiar valores antigos de outros controles."""
    def _save_field():
        _persist_artwork_fields(int(item_id), (widget_suffix,))
    return _save_field


def _batch_activate_art_area_preserving_transform(item: dict, spec: ButtonSpec) -> set[str]:
    """Ativa a área da arte para exibir preenchimentos, preservando ajustes manuais.

    Reproduz a regra da edição individual: se a arte ainda usa o preset Corte,
    passa para o preset Arte; se já está em enquadramento livre/manual, converte
    apenas a base geométrica e preserva tamanho físico e deslocamentos.
    """
    edit = item.setdefault("edit", {})
    changed: set[str] = set()
    cut_checked = bool(edit.get("fit_cut_checked", False))
    art_checked = bool(edit.get("fit_art_checked", False))
    manual = bool(edit.get("manual_transform_active", False)) or not (cut_checked or art_checked)
    current_mode = str(edit.get("fit_basis_mode", "cut")).lower()
    if current_mode not in {"cut", "art"}:
        current_mode = "cut"
    profile = edit.get("scale_reference_spec")
    if not isinstance(profile, dict):
        profile = _button_scale_profile(spec)

    try:
        cut_diameter = float(profile["cut"])
        art_diameter = float(profile["art"])
        safe_diameter = float(profile["safe"])
        profile_valid = all(math.isfinite(v) and v > 0 for v in (cut_diameter, art_diameter, safe_diameter))
    except (KeyError, TypeError, ValueError):
        profile_valid = False
        cut_diameter = art_diameter = safe_diameter = 0.0

    if manual:
        zoom = float(edit.get("zoom", 1.0))
        off_x = float(edit.get("offset_x", 0.0))
        off_y = float(edit.get("offset_y", 0.0))
        if current_mode != "art" and profile_valid:
            try:
                image_width, image_height = get_image_dimensions(
                    str(item.get("image_digest", "")), item.get("image", b"")
                )
                old_basis = float(min(image_width, image_height))
                new_basis = float(max(image_width, image_height))
                if old_basis > 0 and new_basis > 0:
                    zoom *= (cut_diameter / old_basis) / (art_diameter / new_basis)
                else:
                    zoom *= cut_diameter / art_diameter
            except Exception:
                zoom *= cut_diameter / art_diameter
        zoom = max(0.01, min(20.0, zoom))
        edit["fit_basis_mode"] = "art"
        edit["fit_cut_checked"] = False
        edit["fit_art_checked"] = False
        edit["manual_transform_active"] = True
        edit["zoom"] = zoom
        edit["offset_x"] = max(-5000.0, min(5000.0, off_x))
        edit["offset_y"] = max(-5000.0, min(5000.0, off_y))
        if current_mode != "art":
            edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
        changed.update({
            "fit_basis_mode", "fit_cut_checked", "fit_art_checked", "manual_transform_active",
            "zoom", "offset_x", "offset_y", "transform_revision",
        })
    else:
        # Um preset ativo ainda não foi personalizado: mudar Corte -> Arte
        # reproduz a ação explícita do usuário na edição individual.
        if not (art_checked and not cut_checked and current_mode == "art"):
            edit["fit_basis_mode"] = "art"
            edit["fit_cut_checked"] = False
            edit["fit_art_checked"] = True
            edit["manual_transform_active"] = False
            edit["zoom"] = 1.0
            edit["offset_x"] = 0.0
            edit["offset_y"] = 0.0
            edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
            edit["event_seq"] = int(edit.get("event_seq", 0) or 0) + 1
            changed.update({
                "fit_basis_mode", "fit_cut_checked", "fit_art_checked", "manual_transform_active",
                "zoom", "offset_x", "offset_y", "transform_revision", "event_seq",
            })

    edit["scale_reference_spec"] = profile
    edit["scale_reference_diameter"] = float(spec.safe)
    _safety_relative_transform(edit, profile)
    changed.update({"scale_reference_spec", "scale_reference_diameter"})
    return changed


def apply_batch_artwork_settings(
    item_ids: list[int],
    spec: ButtonSpec,
    *,
    framing_mode: str | None = None,
    fill_mode: str | None = None,
    blur_strength: float = BATCH_BLUR_DEFAULT,
    blur_base_color: str | None = None,
    solid_background_color: str | None = None,
    background_color: str | None = None,
    custom_background_payload: tuple[bytes, str, str] | None = None,
    progress_callback=None,
) -> dict:
    """Aplica opções independentes diretamente a todas as artes marcadas.

    Não utiliza a imagem em foco como origem. A própria seleção do Pool define
    os destinos e inclui a arte em foco quando ela estiver marcada.
    """
    selected_ids: list[int] = []
    for value in item_ids:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id not in selected_ids:
            selected_ids.append(item_id)

    if not selected_ids:
        return {"updated": 0, "errors": []}

    framing_mode = framing_mode if framing_mode in {"cut", "art"} else None
    fill_mode = fill_mode if fill_mode in {"blur", "color"} else None

    try:
        blur_strength = max(0.0, min(18.0, float(blur_strength)))
    except (TypeError, ValueError):
        blur_strength = BATCH_BLUR_DEFAULT

    # Compatibilidade com sessões e chamadas antigas que usavam uma única cor.
    legacy_color = background_color if background_color is not None else BATCH_BACKGROUND_DEFAULT

    def _validated_color(value, fallback):
        try:
            ImageColor.getrgb(str(value))
            return str(value).upper()
        except (TypeError, ValueError):
            return fallback

    blur_base_color = _validated_color(
        blur_base_color if blur_base_color is not None else legacy_color,
        BATCH_BACKGROUND_DEFAULT,
    )
    solid_background_color = _validated_color(
        solid_background_color if solid_background_color is not None else legacy_color,
        BATCH_BACKGROUND_DEFAULT,
    )

    bg_data = b""
    bg_digest = ""
    bg_name = ""
    if custom_background_payload is not None:
        try:
            bg_data, bg_digest, bg_name = custom_background_payload
            if not isinstance(bg_data, bytes) or not bg_data:
                raise ValueError("A imagem de fundo personalizada não é válida.")
            actual_digest = hashlib.sha256(bg_data).hexdigest()
            if str(bg_digest) != actual_digest:
                raise ValueError("A imagem de fundo personalizada está corrompida. Selecione o arquivo novamente.")
        except Exception as exc:
            return {"updated": 0, "errors": [str(exc)]}

    # Prioridade defensiva: mesmo que uma sessão antiga guarde flags
    # incompatíveis, o modo Corte vence todos os fundos. Caso contrário,
    # imagem personalizada vence Desfoque/Cor; estes, por sua vez, exigem
    # o enquadramento na área da arte para que o fundo fique visível.
    if framing_mode == "cut":
        fill_mode = None
        bg_data = b""
        bg_digest = ""
        bg_name = ""
    elif bg_data:
        fill_mode = None
    # If a fill/background is requested without an explicit framing command,
    # each artwork is moved to the Arte clipping basis using the same
    # manual-transform-preserving rule as the individual editor.

    by_id = {
        int(item.get("id", -1)): item
        for item in st.session_state.get("artwork_items", [])
    }
    target_items = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
    total = len(target_items)
    if total == 0:
        return {"updated": 0, "errors": []}

    # Confirma movimentos/zoom recentes no canvas antes de aplicar um preset
    # à arte em foco. Esta chamada não define a origem das demais artes.
    current_selected_id = st.session_state.get("artwork_selected_id")
    try:
        focused_is_selected = current_selected_id is not None and int(current_selected_id) in selected_ids
    except (TypeError, ValueError):
        focused_is_selected = False
    if focused_is_selected:
        try:
            _commit_artwork_editor_state(int(current_selected_id))
        except (TypeError, ValueError, KeyError):
            pass

    profile = _button_scale_profile(spec)
    updated = 0
    errors: list[str] = []

    for index, item in enumerate(target_items):
        item_id = int(item.get("id", -1))
        item_name = str(item.get("name", f"Arte {item_id}"))
        edit = item.setdefault("edit", {})
        try:
            if progress_callback:
                progress_callback(
                    index, total, item_name, "Aplicando configurações",
                    int(round(index / max(total, 1) * 100)),
                )

            changed_fields: set[str] = set()

            def _clear_custom_background_layer() -> None:
                edit["custom_background_active"] = False
                edit["custom_background_pending"] = False
                edit["background_image_bytes"] = None
                edit["background_image_digest"] = ""
                edit["background_image_name"] = ""
                edit["custom_background_revision"] = int(edit.get("custom_background_revision", 0) or 0) + 1
                edit["background_upload_revision"] = int(edit.get("background_upload_revision", 0) or 0) + 1
                edit.pop("background_image_previous_transform", None)
                edit.pop("background_image_previous_fill_mode", None)
                edit.pop("background_image_error", None)
                changed_fields.update({
                    "background_image_bytes", "background_image_digest", "background_image_name",
                    "custom_background_active", "custom_background_pending", "custom_background_revision",
                    "background_upload_revision",
                })

            if framing_mode == "cut":
                # Mesmo comportamento da edição individual: o preenchimento
                # fica suspenso, mas cores e intensidade são preservadas.
                previous_mode = str(edit.get("background_mode", "Cor sólida"))
                if previous_mode == "Fundo personalizado":
                    previous_mode = str(edit.get("background_image_previous_fill_mode", "Cor sólida"))
                if previous_mode == "Sem preenchimento":
                    previous_mode = str(edit.get("fit_cut_previous_fill_mode", "Cor sólida"))
                if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
                    previous_mode = "Cor sólida"
                edit["fit_cut_previous_fill_mode"] = previous_mode
                _clear_custom_background_layer()
                edit["background_mode"] = "Sem preenchimento"
                edit.pop("background_image_previous_fill_mode", None)
                edit["fit_basis_mode"] = "cut"
                edit["fit_cut_checked"] = True
                edit["fit_art_checked"] = False
                edit["manual_transform_active"] = False
                edit["zoom"] = 1.0
                edit["offset_x"] = 0.0
                edit["offset_y"] = 0.0
                edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
                edit["event_seq"] = int(edit.get("event_seq", 0) or 0) + 1
                edit["scale_reference_spec"] = profile
                edit["scale_reference_diameter"] = float(spec.safe)
                _safety_relative_transform(edit, profile)
                changed_fields.update({
                    "background_mode", "fit_cut_previous_fill_mode", "fit_basis_mode",
                    "fit_cut_checked", "fit_art_checked", "manual_transform_active",
                    "zoom", "offset_x", "offset_y", "transform_revision", "event_seq",
                    "scale_reference_spec", "scale_reference_diameter",
                })

            elif framing_mode == "art":
                # Preset explícito: aplica o ajuste padrão, como o checkbox individual.
                edit["fit_basis_mode"] = "art"
                edit["fit_cut_checked"] = False
                edit["fit_art_checked"] = True
                edit["manual_transform_active"] = False
                edit["zoom"] = 1.0
                edit["offset_x"] = 0.0
                edit["offset_y"] = 0.0
                edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
                edit["event_seq"] = int(edit.get("event_seq", 0) or 0) + 1
                edit["scale_reference_spec"] = profile
                edit["scale_reference_diameter"] = float(spec.safe)
                _safety_relative_transform(edit, profile)
                changed_fields.update({
                    "fit_basis_mode", "fit_cut_checked", "fit_art_checked", "manual_transform_active",
                    "zoom", "offset_x", "offset_y", "transform_revision", "event_seq",
                    "scale_reference_spec", "scale_reference_diameter",
                })
                if str(edit.get("background_mode", "")) == "Sem preenchimento":
                    restored = str(edit.pop("fit_cut_previous_fill_mode", "Cor sólida"))
                    if restored not in {"Cor sólida", "Imagem desfocada"}:
                        restored = "Cor sólida"
                    edit["background_mode"] = restored
                    changed_fields.update({"background_mode", "fit_cut_previous_fill_mode"})

            elif fill_mode in {"blur", "color"} or bg_data:
                # Sem preset explícito, aplica a mesma transição do editor
                # individual: exige recorte pela guia Arte, mas preserva
                # zoom e deslocamentos caso o usuário esteja em modo manual.
                changed_fields.update(_batch_activate_art_area_preserving_transform(item, spec))

            if framing_mode != "cut" and fill_mode in {"blur", "color"}:
                # Desfoque/Cor substituem o fundo personalizado, mas não alteram
                # a transformação manual da imagem principal.
                if (
                    edit.get("custom_background_active")
                    or edit.get("background_image_bytes")
                    or edit.get("custom_background_pending")
                    or str(edit.get("background_mode", "")) == "Fundo personalizado"
                ):
                    _clear_custom_background_layer()
                edit.pop("fit_cut_previous_fill_mode", None)
                edit.pop("background_image_previous_fill_mode", None)
                if fill_mode == "blur":
                    edit["background_mode"] = "Imagem desfocada"
                    edit["blur_base_color"] = blur_base_color
                    edit["blur_strength"] = blur_strength
                    changed_fields.update({"background_mode", "blur_base_color", "blur_strength"})
                else:
                    edit["background_mode"] = "Cor sólida"
                    edit["background"] = solid_background_color
                    changed_fields.update({"background_mode", "background"})

            elif framing_mode != "cut" and bg_data:
                # A imagem de borda é um modo próprio; o preenchimento anterior
                # permanece guardado para quando a camada for removida.
                previous_mode = str(edit.get("background_mode", "Cor sólida"))
                if previous_mode == "Fundo personalizado":
                    previous_mode = str(edit.get("background_image_previous_fill_mode", "Cor sólida"))
                if previous_mode == "Sem preenchimento":
                    previous_mode = str(edit.get("fit_cut_previous_fill_mode", "Cor sólida"))
                if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
                    previous_mode = "Cor sólida"
                edit["background_image_previous_fill_mode"] = previous_mode
                edit.pop("fit_cut_previous_fill_mode", None)
                edit["background_mode"] = "Fundo personalizado"
                edit["background_image_bytes"] = bg_data
                edit["background_image_digest"] = bg_digest
                edit["background_image_name"] = bg_name or "Fundo personalizado"
                edit["custom_background_active"] = True
                edit["custom_background_pending"] = False
                edit.pop("background_image_previous_transform", None)
                edit["custom_background_revision"] = int(edit.get("custom_background_revision", 0) or 0) + 1
                edit["background_upload_revision"] = int(edit.get("background_upload_revision", 0) or 0) + 1
                edit.pop("background_image_error", None)
                changed_fields.update({
                    "background_mode", "background_image_bytes", "background_image_digest",
                    "background_image_name", "custom_background_active", "custom_background_pending",
                    "background_image_previous_fill_mode", "custom_background_revision", "background_upload_revision",
                })

            # Sincroniza controles da arte em foco antes de os widgets serem
            # instanciados mais abaixo nesta execução do Streamlit.
            prefix = f"artwork_{item_id}"
            for field in changed_fields:
                widget_key = f"{prefix}_{field}"
                if widget_key in st.session_state:
                    st.session_state[widget_key] = edit.get(field)

            updated += 1
            if progress_callback:
                progress_callback(
                    index + 1, total, item_name, "Concluído",
                    int(round((index + 1) / max(total, 1) * 100)),
                )
        except Exception as exc:
            errors.append(f"{item_name}: {exc}")

    if updated:
        st.session_state.pop("focused_art_png_export", None)
        st.session_state.pop("pdf", None)
        st.session_state.pop("_sheet_real_size_preview_cache", None)
        st.session_state.pop("_sheet_preview_cache", None)

    return {"updated": updated, "errors": errors}


def apply_batch_background_removal(
    item_ids: list[int],
    *,
    progress_callback=None,
) -> dict:
    """Remove o fundo por IA de todas as artes marcadas no Pool.

    A ação é independente das demais opções de lote e também processa a arte
    em foco quando ela estiver marcada. Cada imagem usa seu próprio original.
    """
    selected_ids: list[int] = []
    for value in item_ids:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id not in selected_ids:
            selected_ids.append(item_id)

    if not selected_ids:
        return {"updated": 0, "errors": []}

    by_id = {
        int(item.get("id", -1)): item
        for item in st.session_state.get("artwork_items", [])
    }
    target_items = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
    total = len(target_items)
    if total == 0:
        return {"updated": 0, "errors": []}

    current_selected_id = st.session_state.get("artwork_selected_id")
    try:
        focused_is_selected = current_selected_id is not None and int(current_selected_id) in selected_ids
    except (TypeError, ValueError):
        focused_is_selected = False
    if focused_is_selected:
        try:
            _commit_artwork_editor_state(int(current_selected_id))
        except (TypeError, ValueError, KeyError):
            pass

    updated = 0
    errors: list[str] = []
    previous_batch_flag = bool(st.session_state.get("_batch_processing_active", False))
    st.session_state["_batch_processing_active"] = True
    try:
        for index, target in enumerate(target_items):
            target_id = int(target.get("id", -1))
            target_name = str(target.get("name", f"Arte {target_id}"))
            edit = target.setdefault("edit", {})
            edit["remove_background"] = True
            widget_key = f"artwork_{target_id}_remove_background"
            if widget_key in st.session_state:
                st.session_state[widget_key] = True

            # Uma falha anterior grava a assinatura; esta ação explícita tenta
            # novamente em vez de reutilizar o resultado de erro em cache.
            if target.pop("image_processing_error", None):
                target.pop("_processed_image_signature", None)

            if progress_callback:
                progress_callback(index, total, target_name, "Preparando IA", 0)

            def _item_progress(local_percent: int, stage: str) -> None:
                if progress_callback:
                    overall = int(round(((index + max(0, min(100, local_percent)) / 100) / max(total, 1)) * 100))
                    progress_callback(index, total, target_name, stage, overall)

            _sync_processed_artwork(target, progress_callback=_item_progress)
            processing_error = target.get("image_processing_error")
            if processing_error:
                errors.append(f"{target_name}: {processing_error}")
            updated += 1
            if progress_callback:
                overall = int(round((index + 1) / max(total, 1) * 100))
                progress_callback(index + 1, total, target_name, "Concluído", overall)
    finally:
        if previous_batch_flag:
            st.session_state["_batch_processing_active"] = True
        else:
            st.session_state.pop("_batch_processing_active", None)

    if updated:
        st.session_state.pop("focused_art_png_export", None)
        st.session_state.pop("pdf", None)
        st.session_state.pop("_sheet_real_size_preview_cache", None)
        st.session_state.pop("_sheet_preview_cache", None)

    return {"updated": updated, "errors": errors}


def _on_pool_batch_selection_change() -> None:
    if st.session_state.get("_pool_component_batch_clear_pending", False):
        st.session_state["artwork_batch_selected_ids"] = []
        return
    result = st.session_state.get("artwork_pool_ui")
    raw = _component_value(result, "selected_ids")
    if raw is None:
        raw = st.session_state.get("artwork_batch_selected_ids", [])
    valid = {int(item.get("id", -1)) for item in st.session_state.get("artwork_items", [])}
    clean = []
    if isinstance(raw, (list, tuple, set)):
        for value in raw:
            try:
                item_id = int(value)
            except (TypeError, ValueError):
                continue
            if item_id in valid and item_id not in clean:
                clean.append(item_id)
    st.session_state["artwork_batch_selected_ids"] = clean


def _sync_fallback_batch_selection(item_id: int, widget_key: str) -> None:
    selected = bool(st.session_state.get(widget_key, False))
    current = [int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])]
    if selected and int(item_id) not in current:
        current.append(int(item_id))
    elif not selected:
        current = [value for value in current if value != int(item_id)]
    st.session_state["artwork_batch_selected_ids"] = current


def _toggle_all_fallback_batch_selection() -> None:
    valid_ids = [int(item["id"]) for item in st.session_state.get("artwork_items", [])]
    current = set(int(value) for value in st.session_state.get("artwork_batch_selected_ids", []))
    select_all = not (bool(valid_ids) and current == set(valid_ids))
    next_selection = valid_ids if select_all else []
    st.session_state["artwork_batch_selected_ids"] = next_selection
    # Sincroniza também os checkboxes do modo de fallback antes de serem
    # recriados no próximo rerun; evita que a UI mostre uma seleção antiga.
    for item_id in valid_ids:
        widget_key = f"pool_batch_select_{item_id}"
        if widget_key in st.session_state:
            st.session_state[widget_key] = select_all


def _delete_fallback_batch_action() -> None:
    """Abre uma confirmação antes de remover no modo de Pool de fallback."""
    selected = [int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])]
    targets = selected if selected else [int(item["id"]) for item in st.session_state.get("artwork_items", [])]
    st.session_state["_pool_pending_delete_ids"] = targets


def _confirm_fallback_batch_delete() -> None:
    targets = st.session_state.pop("_pool_pending_delete_ids", [])
    removed = _remove_artworks_from_pool(targets)
    if removed:
        st.session_state["_pool_delete_notice"] = f"{removed} arte(s) removida(s) do Pool."


def _cancel_fallback_batch_delete() -> None:
    st.session_state.pop("_pool_pending_delete_ids", None)


def _on_pool_apply_selected_fallback(progress_bar=None, settings: dict | None = None) -> dict:
    """Aplica as opções marcadas a todas as artes selecionadas, inclusive a atual."""
    selected = [int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])]
    spec = BUTTONS.get(str(st.session_state.get("single_size")))
    settings = settings if isinstance(settings, dict) else {}
    has_options = any((
        settings.get("framing_mode") in {"cut", "art"},
        settings.get("fill_mode") in {"blur", "color"},
        settings.get("custom_background_payload") is not None,
    ))
    if spec is None or not selected or not has_options:
        result = {"updated": 0, "errors": []}
    else:
        def _update_progress(done: int, total: int, name: str, stage: str, overall_percent: int | None = None) -> None:
            if progress_bar is None or total <= 0:
                return
            percent = max(0, min(100, int(overall_percent if overall_percent is not None else round((done / total) * 100))))
            current_index = min(done + 1, total) if stage != "Concluído" else max(1, done)
            progress_bar.progress(
                percent,
                text=f"Aplicando ajustes em lote · {percent}% · {stage}: {name} ({current_index}/{total})",
            )

        result = apply_batch_artwork_settings(
            selected,
            spec,
            framing_mode=settings.get("framing_mode"),
            fill_mode=settings.get("fill_mode"),
            blur_strength=settings.get("blur_strength", BATCH_BLUR_DEFAULT),
            blur_base_color=settings.get("blur_base_color", BATCH_BACKGROUND_DEFAULT),
            solid_background_color=settings.get("solid_background_color", BATCH_BACKGROUND_DEFAULT),
            background_color=settings.get("background_color"),  # compatibilidade com chamadas legadas
            custom_background_payload=settings.get("custom_background_payload"),
            progress_callback=_update_progress,
        )

    count = int(result.get("updated", 0))
    errors = result.get("errors") or []
    if count:
        notice = f"Ajustes em lote aplicados a {count} arte(s) selecionada(s), incluindo a arte em foco quando marcada no Pool."
    else:
        notice = "Nenhuma arte recebeu alterações. Confira a seleção no Pool e as opções marcadas."
    if errors:
        notice += f" {len(errors)} arte(s) apresentaram erro; confira os detalhes abaixo."
    st.session_state["_artwork_batch_notice"] = notice
    st.session_state["_artwork_batch_errors"] = errors
    return result


def _on_pool_batch_remove_background_fallback(progress_bar=None) -> dict:
    """Executa remoção por IA em todas as artes marcadas, sem imagem de origem."""
    selected = [int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])]

    def _update_progress(done: int, total: int, name: str, stage: str, overall_percent: int | None = None) -> None:
        if progress_bar is None or total <= 0:
            return
        percent = max(0, min(100, int(overall_percent if overall_percent is not None else round((done / total) * 100))))
        current_index = min(done + 1, total) if stage != "Concluído" else max(1, done)
        progress_bar.progress(
            percent,
            text=f"Remoção de fundo por IA · {percent}% · {stage}: {name} ({current_index}/{total})",
        )

    result = apply_batch_background_removal(
        selected,
        progress_callback=_update_progress,
    )
    count = int(result.get("updated", 0))
    errors = result.get("errors") or []
    if count:
        notice = f"Remoção de fundo por IA executada em {count} arte(s) selecionada(s), incluindo a arte em foco quando marcada no Pool."
    else:
        notice = "Nenhuma arte recebeu remoção. Marque as imagens desejadas no Pool."
    if errors:
        notice += f" {len(errors)} arte(s) apresentaram erro; confira os detalhes abaixo."
    st.session_state["_artwork_batch_notice"] = notice
    st.session_state["_artwork_batch_errors"] = errors
    return result


def _finalize_successful_batch_operation(result: dict, operation_label: str) -> bool:
    """Limpa a seleção e reinicia o painel somente após sucesso integral.

    Se houver qualquer erro, mantém a seleção para permitir conferência/repetição.
    A arte em foco (moldura verde) nunca é alterada por esta função.
    """
    if not isinstance(result, dict):
        return False
    try:
        updated = int(result.get("updated", 0) or 0)
    except (TypeError, ValueError):
        updated = 0
    errors = result.get("errors") or []
    if updated <= 0 or errors:
        return False

    selected_ids = list(st.session_state.get("artwork_batch_selected_ids", []))
    st.session_state["artwork_batch_selected_ids"] = []
    # O componente visual é controlado pelo estado enviado pelo Python. Este
    # sinal impede que um valor antigo do componente reintroduza a seleção.
    st.session_state["_pool_component_batch_clear_pending"] = True
    # Os toggles touch do fallback leem essas chaves diretamente; zerá-las
    # antes do próximo rerun atualiza os símbolos das caixas no Pool.
    st.session_state["_pool_fallback_batch_clear_pending"] = True
    # Uma nova chave para o contêiner recria o expander recolhido no próximo run.
    st.session_state["_artwork_batch_panel_reset_version"] = int(
        st.session_state.get("_artwork_batch_panel_reset_version", 0) or 0
    ) + 1

    notice = st.session_state.get("_artwork_batch_notice")
    if notice:
        st.session_state["_artwork_batch_notice"] = (
            f"{operation_label} concluído em {updated} arte(s). O painel foi recolhido e "
            f"{len(selected_ids)} seleção(ões) foram desmarcadas no Pool. A arte em foco foi preservada."
        )
    return True


def make_batch_color_picker_callback(component_key: str, color_key: str):
    """Persiste a cor escolhida nos controles globais do ajuste em lote."""
    def _save_color():
        result = st.session_state.get(component_key)
        color = _component_value(result, "color")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            return
        st.session_state[color_key] = color.upper()
    return _save_color


def make_artwork_color_picker_callback(
    component_key: str,
    color_key: str,
    item_id: int,
    edit_field: str,
):
    # Persiste a cor emitida pelo seletor integrado, seja pelo picker ou conta-gotas.
    def _save_color():
        result = st.session_state.get(component_key)
        color = _component_value(result, "color")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            return
        color = color.upper()
        st.session_state[color_key] = color
        _persist_artwork_fields(int(item_id), (edit_field,))
    return _save_color


@st.cache_resource(show_spinner=False)
def get_artwork_integrated_color_picker_component():
    """Usa o seletor nativo do navegador, com conta-gotas no Chromium desktop."""
    html = r'''<div class="atomica-integrated-color" id="atomica-integrated-color">
      <input class="atomica-integrated-color-input" id="atomica-integrated-color-input"
        type="color" value="#FFFFFF" aria-label="Selecionar cor">
      <span class="atomica-integrated-color-value" id="atomica-integrated-color-value">#FFFFFF</span>
    </div>'''
    css = r'''
    .atomica-integrated-color {
      display:flex; align-items:center; gap:12px; width:100%; min-height:46px;
      padding:6px 9px; box-sizing:border-box; border:1px solid #55718E;
      border-radius:10px; background:rgba(15,34,55,.82); color:#F4F7FB;
      font-family:inherit;
    }
    .atomica-integrated-color-input {
      flex:0 0 48px; width:48px; height:34px; padding:2px; cursor:pointer;
      border:1px solid rgba(255,255,255,.34); border-radius:8px; background:#FFFFFF;
    }
    .atomica-integrated-color-value {
      flex:1; min-width:0; font:700 13px/1.2 ui-monospace,Consolas,monospace;
      color:#EAF3FC; letter-spacing:.045em;
    }
    '''
    js = r'''
    export default function(component) {
      const { parentElement, data, setTriggerValue } = component;
      const root = parentElement.querySelector('#atomica-integrated-color');
      if (!root) return;
      const input = root.querySelector('#atomica-integrated-color-input');
      const value = root.querySelector('#atomica-integrated-color-value');
      const normalise = raw => {
        const text = String(raw || '').trim().toUpperCase();
        return /^#[0-9A-F]{6}$/.test(text) ? text : '#FFFFFF';
      };
      const sync = raw => {
        const color = normalise(raw);
        if (input.value.toUpperCase() !== color) input.value = color;
        value.textContent = color;
      };
      sync(data?.color);
      if (root.dataset.initialized !== 'true') {
        // O seletor nativo do Chrome/Edge desktop já oferece conta-gotas
        // na própria janela de escolha de cor. Não há um segundo botão.
        input.addEventListener('change', () => {
          const color = normalise(input.value);
          sync(color);
          setTriggerValue('color', color);
        });
        root.dataset.initialized = 'true';
      }
    }
    '''
    return st.components.v2.component(
        "atomica_artwork_integrated_color_picker", html=html, css=css, js=js
    )

@st.cache_resource(show_spinner=False)
def get_sheet_real_size_viewer_component():
    """Visualizador de folha em tela cheia com escala física calibrável."""
    html = r"""
    <div class="atomica-real-size-root" id="atomica-real-size-root">
      <button type="button" class="atomica-real-size-open" id="atomica-real-size-open">
        ⛶  Ver em tela cheia · tamanho real
      </button>
      <span class="atomica-real-size-status" id="atomica-real-size-status"></span>
      <div class="atomica-real-size-overlay" id="atomica-real-size-overlay" aria-hidden="true">
        <header class="atomica-real-size-toolbar">
          <div class="atomica-real-size-heading">
            <strong>Visualização em tamanho real</strong>
            <span id="atomica-real-size-meta">Folha · escala física calibrável</span>
          </div>
          <div class="atomica-real-size-calibration">
            <label for="atomica-real-size-scale">Calibração física da tela <output id="atomica-real-size-scale-value">100,00%</output></label>
            <div class="atomica-real-size-calibration-stepper" aria-label="Ajuste fino da calibração">
              <button type="button" id="atomica-real-size-scale-minus" aria-label="Diminuir calibração">−</button>
              <span>Incremento: 0,01%</span>
              <button type="button" id="atomica-real-size-scale-plus" aria-label="Aumentar calibração">+</button>
            </div>
            <input id="atomica-real-size-scale" type="range" min="0.50" max="2.00" step="0.0001" value="1">
            <small>Ajuste a régua de referência para medir exatamente 100 mm. Use − e + para correções finas de 0,01%. A mesma escala será aplicada a todos os tamanhos.</small>
            <div class="atomica-real-size-ruler-wrap">
              <svg class="atomica-real-size-ruler" id="atomica-real-size-ruler" viewBox="0 0 1000 24" preserveAspectRatio="none" role="img" aria-label="Régua de referência de 100 milímetros">
                <line x1="0" y1="22" x2="1000" y2="22" />
                <g id="atomica-real-size-ruler-ticks"></g>
              </svg>
              <div class="atomica-real-size-ruler-labels"><span>0</span><span>50 mm</span><span>100 mm</span></div>
            </div>
            <details class="atomica-real-size-checks">
              <summary>Conferir diâmetros de corte dos bottons de 44 mm e 58 mm</summary>
              <div class="atomica-real-size-checks-row">
                <div class="atomica-real-size-check-item">
                  <span class="atomica-real-size-check-circle" id="atomica-real-size-check-54"></span>
                  <b>54 mm · corte do botton 44 mm</b>
                </div>
                <div class="atomica-real-size-check-item">
                  <span class="atomica-real-size-check-circle" id="atomica-real-size-check-70"></span>
                  <b>70 mm · corte do botton 58 mm</b>
                </div>
              </div>
              <small>Os círculos representam o diâmetro externo de CORTE (54 e 70 mm), não a face nominal da arte. Ambos devem coincidir com os respectivos gabaritos físicos usando a mesma calibração.</small>
            </details>
          </div>
          <button type="button" class="atomica-real-size-close" id="atomica-real-size-close">Fechar ×</button>
        </header>
        <main class="atomica-real-size-stage" id="atomica-real-size-stage">
          <img class="atomica-real-size-page" id="atomica-real-size-page" alt="Folha de bottons em escala física">
        </main>
      </div>
    </div>
    """
    css = r"""
    .atomica-real-size-root { width:100%; min-height:42px; box-sizing:border-box; font-family:inherit; }
    .atomica-real-size-open {
      display:inline-flex; align-items:center; justify-content:center; gap:8px;
      min-height:40px; padding:8px 15px; border:1px solid #4A779E; border-radius:9px;
      background:#173653; color:#F4F7FB; font-family:inherit; font-size:13px;
      font-weight:750; cursor:pointer; transition:background .15s,border-color .15s;
    }
    .atomica-real-size-open:hover { background:#24517A; border-color:#83B8E8; }
    .atomica-real-size-status { display:block; margin-top:5px; color:#CFA76B; font-size:11px; }
    .atomica-real-size-status:empty { display:none; }
    .atomica-real-size-overlay {
      display:none; position:fixed; inset:0; z-index:2147483000; width:100vw; height:100vh;
      box-sizing:border-box; flex-direction:column; overflow:hidden; background:#101923; color:#F4F7FB;
      font-family:Arial, sans-serif;
    }
    .atomica-real-size-overlay.is-open { display:flex; }
    .atomica-real-size-overlay:fullscreen { width:100vw; height:100vh; background:#101923; }
    .atomica-real-size-toolbar {
      flex:0 0 auto; display:flex; flex-wrap:wrap; align-items:center; gap:16px;
      padding:11px 16px; box-sizing:border-box; background:#172A3F; border-bottom:1px solid #35516D;
      box-shadow:0 4px 15px rgba(0,0,0,.22);
    }
    .atomica-real-size-heading { min-width:190px; display:flex; flex-direction:column; gap:4px; }
    .atomica-real-size-heading strong { color:#F4F7FB; font-size:15px; }
    .atomica-real-size-heading span { color:#B7C8D9; font-size:11px; }
    .atomica-real-size-calibration { display:flex; flex:1 1 370px; flex-direction:column; gap:4px; min-width:260px; }
    .atomica-real-size-calibration label { color:#E7F0F8; font-size:12px; font-weight:700; }
    .atomica-real-size-calibration output { color:#69D3E5; margin-left:7px; font-variant-numeric:tabular-nums; }
    .atomica-real-size-calibration-stepper { display:flex; align-items:center; gap:9px; width:min(100%,390px); }
    .atomica-real-size-calibration-stepper button {
      display:inline-flex; align-items:center; justify-content:center; width:31px; height:28px;
      border:1px solid #547995; border-radius:6px; background:#203E59; color:#F4F7FB;
      font-size:19px; line-height:1; font-weight:700; cursor:pointer;
    }
    .atomica-real-size-calibration-stepper button:hover { background:#2B587A; border-color:#80B8D4; }
    .atomica-real-size-calibration-stepper span { color:#AFC4D6; font-size:10px; flex:1; }
    .atomica-real-size-calibration input { width:min(100%,390px); accent-color:#26C6DA; margin:0; }
    .atomica-real-size-calibration small { color:#B7C8D9; font-size:10.5px; line-height:1.35; }
    .atomica-real-size-ruler-wrap { display:flex; flex-direction:column; align-items:flex-start; gap:2px; }
    .atomica-real-size-ruler { display:block; width:1px; height:24px; overflow:visible; }
    .atomica-real-size-ruler line { stroke:#F7D77B; stroke-width:1.15; vector-effect:non-scaling-stroke; stroke-linecap:square; }
    .atomica-real-size-ruler-labels { display:flex; justify-content:space-between; width:min(100%,390px); color:#F7D77B; font-size:9px; font-variant-numeric:tabular-nums; }
    .atomica-real-size-checks { margin-top:3px; color:#D6E3EF; font-size:10.5px; }
    .atomica-real-size-checks summary { cursor:pointer; color:#86D8E6; font-weight:700; }
    .atomica-real-size-checks-row { display:flex; align-items:flex-start; flex-wrap:wrap; gap:16px; padding:9px 2px 5px; }
    .atomica-real-size-check-item { display:flex; flex-direction:column; align-items:center; gap:4px; color:#F7D77B; }
    .atomica-real-size-check-circle { display:block; box-sizing:border-box; flex:none; border:1.5px solid #69D3E5; border-radius:50%; background:transparent; }
    .atomica-real-size-check-item b { font-size:10px; }
    .atomica-real-size-close {
      flex:0 0 auto; min-height:37px; padding:7px 13px; border:1px solid #8D6370;
      border-radius:8px; background:#542D39; color:#FFF; font-size:12px; font-weight:700; cursor:pointer;
    }
    .atomica-real-size-close:hover { background:#713A49; }
    .atomica-real-size-stage { flex:1 1 auto; min-height:0; width:100%; overflow:auto; box-sizing:border-box; padding:18px; text-align:center; }
    .atomica-real-size-page {
      display:block; flex:none; max-width:none !important; max-height:none !important;
      width:auto; height:auto; margin:0 auto 18px; border:1px solid #B8C0C9;
      background:#FFF; box-shadow:0 10px 36px rgba(0,0,0,.35); image-rendering:auto;
    }
    @media (max-width:700px) {
      .atomica-real-size-toolbar { gap:10px; padding:9px; }
      .atomica-real-size-heading { min-width:150px; flex:1 1 100%; }
      .atomica-real-size-calibration { min-width:210px; }
      .atomica-real-size-close { margin-left:auto; }
      .atomica-real-size-stage { padding:8px; }
    }
    """
    js = r"""
    export default function(component) {
      const { parentElement, data } = component;
      const root = parentElement.querySelector('#atomica-real-size-root');
      if (!root) return;
      const openButton = root.querySelector('#atomica-real-size-open');
      const status = root.querySelector('#atomica-real-size-status');
      const overlay = root.querySelector('#atomica-real-size-overlay');
      const closeButton = root.querySelector('#atomica-real-size-close');
      const image = root.querySelector('#atomica-real-size-page');
      const slider = root.querySelector('#atomica-real-size-scale');
      const scaleValue = root.querySelector('#atomica-real-size-scale-value');
      const ruler = root.querySelector('#atomica-real-size-ruler');
      const rulerTicks = root.querySelector('#atomica-real-size-ruler-ticks');
      const rulerLabels = root.querySelector('.atomica-real-size-ruler-labels');
      const scaleMinus = root.querySelector('#atomica-real-size-scale-minus');
      const scalePlus = root.querySelector('#atomica-real-size-scale-plus');
      const check54 = root.querySelector('#atomica-real-size-check-54');
      const check70 = root.querySelector('#atomica-real-size-check-70');
      const meta = root.querySelector('#atomica-real-size-meta');
      const storageKey = 'atomica_printlab_real_size_calibration_v1';
      // A folha é apresentada em CSS px na referência de 96 px/pol.;
      // a calibração ajusta um único fator linear para todas as dimensões.
      const pxPerMm = 96 / 25.4;
      const SCALE_MIN = 0.5;
      const SCALE_MAX = 2.0;
      const SCALE_STEP = 0.0001; // 0,01% por clique nos controles finos.
      const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

      function buildRulerTicks() {
        if (!rulerTicks || rulerTicks.childElementCount) return;
        const ns = 'http://www.w3.org/2000/svg';
        for (let mm = 0; mm <= 100; mm += 1) {
          const tick = document.createElementNS(ns, 'line');
          const x = mm * 10; // viewBox de 1000 unidades para 100 mm.
          const isMajor = mm % 10 === 0;
          const isMid = mm % 5 === 0;
          tick.setAttribute('x1', String(x));
          tick.setAttribute('x2', String(x));
          tick.setAttribute('y1', isMajor ? '2' : (isMid ? '8' : '14'));
          tick.setAttribute('y2', '22');
          // Todos os traços usam a mesma espessura vetorial; só o comprimento muda.
          tick.setAttribute('stroke-width', '1.15');
          rulerTicks.appendChild(tick);
        }
      }

      function readSavedScale() {
        try {
          const saved = Number(window.localStorage.getItem(storageKey));
          return Number.isFinite(saved) && saved >= 0.5 && saved <= 2 ? saved : 1;
        } catch (_) { return 1; }
      }
      function applyScale() {
        const scale = clamp(Number(slider.value) || 1, 0.5, 2);
        const pageW = Math.max(1, Number(root.dataset.pageWmm) || 210);
        const pageH = Math.max(1, Number(root.dataset.pageHmm) || 297);
        image.style.width = `${pageW * pxPerMm * scale}px`;
        image.style.height = `${pageH * pxPerMm * scale}px`;
        // Referência vetorial exata de 100 mm, com traços de espessura uniforme.
        ruler.style.width = `${100 * pxPerMm * scale}px`;
        if (rulerLabels) rulerLabels.style.width = ruler.style.width;
        if (check54) {
          const cut54 = 54 * pxPerMm * scale;
          check54.style.width = `${cut54}px`;
          check54.style.height = `${cut54}px`;
        }
        if (check70) {
          const cut70 = 70 * pxPerMm * scale;
          check70.style.width = `${cut70}px`;
          check70.style.height = `${cut70}px`;
        }
        slider.value = String(scale);
        scaleValue.textContent = `${(scale * 100).toFixed(2).replace('.', ',')}%`;
        try { window.localStorage.setItem(storageKey, String(scale)); } catch (_) {}
      }
      function stepScale(direction) {
        const current = clamp(Number(slider.value) || 1, SCALE_MIN, SCALE_MAX);
        const next = clamp(current + (direction * SCALE_STEP), SCALE_MIN, SCALE_MAX);
        slider.value = String(next);
        applyScale();
      }
      function closeViewer() {
        overlay.classList.remove('is-open');
        overlay.setAttribute('aria-hidden', 'true');
      }

      if (root.dataset.initialized !== 'true') {
        slider.value = String(readSavedScale());
        buildRulerTicks();
        slider.addEventListener('input', applyScale);
        scaleMinus.addEventListener('click', () => stepScale(-1));
        scalePlus.addEventListener('click', () => stepScale(1));
        closeButton.addEventListener('click', () => {
          if (document.fullscreenElement) {
            const promise = document.exitFullscreen?.();
            if (promise?.catch) promise.catch(() => closeViewer());
          } else closeViewer();
        });
        openButton.addEventListener('click', () => {
          if (!image.src || image.src === window.location.href) {
            status.textContent = 'A prévia em tamanho real ainda está sendo preparada.';
            return;
          }
          overlay.classList.add('is-open');
          overlay.setAttribute('aria-hidden', 'false');
          applyScale();
          status.textContent = '';
          if (typeof overlay.requestFullscreen === 'function') {
            // A chamada acontece diretamente no gesto do usuário para respeitar
            // a política de ativação exigida pela API de tela cheia.
            try {
              const promise = overlay.requestFullscreen();
              if (promise?.catch) promise.catch(() => {
                closeViewer();
                status.textContent = 'O navegador bloqueou a tela cheia. Verifique as permissões do navegador.';
              });
            } catch (error) {
              closeViewer();
              status.textContent = 'Não foi possível abrir a tela cheia neste navegador.';
            }
          } else {
            closeViewer();
            status.textContent = 'Este navegador não oferece suporte à visualização em tela cheia.';
          }
        });
        document.addEventListener('fullscreenchange', () => {
          if (!document.fullscreenElement) closeViewer();
        });
        root.dataset.initialized = 'true';
      }

      const signature = String(data?.signature || '');
      if (signature && root.dataset.signature !== signature) {
        root.dataset.pageWmm = String(Number(data?.page_w_mm) || 210);
        root.dataset.pageHmm = String(Number(data?.page_h_mm) || 297);
        image.src = String(data?.image_data_url || '');
        image.alt = `Folha ${data?.page_label || ''} em escala física calibrada`;
        meta.textContent = `${data?.page_label || 'Folha'} · ${data?.page_w_mm || 210} × ${data?.page_h_mm || 297} mm · Botton ${data?.button_size_mm || 44} mm · Corte ${data?.cut_mm || 54} mm`;
        root.dataset.signature = signature;
        applyScale();
      }
    }
    """
    return st.components.v2.component(
        "atomica_sheet_real_size_viewer", html=html, css=css, js=js
    )


@st.cache_resource(show_spinner=False)
def get_pdf_print_component():
    """Botão único que solicita a impressão do PDF sem abrir prévia ou nova aba."""
    html = (
        '<div class="atomica-pdf-print-root" id="atomica-pdf-print-root">'
        '<button type="button" class="atomica-pdf-print-button" id="atomica-pdf-print-button">'
        'Imprimir PDF</button>'
        '<span class="atomica-pdf-print-status" id="atomica-pdf-print-status" aria-live="polite"></span>'
        '<iframe class="atomica-pdf-print-frame" id="atomica-pdf-print-frame" '
        'title="Documento preparado para impressão" aria-hidden="true" tabindex="-1"></iframe>'
        '</div>'
    )
    css = r"""
    .atomica-pdf-print-root { width:100%; min-height:42px; color:#F4F7FB; font-family:inherit; }
    .atomica-pdf-print-button { border:1px solid #4F9D69; border-radius:9px; background:#17613A; color:#FFFFFF;
      min-height:40px; padding:8px 16px; font-family:inherit; font-size:13px; font-weight:700; cursor:pointer; }
    .atomica-pdf-print-button:hover { background:#207A4A; }
    .atomica-pdf-print-button:disabled { opacity:.55; cursor:wait; }
    .atomica-pdf-print-status { display:block; margin-top:5px; color:#D5A45A; font-size:12px; }
    .atomica-pdf-print-status:empty { display:none; }
    /* Mantém o PDF carregado sem exibir uma sessão de pré-visualização. */
    .atomica-pdf-print-frame { position:fixed !important; left:-10000px !important; top:0 !important;
      display:block !important; width:1px !important; height:1px !important; opacity:0 !important;
      border:0 !important; pointer-events:none !important; }
    """
    js = r"""
    export default function(component) {
      const { parentElement, data } = component;
      const root = parentElement.querySelector('#atomica-pdf-print-root');
      if (!root) return;
      const button = root.querySelector('#atomica-pdf-print-button');
      const status = root.querySelector('#atomica-pdf-print-status');
      const frame = root.querySelector('#atomica-pdf-print-frame');
      const signature = String(data?.signature || '');
      const encoded = String(data?.pdf_base64 || '');

      if (!signature || !encoded) {
        button.disabled = true;
        status.textContent = 'Gere o PDF para habilitar a impressão.';
        return;
      }

      if (root.dataset.signature !== signature) {
        button.disabled = true;
        status.textContent = 'Preparando impressão…';
        try {
          const binary = atob(encoded);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          const blob = new Blob([bytes], { type: 'application/pdf' });
          const oldUrl = root.dataset.pdfUrl;
          const url = URL.createObjectURL(blob);

          root.dataset.signature = signature;
          root.dataset.pdfUrl = url;
          root.dataset.ready = 'false';
          frame.onload = () => {
            root.dataset.ready = 'true';
            button.disabled = false;
            status.textContent = '';
            if (oldUrl) URL.revokeObjectURL(oldUrl);
          };
          frame.onerror = () => {
            root.dataset.ready = 'false';
            button.disabled = true;
            status.textContent = 'Não foi possível preparar o PDF. Baixe o arquivo e use Ctrl+P.';
            if (url) URL.revokeObjectURL(url);
          };
          frame.src = url;
        } catch (error) {
          button.disabled = true;
          status.textContent = 'Não foi possível preparar o PDF para impressão.';
          return;
        }
      } else {
        button.disabled = root.dataset.ready !== 'true';
      }

      // Executa print() de forma síncrona no clique do usuário. O PDF fica em
      // um iframe invisível; não há nova aba nem seção de visualização.
      button.onclick = () => {
        if (root.dataset.ready !== 'true') return;
        try {
          const printWindow = frame.contentWindow;
          if (!printWindow || typeof printWindow.print !== 'function') {
            throw new Error('Janela de impressão indisponível');
          }
          printWindow.focus();
          printWindow.print();
        } catch (error) {
          status.textContent = 'O navegador não permitiu imprimir diretamente. Use Baixar PDF e pressione Ctrl+P.';
        }
      };
    }
    """
    return st.components.v2.component(
        "atomica_pdf_print_direct", html=html, css=css, js=js
    )


def _normalize_artwork_item_ids() -> None:
    """Garante IDs únicos e um próximo ID acima de todos os registros existentes."""
    items = st.session_state.get("artwork_items", [])
    seen: set[int] = set()
    try:
        next_id = max(1, int(st.session_state.get("artwork_next_id", 1) or 1))
    except (TypeError, ValueError):
        next_id = 1
    for item in items:
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            item_id = -1
        if item_id < 1 or item_id in seen:
            while next_id in seen:
                next_id += 1
            item_id = next_id
            item["id"] = item_id
            next_id += 1
        else:
            item["id"] = item_id
        seen.add(item_id)
        next_id = max(next_id, item_id + 1)
        image_bytes = item.get("image", b"")
        digest = item.get("image_digest")
        if image_bytes and (not isinstance(digest, str) or len(digest) != 64):
            item["image_digest"] = hashlib.sha256(image_bytes).hexdigest()
    st.session_state["artwork_next_id"] = next_id


def _remove_artworks_from_pool(item_ids) -> int:
    """Remove um conjunto de artes de uma só vez, mantendo a seleção coerente."""
    targets = set()
    if isinstance(item_ids, (list, tuple, set)):
        raw_ids = item_ids
    else:
        raw_ids = [item_ids]
    for value in raw_ids:
        try:
            targets.add(int(value))
        except (TypeError, ValueError):
            continue
    if not targets:
        return 0

    items = st.session_state.get("artwork_items", [])
    existing_ids = {int(item.get("id", -1)) for item in items}
    targets.intersection_update(existing_ids)
    if not targets:
        return 0

    try:
        selected_id = int(st.session_state.get("artwork_selected_id"))
    except (TypeError, ValueError):
        selected_id = None
    if selected_id in targets:
        try:
            _commit_artwork_editor_state(selected_id)
        except (NameError, TypeError, ValueError):
            _persist_artwork_transform_for_item(selected_id)

    st.session_state["artwork_items"] = [
        item for item in items if int(item.get("id", -1)) not in targets
    ]
    batch_ids = st.session_state.get("artwork_batch_selected_ids", [])
    st.session_state["artwork_batch_selected_ids"] = [
        int(value) for value in batch_ids if int(value) not in targets
    ]
    if selected_id in targets:
        remaining = st.session_state["artwork_items"]
        st.session_state["artwork_selected_id"] = int(remaining[0]["id"]) if remaining else None
    return len(targets)


def _remove_artwork_from_pool(item_id: int | None) -> bool:
    """Compatibilidade com o botão X individual; operação idempotente."""
    return bool(_remove_artworks_from_pool([item_id]))


def _on_pool_delete_ids_change() -> None:
    """Exclusão em lote/todas as artes, com confirmação no próprio componente."""
    result = st.session_state.get("artwork_pool_ui")
    payload = _component_value(result, "delete_ids")
    nonce = 0
    if isinstance(payload, dict):
        raw_ids = payload.get("ids", [])
        try:
            nonce = int(payload.get("nonce", 0) or 0)
        except (TypeError, ValueError):
            nonce = 0
    else:
        raw_ids = payload if isinstance(payload, (list, tuple, set)) else []
    removed = _remove_artworks_from_pool(raw_ids)
    if nonce:
        st.session_state["_artwork_pool_delete_ack"] = nonce
    if removed:
        st.session_state["_pool_delete_notice"] = f"{removed} arte(s) removida(s) do Pool."


def _on_pool_remove_art_change() -> None:
    """Processa a ação de exclusão no callback, antes do rerun principal."""
    result = st.session_state.get("artwork_pool_ui")
    remove_id = _component_value(result, "remove_id")
    _remove_artwork_from_pool(remove_id)


def _sync_pool_quantity_from_widget(item_id: int, widget_key: str) -> None:
    """Persiste a quantidade digitada no contador nativo de fallback."""
    try:
        value = max(1, min(2000, int(st.session_state.get(widget_key, 1))))
    except (TypeError, ValueError):
        value = 1
    for item in st.session_state.get("artwork_items", []):
        if int(item.get("id", -1)) == int(item_id):
            item["qty"] = value
            st.session_state["_skip_saved_pool_quantities_once"] = True
            break


def _step_pool_quantity(item_id: int, delta: int, widget_key: str) -> None:
    """Callback dos botões +/- do fallback, sincronizando o campo numérico."""
    for item in st.session_state.get("artwork_items", []):
        if int(item.get("id", -1)) == int(item_id):
            value = max(1, min(2000, int(item.get("qty", 1)) + int(delta)))
            item["qty"] = value
            # Callback roda antes da criação do number_input neste rerun.
            st.session_state[widget_key] = value
            st.session_state["_skip_saved_pool_quantities_once"] = True
            break


def _fill_fallback_pool_sheet(item_id: int, amount: int) -> None:
    """Completa a folha no fallback nativo sem ser sobrescrito por estado antigo."""
    for item in st.session_state.get("artwork_items", []):
        if int(item.get("id", -1)) == int(item_id):
            item["qty"] = max(1, min(2000, int(item.get("qty", 1)) + int(amount)))
            st.session_state["_skip_saved_pool_quantities_once"] = True
            break


def remaining_slots_to_fill(page_capacity: int, total_quantity: int) -> int:
    """Unidades necessárias para completar a última folha (ou iniciar a próxima)."""
    capacity = max(0, int(page_capacity))
    if capacity == 0:
        return 0
    return capacity - (max(0, int(total_quantity)) % capacity)


def _restore_individual_fill_after_cut(edit: dict, prefix: str) -> str:
    """Restaura o último preenchimento utilizável ao sair do modo Corte."""
    previous = edit.pop("fit_cut_previous_fill_mode", None)
    if previous not in {"Cor sólida", "Imagem desfocada"}:
        previous = "Cor sólida"
    edit["background_mode"] = previous
    edit["custom_background_pending"] = False
    st.session_state[f"{prefix}_background_mode"] = previous
    st.session_state[f"{prefix}_custom_background_pending"] = False
    st.session_state.pop("focused_art_png_export", None)
    st.session_state.pop("pdf", None)
    st.session_state.pop("_sheet_real_size_preview_cache", None)
    return previous


def _deactivate_individual_fill_for_cut(item_id: int, prefix: str, edit: dict) -> dict:
    """Desativa todos os preenchimentos incompatíveis com Preencher até o corte.

    O modo interno "Sem preenchimento" não é uma opção do seletor: ele representa
    que Cor sólida, Desfoque e Fundo personalizado estão inativos enquanto o
    preset Corte estiver marcado. As cores e a intensidade ficam armazenadas,
    mas não são aplicadas à prévia, ao PNG, à folha ou ao PDF.
    """
    current_mode = str(edit.get("background_mode", "Cor sólida"))
    previous_mode = edit.get("fit_cut_previous_fill_mode")
    if current_mode in {"Cor sólida", "Imagem desfocada"}:
        previous_mode = current_mode
    elif current_mode == "Fundo personalizado":
        previous_mode = edit.get("background_image_previous_fill_mode", previous_mode)
    if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
        previous_mode = "Cor sólida"
    edit["fit_cut_previous_fill_mode"] = previous_mode

    has_custom_data = bool(
        edit.get("custom_background_active")
        or edit.get("background_image_bytes")
        or edit.get("background_image_digest")
        or edit.get("custom_background_pending")
        or current_mode == "Fundo personalizado"
    )
    if has_custom_data:
        remove_artwork_custom_background(
            int(item_id), restore_previous_fill_mode=False, commit_transform=False
        )
        item = _find_artwork_item(int(item_id))
        if item is not None:
            edit = item.setdefault("edit", {})

    edit["custom_background_active"] = False
    edit["custom_background_pending"] = False
    edit["background_mode"] = "Sem preenchimento"
    edit.pop("background_image_previous_fill_mode", None)
    st.session_state[f"{prefix}_background_mode"] = "Sem preenchimento"
    st.session_state[f"{prefix}_custom_background_pending"] = False
    st.session_state.pop("focused_art_png_export", None)
    st.session_state.pop("pdf", None)
    st.session_state.pop("_sheet_real_size_preview_cache", None)
    return edit


def _on_fit_mode_changed(
    selected_mode: str,
    selected_key: str,
    other_key: str,
    prefix: str = "global_image",
    item_id: int | None = None,
) -> None:
    """Aplica o preset individual e resolve os conflitos de fundo da arte."""
    selected = bool(st.session_state.get(selected_key, False))
    if not selected:
        # Ao desmarcar o preset, o usuário assume o controle manual do ajuste.
        if item_id is not None:
            item = _find_artwork_item(int(item_id))
            if item is not None:
                edit = item.setdefault("edit", {})
                edit["manual_transform_active"] = True
                edit["fit_cut_checked"] = False
                edit["fit_art_checked"] = False
                if str(edit.get("background_mode", "")) == "Sem preenchimento":
                    _restore_individual_fill_after_cut(edit, prefix)
                _persist_artwork_fields(int(item_id), ("fit_cut_checked", "fit_art_checked"))
                _safety_relative_transform(edit, edit.get("scale_reference_spec"))
        return

    st.session_state[other_key] = False
    st.session_state[f"{prefix}_fit_basis_mode"] = selected_mode
    if item_id is not None:
        item = _find_artwork_item(int(item_id))
        if item is not None:
            edit = item.setdefault("edit", {})
            edit["manual_transform_active"] = False
            edit["fit_basis_mode"] = selected_mode
            edit["fit_cut_checked"] = selected_mode == "cut"
            edit["fit_art_checked"] = selected_mode == "art"
            if selected_mode == "cut":
                # Corte invalida todos os modos de preenchimento/fundo, tal como
                # no Ajuste em Lote. As cores ficam guardadas, mas não ativas.
                edit = _deactivate_individual_fill_for_cut(int(item_id), prefix, edit)
            elif str(edit.get("background_mode", "")) == "Sem preenchimento":
                # Ao voltar para a área da arte, restaura o último tipo de
                # preenchimento válido (nunca uma imagem personalizada removida).
                _restore_individual_fill_after_cut(edit, prefix)
    st.session_state[f"{prefix}_zoom"] = 1.0
    st.session_state[f"{prefix}_offset_x"] = 0.0
    st.session_state[f"{prefix}_offset_y"] = 0.0
    st.session_state[f"{prefix}_transform_revision"] = int(
        st.session_state.get(f"{prefix}_transform_revision", 0)
    ) + 1
    if item_id is not None:
        item = _find_artwork_item(int(item_id))
        spec = BUTTONS.get(str(st.session_state.get("single_size")))
        if item is not None and spec is not None:
            edit = item.setdefault("edit", {})
            edit["scale_reference_spec"] = _button_scale_profile(spec)
            edit["scale_reference_diameter"] = float(spec.safe)
        _persist_artwork_fields(
            int(item_id),
            ("fit_basis_mode", "fit_cut_checked", "fit_art_checked",
             "zoom", "offset_x", "offset_y", "transform_revision"),
        )


def _toggle_touch_checkbox_state(state_key: str, callback=None, callback_args=()) -> None:
    """Alterna uma opção booleana por botão nativo, confiável em telas touch."""
    st.session_state[state_key] = not bool(st.session_state.get(state_key, False))
    if callable(callback):
        callback(*(callback_args or ()))


def render_touch_checkbox(
    label: str,
    *,
    key: str,
    value: bool = False,
    on_change=None,
    args=(),
    help: str | None = None,
    disabled: bool = False,
) -> bool:
    """Renderiza uma caixa de seleção visual usando um botão Streamlit.

    Em alguns navegadores móveis, os checkboxes nativos não recebem o toque
    corretamente quando a página combina CSS responsivo, componentes React
    e interações por pointer. O botão preserva a semântica visual de seleção,
    mantém o estado em session_state e usa o mesmo caminho de clique dos demais
    botões que já funcionam em celulares/tablets.
    """
    st.session_state.setdefault(key, bool(value))
    checked = bool(st.session_state.get(key, False))
    marker = "☑" if checked else "☐"
    safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(key))
    with st.container(key=f"touch_checkbox_{safe_key}"):
        st.button(
            f"{marker}  {label}",
            key=f"touch_checkbox_button_{safe_key}",
            type="secondary",
            use_container_width=True,
            disabled=disabled,
            help=help,
            on_click=_toggle_touch_checkbox_state,
            args=(key, on_change, tuple(args or ())),
        )
    return checked


def render_fit_mode_controls(
    prefix: str = "global_image", item_id: int | None = None
) -> str:
    """Mostra presets mutuamente exclusivos, sempre editáveis por arte."""
    cut_key = f"{prefix}_fit_cut_checked"
    art_key = f"{prefix}_fit_art_checked"
    basis_key = f"{prefix}_fit_basis_mode"
    st.session_state.setdefault(cut_key, st.session_state.get(basis_key, "cut") == "cut")
    st.session_state.setdefault(art_key, st.session_state.get(basis_key, "cut") == "art")
    st.session_state.setdefault(basis_key, "art" if st.session_state.get(art_key) else "cut")
    render_touch_checkbox(
        "Preencher até o corte",
        key=cut_key,
        on_change=_on_fit_mode_changed,
        args=("cut", cut_key, art_key, prefix, item_id),
        help="Usa a guia Corte como referência de escala. Com um fundo de borda ativo, a imagem principal continua visível somente até a guia Arte.",
    )
    render_touch_checkbox(
        "Encaixar na área da arte",
        key=art_key,
        on_change=_on_fit_mode_changed,
        args=("art", art_key, cut_key, prefix, item_id),
        help="Redimensiona para caber integralmente na guia da arte e centraliza o enquadramento.",
    )
    st.caption("Você pode alterar o enquadramento com os presets, arrastar a imagem ou usar as alças de zoom. Cada arte mantém seus próprios ajustes.")
    return str(st.session_state.get(basis_key, "cut"))


def _activate_art_area_preserving_manual_transform(item_id: int, prefix: str) -> None:
    """Mostra o preenchimento fora da arte sem descartar zoom/posição manuais.

    Os fundos decorativos ocupam a guia Corte e, por isso, a imagem principal
    precisa ser recortada pela guia Arte. Quando a composição já é manual,
    convertemos somente a base geométrica (Corte -> Arte), preservando o tamanho
    físico e o deslocamento atuais. Um preset explícito continua podendo recentrar.
    """
    item = _find_artwork_item(int(item_id))
    if item is None:
        return

    # Captura o último gesto/zoom do canvas antes de alterar a base geométrica.
    try:
        _commit_artwork_editor_state(int(item_id))
    except (NameError, TypeError, ValueError, KeyError):
        pass

    item = _find_artwork_item(int(item_id)) or item
    edit = item.setdefault("edit", {})
    cut_key = f"{prefix}_fit_cut_checked"
    art_key = f"{prefix}_fit_art_checked"
    basis_key = f"{prefix}_fit_basis_mode"
    zoom_key = f"{prefix}_zoom"
    x_key = f"{prefix}_offset_x"
    y_key = f"{prefix}_offset_y"
    revision_key = f"{prefix}_transform_revision"

    cut_checked = bool(st.session_state.get(cut_key, edit.get("fit_cut_checked", False)))
    art_checked = bool(st.session_state.get(art_key, edit.get("fit_art_checked", False)))
    manual = bool(edit.get("manual_transform_active", False)) or not (cut_checked or art_checked)
    current_mode = str(edit.get("fit_basis_mode", st.session_state.get(basis_key, "cut"))).lower()
    if current_mode not in {"cut", "art"}:
        current_mode = "cut"

    spec = BUTTONS.get(str(st.session_state.get("single_size")))
    profile = edit.get("scale_reference_spec")
    if not isinstance(profile, dict):
        profile = _button_scale_profile(spec) if spec is not None else None
    try:
        cut_diameter = float(profile["cut"])
        art_diameter = float(profile["art"])
        safe_diameter = float(profile["safe"])
        profile_valid = all(
            math.isfinite(value) and value > 0
            for value in (cut_diameter, art_diameter, safe_diameter)
        )
    except (KeyError, TypeError, ValueError):
        profile_valid = False

    if manual:
        # Em composição manual, trocar a área de recorte não deve mexer na
        # escala física nem na posição da imagem. Como o zoom é relativo à guia
        # ativa, convertemos a razão Corte/Arte e mantemos X/Y em milímetros.
        zoom = float(st.session_state.get(zoom_key, edit.get("zoom", 1.0)))
        off_x = float(st.session_state.get(x_key, edit.get("offset_x", 0.0)))
        off_y = float(st.session_state.get(y_key, edit.get("offset_y", 0.0)))
        if current_mode != "art" and profile_valid:
            # O modo Corte usa min(largura, altura) como base; o modo Arte
            # usa max(largura, altura). Considerar também a proporção da imagem
            # é essencial para preservar a dimensão física em artes retangulares.
            try:
                image_width, image_height = get_image_dimensions(
                    str(item.get("image_digest", "")), item.get("image", b"")
                )
                old_image_basis = float(min(image_width, image_height))
                new_image_basis = float(max(image_width, image_height))
                if old_image_basis > 0 and new_image_basis > 0:
                    old_scale_per_zoom = cut_diameter / old_image_basis
                    new_scale_per_zoom = art_diameter / new_image_basis
                    zoom *= old_scale_per_zoom / new_scale_per_zoom
                else:
                    zoom *= cut_diameter / art_diameter
            except Exception:
                zoom *= cut_diameter / art_diameter
        zoom = max(0.01, min(20.0, zoom))
        off_x = max(-5000.0, min(5000.0, off_x))
        off_y = max(-5000.0, min(5000.0, off_y))

        edit["fit_basis_mode"] = "art"
        edit["fit_cut_checked"] = False
        edit["fit_art_checked"] = False
        edit["manual_transform_active"] = True
        edit["zoom"] = zoom
        edit["offset_x"] = off_x
        edit["offset_y"] = off_y
        edit["transform_revision"] = int(
            st.session_state.get(revision_key, edit.get("transform_revision", 0)) or 0
        ) + (1 if current_mode != "art" else 0)
        if profile_valid:
            edit["scale_reference_spec"] = profile
            edit["scale_reference_diameter"] = safe_diameter
            _safety_relative_transform(edit, profile)

        st.session_state[cut_key] = False
        st.session_state[art_key] = False
        st.session_state[basis_key] = "art"
        st.session_state[zoom_key] = zoom
        st.session_state[x_key] = off_x
        st.session_state[y_key] = off_y
        st.session_state[revision_key] = edit["transform_revision"]
        _persist_artwork_fields(
            int(item_id),
            ("fit_basis_mode", "fit_cut_checked", "fit_art_checked", "manual_transform_active",
             "zoom", "offset_x", "offset_y", "transform_revision"),
        )
        return

    # Se havia um preset ativo, o preenchimento decorativo exige o preset Arte.
    # Nesse caso o recentramento é intencional: o usuário ainda não personalizou
    # a posição manualmente. Se Arte já era o preset, não há nada a alterar.
    if not (art_checked and not cut_checked and current_mode == "art"):
        st.session_state[art_key] = True
        st.session_state[cut_key] = False
        _on_fit_mode_changed("art", art_key, cut_key, prefix, int(item_id))


def make_artwork_background_mode_callback(item_id: int, prefix: str):
    """Alterna entre Cor sólida, Desfoque e Fundo personalizado na arte individual.

    O fundo personalizado é uma opção real do seletor, mesmo antes do upload.
    Seus controles aparecem somente quando esse modo está ativo; o modo de
    preenchimento anterior fica guardado para ser restaurado ao remover a imagem.
    A troca de preenchimento nunca descarta zoom/posição ajustados manualmente.
    """
    def _apply_mode():
        mode_key = f"{prefix}_background_mode"
        selected_mode = str(st.session_state.get(mode_key, "Cor sólida"))
        valid_modes = {"Cor sólida", "Imagem desfocada", "Fundo personalizado"}
        if selected_mode not in valid_modes:
            selected_mode = "Cor sólida"
            st.session_state[mode_key] = selected_mode

        item = _find_artwork_item(int(item_id))
        if item is None:
            return
        edit = item.setdefault("edit", {})
        has_custom_layer = bool(_custom_background_payload(edit)[0])
        was_custom_selected = str(edit.get("background_mode", "Cor sólida")) == "Fundo personalizado"

        if selected_mode == "Fundo personalizado":
            edit.pop("fit_cut_previous_fill_mode", None)
            # Guardar o preenchimento que deverá voltar quando a camada for
            # removida. Se o modo personalizado já estava ativo/pendente,
            # preservar o modo anterior em vez de gravar "Fundo personalizado".
            previous_mode = edit.get("background_image_previous_fill_mode")
            if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
                current_mode = str(edit.get("background_mode", "Cor sólida"))
                previous_mode = current_mode if current_mode in {"Cor sólida", "Imagem desfocada"} else "Cor sólida"
                edit["background_image_previous_fill_mode"] = previous_mode

            edit["background_mode"] = "Fundo personalizado"
            edit["custom_background_pending"] = not has_custom_layer
            st.session_state[f"{prefix}_custom_background_pending"] = not has_custom_layer
            _persist_artwork_fields(
                int(item_id),
                ("background_mode", "custom_background_pending", "background_image_previous_fill_mode"),
            )

        else:
            # Uma nova escolha explícita de preenchimento passa a ser a última
            # preferência; não reutilizar um marcador antigo do preset Corte.
            edit.pop("fit_cut_previous_fill_mode", None)
            # Cor sólida/Desfoque substituem tanto uma camada já carregada
            # quanto uma seleção de Fundo Personalizado ainda sem upload.
            if (
                has_custom_layer
                or edit.get("custom_background_active")
                or edit.get("background_image_bytes")
                or edit.get("custom_background_pending")
                or was_custom_selected
            ):
                remove_artwork_custom_background(
                    int(item_id), restore_previous_fill_mode=False, commit_transform=False
                )
                item = _find_artwork_item(int(item_id)) or item
                edit = item.setdefault("edit", {})

            edit["custom_background_pending"] = False
            st.session_state[f"{prefix}_custom_background_pending"] = False
            edit.pop("background_image_previous_fill_mode", None)
            edit["background_mode"] = selected_mode
            st.session_state[mode_key] = selected_mode
            _persist_artwork_fields(int(item_id), ("background_mode", "custom_background_pending"))

        # Qualquer preenchimento fica visível fora da arte principal somente
        # com a guia Arte como limite. A troca da base geométrica preserva uma
        # composição manual já ajustada pelo usuário.
        _activate_art_area_preserving_manual_transform(int(item_id), prefix)

    return _apply_mode


def make_image_editor_sync_callback(
    component_key: str,
    prefix: str,
    token_state_key: str,
    fit_cut_key: str = "global_fit_cut_checked",
    fit_art_key: str = "global_fit_art_checked",
    item_id: int | None = None,
):
    """Confirma a transformação e persiste-a imediatamente na arte correspondente."""
    def _sync_editor_state():
        component_result = st.session_state.get(component_key)
        transform = _component_value(component_result, "transform")
        expected_token = st.session_state.get(token_state_key)
        if _store_editor_transform(
            transform, expected_token, prefix, update_widget_flags=True
        ):
            if item_id is not None:
                for artwork in st.session_state.get("artwork_items", []):
                    if int(artwork.get("id", -1)) == int(item_id):
                        persist_artwork_edit_state(artwork, prefix)
                        break
    return _sync_editor_state


def _commit_artwork_editor_state(item_id: int) -> None:
    """Salva a última transformação do componente antes de trocar a miniatura."""
    artwork = next(
        (entry for entry in st.session_state.get("artwork_items", [])
         if int(entry.get("id", -1)) == int(item_id)),
        None,
    )
    if artwork is None:
        return
    prefix = ensure_artwork_edit_state(artwork, sync_session=False)
    component_key = f"main_visual_image_editor_{int(item_id)}"
    token_state_key = f"_{prefix}_expected_token"
    result = st.session_state.get(component_key)
    transform = _component_value(result, "transform")
    expected_token = st.session_state.get(token_state_key)
    _store_editor_transform(
        transform, expected_token, prefix, update_widget_flags=True
    )
    persist_artwork_edit_state(artwork, prefix)


def _on_pool_selected_art_change() -> None:
    """Confirma a arte atual antes de aceitar a seleção da próxima miniatura."""
    previous_id = st.session_state.get("artwork_selected_id")
    if previous_id is not None:
        try:
            _commit_artwork_editor_state(int(previous_id))
        except (TypeError, ValueError):
            pass
    # O novo selected_id é aplicado pelo retorno do componente no fluxo principal.


def _navigate_editor_artwork(offset: int) -> None:
    """Move a prévia para a arte anterior/próxima na ordem atual do Pool."""
    items = st.session_state.get("artwork_items", [])
    if len(items) < 2:
        return
    try:
        current_id = int(st.session_state.get("artwork_selected_id"))
    except (TypeError, ValueError):
        current_id = int(items[0]["id"])
    positions = {int(item["id"]): index for index, item in enumerate(items)}
    current_index = positions.get(current_id, 0)
    target_index = max(0, min(len(items) - 1, current_index + int(offset)))
    if target_index == current_index:
        return
    # Confirma zoom/posição do canvas antes de trocar a arte em foco.
    _commit_artwork_editor_state(current_id)
    target_id = int(items[target_index]["id"])
    st.session_state["artwork_selected_id"] = target_id
    # O estado salvo do componente de Pool pode conter a seleção anterior.
    # Este marcador impede que ela sobreponha a navegação neste rerun.
    st.session_state["_editor_navigation_target_id"] = target_id


def _commit_selected_artwork_before_size_change() -> None:
    """Salva o último arraste/zoom antes de converter a arte para outro botton."""
    selected_id = st.session_state.get("artwork_selected_id")
    if selected_id is None:
        return
    try:
        _commit_artwork_editor_state(int(selected_id))
    except (TypeError, ValueError):
        return


def current_editor_transform(component_key: str, token: str, prefix: str) -> dict:
    """Lê apenas a transformação canônica do Session State (nunca uma cópia antiga do componente)."""
    return {
        "token": token,
        "zoom": float(st.session_state[f"{prefix}_zoom"]),
        "off_x": float(st.session_state[f"{prefix}_offset_x"]),
        "off_y": float(st.session_state[f"{prefix}_offset_y"]),
        "revision": int(st.session_state.get(f"{prefix}_transform_revision", 0)),
        "event_seq": int(st.session_state.get(f"{prefix}_event_seq", 0)),
    }


def render_guide_legend() -> None:
    """Legenda horizontal e compacta para ficar junto da prévia."""
    guide_rows = [
        ("#E5457B", "dashed", "Corte", "Limite externo e sangria"),
        ("#FFFF00", "dashed", "Dobra", "Linha de dobra da matriz"),
        ("#2F9BD8", "solid", "Arte", "Área visível do botton"),
        ("#00F919", "dashed", "Segurança", "Área de textos e detalhes"),
    ]
    rows = []
    for color, style, label, description in guide_rows:
        rows.append(
            f'<div class="guide-compact-item" title="{description}">'
            f'<span class="guide-compact-line" style="border-top-style:{style};border-top-color:{color};"></span>'
            f'<span class="guide-compact-label">{label}</span>'
            f'<span class="guide-compact-description">{description}</span>'
            '</div>'
        )
    st.markdown('<div class="guide-compact-legend">' + ''.join(rows) + '</div>', unsafe_allow_html=True)



@st.cache_resource(show_spinner=False)
def get_image_editor_component():
    """Editor visual persistente: mantém DOM e transformação entre reruns do Streamlit."""
    html = '<div class="atomica-editor-root" id="atomica-editor-root"></div>'
    css = r"""
    .atomica-editor-root {
      width:100%; max-width:620px; margin:0 auto; color:#EAF1FA;
      font-family:inherit; box-sizing:border-box;
    }
    .atomica-editor-stage {
      width:100%; max-width:620px; margin:0 auto; position:relative;
      display:flex; justify-content:center; align-items:center;
      border:1px solid #29415F; border-radius:16px; padding:12px;
      background:linear-gradient(145deg,#0C1B30,#132A45);
      box-sizing:border-box; overflow:hidden;
    }
    .atomica-editor-canvas {
      display:block; width:100%; max-width:580px; height:auto; aspect-ratio:1/1;
      border-radius:10px; background:#F3F5F8; cursor:default;
      touch-action:none; user-select:none; -webkit-user-select:none;
      box-shadow:0 8px 24px rgba(0,0,0,.22);
    }
    .atomica-editor-canvas.moving { cursor:grabbing; }
    .atomica-editor-canvas.resizing { cursor:nwse-resize; }
    .atomica-editor-toolbar {
      display:flex; flex-wrap:wrap; align-items:center; justify-content:center;
      gap:8px; padding:11px 4px 3px;
    }
    .atomica-editor-toolbar button {
      min-width:42px; min-height:37px; padding:7px 12px; border-radius:9px;
      border:1px solid #3A587B; background:#183657; color:#F4F7FB;
      font-size:14px; font-weight:700; cursor:pointer;
    }
    .atomica-editor-toolbar button:hover { background:#24517A; border-color:#67A4D7; }
    .atomica-editor-toolbar button.primary {
      background:#123D50; border-color:#26C6DA; color:#D9FBFF;
    }
    .atomica-editor-zoom {
      min-width:58px; text-align:center; font-size:13px; font-weight:800;
      color:#DDEAF8; font-variant-numeric:tabular-nums;
    }
    .atomica-editor-hint {
      max-width:660px; margin:0 auto; padding:7px 5px 0;
      text-align:center; color:#AFC0D4; font-size:12px; line-height:1.5;
    }
    .atomica-guide-legend {
      display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:9px;
      max-width:660px; margin:12px auto 3px; padding:11px 12px;
      border:1px solid #29415F; border-radius:12px; background:#0B1B30;
      box-sizing:border-box;
    }
    .atomica-guide-item { min-width:0; display:grid; grid-template-columns:32px 1fr; gap:7px; align-items:start; }
    .atomica-guide-line { display:block; width:29px; height:0; margin-top:8px; border-top-width:3px; border-top-style:solid; }
    .atomica-guide-line.cut { border-top-color:#E5457B; border-top-style:dashed; }
    .atomica-guide-line.fold { border-top-color:#FFFF00; border-top-style:dashed; }
    .atomica-guide-line.art { border-top-color:#2F9BD8; border-top-style:solid; }
    .atomica-guide-line.safe { border-top-color:#00F919; border-top-style:dashed; }
    .atomica-guide-copy strong { display:block; color:#F4F7FB; font-size:12px; line-height:1.3; }
    .atomica-guide-copy span { display:block; color:#AFC0D4; font-size:10.5px; line-height:1.35; margin-top:3px; }
    @media (max-width:760px) {
      .atomica-guide-legend { grid-template-columns:repeat(2,minmax(0,1fr)); }
    }
    @media (max-height:850px) and (min-width:901px) {
      .atomica-editor-root, .atomica-editor-stage { max-width:540px; }
      .atomica-editor-canvas { max-width:510px; }
    }
    @media (max-width:760px) {
      .atomica-editor-root, .atomica-editor-stage { width:100%; max-width:100%; min-width:0; }
      .atomica-editor-stage { padding:7px; border-radius:12px; }
      .atomica-editor-canvas { width:100%; max-width:100%; border-radius:8px; touch-action:none; }
      .atomica-editor-toolbar { gap:7px; padding:9px 2px 2px; }
      .atomica-editor-toolbar button { min-width:46px; min-height:46px; padding:9px 12px; touch-action:manipulation; }
      .atomica-editor-zoom { min-width:70px; min-height:40px; display:flex; align-items:center; justify-content:center; }
      .atomica-editor-hint { font-size:12px; line-height:1.55; padding:9px 4px 0; }
      .atomica-guide-legend { gap:8px; padding:9px; }
      .atomica-guide-item { grid-template-columns:26px 1fr; gap:5px; }
      .atomica-guide-line { width:23px; }
    }
    @media (max-width:420px) {
      .atomica-editor-toolbar { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; }
      .atomica-editor-toolbar button { min-width:0; width:100%; min-height:48px; padding:8px 5px; font-size:14px; }
      .atomica-editor-zoom { grid-column:span 4; min-height:32px; }
      .atomica-guide-legend { grid-template-columns:repeat(2,minmax(0,1fr)); }
    }
    """
    js = r"""
    // O renderer v2 do Streamlit pode ser chamado novamente a cada rerun.
    // A instância fica guardada no próprio root: DOM, listeners e transformação
    // não são recriados quando Python recebe um novo estado.
    export default function(component) {
      const { parentElement, data, setStateValue } = component;
      const host = parentElement;
      let root = host.querySelector('#atomica-editor-root');
      if (!root) return;

      const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
      const finiteOrZero = value => {
        const n = Number(value);
        return Number.isFinite(n) ? n : 0;
      };
      const readTransform = value => {
        const t = value && typeof value === 'object' ? value : {};
        return {
          zoom: clamp(Number(t.zoom) || 1, 0.01, 20),
          off_x: finiteOrZero(t.off_x),
          off_y: finiteOrZero(t.off_y),
          revision: Math.max(0, Math.floor(Number(t.revision) || 0)),
          event_seq: Math.max(0, Math.floor(Number(t.event_seq) || 0))
        };
      };
      const transformSignature = t =>
        `${Number(t.zoom).toFixed(5)}|${Number(t.off_x).toFixed(4)}|${Number(t.off_y).toFixed(4)}`;

      function createInstance(rootElement) {
        const instance = {
          root: rootElement,
          cfg: {},
          token: null,
          state: { zoom: 1, off_x: 0, off_y: 0 },
          lastIncomingSignature: null,
          lastPublishedSignature: null,
          transformRevision: null,
          eventSequence: 0,
          setStateValue: null,
          image: new Image(),
          backdropImage: new Image(),
          customBackgroundImage: new Image(),
          customBackgroundRevision: -1,
          imageReady: false,
          backdropReady: false,
          customBackgroundReady: false,
          imageSrc: '',
          backdropSrc: '',
          customBackgroundSrc: '',
          blurCanvas: null,
          blurCanvasSignature: null,
          lastDrawSignature: null,
          activeDrag: null,
          activePointers: new Map(),
          pinchGesture: null,
          wheelTimer: null,
          drawFrame: null,
          fixedView: null,
          fixedViewSignature: null,
          root: rootElement,
          nodes: []
        };
        const side = 1000;
        function getHandleRadius() {
          const renderedWidth = Math.max(1, canvas.getBoundingClientRect().width || 580);
          // Em telefones/tablets, 10–12 CSS px de raio mantém as alças fáceis
          // de tocar, sem alterar a escala física exportada da arte.
          return clamp((11 * side) / renderedWidth, 17, 40);
        }

        rootElement.replaceChildren();
        const stage = document.createElement('div');
        stage.className = 'atomica-editor-stage';
        const canvas = document.createElement('canvas');
        canvas.className = 'atomica-editor-canvas';
        canvas.width = side;
        canvas.height = side;
        canvas.setAttribute('aria-label', 'Editor de botton com quatro alças de redimensionamento');
        stage.appendChild(canvas);
        rootElement.appendChild(stage);
        const ctx = canvas.getContext('2d');
        instance.canvas = canvas;
        instance.ctx = ctx;

        const toolbar = document.createElement('div');
        toolbar.className = 'atomica-editor-toolbar';
        function makeButton(label, title, cls, handler) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = label;
          button.title = title;
          if (cls) button.className = cls;
          button.addEventListener('click', handler);
          toolbar.appendChild(button);
          return button;
        }
        const status = document.createElement('span');
        status.className = 'atomica-editor-zoom';
        instance.status = status;
        function updateStatus() {
          status.textContent = `${Math.round(instance.state.zoom * 100)}%`;
          status.title = `Escala ${instance.state.zoom.toFixed(2)} · X ${instance.state.off_x.toFixed(1)} mm · Y ${instance.state.off_y.toFixed(1)} mm`;
        }
        instance.updateStatus = updateStatus;
        function publish() {
          instance.eventSequence = Math.max(
            Number(instance.eventSequence) || 0,
            Number(instance.state.event_seq) || 0
          ) + 1;
          instance.state.event_seq = instance.eventSequence;
          const cfg = getCfg();
          const effectiveFitMode = activeFitMode();
          const payload = {
            token: instance.token,
            zoom: Number(instance.state.zoom.toFixed(5)),
            off_x: Number(instance.state.off_x.toFixed(4)),
            off_y: Number(instance.state.off_y.toFixed(4)),
            revision: Number(instance.transformRevision || 0),
            event_seq: instance.eventSequence,
            // O perfil acompanha o evento, para que a conversão use exatamente
            // as guias do canvas em que o usuário fez o último ajuste, mesmo
            // quando o selectbox de tamanho já está mudando no Streamlit.
            profile: {
              size: Number(cfg.size) || 0,
              cut: Number(cfg.cut),
              fold: Number(cfg.fold),
              art: Number(cfg.art),
              safe: Number(cfg.safe),
              shape: String(cfg.shape || 'circle')
            },
            fit_mode: effectiveFitMode,
            safety_relative: {
              zoom: Number((instance.state.zoom * (effectiveFitMode === 'art' ? Number(cfg.art) : Number(cfg.cut)) / Math.max(1e-9, Number(cfg.safe))).toFixed(8)),
              offset_x: Number((instance.state.off_x / Math.max(1e-9, Number(cfg.safe))).toFixed(8)),
              offset_y: Number((instance.state.off_y / Math.max(1e-9, Number(cfg.safe))).toFixed(8))
            },
            manual_edit: true
          };
          instance.lastPublishedSignature = transformSignature(payload);
          if (typeof instance.setStateValue === 'function') {
            instance.setStateValue('transform', payload);
          }
        }
        instance.publish = publish;
        function redraw() {
          if (instance.drawFrame !== null) return;
          instance.drawFrame = requestAnimationFrame(() => {
            instance.drawFrame = null;
            draw();
          });
        }
        function finishChange() {
          instance.activeDrag = null;
          draw();
          updateStatus();
          publish();
        }
        instance.finishChange = finishChange;

        makeButton('−', 'Diminuir o zoom em 1 ponto percentual', '', () => {
          const currentPercent = Math.round(instance.state.zoom * 100);
          instance.state.zoom = clamp((currentPercent - 1) / 100, 0.01, 20);
          finishChange();
        });
        toolbar.appendChild(status);
        makeButton('+', 'Aumentar o zoom em 1 ponto percentual', '', () => {
          const currentPercent = Math.round(instance.state.zoom * 100);
          instance.state.zoom = clamp((currentPercent + 1) / 100, 0.01, 20);
          finishChange();
        });
        makeButton('Centralizar', 'Centraliza a arte sem alterar seu tamanho', 'primary', () => {
          instance.state.off_x = 0;
          instance.state.off_y = 0;
          finishChange();
        });
        makeButton('Restaurar', 'Voltar ao enquadramento inicial da imagem dentro do Corte', '', () => {
          const initialZoom = Number(getCfg().initial_zoom);
          instance.state.zoom = clamp(Number.isFinite(initialZoom) && initialZoom > 0 ? initialZoom : 1, 0.01, 20);
          instance.state.off_x = 0;
          instance.state.off_y = 0;
          finishChange();
        });
        rootElement.appendChild(toolbar);

        const hint = document.createElement('div');
        hint.className = 'atomica-editor-hint';
        hint.textContent = 'Arraste a imagem para mover · use as alças dos cantos para redimensionar · no celular, faça o gesto de pinça com dois dedos para aproximar ou afastar · use + e − para ajustes finos.';
        rootElement.appendChild(hint);

        function getCfg() { return instance.cfg || {}; }
        function activeFitMode() {
          const cfg = getCfg();
          // A imagem de fundo é uma camada separada e não altera o preset nem
          // a transformação da arte principal.
          return cfg.fit_mode || 'cut';
        }
        function sourceBasis(fit, sourceImage) {
          return fit ? Math.max(sourceImage.naturalWidth, sourceImage.naturalHeight)
                     : Math.min(sourceImage.naturalWidth, sourceImage.naturalHeight);
        }
        function getPhysicalSize(fit, transform=instance.state, sourceImage=instance.image) {
          const cfg = getCfg();
          const diameter = fit ? (Number(cfg.art) || Number(cfg.cut) || 1) : (Number(cfg.cut) || 1);
          const basis = sourceBasis(fit, sourceImage);
          const factor = diameter / Math.max(1, basis) * transform.zoom;
          return {
            width: sourceImage.naturalWidth * factor,
            height: sourceImage.naturalHeight * factor
          };
        }
        function getView() {
          const cfg = getCfg();
          const signature = [
            cfg.token, cfg.shape, cfg.cut, cfg.art, activeFitMode(),
            instance.image.naturalWidth, instance.image.naturalHeight
          ].join('|');
          if (instance.fixedView && instance.fixedViewSignature === signature) return instance.fixedView;
          const cut = Math.max(1, Number(cfg.cut) || 1);
          // A área de trabalho deve priorizar o botton, não as dimensões totais
          // do arquivo original. Em artes panorâmicas, considerar toda a imagem
          // reduzia demais as guias, embora o excedente fique recortado pelo corte.
          // O limite externo de corte ocupa 90% do canvas, com 5% de margem em cada lado.
          const domain = cut / 0.9;
          instance.fixedViewSignature = signature;
          instance.fixedView = { domain, ppm: side / domain };
          return instance.fixedView;
        }
        function getRect(view, fit, transform=instance.state, sourceImage=instance.image) {
          const dimensions = getPhysicalSize(fit, transform, sourceImage);
          const width = dimensions.width * view.ppm;
          const height = dimensions.height * view.ppm;
          const cx = side / 2 + transform.off_x * view.ppm;
          const cy = side / 2 - transform.off_y * view.ppm;
          return {x:cx-width/2, y:cy-height/2, w:width, h:height, cx, cy};
        }
        function shapePath(context, diameter, view) {
          const radius = diameter * view.ppm / 2;
          context.beginPath();
          if (getCfg().shape === 'square') {
            context.rect(side/2-radius, side/2-radius, radius*2, radius*2);
          } else {
            context.arc(side/2, side/2, radius, 0, Math.PI*2);
          }
        }
        function drawImagePlaced(view, fit, sourceImage=instance.image, transform=instance.state) {
          const rect = getRect(view, fit, transform, sourceImage);
          ctx.drawImage(sourceImage, rect.x, rect.y, rect.w, rect.h);
          return rect;
        }
        function ensureBlurBackdrop(view) {
          const cfg = getCfg();
          const blurPx = Math.max(0, Number(cfg.blur_mm) || 0) * view.ppm;
          const signature = [
            instance.token, instance.backdropSrc, instance.backdropReady ? 'ready' : 'fallback',
            cfg.shape, cfg.cut, cfg.bg || '#FFFFFF', blurPx.toFixed(3), view.ppm.toFixed(4),
          ].join('|');
          if (instance.blurCanvas && instance.blurCanvasSignature === signature) {
            return instance.blurCanvas;
          }
          const layer = document.createElement('canvas');
          layer.width = side;
          layer.height = side;
          const layerCtx = layer.getContext('2d');
          layerCtx.save();
          shapePath(layerCtx, Number(cfg.cut) || 1, view);
          layerCtx.clip();
          layerCtx.fillStyle = cfg.bg || '#FFFFFF';
          layerCtx.fillRect(0, 0, side, side);
          const sourceImage = instance.backdropReady ? instance.backdropImage : instance.image;
          if (sourceImage && sourceImage.complete && sourceImage.naturalWidth > 0) {
            const rect = getRect(view, false, {zoom:1, off_x:0, off_y:0}, sourceImage);
            layerCtx.save();
            if (blurPx > 0) layerCtx.filter = `blur(${blurPx}px)`;
            layerCtx.drawImage(sourceImage, rect.x, rect.y, rect.w, rect.h);
            layerCtx.restore();
          }
          layerCtx.restore();
          instance.blurCanvas = layer;
          instance.blurCanvasSignature = signature;
          return layer;
        }
        function drawGuides(view) {
          const cfg = getCfg();
          const cut = Number(cfg.cut) || 1;
          const definitions = [
            {key:'cut', diameter:cut, color:'#E5457B', style:'dashed'},
            {key:'fold', diameter:Number(cfg.fold)||cut, color:'#FFFF00', style:'dashed'},
            {key:'art', diameter:Number(cfg.art)||cut, color:'#2F9BD8', style:'solid'},
            {key:'safe', diameter:Number(cfg.safe)||cut, color:'#00F919', style:'dashed'}
          ];
          definitions.forEach(g => {
            if (!Array.isArray(cfg.guides) || !cfg.guides.includes(g.key)) return;
            ctx.save();
            if (g.style === 'dashed') {
              ctx.setLineDash([2.2*view.ppm, 1.4*view.ppm]); ctx.lineCap = 'butt';
            } else if (g.style === 'dotted') {
              ctx.setLineDash([0.1*view.ppm, 1.6*view.ppm]); ctx.lineCap = 'round';
            } else {
              ctx.setLineDash([]); ctx.lineCap = 'round';
            }
            // Contorno escuro por baixo cria contraste sobre artes claras e escuras.
            ctx.lineWidth = Math.max(3.8, 0.38 * view.ppm);
            ctx.strokeStyle = 'rgba(8, 20, 36, 0.88)';
            shapePath(ctx, g.diameter, view);
            ctx.stroke();
            // Traço colorido central preserva a identificação de cada guia.
            ctx.lineWidth = Math.max(2.4, 0.24 * view.ppm);
            ctx.strokeStyle = g.color;
            shapePath(ctx, g.diameter, view);
            ctx.stroke();
            ctx.restore();
          });
        }
        function getHandles(rect) {
          // Mantém as alças na área clicável mesmo quando os limites da imagem
          // ficam fora do canvas (comum em artes retangulares no modo Preencher).
          const radius = getHandleRadius();
          const inset = radius + 5;
          const visible = value => clamp(value, inset, side - inset);
          return [
            {sx:-1, sy:-1, x:visible(rect.x), y:visible(rect.y), rawX:rect.x, rawY:rect.y},
            {sx: 1, sy:-1, x:visible(rect.x+rect.w), y:visible(rect.y), rawX:rect.x+rect.w, rawY:rect.y},
            {sx: 1, sy: 1, x:visible(rect.x+rect.w), y:visible(rect.y+rect.h), rawX:rect.x+rect.w, rawY:rect.y+rect.h},
            {sx:-1, sy: 1, x:visible(rect.x), y:visible(rect.y+rect.h), rawX:rect.x, rawY:rect.y+rect.h}
          ];
        }
        function drawTransformBox(rect) {
          ctx.save();
          ctx.strokeStyle = '#16B9D2';
          ctx.lineWidth = 2.5;
          ctx.setLineDash([8,5]);
          ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
          ctx.setLineDash([]);
          const radius = getHandleRadius();
          getHandles(rect).forEach(handle => {
            ctx.beginPath(); ctx.arc(handle.x, handle.y, radius, 0, Math.PI*2);
            ctx.fillStyle = '#FFFFFF'; ctx.fill();
            ctx.lineWidth = 4; ctx.strokeStyle = '#12AFC8'; ctx.stroke();
            ctx.beginPath(); ctx.arc(handle.x, handle.y, 3, 0, Math.PI*2);
            ctx.fillStyle = '#12AFC8'; ctx.fill();
          });
          ctx.restore();
        }
        function draw() {
          const cfg = getCfg();
          ctx.clearRect(0,0,side,side);
          ctx.fillStyle = '#F3F5F8'; ctx.fillRect(0,0,side,side);
          if (!instance.imageReady) return;
          const view = getView();
          ctx.save();
          shapePath(ctx, Number(cfg.cut)||1, view);
          ctx.clip();
          ctx.fillStyle = cfg.bg || '#FFFFFF';
          ctx.fillRect(0,0,side,side);
          if (instance.customBackgroundReady) {
            // O fundo personalizado ocupa toda a guia Corte; a arte principal
            // será composta em seguida, limitada à guia Arte.
            const backgroundRect = getRect(
              view, false, {zoom:1, off_x:0, off_y:0}, instance.customBackgroundImage
            );
            ctx.drawImage(
              instance.customBackgroundImage,
              backgroundRect.x, backgroundRect.y, backgroundRect.w, backgroundRect.h
            );
          } else if ((cfg.background_mode || 'color') === 'blur') {
            // O fundo desfocado é pré-renderizado uma vez por imagem/cor/raio;
            // durante arraste e zoom apenas copiamos a camada pronta ao canvas.
            const blurredBackdrop = ensureBlurBackdrop(view);
            if (blurredBackdrop) ctx.drawImage(blurredBackdrop, 0, 0);
          }
          let selectedRect;
          if (activeFitMode() === 'art' || instance.customBackgroundReady) {
            // A borda personalizada fica visível no anel Arte–Corte, mas a
            // escala e o zoom da arte principal continuam sob controle do usuário.
            ctx.save();
            shapePath(ctx, Number(cfg.art)||Number(cfg.cut)||1, view);
            ctx.clip();
            selectedRect = drawImagePlaced(view, activeFitMode() === 'art');
            ctx.restore();
          } else {
            selectedRect = drawImagePlaced(view, false);
          }
          ctx.restore();
          drawGuides(view);
          drawTransformBox(selectedRect);
          updateStatus();
        }
        instance.draw = draw;
        instance.redraw = redraw;
        instance.getView = getView;
        instance.getRect = getRect;

        function canvasPoint(event) {
          const rect = canvas.getBoundingClientRect();
          return {
            x:(event.clientX-rect.left)*side/Math.max(1,rect.width),
            y:(event.clientY-rect.top)*side/Math.max(1,rect.height)
          };
        }
        function hitHandle(point, rect, pointerType='mouse') {
          const visualRadius = getHandleRadius();
          const canvasWidth = Math.max(1, canvas.getBoundingClientRect().width || 580);
          // No touch, o alvo efetivo tem pelo menos 26 CSS px de raio. Isso
          // evita exigir precisão de mouse para alcançar as alças em celulares.
          const touchHitRadius = 26 * side / canvasWidth;
          const hitRadius = pointerType === 'touch'
            ? Math.max(visualRadius * 2.0, touchHitRadius)
            : visualRadius * 2.0;
          for (const handle of getHandles(rect)) {
            if (Math.hypot(point.x-handle.x, point.y-handle.y) <= hitRadius) return handle;
          }
          return null;
        }
        function pointInsideRect(point, rect) {
          return point.x>=rect.x && point.x<=rect.x+rect.w &&
                 point.y>=rect.y && point.y<=rect.y+rect.h;
        }
        function setCanvasPointerCapture(event) {
          // Eventos sintéticos de testes e alguns navegadores podem recusar a
          // captura; os eventos normais continuam sendo tratados pelo canvas.
          try {
            if (typeof canvas.setPointerCapture === 'function') {
              canvas.setPointerCapture(event.pointerId);
            }
          } catch (_) { /* captura não disponível para este evento */ }
        }
        function touchPointerEntries() {
          return [...instance.activePointers.entries()].filter(([, pointer]) => pointer.type === 'touch');
        }
        function startPinchGesture() {
          const pointers = touchPointerEntries();
          if (pointers.length < 2) return false;
          const [first, second] = pointers.slice(0, 2);
          const a = first[1], b = second[1];
          const midpoint = {x:(a.x+b.x)/2, y:(a.y+b.y)/2};
          const distance = Math.max(1, Math.hypot(b.x-a.x, b.y-a.y));
          const view = getView();
          instance.pinchGesture = {
            pointerIds: [first[0], second[0]],
            startDistance: distance,
            startMidpoint: midpoint,
            startZoom: instance.state.zoom,
            startCenterX: side/2 + instance.state.off_x*view.ppm,
            startCenterY: side/2 - instance.state.off_y*view.ppm,
            ppm: view.ppm,
            changed: false
          };
          instance.activeDrag = null;
          canvas.classList.remove('moving', 'resizing');
          return true;
        }
        function applyPinchGesture() {
          const gesture = instance.pinchGesture;
          if (!gesture) return false;
          const first = instance.activePointers.get(gesture.pointerIds[0]);
          const second = instance.activePointers.get(gesture.pointerIds[1]);
          if (!first || !second) return false;
          const midpoint = {x:(first.x+second.x)/2, y:(first.y+second.y)/2};
          const distance = Math.max(1, Math.hypot(second.x-first.x, second.y-first.y));
          const rawFactor = distance / gesture.startDistance;
          instance.state.zoom = clamp(gesture.startZoom * rawFactor, 0.01, 20);
          const actualFactor = instance.state.zoom / Math.max(0.000001, gesture.startZoom);
          // Mantém o ponto da imagem que estava sob o centro dos dedos; ao
          // mesmo tempo, o deslocamento do centro da pinça produz pan natural.
          const nextCenterX = midpoint.x + (gesture.startCenterX-gesture.startMidpoint.x)*actualFactor;
          const nextCenterY = midpoint.y + (gesture.startCenterY-gesture.startMidpoint.y)*actualFactor;
          instance.state.off_x = (nextCenterX-side/2)/gesture.ppm;
          instance.state.off_y = (side/2-nextCenterY)/gesture.ppm;
          gesture.changed = true;
          redraw();
          updateStatus();
          return true;
        }
        function beginOneFingerPan(pointerId, pointer) {
          const view = getView();
          const rect = getRect(view, activeFitMode()==='art');
          instance.activeDrag = {
            mode: 'move', pointerId, view, rect,
            startPoint: {x:pointer.x, y:pointer.y},
            startZoom: instance.state.zoom,
            startX: instance.state.off_x,
            startY: instance.state.off_y
          };
          canvas.classList.add('moving');
          canvas.classList.remove('resizing');
        }
        function endSingleDrag() {
          if (!instance.activeDrag) return;
          instance.activeDrag = null;
          canvas.classList.remove('moving','resizing');
          draw(); updateStatus(); publish();
        }
        function finishTouchPointer(event) {
          const wasTrackedTouch = instance.activePointers.has(event.pointerId);
          if (!wasTrackedTouch) {
            if (instance.activeDrag && instance.activeDrag.pointerId === event.pointerId) endSingleDrag();
            return;
          }
          instance.activePointers.delete(event.pointerId);
          if (instance.pinchGesture) {
            const gestureChanged = instance.pinchGesture.changed;
            instance.pinchGesture = null;
            const remaining = touchPointerEntries();
            if (remaining.length >= 2) {
              // Se um dedo sair de uma pinça com três toques, inicia outra pinça
              // com os dois ponteiros restantes, sem congelar a edição.
              startPinchGesture();
              if (instance.pinchGesture) instance.pinchGesture.changed = gestureChanged;
              return;
            }
            if (remaining.length === 1) {
              // Ao soltar um dedo, o restante pode continuar arrastando a imagem
              // sem interromper o gesto ou exigir um novo toque.
              beginOneFingerPan(remaining[0][0], remaining[0][1]);
              return;
            }
            instance.activeDrag = null;
            canvas.classList.remove('moving','resizing');
            draw(); updateStatus();
            if (gestureChanged) publish();
            return;
          }
          if (instance.activeDrag && instance.activeDrag.pointerId === event.pointerId) {
            endSingleDrag();
          }
        }

        instance.image.onload = () => {
          instance.imageReady = true; instance.fixedView = null;
          instance.blurCanvasSignature = null; draw();
        };
        instance.backdropImage.onload = () => {
          instance.backdropReady = true; instance.blurCanvasSignature = null; draw();
        };
        instance.image.onerror = () => {
          ctx.fillStyle='#FFFFFF'; ctx.fillRect(0,0,side,side);
          ctx.fillStyle='#A33'; ctx.font='22px sans-serif';
          ctx.fillText('Não foi possível carregar a imagem',30,side/2);
        };

        canvas.addEventListener('pointerdown', event => {
          if (!instance.imageReady) return;
          const point = canvasPoint(event);
          const isTouch = event.pointerType === 'touch';
          if (isTouch) {
            instance.activePointers.set(event.pointerId, {x:point.x, y:point.y, type:'touch'});
            setCanvasPointerCapture(event);
            if (touchPointerEntries().length >= 2) {
              if (!instance.pinchGesture) startPinchGesture();
              event.preventDefault();
              return;
            }
          }
          const view = getView();
          const rect = getRect(view, activeFitMode()==='art');
          const handle = hitHandle(point, rect, isTouch ? 'touch' : 'mouse');
          const mode = handle ? 'resize' : (pointInsideRect(point, rect) ? 'move' : null);
          if (!mode) return;
          instance.activeDrag = {
            mode, handle, pointerId:event.pointerId, view, rect, startPoint:point,
            startZoom:instance.state.zoom,
            startX:instance.state.off_x,
            startY:instance.state.off_y
          };
          canvas.classList.toggle('moving', mode==='move');
          canvas.classList.toggle('resizing', mode==='resize');
          if (!isTouch) setCanvasPointerCapture(event);
          event.preventDefault();
        });
        canvas.addEventListener('pointermove', event => {
          if (instance.activePointers.has(event.pointerId)) {
            const point = canvasPoint(event);
            instance.activePointers.set(event.pointerId, {x:point.x, y:point.y, type:'touch'});
            if (instance.pinchGesture) {
              applyPinchGesture();
              event.preventDefault();
              return;
            }
          }
          if (!instance.activeDrag || instance.activeDrag.pointerId !== event.pointerId) return;
          const drag = instance.activeDrag;
          const point = canvasPoint(event);
          if (drag.mode === 'move') {
            const dx = (point.x-drag.startPoint.x)/drag.view.ppm;
            const dy = (point.y-drag.startPoint.y)/drag.view.ppm;
            instance.state.off_x = drag.startX + dx;
            instance.state.off_y = drag.startY - dy;
          } else {
            const h = drag.handle;
            const pivot = {
              x:drag.rect.cx-h.sx*drag.rect.w/2,
              y:drag.rect.cy-h.sy*drag.rect.h/2
            };
            // A alça visual pode estar encostada na borda do canvas, enquanto
            // o canto real da imagem está fora dele. Usamos o canto real como
            // referência e aplicamos somente o deslocamento feito pelo usuário.
            const startVector = {x:h.rawX-pivot.x, y:h.rawY-pivot.y};
            const currentVector = {
              x:h.rawX + (point.x-drag.startPoint.x) - pivot.x,
              y:h.rawY + (point.y-drag.startPoint.y) - pivot.y
            };
            const startDistance = Math.hypot(startVector.x,startVector.y);
            const currentDistance = Math.hypot(currentVector.x,currentVector.y);
            const factor = startDistance>0 ? currentDistance/startDistance : 1;
            const safeFactor = clamp(factor, 0.01/drag.startZoom, 20/drag.startZoom);
            instance.state.zoom = clamp(drag.startZoom*safeFactor,0.01,20);
            const newW = drag.rect.w*safeFactor;
            const newH = drag.rect.h*safeFactor;
            const newCx = pivot.x + h.sx*newW/2;
            const newCy = pivot.y + h.sy*newH/2;
            instance.state.off_x = (newCx-side/2)/drag.view.ppm;
            instance.state.off_y = (side/2-newCy)/drag.view.ppm;
          }
          redraw();
          event.preventDefault();
        });
        function releasePointer(event) {
          finishTouchPointer(event);
        }
        canvas.addEventListener('pointerup', releasePointer);
        canvas.addEventListener('pointercancel', releasePointer);
        canvas.addEventListener('wheel', event => {
          if (!instance.imageReady || instance.activeDrag) return;
          event.preventDefault();
          const factor = Math.exp(-event.deltaY*0.0012);
          instance.state.zoom = clamp(instance.state.zoom*factor,0.01,20);
          draw(); updateStatus();
          if (instance.wheelTimer) clearTimeout(instance.wheelTimer);
          instance.wheelTimer = setTimeout(() => {
            instance.wheelTimer = null;
            publish();
          }, 140);
        }, {passive:false});

        // Se o usuário clicar em outro controle logo após usar a roda, confirma
        // o zoom pendente antes que o Streamlit inicie o próximo rerun.
        instance.flushPendingWheel = () => {
          if (instance.wheelTimer) {
            clearTimeout(instance.wheelTimer);
            instance.wheelTimer = null;
            publish();
          }
        };
        instance.flushOutsidePointer = event => {
          if (!instance.root.contains(event.target)) instance.flushPendingWheel();
        };
        document.addEventListener('pointerdown', instance.flushOutsidePointer, true);
        instance.nodes = [stage, toolbar, hint];
        return instance;
      }

      // O host é mais estável que o elemento interno: Streamlit pode recriar
      // o root durante um rerun. Guardar a instância no host preserva canvas,
      // listeners e transformação entre esses ciclos.
      let instance = host.__atomicaEditorInstance || root.__atomicaEditorInstance;
      if (!instance) {
        instance = createInstance(root);
        host.__atomicaEditorInstance = instance;
        root.__atomicaEditorInstance = instance;
      } else {
        if (instance.root !== root || !instance.canvas.isConnected) {
          root.replaceChildren(...instance.nodes);
          instance.root = root;
          root.__atomicaEditorInstance = instance;
        }
        host.__atomicaEditorInstance = instance;
      }
      instance.setStateValue = setStateValue;

      const cfg = data || {};
      const incomingToken = String(cfg.token || '');
      const incomingTransform = readTransform(cfg.transform);
      const incomingSignature = transformSignature(incomingTransform);
      const incomingRevision = Math.max(0, Math.floor(Number(cfg.transform_revision ?? incomingTransform.revision) || 0));
      const tokenChanged = instance.token !== incomingToken;
      const revisionChanged = instance.transformRevision !== incomingRevision;
      const localSignature = transformSignature(instance.state);

      if (tokenChanged || revisionChanged) {
        // Uma nova imagem ou preset de enquadramento inicia a partir do estado
        // canônico enviado pelo Python. Eventos de uma revisão anterior deixam
        // de poder restaurar um zoom/deslocamento antigo.
        instance.token = incomingToken;
        instance.transformRevision = incomingRevision;
        instance.state = incomingTransform;
        instance.eventSequence = Number(incomingTransform.event_seq) || 0;
        instance.activeDrag = null;
        instance.activePointers.clear();
        instance.pinchGesture = null;
        instance.fixedView = null;
        instance.fixedViewSignature = null;
        instance.canvas.classList.remove('moving','resizing');
      } else if (!instance.activeDrag && incomingSignature !== localSignature) {
        instance.state = incomingTransform;
        instance.eventSequence = Math.max(
          Number(instance.eventSequence) || 0,
          Number(incomingTransform.event_seq) || 0
        );
      } else {
        instance.eventSequence = Math.max(
          Number(instance.eventSequence) || 0,
          Number(incomingTransform.event_seq) || 0
        );
      }
      instance.cfg = cfg;

      const nextImageSrc = String(cfg.image || '');
      if (nextImageSrc && nextImageSrc !== instance.imageSrc) {
        instance.imageSrc = nextImageSrc;
        instance.imageReady = false;
        instance.blurCanvasSignature = null;
        instance.image.src = nextImageSrc;
      }
      const nextBackdropSrc = String(cfg.backdrop_image || '');
      if (nextBackdropSrc && nextBackdropSrc !== instance.backdropSrc) {
        instance.backdropSrc = nextBackdropSrc;
        instance.backdropReady = false;
        instance.blurCanvasSignature = null;
        instance.backdropImage.src = nextBackdropSrc;
      }
      const nextCustomBackgroundSrc = String(cfg.custom_background_image || '');
      const nextCustomBackgroundRevision = Math.max(
        0, Math.floor(Number(cfg.custom_background_revision) || 0)
      );
      if (
        nextCustomBackgroundSrc !== instance.customBackgroundSrc ||
        nextCustomBackgroundRevision !== instance.customBackgroundRevision
      ) {
        instance.customBackgroundSrc = nextCustomBackgroundSrc;
        instance.customBackgroundRevision = nextCustomBackgroundRevision;
        instance.customBackgroundReady = false;

        // Nunca reutilizar o Image anterior nem atribuir src="". Em alguns
        // navegadores, src vazio pode solicitar a URL do documento e callbacks
        // atrasados podem voltar a desenhar um fundo que já foi removido.
        const replacementImage = new Image();
        instance.customBackgroundImage = replacementImage;
        if (nextCustomBackgroundSrc) {
          replacementImage.onload = () => {
            if (
              instance.customBackgroundImage !== replacementImage ||
              instance.customBackgroundSrc !== nextCustomBackgroundSrc ||
              instance.customBackgroundRevision !== nextCustomBackgroundRevision
            ) return;
            instance.customBackgroundReady = true;
            instance.blurCanvasSignature = null;
            instance.draw();
          };
          replacementImage.onerror = () => {
            if (instance.customBackgroundImage !== replacementImage) return;
            instance.customBackgroundReady = false;
            instance.draw();
          };
          replacementImage.src = nextCustomBackgroundSrc;
        } else {
          // Com a referência removida, o canvas redesenha imediatamente a
          // imagem principal usando novamente o preenchimento selecionado.
          instance.draw();
        }
      }
      const drawSignature = JSON.stringify([
        incomingToken, cfg.shape, cfg.cut, cfg.fold, cfg.art, cfg.safe,
        Array.isArray(cfg.guides) ? cfg.guides.join(',') : '',
        cfg.fit_mode, cfg.background_mode, cfg.blur_mm, cfg.bg,
        cfg.custom_background_image, cfg.custom_background_revision,
        instance.imageReady, instance.backdropReady,
        instance.customBackgroundReady,
        transformSignature(instance.state),
      ]);
      // Um rerun provocado por margem, disposição ou outro controle externo não
      // precisa redesenhar o canvas se nada que aparece nele tiver mudado.
      if (drawSignature !== instance.lastDrawSignature) {
        instance.draw();
        instance.lastDrawSignature = drawSignature;
      }
      instance.updateStatus();
    }
    """
    return st.components.v2.component(
        "atomica_visual_image_editor", html=html, css=css, js=js
    )


def restore_recommended_layout() -> None:
    """Callback executado antes da recriação dos widgets de margem e espaçamento."""
    st.session_state["margin_mm"] = float(st.session_state.get("_recommended_margin", 5.0))
    st.session_state["gap_mm"] = float(st.session_state.get("_recommended_gap", 3.0))


@st.cache_resource(show_spinner=False)
def get_artwork_pool_component():
    """Pool compacto com miniaturas, quantidade editável, arraste e preenchimento da folha."""
    html = """
    <div class="atomica-pool-shell">
      <div class="pool-batch-toolbar">
        <div class="pool-batch-copy">
          <strong>Selecione as artes no Pool</strong>
          <span id="pool-batch-status">0 selecionadas</span>
        </div>
        <div class="pool-batch-actions">
          <button type="button" class="pool-batch-secondary" id="pool-batch-select-all" disabled>Selecionar todas</button>
          <button type="button" class="pool-batch-danger" id="pool-batch-delete" disabled>EXCLUIR TODAS</button>
        </div>
      </div>
      <div class="atomica-pool-root" id="atomica-pool-root"></div>
      <div class="pool-toolbar">
        <div class="pool-fill-copy">
          <strong>Completar a folha</strong>
          <span id="pool-fill-status">Selecione uma arte para completar as posições restantes.</span>
        </div>
        <button type="button" class="pool-fill-button" id="pool-fill-button" disabled>
          Preencher folha
        </button>
      </div>
    </div>
    """
    css = r"""
    .atomica-pool-shell { width:100%; }
    .pool-toolbar {
      display:flex; align-items:center; justify-content:space-between; gap:14px;
      flex-wrap:wrap; margin:13px 0 0; padding:10px 13px;
      border:1px solid #29415F; border-radius:12px; background:#0B1B30;
    }
    .pool-fill-copy { display:flex; flex-direction:column; gap:3px; min-width:190px; }
    .pool-fill-copy strong { color:#F4F7FB; font-size:13px; }
    .pool-fill-copy span { color:#AFC0D4; font-size:11px; line-height:1.4; }
    .pool-fill-button {
      flex:0 0 auto; min-height:36px; padding:7px 14px; border-radius:9px;
      border:1px solid #26C6DA; background:#123D50; color:#E6FCFF;
      font-size:12px; font-weight:800; cursor:pointer;
    }
    .pool-fill-button:hover:not(:disabled) { background:#18566A; }
    .pool-fill-button:disabled { opacity:.38; cursor:not-allowed; }
    .pool-batch-toolbar {
      display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap;
      gap:10px; margin:0 0 11px; padding:10px 12px;
      border:1px solid #29415F; border-radius:11px; background:#0B1B30;
    }
    .pool-batch-copy { display:flex; flex-direction:column; gap:3px; min-width:180px; }
    .pool-batch-copy strong { color:#F4F7FB; font-size:12px; }
    .pool-batch-copy span { color:#AFC0D4; font-size:11px; }
    .pool-batch-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
    .pool-batch-secondary, .pool-batch-danger {
      min-height:36px; padding:7px 12px; border-radius:8px; font-size:11px;
      font-weight:800; cursor:pointer; border:1px solid #3A587B;
      background:#183657; color:#F4F7FB; white-space:nowrap;
    }
    .pool-batch-secondary:hover { background:#24517A; border-color:#67A4D7; }
    .pool-batch-danger { border-color:#8C4651; background:#48232D; color:#FFECEF; }
    .pool-batch-danger:hover:not(:disabled) { background:#66303B; border-color:#D46B78; }
    .pool-batch-danger:disabled { opacity:.35; cursor:not-allowed; }
    .atomica-pool-root {
      display:grid;
      grid-template-columns:repeat(auto-fill, minmax(min(100%, 136px), 1fr));
      justify-content:start;
      align-items:start;
      gap:11px;
      width:100%;
      max-width:980px;
      box-sizing:border-box;
    }
    .pool-card {
      position:relative;
      display:flex;
      flex-direction:column;
      align-items:stretch;
      gap:7px;
      box-sizing:border-box;
      width:100%;
      min-width:0;
      min-height:143px;
      padding:7px;
      border:1px solid #29415F;
      border-radius:12px;
      background:#0E203B;
      color:#F4F7FB;
      cursor:grab;
      user-select:none;
      transition:border-color .12s, background .12s, transform .12s;
    }
    .pool-card:hover { border-color:#4D83B7; background:#132B4B; transform:translateY(-1px); }
    /* A moldura verde identifica apenas a arte em foco para edição.
       A caixa azul no canto inferior direito continua sendo a seleção em lote. */
    .pool-card.selected {
      border:2px solid #2DBB72;
      padding:6px;
      background:#102D35;
      box-shadow:0 0 0 2px rgba(45,187,114,.16), 0 7px 20px rgba(0,0,0,.18);
    }
    .pool-card.dragging { opacity:.42; cursor:grabbing; transform:scale(.97); }
    .pool-thumb {
      display:block;
      width:100%;
      aspect-ratio:1 / 1;
      overflow:hidden;
      border-radius:8px;
      background:#FFFFFF;
      border:1px solid rgba(255,255,255,.12);
    }
    .pool-thumb img { display:block; width:100%; height:100%; object-fit:contain; }
    .pool-counter { display:flex; align-items:center; justify-content:center; gap:4px; width:100%; }
    .pool-counter button, .pool-remove {
      display:flex; align-items:center; justify-content:center;
      border:1px solid #3A587B; border-radius:8px; background:#183657; color:#F4F7FB;
      font-size:18px; line-height:1; width:26px; height:28px; padding:0; cursor:pointer;
      flex:0 0 auto;
    }
    .pool-counter button:hover, .pool-remove:hover { background:#24517A; border-color:#67A4D7; }
    .pool-counter button:disabled { opacity:.35; cursor:not-allowed; }
    .pool-qty {
      box-sizing:border-box; width:60px; min-width:60px; height:28px;
      padding:2px 2px; text-align:center; color:#FFFFFF; font-size:13px; font-weight:800;
      font-variant-numeric:tabular-nums; background:#0A1A2D; border:1px solid #3A587B;
      border-radius:7px; outline:none;
    }
    .pool-qty:focus { border-color:#26C6DA; box-shadow:0 0 0 2px rgba(38,198,218,.18); }
    .pool-qty::-webkit-inner-spin-button, .pool-qty::-webkit-outer-spin-button { opacity:1; }
    .pool-actions {
      position:absolute; top:11px; left:11px; right:11px;
      display:flex; align-items:flex-start; justify-content:space-between;
      pointer-events:none;
    }
    .pool-grip, .pool-remove, .pool-order-step { pointer-events:auto; box-shadow:0 1px 5px rgba(0,0,0,.28); }
    .pool-order-controls { display:none; align-items:center; gap:4px; pointer-events:none; }
    .pool-order-step {
      display:flex; align-items:center; justify-content:center; pointer-events:auto;
      width:27px; height:27px; border-radius:7px; padding:0;
      color:#FFFFFF; background:rgba(10,25,45,.86); border:1px solid rgba(255,255,255,.25);
      font-size:15px; font-weight:900; line-height:1; cursor:pointer;
    }
    .pool-order-step:disabled { opacity:.3; cursor:not-allowed; }
    .pool-grip {
      display:flex; align-items:center; justify-content:center;
      width:25px; height:25px; border-radius:7px;
      color:#FFFFFF; background:rgba(10,25,45,.78); border:1px solid rgba(255,255,255,.25);
      font-size:17px; line-height:1; cursor:grab;
    }
    .pool-remove { width:25px; height:25px; font-size:16px; background:rgba(10,25,45,.86); }
    .pool-batch-toggle {
      position:absolute; right:11px; bottom:36px; z-index:2;
      display:flex; align-items:center; justify-content:center;
      width:23px; height:23px; border-radius:7px; padding:0;
      border:1px solid #8AA0B8; background:rgba(8,22,39,.9); color:#FFFFFF;
      font-size:14px; font-weight:900; line-height:1; cursor:pointer;
      box-shadow:0 1px 5px rgba(0,0,0,.3);
    }
    .pool-batch-toggle:hover { border-color:#26C6DA; }
    .pool-batch-toggle.checked { color:#061B2D; background:#26C6DA; border-color:#FFFFFF; }
    @media (max-width:1024px) {
      .pool-order-controls { display:flex; }
      .pool-order-step { width:32px; height:32px; font-size:17px; }
      .pool-grip, .pool-remove { width:32px; height:32px; }
      .pool-batch-toggle { width:34px; height:34px; right:8px; bottom:38px; font-size:17px; }
      .pool-counter button { width:36px; height:36px; font-size:21px; }
      .pool-qty { width:64px; min-width:64px; height:36px; font-size:14px; }
      .pool-actions { top:8px; left:8px; right:8px; }
    }
    @media (max-width:520px) {
      .atomica-pool-root { grid-template-columns:repeat(auto-fill, minmax(min(100%, 124px), 1fr)); gap:8px; }
      .pool-card { width:100%; min-width:0; min-height:132px; padding:6px; }
      .pool-card.selected { padding:5px; }
      .pool-counter { gap:4px; }
      .pool-counter button { width:32px; height:36px; }
      .pool-qty { width:54px; min-width:54px; height:36px; font-size:13px; }
      .pool-actions { top:8px; left:8px; right:8px; }
      .pool-order-step, .pool-grip, .pool-remove { width:26px; height:28px; }
      .pool-order-controls { gap:1px; }
      .pool-batch-toggle { width:32px; height:32px; right:7px; bottom:38px; }
      .pool-toolbar { align-items:stretch; }
      .pool-fill-button { width:100%; }
      .pool-batch-toolbar { align-items:stretch; }
      .pool-batch-copy { min-width:0; width:100%; }
      .pool-batch-actions { width:100%; }
      .pool-batch-secondary, .pool-batch-danger { flex:1 1 0; min-height:42px; white-space:normal; }
    }
    """
    js = r"""
    export default function(component) {
      const { parentElement, data, setStateValue, setTriggerValue } = component;
      const root = parentElement.querySelector('#atomica-pool-root');
      const fillButton = parentElement.querySelector('#pool-fill-button');
      const fillStatus = parentElement.querySelector('#pool-fill-status');
      const batchStatus = parentElement.querySelector('#pool-batch-status');
      const selectAllButton = parentElement.querySelector('#pool-batch-select-all');
      const deleteButton = parentElement.querySelector('#pool-batch-delete');
      if (!root || !fillButton || !fillStatus || !batchStatus || !selectAllButton || !deleteButton) return;

      const items = Array.isArray(data?.items) ? data.items : [];
      const validIds = items.map(item => String(item.id));
      const validSet = new Set(validIds);
      let order = Array.isArray(data?.order) ? data.order.map(String).filter(id => validSet.has(id)) : [];
      validIds.forEach(id => { if (!order.includes(id)) order.push(id); });
      const quantities = { ...(data?.quantities || {}) };
      items.forEach(item => {
        const id = String(item.id);
        const value = Number(quantities[id] ?? item.qty ?? 1);
        quantities[id] = Math.max(1, Math.min(2000, Number.isFinite(value) ? Math.round(value) : 1));
      });
      const perPage = Math.max(0, Math.floor(Number(data?.per_page) || 0));
      let selectedId = String(data?.selected_id ?? (validIds[0] || ''));
      if (!validSet.has(selectedId)) selectedId = validIds[0] || '';
      const batchSelectedIds = new Set(
        (Array.isArray(data?.selected_ids) ? data.selected_ids : [])
          .map(String).filter(id => validSet.has(id))
      );
      let draggedId = null;
      const byId = new Map(items.map(item => [String(item.id), item]));
      root.__atomicaPoolSelectedId = selectedId;
      // Mantém o estado do componente sincronizado quando a seleção muda fora
      // do Pool (por exemplo, pelas setas junto à prévia de ajuste).
      const previousPropSelectedId = root.dataset.atomicaPoolPropSelectedId;
      if (previousPropSelectedId !== undefined && previousPropSelectedId !== selectedId) {
        setStateValue('selected_id', selectedId);
      }
      root.dataset.atomicaPoolPropSelectedId = selectedId;
      const incomingBatchSelection = order.filter(id => batchSelectedIds.has(id));
      const incomingBatchSelectionSignature = incomingBatchSelection.join('|');
      const previousPropBatchSelectionSignature = root.dataset.atomicaPoolPropBatchSelection;
      if (previousPropBatchSelectionSignature === undefined ||
          previousPropBatchSelectionSignature !== incomingBatchSelectionSignature) {
        // O estado do Python é a fonte de verdade quando uma operação em lote
        // termina e limpa a seleção programaticamente.
        setStateValue('selected_ids', incomingBatchSelection);
      }
      root.dataset.atomicaPoolPropBatchSelection = incomingBatchSelectionSignature;
      root.__atomicaPoolBatchSelectedIds = batchSelectedIds;
      root.__atomicaPoolBatchSelectedCount = batchSelectedIds.size;
      // O componente recebe novas props em cada rerun. A troca da arte em foco
      // e a seleção em lote atualizam apenas as classes/ícones; o DOM das thumbs
      // permanece montado para evitar decodificar novamente todas as imagens.
      root.__atomicaPoolSetStateValue = setStateValue;
      root.__atomicaPoolSetTriggerValue = setTriggerValue;
      const itemRenderSignature = JSON.stringify(
        items.map(item => [String(item.id), String(item.thumb || '').length])
      );
      function currentRenderSignature() {
        // A seleção da miniatura em edição não altera as imagens do Pool. Ela é
        // atualizada em place para evitar desmontar/redecodificar todas as thumbs.
        return JSON.stringify([itemRenderSignature, order, quantities]);
      }
      const renderSignature = currentRenderSignature();

      function publishOrder() { root.__atomicaPoolSetStateValue('order', [...order]); }
      function publishQuantities() { root.__atomicaPoolSetStateValue('quantities', { ...quantities }); }
      function publishSelection() {
        root.__atomicaPoolSetStateValue('selected_id', String(root.__atomicaPoolSelectedId || ''));
      }
      function publishBatchSelection() {
        const selectedSet = root.__atomicaPoolBatchSelectedIds || new Set();
        root.__atomicaPoolSetStateValue('selected_ids', order.filter(id => selectedSet.has(id)));
      }
      function syncCardSelectionState() {
        const activeId = String(root.__atomicaPoolSelectedId || '');
        root.querySelectorAll('.pool-card').forEach(card => {
          const id = String(card.dataset.id || '');
          const isFocused = id === activeId;
          card.classList.toggle('selected', isFocused);
          const batchToggle = card.querySelector('.pool-batch-toggle');
          if (batchToggle) {
            const selectedSet = root.__atomicaPoolBatchSelectedIds || new Set();
            const batchSelected = selectedSet.has(id);
            batchToggle.classList.toggle('checked', batchSelected);
            batchToggle.textContent = batchSelected ? '✓' : '□';
            batchToggle.title = batchSelected ? 'Remover da seleção em lote' : 'Incluir na seleção em lote';
            batchToggle.setAttribute('aria-pressed', batchSelected ? 'true' : 'false');
            batchToggle.setAttribute('aria-checked', batchSelected ? 'true' : 'false');
            batchToggle.setAttribute('aria-label', batchSelected ? 'Desmarcar arte para ajuste em lote' : 'Marcar arte para ajuste em lote');
          }
        });
      }
      function updateBatchControls() {
        const selectedSet = root.__atomicaPoolBatchSelectedIds || batchSelectedIds;
        const count = selectedSet.size;
        root.__atomicaPoolBatchSelectedCount = count;
        batchStatus.textContent = `${count} arte${count === 1 ? '' : 's'} selecionada${count === 1 ? '' : 's'}`;
        const allSelected = validSet.size > 0 && count === validSet.size;
        selectAllButton.textContent = allSelected ? 'Desselecionar todas' : 'Selecionar todas';
        selectAllButton.disabled = validSet.size === 0;
        const busy = deleteButton.dataset.busy === '1';
        if (!busy) deleteButton.textContent = count > 0 ? 'EXCLUIR SELEÇÃO' : 'EXCLUIR TODAS';
        deleteButton.disabled = validSet.size === 0 || busy;
      }
      root.__atomicaPoolSyncCardSelectionState = syncCardSelectionState;
      root.__atomicaPoolPublishSelection = publishSelection;
      root.__atomicaPoolPublishBatchSelection = publishBatchSelection;
      root.__atomicaPoolUpdateBatchControls = updateBatchControls;
      root.__atomicaPoolResetDeleteButton = (nonce) => {
        if (nonce && Number(root.__atomicaPoolDeleteNonce) !== Number(nonce)) return;
        if (root.__atomicaPoolDeleteResetTimer) clearTimeout(root.__atomicaPoolDeleteResetTimer);
        root.__atomicaPoolDeleteNonce = null;
        deleteButton.dataset.busy = '0';
        updateBatchControls();
      };
      selectAllButton.onclick = event => {
        event.preventDefault(); event.stopPropagation();
        const selectedSet = root.__atomicaPoolBatchSelectedIds || batchSelectedIds;
        const allSelected = validSet.size > 0 && selectedSet.size === validSet.size;
        if (allSelected) selectedSet.clear();
        else validIds.forEach(id => selectedSet.add(id));
        root.__atomicaPoolBatchSelectedIds = selectedSet;
        root.__atomicaPoolBatchSelectedCount = selectedSet.size;
        syncCardSelectionState(); publishBatchSelection(); updateBatchControls();
      };
      deleteButton.onclick = event => {
        event.preventDefault(); event.stopPropagation();
        if (deleteButton.dataset.busy === '1') return;
        const selectedSet = root.__atomicaPoolBatchSelectedIds || batchSelectedIds;
        const deletingSelection = selectedSet.size > 0;
        const idsToDelete = deletingSelection
          ? order.filter(id => selectedSet.has(id))
          : [...order];
        if (!idsToDelete.length) return;
        const confirmation = deletingSelection
          ? `Excluir ${idsToDelete.length} arte(s) selecionada(s) do Pool?`
          : `Excluir TODAS as ${idsToDelete.length} artes do Pool? Esta ação não pode ser desfeita.`;
        if (!window.confirm(confirmation)) return;
        const nonce = Date.now();
        root.__atomicaPoolDeleteNonce = nonce;
        deleteButton.dataset.busy = '1';
        deleteButton.disabled = true;
        deleteButton.textContent = 'EXCLUINDO…';
        root.__atomicaPoolSetTriggerValue('delete_ids', {
          ids: idsToDelete,
          nonce,
          mode: deletingSelection ? 'selected' : 'all',
        });
        // Salvaguarda para o caso de uma versão do componente não devolver o
        // callback; no próximo rerun o ack oficial limpa o estado antes disso.
        if (root.__atomicaPoolDeleteResetTimer) clearTimeout(root.__atomicaPoolDeleteResetTimer);
        root.__atomicaPoolDeleteResetTimer = setTimeout(() => {
          if (typeof root.__atomicaPoolResetDeleteButton === 'function') {
            root.__atomicaPoolResetDeleteButton(nonce);
          }
        }, 5000);
      };
      function totalQuantity() {
        return Object.values(quantities).reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0);
      }
      function remainingForSheet() {
        if (perPage <= 0) return 0;
        return perPage - (totalQuantity() % perPage);
      }
      function updateFillControls() {
        const amount = remainingForSheet();
        const total = totalQuantity();
        const used = perPage > 0 ? total % perPage : 0;
        const currentSelectedId = String(root.__atomicaPoolSelectedId || selectedId);
        const selectedQty = Number(quantities[currentSelectedId] || 0);
        const selectedExists = validSet.has(currentSelectedId);
        const exceedsLimit = selectedExists && selectedQty + amount > 2000;
        if (!perPage) {
          fillStatus.textContent = 'Ajuste a margem e o espaçamento para calcular a capacidade da folha.';
          fillButton.textContent = 'Preencher folha';
          fillButton.disabled = true;
          fillButton.title = 'Não há posições disponíveis com o layout atual.';
          return;
        }
        fillStatus.textContent = used === 0
          ? `A próxima folha está vazia: ${amount} posição(ões) disponíveis.`
          : `Restam ${amount} posição(ões) na última folha.`;
        fillButton.textContent = `Preencher folha (+${amount})`;
        fillButton.disabled = !selectedExists || exceedsLimit;
        fillButton.title = exceedsLimit
          ? 'O limite de 2.000 unidades por arte impede completar a folha com esta imagem.'
          : (selectedExists ? `Adicionar ${amount} cópia(s) à arte selecionada.` : 'Selecione uma arte no pool.');
      }
      root.__atomicaPoolUpdateFillControls = updateFillControls;
      fillButton.onclick = event => {
        event.preventDefault();
        event.stopPropagation();
        const amount = remainingForSheet();
        const currentSelectedId = String(root.__atomicaPoolSelectedId || selectedId);
        if (!validSet.has(currentSelectedId) || amount <= 0 || Number(quantities[currentSelectedId] || 0) + amount > 2000) return;
        quantities[currentSelectedId] += amount;
        render();
        publishQuantities();
        updateFillControls();
      };

      function render() {
        root.replaceChildren();
        order.forEach(id => {
          const item = byId.get(id);
          if (!item) return;
          const card = document.createElement('div');
          card.className = 'pool-card' + (id === selectedId ? ' selected' : '');
          card.dataset.id = id;
          card.draggable = true;
          card.tabIndex = 0;
          card.setAttribute('role', 'button');
          card.setAttribute('aria-label', 'Selecionar miniatura para a prévia; arrastar para ordenar');

          const thumb = document.createElement('div');
          thumb.className = 'pool-thumb';
          const img = document.createElement('img');
          img.src = item.thumb;
          img.alt = 'Prévia quadrada da arte';
          img.draggable = false;
          thumb.appendChild(img);

          const counter = document.createElement('div');
          counter.className = 'pool-counter';
          const minus = document.createElement('button');
          minus.type = 'button'; minus.textContent = '−'; minus.title = 'Diminuir quantidade';
          minus.disabled = quantities[id] <= 1;
          const qty = document.createElement('input');
          qty.className = 'pool-qty'; qty.type = 'number';
          qty.min = '1'; qty.max = '2000'; qty.step = '1'; qty.value = String(quantities[id]);
          qty.setAttribute('aria-label', 'Quantidade de cópias desta arte');
          qty.title = 'Digite a quantidade desejada';
          qty.addEventListener('click', event => event.stopPropagation());
          qty.addEventListener('pointerdown', event => event.stopPropagation());
          qty.addEventListener('keydown', event => {
            event.stopPropagation();
            if (event.key === 'Enter') qty.blur();
          });
          const plus = document.createElement('button');
          plus.type = 'button'; plus.textContent = '+'; plus.title = 'Aumentar quantidade';
          plus.disabled = quantities[id] >= 2000;

          function commitQuantity() {
            const parsed = Number(qty.value);
            const next = Number.isFinite(parsed) ? Math.max(1, Math.min(2000, Math.round(parsed))) : quantities[id];
            quantities[id] = next;
            qty.value = String(next);
            minus.disabled = next <= 1;
            plus.disabled = next >= 2000;
            publishQuantities();
            root.__atomicaPoolUpdateFillControls();
          }
          qty.addEventListener('change', commitQuantity);
          minus.addEventListener('click', event => {
            event.stopPropagation();
            quantities[id] = Math.max(1, quantities[id] - 1);
            render(); publishQuantities(); root.__atomicaPoolUpdateFillControls();
          });
          plus.addEventListener('click', event => {
            event.stopPropagation();
            quantities[id] = Math.min(2000, quantities[id] + 1);
            render(); publishQuantities(); root.__atomicaPoolUpdateFillControls();
          });
          counter.append(minus, qty, plus);

          const actions = document.createElement('div');
          actions.className = 'pool-actions';
          const grip = document.createElement('span');
          grip.className = 'pool-grip'; grip.textContent = '⠿'; grip.title = 'Arraste para ordenar';
          const remove = document.createElement('button');
          remove.type = 'button'; remove.className = 'pool-remove'; remove.textContent = '×';
          remove.title = 'Remover esta arte';
          remove.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            // A exclusão deve ser um único evento. Publicar também ordem/seleção
            // no mesmo clique criava reruns concorrentes e podia reintroduzir a
            // miniatura com a ordem antiga antes de o Python processar remove_id.
            remove.disabled = true;
            remove.textContent = '…';
            root.__atomicaPoolSetTriggerValue('remove_id', id);
          });
          const orderControls = document.createElement('div');
          orderControls.className = 'pool-order-controls';
          const moveUp = document.createElement('button');
          moveUp.type = 'button'; moveUp.className = 'pool-order-step';
          moveUp.textContent = '↑'; moveUp.title = 'Mover uma posição para cima';
          moveUp.setAttribute('aria-label', 'Mover arte para cima na ordem de impressão');
          moveUp.disabled = order.indexOf(id) <= 0;
          moveUp.addEventListener('click', event => {
            event.preventDefault(); event.stopPropagation();
            const from = order.indexOf(id);
            if (from <= 0) return;
            order.splice(from, 1); order.splice(from - 1, 0, id);
            render(); publishOrder(); root.__atomicaPoolUpdateFillControls();
          });
          const moveDown = document.createElement('button');
          moveDown.type = 'button'; moveDown.className = 'pool-order-step';
          moveDown.textContent = '↓'; moveDown.title = 'Mover uma posição para baixo';
          moveDown.setAttribute('aria-label', 'Mover arte para baixo na ordem de impressão');
          moveDown.disabled = order.indexOf(id) >= order.length - 1;
          moveDown.addEventListener('click', event => {
            event.preventDefault(); event.stopPropagation();
            const from = order.indexOf(id);
            if (from < 0 || from >= order.length - 1) return;
            order.splice(from, 1); order.splice(from + 1, 0, id);
            render(); publishOrder(); root.__atomicaPoolUpdateFillControls();
          });
          orderControls.append(moveUp, moveDown);
          actions.append(grip, orderControls, remove);

          card.append(thumb, counter, actions);
          const batchToggle = document.createElement('button');
          const isBatchSelected = batchSelectedIds.has(id);
          batchToggle.type = 'button';
          batchToggle.className = 'pool-batch-toggle' + (isBatchSelected ? ' checked' : '');
          batchToggle.textContent = isBatchSelected ? '✓' : '□';
          batchToggle.title = isBatchSelected ? 'Remover da seleção em lote' : 'Incluir na seleção em lote';
          batchToggle.setAttribute('aria-pressed', isBatchSelected ? 'true' : 'false');
          batchToggle.setAttribute('role', 'checkbox');
          batchToggle.setAttribute('aria-checked', isBatchSelected ? 'true' : 'false');
          batchToggle.setAttribute('aria-label', isBatchSelected ? 'Desmarcar arte para ajuste em lote' : 'Marcar arte para ajuste em lote');
          // O botão é filho do cartão, que também responde ao toque para abrir a arte.
          // Interrompe a propagação do pointerdown sem cancelar o clique nativo do botão.
          batchToggle.addEventListener('pointerdown', event => event.stopPropagation(), { passive: true });
          batchToggle.addEventListener('click', event => {
            event.preventDefault(); event.stopPropagation();
            const selectedSet = root.__atomicaPoolBatchSelectedIds || new Set();
            if (selectedSet.has(id)) selectedSet.delete(id);
            else selectedSet.add(id);
            root.__atomicaPoolBatchSelectedIds = selectedSet;
            root.__atomicaPoolBatchSelectedCount = selectedSet.size;
            syncCardSelectionState(); publishBatchSelection(); updateBatchControls();
          });
          card.appendChild(batchToggle);
          card.addEventListener('click', () => {
            if (String(root.__atomicaPoolSelectedId || '') !== id) {
              root.__atomicaPoolSelectedId = id;
              if (typeof root.__atomicaPoolSyncCardSelectionState === 'function') root.__atomicaPoolSyncCardSelectionState();
              if (typeof root.__atomicaPoolPublishSelection === 'function') root.__atomicaPoolPublishSelection();
              if (typeof root.__atomicaPoolUpdateFillControls === 'function') root.__atomicaPoolUpdateFillControls();
            }
          });
          card.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              root.__atomicaPoolSelectedId = id;
              if (typeof root.__atomicaPoolSyncCardSelectionState === 'function') root.__atomicaPoolSyncCardSelectionState();
              if (typeof root.__atomicaPoolPublishSelection === 'function') root.__atomicaPoolPublishSelection();
              if (typeof root.__atomicaPoolUpdateFillControls === 'function') root.__atomicaPoolUpdateFillControls();
            }
          });
          card.addEventListener('dragstart', event => {
            draggedId = id; card.classList.add('dragging');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', id);
          });
          card.addEventListener('dragend', () => { draggedId = null; card.classList.remove('dragging'); });
          card.addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; });
          card.addEventListener('drop', event => {
            event.preventDefault(); event.stopPropagation();
            const targetId = id;
            if (!draggedId || draggedId === targetId) return;
            const from = order.indexOf(draggedId);
            let to = order.indexOf(targetId);
            const rect = card.getBoundingClientRect();
            const sameRow = event.clientY >= rect.top && event.clientY <= rect.bottom;
            const after = sameRow
              ? event.clientX > rect.left + rect.width / 2
              : event.clientY > rect.top + rect.height / 2;
            if (after) to += 1;
            order.splice(from, 1);
            if (from < to) to -= 1;
            order.splice(Math.max(0, Math.min(order.length, to)), 0, draggedId);
            draggedId = null; render(); publishOrder(); root.__atomicaPoolUpdateFillControls();
          });
          root.appendChild(card);
        });
        // Registra o novo estado local: o rerun provocado por este evento não
        // precisará reconstruir os mesmos cartões uma segunda vez.
        root.dataset.atomicaPoolRenderSignature = currentRenderSignature();
        root.__atomicaPoolUpdateFillControls();
        updateBatchControls();
      }
      if (root.dataset.atomicaPoolRenderSignature !== renderSignature) {
        render();
        root.dataset.atomicaPoolRenderSignature = currentRenderSignature();
      } else {
        // Atualiza seleção e controles em place; as miniaturas existentes não
        // são desmontadas nem precisam ser baixadas/decodificadas novamente.
        syncCardSelectionState();
        root.__atomicaPoolUpdateFillControls();
        updateBatchControls();
      }
      const deleteAck = Number(data?.delete_ack || 0);
      if (deleteAck && root.__atomicaPoolDeleteNonce && deleteAck === Number(root.__atomicaPoolDeleteNonce)) {
        root.__atomicaPoolResetDeleteButton(deleteAck);
      }
    }
    """
    return st.components.v2.component(
        "atomica_artwork_pool", html=html, css=css, js=js
    )

@st.cache_data(show_spinner=False, max_entries=256, hash_funcs={bytes: lambda _value: 0})
def render_button_screen_preview_cached(
    image_digest: str,
    image_bytes: bytes,
    spec: ButtonSpec,
    ppm: float,
    zoom: float,
    off_x: float,
    off_y: float,
    fit_mode: str,
    background_mode: str,
    blur_mm: float,
    item_bg: str,
    background_image_digest: str,
    background_image_bytes: bytes,
    guides: tuple,
) -> Image.Image:
    """Cacheia cada botton individual; alterar uma arte não redesenha as demais."""
    image = load_image_preview(image_digest, image_bytes, 1200)
    custom_background = (
        load_image_preview(background_image_digest, background_image_bytes, 1200)
        if background_image_bytes else None
    )
    return render_button(
        spec, image, ppm=ppm, zoom=zoom, off_x=off_x, off_y=off_y,
        bg=item_bg, guides=guides, pad_mm=0.5, line_mm=0.25,
        fit_mode=fit_mode, blur_mm=blur_mm, background_mode=background_mode,
        background_image=custom_background,
        ss=2,
    )


@st.cache_data(show_spinner=False, max_entries=16, hash_funcs={bytes: lambda _value: 0})
def render_mixed_sheet_preview_cached(
    item_payload: tuple,
    placements: tuple,
    spec: ButtonSpec,
    page_w: float,
    page_h: float,
    bg: str,
    guides: tuple,
    preview_ppm: float | None = None,
    footer_margin_mm: float = 5.0,
) -> Image.Image:
    """Monta a prévia reutilizando imagens de bottons já rasterizadas em cache.

    preview_ppm permite gerar uma cópia independente em resolução de tela
    (96 CSS px/pol.) para a visualização física calibrável.
    """
    ppm = (
        float(preview_ppm)
        if preview_ppm is not None and math.isfinite(float(preview_ppm)) and float(preview_ppm) > 0
        else sheet_preview_pixels_per_mm(page_w, page_h)
    )
    sheet = Image.new("RGB", (round(page_w * ppm), round(page_h * ppm)), "white")
    ImageDraw.Draw(sheet).rectangle([0, 0, sheet.width - 1, sheet.height - 1], outline="#C9CED6")
    for item_index, cx, cy in placements:
        payload = item_payload[item_index]
        (
            image_digest, image_bytes, zoom, off_x, off_y, fit_mode,
            background_mode, blur_mm, item_bg, background_image_digest,
            background_image_bytes,
        ) = payload
        button = render_button_screen_preview_cached(
            image_digest, image_bytes, spec, ppm, zoom, off_x, off_y,
            fit_mode, background_mode, blur_mm, item_bg,
            background_image_digest, background_image_bytes, guides,
        )
        sheet.paste(
            button,
            (round(cx * ppm - button.width / 2), round(cy * ppm - button.height / 2)),
            button,
        )
    _draw_sheet_brand_footer_pil(sheet, ppm, footer_margin_mm)
    return sheet


def build_button_art_png(
    spec: ButtonSpec,
    img: Image.Image,
    *,
    dpi: int,
    zoom: float,
    off_x: float,
    off_y: float,
    bg: str,
    fit_mode: str,
    background_mode: str,
    blur_mm: float,
    background_image: Image.Image | None = None,
) -> tuple[bytes, tuple[int, int]]:
    """Exporta PNG no tamanho físico; artes redondas recebem transparência fora do corte."""
    ppm = dpi / 25.4
    artwork = render_artwork(
        spec,
        img,
        ppm=ppm,
        zoom=zoom,
        off_x=off_x,
        off_y=off_y,
        bg=bg,
        fit_mode=fit_mode,
        background_mode=background_mode,
        blur_mm=blur_mm,
        background_image=background_image,
    )
    if spec.shape == "circle":
        # PNG individual redondo: preserva a arte e o preenchimento dentro do
        # corte, mas remove os quatro cantos externos com transparência real.
        rgba = artwork.convert("RGBA")
        scale = 2
        hi_size = (rgba.width * scale, rgba.height * scale)
        hi_mask = Image.new("L", hi_size, 0)
        radius = (spec.cut * ppm / 2) * scale
        center_x, center_y = hi_size[0] / 2, hi_size[1] / 2
        ImageDraw.Draw(hi_mask).ellipse(
            [center_x - radius, center_y - radius, center_x + radius, center_y + radius],
            fill=255,
        )
        circle_mask = hi_mask.resize(rgba.size, Image.Resampling.LANCZOS)
        rgba.putalpha(ImageChops.multiply(rgba.getchannel("A"), circle_mask))
        artwork = rgba
    buffer = io.BytesIO()
    artwork.save(buffer, format="PNG", dpi=(dpi, dpi), optimize=True)
    return buffer.getvalue(), artwork.size


def build_template_svg(spec: ButtonSpec) -> bytes:
    """Gabarito SVG A4 colorido, transparente e dimensionado em milímetros."""
    page_w, page_h = 210.0, 297.0
    cx, cy = page_w / 2, 132.0
    shape_title = "quadrado" if spec.shape == "square" else "redondo"
    size_title = f"{spec.size:g} x {spec.size:g} mm" if spec.shape == "square" else f"{spec.size:g} mm"
    guide_elements = []

    def dash_attributes(guide: Guide) -> str:
        if guide.style == "dashed":
            return ' stroke-dasharray="2.2 1.4"'
        if guide.style == "dotted":
            return ' stroke-dasharray="0.1 1.6" stroke-linecap="round"'
        return ""

    def guide_geometry(guide: Guide, stroke: str, stroke_width: float) -> str:
        diameter = float(getattr(spec, guide.attr))
        dash = dash_attributes(guide)
        if spec.shape == "square":
            half = diameter / 2
            x, y = cx - half, cy - half
            return (
                f'<rect x="{x:.4f}" y="{y:.4f}" width="{diameter:.4f}" '
                f'height="{diameter:.4f}" fill="none" stroke="{stroke}" '
                f'stroke-width="{stroke_width:.3f}"{dash}/>'
            )
        return (
            f'<circle cx="{cx:.4f}" cy="{cy:.4f}" r="{diameter / 2:.4f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{stroke_width:.3f}"{dash}/>'
        )

    # Cada guia recebe uma linha escura externa e uma linha colorida interna.
    # Isso preserva as cores de identificação e mantém a guia amarela legível
    # quando o SVG é aberto sobre uma página branca. Nenhuma forma de fundo é
    # desenhada: a prancheta e as áreas externas às guias permanecem transparentes.
    for guide in GUIDES:
        guide_elements.append(guide_geometry(guide, "#243447", 0.48))
        guide_elements.append(guide_geometry(guide, guide.color, 0.22))

    legend_y = 226.0
    legend_rows = []
    for guide in GUIDES:
        diameter = float(getattr(spec, guide.attr))
        y = legend_y + len(legend_rows) * 10.0
        dash = dash_attributes(guide)
        legend_rows.append(
            f'<line x1="16" y1="{y:.2f}" x2="29" y2="{y:.2f}" '
            f'stroke="#243447" stroke-width="1.05"{dash}/>'
            f'<line x1="16" y1="{y:.2f}" x2="29" y2="{y:.2f}" '
            f'stroke="{guide.color}" stroke-width="0.58"{dash}/>'
            f'<text x="34" y="{y + 1.2:.2f}" font-family="Arial, sans-serif" '
            f'font-size="3.4" fill="#222222">{guide.label}: {diameter:g} mm</text>'
        )

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297" version="1.1" style="background-color:transparent">
  <title>Gabarito A4 - Botton {shape_title} {size_title}</title>
  <desc>Gabarito vetorial colorido em escala real. Prancheta A4 de 210 por 297 mm. Corte em magenta, Dobra em amarelo, Arte em azul e Segurança em verde. Fundo transparente, sem preenchimento de página.</desc>
  <g id="guias-botton" fill="none" stroke-linejoin="round" stroke-linecap="round">
    {''.join(guide_elements)}
  </g>
  <g id="identificacao" font-family="Arial, sans-serif" fill="#222222">
    <text x="105" y="18" text-anchor="middle" font-size="5.0" font-weight="700">GABARITO DE BOTTON</text>
    <text x="105" y="25" text-anchor="middle" font-size="3.5">Formato {shape_title} · {size_title}</text>
    <text x="16" y="216" font-size="3.6" font-weight="700">DIMENSÕES DAS GUIAS</text>
    {''.join(legend_rows)}
    <text x="16" y="278" font-size="2.8" fill="#555555">Documento A4 · 210 × 297 mm · escala real 1:1</text>
    <text x="16" y="283" font-size="2.8" fill="#555555">Fundo transparente. Importe sem redimensionar para manter as medidas originais.</text>
  </g>
</svg>"""
    return svg.encode("utf-8")


def _sync_layout_from_capacity_choice() -> None:
    """Mapeia a opção funcional de capacidade para o modo de distribuição."""
    choice = str(st.session_state.get("layout_capacity_choice", ""))
    if choice.startswith("Grade:"):
        st.session_state["layout_label"] = "Grade"
    elif choice.startswith("Hexagonal:"):
        st.session_state["layout_label"] = "Hexagonal (mais compacta)"


def _step_sheet_preview_page(delta: int, total_pages: int) -> None:
    """Navega a prévia sem alterar a paginação/exportação do PDF."""
    current = int(st.session_state.get("_sheet_preview_page_selector", 1) or 1)
    st.session_state["_sheet_preview_page_selector"] = max(1, min(int(total_pages), current + int(delta)))


def _get_items_for_sheet_page(runtime_groups, page_number: int, per_page: int):
    """Retorna somente os itens que ocupam a página indicada, respeitando a ordem do Pool."""
    start = max(0, (int(page_number) - 1) * int(per_page))
    end = start + max(0, int(per_page))
    cursor = 0
    page_items = []
    for entry in runtime_groups:
        qty = max(0, int(entry.get("qty", 0)))
        overlap_start = max(start, cursor)
        overlap_end = min(end, cursor + qty)
        count = max(0, overlap_end - overlap_start)
        if count:
            page_items.extend([entry] * count)
        cursor += qty
        if cursor >= end:
            break
    return page_items


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------


def legend_html(spec: ButtonSpec) -> str:
    """Legenda das guias, com o mesmo traço/cor usado nas prévias e no PDF."""
    rows = "".join(
        f'<div class="lg"><span class="lg-sw" style="border-color:{g.color};border-style:{g.style}"></span>'
        f"{g.label}<i>{getattr(spec, g.attr):g} mm</i></div>"
        for g in GUIDES
    )
    return f'<div class="legend">{rows}</div>'



def migrate_mixed_items_state():
    """Converte registros antigos com quantidade em bottons individuais."""
    if st.session_state.get("mixed_items_schema") == 2:
        return

    old_items = st.session_state.get("mixed_items", [])
    known_ids = {
        int(item["id"])
        for item in old_items
        if str(item.get("id", "")).isdigit()
    }
    next_id = max(
        max(known_ids, default=0),
        int(st.session_state.get("mixed_next_id", 1)) - 1,
    ) + 1
    used_ids = set()
    migrated = []

    for old in old_items:
        count = max(1, int(old.get("qty", 1)))
        old_id = old.get("id")
        for copy_index in range(count):
            item = dict(old)
            preferred_id = int(old_id) if copy_index == 0 and str(old_id).isdigit() else None
            if preferred_id is not None and preferred_id not in used_ids:
                item["id"] = preferred_id
                used_ids.add(preferred_id)
            else:
                while next_id in used_ids:
                    next_id += 1
                item["id"] = next_id
                used_ids.add(next_id)
                next_id += 1

            item["qty"] = 1
            item.setdefault("zoom", 1.0)
            item.setdefault("event_seq", 0)
            item.setdefault("off_x", 0.0)
            item.setdefault("off_y", 0.0)
            item.setdefault("fit_mode", "cut")
            item.setdefault("blur_fill", True)
            item.setdefault("blur_mm", 0.5)
            migrated.append(item)

    st.session_state["mixed_items"] = migrated
    st.session_state["mixed_next_id"] = max(
        int(st.session_state.get("mixed_next_id", 1)), next_id
    )
    st.session_state["mixed_items_schema"] = 2



def _button_scale_profile(spec: ButtonSpec) -> dict:
    """Perfil geométrico necessário para converter um ajuste entre tamanhos."""
    return {
        "size": float(spec.size),
        "cut": float(spec.cut),
        "fold": float(spec.fold),
        "art": float(spec.art),
        "safe": float(spec.safe),
        "shape": str(spec.shape),
    }


def _legacy_scale_profile(saved: dict, current_spec: ButtonSpec, mode: str) -> dict | None:
    """Recupera o perfil salvo em versões que guardavam apenas um diâmetro-base.

    A referência passou a ser a guia Segurança. As versões anteriores também
    podem ter gravado Arte ou Corte; procurar as três medidas permite migrar
    sessões antigas, inclusive o preset de 18 mm (Segurança = 12 mm).
    """
    old_diameter = saved.get("scale_reference_diameter")
    try:
        old_diameter = float(old_diameter)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(old_diameter) or old_diameter <= 0:
        return None

    matches = []
    for attr in ("safe", "art", "cut"):
        matches = [
            candidate for candidate in BUTTONS.values()
            if candidate.shape == current_spec.shape
            and math.isclose(float(getattr(candidate, attr)), old_diameter, rel_tol=0, abs_tol=1e-6)
        ]
        if matches:
            break
    if not matches:
        return None
    # Alguns perfis podem compartilhar uma medida (p.ex., Corte 32/33 mm);
    # escolhe o tamanho nominal mais próximo do destino selecionado.
    previous = min(matches, key=lambda candidate: abs(candidate.size - current_spec.size))
    return _button_scale_profile(previous)


def _safety_relative_transform(saved: dict, profile: dict | None = None) -> bool:
    """Atualiza a transformação canônica em unidades relativas à guia Segurança.

    O zoom do editor é expresso sobre a guia ativa (Corte ou Arte), mas a
    composição deve permanecer proporcional à guia Segurança ao trocar tamanhos.
    Guardar a transformação normalizada evita conversões acumulativas e elimina
    a dependência da ordem dos reruns do Streamlit.
    """
    profile = profile if isinstance(profile, dict) else saved.get("scale_reference_spec")
    if not isinstance(profile, dict):
        return False
    mode = str(saved.get("fit_basis_mode", "cut")).lower()
    if mode not in {"cut", "art"}:
        mode = "cut"
    try:
        safe = float(profile["safe"])
        basis = float(profile["art" if mode == "art" else "cut"])
        zoom = float(saved.get("zoom", 1.0))
        off_x = float(saved.get("offset_x", 0.0))
        off_y = float(saved.get("offset_y", 0.0))
        if not all(math.isfinite(v) and v > 0 for v in (safe, basis)):
            return False
        if not all(math.isfinite(v) for v in (zoom, off_x, off_y)):
            return False
    except (KeyError, TypeError, ValueError):
        return False

    saved["safety_relative_zoom"] = zoom * basis / safe
    saved["safety_relative_offset_x"] = off_x / safe
    saved["safety_relative_offset_y"] = off_y / safe
    saved["safety_transform_schema"] = 1
    return True


def reset_artwork_image_adjustments(item_id: int) -> None:
    """Restaura os controles visuais e reativa o arquivo original da arte."""
    item = _find_artwork_item(int(item_id))
    if item is None:
        return
    edit = item.setdefault("edit", {})
    prefix = f"artwork_{int(item_id)}"
    for field, default_value in IMAGE_ADJUSTMENT_DEFAULTS.items():
        edit[field] = default_value
        widget_key = f"{prefix}_{field}"
        if widget_key in st.session_state:
            st.session_state[widget_key] = default_value
    item.pop("image_processing_error", None)


def set_artwork_background_removal(item_id: int, enabled: bool) -> None:
    """Ativa/desativa remoção de fundo sem substituir o arquivo original."""
    item = _find_artwork_item(int(item_id))
    if item is None:
        return
    item.setdefault("edit", {})["remove_background"] = bool(enabled)
    item.pop("image_processing_error", None)
    item.pop("_processed_image_signature", None)


def _custom_background_payload(edit: dict) -> tuple[bytes, str]:
    """Retorna somente uma camada de fundo explicitamente ativa e completa.

    O sinalizador explícito evita que bytes residuais de sessões antigas ou de
    um uploader versionado mantenham o fundo ativo em outras áreas do app.
    Sessões anteriores à v3.25 são migradas pela presença simultânea de bytes
    e digest, mas depois disso o sinalizador passa a ser a fonte de verdade.
    """
    if not isinstance(edit, dict):
        return b"", ""
    data = edit.get("background_image_bytes") or b""
    digest = str(edit.get("background_image_digest", "") or "")
    active = edit.get("custom_background_active")
    if active is None:
        active = bool(data and digest)
    if not active or not isinstance(data, bytes) or not data or len(digest) != 64:
        return b"", ""
    return data, digest


def _prepare_batch_custom_background_upload(uploaded) -> tuple[bytes, str, str] | None:
    """Valida imagem escolhida no painel de lote e retorna bytes, hash e nome."""
    if uploaded is None:
        return None
    data = uploaded.getvalue() if hasattr(uploaded, "getvalue") else bytes(uploaded)
    if not data:
        return None
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("O arquivo ultrapassa o limite de 20 MB.")
    with Image.open(io.BytesIO(data)) as probe:
        width, height = probe.size
        probe.verify()
    if width < 32 or height < 32:
        raise ValueError("A imagem precisa ter pelo menos 32 × 32 pixels.")
    digest = hashlib.sha256(data).hexdigest()
    name = str(getattr(uploaded, "name", "Fundo personalizado"))
    return data, digest, name


def set_artwork_custom_background(item_id: int, upload_key: str) -> None:
    """Salva uma imagem de fundo independente para compor a borda Arte–Corte."""
    item = _find_artwork_item(int(item_id))
    uploaded = st.session_state.get(upload_key)
    if item is None or uploaded is None:
        return
    try:
        data = uploaded.getvalue() if hasattr(uploaded, "getvalue") else bytes(uploaded)
        if not data:
            return
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("O arquivo ultrapassa o limite de 20 MB.")
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            if width < 32 or height < 32:
                raise ValueError("A imagem precisa ter pelo menos 32 × 32 pixels.")
            digest = hashlib.sha256(data).hexdigest()
            edit = item.setdefault("edit", {})
            if edit.get("background_image_digest") == digest and edit.get("custom_background_active", True):
                edit["custom_background_active"] = True
                edit["custom_background_pending"] = False
                edit["background_mode"] = "Fundo personalizado"
                edit.pop("background_image_error", None)
                return
            if not bool(edit.get("custom_background_active", False)):
                previous_mode = edit.get("background_image_previous_fill_mode")
                if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
                    current_mode = str(edit.get("background_mode", "Cor sólida"))
                    previous_mode = current_mode if current_mode in {"Cor sólida", "Imagem desfocada"} else "Cor sólida"
                edit["background_image_previous_fill_mode"] = previous_mode
            edit["background_image_bytes"] = data
            edit["background_image_digest"] = digest
            edit["background_image_name"] = str(getattr(uploaded, "name", "Fundo personalizado"))
            edit["custom_background_active"] = True
            edit["custom_background_pending"] = False
            edit["background_mode"] = "Fundo personalizado"
            edit["custom_background_revision"] = int(edit.get("custom_background_revision", 0) or 0) + 1
            edit.pop("background_image_error", None)
            prefix = f"artwork_{int(item_id)}"
            st.session_state[f"{prefix}_background_mode"] = "Fundo personalizado"
            cut_key = f"{prefix}_fit_cut_checked"
            art_key = f"{prefix}_fit_art_checked"
            already_uses_art_basis = str(edit.get("fit_basis_mode", st.session_state.get(f"{prefix}_fit_basis_mode", "cut"))).lower() == "art"
            # A seleção de Fundo personalizado já prepara a guia Arte antes do
            # upload. Mesmo em sessões antigas, esta migração preserva qualquer
            # zoom/posição manual em vez de recentrar a imagem ao carregar.
            if not already_uses_art_basis:
                _activate_art_area_preserving_manual_transform(int(item_id), prefix)
            else:
                st.session_state[cut_key] = False
                # Não marcar o preset Arte quando a composição já é manual.
                if bool(edit.get("manual_transform_active", False)):
                    st.session_state[art_key] = False
                else:
                    st.session_state[art_key] = True
    except Exception as exc:
        item.setdefault("edit", {})["background_image_error"] = str(exc)


def remove_artwork_custom_background(
    item_id: int, restore_previous_fill_mode: bool = True, commit_transform: bool = True
) -> None:
    """Desativa a camada de fundo em todas as saídas e invalida artefatos antigos."""
    item = _find_artwork_item(int(item_id))
    if item is None:
        return

    # O clique manual confirma o canvas antes de remover. Os callbacks de
    # prioridade já estão no meio de uma troca explícita de preset e não devem
    # sobrescrever o novo estado com o último transform enviado pelo navegador.
    if commit_transform:
        try:
            _commit_artwork_editor_state(int(item_id))
        except (NameError, TypeError, ValueError):
            pass

    edit = item.setdefault("edit", {})
    # A bandeira é desligada antes dos dados serem limpos. Todas as rotinas de
    # prévia/exportação consultam a mesma fonte de verdade (_custom_background_payload).
    edit["custom_background_active"] = False
    edit["custom_background_pending"] = False
    edit["background_image_bytes"] = None
    edit["background_image_digest"] = ""
    edit["background_image_name"] = ""
    edit.pop("background_image_error", None)
    edit["custom_background_revision"] = int(edit.get("custom_background_revision", 0) or 0) + 1
    edit["background_upload_revision"] = int(edit.get("background_upload_revision", 0) or 0) + 1
    previous_fill_mode = edit.pop("background_image_previous_fill_mode", None)
    if restore_previous_fill_mode:
        restored_mode = previous_fill_mode if previous_fill_mode in {"Cor sólida", "Imagem desfocada"} else "Cor sólida"
        edit["background_mode"] = restored_mode
        st.session_state[f"artwork_{int(item_id)}_background_mode"] = restored_mode

    # Compatibilidade com registros muito antigos que guardavam transformação
    # temporária ao ativar o fundo. A remoção volta ao enquadramento anterior.
    previous = edit.pop("background_image_previous_transform", None)
    if isinstance(previous, dict):
        for field, value in previous.items():
            if value is not None:
                edit[field] = value
        prefix = f"artwork_{int(item_id)}"
        for field in (
            "fit_basis_mode", "fit_cut_checked", "fit_art_checked", "zoom", "offset_x", "offset_y",
        ):
            if field in edit:
                st.session_state[f"{prefix}_{field}"] = edit[field]
        edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
        st.session_state[f"{prefix}_transform_revision"] = edit["transform_revision"]
        _safety_relative_transform(edit, edit.get("scale_reference_spec"))

    # Descarta também qualquer exportação já preparada com o fundo anterior.
    st.session_state.pop("focused_art_png_export", None)
    st.session_state.pop("pdf", None)
    st.session_state.pop("_sheet_real_size_preview_cache", None)
    st.session_state["_custom_background_removed_notice"] = int(item_id)


def ensure_artwork_edit_state(
    item: dict,
    spec: ButtonSpec | None = None,
    *,
    sync_session: bool = True,
) -> str:
    """Restaurar a transformação da arte a partir de uma referência estável.

    Quando um preset está marcado, ele é uma regra, não apenas um valor inicial:
    Preencher até o corte e Encaixar na área da arte permanecem em zoom 100% e
    centralizados em qualquer tamanho. Depois que o usuário move/redimensiona,
    os presets são desmarcados e a transformação manual passa a ser normalizada
    pela guia Segurança, permitindo trocar de diâmetro sem conversões acumulativas.
    """
    item_id = int(item["id"])
    prefix = f"artwork_{item_id}"
    saved = item.setdefault("edit", {})
    # Migração de sessões v3.24 e anteriores: reconhece camadas já carregadas.
    # Depois da migração, custom_background_active controla todas as saídas.
    if "custom_background_active" not in saved:
        saved["custom_background_active"] = bool(
            saved.get("background_image_bytes") and saved.get("background_image_digest")
        )
    defaults = {
        "zoom": 1.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
        "transform_revision": 0,
        "event_seq": 0,
        "fit_basis_mode": "cut",
        "fit_cut_checked": True,
        "fit_art_checked": False,
        "manual_transform_active": False,
        "background_mode": "Cor sólida",
        "blur_strength": 0.5,
        "background": "#FFFFFF",
        "background_image_bytes": None,
        "background_image_digest": "",
        "background_image_name": "",
        "custom_background_active": False,
        "custom_background_pending": False,
        "background_upload_revision": 0,
        "custom_background_revision": 0,
        **IMAGE_ADJUSTMENT_DEFAULTS,
    }
    for suffix, default_value in defaults.items():
        if suffix not in saved:
            legacy_key = f"{prefix}_{suffix}"
            saved[suffix] = st.session_state.get(legacy_key, default_value)
    # Nas versões anteriores, o desfoque e a cor sólida compartilhavam um
    # único campo. Migra a cor antiga para a base do desfoque apenas uma vez.
    if "blur_base_color" not in saved:
        saved["blur_base_color"] = str(saved.get("background", "#FFFFFF"))
    has_custom_layer = bool(
        saved.get("custom_background_active")
        and saved.get("background_image_bytes")
        and saved.get("background_image_digest")
    )
    if has_custom_layer:
        saved["background_mode"] = "Fundo personalizado"
        saved["custom_background_pending"] = False
    elif str(saved.get("background_mode")) == "Fundo personalizado":
        if bool(saved.get("custom_background_pending", False)):
            # Modo escolhido; upload ainda não concluído. Não restaurar o
            # preenchimento anterior em cada rerun, pois o uploader depende
            # dessa seleção para aparecer.
            previous_mode = saved.get("background_image_previous_fill_mode")
            if previous_mode not in {"Cor sólida", "Imagem desfocada"}:
                saved["background_image_previous_fill_mode"] = "Cor sólida"
        else:
            saved["background_mode"] = str(
                saved.pop("background_image_previous_fill_mode", None) or "Cor sólida"
            )

    # O estado dos dois checkboxes define a base ativa quando exatamente um
    # está marcado. Quando ambos estão desmarcados, preservamos a última base
    # escolhida, usada para interpretar a transformação manual.
    cut_checked = bool(saved.get("fit_cut_checked", False))
    art_checked = bool(saved.get("fit_art_checked", False))
    if bool(saved.get("manual_transform_active", False)):
        cut_checked = False
        art_checked = False
    mode = str(saved.get("fit_basis_mode", "cut")).lower()
    if cut_checked and not art_checked:
        mode = "cut"
    elif art_checked and not cut_checked:
        mode = "art"
    if mode not in {"cut", "art"}:
        mode = "cut"
    saved["fit_basis_mode"] = mode
    saved["fit_cut_checked"] = bool(cut_checked and mode == "cut")
    saved["fit_art_checked"] = bool(art_checked and mode == "art")

    if spec is not None:
        schema = int(saved.get("initial_cut_fit_schema", 0) or 0)
        if schema < 4:
            # Migração única dos itens criados com o antigo enquadramento
            # contain. Só altera artes sem histórico de interação.
            try:
                has_editor_history = (
                    int(saved.get("event_seq", 0) or 0) > 0
                    or int(saved.get("transform_revision", 0) or 0) > 0
                )
                current_zoom = float(saved.get("zoom", 1.0))
                old_contain_zoom = _legacy_initial_contain_zoom(item.get("image", b""))
                untouched_legacy_default = (
                    not has_editor_history
                    and mode == "cut"
                    and abs(float(saved.get("offset_x", 0.0) or 0.0)) < 1e-8
                    and abs(float(saved.get("offset_y", 0.0) or 0.0)) < 1e-8
                    and (
                        abs(current_zoom - old_contain_zoom) < 1e-7
                        or abs(current_zoom - 1.0) < 1e-7
                    )
                )
            except (TypeError, ValueError):
                untouched_legacy_default = False

            # Também corrige registros recentes em que o preset inicial foi
            # perdido sem existir qualquer gesto manual ou preset Arte escolhido.
            needs_fill_preset = (
                not has_editor_history
                and not bool(saved.get("fit_art_checked", False))
                and not bool(saved.get("manual_transform_active", False))
                and mode == "cut"
            )
            if untouched_legacy_default or needs_fill_preset:
                saved["zoom"] = 1.0
                saved["offset_x"] = 0.0
                saved["offset_y"] = 0.0
                saved["fit_basis_mode"] = "cut"
                saved["fit_cut_checked"] = True
                saved["fit_art_checked"] = False
                saved["manual_transform_active"] = False
                mode = "cut"
                # Recria o estado canônico usando o perfil geométrico atual.
                for key in (
                    "safety_relative_zoom", "safety_relative_offset_x",
                    "safety_relative_offset_y", "safety_transform_schema",
                    "scale_reference_spec", "scale_reference_diameter",
                ):
                    saved.pop(key, None)
            saved["initial_cut_fit_schema"] = 4

        target_profile = _button_scale_profile(spec)
        mode = str(saved.get("fit_basis_mode", "cut")).lower()
        if mode not in {"cut", "art"}:
            mode = "cut"
        saved["fit_basis_mode"] = mode
        preset_active = (
            (mode == "cut" and bool(saved.get("fit_cut_checked")))
            or (mode == "art" and bool(saved.get("fit_art_checked")))
        )

        if preset_active:
            saved["manual_transform_active"] = False
            # Preset ativo sempre cobre a guia correspondente, em qualquer
            # tamanho. Isso impede que Fill-to-Cut inicial fique com zoom < 100%
            # ao converter entre bottons com proporções Corte/Segurança distintas.
            saved["zoom"] = 1.0
            saved["offset_x"] = 0.0
            saved["offset_y"] = 0.0
            saved["scale_reference_spec"] = target_profile
            saved["scale_reference_diameter"] = float(spec.safe)
            _safety_relative_transform(saved, target_profile)
        else:
            try:
                canonical_values = (
                    float(saved["safety_relative_zoom"]),
                    float(saved["safety_relative_offset_x"]),
                    float(saved["safety_relative_offset_y"]),
                )
                canonical_ok = (
                    int(saved.get("safety_transform_schema", 0) or 0) == 1
                    and all(math.isfinite(value) for value in canonical_values)
                )
            except (KeyError, TypeError, ValueError):
                canonical_ok = False

            if not canonical_ok:
                previous_profile = saved.get("scale_reference_spec")
                if not isinstance(previous_profile, dict):
                    previous_profile = _legacy_scale_profile(saved, spec, mode)
                if not isinstance(previous_profile, dict):
                    previous_profile = target_profile
                try:
                    old_safe = float(previous_profile["safe"])
                    old_basis = float(previous_profile["art" if mode == "art" else "cut"])
                    old_zoom = float(saved.get("zoom", 1.0))
                    old_x = float(saved.get("offset_x", 0.0))
                    old_y = float(saved.get("offset_y", 0.0))
                    valid = (
                        math.isfinite(old_safe) and old_safe > 0
                        and math.isfinite(old_basis) and old_basis > 0
                        and all(math.isfinite(value) for value in (old_zoom, old_x, old_y))
                    )
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    old_safe = float(spec.safe)
                    old_basis = float(spec.art if mode == "art" else spec.cut)
                    old_zoom = 1.0
                    old_x = old_y = 0.0
                saved["safety_relative_zoom"] = old_zoom * old_basis / old_safe
                saved["safety_relative_offset_x"] = old_x / old_safe
                saved["safety_relative_offset_y"] = old_y / old_safe
                saved["safety_transform_schema"] = 1

            safe_rel_zoom = float(saved["safety_relative_zoom"])
            safe_rel_x = float(saved["safety_relative_offset_x"])
            safe_rel_y = float(saved["safety_relative_offset_y"])
            new_safe = float(spec.safe)
            new_basis = float(spec.art if mode == "art" else spec.cut)
            if (
                not all(math.isfinite(value) for value in (safe_rel_zoom, safe_rel_x, safe_rel_y, new_safe, new_basis))
                or new_safe <= 0 or new_basis <= 0
            ):
                safe_rel_zoom = new_basis / max(new_safe, 1e-9)
                safe_rel_x = safe_rel_y = 0.0
                saved["safety_relative_zoom"] = safe_rel_zoom
                saved["safety_relative_offset_x"] = 0.0
                saved["safety_relative_offset_y"] = 0.0
                saved["safety_transform_schema"] = 1

            # zoom representa a escala relativa à base ativa (Corte ou Arte).
            # X/Y são deslocamentos físicos em mm. Ambos são convertidos pela
            # mesma referência canônica de Segurança, não pelo tamanho anterior.
            derived_zoom = safe_rel_zoom * new_safe / new_basis
            derived_x = safe_rel_x * new_safe
            derived_y = safe_rel_y * new_safe
            saved["zoom"] = max(0.01, min(20.0, derived_zoom))
            saved["offset_x"] = max(-5000.0, min(5000.0, derived_x))
            saved["offset_y"] = max(-5000.0, min(5000.0, derived_y))
            saved["scale_reference_spec"] = target_profile
            saved["scale_reference_diameter"] = new_safe

        initial_basis = spec.art if mode == "art" else spec.cut
        saved["initial_safety_relative_zoom"] = float(initial_basis) / max(float(spec.safe), 1e-9)

    # Migração/normalização individual: qualquer arte com o preset Corte ativo
    # deve manter os preenchimentos e a borda desativados, inclusive em sessões
    # que foram salvas por versões anteriores onde esses estados coexistiam.
    cut_preset_active = bool(
        saved.get("fit_cut_checked", False)
        and not saved.get("manual_transform_active", False)
        and str(saved.get("fit_basis_mode", "cut")) == "cut"
    )
    if cut_preset_active and (
        str(saved.get("background_mode", "Cor sólida")) != "Sem preenchimento"
        or saved.get("custom_background_active")
        or saved.get("custom_background_pending")
        or saved.get("background_image_bytes")
        or saved.get("background_image_digest")
    ):
        saved = _deactivate_individual_fill_for_cut(item_id, prefix, saved)
    elif (
        bool(saved.get("manual_transform_active", False))
        and str(saved.get("background_mode", "")) == "Sem preenchimento"
    ):
        # Um gesto manual no canvas desmarca o preset Corte. Nesse momento,
        # libera novamente o último preenchimento para o usuário poder escolher
        # se deseja manter a borda enquanto continua ajustando a imagem.
        _restore_individual_fill_after_cut(saved, prefix)

    if sync_session:
        # Preparar estado por ID antes de instanciar os respectivos widgets.
        for suffix, default_value in defaults.items():
            st.session_state[f"{prefix}_{suffix}"] = saved.get(suffix, default_value)
        st.session_state[f"{prefix}_blur_base_color"] = saved.get("blur_base_color", "#FFFFFF")
        if spec is not None:
            st.session_state[f"{prefix}_scale_reference_diameter"] = saved.get(
                "scale_reference_diameter", float(spec.safe)
            )
    return prefix


def persist_artwork_edit_state(item: dict, prefix: str) -> None:
    """Persiste o estado sem reativar presets por meio de widgets desatualizados."""
    saved = item.setdefault("edit", {})
    manual_active = bool(saved.get("manual_transform_active", False))
    for suffix in (
        "zoom", "offset_x", "offset_y", "transform_revision", "event_seq",
        "fit_basis_mode", "fit_cut_checked", "fit_art_checked",
    ):
        key = f"{prefix}_{suffix}"
        if key not in st.session_state:
            continue
        if manual_active and suffix in {"fit_cut_checked", "fit_art_checked"}:
            saved[suffix] = False
        else:
            saved[suffix] = st.session_state[key]
    reference_profile = saved.get("scale_reference_spec")
    if isinstance(reference_profile, dict):
        try:
            saved["scale_reference_diameter"] = float(reference_profile["safe"])
        except (KeyError, TypeError, ValueError):
            pass
    # Não recalcular a forma canônica a cada rerun. Ela é atualizada somente
    # quando há um gesto no canvas ou mudança explícita de preset; assim a
    # conversão entre tamanhos nunca acumula arredondamento/clamp intermediário.
    if int(saved.get("safety_transform_schema", 0) or 0) != 1:
        _safety_relative_transform(saved, reference_profile)


def _new_artwork_pool_entry(item_id: int, file_name: str, image_bytes: bytes, digest: str, thumb_url: str, spec: ButtonSpec) -> dict:
    """Cria o mesmo registro de Pool usado pelo upload tradicional."""
    return {
        "id": int(item_id),
        "name": str(file_name),
        "image": image_bytes,
        "image_digest": digest,
        "original_image": image_bytes,
        "original_image_digest": digest,
        "thumbnail_data_url": thumb_url,
        "qty": 1,
        "edit": {
            "zoom": 1.0,
            "initial_safety_relative_zoom": float(spec.cut) / max(float(spec.safe), 1e-9),
            "offset_x": 0.0,
            "offset_y": 0.0,
            "transform_revision": 0,
            "event_seq": 0,
            "fit_basis_mode": "cut",
            "fit_cut_checked": True,
            "fit_art_checked": False,
            "manual_transform_active": False,
            "scale_reference_spec": _button_scale_profile(spec),
            "scale_reference_diameter": float(spec.safe),
            "safety_relative_zoom": float(spec.cut) / max(float(spec.safe), 1e-9),
            "safety_relative_offset_x": 0.0,
            "safety_relative_offset_y": 0.0,
            "safety_transform_schema": 1,
            "initial_cut_fit_schema": 4,
            "background_mode": "Cor sólida",
            "blur_strength": 0.5,
            "background": "#FFFFFF",
            "blur_base_color": "#FFFFFF",
            "background_image_bytes": None,
            "background_image_digest": "",
            "background_image_name": "",
            "custom_background_active": False,
            "custom_background_pending": False,
            "custom_background_revision": 0,
            **IMAGE_ADJUSTMENT_DEFAULTS,
        },
    }


@st.fragment(run_every="2s")
def _mobile_upload_inbox_monitor(spec: ButtonSpec):
    """Recebe arquivos do pequeno servidor local e acrescenta-os ao Pool existente."""
    state = st.session_state.get("_mobile_upload_session")
    if not isinstance(state, dict) or not state.get("token"):
        return
    try:
        from mobile_upload import take_uploaded_files, upload_session_is_active
        if not upload_session_is_active(str(state["token"])):
            if not state.get("_expired_notice_shown"):
                st.warning("Este QR Code expirou por inatividade. Gere uma nova sessão para continuar enviando imagens.")
                state["_expired_notice_shown"] = True
            return
        received = take_uploaded_files(str(state["token"]))
    except Exception as exc:
        st.error(f"Não foi possível consultar os envios do smartphone: {exc}")
        return
    if not received:
        return

    items = st.session_state.setdefault("artwork_items", [])
    existing_ids = {int(item.get("id", 0)) for item in items}
    next_id = max(int(st.session_state.get("artwork_next_id", 1)), max(existing_ids, default=0) + 1)
    added = []
    failures = []
    for file_name, image_bytes in received:
        try:
            digest, thumb_url = prepare_uploaded_artwork(file_name, image_bytes)
            record = _new_artwork_pool_entry(next_id, file_name, image_bytes, digest, thumb_url, spec)
            items.append(record)
            added.append(next_id)
            next_id += 1
        except Exception as exc:
            failures.append((file_name, str(exc)))

    st.session_state["artwork_next_id"] = next_id
    if added:
        if st.session_state.get("artwork_selected_id") is None:
            st.session_state["artwork_selected_id"] = added[0]
        st.session_state["artwork_upload_revision"] = int(st.session_state.get("artwork_upload_revision", 0)) + 1
        st.success(f"{len(added)} imagem(ns) recebida(s) e adicionada(s) ao Pool de Artes.")
    if failures:
        st.warning("Alguns arquivos foram ignorados por não serem imagens válidas: " + ", ".join(name for name, _ in failures))
    if added:
        st.rerun(scope="app")


def _ensure_mobile_upload_session(force_new: bool = False) -> dict:
    """Cria ou reutiliza uma sessao HTTP local para upload resiliente em dispositivos moveis."""
    from mobile_upload import (
        close_upload_session, create_upload_session, get_lan_ip,
        get_mobile_upload_port, make_qr_code_png, renew_upload_session,
        upload_session_is_active,
    )

    current = st.session_state.get("_mobile_upload_session")
    if isinstance(current, dict) and current.get("token") and not force_new:
        token = str(current["token"])
        if upload_session_is_active(token) and renew_upload_session(token):
            return current

    if isinstance(current, dict) and current.get("token"):
        close_upload_session(str(current["token"]))

    host = get_lan_ip()
    if not host:
        raise RuntimeError("Nao foi possivel identificar o endereco IPv4 da rede local deste computador.")
    port = get_mobile_upload_port()
    token = create_upload_session()
    url = f"http://{host}:{port}/upload/{token}"
    session = {
        "token": token, "url": url, "qr_png": make_qr_code_png(url),
        "host": host, "port": port,
    }
    st.session_state["_mobile_upload_session"] = session
    return session


def _render_mobile_upload_panel(spec: ButtonSpec):
    """Painel visual de transferência local de imagens por QR Code."""
    with st.expander("📲 Enviar imagens pelo smartphone", expanded=False):
        st.markdown(
            """
            <style>
              .mobile-transfer-hero{position:relative;overflow:hidden;display:grid;grid-template-columns:minmax(0,1fr) auto;
                gap:18px;align-items:center;margin:2px 0 14px;padding:19px 20px;border:1px solid rgba(102,145,180,.34);
                border-radius:17px;background:radial-gradient(circle at 92% 15%,rgba(238,29,126,.17),transparent 30%),
                radial-gradient(circle at 5% 100%,rgba(27,169,216,.18),transparent 35%),linear-gradient(125deg,#142941,#101C2D 70%);
                box-shadow:0 12px 30px rgba(0,0,0,.18)}
              .mobile-transfer-hero:before{content:"";position:absolute;top:0;left:0;right:0;height:3px;
                background:linear-gradient(90deg,#1BA9D8 0 33.3%,#EE1D7E 33.3% 66.6%,#F5DD1E 66.6%)}
              .mobile-transfer-kicker{font-size:9px;font-weight:900;letter-spacing:.16em;color:#8DD8E9;margin-bottom:7px}
              .mobile-transfer-title{font-size:clamp(17px,2vw,22px);font-weight:850;line-height:1.18;color:#F7FAFD;max-width:620px}
              .mobile-transfer-copy{font-size:11px;line-height:1.55;color:#B8C8D8;max-width:650px;margin-top:7px}
              .mobile-transfer-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
              .mobile-transfer-tag{padding:5px 8px;border:1px solid rgba(129,167,197,.28);border-radius:999px;
                background:rgba(5,18,32,.42);font-size:9px;font-weight:750;color:#D7E5EF}
              .mobile-transfer-art{width:92px;height:92px;display:grid;place-items:center;border-radius:25px;
                border:1px solid rgba(116,181,211,.34);background:linear-gradient(145deg,rgba(27,169,216,.18),rgba(238,29,126,.12));
                color:#BDECF6;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
              .mobile-transfer-art svg{display:block;width:68px;height:68px}
              .mobile-transfer-steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0 0 13px}
              .mobile-transfer-step{display:flex;gap:8px;align-items:flex-start;padding:9px 10px;border:1px solid rgba(105,140,171,.2);
                border-radius:11px;background:rgba(20,39,60,.42);min-width:0}
              .mobile-transfer-step-no{width:21px;height:21px;display:grid;place-items:center;flex:none;border-radius:7px;
                background:#1BA9D8;color:#061A29;font-size:9px;font-weight:950}
              .mobile-transfer-step b{display:block;font-size:10px;line-height:1.25;color:#F0F5F9}
              .mobile-transfer-step small{display:block;margin-top:3px;font-size:9px;line-height:1.35;color:#AFC0D0}
              .mobile-qr-panel{height:100%;padding:14px;border:1px solid rgba(93,139,171,.28);border-radius:15px;
                background:linear-gradient(160deg,rgba(22,45,67,.72),rgba(12,25,41,.86));text-align:center}
              .mobile-qr-eyebrow{font-size:9px;font-weight:900;letter-spacing:.12em;color:#88D6E6;margin-bottom:8px}
              .mobile-qr-note{font-size:10px;line-height:1.45;color:#B6C8D8;margin-top:7px}
              .mobile-session-card{padding:14px 15px;border:1px solid rgba(93,139,171,.25);border-radius:15px;
                background:rgba(17,35,54,.48);margin-bottom:10px}
              .mobile-session-status{display:inline-flex;align-items:center;gap:7px;padding:5px 9px;border-radius:999px;
                border:1px solid rgba(45,187,114,.36);background:rgba(45,187,114,.1);color:#A9E8C4;
                font-size:9px;font-weight:850;letter-spacing:.04em}
              .mobile-session-dot{width:7px;height:7px;border-radius:50%;background:#2DBB72;box-shadow:0 0 0 3px rgba(45,187,114,.13)}
              .mobile-session-title{margin:9px 0 5px;font-size:15px;font-weight:850;color:#F4F8FB}
              .mobile-session-copy{font-size:10px;line-height:1.48;color:#B4C4D3}
              .mobile-session-limits{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
              .mobile-session-limit{padding:5px 8px;border-radius:8px;background:rgba(3,15,28,.48);border:1px solid rgba(111,145,174,.18);
                color:#C5D2DE;font-size:9px;font-weight:700}
              @media(max-width:640px){.mobile-transfer-hero{grid-template-columns:1fr;padding:15px}.mobile-transfer-art{display:none}
                .mobile-transfer-steps{grid-template-columns:1fr}.mobile-transfer-step{align-items:center}}
            </style>
            <div class="mobile-transfer-hero">
              <div>
                <div class="mobile-transfer-kicker">TRANSFERÊNCIA LOCAL · DIRETO PARA O POOL</div>
                <div class="mobile-transfer-title">Envie imagens do celular sem precisar transferi-las manualmente para o computador</div>
                <div class="mobile-transfer-copy">Crie uma sessão temporária, leia o QR Code com a câmera do smartphone e envie várias imagens. Os arquivos chegam diretamente ao Pool de Artes deste computador.</div>
                <div class="mobile-transfer-tags"><span class="mobile-transfer-tag">Mesma rede Wi‑Fi</span><span class="mobile-transfer-tag">Sem serviço em nuvem</span><span class="mobile-transfer-tag">Sessão temporária</span></div>
              </div>
              <div class="mobile-transfer-art" aria-hidden="true">
                <svg viewBox="0 0 96 96" role="presentation" xmlns="http://www.w3.org/2000/svg">
                  <rect x="27" y="7" width="42" height="82" rx="9" fill="#0A1B2C" stroke="#A7E5F1" stroke-width="2.8"/>
                  <rect x="32" y="16" width="32" height="59" rx="3" fill="#DDF8FC"/>
                  <circle cx="48" cy="82" r="2.5" fill="#A7E5F1"/>
                  <g fill="#12354A">
                    <rect x="36" y="22" width="10" height="10"/><rect x="38.5" y="24.5" width="5" height="5" fill="#DDF8FC"/>
                    <rect x="52" y="22" width="10" height="10"/><rect x="54.5" y="24.5" width="5" height="5" fill="#DDF8FC"/>
                    <rect x="36" y="36" width="10" height="10"/><rect x="38.5" y="38.5" width="5" height="5" fill="#DDF8FC"/>
                    <rect x="50" y="36" width="4" height="4"/><rect x="56" y="36" width="4" height="4"/>
                    <rect x="50" y="42" width="4" height="4"/><rect x="58" y="44" width="4" height="4"/>
                    <rect x="36" y="52" width="4" height="4"/><rect x="42" y="52" width="4" height="4"/>
                    <rect x="50" y="52" width="4" height="4"/><rect x="56" y="52" width="6" height="6"/>
                    <rect x="36" y="60" width="6" height="6"/><rect x="46" y="60" width="4" height="4"/>
                    <rect x="54" y="62" width="4" height="4"/>
                  </g>
                  <path d="M18 30h-7v36h7M78 30h7v36h-7" fill="none" stroke="#EE1D7E" stroke-width="2.6" stroke-linecap="round"/>
                </svg>
              </div>
            </div>
            <div class="mobile-transfer-steps">
              <div class="mobile-transfer-step"><span class="mobile-transfer-step-no">1</span><span><b>Gere o QR Code</b><small>O computador cria um endereço temporário na rede local.</small></span></div>
              <div class="mobile-transfer-step"><span class="mobile-transfer-step-no">2</span><span><b>Leia com o celular</b><small>Use a câmera ou o leitor de QR Code do smartphone.</small></span></div>
              <div class="mobile-transfer-step"><span class="mobile-transfer-step-no">3</span><span><b>Envie para o Pool</b><small>Selecione imagens ou tire fotos e envie em conjunto.</small></span></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        session = st.session_state.get("_mobile_upload_session")
        create_label = "Gerar novo QR Code" if isinstance(session, dict) and session.get("token") else "Gerar QR Code para enviar imagens"
        create_col, hint_col = st.columns([1, 2.1], gap="medium")
        with create_col:
            create_clicked = st.button(
                create_label,
                key="mobile_upload_create_session",
                type="primary",
                use_container_width=True,
            )
        with hint_col:
            st.markdown(
                '<div style="padding:8px 2px;color:#AFC0D0;font-size:10px;line-height:1.5;">'
                '<b style="color:#E4EDF4;">Importante:</b> computador e smartphone precisam estar conectados à mesma rede Wi‑Fi. '
                'A transferência ocorre somente na rede local.</div>',
                unsafe_allow_html=True,
            )
        if create_clicked:
            try:
                _ensure_mobile_upload_session(force_new=True)
                st.rerun()
            except Exception as exc:
                st.error(f"Não foi possível gerar o QR Code: {exc}")

        session = st.session_state.get("_mobile_upload_session")
        if isinstance(session, dict) and session.get("token"):
            st.divider()
            qr_col, info_col = st.columns([1, 1.75], gap="large")
            with qr_col:
                with st.container(border=True, key="mobile_qr_card"):
                    st.markdown('<div class="mobile-qr-eyebrow">APONTE A CÂMERA PARA O CÓDIGO</div>', unsafe_allow_html=True)
                    st.image(session["qr_png"], width=230)
                    st.markdown('<div class="mobile-qr-note">Mantenha o QR Code visível enquanto envia as imagens.</div>', unsafe_allow_html=True)
            with info_col:
                st.markdown(
                    '<div class="mobile-session-card">'
                    '<span class="mobile-session-status"><span class="mobile-session-dot"></span> SESSÃO ATIVA</span>'
                    '<div class="mobile-session-title">Seu smartphone já pode se conectar</div>'
                    '<div class="mobile-session-copy">Abra a câmera, leia o código e siga as instruções exibidas no celular. Depois do envio, as imagens serão adicionadas automaticamente ao Pool de Artes.</div>'
                    '<div class="mobile-session-limits"><span class="mobile-session-limit">30 min sem atividade</span>'
                    '<span class="mobile-session-limit">Até 50 imagens</span><span class="mobile-session-limit">250 MB por sessão</span>'
                    '<span class="mobile-session-limit">100 MB por arquivo</span></div></div>',
                    unsafe_allow_html=True,
                )
                st.markdown("**Endereço de envio**")
                st.code(session["url"], language=None)
                st.caption("Na primeira conexão, o Firewall do Windows pode solicitar autorização para o Python na rede privada.")
                close_col, _ = st.columns([1, 1.6])
                with close_col:
                    if st.button("Encerrar sessão e invalidar QR", key="mobile_upload_close_session", use_container_width=True):
                        try:
                            from mobile_upload import close_upload_session
                            close_upload_session(str(session["token"]))
                        except Exception:
                            pass
                        st.session_state.pop("_mobile_upload_session", None)
                        st.rerun()


def main():
    st.set_page_config(
        page_title=brand.APP_TITLE,
        page_icon=brand.icon_image(),
        layout="wide",
    )
    _apply_pending_atomica_project_restore()
    brand.apply_theme()
    st.markdown(
        """<style>
        /* Responsividade global: evita colunas estreitas e controles cortados em telas táteis. */
        @media (max-width: 1024px) {
          html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            max-width:100% !important; min-width:0 !important; overflow-x:hidden !important;
          }
          [data-testid="stMainBlockContainer"] *, [data-testid="stSidebar"] * { min-width:0; }
          [data-testid="stMainBlockContainer"] {
            max-width:100% !important;
            padding-left:clamp(.75rem,2.5vw,1.25rem) !important;
            padding-right:clamp(.75rem,2.5vw,1.25rem) !important;
          }
          .st-key-editor_workspace [data-testid="stHorizontalBlock"]:has(.st-key-card_left):has(.st-key-card_preview) {
            display:flex !important; flex-direction:column !important; align-items:stretch !important;
            gap:1rem !important; width:100% !important;
          }
          .st-key-editor_workspace [data-testid="stHorizontalBlock"]:has(.st-key-card_left):has(.st-key-card_preview) > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important; min-width:0 !important;
          }
          .st-key-card_left, .st-key-card_preview, .st-key-card_right { min-width:0 !important; width:100% !important; }
        }
        @media (max-width: 760px) {
          /* Quebra responsiva geral para grupos de colunas que não tenham
             uma regra específica: evita comprimir campos e esconder botões. */
          [data-testid="stHorizontalBlock"] {
            display:flex !important; flex-wrap:wrap !important; align-items:stretch !important;
            gap:.65rem !important; width:100% !important; min-width:0 !important;
          }
          [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 245px !important; width:auto !important; max-width:100% !important;
            min-width:0 !important; box-sizing:border-box !important;
          }
          /* Abas tornam-se navegáveis por gesto sem comprimir os seus rótulos. */
          [data-testid="stTabs"] [data-baseweb="tab-list"] {
            display:flex !important; flex-wrap:nowrap !important; overflow-x:auto !important;
            overflow-y:hidden !important; max-width:100% !important; gap:4px !important;
            overscroll-behavior-x:contain !important; scrollbar-width:thin !important;
            -webkit-overflow-scrolling:touch !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab"] {
            flex:0 0 auto !important; min-width:max-content !important; min-height:46px !important;
            padding:0 13px !important; white-space:nowrap !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab"] p {
            white-space:nowrap !important; font-size:13px !important;
          }
          [data-testid="stSidebar"] {
            width:min(88vw, 340px) !important; min-width:min(88vw, 340px) !important;
            max-width:88vw !important;
          }
          [data-testid="stSidebarContent"] { overflow-x:hidden !important; }
          [class*="st-key-artwork_batch_panel_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important; min-width:0 !important;
          }
          .st-key-sheet_settings_panel [data-testid="stHorizontalBlock"] {
            display:flex !important; flex-wrap:wrap !important; gap:.55rem !important;
          }
          .st-key-sheet_settings_panel [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 calc(50% - .55rem) !important; width:calc(50% - .55rem) !important;
            max-width:calc(50% - .55rem) !important; min-width:0 !important;
          }
          .st-key-pool_fallback_grid [data-testid="stHorizontalBlock"] {
            display:flex !important; flex-wrap:wrap !important; gap:.5rem !important;
          }
          .st-key-pool_fallback_grid [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 calc(33.333% - .5rem) !important; width:calc(33.333% - .5rem) !important;
            max-width:calc(33.333% - .5rem) !important; min-width:0 !important;
          }
          [data-testid="stRadio"] > div { flex-wrap:wrap !important; gap:.45rem .8rem !important; }
          [data-testid="stHorizontalBlock"]:has(.st-key-mobile_upload_create_session),
          [data-testid="stHorizontalBlock"]:has(.mobile-qr-eyebrow):has(.mobile-session-card) {
            display:flex !important; flex-direction:column !important; gap:.75rem !important;
          }
          [data-testid="stHorizontalBlock"]:has(.st-key-mobile_upload_create_session) > [data-testid="stColumn"],
          [data-testid="stHorizontalBlock"]:has(.mobile-qr-eyebrow):has(.mobile-session-card) > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important; min-width:0 !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has(.st-key-preview_nav_previous) {
            display:flex !important; flex-wrap:wrap !important; gap:.45rem !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has(.st-key-preview_nav_previous) > [data-testid="stColumn"] {
            flex:1 1 calc(50% - .45rem) !important; width:calc(50% - .45rem) !important;
            max-width:calc(50% - .45rem) !important; min-width:0 !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has(.st-key-preview_nav_previous) > [data-testid="stColumn"]:first-child {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has([class*="st-key-prepare_focused_art_png_"]) {
            display:flex !important; flex-direction:column !important; gap:.55rem !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has([class*="st-key-prepare_focused_art_png_"]) > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important; min-width:0 !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="stColumn"] {
            flex:1 1 calc(50% - .5rem) !important; min-width:0 !important;
          }
          /* Textos da folha de impressão não podem ser truncados em tablets.
             Isso inclui capacidade Grade/Hexagonal, métricas e dicas em azul. */
          .st-key-card_right, .st-key-sheet_settings_panel, .st-key-sheet_canvas_panel {
            min-width:0 !important; max-width:100% !important; overflow-wrap:anywhere !important;
          }
          .st-key-card_right [data-testid="stMarkdownContainer"],
          .st-key-card_right [data-testid="stCaptionContainer"],
          .st-key-sheet_settings_panel [data-testid="stMarkdownContainer"],
          .st-key-sheet_settings_panel [data-testid="stCaptionContainer"],
          .st-key-sheet_canvas_panel [data-testid="stMarkdownContainer"],
          .st-key-sheet_canvas_panel [data-testid="stCaptionContainer"] {
            min-width:0 !important; max-width:100% !important; white-space:normal !important;
            overflow-wrap:anywhere !important;
          }
          .st-key-card_right [data-testid="stMarkdownContainer"] p,
          .st-key-card_right [data-testid="stCaptionContainer"] p,
          .st-key-sheet_settings_panel [data-testid="stMarkdownContainer"] p,
          .st-key-sheet_settings_panel [data-testid="stCaptionContainer"] p,
          .st-key-sheet_canvas_panel [data-testid="stMarkdownContainer"] p,
          .st-key-sheet_canvas_panel [data-testid="stCaptionContainer"] p,
          .st-key-sheet_canvas_panel [data-testid="stMetricLabel"],
          .st-key-sheet_canvas_panel [data-testid="stMetricValue"] {
            max-width:100% !important; white-space:normal !important;
            overflow-wrap:anywhere !important; word-break:normal !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stRadio"] label {
            min-width:0 !important; max-width:100% !important; height:auto !important;
            white-space:normal !important; overflow-wrap:anywhere !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stRadio"] [data-testid="stMarkdownContainer"] p {
            white-space:normal !important; overflow-wrap:anywhere !important;
          }
          .st-key-card_right [data-testid="stAlert"],
          .st-key-sheet_settings_panel [data-testid="stAlert"],
          .st-key-sheet_canvas_panel [data-testid="stAlert"] {
            min-width:0 !important; max-width:100% !important; overflow-wrap:anywhere !important;
          }
          .st-key-card_right [data-testid="stAlert"] p,
          .st-key-sheet_settings_panel [data-testid="stAlert"] p,
          .st-key-sheet_canvas_panel [data-testid="stAlert"] p {
            white-space:normal !important; overflow-wrap:anywhere !important; word-break:normal !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stHorizontalBlock"]:has(.st-key-sheet_preview_page_previous) {
            display:flex !important; flex-direction:column !important; gap:.45rem !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stHorizontalBlock"]:has(.st-key-sheet_preview_page_previous) > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          .st-key-mobile_qr_card img { max-width:100% !important; height:auto !important; }
          .stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] button {
            min-height:48px !important; white-space:normal !important; line-height:1.25 !important;
            touch-action:manipulation !important; -webkit-tap-highlight-color:rgba(27,169,216,.18);
          }
          /* Checkboxes touch-safe: os estados sao botoes nativos com aparencia de caixa. */
          [class*="st-key-touch_checkbox_"] [data-testid="stButton"] { width:100% !important; }
          [class*="st-key-touch_checkbox_"] [data-testid="stButton"] > button {
            display:flex !important; align-items:center !important; justify-content:flex-start !important;
            width:100% !important; min-width:0 !important; min-height:50px !important;
            padding:10px 13px !important; text-align:left !important; white-space:normal !important;
            overflow-wrap:anywhere !important; line-height:1.3 !important; font-size:14px !important;
            border-radius:10px !important; touch-action:manipulation !important;
            user-select:none !important; -webkit-user-select:none !important;
            -webkit-tap-highlight-color:rgba(27,169,216,.22) !important;
          }
          [class*="st-key-touch_checkbox_"] [data-testid="stButton"] > button p {
            width:100% !important; margin:0 !important; text-align:left !important;
            white-space:normal !important; overflow-wrap:anywhere !important;
          }
          [data-testid="stRadio"] label {
            min-height:46px !important; display:flex !important; align-items:center !important;
            touch-action:manipulation !important; padding-block:5px !important;
          }
          [data-testid="stSegmentedControl"] button {
            min-height:48px !important; touch-action:manipulation !important;
          }
          [data-testid="stSlider"] [role="slider"] {
            width:26px !important; height:26px !important; touch-action:none !important;
          }
          [data-testid="stSlider"] { padding-block:5px !important; }
          [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
          [data-testid="stSelectbox"], [data-testid="stFileUploader"] { touch-action:manipulation !important; }
          [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
          [data-testid="stSelectbox"], [data-testid="stFileUploader"] { min-width:0 !important; max-width:100% !important; }
          .stMarkdown table { display:block !important; width:100% !important; overflow-x:auto !important; }
          .stImage img { max-width:100% !important; height:auto !important; }
        }
        @media (max-width: 520px) {
          [data-testid="stMainBlockContainer"] { padding-top:1.15rem !important; }
          [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab"] {
            min-height:44px !important; padding:0 11px !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab"] p { font-size:12px !important; }
          [data-testid="stSidebar"] {
            width:min(90vw, 320px) !important; min-width:min(90vw, 320px) !important;
            max-width:90vw !important;
          }
          [data-testid="stNumberInput"] button, [data-testid="stDateInput"] button,
          [data-testid="stTimeInput"] button { min-width:40px !important; min-height:40px !important; }
          [data-testid="stFileUploader"] section { padding:12px !important; }
          [data-testid="stFileUploader"] button { min-height:42px !important; }
          .st-key-sheet_settings_panel [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          .st-key-pool_fallback_grid [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex:1 1 calc(50% - .5rem) !important; width:calc(50% - .5rem) !important;
            max-width:calc(50% - .5rem) !important; min-width:0 !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has(.st-key-preview_nav_previous) > [data-testid="stColumn"] {
            flex:1 1 calc(50% - .4rem) !important; width:calc(50% - .4rem) !important;
            max-width:calc(50% - .4rem) !important;
          }
          .st-key-card_preview [data-testid="stHorizontalBlock"]:has(.st-key-preview_nav_previous) > [data-testid="stColumn"]:first-child {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] button {
            min-height:48px !important; padding:8px 4px !important; font-size:11px !important;
            white-space:normal !important; overflow-wrap:anywhere !important;
          }
          .st-key-card_preview .st-key-preview_nav_previous button,
          .st-key-card_preview .st-key-preview_nav_next button { min-height:44px !important; font-size:10px !important; }
          .st-key-sheet_canvas_panel [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="stColumn"] {
            flex:1 1 100% !important; width:100% !important; max-width:100% !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stMetricValue"] {
            font-size:clamp(1.05rem,5.2vw,1.45rem) !important;
            line-height:1.2 !important; white-space:normal !important; overflow-wrap:anywhere !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stMetricLabel"] {
            font-size:clamp(.75rem,3.3vw,.9rem) !important;
            white-space:normal !important; overflow-wrap:anywhere !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stRadio"] > div {
            display:flex !important; flex-wrap:wrap !important; align-items:stretch !important;
            gap:.35rem .7rem !important; width:100% !important;
          }
          .st-key-sheet_canvas_panel [data-testid="stRadio"] label {
            min-height:44px !important; white-space:normal !important;
            overflow-wrap:anywhere !important; max-width:100% !important;
          }
        }
          /* Tablets touch: aplica os mesmos alvos ampliados mesmo em larguras acima de 760 px. */
        @media (pointer: coarse), (max-width: 1024px) {
          .stButton > button, .stDownloadButton > button,
          [data-testid="stFormSubmitButton"] button {
            min-height:50px !important; padding:10px 12px !important;
            touch-action:manipulation !important;
          }
          [data-testid="stRadio"] label {
            min-height:48px !important; display:flex !important; align-items:center !important;
            padding:6px 4px !important; touch-action:manipulation !important;
          }
          [data-testid="stSegmentedControl"] button {
            min-height:50px !important; padding:9px 8px !important;
            touch-action:manipulation !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab-list"] {
            display:flex !important; flex-wrap:nowrap !important; overflow-x:auto !important;
            overflow-y:hidden !important; max-width:100% !important;
            -webkit-overflow-scrolling:touch !important; overscroll-behavior-x:contain !important;
          }
          [data-testid="stTabs"] [data-baseweb="tab"] {
            flex:0 0 auto !important; min-width:max-content !important; min-height:48px !important;
            touch-action:pan-x !important;
          }
          /* Tablets: textos da Folha de Impressao quebram linha mesmo acima de 760 px. */
          .st-key-card_right, .st-key-sheet_settings_panel, .st-key-sheet_canvas_panel {
            width:100% !important; max-width:100% !important; min-width:0 !important;
            overflow-wrap:anywhere !important;
          }
          .st-key-card_right [data-testid="stMarkdownContainer"],
          .st-key-card_right [data-testid="stCaptionContainer"],
          .st-key-card_right [data-testid="stAlert"],
          .st-key-sheet_settings_panel [data-testid="stMarkdownContainer"],
          .st-key-sheet_settings_panel [data-testid="stCaptionContainer"],
          .st-key-sheet_settings_panel [data-testid="stAlert"],
          .st-key-sheet_canvas_panel [data-testid="stMarkdownContainer"],
          .st-key-sheet_canvas_panel [data-testid="stCaptionContainer"],
          .st-key-sheet_canvas_panel [data-testid="stAlert"] {
            min-width:0 !important; max-width:100% !important; white-space:normal !important;
            overflow-wrap:anywhere !important; word-break:normal !important;
          }
          .st-key-card_right [data-testid="stMarkdownContainer"] p,
          .st-key-card_right [data-testid="stCaptionContainer"] p,
          .st-key-card_right [data-testid="stAlert"] p,
          .st-key-sheet_settings_panel [data-testid="stMarkdownContainer"] p,
          .st-key-sheet_settings_panel [data-testid="stCaptionContainer"] p,
          .st-key-sheet_settings_panel [data-testid="stAlert"] p,
          .st-key-sheet_canvas_panel [data-testid="stMarkdownContainer"] p,
          .st-key-sheet_canvas_panel [data-testid="stCaptionContainer"] p,
          .st-key-sheet_canvas_panel [data-testid="stAlert"] p,
          .st-key-sheet_canvas_panel [data-testid="stMetricLabel"],
          .st-key-sheet_canvas_panel [data-testid="stMetricValue"],
          .st-key-sheet_canvas_panel [data-testid="stRadio"] label,
          .st-key-sheet_canvas_panel [data-testid="stRadio"] [data-testid="stMarkdownContainer"] p {
            max-width:100% !important; min-width:0 !important; height:auto !important;
            white-space:normal !important; overflow-wrap:anywhere !important; word-break:normal !important;
          }
          [data-testid="stSlider"] [role="slider"] {
            width:28px !important; height:28px !important; touch-action:none !important;
          }
          [data-testid="stNumberInput"] button, [data-testid="stDateInput"] button,
          [data-testid="stTimeInput"] button, [data-testid="stFileUploader"] button {
            min-width:44px !important; min-height:48px !important; touch-action:manipulation !important;
          }
          [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input {
            min-height:48px !important; font-size:16px !important;
          }
          [data-testid="stSelectbox"] [role="combobox"] { min-height:48px !important; }
          [data-testid="stSidebar"] .sidebar-brand-logo img {
            display:block !important; width:100% !important; max-width:220px !important;
            max-height:24vh !important; height:auto !important; object-fit:contain !important;
          }
        }
        </style>""",
        unsafe_allow_html=True,
    )
    brand.render_header()
    if st.session_state.pop("_atomica_project_restored_notice", False):
        st.success("Projeto do Atômica Print Lab reaberto. Pool, ordem, quantidades e configurações foram restaurados.")

    st.session_state.setdefault("artwork_items", [])
    st.session_state.setdefault("artwork_next_id", 1)
    st.session_state.setdefault("artwork_upload_revision", 0)
    st.session_state.setdefault("_artwork_upload_processed_signatures", [])
    # As opcoes antes eram widgets st.checkbox e agora usam botoes com estado
    # explicito. Os mesmos bool keys sao mantidos para preservar selecoes salvas.
    st.session_state.setdefault("artwork_selected_id", None)
    st.session_state.setdefault("artwork_batch_selected_ids", [])
    st.session_state.setdefault("_artwork_batch_panel_reset_version", 0)
    if st.session_state.pop("_pool_fallback_batch_clear_pending", False):
        for artwork_entry in st.session_state.get("artwork_items", []):
            fallback_key = f"pool_batch_select_{int(artwork_entry.get('id', -1))}"
            if fallback_key in st.session_state:
                st.session_state[fallback_key] = False
    _normalize_artwork_item_ids()

    # Toda a ferramenta permanece em uma única sessão: a folha de impressão.
    # O mesmo preview edita a arte em foco e também exporta seu PNG individual.
    # As opções de papel, margens, espaçamento e disposição são preservadas
    # diretamente da organização funcional da versão 3.10.

    # ---------------- Configuração compartilhada do botton ----------------
    with st.sidebar:
        brand.render_sidebar_logo()
        brand.sidebar_title("INSUMOS", brand.CYAN)
        format_label = st.radio("Formato", ["Redondo", "Quadrado"], horizontal=True, key="format_label")
        selected_shape = "circle" if format_label == "Redondo" else "square"
        size_options = [key for key, item in BUTTONS.items() if item.shape == selected_shape]
        size_key = st.selectbox(
            "Tamanho do botton", size_options, key="single_size",
            on_change=_commit_selected_artwork_before_size_change,
            filter_mode=None,
            accept_new_options=False,
        )
        base_spec = BUTTONS[size_key]
        cut_overrides = st.session_state.setdefault("_cut_diameter_overrides", {})
        cut_widget_key = _cut_diameter_widget_key(size_key)
        if cut_widget_key not in st.session_state:
            initial_cut = cut_overrides.get(size_key, base_spec.cut)
            st.session_state[cut_widget_key] = _clamp_cut_diameter(initial_cut, base_spec)
        else:
            # Revalida o estado ao voltar a um tamanho, sem deixar que um valor
            # antigo ultrapasse os limites físicos do perfil selecionado.
            st.session_state[cut_widget_key] = _clamp_cut_diameter(
                st.session_state[cut_widget_key], base_spec
            )

        st.number_input(
            "Diâmetro da guia magenta · Corte (mm)",
            min_value=float(base_spec.fold),
            max_value=float(base_spec.cut) + 5.0,
            step=0.5,
            format="%.1f",
            key=cut_widget_key,
            on_change=_save_cut_diameter_for_size,
            args=(size_key, cut_widget_key),
            help=(
                f"Padrão: {base_spec.cut:g} mm. O valor pode variar da guia da Dobra "
                f"({base_spec.fold:g} mm) até 5 mm acima do corte padrão "
                f"({base_spec.cut + 5:g} mm). As guias Dobra, Arte e Segurança "
                "mantêm suas medidas originais."
            ),
        )
        cut_diameter = _clamp_cut_diameter(st.session_state[cut_widget_key], base_spec)
        cut_overrides[size_key] = cut_diameter
        spec = replace(base_spec, cut=cut_diameter)
        st.caption(
            f"Padrão: {base_spec.cut:g} mm · Limites: {base_spec.fold:g} a "
            f"{base_spec.cut + 5:g} mm. O ajuste deste tamanho fica salvo ao alternar bottons."
        )
        st.markdown(legend_html(spec), unsafe_allow_html=True)
        st.divider()
        gabarito_svg = build_template_svg(spec)
        gabarito_name = (
            f"gabarito_botton_quadrado_{spec.size:g}x{spec.size:g}mm_A4.svg"
            if spec.shape == "square"
            else f"gabarito_botton_redondo_{spec.size:g}mm_A4.svg"
        )
        st.download_button(
            "📐 Baixar gabarito SVG · A4",
            data=gabarito_svg,
            file_name=gabarito_name,
            mime="image/svg+xml",
            use_container_width=True,
            help="Gabarito vetorial em A4 (210 × 297 mm), com todas as guias nas dimensões reais em milímetros.",
            key="download_button_template_svg_a4",
        )

    # Estado das configurações da folha é lido antes do Pool, pois capacidade e
    # preenchimento dependem da orientação, margens e disposição. Os widgets são
    # renderizados mais abaixo, ao lado do canvas, mas seus valores persistem no
    # session_state entre os reruns.
    st.session_state.setdefault("paper_size", "A4")
    st.session_state.setdefault("custom_page_w", 210.0)
    st.session_state.setdefault("custom_page_h", 297.0)
    st.session_state.setdefault("page_orientation", "Retrato")
    st.session_state.setdefault("margin_mm", 5.0)
    st.session_state.setdefault("gap_mm", 3.0)
    for guide in GUIDES:
        st.session_state.setdefault(f"guide_single_{guide.key}", guide.key == "cut")

    paper = st.session_state.get("paper_size", "A4")
    if paper not in [*PAPERS, "Personalizado"]:
        paper = "A4"
        st.session_state["paper_size"] = paper
    if paper == "Personalizado":
        pw = float(st.session_state.get("custom_page_w", 210.0))
        ph = float(st.session_state.get("custom_page_h", 297.0))
    else:
        pw, ph = PAPERS[paper]
    orient = st.session_state.get("page_orientation", "Retrato")
    if orient not in {"Retrato", "Paisagem"}:
        orient = "Retrato"
        st.session_state["page_orientation"] = orient
    page_w, page_h = (min(pw, ph), max(pw, ph)) if orient == "Retrato" else (max(pw, ph), min(pw, ph))

    # O perfil não distingue a rotação da folha; assim, mudar Retrato/Paisagem
    # preserva margem, espaçamento e disposição escolhidos pelo usuário.
    recommended_margin, recommended_gap, recommended_mode = recommend_single_layout(
        page_w, page_h, spec
    )
    st.session_state["_recommended_margin"] = recommended_margin
    st.session_state["_recommended_gap"] = recommended_gap
    profile_key = layout_settings_profile_key(paper, page_w, page_h, size_key, spec.shape)

    # Migração única da folha: versões intermediárias podiam armazenar 5/3 mm
    # como se fossem valores escolhidos pelo usuário. O primeiro pacote publicado
    # calculava recomendações específicas por papel e tamanho do botton. Nesta
    # revisão, descartamos somente esse cache legado uma vez e voltamos a usar
    # recommend_single_layout() como fonte dos padrões de cada perfil. Depois
    # disso, qualquer alteração manual continua sendo salva por perfil.
    layout_profile_schema = 2
    if int(st.session_state.get("_layout_profile_schema", 0) or 0) < layout_profile_schema:
        st.session_state["_layout_profiles"] = {}
        st.session_state.pop("_layout_profile_key", None)
        st.session_state["_layout_profile_schema"] = layout_profile_schema
        st.session_state["margin_mm"] = recommended_margin
        st.session_state["gap_mm"] = recommended_gap
        st.session_state["layout_label"] = (
            "Hexagonal (mais compacta)" if recommended_mode == "hex" else "Grade"
        )

    saved_layout_profiles = st.session_state.setdefault("_layout_profiles", {})
    previous_profile_key = st.session_state.get("_layout_profile_key")
    if previous_profile_key != profile_key:
        # Salva a configuração do perfil anterior antes de carregar/sugerir a nova.
        if previous_profile_key is not None:
            saved_layout_profiles[previous_profile_key] = {
                "margin_mm": float(st.session_state.get("margin_mm", recommended_margin)),
                "gap_mm": float(st.session_state.get("gap_mm", recommended_gap)),
                "layout_label": str(st.session_state.get("layout_label", "Grade")),
            }
        remembered = saved_layout_profiles.get(profile_key)
        if isinstance(remembered, dict):
            st.session_state["margin_mm"] = float(remembered.get("margin_mm", recommended_margin))
            st.session_state["gap_mm"] = float(remembered.get("gap_mm", recommended_gap))
            st.session_state["layout_label"] = str(
                remembered.get("layout_label", "Hexagonal (mais compacta)" if recommended_mode == "hex" else "Grade")
            )
        else:
            st.session_state["margin_mm"] = recommended_margin
            st.session_state["gap_mm"] = recommended_gap
            st.session_state["layout_label"] = "Hexagonal (mais compacta)" if recommended_mode == "hex" else "Grade"
        st.session_state["_layout_profile_key"] = profile_key

    layout_options = ["Grade"] + (["Hexagonal (mais compacta)"] if spec.shape == "circle" else [])
    if st.session_state.get("layout_label") not in layout_options:
        st.session_state["layout_label"] = layout_options[0]
    margin = float(st.session_state.get("margin_mm", recommended_margin))
    gap = float(st.session_state.get("gap_mm", recommended_gap))
    layout_label = st.session_state.get("layout_label", layout_options[0])
    saved_layout_profiles[profile_key] = {
        "margin_mm": margin,
        "gap_mm": gap,
        "layout_label": layout_label,
    }
    mode = "grid" if layout_label == "Grade" else "hex"
    guides = tuple(g.key for g in GUIDES if bool(st.session_state.get(f"guide_single_{g.key}", g.key == "cut")))

    # ---------------- Pool de artes ----------------
    brand.section_title(None, "ARTES DA IMPRESSÃO", brand.CYAN)
    upload_revision = st.session_state["artwork_upload_revision"]
    with st.expander(
        "＋ Adicionar imagens",
        expanded=(len(st.session_state["artwork_items"]) == 0),
    ):
        # O uploader nativo do Streamlit continua sendo o caminho padrão e único
        # desta área. A seleção fica retida até a confirmação; se um arquivo falhar
        # na leitura local, os demais são aproveitados e o usuário pode tentar
        # novamente sem reenviar os que já entraram no Pool.
        with st.form(f"upload_artworks_{upload_revision}", clear_on_submit=False):
            uploaded_files = st.file_uploader(
                "Selecione imagens ou um projeto Atômica Print Lab (.atp)",
                type=["png", "jpg", "jpeg", "webp", "pdf", "atp"],
                accept_multiple_files=True,
                max_upload_size=200,
                key=f"artwork_file_uploader_{upload_revision}",
                help=(
                    "Imagens são adicionadas individualmente. Um arquivo .atp recupera o Pool completo e as configurações do projeto. "
                    "PDFs de projeto exportados por versões anteriores também podem ser reabertos. "
                    "Em alguns celulares/tablets, o seletor do próprio sistema pode limitar a seleção múltipla; "
                    "nesse caso, adicione as imagens em etapas menores e confirme cada seleção. Se o seletor abrir outro aplicativo, retorne ao navegador sem fechar a página do editor."
                ),
            )
            add_uploads = st.form_submit_button(
                "Adicionar imagens à lista", type="primary", use_container_width=True
            )

        st.info(
            "Também é possível enviar um arquivo de projeto .atp para retomar o trabalho exatamente de onde parou. "
            "O aplicativo recupera o Pool, a ordem das artes, as quantidades, os originais, os fundos e as configurações salvas. "
            "PDFs de projeto de versões anteriores continuam aceitos quando contêm os dados internos.",
            icon="📁",
        )

        st.caption(
            "No celular ou tablet, o seletor de arquivos pode levar alguns segundos para retornar ao navegador. "
            "A sessão do editor foi configurada para tolerar esse intervalo."
        )

        if add_uploads:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            project_uploads = [
                f for f in (uploaded_files or [])
                if str(getattr(f, "name", "")).lower().endswith((".pdf", ".atp"))
            ]
            if project_uploads:
                if len(project_uploads) != 1 or len(uploaded_files or []) != 1:
                    st.toast("Envie um arquivo de projeto por vez, sem misturá-lo com imagens.", icon="⚠️")
                else:
                    uploaded_project = project_uploads[0]
                    project_name = str(getattr(uploaded_project, "name", "")).lower()
                    try:
                        project_bytes = uploaded_project.getvalue()
                        if project_name.endswith(".atp"):
                            restored_project = restore_atomica_project_from_atp(project_bytes)
                        else:
                            if not project_bytes.startswith(b"%PDF-"):
                                raise ValueError("O arquivo selecionado não parece ser um PDF válido.")
                            restored_project = restore_atomica_project_from_pdf(project_bytes)
                        st.session_state["_pending_atomica_project_restore"] = restored_project
                        st.rerun(scope="app")
                    except Exception as exc:
                        st.toast(str(exc) or "Não foi possível reabrir este projeto.", icon="⚠️")
            else:
                added_ids = []
                existing_ids_before_upload = {int(item["id"]) for item in st.session_state["artwork_items"]}
                selected_before_upload = st.session_state.get("artwork_selected_id")
                processed_signatures = set(st.session_state.get("_artwork_upload_processed_signatures", []))
                files_to_prepare = []
                failures = []

                # A leitura é isolada por arquivo. Um item vazio/inacessível enviado
                # pelo seletor móvel não deve invalidar os outros arquivos do lote.
                for uploaded_file in (uploaded_files or []):
                    file_name = str(getattr(uploaded_file, "name", "imagem"))
                    try:
                        image_bytes = uploaded_file.getvalue()
                        if not image_bytes:
                            raise ValueError(
                                "O dispositivo retornou um arquivo vazio. Selecione a imagem novamente no app Arquivos ou Galeria."
                            )
                        digest = hashlib.sha256(image_bytes).hexdigest()
                        signature = f"{file_name}|{len(image_bytes)}|{digest}"
                        if signature in processed_signatures:
                            continue
                        files_to_prepare.append((file_name, image_bytes, digest, signature))
                    except Exception as exc:
                        failures.append((file_name, str(exc) or "Não foi possível ler o arquivo enviado."))

                if not uploaded_files:
                    st.warning(
                        "Nenhuma imagem chegou ao editor. Volte ao seletor do dispositivo, confirme a seleção e tente novamente."
                    )
                else:
                    prepared = [None] * len(files_to_prepare)
                    progress = st.progress(0, text="Preparando miniaturas…") if files_to_prepare else None
                    if files_to_prepare:
                        # Dois workers equilibram paralelismo e memória ao lidar com
                        # fotos grandes; cada falha é isolada para preservar os demais.
                        with ThreadPoolExecutor(max_workers=min(2, len(files_to_prepare))) as executor:
                            future_indexes = {
                                executor.submit(prepare_uploaded_artwork, name, data): index
                                for index, (name, data, _digest, _signature) in enumerate(files_to_prepare)
                            }
                            completed = 0
                            for future in as_completed(future_indexes):
                                index = future_indexes[future]
                                try:
                                    prepared[index] = future.result()
                                except Exception as exc:
                                    file_name = files_to_prepare[index][0]
                                    failures.append((file_name, str(exc) or "Formato de imagem inválido."))
                                completed += 1
                                progress.progress(
                                    completed / len(files_to_prepare),
                                    text=f"Preparando miniaturas… {completed}/{len(files_to_prepare)}",
                                )
                        progress.empty()

                    used_item_ids = {int(item.get("id", -1)) for item in st.session_state["artwork_items"]}
                    next_id = max(int(st.session_state.get("artwork_next_id", 1)), max(used_item_ids, default=0) + 1)
                    for index, (file_name, image_bytes, digest, signature) in enumerate(files_to_prepare):
                        prepared_item = prepared[index]
                        if prepared_item is None:
                            continue
                        thumb_digest, thumb_url = prepared_item
                        item_id = next_id
                        next_id += 1
                        st.session_state["artwork_items"].append({
                            "id": item_id,
                            "name": file_name,
                            "image": image_bytes,
                            "image_digest": thumb_digest or digest,
                            "original_image": image_bytes,
                            "original_image_digest": thumb_digest or digest,
                            "thumbnail_data_url": thumb_url,
                            "qty": 1,
                            "edit": {
                                # Regra inicial: toda nova arte começa em Preencher até o Corte.
                                # Um gesto manual desativa explicitamente esse preset.
                                "zoom": 1.0,
                                "initial_safety_relative_zoom": float(spec.cut) / max(float(spec.safe), 1e-9),
                                "offset_x": 0.0,
                                "offset_y": 0.0,
                                "transform_revision": 0,
                                "event_seq": 0,
                                "fit_basis_mode": "cut",
                                "fit_cut_checked": True,
                                "fit_art_checked": False,
                                "manual_transform_active": False,
                                "scale_reference_spec": _button_scale_profile(spec),
                                "scale_reference_diameter": float(spec.safe),
                                "safety_relative_zoom": float(spec.cut) / max(float(spec.safe), 1e-9),
                                "safety_relative_offset_x": 0.0,
                                "safety_relative_offset_y": 0.0,
                                "safety_transform_schema": 1,
                                "initial_cut_fit_schema": 4,
                                "background_mode": "Cor sólida",
                                "blur_strength": 0.5,
                                "background": "#FFFFFF",
                                "blur_base_color": "#FFFFFF",
                                "background_image_bytes": None,
                                "background_image_digest": "",
                                "background_image_name": "",
                                "custom_background_active": False,
                                "custom_background_pending": False,
                                "background_upload_revision": 0,
                                "custom_background_revision": 0,
                                **IMAGE_ADJUSTMENT_DEFAULTS,
                            },
                        })
                        added_ids.append(item_id)
                        processed_signatures.add(signature)

                    st.session_state["artwork_next_id"] = next_id
                    st.session_state["_artwork_upload_processed_signatures"] = sorted(processed_signatures)

                    if added_ids and selected_before_upload not in existing_ids_before_upload:
                        st.session_state["artwork_selected_id"] = added_ids[0]

                    if failures:
                        st.warning(
                            f"{len(failures)} arquivo(s) apresentaram problema. Os arquivos válidos foram adicionados; "
                            "os que falharam continuam no seletor para nova tentativa."
                        )
                        with st.expander("Ver arquivos que precisam de atenção", expanded=True):
                            for file_name, reason in failures:
                                st.error(f"{file_name}: {reason}")
                    else:
                        # Só limpa o seletor quando todos os arquivos pendentes foram
                        # processados. Isso evita perder a lista se um arquivo falhar.
                        st.session_state["artwork_upload_revision"] += 1
                        st.session_state["_artwork_upload_processed_signatures"] = []
                        if added_ids:
                            st.toast(f"{len(added_ids)} imagem(ns) adicionada(s) ao Pool.", icon="✅")
                        st.rerun()
        _render_mobile_upload_panel(spec)

    if isinstance(st.session_state.get("_mobile_upload_session"), dict):
        _mobile_upload_inbox_monitor(spec)

    art_items = st.session_state["artwork_items"]
    batch_notice = st.session_state.pop("_artwork_batch_notice", None)
    if batch_notice:
        st.toast(batch_notice, icon="✅")
    batch_errors = st.session_state.pop("_artwork_batch_errors", [])
    if batch_errors:
        with st.expander(f"Detalhes de processamento ({len(batch_errors)} erro(s))", expanded=True):
            for error in batch_errors:
                st.warning(error)
    pool_delete_notice = st.session_state.pop("_pool_delete_notice", None)
    if pool_delete_notice:
        st.toast(pool_delete_notice, icon="✅")
    if art_items:
        current_pool_ids = {int(item["id"]) for item in art_items}
        st.session_state["artwork_batch_selected_ids"] = [
            int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])
            if int(value) in current_pool_ids
        ]
    editor_navigation_target = st.session_state.pop("_editor_navigation_target_id", None)
    try:
        editor_navigation_target = int(editor_navigation_target) if editor_navigation_target is not None else None
    except (TypeError, ValueError):
        editor_navigation_target = None

    if art_items:
        # Inicializa/migra TODAS as artes antes de o Pool permitir alternar
        # entre miniaturas. Assim, a segunda/terceira imagem não depende de
        # ser selecionada uma primeira vez para receber seu preset canônico.
        for artwork_entry in art_items:
            ensure_artwork_edit_state(artwork_entry, spec, sync_session=False)
            _sync_processed_artwork(artwork_entry)

        saved_pool_state = st.session_state.get("artwork_pool_ui")
        skip_saved_quantities = bool(st.session_state.pop("_skip_saved_pool_quantities_once", False))
        if saved_pool_state is not None:
            current = st.session_state["artwork_items"]
            current_ids = [int(item["id"]) for item in current]
            valid_ids = set(current_ids)
            saved_order = getattr(saved_pool_state, "order", None)
            if isinstance(saved_order, (list, tuple)):
                try:
                    ordered_ids = [int(value) for value in saved_order if int(value) in valid_ids]
                except (TypeError, ValueError):
                    ordered_ids = []
                ordered_ids.extend(item_id for item_id in current_ids if item_id not in ordered_ids)
                by_id = {int(item["id"]): item for item in current}
                st.session_state["artwork_items"] = [by_id[item_id] for item_id in ordered_ids]

            saved_quantities = getattr(saved_pool_state, "quantities", None)
            if isinstance(saved_quantities, dict) and not skip_saved_quantities:
                for item in st.session_state["artwork_items"]:
                    value = saved_quantities.get(str(item["id"]), item.get("qty", 1))
                    try:
                        item["qty"] = max(1, min(2000, int(value)))
                    except (TypeError, ValueError):
                        item["qty"] = max(1, int(item.get("qty", 1)))

            saved_selected = getattr(saved_pool_state, "selected_id", None)
            try:
                saved_selected = int(saved_selected) if saved_selected is not None else None
            except (TypeError, ValueError):
                saved_selected = None
            remaining_ids = {int(item["id"]) for item in st.session_state["artwork_items"]}
            if saved_selected in remaining_ids and editor_navigation_target is None:
                previous_selected = st.session_state.get("artwork_selected_id")
                if previous_selected is not None and int(previous_selected) != saved_selected:
                    # Persiste a arte anterior antes de trocar o prefixo dos
                    # widgets. O estado visual de cada imagem fica isolado pelo ID.
                    try:
                        _commit_artwork_editor_state(int(previous_selected))
                    except (TypeError, ValueError):
                        _persist_artwork_transform_for_item(int(previous_selected))
                st.session_state["artwork_selected_id"] = saved_selected

        art_items = st.session_state["artwork_items"]
        current_ids_int = [int(item["id"]) for item in art_items]
        if editor_navigation_target in current_ids_int:
            st.session_state["artwork_selected_id"] = editor_navigation_target
        pool_positions = compute_layout(page_w, page_h, spec.cut, margin, gap, mode)
        pool_page_capacity = len(pool_positions)
        current_ids = [str(item["id"]) for item in art_items]
        if st.session_state.get("artwork_selected_id") not in [int(value) for value in current_ids]:
            st.session_state["artwork_selected_id"] = int(current_ids[0])

        pool_data_items = [
            {
                "id": str(item["id"]),
                "thumb": item.get("thumbnail_data_url") or thumbnail_data_url(item["image_digest"], item["image"]),
                "qty": max(1, int(item.get("qty", 1))),
            }
            for item in art_items
        ]
        pool_order = [str(item["id"]) for item in art_items]
        pool_quantities = {str(item["id"]): max(1, int(item.get("qty", 1))) for item in art_items}
        pool_selected = str(st.session_state["artwork_selected_id"])
        st.markdown(
            """
            <style>
              .pool-guide{margin:0 0 13px;padding:15px 16px 14px;border:1px solid rgba(91,127,160,.34);
                border-radius:15px;background:linear-gradient(135deg,rgba(18,41,65,.94),rgba(13,27,44,.96));
                box-shadow:0 8px 24px rgba(0,0,0,.15);color:#EAF1F7}
              .pool-guide-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:13px}
              .pool-guide-kicker{font-size:9px;font-weight:900;letter-spacing:.16em;color:#8FC9D8;margin-bottom:4px}
              .pool-guide-title{font-size:17px;line-height:1.2;font-weight:850;color:#F7FAFD}
              .pool-guide-subtitle{font-size:11px;line-height:1.45;color:#B5C5D4;margin-top:4px;max-width:610px}
              .pool-guide-legend{display:flex;flex-wrap:wrap;gap:8px}
              .pool-guide-key{display:flex;align-items:center;gap:8px;min-width:180px;padding:8px 10px;
                border:1px solid rgba(128,157,184,.22);border-radius:10px;background:rgba(5,17,31,.42)}
              .pool-guide-key b{display:block;font-size:10px;color:#F3F7FB;line-height:1.25}
              .pool-guide-key small{display:block;font-size:9px;line-height:1.35;color:#AFC0D0;margin-top:2px}
              .pool-guide-focus{width:22px;height:22px;flex:none;border:2px solid #2DBB72;border-radius:6px;background:#102D35;
                box-shadow:0 0 0 2px rgba(45,187,114,.13)}
              .pool-guide-check{width:19px;height:19px;flex:none;display:grid;place-items:center;border:1px solid #fff;
                border-radius:6px;background:#26C6DA;color:#061B2D;font-size:12px;font-weight:950}
              .pool-guide-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:8px}
              .pool-guide-item{display:flex;gap:9px;align-items:flex-start;min-width:0;padding:9px 10px;
                border:1px solid rgba(111,146,176,.18);border-radius:10px;background:rgba(5,17,31,.28)}
              .pool-guide-no{width:22px;height:22px;display:grid;place-items:center;flex:none;border-radius:7px;
                color:#071827;background:linear-gradient(135deg,#1BA9D8,#6BC5D8);font-size:9px;font-weight:950}
              .pool-guide-item > div b{display:block;font-size:10px;line-height:1.3;color:#F2F6FA;margin:0 0 3px}
              .pool-guide-item > div span{display:block;font-size:9px;line-height:1.42;color:#B5C4D2}
              @media(max-width:650px){.pool-guide{padding:12px}.pool-guide-key{min-width:100%;flex:1}.pool-guide-grid{grid-template-columns:1fr 1fr}}
              @media(max-width:430px){.pool-guide-grid{grid-template-columns:1fr}}
            </style>
            <div class="pool-guide">
              <div class="pool-guide-head">
                <div>
                  <div class="pool-guide-kicker">GUIA RÁPIDA · POOL DE ARTES</div>
                  <div class="pool-guide-title">Como usar o Pool</div>
                  <div class="pool-guide-subtitle">O Pool reúne as imagens da produção. O contorno verde e a caixa azul têm funções diferentes:</div>
                </div>
                <div class="pool-guide-legend">
                  <div class="pool-guide-key"><span class="pool-guide-focus"></span><span><b>Foco de edição</b><small>Moldura verde: arte aberta na prévia para edição individual.</small></span></div>
                  <div class="pool-guide-key"><span class="pool-guide-check">✓</span><span><b>Selecionada para ações</b><small>Caixa azul: inclui a arte nos ajustes em lote e na remoção por IA.</small></span></div>
                </div>
              </div>
              <div class="pool-guide-grid">
                <div class="pool-guide-item"><span class="pool-guide-no">01</span><div><b>Abrir e editar</b><span>Clique na miniatura para colocá-la em foco. A moldura verde identifica qual arte está aberta na prévia.</span></div></div>
                <div class="pool-guide-item"><span class="pool-guide-no">02</span><div><b>Selecionar para ações em lote</b><span>Use a caixa azul no canto inferior direito. A seleção é independente do foco; a arte em foco só entra no lote se sua caixa azul estiver marcada.</span></div></div>
                <div class="pool-guide-item"><span class="pool-guide-no">03</span><div><b>Quantidade e ordem de impressão</b><span>Digite a quantidade ou use −/+. Arraste pelo ícone de alça para reorganizar a sequência de impressão.</span></div></div>
                <div class="pool-guide-item"><span class="pool-guide-no">04</span><div><b>Selecionar, completar ou excluir</b><span>Use “Selecionar todas” para marcar o Pool. “Preencher folha” completa as posições restantes; excluir seleção remove as marcadas, e sem seleção o botão oferece excluir todas.</span></div></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        try:
            pool_component = get_artwork_pool_component()
            pool_result = pool_component(
                data={
                    "items": pool_data_items,
                    "order": pool_order,
                    "quantities": pool_quantities,
                    "selected_id": pool_selected,
                    "selected_ids": [str(value) for value in st.session_state.get("artwork_batch_selected_ids", [])],
                    "delete_ack": int(st.session_state.get("_artwork_pool_delete_ack", 0) or 0),
                    "per_page": pool_page_capacity,
                },
                default={
                    "order": pool_order, "quantities": pool_quantities, "selected_id": pool_selected,
                    "selected_ids": [str(value) for value in st.session_state.get("artwork_batch_selected_ids", [])],
                },
                key="artwork_pool_ui",
                on_order_change=lambda: None,
                on_quantities_change=lambda: None,
                on_selected_id_change=_on_pool_selected_art_change,
                on_selected_ids_change=_on_pool_batch_selection_change,
                on_delete_ids_change=_on_pool_delete_ids_change,
                on_remove_id_change=_on_pool_remove_art_change,
            )
            remove_id = getattr(pool_result, "remove_id", None)
            if remove_id is not None:
                # Idempotente: se o callback já removeu, esta segunda passagem
                # não altera nem restaura a lista.
                _remove_artwork_from_pool(remove_id)
            delete_payload = getattr(pool_result, "delete_ids", None)
            if isinstance(delete_payload, dict):
                delete_nonce = int(delete_payload.get("nonce", 0) or 0)
                _remove_artworks_from_pool(delete_payload.get("ids", []))
                if delete_nonce:
                    st.session_state["_artwork_pool_delete_ack"] = delete_nonce

            current = st.session_state["artwork_items"]
            valid_ids = {int(item["id"]) for item in current}
            result_order = getattr(pool_result, "order", None)
            if isinstance(result_order, (list, tuple)):
                try:
                    ordered_ids = [int(value) for value in result_order if int(value) in valid_ids]
                except (TypeError, ValueError):
                    ordered_ids = []
                ordered_ids.extend(int(item["id"]) for item in current if int(item["id"]) not in ordered_ids)
                by_id = {int(item["id"]): item for item in current}
                st.session_state["artwork_items"] = [by_id[item_id] for item_id in ordered_ids]

            result_quantities = getattr(pool_result, "quantities", None)
            if isinstance(result_quantities, dict):
                for item in st.session_state["artwork_items"]:
                    value = result_quantities.get(str(item["id"]), item.get("qty", 1))
                    try:
                        item["qty"] = max(1, min(2000, int(value)))
                    except (TypeError, ValueError):
                        pass

            result_selected = getattr(pool_result, "selected_id", None)
            current_ids_int = [int(item["id"]) for item in st.session_state["artwork_items"]]
            if result_selected is not None:
                try:
                    result_selected = int(result_selected)
                except (TypeError, ValueError):
                    result_selected = None
            if editor_navigation_target in current_ids_int:
                # A navegação anterior/próxima tem prioridade sobre um valor
                # selecionado antigo ainda devolvido pelo componente do Pool.
                st.session_state["artwork_selected_id"] = editor_navigation_target
            elif result_selected in current_ids_int:
                st.session_state["artwork_selected_id"] = result_selected

            result_batch_ids = getattr(pool_result, "selected_ids", None)
            if st.session_state.pop("_pool_component_batch_clear_pending", False):
                # O resultado devolvido nesta execução ainda pode conter a
                # seleção anterior; o estado Python já foi limpo após sucesso.
                st.session_state["artwork_batch_selected_ids"] = []
            elif isinstance(result_batch_ids, (list, tuple, set)):
                valid_batch_ids = set(current_ids_int)
                clean_batch_ids = []
                for value in result_batch_ids:
                    try:
                        item_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if item_id in valid_batch_ids and item_id not in clean_batch_ids:
                        clean_batch_ids.append(item_id)
                st.session_state["artwork_batch_selected_ids"] = clean_batch_ids

        except (AttributeError, TypeError, RuntimeError) as exc:
            if st.session_state.pop("_pool_component_batch_clear_pending", False):
                st.session_state["artwork_batch_selected_ids"] = []
            st.caption("Ordenação por arraste indisponível nesta instalação; use as setas para reorganizar.")
            fallback_items = list(st.session_state["artwork_items"])
            fallback_ids = [int(item["id"]) for item in fallback_items]
            fallback_batch_ids = [int(value) for value in st.session_state.get("artwork_batch_selected_ids", []) if int(value) in set(fallback_ids)]
            fallback_all_selected = bool(fallback_ids) and set(fallback_batch_ids) == set(fallback_ids)
            fallback_action_cols = st.columns(2)
            fallback_action_cols[0].button(
                "Desselecionar todas" if fallback_all_selected else "Selecionar todas",
                key="pool_fallback_toggle_all_selection",
                use_container_width=True,
                disabled=not bool(fallback_items),
                on_click=_toggle_all_fallback_batch_selection,
            )
            fallback_action_cols[1].button(
                "EXCLUIR SELEÇÃO" if fallback_batch_ids else "EXCLUIR TODAS",
                key="pool_fallback_delete_batch",
                use_container_width=True,
                disabled=not bool(fallback_items),
                on_click=_delete_fallback_batch_action,
                help="Remove apenas as artes marcadas; sem seleção, remove todas as artes do Pool.",
            )
            pending_delete_ids = st.session_state.get("_pool_pending_delete_ids")
            if pending_delete_ids is not None:
                st.warning(f"Confirma a exclusão de {len(pending_delete_ids)} arte(s)? Essa ação não pode ser desfeita.")
                confirm_cols = st.columns(2)
                confirm_cols[0].button(
                    "Confirmar exclusão", key="pool_fallback_confirm_delete",
                    type="primary", use_container_width=True,
                    on_click=_confirm_fallback_batch_delete,
                )
                confirm_cols[1].button(
                    "Cancelar", key="pool_fallback_cancel_delete",
                    use_container_width=True, on_click=_cancel_fallback_batch_delete,
                )
            with st.container(key="pool_fallback_grid"):
                cards_per_row = 5
                for row_start in range(0, len(fallback_items), cards_per_row):
                    row_items = fallback_items[row_start:row_start + cards_per_row]
                    columns = st.columns(cards_per_row)
                    for offset, (column, item) in enumerate(zip(columns, row_items)):
                        index = row_start + offset
                        with column:
                            with st.container(border=True):
                                selected = st.session_state.get("artwork_selected_id") == item["id"]
                                st.image(item.get("thumbnail_data_url") or make_art_thumbnail(item["image"]), use_container_width=True)
                                batch_ids = st.session_state.get("artwork_batch_selected_ids", [])
                                batch_selected = int(item["id"]) in [int(value) for value in batch_ids]
                                batch_key = f"pool_batch_select_{int(item['id'])}"
                                render_touch_checkbox(
                                    "Aplicar ajustes", value=batch_selected, key=batch_key,
                                    on_change=_sync_fallback_batch_selection,
                                    args=(int(item["id"]), batch_key),
                                )
                                if st.button("✓ Prévia" if selected else "Selecionar", key=f"pool_select_{item['id']}", use_container_width=True):
                                    st.session_state["artwork_selected_id"] = item["id"]
                                    st.rerun()
                                item_id = int(item["id"])
                                qty_key = f"pool_qty_input_{item_id}"
                                st.session_state[qty_key] = int(item.get("qty", 1))
                                counter_cols = st.columns([0.9, 2.2, 0.9])
                                counter_cols[0].button(
                                    "−", key=f"pool_minus_{item_id}", disabled=int(item.get("qty", 1)) <= 1,
                                    use_container_width=True, on_click=_step_pool_quantity, args=(item_id, -1, qty_key),
                                )
                                counter_cols[1].number_input(
                                    "Unidades", min_value=1, max_value=2000, step=1, key=qty_key,
                                    label_visibility="collapsed", on_change=_sync_pool_quantity_from_widget,
                                    args=(item_id, qty_key),
                                )
                                counter_cols[2].button(
                                    "+", key=f"pool_plus_{item_id}", disabled=int(item.get("qty", 1)) >= 2000,
                                    use_container_width=True, on_click=_step_pool_quantity, args=(item_id, 1, qty_key),
                                )
                                action_cols = st.columns(3)
                                if action_cols[0].button("↑", key=f"pool_up_{item['id']}", disabled=index == 0, use_container_width=True):
                                    items = st.session_state["artwork_items"]
                                    items[index - 1], items[index] = items[index], items[index - 1]
                                    st.rerun()
                                if action_cols[1].button("↓", key=f"pool_down_{item['id']}", disabled=index == len(st.session_state["artwork_items"]) - 1, use_container_width=True):
                                    items = st.session_state["artwork_items"]
                                    items[index + 1], items[index] = items[index], items[index + 1]
                                    st.rerun()
                                if action_cols[2].button("×", key=f"pool_remove_{item['id']}", help="Remover esta arte", use_container_width=True):
                                    st.session_state["artwork_items"] = [entry for entry in st.session_state["artwork_items"] if entry["id"] != item["id"]]
                                    if selected:
                                        remaining = st.session_state["artwork_items"]
                                        st.session_state["artwork_selected_id"] = remaining[0]["id"] if remaining else None
                                    st.rerun()

            fallback_total = sum(max(1, int(item.get("qty", 1))) for item in st.session_state["artwork_items"])
            fallback_remaining = remaining_slots_to_fill(pool_page_capacity, fallback_total)
            selected_for_fill = next(
                (item for item in st.session_state["artwork_items"] if int(item["id"]) == int(st.session_state.get("artwork_selected_id") or -1)),
                None,
            )
            fill_disabled = selected_for_fill is None or fallback_remaining <= 0 or int(selected_for_fill.get("qty", 1)) + fallback_remaining > 2000
            st.button(
                f"Preencher folha com a arte selecionada (+{fallback_remaining})",
                key="pool_fallback_fill_sheet", type="primary", use_container_width=True,
                disabled=fill_disabled, help="Adiciona à arte selecionada exatamente a quantidade necessária para completar a folha atual.",
                on_click=_fill_fallback_pool_sheet,
                args=(int(selected_for_fill["id"]), fallback_remaining) if selected_for_fill else (0, 0),
            )

        art_items = st.session_state["artwork_items"]
        total_art_qty = sum(max(1, int(item.get("qty", 1))) for item in art_items)
        st.caption(f"{len(art_items)} arte(s) no pool · {total_art_qty} botton(s) no lote")
   

    # ---------------- Editor: ferramentas, prévia e exportação PNG ----------------
    selected_item = None
    selected_img = None
    selected_dimensions = None
    background_label = "Cor sólida"
    background_mode = "color"
    bg = "#FFFFFF"
    fit_mode = "cut"
    blur_mm = 0.5
    zoom = 1.0
    off_x = 0.0
    off_y = 0.0
    prefix = None

    current_items = st.session_state["artwork_items"]
    if current_items:
        selected_id = st.session_state.get("artwork_selected_id")
        selected_item = next((entry for entry in current_items if int(entry["id"]) == int(selected_id)), current_items[0])
        st.session_state["artwork_selected_id"] = int(selected_item["id"])
        prefix = ensure_artwork_edit_state(selected_item, spec, sync_session=True)

    # Carrega sempre o estado canônico da arte, mesmo quando a aba ativa não é
    # Enquadrar. Assim a prévia nunca troca temporariamente para valores padrão.
    has_custom_background = False
    custom_background_bytes = b""
    custom_background_digest = ""
    if selected_item is not None:
        saved_edit = selected_item.setdefault("edit", {})
        custom_background_bytes, custom_background_digest = _custom_background_payload(saved_edit)
        has_custom_background = bool(custom_background_bytes and custom_background_digest)
        background_label = str(saved_edit.get("background_mode", "Cor sólida"))
        background_mode = "blur" if background_label == "Imagem desfocada" else "color"
        if background_label == "Sem preenchimento":
            bg = "#FFFFFF"
        else:
            bg_field = "blur_base_color" if background_label == "Imagem desfocada" else "background"
            bg = str(saved_edit.get(bg_field, saved_edit.get("background", "#FFFFFF")))
        fit_mode = str(saved_edit.get("fit_basis_mode", "cut"))
        blur_mm = float(saved_edit.get("blur_strength", 0.5))

    # Cabeçalho, ajustes em lote e navegação ocupam toda a largura da área de edição.
    # As abas deixam de disputar espaço com a coluna estreita de controles.
    with st.container(key="editor_tools_header"):
        brand.section_title(None, "FERRAMENTAS DE EDIÇÃO", brand.YELLOW)
        if selected_item is not None:
            st.caption(f"Arte em foco: {selected_item.get('name', 'Imagem selecionada')}")
        else:
            st.info("Adicione uma arte ao Pool para liberar as ferramentas de edição.")

        valid_artwork_ids = {int(item.get("id", -1)) for item in st.session_state.get("artwork_items", [])}
        batch_target_ids = list(dict.fromkeys(
            int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])
            if int(value) in valid_artwork_ids
        ))
        batch_auto_expand = len(batch_target_ids) >= 2
        st.markdown(
            """<style>
            .st-key-artwork_batch_expanded_panel {
                border:1px solid rgba(238,29,126,.60) !important;
                border-radius:14px !important;
                padding:12px 14px !important;
                background:linear-gradient(112deg,rgba(27,169,216,.15),rgba(238,29,126,.13),rgba(245,221,30,.10)) !important;
                box-shadow:0 7px 22px rgba(0,0,0,.20) !important;
            }
            .st-key-artwork_batch_expanded_panel .batch-panel-expanded-title {
                display:flex;align-items:center;justify-content:space-between;gap:12px;
                margin:0 0 8px;padding:3px 0;color:#FFFFFF;font-weight:900;
                letter-spacing:.045em;text-shadow:0 1px 8px rgba(0,0,0,.28);
            }
            .st-key-artwork_batch_expanded_panel .batch-panel-expanded-title span {
                padding:4px 9px;border-radius:999px;background:rgba(8,12,20,.45);
                border:1px solid rgba(255,255,255,.24);font-size:11px;white-space:nowrap;
            }
            [class*="st-key-artwork_batch_panel_"] [data-testid="stExpander"] {
                border:1px solid rgba(238,29,126,.60) !important;
                border-radius:14px !important;
                overflow:hidden !important;
                background:linear-gradient(112deg,rgba(27,169,216,.15),rgba(238,29,126,.13),rgba(245,221,30,.10)) !important;
                box-shadow:0 7px 22px rgba(0,0,0,.20);
            }
            [class*="st-key-artwork_batch_panel_"] [data-testid="stExpander"] summary {
                background:linear-gradient(90deg,rgba(27,169,216,.20),rgba(238,29,126,.17),rgba(245,221,30,.13)) !important;
                border-bottom:1px solid rgba(238,29,126,.24);
            }
            [class*="st-key-artwork_batch_panel_"] [data-testid="stExpander"] summary p {
                color:#FFFFFF !important;font-weight:850 !important;letter-spacing:.045em !important;
                text-shadow:0 1px 8px rgba(0,0,0,.28);
            }
            [class*="st-key-artwork_batch_panel_"] [data-testid="stExpander"] summary svg {
                color:#F5DD1E !important;
            }
            [class*="st-key-artwork_batch_panel_"] [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
                background:rgba(8,12,20,.30);
            }
            </style>""",
            unsafe_allow_html=True,
        )
        batch_panel_revision = int(st.session_state.get("_artwork_batch_panel_reset_version", 0) or 0)
        with st.container(key=f"artwork_batch_panel_{batch_panel_revision}"):
            def render_batch_controls():
                selected_count = len(batch_target_ids)
                if selected_count:
                    st.caption(
                        f"{selected_count} arte(s) selecionada(s) no Pool. "
                        "As ações serão aplicadas somente às artes marcadas, inclusive à arte em foco quando ela estiver selecionada."
                    )
                else:
                    st.info("Marque no mínimo uma arte no Pool para habilitar as operações em lote.")

                # Inicializa as opções novas sem herdar seleções antigas da v3.28.
                batch_defaults = {
                    "batch_apply_framing_cut": False,
                    "batch_apply_framing_art": False,
                    "batch_apply_blur": False,
                    "batch_apply_solid_color": False,
                    "batch_blur_strength": BATCH_BLUR_DEFAULT,
                    "batch_blur_base_color": st.session_state.get("batch_background_color", BATCH_BACKGROUND_DEFAULT),
                    "batch_solid_background_color": st.session_state.get("batch_background_color", BATCH_BACKGROUND_DEFAULT),
                    "batch_apply_custom_background": False,
                }
                for key, default_value in batch_defaults.items():
                    st.session_state.setdefault(key, default_value)
                # Corrige combinações incompatíveis que possam ter ficado salvas
                # de uma sessão anterior antes de renderizar as caixas.
                _normalise_batch_priority_state(st.session_state)

                left_col, right_col = st.columns(2, gap="medium")
                with left_col:
                    with st.container(border=True):
                        st.markdown("**ENQUADRAMENTO**")
                        st.caption("Preencher até o corte elimina fundos e bordas. Encaixar na área da arte permite exibi-los.")
                        render_touch_checkbox(
                            "Preencher até o corte", key="batch_apply_framing_cut",
                            on_change=_batch_framing_cut_changed,
                            help="A imagem cobre toda a área até a guia Corte e desativa desfoque, cor sólida e fundo personalizado.",
                        )
                        render_touch_checkbox(
                            "Encaixar na área da arte", key="batch_apply_framing_art",
                            on_change=_batch_framing_art_changed,
                            help="Ativa o preset da guia Arte. Se desmarcado, o lote mantém o enquadramento livre/manual; preenchimentos continuam disponíveis.",
                        )

                    with st.container(border=True):
                        st.markdown("**PREENCHIMENTO DAS ÁREAS VAZIAS**")
                        st.caption("Desfoque e cor sólida são alternativas entre si e substituem o fundo personalizado.")
                        render_touch_checkbox(
                            "Aplicar desfoque", key="batch_apply_blur",
                            on_change=_batch_fill_blur_changed,
                            help="Ativa o desfoque e desativa cor sólida/fundo personalizado. Cada arte passa a exibir o preenchimento sem perder o enquadramento manual.",
                        )
                        if bool(st.session_state.get("batch_apply_blur", False)):
                            st.slider(
                                "Intensidade do desfoque",
                                0.0, 18.0, step=0.1,
                                key="batch_blur_strength",
                                help="Define a intensidade do desfoque aplicada a todas as artes marcadas.",
                            )
                            st.markdown("**Cor de base**")
                            batch_blur_picker_key = "batch_blur_base_color_picker"
                            get_artwork_integrated_color_picker_component()(
                                data={"color": str(st.session_state.get("batch_blur_base_color", BATCH_BACKGROUND_DEFAULT))},
                                on_color_change=make_batch_color_picker_callback(
                                    batch_blur_picker_key, "batch_blur_base_color"
                                ),
                                key=batch_blur_picker_key,
                            )
                        render_touch_checkbox(
                            "Aplicar cor sólida", key="batch_apply_solid_color",
                            on_change=_batch_fill_color_changed,
                            help="Aplica uma cor uniforme, desativa desfoque/fundo personalizado e ativa a área da arte quando necessário.",
                        )
                        if bool(st.session_state.get("batch_apply_solid_color", False)):
                            st.markdown("**Selecione a cor:**")
                            batch_solid_picker_key = "batch_solid_background_color_picker"
                            get_artwork_integrated_color_picker_component()(
                                data={"color": str(st.session_state.get("batch_solid_background_color", BATCH_BACKGROUND_DEFAULT))},
                                on_color_change=make_batch_color_picker_callback(
                                    batch_solid_picker_key, "batch_solid_background_color"
                                ),
                                key=batch_solid_picker_key,
                            )

                with right_col:
                    with st.container(border=True):
                        st.markdown("**FUNDO PERSONALIZADO**")
                        st.caption("O fundo personalizado substitui desfoque/cor. Cada arte passa a exibir a borda, preservando o enquadramento manual quando existente.")
                        render_touch_checkbox(
                            "Aplicar Fundo Personalizado", key="batch_apply_custom_background",
                            on_change=_batch_custom_background_checkbox_changed,
                            help="Aplica a imagem como borda e desativa desfoque/cor sólida, preservando os ajustes manuais de cada arte.",
                        )
                        batch_background_payload = None
                        batch_background_error = None
                        if bool(st.session_state.get("batch_apply_custom_background", False)):
                            upload_revision = int(st.session_state.get("batch_custom_background_upload_revision", 0) or 0)
                            upload_key = f"batch_custom_background_upload_{upload_revision}"
                            uploaded_batch_background = st.file_uploader(
                                "Escolher imagem de fundo",
                                type=["png", "jpg", "jpeg", "webp"],
                                key=upload_key,
                                help="PNG, JPG ou WEBP. Limite de 20 MB.",
                            )
                            if uploaded_batch_background is not None:
                                try:
                                    batch_background_payload = _prepare_batch_custom_background_upload(uploaded_batch_background)
                                    if batch_background_payload:
                                        preview_image = Image.open(io.BytesIO(batch_background_payload[0])).convert("RGB")
                                        st.image(preview_image, caption=batch_background_payload[2], width=190)
                                except Exception as exc:
                                    batch_background_error = str(exc)
                                    st.error(f"Não foi possível usar esta imagem: {batch_background_error}")
                            else:
                                st.info("Selecione a imagem que será aplicada às artes marcadas.")

                    with st.container(border=True):
                        st.markdown("**REMOÇÃO DE FUNDO COM IA**")
                        st.caption(
                            "Operação independente. Cada arte marcada será processada a partir do próprio arquivo original, "
                            "inclusive a arte em foco quando estiver selecionada."
                        )
                        remove_ai_clicked = st.button(
                            "Remover Fundo com IA",
                            key="batch_remove_background_ai_action",
                            type="primary",
                            use_container_width=True,
                            disabled=(selected_count < 1),
                            help="Executa a remoção U²-Net em todas as artes marcadas. A primeira execução pode demorar mais para baixar e carregar o modelo.",
                        )
                        if remove_ai_clicked:
                            progress_bar = st.progress(0, text="Preparando remoção de fundo por IA…")
                            with st.spinner("A IA está analisando e removendo o fundo das artes selecionadas…"):
                                result = _on_pool_batch_remove_background_fallback(progress_bar=progress_bar)
                            if result.get("errors"):
                                st.warning("A remoção por IA terminou com erros em algumas artes. A seleção foi mantida para conferência e nova tentativa.")
                            elif result.get("updated", 0):
                                progress_bar.progress(100, text="Remoção de fundo por IA concluída · 100%")
                                _finalize_successful_batch_operation(result, "Remoção de fundo por IA")
                            st.rerun()

                framing_mode = None
                if bool(st.session_state.get("batch_apply_framing_cut", False)):
                    framing_mode = "cut"
                elif bool(st.session_state.get("batch_apply_framing_art", False)):
                    framing_mode = "art"

                fill_mode = None
                if bool(st.session_state.get("batch_apply_blur", False)):
                    fill_mode = "blur"
                elif bool(st.session_state.get("batch_apply_solid_color", False)):
                    fill_mode = "color"

                custom_background_enabled = bool(st.session_state.get("batch_apply_custom_background", False))
                has_any_setting = bool(framing_mode or fill_mode or (custom_background_enabled and batch_background_payload))
                upload_missing = custom_background_enabled and batch_background_payload is None

                apply_clicked = st.button(
                    "Aplicar opções selecionadas",
                    key="batch_apply_independent_settings",
                    type="primary",
                    use_container_width=True,
                    disabled=(selected_count < 1 or not has_any_setting or upload_missing or bool(batch_background_error)),
                    help="Aplica somente as opções marcadas a todas as artes selecionadas no Pool, incluindo a arte em foco se estiver marcada.",
                )
                if apply_clicked:
                    settings = {
                        "framing_mode": framing_mode,
                        "fill_mode": fill_mode,
                        "blur_strength": float(st.session_state.get("batch_blur_strength", BATCH_BLUR_DEFAULT)),
                        "blur_base_color": str(st.session_state.get("batch_blur_base_color", BATCH_BACKGROUND_DEFAULT)),
                        "solid_background_color": str(st.session_state.get("batch_solid_background_color", BATCH_BACKGROUND_DEFAULT)),
                        "custom_background_payload": batch_background_payload if custom_background_enabled else None,
                    }
                    progress_bar = st.progress(0, text="Preparando aplicação dos ajustes em lote…")
                    with st.spinner("Aplicando as opções às artes selecionadas…"):
                        result = _on_pool_apply_selected_fallback(progress_bar=progress_bar, settings=settings)
                    if result.get("errors"):
                        st.warning("Algumas artes não puderam receber as configurações. A seleção foi mantida para conferência e nova tentativa.")
                    elif result.get("updated", 0):
                        progress_bar.progress(100, text="Ajustes em lote concluídos · 100%")
                        _finalize_successful_batch_operation(result, "Ajustes em lote")
                    st.rerun()


            if batch_auto_expand:
                # Com duas ou mais artes marcadas, mostrar o painel inteiro em
                # vez de depender apenas do estado visual interno do expander.
                with st.container(key="artwork_batch_expanded_panel", border=True):
                    st.markdown(
                        f'<div class="batch-panel-expanded-title">✦ AJUSTES EM LOTE '
                        f'<span>{len(batch_target_ids)} ARTES SELECIONADAS</span></div>',
                        unsafe_allow_html=True,
                    )
                    render_batch_controls()
            else:
                with st.expander("✦ AJUSTES EM LOTE", expanded=False):
                    render_batch_controls()

        # Compatibilidade com sessões Streamlit que ainda guardem rótulos antigos.
        if st.session_state.get("artwork_editor_tool_section") in {"Fundo IA", "Fundo"}:
            st.session_state["artwork_editor_tool_section"] = "Fundo & Borda"
        if st.session_state.get("artwork_editor_tool_section") == "Lote":
            st.session_state["artwork_editor_tool_section"] = "Enquadrar"
        tool_section = st.session_state.get("artwork_editor_tool_section", "Enquadrar")
        if tool_section not in {"Enquadrar", "Luz e cor", "Fundo & Borda"}:
            tool_section = "Enquadrar"
            st.session_state["artwork_editor_tool_section"] = tool_section
        st.markdown(
            """<style>
            /* Navegação em segmentos nativos: dimensões previsíveis e rótulos inteiros. */
            .st-key-artwork_editor_tool_nav,
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] {
                width:100% !important;
                max-width:100% !important;
            }
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] [role="radiogroup"],
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] [data-baseweb="button-group"] {
                display:grid !important;
                grid-template-columns:repeat(3,minmax(0,1fr)) !important;
                align-items:stretch !important;
                width:100% !important;
                gap:5px !important;
            }
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] button {
                min-width:0 !important;
                width:100% !important;
                min-height:44px !important;
                height:auto !important;
                padding:9px 7px !important;
                white-space:nowrap !important;
                overflow:visible !important;
                text-overflow:clip !important;
                justify-content:center !important;
                font-size:clamp(11px, 1.05vw, 14px) !important;
                line-height:1.2 !important;
            }
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] button > div,
            .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] button span {
                min-width:0 !important;
                white-space:nowrap !important;
                overflow:visible !important;
                text-overflow:clip !important;
                line-height:1.2 !important;
            }
            @media (max-width: 520px) {
                .st-key-artwork_editor_tool_nav [data-testid="stSegmentedControl"] button {
                    padding:8px 4px !important;
                    font-size:12px !important;
                }
            }
            </style>""",
            unsafe_allow_html=True,
        )
        with st.container(key="artwork_editor_tool_nav"):
            selected_tool_section = st.segmented_control(
                "Área de edição",
                options=("Enquadrar", "Luz e cor", "Fundo & Borda"),
                selection_mode="single",
                key="artwork_editor_tool_section",
                default=tool_section,
                width="stretch",
                label_visibility="collapsed",
                help="Escolha a categoria de ferramentas que deseja utilizar.",
            )
            tool_section = selected_tool_section or "Enquadrar"

    with st.container(key="editor_workspace"):
        tools_col, editor_col = st.columns([1.35, 1.85], gap="large")
        with tools_col:
            with st.container(key="card_left"):
                if tool_section == "Enquadrar":
                    st.markdown("**Enquadramento da arte**")
                    if selected_item is not None:
                        fit_mode = render_fit_mode_controls(prefix, int(selected_item["id"]))
                    else:
                        st.caption("Os controles de enquadramento serão habilitados quando uma arte for selecionada.")

                elif tool_section == "Luz e cor":
                    st.markdown("**Correção de luz, cor e detalhes**")
                    if selected_item is not None:
                        st.caption("A prévia permanece no mesmo enquadramento. Os ajustes também serão aplicados ao PNG e ao PDF.")
                        with st.container(border=True):
                            st.markdown("**Balanço de brancos**")
                            st.slider(
                                "Temperatura · frio ↔ quente", -100, 100, step=1,
                                key=f"{prefix}_white_balance_temperature",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "white_balance_temperature"),
                                help="Valores negativos deixam a imagem mais fria; positivos aquecem os tons.",
                            )
                            st.slider(
                                "Matiz · verde ↔ magenta", -100, 100, step=1,
                                key=f"{prefix}_white_balance_tint",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "white_balance_tint"),
                                help="Compensa dominantes esverdeadas ou magentas.",
                            )
                        with st.container(border=True):
                            st.markdown("**Iluminação**")
                            st.slider(
                                "Exposição", -2.0, 2.0, step=0.1, format="%.1f EV",
                                key=f"{prefix}_exposure",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "exposure"),
                                help="Ajuste global de luminosidade em pontos EV.",
                            )
                            st.slider(
                                "Contraste", 0, 200, step=5, format="%d%%",
                                key=f"{prefix}_contrast",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "contrast"),
                            )
                        with st.container(border=True):
                            st.markdown("**Cor e textura**")
                            st.slider(
                                "Saturação", 0, 200, step=5, format="%d%%",
                                key=f"{prefix}_saturation",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "saturation"),
                            )
                            st.slider(
                                "Matiz de cor", -180, 180, step=1, format="%d°",
                                key=f"{prefix}_hue",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "hue"),
                            )
                            st.slider(
                                "Nitidez", 0, 200, step=5, format="%d%%",
                                key=f"{prefix}_sharpness",
                                on_change=make_artwork_settings_callback(int(selected_item["id"]), "sharpness"),
                                help="Reduza para suavizar detalhes; aumente para realçar contornos.",
                            )
                        st.button(
                            "Restaurar luz, cor e textura",
                            key=f"reset_image_adjustments_{int(selected_item['id'])}",
                            use_container_width=True,
                            on_click=reset_artwork_image_adjustments,
                            args=(int(selected_item["id"]),),
                            help="Volta os ajustes de imagem aos valores originais, sem alterar o enquadramento.",
                        )
                    else:
                        st.caption("Selecione uma arte no Pool para ajustar balanço de brancos, iluminação, cor e nitidez.")

                elif tool_section == "Fundo & Borda":
                    if selected_item is not None:
                        item_id = int(selected_item["id"])
                        current_edit = selected_item.get("edit", {})
                        st.markdown("**Preenchimento das áreas vazias**")
                        cut_preset_active = bool(
                            current_edit.get("fit_cut_checked", False)
                            and not current_edit.get("manual_transform_active", False)
                            and str(current_edit.get("fit_basis_mode", "cut")) == "cut"
                        )

                        if cut_preset_active:
                            st.info(
                                "Os preenchimentos ficam indisponíveis somente enquanto o preset **Preencher até o corte** estiver ativo. "
                                "Para habilitá-los, selecione **Encaixar na área da arte**, desmarque **Preencher até o corte** "
                                "para usar o enquadramento livre ou arraste/redimensione a imagem para sair do preset."
                            )
                            background_label = "Sem preenchimento"
                        else:
                            st.caption("Escolha um único modo. Os controles específicos aparecem somente para a opção ativa.")
                            current_background_mode = str(current_edit.get("background_mode", "Cor sólida"))
                            if current_background_mode == "Sem preenchimento":
                                current_background_mode = _restore_individual_fill_after_cut(current_edit, prefix)
                            background_label = st.radio(
                                "Modo de preenchimento",
                                ["Cor sólida", "Imagem desfocada", "Fundo personalizado"],
                                key=f"{prefix}_background_mode",
                                on_change=make_artwork_background_mode_callback(item_id, prefix),
                                horizontal=False,
                                help=(
                                    "Cor sólida usa uma cor uniforme. Imagem desfocada usa a cor de base e a intensidade do desfoque. "
                                    "Fundo personalizado permite carregar uma imagem para criar a borda."
                                ),
                            )

                        if background_label == "Cor sólida":
                            with st.container(border=True):
                                st.markdown("**Selecione a cor:**")
                                color_field = "background"
                                color_key = f"{prefix}_{color_field}"
                                bg = str(st.session_state.get(color_key, current_edit.get(color_field, "#FFFFFF")))
                                color_component_key = f"artwork_color_picker_{item_id}_solid"
                                color_picker_component = get_artwork_integrated_color_picker_component()
                                color_picker_component(
                                    data={"color": bg},
                                    on_color_change=make_artwork_color_picker_callback(
                                        color_component_key, color_key, item_id, color_field
                                    ),
                                    key=color_component_key,
                                )

                        elif background_label == "Imagem desfocada":
                            with st.container(border=True):
                                st.slider(
                                    "Intensidade do desfoque", 0.0, 18.0, step=0.1,
                                    key=f"{prefix}_blur_strength",
                                    on_change=make_artwork_settings_callback(item_id, "blur_strength"),
                                    help="Aumente para suavizar mais o fundo. O valor inicial é 0,5 mm.",
                                )
                                color_field = "blur_base_color"
                                color_key = f"{prefix}_{color_field}"
                                bg = str(st.session_state.get(color_key, current_edit.get(color_field, "#FFFFFF")))
                                st.markdown("**Cor de base**")
                                color_component_key = f"artwork_color_picker_{item_id}_base"
                                color_picker_component = get_artwork_integrated_color_picker_component()
                                color_picker_component(
                                    data={"color": bg},
                                    on_color_change=make_artwork_color_picker_callback(
                                        color_component_key, color_key, item_id, color_field
                                    ),
                                    key=color_component_key,
                                )

                        elif background_label == "Fundo personalizado":
                            with st.container(border=True):
                                st.caption("A imagem ocupa a região entre a guia Arte e a guia Corte; a arte principal permanece ajustável e limitada à guia Arte.")
                                current_bg_bytes, current_bg_digest = _custom_background_payload(current_edit)
                                current_bg_name = current_edit.get("background_image_name", "Fundo carregado")
                                if current_bg_bytes and current_bg_digest:
                                    status_cols = st.columns([1, 1.7])
                                    with status_cols[0]:
                                        st.image(current_bg_bytes, caption="Fundo aplicado", use_container_width=True)
                                    with status_cols[1]:
                                        st.success("Fundo personalizado ativo")
                                        st.caption(str(current_bg_name))
                                        st.button(
                                            "Remover imagem de fundo",
                                            key=f"remove_custom_background_{item_id}",
                                            use_container_width=True,
                                            help="Remove a camada decorativa e restaura o modo de preenchimento anterior.",
                                            on_click=remove_artwork_custom_background,
                                            args=(item_id,),
                                        )
                                    uploader_label = "Substituir imagem de fundo"
                                else:
                                    st.info("Selecione uma imagem para ativar o fundo personalizado.")
                                    uploader_label = "Adicionar imagem de fundo"

                                upload_revision = int(current_edit.get("background_upload_revision", 0) or 0)
                                upload_key = f"{prefix}_custom_background_upload_{upload_revision}"
                                st.file_uploader(
                                    uploader_label,
                                    type=["png", "jpg", "jpeg", "webp"],
                                    key=upload_key,
                                    on_change=set_artwork_custom_background,
                                    args=(item_id, upload_key),
                                    help="Use uma textura, padrão ou ilustração para criar uma borda personalizada. Limite de 20 MB.",
                                )
                                bg_error = current_edit.get("background_image_error")
                                if bg_error:
                                    st.error(f"Não foi possível carregar a imagem de fundo: {bg_error}")

                        st.divider()
                        st.markdown("**Recorte automático do assunto**")
                        st.caption("A remoção por IA atua somente na arte principal. A imagem de borda e o preenchimento são mantidos; a IA não é executada em outras artes, a menos que seja marcada separadamente em Ajustes em Lote.")
                        remove_bg_active = bool(current_edit.get("remove_background", False))
                        processing_error = selected_item.get("image_processing_error")
                        st.button(
                            "Tentar remoção novamente" if remove_bg_active and processing_error else "Remover fundo com IA",
                            key=f"remove_bg_{item_id}",
                            type="primary",
                            use_container_width=True,
                            disabled=remove_bg_active and not bool(processing_error),
                            on_click=set_artwork_background_removal,
                            args=(item_id, True),
                            help="Detecta o assunto principal e remove o fundo da imagem original.",
                        )
                        st.button(
                            "Restaurar fundo original da arte",
                            key=f"restore_bg_{item_id}",
                            use_container_width=True,
                            disabled=not remove_bg_active,
                            on_click=set_artwork_background_removal,
                            args=(item_id, False),
                            help="Desativa a remoção por IA sem apagar a imagem de borda personalizada.",
                        )
                        if remove_bg_active:
                            st.success("Remoção de fundo da arte principal ativa.")
                        if processing_error:
                            st.error(
                                "Não foi possível processar esta imagem. Confira a instalação do removedor de fundos "
                                "e a conexão necessária para baixar o modelo na primeira utilização. " + str(processing_error)
                            )
                    else:
                        st.caption("Selecione uma arte no Pool para configurar o preenchimento, a borda personalizada e a remoção de fundo por IA.")

        if selected_item is not None:
            selected_digest = selected_item["image_digest"]
            # Apenas metadados são lidos em trocas/ajustes; a imagem de produção em
            # resolução total só é decodificada quando o usuário prepara o PNG/PDF.
            selected_dimensions = get_image_dimensions(selected_digest, selected_item["image"])
            # O estado é individual por item; nenhuma arte herda o zoom/posição da anterior.
            zoom = float(st.session_state[f"{prefix}_zoom"])
            off_x = float(st.session_state[f"{prefix}_offset_x"])
            off_y = float(st.session_state[f"{prefix}_offset_y"])
            blur_mm = float(st.session_state[f"{prefix}_blur_strength"])
            persist_artwork_edit_state(selected_item, prefix)

        with editor_col:
            with st.container(key="card_preview"):
                st.markdown(
                    """<style>
                    .st-key-preview_nav_previous button,
                    .st-key-preview_nav_next button {
                        min-height:46px !important;
                        height:auto !important;
                        border-radius:12px !important;
                        padding:10px 8px !important;
                        font-size:clamp(11px, .95vw, 14px) !important;
                        font-weight:850 !important;
                        letter-spacing:.025em !important;
                        white-space:nowrap !important;
                        border:1px solid rgba(255,255,255,.32) !important;
                        box-shadow:0 4px 14px rgba(0,0,0,.20) !important;
                        transition:filter .15s ease, transform .15s ease !important;
                    }
                    .st-key-preview_nav_previous button {
                        background:linear-gradient(135deg,#159CCB,#0878A7) !important;
                        color:#FFFFFF !important;
                    }
                    .st-key-preview_nav_next button {
                        background:linear-gradient(135deg,#ED1B78,#B70F59) !important;
                        color:#FFFFFF !important;
                    }
                    .st-key-preview_nav_previous button:hover:not(:disabled),
                    .st-key-preview_nav_next button:hover:not(:disabled) {
                        filter:brightness(1.13) saturate(1.08) !important;
                        transform:translateY(-1px) !important;
                    }
                    .st-key-preview_nav_previous button:disabled,
                    .st-key-preview_nav_next button:disabled {
                        opacity:.42 !important;
                        filter:grayscale(.45) !important;
                    }
                    @media(max-width:700px) {
                        .st-key-preview_nav_previous button,
                        .st-key-preview_nav_next button {
                            font-size:10px !important;
                            padding:8px 4px !important;
                        }
                    }
                    </style>""",
                    unsafe_allow_html=True,
                )
                preview_items = st.session_state.get("artwork_items", [])
                preview_ids = [int(item.get("id", -1)) for item in preview_items]
                try:
                    preview_index = preview_ids.index(int(selected_item["id"])) if selected_item is not None else -1
                except (ValueError, TypeError):
                    preview_index = -1
                previous_disabled = preview_index <= 0
                next_disabled = preview_index < 0 or preview_index >= len(preview_items) - 1
                preview_heading_cols = st.columns([5.0, 2.5, 2.5], gap="small", vertical_alignment="center")
                with preview_heading_cols[0]:
                    brand.section_title(None, "PRÉVIA DE AJUSTE", brand.MAGENTA)
                    if preview_index >= 0:
                        st.caption(f"Arte {preview_index + 1} de {len(preview_items)} · navegue pelo Pool")
                with preview_heading_cols[1]:
                    with st.container(key="preview_nav_previous"):
                        st.button(
                            "◀  ANTERIOR",
                            key="editor_preview_previous_artwork",
                            help="Editar a arte anterior na ordem do Pool",
                            disabled=previous_disabled,
                            use_container_width=True,
                            on_click=_navigate_editor_artwork,
                            args=(-1,),
                        )
                with preview_heading_cols[2]:
                    with st.container(key="preview_nav_next"):
                        st.button(
                            "PRÓXIMA  ▶",
                            key="editor_preview_next_artwork",
                            help="Editar a próxima arte na ordem do Pool",
                            disabled=next_disabled,
                            use_container_width=True,
                            on_click=_navigate_editor_artwork,
                            args=(1,),
                        )
                if selected_item is not None and selected_dimensions is not None:
                    token = image_editor_token(
                        selected_item["image"], spec, int(selected_item["id"]), selected_digest
                    )
                    component_key = f"main_visual_image_editor_{int(selected_item['id'])}"
                    token_state_key = f"_{prefix}_expected_token"
                    st.session_state[token_state_key] = token
                    editor_transform = current_editor_transform(component_key, token, prefix)
                    editor = get_image_editor_component()
                    editor_result = editor(
                        data={
                            "token": token,
                            "image": editor_image_data_url(selected_digest, selected_item["image"]),
                            # O backdrop é caro e desnecessário no modo de cor sólida.
                            "backdrop_image": (
                                editor_backdrop_data_url(selected_digest, selected_item["image"], bg)
                                if background_mode == "blur" else ""
                            ),
                            "custom_background_image": (
                                editor_image_data_url(custom_background_digest, custom_background_bytes)
                                if has_custom_background else ""
                            ),
                            "custom_background_revision": int(saved_edit.get("custom_background_revision", 0) or 0),
                            "shape": spec.shape,
                            "size": spec.size,
                            "cut": spec.cut,
                            "fold": spec.fold,
                            "art": spec.art,
                            "safe": spec.safe,
                            "guides": [g.key for g in GUIDES],
                            "fit_mode": fit_mode,
                            # Os dois presets restauram a escala-base exata do modo ativo.
                            # A conversão entre bottons continua sendo calculada pela Segurança.
                            "initial_zoom": 1.0,
                            "background_mode": background_mode,
                            "blur_mm": float(blur_mm),
                            "bg": bg,
                            "transform": editor_transform,
                            "transform_revision": int(editor_transform.get("revision", 0)),
                        },
                        default={"transform": editor_transform},
                        on_transform_change=make_image_editor_sync_callback(
                            component_key, prefix, token_state_key,
                            f"{prefix}_fit_cut_checked", f"{prefix}_fit_art_checked",
                            int(selected_item["id"]),
                        ),
                        key=component_key,
                    )
                    apply_image_editor_state(editor_result, token, prefix)
                    zoom = float(st.session_state[f"{prefix}_zoom"])
                    off_x = float(st.session_state[f"{prefix}_offset_x"])
                    off_y = float(st.session_state[f"{prefix}_offset_y"])
                    persist_artwork_edit_state(selected_item, prefix)

                    if fit_mode == "cut":
                        box = image_box(selected_dimensions[0], selected_dimensions[1], spec.cut, zoom, off_x, off_y)
                        dpi = min(selected_dimensions) / ((spec.cut * zoom) / 25.4)
                    else:
                        dpi = max(selected_dimensions) / ((spec.art * zoom) / 25.4)
                    if dpi < 200:
                        st.warning(f"Resolução efetiva ≈ {dpi:.0f} dpi — a impressão pode ficar pixelada (ideal: 300 dpi).")
                    else:
                        st.caption(f"Resolução efetiva da arte selecionada ≈ {dpi:.0f} dpi")
                else:
                    st.info("Selecione uma miniatura no pool para abrir a arte no editor com as guias.")

                render_guide_legend()

                # Exportação PNG sob demanda: não renderiza centenas de milhares de
                # pixels durante cada arraste, clique ou atualização da prévia.
                if selected_item is not None and selected_dimensions is not None:
                    st.divider()
                    brand.section_title(None, "EXPORTAR ARTE EM FOCO", brand.YELLOW)
                    export_cols = st.columns([1, 2])
                    with export_cols[0]:
                        dpi_label = st.selectbox(
                            "Resolução do PNG",
                            ["300 dpi · alta qualidade", "600 dpi · máxima definição"],
                            key="focused_art_png_dpi",
                            filter_mode=None,
                            accept_new_options=False,
                        )
                        dpi_export = 600 if dpi_label.startswith("600") else 300
                    png_signature_data = (
                        int(selected_item["id"]), selected_item["image_digest"],
                        custom_background_digest,
                        spec.shape, spec.size, spec.cut, spec.art, dpi_export,
                        round(float(zoom), 6), round(float(off_x), 5), round(float(off_y), 5),
                        bg, fit_mode, background_mode, round(float(blur_mm), 4),
                    )
                    png_signature = hashlib.sha256(repr(png_signature_data).encode("utf-8")).hexdigest()
                    with export_cols[0]:
                        prepare_png = st.button(
                            "Preparar PNG",
                            key=f"prepare_focused_art_png_{int(selected_item['id'])}",
                            type="primary",
                            use_container_width=True,
                        )
                    if prepare_png:
                        with st.spinner("Preparando PNG da arte selecionada em resolução original…"):
                            production_image = load_image(
                                selected_item["image"], bg, image_digest=selected_digest
                            )
                            production_background = (
                                load_image(
                                    custom_background_bytes,
                                    image_digest=custom_background_digest,
                                )
                                if has_custom_background else None
                            )
                            clean_png, png_size = build_button_art_png(
                                spec, production_image, dpi=dpi_export, zoom=zoom,
                                off_x=off_x, off_y=off_y, bg=bg, fit_mode=fit_mode,
                                background_mode=background_mode, blur_mm=float(blur_mm),
                                background_image=production_background,
                            )
                        shape_label = "quadrado" if spec.shape == "square" else "redondo"
                        size_label = f"{spec.size:g}x{spec.size:g}mm" if spec.shape == "square" else f"{spec.size:g}mm"
                        st.session_state["focused_art_png_export"] = {
                            "signature": png_signature,
                            "bytes": clean_png,
                            "size": png_size,
                            "name": f"arte_botton_{shape_label}_{size_label}_{dpi_export}dpi.png",
                            "dpi": dpi_export,
                        }
                    cached_png = st.session_state.get("focused_art_png_export")
                    with export_cols[1]:
                        if cached_png and cached_png.get("signature") == png_signature:
                            transparency_label = "círculo com transparência externa" if spec.shape == "circle" else "quadrado opaco"
                            st.caption(
                                f"PNG {transparency_label}, sem guias · {cached_png['size'][0]} × {cached_png['size'][1]} px · "
                                f"{cached_png['dpi']} DPI · corte de {spec.cut:g} mm"
                            )
                            st.download_button(
                                "⬇️ Baixar PNG da arte selecionada",
                                data=cached_png["bytes"],
                                file_name=cached_png["name"],
                                mime="image/png",
                                type="primary",
                                use_container_width=True,
                                key=f"download_focused_art_png_{int(selected_item['id'])}",
                            )
                        else:
                            st.caption("Prepare o PNG somente quando a arte estiver pronta. A prévia continua leve durante a edição.")

    # ---------------- Folha de impressão e exportação ----------------
    with st.container(key="card_right"):
        brand.section_title(None, "FOLHA DE IMPRESSÃO", brand.MAGENTA)
        with st.container(border=True, key="sheet_settings_panel"):
            st.markdown('<div class="sheet-config-title">CONFIGURAÇÃO DA FOLHA</div>', unsafe_allow_html=True)
            st.caption("Margem e distância entre bottons continuam configuráveis e são lembradas separadamente para cada combinação de papel e tamanho.")
            settings_cols = st.columns([1.15, 1.0, 1.0, 1.0])
            with settings_cols[0]:
                paper = st.selectbox("Papel", [*PAPERS, "Personalizado"], key="paper_size", filter_mode=None, accept_new_options=False)
            with settings_cols[1]:
                st.selectbox("Orientação", ["Retrato", "Paisagem"], key="page_orientation", filter_mode=None, accept_new_options=False)
            with settings_cols[2]:
                st.number_input(
                    "Margem da folha (mm)", 0.0, 50.0, step=0.5, format="%.1f", key="margin_mm",
                    help="Margem externa entre os bottons e a borda da folha. O valor é salvo por papel e tamanho.",
                )
            with settings_cols[3]:
                st.number_input(
                    "Espaço entre bottons (mm)", 0.0, 20.0, step=0.5, format="%.1f", key="gap_mm",
                    help="Distância mínima entre os limites externos de corte dos bottons. O valor é salvo por papel e tamanho.",
                )
            if paper == "Personalizado":
                custom_cols = st.columns(2)
                custom_cols[0].number_input(
                    "Largura da folha (mm)", 60.0, 1500.0, step=1.0, key="custom_page_w"
                )
                custom_cols[1].number_input(
                    "Altura da folha (mm)", 60.0, 1500.0, step=1.0, key="custom_page_h"
                )
            action_cols = st.columns([1.2, 2.8])
            with action_cols[0]:
                st.button(
                    "Restaurar valores recomendados",
                    key="restore_recommended_layout_button",
                    use_container_width=True,
                    on_click=restore_recommended_layout,
                )
            with action_cols[1]:
                st.caption(
                    f"Sugestão automática para este perfil: margem {recommended_margin:g} mm · "
                    f"distância {recommended_gap:g} mm. As alterações manuais são preservadas ao trocar de tamanho."
                )
            with st.expander("Guias impressas", expanded=False):
                for guide in GUIDES:
                    render_touch_checkbox(
                        f"{guide.label} ({getattr(spec, guide.attr):g} mm)",
                        key=f"guide_single_{guide.key}",
                    )

        project_shape_name = "quadrado" if spec.shape == "square" else "redondo"
        project_size_name = f"{spec.size:g}x{spec.size:g}mm" if spec.shape == "square" else f"{spec.size:g}mm"
        project_name = f"atomica_projeto_{project_shape_name}_{project_size_name}_{len(st.session_state.get('artwork_items', []))}artes.atp"
        project_settings = {
            "size_key": size_key,
            "shape": spec.shape,
            "cut_diameter": float(spec.cut),
            "cut_diameter_overrides": {
                str(key): float(value)
                for key, value in st.session_state.get("_cut_diameter_overrides", {}).items()
                if key in BUTTONS
            },
            "paper_size": paper,
            "page_orientation": orient,
            "custom_page_w": float(st.session_state.get("custom_page_w", 210.0)),
            "custom_page_h": float(st.session_state.get("custom_page_h", 297.0)),
            "page_w": float(page_w),
            "page_h": float(page_h),
            "margin_mm": float(margin),
            "gap_mm": float(gap),
            "layout_label": str(layout_label),
            "guides": list(guides),
            "selected_id": st.session_state.get("artwork_selected_id"),
        }

        with st.container(border=True):
            brand.section_title(None, "SALVAR PROJETO", brand.CYAN)
            st.caption(
                "Salve um arquivo .atp para continuar editando depois. Ele preserva o Pool, "
                "as imagens originais, os fundos e as configurações, sem gerar uma folha PDF."
            )
            if st.button(
                "Preparar arquivo de projeto (.atp)",
                disabled=not bool(st.session_state.get("artwork_items")),
                key="prepare_atomica_atp_project",
                use_container_width=True,
            ):
                selected_for_save = st.session_state.get("artwork_selected_id")
                if selected_for_save is not None:
                    _commit_artwork_editor_state(int(selected_for_save))
                try:
                    with st.spinner("Compactando o projeto e organizando os arquivos…"):
                        atp_bytes = build_atomica_project_archive(
                            st.session_state["artwork_items"], project_settings
                        )
                    st.session_state["_atp_project_export"] = {
                        "bytes": atp_bytes,
                        "name": project_name,
                        "signature": atomica_project_export_signature(
                            st.session_state["artwork_items"], project_settings
                        ),
                    }
                    st.toast("Arquivo de projeto preparado. Agora você pode baixá-lo.", icon="✅")
                except Exception as exc:
                    st.toast(str(exc) or "Não foi possível preparar o arquivo .atp.", icon="⚠️")

            atp_signature = atomica_project_export_signature(
                st.session_state.get("artwork_items", []), project_settings
            )
            stored_atp = st.session_state.get("_atp_project_export")
            if stored_atp and stored_atp.get("signature") == atp_signature:
                st.download_button(
                    f"⬇️ Baixar projeto .atp ({len(stored_atp['bytes']) / 1e6:.1f} MB)",
                    stored_atp["bytes"],
                    file_name=stored_atp["name"],
                    mime="application/octet-stream",
                    type="primary",
                    key="download_atomica_atp_project",
                    use_container_width=True,
                )
            elif stored_atp:
                st.info("O projeto mudou desde o último arquivo .atp. Prepare um novo arquivo para salvar as alterações.")

        with st.container(border=True, key="sheet_canvas_panel"):
            brand.section_title(None, "PRÉVIA DA FOLHA", brand.MAGENTA)
            positions = compute_layout(page_w, page_h, spec.cut, margin, gap, mode)
            per_page = len(positions)
            if per_page == 0:
                st.error("Os bottons não cabem na folha com essas margens.")
            else:
                grid_n = len(compute_layout(page_w, page_h, spec.cut, margin, gap, "grid"))
                hex_n = len(compute_layout(page_w, page_h, spec.cut, margin, gap, "hex")) if spec.shape == "circle" else 0

                # A capacidade substitui o seletor antigo de disposição:
                # os próprios rótulos passam a ser as opções funcionais.
                grid_choice = f"Grade: {grid_n} por folha"
                hex_choice = f"Hexagonal: {hex_n} por folha"
                current_layout_label = str(st.session_state.get("layout_label", "Grade"))
                selected_capacity_choice = (
                    hex_choice if current_layout_label.startswith("Hexagonal") else grid_choice
                )
                capacity_options = [grid_choice, hex_choice] if spec.shape == "circle" else [grid_choice]
                if st.session_state.get("layout_capacity_choice") != selected_capacity_choice:
                    st.session_state["layout_capacity_choice"] = selected_capacity_choice
                if spec.shape == "circle":
                    st.radio(
                        "Capacidade por disposição",
                        capacity_options,
                        horizontal=True,
                        key="layout_capacity_choice",
                        on_change=_sync_layout_from_capacity_choice,
                        label_visibility="collapsed",
                    )
                else:
                    st.caption(grid_choice)

                metrics = st.columns(3)
                metrics[0].metric("Bottons por folha", per_page)
                metrics[1].metric("Folha", f"{page_w:g} × {page_h:g} mm")
                metrics[2].metric("Artes diferentes", len(st.session_state["artwork_items"]))

                runtime_groups = []
                for entry in st.session_state["artwork_items"]:
                    ensure_artwork_edit_state(entry, spec, sync_session=False)
                    edit = entry.get("edit", {})
                    item_background_label = str(edit.get("background_mode", "Cor sólida"))
                    item_background_mode = "blur" if item_background_label == "Imagem desfocada" else "color"
                    if item_background_label == "Sem preenchimento":
                        item_bg = "#FFFFFF"
                    else:
                        item_bg_field = "blur_base_color" if item_background_mode == "blur" else "background"
                        item_bg = str(edit.get(item_bg_field, edit.get("background", "#FFFFFF")))
                    item_background_bytes, item_background_digest = _custom_background_payload(edit)
                    runtime_groups.append({
                        "id": entry["id"],
                        "qty": int(entry.get("qty", 1)),
                        "spec": spec,
                        "image_digest": entry["image_digest"],
                        "image_bytes": entry["image"],
                        "zoom": float(edit.get("zoom", 1.0)),
                        "off_x": float(edit.get("offset_x", 0.0)),
                        "off_y": float(edit.get("offset_y", 0.0)),
                        "fit_mode": str(edit.get("fit_basis_mode", "cut")),
                        "background_mode": item_background_mode,
                        "blur_mm": float(edit.get("blur_strength", 0.5)),
                        "bg": item_bg,
                        "background_image_digest": item_background_digest,
                        "background_image_bytes": item_background_bytes,
                    })

                total_qty = sum(entry["qty"] for entry in runtime_groups)
                if runtime_groups:
                    n_pages = math.ceil(total_qty / per_page)
                    page_key = "_sheet_preview_page_selector"
                    current_page = int(st.session_state.get(page_key, 1) or 1)
                    current_page = max(1, min(n_pages, current_page))
                    st.session_state[page_key] = current_page

                    if n_pages > 1:
                        page_nav = st.columns([1.1, 1.6, 1.1], gap="small")
                        with page_nav[0]:
                            st.button(
                                "← Página anterior",
                                key="sheet_preview_page_previous",
                                use_container_width=True,
                                disabled=current_page <= 1,
                                on_click=_step_sheet_preview_page,
                                args=(-1, n_pages),
                            )
                        with page_nav[1]:
                            st.selectbox(
                                "Página da prévia",
                                options=list(range(1, n_pages + 1)),
                                format_func=lambda value: f"Página {value} de {n_pages}",
                                key=page_key,
                                label_visibility="collapsed",
                                filter_mode=None,
                                accept_new_options=False,
                            )
                        with page_nav[2]:
                            st.button(
                                "Próxima página →",
                                key="sheet_preview_page_next",
                                use_container_width=True,
                                disabled=current_page >= n_pages,
                                on_click=_step_sheet_preview_page,
                                args=(1, n_pages),
                            )
                    else:
                        st.caption("Página 1 de 1")

                    current_page = int(st.session_state.get(page_key, 1) or 1)
                    current_page_items = _get_items_for_sheet_page(runtime_groups, current_page, per_page)
                    preview_placements_tuple = tuple(
                        (index, position[0], position[1])
                        for index, position in enumerate(positions[:len(current_page_items)])
                    )
                    preview_payload = tuple(
                        (
                            entry["image_digest"], entry["image_bytes"],
                            entry["zoom"], entry["off_x"], entry["off_y"],
                            entry["fit_mode"], entry["background_mode"], entry["blur_mm"], entry["bg"],
                            entry["background_image_digest"], entry["background_image_bytes"],
                        )
                        for entry in current_page_items
                    )
                    sheet_preview_image = render_mixed_sheet_preview_cached(
                        preview_payload, preview_placements_tuple, spec,
                        page_w, page_h, bg, tuple(guides),
                        footer_margin_mm=float(margin),
                    )
                    st.image(
                        sheet_preview_image,
                        caption=(
                            f"Página {current_page} de {n_pages} · artes repetidas conforme as quantidades "
                            "e a ordem das miniaturas do Pool"
                        ),
                        width=SHEET_PREVIEW_WIDTH_PX,
                    )

                    item_signature = tuple(
                        (
                            entry["id"], entry["image_digest"], entry["background_image_digest"], entry["qty"],
                            entry["zoom"], entry["off_x"], entry["off_y"],
                            entry["fit_mode"], entry["background_mode"], entry["blur_mm"], entry["bg"],
                        )
                        for entry in runtime_groups
                    )
                    sig = (
                        "art_batch", item_signature, size_key, page_w, page_h,
                        margin, gap, mode, guides,
                    )

                    # A visualização em tamanho real acompanha a página selecionada.
                    real_size_signature = hashlib.sha256(
                        repr((sig, "real-size-viewer-v2", page_w, page_h, current_page)).encode("utf-8")
                    ).hexdigest()
                    real_size_cache = st.session_state.get("_sheet_real_size_preview_cache")
                    if not real_size_cache or real_size_cache.get("signature") != real_size_signature:
                        # Renderizar a origem em 150 dpi e exibi-la em 96 CSS px/pol.
                        # A resolução extra reduz a quantização das bordas circulares,
                        # sem alterar a dimensão física mostrada no monitor.
                        real_size_ppm = min(150.0 / 25.4, 5000.0 / max(float(page_w), float(page_h), 1.0))
                        real_size_image = render_mixed_sheet_preview_cached(
                            preview_payload, preview_placements_tuple, spec,
                            page_w, page_h, bg, tuple(guides),
                            preview_ppm=real_size_ppm,
                            footer_margin_mm=float(margin),
                        )
                        real_size_buffer = io.BytesIO()
                        real_size_image.save(real_size_buffer, format="WEBP", quality=92, method=1)
                        real_size_data_url = (
                            "data:image/webp;base64,"
                            + base64.b64encode(real_size_buffer.getvalue()).decode("ascii")
                        )
                        st.session_state["_sheet_real_size_preview_cache"] = {
                            "signature": real_size_signature,
                            "image_data_url": real_size_data_url,
                        }
                        real_size_cache = st.session_state["_sheet_real_size_preview_cache"]

                    real_size_viewer = get_sheet_real_size_viewer_component()
                    real_size_viewer(
                        data={
                            "signature": real_size_cache["signature"],
                            "image_data_url": real_size_cache["image_data_url"],
                            "page_w_mm": float(page_w),
                            "page_h_mm": float(page_h),
                            "page_label": f"{paper} · página {current_page}/{n_pages}",
                            "button_size_mm": float(spec.size),
                            "cut_mm": float(spec.cut),
                        },
                        key="sheet_real_size_viewer",
                        width="stretch",
                    )
                    st.caption(
                        "A visualização em tamanho real usa a referência de 96 px/pol. "
                        "Na tela cheia, calibre a régua de 100 mm com uma régua física. Use os botões + e − para correções finas. "
                        "A calibração fica salva neste navegador e pode ser refeita ao trocar de monitor ou zoom."
                    )

                    shape_name = "quadrado" if spec.shape == "square" else "redondo"
                    size_name = f"{spec.size:g}x{spec.size:g}mm" if spec.shape == "square" else f"{spec.size:g}mm"
                    fname = f"bottons_{shape_name}_{size_name}_{total_qty}un.pdf"

                    if st.button("Gerar PDF", type="primary", disabled=(total_qty <= 0), key="generate_art_batch_pdf"):
                        with st.spinner("Gerando PDF com todas as artes do lote…"):
                            pdf_items = []
                            for entry in runtime_groups:
                                pdf_entry = dict(entry)
                                pdf_entry["img"] = load_image(
                                    entry["image_bytes"], image_digest=entry["image_digest"]
                                )
                                pdf_entry["background_image"] = (
                                    load_image(
                                        entry["background_image_bytes"],
                                        image_digest=entry["background_image_digest"],
                                    )
                                    if entry["background_image_bytes"] and entry["background_image_digest"] else None
                                )
                                pdf_items.append(pdf_entry)
                            # PDF convencional: somente conteúdo de impressão, sem anexar
                            # imagens originais nem manifesto. O projeto editável é salvo à parte em .atp.
                            pdf = build_art_batch_pdf(
                                pdf_items, positions, page_w, page_h, total_qty,
                                bg=bg, guides=guides,
                                page_margin_mm=float(margin),
                            )
                        pdf_base64 = base64.b64encode(pdf).decode("ascii")
                        st.session_state["pdf"] = {
                            "sig": sig, "bytes": pdf, "base64": pdf_base64, "name": fname
                        }

                    stored = st.session_state.get("pdf")
                    if stored and stored["sig"] == sig:
                        st.download_button(
                            f"⬇️ Baixar PDF ({len(stored['bytes']) / 1e6:.1f} MB)",
                            stored["bytes"], file_name=stored["name"], mime="application/pdf", type="primary",
                            key="download_art_batch_pdf",
                        )
                        st.caption("Este PDF é destinado à impressão. Para guardar e reabrir o Pool e as configurações, salve também o arquivo de projeto .atp acima.")
                        pdf_print = get_pdf_print_component()
                        pdf_print(
                            data={
                                "pdf_base64": stored.get("base64") or base64.b64encode(stored["bytes"]).decode("ascii"),
                                "signature": hashlib.sha256(repr(stored["sig"]).encode("utf-8")).hexdigest(),
                            },
                            key="pdf_print_direct",
                            width="stretch",
                        )
                        with st.container(border=True):
                            st.markdown("**Impressão em escala real**")
                            st.caption(
                                "O botão Imprimir PDF solicita diretamente o diálogo de impressão do navegador, sem abrir prévia ou nova aba. "
                                "Para manter a escala física 1:1, escolha Tamanho real/100% e desative Ajustar à página."
                            )
                    elif stored:
                        st.info("As artes, quantidades ou configurações mudaram desde o último PDF — gere o arquivo novamente.")
                else:
                    st.info("Adicione as artes para montar a folha de impressão.")

    brand.render_footer()


if __name__ == "__main__":
    main()
