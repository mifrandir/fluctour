"""Private, bounded activity imports with descriptive history summaries."""

import csv
import hashlib
import io
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime

from .store import now


MAX_BYTES = 2 * 1024 * 1024
MAX_ACTIVITIES = 10000
HISTORY_KEY = "activity_history"
METRICS = ("distance_m", "elevation_m", "moving_time_s")
GROUPS = ("Hike", "Walk", "TrailRun", "Swim")
API_DOCS = "https://developers.strava.com/docs/reference/"
EXPORT_DOCS = "https://support.strava.com/hc/en-us/articles/216918437-Exporting-your-Data-and-Bulk-Export"
CSV_UNITS = {
    "distance_m": {"distance (m)": 1, "distance_m": 1,
                   "distance (km)": 1000, "distance_km": 1000,
                   "distance (mi)": 1609.344},
    "elevation_m": {"elevation gain (m)": 1, "elevation_m": 1,
                    "total_elevation_gain (m)": 1, "elevation gain (ft)": 0.3048},
    "moving_time_s": {"moving time (s)": 1, "moving_time_s": 1,
                      "moving time (seconds)": 1, "moving time (minutes)": 60,
                      "moving time (hh:mm:ss)": "clock"},
}


def _number(value, field, csv_value=False):
    if value is None or (csv_value and value.strip() == ""):
        return None
    if csv_value:
        value = value.strip()
        # A comma can mean a decimal point or thousands separator; neither is guessed.
        if not re.fullmatch(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
            raise ValueError(f"{field} must use nonnegative numbers with a decimal point.")
        value = float(value)
    if (type(value) not in (int, float) or value < 0 or value > 1e12
            or not math.isfinite(value)):
        raise ValueError(f"{field} must be finite, nonnegative and at most 10^12.")
    return value


def _date(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError("Activity date is required.")
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %H:%M:%S",
                        "%d %b %Y, %H:%M:%S", "%b %d, %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError("Use an ISO date or an English Strava export date.") from None
    return parsed.date().isoformat()


def _activity_type(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 60:
        raise ValueError("Activity type is required and must be at most 60 characters.")
    clean = value.strip()
    try:
        clean.encode("utf-8")
    except UnicodeError:
        raise ValueError("Activity type must contain valid UTF-8 text.") from None
    aliases = {"hike": "Hike", "hiking": "Hike", "walk": "Walk", "walking": "Walk",
               "trailrun": "TrailRun", "trail run": "TrailRun", "trail running": "TrailRun",
               "swim": "Swim", "swimming": "Swim"}
    return aliases.get(clean.lower(), clean)


def _source_id(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,30}", str(value)):
        raise ValueError("Activity ID must contain digits only.")
    return str(value)


def _region(value):
    if value is None or value == []:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(number) not in (int, float) for number in value)
            or not -90 <= value[0] <= 90 or not -180 <= value[1] <= 180):
        raise ValueError("Activity start coordinates must be a valid latitude/longitude pair.")
    latitude = min(88, math.floor(value[0] / 2) * 2)
    longitude = min(178, math.floor(value[1] / 2) * 2)
    return f"{latitude}..{latitude + 2} latitude, {longitude}..{longitude + 2} longitude"


def _optional_text(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Activity names and source-provided locations must be text of at most 200 characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Activity names and locations must contain valid UTF-8 text.") from None
    return value.strip() or None


def _normalize(raw, source):
    started = raw.get("start_date_local") or raw.get("start_date")
    item = {"date": _date(started), "activity_type": _activity_type(raw.get("sport_type") or raw.get("type")),
            "distance_m": _number(raw.get("distance"), "distance"),
            "elevation_m": _number(raw.get("total_elevation_gain"), "elevation"),
            "moving_time_s": _number(raw.get("moving_time"), "moving time"),
            "source_activity_id": _source_id(raw.get("id")), "region_cell": _region(raw.get("start_latlng")),
            "source": source, "name": _optional_text(raw.get("name")),
            "location_city": _optional_text(raw.get("location_city")),
            "location_country": _optional_text(raw.get("location_country"))}
    # The identity digest distinguishes same-day sessions without retaining exact start times.
    identity = {key: value for key, value in item.items() if key not in (
        "source", "source_activity_id", "name", "location_city", "location_country")}
    identity["started"] = started.strip()
    item["key"] = ("strava:" + item["source_activity_id"] if item["source_activity_id"] else
                   "anonymous:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
    return item


def _parse_json(text):
    try:
        rows = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("Provide a JSON array of Strava API activities.") from None
    if not isinstance(rows, list):
        raise ValueError("Provide a JSON array of activities, without tokens or credentials.")
    if len(rows) > MAX_ACTIVITIES:
        raise ValueError("Import at most 10000 activities at once.")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"Activity {index} must be an object.")
        try:
            result.append(_normalize(row, "strava_api_json"))
        except (ValueError, TypeError):
            raise ValueError(f"Activity {index} has an invalid date, type, metric, ID or coordinates.") from None
    return result


def _csv_metrics(headers, row):
    normalized = {}
    untyped = {"distance_m": {"distance"}, "elevation_m": {"elevation gain", "total_elevation_gain"},
               "moving_time_s": {"moving time", "moving_time"}}
    for field, candidates in CSV_UNITS.items():
        matches = [(index, candidates[name]) for index, name in enumerate(headers) if name in candidates]
        if len(matches) > 1:
            raise ValueError(f"Use one explicitly unit-labelled column for {field}.")
        if not matches:
            if any(row[index].strip() for index, name in enumerate(headers) if name in untyped[field]):
                raise ValueError(f"{field} units are unknown; label the column with m/km/mi, m/ft, or s/minutes/HH:MM:SS.")
            normalized[field] = None
            continue
        index, multiplier = matches[0]
        value = row[index].strip()
        if multiplier == "clock" and value:
            match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)", value)
            if not match:
                raise ValueError("Moving Time (HH:MM:SS) must use hours:minutes:seconds.")
            normalized[field] = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
        else:
            number = _number(value, field, csv_value=True)
            normalized[field] = number * multiplier if number is not None else None
        if normalized[field] is not None and not math.isfinite(normalized[field]):
            raise ValueError(f"{field} exceeds the supported numeric range.")
    return normalized


def _parse_csv(text):
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        headers = [header.strip().lower() for header in next(reader)]
    except (StopIteration, csv.Error):
        raise ValueError("Provide CSV headers and activity rows.") from None
    if len(headers) > 200 or any(len(header) > 120 for header in headers):
        raise ValueError("CSV headers exceed the supported size.")
    def column(names, required=False):
        matches = [index for index, name in enumerate(headers) if name in names]
        if len(matches) > 1 or (required and not matches):
            raise ValueError("CSV requires one Activity Date and one Activity Type column.")
        return matches[0] if matches else None
    date_column = column({"activity date", "start_date", "start_date_local", "date"}, True)
    type_column = column({"activity type", "sport_type", "type"}, True)
    id_column = column({"activity id", "id"})
    name_column = column({"activity name", "name"})
    city_column = column({"location city", "location_city"})
    country_column = column({"location country", "location_country"})
    results = []
    try:
        for row_number, row in enumerate(reader, 2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(results) >= MAX_ACTIVITIES:
                raise ValueError("Import at most 10000 activities at once.")
            if len(row) != len(headers):
                raise ValueError(f"CSV row {row_number} has the wrong number of columns.")
            try:
                metrics = _csv_metrics(headers, row)
                item = _normalize({"start_date": row[date_column], "type": row[type_column],
                                   "id": row[id_column].strip() if id_column is not None else None,
                                   "name": row[name_column] if name_column is not None else None,
                                   "location_city": row[city_column] if city_column is not None else None,
                                   "location_country": row[country_column] if country_column is not None else None,
                                   "distance": metrics["distance_m"], "total_elevation_gain": metrics["elevation_m"],
                                   "moving_time": metrics["moving_time_s"]}, "strava_csv")
                results.append(item)
            except (ValueError, TypeError) as error:
                raise ValueError(f"CSV row {row_number}: {error}") from None
    except csv.Error:
        raise ValueError("CSV is malformed or contains an oversized field.") from None
    return results


def _load(store):
    row = store.db.execute("SELECT value FROM settings WHERE key=?", (HISTORY_KEY,)).fetchone()
    return json.loads(row[0]) if row else {"activities": [], "imports": []}


def _distribution(items, field):
    values = sorted(item[field] for item in items if item.get(field) is not None)
    return {"count": len(values), "missing": len(items) - len(values),
            "min": values[0] if values else None, "median": statistics.median(values) if values else None,
            "max": values[-1] if values else None, "total": math.fsum(values) if values else None}


def activity_summary(store):
    """Describe recorded activities; history does not establish enjoyment or fitness."""
    history = _load(store)
    activities = history["activities"]
    dates = sorted(item["date"] for item in activities)
    groups = {}
    for activity_type in GROUPS:
        items = [item for item in activities if item["activity_type"] == activity_type]
        groups[activity_type] = {"count": len(items)} | {field: _distribution(items, field) for field in METRICS}
    regions = Counter(item["region_cell"] for item in activities if item.get("region_cell"))
    return {"has_history": bool(activities), "count": len(activities),
            "date_range": {"first": dates[0], "last": dates[-1]} if dates else None,
            "by_type": groups, "other_types": dict(sorted(Counter(
                item["activity_type"] for item in activities if item["activity_type"] not in GROUPS).items())),
            "coarse_regions": [{"cell": cell, "count": count} for cell, count in sorted(regions.items())],
            "recent": [{"id": item["source_activity_id"], "name": item.get("name"),
                        "date": item["date"], "type": item["activity_type"],
                        "distance_m": item["distance_m"], "elevation_m": item["elevation_m"],
                        "location": ", ".join(part for part in (item.get("location_city"),
                                                                 item.get("location_country")) if part) or None}
                       for item in sorted(activities, key=lambda item: (item["date"], item["key"]), reverse=True)[:10]],
            "provenance": history["imports"], "inferred": [],
            "unknown": ["Which recorded activities you enjoyed or want to repeat.",
                        "Current fitness, comfortable effort, swimming conditions and unrecorded travel.",
                        "A recorded start region does not prove a destination visit or a preferred location."],
            "privacy": "Private activity names and source-provided cities retained; no exact coordinates, routes or notes. GPS becomes coarse 2-degree cells.",
            "interpretation": "Observed records only. Distances and durations are descriptive, not fitness limits or preferences."}


def import_activities(store, text, format="csv"):
    """Atomically merge a CSV export or API-style JSON list into private SQLite state."""
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            raise ValueError("Activity import exceeds 2 MiB.")
        try:
            text = text.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("Activity import must use UTF-8.") from None
    if not isinstance(text, str):
        raise ValueError("Provide activity data as UTF-8 text.")
    if len(text) > MAX_BYTES:
        raise ValueError("Activity import exceeds 2 MiB.")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Activity import must use UTF-8.") from None
    if len(raw) > MAX_BYTES:
        raise ValueError("Activity import exceeds 2 MiB.")
    text = text.lstrip("\ufeff")
    if format not in ("csv", "json"):
        raise ValueError("Activity format must be csv or json.")
    activities = _parse_csv(text) if format == "csv" else _parse_json(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with store.db:
        # Serialize read-merge-write so simultaneous imports cannot lose activities.
        store.db.execute("BEGIN IMMEDIATE")
        history = _load(store)
        merged = {item["key"]: item for item in history["activities"]}
        added = updated = duplicate = 0
        for item in activities:
            existing = merged.get(item["key"])
            if existing is None:
                added += 1
            else:
                # Missing fields in a partial API page do not erase existing evidence.
                item = {key: value if value is not None else existing.get(key) for key, value in item.items()}
                comparable = lambda value: {key: val for key, val in value.items() if key != "source"}
                if comparable(existing) == comparable(item):
                    duplicate += 1
                    item = existing
                else:
                    updated += 1
            merged[item["key"]] = item
        if len(merged) > MAX_ACTIVITIES:
            raise ValueError("Stored activity history is limited to 10000 activities.")
        entry = {"source": "strava_csv" if format == "csv" else "strava_api_json", "format": format,
                 "imported_at": now(), "input_count": len(activities), "added_count": added,
                 "updated_count": updated, "duplicate_count": duplicate, "sha256": digest,
                 "unit_basis": "Explicit CSV metric headers" if format == "csv" else "Strava API metres and seconds",
                 "documentation": EXPORT_DOCS if format == "csv" else API_DOCS}
        history["activities"] = sorted(merged.values(), key=lambda item: (item["date"], item["key"]), reverse=True)
        if not any(previous["sha256"] == digest for previous in history["imports"]):
            history["imports"] = (history["imports"] + [entry])[-20:]
        store.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",
                         (HISTORY_KEY, json.dumps(history, ensure_ascii=False, allow_nan=False)))
    return activity_summary(store) | {"last_import": entry}
