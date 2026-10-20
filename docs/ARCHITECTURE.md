# Fault-tolerant architecture: floorplans, gadget rules, parallel execution, Shor

**Goal.** Compile the whole execution of a fault-tolerant Shor's algorithm onto a QCCD
machine built from verified gadgets, on a floorplan the user designs, with every gadget
working in parallel wherever nothing conflicts, and with every rule the composition obeys
written down as an assertion a reader can check.

This document is the specification. It extends the verified place library and the Logical
Algorithm town (`docs/GADGETS.md` §9–§10); the device-level rule language it reuses is
`docs/rule-language.md`. Started 2026-10-20 on the `compiler` branch.

---

## 1 · The four layers, and what is checked at each

| layer | what it is | what is checked | how |
|---|---|---|---|
| **place** | one verified gadget: a device, ports, ops (`leaves/places.py`) | each op as hardware (the device rules) and as logic (flows, fault distance) | once per master |
| **gate** | a composite gate written in the algorithm language from places (`gate t q: …`) | its body implements its contract, in every branch | once per definition (flat sign-off) |
| **floorplan** | where the places stand and how roads, junctions and wires join them | the floorplan rules (§3): footprints, roads, doors, regions, wiring | once per floorplan |
| **execution** | a program scheduled on a floorplan: events, carries, messages | the execution rules (§4): interface, capacity, ownership, non-interference, causality | once per program |

The claim for a whole program is compositional: if every place op is verified, every gate
definition implements its contract, the floorplan and the execution obey their rules, and
every gate call in the program is its definition's instruction sequence (renamed), then the
program implements its logical circuit gate for gate. The logical circuit itself is then
checked against the algorithm's specification at the logical level (§6), where it is small.
The flat sign-off (`logic/flat.py`) stays as the end-to-end check for programs whose
branches can be enumerated, and as the check for gate definitions.

---

## 2 · The floorplan language

A floorplan is a text file (`*.floor`). Every place of the machine is placed by hand, in
model units, and every road is drawn: nothing about the layout is fixed by the tool.

```ebnf
floorplan = "floorplan", name, [title], NL, { line } ;
line      = "code", name
          | "region", kind, name, [title]                    (* following places belong to it *)
          | "place", name, master, "at", num, num            (* master or category word *)
          | "junction", name, "at", num, num
          | "road", end, "--", end, [ "via", point, { ",", point } ], [ "as", name ]
          | "wire", end, "->", end, [ "latency", num ]
          | "wires", "auto"                                  (* every measuring place to the decoder, … *)
          | "param", name, "=", value ;
end       = name, ".", port          (* a place's door, or a junction's arm n/e/s/w *)
          | name ;                    (* a junction: the arm is chosen by direction *)
kind      = "memory" | "compute" | "factory" | "supply" | "control" | "traffic" ;
```

* A **region** groups places by what they are for. `memory` holds logical zones; `compute`
  holds the places that act on blocks (preparation, syndrome extraction, transversal gates,
  lattice surgery, readout); `factory` holds magic-state factories and injection places;
  `supply` holds ion depots; `control` holds the decoder and the classical memory;
  `traffic` names a set of junctions (a bus). The region a place sits in is a statement the
  floorplan rules check (F9).
* A **road** is a conveyor channel. Its length in sites is its Manhattan route length less
  one, exactly as for every composite (`designs.Designer`). A road may end on a place's
  door or on a junction arm.
* A **junction** is a `junc9`: four arms, it passes one bundle at a time.
* A **wire** carries bits, not ions; its one cost is its latency.

`town.floor` is the Logical Algorithm town of GADGETS.md §9 written in this language;
`qccd.gadget.floorplan.load()` turns any floorplan into the same `Library` composite the
town builder made.

---

## 3 · The floorplan rules

Each rule is an assertion in the rule language (`docs/rule-language.md`) over the words of
§5, evaluated on the floorplan alone (no program).

| id | rule |
|---|---|
| F1 | every road joins two ion ports that accept each other (kind, role, width, code, direction) |
| F2 | every ion door of a place is on exactly one road; a junction arm is on at most one |
| F3 | footprints do not overlap |
| F4 | a road passes through no place it does not end at, and two roads cross only at a junction |
| F5 | a road a bundle may wait on holds a bundle: every road ending at a door is at least as long as the door is wide |
| F6 | every out door reaches every in door it may have to serve (places are endpoints, junctions are the only thoroughfares) |
| F7 | every place that measures is wired to a decoder, every place that reports an outcome to a classical memory, and every place that can be told to run is wired from one |
| F8 | the decoder's frame reaches the classical memory |
| F9 | every place stands in a region of its kind |

---

## 4 · The execution rules

The G-rules of GADGETS.md §8–§10 (G1–G11) become assertions, and three more state what
parallel execution must respect. An execution is the GIR of a scheduled program.

| id | family | rule |
|---|---|---|
| G1 | interface | ports on a road agree; every bundle arrives in the order its place takes it in |
| G2 | ownership | every ion leaves only from where it is; residents end at home; loaded ions end dumped or in a living block |
| G3 | capacity | no place ever holds more ions than its capacity |
| G4 | capacity | no road ever holds more ions than its sites, nor carries two ways at once |
| G5 | exclusivity | a place runs one op at a time |
| G6 | physics | no ion gets hotter than the ion-loss limit |
| G7 | composition | every instruction is realised, and each block's instructions run in program order |
| G8 | physics | no block goes longer than its coherence budget without syndrome extraction |
| G9 | contract | every op used is verified (a classical op is modeled) |
| G10 | causality | every classical bit is used after it exists |
| G11 | classical | every syndrome is decoded, the decoder keeps up, nothing reads an outcome before it is decoded |
| G12 | ownership | every bundle that travels is taken in at the far end, by exactly one op: no ion is left on a road |
| G13 | non-interference | a road carries one bundle at a time, from its first ion in to its last taken off |
| G14 | traffic | a bundle waits only where the whole bundle fits: on the road in front of a door |

