# Company source gaps and fallbacks

Every company here is wanted but has no working source with a verified company identity. Each entry
gives the specific reason. Update this list on every expansion pass: remove a company when a source
goes live, and add every company that was probed and missed.

Robotics, mechanical, aerospace and automotive companies come first, because those are the search's
priority. Last updated 2026-10-22, when the Freehire fallback was removed entirely at the user's
request (it is not used for any company). 45 of its 54 companies, and AT&T, now read their own
boards (see [collection-verification.md](collection-verification.md)); the other nine are listed
below with their reasons. The board-catalog pass on 2026-10-18 moved 70 companies to direct sources.

No aggregator or LinkedIn-based source is used. Tesla answers only a real browser, so the
`tesla-relay` source reads Tesla's own internships, which the `extension/tesla-relay` Chrome
extension reads in the user's Chrome and commits to the `tesla-feed` branch; see
[operator-runbook.md](operator-runbook.md).

## 1. Careers site refuses ordinary requests

| Company | What the provider answers | Note |
| --- | --- | --- |
| Verizon | Its job API (`mycareer.verizon.com/api/jobs`) answers a Cloudflare challenge (HTTP 403) | A real browser reads it, so the Tesla relay approach would work |
| TSMC | Avature career site answers HTTP 403 | Also no Avature adapter |
| SAP | `jobs.sap.com` answers HTTP 403 and says current openings are still on the classic SuccessFactors page (`career5.successfactors.eu`, `career_company=SAP`) | The classic page has no career-site search the adapter reads |
| HCA Healthcare | Careers site answers HTTP 403 | Not a priority |
| DENSO Robotics | The main DENSO careers site answers HTTP 403 | DENSO North America is already collected by `denso-oracle` |

## 2. No US roles

| Company | Note |
| --- | --- |
| CMR Surgical | Its job search lists only UK, India and Brazil locations |
| StoneX | The careers page is a talent-network sign-up with no public job board |

## 3. Reachable job system, but no adapter for it yet

| Job system | Priority companies | Others |
| --- | --- | --- |
| SuccessFactors tile layout (no result total, so completeness cannot be judged) | KUKA, CNH Industrial, TE Connectivity, Ferrari | |
| SuccessFactors without a career-site search | BMW Group | American Airlines |
| Kenexa BrassRing | General Atomics | Archer Daniels Midland |
| Paycor (listing built by JavaScript) | Vecna Robotics, Neocis | |
| Teamtailor | Starship Technologies (1 posting) | |
| Gem | Chef Robotics (1 posting) | |
| Personio | Cartken (offices named without a country) | |
| Factorial | THEKER Robotics | |
| Y Combinator Work at a Startup (no ATS behind it) | Ultra (warehouse robots) | |
| Custom server-rendered job list (no JSON API) | | AbbVie, Progressive |
| Beesite / Jobylon / Dayforce | Porsche / Einride / Amphenol (a UK unit) | |
| Eightfold with no derivable list endpoint | none | Chevron, Citigroup, American Express, Capital One, Morgan Stanley |

Jibe also serves Arm, PepsiCo and State Farm, which wait for the Fortune 500 pass.

## 4. Board found, but not usable as is

| Company | Problem |
| --- | --- |
| Xwing | The careers page links to a Greenhouse board that answers HTTP 404. |
| Realtime Robotics | freehire lists Workable postings, but the account name behind them could not be identified. |
| Kratos Defense | The only board found, on Paylocity, belongs to "Kratos Industries"; it could not be confirmed as Kratos Defense. |
| TransDigm | Only subsidiary ADP sites (such as `whippany`) were found, not a group board. |
| Hyundai Motor Group | Its SuccessFactors site lists European postings only. |
| JPMorgan Chase | Its Oracle board holds 7,318 postings. A keyword search matches description text, so it cannot narrow the board. Deferred, since JPMorgan isn't a priority. |
| Mitsubishi Electric, Daifuku | Covered by one board each (Mitsubishi Electric US `meus-mea`; Daifuku Intralogistics America). Their second boards, MEPPI (HRM Direct) and Wynright (iCIMS), wait, because the registry keeps one source per company. |
| General Dynamics | Only Electric Boat is covered. GDIT has a public Workday board but is IT services; no board was found for the other units. |

## 5. No public board found (probed 2026-10-14, twice on 2026-10-15, and in freehire's catalog on 2026-10-18)

These companies have no board under any guessed name on the providers the monitor reads, and
freehire's catalog of 294,000 companies has none either, or only aggregator reposts.

- **Robotics:** Aurora Innovation, Covariant, GreyOrange, Unitree, AGIBOT, Pudu Robotics, Geek+,
  Kawasaki Robotics, Sarcos, Vention, Electric Sheep, Monarch Tractor, Hello Robot, Moon Surgical,
  Vicarious Surgical. From the 2026-10-20 list: Booster Robotics, X Square Robot, EngineAI, LimX
  Dynamics, ROBOTERA, Sharpa, CenoBots, August Robotics, AITAN eyesAtop and Mecka (freehire has
  only aggregator reposts of Mecka). The Chinese companies recruit through domestic systems such as
  Moka and Feishu, which the monitor does not read.
- **Aerospace and defense:** Huntington Ingalls, Spirit AeroSystems.
- **Automotive and mobility:** Kia, BYD, Renault, Mazda, Geely, SAIC, Chery, Plus, Luminar, Canoo,
  Dana, AGCO, Volvo Cars.
- **Industrial:** Siemens (only its Healthineers and Energy subsidiaries have boards), Parker
  Hannifin, Ingersoll Rand, Garmin (only a Romanian office's Workable board), Seagate.

## Reachable, but deferred to a later pass

Their public boards answer, but they are less central to robotics and mechanical work, so they wait
for the Fortune 500 pass: Thermo Fisher Scientific, Danaher, Hewlett Packard Enterprise, Leidos,
GDIT (General Dynamics), NXP, Arm, PepsiCo, State Farm.

## Rejected look-alike boards

These board names belong to a different company, so they are not used: `figure` (Figure Lending),
`archer` (a veterinary clinic; Archer Aviation's board is `archer56`), `radiant` (an AI cloud
company, not Radiant Nuclear), `slate` (on Lever, the Slate podcast publisher; on Ashby, an
unrelated video-software company), `sila` (Sila Services, an HVAC company, not Sila
Nanotechnologies), `monogram` (an AI design startup, not Monogram Orthopaedics), `persona` on Ashby
(Persona the identity-verification company; Persona AI's board is `persona.ai`), `emerson` on
Workday (Emerson College), Vention's `join.ventionteams.com` and Teamtailor site (Vention the
software outsourcer, not Vention the robotics company), and the Kia and Mazda boards in freehire's
catalog (car dealerships), `ultra` on Ashby (a nicotine-pouch brand, not Ultra the robotics
company), and `genesis-molecular-ai` on Ashby (a drug-discovery company; Genesis AI's board is
`genesis`). Allen Control Systems also has a Greenhouse board (`allencontrolsystems`), but nothing
on it has changed since March 2026; its Ashby board is the live one.
