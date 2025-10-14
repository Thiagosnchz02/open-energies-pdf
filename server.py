# server.py (versión final refactorizada)
from __future__ import annotations

import os
import asyncio
from pathlib import Path
from typing import Dict, Literal, Optional

from fastapi import FastAPI, Response, Depends, Security, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import html
import io
import base64

# Importaciones para gráficos
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

# Importaciones para PDF con Playwright
from playwright.async_api import async_playwright, Browser as AsyncBrowser, Playwright as AsyncPlaywright

# ===================== CONFIGURACIÓN Y SEGURIDAD =====================

INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN")
api_key_header = APIKeyHeader(name="X-Internal-Auth-Token", auto_error=True)

async def get_api_key(api_key: str = Security(api_key_header)):
    """Dependencia de FastAPI para validar el token de autenticación."""
    if not INTERNAL_API_TOKEN or api_key != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

_pw: Optional[AsyncPlaywright] = None
_browser: Optional[AsyncBrowser] = None

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(
    title="Open Energies PDF Generation Service",
    version="1.1.0",
    description="Microservicio para generar comparativas energéticas en PDF.",
)

# ===================== MODELOS DE DATOS (PYDANTIC) =====================

TarifaLiteral = Literal["2.0TD", "3.0TD", "6.1TD"]

TARIFF_PERIODS = {
    "2.0TD": {"potencia": ["P1", "P2"], "energia": ["E1", "E2", "E3"]},
    "3.0TD": {"potencia": ["P1", "P2", "P3", "P4", "P5", "P6"], "energia": ["E1", "E2", "E3", "E4", "E5", "E6"]},
    "6.1TD": {"potencia": ["P1", "P2", "P3", "P4", "P5", "P6"], "energia": ["E1", "E2", "E3", "E4", "E5", "E6"]},
}

class PlanInput(BaseModel):
    nombre: str = "Plan"
    precio_potencia: Dict[str, float]
    precio_energia: Dict[str, float]
    cargos_fijos_anual_eur: float = 0.0

class SuministroInfo(BaseModel):
    direccion: str
    cif: str
    fecha_estudio: str
    poblacion: str
    cups: str
    nombre_cliente: Optional[str] = None

class MonthlyInput(BaseModel):
    tarifa: TarifaLiteral
    energia_kwh_mes: Dict[str, list[float]]
    potencia_contratada_kw: Dict[str, float]
    actual: PlanInput
    propuesta: PlanInput
    iva_pct: float = 0.21
    impuesto_electricidad_pct: float = 0.05112
    suministro: Optional[SuministroInfo] = None

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

class MonthlyResult(BaseModel):
    meses: list[str]
    energia_actual: list[float]
    potencia_actual: list[float]
    energia_propuesta: list[float]
    potencia_propuesta: list[float]
    impuestos_actual: list[float]
    impuestos_propuesta: list[float]
    resumen: CompareResult

DEFAULT_MESES = ["ENE","FEB","MAR","ABR","MAY","JUN","JUL","AGO","SEP","OCT","NOV","DIC"]


# ===================== LÓGICA DE NEGOCIO Y UTILIDADES =====================

def _compute_bill(tarifa: str, energia_kwh: dict, pot_contr: dict, plan: PlanInput, ie_pct: float, iva_pct: float) -> BillBreakdown:
    cfg = TARIFF_PERIODS[tarifa]
    
    pot_total = sum(float(pot_contr.get(p, 0.0)) * float(plan.precio_potencia.get(p, 0.0)) for p in cfg["potencia"])
    ene_total = sum(float(energia_kwh.get(e, 0.0)) * float(plan.precio_energia.get(e, 0.0)) for e in cfg["energia"])
    
    # Base para el Impuesto Eléctrico (solo potencia y energía)
    base_impuestos = pot_total + ene_total
    imp_elec = base_impuestos * ie_pct
    
    # Base para el IVA (incluye todo lo anterior + cargos fijos)
    base_iva = base_impuestos + imp_elec + plan.cargos_fijos_anual_eur
    iva = base_iva * iva_pct
    
    total = base_iva + iva

    return BillBreakdown(
        potencia_anual=round(pot_total, 2),
        energia_anual=round(ene_total, 2),
        cargos_fijos_anual=round(plan.cargos_fijos_anual_eur, 2),
        impuesto_electricidad=round(imp_elec, 2),
        iva=round(iva, 2),
        total_anual=round(total, 2),
        total_mensual=round(total / 12.0, 2),
    )