G5 and G13 are the rules that make parallelism safe: a place runs one op at a time and a road
carries one bundle at a time, so a magic-state factory producing while a lattice surgery runs
elsewhere is legal exactly when no place, junction or road is asked to do two things at
once; G2 follows every ion, so no ion is ever in two of them. G1–G11, written as text, give
the place model's own checks' verdicts on every example, sequential and parallel, and on
every mutation (`tests/test_gadget_architecture.py`); G12 catches a bundle nobody takes in,
which those checks never looked at.

---

## 5 · The vocabulary (machine level)

The rule language is the device-level one; the machine level adds an execution kind,
`machine`, built from a floorplan and (optionally) a GIR. Its words:

| word | meaning |
|---|---|
| `places`, `junctions`, `roads`, `wires`, `doors`, `regions` | the floorplan's parts |
| `family(p)`, `region(p)`, `master(p)`, `capacity(x)` | what a place is, where it stands, how many ions a place or road holds |
| `length(r)`, `ends(r)`, `door_of(r)`, `width(d)`, `dir(d)`, `accepts(d1, d2)` | roads and doors |
| `overlaps(p, q)`, `passes_through(r, p)`, `crosses(r1, r2)`, `reachable(d1, d2)`, `wired(p, s)` | geometry and connectivity |
| `blocks`, `instructions`, `events`, `carries`, `messages`, `windows` | the execution |
| `place(e)`, `road(c)`, `instruction(x)`, `ions(x)`, `start(x)`, `end(x)`, `overlap(x, y)` | where and when |
| `occupancy(x, t)`, `holder(b, t)`, `owner(i, t)` | state at an instant |
| `resources(x)` | the places, junctions, roads and ions an activity holds while it runs |
| `traffic()`, `ion_sites(c)`, `wait(c)` | how much shuttling an execution does |

---

## 6 · Composite gates, and the logical check

The algorithm language (`algorithm.py`) gains **gate definitions**:

```
gate t q:                 # T on q, by teleportation
  inject m
  zz q m -> a
  read m X -> b
  decode
  if a: s q
  if b: z q
```

A definition's body is ordinary instructions over its parameters and local blocks; its
outcomes are local. A call `t q3` expands to the body with fresh names. Guards nest: a
guarded call guards every instruction of the body that can be guarded, and a condition is
then a conjunction of parities (`if m & a: s q`). A definition is verified once, by the flat
sign-off of its body against its contract (`contract T q`); a call is then a single
logical gate. Composite gates shipped: `t`, `tdg`, `sx` (√X̄ by X̄X̄ surgery against |Ȳ⟩),
`h` (S̄·√X̄·S̄), `ccx` (Toffoli, 7 T), `cswap`.

The logical check of a program then simulates its logical circuit (one qubit per block,
every gate call its contract) exactly, branch by branch over the program's own outcomes.

---

## 7 · Parallel execution

`city.py` scheduled one instruction after another, and one road trip after another. The
parallel scheduler keeps program order as the order instructions are *issued*, and places
each as early as its dependencies and its resources allow:

* every resource -- place, junction, road, zone -- has a calendar of leases; a lease may
  be open-ended (a block resting in a zone until its next use is issued) and is closed
  when that use is scheduled;
* a trip waits only where a bundle fits (its door roads); between them it runs as a green
  wave, reserved hop by hop, and the whole wave moves later if any hop is leased;
* a magic state is produced while the computation runs: `inject` has no dependency on any
  block, so the factory starts as soon as it is free and the state rests in a zone until
  it is consumed;
* the classical half is unchanged: a decode works alongside the ions.

The **traffic** of an execution -- ion-sites shuttled, junction passes, time bundles wait on
roads -- is reported with every schedule, and is what a floorplan is designed to reduce.

---

## 8 · Shor

`shor15.alg`: order finding for N = 15, a = 7, with one control block recycled three times
(Kitaev's iterative phase estimation, the form Monz et al. ran on trapped ions, Science
351:1068, 2016), the work register in four blocks, the semiclassical inverse QFT as
classically controlled S̄† and T̄† corrections, and every Toffoli as seven T gates from the
factories. Its expected output: the first bit is 0 and the last two are uniform, so the
phase is s/4 and the order is 4, and gcd(7² ± 1, 15) = 3, 5.

---

## 9 · Phases

| | deliverable | state |
|---|---|---|
| A1 | machine vocabulary; G1–G11 as rules, equal to `place_checks.py` on every example and mutation | ✅ `qccd/ruleset/machine.py`, `library/machine.rules`; 0.05 s per example |
| A2 | floorplan language, `town.floor`, F1–F9 | ✅ `qccd/gadget/floorplan.py`; the town round-trips exactly; seven broken floorplans each fail their rule |
| A3 | parallel scheduler, G12–G14, traffic | ✅ `city.py` `parallel=True`; every example passes F1–F9 and G1–G14 and signs off; `magic_teleport` 791 → 490 ms |
| A4 | gate definitions, nested guards, `sx`/`h`/`t`/`tdg`/`ccx`/`cswap`, contracts | |
| A5 | `shor15.alg` on a Shor floorplan, compositional sign-off, logical check | |
| A6 | the design page: floorplans, traffic, parallel lanes | |
| A7 | the tutorial's gadget parts | |
