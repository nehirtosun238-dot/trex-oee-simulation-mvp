"""
db_engine.py - DuckDB connection and data access layer.
"""

from datetime import datetime, timedelta
import json
import duckdb
import pandas as pd
import pathlib
import streamlit as st

SHIFT_MS = 28_800_000
DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    DATA_DIR_STR = str(DATA_DIR).replace("\\", "/")
    conn = duckdb.connect(database=":memory:", read_only=False)

    # --- Telemetri ---
    conn.execute(
        f"CREATE OR REPLACE VIEW telemetry AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_nightwatch_data_*.csv', "
        f"ignore_errors=true)"
    )

    # --- String / alarm log (encoding: latin-1) ---
    try:
        conn.execute(
            f"CREATE OR REPLACE VIEW alarms AS "
            f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_nightwatch_data_string_*.csv', "
            f"ignore_errors=true, encoding='latin-1')"
        )
    except Exception:
        conn.execute(
            f"CREATE OR REPLACE VIEW alarms AS "
            f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_nightwatch_data_string_*.csv', "
            f"ignore_errors=true)"
        )

    # --- Makine hiyerarşisi (uid -> name) ---
    conn.execute(
        f"CREATE OR REPLACE TABLE mes_unit AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_unit.csv', ignore_errors=true)"
    )

    # --- OEE özet ---
    conn.execute(
        f"CREATE OR REPLACE TABLE oee_summary AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_oee_summary.csv', ignore_errors=true)"
    )

    # --- Duruşlar (gerçek duruş verisi) ---
    conn.execute(
        f"CREATE OR REPLACE TABLE stoppages AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_stoppage_slice.csv', ignore_errors=true)"
    )

    # --- Sayaçlar ---
    conn.execute(
        f"CREATE OR REPLACE TABLE counters AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_counter_slice.csv', ignore_errors=true)"
    )

    # --- İş emirleri ---
    conn.execute(
        f"CREATE OR REPLACE TABLE workorders AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_workorder.csv', ignore_errors=true)"
    )

    # --- Alerts (latin-1 encoding) ---
    try:
        conn.execute(
            f"CREATE OR REPLACE TABLE alerts AS "
            f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_alert.csv', "
            f"ignore_errors=true, encoding='latin-1')"
        )
    except Exception:
        conn.execute(
            f"CREATE OR REPLACE TABLE alerts AS "
            f"SELECT * FROM read_csv_auto('{DATA_DIR_STR}/trex_mes_alert.csv', ignore_errors=true)"
        )

    return conn


def _get_unit_uid(machine_id: str, conn) -> str | None:
    """
    mes_unit tablosundan name'e karşılık gelen uid'i döner.
    machine_id hem name (ör: 'TurboCut 400') hem uid olabilir.
    """
    try:
        df = conn.execute(
            "SELECT uid FROM mes_unit WHERE uid = ? LIMIT 1",
            (machine_id,),
        ).df()
        if not df.empty:
            return str(df.iloc[0, 0])

        df = conn.execute(
            "SELECT uid FROM mes_unit WHERE name = ? LIMIT 1",
            (machine_id,),
        ).df()
        if not df.empty:
            return str(df.iloc[0, 0])

        df = conn.execute(
            "SELECT uid FROM mes_unit WHERE name LIKE ? LIMIT 1",
            (f"%{machine_id}%",),
        ).df()
        return str(df.iloc[0, 0]) if not df.empty else None
    except Exception:
        return None


def get_machines(conn) -> list[str]:
    """Makine listesini name olarak döner (UUID değil)."""
    try:
        df = conn.execute(
            "SELECT DISTINCT name FROM mes_unit WHERE name IS NOT NULL ORDER BY name"
        ).df()
        machines = df["name"].tolist()
        if machines:
            return machines
    except Exception:
        pass

    try:
        df = conn.execute(
            "SELECT DISTINCT unit_uid FROM oee_summary ORDER BY unit_uid"
        ).df()
        if not df.empty:
            return df.iloc[:, 0].tolist()
    except Exception:
        pass

    return ["Makine 1", "Makine 2", "Makine 3", "Makine 4", "Makine 5"]


