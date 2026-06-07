"""
TRex Hackathon — OEE + Anomali Analiz Dashboardu
Streamlit giriş noktası
"""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from datetime import date

from src.db_engine import (
    get_connection, get_machines, get_oee_summary,
    get_stoppages, get_date_range,
)
from src.rca_engine import get_rca_analysis, get_anomaly_summary
from src.whatif_engine import simulate_oee, calculate_financial_gain, get_whatif_summary

# ---------------------------------------------------------------------------
# Sayfa yapılandırması
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="TRex OEE Analiz",
    page_icon="🦖",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Makine tipi yardımcısı
# ---------------------------------------------------------------------------
MACHINE_TYPE_KEYWORDS = {
    "fanuc":      "🔧 Fanuc CNC",
    "mitsubishi": "🔧 Mitsubishi CNC",
    "nukon":      "⚡ Nukon Laser",
    "laser":      "⚡ Laser",
    "cnc":        "🔧 CNC",
    "turbocut":   "⚡ TurboCut Laser",
    "ares":       "🔧 Ares Seiki CNC",
}


def machine_label(name: str) -> str:
    lower = name.lower()
    for kw, label in MACHINE_TYPE_KEYWORDS.items():
        if kw in lower:
            return label
    return "🏭 Makine"


# ---------------------------------------------------------------------------
# Önbellekli veri yardımcıları
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300)
def cached_oee_summary(machine_id: str, date_str: str):
    conn = get_connection()
    return get_oee_summary(machine_id, date_str, conn)


@st.cache_data(ttl=300)
def cached_stoppages(machine_id: str, date_str: str):
    conn = get_connection()
    return get_stoppages(machine_id, date_str, conn)


@st.cache_data(ttl=300)
def cached_rca(machine_id: str, stoppage_ts: int, z_threshold: float):
    conn = get_connection()
    return get_rca_analysis(machine_id, stoppage_ts, conn, z_threshold=z_threshold)


@st.cache_data(ttl=300)
def cached_oee_trend(machine_id: str, start_date: str, end_date: str):
    """Seçili makine için OEE trend verisi döner (son 30 gün)."""
    conn = get_connection()
    from src.db_engine import _get_unit_uid
    unit_uid = _get_unit_uid(machine_id, conn)
    if not unit_uid:
        return pd.DataFrame()
    try:
        df = conn.execute(
            "SELECT CAST(trans_date AS DATE) as tarih, "
            "AVG(oee) as oee, AVG(availability) as uygunluk, "
            "AVG(performance) as performans, AVG(quality) as kalite "
            "FROM oee_summary "
            "WHERE unit_uid = ? "
            "AND CAST(trans_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) "
            "AND level = 1 "
            "GROUP BY tarih ORDER BY tarih",
            (unit_uid, start_date, end_date),
        ).df()
        if df.empty:
            # level filtresi olmadan tekrar dene
            df = conn.execute(
                "SELECT CAST(trans_date AS DATE) as tarih, "
                "AVG(oee) as oee, AVG(availability) as uygunluk, "
                "AVG(performance) as performans, AVG(quality) as kalite "
                "FROM oee_summary "
                "WHERE unit_uid = ? "
                "AND CAST(trans_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) "
                "GROUP BY tarih ORDER BY tarih",
                (unit_uid, start_date, end_date),
            ).df()

        # Değerleri normalize et (0-100 scale → 0-1)
        for col in ["oee", "uygunluk", "performans", "kalite"]:
            if col in df.columns:
                max_val = df[col].max()
                if max_val > 1.0:
                    df[col] = df[col] / 100.0
        return df
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Kenar çubuğu
# ---------------------------------------------------------------------------
conn = get_connection()
machines = get_machines(conn)
min_date, max_date = get_date_range(conn)

st.sidebar.title("🦖 TRex OEE")
selected_machine = st.sidebar.selectbox("Makine", machines)

selected_date = st.sidebar.date_input(
    "Tarih",
    value=max_date,
    min_value=min_date,
    max_value=max_date,
)
date_str = str(selected_date)

mtype = machine_label(selected_machine)
st.sidebar.markdown(f"**Tip:** {mtype}")
st.sidebar.caption(f"Veri aralığı: {min_date} — {max_date}")

# ---------------------------------------------------------------------------
# Sekmeler
# ---------------------------------------------------------------------------
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "🔍 KNA (Kök Neden Analizi)", "🎛️ What-If"])




