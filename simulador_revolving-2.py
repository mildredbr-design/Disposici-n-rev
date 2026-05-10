import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
import calendar
from decimal import Decimal, ROUND_HALF_UP, getcontext
from io import BytesIO

getcontext().prec = 10

st.set_page_config(page_title="Simulador Crédito", layout="wide")
st.title("💳 Simulador de Crédito — Revolving / Amortizable")

# ---------------------------------------------------------
# CARGA FECHAS DE BLOQUEO  →  COFES_01_Date_Blocage.txt
# Formato: una fecha por línea, DD/MM/YYYY
# ---------------------------------------------------------

ARCHIVO_BLOQUEO = "COFES_01_Date_Blocage.txt"

@st.cache_data
def cargar_fechas_bloqueo(ruta):
    fechas = []
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    fechas.append(datetime.strptime(linea, "%d/%m/%Y").date())
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return sorted(fechas)

fechas_bloqueo_global = cargar_fechas_bloqueo(ARCHIVO_BLOQUEO)

if not fechas_bloqueo_global:
    st.warning(
        f"No se encontro `{ARCHIVO_BLOQUEO}`. "
        "Sube el fichero manualmente o colócalo en el mismo directorio."
    )
    fichero_subido = st.file_uploader("Subir COFES_01_Date_Blocage.txt", type=["txt"])
    if fichero_subido:
        contenido = fichero_subido.read().decode("utf-8")
        for linea in contenido.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            try:
                fechas_bloqueo_global.append(datetime.strptime(linea, "%d/%m/%Y").date())
            except ValueError:
                pass
        fechas_bloqueo_global = sorted(fechas_bloqueo_global)
        if fechas_bloqueo_global:
            st.success(f"{len(fechas_bloqueo_global)} fechas de bloqueo cargadas.")


def fecha_bloqueo_para_mes(fecha_recibo):
    for fb in fechas_bloqueo_global:
        if fb.year == fecha_recibo.year and fb.month == fecha_recibo.month:
            return fb
    return fecha_recibo - timedelta(days=2)


# ---------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------

def dias_ano_real(fecha):
    return 366 if calendar.isleap(fecha.year) else 365


def crear_fecha_recibo(fecha_base, dia):
    ultimo_dia = calendar.monthrange(fecha_base.year, fecha_base.month)[1]
    return date(fecha_base.year, fecha_base.month, min(dia, ultimo_dia))


def siguiente_mes_fecha(fecha):
    if fecha.month == 12:
        return date(fecha.year + 1, 1, fecha.day)
    return date(fecha.year, fecha.month + 1, fecha.day)


# ---------------------------------------------------------
# BASES DE CALCULO
#
# REVOLVING:
#   dias reales entre fechas / año real (365 o 366)
#
# AMORTIZABLE:
#   - Mes CON amortizacion anticipada:
#       dias reales entre fechas / 360 (base comercial)
#   - Mes SIN amortizacion anticipada:
#       30 dias / 360
# ---------------------------------------------------------

def calcular_interes_tramo(capital, tin, fecha_ini, fecha_fin,
                            tipo_producto, hay_amort_anticipada):
    capital = Decimal(str(capital))
    tin_dec = Decimal(str(tin)) / Decimal("100")

    if tipo_producto == "Revolving":
        dias = Decimal(str((fecha_fin - fecha_ini).days))
        base = Decimal(str(dias_ano_real(fecha_ini)))
    else:
        if hay_amort_anticipada:
            dias = Decimal(str((fecha_fin - fecha_ini).days))
            base = Decimal("360")
        else:
            dias = Decimal("30")
            base = Decimal("360")

    return capital * tin_dec * dias / base


def interes_periodo(capital, tin, fecha_inicio, fecha_fin,
                    tipo_producto, hay_amort_anticipada):
    resultado = calcular_interes_tramo(
        capital, tin, fecha_inicio, fecha_fin,
        tipo_producto, hay_amort_anticipada
    )
    return resultado.quantize(Decimal("0.00001"))


