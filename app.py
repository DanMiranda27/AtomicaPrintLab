"""
Atômica Print Lab · Criar Bottons (Streamlit) v3.16

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
import math
from dataclasses import dataclass
from typing import Literal

import streamlit as st
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageOps

from reportlab.lib.colors import HexColor, black
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
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
    "35 mm": ButtonSpec(size=35, cut=45, fold=41, art=35, safe=32),
    "37 mm": ButtonSpec(size=37, cut=46, fold=41, art=37, safe=34),
    "38 mm": ButtonSpec(size=38, cut=48, fold=44, art=38, safe=34),
    "44 mm": ButtonSpec(size=44, cut=54, fold=49, art=44, safe=40),
    "45 mm": ButtonSpec(size=45, cut=55, fold=51, art=45, safe=41),
    "55 mm": ButtonSpec(size=55, cut=69, fold=61, art=55, safe=49),
    "50 × 50 mm (quadrado)": ButtonSpec(
        size=50, cut=61, fold=58, art=50, safe=47, shape="square"
    ),
    "58 mm": ButtonSpec(size=58, cut=68, fold=64, art=58, safe=54),
    "65 mm": ButtonSpec(size=65, cut=75, fold=71, art=65, safe=61),
    "88 mm": ButtonSpec(size=88, cut=98, fold=94, art=88, safe=84),
}.items(), key=lambda item: item[1].size))

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


def compute_layout(page_w: float, page_h: float, d: float, margin: float, gap: float, mode: str):
    """
    Centros (x, y) em mm, com origem no canto SUPERIOR esquerdo da folha,
    ordenados da esquerda para a direita e de cima para baixo.
    mode: "grid" (alinhado) ou "hex" (colunas alternadas, mais compacto).
    """
    pitch = d + gap
    usable_w, usable_h = page_w - 2 * margin, page_h - 2 * margin
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
    dy = (page_h - (max_y - min_y)) / 2 - min_y
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
) -> Image.Image:
    """Gera a arte sem guias; o fundo desfocado cobre toda a área de corte."""
    pixels = max(1, round(spec.cut * ppm))
    center = pixels / 2
    background_mode = background_mode or ("blur" if fit_mode == "art" and blur_fill else "color")
    if background_mode not in {"color", "blur"}:
        background_mode = "color"

    base = Image.new("RGB", (pixels, pixels), bg)
    if img is not None:
        source = img.convert("RGBA")

        if background_mode == "blur":
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
            base = backdrop.filter(
                ImageFilter.GaussianBlur(radius=max(0.0, float(blur_mm) * ppm))
            )

        # A arte principal usa exatamente o zoom e os deslocamentos definidos
        # no editor. O fundo desfocado permanece fixo e preenche qualquer espaço
        # que a arte não ocupar dentro da guia de corte.
        if fit_mode == "art":
            sharp = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
            _paste_box(
                sharp,
                source,
                fit_image_box(source.width, source.height, spec.art, zoom, off_x, off_y),
                ppm,
                center,
            )
            art_mask = _shape_mask(spec.shape, (pixels, pixels), spec.art * ppm)
            sharp.putalpha(ImageChops.multiply(sharp.getchannel("A"), art_mask))
            foreground = sharp
        else:
            foreground = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
            _paste_box(
                foreground,
                source,
                image_box(source.width, source.height, spec.cut, zoom, off_x, off_y),
                ppm,
                center,
            )

        composed = base.convert("RGBA")
        composed.alpha_composite(foreground)
        base = composed.convert("RGB")

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
        background_mode=background_mode,
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
    )
    for x, y in positions:
        sheet.paste(btn, (round(x * ppm - btn.width / 2), round(y * ppm - btn.height / 2)), btn)
    return sheet


def render_mixed_sheet_preview(
    items, placements, page_w, page_h, *, zoom=1.0, off_x=0.0, off_y=0.0,
    bg="#FFFFFF", guides=tuple(g.key for g in GUIDES), fit_mode="cut",
    blur_fill=True, blur_mm=0.5, background_mode=None,
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
        )
        sheet.paste(
            button,
            (round(cx * ppm - button.width / 2), round(cy * ppm - button.height / 2)),
            button,
        )
    return sheet


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
) -> bytes:
    # A composição é rasterizada em alta resolução; as linhas permanecem vetoriais.
    artwork = render_artwork(
        spec, img, ppm=PDF_MAX_DPI / 25.4, zoom=zoom, off_x=off_x, off_y=off_y,
        bg=bg, fit_mode=fit_mode, blur_fill=blur_fill, blur_mm=blur_mm,
        background_mode=background_mode,
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
    items, positions, page_w, page_h, total_qty, *, bg, guides,
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

    image_buffers = []
    expanded_order = []
    for index, item in enumerate(items):
        artwork = render_artwork(
            item["spec"], item["img"], ppm=PDF_MAX_DPI / 25.4,
            zoom=item["zoom"], off_x=item["off_x"], off_y=item["off_y"],
            bg=item.get("bg", bg), fit_mode=item["fit_mode"],
            blur_fill=item.get("blur_fill", True), blur_mm=item.get("blur_mm", 0.5),
            background_mode=item.get("background_mode", "color"),
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
        if page_index < page_count - 1:
            c.showPage()

    c.save()
    return buf.getvalue()


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


def apply_batch_artwork_settings(item_ids: list[int], spec: ButtonSpec, source_item_id: int | None = None) -> int:
    """Aplica os controles existentes da arte em foco às artes selecionadas no Pool.

    Só copia os presets de enquadramento se um deles estiver marcado. Não copia
    deslocamentos/zoom manuais: cada arte continua com seu próprio posicionamento.
    Modo/cor/intensidade do preenchimento são copiados do painel já existente.
    """
    targets = {int(value) for value in item_ids}
    if not targets:
        return 0

    source_id = source_item_id if source_item_id is not None else st.session_state.get("artwork_selected_id")
    try:
        source_id = int(source_id)
    except (TypeError, ValueError):
        return 0
    source = _find_artwork_item(source_id)
    if source is None:
        return 0
    _commit_artwork_editor_state(source_id)
    # The General Adjustments panel is the single source for batch settings.
    # Persist its current widget values before reading the selected artwork's
    # canonical edit record; this also covers a click immediately after a change.
    _persist_artwork_fields(
        source_id,
        ("fit_basis_mode", "fit_cut_checked", "fit_art_checked",
         "background_mode", "background", "blur_strength"),
    )
    source_edit = source.setdefault("edit", {})

    source_cut_checked = bool(source_edit.get("fit_cut_checked"))
    source_art_checked = bool(source_edit.get("fit_art_checked"))
    fit_mode = None
    if source_cut_checked and not source_art_checked:
        fit_mode = "cut"
    elif source_art_checked and not source_cut_checked:
        fit_mode = "art"
    source_basis_mode = str(source_edit.get("fit_basis_mode", "cut")).lower()
    if source_basis_mode not in {"cut", "art"}:
        source_basis_mode = fit_mode or "cut"

    background_mode = (
        "blur" if str(source_edit.get("background_mode", "Cor sólida")) == "Imagem desfocada"
        else "color"
    )
    background = str(source_edit.get("background", "#FFFFFF")).upper()
    try:
        ImageColor.getrgb(background)
    except ValueError:
        background = "#FFFFFF"
    try:
        blur_strength = max(0.0, min(18.0, float(source_edit.get("blur_strength", 0.5))))
    except (TypeError, ValueError):
        blur_strength = 0.5

    profile = _button_scale_profile(spec)
    updated = 0
    for item in st.session_state.get("artwork_items", []):
        item_id = int(item.get("id", -1))
        if item_id not in targets:
            continue
        edit = item.setdefault("edit", {})
        edit["fit_basis_mode"] = source_basis_mode
        edit["fit_cut_checked"] = fit_mode == "cut"
        edit["fit_art_checked"] = fit_mode == "art"
        if fit_mode in {"cut", "art"}:
            # Ao aplicar um preset, todas as selecionadas recebem o mesmo
            # enquadramento-base, mas continuam com IDs/estados independentes.
            edit["zoom"] = 1.0
            edit["offset_x"] = 0.0
            edit["offset_y"] = 0.0
            edit["transform_revision"] = int(edit.get("transform_revision", 0) or 0) + 1
            edit["scale_reference_spec"] = profile
            edit["scale_reference_diameter"] = float(spec.safe)
            edit["initial_cut_fit_schema"] = 4
            edit["manual_transform_active"] = False
            _safety_relative_transform(edit, profile)
        else:
            # Se a arte-fonte está em ajuste manual, replicamos o estado dos
            # controles sem copiar a posição/zoom da fonte para as demais.
            edit["manual_transform_active"] = True
        # Copiar o conjunto de preenchimento da arte-fonte, usando os controles
        # que já existem à esquerda do editor.
        edit["background_mode"] = source_edit.get("background_mode", "Cor sólida")
        edit["background"] = background
        edit["blur_strength"] = blur_strength
        updated += 1
    return updated


def _on_pool_batch_selection_change() -> None:
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


def _on_pool_apply_selected_fallback() -> None:
    selected = st.session_state.get("artwork_batch_selected_ids", [])
    selected_id = st.session_state.get("artwork_selected_id")
    spec = BUTTONS.get(str(st.session_state.get("single_size")))
    if spec is not None:
        count = apply_batch_artwork_settings([int(value) for value in selected], spec, selected_id)
        st.session_state["_artwork_batch_notice"] = f"Ajustes aplicados a {count} arte(s) selecionada(s)."


def make_color_eyedropper_callback(component_key: str, color_key: str, item_id: int | None = None):
    """Aplica a cor selecionada e grava somente o fundo da arte correspondente."""
    def _apply_picked_color():
        result = st.session_state.get(component_key)
        picked = _component_value(result, "picked_color")
        if isinstance(picked, str) and len(picked) == 7 and picked.startswith("#"):
            try:
                ImageColor.getrgb(picked)
            except ValueError:
                return
            st.session_state[color_key] = picked.upper()
            if item_id is not None:
                _persist_artwork_fields(int(item_id), ("background",))
    return _apply_picked_color


@st.cache_resource(show_spinner=False)
def get_color_eyedropper_component():
    """Botão de conta-gotas nativo do navegador (EyeDropper API)."""
    html = '<div class="atomica-eyedropper-root" id="atomica-eyedropper-root"></div>'
    css = r"""
    .atomica-eyedropper-root { width:100%; margin:5px 0 2px; font-family:inherit; }
    .atomica-eyedropper-button {
      display:inline-flex; align-items:center; justify-content:center; gap:8px;
      min-height:36px; padding:7px 12px; border:1px solid #507091; border-radius:9px;
      background:#173653; color:#F4F7FB; font-family:inherit; font-size:13px; font-weight:700; line-height:1.2; cursor:pointer;
      transition:background .15s,border-color .15s;
    }
    .atomica-eyedropper-button:hover { background:#24517A; border-color:#77B6E8; }
    .atomica-eyedropper-button:disabled { opacity:.65; cursor:wait; }
    .atomica-eyedropper-status { display:inline-block; margin-left:9px; color:#9EB1C7; font-size:11px; }
    """
    js = r"""
    export default function(component) {
      const { parentElement, setTriggerValue } = component;
      let root = parentElement.querySelector('#atomica-eyedropper-root');
      if (!root) return;
      let button = root.querySelector('button');
      let status = root.querySelector('.atomica-eyedropper-status');
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'atomica-eyedropper-button';
        button.textContent = '⌖  Conta-gotas: capturar cor da tela';
        status = document.createElement('span');
        status.className = 'atomica-eyedropper-status';
        root.replaceChildren(button, status);
      }
      button.onclick = async () => {
        if (!window.isSecureContext || typeof window.EyeDropper !== 'function') {
          status.textContent = 'Indisponível neste navegador; use Chrome ou Edge atualizado.';
          return;
        }
        button.disabled = true;
        status.textContent = 'Clique em qualquer ponto da tela…';
        try {
          const result = await new window.EyeDropper().open();
          const color = String(result.sRGBHex || '').toUpperCase();
          if (/^#[0-9A-F]{6}$/.test(color)) {
            setTriggerValue('picked_color', color);
            status.textContent = `Cor selecionada: ${color}`;
          } else {
            status.textContent = 'Não foi possível ler a cor selecionada.';
          }
        } catch (error) {
          status.textContent = error && error.name === 'AbortError'
            ? 'Seleção cancelada.' : 'Não foi possível capturar a cor.';
        } finally {
          button.disabled = false;
        }
      };
    }
    """
    return st.components.v2.component(
        "atomica_color_eyedropper", html=html, css=css, js=js
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
                  <span class="atomica-real-size-check-circle" id="atomica-real-size-check-68"></span>
                  <b>68 mm · corte do botton 58 mm</b>
                </div>
              </div>
              <small>Os círculos representam o diâmetro externo de CORTE (54 e 68 mm), não a face nominal da arte. Ambos devem coincidir com os respectivos gabaritos físicos usando a mesma calibração.</small>
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
      const check68 = root.querySelector('#atomica-real-size-check-68');
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
        if (check68) {
          const cut68 = 68 * pxPerMm * scale;
          check68.style.width = `${cut68}px`;
          check68.style.height = `${cut68}px`;
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


def _on_fit_mode_changed(
    selected_mode: str,
    selected_key: str,
    other_key: str,
    prefix: str = "global_image",
    item_id: int | None = None,
) -> None:
    """Aplica o preset automático somente à arte selecionada e recentra seu editor."""
    if not st.session_state.get(selected_key, False):
        # Ao desmarcar o preset, o usuário assume o controle manual do ajuste.
        if item_id is not None:
            item = _find_artwork_item(int(item_id))
            if item is not None:
                item.setdefault("edit", {})["manual_transform_active"] = True
        # Ao desmarcar o preset, congela a transformação atual como composição
        # manual relativa à guia Segurança antes de uma troca futura de tamanho.
        if item_id is not None:
            _persist_artwork_fields(int(item_id), ("fit_cut_checked", "fit_art_checked"))
            item = _find_artwork_item(int(item_id))
            if item is not None:
                edit = item.setdefault("edit", {})
                _safety_relative_transform(edit, edit.get("scale_reference_spec"))
        return

    st.session_state[other_key] = False
    st.session_state[f"{prefix}_fit_basis_mode"] = selected_mode
    if item_id is not None:
        item = _find_artwork_item(int(item_id))
        if item is not None:
            item.setdefault("edit", {})["manual_transform_active"] = False
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


def render_fit_mode_controls(prefix: str = "global_image", item_id: int | None = None) -> str:
    """Mostra presets mutuamente exclusivos com estado independente por arte."""
    cut_key = f"{prefix}_fit_cut_checked"
    art_key = f"{prefix}_fit_art_checked"
    basis_key = f"{prefix}_fit_basis_mode"
    st.session_state.setdefault(cut_key, st.session_state.get(basis_key, "cut") == "cut")
    st.session_state.setdefault(art_key, st.session_state.get(basis_key, "cut") == "art")
    st.session_state.setdefault(basis_key, "art" if st.session_state.get(art_key) else "cut")
    st.checkbox(
        "Preencher até o corte",
        key=cut_key,
        on_change=_on_fit_mode_changed,
        args=("cut", cut_key, art_key, prefix, item_id),
        help="Aumenta a arte proporcionalmente até cobrir a guia de corte e centraliza o enquadramento.",
    )
    st.checkbox(
        "Encaixar na área da arte",
        key=art_key,
        on_change=_on_fit_mode_changed,
        args=("art", art_key, cut_key, prefix, item_id),
        help="Redimensiona para caber integralmente na guia da arte e centraliza o enquadramento.",
    )
    st.caption("Todas as imagens começam com ‘Preencher até o corte’ ativado. Você pode desmarcar o preset e ajustar o enquadramento manualmente; cada arte salva seus próprios ajustes.")
    return str(st.session_state.get(basis_key, "cut"))


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
    """Legenda vertical das guias, usada ao lado da prévia ou abaixo do editor."""
    guide_rows = [
        ("#E5457B", "dashed", "Corte", "Limite externo do papel e área de sangria."),
        ("#FFFF00", "dashed", "Dobra", "Linha onde a matriz dobra o papel."),
        ("#2F9BD8", "solid", "Arte", "Área visível da imagem no botton pronto."),
        ("#00F919", "dashed", "Segurança", "Mantenha textos e detalhes importantes dentro desta área."),
    ]
    rows = []
    for color, style, label, description in guide_rows:
        rows.append(
            "<div style='display:flex;gap:11px;align-items:flex-start;padding:12px 10px;"
            "border-bottom:1px solid rgba(120,150,185,.18);'>"
            f"<span style='display:block;flex:0 0 34px;margin-top:8px;border-top:3px {style} {color};'></span>"
            "<div style='min-width:0;'>"
            f"<div style='font-size:13px;font-weight:750;color:#F4F7FB;'>{label}</div>"
            f"<div style='font-size:11px;line-height:1.45;color:#AFC0D4;margin-top:3px;'>{description}</div>"
            "</div></div>"
        )
    html = (
        "<div style='border:1px solid #29415F;border-radius:13px;background:#0B1B30;"
        "overflow:hidden;width:100%;box-sizing:border-box;'>"
        + "".join(rows)
        + "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


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
    @media (max-width:560px) {
      .atomica-editor-stage { padding:6px; }
      .atomica-editor-toolbar { gap:6px; }
      .atomica-editor-toolbar button { min-height:35px; padding:6px 9px; }
      .atomica-guide-legend { gap:8px; padding:9px; }
      .atomica-guide-item { grid-template-columns:26px 1fr; gap:5px; }
      .atomica-guide-line { width:23px; }
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
          imageReady: false,
          backdropReady: false,
          imageSrc: '',
          backdropSrc: '',
          blurCanvas: null,
          blurCanvasSignature: null,
          lastDrawSignature: null,
          activeDrag: null,
          wheelTimer: null,
          drawFrame: null,
          fixedView: null,
          fixedViewSignature: null,
          root: rootElement,
          nodes: []
        };
        const side = 1000;
        const handleRadius = 17;

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
            fit_mode: String(cfg.fit_mode || 'cut'),
            safety_relative: {
              zoom: Number((instance.state.zoom * ((cfg.fit_mode || 'cut') === 'art' ? Number(cfg.art) : Number(cfg.cut)) / Math.max(1e-9, Number(cfg.safe))).toFixed(8)),
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

        makeButton('−', 'Diminuir o zoom em exatamente 1 ponto percentual', '', () => {
          const currentPercent = Math.round(instance.state.zoom * 100);
          instance.state.zoom = clamp((currentPercent - 1) / 100, 0.01, 20);
          finishChange();
        });
        toolbar.appendChild(status);
        makeButton('+', 'Aumentar o zoom em exatamente 1 ponto percentual', '', () => {
          const currentPercent = Math.round(instance.state.zoom * 100);
          instance.state.zoom = clamp((currentPercent + 1) / 100, 0.01, 20);
          finishChange();
        });
        makeButton('Centralizar', 'Centralizar a arte sem alterar seu tamanho', 'primary', () => {
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
        hint.textContent = 'Arraste o interior da arte para mover · use as quatro alças dos cantos para redimensionar proporcionalmente · use + e − para ajustes finos.';
        rootElement.appendChild(hint);

        function getCfg() { return instance.cfg || {}; }
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
            cfg.token, cfg.shape, cfg.cut, cfg.art, cfg.fit_mode,
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
          const inset = handleRadius + 5;
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
          getHandles(rect).forEach(handle => {
            ctx.beginPath(); ctx.arc(handle.x, handle.y, handleRadius, 0, Math.PI*2);
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
          const fitMode = cfg.fit_mode || 'cut';
          if ((cfg.background_mode || 'color') === 'blur') {
            // O fundo desfocado é pré-renderizado uma vez por imagem/cor/raio;
            // durante arraste e zoom apenas copiamos a camada pronta ao canvas.
            const blurredBackdrop = ensureBlurBackdrop(view);
            if (blurredBackdrop) ctx.drawImage(blurredBackdrop, 0, 0);
          }
          let selectedRect;
          if (fitMode === 'art') {
            ctx.save();
            shapePath(ctx, Number(cfg.art)||Number(cfg.cut)||1, view);
            ctx.clip();
            selectedRect = drawImagePlaced(view, true);
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
        function hitHandle(point, rect) {
          for (const handle of getHandles(rect)) {
            if (Math.hypot(point.x-handle.x, point.y-handle.y) <= handleRadius*2.0) return handle;
          }
          return null;
        }
        function pointInsideRect(point, rect) {
          return point.x>=rect.x && point.x<=rect.x+rect.w &&
                 point.y>=rect.y && point.y<=rect.y+rect.h;
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
          const cfg = getCfg();
          const view = getView();
          const rect = getRect(view, (cfg.fit_mode||'cut')==='art');
          const point = canvasPoint(event);
          const handle = hitHandle(point, rect);
          const mode = handle ? 'resize' : (pointInsideRect(point, rect) ? 'move' : null);
          if (!mode) return;
          instance.activeDrag = {
            mode, handle, view, rect, startPoint:point,
            startZoom:instance.state.zoom,
            startX:instance.state.off_x,
            startY:instance.state.off_y
          };
          canvas.classList.toggle('moving', mode==='move');
          canvas.classList.toggle('resizing', mode==='resize');
          canvas.setPointerCapture(event.pointerId);
          event.preventDefault();
        });
        canvas.addEventListener('pointermove', event => {
          if (!instance.activeDrag) return;
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
            const startVector = {
              x:h.rawX-pivot.x,
              y:h.rawY-pivot.y
            };
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
        function endDrag() {
          if (!instance.activeDrag) return;
          instance.activeDrag = null;
          canvas.classList.remove('moving','resizing');
          draw(); updateStatus(); publish();
        }
        canvas.addEventListener('pointerup', endDrag);
        canvas.addEventListener('pointercancel', endDrag);
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
      const drawSignature = JSON.stringify([
        incomingToken, cfg.shape, cfg.cut, cfg.fold, cfg.art, cfg.safe,
        Array.isArray(cfg.guides) ? cfg.guides.join(',') : '',
        cfg.fit_mode, cfg.background_mode, cfg.blur_mm, cfg.bg,
        instance.imageReady, instance.backdropReady,
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
      grid-template-columns:repeat(auto-fill, 136px);
      justify-content:start;
      align-items:start;
      gap:11px;
      width:100%;
      max-width:980px;
    }
    .pool-card {
      position:relative;
      display:flex;
      flex-direction:column;
      align-items:stretch;
      gap:7px;
      box-sizing:border-box;
      width:136px;
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
    .pool-card.selected {
      border:2px solid #26C6DA;
      padding:6px;
      background:#122D4B;
      box-shadow:0 0 0 1px rgba(38,198,218,.12);
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
    .pool-grip, .pool-remove { pointer-events:auto; box-shadow:0 1px 5px rgba(0,0,0,.28); }
    .pool-grip {
      display:flex; align-items:center; justify-content:center;
      width:25px; height:25px; border-radius:7px;
      color:#FFFFFF; background:rgba(10,25,45,.78); border:1px solid rgba(255,255,255,.25);
      font-size:17px; line-height:1; cursor:grab;
    }
    .pool-remove { width:25px; height:25px; font-size:16px; background:rgba(10,25,45,.86); }
    .pool-selected-mark {
      position:absolute; left:12px; right:auto; top:auto; bottom:38px; z-index:1;
      width:19px; height:19px; border-radius:50%;
      display:flex; align-items:center; justify-content:center;
      color:#061B2D; background:#26C6DA; border:1px solid #FFFFFF;
      font-size:12px; font-weight:900; line-height:1;
      pointer-events:none;
    }
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
    @media (max-width:520px) {
      .atomica-pool-root { grid-template-columns:repeat(auto-fill, 130px); gap:8px; }
      .pool-card { width:130px; min-height:130px; padding:6px; }
      .pool-card.selected { padding:5px; }
      .pool-counter { gap:4px; }
      .pool-counter button { width:24px; height:28px; }
      .pool-qty { width:58px; min-width:58px; font-size:13px; }
      .pool-actions { top:9px; left:9px; right:9px; }
      .pool-selected-mark { left:10px; bottom:35px; }
      .pool-toolbar { align-items:stretch; }
      .pool-fill-button { width:100%; }
      .pool-batch-toolbar { align-items:stretch; }
      .pool-batch-copy { min-width:0; width:100%; }
      .pool-batch-actions { width:100%; }
      .pool-batch-secondary, .pool-batch-danger { flex:1 1 0; }
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
          let mark = card.querySelector('.pool-selected-mark');
          if (isFocused && !mark) {
            mark = document.createElement('span');
            mark.className = 'pool-selected-mark';
            mark.textContent = '✓';
            mark.setAttribute('aria-label', 'Arte em edição na prévia');
            card.appendChild(mark);
          } else if (!isFocused && mark) {
            mark.remove();
          }
          const batchToggle = card.querySelector('.pool-batch-toggle');
          if (batchToggle) {
            const selectedSet = root.__atomicaPoolBatchSelectedIds || new Set();
            const batchSelected = selectedSet.has(id);
            batchToggle.classList.toggle('checked', batchSelected);
            batchToggle.textContent = batchSelected ? '✓' : '□';
            batchToggle.title = batchSelected ? 'Remover da seleção em lote' : 'Incluir na seleção em lote';
            batchToggle.setAttribute('aria-pressed', batchSelected ? 'true' : 'false');
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
          actions.append(grip, remove);

          card.append(thumb, counter, actions);
          if (id === selectedId) {
            const selectedMark = document.createElement('span');
            selectedMark.className = 'pool-selected-mark';
            selectedMark.textContent = '✓';
            selectedMark.setAttribute('aria-label', 'Arte em edição na prévia');
            card.appendChild(selectedMark);
          }
          const batchToggle = document.createElement('button');
          const isBatchSelected = batchSelectedIds.has(id);
          batchToggle.type = 'button';
          batchToggle.className = 'pool-batch-toggle' + (isBatchSelected ? ' checked' : '');
          batchToggle.textContent = isBatchSelected ? '✓' : '□';
          batchToggle.title = isBatchSelected ? 'Remover da seleção em lote' : 'Incluir na seleção em lote';
          batchToggle.setAttribute('aria-pressed', isBatchSelected ? 'true' : 'false');
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
    guides: tuple,
) -> Image.Image:
    """Cacheia cada botton individual; alterar uma arte não redesenha as demais."""
    image = load_image_preview(image_digest, image_bytes, 1200)
    return render_button(
        spec, image, ppm=ppm, zoom=zoom, off_x=off_x, off_y=off_y,
        bg=item_bg, guides=guides, pad_mm=0.5, line_mm=0.25,
        fit_mode=fit_mode, blur_mm=blur_mm, background_mode=background_mode,
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
        image_digest, image_bytes, zoom, off_x, off_y, fit_mode, background_mode, blur_mm, item_bg = payload
        button = render_button_screen_preview_cached(
            image_digest, image_bytes, spec, ppm, zoom, off_x, off_y,
            fit_mode, background_mode, blur_mm, item_bg, guides,
        )
        sheet.paste(
            button,
            (round(cx * ppm - button.width / 2), round(cy * ppm - button.height / 2)),
            button,
        )
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
    }
    for suffix, default_value in defaults.items():
        if suffix not in saved:
            legacy_key = f"{prefix}_{suffix}"
            saved[suffix] = st.session_state.get(legacy_key, default_value)

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

    if sync_session:
        # Preparar estado por ID antes de instanciar os respectivos widgets.
        for suffix, default_value in defaults.items():
            st.session_state[f"{prefix}_{suffix}"] = saved.get(suffix, default_value)
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


def main():
    st.set_page_config(
        page_title=brand.APP_TITLE,
        page_icon=brand.icon_image(),
        layout="wide",
    )
    brand.apply_theme()
    brand.render_header()

    st.session_state.setdefault("artwork_items", [])
    st.session_state.setdefault("artwork_next_id", 1)
    st.session_state.setdefault("artwork_upload_revision", 0)
    st.session_state.setdefault("artwork_selected_id", None)
    st.session_state.setdefault("artwork_batch_selected_ids", [])
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
        )
        spec = BUTTONS[size_key]
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
    if st.session_state.get("_layout_profile_key") != profile_key:
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
    mode = "grid" if layout_label == "Grade" else "hex"
    guides = tuple(g.key for g in GUIDES if bool(st.session_state.get(f"guide_single_{g.key}", g.key == "cut")))

    # ---------------- Pool de artes ----------------
    brand.section_title(None, "ARTES DA IMPRESSÃO", brand.CYAN)
    upload_revision = st.session_state["artwork_upload_revision"]
    with st.expander("＋ Adicionar imagens", expanded=(len(st.session_state["artwork_items"]) == 0)):
        with st.form(f"upload_artworks_{upload_revision}", clear_on_submit=True):
            uploaded_files = st.file_uploader(
                "Selecione uma ou várias artes",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
                help="Cada arquivo será adicionado como uma arte independente. Depois, defina quantas cópias de cada uma deseja imprimir.",
            )
            add_uploads = st.form_submit_button("Adicionar imagens à lista", type="primary", use_container_width=True)
        if add_uploads:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            added_ids = []
            existing_ids_before_upload = {int(item["id"]) for item in st.session_state["artwork_items"]}
            selected_before_upload = st.session_state.get("artwork_selected_id")
            files_to_prepare = [
                (uploaded_file.name, uploaded_file.getvalue())
                for uploaded_file in (uploaded_files or [])
            ]
            prepared = [None] * len(files_to_prepare)
            failures = []
            progress = st.progress(0, text="Preparando miniaturas…") if files_to_prepare else None
            if files_to_prepare:
                # Dois workers equilibram paralelismo e memória ao lidar com
                # fotos grandes; a decodificação reduzida de JPEG evita abrir
                # megapixels desnecessários para miniaturas pequenas.
                with ThreadPoolExecutor(max_workers=min(2, len(files_to_prepare))) as executor:
                    future_indexes = {
                        executor.submit(prepare_uploaded_artwork, name, data): index
                        for index, (name, data) in enumerate(files_to_prepare)
                    }
                    completed = 0
                    for future in as_completed(future_indexes):
                        index = future_indexes[future]
                        try:
                            prepared[index] = future.result()
                        except Exception as exc:
                            failures.append((index, str(exc)))
                        completed += 1
                        progress.progress(
                            completed / len(files_to_prepare),
                            text=f"Preparando miniaturas… {completed}/{len(files_to_prepare)}",
                        )
                progress.empty()

            used_item_ids = {int(item.get("id", -1)) for item in st.session_state["artwork_items"]}
            next_id = max(int(st.session_state.get("artwork_next_id", 1)), max(used_item_ids, default=0) + 1)
            for index, (file_name, image_bytes) in enumerate(files_to_prepare):
                prepared_item = prepared[index]
                if prepared_item is None:
                    continue
                digest, thumb_url = prepared_item
                item_id = next_id
                next_id += 1
                st.session_state["artwork_items"].append({
                    "id": item_id,
                    "name": file_name,
                    "image": image_bytes,
                    "image_digest": digest,
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
                    },
                })
                added_ids.append(item_id)
            st.session_state["artwork_next_id"] = next_id

            if added_ids and selected_before_upload not in existing_ids_before_upload:
                st.session_state["artwork_selected_id"] = added_ids[0]
            if failures:
                st.warning(f"{len(failures)} arquivo(s) não puderam ser lidos como imagem e foram ignorados.")
            st.session_state["artwork_upload_revision"] += 1
            st.rerun()

    art_items = st.session_state["artwork_items"]
    batch_notice = st.session_state.pop("_artwork_batch_notice", None)
    if batch_notice:
        st.toast(batch_notice, icon="✅")
    pool_delete_notice = st.session_state.pop("_pool_delete_notice", None)
    if pool_delete_notice:
        st.toast(pool_delete_notice, icon="✅")
    if art_items:
        current_pool_ids = {int(item["id"]) for item in art_items}
        st.session_state["artwork_batch_selected_ids"] = [
            int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])
            if int(value) in current_pool_ids
        ]
    if art_items:
        # Inicializa/migra TODAS as artes antes de o Pool permitir alternar
        # entre miniaturas. Assim, a segunda/terceira imagem não depende de
        # ser selecionada uma primeira vez para receber seu preset canônico.
        for artwork_entry in art_items:
            ensure_artwork_edit_state(artwork_entry, spec, sync_session=False)

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
            if saved_selected in remaining_ids:
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
        st.caption("Clique na miniatura para editá-la. Use as caixas nas miniaturas para selecionar artes. A aplicação em lote fica dentro de AJUSTES GERAIS DA ARTE; ao lado da seleção há o comando contextual para excluir a seleção ou todas as artes. A posição e o zoom manuais continuam individuais. Digite a quantidade ou use +/−; arraste para ordenar a impressão.")

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
            if result_selected in current_ids_int:
                st.session_state["artwork_selected_id"] = result_selected

            result_batch_ids = getattr(pool_result, "selected_ids", None)
            if isinstance(result_batch_ids, (list, tuple, set)):
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
                            st.checkbox("Aplicar ajustes", value=batch_selected, key=batch_key,
                                        on_change=_sync_fallback_batch_selection, args=(int(item["id"]), batch_key))
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
   

    # ---------------- Ajustes, preview e exportação PNG da arte em foco ----------------
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

    controls_col, editor_col, legend_col = st.columns([0.92, 1.72, 0.96], gap="medium")
    with controls_col:
        with st.container(key="card_left"):
            brand.section_title(None, "AJUSTES GERAIS DA ARTE", brand.YELLOW)
            if selected_item is not None:
                st.caption("Os ajustes abaixo ficam salvos individualmente para a arte selecionada.")
            else:
                st.info("Adicione uma arte para liberar os ajustes.")

            st.markdown("**Ajuste da imagem**")
            if selected_item is not None:
                fit_mode = render_fit_mode_controls(prefix, int(selected_item["id"]))
                background_label = st.radio(
                    "Preenchimento das áreas vazias",
                    ["Cor sólida", "Imagem desfocada"],
                    key=f"{prefix}_background_mode",
                    on_change=make_artwork_settings_callback(int(selected_item["id"]), "background_mode"),
                    help="O fundo ocupa toda a área interna do corte; a arte ajustada fica por cima.",
                )
                background_mode = "blur" if background_label == "Imagem desfocada" else "color"
                blur_mm = st.slider(
                    "Intensidade do desfoque", 0.0, 18.0, step=0.1,
                    key=f"{prefix}_blur_strength",
                    on_change=make_artwork_settings_callback(int(selected_item["id"]), "blur_strength"),
                    disabled=(background_mode != "blur"),
                    help="0 desativa o desfoque; valor inicial: 0,5 mm.",
                )
                bg_label = "Cor de base" if background_mode == "blur" else "Cor de fundo"
                color_key = f"{prefix}_background"
                bg = st.color_picker(
                    bg_label, key=color_key,
                    on_change=make_artwork_settings_callback(int(selected_item["id"]), "background"),
                )
                eyedropper_key = f"color_eyedropper_{int(selected_item['id'])}"
                eyedropper = get_color_eyedropper_component()
                eyedropper(
                    data={"current_color": bg},
                    on_picked_color_change=make_color_eyedropper_callback(
                        eyedropper_key, color_key, int(selected_item["id"])
                    ),
                    key=eyedropper_key,
                )

                # A aplicação em lote fica junto dos controles de origem. Assim,
                # ela sempre reutiliza os ajustes visíveis neste mesmo painel.
                batch_target_ids = [
                    int(value) for value in st.session_state.get("artwork_batch_selected_ids", [])
                    if any(int(item.get("id", -1)) == int(value) for item in st.session_state.get("artwork_items", []))
                ]
                with st.container(border=True):
                    st.markdown("**Aplicar ajustes aos selecionados**")
                    if batch_target_ids:
                        st.caption(f"Os ajustes desta área serão aplicados a {len(batch_target_ids)} arte(s) marcada(s) no Pool. O zoom e a posição manuais de cada uma serão preservados.")
                    else:
                        st.caption("Marque uma ou mais artes diretamente nas miniaturas do Pool para habilitar a aplicação em lote.")
                    st.button(
                        "APLICAR AJUSTES AOS SELECIONADOS",
                        key="general_apply_settings_to_selected_artworks",
                        type="primary",
                        use_container_width=True,
                        disabled=not bool(batch_target_ids),
                        on_click=_on_pool_apply_selected_fallback,
                    )
            else:
                st.caption("Os controles serão habilitados quando uma arte for selecionada.")

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
            brand.section_title(None, "PRÉVIA DE AJUSTE", brand.MAGENTA)
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
                    if not covers_shape(box, spec.cut):
                        fill_text = "o fundo desfocado" if background_mode == "blur" else "a cor sólida escolhida"
                        st.info(f"As áreas do corte não ocupadas pela arte principal serão preenchidas por {fill_text}.")
                else:
                    dpi = max(selected_dimensions) / ((spec.art * zoom) / 25.4)
                if dpi < 200:
                    st.warning(f"Resolução efetiva ≈ {dpi:.0f} dpi — a impressão pode ficar pixelada (ideal: 300 dpi).")
                else:
                    st.caption(f"Resolução efetiva da arte selecionada ≈ {dpi:.0f} dpi")
            else:
                st.info("Selecione uma miniatura no pool para abrir a arte no editor com as guias.")

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
                    )
                    dpi_export = 600 if dpi_label.startswith("600") else 300
                png_signature_data = (
                    int(selected_item["id"]), selected_item["image_digest"],
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
                        clean_png, png_size = build_button_art_png(
                            spec, production_image, dpi=dpi_export, zoom=zoom,
                            off_x=off_x, off_y=off_y, bg=bg, fit_mode=fit_mode,
                            background_mode=background_mode, blur_mm=float(blur_mm),
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

    with legend_col:
        with st.container(key="card_guide_legend_side"):
            brand.section_title(None, "LEGENDA", brand.YELLOW)
            render_guide_legend()

    # ---------------- Folha de impressão e exportação ----------------
    with st.container(key="card_right"):
        brand.section_title(None, "FOLHA DE IMPRESSÃO", brand.MAGENTA)
        sheet_settings_col, sheet_canvas_col = st.columns([1.02, 3.18], gap="large")

        # Controles físicos da folha ficam ao lado da prévia, dentro da mesma
        # sessão visual. O gabarito SVG A4 foi movido para a barra lateral.
        with sheet_settings_col:
            with st.container(border=True, key="sheet_settings_panel"):
                st.markdown('<div class="sheet-config-title">CONFIGURAÇÃO DA FOLHA</div>', unsafe_allow_html=True)
                st.caption("Ajuste a folha e acompanhe a prévia ao lado.")
                paper = st.selectbox("Papel", [*PAPERS, "Personalizado"], key="paper_size")
                if paper == "Personalizado":
                    custom_cols = st.columns(2)
                    custom_cols[0].number_input(
                        "Largura (mm)", 60.0, 1500.0, step=1.0, key="custom_page_w"
                    )
                    custom_cols[1].number_input(
                        "Altura (mm)", 60.0, 1500.0, step=1.0, key="custom_page_h"
                    )
                st.radio(
                    "Orientação", ["Retrato", "Paisagem"], horizontal=True,
                    key="page_orientation",
                )
                st.number_input(
                    "Margem da folha (mm)", 0.0, 50.0, step=0.5, key="margin_mm",
                    help="Valor inicial calculado para o papel e tamanho escolhido. Você pode alterar livremente.",
                )
                st.number_input(
                    "Espaço entre bottons (mm)", 0.0, 20.0, step=0.5, key="gap_mm",
                    help="Distância mínima entre os limites externos de corte dos bottons.",
                )
                st.caption(
                    f"Sugestão automática: margem {recommended_margin:g} mm · "
                    f"distância {recommended_gap:g} mm."
                )
                st.button(
                    "Restaurar valores recomendados",
                    use_container_width=True,
                    on_click=restore_recommended_layout,
                )
                with st.expander("Guias impressas", expanded=False):
                    for guide in GUIDES:
                        st.checkbox(
                            f"{guide.label} ({getattr(spec, guide.attr):g} mm)",
                            key=f"guide_single_{guide.key}",
                        )

        with sheet_canvas_col:
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
                        item_bg = str(edit.get("background", "#FFFFFF"))
                        item_background_label = str(edit.get("background_mode", "Cor sólida"))
                        item_background_mode = "blur" if item_background_label == "Imagem desfocada" else "color"
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
                            )
                            for entry in current_page_items
                        )
                        sheet_preview_image = render_mixed_sheet_preview_cached(
                            preview_payload, preview_placements_tuple, spec,
                            page_w, page_h, bg, tuple(guides),
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
                                entry["id"], entry["image_digest"], entry["qty"],
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
                                    pdf_items.append(pdf_entry)
                                pdf = build_art_batch_pdf(
                                    pdf_items, positions, page_w, page_h, total_qty,
                                    bg=bg, guides=guides,
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
