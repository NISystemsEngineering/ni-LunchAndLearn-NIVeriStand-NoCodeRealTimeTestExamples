#!/usr/bin/env python3
"""
check_dio_response.py

Verify from a TDMS log that every state change on a digital input channel is
followed by a state change on a digital output channel within a time limit,
and save the result as a report.

Usage:
    python check_dio_response.py <file.tdms>
    python check_dio_response.py <file.tdms> --limit 1.5 \
        --input DUT.DigitalInput --output DUT.DigitalOutput \
        --report my_report.html --csv results.csv

By default the report is saved next to the TDMS file as <name>_report.html.
Give --report a path ending in .txt for a plain-text report instead, or pass
--no-report to skip it.

Exit code: 0 = PASS, 1 = FAIL, 2 = could not run (missing channel, no data...)

Requires: pip install nptdms numpy matplotlib   (matplotlib is optional: without
it the HTML report is written without the plot)
"""
import argparse
import base64
import csv
import datetime as dt
import html
import io
import os
import sys

import numpy as np
from nptdms import TdmsFile


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def find_channel(tdms, name, group=None):
    """Return (group, channel) for the channel called `name`, searching every group unless one is given."""
    groups = [tdms[group]] if group else tdms.groups()
    for g in groups:
        for ch in g.channels():
            if ch.name == name:
                return g, ch
    raise KeyError(f"Channel '{name}' not found" + (f" in group '{group}'" if group else ""))


def time_axis(group, channel, n, time_channel):
    """Build a time vector in seconds starting at 0.

    Use the group's time channel (e.g. VeriStand's 'Absolute Time') if it has
    one; otherwise fall back to the channel's waveform increment.
    """
    if time_channel:
        try:
            t = group[time_channel][:n].astype(float)
            if len(t) == n:
                return t - t[0], f"'{time_channel}' channel"
        except KeyError:
            pass
    inc = channel.properties.get("wf_increment") or group.properties.get("Group dt")
    if not inc:
        raise ValueError("No time channel and no wf_increment; cannot build a time axis")
    return np.arange(n) * float(inc), f"wf_increment = {inc} s"


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def edges(t, x, threshold):
    """Return a list of (time, index, old_state, new_state) for each change of state."""
    state = (np.asarray(x, dtype=float) > threshold).astype(int)
    idx = np.flatnonzero(np.diff(state)) + 1
    return [(float(t[i]), int(i), int(state[i - 1]), int(state[i])) for i in idx]


def analyze(t, din, dout, limit, threshold, require_same_polarity):
    in_edges = edges(t, din, threshold)
    out_edges = edges(t, dout, threshold)
    t_end = float(t[-1])

    results = []
    used = set()  # each output change may answer only one input change
    for t_in, _, old, new in in_edges:
        match = None
        for k, (t_out, _, o_old, o_new) in enumerate(out_edges):
            if k in used or t_out < t_in:
                continue
            if t_out - t_in > limit:
                break
            if require_same_polarity and (o_old, o_new) != (old, new):
                continue
            match = k
            break

        row = {"input_time_s": round(t_in, 6), "input_change": f"{old}->{new}"}
        if match is not None:
            used.add(match)
            t_out, _, o_old, o_new = out_edges[match]
            row.update(output_time_s=round(t_out, 6),
                       output_change=f"{o_old}->{o_new}",
                       delay_s=round(t_out - t_in, 6),
                       result="PASS")
        else:
            # If the recording ends before the window closes, we can't tell.
            verdict = "INCONCLUSIVE" if t_end - t_in < limit else "FAIL"
            row.update(output_time_s="", output_change="", delay_s="", result=verdict)
        results.append(row)

    unmatched_out = [out_edges[k] for k in range(len(out_edges)) if k not in used]
    return results, in_edges, out_edges, unmatched_out


def summarize(results, in_edges, unmatched_out, strict_output):
    delays = [r["delay_s"] for r in results if r["result"] == "PASS"]
    n_fail = sum(r["result"] == "FAIL" for r in results)
    n_inc = sum(r["result"] == "INCONCLUSIVE" for r in results)
    if not in_edges:
        overall = "INCONCLUSIVE"
    elif n_fail or (strict_output and unmatched_out):
        overall = "FAIL"
    else:
        overall = "PASS"
    return {
        "overall": overall,
        "n_pass": len(results) - n_fail - n_inc,
        "n_fail": n_fail,
        "n_inconclusive": n_inc,
        "n_total": len(results),
        "delay_min": min(delays) if delays else None,
        "delay_max": max(delays) if delays else None,
        "delay_mean": float(np.mean(delays)) if delays else None,
    }


