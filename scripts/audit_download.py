#!/usr/bin/env python3
"""Audita dos CSV M1 BID y ASK de Dukascopy/JForex sin modificarlos."""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPECTED_HEADER = ["Time (UTC)", "Open", "High", "Low", "Close", "Volume"]
DATE_FORMAT = "%Y.%m.%d %H:%M:%S"
STEP = timedelta(minutes=1)
DETAIL_LIMIT = 200
PRICE_FIELDS = ("Open", "High", "Low", "Close")
SPREAD_NOTE = (
    "Open, High, Low y Close de una barra M1 no proceden necesariamente "
    "del mismo instante. Su diferencia no es el spread instantáneo y estas "
    "barras no permiten reconstruirlo."
)
COVERAGE_NOTE = (
    "Esta revisión no establece cobertura completa. No hay un calendario "
    "histórico de mercado ni un rango de fechas solicitado entre los argumentos. "
    "La etiqueta de fin de semana es solo orientativa."
)
GAP_NOTE = (
    "Los huecos se miden contra un minuto fijo. La mediana del intervalo "
    "observado es solo un diagnóstico y no cambia ese criterio. Las marcas "
    "duplicadas no entran en la serie; las demás se ordenan en memoria y el "
    "archivo no se modifica. En cada lista se muestran primero los huecos "
    "cuyo número de minutos ausentes es distinto de 60."
)


@dataclass
class Bucket[T]:
    total: int = 0
    items: list[T] = field(default_factory=list)

    def add(self, item: T) -> None:
        self.total += 1
        if len(self.items) < DETAIL_LIMIT:
            self.items.append(item)


@dataclass
class Gap:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def missing_minutes(self) -> float:
        return self.duration.total_seconds() / 60 - 1


@dataclass
class ZeroBar:
    stamp: datetime
    prices: tuple[float, float, float, float] | None


@dataclass
class ZeroSpan:
    start: datetime
    end: datetime
    bars: int
    constant: bool


@dataclass
class ZeroRow:
    start: datetime
    end: datetime
    bid_bars: int | None
    ask_bars: int | None
    bid_constant: bool | None
    ask_constant: bool | None

    @property
    def coincides(self) -> bool:
        return self.bid_bars is not None and self.ask_bars is not None

    @property
    def length(self) -> int:
        return max(self.bid_bars or 0, self.ask_bars or 0)


@dataclass
class FileAudit:
    path: Path
    rows_read: int = 0
    valid_dates: int = 0
    first_date: datetime | None = None
    last_date: datetime | None = None
    duplicate_rows: int = 0
    lines_by_time: dict[datetime, list[int]] = field(default_factory=dict)
    ohlc_by_time: dict[datetime, tuple[float, float, float, float]] = field(
        default_factory=dict
    )
    positive_intervals: list[float] = field(default_factory=list)
    lectura: Bucket[str] = field(default_factory=Bucket)
    duplicates: Bucket[str] = field(default_factory=Bucket)
    order: Bucket[str] = field(default_factory=Bucket)
    ohlc: Bucket[str] = field(default_factory=Bucket)
    volume_invalid: Bucket[str] = field(default_factory=Bucket)
    zero_bars: list[ZeroBar] = field(default_factory=list)
    zero_undated: int = 0
    zero_spans: list[ZeroSpan] = field(default_factory=list)
    weekend_gaps: Bucket[Gap] = field(default_factory=Bucket)
    other_gaps: Bucket[Gap] = field(default_factory=Bucket)
    _previous: datetime | None = None

    @property
    def zero_bar_count(self) -> int:
        return len(self.zero_bars) + self.zero_undated

    def duplicate_times(self) -> set[datetime]:
        return {stamp for stamp, lines in self.lines_by_time.items() if len(lines) > 1}

    def unique_times(self) -> set[datetime]:
        return {stamp for stamp, lines in self.lines_by_time.items() if len(lines) == 1}


@dataclass
class PriceDiffs:
    negatives: Bucket[str] = field(default_factory=Bucket)
    wides: Bucket[str] = field(default_factory=Bucket)
    omitted: list[str] = field(default_factory=list)
    medians: dict[str, float] = field(default_factory=dict)
    compared: int = 0
    skipped_prices: int = 0
    threshold_available: bool = False