def interes_con_movimientos(capital, tin, fecha_inicio, fecha_fin,
                            movimientos, tipo_producto):
    """
    Calcula el interes total con movimientos intermedios.
    movimientos: lista de (fecha, importe, tipo)
      tipo = "amortizacion" → reduce capital
      tipo = "disposicion"  → aumenta capital
    Devuelve (interes_total, capital_final).
    """
    capital = Decimal(str(capital))
    interes_total = Decimal("0")
    fecha_actual = fecha_inicio

    for fecha_mov, importe, tipo_mov in sorted(movimientos, key=lambda x: x[0]):
        tramo = calcular_interes_tramo(
            capital, tin, fecha_actual, fecha_mov,
            tipo_producto, hay_amort_anticipada=True
        )
        interes_total += tramo
        if tipo_mov == "amortizacion":
            capital -= Decimal(str(importe))
            if capital < 0:
                capital = Decimal("0")
        else:  # disposicion
            capital += Decimal(str(importe))
        fecha_actual = fecha_mov

    tramo_final = calcular_interes_tramo(
        capital, tin, fecha_actual, fecha_fin,
        tipo_producto, hay_amort_anticipada=True
    )
    interes_total += tramo_final

    return interes_total.quantize(Decimal("0.00001")), capital


def interes_con_amortizaciones(capital, tin, fecha_inicio, fecha_fin,
                                amortizaciones, tipo_producto):
    """Wrapper para compatibilidad — convierte lista de amortizaciones al nuevo formato."""
    movs = [(fa, imp, "amortizacion") for fa, imp in amortizaciones]
    return interes_con_movimientos(capital, tin, fecha_inicio, fecha_fin,
                                   movs, tipo_producto)


# ---------------------------------------------------------
# SIMULADOR
# ---------------------------------------------------------

