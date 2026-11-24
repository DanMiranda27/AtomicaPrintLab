"""
Identidade visual do app — Atômica Print

Para trocar logo, cores ou fonte, altere apenas este arquivo e a pasta assets/:
    assets/icon.png                 ícone (PNG quadrado, de preferência com fundo transparente)
    assets/GochiHand-Regular.ttf    fonte opcional da marca (licença OFL — ver assets/OFL.txt)
As cores base do tema (fundo, cor primária) ficam em .streamlit/config.toml.
"""

from __future__ import annotations

import base64
import io
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

import streamlit as st
from PIL import Image

ASSETS = Path(__file__).parent / "assets"
ICON_FILE = ASSETS / "icon.png"
SIDEBAR_LOGO_FILE = ASSETS / "logo-atomica-print.png"
HEADER_ILLUSTRATION_FILE = ASSETS / "erlenmeyer-lab.png"
FONT_FILE = ASSETS / "GochiHand-Regular.ttf"
GOCHI_FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/gochihand/GochiHand-Regular.ttf"


@lru_cache(maxsize=1)
def sheet_font_path() -> Path | None:
    """Retorna Gochi Hand para desenho raster/PDF; baixa uma cópia local se necessário."""
    if FONT_FILE.is_file():
        return FONT_FILE
    try:
        request = Request(GOCHI_FONT_URL, headers={"User-Agent": "AtomicaPrintLab/3.43.0"})
        with urlopen(request, timeout=8) as response:
            payload = response.read(500_001)
        # Gochi Hand é um TTF TrueType estático; rejeita HTML/erros e respostas excessivas.
        if not (20_000 <= len(payload) <= 500_000 and payload.startswith(b"\x00\x01\x00\x00")):
            return None
        ASSETS.mkdir(parents=True, exist_ok=True)
        temporary = FONT_FILE.with_name(FONT_FILE.name + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(FONT_FILE)
        return FONT_FILE
    except Exception:
        # A aplicação continua funcional offline, usando a fonte alternativa disponível.
        return None


BRAND_PRE, BRAND_POST = "Atômica ", "Print"  # a 2ª parte é destacada em magenta
BRAND_NAME = BRAND_PRE + BRAND_POST
APP_TITLE = "Atômica Print Lab"
APP_SLOGAN = "Crie suas artes de forma fácil e rápida com a gente!"

# Paleta extraída da logo (CMYK)
CYAN = "#1BA9D8"
MAGENTA = "#EE1D7E"
YELLOW = "#F5DD1E"
INK = "#0C0C0E"  # fundo
PANEL = "#16161B"  # cartões
LINE = "#2A2A33"  # bordas
MUTED = "#9A9AA6"  # textos secundários

_CSS = """
@font-face{font-family:"Gochi Hand";font-style:normal;font-weight:400;font-display:swap;
src:url(data:font/ttf;base64,__FONT__) format("truetype");}

:root{--c:__C__;--m:__M__;--y:__Y__;--ink:__INK__;--panel:__PANEL__;--line:__LINE__;--muted:__MUTED__;}

/* ---------- base ---------- */
.stApp{background:
  radial-gradient(900px 420px at 100% -8%, rgba(238,29,126,.13), transparent 60%),
  radial-gradient(800px 420px at -5% 0%, rgba(27,169,216,.11), transparent 55%),
  var(--ink);}
.stApp::before{content:"";position:fixed;inset:0 0 auto 0;height:4px;z-index:1000000;
  background:linear-gradient(90deg,var(--c) 0 33.34%,var(--m) 33.34% 66.67%,var(--y) 66.67% 100%);}
::selection{background:var(--m);color:#fff}
[data-testid="stHeader"]{background:transparent}
[data-testid="stMainBlockContainer"]{padding-top:2.4rem;max-width:1280px}
[data-testid="stSidebar"]{background:#101014;border-right:1px solid var(--line)}
[data-testid="stElementContainer"]:has(style){display:none}
footer{visibility:hidden}

/* ---------- tipografia ---------- */
h1,h2,h3,h4{font-family:"Gochi Hand",cursive !important;font-weight:400 !important;letter-spacing:.03em}

/* ---------- cabeçalho da marca ---------- */
/* O nome e o slogan ficam centralizados; a ilustração acompanha LAB à direita. */
.brand{display:flex;justify-content:center;align-items:center;width:100%;padding:.15rem 0 .1rem}
.brand-lockup{display:flex;align-items:center;justify-content:center;gap:clamp(.35rem,1.1vw,.7rem);
  width:100%;max-width:100%;min-width:0}
.brand-copy{flex:1 1 auto;min-width:0;text-align:center}
.brand-name{font-family:"Gochi Hand",cursive !important;font-size:clamp(2.35rem,4.9vw,4.15rem);line-height:1.02;
  letter-spacing:.018em;color:#fff;text-transform:uppercase;white-space:nowrap}
.brand-name .brand-cyan{color:var(--c)}
.brand-name .brand-magenta{color:var(--m)}
.brand-name .brand-yellow{color:var(--y)}
.brand-sub{margin-top:.38rem;color:var(--muted);font-size:1.03rem;letter-spacing:.02em;text-align:center}
.brand-illustration{display:block;flex:0 0 auto;width:auto;height:clamp(112px,13vw,166px);
  max-width:clamp(78px,9.5vw,118px);object-fit:contain;object-position:center;
  filter:drop-shadow(0 5px 12px rgba(0,0,0,.28))}
.brand-rule{height:3px;border-radius:3px;margin:.8rem 0 1.5rem;opacity:.9;
  background:linear-gradient(90deg,var(--c),var(--m),var(--y))}

/* ---------- única logo ilustrada: acima de INSUMOS ---------- */
.sidebar-brand-logo{display:flex;align-items:center;justify-content:center;width:100%;
  padding:.2rem .1rem 1rem;margin:0 0 .8rem;border-bottom:1px solid var(--line);
  background:transparent !important;box-shadow:none !important;overflow:visible}
.sidebar-brand-logo img{display:block;width:100%;max-width:258px;height:auto;object-fit:contain;
  border:0 !important;border-radius:0 !important;background:transparent !important;
  box-shadow:none !important;mix-blend-mode:normal;
  /* Contorno delicado aplicado ao alfa do PNG: não cria fundo nem quadrado. */
  filter:drop-shadow(0 0 1px rgba(255,255,255,.96))
         drop-shadow(0 0 1.8px rgba(255,255,255,.42))
         drop-shadow(0 7px 18px rgba(0,0,0,.30))}

@media(max-width:1024px){
  .brand-lockup{gap:clamp(.2rem,.8vw,.5rem)}
  .brand-name{font-size:clamp(2.05rem,5.1vw,3.35rem);letter-spacing:.005em}
  .brand-illustration{height:clamp(96px,13vw,138px);max-width:clamp(68px,9vw,96px)}
}
@media(max-width:700px){
  .brand{padding:.1rem 0}
  .brand-lockup{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:clamp(.15rem,1vw,.35rem)}
  .brand-copy{width:100%;}
  .brand-name{font-size:clamp(1.12rem,5.8vw,2.25rem);letter-spacing:0;line-height:1.08}
  .brand-sub{font-size:clamp(.62rem,2.35vw,.86rem);line-height:1.25;margin-top:.25rem;padding-inline:.1rem}
  .brand-illustration{height:clamp(68px,18vw,108px);max-width:clamp(44px,14vw,76px);width:auto;}
}
@media(max-width:380px){
  .brand-lockup{gap:.12rem}
  .brand-name{font-size:clamp(1.02rem,5.25vw,1.28rem);letter-spacing:-.015em}
  .brand-sub{font-size:.62rem;}
  .brand-illustration{height:70px;max-width:47px}
}

/* ---------- títulos de seção ---------- */
.sec{display:flex;align-items:center;gap:.7rem;margin:0 0 1rem}
.sec-n{display:inline-grid;place-items:center;width:2.1rem;height:2.1rem;border-radius:50%;flex:none;
  background:var(--accent);color:var(--ink);font-family:"Gochi Hand",cursive;font-size:1.3rem;line-height:1}
.sec-mark{display:block;width:.42rem;height:1.55rem;border-radius:5px;flex:none;
  background:var(--accent);box-shadow:0 0 12px color-mix(in srgb,var(--accent) 38%,transparent)}
.sec-t{font-family:"Gochi Hand",cursive;font-size:1.75rem;letter-spacing:.03em;color:#fff;line-height:1.1}
.side-t{display:flex;align-items:center;gap:.5rem;margin:1.3rem 0 .45rem;color:var(--accent);
  font-family:"Gochi Hand",cursive;font-size:1.5rem;letter-spacing:.04em}
.side-t::before{content:"";width:.6rem;height:.6rem;border-radius:50%;background:var(--accent);flex:none}

/* Painel de configuração da folha: título sem marcador e com quebra responsiva. */
.sheet-config-title{display:block;max-width:100%;margin:.15rem 0 .55rem;color:#fff;
  font-family:"Gochi Hand",cursive;font-size:clamp(1.02rem,1.35vw,1.28rem);
  line-height:1.12;letter-spacing:.025em;white-space:normal;overflow-wrap:anywhere}
/* Centraliza o canvas dentro do painel, mantendo 760 px quando houver espaço. */
.st-key-sheet_canvas_panel [data-testid="stImage"]{display:flex;width:100%;justify-content:center;align-items:center}
.st-key-sheet_canvas_panel [data-testid="stImage"] img{display:block;width:760px !important;
  max-width:100% !important;height:auto !important;margin-inline:auto;object-fit:contain}

/* ---------- legenda das guias ---------- */
.legend{display:grid;gap:.5rem;margin:.3rem 0 .5rem}
.lg{display:flex;align-items:center;gap:.65rem;font-size:.92rem;color:#E8E8EE}
.lg i{margin-left:auto;font-style:normal;color:var(--muted);font-variant-numeric:tabular-nums}
.lg-sw{width:1.05rem;height:1.05rem;border-radius:50%;border-width:2px;flex:none}

/* ---------- cartões e estação de edição ---------- */
.st-key-card_left,.st-key-card_right,.st-key-card_preview{background:var(--panel);border:1px solid var(--line);
  border-radius:20px;padding:1.25rem 1.25rem 1.1rem;box-shadow:0 14px 40px rgba(0,0,0,.30);min-width:0}
/* A coluna de ferramentas rola por conta própria. A prévia ao lado permanece
   parada e visível enquanto o usuário percorre os controles. */
.st-key-card_left{position:sticky;top:.75rem;max-height:calc(100vh - 1.5rem);overflow-y:auto;
  overflow-x:hidden;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:#426488 transparent}
.st-key-card_left::-webkit-scrollbar{width:8px}
.st-key-card_left::-webkit-scrollbar-thumb{background:#426488;border:2px solid transparent;border-radius:10px;background-clip:padding-box}
.st-key-card_left .sec{margin-bottom:.7rem}
.st-key-artwork_editor_tool_section{width:100%;margin-top:.25rem}
.st-key-artwork_editor_tool_section [data-testid="stSegmentedControl"]{width:100%}
.st-key-artwork_editor_tool_section [role="radiogroup"]{width:100%;display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));gap:.18rem}
.st-key-artwork_editor_tool_section [role="radio"]{min-width:0;padding:.5rem .2rem;
  font-size:.8rem;font-weight:700;white-space:normal;text-align:center}
.st-key-card_left [data-testid="stVerticalBlock"]{gap:.72rem}
.st-key-card_left [data-testid="stCaptionContainer"] p{line-height:1.45}
.st-key-card_preview{min-width:0}
.st-key-card_preview .sec{margin-bottom:.85rem}
.st-key-card_preview [data-testid="stElementContainer"]{min-width:0}
.st-key-card_right [data-testid="stImage"] img{box-shadow:0 12px 34px rgba(0,0,0,.55)}
[data-testid="stImage"] img{border-radius:12px}

/* Legenda da prévia em uma faixa horizontal, sem consumir uma coluna inteira. */
.guide-compact-legend{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.5rem;
  margin:.65rem 0 .2rem;padding:.7rem .8rem;border:1px solid var(--line);border-radius:13px;
  background:rgba(8,20,36,.52);min-width:0}
.guide-compact-item{display:grid;grid-template-columns:25px minmax(0,1fr);gap:.1rem .45rem;align-items:center;min-width:0}
.guide-compact-line{display:block;width:23px;height:0;border-top-width:3px;grid-row:1 / span 2}
.guide-compact-label{font-size:.82rem;font-weight:800;color:#F2F6FB;line-height:1.1}
.guide-compact-description{font-size:.67rem;color:var(--muted);line-height:1.25;overflow-wrap:anywhere}

/* Os rótulos dos botões quebram em mais de uma linha quando necessário.
   Isso evita texto truncado em telas menores ou em colunas estreitas. */
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button,
[data-testid="stFormSubmitButton"] button{height:auto !important;min-height:2.55rem;white-space:normal !important;
  overflow:visible;padding:.55rem .75rem;min-width:0}
[data-testid="stButton"] button [data-testid="stMarkdownContainer"],
[data-testid="stDownloadButton"] button [data-testid="stMarkdownContainer"],
[data-testid="stFormSubmitButton"] button [data-testid="stMarkdownContainer"]{width:100%;min-width:0;white-space:normal !important}
[data-testid="stButton"] button [data-testid="stMarkdownContainer"] p,
[data-testid="stDownloadButton"] button [data-testid="stMarkdownContainer"] p,
[data-testid="stFormSubmitButton"] button [data-testid="stMarkdownContainer"] p{margin:0;white-space:normal !important;
  overflow-wrap:anywhere;word-break:normal;line-height:1.22;text-align:center}
@media(max-width:900px){
  .st-key-card_left{position:relative;top:auto;max-height:none;overflow:visible}
  .guide-compact-legend{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:560px){
  .st-key-card_left,.st-key-card_right,.st-key-card_preview{padding:.9rem;border-radius:15px}
  .st-key-artwork_editor_tool_section [role="radio"]{font-size:.73rem;padding-inline:.15rem}
  .guide-compact-description{font-size:.63rem}
}

/* ---------- widgets ---------- */
[data-testid="stFileUploaderDropzone"]{border:2px dashed var(--c);background:rgba(27,169,216,.07);
  border-radius:16px;transition:background .15s,border-color .15s}
[data-testid="stFileUploaderDropzone"]:hover{background:rgba(27,169,216,.15);border-color:var(--m)}
[data-testid="stBaseButton-primary"]{border:0;font-weight:700;letter-spacing:.02em;
  box-shadow:0 6px 18px rgba(238,29,126,.28);transition:transform .12s,box-shadow .12s}
[data-testid="stBaseButton-primary"]:disabled{box-shadow:none;transform:none}
[data-testid="stBaseButton-primary"]:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 10px 26px rgba(238,29,126,.45)}
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]{border:1px solid var(--c);color:var(--c)}
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover{background:rgba(27,169,216,.12)}
[data-testid="stMetricValue"]{font-family:"Gochi Hand",cursive;color:var(--c);font-size:2.3rem}
[data-testid="stMetricLabel"]{color:var(--muted)}

/* ---------- rodapé legal ---------- */
.legal-footer{border-top:1px solid var(--line);margin:3rem auto .7rem;padding:1.25rem .8rem .2rem;
  color:var(--muted);text-align:center;font-size:.78rem;line-height:1.6}
.legal-footer .foot-brand{display:flex;align-items:baseline;justify-content:center;gap:.35rem;flex-wrap:wrap;
  margin-bottom:.25rem;color:#D8D8E0}
.legal-footer .foot-brand-name{font-family:"Gochi Hand",cursive;font-weight:400;letter-spacing:.035em;
  font-size:1.55rem;line-height:1.1;color:#fff}
.legal-footer .foot-copy{max-width:1050px;margin:.2rem auto}
.legal-footer .foot-copy strong{color:#E6E6ED;font-weight:600}
"""


@lru_cache(maxsize=None)
def _icon_data_uri(px: int) -> str:
    im = Image.open(ICON_FILE).convert("RGBA").resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@lru_cache(maxsize=1)
def _sidebar_logo_data_uri() -> str:
    """Logo principal enviada pelo usuário, preservando a transparência PNG."""
    if not SIDEBAR_LOGO_FILE.exists():
        return _icon_data_uri(512)
    return "data:image/png;base64," + base64.b64encode(SIDEBAR_LOGO_FILE.read_bytes()).decode()


@lru_cache(maxsize=1)
def _header_illustration_data_uri() -> str:
    """Ilustração de química usada à direita de LAB no cabeçalho."""
    if not HEADER_ILLUSTRATION_FILE.exists():
        return _sidebar_logo_data_uri()
    return "data:image/png;base64," + base64.b64encode(HEADER_ILLUSTRATION_FILE.read_bytes()).decode()


@lru_cache(maxsize=None)
def _css() -> str:
    css = _CSS
    if FONT_FILE.exists():
        font_b64 = base64.b64encode(FONT_FILE.read_bytes()).decode()
    else:
        # Sem o arquivo local, carrega a família Gochi Hand do Google Fonts.
        # Isso mantém a identidade tipográfica sem empacotar/redistribuir o TTF.
        font_b64 = ""
        font_start = css.find('@font-face{font-family:"Gochi Hand"')
        font_end = css.find("\n\n:root", font_start)
        if font_start >= 0 and font_end > font_start:
            css = css[:font_start] + css[font_end + 2:]
        css = '@import url("https://fonts.googleapis.com/css2?family=Gochi+Hand&display=swap");\n' + css
    for token, value in {
        "__FONT__": font_b64, "__C__": CYAN, "__M__": MAGENTA, "__Y__": YELLOW,
        "__INK__": INK, "__PANEL__": PANEL, "__LINE__": LINE, "__MUTED__": MUTED,
    }.items():
        css = css.replace(token, value)
    return css


def icon_image() -> Image.Image:
    """Ícone para a aba do navegador (st.set_page_config)."""
    return Image.open(ICON_FILE).convert("RGBA").resize((128, 128), Image.LANCZOS)


def apply_theme() -> None:
    """Injeta fonte, cores e estilos da marca. Chamar logo após st.set_page_config."""
    st.markdown(f"<style>{_css()}</style>", unsafe_allow_html=True)


def render_header() -> None:
    """Cabeçalho centralizado, com a ilustração de Erlenmeyer à direita de LAB."""
    st.markdown(
        f"""<div class="brand"><div class="brand-lockup">
<div class="brand-copy">
<div class="brand-name"><span class="brand-cyan">ATÔMICA</span>
<span class="brand-magenta">PRINT</span> <span class="brand-yellow">LAB</span></div>
<div class="brand-sub">{APP_SLOGAN}</div>
</div>
<img class="brand-illustration" src="{_header_illustration_data_uri()}" alt="Erlenmeyer e elementos de química" />
</div></div>
<div class="brand-rule"></div>""",
        unsafe_allow_html=True,
    )


def render_sidebar_logo() -> None:
    """Exibe a logo completa em destaque acima de INSUMOS na barra lateral."""
    st.markdown(
        f'<div class="sidebar-brand-logo"><img src="{_sidebar_logo_data_uri()}" '
        'alt="Logo Atômica Print"></div>',
        unsafe_allow_html=True,
    )


def section_title(number: int | None, text: str, accent: str) -> None:
    """Título de seção com marcador CMY; número é opcional para evitar repetição visual."""
    marker = f'<span class="sec-n">{number}</span>' if number is not None else '<span class="sec-mark" aria-hidden="true"></span>'
    st.markdown(
        f'<div class="sec" style="--accent:{accent}">{marker}'
        f'<span class="sec-t">{text}</span></div>',
        unsafe_allow_html=True,
    )


def sidebar_title(text: str, accent: str) -> None:
    st.markdown(f'<div class="side-t" style="--accent:{accent}">{text}</div>', unsafe_allow_html=True)


def render_footer() -> None:
    """Rodapé de marca e aviso legal solicitado para o Atômica Print Lab."""
    st.markdown(
        """<div class="legal-footer" role="contentinfo">
<div class="foot-brand"><span class="foot-brand-name">Atômica Print</span>
<span>— Copyright © 2026. Todos os direitos reservados.</span></div>
<div class="foot-copy">É proibida qualquer reprodução, modificação ou distribuição sem autorização prévia.</div>
<div class="foot-copy">Software protegido pela Lei nº 9.609/1998 e pela Lei nº 9.610/1998.</div>
</div>""",
        unsafe_allow_html=True,
    )
