"""
rca_engine.py — Kök Neden Analizi (RCA) motoru.

Yenilikler (v2):
  - get_rca_window() artik 3 df doner: telemetry, alarms, alerts
  - trex_mes_alert tablosu RCA'ya entegre edildi
  - Pareto chart verisi (get_pareto_data) eklendi
  - AI yorum icin structured context builder eklendi
"""

import pandas as pd
import numpy as np

from src.db_engine import get_rca_window

# ---------------------------------------------------------------------------
# Makine tipi
# ---------------------------------------------------------------------------
MACHINE_TYPE: dict[str, str] = {
    "M1": "Fanuc CNC",
    "M2": "Mitsubishi CNC",
    "M3": "Nukon Laser",
    "M4": "Fanuc CNC",
    "M5": "Mitsubishi CNC",
    "M6": "Nukon Laser",
}

# ---------------------------------------------------------------------------
# Alarm anahtar kelime sozlugu
# ---------------------------------------------------------------------------
ALARM_KEYWORDS: list[tuple[list[str], str]] = [
    (["spindle", "spindel"],                         "Spindle Fault"),
    (["servo", "axis", "eksen"],                     "Servo / Axis Error"),
    (["coolant", "sogutma", "coolant_pressure"],     "Coolant System"),
    (["laser", "power", "guc", "beam"],              "Laser Power Issue"),
    (["door", "kapi", "interlock", "guard"],         "Safety Interlock"),
    (["estop", "e-stop", "emergency"],               "Emergency Stop"),
    (["overload", "asiri yuk", "overcurrent"],       "Motor Overload"),
    (["temperature", "temp", "sicaklik", "thermal"], "Thermal Alarm"),
    (["lubrication", "yag", "oil"],                  "Lubrication"),
    (["program", "nc", "macro", "subprogram"],       "NC Program Error"),
    (["tool", "takim", "broken", "kirik"],           "Tool Break"),
    (["material", "workpiece", "baglama", "fixture"],"Fixturing / Material"),
    (["network", "communication", "baglanti"],       "Communication Fault"),
    (["power", "voltage", "gerilim", "supply"],      "Power Supply"),
]

UNKNOWN_CAUSE = "Unknown — manual review required"


# ---------------------------------------------------------------------------
# Iç yardimcilar
# ---------------------------------------------------------------------------

def _match_alarm_keyword(text: str) -> str:
    lower = text.lower()
    for keywords, category in ALARM_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category
    return UNKNOWN_CAUSE


def _find_timestamp_col(df: pd.DataFrame) -> str:
    for col in df.columns:
        if "time" in col.lower() or col.lower() in ("ts", "timestamp", "event_ts", "created_at"):
            return col
    raise ValueError(f"Timestamp sutunu bulunamadi: {list(df.columns)}")


def _detect_anomalies(
    df: pd.DataFrame,
    z_threshold: float = 3.0,
    rolling_window: int = 20,
) -> pd.DataFrame:
    """
    Numeric telemetri kanallar icin Z-Score anomali tespiti.

    Cikti sutunlari:
        timestamp | channel | value | rolling_mean | rolling_std | z_score | is_anomaly
    """
    empty = pd.DataFrame(columns=[
        "timestamp", "channel", "value",
        "rolling_mean", "rolling_std", "z_score", "is_anomaly",
    ])

    if df.empty:
        return empty

    try:
        ts_col = _find_timestamp_col(df)
    except ValueError:
        return empty

    df = df.copy()
    df[ts_col] = pd.to_datetime(df[ts_col], unit="ms", errors="coerce").fillna(
        pd.to_datetime(df[ts_col], errors="coerce")
    )
    df = df.sort_values(ts_col).reset_index(drop=True)

    skip_cols = {ts_col, "machine_id", "unit_uid", "uid", "id"}
    numeric_cols = [
        c for c in df.columns
        if c not in skip_cols and pd.api.types.is_numeric_dtype(df[c])
    ]

    if not numeric_cols:
        return empty

    records: list[dict] = []
    for col in numeric_cols:
        series = df[col].ffill().fillna(0)
        roll   = series.rolling(window=rolling_window, min_periods=1)
        r_mean = roll.mean()
        r_std  = roll.std().fillna(0)
        safe_std = r_std.replace(0, np.nan)
        z = ((series - r_mean) / safe_std).fillna(0)

        for i, ts in enumerate(df[ts_col]):
            records.append({
                "timestamp":    ts,
                "channel":      col,
                "value":        float(series.iloc[i]),
                "rolling_mean": round(float(r_mean.iloc[i]), 4),
                "rolling_std":  round(float(r_std.iloc[i]), 4),
                "z_score":      round(float(z.iloc[i]), 3),
                "is_anomaly":   bool(abs(z.iloc[i]) >= z_threshold),
            })

    result = pd.DataFrame(records)
    return result.sort_values(["timestamp", "channel"]).reset_index(drop=True)


