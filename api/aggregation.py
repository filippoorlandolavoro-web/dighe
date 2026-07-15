"""Server-side port of the aggregation logic from the web dashboard's script.js.

Comments reference the original script.js function names/line ranges for
traceability while porting (getAnnualData, getCompleteData,
calculateYearlyEstimateFromPartialData, interpolateData).

Two deliberate deviations from script.js, agreed with the project owner:
1. Volume (lordo/netto) averages use their own valid-month count, instead of
   reusing quota's valid-month count (script.js bug: a month with a valid
   quota but a null volume silently contributed 0 to the volume average).
2. A month/year is considered "valid" if ANY of quota/lordo/netto is present,
   not only quota. script.js's quota-only check misclassifies dams that
   stopped reporting quota_slm_attuale but still report lordo_mc_attuale in
   later years (see README_PROGETTO.md) as having no data at all.
"""

LEVEL_FIELDS = ["quota_slm_attuale", "lordo_mc_attuale", "netto_mc_attuale"]

_YEAR_FIELDS = LEVEL_FIELDS + [
    "quota_max_slm",
    "lordo_max_mc",
    "pioggia_mm_attuale",
    "neve_cm_attuale",
]


def _row_month(row):
    return int(row["data"][5:7])


def _row_year(row):
    return int(row["data"][0:4])


def _empty_month(month):
    return {
        "month": month,
        "quota_slm_attuale": None,
        "lordo_mc_attuale": None,
        "netto_mc_attuale": None,
        "quota_max_slm": None,
        "lordo_max_mc": None,
        "pioggia_mm_attuale": None,
        "neve_cm_attuale": None,
        "has_data": False,
    }


def monthly_snapshots(rows_for_year):
    """Port of getMonthlyData + the per-month reduction in getAnnualData.

    For each month: takes the LAST day's row as the level/volume/max snapshot
    (these are instantaneous values, not summed), and SUMS pioggia/neve over
    the days present in the month (these are cumulative daily values).
    """
    by_month = {m: [] for m in range(1, 13)}
    for row in rows_for_year:
        by_month[_row_month(row)].append(row)

    snapshots = []
    for month in range(1, 13):
        days = sorted(by_month[month], key=lambda r: r["data"])
        if not days:
            snapshots.append(_empty_month(month))
            continue
        last = days[-1]
        total_pioggia = sum(d["pioggia_mm_attuale"] or 0 for d in days)
        total_neve = sum(d["neve_cm_attuale"] or 0 for d in days)
        snapshots.append({
            "month": month,
            "quota_slm_attuale": last["quota_slm_attuale"],
            "lordo_mc_attuale": last["lordo_mc_attuale"],
            "netto_mc_attuale": last["netto_mc_attuale"],
            "quota_max_slm": last["quota_max_slm"],
            "lordo_max_mc": last["lordo_max_mc"],
            "pioggia_mm_attuale": total_pioggia,
            "neve_cm_attuale": total_neve,
            "has_data": True,
        })
    return snapshots


def compute_annual_months(rows_for_year):
    return monthly_snapshots(rows_for_year)


def _month_is_valid(month_snapshot):
    return any(month_snapshot[f] is not None for f in LEVEL_FIELDS)


def _average_field(items, field):
    values = [i[field] for i in items if i[field] is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _max_field(items, field):
    values = [i[field] for i in items if i[field] is not None]
    return max(values) if values else None


def _year_stats_from_snapshots(snapshots):
    """One year's 12 monthly snapshots -> aggregated year values + status,
    before cross-year interpolation.
    """
    valid_months = [s for s in snapshots if _month_is_valid(s)]
    n_valid = len(valid_months)

    result = {field: None for field in _YEAR_FIELDS}

    if n_valid == 0:
        return result, "missing"

    for field in LEVEL_FIELDS:
        result[field] = _average_field(snapshots, field)

    if n_valid >= 6:
        # "actual": full-year sum for rain/snow (missing months count as 0),
        # true max across the valid months for the capacity fields.
        result["quota_max_slm"] = _max_field(valid_months, "quota_max_slm")
        result["lordo_max_mc"] = _max_field(valid_months, "lordo_max_mc")
        result["pioggia_mm_attuale"] = sum(s["pioggia_mm_attuale"] or 0 for s in snapshots)
        result["neve_cm_attuale"] = sum(s["neve_cm_attuale"] or 0 for s in snapshots)
        return result, "actual"

    # "estimated" (1-5 valid months): extrapolate rain/snow from the average
    # of the valid months x12. Capacity fields are left null - script.js
    # deliberately never estimates max values ("Non stimiamo i valori massimi").
    avg_pioggia = _average_field(valid_months, "pioggia_mm_attuale")
    avg_neve = _average_field(valid_months, "neve_cm_attuale")
    result["pioggia_mm_attuale"] = avg_pioggia * 12 if avg_pioggia is not None else None
    result["neve_cm_attuale"] = avg_neve * 12 if avg_neve is not None else None
    return result, "estimated"


def _interpolate_years(years, field):
    """Linear interpolation across years with a null value for `field`, only
    when both a previous and a next non-null year exist (port of
    interpolateData). Returns the set of indices that got filled in.
    """
    values = [y[field] for y in years]
    filled = list(values)
    interpolated_indices = set()
    for i, value in enumerate(values):
        if value is not None:
            continue
        prev_index = i - 1
        while prev_index >= 0 and values[prev_index] is None:
            prev_index -= 1
        next_index = i + 1
        while next_index < len(values) and values[next_index] is None:
            next_index += 1
        if prev_index >= 0 and next_index < len(values):
            prev_value = values[prev_index]
            next_value = values[next_index]
            steps = next_index - prev_index
            step_value = (next_value - prev_value) / steps
            filled[i] = prev_value + step_value * (i - prev_index)
            interpolated_indices.add(i)
    for i, year in enumerate(years):
        year[field] = filled[i]
    return interpolated_indices


def compute_complete_years(rows, start_year, end_year):
    """Full 1998-today aggregation for the "Completa" view.

    Returns one entry per year with quota/lordo/netto/max/pioggia/neve plus a
    `status` of "actual" | "estimated" | "interpolated" | "missing".
    """
    rows_by_year = {y: [] for y in range(start_year, end_year + 1)}
    for row in rows:
        y = _row_year(row)
        if y in rows_by_year:
            rows_by_year[y].append(row)

    years = []
    for y in range(start_year, end_year + 1):
        snapshots = monthly_snapshots(rows_by_year[y])
        stats, status = _year_stats_from_snapshots(snapshots)
        years.append({"year": y, "status": status, **stats})

    interpolated = [False] * len(years)
    for field in LEVEL_FIELDS:
        touched = _interpolate_years(years, field)
        for i in touched:
            interpolated[i] = True

    for i, year in enumerate(years):
        if year["status"] == "missing" and interpolated[i]:
            year["status"] = "interpolated"

    return years