# --------------------------------------------------------------------------
# Text output (console and .txt report)
# --------------------------------------------------------------------------
def text_report(ctx):
    s, a = ctx["summary"], ctx["args"]
    L = []
    L.append("DIGITAL I/O RESPONSE TIME TEST REPORT")
    L.append("=" * 72)
    L.append(f"Result:        {s['overall']}  ({s['n_pass']}/{s['n_total']} input changes answered "
             f"within {a.limit} s)")
    L.append(f"Report date:   {ctx['generated']}")
    L.append(f"File:          {a.tdms_file}")
    if ctx["recorded"]:
        L.append(f"Recorded:      {ctx['recorded']}")
    L.append(f"Input:         {a.input}")
    L.append(f"Output:        {a.output}")
    L.append(f"Samples:       {ctx['n']}  ({ctx['duration']:.3f} s, dt ~ {ctx['dt']:.4f} s "
             f"from {ctx['t_src']})")
    L.append(f"Requirement:   output changes within {a.limit} s of each input change")
    L.append(f"Resolution:    +/- {ctx['dt']:.3f} s (one sample)")
    L.append(f"Options:       threshold {a.threshold}, same polarity "
             f"{'required' if a.require_same_polarity else 'not required'}, "
             f"spontaneous output changes {'fail' if a.strict_output else 'reported only'}")
    L.append(f"Changes:       input {len(ctx['in_edges'])}, output {len(ctx['out_edges'])}")
    if s["delay_min"] is not None:
        L.append(f"Delay:         min {s['delay_min']:.3f} s, max {s['delay_max']:.3f} s, "
                 f"mean {s['delay_mean']:.3f} s")
    for note in ctx["notes"]:
        L.append(f"Note:          {note}")
    L.append("")
    L.append(f"{'#':>3}  {'Input t (s)':>11}  {'In':>4}  {'Output t (s)':>12}  {'Out':>4}  "
             f"{'Delay (s)':>9}  Result")
    for i, r in enumerate(ctx["results"], 1):
        out_t = f"{r['output_time_s']:.3f}" if r["output_time_s"] != "" else "-"
        delay = f"{r['delay_s']:.3f}" if r["delay_s"] != "" else "-"
        L.append(f"{i:>3}  {r['input_time_s']:>11.3f}  {r['input_change']:>4}  {out_t:>12}  "
                 f"{r['output_change'] or '-':>4}  {delay:>9}  {r['result']}")
    L.append("")
    L.append(f"OVERALL: {s['overall']}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------
def make_plot_png(ctx):
    """Return the plot as base64 PNG, or None if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    t, din, dout, a = ctx["t"], ctx["din"], ctx["dout"], ctx["args"]
    thr = a.threshold
    di = (np.asarray(din, float) > thr).astype(int)
    do = (np.asarray(dout, float) > thr).astype(int)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 5.6), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 2]})
    ax1.step(t, di + 1.5, where="post", color="#2563eb", lw=1.4, label=a.input)
    ax1.step(t, do, where="post", color="#ea580c", lw=1.4, label=a.output)
    ax1.set_yticks([0, 1, 1.5, 2.5])
    ax1.set_yticklabels(["0", "1", "0", "1"])
    ax1.set_ylim(-0.3, 2.8)
    ax1.set_ylabel("State")
    ax1.legend(loc="upper right", frameon=False, ncol=2, fontsize=9)
    ax1.set_title("Signals (input offset above output)", fontsize=10, loc="left")

    res = ctx["results"]
    ok = [(r["input_time_s"], r["delay_s"]) for r in res if r["result"] == "PASS"]
    bad = [r["input_time_s"] for r in res if r["result"] != "PASS"]
    if ok:
        ax2.plot(*zip(*ok), "o", ms=4, color="#16a34a", label="Response delay")
    for x in bad:
        ax2.axvline(x, color="#dc2626", lw=1, alpha=0.7)
    ax2.axhline(a.limit, color="#dc2626", ls="--", lw=1.2, label=f"Limit {a.limit} s")
    top = max([a.limit] + [d for _, d in ok]) * 1.2
    ax2.set_ylim(0, top)
    ax2.set_ylabel("Delay (s)")
    ax2.set_xlabel("Time (s)")
    ax2.legend(loc="lower right", frameon=False, fontsize=9)
    ax2.set_title("Response delay at each input change"
                  + ("  (red lines = no response)" if bad else ""), fontsize=10, loc="left")
    for ax in (ax1, ax2):
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def html_report(ctx):
    s, a = ctx["summary"], ctx["args"]
    e = html.escape
    color = {"PASS": "#15803d", "FAIL": "#b91c1c", "INCONCLUSIVE": "#a16207"}[s["overall"]]

    def fmt(v, nd=3):
        return "-" if v in ("", None) else f"{v:.{nd}f}"

    info = [
        ("File", a.tdms_file),
        ("Recorded", ctx["recorded"] or "-"),
        ("Report generated", ctx["generated"]),
        ("Input channel", a.input),
        ("Output channel", a.output),
        ("Requirement", f"Output changes within {a.limit} s of each input change"),
        ("Samples", f"{ctx['n']} ({ctx['duration']:.3f} s, dt ≈ {ctx['dt']:.4f} s from {ctx['t_src']})"),
        ("Timing resolution", f"± {ctx['dt']:.3f} s (one sample)"),
        ("Logic threshold", str(a.threshold)),
        ("Same polarity", "Required" if a.require_same_polarity else "Not required"),
        ("Spontaneous output changes", "Fail the test" if a.strict_output else "Reported only"),
    ]
    info_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in info)

    stats = [
        ("Input changes", len(ctx["in_edges"])),
        ("Output changes", len(ctx["out_edges"])),
        ("Passed", s["n_pass"]),
        ("Failed", s["n_fail"]),
        ("Inconclusive", s["n_inconclusive"]),
        ("Min delay (s)", fmt(s["delay_min"])),
        ("Max delay (s)", fmt(s["delay_max"])),
        ("Mean delay (s)", fmt(s["delay_mean"])),
    ]
    stat_cards = "".join(f"<div class='stat'><div class='v'>{v}</div><div class='k'>{e(k)}</div></div>"
                         for k, v in stats)

    notes = "".join(f"<li>{e(n)}</li>" for n in ctx["notes"])
    notes_html = f"<h2>Notes</h2><ul>{notes}</ul>" if notes else ""

    png = make_plot_png(ctx)
    plot_html = (f"<h2>Signals</h2><img alt='Signal plot' src='data:image/png;base64,{png}'>"
                 if png else "<p><em>Plot omitted (matplotlib not installed).</em></p>")

    rows = []
    for i, r in enumerate(ctx["results"], 1):
        cls = r["result"].lower()
        rows.append(
            f"<tr class='{cls}'><td>{i}</td><td>{fmt(r['input_time_s'])}</td>"
            f"<td>{e(r['input_change'])}</td><td>{fmt(r['output_time_s'])}</td>"
            f"<td>{e(r['output_change'] or '-')}</td><td>{fmt(r['delay_s'])}</td>"
            f"<td class='res'>{r['result']}</td></tr>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DIO Response Report – {e(os.path.basename(a.tdms_file))}</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; color:#1f2937;
        background:#fff; max-width:1100px; margin:24px auto; padding:0 16px; line-height:1.4; }}
 h1 {{ font-size:22px; margin:0 0 4px; }}
 h2 {{ font-size:16px; margin:28px 0 8px; border-bottom:1px solid #e5e7eb; padding-bottom:4px; }}
 .sub {{ color:#6b7280; font-size:13px; }}
 .verdict {{ margin:18px 0; padding:14px 18px; border-radius:8px; color:#fff; background:{color};
            font-size:20px; font-weight:600; }}
 .verdict span {{ font-weight:400; font-size:14px; opacity:.95; display:block; margin-top:2px; }}
 table {{ border-collapse:collapse; width:100%; font-size:13px; }}
 .info th {{ text-align:left; width:220px; color:#4b5563; font-weight:500; }}
 .info th, .info td {{ padding:4px 8px; border-bottom:1px solid #f3f4f6; vertical-align:top; }}
 .stats {{ display:flex; flex-wrap:wrap; gap:10px; }}
 .stat {{ border:1px solid #e5e7eb; border-radius:8px; padding:8px 14px; min-width:110px; }}
 .stat .v {{ font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }}
 .stat .k {{ font-size:12px; color:#6b7280; }}
 img {{ max-width:100%; border:1px solid #e5e7eb; border-radius:6px; }}
 .tx th, .tx td {{ padding:4px 10px; text-align:right; border-bottom:1px solid #f3f4f6;
                  font-variant-numeric:tabular-nums; }}
 .tx th {{ background:#f9fafb; position:sticky; top:0; }}
 .tx td.res {{ text-align:left; font-weight:600; }}
 tr.pass td.res {{ color:#15803d; }}
 tr.fail {{ background:#fef2f2; }}  tr.fail td.res {{ color:#b91c1c; }}
 tr.inconclusive {{ background:#fefce8; }}  tr.inconclusive td.res {{ color:#a16207; }}
 @media print {{ .tx th {{ position:static; }} body {{ margin:0; }} }}
</style></head><body>
<h1>Digital I/O Response Time Test Report</h1>
<div class="sub">{e(a.input)} → {e(a.output)} &nbsp;·&nbsp; limit {a.limit} s</div>
<div class="verdict">{s['overall']}
 <span>{s['n_pass']} of {s['n_total']} input changes were followed by an output change within {a.limit} s</span></div>
<h2>Test information</h2><table class="info">{info_rows}</table>
<h2>Summary</h2><div class="stats">{stat_cards}</div>
{notes_html}
{plot_html}
<h2>Transitions</h2>
<table class="tx"><thead><tr><th>#</th><th>Input t (s)</th><th>Input</th><th>Output t (s)</th>
<th>Output</th><th>Delay (s)</th><th style="text-align:left">Result</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>
"""


def write_report(path, ctx):
    content = text_report(ctx) + "\n" if path.lower().endswith((".txt", ".log")) else html_report(ctx)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tdms_file")
    p.add_argument("--input", default="DUT.DigitalInput", help="input channel name")
    p.add_argument("--output", default="DUT.DigitalOutput", help="output channel name")
    p.add_argument("--group", default=None, help="TDMS group (default: search all)")
    p.add_argument("--limit", type=float, default=1.5, help="max allowed response time, s (default 1.5)")
    p.add_argument("--threshold", type=float, default=0.5, help="logic threshold (default 0.5)")
    p.add_argument("--time-channel", default="Absolute Time", help="time channel name ('' to use wf_increment)")
    p.add_argument("--require-same-polarity", action="store_true",
                   help="output must change in the same direction as the input (0->1 answers 0->1)")
    p.add_argument("--strict-output", action="store_true",
                   help="also FAIL if the output changes without a preceding input change")
    p.add_argument("--report", help="report file path (.html or .txt); default <tdms name>_report.html")
    p.add_argument("--no-report", action="store_true", help="don't save a report")
    p.add_argument("--csv", help="also write per-transition results to this CSV file")
    args = p.parse_args()

    try:
        tdms = TdmsFile.read(args.tdms_file)
        g_in, ch_in = find_channel(tdms, args.input, args.group)
        _, ch_out = find_channel(tdms, args.output, args.group)
        din, dout = ch_in[:], ch_out[:]
        n = min(len(din), len(dout))
        if n < 2:
            raise ValueError("Not enough samples to analyse")
        din, dout = din[:n], dout[:n]
        t, t_src = time_axis(g_in, ch_in, n, args.time_channel)
    except Exception as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 2

    results, in_edges, out_edges, unmatched_out = analyze(
        t, din, dout, args.limit, args.threshold, args.require_same_polarity)
    summary = summarize(results, in_edges, unmatched_out, args.strict_output)

    notes = []
    if not in_edges:
        notes.append("The input never changed state, so the requirement could not be tested.")
    if unmatched_out:
        notes.append(f"{len(unmatched_out)} output change(s) with no matching input change at t = "
                     + ", ".join(f"{x[0]:.3f}" for x in unmatched_out) + " s.")
    if summary["n_inconclusive"]:
        notes.append(f"{summary['n_inconclusive']} input change(s) occurred within {args.limit} s of the "
                     "end of the recording, so their response could not be checked.")
    if len(ch_in) != len(ch_out):
        notes.append(f"Channel lengths differ ({len(ch_in)} vs {len(ch_out)} samples); "
                     f"analysed the first {n}.")

    created = tdms.properties.get("Created")
    ctx = {
        "args": args, "t": t, "din": din, "dout": dout, "n": n, "t_src": t_src,
        "dt": float(np.median(np.diff(t))), "duration": float(t[-1]),
        "results": results, "in_edges": in_edges, "out_edges": out_edges,
        "unmatched_out": unmatched_out, "summary": summary, "notes": notes,
        "recorded": str(created).replace("T", " ")[:19] + " (UTC)" if created is not None else "",
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(text_report(ctx))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["input_time_s", "input_change", "output_time_s",
                                              "output_change", "delay_s", "result"])
            w.writeheader()
            w.writerows(results)
        print(f"CSV written to {args.csv}")

    if not args.no_report:
        path = args.report or os.path.splitext(args.tdms_file)[0] + "_report.html"
        try:
            write_report(path, ctx)
            print(f"Report written to {path}")
        except OSError as ex:
            print(f"ERROR: could not write report: {ex}", file=sys.stderr)

    return {"PASS": 0, "FAIL": 1}.get(summary["overall"], 2)


if __name__ == "__main__":
    sys.exit(main())