# ===========================================================================
# SEKME 1 — Dashboard
# ===========================================================================
with tab1:
    st.header(f"📊 OEE Dashboard — {selected_machine} / {date_str}")

    oee = cached_oee_summary(selected_machine, date_str)

    if not oee:
        st.warning("Bu tarih için veri bulunamadı.")
        st.info(f"💡 Veri aralığı: **{min_date}** ile **{max_date}** arasında. Sol panelden tarih seçin.")
    else:
        avail_pct = round(oee["availability"] * 100, 1)
        perf_pct  = round(oee["performance"]  * 100, 1)
        qual_pct  = round(oee["quality"]       * 100, 1)
        oee_pct   = round(oee["oee"]           * 100, 1)

        has_perf = oee.get("has_perf_data", True)
        has_qual = oee.get("has_qual_data", True)

        # --- 4 metrik kartı ---
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("OEE",         f"{oee_pct}%")
        c2.metric("Uygunluk (Availability)", f"{avail_pct}%")

        if not has_perf:
            c3.metric("Performans (Performance)", "—")
            c3.caption("⚠️ Veri yok")
        else:
            c3.metric("Performans (Performance)", f"{perf_pct}%")

        if not has_qual:
            c4.metric("Kalite (Quality)", "—")
            c4.caption("⚠️ Veri yok")
        else:
            c4.metric("Kalite (Quality)", f"{qual_pct}%")

        if not has_perf or not has_qual:
            st.warning(
                "⚠️ Bu makine/tarih için bazı OEE bileşenleri veritabanında mevcut değil. "
                "Performance veya Quality sıfır göründüğünde OEE hesabı etkilenir."
            )

        st.divider()

        col_gauge, col_bar = st.columns([1, 2])

        # --- Gösterge (Gauge) ---
        with col_gauge:
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number",
                value=oee_pct,
                title={"text": "OEE"},
                gauge={
                    "axis": {"range": [0, 100]},
                    "bar": {"color": "darkblue"},
                    "steps": [
                        {"range": [0, 60],  "color": "#e74c3c"},
                        {"range": [60, 80], "color": "#f39c12"},
                        {"range": [80, 100],"color": "#2ecc71"},
                    ],
                    "threshold": {
                        "line": {"color": "black", "width": 4},
                        "thickness": 0.75,
                        "value": oee_pct,
                    },
                },
            ))
            fig_gauge.update_layout(height=300, margin=dict(t=40, b=10))
            st.plotly_chart(fig_gauge, use_container_width=True)

        # --- OEE Bileşenleri çubuk grafiği ---
        with col_bar:
            bar_labels = ["Uygunluk", "Performans", "Kalite"]
            bar_values = [avail_pct, perf_pct, qual_pct]
            bar_colors = ["#3498db", "#9b59b6", "#1abc9c"]

            fig_bar = go.Figure(data=[
                go.Bar(
                    name=lbl, x=[lbl], y=[val], marker_color=col,
                    text=[f"{val}%" if val > 0 else "Veri yok"],
                    textposition="outside",
                )
                for lbl, val, col in zip(bar_labels, bar_values, bar_colors)
            ])
            fig_bar.update_layout(
                title="OEE Bileşenleri (%)",
                yaxis=dict(range=[0, 110], title="Değer (%)"),
                xaxis_title="Bileşen",
                height=300,
                showlegend=False,
                margin=dict(t=40, b=10),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        # --- Trend grafiği (son 30 gün) ---
        st.subheader("📈 Son 30 Günlük OEE Trendi")
        from datetime import timedelta
        trend_start = str(selected_date - timedelta(days=30))
        trend_df = cached_oee_trend(selected_machine, trend_start, date_str)

        if trend_df.empty:
            st.info("Trend verisi bulunamadı.")
        else:
            fig_trend = go.Figure()
            renk_map = {
                "oee":       ("#2c3e50", "OEE"),
                "uygunluk":  ("#3498db", "Uygunluk"),
                "performans":("#9b59b6", "Performans"),
                "kalite":    ("#1abc9c", "Kalite"),
            }
            for col_key, (color, label) in renk_map.items():
                if col_key in trend_df.columns and trend_df[col_key].max() > 0:
                    fig_trend.add_trace(go.Scatter(
                        x=trend_df["tarih"],
                        y=(trend_df[col_key] * 100).round(1),
                        mode="lines+markers",
                        name=label,
                        line=dict(color=color, width=2),
                    ))
            fig_trend.update_layout(
                yaxis=dict(range=[0, 100], title="Değer (%)"),
                xaxis_title="Tarih",
                height=320,
                margin=dict(t=30, b=10),
                legend=dict(orientation="h", y=-0.2),
            )
            st.plotly_chart(fig_trend, use_container_width=True)

        # --- Duruş listesi ---
        st.subheader("Duruş Listesi")
        stoppages = cached_stoppages(selected_machine, date_str)
        if stoppages.empty:
            st.info("Bu tarih için duruş kaydı bulunamadı.")
        else:
            df_display = stoppages.copy()
            if "duration_milliseconds" in df_display.columns:
                df_display["Süre"] = df_display["duration_milliseconds"].apply(
                    lambda ms: f"{int(ms/60000)} dk {int((ms%60000)/1000)} sn"
                    if pd.notna(ms) and ms > 0 else "-"
                )
            if "is_planned" in df_display.columns:
                df_display["Tür"] = df_display["is_planned"].map(
                    {True: "✅ Planlı", False: "❌ Plansız",
                     "t": "✅ Planlı", "f": "❌ Plansız"}
                )
            # Sütun adlarını Türkçeleştir
            rename_map = {
                "started_on": "Başlangıç",
                "ended_on":   "Bitiş",
                "duration_milliseconds": "Süre (ms)",
                "is_planned": "Planlı mı",
                "is_unit_on": "Ünite Açık",
                "exclude_from_oee": "OEE Dışı",
            }
            df_display = df_display.rename(columns=rename_map)
            st.dataframe(df_display, use_container_width=True)


# ===========================================================================
# SEKME 2 — KNA (Kök Neden Analizi)
# ===========================================================================
with tab2:
    st.header(f"🔍 Kök Neden Analizi — {selected_machine} / {date_str}")

    stoppages_df = cached_stoppages(selected_machine, date_str)

    if stoppages_df.empty:
        st.warning("Bu tarihte duruş verisi yok.")
    else:
        st.subheader("Duruşlar")

        # ✅ Kullanıcıya duruş seçtir
        options = stoppages_df.apply(
            lambda row: f"{row['started_on']} | "
                        f"{int(row.get('duration_milliseconds', 0)/60000)} dk | "
                        f"{'Planlı' if row.get('is_planned', False) else 'Plansız'}",
            axis=1
        )

        selected_option = st.selectbox("📍 Duruş Seç", options)

        # ✅ seçilen satırı bul
        selected_row = stoppages_df[options == selected_option].iloc[0]

        # ✅ seçilen duruşu göster
        st.markdown("### 📋 Seçilen Duruş")
        st.write(selected_row)
        st.divider()

        # ✅ timestamp üret
        stoppage_ts = int(pd.Timestamp(selected_row["started_on"]).timestamp() * 1000)

        # ✅ RCA çağır
        anomaly_df, root_cause = cached_rca(
            selected_machine,
            stoppage_ts,
            z_threshold=3.0,
        )

        # ✅ ANALİZ
        if anomaly_df is None or anomaly_df.empty:
            st.warning("Telemetry yok → fallback analiz")

            cause_list = []

            if selected_row.get("is_planned"):
                cause_list.append("Planlı bakım / planlı duruş")
            else:
                cause_list.append("Plansız üretim duruşu")

            if selected_row.get("duration_milliseconds", 0) > 300000:
                cause_list.append("Uzun süreli duruş (mekanik / operatör kaynaklı olabilir)")

            if cause_list:
                st.success("📌 Olası kök neden:")
                for c in cause_list:
                    st.write(f"- {c}")
            else:
                st.warning("Anlamlı veri bulunamadı")

        else:
            st.success("📌 Kök neden:")
            st.write(root_cause)



# ===========================================================================
# SEKME 3 — What-If
# ===========================================================================
with tab3:
    st.header(f"🎛️ What-If Simülasyonu — {selected_machine} / {date_str}")

    oee3 = cached_oee_summary(selected_machine, date_str)

    if not oee3:
        st.warning("Bu tarih için veri bulunamadı.")
        st.info(f"💡 Veri aralığı: **{min_date}** ile **{max_date}** arasında.")
    else:
        has_perf3 = oee3.get("has_perf_data", True)

        if not has_perf3:
            st.warning(
                "⚠️ Bu makine/tarih için **Performans** verisi mevcut değil. "
                "Plansız Duruş Azaltımı yalnızca Uygunluk'u etkiler; "
                "OEE sıfır kalmaya devam edebilir."
            )

        left_col, right_col = st.columns([1, 1])

        with left_col:
            st.subheader("Senaryo Parametreleri")
            unplanned_reduction = st.slider(
                "Plansız Duruş Azaltımı (%)", 0, 100, 0,
                help="Plansız duruş süresini bu oranda azalt"
            ) / 100
            planned_reduction = st.slider(
                "Planlı Duruş Azaltımı (%)", 0, 100, 0,
                help="Planlı duruş/bakım süresini bu oranda azalt"
            ) / 100
            scrap_rate = st.slider(
                "Hedef Fire Oranı (%)", 0, 10, 0,
                help="Ürün fire/ıskarta oranı"
            ) / 100
            parts_per_ms = st.number_input(
                "Parça/ms", value=0.00005, format="%.6f", min_value=0.0
            )
            price_per_part = st.number_input(
                "Parça Fiyatı (₺)", value=200.0, min_value=0.0
            )

        reduction_scenarios = {
            "unplanned_pct": unplanned_reduction,
            "planned_pct":   planned_reduction,
            "scrap_pct":     scrap_rate,
        }
        simulated = simulate_oee(oee3, reduction_scenarios)
        financial  = calculate_financial_gain(
            oee3["oee"], simulated["oee"],
            oee3["shift_ms"], parts_per_ms, price_per_part,
        )
        summary_str = get_whatif_summary(oee3, simulated, financial)

        with right_col:
            st.subheader("Sonuç Karşılaştırması")
            bc1, bc2 = st.columns(2)

            base_oee_pct = round(oee3["oee"]          * 100, 1)
            base_a_pct   = round(oee3["availability"]  * 100, 1)
            base_p_pct   = round(oee3["performance"]   * 100, 1)
            base_q_pct   = round(oee3["quality"]       * 100, 1)

            new_oee_pct  = round(simulated["oee"]          * 100, 1)
            new_a_pct    = round(simulated["availability"]  * 100, 1)
            new_p_pct    = round(simulated["performance"]   * 100, 1)
            new_q_pct    = round(simulated["quality"]       * 100, 1)

            with bc1:
                st.markdown("**⬅️ Önce**")
                st.metric("OEE",          f"{base_oee_pct}%")
                st.metric("Uygunluk",     f"{base_a_pct}%")
                st.metric("Performans",   f"{base_p_pct}%" if has_perf3 else "—")
                st.metric("Kalite",       f"{base_q_pct}%" if oee3.get("has_qual_data") else "—")

            with bc2:
                st.markdown("**➡️ Sonra**")
                st.metric("OEE",          f"{new_oee_pct}%",
                          delta=f"{new_oee_pct - base_oee_pct:+.1f}")
                st.metric("Uygunluk",     f"{new_a_pct}%",
                          delta=f"{new_a_pct - base_a_pct:+.1f}")
                st.metric("Performans",   f"{new_p_pct}%" if has_perf3 else "—",
                          delta=f"{new_p_pct - base_p_pct:+.1f}" if has_perf3 else None)
                st.metric("Kalite",       f"{new_q_pct}%",
                          delta=f"{new_q_pct - base_q_pct:+.1f}")

        st.divider()
        st.success(summary_str)

        # --- Önce / Sonra karşılaştırma grafiği ---
        metrics_labels = ["OEE", "Uygunluk", "Performans", "Kalite"]
        before_vals = [base_oee_pct, base_a_pct, base_p_pct, base_q_pct]
        after_vals  = [new_oee_pct,  new_a_pct,  new_p_pct,  new_q_pct]

        fig_wi = go.Figure(data=[
            go.Bar(name="Önce", x=metrics_labels, y=before_vals,
                   marker_color="#95a5a6"),
            go.Bar(name="Sonra", x=metrics_labels, y=after_vals,
                   marker_color="#2ecc71"),
        ])
        fig_wi.update_layout(
            title="Önce / Sonra Karşılaştırması (%)",
            yaxis=dict(range=[0, 110], title="Değer (%)"),
            xaxis_title="OEE Bileşeni",
            barmode="group",
            height=350,
        )
        st.plotly_chart(fig_wi, use_container_width=True)

        extra_parts  = int(financial.get("extra_parts", 0))
        revenue_gain = financial.get("revenue_gain", 0.0)
        st.info(
            f"💰 **Ekstra Parça:** ~{extra_parts} adet\n\n"
            f"💵 **Tahmini Kazanç:** ₺{revenue_gain:,.0f}"
        )