# server.py
from __future__ import annotations

import os
import sys
import uuid
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Dict, Literal
from pydantic import conlist
from fastapi import APIRouter
import json
import html as html_escape  # para escapar textos en el HTML
import base64


IS_WINDOWS = sys.platform.startswith("win")

# --- Playwright imports y tipos según SO ---
if IS_WINDOWS:
    # Windows: API síncrona (la corremos en un hilo)
    from playwright.sync_api import (
        sync_playwright,
        Browser as SyncBrowser,
        Playwright as SyncPlaywright,
    )
    _sync_pw: Optional[SyncPlaywright] = None
    _sync_browser: Optional[SyncBrowser] = None
else:
    # Linux/Mac: API asíncrona
    from playwright.async_api import (
        async_playwright,
        Browser as AsyncBrowser,
        Playwright as AsyncPlaywright,
    )
    _pw: Optional[AsyncPlaywright] = None
    _browser: Optional[AsyncBrowser] = None


# ====== CATÁLOGO DE OFERTAS (servidor) ======
# Estructura:
# OFFERS[clave]["label"]
# OFFERS[clave]["tarifas"][TARIFA]["potencia"][P1..P6]  -> €/kW·año
# OFFERS[clave]["tarifas"][TARIFA]["energia"][E1..E6]   -> €/kWh
OFFERS = {
    "PELITO_ECO": {
        "label": "Tarifa Pelito Eco",
        "tarifas": {
            "2.0TD": {
                "potencia": {"P1": 38.93, "P2": 20.69},
                "energia": {"E1": 0.098155, "E2": 0.098155, "E3": 0.098155}
            },
            # "3.0TD": { "potencia": { "P1": 20.76988, "P2": 14.781919, "P3": 8.005384, "P4": 7.106183, "P5": 5.399377, "P6": 3.63993 },
            #           "energia":  { "E1": 0.128, "E2": 0.128, "E3": 0.110, "E4": 0.110, "E5": 0.105, "E6": 0.110 } },
            # "6.1TD": { ... }
        }
    },
    "VERSATIL": {
        "label": "Tarifa Versátil",
        "tarifas": {
            "2.0TD": {
                "potencia": {"P1": 34.17266, "P2": 3.124359},
                "energia": {"E1": 0.170, "E2": 0.123, "E3": 0.123}
            },
            # "3.0TD": { "potencia": { "P1": 20.76988, "P2": 14.781919, "P3": 8.005384, "P4": 7.106183, "P5": 5.399377, "P6": 3.63993 },
            #           "energia":  { "E1": 0.170, "E2": 0.123, "E3": 0.123, "E4": 0.105, "E5": 0.105, "E6": 0.105 } },
        }
    },
    "PERSONALIZADA": {
        "label": "Tarifa Personalizada",
        "tarifas": {
            "2.0TD": {
                "potencia": {"P1": 34.67266, "P2": 4.424359},
                "energia": {"E1": 0.15891, "E2": 0.15891, "E3": 0.15891}
            }
        }
    }
}

async def _render_html_to_pdf(html: str) -> bytes:
    """Convierte HTML en PDF usando el navegador ya abierto (Windows/Linux)."""
    if IS_WINDOWS:
        def _render_sync(html: str) -> bytes:
            page = _sync_browser.new_page()
            page.set_content(html, wait_until="networkidle")
            pdf = page.pdf(format="A4", print_background=True, margin={"top": "12mm", "bottom": "15mm", "left": "12mm", "right": "12mm"})
            page.close()
            return pdf
        return await asyncio.to_thread(_render_sync, html)
    else:
        page = await _browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        pdf = await page.pdf(format="A4", print_background=True, margin={"top": "12mm", "bottom": "15mm", "left": "12mm", "right": "12mm"})
        await page.close()
        return pdf
    

def _img_b64(path: Path, mime: str = "image/png") -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

