import streamlit as st
import pandas as pd
import os
import pathlib
from datetime import date, datetime, timedelta
import calendar
from decimal import Decimal, ROUND_HALF_UP, getcontext
from io import BytesIO

getcontext().prec = 10

st.set_page_config(page_title="Simulador Credito", layout="wide")
st.title("Simulador de Credito — Revolving / Amortizable")

# ---------------------------------------------------------
# CARGA FECHAS DE BLOQUEO
# Archivo: COFES_01_Date_Blocage.txt (misma carpeta que este script)
# Formato: una fecha por linea DD/MM/YYYY
# ---------------------------------------------------------

_DIR = pathlib.Path(__file__).parent

def _leer_fechas_bloqueo():
    fechas = []
    ruta = _DIR / "COFES_01_Date_Blocage.txt"
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

fechas_bloqueo_global = _leer_fechas_bloqueo()

if not fechas_bloqueo_global:
    st.warning("No se encontro COFES_01_Date_Blocage.txt. Sube el fichero manualmente.")
    fichero_subido = st.file_uploader("Subir COFES_01_Date_Blocage.txt", type=["txt"])
    if fichero_subido:
        for linea in fichero_subido.read().decode("utf-8").splitlines():
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


def siguiente_mes_fecha(fecha, dia=None):
    """Si se pasa dia, usa ese dia en vez de fecha.day."""
    d = dia if dia is not None else fecha.day
    if fecha.month == 12:
        return date(fecha.year + 1, 1, d)
    return date(fecha.year, fecha.month + 1, d)


# ---------------------------------------------------------
# BASES DE CALCULO
# REVOLVING:   dias reales / ano real (365 o 366)
# AMORTIZABLE: con movimiento  -> dias reales / 360
#              sin movimiento  -> 30 dias / 360
# ---------------------------------------------------------

def calcular_interes_tramo(capital, tin, fecha_ini, fecha_fin,
                            tipo_producto, hay_movimiento):
    capital = Decimal(str(capital))
    tin_dec = Decimal(str(tin)) / Decimal("100")

    if tipo_producto == "Revolving":
        dias = Decimal(str((fecha_fin - fecha_ini).days))
        base = Decimal(str(dias_ano_real(fecha_ini)))
    else:
        if hay_movimiento:
            dias = Decimal(str((fecha_fin - fecha_ini).days))
            base = Decimal("360")
        else:
            dias = Decimal("30")
            base = Decimal("360")

    return capital * tin_dec * dias / base


def interes_periodo(capital, tin, fecha_inicio, fecha_fin,
                    tipo_producto, hay_movimiento):
    return calcular_interes_tramo(
        capital, tin, fecha_inicio, fecha_fin,
        tipo_producto, hay_movimiento
    ).quantize(Decimal("0.00001"))


def interes_con_movimientos(capital, tin, fecha_inicio, fecha_fin,
                             movimientos, tipo_producto):
    """
    Calcula interes total con movimientos intermedios ordenados por fecha.
    movimientos: lista de (fecha, importe, tipo)
      tipo = "amortizacion" -> reduce capital
      tipo = "disposicion"  -> aumenta capital
    Devuelve (interes_total, capital_final).
    """
    capital = Decimal(str(capital))
    interes_total = Decimal("0")
    fecha_actual = fecha_inicio

    for fecha_mov, importe, tipo_mov in sorted(movimientos, key=lambda x: x[0]):
        tramo = calcular_interes_tramo(
            capital, tin, fecha_actual, fecha_mov,
            tipo_producto, hay_movimiento=True
        )
        interes_total += tramo
        if tipo_mov == "amortizacion":
            capital -= Decimal(str(importe))
            if capital < 0:
                capital = Decimal("0")
        else:
            capital += Decimal(str(importe))
        fecha_actual = fecha_mov

    tramo_final = calcular_interes_tramo(
        capital, tin, fecha_actual, fecha_fin,
        tipo_producto, hay_movimiento=True
    )
    interes_total += tramo_final

    return interes_total.quantize(Decimal("0.00001")), capital


# ---------------------------------------------------------
# SIMULADOR
# ---------------------------------------------------------