def _classify_text_source(df: pd.DataFrame) -> list[tuple[str, str]]:
    """
    DataFrame'deki text sutunlarini anahtar kelime sozluguyle siniflandir.
    (hem alarms hem alerts icin kullanilir)
    """
    if df.empty:
        return []
    text_cols = [c for c in df.columns if df[c].dtype == object]
    results: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        combined = " ".join(str(row[c]) for c in text_cols if pd.notna(row[c]))
        category = _match_alarm_keyword(combined)
        results.append((combined[:120].strip(), category))
    return results


def _dominant_anomaly_channels(anomaly_df: pd.DataFrame, top_n: int = 3) -> list[str]:
    if anomaly_df.empty:
        return []
    flagged = anomaly_df[anomaly_df["is_anomaly"]]
    if flagged.empty:
        return []
    ranked = (
        flagged.groupby("channel")["z_score"]
        .apply(lambda s: s.abs().mean())
        .sort_values(ascending=False)
    )
    return ranked.head(top_n).index.tolist()


def _build_root_cause_string(
    machine_id: str,
    anomaly_df: pd.DataFrame,
    alarm_categories: list[str],
    alert_categories: list[str],
) -> str:
    machine_type = MACHINE_TYPE.get(machine_id, "Unknown machine type")
    parts: list[str] = [f"[{machine_id} — {machine_type}]"]

    # Alarm kategorileri
    seen: set[str] = set()
    unique_alarms: list[str] = []
    for c in alarm_categories + alert_categories:
        if c != UNKNOWN_CAUSE and c not in seen:
            seen.add(c)
            unique_alarms.append(c)
    if unique_alarms:
        parts.append("Sinyal: " + ", ".join(unique_alarms))

    # Anomali kanallar
    dominant = _dominant_anomaly_channels(anomaly_df)
    if dominant:
        parts.append("Anomali kanallar: " + ", ".join(dominant))

    # Anomali sayisi
    n_anomalies = int(anomaly_df["is_anomaly"].sum()) if not anomaly_df.empty else 0
    if n_anomalies:
        parts.append(f"{n_anomalies} anomali noktasi [-15dk, +5dk] penceresinde")

    if len(parts) == 1:
        parts.append(UNKNOWN_CAUSE)

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_rca_analysis(
    machine_id: str,
    stoppage_ts: int,
    conn,
    z_threshold: float = 3.0,
    rolling_window: int = 20,
) -> tuple[pd.DataFrame, str]:
    """
    Tek bir duruş olayı için RCA çalıştırır.

    Doner:
        anomaly_df   — anomali bayraklı tidy DataFrame
        root_cause   — okunabilir kök neden özeti
    """
    telemetry_df, alarms_df, alerts_df = get_rca_window(machine_id, stoppage_ts, conn)

    anomaly_df = _detect_anomalies(
        telemetry_df,
        z_threshold=z_threshold,
        rolling_window=rolling_window,
    )

    alarm_matches   = _classify_text_source(alarms_df)
    alert_matches   = _classify_text_source(alerts_df)

    alarm_categories = [cat for _, cat in alarm_matches]
    alert_categories = [cat for _, cat in alert_matches]

    root_cause = _build_root_cause_string(
        machine_id=machine_id,
        anomaly_df=anomaly_df,
        alarm_categories=alarm_categories,
        alert_categories=alert_categories,
    )

    return anomaly_df, root_cause