def simulador(capital, tin, cuota_mensual, fecha_inicio,
              dia_recibo, df_amort, df_dispos, seguro_tasa, tipo_producto):

    capital = Decimal(str(capital))
    saldo = capital
    seguro_tasa = Decimal(str(seguro_tasa))
    cuota = Decimal(str(cuota_mensual)).quantize(Decimal("0.01"), ROUND_HALF_UP)

    fecha_recibo = crear_fecha_recibo(fecha_inicio, dia_recibo)
    if fecha_recibo <= fecha_inicio:
        fecha_recibo = crear_fecha_recibo(siguiente_mes_fecha(fecha_inicio), dia_recibo)

    fecha_anterior = fecha_inicio

    datos = []
    mes = 1
    regularizacion_pendiente = Decimal("0")

    while saldo > 0:

        fb = fecha_bloqueo_para_mes(fecha_recibo)
        corte = fb - timedelta(days=2)

        amorts_p1 = []
        amorts_p2 = []
        dispos_mes = []   # disposiciones del mes (aumentan capital)

        for _, row in df_amort.iterrows():
            if pd.isna(row["Fecha"]):
                continue
            fa = pd.to_datetime(row["Fecha"]).date()
            imp = Decimal(str(row["Importe (€)"]))
            if imp <= 0:
                continue
            if fecha_anterior <= fa <= corte:
                amorts_p1.append((fa, imp))
            elif corte < fa <= fecha_recibo:
                amorts_p2.append((fa, imp))

        for _, row in df_dispos.iterrows():
            if pd.isna(row["Fecha"]):
                continue
            fa = pd.to_datetime(row["Fecha"]).date()
            imp = Decimal(str(row["Importe (€)"]))
            if imp <= 0:
                continue
            if fecha_anterior <= fa <= fecha_recibo:
                dispos_mes.append((fa, imp))

        amorts_p1.sort()
        amorts_p2.sort()
        dispos_mes.sort()

        hay_movimientos = len(amorts_p1) > 0 or len(amorts_p2) > 0 or len(dispos_mes) > 0

        amort_extra_p1 = sum(a[1] for a in amorts_p1)
        amort_extra_p2 = sum(a[1] for a in amorts_p2)
        dispos_total   = sum(d[1] for d in dispos_mes)

        # --- Calculo de interes ---
        # Todos los movimientos del mes (amortizaciones y disposiciones)
        # se ordenan por fecha y se calculan tramos entre cada uno.
        # Amortizaciones → bajan capital
        # Disposiciones  → suben capital

        if hay_movimientos:
            movimientos = (
                [(fa, imp, "amortizacion") for fa, imp in amorts_p1] +
                [(fa, imp, "amortizacion") for fa, imp in amorts_p2] +
                [(fa, imp, "disposicion")  for fa, imp in dispos_mes]
            )
            interes, _ = interes_con_movimientos(
                saldo, tin, fecha_anterior, fecha_recibo,
                movimientos, tipo_producto
            )
        else:
            interes = interes_periodo(
                saldo, tin, fecha_anterior, fecha_recibo,
                tipo_producto, hay_amort_anticipada=False
            )

        # Regularizacion diferida del mes anterior (de P2 previo)
        interes += regularizacion_pendiente
        regularizacion_pendiente = Decimal("0")

        # Aplicar amortizaciones P1 al saldo (efecto este mes)
        saldo -= amort_extra_p1
        if saldo < 0:
            saldo = Decimal("0")

        # Aplicar disposiciones al saldo (aumentan capital este mes)
        saldo += dispos_total

        # --- Periodo 2: recibo proximo intacto ---
        if amorts_p2:
            fecha_sig_recibo = crear_fecha_recibo(
                siguiente_mes_fecha(fecha_recibo), dia_recibo
            )
            for fa, imp in amorts_p2:
                ahorro = calcular_interes_tramo(
                    imp, tin, fa, fecha_sig_recibo,
                    tipo_producto, hay_amort_anticipada=True
                )
                regularizacion_pendiente -= ahorro
            saldo -= amort_extra_p2
            if saldo < 0:
                saldo = Decimal("0")

        # --- Cuota fija ---

        interes = interes.quantize(Decimal("0.01"), ROUND_HALF_UP)
        seguro = ((saldo + interes) * seguro_tasa).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )

        if saldo + interes <= cuota:
            amort = saldo
            saldo = Decimal("0")
            cuota_final = amort + interes
        else:
            amort = cuota - interes
            if amort < 0:
                amort = Decimal("0")
            saldo = saldo - amort
            cuota_final = cuota

        if tipo_producto == "Revolving":
            base_info = f"Real / {dias_ano_real(fecha_anterior)}"
        else:
            base_info = "Real / 360" if hay_amort_mes else "30 / 360"

        datos.append({
            "Mes": mes,
            "Fecha recibo": fecha_recibo,
            "Fecha bloqueo": fb,
            "Base interes": base_info,
            "Capital pendiente (EUR)": float(saldo + amort),
            "Cuota (EUR)": float(cuota_final),
            "Intereses (EUR)": float(interes),
            "Amortizacion (EUR)": float(amort),
            "Amort. anticipada P1 (EUR)": float(amort_extra_p1),
            "Amort. anticipada P2 (EUR)": float(amort_extra_p2),
            "Disposicion (EUR)": float(dispos_total),
            "Saldo (EUR)": float(saldo),
            "Seguro (EUR)": float(seguro),
            "Recibo total (EUR)": float(cuota_final + seguro),
        })

        fecha_anterior = fecha_recibo
        fecha_recibo = crear_fecha_recibo(siguiente_mes_fecha(fecha_recibo), dia_recibo)
        mes += 1

        if mes > 600:
            break

    return pd.DataFrame(datos)


# ---------------------------------------------------------
# TAE
# ---------------------------------------------------------

def calcular_tae(flujos, fechas):
    tiempos = [0.0]
    for i in range(1, len(fechas)):
        f0 = fechas[i - 1]
        f1 = fechas[i]
        fraccion = (f1 - f0).days / dias_ano_real(f0)
        tiempos.append(tiempos[-1] + fraccion)

    def van(tasa):
        return sum(c / ((1 + tasa) ** t) for c, t in zip(flujos, tiempos))

    minimo, maximo = -0.9999, 10.0
    for _ in range(1000):
        medio = (minimo + maximo) / 2
        valor = van(medio)
        if abs(valor) < 1e-10:
            return round(medio * 100, 2)
        if valor > 0:
            minimo = medio
        else:
            maximo = medio
    return round(medio * 100, 2)