def _parse_oee_field(value, fallback: float = 0.0) -> float:
    """
    OEE alanını float'a çevirir. NULL/hatalı değer → fallback (varsayılan 0.0).
    NOT: quality için de artık fallback=0.0 kullanılmalı, 1.0 değil.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return fallback
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        parsed = json.loads(s)
        if isinstance(parsed, (int, float)):
            return float(parsed)
        if isinstance(parsed, list) and parsed:
            return float(parsed[0])
        if isinstance(parsed, dict):
            for v in parsed.values():
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    except (json.JSONDecodeError, TypeError):
        pass
    return fallback


def _normalize_oee_value(value: float) -> float:
    """
    OEE değerini 0-1 aralığına normalize eder ve veri setindeki 
    %100'ü (1.0) aşan üretim anomalilerini temizleyerek 1.0'a sabitler.
    """
    # 1. Adım: Eğer değer 2.0'dan büyükse, muhtemelen 0-100 formatında girilmiştir (Örn: 85.0 -> %85)
    if value > 2.0:
        value = value / 100.0
        
    # 2. Adım: Veri temizliği (Capping) - Eğer değer 1.0'ı aşıyorsa (Örn: 1.15 veya 1.04), 1.0'a eşitle
    if value > 1.0:
        value = 1.0
        
    # 3. Adım: Eksi veya hatalı değerlere karşı alt sınırı da 0.0 yap
    return max(0.0, value)


def get_oee_summary(machine_id: str, date_str: str, conn) -> dict:
    """
    Makine adı (name) + tarih için OEE dict döner.
    mes_unit.name -> mes_unit.uid -> oee_summary.unit_uid join.
    """
    unit_uid = _get_unit_uid(machine_id, conn)
    if not unit_uid:
        return {}

    try:
        oee_cols = conn.execute("DESCRIBE oee_summary").df()["column_name"].tolist()
    except Exception:
        return {}

    date_col = next((c for c in ["trans_date", "date", "day", "shift_date"] if c in oee_cols), None)
    if not date_col:
        return {}

    df = pd.DataFrame()

    for use_level in [True, False]:
        if not df.empty:
            break
        level_clause = "AND level = 1" if use_level else ""
        try:
            df = conn.execute(
                f"SELECT * FROM oee_summary "
                f"WHERE unit_uid = ? "
                f"AND CAST({date_col} AS DATE) = CAST(? AS DATE) "
                f"{level_clause}",
                (unit_uid, date_str),
            ).df()
        except Exception:
            pass

    if df.empty:
        return {}

    if "level" in df.columns and len(df) > 1:
        lvl1 = df[df["level"] == 1]
        if not lvl1.empty:
            df = lvl1

    row = df.iloc[0]

    # DÜZELTME: quality fallback 0.0 (önceden 1.0 idi — her zaman %100 görünüyordu)
    a_raw = _parse_oee_field(row.get("availability"), 0.0)
    p_raw = _parse_oee_field(row.get("performance"),  0.0)
    q_raw = _parse_oee_field(row.get("quality"),      0.0)  # ← düzeltildi

    # DÜZELTME: 0-100 scale tespiti ve normalizasyon
    a = _normalize_oee_value(a_raw)
    p = _normalize_oee_value(p_raw)
    q = _normalize_oee_value(q_raw)

    unplanned_ms = _parse_oee_field(row.get("unplanned_ms"), 0.0) if "unplanned_ms" in oee_cols else 0.0
    planned_ms   = _parse_oee_field(row.get("planned_ms"),   0.0) if "planned_ms"   in oee_cols else 0.0

    # Availability sıfırsa unplanned_ms'den hesapla
    if a == 0.0 and unplanned_ms > 0:
        a = max(0.0, (SHIFT_MS - unplanned_ms) / SHIFT_MS)

    oee_raw = _parse_oee_field(row.get("oee"), None)
    if oee_raw is not None:
        oee_val = _normalize_oee_value(oee_raw)
    else:
        oee_val = a * p * q if p > 0 else 0.0

    # Veri yoksa None döndür (UI'da "Veri yok" göstermek için)
    has_perf_data = p_raw != 0.0
    has_qual_data = q_raw != 0.0

    return {
        "availability":    max(0.0, min(1.0, a)),
        "performance":     max(0.0, min(1.0, p)),
        "quality":         max(0.0, min(1.0, q)),
        "oee":             max(0.0, min(1.0, oee_val)),
        "unplanned_ms":    unplanned_ms,
        "planned_ms":      planned_ms,
        "shift_ms":        SHIFT_MS,
        "has_perf_data":   has_perf_data,   # Performance verisi var mı?
        "has_qual_data":   has_qual_data,   # Quality verisi var mı?
    }


def get_stoppages(machine_id: str, date_str: str, conn) -> pd.DataFrame:
    """
    Makine adı + tarih için duruş DataFrame döner.
    stoppages sütunları: unit_uid, started_on (datetime string), duration_milliseconds
    """
    unit_uid = _get_unit_uid(machine_id, conn)
    if not unit_uid:
        return pd.DataFrame()

    for query in [
        (
            "SELECT * FROM stoppages "
            "WHERE unit_uid = ? "
            "AND CAST(started_on AS DATE) = CAST(? AS DATE) "
            "ORDER BY started_on",
            (unit_uid, date_str),
        ),
        (
            "SELECT * FROM stoppages "
            "WHERE unit_uid = ? "
            "AND started_on LIKE ? "
            "ORDER BY started_on",
            (unit_uid, f"{date_str}%"),
        ),
    ]:
        try:
            df = conn.execute(query[0], query[1]).df()
            if not df.empty:
                display_cols = [c for c in [
                    "started_on", "ended_on", "duration_milliseconds",
                    "is_planned", "is_unit_on", "exclude_from_oee"
                ] if c in df.columns]
                return df[display_cols] if display_cols else df
        except Exception:
            continue

    return pd.DataFrame()


def get_rca_window(
    machine_id: str,
    stoppage_ts: int,
    conn,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    unit_uid = _get_unit_uid(machine_id, conn)

    # ✅ stoppage timestamp → datetime (SAFE)
    try:
        stoppage_dt = datetime.fromtimestamp(stoppage_ts / 1000)
    except Exception:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # ✅ DAHA GENİŞ WINDOW (çok önemli)
    lo_dt = stoppage_dt - timedelta(hours=3)
    hi_dt = stoppage_dt + timedelta(hours=1)

    def _query_window(table):

        try:
            df = conn.execute(f"SELECT * FROM {table}").df()
        except Exception:
            return pd.DataFrame()

        if df.empty:
            return df

        ts_col = next(
            (c for c in df.columns if "time" in c.lower() or "ts" in c.lower()),
            None,
        )

        if not ts_col:
            return pd.DataFrame()

        # ✅ timestamp normalize
        try:
            df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
        except Exception:
            return pd.DataFrame()

        # ✅ machine filter
        if "unit_uid" in df.columns:
            df = df[df["unit_uid"] == unit_uid]

        # ✅ time filter
        df = df[(df[ts_col] >= lo_dt) & (df[ts_col] <= hi_dt)]

        return df

    telemetry_df = _query_window("telemetry")
    alarms_df    = _query_window("alarms")
    alerts_df    = _query_window("alerts")

    return telemetry_df, alarms_df, alerts_df
    def _get_ts_col_and_scale(table: str):
        """
        Tablodaki timestamp sütununu ve birimini (ms vs s) döndürür.
        Örnek değerlere bakarak tahmin yapar.
        """
        try:
            cols = conn.execute(f"DESCRIBE {table}").df()["column_name"].tolist()
        except Exception:
            return None, None, None

        ts_col = next(
            (c for c in ["timestamp", "ts", "time_ms", "event_ts", "created_at", "time"] if c in cols),
            None,
        )
        if not ts_col:
            return None, None, cols

        # Örnek değer oku — ms mi s mi anlayalım
        try:
            sample = conn.execute(f"SELECT {ts_col} FROM {table} LIMIT 5").df()
            sample_vals = sample[ts_col].dropna().tolist()
            if sample_vals:
                sample_val = float(sample_vals[0])
                # Unix ms genellikle 1.6e12 civarı (2020+), saniye ise 1.6e9
                if sample_val > 1e11:
                    scale = "ms"
                else:
                    scale = "s"
            else:
                scale = "ms"
        except Exception:
            scale = "ms"

        return ts_col, scale, cols

    def _query_window(table, uid_val):
        ts_col, scale, cols = _get_ts_col_and_scale(table)
        if not ts_col or not cols:
            return pd.DataFrame()

        uid_col = next((c for c in ["unit_uid", "uid", "machine_id"] if c in cols), None)

        lo = lo_ms if scale == "ms" else lo_s
        hi = hi_ms if scale == "ms" else hi_s

        # uid filtreli dene
        for try_uid in ([uid_val, machine_id] if uid_val != machine_id else [uid_val]):
            if not try_uid:
                continue
            try:
                if uid_col:
                    df = conn.execute(
                        f"SELECT * FROM {table} "
                        f"WHERE {uid_col} = ? AND {ts_col} BETWEEN ? AND ? "
                        f"ORDER BY {ts_col}",
                        (try_uid, lo, hi),
                    ).df()
                else:
                    df = conn.execute(
                        f"SELECT * FROM {table} "
                        f"WHERE {ts_col} BETWEEN ? AND ? "
                        f"ORDER BY {ts_col}",
                        (lo, hi),
                    ).df()
                if not df.empty:
                    # ts_col'u ms'e normalize et (RCA analizi ms bekliyor)
                    if scale == "s":
                        df = df.copy()
                        df[ts_col] = df[ts_col] * 1000
                    return df
            except Exception:
                continue

        return pd.DataFrame()

    telemetry_df = _query_window("telemetry", unit_uid)
    alarms_df    = _query_window("alarms",    unit_uid)
    alerts_df    = _query_window("alerts",    unit_uid)

    return telemetry_df, alarms_df, alerts_df


def get_date_range(conn) -> tuple:
    """
    OEE verisinin tarih aralığını döner.
    DÜZELTME: Sadece level=1 (makine seviyesi) kayıtlar kullanılır — outlier tarihleri elenir.
    """
    # Önce level=1 ile dene
    for level_clause in ["WHERE level = 1", ""]:
        try:
            df = conn.execute(
                f"SELECT MIN(CAST(trans_date AS DATE)) as min_d, "
                f"MAX(CAST(trans_date AS DATE)) as max_d "
                f"FROM oee_summary {level_clause}"
            ).df()
            min_d = df.iloc[0]["min_d"]
            max_d = df.iloc[0]["max_d"]
            if min_d and max_d:
                return min_d, max_d
        except Exception:
            pass

    from datetime import date
    return date(2025, 11, 1), date(2025, 11, 30)


def inspect_tables(conn) -> dict:
    result = {}
    for tbl in ["oee_summary", "stoppages", "alerts", "mes_unit", "counters", "workorders"]:
        try:
            result[tbl] = conn.execute(f"DESCRIBE {tbl}").df()["column_name"].tolist()
        except Exception as e:
            result[tbl] = f"HATA: {e}"
    try:
        result["telemetry_sample"] = conn.execute("SELECT * FROM telemetry LIMIT 2").df().columns.tolist()
    except Exception as e:
        result["telemetry_sample"] = f"HATA: {e}"
    return result