# FUNCIÓN PRINCIPAL REEMPLAZADA
def compute_monthly_from_sips(inp: MonthlyInput) -> MonthlyResult:
    cfg = TARIFF_PERIODS[inp.tarifa]
    periods_e, periods_p = cfg["energia"], cfg["potencia"]

    # (Las validaciones iniciales no cambian)
    for p in periods_e:
        if p not in inp.energia_kwh_mes or len(inp.energia_kwh_mes[p]) != 12:
            raise HTTPException(400, detail=f"{p} debe tener 12 valores (1 por mes)")
    for p in periods_p:
        if p not in inp.potencia_contratada_kw:
            raise HTTPException(400, detail=f"Falta potencia contratada para {p}")

    # --- Cálculos para el gráfico mensual (sin cambios) ---
    ener_a, ener_b = [0.0]*12, [0.0]*12
    # ... (el bucle `for e in periods_e:` se mantiene igual)
    for e in periods_e:
        pe_a = float(inp.actual.precio_energia.get(e, 0.0))
        pe_b = float(inp.propuesta.precio_energia.get(e, 0.0))
        kwh_list = [float(x or 0) for x in inp.energia_kwh_mes[e]]
        for i in range(12):
            ener_a[i] += kwh_list[i] * pe_a
            ener_b[i] += kwh_list[i] * pe_b

    pot_anual_a = sum(float(inp.potencia_contratada_kw.get(p,0.0)) * float(inp.actual.precio_potencia.get(p,0.0)) for p in periods_p)
    pot_anual_b = sum(float(inp.potencia_contratada_kw.get(p,0.0)) * float(inp.propuesta.precio_potencia.get(p,0.0)) for p in periods_p)
    pot_mes_a, pot_mes_b = pot_anual_a/12.0, pot_anual_b/12.0
    pot_a, pot_b = [pot_mes_a]*12, [pot_mes_b]*12

    iva_pct, ie_pct = float(inp.iva_pct), float(inp.impuesto_electricidad_pct)
    imp_a, imp_b = [], []
    for i in range(12):
        base_a = ener_a[i] + pot_a[i]
        imp_elec_a = base_a * ie_pct
        iva_a = (base_a + imp_elec_a) * iva_pct
        imp_a.append(imp_elec_a + iva_a)

        base_b = ener_b[i] + pot_b[i]
        imp_elec_b = base_b * ie_pct
        iva_b = (base_b + imp_elec_b) * iva_pct
        imp_b.append(imp_elec_b + iva_b)
        
    # --- ¡CAMBIO CLAVE! Usamos la nueva función para un cálculo anual preciso ---
    total_kwh_anual = {e: sum(inp.energia_kwh_mes.get(e, [])) for e in periods_e}
    
    actual_bill = _compute_bill(inp.tarifa, total_kwh_anual, inp.potencia_contratada_kw, inp.actual, ie_pct, iva_pct)
    propuesta_bill = _compute_bill(inp.tarifa, total_kwh_anual, inp.potencia_contratada_kw, inp.propuesta, ie_pct, iva_pct)
    
    ahorro_anual = actual_bill.total_anual - propuesta_bill.total_anual
    ahorro_pct = (ahorro_anual / actual_bill.total_anual * 100.0) if actual_bill.total_anual > 0 else 0.0

    resumen = CompareResult(
        tarifa=inp.tarifa,
        actual=actual_bill,
        propuesta=propuesta_bill,
        ahorro_anual=round(ahorro_anual, 2),
        ahorro_mensual=round(ahorro_anual / 12.0, 2),
        ahorro_pct=round(ahorro_pct, 2),
    )

    return MonthlyResult(
        meses=DEFAULT_MESES,
        energia_actual=[round(x,2) for x in ener_a],
        potencia_actual=[round(x,2) for x in pot_a],
        energia_propuesta=[round(x,2) for x in ener_b],
        potencia_propuesta=[round(x,2) for x in pot_b],
        impuestos_actual=[round(x,2) for x in imp_a],
        impuestos_propuesta=[round(x,2) for x in imp_b],
        resumen=resumen,
    )
    