def _build_report_html(info: SuministroInfo, res: CompareResult) -> str:
    titulo = f"Informe comparativa — {res.tarifa}"
    cliente   = html_escape.escape(info.nombre_cliente or "")
    direccion = html_escape.escape(info.direccion)
    poblacion = html_escape.escape(info.poblacion)
    cif       = html_escape.escape(info.cif)
    cups      = html_escape.escape(info.cups)
    fecha     = html_escape.escape(info.fecha_estudio)

    ah_pct, ah_eur, ah_mes = res.ahorro_pct, res.ahorro_anual, res.ahorro_mensual

    # logo inline (base64). Si no existe, no se muestra.
    logo_src = _img_b64(TEMPLATES_DIR / "logo_openenergies.png")

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>{titulo}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"></script>
  <style>
    :root {{
      --ink:#0b1324;
      --muted:#64748b;
      --border:#e2e8f0;
      --bg:#ecfdf5;
      --brand:#10b981;
      --brand-strong:#059669;
      --brand-weak:#d1fae5;
    }}
    * {{ box-sizing:border-box }}
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; color:var(--ink); background:#fff; }}
    .wrap {{ padding: 22px 24px; }}
    .header {{
      display:flex; justify-content:space-between; align-items:center; gap:16px;
      border:1px solid var(--border); border-radius:16px;
      background:linear-gradient(180deg, var(--bg), #ffffff);
      padding:10px 14px;
    }}
    .brand {{ font-weight:800; font-size:18px; color:var(--brand); letter-spacing:.2px }}
    .h1 {{ font-size:22px; font-weight:800; margin:4px 0 2px }}
    .muted {{ color:var(--muted); font-size:12px }}
    .pill {{
      display:inline-block; background:var(--brand-weak); color:var(--brand-strong);
      padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800
    }}
    .card {{ border:1px solid var(--border); border-radius:16px; background:#fff; padding:14px; margin-top:12px; }}
    .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    .kpi {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .kpi .box {{ border:1px solid var(--border); border-radius:12px; padding:10px 12px; background:#fff; }}
    .kpi .label {{ font-size:12px; color:var(--muted) }}
    .kpi .val {{ font-size:18px; font-weight:800 }}
    table {{ width:100%; border-collapse:separate; border:1px solid var(--border); border-radius:12px; overflow:hidden; border-spacing:0; }}
    th, td {{ padding:8px 10px; border-bottom:1px solid var(--border); font-size:12px; text-align:left; }}
    th {{ background:#f8fafc; font-weight:700 }}
    tr:last-child td {{ border-bottom:0 }}
    .logo {{ height:56px; width:auto; object-fit:contain; margin-right:10px }}
    .head-left {{ display:flex; align-items:center; gap:12px }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="head-left">
        {f'<img class="logo" src="{logo_src}" alt="Logo" />' if logo_src else ''}
        <div>
          <div class="brand">Open Energies</div>
          <div class="h1">{titulo}</div>
          <div class="muted">Fecha de estudio: {fecha}</div>
        </div>
      </div>
      <div style="text-align:right">
        <div class="pill">{ah_pct}% ahorro</div>
        <div class="muted" style="margin-top:6px">Ahorro mes: <b>{ah_mes} €</b> · Ahorro año: <b>{ah_eur} €</b></div>
      </div>
    </div>

    <div class="card">
      <div class="grid2">
        <div>
          <div style="font-weight:700; margin-bottom:4px">Datos del suministro</div>
          <div class="muted">{cliente}</div>
          <div class="muted">{direccion}</div>
          <div class="muted">{poblacion}</div>
          <div class="muted">CUPS: {cups}</div>
          <div class="muted">CIF: {cif}</div>
        </div>
        <div class="kpi">
          <div class="box"><div class="label">Total anual (Actual)</div><div class="val">{res.actual.total_anual} €</div></div>
          <div class="box"><div class="label">Total anual (Propuesta)</div><div class="val">{res.propuesta.total_anual} €</div></div>
          <div class="box"><div class="label">Ahorro anual</div><div class="val">{res.ahorro_anual} €</div></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div style="font-weight:700; margin-bottom:8px">Desglose por conceptos</div>
      <table>
        <thead>
          <tr><th>Concepto</th><th>Actual</th><th>Propuesta</th></tr>
        </thead>
        <tbody>
          <tr><td>Potencia</td><td>{res.actual.potencia_anual} €</td><td>{res.propuesta.potencia_anual} €</td></tr>
          <tr><td>Energía</td><td>{res.actual.energia_anual} €</td><td>{res.propuesta.energia_anual} €</td></tr>
          <tr><td>Fijos</td><td>{res.actual.cargos_fijos_anual} €</td><td>{res.propuesta.cargos_fijos_anual} €</td></tr>
          <tr><td>Impuesto electricidad</td><td>{res.actual.impuesto_electricidad} €</td><td>{res.propuesta.impuesto_electricidad} €</td></tr>
          <tr><td>IVA</td><td>{res.actual.iva} €</td><td>{res.propuesta.iva} €</td></tr>
          <tr><th>Total</th><th>{res.actual.total_anual} €</th><th>{res.propuesta.total_anual} €</th></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="muted" style="margin-bottom:6px">Totales anuales y ahorro</div>
      <canvas id="chartTotals" width="560" height="340"></canvas>
    </div>
  </div>

  <!-- SCRIPT del gráfico con margen y etiquetas controladas -->
  <script>
    const totalActual = {res.actual.total_anual};
    const totalProp   = {res.propuesta.total_anual};
    const ahorro      = {res.ahorro_anual};
    const maxVal = Math.max(totalActual, totalProp, Math.abs(ahorro));

    Chart.register(ChartDataLabels);

    new Chart(document.getElementById('chartTotals'), {{
      type: 'bar',
      data: {{
        labels: ['Actual', 'Propuesta', 'Ahorro'],
        datasets: [{{
          label: '€',
          data: [totalActual, totalProp, Math.abs(ahorro)],
          backgroundColor: ['#86efac', '#34d399', (ahorro >= 0 ? '#10b981' : '#ef4444')],
          borderColor:    ['#16a34a', '#059669', (ahorro >= 0 ? '#065f46' : '#b91c1c')],
          borderWidth: 1.5
        }}]
      }},
      options: {{
        responsive: true,
        layout: {{ padding: {{ top: 16 }} }},
        plugins: {{
          legend: {{ display: false }},
          datalabels: {{
            anchor: 'end', align: 'end', offset: -2, clamp: true, clip: false,
            formatter: (v, ctx) => {{
              const isAhorro = ctx.dataIndex === 2;
              const label = isAhorro ? (ahorro >= 0 ? '' : 'Sobre-coste ') : '';
              return label + v.toLocaleString('es-ES', {{ minimumFractionDigits: 2 }}) + ' €';
            }},
            color: '#064e3b', font: {{ weight: '700' }}
          }}
        }},
        scales: {{
          y: {{ beginAtZero: true, suggestedMax: maxVal * 1.18, grace: '12%' }}
        }}
      }}
    }});
  </script>
</body>
</html>"""


# ---------------------------
# Config básica
# ---------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
TEMPLATES_DIR = BASE_DIR / "templates"

MAX_UPLOAD_MB = 15
ALLOWED_MIME = {"application/pdf"}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Open Energies PDF Service", version="0.1.0")
app.mount("/static", StaticFiles(directory=TEMPLATES_DIR), name="static")

@app.get("/offers")
async def list_offers():
    return OFFERS

# ---------------------------
# Modelos
# ---------------------------
class Margenes(BaseModel):
    superior: str = "15mm"
    inferior: str = "15mm"
    izquierda: str = "12mm"
    derecha: str = "12mm"

class Opciones(BaseModel):
    formato: str = "A4"
    imprimirFondo: bool = True
    escala: float = 1.0
    margenes: Margenes = Field(default_factory=Margenes)
    incluirPie: bool = True
    pieHtml: str = (
        "<div style='font-size:10px;width:100%;text-align:center;'>"
        "Página <span class='pageNumber'></span> de <span class='totalPages'></span>"
        "</div>"
    )
    esperarRedCompleta: bool = True

class PeticionRender(BaseModel):
    html: str
    opciones: Opciones = Field(default_factory=Opciones)

# ---------------------------
# Utilidades
# ---------------------------
def _bytes_to_mb(n: int) -> float:
    return n / (1024 * 1024)

def _safe_filename(original_name: str) -> str:
    ext = "".join(Path(original_name).suffixes).lower() or ".pdf"
    if ext != ".pdf":
        ext = ".pdf"
    return f"{uuid.uuid4().hex}{ext}"

# ---------------------------
# Ciclo de vida
# ---------------------------
@app.on_event("startup")
async def startup() -> None:
    """Arranca Playwright/Chromium según plataforma."""
    global _pw, _browser, _sync_pw, _sync_browser

    if IS_WINDOWS:
        # Arrancamos síncrono en un hilo para no pelear con asyncio en Windows
        def _start_sync():
            global _sync_pw, _sync_browser
            _sync_pw = sync_playwright().start()
            _sync_browser = _sync_pw.chromium.launch()
        await asyncio.to_thread(_start_sync)
        print("Chromium (sync) listo en Windows.")
    else:
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(args=["--no-sandbox"])
        print("Chromium (async) listo.")

@app.on_event("shutdown")
async def shutdown() -> None:
    """Cierra navegadores correctamente."""
    global _pw, _browser, _sync_pw, _sync_browser

    if IS_WINDOWS:
        def _stop_sync():
            global _sync_pw, _sync_browser
            try:
                if _sync_browser:
                    _sync_browser.close()
                if _sync_pw:
                    _sync_pw.stop()
            finally:
                _sync_browser = None
                _sync_pw = None
        await asyncio.to_thread(_stop_sync)
    else:
        if _browser:
            await _browser.close()
            _browser = None
        if _pw:
            await _pw.stop()
            _pw = None

# ---------------------------
# Rutas
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Falta templates/index.html")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))

@app.post("/upload")
async def upload(documento: UploadFile = File(...)) -> dict:
    if documento.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail=f"Tipo no permitido: {documento.content_type}")

    data = await documento.read()
    size_mb = _bytes_to_mb(len(data))
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande ({size_mb:.1f} MB). Límite {MAX_UPLOAD_MB} MB.")

    safe_name = _safe_filename(documento.filename or "documento.pdf")
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(data)
    return {"ok": True, "mensaje": "Documento subido correctamente.", "archivo": safe_name, "tamano_mb": round(size_mb, 2)}

@app.post("/render")
async def render_api(payload: PeticionRender) -> Response:
    if not payload.html or len(payload.html) < 16:
        raise HTTPException(status_code=400, detail="HTML vacío o demasiado corto.")

    if IS_WINDOWS:
        # Render síncrono en un hilo
        def _render_sync(html: str, opt: Opciones) -> bytes:
            page = _sync_browser.new_page()
            page.set_content(html, wait_until="networkidle" if opt.esperarRedCompleta else "domcontentloaded")
            pdf_bytes = page.pdf(
                format=opt.formato,
                print_background=opt.imprimirFondo,
                scale=opt.escala,
                margin={
                    "top": opt.margenes.superior,
                    "bottom": opt.margenes.inferior,
                    "left": opt.margenes.izquierda,
                    "right": opt.margenes.derecha,
                },
                display_header_footer=opt.incluirPie,
                header_template="<div></div>",
                footer_template=opt.pieHtml if opt.incluirPie else "<div></div>",
            )
            page.close()
            return pdf_bytes

        pdf_bytes = await asyncio.to_thread(_render_sync, payload.html, payload.opciones)
    else:
        # Render asíncrono normal
        page = await _browser.new_page()
        wait_until = "networkidle" if payload.opciones.esperarRedCompleta else "domcontentloaded"
        await page.set_content(payload.html, wait_until=wait_until)
        pdf_bytes = await page.pdf(
            format=payload.opciones.formato,
            print_background=payload.opciones.imprimirFondo,
            scale=payload.opciones.escala,
            margin={
                "top": payload.opciones.margenes.superior,
                "bottom": payload.opciones.margenes.inferior,
                "left": payload.opciones.margenes.izquierda,
                "right": payload.opciones.margenes.derecha,
            },
            display_header_footer=payload.opciones.incluirPie,
            header_template="<div></div>",
            footer_template=payload.opciones.pieHtml if payload.opciones.incluirPie else "<div></div>",
        )
        await page.close()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename=\"documento.pdf\"'}
    )

@app.post("/render-form")
async def render_form(html: str = Form(...)) -> Response:
    payload = PeticionRender(html=html)
    return await render_api(payload)

# >>> COMPARATIVA (nuevo) >>>
TarifaLiteral = Literal["2.0TD", "3.0TD", "6.1TD"]

TARIFF_PERIODS = {
    "2.0TD": {"potencia": ["P1", "P2"], "energia": ["E1", "E2", "E3"]},
    "3.0TD": {"potencia": ["P1", "P2", "P3", "P4", "P5", "P6"], "energia": ["E1", "E2", "E3", "E4", "E5", "E6"]},
    "6.1TD": {"potencia": ["P1", "P2", "P3", "P4", "P5", "P6"], "energia": ["E1", "E2", "E3", "E4", "E5", "E6"]},
}



class PlanInput(BaseModel):
    nombre: str = "Plan"
    # €/kW año por periodo de potencia
    precio_potencia: Dict[str, float]
    # €/kWh por periodo de energía
    precio_energia: Dict[str, float]
    # Extras fijos anuales (alquiler contador, etc.)
    cargos_fijos_anual_eur: float = 0.0

class CompareInput(BaseModel):
    tarifa: TarifaLiteral
    energia_kwh: Dict[str, float]
    potencia_contratada_kw: Dict[str, float]
    potencia_facturada_kw: Dict[str, float] | None = None

    actual: PlanInput
    propuesta: PlanInput

    impuesto_electricidad_pct: float = 0.05112
    iva_pct: float = 0.21

    # 🔧 NUEVO: opciones de cálculo
    unidad_precio_potencia: Literal["eur_kw_anio", "eur_kw_dia"] = "eur_kw_anio"
    aplicar_ie_solo_a_pot_y_energia: bool = True
    redondeo_por_linea: bool = True
    redondear_impuestos: bool = True

class SuministroInfo(BaseModel):
    direccion: str
    cif: str
    fecha_estudio: str
    poblacion: str
    cups: str
    nombre_cliente: str | None = None  # opcional

class CompareReportInput(CompareInput):
    suministro: SuministroInfo

class BillBreakdown(BaseModel):
    potencia_anual: float
    energia_anual: float
    cargos_fijos_anual: float
    impuesto_electricidad: float
    iva: float
    total_anual: float
    total_mensual: float

class CompareResult(BaseModel):
    tarifa: TarifaLiteral
    actual: BillBreakdown
    propuesta: BillBreakdown
    ahorro_anual: float
    ahorro_mensual: float
    ahorro_pct: float

def _round2(n: float) -> float:
    return round(float(n), 2)

def _validate_periods(tarifa: str, energia_kwh: Dict[str, float], pot_contr: Dict[str, float], pot_fact: Dict[str, float] | None, plan_a: PlanInput, plan_b: PlanInput):
    cfg = TARIFF_PERIODS[tarifa]
    def _chk(keys: Dict[str, float], must: list[str], name: str):
        desconocidos = [k for k in keys.keys() if k not in must]
        faltantes = [k for k in must if k not in keys]
        if desconocidos:
            raise HTTPException(400, detail=f"{name}: periodos no válidos: {desconocidos}. Deben ser {must}")
        if faltantes:
            raise HTTPException(400, detail=f"{name}: faltan periodos: {faltantes}. Deben ser {must}")

    _chk(energia_kwh, cfg["energia"], "energia_kwh")
    _chk(pot_contr, cfg["potencia"], "potencia_contratada_kw")
    if pot_fact is not None:
        _chk(pot_fact, cfg["potencia"], "potencia_facturada_kw")
    _chk(plan_a.precio_energia, cfg["energia"], "actual.precio_energia")
    _chk(plan_b.precio_energia, cfg["energia"], "propuesta.precio_energia")
    _chk(plan_a.precio_potencia, cfg["potencia"], "actual.precio_potencia")
    _chk(plan_b.precio_potencia, cfg["potencia"], "propuesta.precio_potencia")

def _compute_bill(
    tarifa: str,
    energia_kwh: Dict[str, float],
    pot_contr: Dict[str, float],
    pot_fact: Dict[str, float] | None,
    plan: PlanInput,
    imp_elec_pct: float,
    iva_pct: float,
    unidad_precio_potencia: str = "eur_kw_anio",
    aplicar_ie_solo_a_pot_y_energia: bool = True,
    redondeo_por_linea: bool = True,
    redondear_impuestos: bool = True,
) -> BillBreakdown:
    cfg = TARIFF_PERIODS[tarifa]
    pot_base = pot_fact or pot_contr

    factor_pot = 365.0 if unidad_precio_potencia == "eur_kw_dia" else 1.0

    # Potencia por periodo
    pot_importes = []
    for p in cfg["potencia"]:
        importe = float(pot_base.get(p, 0.0)) * float(plan.precio_potencia.get(p, 0.0)) * factor_pot
        pot_importes.append(round(importe, 2) if redondeo_por_linea else importe)
    pot_total = sum(pot_importes)

    # Energía por periodo
    ene_importes = []
    for e in cfg["energia"]:
        importe = float(energia_kwh.get(e, 0.0)) * float(plan.precio_energia.get(e, 0.0))
        ene_importes.append(round(importe, 2) if redondeo_por_linea else importe)
    ene_total = sum(ene_importes)

    base = float(pot_total) + float(ene_total) + float(plan.cargos_fijos_anual_eur)

    base_ie = (float(pot_total) + float(ene_total)) if aplicar_ie_solo_a_pot_y_energia else base
    imp_elec_calc = base_ie * float(imp_elec_pct)
    imp_elec = round(imp_elec_calc, 2) if redondear_impuestos else imp_elec_calc

    iva_calc = (base + imp_elec) * float(iva_pct)
    iva = round(iva_calc, 2) if redondear_impuestos else iva_calc

    total = base + imp_elec + iva

    return BillBreakdown(
        potencia_anual=_round2(pot_total),
        energia_anual=_round2(ene_total),
        cargos_fijos_anual=_round2(plan.cargos_fijos_anual_eur),
        impuesto_electricidad=_round2(imp_elec),
        iva=_round2(iva),
        total_anual=_round2(total),
        total_mensual=_round2(total / 12.0),
    )

@app.post("/compare", response_model=CompareResult)
async def compare(payload: CompareInput) -> CompareResult:
    """Calcula totales 'actual' vs 'propuesta' y devuelve ahorros."""
    _validate_periods(
        payload.tarifa,
        payload.energia_kwh,
        payload.potencia_contratada_kw,
        payload.potencia_facturada_kw,
        payload.actual,
        payload.propuesta,
    )

    actual = _compute_bill(
        payload.tarifa,
        payload.energia_kwh,
        payload.potencia_contratada_kw,
        payload.potencia_facturada_kw,
        payload.actual,
        payload.impuesto_electricidad_pct,
        payload.iva_pct,
        payload.unidad_precio_potencia,
        payload.aplicar_ie_solo_a_pot_y_energia,
        payload.redondeo_por_linea,
        payload.redondear_impuestos,
    )

    propuesta = _compute_bill(
        payload.tarifa,
        payload.energia_kwh,
        payload.potencia_contratada_kw,
        payload.potencia_facturada_kw,
        payload.propuesta,
        payload.impuesto_electricidad_pct,
        payload.iva_pct,
        payload.unidad_precio_potencia,
        payload.aplicar_ie_solo_a_pot_y_energia,
        payload.redondeo_por_linea,
        payload.redondear_impuestos,
    )

    ahorro_anual = _round2(actual.total_anual - propuesta.total_anual)
    ahorro_mensual = _round2(ahorro_anual / 12.0)
    ahorro_pct = _round2( (ahorro_anual / actual.total_anual) * 100.0 if actual.total_anual > 0 else 0.0 )

    return CompareResult(
        tarifa=payload.tarifa,
        actual=actual,
        propuesta=propuesta,
        ahorro_anual=ahorro_anual,
        ahorro_mensual=ahorro_mensual,
        ahorro_pct=ahorro_pct,
    )
# <<< COMPARATIVA (nuevo) <<<

@app.post("/compare-report")
async def compare_report(payload: CompareReportInput):
    """
    Calcula la comparativa y devuelve un PDF con cabecera + tablas + gráficos.
    """
    # 1) Reutiliza el cálculo existente
    res = await compare(payload)  # -> CompareResult

    # 2) Construye el HTML del informe
    html = _build_report_html(payload.suministro, res)

    # 3) Renderiza a PDF
    pdf_bytes = await _render_html_to_pdf(html)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="informe.pdf"'}
    )