def simulador(capital, tin, cuota_mensual, fecha_inicio,
              dia_recibo, df_amort, df_dispos, seguro_tasa,
              tipo_producto, cambios_dia=None, cambios_cuota=None):

    if cambios_dia is None:
        cambios_dia = {}
    if cambios_cuota is None:
        cambios_cuota = {}
    # Copiar para no mutar el original entre reruns
    cambios_dia = dict(cambios_dia)
    cambios_cuota = dict(cambios_cuota)

    capital = Decimal(str(capital))
    saldo = capital
    seguro_tasa = Decimal(str(seguro_tasa))
    cuota = Decimal(str(cuota_mensual)).quantize(Decimal("0.01"), ROUND_HALF_UP)

    fecha_recibo = crear_fecha_recibo(fecha_inicio, dia_recibo)
    if fecha_recibo <= fecha_inicio:
        fecha_recibo = crear_fecha_recibo(siguiente_mes_fecha(fecha_inicio), dia_recibo)

    fecha_anterior = fecha_inicio
    dia_pago_actual = dia_recibo
    datos = []
    mes = 1
    regularizacion_pendiente = Decimal("0")

    while saldo > 0:

        clave = (fecha_recibo.year, fecha_recibo.month)

        # Cambio de dia de pago y cambio de cuota:
        # Se introducen en el mes ANTERIOR al recibo que cambia.
        # Ejemplo: cambio introducido en mayo → afecta al recibo de junio.
        clave_anterior = (fecha_anterior.year, fecha_anterior.month)

        if cambios_dia and clave_anterior in cambios_dia:
            nuevo_dia = cambios_dia[clave_anterior]
            fecha_recibo = crear_fecha_recibo(fecha_recibo, nuevo_dia)
            dia_pago_actual = nuevo_dia
            del cambios_dia[clave_anterior]

        if cambios_cuota and clave_anterior in cambios_cuota:
            cuota = Decimal(str(cambios_cuota[clave_anterior])).quantize(
                Decimal("0.01"), ROUND_HALF_UP
            )
            del cambios_cuota[clave_anterior]

        fb = fecha_bloqueo_para_mes(fecha_recibo)
        corte = fb - timedelta(days=2)

        # Clasificar amortizaciones en P1 o P2
        amorts_p1 = []
        amorts_p2 = []
        for _, row in df_amort.iterrows():
            if pd.isna(row["Fecha"]):
                continue
            fa = pd.to_datetime(row["Fecha"]).date()
            imp = Decimal(str(row["Importe"]))
            if imp <= 0:
                continue
            if fecha_anterior <= fa <= corte:
                amorts_p1.append((fa, imp))
            elif corte < fa <= fecha_recibo:
                amorts_p2.append((fa, imp))

        # Recoger disposiciones del mes
        dispos_mes = []
        for _, row in df_dispos.iterrows():
            if pd.isna(row["Fecha"]):
                continue
            fa = pd.to_datetime(row["Fecha"]).date()
            imp = Decimal(str(row["Importe"]))
            if imp <= 0:
                continue
            if fecha_anterior <= fa <= fecha_recibo:
                dispos_mes.append((fa, imp))

        amorts_p1.sort()
        amorts_p2.sort()
        dispos_mes.sort()

        amort_extra_p1 = sum(a[1] for a in amorts_p1)
        amort_extra_p2 = sum(a[1] for a in amorts_p2)
        dispos_total   = sum(d[1] for d in dispos_mes)

        hay_movimientos = bool(amorts_p1 or amorts_p2 or dispos_mes)

        # saldo_inicio_mes = capital ANTES de cualquier movimiento
        saldo_inicio_mes = saldo

        # --- Calculo de interes en tramos ---
        if hay_movimientos:
            movimientos = (
                [(fa, imp, "amortizacion") for fa, imp in amorts_p1] +
                [(fa, imp, "amortizacion") for fa, imp in amorts_p2] +
                [(fa, imp, "disposicion")  for fa, imp in dispos_mes]
            )
            interes, _ = interes_con_movimientos(
                saldo_inicio_mes, tin, fecha_anterior, fecha_recibo,
                movimientos, tipo_producto
            )
        else:
            interes = interes_periodo(
                saldo, tin, fecha_anterior, fecha_recibo,
                tipo_producto, hay_movimiento=False
            )

        # Regularizacion diferida del mes anterior (P2 previo)
        interes += regularizacion_pendiente
        regularizacion_pendiente = Decimal("0")

        # Aplicar P1 al saldo este mes
        saldo -= amort_extra_p1
        if saldo < 0:
            saldo = Decimal("0")

        # Aplicar disposiciones al saldo este mes
        saldo += dispos_total

        # P2: recibo proximo intacto, diferir ahorro al mes siguiente
        if amorts_p2:
            fecha_sig_recibo = crear_fecha_recibo(
                siguiente_mes_fecha(fecha_recibo, dia_pago_actual), dia_pago_actual
            )
            for fa, imp in amorts_p2:
                ahorro = calcular_interes_tramo(
                    imp, tin, fa, fecha_sig_recibo,
                    tipo_producto, hay_movimiento=True
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
            base_info = f"Real/{dias_ano_real(fecha_anterior)}"
        else:
            base_info = "Real/360" if hay_movimientos else "30/360"

        datos.append({
            "Mes": mes,
            "Fecha recibo": fecha_recibo,
            "Fecha bloqueo": fb,
            "Base interes": base_info,
            "Capital pendiente (EUR)": float(saldo + amort),
            "Cuota aplicada (EUR)": float(cuota),
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
        fecha_recibo = crear_fecha_recibo(
            siguiente_mes_fecha(fecha_recibo, dia_pago_actual), dia_pago_actual
        )
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
        v = van(medio)
        if abs(v) < 1e-10:
            return round(medio * 100, 2)
        if v > 0:
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
        "Revolving: dias reales / ano real (365 o 366).  \n"
        "Amortizable: mes con movimiento -> dias reales / 360; "
        "sin movimiento -> 30 dias / 360."
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

    # DESACTIVADO - conservar para uso futuro:
    # tipo_calculo = st.selectbox("Tipo calculo", ["Vitesse", "Cuota"])
    # valor = st.number_input("Valor calculo", 0.0, 1000.0, 3.0)
    # if tipo_calculo == "Vitesse":
    #     cuota_input = round(capital * valor / 100, 2)
    # else:
    #     cuota_input = valor

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
        "P1 (recibo -> bloqueo-2d): interes en dos tramos, mismo mes. "
        "P2 (bloqueo-2d -> proximo recibo): recibo intacto, regularizacion al mes siguiente. "
        "Base: dias reales / ano real."
    )
else:
    st.caption(
        "P1: base dias reales / 360. "
        "P2: recibo intacto, regularizacion al mes siguiente. "
        "Sin movimiento: 30 dias / 360."
    )

df_amort_raw = st.data_editor(
    pd.DataFrame({"Fecha": [None], "Importe": [0.0]}),
    column_config={
        "Fecha": st.column_config.DateColumn("Fecha amortizacion", format="DD/MM/YYYY"),
        "Importe": st.column_config.NumberColumn("Importe (EUR)", min_value=0, step=100),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="editor_amort",
)

# ---------------------------------------------------------
# DISPOSICIONES
# ---------------------------------------------------------

st.subheader("Disposiciones (aumentos de capital)")
st.caption(
    "El interes se recalcula en tramos considerando el aumento de capital desde la fecha indicada."
)

df_dispos_raw = st.data_editor(
    pd.DataFrame({"Fecha": [None], "Importe": [0.0]}),
    column_config={
        "Fecha": st.column_config.DateColumn("Fecha disposicion", format="DD/MM/YYYY"),
        "Importe": st.column_config.NumberColumn("Importe (EUR)", min_value=0, step=100),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="editor_dispos",
)

# ---------------------------------------------------------
# CAMBIO DE DIA DE PAGO
# ---------------------------------------------------------

st.subheader("Cambio de dia de pago")
st.caption(
    "Introduce el mes en que cambia el dia de pago (formato YYYY-MM) y el nuevo dia. "
    "Solo afecta al numero de dias del calculo de intereses. La cuota no cambia."
)

df_cambio_dia_raw = st.data_editor(
    pd.DataFrame({"Fecha del cambio": [None], "Nuevo dia": [None]}),
    column_config={
        "Fecha del cambio": st.column_config.DateColumn(
            "Mes del cambio (cualquier dia del mes)", format="DD/MM/YYYY"
        ),
        "Nuevo dia": st.column_config.NumberColumn(
            "Nuevo dia de pago", min_value=1, max_value=28, step=1
        ),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="editor_cambio_dia",
)

# Convertir a diccionario {(year, month): nuevo_dia}
cambios_dia = {}
for _, row in df_cambio_dia_raw.iterrows():
    try:
        if pd.isna(row["Fecha del cambio"]) or pd.isna(row["Nuevo dia"]):
            continue
        fa = pd.to_datetime(row["Fecha del cambio"]).date()
        nd = int(row["Nuevo dia"])
        if 1 <= nd <= 28:
            cambios_dia[(fa.year, fa.month)] = nd
    except Exception:
        pass

# ---------------------------------------------------------
# CAMBIO DE MENSUALIDAD
# ---------------------------------------------------------

st.subheader("Cambio de mensualidad")
st.caption(
    "Introduce el mes en que cambia la cuota (formato YYYY-MM) y el nuevo importe. "
    "Solo afecta a la amortizacion de capital. Los intereses se calculan igual."
)

df_cambio_cuota_raw = st.data_editor(
    pd.DataFrame({"Fecha del cambio": [None], "Nueva cuota (EUR)": [None]}),
    column_config={
        "Fecha del cambio": st.column_config.DateColumn(
            "Mes del cambio (cualquier dia del mes)", format="DD/MM/YYYY"
        ),
        "Nueva cuota (EUR)": st.column_config.NumberColumn(
            "Nueva cuota (EUR)", min_value=0, step=1.0
        ),
    },
    num_rows="dynamic",
    use_container_width=True,
    key="editor_cambio_cuota",
)

# Convertir a diccionario {(year, month): nueva_cuota}
cambios_cuota = {}
for _, row in df_cambio_cuota_raw.iterrows():
    try:
        if pd.isna(row["Fecha del cambio"]) or pd.isna(row["Nueva cuota (EUR)"]):
            continue
        fa = pd.to_datetime(row["Fecha del cambio"]).date()
        nc = float(row["Nueva cuota (EUR)"])
        if nc > 0:
            cambios_cuota[(fa.year, fa.month)] = nc
    except Exception:
        pass

# Debug: mostrar cambios detectados
st.caption(f"Cambios de dia detectados: {cambios_dia if cambios_dia else 'Ninguno'}")
st.caption(f"Cambios de cuota detectados: {cambios_cuota if cambios_cuota else 'Ninguno'}")

# ---------------------------------------------------------
# TAE referencia y bloqueo
# ---------------------------------------------------------

fecha_ref_tae = None
for _, row in df_amort_raw.iterrows():
    if not pd.isna(row["Fecha"]) and row["Importe"] > 0:
        fecha_ref_tae = pd.to_datetime(row["Fecha"]).date()
        break

if fecha_ref_tae:
    st.info(f"TAE calculada desde: **{fecha_ref_tae.strftime('%d/%m/%Y')}**")
else:
    st.info("TAE calculada desde la fecha de inicio.")

if fechas_bloqueo_global:
    with st.expander("Fechas de bloqueo cargadas desde COFES_01_Date_Blocage.txt"):
        st.write(pd.DataFrame({"Fecha bloqueo": fechas_bloqueo_global}))

# ---------------------------------------------------------
# CALCULAR
# ---------------------------------------------------------

if st.button("Calcular", type="primary"):

    tabla = simulador(
        capital, tin, cuota_input,
        fecha_inicio, dia_recibo,
        df_amort_raw, df_dispos_raw,
        seguro_tasa, tipo_producto,
        cambios_dia, cambios_cuota
    )

    st.subheader("Tabla de amortizacion")
    st.dataframe(tabla, use_container_width=True)

    # TAE
    # Fecha origen = fecha_inicio (siempre desde el inicio del credito)
    # Flujos negativos: capital inicial + disposiciones (desembolsos)
    # Flujos positivos: cuotas pagadas (recibos)
    # Amortizaciones anticipadas son devoluciones anticipadas de capital
    # y no se incluyen como flujos separados porque ya reducen el saldo
    # y por tanto acortan las cuotas futuras.

    fecha_origen_tae = fecha_inicio

    # Flujo inicial: capital original desembolsado
    flujos = [-float(capital)]
    fechas_flujos = [fecha_origen_tae]

    # Disposiciones: desembolsos adicionales (flujos negativos)
    for _, row in df_dispos_raw.iterrows():
        if pd.isna(row["Fecha"]):
            continue
        fa = pd.to_datetime(row["Fecha"]).date()
        imp = row["Importe"]
        if imp > 0:
            flujos.append(-float(imp))
            fechas_flujos.append(fa)

    # Amortizaciones anticipadas: devoluciones de capital (flujos positivos)
    for _, row in df_amort_raw.iterrows():
        if pd.isna(row["Fecha"]):
            continue
        fa = pd.to_datetime(row["Fecha"]).date()
        imp = row["Importe"]
        if imp > 0:
            flujos.append(float(imp))
            fechas_flujos.append(fa)

    # Cuotas pagadas: flujos positivos
    # Los cambios de mensualidad y dia de pago ya estan reflejados
    # en las fechas y cuotas de la tabla
    for _, row in tabla.iterrows():
        flujos.append(row["Recibo total (EUR)"])
        fechas_flujos.append(row["Fecha recibo"])

    datos_tae = sorted(zip(fechas_flujos, flujos))
    fechas_flujos = [x[0] for x in datos_tae]
    flujos = [x[1] for x in datos_tae]

    with st.expander("Debug TAE - flujos utilizados"):
        df_debug = pd.DataFrame({"Fecha": fechas_flujos, "Flujo (EUR)": flujos})
        st.dataframe(df_debug)

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
