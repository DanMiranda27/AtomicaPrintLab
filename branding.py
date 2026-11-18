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

import streamlit as st
from PIL import Image

ASSETS = Path(__file__).parent / "assets"
ICON_FILE = ASSETS / "icon.png"
SIDEBAR_LOGO_FILE = ASSETS / "logo-atomica-print.png"
HEADER_ILLUSTRATION_FILE = ASSETS / "erlenmeyer-lab.png"
FONT_FILE = ASSETS / "GochiHand-Regular.ttf"

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
.brand-lockup{display:flex;align-items:center;justify-content:center;gap:.7rem;max-width:100%}
.brand-copy{min-width:0;text-align:center}
.brand-name{font-family:"Gochi Hand",cursive !important;font-size:4.15rem;line-height:1.02;
  letter-spacing:.025em;color:#fff;text-transform:uppercase;white-space:nowrap}
.brand-name .brand-cyan{color:var(--c)}
.brand-name .brand-magenta{color:var(--m)}
.brand-name .brand-yellow{color:var(--y)}
.brand-sub{margin-top:.38rem;color:var(--muted);font-size:1.03rem;letter-spacing:.02em;text-align:center}
.brand-illustration{display:block;flex:none;width:auto;height:166px;max-width:118px;object-fit:contain;
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

@media(max-width:700px){
  .brand-lockup{gap:.25rem}
  .brand-name{font-size:clamp(1.45rem,6.1vw,2.6rem)}
  .brand-sub{font-size:clamp(.68rem,2.2vw,.9rem);margin-top:.28rem}
  .brand-illustration{height:clamp(88px,23vw,130px);max-width:clamp(58px,17vw,90px)}
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

/* ---------- legenda das guias ---------- */
.legend{display:grid;gap:.5rem;margin:.3rem 0 .5rem}
.lg{display:flex;align-items:center;gap:.65rem;font-size:.92rem;color:#E8E8EE}
.lg i{margin-left:auto;font-style:normal;color:var(--muted);font-variant-numeric:tabular-nums}
.lg-sw{width:1.05rem;height:1.05rem;border-radius:50%;border-width:2px;flex:none}

/* ---------- cartões ---------- */
.st-key-card_left,.st-key-card_right{background:var(--panel);border:1px solid var(--line);
  border-radius:20px;padding:1.5rem 1.5rem 1.3rem;box-shadow:0 14px 40px rgba(0,0,0,.35)}
[data-testid="stImage"] img{border-radius:12px}
.st-key-card_right [data-testid="stImage"] img{box-shadow:0 12px 34px rgba(0,0,0,.55)}

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