def make_monthly_bar_chart_dual(meses: list[str], ener_actual: list[float], pot_actual: list[float], imp_actual: list[float], ener_prop: list[float], pot_prop: list[float], imp_prop: list[float]) -> str:
    colores = { "imp": "#2E2E2E", "ener_actual": "#BDC3C7", "pot_actual": "#7F8C8D", "ener_prop": "#A3D9A5", "pot_prop": "#57A773" }
    width, gap, group_space = 0.50, 0.05, 1.50
    x = np.arange(12) * group_space
    left_x, right_x = x - (width/2 + gap/2), x + (width/2 + gap/2)

    impA, pa, ea = np.array(imp_actual), np.array(pot_actual), np.array(ener_actual)
    impB, pb, eb = np.array(imp_prop), np.array(pot_prop), np.array(ener_prop)

    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=False)
    ax.bar(left_x, impB, width=width, label="Impuestos", color=colores["imp"])
    ax.bar(left_x, pb, width=width, bottom=impB, label="Oferta: Potencia", color=colores["pot_prop"])
    ax.bar(left_x, eb, width=width, bottom=impB + pb, label="Oferta: Energía", color=colores["ener_prop"])
    ax.bar(right_x, impA, width=width, color=colores["imp"], label="_nolegend_")
    ax.bar(right_x, pa, width=width, bottom=impA, label="Actual: Potencia", color=colores["pot_actual"])
    ax.bar(right_x, ea, width=width, bottom=impA + pa, label="Actual: Energía", color=colores["ener_actual"])

    fig.suptitle("SIMULACIÓN MENSUAL (Oferta vs Actual)", y=0.98, fontsize=13, fontweight="bold")
    ax.annotate("", xy=(0, 1.03), xytext=(1, 1.03), xycoords="axes fraction", arrowprops=dict(arrowstyle="-", lw=0.8, color="#BDBDBD"))
    ax.legend(ncol=5, loc="lower center", bbox_to_anchor=(0.5, 1.065), frameon=False, borderaxespad=0.0, columnspacing=1.4, handlelength=1.8, fontsize=10)

    ax.set_xticks(x); ax.set_xticklabels(meses)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{int(round(v))}€"))
    ax.grid(axis="y", linestyle=":", linewidth=0.5)
    ax.set_xlim(x[0] - group_space + width + gap/2, x[-1] + group_space - width - gap/2)
    plt.subplots_adjust(left=0.08, right=0.98, bottom=0.12, top=0.82)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160); plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"

def _img_b64(path: Path, mime: str = "image/png") -> str:
    if not path.exists(): return ""
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

