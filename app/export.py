"""
Exports for a grading session: CSV files and PNG summary charts.

  results.csv        one row per student (score, written scores, every answer)
  item_analysis.csv  one row per question (% correct, # missed, choice counts)
  score_distribution.png  histogram of student percentages
  most_missed.png         questions ranked by how many students missed them
  choice_distribution.png horizontal stacked bars of which choice was picked

Colors come from a colorblind-validated categorical palette, assigned to answer
letters in a fixed order (A = slot 1, B = slot 2, ...) so a letter always has
the same color.
"""

import csv
import io
import zipfile
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")  # render to files; no display on the server
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .grading import GradingSession, StudentResult, letters  # noqa: E402
from .sheet_layout import CHOICE_LETTERS  # noqa: E402

# ---- chart styling (light surface; PNGs are usually printed or pasted) ----
SURFACE = "#ffffff"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
SERIES_1 = "#2a78d6"
NEUTRAL = "#b9b8b2"       # "blank" answers
OTHER = "#8a8984"         # choices beyond the 8 palette slots are folded here
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# Light fills (aqua, yellow, magenta) get dark labels for contrast.
LIGHT_FILLS = {"#1baf7a", "#eda100", "#e87ba4", NEUTRAL}
DPI = 160

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT_SECONDARY,
    "axes.titlecolor": TEXT_PRIMARY,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "xtick.color": TEXT_SECONDARY,
    "ytick.color": TEXT_SECONDARY,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
})


# ------------------------------------------------------------ statistics ----

@dataclass
class ItemStats:
    question: int
    key: str
    correct: int
    incorrect: int
    blank: int
    choice_counts: list[int]  # students who marked each choice (multi-marks count each)

    @property
    def answered(self) -> int:
        return self.correct + self.incorrect

    @property
    def pct_correct(self) -> float:
        total = self.correct + self.incorrect
        return 100.0 * self.correct / total if total else 0.0


def graded_students(session: GradingSession) -> list[StudentResult]:
    """Students whose bubble page was read, by class then roster order."""
    return [s for s in session.ordered_students() if s.answers is not None]


def item_stats(session: GradingSession) -> list[ItemStats]:
    students = graded_students(session)
    k = session.spec.num_choices
    stats = []
    for q in sorted(session.key.answers):
        counts = [0] * k
        correct = blank = 0
        for s in students:
            given = s.answers.get(q, frozenset())
            if not given:
                blank += 1
            for c in given:
                counts[c] += 1
            correct += session.is_correct(q, given)
        # Blank answers count as incorrect for scoring, and are also reported.
        stats.append(ItemStats(q, letters(session.key.answers[q]), correct,
                               len(students) - correct, blank, counts))
    return stats


# ------------------------------------------------------------------ CSV ----

