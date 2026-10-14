# Customer brief: alarm triage for CNC maintenance

*A one-page framing written the way an FDE would before building anything. The plant is fictional; the problems are the ones real machining sites have.*

## Who the user is

A maintenance engineer responsible for 8 CNC machining centres across two sites. They get paged for alarms, write work orders, own preventive maintenance (PM) scheduling, and report recurring problems to the plant manager.

## The problem

Machines raise hundreds of alarms a month. Most are routine (tool wear, door interlock), a few signal real, developing faults. Today, working out *why* an alarm keeps coming back means:

1. exporting alarm history from the machine monitoring system,
2. searching work orders in the CMMS, where notes are short, inconsistent and in mixed English and Chinese,
3. checking the PM schedule and tooling records separately,
4. looking up the alarm code in the manual.

This takes one to several hours per issue, so it only happens for the loudest problems. In practice, recurring alarms get **reset rather than investigated**: operators clear them and production continues until something breaks.

## What "good" looks like

- An engineer can ask in plain language ("why does M03 keep overheating?") and get an initial analysis in under a minute.
- Every claim points to a record they can check (work order, PM plan entry, tool change, alarm count). The engineer stays the decision-maker; the tool shortens the search.
- The tool says clearly when the data cannot answer the question instead of producing a confident guess.
- It notices common-mode causes (a site-wide power event, a supplier change) instead of treating each machine separately.

## Constraints

- Read-only. The assistant must never modify maintenance records.
- Works with the data the plant already has: alarm logs, CMMS work orders, PM plan, tool change log, manuals. No new sensors.
- Mixed-language, messy free text is the norm, not an edge case.

## Out of scope for v1

Automatic work order creation, real-time alerting, sensor time series, predictive failure models, multi-plant rollout. See "Taking it to production" in the README.
