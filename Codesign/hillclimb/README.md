# Hillclimb · agent-designed devices for five architecture boards

Teams of Claude Code agents searched for trapped-ion QCCD **devices** with the lowest `T_jones` on
five qccd.academy architecture boards, one board at a time, between 2026-10-18 and 2026-10-19. They
ran on Opus 5.5, except for their first four minutes on `surface17` (05:37 to 05:41 UTC), which ran
on Opus 5. Those minutes made 37 of the board's 81 grades, including those of two of its seven
picks, `bingran-agent-4` and `-6`. This folder holds the best devices of every board, the numbers
of every new distinct device they graded that passed this climb's audit, and a script that grades
them again.

**Every device in the `results.csv` files, and every live design that could be rebuilt, was
re-graded with the toolchain production runs since 2026-10-19 20:30 UTC**: the Python of QCCD
`972d8bf`, compiler `dbd13d8952f3` (the published Linux build, sha256 `9897b5f6…`) and qcheck built
from `972d8bf` (sha256 `4c78a9d5…`, the same bytes as production's). The search itself ran on
compiler `c7c535380bfb`, since withdrawn. Each board's `results.csv` keeps both numbers. Numbers
from the agents' probes, marked as such, are from the search under `c7c535380bfb` and were not
re-graded. Nothing has been submitted to the official leaderboard.

| board | best here | best live design, graded the same way | faster by | the device |
|---|---:|---:|---:|---|
| [`surface17@1`](surface17/) | **1.585 ms** | 2.045 ms (`stationary_chain`) | 1.29× | 5 traps on a junction-free cycle, direct wiring, 120 DACs |
| [`bb144@1`](bb144/) | **96.74 ms** | 417.89 ms (`grid13x13 broadcast`) | 4.32× | 168 traps on a junction-free cycle declared as one loop, broadcast wiring, 46 DACs |
| [`steane@1`](steane/) | **1.44 ms** | 1.825 ms (`stationary_chain`) | 1.27× | 5 traps on a junction-free cycle, direct wiring, 120 DACs |
| [`five_qubit@1`](five_qubit/) | **1.62 ms** | 1.815 ms (`stationary_chain`) | 1.12× | 6 traps on a junction-free cycle, broadcast wiring, 33 DACs |
| [`rep9@1`](rep9/) | **1.305 ms** | 1.99 ms (`stationary_chain`) | 1.52× | a 20-site race track: gate-only traps between readout-only sites, 34 DACs |

"Best live design" is the fastest design on the board's qccd.academy page (2026-10-19 23:33 UTC)
that could be rebuilt here and graded with the same flow. The page's numbers replay the study's own
schedules. For most designs these match the reference compiler, but the dock rings take longer here.
On `bb144` the page shows dock rings down to 24.93 ms, and under production's compiler they take over
a second. It also lists `grid12x12 a72` at 305.70 ms, which cannot be rebuilt here (and whose name
suggests 72 ancillas, where the board's circuit uses 24); 96.74 ms is 3.16× faster than that too. See
[`bb144/`](bb144/).

---

## Layout

```
Codesign/hillclimb/
  README.md         this page
  grade.py          grade device files on a board, as `qccd submit --local` does
  <board>/
    README.md       the winning device, why it is fast, the top designs and the live designs
    chart.png       every graded design that passed the audit, ranked beside the live designs, as on the board's page
    designs/        the best distinct devices, up to 10 (surface17 also has its 7 picks), as .arch.json
    results.csv     every new distinct device the agents graded that passed the audit, both numbers, eligibility and audit
```

To grade them again, use yezhuoyang/QCCD's `compiler` branch at `972d8bf` with this folder copied
in. `grade.py` needs `qccd.workspace`, which `main` does not have, and this fork's `compiler` branch
does not contain `972d8bf`. From the root of a checkout that has `Codesign/hillclimb`:

```bash
git fetch https://github.com/yezhuoyang/QCCD compiler
git worktree add ../qccd-compiler 972d8bf
cp -R Codesign/hillclimb ../qccd-compiler/Codesign/
cd ../qccd-compiler
pip install -e '.[agent]'                  # `qccd`, and stim, qiskit and numpy for the semantics stage
qccd toolchain install                     # compiler dbd13d8952f3, on Linux or Windows (x86-64) only
(cd Compiler/lean/checker && lake build)   # qcheck
python Codesign/hillclimb/grade.py rep9@1 Codesign/hillclimb/rep9/designs/*.arch.json
```

At `972d8bf`, `qccd toolchain install` installs no checker on any platform, and no compiler on
macOS. Without qcheck the required `lean_certificate` stage is `unsupported`, so every design comes
out not eligible. `lake build` needs Lean's `elan`, which fetches the pinned
`leanprover/lean4:v4.29.0-rc2`. On macOS, build the compiler from source too: OCaml 5.3 with opam,
`opam install dune yojson.2.2.2`, then `dune build bin/qccdc_cli.exe` in `Compiler/ocaml`, whose tree
at `972d8bf` is `dbd13d8952f3`. The evaluator takes `QCCD_QCCDC` and `QCCD_QCHECK` if they are set,
then a build in the checkout, then what `qccd toolchain install` installed.

## How the search ran

| board | agents | hours | grades | new distinct devices |
|---|---:|---:|---:|---:|
| surface17 | 12 | 3 | 81 | 66 |
| bb144 | 12 | 3 | 90 | 78 |
| steane | 4 | 1 | 11 | 11 |
| five_qubit | 4 | 1 | 7 | 7 |
| rep9 | 2 | 1 | 3 | 3 |

Each agent got the board's circuit, the live board's numbers, one strategy to start from (dock
rings, junction networks, cooling, measurement batching, a lower bound, a search engine, …) and a
shared board of everyone's graded designs. A design counted only if it was **eligible** under the
reference evaluator (all ten stages, the same code path as `qccd submit --local`) **and** passed
this climb's own audit:

1. **Only devices.** The program always comes from the reference compiler (`auto`); no edited
   programs, no placement passed in.
2. **Honest split/merge.** A movement class declares at least the `entails` the shipped generators
   declare for that class id; a new class id entails both split and merge.
3. **Locked budget.** At most 128 DACs and 200 junctions (the evaluator does not check this; the
   audit does). `control.model = "direct"` pays direct wiring: at least 24 DACs per trap and 48 per
   junction. No segment joins two nodes at the same position.
4. **Physics unchanged.** The primitives, heating, species and budget blocks are the board's.

Agents could probe as much as they liked (a local copy of the compiler, or `hc-grade --no-log`,
which grades without logging), but only grades logged on the shared board count. On `surface17`, two
of them failed the audit: a06's direct-wired `grid(2,3)` and `grid(3,3)`, at 168 and 288 DACs. They
are among the 81 grades but not the 66 devices, and were not re-graded. Anything that lowered
`T_jones` by exploiting the grader rather than improving the device was written up and, with two
exceptions, not logged: one loop through junctions on [`bb144`](bb144/), and a line whose rails pass
over other traps on [`five_qubit`](five_qubit/) (#2). On `five_qubit`, a02 also argued that the
winner relies on coarse hop labels (only the signs of dx and dy), which let hops at different angles
share one waveform; a04 counted it as honest. Each board's README lists what its agents found.

## What carried over between boards

- **No junctions.** Every winner is junction-free. A junction crossing costs 100–120 µs and +3
  quanta, which soon forces a 300 µs cool; a hop along a cycle costs 5 µs and +0.1 quanta.
- **The general router's fixed part.** It emits one measure instruction per ASAP layer of the
  circuit as written, and one MS instruction per layer, unless a layer has more gates than there
  are gate traps (R12); then the layer splits into extra rounds. On a device with enough gate traps,
  the state-prep cool, gates and readout are fixed, and the device only changes transport and the
  cools that transport causes. On `surface17` that floor is 1.505 ms + 5 µs per transport
  instruction. On `steane` the agents derived `T_jones ≥ 1.400 ms` for any device.
- **Document order is a lever.** The placer and the meeting-point choice break ties by node and
  segment order in the device file, so the agents searched that order. The geometry, wiring and
  physics do not change, but the compiled schedule does. On `bb144`, listing the data traps first
  took the cycle from 134.6 ms (a search-time probe, never graded) to 110.6 ms, and order search took
  it from 103.1 to 96.74 ms. Read these numbers as "this device, as written, under the reference
  compiler".
- **Leaving the general router.** On `rep9` the general router cannot route a loop of capacity-1
  sites, and the rotation pass declines (no docks), so the compiler falls back to the race-track pass.
  With gate traps that cannot measure, that pass holds every readout back and reads all 8 ancillas
  in one 120 µs instruction instead of 8. This is the only board where leaving the router paid.

## What the new compiler changed

`dbd13d8952f3` never reorders a qubit's gates, commuting or not, its conveyor sweep is greedy, and
R10 checks each qubit's order. None of the winners moved. What moved are the designs that relied on
the rotation pass commuting gates into shared batches:

| board | design | at search | now |
|---|---|---:|---:|
| bb144 | 144-slot dock ring with 24 aligned docks (the conveyor family's best) | 118.57 ms | 896.28 ms |
| bb144 | the live page's answer, `ring(143,3,36)` | 480.43 ms | 1,092.95 ms |
| rep9 | dock ring `ring(5,2,10)`, docks at rails 1..8 | 2.06 ms | 15.18 ms |

All 165 new devices are still eligible and audited under the new toolchain. "New" excludes the
agents' re-grades of live designs, which were matched by device and by program.

## Caveats

- **Split and merge are not charged on `shuttle`.** The router moves ions with the seed's `shuttle`
  class, which entails no split or merge, so an ion joining or leaving a multi-ion chain pays neither.
  The designs here rely on this convention, and so do the live ones. Under honest split/merge, a
  search-time probe by `surface17`'s a02 put its own 1.595 ms 4-cycle at 7.18 ms (10 cools, and a
  program that breaks R14, so it would be refused) and `cyclone_base` at 23.5 ms. Neither number
  was re-graded.
- **The audit is this climb's.** The official evaluator checks neither the DAC budget nor direct-wiring
  cost. All designs here pass both, but the official board would accept designs that do not.
- **Ties.** Many devices tie at the top. Ties are ordered by the search grade and then by time,
  which carries no meaning.