def get_anomaly_summary(anomaly_df: pd.DataFrame) -> pd.DataFrame:
    """Kanal basina anomali ozeti — UI tablosu icin."""
    if anomaly_df.empty or "is_anomaly" not in anomaly_df.columns:
        return pd.DataFrame()

    flagged = anomaly_df[anomaly_df["is_anomaly"]].copy()
    if flagged.empty:
        return pd.DataFrame(
            columns=["channel", "anomaly_count", "max_z", "mean_z", "first_anomaly_ts"]
        )

    summary = (
        flagged.groupby("channel")
        .agg(
            anomaly_count    = ("is_anomaly", "sum"),
            max_z            = ("z_score",    lambda s: round(s.abs().max(), 3)),
            mean_z           = ("z_score",    lambda s: round(s.abs().mean(), 3)),
            first_anomaly_ts = ("timestamp",  "min"),
        )
        .sort_values("max_z", ascending=False)
        .reset_index()
    )
    return summary


def get_pareto_data(machine_id: str, conn) -> pd.DataFrame:
    """
    Duruş nedeni Pareto verisi — RCA sekmesinde bar chart için.
    stoppages + alerts tablolarından duruş kategorilerini sayar.
    """
    records: list[dict] = []

    # Stoppages tablosundan reason/category sutunu
    try:
        stop_cols = conn.execute("DESCRIBE stoppages").df()["column_name"].tolist()
        reason_col = next(
            (c for c in ["reason", "category", "stoppage_type", "description"] if c in stop_cols),
            None,
        )
        uid_col = next((c for c in ["unit_uid", "uid", "machine_id"] if c in stop_cols), None)

        if reason_col and uid_col:
            df = conn.execute(
                f"SELECT {reason_col} as category, COUNT(*) as count "
                f"FROM stoppages WHERE {uid_col} = ? "
                f"GROUP BY {reason_col} ORDER BY count DESC LIMIT 10",
                (machine_id,),
            ).df()
            records.extend(df.to_dict("records"))
    except Exception:
        pass

    # Alerts tablosundan alarm_type/severity
    try:
        alt_cols = conn.execute("DESCRIBE alerts").df()["column_name"].tolist()
        type_col = next(
            (c for c in ["alarm_type", "category", "type", "severity", "description"] if c in alt_cols),
            None,
        )
        uid_col2 = next((c for c in ["unit_uid", "uid", "machine_id"] if c in alt_cols), None)

        if type_col and uid_col2:
            df2 = conn.execute(
                f"SELECT {type_col} as category, COUNT(*) as count "
                f"FROM alerts WHERE {uid_col2} = ? "
                f"GROUP BY {type_col} ORDER BY count DESC LIMIT 10",
                (machine_id,),
            ).df()
            records.extend(df2.to_dict("records"))
    except Exception:
        pass

    if not records:
        return pd.DataFrame(columns=["category", "count", "cumulative_pct"])

    pareto = (
        pd.DataFrame(records)
        .groupby("category")["count"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    total = pareto["count"].sum()
    pareto["cumulative_pct"] = (pareto["count"].cumsum() / total * 100).round(1)
    return pareto


def build_ai_context(
    machine_id: str,
    stoppage_ts: int,
    anomaly_df: pd.DataFrame,
    root_cause: str,
    pareto_df: pd.DataFrame,
) -> str:
    """
    Anthropic API'ye gönderilecek yapılandırılmış bağlam metni.
    RCA Tab'daki AI yorum butonu bu fonksiyonu kullanır.
    """
    machine_type = MACHINE_TYPE.get(machine_id, "Unknown")
    n_anom = int(anomaly_df["is_anomaly"].sum()) if not anomaly_df.empty else 0
    dominant = _dominant_anomaly_channels(anomaly_df)

    top_pareto = ""
    if not pareto_df.empty:
        top5 = pareto_df.head(5)
        top_pareto = "\n".join(
            f"  - {r['category']}: {int(r['count'])} olay ({r['cumulative_pct']}% kümülatif)"
            for _, r in top5.iterrows()
        )

    ctx = f"""
Makine: {machine_id} ({machine_type})
Duruş Timestamp: {stoppage_ts} ms
Pencere: [-15 dakika, +5 dakika]

Kök Neden Özeti:
{root_cause}

Anomali İstatistikleri:
  - Toplam anomali noktası: {n_anom}
  - En çok etkilenen kanallar: {', '.join(dominant) if dominant else 'Yok'}

Top-5 Duruş Kategorisi (Pareto):
{top_pareto if top_pareto else '  Veri yok'}
""".strip()

    return ctx