# ---------------------------------------------------------
# INPUTS
# ---------------------------------------------------------

tipo_producto = st.radio(
    "Tipo de producto",
    ["Revolving", "Amortizable"],
    horizontal=True,
    help=(
        "Revolving: interes sobre dias naturales reales / ano real (365 o 366).  \n"
        "Amortizable: mes con amortizacion anticipada -> dias reales / 360; "
        "resto de meses -> 30 dias / 360."
    )
)

st.divider()

col1, col2 = st.columns(2)

with col1:
    capital = st.number_input("Capital pendiente (EUR)", 0.0, 1_000_000.0, 6000.0)
    tin = st.number_input("TIN anual (%)", 0.0, 100.0, 21.79)
    fecha_inicio = st.date_input("Fecha inicio", datetime.today())
    dia_recibo = st.selectbox("Dia del recibo", list(range(1, 29)))

with col2:
    cuota_input = st.number_input("Cuota mensual (EUR)", 0.0, 100_000.0, 180.0, step=1.0)

    # --- DESACTIVADO (conservar para uso futuro) ---
    # tipo_calculo = st.selectbox("Tipo calculo", ["Vitesse", "Cuota"])
    # valor = st.number_input("Valor calculo", 0.0, 1000.0, 3.0)
    # if tipo_calculo == "Vitesse":
    #     cuota_input = round(capital * valor / 100, 2)
    # else:
    #     cuota_input = valor
    # ------------------------------------------------

    opciones_seguro = {
        "No": 0,
        "Un titular Light": 0.0035,
        "Un titular Full/Senior": 0.0061,
        "Dos titulares Full/Full": 0.0104,
        "Dos titulares Light/Light": 0.0059,
    }
    seguro_str = st.selectbox("Seguro", list(opciones_seguro.keys()))
    seguro_tasa = opciones_seguro[seguro_str]

# ---------------------------------------------------------
# AMORTIZACIONES
# ---------------------------------------------------------

st.subheader("Amortizaciones anticipadas")

if tipo_producto == "Revolving":
    st.caption(
        "Periodo 1 (fecha recibo -> bloqueo-2d): interes en dos tramos, mismo mes. "
        "Cuota fija: menor interes = mayor amortizacion ordinaria.  \n"
        "Periodo 2 (bloqueo-2d -> proximo recibo): recibo proximo intacto; "
        "regularizacion de intereses en el mes siguiente.  \n"
        "Base: dias naturales reales / ano real (365 o 366)."
    )
else:
    st.caption(
        "Periodo 1 (fecha recibo -> bloqueo-2d): base dias reales / 360. "
        "Cuota fija.  \n"
        "Periodo 2 (bloqueo-2d -> proximo recibo): recibo proximo intacto; "
        "regularizacion en el mes siguiente.  \n"
        "Meses sin amortizacion anticipada: base 30 dias / 360."
    )

df_amort = st.data_editor(
    pd.DataFrame({"Fecha": [None], "Importe (EUR)": [0.0]}),
    column_config={
        "Fecha": st.column_config.DateColumn("Fecha amortizacion", format="DD/MM/YYYY"),
        "Importe (EUR)": st.column_config.NumberColumn(
            "Importe (EUR)", min_value=0, step=100
        ),
    },
    num_rows="dynamic",
    use_container_width=True,
)

# Renombrar columna interna para compatibilidad con el simulador
df_amort = df_amort.rename(columns={"Importe (EUR)": "Importe (€)"})

# ---------------------------------------------------------
# DISPOSICIONES
# ---------------------------------------------------------

st.subheader("Disposiciones (aumentos de capital)")
st.caption(
    "Introduce la fecha y el importe de cada disposicion. "
    "El interes del mes se recalcula en tramos considerando el aumento de capital."
)