def _build_report_html(
    info: SuministroInfo,
    res: CompareResult,
    iva_pct: float,
    chart_url: Optional[str] = None,
    actual_plan: Optional[PlanInput] = None,
    propuesta_plan: Optional[PlanInput] = None,
    **_    # ignora kwargs extra
) -> str:
    logo_src = _img_b64(TEMPLATES_DIR / "logo_openenergies.png")

    # ---------- helpers robustos (idénticos en espíritu) ----------
    import html as _html, re

    def _fmt_number(n) -> str:
        """2 decimales estilo ES, tolerante a str/None/€, comas, espacios."""
        if n is None:
            x = 0.0
        else:
            try:
                x = float(n)
            except (TypeError, ValueError):
                s = str(n).replace("€", "").replace(" ", "").strip()
                s = s.replace(",", ".")
                try:
                    x = float(s)
                except Exception:
                    return _html.escape(str(n))
        return f"{x:,.2f}".replace(",", " ").replace(".", ",")

    def _as_dict(plan) -> dict:
        if plan is None:
            return {}
        if isinstance(plan, dict):
            return plan
        if hasattr(plan, "model_dump"):
            return plan.model_dump()
        if hasattr(plan, "dict"):
            return plan.dict()
        return getattr(plan, "__dict__", {}) or {}

    def _sorted_items(d: dict) -> list[tuple[str, float]]:
        def key(k: str):
            m = re.match(r"^[PE](\d+)$", k.strip().upper())
            return (k[0], int(m.group(1))) if m else (k, 0)
        return sorted(d.items(), key=lambda kv: key(kv[0]))

    # Grids de P1..P6 / E1..E6 (sin €)
    def _grid_row(prefix: str, values: dict) -> str:
        order = [f"{prefix}{i}" for i in range(1, 7)]
        cells = []
        for k in order:
            v = values.get(k)
            txt = _fmt_number(v) if v is not None else "—"
            cells.append(f"<div class='cell'><span class='cell-k'>{k}:</span> <span class='cell-v'>{txt}</span></div>")
        return "<div class='grid-row'>" + "".join(cells) + "</div>"

    def _grid_block(title: str, prefix: str, values: dict) -> str:
        return f"""
        <div class="grid-block">
          <div class="grid-title">{title}</div>
          {_grid_row(prefix, values or {})}
        </div>
        """

    def _precios_col(plan: Optional[PlanInput], titulo: str) -> str:
        d = _as_dict(plan)
        if not d:
            return ""
        pot = d.get("precio_potencia", {}) or {}
        ene = d.get("precio_energia", {}) or {}
        return f"""
        <div class="price-card">
          <div class="price-card__title">{titulo}</div>
          {_grid_block("Potencia (€/kW·año)", "P", dict(_sorted_items(pot)))}
          {_grid_block("Energía (€/kWh)", "E", dict(_sorted_items(ene)))}
        </div>
        """

    precios_block = ""
    if actual_plan or propuesta_plan:
        precios_block = f"""
        <div class="section-title">Comparativa de precios (Actual vs Propuesta)</div>
        <div class="compare-grid">
          {_precios_col(actual_plan, "Actual")}
          {_precios_col(propuesta_plan, "Propuesta")}
        </div>
        """

    # ---------- datos saneados para cabecera ----------
    cliente   = _html.escape(info.nombre_cliente or "-")
    direccion = _html.escape(info.direccion)
    poblacion = _html.escape(info.poblacion)
    cif       = _html.escape(info.cif)
    cups      = _html.escape(info.cups)
    fecha     = _html.escape(info.fecha_estudio)
    ah_pct, ah_eur, ah_mes = res.ahorro_pct, res.ahorro_anual, res.ahorro_mensual

    # ---------- HTML ----------
    return f"""
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>Informe Comparativa Open Energies</title>
  <style>
    :root {{
      --ink:#1f2937; --muted:#6b7280; --border:#e5e7eb;
      --brand:#2BB673; --brandDark:#166534;
      --headGradA:#ecfdf5; --headGradB:#e6faf1; /* verde suave del header */
      --bgHead:#ecfdf5;  /* encabezado de tabla verdoso */
      --ok:#16a34a; --bad:#dc2626;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
      color:var(--ink); background:#fff; margin:0; padding:0; font-size:10px;
    }}
    .page {{ padding:16mm 12mm; }}

    /* HEADER completo verde (fijo para que no cambie comportamiento) */
    .header {{
      position:fixed; top:10mm; left:12mm; right:12mm;
      display:flex; justify-content:space-between; align-items:center; gap:12px;
      background:linear-gradient(90deg,var(--headGradA) 0%, var(--headGradB) 100%);
      border:1px solid #d1fae5; border-radius:12px;
      padding:10px 12px;
    }}
    .header-left {{ display:flex; align-items:center; gap:10px; }}
    .logo {{ height:32px; width:auto; object-fit:contain; border-radius:6px; background:#fff; padding:3px; }}
    .titles .brand {{ color:var(--brand); font-weight:800; font-size:12px; line-height:1.1; }}
    .titles .title {{ color:#111827; font-weight:800; font-size:15px; line-height:1.1; }}
    .header-info {{ text-align:right; font-size:9px; color:#374151; line-height:1.5; }}
    .main {{ padding-top:92px; }} /* asegura que nada se solape con la cabecera */

    /* PANEL contenedor para Datos de suministro */
    .panel {{ border:1px solid var(--border); border-radius:10px; padding:10px; background:#fff; }}
    .section-title {{ font-size:13px; font-weight:700; color:var(--brandDark); padding-bottom:6px; margin:14px 0 8px; }}

    /* Datos + KPIs en un único panel */
    .supply-grid {{ display:grid; grid-template-columns:1fr minmax(260px,340px); gap:12px; align-items:start; }}
    .data-block {{ font-size:10px; line-height:1.7; }}
    .data-block .label {{ font-weight:600; color:#374151; }}

    .kpis {{ display:flex; gap:10px; align-items:stretch; justify-content:flex-end; }}
    .kpi {{ border:1px solid var(--border); border-radius:10px; padding:8px 10px; background:#fff; min-width:110px;
           display:flex; flex-direction:column; align-items:center; justify-content:center; }}
    .kpi .value {{ font-size:17px; font-weight:800; line-height:1.1; }}
    .kpi .label {{ font-size:8px; color:var(--muted); text-transform:uppercase; font-weight:600; margin-top:3px; }}
    .value.ok {{ color:var(--ok); }} .value.bad {{ color:var(--bad); }}

    /* Comparativa de precios (chips SIN bordes) */
    .compare-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .price-card {{ border:1px solid var(--border); border-radius:10px; padding:10px; background:#fff; }}
    .price-card__title {{ font-weight:700; margin-bottom:8px; color:#374151; }}
    .grid-block {{ margin-bottom:8px; }}
    .grid-title {{ font-weight:600; font-size:9px; color:#374151; margin:0 0 4px 0; }}
    .grid-row {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:6px; }}
    .cell {{ border:0; background:#f7fcf9; border-radius:6px; padding:6px 8px; text-align:center; font-size:9px; }}
    .cell-k {{ color:#4b5563; font-weight:600; margin-right:4px; }}
    .cell-v {{ color:#111827; font-weight:700; }}

    /* Tabla desglose (verde suave) */
    table {{ width:100%; border-collapse:collapse; font-size:10px; margin-top:10px; }}
    th, td {{ padding:8px; text-align:left; }}
    th {{ background:var(--bgHead); border-bottom:1px solid #c7eadc; color:#064e3b; font-weight:600; }}
    td {{ border-bottom:1px solid var(--border); }}
    tr:last-child td {{ border-bottom:0; }}
    .right {{ text-align:right; }}
    .currency {{ white-space:nowrap; }}

    /* Gráfico: forzamos a misma página */
    .chart-container {{ margin-top:12px; page-break-inside:avoid; }}
    .chart-container img {{ width:100%; height:auto; max-height:250px; border:1px solid var(--border); border-radius:8px; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="header-left">
      <img class="logo" src="{logo_src}" alt="Logo Open Energies" />
      <div class="titles">
        <div class="brand">Open Energies</div>
        <div class="title">Informe comparativa</div>
      </div>
    </div>
    <div class="header-info">
      <div><b>Fecha Estudio:</b> {fecha}</div>
      <div><b>Realizado por:</b> Open Energies</div>
      <div><b>CUPS:</b> {cups}</div>
    </div>
  </div>

  <div class="page main">
    <!-- Datos de Suministro + KPIs en un solo panel -->
    <div class="panel">
      <div class="section-title">Datos de Suministro</div>
      <div class="supply-grid">
        <div class="data-block">
          <div><span class="label">Titular:</span> {cliente}</div>
          <div><span class="label">CIF/DNI:</span> {cif}</div>
          <div><span class="label">Dirección:</span> {direccion}</div>
          <div><span class="label">Población:</span> {poblacion}</div>
        </div>
        <div class="kpis">
          <div class="kpi"><div class="value {'ok' if ah_pct >= 0 else 'bad'}">{_fmt_number(ah_pct)}%</div><div class="label">% Ahorro</div></div>
          <div class="kpi"><div class="value {'ok' if ah_mes >= 0 else 'bad'}">{_fmt_number(ah_mes)} €</div><div class="label">Ahorro Mes</div></div>
          <div class="kpi"><div class="value {'ok' if ah_eur >= 0 else 'bad'}">{_fmt_number(ah_eur)} €</div><div class="label">Ahorro Año</div></div>
        </div>
      </div>
    </div>

    {precios_block}

    <div class="section-title">Desglose por conceptos</div>
    <table>
      <thead>
        <tr>
          <th>Concepto</th>
          <th class="right">Tarifa Actual</th>
          <th class="right">Tarifa Propuesta</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>Importe Potencia</td><td class="right currency">{_fmt_number(res.actual.potencia_anual)} €</td><td class="right currency">{_fmt_number(res.propuesta.potencia_anual)} €</td></tr>
        <tr><td>Importe Energía</td><td class="right currency">{_fmt_number(res.actual.energia_anual)} €</td><td class="right currency">{_fmt_number(res.propuesta.energia_anual)} €</td></tr>
        <tr><td>Otros Cargos (Alquiler, etc.)</td><td class="right currency">{_fmt_number(res.actual.cargos_fijos_anual)} €</td><td class="right currency">{_fmt_number(res.propuesta.cargos_fijos_anual)} €</td></tr>
        <tr><td>Impuesto Eléctrico</td><td class="right currency">{_fmt_number(res.actual.impuesto_electricidad)} €</td><td class="right currency">{_fmt_number(res.propuesta.impuesto_electricidad)} €</td></tr>
        <tr><td>IVA ({int(iva_pct*100)}%)</td><td class="right currency">{_fmt_number(res.actual.iva)} €</td><td class="right currency">{_fmt_number(res.propuesta.iva)} €</td></tr>
        <tr style="font-weight:700; background: var(--bgHead);">
          <td>Facturación Anual Total</td>
          <td class="right currency">{_fmt_number(res.actual.total_anual)} €</td>
          <td class="right currency">{_fmt_number(res.propuesta.total_anual)} €</td>
        </tr>
      </tbody>
    </table>

    {f'<div class="chart-container"><div class="section-title">Evolución Mensual</div><img src="{chart_url}" alt="Evolución mensual" /></div>' if chart_url else ''}
  </div>
</body>
</html>
"""



