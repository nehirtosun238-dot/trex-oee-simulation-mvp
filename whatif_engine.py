"""
whatif_engine.py
OEE iyileştirme senaryolarını simüle eder ve finansal etkiyi hesaplar.
"""

SHIFT_MS = 28_800_000


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def simulate_oee(base_oee_dict: dict, reduction_scenarios: dict) -> dict:
    unplanned_ms = base_oee_dict.get("unplanned_ms", 0.0)
    planned_ms   = base_oee_dict.get("planned_ms",   0.0)
    shift_ms     = base_oee_dict.get("shift_ms",     SHIFT_MS)
    performance  = base_oee_dict.get("performance",  0.0)
    quality      = base_oee_dict.get("quality",      0.0)

    unplanned_pct = reduction_scenarios.get("unplanned_pct", 0.0)
    planned_pct   = reduction_scenarios.get("planned_pct",   0.0)
    scrap_pct     = reduction_scenarios.get("scrap_pct",     0.0)

    # ✅ Güvenli azaltım (negatif engelle)
    new_unplanned_ms = max(0, unplanned_ms * (1.0 - unplanned_pct))
    new_planned_ms   = max(0, planned_ms   * (1.0 - planned_pct))

    # ✅ Availability (clamp ile)
    if shift_ms > 0:
        new_a = (shift_ms - new_unplanned_ms) / shift_ms
    else:
        new_a = 0.0

    new_a = max(0, min(1, new_a))  # clamp

    # ✅ Performance FIX
    perf_data_missing = (performance == 0.0)

    if perf_data_missing:
        new_p = 0.0
    else:
        new_p = performance  # doğru karar (senin mantığın doğru)

    new_p = max(0, min(1, new_p))

    # ✅ Quality FIX (en kritik düzeltmelerden biri)
    if quality == 0.0 and scrap_pct == 0.0:
        new_q = 0.0
    else:
        base_q = quality if quality > 0 else 1.0
        new_q = base_q * (1.0 - scrap_pct)

    new_q = max(0, min(1, new_q))

    # ✅ OEE
    new_oee = new_a * new_p * new_q
    new_oee = max(0, min(1, new_oee))

    return {
        "availability":       new_a,
        "performance":        new_p,
        "quality":            new_q,
        "oee":                new_oee,
        "new_unplanned_ms":   new_unplanned_ms,
        "new_planned_ms":     new_planned_ms,
        "perf_data_missing":  perf_data_missing,
    }


def calculate_financial_gain(
    base_oee: float,
    new_oee: float,
    shift_ms: float,
    parts_per_ms: float,
    price_per_part: float
) -> dict:
    """OEE deltasının finansal etkisini hesaplar."""
    delta_oee     = new_oee - base_oee
    delta_oee_pct = delta_oee * 100.0
    extra_parts   = delta_oee * shift_ms * parts_per_ms
    revenue_gain  = extra_parts * price_per_part

    return {
        "delta_oee":     delta_oee,
        "delta_oee_pct": delta_oee_pct,
        "extra_parts":   extra_parts,
        "revenue_gain":  revenue_gain,
    }


def get_whatif_summary(base: dict, simulated: dict, financial: dict) -> str:
    """What-If analiz sonuçlarının özet metnini üretir."""
    b_oee = base.get("oee", 0.0) * 100
    b_a   = base.get("availability", 0.0) * 100
    b_p   = base.get("performance",  0.0) * 100
    b_q   = base.get("quality",      0.0) * 100

    s_oee = simulated.get("oee", 0.0) * 100
    s_a   = simulated.get("availability", 0.0) * 100
    s_p   = simulated.get("performance",  0.0) * 100
    s_q   = simulated.get("quality",      0.0) * 100

    delta_pts   = s_oee - b_oee
    extra_parts = int(financial.get("extra_parts", 0.0))
    revenue     = financial.get("revenue_gain", 0.0)

    perf_warn = ""
    if simulated.get("perf_data_missing", False):
        perf_warn = "\n⚠️ Performance verisi mevcut değil — OEE simülasyonu sadece Availability üzerinden hesaplanıyor."

    summary = (
        f"OEE: %{b_oee:.1f} → %{s_oee:.1f} (+{delta_pts:.1f} puan)\n"
        f"Uygunluk: %{b_a:.1f} → %{s_a:.1f}\n"
        f"Performans: %{b_p:.1f} → %{s_p:.1f}\n"
        f"Kalite: %{b_q:.1f} → %{s_q:.1f}\n"
        f"Ekstra Parça: ~{extra_parts} adet | Kazanç: ₺{revenue:,.0f}"
        f"{perf_warn}"
    )
    return summary


if __name__ == "__main__":
    base = {
        "availability": 0.82,
        "performance":  0.91,
        "quality":      0.98,
        "unplanned_ms": 3_000_000,
        "planned_ms":   1_800_000,
        "shift_ms":     28_800_000,
    }
    scenarios = {"unplanned_pct": 0.30, "planned_pct": 0.20, "scrap_pct": 0.02}
    sim = simulate_oee(base, scenarios)
    fin = calculate_financial_gain(
        base_oee=base["availability"] * base["performance"] * base["quality"],
        new_oee=sim["oee"],
        shift_ms=base["shift_ms"],
        parts_per_ms=0.00005,
        price_per_part=200,
    )
    summary = get_whatif_summary(
        {**base, "oee": base["availability"] * base["performance"] * base["quality"]},
        sim, fin,
    )
    print(summary)