def _csv_bytes(rows: list[list]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 names correctly


def results_csv(session: GradingSession) -> bytes:
    """One row per student. Also re-uploadable (see results_import.py), so it
    records the exam details in an EXAM INFO row and each student's sheet ID."""
    spec = session.spec
    questions = sorted(session.key.answers)
    total_q = len(questions)
    n_written = len(spec.written_heights)
    written_cols = [f"Written {n}" for n in range(1, n_written + 1)]
    header = (["Student", "Class", "Sheet ID", "MC correct", "MC possible", "MC percent"]
              + written_cols + (["Total (MC + written)"] if n_written else [])
              + ["Needs review", "Notes"] + [f"Q{q}" for q in questions])

    def row(values: dict, answers: list[str]) -> list:
        """Place named values under their header columns, then the answers."""
        return [values.get(h, "") for h in header[:-total_q]] + answers

    rows = [header]
    rows.append(row({"Student": "EXAM INFO", "Class": f"exam={spec.exam_name}",
                     "Sheet ID": f"questions={total_q}", "MC correct": f"choices={spec.num_choices}",
                     "MC possible": "written=" + ";".join(f"{h:g}" for h in spec.written_heights),
                     "MC percent": f"scoring={session.multi_mode}",
                     "Needs review": "ids=" + ";".join(sorted(session.exam_ids))},
                    [""] * total_q))
    rows.append(row({"Student": "ANSWER KEY", "MC correct": total_q, "MC possible": total_q,
                     "MC percent": 100.0},
                    [letters(session.key.answers[q]) for q in questions]))

    for s in session.ordered_students():
        base = {"Student": s.name, "Class": s.class_name, "Sheet ID": s.sheet_id,
                "MC possible": total_q, "Notes": "; ".join(s.notes)}
        if s.answers is None:
            rows.append(row({**base, "Needs review": "Bubble page not scanned"}, [""] * total_q))
            continue
        correct = session.score(s)
        written = {f"Written {n}": s.written_scores.get(n) for n in range(1, n_written + 1)}
        total = correct + sum(w for w in written.values() if w is not None)
        open_flags = sorted(f.question for f in s.open_flags)
        values = {**base, "MC correct": correct, "MC percent": round(100 * correct / total_q, 1),
                  **{k: ("" if v is None else v) for k, v in written.items()},
                  "Total (MC + written)": total,
                  "Needs review": ", ".join(f"Q{q}" for q in open_flags)}
        answers = [letters(s.answers.get(q, frozenset())) + ("?" if q in open_flags else "")
                   for q in questions]
        rows.append(row(values, answers))
    return _csv_bytes(rows)


def item_analysis_csv(session: GradingSession) -> bytes:
    k = session.spec.num_choices
    rows = [["Question", "Correct answer", "% correct", "# correct", "# incorrect", "# blank"]
            + [f"# chose {CHOICE_LETTERS[c]}" for c in range(k)]]
    for it in item_stats(session):
        rows.append([it.question, it.key, round(it.pct_correct, 1), it.correct, it.incorrect,
                     it.blank, *it.choice_counts])
    return _csv_bytes(rows)


# ---------------------------------------------------------------- charts ----

def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def _style(ax, grid_axis: str) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def _title(session: GradingSession, what: str) -> str:
    classes = " / ".join(session.class_names())  # several when periods are combined
    prefix = f"{classes} — " if classes else ""
    return f"{what}\n{prefix}{session.spec.exam_name}"


def score_distribution_png(session: GradingSession) -> bytes:
    students = graded_students(session)
    total_q = len(session.key.answers)
    pcts = [100 * session.score(s) / total_q for s in students]
    bins = list(range(0, 101, 10))
    counts = [0] * 10
    for p in pcts:
        counts[min(int(p // 10), 9)] += 1

    fig, ax = plt.subplots(figsize=(7, 3.8))
    labels = [f"{b}–{b + 9}" if b < 90 else "90–100" for b in bins[:-1]]
    bars = ax.bar(labels, counts, color=SERIES_1, width=0.9, edgecolor=SURFACE, linewidth=2)
    for bar, n in zip(bars, counts):
        if n:
            ax.text(bar.get_x() + bar.get_width() / 2, n, str(n), ha="center", va="bottom",
                    color=TEXT_PRIMARY, fontsize=8)
    ax.set_xlabel("Multiple choice score (%)")
    ax.set_ylabel("Students")
    ax.yaxis.get_major_locator().set_params(integer=True)
    avg = sum(pcts) / len(pcts) if pcts else 0
    ax.set_title(_title(session, f"Score distribution  (n = {len(pcts)}, mean {avg:.0f}%)"))
    _style(ax, "y")
    return _png(fig)


def most_missed_png(session: GradingSession) -> bytes:
    stats = sorted(item_stats(session), key=lambda it: (-it.incorrect, it.question))
    n = len(stats)
    fig, ax = plt.subplots(figsize=(7, max(2.5, 0.22 * n + 1.2)))
    ys = list(range(n))
    ax.barh(ys, [it.incorrect for it in stats], color=SERIES_1, height=0.75,
            edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(ys, [f"Q{it.question}" for it in stats])
    ax.invert_yaxis()  # most missed at the top
    total = len(graded_students(session))
    for y, it in zip(ys, stats):
        ax.text(it.incorrect + total * 0.01, y, f"{it.incorrect} missed · "
                f"{it.pct_correct:.0f}% correct · key {it.key}",
                va="center", color=TEXT_SECONDARY, fontsize=7.5)
    ax.set_xlim(0, max(max((it.incorrect for it in stats), default=0), 1) * 1.6)
    ax.set_xlabel("Students who answered incorrectly or left it blank")
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_title(_title(session, "Most missed questions"))
    _style(ax, "x")
    return _png(fig)


def choice_distribution_png(session: GradingSession) -> bytes:
    stats = item_stats(session)
    k = session.spec.num_choices
    n = len(stats)

    # Letters A-H get their own color; I and J (rare) share "Other".
    def color(c: int) -> str:
        return CATEGORICAL[c] if c < len(CATEGORICAL) else OTHER

    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.24 * n + 1.6)))
    for y, it in enumerate(stats):
        left = 0.0
        segments = [(c, cnt) for c, cnt in enumerate(it.choice_counts)] + [(-1, it.blank)]
        # Students may mark several bubbles on multi-answer questions, so each
        # row is scaled to its own total number of marks (+ blanks) = 100%.
        row_total = max(sum(cnt for _, cnt in segments), 1)
        for c, cnt in segments:
            if not cnt:
                continue
            width = 100 * cnt / row_total
            fill = NEUTRAL if c < 0 else color(c)
            ax.barh(y, width, left=left, height=0.72, color=fill,
                    edgecolor=SURFACE, linewidth=1.5)
            # Direct label (the letter) on segments wide enough to hold it.
            if c >= 0 and width >= 7:
                is_key = CHOICE_LETTERS[c] in it.key
                ax.text(left + width / 2, y, CHOICE_LETTERS[c] + ("✓" if is_key else ""),
                        ha="center", va="center", fontsize=7,
                        color=TEXT_PRIMARY if fill in LIGHT_FILLS else "#ffffff",
                        fontweight="bold" if is_key else "normal")
            left += width
        ax.text(101.5, y, f"key {it.key}", va="center", fontsize=7.5, color=TEXT_SECONDARY)
    ax.set_yticks(range(n), [f"Q{it.question}" for it in stats])
    ax.invert_yaxis()
    ax.set_xlim(0, 118)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("Share of answers marked for each question (✓ = correct answer)")
    handles = [Patch(color=color(c), label=CHOICE_LETTERS[c]) for c in range(min(k, 8))]
    if k > 8:
        handles.append(Patch(color=OTHER, label="/".join(CHOICE_LETTERS[8:k])))
    handles.append(Patch(color=NEUTRAL, label="Blank"))
    ax.legend(handles=handles, ncol=len(handles), loc="lower left", bbox_to_anchor=(0, 1.0),
              frameon=False, fontsize=8, handlelength=1.2, columnspacing=1.0)
    ax.set_title(_title(session, "Answer choice distribution"), pad=24)
    _style(ax, "x")
    return _png(fig)


CHARTS = {
    "score_distribution": score_distribution_png,
    "most_missed": most_missed_png,
    "choice_distribution": choice_distribution_png,
}


def all_exports_zip(session: GradingSession) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("results.csv", results_csv(session))
        z.writestr("item_analysis.csv", item_analysis_csv(session))
        for name, fn in CHARTS.items():
            z.writestr(f"{name}.png", fn(session))
    return buf.getvalue()