# ===================== CICLO DE VIDA DE LA APLICACIÓN (PLAYWRIGHT) =====================

@app.on_event("startup")
async def startup_playwright() -> None:
    """Inicializa Playwright y lanza el navegador al arrancar el servicio."""
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(args=["--no-sandbox"])
    print("Playwright-Chromium (async) inicializado con éxito.")

@app.on_event("shutdown")
async def shutdown_playwright() -> None:
    """Cierra el navegador y detiene Playwright al apagar el servicio."""
    global _pw, _browser
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
    print("Playwright-Chromium (async) detenido.")

async def render_html_to_pdf(html: str) -> bytes:
    """Función refactorizada para generar el PDF usando la instancia global del navegador."""
    if not _browser:
        raise RuntimeError("Playwright browser no está inicializado.")
    
    page = await _browser.new_page()
    try:
        await page.set_content(html, wait_until="networkidle")
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "12mm", "bottom": "15mm", "left": "12mm", "right": "12mm"},
        )
        return pdf_bytes
    finally:
        await page.close()


# server.py (versión mejorada)

# ===================== ENDPOINTS DE LA API =====================

@app.get("/health", summary="Comprobar estado del servicio")
async def health_check():
    """Endpoint de salud para verificar que el servicio está activo."""
    return {"status": "ok"}