df_dispos = st.data_editor(
    pd.DataFrame({"Fecha": [None], "Importe (EUR)": [0.0]}),
    column_config={
        "Fecha": st.column_config.DateColumn("Fecha disposicion", format="DD/MM/YYYY"),
        "Importe (EUR)": st.column_config.NumberColumn(
            "Importe (EUR)", min_value=0, step=100
        ),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="editor_dispos",
)

df_dispos = df_dispos.rename(columns={"Importe (EUR)": "Importe (€)"})

fecha_ref_tae = None
for _, row in df_amort.iterrows():
    if not pd.isna(row["Fecha"]) and row["Importe (€)"] > 0:
        fecha_ref_tae = pd.to_datetime(row["Fecha"]).date()
        break

if fecha_ref_tae:
    st.info(
        f"TAE calculada desde la fecha de amortizacion: "
        f"**{fecha_ref_tae.strftime('%d/%m/%Y')}**"
    )
else:
    st.info("TAE calculada desde la fecha de inicio (sin amortizaciones definidas).")

if fechas_bloqueo_global:
    with st.expander("Fechas de bloqueo cargadas desde COFES_01_Date_Blocage.txt"):
        st.write(pd.DataFrame({"Fecha bloqueo": fechas_bloqueo_global}))

# ---------------------------------------------------------
# CALCULAR
# ---------------------------------------------------------

if st.button("Calcular", type="primary"):

    tabla = simulador(
        capital, tin, cuota_input,
        fecha_inicio, dia_recibo, df_amort, df_dispos, seguro_tasa,
        tipo_producto
    )

    st.subheader("Tabla de amortizacion")
    st.dataframe(tabla, use_container_width=True)

    # TAE
    fecha_origen_tae = fecha_ref_tae if fecha_ref_tae else fecha_inicio

    capital_tae = Decimal(str(capital))
    for _, row in df_amort.iterrows():
        if pd.isna(row["Fecha"]):
            continue
        fa = pd.to_datetime(row["Fecha"]).date()
        if fa < fecha_origen_tae:
            capital_tae -= Decimal(str(row["Importe (€)"]))
    capital_tae = max(capital_tae, Decimal("0"))

    flujos = [-float(capital_tae)]
    fechas_flujos = [fecha_origen_tae]

    for _, row in tabla.iterrows():
        if row["Fecha recibo"] >= fecha_origen_tae:
            flujos.append(row["Recibo total (EUR)"])
            fechas_flujos.append(row["Fecha recibo"])

    for _, row in df_amort.iterrows():
        if pd.isna(row["Fecha"]):
            continue
        fa = pd.to_datetime(row["Fecha"]).date()
        if fa >= fecha_origen_tae and row["Importe (€)"] > 0:
            flujos.append(row["Importe (€)"])
            fechas_flujos.append(fa)

    datos_tae = sorted(zip(fechas_flujos, flujos))
    fechas_flujos = [x[0] for x in datos_tae]
    flujos = [x[1] for x in datos_tae]

    tae = calcular_tae(flujos, fechas_flujos)

    total_intereses = round(tabla["Intereses (EUR)"].sum(), 2)
    total_seguro    = round(tabla["Seguro (EUR)"].sum(), 2)
    total_pago      = round(tabla["Recibo total (EUR)"].sum(), 2)
    total_p1        = round(tabla["Amort. anticipada P1 (EUR)"].sum(), 2)
    total_p2        = round(tabla["Amort. anticipada P2 (EUR)"].sum(), 2)
    total_dispos    = round(tabla["Disposicion (EUR)"].sum(), 2)

    resumen = pd.DataFrame({
        "Concepto": [
            "Tipo de producto",
            "Duracion (meses)",
            "Intereses totales (EUR)",
            "Seguro total (EUR)",
            "Total pagado (EUR)",
            "Amort. anticipada P1 (EUR)",
            "Amort. anticipada P2 (EUR)",
            "Disposiciones (EUR)",
            f"TAE (%) desde {fecha_origen_tae.strftime('%d/%m/%Y')}",
        ],
        "Valor": [
            tipo_producto,
            len(tabla),
            total_intereses,
            total_seguro,
            total_pago,
            total_p1,
            total_p2,
            total_dispos,
            tae,
        ],
    })

    st.subheader("Resumen")
    st.table(resumen)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        tabla.to_excel(writer, sheet_name="Amortizacion", index=False)
        resumen.to_excel(writer, sheet_name="Resumen", index=False)

    st.download_button(
        "Descargar Excel",
        output.getvalue(),
        "simulacion_credito.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