class AuditError(Exception):
    """El archivo no puede auditarse y no debe generarse informe."""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_number(text: str) -> float | None:
    value = text.strip()
    if not value or ("," in value and "." in value):
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def parse_time(text: str) -> datetime | None:
    try:
        parsed = datetime.strptime(text.strip(), DATE_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def format_time(value: datetime) -> str:
    return value.strftime(DATE_FORMAT)


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} s")
    return sign + " ".join(parts)


def format_seconds(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        shown = str(int(round(value)))
    else:
        shown = f"{value:.3f}"
    return f"{shown} segundos"


def missing_minutes(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60 - 1
    if abs(minutes - round(minutes)) < 1e-6:
        return str(int(round(minutes)))
    return f"{minutes:.3f}"


def date_text(value: datetime | None) -> str:
    if value is None:
        return "sin fechas válidas"
    return format_time(value)


def interval_text(audit: FileAudit) -> str:
    if not audit.positive_intervals:
        return "no se puede calcular (no hay intervalos positivos)"
    return format_seconds(median(audit.positive_intervals))


def clip(text: str, limit: int = 120) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def is_weekend_gap(start: datetime, end: datetime) -> bool:
    if start.weekday() == 4 and end.weekday() == 6:
        return True
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() == 5:
            saturday = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            sunday = saturday + timedelta(days=1)
            if max(start, saturday) < min(end, sunday):
                return True
        day += timedelta(days=1)
    return False


def price_problem(name: str, value: float) -> str | None:
    if not math.isfinite(value):
        return f"{name} no finito ({format_number(value)})"
    if value <= 0:
        return f"{name} no positivo ({format_number(value)})"
    return None


def coherence_problem(prices: tuple[float, float, float, float]) -> str | None:
    open_, high, low, close = prices
    if high < open_ or high < low or high < close or low > open_ or low > close:
        rendered = ", ".join(
            f"{name}={format_number(value)}"
            for name, value in zip(PRICE_FIELDS, prices, strict=True)
        )
        return f"OHLC incoherente ({rendered})"
    return None


def locate(stamp: datetime | None, line_number: int) -> str:
    if stamp is None:
        return f"línea {line_number}"
    return f"{format_time(stamp)} (línea {line_number})"


def audit_file(path: Path, text: str) -> FileAudit:
    audit = FileAudit(path=path)
    reader = csv.reader(io.StringIO(text), delimiter=";")
    try:
        header = next(reader)
    except StopIteration as exc:
        raise AuditError(f"El archivo no tiene el encabezado esperado: {path}") from exc
    header = [cell.strip() for cell in header]
    if header != EXPECTED_HEADER:
        found = ";".join(header)
        expected = ";".join(EXPECTED_HEADER)
        raise AuditError(
            f"El archivo no tiene el encabezado esperado: {path}\n"
            f"Encabezado leído: {found}\n"
            f"Encabezado exigido: {expected}"
        )
    for line_number, cells in enumerate(reader, start=2):
        inspect_row(audit, line_number, cells)
    finish_duplicates(audit)
    finish_gaps(audit)
    finish_zero_spans(audit)
    return audit


def inspect_row(audit: FileAudit, line_number: int, cells: list[str]) -> None:
    audit.rows_read += 1
    if len(cells) != 6:
        audit.lectura.add(
            f"Línea {line_number}: {len(cells)} columnas; se esperaban 6"
        )
        return
    raw_time, raw_open, raw_high, raw_low, raw_close, raw_volume = (
        cell.strip() for cell in cells
    )
    stamp = parse_time(raw_time)
    parsed_prices = [
        parse_number(value) for value in (raw_open, raw_high, raw_low, raw_close)
    ]
    volume = parse_number(raw_volume)
    if stamp is None:
        audit.lectura.add(f"Línea {line_number}: fecha ilegible ({clip(raw_time)})")
    for name, raw, parsed in zip(
        PRICE_FIELDS,
        (raw_open, raw_high, raw_low, raw_close),
        parsed_prices,
        strict=True,
    ):
        if parsed is None:
            audit.lectura.add(f"Línea {line_number}: {name} ilegible ({clip(raw)})")
    if volume is None:
        audit.lectura.add(
            f"Línea {line_number}: Volume ilegible ({clip(raw_volume)})"
        )
    if stamp is not None:
        register_time(audit, stamp, line_number)
    finite_prices = None
    if None not in parsed_prices:
        prices = (
            float(parsed_prices[0]),  # type: ignore[arg-type]
            float(parsed_prices[1]),  # type: ignore[arg-type]
            float(parsed_prices[2]),  # type: ignore[arg-type]
            float(parsed_prices[3]),  # type: ignore[arg-type]
        )
        register_prices(audit, stamp, line_number, prices)
        if all(math.isfinite(value) for value in prices):
            finite_prices = prices
    if volume is not None:
        register_volume(audit, stamp, line_number, volume, finite_prices)


def register_time(audit: FileAudit, stamp: datetime, line_number: int) -> None:
    audit.valid_dates += 1
    if audit.first_date is None:
        audit.first_date = stamp
    audit.last_date = stamp
    audit.lines_by_time.setdefault(stamp, []).append(line_number)
    if audit._previous is not None:
        delta = (stamp - audit._previous).total_seconds()
        if delta > 0:
            audit.positive_intervals.append(delta)
        elif delta < 0:
            audit.order.add(
                f"{format_time(stamp)} (línea {line_number}) es anterior a "
                f"{format_time(audit._previous)}"
            )
    audit._previous = stamp


def register_prices(
    audit: FileAudit,
    stamp: datetime | None,
    line_number: int,
    prices: tuple[float, float, float, float],
) -> None:
    problems = [
        problem
        for name, value in zip(PRICE_FIELDS, prices, strict=True)
        if (problem := price_problem(name, value)) is not None
    ]
    if not problems:
        coherence = coherence_problem(prices)
        if coherence is not None:
            problems.append(coherence)
    if problems:
        audit.ohlc.add(f"{locate(stamp, line_number)}: {'; '.join(problems)}")
        return
    if stamp is not None and len(audit.lines_by_time.get(stamp, ())) == 1:
        audit.ohlc_by_time[stamp] = prices


def register_volume(
    audit: FileAudit,
    stamp: datetime | None,
    line_number: int,
    volume: float,
    prices: tuple[float, float, float, float] | None,
) -> None:
    where = locate(stamp, line_number)
    if not math.isfinite(volume):
        audit.volume_invalid.add(f"{where}: volumen no finito ({format_number(volume)})")
        return
    if volume < 0:
        audit.volume_invalid.add(f"{where}: volumen negativo ({format_number(volume)})")
        return
    if volume != 0:
        return
    if stamp is None:
        audit.zero_undated += 1
        return
    audit.zero_bars.append(ZeroBar(stamp, prices))


def finish_zero_spans(audit: FileAudit) -> None:
    ordered = sorted(audit.zero_bars, key=lambda bar: bar.stamp)
    groups: list[list[ZeroBar]] = []
    for bar in ordered:
        if groups and bar.stamp - groups[-1][-1].stamp in {timedelta(0), STEP}:
            groups[-1].append(bar)
            continue
        groups.append([bar])
    for group in groups:
        prices = [bar.prices for bar in group]
        constant = all(price is not None for price in prices) and all(
            price == prices[0] for price in prices
        )
        audit.zero_spans.append(
            ZeroSpan(group[0].stamp, group[-1].stamp, len(group), constant)
        )


def is_common_gap(gap: Gap) -> bool:
    return abs(gap.missing_minutes - 60) < 1e-6


def finish_gaps(audit: FileAudit) -> None:
    weekend: list[Gap] = []
    other: list[Gap] = []
    ordered = sorted(audit.unique_times())
    for start, end in zip(ordered, ordered[1:]):
        if end - start <= STEP:
            continue
        gap = Gap(start, end)
        if is_weekend_gap(start, end):
            weekend.append(gap)
        else:
            other.append(gap)
    for gap in sorted(weekend, key=lambda item: (is_common_gap(item), item.start)):
        audit.weekend_gaps.add(gap)
    for gap in sorted(other, key=lambda item: (is_common_gap(item), item.start)):
        audit.other_gaps.add(gap)


def finish_duplicates(audit: FileAudit) -> None:
    for stamp, lines in sorted(audit.lines_by_time.items()):
        if len(lines) < 2:
            continue
        audit.duplicate_rows += len(lines)
        rendered = ", ".join(str(line) for line in lines)
        audit.duplicates.add(
            f"{format_time(stamp)} aparece {len(lines)} veces (líneas {rendered})"
        )
        audit.ohlc_by_time.pop(stamp, None)


def compare_prices(bid: FileAudit, ask: FileAudit, both: set[datetime]) -> PriceDiffs:
    diffs = PriceDiffs()
    positive: dict[str, list[float]] = {name: [] for name in PRICE_FIELDS}
    comparable: list[datetime] = []
    for stamp in sorted(both):
        bid_prices = bid.ohlc_by_time.get(stamp)
        ask_prices = ask.ohlc_by_time.get(stamp)
        if bid_prices is None or ask_prices is None:
            diffs.skipped_prices += 1
            continue
        comparable.append(stamp)
        for index, name in enumerate(PRICE_FIELDS):
            difference = ask_prices[index] - bid_prices[index]
            if difference > 0 and math.isfinite(difference):
                positive[name].append(difference)
    diffs.compared = len(comparable)
    for name, values in positive.items():
        if not values:
            diffs.omitted.append(
                f"{name}: no hay diferencias positivas finitas; se omite el umbral "
                "de 10 veces la mediana."
            )
            continue
        diffs.medians[name] = median(values)
    diffs.threshold_available = bool(diffs.medians)
    for stamp in comparable:
        bid_prices = bid.ohlc_by_time[stamp]
        ask_prices = ask.ohlc_by_time[stamp]
        for index, name in enumerate(PRICE_FIELDS):
            difference = ask_prices[index] - bid_prices[index]
            described = (
                f"{format_time(stamp)}: {name} ASK {format_number(ask_prices[index])}"
                f" - BID {format_number(bid_prices[index])}"
                f" = {format_number(difference)}"
            )
            if difference < 0:
                diffs.negatives.add(described)
                continue
            threshold_median = diffs.medians.get(name)
            if threshold_median is not None and difference > 10 * threshold_median:
                diffs.wides.add(
                    f"{described} (observación: supera 10 veces la mediana "
                    f"{format_number(threshold_median)})"
                )
    return diffs


def report_stem(bid: Path, ask: Path) -> str:
    def normalize(stem: str) -> str:
        parts = re.split(r"[_\-\s]+", stem)
        kept = [part for part in parts if part.lower() not in {"bid", "ask"}]
        return "_".join(kept)

    left = normalize(bid.stem)
    right = normalize(ask.stem)
    if left and left == right:
        common = left
    else:
        prefix: list[str] = []
        for char_left, char_right in zip(left, right):
            if char_left != char_right:
                break
            prefix.append(char_left)
        common = "".join(prefix).strip("_- ")
        if len(common) < 3:
            common = f"{left}__{right}" if left and right else "auditoria"
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", common).strip(" ._")
    return cleaned or "auditoria"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_report_path(bid: Path, ask: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return project_root() / "catalog" / "audits" / f"{report_stem(bid, ask)}_{stamp}.md"


def add_text_section(lines: list[str], title: str, bucket: Bucket[str]) -> None:
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"Total: {bucket.total}")
    lines.append("")
    if bucket.total == 0:
        lines.append("Ninguna.")
        lines.append("")
        return
    for item in bucket.items:
        lines.append(f"- {item}")
    if bucket.total > len(bucket.items):
        lines.append("")
        lines.append(f"Quedan {bucket.total - len(bucket.items)} sin mostrar.")
    lines.append("")


def combine_zero_spans(bid: FileAudit, ask: FileAudit) -> list[ZeroRow]:
    bid_map = {(span.start, span.end): span for span in bid.zero_spans}
    ask_map = {(span.start, span.end): span for span in ask.zero_spans}
    rows: list[ZeroRow] = []
    for key in set(bid_map) | set(ask_map):
        left = bid_map.get(key)
        right = ask_map.get(key)
        rows.append(
            ZeroRow(
                key[0],
                key[1],
                None if left is None else left.bars,
                None if right is None else right.bars,
                None if left is None else left.constant,
                None if right is None else right.constant,
            )
        )
    rows.sort(key=lambda row: (-row.length, row.start))
    return rows


def format_yes(value: bool | None) -> str:
    if value is None:
        return "sin tramo"
    return "sí" if value else "no"


def format_bars(value: int | None) -> str:
    if value is None:
        return "—"
    return str(value)


def add_zero_section(lines: list[str], bid: FileAudit, ask: FileAudit) -> None:
    rows = combine_zero_spans(bid, ask)
    lines.append("## Volumen cero")
    lines.append("")
    lines.append(
        "Cada tramo agrupa barras consecutivas de volumen cero, separadas por "
        "un minuto. Si la marca se repite, esas filas siguen en el mismo tramo. "
        "Los tramos más largos aparecen primero. OHLC constante significa que "
        "Open, High, Low y Close no cambian en ninguna barra del tramo."
    )
    lines.append("")
    lines.append(f"Total de tramos: {len(rows)}")
    lines.append("")
    if not rows:
        lines.append("Ninguno.")
        lines.append("")
    else:
        lines.append(
            "| Inicio | Fin | Barras BID | Barras ASK | Coincide | "
            "OHLC constante BID | OHLC constante ASK |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in rows[:DETAIL_LIMIT]:
            lines.append(
                f"| {format_time(row.start)} | {format_time(row.end)} | "
                f"{format_bars(row.bid_bars)} | {format_bars(row.ask_bars)} | "
                f"{format_yes(row.coincides)} | {format_yes(row.bid_constant)} | "
                f"{format_yes(row.ask_constant)} |"
            )
        if len(rows) > DETAIL_LIMIT:
            lines.append("")
            lines.append(f"Quedan {len(rows) - DETAIL_LIMIT} sin mostrar.")
        lines.append("")
    undated = bid.zero_undated + ask.zero_undated
    if undated:
        lines.append(
            f"Barras con volumen cero y sin fecha válida: {undated} "
            f"(BID {bid.zero_undated}, ASK {ask.zero_undated})."
        )
        lines.append("")


def add_gap_section(lines: list[str], title: str, bucket: Bucket[Gap]) -> None:
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"Total: {bucket.total}")
    lines.append("")
    if bucket.total == 0:
        lines.append("Ninguna.")
        lines.append("")
        return
    lines.append("| Inicio | Fin | Duración | Minutos ausentes |")
    lines.append("| --- | --- | --- | --- |")
    for gap in bucket.items:
        lines.append(
            f"| {format_time(gap.start)} | {format_time(gap.end)} | "
            f"{format_duration(gap.duration)} | {missing_minutes(gap.duration)} |"
        )
    if bucket.total > len(bucket.items):
        lines.append("")
        lines.append(f"Quedan {bucket.total - len(bucket.items)} sin mostrar.")
    lines.append("")


def add_date_section(lines: list[str], title: str, stamps: list[datetime]) -> None:
    bucket: Bucket[str] = Bucket()
    for stamp in stamps:
        bucket.add(format_time(stamp))
    add_text_section(lines, title, bucket)


def file_summary(name: str, audit: FileAudit) -> list[str]:
    return [
        f"### {name}",
        "",
        f"- Archivo: `{audit.path}`",
        f"- Filas leídas: {audit.rows_read}",
        f"- Fechas válidas: {audit.valid_dates}",
        f"- Primera fecha: {date_text(audit.first_date)}",
        f"- Última fecha: {date_text(audit.last_date)}",
        (
            f"- Mediana del intervalo observado: {interval_text(audit)}. "
            "Es un diagnóstico; los huecos se miden contra 1 minuto."
        ),
        f"- Filas de lectura ilegibles o incompletas: {audit.lectura.total}",
        (
            f"- Marcas duplicadas: {audit.duplicates.total} "
            f"(filas afectadas: {audit.duplicate_rows})"
        ),
        f"- Fechas fuera de orden: {audit.order.total}",
        f"- Filas con OHLC inválido: {audit.ohlc.total}",
        f"- Volúmenes no finitos o negativos: {audit.volume_invalid.total}",
        f"- Volúmenes cero: {audit.zero_bar_count} barras en {len(audit.zero_spans)} tramos",
        f"- Huecos de fin de semana: {audit.weekend_gaps.total}",
        f"- Huecos a revisar: {audit.other_gaps.total}",
        "",
    ]


def build_report(
    bid: FileAudit,
    ask: FileAudit,
    only_bid: set[datetime],
    only_ask: set[datetime],
    both: set[datetime],
    excluded: set[datetime],
    diffs: PriceDiffs,
    executed_at: datetime,
) -> str:
    lines = [
        "# Auditoría de descarga",
        "",
        f"- Ejecución (UTC): {executed_at.strftime('%Y.%m.%d %H:%M:%S.%f')}",
        f"- BID: `{bid.path}`",
        f"- ASK: `{ask.path}`",
        "",
        COVERAGE_NOTE,
        "",
        "## Resumen",
        "",
        *file_summary("BID", bid),
        *file_summary("ASK", ask),
        "### Alineación",
        "",
        (
            "Las marcas duplicadas en BID o en ASK quedan fuera de coincidencias, "
            "ausencias y diferencias de precio. No se elige ni se sobrescribe ninguna fila."
        ),
        "",
        f"- Marcas excluidas por duplicado: {len(excluded)}",
        f"- Solo en BID: {len(only_bid)}",
        f"- Solo en ASK: {len(only_ask)}",
        f"- Coincidentes: {len(both)}",
        f"- Coincidentes con precios válidos en ambos: {diffs.compared}",
        (
            "- Coincidentes excluidas de las diferencias por precios inválidos: "
            f"{diffs.skipped_prices}"
        ),
        f"- Diferencias negativas: {diffs.negatives.total}",
        f"- Diferencias por encima de 10 veces la mediana: {diffs.wides.total}",
        "",
        GAP_NOTE,
        "",
    ]
    add_gap_section(lines, "Huecos de fin de semana en BID", bid.weekend_gaps)
    add_gap_section(lines, "Huecos a revisar en BID", bid.other_gaps)
    add_gap_section(lines, "Huecos de fin de semana en ASK", ask.weekend_gaps)
    add_gap_section(lines, "Huecos a revisar en ASK", ask.other_gaps)

    lines.extend(["## Incidencias", ""])
    add_text_section(lines, "Lectura de BID", bid.lectura)
    add_text_section(lines, "Lectura de ASK", ask.lectura)
    add_text_section(lines, "Duplicados en BID", bid.duplicates)
    add_text_section(lines, "Duplicados en ASK", ask.duplicates)
    add_text_section(lines, "Fechas fuera de orden en BID", bid.order)
    add_text_section(lines, "Fechas fuera de orden en ASK", ask.order)
    add_text_section(lines, "OHLC de BID", bid.ohlc)
    add_text_section(lines, "OHLC de ASK", ask.ohlc)
    add_text_section(lines, "Volumen inválido en BID", bid.volume_invalid)
    add_text_section(lines, "Volumen inválido en ASK", ask.volume_invalid)
    add_date_section(lines, "Fechas solo en BID", sorted(only_bid))
    add_date_section(lines, "Fechas solo en ASK", sorted(only_ask))
    lines.extend(
        [
            "## Diferencias negativas entre OHLC",
            "",
            (
                "Una diferencia negativa queda como incidencia a revisar. "
                + SPREAD_NOTE
            ),
            "",
        ]
    )
    add_text_section(lines, "Detalle de diferencias negativas", diffs.negatives)

    lines.extend(["## Observaciones", ""])
    add_zero_section(lines, bid, ask)
    if diffs.omitted:
        lines.append("## Umbral no calculado")
        lines.append("")
        for note in diffs.omitted:
            lines.append(f"- {note}")
        lines.append("")
    if diffs.threshold_available:
        lines.extend(
            [
                "## Diferencias por encima de 10 veces la mediana",
                "",
                (
                    "Estas diferencias son observaciones, no errores. "
                    + SPREAD_NOTE
                ),
                "",
            ]
        )
        add_text_section(lines, "Detalle de diferencias amplias", diffs.wides)

    lines.extend(["## Nota sobre el spread", "", SPREAD_NOTE, ""])
    return "\n".join(lines)


def print_summary(
    bid: FileAudit,
    ask: FileAudit,
    only_bid: set[datetime],
    only_ask: set[datetime],
    both: set[datetime],
    excluded: set[datetime],
    diffs: PriceDiffs,
    report: Path,
) -> None:
    print("Auditoría de descarga")
    print(f"BID: {bid.path}")
    print(f"ASK: {ask.path}")
    print(COVERAGE_NOTE)
    print()
    for name, audit in (("BID", bid), ("ASK", ask)):
        print(name)
        print(f"  Filas leídas: {audit.rows_read}")
        print(f"  Fechas válidas: {audit.valid_dates}")
        print(f"  Primera fecha: {date_text(audit.first_date)}")
        print(f"  Última fecha: {date_text(audit.last_date)}")
        print(
            "  Mediana del intervalo observado: "
            f"{interval_text(audit)} (diagnóstico; los huecos se miden contra 1 minuto)"
        )
        print(f"  Lectura: {audit.lectura.total}")
        print(
            f"  Duplicados: {audit.duplicates.total} marcas, "
            f"{audit.duplicate_rows} filas"
        )
        print(f"  Fuera de orden: {audit.order.total}")
        print(f"  OHLC: {audit.ohlc.total}")
        print(f"  Volumen inválido: {audit.volume_invalid.total}")
        print(
            f"  Volumen cero: {audit.zero_bar_count} barras, "
            f"{len(audit.zero_spans)} tramos"
        )
        print(f"  Huecos de fin de semana: {audit.weekend_gaps.total}")
        print(f"  Huecos a revisar: {audit.other_gaps.total}")
        print()
    print("Alineación")
    print(f"  Excluidas por duplicado: {len(excluded)}")
    print(f"  Solo en BID: {len(only_bid)}")
    print(f"  Solo en ASK: {len(only_ask)}")
    print(f"  Coincidentes: {len(both)}")
    print(f"  Con precios válidos en ambos: {diffs.compared}")
    print(f"  Excluidas por precios inválidos: {diffs.skipped_prices}")
    print(f"  Diferencias negativas: {diffs.negatives.total}")
    zero_rows = combine_zero_spans(bid, ask)
    matched = sum(row.coincides for row in zero_rows)
    print(f"  Tramos de volumen cero coincidentes: {matched}")
    print(f"  Diferencias amplias (observación): {diffs.wides.total}")
    if diffs.omitted:
        print("  Umbral omitido:")
        for note in diffs.omitted:
            print(f"    {note}")
    print()
    print(SPREAD_NOTE)
    print(f"Informe: {report}")


def load_audit(path: Path) -> FileAudit:
    try:
        text = read_text(path)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise AuditError(f"No se puede abrir el archivo: {path} ({reason})") from exc
    return audit_file(path, text)


def alignment(
    bid: FileAudit, ask: FileAudit
) -> tuple[set[datetime], set[datetime], set[datetime], set[datetime]]:
    excluded = bid.duplicate_times() | ask.duplicate_times()
    bid_times = bid.unique_times() - excluded
    ask_times = ask.unique_times() - excluded
    only_bid = bid_times - ask_times
    only_ask = ask_times - bid_times
    both = bid_times & ask_times
    return only_bid, only_ask, both, excluded


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audita dos CSV históricos M1, BID y ASK, de Dukascopy/JForex."
    )
    parser.add_argument("bid", type=Path, help="CSV de BID")
    parser.add_argument("ask", type=Path, help="CSV de ASK")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    errors: list[str] = []
    audits: list[FileAudit] = []
    for path in (args.bid, args.ask):
        try:
            audits.append(load_audit(path))
        except AuditError as exc:
            errors.append(str(exc))
    if errors or len(audits) != 2:
        for message in errors:
            print(message, file=sys.stderr)
        return 1
    bid, ask = audits
    only_bid, only_ask, both, excluded = alignment(bid, ask)
    diffs = compare_prices(bid, ask, both)
    executed_at = datetime.now(timezone.utc)
    report = build_report_path(bid.path, ask.path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        build_report(
            bid, ask, only_bid, only_ask, both, excluded, diffs, executed_at
        ),
        encoding="utf-8",
        newline="\n",
    )
    print_summary(bid, ask, only_bid, only_ask, both, excluded, diffs, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