@app.post(
    "/generate-report",
    summary="Generar informe de comparativa en PDF",
    dependencies=[Depends(get_api_key)], # <-- ¡Aquí se aplica la seguridad!
)
async def generate_report_endpoint(payload: MonthlyInput) -> Response:
    """
    Endpoint principal y seguro.
    Recibe los datos, genera el gráfico y el HTML, lo renderiza a PDF y lo devuelve.
    """
    try:
        # 1. Realiza los cálculos de la comparativa
        comparison_result = compute_monthly_from_sips(payload)

        # 2. Genera la URL del gráfico en base64
        chart_url = make_monthly_bar_chart_dual(
            meses=comparison_result.meses,
            ener_actual=comparison_result.energia_actual, pot_actual=comparison_result.potencia_actual, imp_actual=comparison_result.impuestos_actual,
            ener_prop=comparison_result.energia_propuesta, pot_prop=comparison_result.potencia_propuesta, imp_prop=comparison_result.impuestos_propuesta
        )

        # 3. Construye el HTML del informe
        suministro_info = payload.suministro or SuministroInfo(direccion="-", cif="-", fecha_estudio="-", poblacion="-", cups="-")
        html_content = _build_report_html(
        suministro_info,
        comparison_result.resumen,
        payload.iva_pct,
        chart_url=chart_url,
        actual_plan=payload.actual,
        propuesta_plan=payload.propuesta,
    )

        # 4. Renderiza el HTML a PDF (usando la nueva función)
        pdf_bytes = await render_html_to_pdf(html_content)

        # 5. Devuelve el PDF
        return Response(content=pdf_bytes, media_type="application/pdf")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"Error inesperado al generar el informe: {e}")
        raise HTTPException(status_code=500, detail="Internal server error generating PDF")